from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

from ione_qms.ai.governance import validate_policy_runtime
from ione_qms.ai.output_quarantine import (
	OUTPUT_SAFETY_FLAG,
	SANITIZED_FAILURE_TYPE,
	quarantine_aggregate_output_run,
	read_aggregate_output_violation,
)
from ione_qms.ai.tool_registry import IONE_FLOW_TOOL_BY_SLUG, assert_ione_tool_integrity
from ione_qms.permissions import require_scope_read
from ione_qms.services.runtime_settings import require_post_migrate_runtime_ready

APPROVAL_TTL_MINUTES = 30
APPROVAL_RUN_LOCK_TIMEOUT_SECONDS = 30
APPROVAL_REVIEW_ROLES = frozenset({"IONE Agent Reviewer", "IONE QC Reviewer", "IONE Medical Affairs"})


def create_tool_approvals(task, policy, run) -> list[str]:
	"""Materialize exact, immutable pending calls from one Paused governed run."""
	if run.get("status") != "Paused":
		return []
	questions = _questions(run)
	call_map = _pending_call_map(run)
	if not questions:
		frappe.throw("Paused governed Flow Run has no approval questions")
	names: list[str] = []
	create_flag = getattr(frappe.flags, "ione_ai_approval_create", False)
	try:
		frappe.flags.ione_ai_approval_create = True
		for question in questions:
			key = str(question.get("key") or "")
			call = call_map.get(key)
			if not call:
				frappe.throw("Paused governed Flow Run question has no matching tool call")
			tool_slug = str(call.get("name") or "")
			spec = IONE_FLOW_TOOL_BY_SLUG.get(tool_slug)
			if not spec or not spec.requires_confirmation:
				frappe.throw("Paused governed Flow Run requested an unregistered or non-confirmation tool")
			assert_ione_tool_integrity({tool_slug})
			arguments_json = _canonical_json(call.get("arguments") or {})
			arguments_hash = hashlib.sha256(arguments_json.encode()).hexdigest()
			question_prompt = str(question.get("prompt") or "")[:20_000]
			question_payload = {
				"run_iteration": int(run.get("iterations") or 0),
				"key": key,
				"prompt": str(question.get("prompt") or ""),
				"options": question.get("options") or [],
				"tool": tool_slug,
				"arguments_hash": arguments_hash,
			}
			question_hash = hashlib.sha256(_canonical_json(question_payload).encode()).hexdigest()
			approval_key = hashlib.sha256(
				f"{task.name}|{run.name}|{int(run.get('iterations') or 0)}|"
				f"{key}|{arguments_hash}|{question_hash}".encode()
			).hexdigest()
			existing = frappe.db.get_value(
				"IONE AI Tool Approval",
				{"approval_key": approval_key},
				"name",
			)
			if existing:
				names.append(existing)
				continue
			doc = frappe.get_doc(
				{
					"doctype": "IONE AI Tool Approval",
					"approval_key": approval_key,
					"task": task.name,
					"policy": policy.name,
					"flow_session": run.session,
					"flow_run": run.name,
					"run_iteration": int(run.get("iterations") or 0),
					"question_key": key,
					"tool_slug": tool_slug,
					"arguments_hash": arguments_hash,
					"arguments_json": arguments_json,
					"question_hash": question_hash,
					"question_prompt_hash": hashlib.sha256(question_prompt.encode()).hexdigest(),
					"question_prompt": question_prompt,
					"hospital": task.get("hospital"),
					"campus": task.get("campus"),
					"department": task.get("department"),
					"ward": task.get("ward"),
					"status": "Pending",
					"requested_at": now_datetime(),
					"expires_at": add_to_date(
						now_datetime(),
						minutes=APPROVAL_TTL_MINUTES,
					),
				}
			)
			doc.insert(ignore_permissions=True)
			names.append(doc.name)
	finally:
		frappe.flags.ione_ai_approval_create = create_flag
	return names


def review_tool_approval(
	approval: str,
	decision: str,
	review_comment: str,
) -> dict[str, Any]:
	user = str(frappe.session.user or "")
	_require_named_reviewer(user)
	if decision not in {"Approve", "Reject"}:
		frappe.throw("Decision must be Approve or Reject")
	comment = str(review_comment or "").strip()
	if len(comment) < 5:
		frappe.throw("Tool approval requires a review comment of at least five characters")
	preview = frappe.get_doc("IONE AI Tool Approval", approval)
	preview.check_permission("read")
	flow_run = str(preview.flow_run or "")
	if not flow_run:
		frappe.throw("AI Tool Approval has no governed Flow Run")
	with _run_advisory_lock(flow_run):
		run = _flow_run_for_update(flow_run)
		doc = frappe.get_doc("IONE AI Tool Approval", approval, for_update=True)
		if str(doc.flow_run or "") != run.name:
			frappe.throw("AI Tool Approval changed its governed Flow Run")
		doc.check_permission("read")
		task = frappe.get_doc("IONE AI Analysis Task", doc.task, for_update=True)
		require_scope_read(
			hospital=task.get("hospital"),
			campus=task.get("campus"),
			department=task.get("department"),
			ward=task.get("ward"),
			user=user,
		)
		if user == task.get("requested_by"):
			frappe.throw("The AI task requester cannot approve its tool calls")
		if doc.status != "Pending":
			frappe.throw("AI Tool Approval has already been reviewed or expired")
		if get_datetime(doc.expires_at) <= now_datetime():
			frappe.throw("AI Tool Approval has expired")
		_assert_current_approval(doc, run=run, task=task)
		flag = getattr(frappe.flags, "ione_ai_approval_review", False)
		try:
			frappe.flags.ione_ai_approval_review = True
			doc.status = "Reviewed"
			doc.decision = "Approved" if decision == "Approve" else "Rejected"
			doc.reviewed_by = user
			doc.reviewed_at = now_datetime()
			doc.review_comment = comment[:2_000]
			doc.save(ignore_permissions=True)
		finally:
			frappe.flags.ione_ai_approval_review = flag
		queued = _enqueue_resume_if_ready(run.name, run=run)
	return {
		"approval": doc.name,
		"status": doc.status,
		"decision": doc.decision,
		"resume_queued": queued,
	}


def queue_resume_for_run(flow_run: str) -> dict[str, Any]:
	_require_named_reviewer(str(frappe.session.user or ""))
	with _run_advisory_lock(flow_run):
		run = _flow_run_for_update(flow_run)
		current = _current_approval_documents(run, for_update=True)
		reviewed = next((doc for doc in current if doc.status == "Reviewed"), None)
		if not reviewed:
			frappe.throw("This Flow Run has no reviewed IONE tool approvals to resume")
		reviewed.check_permission("read")
		queued = _enqueue_resume_if_ready(run.name, run=run, current=current)
	return {"flow_run": run.name, "resume_queued": queued}


def resume_tool_approval_run(
	flow_run: str,
	expected_attempt: int | str | None = None,
) -> dict[str, Any]:
	"""Resume one reviewed pause behind an attempt/token/lease CAS fence."""
	with _run_advisory_lock(flow_run):
		run = _flow_run_for_update(flow_run)
		if run.status != "Paused":
			consumed = _consume_reviewed_terminal_approvals(run)
			if consumed:
				frappe.db.commit()
			return {
				"flow_run": run.name,
				"status": run.status,
				"skipped": True,
				"approvals_consumed": consumed,
			}
		current = _current_approval_documents(run, for_update=True)
		if not current:
			frappe.throw("Paused Flow Run has no current IONE tool approvals")
		task_name = frappe.db.get_value(
			"IONE Flow Run Link",
			{"flow_run": run.name},
			"task",
		) or run.get("reference_name")
		task = frappe.get_doc("IONE AI Analysis Task", task_name, for_update=True)
		policy = frappe.get_doc("IONE Agent Policy", task.policy)
		try:
			fenced_attempt = int(expected_attempt) if expected_attempt is not None else 0
		except TypeError, ValueError:
			fenced_attempt = 0
		next_attempt = int(task.get("execution_attempt") or 0) + 1
		if fenced_attempt < 1 or fenced_attempt != next_attempt:
			return {
				"flow_run": run.name,
				"status": task.status,
				"skipped": True,
				"detail_code": "StaleResumeJobFence",
			}
		if (
			task.flow_run != run.name
			or task.flow_session != run.session
			or run.get("reference_doctype") != "IONE AI Analysis Task"
			or run.get("reference_name") != task.name
		):
			frappe.throw("Paused Flow Run no longer matches its governed IONE task")
		if any(doc.status == "Pending" for doc in current):
			return {"flow_run": run.name, "status": "Awaiting Review", "skipped": True}
		if any(doc.status != "Reviewed" for doc in current):
			frappe.throw("Paused Flow Run contains expired or invalid tool approvals")
		for doc in current:
			_assert_current_approval(doc, run=run, task=task)
		answers = {doc.question_key: "Approve" if doc.decision == "Approved" else "Deny" for doc in current}
		from ione_qms.ai import orchestrator

		claim = orchestrator.claim_resume_attempt(task.name, run.name)
		if claim is None:
			return {
				"flow_run": run.name,
				"status": frappe.db.get_value("IONE AI Analysis Task", task.name, "status"),
				"skipped": True,
			}
		task = claim.task
		agent = frappe.get_doc("Flow Agent", policy.flow_agent)
		original_user = frappe.session.user
		original_task = getattr(frappe.flags, "ione_ai_task", None)
		original_policy = getattr(frappe.flags, "ione_ai_policy", None)
		resumed = None
		failure_type: str | None = None
		output_safety_violation: str | None = None
		original_output_safety_signal = getattr(frappe.flags, OUTPUT_SAFETY_FLAG, None)
		setattr(frappe.flags, OUTPUT_SAFETY_FLAG, None)
		try:
			frappe.flags.ione_ai_task = task.name
			frappe.flags.ione_ai_policy = policy.name
			validate_policy_runtime(policy, task)
			frappe.set_user(policy.service_user)
			from flow.lib.session import load_session

			resumed = load_session(run.session).resume(answers, stream=False)
		except Exception as exc:
			failure_type = type(exc).__name__
		finally:
			output_safety_violation = read_aggregate_output_violation(task.name)
			setattr(frappe.flags, OUTPUT_SAFETY_FLAG, original_output_safety_signal)
			frappe.set_user(original_user)
			frappe.flags.ione_ai_task = original_task
			frappe.flags.ione_ai_policy = original_policy

		if failure_type is not None:
			failed_run = _exact_failed_resume_run(run, task)
			if failed_run is not None:
				output_safety_violation = output_safety_violation or (
					orchestrator._aggregate_output_violation_or_fail_closed(
						failed_run,
						task,
					)
				)
			quarantined_draft_count = 0
			if failed_run is not None and output_safety_violation is not None:
				quarantined_draft_count = quarantine_aggregate_output_run(
					flow_run=failed_run.name,
					task=task.name,
					execution_attempt=claim.attempt,
					reason_code=output_safety_violation,
				)
				failed_run.reload()
			elif failed_run is not None:
				orchestrator._sanitize_failed_run(failed_run.name, failure_type)
				failed_run.reload()
			_consume_approvals(current, "Failed")
			finalized = orchestrator._finalize_attempt(
				claim,
				policy=policy,
				agent=agent,
				run=failed_run,
				status="Failed",
				detail_code=(
					SANITIZED_FAILURE_TYPE if output_safety_violation is not None else f"Resume{failure_type}"
				),
				output_safety_violation=output_safety_violation,
				quarantined_draft_count=quarantined_draft_count,
			)
			if not finalized:
				frappe.db.rollback()
			frappe.log_error(
				title="IONE governed AI approval resume failed",
				message=(
					f"{failure_type}: task {task.name} approval resume failed. "
					"Clinical input and model output were not copied into this log."
				),
				reference_doctype=task.doctype,
				reference_name=task.name,
			)
			return {
				"flow_run": failed_run.name if failed_run is not None else run.name,
				"status": (
					"Failed"
					if finalized
					else frappe.db.get_value("IONE AI Analysis Task", task.name, "status")
				),
				"superseded": not finalized,
			}

		if resumed is None:
			raise RuntimeError("Governed approval resume returned no Flow Run")
		resumed.reload()
		_assert_exact_resumed_run(resumed, task)
		output_safety_violation = output_safety_violation or (
			orchestrator._aggregate_output_violation_or_fail_closed(
				resumed,
				task,
			)
		)
		if output_safety_violation is not None:
			quarantined_draft_count = quarantine_aggregate_output_run(
				flow_run=resumed.name,
				task=task.name,
				execution_attempt=claim.attempt,
				reason_code=output_safety_violation,
			)
			resumed.reload()
			_consume_approvals(current, "Failed")
			finalized = orchestrator._finalize_attempt(
				claim,
				policy=policy,
				agent=agent,
				run=resumed,
				status="Failed",
				detail_code=SANITIZED_FAILURE_TYPE,
				output_safety_violation=output_safety_violation,
				quarantined_draft_count=quarantined_draft_count,
			)
			if not finalized:
				frappe.db.rollback()
			return {
				"flow_run": resumed.name,
				"status": (
					"Failed"
					if finalized
					else frappe.db.get_value("IONE AI Analysis Task", task.name, "status")
				),
				"superseded": not finalized,
				"quarantined": True,
			}
		status, detail_code = orchestrator._task_outcome_for_flow_run(resumed)
		if status == "Completed" and "Deny" in answers.values():
			status = "Rejected"
			detail_code = "HumanDeniedToolCall"
		if resumed.status == "Failed":
			orchestrator._sanitize_failed_run(resumed.name, "FlowRunFailed")
			resumed.reload()
		_consume_approvals(current, resumed.status)
		finalized = orchestrator._finalize_attempt(
			claim,
			policy=policy,
			agent=agent,
			run=resumed,
			status=status,
			detail_code=f"Resume{detail_code}",
		)
		if not finalized:
			frappe.db.rollback()
		return {
			"flow_run": resumed.name,
			"status": (
				status if finalized else frappe.db.get_value("IONE AI Analysis Task", task.name, "status")
			),
			"superseded": not finalized,
		}


def expire_tool_approvals(limit: int = 1_000) -> dict[str, int]:
	require_post_migrate_runtime_ready("expire AI tool approvals")
	cutoff = now_datetime()
	rows = frappe.get_all(
		"IONE AI Tool Approval",
		filters={"status": "Pending", "expires_at": ["<=", cutoff]},
		fields=["name", "flow_run"],
		order_by="expires_at asc",
		limit_page_length=min(max(int(limit or 1_000), 1), 1_000),
	)
	candidates_by_run: dict[str, list[str]] = {}
	for row in rows:
		flow_run = str(row.get("flow_run") or "")
		name = str(row.get("name") or "")
		if flow_run and name:
			candidates_by_run.setdefault(flow_run, []).append(name)

	expired_count = 0
	cancelled_tasks: set[str] = set()
	for flow_run, names in candidates_by_run.items():
		with _run_advisory_lock(flow_run):
			run = _flow_run_for_update(flow_run)
			expired_names: set[str] = set()
			for name in names:
				try:
					doc = frappe.get_doc("IONE AI Tool Approval", name, for_update=True)
				except frappe.DoesNotExistError:
					continue
				if str(doc.flow_run or "") != run.name:
					frappe.throw("AI Tool Approval changed its governed Flow Run")
				if doc.status != "Pending" or get_datetime(doc.expires_at) > cutoff:
					continue
				if _expire_approval(doc):
					expired_names.add(doc.name)
					expired_count += 1
			if not expired_names or run.status != "Paused":
				continue

			current = _current_approval_documents(run, for_update=True)
			if not expired_names.intersection(doc.name for doc in current):
				continue
			task_names = {str(doc.task or "") for doc in current}
			if len(task_names) != 1 or not next(iter(task_names)):
				frappe.throw("Current Flow Run approvals do not have one governed AI task")
			task = frappe.get_doc(
				"IONE AI Analysis Task",
				next(iter(task_names)),
				for_update=True,
			)
			if task.flow_run != run.name or task.flow_session != run.session:
				frappe.throw("Expired Flow Run approval no longer matches its governed AI task")
			if task.status == "Cancelled":
				continue
			if task.status != "Pending Confirmation":
				frappe.throw("Expired Flow Run approval no longer matches its governed AI task")
			task.db_set(
				{
					"status": "Cancelled",
					"completed_at": now_datetime(),
					"error_message": "Tool approval expired before accountable review.",
				},
				update_modified=False,
			)
			cancelled_tasks.add(task.name)
	if expired_count or cancelled_tasks:
		# Expiry/cancellation is a complete governed transition of its own. Make
		# it durable before best-effort reconciliation of unrelated reviewed rows.
		frappe.db.commit()
	reconciled = reconcile_reviewed_tool_approvals(limit=limit)
	return {
		"expired": expired_count,
		"tasks_cancelled": len(cancelled_tasks),
		"reviewed_consumed": reconciled["consumed"],
		"reconciliation_errors": reconciled["errors"],
	}


def reconcile_reviewed_tool_approvals(limit: int = 1_000) -> dict[str, int]:
	"""Consume reviewed rows after a committed resume revision won its CAS.

	This closes the narrow crash window between atomic task/revision finalization
	and the presentation-state update of the approval rows.
	"""
	batch_limit = min(max(int(limit or 1_000), 1), 1_000)
	rows = frappe.get_all(
		"IONE AI Tool Approval",
		filters={"status": "Reviewed"},
		fields=["name", "task", "flow_run", "run_iteration"],
		order_by="reviewed_at asc, name asc",
		limit_page_length=batch_limit,
	)
	by_run: dict[str, list[Any]] = {}
	for row in rows:
		flow_run = str(row.get("flow_run") or "")
		if flow_run:
			by_run.setdefault(flow_run, []).append(row)
	consumed = 0
	errors = 0
	for index, (flow_run, candidates) in enumerate(by_run.items()):
		savepoint = f"ione_ai_approval_reconcile_{index}"
		frappe.db.savepoint(savepoint)
		try:
			with _run_advisory_lock(flow_run):
				run = _flow_run_for_update(flow_run)
				run_iteration = int(run.get("iterations") or 0)
				task_status = str(
					frappe.db.get_value(
						"IONE AI Analysis Task",
						run.get("reference_name"),
						"status",
					)
					or ""
				)
				documents: list[Any] = []
				for row in candidates:
					if (
						run.status == "Paused"
						and int(row.get("run_iteration") or 0) >= run_iteration
						and task_status in {"Pending Confirmation", "Running"}
					):
						continue
					if run.status not in {"Paused", "Completed", "Failed"}:
						continue
					doc = frappe.get_doc(
						"IONE AI Tool Approval",
						row.get("name"),
						for_update=True,
					)
					if doc.status == "Reviewed" and str(doc.flow_run or "") == run.name:
						documents.append(doc)
				if documents:
					resume_status = (
						"Failed"
						if run.status == "Paused" and task_status in {"Failed", "Cancelled", "Rejected"}
						else str(run.status)
					)
					_consume_approvals(documents, resume_status)
					frappe.db.release_savepoint(savepoint)
					frappe.db.commit()
					consumed += len(documents)
				else:
					frappe.db.release_savepoint(savepoint)
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			errors += 1
	return {"consumed": consumed, "errors": errors}


def _enqueue_resume_if_ready(
	flow_run: str,
	*,
	run=None,
	current: list[Any] | None = None,
) -> bool:
	from ione_qms.ai import orchestrator

	run = run or _flow_run_for_update(flow_run)
	if run.name != flow_run or run.status != "Paused":
		return False
	current = current if current is not None else _current_approval_documents(run, for_update=True)
	if not current or any(doc.status != "Reviewed" for doc in current):
		return False
	task_names = {str(doc.get("task") or "") for doc in current}
	if len(task_names) != 1 or not next(iter(task_names)):
		frappe.throw("Current Flow Run approvals do not have one governed AI task")
	task = frappe.get_doc(
		"IONE AI Analysis Task",
		next(iter(task_names)),
		for_update=True,
	)
	if (
		task.status != "Pending Confirmation"
		or str(task.get("flow_run") or "") != run.name
		or str(task.get("flow_session") or "") != str(run.get("session") or "")
	):
		frappe.throw("Current Flow Run approvals no longer match their governed AI task")
	expected_attempt = int(task.get("execution_attempt") or 0) + 1
	job_id = orchestrator.resume_job_id(flow_run, expected_attempt)
	frappe.enqueue(
		"ione_qms.ai.approvals.resume_tool_approval_run",
		queue=orchestrator.get_ai_queue(),
		enqueue_after_commit=True,
		job_id=job_id,
		deduplicate=True,
		flow_run=flow_run,
		expected_attempt=expected_attempt,
	)
	return True


def _current_approval_documents(run, *, for_update: bool = False) -> list[Any]:
	questions = _questions(run)
	call_map = _pending_call_map(run)
	documents: list[Any] = []
	for question in questions:
		key = str(question.get("key") or "")
		call = call_map.get(key)
		if not call:
			frappe.throw("Paused Flow Run question no longer maps to a pending tool call")
		arguments_hash = hashlib.sha256(_canonical_json(call.get("arguments") or {}).encode()).hexdigest()
		name = frappe.db.get_value(
			"IONE AI Tool Approval",
			{
				"flow_run": run.name,
				"run_iteration": int(run.get("iterations") or 0),
				"question_key": key,
				"arguments_hash": arguments_hash,
				"status": ["in", ["Pending", "Reviewed", "Expired"]],
			},
			"name",
			order_by="creation desc",
			for_update=for_update,
		)
		if not name:
			frappe.throw("Paused Flow Run is missing its exact IONE tool approval")
		documents.append(frappe.get_doc("IONE AI Tool Approval", name, for_update=for_update))
	return documents


def _assert_current_approval(doc, *, run=None, task=None) -> None:
	run = run or frappe.get_doc("Flow Run", doc.flow_run)
	if run.status != "Paused" or run.session != doc.flow_session:
		frappe.throw("AI Tool Approval no longer references a Paused matching Flow Run")
	if int(run.get("iterations") or 0) != int(doc.get("run_iteration") or 0):
		frappe.throw("AI Tool Approval belongs to an earlier Flow Run pause")
	task = task or frappe.get_doc("IONE AI Analysis Task", doc.task)
	if (
		task.policy != doc.policy
		or task.flow_run != run.name
		or task.flow_session != run.session
		or task.status != "Pending Confirmation"
	):
		frappe.throw("AI Tool Approval no longer matches its active governed task")
	assert_ione_tool_integrity({str(doc.tool_slug)})
	current = _current_call(run, str(doc.question_key))
	arguments_json = _canonical_json(current.get("arguments") or {})
	if hashlib.sha256(arguments_json.encode()).hexdigest() != doc.arguments_hash:
		frappe.throw("AI Tool Approval arguments changed after review was requested")
	question = next(
		(item for item in _questions(run) if str(item.get("key") or "") == str(doc.question_key)),
		None,
	)
	if not question:
		frappe.throw("AI Tool Approval question is no longer pending")
	question_payload = {
		"run_iteration": int(run.get("iterations") or 0),
		"key": str(question.get("key") or ""),
		"prompt": str(question.get("prompt") or ""),
		"options": question.get("options") or [],
		"tool": str(doc.tool_slug),
		"arguments_hash": doc.arguments_hash,
	}
	if hashlib.sha256(_canonical_json(question_payload).encode()).hexdigest() != doc.question_hash:
		frappe.throw("AI Tool Approval question changed after review was requested")


def _current_call(run, question_key: str) -> dict[str, Any]:
	call = _pending_call_map(run).get(question_key)
	if not call:
		frappe.throw("AI Tool Approval has no matching pending call")
	return call


def _pending_call_map(run) -> dict[str, dict[str, Any]]:
	rows = frappe.get_all(
		"Flow Session Message",
		filters={"parent": run.session, "run": run.name, "role": "assistant"},
		fields=["tool_calls", "idx"],
		order_by="idx desc",
		limit_page_length=20,
	)
	output: dict[str, dict[str, Any]] = {}
	for row in rows:
		value = _json_value(row.tool_calls, [])
		if not isinstance(value, list):
			continue
		for item in value:
			if not isinstance(item, dict):
				continue
			function = item.get("function") if isinstance(item.get("function"), dict) else {}
			key = str(item.get("id") or "")
			if not key:
				continue
			arguments = _json_value(function.get("arguments"), {})
			if not isinstance(arguments, dict):
				frappe.throw("Pending governed tool arguments must be a JSON object")
			output.setdefault(
				key,
				{
					"name": str(function.get("name") or ""),
					"arguments": arguments,
				},
			)
	return output


def _questions(run) -> list[dict[str, Any]]:
	value = _json_value(run.get("questions"), [])
	return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _assert_exact_resumed_run(run, task) -> None:
	if (
		str(run.name or "") != str(task.get("flow_run") or "")
		or str(run.get("session") or "") != str(task.get("flow_session") or "")
		or run.get("reference_doctype") != "IONE AI Analysis Task"
		or str(run.get("reference_name") or "") != task.name
		or str(run.get("status") or "") not in {"Completed", "Paused", "Failed"}
	):
		frappe.throw("Resumed Flow Run failed its governed task and terminal-state fence")


def _exact_failed_resume_run(run, task):
	try:
		run.reload()
	except Exception:
		return None
	if str(run.get("status") or "") != "Failed":
		return None
	try:
		_assert_exact_resumed_run(run, task)
	except Exception:
		return None
	return run


def _consume_reviewed_terminal_approvals(run, limit: int = 100) -> int:
	if str(run.get("status") or "") not in {"Completed", "Failed"}:
		return 0
	rows = frappe.get_all(
		"IONE AI Tool Approval",
		filters={"flow_run": run.name, "status": "Reviewed"},
		fields=["name"],
		order_by="run_iteration asc, creation asc, name asc",
		limit_page_length=min(max(int(limit or 100), 1), 100),
	)
	documents = [
		frappe.get_doc("IONE AI Tool Approval", row.get("name"), for_update=True)
		for row in rows
		if row.get("name")
	]
	if documents:
		_consume_approvals(documents, str(run.status))
	return len(documents)


def _consume_approvals(documents: list[Any], resume_status: str) -> None:
	flag = getattr(frappe.flags, "ione_ai_approval_resume", False)
	try:
		frappe.flags.ione_ai_approval_resume = True
		for doc in documents:
			doc.status = "Consumed"
			doc.consumed_at = now_datetime()
			doc.resume_status = resume_status
			doc.save(ignore_permissions=True)
	finally:
		frappe.flags.ione_ai_approval_resume = flag


def _expire_approval(doc) -> bool:
	if doc.status != "Pending":
		return False
	flag = getattr(frappe.flags, "ione_ai_approval_expire", False)
	try:
		frappe.flags.ione_ai_approval_expire = True
		doc.status = "Expired"
		doc.save(ignore_permissions=True)
	finally:
		frappe.flags.ione_ai_approval_expire = flag
	return True


def _run_advisory_lock(flow_run: str):
	"""Serialize every state transition for one governed Flow Run."""
	return frappe.db.advisory_lock(
		f"ione-qms:ai-tool-run:{flow_run}",
		timeout=APPROVAL_RUN_LOCK_TIMEOUT_SECONDS,
	)


def _flow_run_for_update(flow_run: str):
	"""Keep serialization through Frappe's later transaction commit.

	MariaDB GET_LOCK is session scoped and is released when the advisory-lock
	context exits, while a whitelisted request commits after this function
	returns. The InnoDB row lock closes that gap and makes the next reviewer
	observe every prior committed approval state.
	"""
	return frappe.get_doc("Flow Run", flow_run, for_update=True)


def _require_named_reviewer(user: str) -> None:
	if user in {"", "Guest", "Administrator", None}:
		frappe.throw("AI Tool Approval requires a named accountable reviewer", frappe.PermissionError)
	if not set(frappe.get_roles(user)).intersection(APPROVAL_REVIEW_ROLES):
		frappe.throw("Current user cannot review AI tool calls", frappe.PermissionError)
	if "IONE Agent Service" in frappe.get_roles(user):
		frappe.throw("AI service identities cannot approve their own tool calls", frappe.PermissionError)


def _json_value(value: Any, default: Any) -> Any:
	if value in (None, ""):
		return default
	if isinstance(value, str):
		try:
			return json.loads(value)
		except (TypeError, ValueError) as exc:
			raise frappe.ValidationError("Governed Flow JSON is invalid") from exc
	return value


def _canonical_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)
