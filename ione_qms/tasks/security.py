from __future__ import annotations

import hashlib
import hmac
from typing import Any

import frappe
from frappe.utils import add_days, now_datetime

from ione_qms.ai.audit import EVIDENCE_SNAPSHOT_REDACTED
from ione_qms.ai.flow_artifacts import (
	SANITIZED_FLOW_ERROR,
	flow_run_artifact_hashes,
)
from ione_qms.services.ai_approvals import APPROVAL_CONTENT_REDACTED
from ione_qms.services.immutability import canonical_record_hash
from ione_qms.services.runtime_settings import require_post_migrate_runtime_ready

_MAX_BATCH_SIZE = 1_000
_REDACTED = "[REDACTED BY IONE RETENTION POLICY]"
_REDACTED_JSON = "[]"
_RETENTION_FIELDS = (
	("input_summary", "input_hash", "input_retention_days"),
	("input_payload", "input_hash", "input_retention_days"),
	("output_payload", "output_hash", "output_retention_days"),
)


def enforce_retention(batch_size: int = 500) -> dict[str, int]:
	"""Cryptographically redact expired AI payloads while preserving audit records.

	Clinical events, findings, formal evidence, source lineage, Flow links, access-log
	receipts, and their integrity hashes are never deleted. Expired embedded access
	snapshots and terminal approval content are hash-verified before redaction.
	"""
	require_post_migrate_runtime_ready("enforce retention")
	limit = min(max(int(batch_size or 500), 1), _MAX_BATCH_SIZE)
	summary = {"reviewed": 0, "redacted": 0, "skipped": 0}
	if not frappe.db.exists("DocType", "IONE AI Analysis Task"):
		return summary
	settings = (
		frappe.get_single("IONE AI Settings") if frappe.db.exists("DocType", "IONE AI Settings") else None
	)
	with frappe.db.advisory_lock("ione-qms:ai-retention", timeout=0):
		remaining = limit
		for payload_field, hash_field, setting_field in _RETENTION_FIELDS:
			if remaining <= 0:
				break
			if not _has_field("IONE AI Analysis Task", payload_field):
				continue
			retention_days = int(settings.get(setting_field) or 0) if settings else 0
			if retention_days <= 0:
				summary["skipped"] += 1
				continue
			cutoff = add_days(now_datetime(), -retention_days)
			retention_date_field = (
				"completed_at" if _has_field("IONE AI Analysis Task", "completed_at") else "modified"
			)
			filters: dict[str, Any] = {
				retention_date_field: ["<", cutoff],
				payload_field: ["not in", ["", _REDACTED]],
			}
			if _has_field("IONE AI Analysis Task", "status"):
				filters["status"] = ["in", _final_task_statuses()]
			rows = frappe.get_all(
				"IONE AI Analysis Task",
				filters=filters,
				fields=["name", payload_field, hash_field]
				if _has_field("IONE AI Analysis Task", hash_field)
				else ["name", payload_field],
				order_by=f"{retention_date_field} asc, name asc",
				limit_page_length=remaining,
			)
			for row in rows:
				if remaining <= 0:
					break
				summary["reviewed"] += 1
				remaining -= 1
				if not _has_field("IONE AI Analysis Task", hash_field):
					summary["skipped"] += 1
					continue
				savepoint = f"ione_retention_{summary['reviewed']}"
				frappe.db.savepoint(savepoint)
				failure_type: str | None = None
				try:
					_redact_payload(row, payload_field, hash_field)
				except Exception as exc:
					failure_type = type(exc).__name__
				if failure_type is not None:
					frappe.db.rollback(save_point=savepoint)
					summary["skipped"] += 1
					_log_retention_failure(row.name, payload_field, failure_type)
					continue
				frappe.db.release_savepoint(savepoint)
				summary["redacted"] += 1
		if remaining > 0:
			remaining = _redact_task_bound_input_audit(settings, remaining, summary)
		if remaining > 0:
			_redact_flow_runs(settings, remaining, summary)
	return summary


def _redact_payload(row, payload_field: str, hash_field: str) -> None:
	payload = str(row.get(payload_field) or "")
	if not payload or payload == _REDACTED:
		return
	digest = hashlib.sha256(payload.encode()).hexdigest()
	existing_hash = str(row.get(hash_field) or "")
	if existing_hash and existing_hash != digest:
		raise ValueError(
			f"Stored {hash_field} does not match {payload_field} for AI task {row.name}; "
			"retention redaction stopped to preserve audit integrity."
		)
	frappe.db.set_value(
		"IONE AI Analysis Task",
		row.name,
		{hash_field: digest, payload_field: _REDACTED},
		update_modified=False,
	)
	_add_retention_comment(row.name, payload_field, digest)


def _add_retention_comment(task_name: str, fieldname: str, digest: str) -> None:
	doc = frappe.get_doc("IONE AI Analysis Task", task_name)
	doc.add_comment(
		"Info",
		text=(
			f"IONE retention policy redacted {fieldname}; SHA-256 {digest}. "
			"Clinical evidence and access audit records were retained."
		),
	)


def _redact_task_bound_input_audit(
	settings,
	remaining: int,
	summary: dict[str, int],
) -> int:
	retention_days = int(settings.get("input_retention_days") or 0) if settings else 0
	if retention_days <= 0 or remaining <= 0:
		return remaining
	cutoff = add_days(now_datetime(), -retention_days)
	if frappe.db.exists("DocType", "IONE AI Data Access Log"):
		remaining = _redact_access_log_snapshots(cutoff, remaining, summary)
	if (
		remaining > 0
		and frappe.db.exists("DocType", "IONE AI Tool Approval")
		and _has_field("IONE AI Tool Approval", "question_prompt_hash")
	):
		remaining = _redact_terminal_approval_content(cutoff, remaining, summary)
	return remaining


def _redact_access_log_snapshots(cutoff, remaining: int, summary: dict[str, int]) -> int:
	for row in _aged_access_log_snapshots(cutoff, remaining):
		if remaining <= 0:
			break
		remaining -= 1
		summary["reviewed"] += 1
		savepoint = f"ione_access_retention_{summary['reviewed']}"
		frappe.db.savepoint(savepoint)
		try:
			changed = _redact_access_log_snapshot(row.get("name"))
		except Exception as exc:
			frappe.db.rollback(save_point=savepoint)
			summary["skipped"] += 1
			_log_retention_failure(
				str(row.get("name") or ""),
				"snapshot_json",
				type(exc).__name__,
				doctype="IONE AI Data Access Log",
			)
			continue
		frappe.db.release_savepoint(savepoint)
		if changed:
			summary["redacted"] += 1
	return remaining


def _aged_access_log_snapshots(cutoff, limit: int) -> list[Any]:
	statuses = _final_task_statuses()
	placeholders = ", ".join(["%s"] * len(statuses))
	return frappe.db.sql(
		(
			"select audit.name "  # noqa: S608
			"from `tabIONE AI Data Access Log` audit "
			"inner join `tabIONE AI Analysis Task` task on task.name = audit.task "
			"where task.completed_at < %s "
			f"and task.status in ({placeholders}) "
			"and coalesce(audit.snapshot_json, '') not in ('', %s) "
			"order by task.completed_at asc, audit.name asc "
			"limit %s"
		),
		(cutoff, *statuses, EVIDENCE_SNAPSHOT_REDACTED, limit),
		as_dict=True,
	)


def _redact_access_log_snapshot(name: str | None) -> bool:
	if not name:
		raise ValueError("AI access receipt has no name")
	doc = frappe.get_doc("IONE AI Data Access Log", name, for_update=True)
	if not _task_is_final(doc.get("task")):
		raise ValueError("AI access receipt task is not terminal")
	snapshot_json = str(doc.get("snapshot_json") or "")
	if snapshot_json == EVIDENCE_SNAPSHOT_REDACTED:
		return False
	if not snapshot_json:
		raise ValueError("AI access receipt snapshot is missing")
	content_hash = str(doc.get("content_hash") or "").lower()
	actual_content_hash = hashlib.sha256(snapshot_json.encode()).hexdigest()
	if len(content_hash) != 64 or not hmac.compare_digest(actual_content_hash, content_hash):
		raise ValueError("AI access receipt snapshot failed its content-hash check")
	record_hash = str(doc.get("record_hash") or "").lower()
	actual_record_hash = canonical_record_hash(doc)
	if len(record_hash) != 64 or not hmac.compare_digest(actual_record_hash, record_hash):
		raise ValueError("AI access receipt failed its append-only record-hash check")
	frappe.db.set_value(
		"IONE AI Data Access Log",
		doc.name,
		"snapshot_json",
		EVIDENCE_SNAPSHOT_REDACTED,
		update_modified=False,
	)
	doc.add_comment(
		"Info",
		text=(
			"IONE input retention redacted the embedded evidence snapshot after verifying "
			f"its SHA-256 {content_hash}. The task-bound receipt, source lineage, content "
			"hash, and original append-only record hash were retained."
		),
	)
	return True


def _redact_terminal_approval_content(cutoff, remaining: int, summary: dict[str, int]) -> int:
	for row in _aged_terminal_approvals(cutoff, remaining):
		if remaining <= 0:
			break
		remaining -= 1
		summary["reviewed"] += 1
		savepoint = f"ione_approval_retention_{summary['reviewed']}"
		frappe.db.savepoint(savepoint)
		try:
			changed = _redact_terminal_approval(row.get("name"))
		except Exception as exc:
			frappe.db.rollback(save_point=savepoint)
			summary["skipped"] += 1
			_log_retention_failure(
				str(row.get("name") or ""),
				"arguments_json/question_prompt",
				type(exc).__name__,
				doctype="IONE AI Tool Approval",
			)
			continue
		frappe.db.release_savepoint(savepoint)
		if changed:
			summary["redacted"] += 1
	return remaining


def _aged_terminal_approvals(cutoff, limit: int) -> list[Any]:
	statuses = _final_task_statuses()
	placeholders = ", ".join(["%s"] * len(statuses))
	return frappe.db.sql(
		(
			"select approval.name "  # noqa: S608
			"from `tabIONE AI Tool Approval` approval "
			"inner join `tabIONE AI Analysis Task` task on task.name = approval.task "
			"where task.completed_at < %s "
			f"and task.status in ({placeholders}) "
			"and approval.status in ('Consumed', 'Expired') "
			"and length(coalesce(approval.question_prompt_hash, '')) = 64 "
			"and (coalesce(approval.arguments_json, '') != %s "
			"or coalesce(approval.question_prompt, '') not in ('', %s)) "
			"order by task.completed_at asc, approval.name asc "
			"limit %s"
		),
		(cutoff, *statuses, APPROVAL_CONTENT_REDACTED, APPROVAL_CONTENT_REDACTED, limit),
		as_dict=True,
	)


def _redact_terminal_approval(name: str | None) -> bool:
	if not name:
		raise ValueError("AI Tool Approval has no name")
	doc = frappe.get_doc("IONE AI Tool Approval", name, for_update=True)
	if doc.get("status") not in {"Consumed", "Expired"} or not _task_is_final(doc.get("task")):
		raise ValueError("AI Tool Approval or its task is not terminal")
	if (
		doc.get("arguments_json") == APPROVAL_CONTENT_REDACTED
		and doc.get("question_prompt") == APPROVAL_CONTENT_REDACTED
	):
		return False
	if APPROVAL_CONTENT_REDACTED in {
		doc.get("arguments_json"),
		doc.get("question_prompt"),
	}:
		raise ValueError("AI Tool Approval has a partial or unauthorized retention marker")
	capability = f"{doc.doctype}:{doc.name}"
	previous_capability = getattr(frappe.flags, "ione_ai_approval_retention", None)
	try:
		frappe.flags.ione_ai_approval_retention = capability
		doc.arguments_json = APPROVAL_CONTENT_REDACTED
		doc.question_prompt = APPROVAL_CONTENT_REDACTED
		doc.save(ignore_permissions=True)
	finally:
		frappe.flags.ione_ai_approval_retention = previous_capability
	doc.add_comment(
		"Info",
		text=(
			"IONE input retention hash-verified and redacted the terminal proposed "
			"arguments and confirmation prompt. Their SHA-256 hashes and review "
			"provenance were retained."
		),
	)
	return True


def _redact_flow_runs(settings, remaining: int, summary: dict[str, int]) -> None:
	run_doctype = "Flow Run"
	if not frappe.db.exists("DocType", run_doctype):
		return
	for run_field, setting_field in (
		("input", "input_retention_days"),
		("output", "output_retention_days"),
	):
		if remaining <= 0 or not _has_field(run_doctype, run_field):
			break
		retention_days = int(settings.get(setting_field) or 0) if settings else 0
		if retention_days <= 0:
			continue
		cutoff = add_days(now_datetime(), -retention_days)
		runs = _aged_unredacted_flow_runs(run_field, cutoff, remaining)
		for run in runs:
			if remaining <= 0:
				break
			# Flow may finish before the governed task is finalized, and an
			# apparently stale completed run can still be needed by orphan
			# reconciliation. Never redact any run until its task is terminal.
			if not _task_is_final(run.get("reference_name")):
				continue
			remaining -= 1
			summary["reviewed"] += 1
			savepoint = f"ione_flow_retention_{summary['reviewed']}"
			frappe.db.savepoint(savepoint)
			failure_type: str | None = None
			try:
				link = _flow_run_link(run.name)
				if not link:
					raise ValueError("Governed Flow Run has no immutable IONE revision")
				digest = _validate_flow_payload_hash(
					link,
					run_field,
					str(run.get(run_field) or ""),
				)
				if run_field == "input":
					_redact_flow_input_artifacts(run, link)
				else:
					_redact_flow_output_artifacts(run, link)
				_add_flow_retention_comment(
					link.get("name") if link else None,
					run.get("reference_name"),
					run_field,
					digest,
				)
			except Exception as exc:
				failure_type = type(exc).__name__
			if failure_type is not None:
				frappe.db.rollback(save_point=savepoint)
				summary["skipped"] += 1
				_log_retention_failure(
					run.name,
					run_field,
					failure_type,
					doctype=run_doctype,
				)
				continue
			frappe.db.release_savepoint(savepoint)
			summary["redacted"] += 1


def governed_flow_session_retention_verified(session_name: str | None) -> bool:
	"""Return true only after every governed payload surface was verified/redacted.

	Frappe Log Settings calls Flow's destructive ``clear_old_logs`` independently
	of the IONE retention job. The Flow Session controller extension uses this
	as a durable deletion hold: a missing link, non-terminal task, attachment,
	partial marker, or failed integrity check keeps the whole session.
	"""
	if not session_name:
		return False
	runs = frappe.get_all(
		"Flow Run",
		filters={"session": session_name},
		fields=[
			"name",
			"reference_doctype",
			"reference_name",
			"input",
			"output",
			"tool_calls",
			"questions",
			"error",
		],
		order_by="creation asc, name asc",
		limit_page_length=_MAX_BATCH_SIZE + 1,
	)
	if not runs or len(runs) > _MAX_BATCH_SIZE:
		return False

	found_governed = False
	for run in runs:
		linked = bool(frappe.db.exists("IONE Flow Run Link", {"flow_run": run.get("name")}))
		governed = run.get("reference_doctype") == "IONE AI Analysis Task" or linked
		if not governed:
			# A governed session must not be deleted when it contains an
			# unexplained mixed-lineage run.
			return False
		found_governed = True
		if (
			not _task_is_final(run.get("reference_name"))
			or run.get("input") != _REDACTED
			or run.get("output") != _REDACTED
			or run.get("tool_calls") != _REDACTED_JSON
			or run.get("questions") != _REDACTED_JSON
		):
			return False
		error = str(run.get("error") or "")
		if error and not error.startswith(f"{SANITIZED_FLOW_ERROR}:"):
			return False
		link = _flow_run_link(str(run.get("name") or ""))
		if not link or any(
			len(str(link.get(fieldname) or "")) != 64
			for fieldname in (
				"input_hash",
				"output_hash",
				"input_messages_hash",
				"output_messages_hash",
				"tool_trace_hash",
				"attachment_text_hash",
			)
		):
			return False

	if not found_governed:
		return False
	if frappe.db.exists("Flow Session Attachment", {"parent": session_name}):
		return False
	messages = frappe.get_all(
		"Flow Session Message",
		filters={"parent": session_name},
		fields=["role", "content", "tool_calls"],
		limit_page_length=10_001,
	)
	if len(messages) > 10_000:
		return False
	for message in messages:
		if message.get("role") in {"user", "tool", "assistant"} and message.get("content") not in {
			None,
			"",
			_REDACTED,
		}:
			return False
		if message.get("tool_calls") not in {None, "", _REDACTED_JSON}:
			return False
	return True


def _aged_unredacted_flow_runs(run_field: str, cutoff, limit: int) -> list[Any]:
	if run_field not in {"input", "output"}:
		raise ValueError("Unsupported governed Flow retention field")
	# coalesce deliberately includes NULL/empty output: a denied or tool-only
	# Flow result can still leave sensitive assistant transcript messages.
	return frappe.db.sql(
		(
			f"select name, reference_name, status, modified, `{run_field}`, error "  # noqa: S608
			"from `tabFlow Run` "
			"where reference_doctype = 'IONE AI Analysis Task' "
			"and modified < %s "
			"and status in ('Completed', 'Failed', 'Paused', 'Running') "
			f"and coalesce(`{run_field}`, '') != %s "
			"order by modified asc, name asc "
			"limit %s"
		),
		(cutoff, _REDACTED, min(max(int(limit or 1), 1), _MAX_BATCH_SIZE)),
		as_dict=True,
	)


def _flow_run_link(flow_run: str):
	if not frappe.db.exists("DocType", "IONE Flow Run Link"):
		return None
	fields = [
		"name",
		"run_iteration",
		"input_hash",
		"output_hash",
		"input_messages_hash",
		"output_messages_hash",
		"tool_trace_hash",
		"attachment_text_hash",
	]
	rows = frappe.get_all(
		"IONE Flow Run Link",
		filters={"flow_run": flow_run},
		fields=[
			fieldname
			for fieldname in fields
			if fieldname == "name" or _has_field("IONE Flow Run Link", fieldname)
		],
		order_by="run_iteration desc, creation desc, name desc",
		limit_page_length=1,
	)
	return rows[0] if rows else None


def _validate_flow_payload_hash(link, run_field: str, payload: str) -> str:
	hash_field = f"{run_field}_hash"
	digest = hashlib.sha256(payload.encode()).hexdigest()
	expected_hash = str(link.get(hash_field) or "") if link else ""
	if len(expected_hash) != 64 or expected_hash != digest:
		raise ValueError("Flow Run payload failed its immutable link hash check")
	return digest


def _redact_flow_input_artifacts(run, link) -> None:
	hashes = flow_run_artifact_hashes(run.name)
	_validate_artifact_hash(link, "input_messages_hash", hashes["input_messages_hash"])
	_validate_artifact_hash(link, "tool_trace_hash", hashes["tool_trace_hash"])
	_validate_artifact_hash(link, "attachment_text_hash", hashes["attachment_text_hash"])
	frappe.db.set_value(
		"Flow Run",
		run.name,
		{
			"input": _REDACTED,
			"tool_calls": _REDACTED_JSON,
			"questions": _REDACTED_JSON,
			"error": _safe_flow_error(run.get("error")),
		},
		update_modified=False,
	)
	for row in frappe.get_all(
		"Flow Session Message",
		filters={"run": run.name},
		fields=["name", "role", "content", "tool_calls"],
		limit_page_length=10_000,
	):
		values: dict[str, Any] = {}
		if row.get("role") in {"user", "tool"} and row.get("content") not in {None, "", _REDACTED}:
			values["content"] = _REDACTED
		if row.get("tool_calls") not in {None, "", _REDACTED_JSON}:
			values["tool_calls"] = _REDACTED_JSON
		if values:
			frappe.db.set_value("Flow Session Message", row.name, values, update_modified=False)


def _redact_flow_output_artifacts(run, link) -> None:
	hashes = flow_run_artifact_hashes(run.name)
	_validate_artifact_hash(link, "output_messages_hash", hashes["output_messages_hash"])
	frappe.db.set_value(
		"Flow Run",
		run.name,
		"output",
		_REDACTED,
		update_modified=False,
	)
	for row in frappe.get_all(
		"Flow Session Message",
		filters={"run": run.name, "role": "assistant"},
		fields=["name", "content"],
		limit_page_length=10_000,
	):
		if row.get("content") not in {None, "", _REDACTED}:
			frappe.db.set_value(
				"Flow Session Message",
				row.name,
				"content",
				_REDACTED,
				update_modified=False,
			)


def _validate_artifact_hash(link, fieldname: str, digest: str) -> None:
	expected = str(link.get(fieldname) or "") if link else ""
	if len(expected) != 64 or expected != digest:
		raise ValueError(f"Flow artifact failed its immutable {fieldname} check")


def _safe_flow_error(value: Any) -> str | None:
	text = str(value or "")
	if not text:
		return None
	if text.startswith(f"{SANITIZED_FLOW_ERROR}:"):
		return text[:160]
	return f"{SANITIZED_FLOW_ERROR}:RetainedError"


def _task_is_final(task_name: str | None) -> bool:
	if not task_name:
		return False
	status = frappe.db.get_value("IONE AI Analysis Task", task_name, "status")
	return status in _final_task_statuses()


def _add_flow_retention_comment(
	link_name: str | None,
	task_name: str | None,
	fieldname: str,
	digest: str,
) -> None:
	if link_name:
		doc = frappe.get_doc("IONE Flow Run Link", link_name)
	elif task_name:
		doc = frappe.get_doc("IONE AI Analysis Task", task_name)
	else:
		return
	doc.add_comment(
		"Info",
		text=(
			f"IONE retention policy redacted governed Flow {fieldname} and its transcript "
			f"surfaces; SHA-256 {digest}. Lineage and audit hashes were retained."
		),
	)


def _final_task_statuses() -> list[str]:
	doctype = "IONE AI Analysis Task"
	field = frappe.get_meta(doctype).get_field("status")
	candidates = ("Completed", "Failed", "Rejected", "Cancelled", "Reviewed")
	if not field or field.fieldtype != "Select" or not field.options:
		return list(candidates)
	options = {option.strip() for option in str(field.options).splitlines() if option.strip()}
	selected = [candidate for candidate in candidates if candidate in options]
	return selected or list(options)


def _has_field(doctype: str, fieldname: str) -> bool:
	return bool(frappe.get_meta(doctype).has_field(fieldname))


def _log_retention_failure(
	task_name: str,
	fieldname: str,
	failure_type: str,
	*,
	doctype: str = "IONE AI Analysis Task",
) -> None:
	frappe.log_error(
		title=f"IONE QMS retention skipped {fieldname}",
		message=f"{failure_type}: retention integrity check failed; payload was not logged.",
		reference_doctype=doctype,
		reference_name=task_name,
	)
