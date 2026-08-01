from __future__ import annotations

from typing import Any

import frappe

from ione_qms.constants import QWEN_FLOW_MODEL


@frappe.whitelist(methods=["POST"])
def start_run(
	input: str,
	agent: str | None = None,
	session: str | None = None,
	model: str | None = None,
	attachments: list[str] | str | None = None,
	stream: bool | str = False,
):
	"""Preserve ordinary Flow chat while denying every native route into IONE assets."""
	if _request_targets_ione(agent=agent, session=session, model=model):
		_reject_native_flow_entry()
	from flow.api.api import start_run as flow_start_run

	return flow_start_run(
		input=input,
		agent=agent,
		session=session,
		model=model,
		attachments=attachments,
		stream=stream,
	)


@frappe.whitelist(methods=["POST"])
def resume_run(run_name: str, answers: dict[str, Any] | str, stream: bool | str = False):
	if _run_is_governed(run_name):
		frappe.throw(
			"Governed IONE runs may only be resumed through the accountable IONE tool-approval API.",
			frappe.PermissionError,
		)
	from flow.api.api import resume_run as flow_resume_run

	return flow_resume_run(run_name=run_name, answers=answers, stream=stream)


@frappe.whitelist(methods=["POST"])
def stop_run(run_name: str) -> dict[str, str]:
	if _run_is_governed(run_name):
		frappe.throw(
			"Governed IONE runs cannot be changed through the native Flow API.",
			frappe.PermissionError,
		)
	from flow.api.api import stop_run as flow_stop_run

	return flow_stop_run(run_name=run_name)


@frappe.whitelist(methods=["POST"])
def recover_session(session: str) -> dict[str, int]:
	if _session_is_governed(session):
		frappe.throw(
			"Governed IONE sessions cannot be recovered through the native Flow API.",
			frappe.PermissionError,
		)
	from flow.api.api import recover_session as flow_recover_session

	return flow_recover_session(session=session)


@frappe.whitelist(methods=["POST"])
def submit_feedback(
	run_name: str,
	rating: str,
	comment: str | None = None,
) -> dict[str, Any]:
	if _run_is_governed(run_name):
		frappe.throw(
			"Governed IONE run review is recorded in IONE audit artifacts, not native Flow feedback.",
			frappe.PermissionError,
		)
	from flow.api.api import submit_feedback as flow_submit_feedback

	return flow_submit_feedback(run_name=run_name, rating=rating, comment=comment)


@frappe.whitelist(methods=["GET", "POST"])
def get_agent_tools(agent: str) -> dict[str, bool]:
	if _agent_is_governed(agent):
		frappe.throw(
			"Governed IONE Agent metadata is not exposed through the native Flow API.",
			frappe.PermissionError,
		)
	from flow.api.api import get_agent_tools as flow_get_agent_tools

	return flow_get_agent_tools(agent=agent)


def validate_ione_flow_session(doc, method: str | None = None) -> None:
	"""Prevent manually-created or repurposed sessions from binding IONE assets."""
	del method
	agent = str(doc.get("agent") or "")
	model = str(doc.get("model") or "")
	if not (
		(agent and (_agent_is_governed(agent) or _agent_uses_governed_model(agent)))
		or (model and _model_is_governed(model))
	):
		return
	_require_controlled_runtime_context(agent)


def validate_ione_flow_run(doc, method: str | None = None) -> None:
	"""Require every governed Flow Run write to remain inside one pinned IONE task."""
	del method
	session = (
		frappe.db.get_value(
			"Flow Session",
			doc.get("session"),
			["agent", "model"],
			as_dict=True,
		)
		if doc.get("session")
		else None
	)
	agent = str(session.get("agent") or "") if session else ""
	governed = bool(
		doc.get("reference_doctype") == "IONE AI Analysis Task"
		or (agent and (_agent_is_governed(agent) or _agent_uses_governed_model(agent)))
		or (session and session.get("model") and _model_is_governed(str(session.get("model"))))
	)
	if not governed:
		return
	_require_controlled_runtime_context(agent)
	task_name = str(getattr(frappe.flags, "ione_ai_task", None) or "")
	if doc.get("reference_doctype") and doc.get("reference_doctype") != "IONE AI Analysis Task":
		frappe.throw("Governed IONE Flow Runs must reference an IONE AI Analysis Task")
	if doc.get("reference_name") and str(doc.get("reference_name")) != task_name:
		frappe.throw("Governed IONE Flow Run reference does not match its active task")


def _request_targets_ione(
	*,
	agent: str | None,
	session: str | None,
	model: str | None,
) -> bool:
	if model and _model_is_governed(model):
		return True
	if session:
		return _session_is_governed(session)
	if agent:
		return _agent_is_governed(agent) or _agent_uses_governed_model(agent)
	# The default Assistant is intentionally resolved here as Flow resolves it,
	# so a Qwen model cannot be reached by omitting both agent and model.
	try:
		from flow.assistant import ASSISTANT_AGENT_TITLE

		return _agent_is_governed(ASSISTANT_AGENT_TITLE) or _agent_uses_governed_model(ASSISTANT_AGENT_TITLE)
	except ImportError:
		return False


def _run_is_governed(run_name: str) -> bool:
	if not run_name or not frappe.db.exists("Flow Run", run_name):
		return False
	row = frappe.db.get_value(
		"Flow Run",
		run_name,
		["session", "reference_doctype", "reference_name"],
		as_dict=True,
	)
	if not row:
		return False
	if row.get("reference_doctype") == "IONE AI Analysis Task":
		return True
	if frappe.db.exists("IONE Flow Run Link", {"flow_run": run_name}):
		return True
	return _session_is_governed(str(row.get("session") or ""))


def _session_is_governed(session_name: str) -> bool:
	if not session_name or not frappe.db.exists("Flow Session", session_name):
		return False
	row = frappe.db.get_value(
		"Flow Session",
		session_name,
		["agent", "model"],
		as_dict=True,
	)
	if not row:
		return False
	agent = str(row.get("agent") or "")
	model = str(row.get("model") or "")
	return bool(
		(agent and (_agent_is_governed(agent) or _agent_uses_governed_model(agent)))
		or (model and _model_is_governed(model))
	)


def _agent_is_governed(agent: str) -> bool:
	if not agent:
		return False
	if str(agent).startswith("IONE "):
		return True
	return bool(
		frappe.db.exists(
			"IONE Agent Policy",
			{"flow_agent": agent, "status": ["in", ["Draft", "Active", "Retired"]]},
		)
	)


def _agent_uses_governed_model(agent: str) -> bool:
	model = frappe.db.get_value("Flow Agent", agent, "model")
	return bool(model and _model_is_governed(str(model)))


def _model_is_governed(model: str) -> bool:
	if model == QWEN_FLOW_MODEL:
		return True
	return bool(
		frappe.db.exists(
			"IONE Agent Policy",
			{
				"flow_agent": [
					"in",
					frappe.get_all(
						"Flow Agent",
						filters={"model": model},
						pluck="name",
						limit_page_length=500,
					)
					or ["__none__"],
				]
			},
		)
	)


def _reject_native_flow_entry() -> None:
	frappe.throw(
		"IONE clinical agents and the governed Qwen model may only run from an "
		"authorized IONE AI Analysis Task. Native Flow chat, model overrides, "
		"sessions, and attachments are blocked.",
		frappe.PermissionError,
	)


def _require_controlled_runtime_context(agent: str) -> None:
	task_name = str(getattr(frappe.flags, "ione_ai_task", None) or "")
	policy_name = str(getattr(frappe.flags, "ione_ai_policy", None) or "")
	user = str(frappe.session.user or "")
	if not task_name or not policy_name or user in {"", "Guest", "Administrator"}:
		frappe.throw(
			"Governed IONE Flow documents may only be written by a policy service user "
			"inside an active IONE task.",
			frappe.PermissionError,
		)
	task = frappe.db.get_value(
		"IONE AI Analysis Task",
		task_name,
		["policy"],
		as_dict=True,
	)
	policy = frappe.db.get_value(
		"IONE Agent Policy",
		policy_name,
		["flow_agent", "service_user", "status"],
		as_dict=True,
	)
	if (
		not task
		or str(task.get("policy") or "") != policy_name
		or not policy
		or (policy.get("status") != "Active" and not _governed_evaluation_runtime_active())
		or str(policy.get("service_user") or "") != user
		or str(policy.get("flow_agent") or "") != agent
	):
		frappe.throw(
			"Governed IONE Flow runtime context failed its task, policy, service-user, "
			"or Agent binding check.",
			frappe.PermissionError,
		)


def _governed_evaluation_runtime_active() -> bool:
	from ione_qms.ai.evaluation import governed_evaluation_runtime_active

	return governed_evaluation_runtime_active()
