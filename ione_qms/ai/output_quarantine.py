from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any

import frappe
from frappe.utils import now_datetime

from ione_qms.ai.output_safety import ai_content_privacy_violation
from ione_qms.ai.privacy_runtime import task_known_identifiers

OUTPUT_SAFETY_FLAG = "ione_ai_output_safety_violation"
QUARANTINED_OUTPUT = "[IONE aggregate report output quarantined]"
SANITIZED_FAILURE_TYPE = "AggregateOutputSafetyViolation"
OUTPUT_SAFETY_ERROR_PREFIX = f"GovernedExecutionFailed:{SANITIZED_FAILURE_TYPE}:"
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_QUARANTINE_RECEIPT = re.compile(
	rf"^{re.escape(OUTPUT_SAFETY_ERROR_PREFIX)}([A-Z][A-Z0-9_]{{0,79}}):([0-9]{{1,2}})$"
)
_MAX_DRAFTS_PER_TASK = 10
_MAX_OUTPUT_MESSAGES = 1_000
_MAX_OUTPUT_SURFACE_CHARACTERS = 1_000_000
_INCIDENT_RECEIPT_FIELDS = (
	"incident_key",
	"incident_type",
	"severity",
	"status",
	"detected_at",
	"detected_by",
	"flow_agent",
	"policy",
	"agent_release",
	"analysis_task",
	"flow_run",
	"summary",
	"details",
	"patient_data_involved",
	"external_notification_required",
	"containment_actions",
)


def flag_aggregate_output_violation(task: str, reason_code: str) -> None:
	"""Raise a request-local, content-free signal for the governed outer transaction."""
	task_name = str(task or "").strip()
	code = str(reason_code or "").strip()
	if not task_name or not _SAFE_CODE.fullmatch(code):
		code = "OUTPUT_SAFETY_SIGNAL_INVALID"
	payload = {"task": task_name, "reason_code": code}
	existing = getattr(frappe.flags, OUTPUT_SAFETY_FLAG, None)
	if existing and existing != payload:
		payload = {"task": task_name, "reason_code": "OUTPUT_SAFETY_SIGNAL_CONFLICT"}
	setattr(frappe.flags, OUTPUT_SAFETY_FLAG, payload)


def read_aggregate_output_violation(task: str) -> str | None:
	"""Return only the stable reason code; malformed or cross-task signals fail closed."""
	payload = getattr(frappe.flags, OUTPUT_SAFETY_FLAG, None)
	if payload is None:
		return None
	if (
		not isinstance(payload, dict)
		or set(payload) != {"task", "reason_code"}
		or str(payload.get("task") or "") != str(task or "")
		or not _SAFE_CODE.fullmatch(str(payload.get("reason_code") or ""))
	):
		return "OUTPUT_SAFETY_SIGNAL_INVALID"
	return str(payload["reason_code"])


def aggregate_flow_run_output_violation(run, task) -> str | None:
	"""Scan every model-controlled Flow output surface for patient-identity leakage."""
	known_identifiers = task_known_identifiers(task)
	for fieldname in ("output", "tool_calls", "questions", "error"):
		violation = _output_surface_violation(
			run.get(fieldname),
			known_identifiers=known_identifiers,
		)
		if violation:
			return violation
	rows = frappe.get_all(
		"Flow Session Message",
		filters={"run": run.name, "role": ["in", ["assistant", "tool"]]},
		fields=["role", "content", "tool_calls", "tool_call_id"],
		order_by="idx asc, name asc",
		limit_page_length=_MAX_OUTPUT_MESSAGES + 1,
	)
	if len(rows) > _MAX_OUTPUT_MESSAGES:
		return "OUTPUT_SURFACE_LIMIT_EXCEEDED"
	for row in rows:
		for fieldname in ("content", "tool_calls", "tool_call_id"):
			violation = _output_surface_violation(
				row.get(fieldname),
				known_identifiers=known_identifiers,
			)
			if violation:
				return violation
	return None


def quarantine_aggregate_output_run(
	*,
	flow_run: str,
	task: str,
	execution_attempt: int,
	reason_code: str,
) -> int:
	"""Scrub Flow output and remove only the draft bound to this exact run attempt."""
	run_name = str(flow_run or "")
	task_name = str(task or "")
	attempt = int(execution_attempt or 0)
	code = str(reason_code or "")
	if not run_name or not task_name or attempt < 1:
		frappe.throw("Aggregate output quarantine attempt provenance is invalid.")
	if not _SAFE_CODE.fullmatch(code):
		code = "OUTPUT_SAFETY_SIGNAL_INVALID"
	lineage = frappe.db.get_value(
		"Flow Run",
		run_name,
		["reference_doctype", "reference_name"],
		as_dict=True,
	)
	if (
		not lineage
		or str(lineage.get("reference_doctype") or "") != "IONE AI Analysis Task"
		or str(lineage.get("reference_name") or "") != task_name
	):
		frappe.throw("Aggregate output quarantine Flow Run lineage is invalid.")

	rows = frappe.get_all(
		"IONE AI Report Draft",
		filters={
			"task": task_name,
			"execution_attempt": attempt,
			"flow_run": run_name,
		},
		fields=["name", "status", "reviewed_by", "reviewed_at"],
		order_by="creation asc, name asc",
		limit_page_length=_MAX_DRAFTS_PER_TASK + 1,
	)
	if len(rows) > _MAX_DRAFTS_PER_TASK:
		frappe.throw("Aggregate output quarantine encountered an unexpected draft count.")
	removed = 0
	for row in rows:
		name = str(row.get("name") or "")
		if not name:
			continue
		if str(row.get("status") or "") != "Draft" or row.get("reviewed_by") or row.get("reviewed_at"):
			frappe.throw("Aggregate output quarantine refused to remove reviewed evidence.")
		frappe.db.delete(
			"IONE AI Report Draft",
			{
				"name": name,
				"task": task_name,
				"execution_attempt": attempt,
				"flow_run": run_name,
			},
		)
		removed += 1

	frappe.db.sql(
		(
			"update `tabFlow Session Message` "
			"set content = %s, tool_calls = null, tool_call_id = null "
			"where run = %s and role in ('assistant', 'tool')"
		),
		(QUARANTINED_OUTPUT, run_name),
	)
	frappe.db.set_value(
		"Flow Run",
		run_name,
		{
			"status": "Failed",
			"output": QUARANTINED_OUTPUT,
			"tool_calls": _quarantined_tool_calls(code, removed),
			"questions": None,
			"error": f"{OUTPUT_SAFETY_ERROR_PREFIX}{code}:{removed}",
		},
		update_modified=False,
	)
	# Flow publishes its Running row before the model call. Publish only the
	# scrubbed terminal state before later linking or incident writes can fail.
	frappe.db.commit()
	return removed


def aggregate_output_quarantine_receipt(run) -> tuple[str, int] | None:
	"""Verify and decode a content-free receipt used by orphan reconciliation."""
	error = str(run.get("error") or "")
	if not error.startswith(OUTPUT_SAFETY_ERROR_PREFIX):
		return None
	match = _QUARANTINE_RECEIPT.fullmatch(error)
	if not match:
		frappe.throw("Aggregate output quarantine receipt is malformed.")
	code = match.group(1)
	removed = int(match.group(2))
	raw_tool_calls = run.get("tool_calls")
	try:
		tool_calls = json.loads(raw_tool_calls) if isinstance(raw_tool_calls, str) else raw_tool_calls
	except TypeError, ValueError:
		tool_calls = None
	expected_tool_calls = json.loads(_quarantined_tool_calls(code, removed))
	if (
		str(run.get("status") or "") != "Failed"
		or str(run.get("output") or "") != QUARANTINED_OUTPUT
		or run.get("questions")
		or tool_calls != expected_tool_calls
	):
		frappe.throw("Aggregate output quarantine Flow Run evidence is invalid.")
	unsafe_message_count = int(
		frappe.db.sql(
			(
				"select count(*) "
				"from `tabFlow Session Message` "
				"where run = %s and role in ('assistant', 'tool') "
				"and (coalesce(content, '') != %s "
				"or coalesce(tool_calls, '') != '' "
				"or coalesce(tool_call_id, '') != '')"
			),
			(run.name, QUARANTINED_OUTPUT),
		)[0][0]
	)
	if unsafe_message_count:
		frappe.throw("Aggregate output quarantine message evidence is invalid.")
	return code, removed


def append_aggregate_output_incident(
	*,
	task,
	policy,
	agent,
	run,
	attempt: int,
	token_hash: str,
	reason_code: str,
	quarantined_draft_count: int,
) -> str:
	"""Append one deterministic, content-free containment receipt inside finalization."""
	code = str(reason_code or "")
	if not _SAFE_CODE.fullmatch(code):
		code = "OUTPUT_SAFETY_SIGNAL_INVALID"
	run_name = str(run.name) if run is not None else ""
	identity = {
		"analysis_task": str(task.name),
		"execution_attempt": int(attempt),
		"execution_token_hash": str(token_hash),
		"flow_run": run_name or None,
		"reason_code": code,
	}
	incident_key = _hash(identity)
	existing = frappe.db.get_value(
		"IONE Agent Incident",
		incident_key,
		[*_INCIDENT_RECEIPT_FIELDS, "record_hash"],
		as_dict=True,
	)
	if existing:
		expected_record_hash = _hash(
			{fieldname: existing.get(fieldname) for fieldname in _INCIDENT_RECEIPT_FIELDS}
		)
		if (
			str(existing.get("analysis_task") or "") != str(task.name)
			or str(existing.get("flow_run") or "") != run_name
			or str(existing.get("policy") or "") != str(policy.name)
			or str(existing.get("incident_type") or "") != "Patient Data Exposure"
			or not hmac.compare_digest(
				str(existing.get("record_hash") or ""),
				expected_record_hash,
			)
		):
			frappe.throw("Existing aggregate output safety incident does not match its receipt.")
		return incident_key

	detected_at = now_datetime()
	agent_release = frappe.db.get_value(
		"IONE Agent Release",
		{
			"policy": policy.name,
			"flow_agent": agent.name,
			"status": "Approved",
		},
		"name",
		order_by="approved_at desc",
	)
	values: dict[str, Any] = {
		"doctype": "IONE Agent Incident",
		"incident_key": incident_key,
		"incident_type": "Patient Data Exposure",
		"severity": "High",
		"status": "Contained",
		"detected_at": detected_at,
		"detected_by": policy.get("service_user"),
		"flow_agent": agent.name,
		"policy": policy.name,
		"agent_release": agent_release,
		"analysis_task": task.name,
		"flow_run": run_name or None,
		"summary": "Governed AI output safety gate blocked direct-identifier content.",
		"details": (
			f"Stable reason code: {code}. No model output or direct identifier was retained. "
			"Privacy/security staff must determine whether external notification is required."
		),
		"patient_data_involved": 1,
		"external_notification_required": 0,
		"containment_actions": (
			"Unsafe artifact persistence was blocked; the Flow output and tool trace were "
			f"quarantined; {int(quarantined_draft_count)} unreviewed draft(s) from this "
			"attempt were removed; the governed task was failed."
		),
	}
	values["record_hash"] = _hash({key: value for key, value in values.items() if key != "doctype"})
	doc = frappe.get_doc(values)
	doc.insert(ignore_permissions=True)
	return str(doc.name)


def _quarantined_tool_calls(reason_code: str, removed_draft_count: int) -> str:
	return json.dumps(
		[
			{
				"id": "quarantined",
				"name": "ione_create_report_draft",
				"arguments": {
					"quarantined": 1,
					"reason_code": str(reason_code),
					"removed_draft_count": int(removed_draft_count),
				},
			}
		],
		sort_keys=True,
		separators=(",", ":"),
	)


def _output_surface_violation(
	value: Any,
	*,
	known_identifiers: tuple[str, ...],
) -> str | None:
	if value is None:
		return None
	if isinstance(value, str):
		text = value
	else:
		try:
			text = json.dumps(
				value,
				ensure_ascii=False,
				sort_keys=True,
				separators=(",", ":"),
				default=str,
			)
		except TypeError, ValueError:
			return "OUTPUT_SURFACE_UNSERIALIZABLE"
	if len(text) > _MAX_OUTPUT_SURFACE_CHARACTERS:
		return "OUTPUT_SURFACE_LIMIT_EXCEEDED"
	violation = ai_content_privacy_violation(
		text,
		known_identifiers=known_identifiers,
	)
	if violation:
		return violation
	if not isinstance(value, str):
		return None
	try:
		decoded = json.loads(value)
	except TypeError, ValueError:
		return None
	if isinstance(decoded, str) and decoded == value:
		return None
	try:
		decoded_text = json.dumps(
			decoded,
			ensure_ascii=False,
			sort_keys=True,
			separators=(",", ":"),
			default=str,
		)
	except TypeError, ValueError:
		return "OUTPUT_SURFACE_UNSERIALIZABLE"
	if len(decoded_text) > _MAX_OUTPUT_SURFACE_CHARACTERS:
		return "OUTPUT_SURFACE_LIMIT_EXCEEDED"
	return ai_content_privacy_violation(
		decoded_text,
		known_identifiers=known_identifiers,
	)


def _hash(value: Any) -> str:
	return hashlib.sha256(
		json.dumps(
			value,
			ensure_ascii=False,
			sort_keys=True,
			separators=(",", ":"),
			default=str,
		).encode()
	).hexdigest()
