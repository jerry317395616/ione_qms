from __future__ import annotations

import hashlib
from typing import Any

import frappe
from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification
from frappe.utils import getdate, now_datetime, nowdate

from ione_qms.rule_engine.executor import evaluate_event_job
from ione_qms.services.runtime_settings import require_post_migrate_runtime_ready

_MAX_BATCH_SIZE = 1_000
_MAX_CONTINUATION_BATCHES = 10_000
_OVERDUE_SOURCES = (
	("IONE QC Finding", frozenset({"Closed", "Appeal Approved"}), "质量问题"),
	("IONE QC Rectification", frozenset({"Closed", "Rejected", "Cancelled"}), "整改任务"),
	("IONE Quality Action Item", frozenset({"Closed", "Cancelled"}), "Quality action item"),
)
_DEPARTMENT_ESCALATION_ROLES = frozenset({"IONE Department Director", "IONE Department QC Officer"})
_GLOBAL_ESCALATION_ROLES = frozenset({"IONE QC Reviewer", "IONE Medical Affairs"})


def execute_daily_quality_scan(
	batch_size: int = 500,
	cursor: str | None = None,
	remaining_batches: int = _MAX_CONTINUATION_BATCHES,
) -> dict[str, int]:
	"""Evaluate a bounded page of pending events with event-level rollback."""
	require_post_migrate_runtime_ready("run the daily quality scan")
	limit = min(max(int(batch_size or 500), 1), _MAX_BATCH_SIZE)
	summary = {"attempted": 0, "completed": 0, "failed": 0}
	with frappe.db.advisory_lock(
		"ione-qms:quality-scan",
		timeout=0,
	):
		events = _pending_events(cursor, limit + 1)
		for index, event_name in enumerate(events[:limit], start=1):
			summary["attempted"] += 1
			savepoint = f"ione_quality_{index}"
			frappe.db.savepoint(savepoint)
			try:
				results = evaluate_event_job(event_name)
			except Exception as exc:
				frappe.db.rollback(save_point=savepoint)
				_mark_event_error(event_name, exc)
				_upsert_data_quality_issue(event_name, exc)
				summary["failed"] += 1
				continue
			frappe.db.release_savepoint(savepoint)
			if any(item.get("result") == "Error" for item in results):
				summary["failed"] += 1
			else:
				summary["completed"] += 1
		if len(events) > limit and remaining_batches > 0:
			_enqueue_continuation(limit, events[limit - 1], remaining_batches - 1)
	return summary


def escalate_overdue_items(
	batch_size: int = 500,
	source_index: int = 0,
	cursor: str | None = None,
	remaining_batches: int = _MAX_CONTINUATION_BATCHES,
) -> dict[str, int]:
	"""Notify accountable users about one bounded page of overdue work.

	Notification text contains workflow metadata only. Patient, encounter, finding
	title, narrative, and evidence fields are deliberately never queried.
	"""
	require_post_migrate_runtime_ready("run overdue quality escalation")
	limit = min(max(int(batch_size or 500), 1), _MAX_BATCH_SIZE)
	index = min(max(int(source_index or 0), 0), len(_OVERDUE_SOURCES))
	summary = {"reviewed": 0, "recipients": 0, "without_recipient": 0, "failed": 0}
	if index >= len(_OVERDUE_SOURCES):
		return summary
	doctype, terminal_statuses, label = _OVERDUE_SOURCES[index]
	if not frappe.db.exists("DocType", doctype):
		_enqueue_overdue_continuation(limit, index + 1, None, remaining_batches)
		return summary
	today = getdate(nowdate())
	with frappe.db.advisory_lock(f"ione-qms:overdue:{today}:{index}", timeout=0):
		rows = _overdue_rows(doctype, terminal_statuses, today, cursor, limit + 1)
		role_users = {
			"department": _user_names_with_roles(_DEPARTMENT_ESCALATION_ROLES),
			"global": _user_names_with_roles(_GLOBAL_ESCALATION_ROLES),
		}
		global_recipients = _enabled_user_emails(role_users["global"])
		department_cache: dict[str, set[str]] = {}
		for row in rows[:limit]:
			summary["reviewed"] += 1
			failure_type: str | None = None
			try:
				recipients = _overdue_recipients(
					row,
					role_users=role_users,
					global_recipients=global_recipients,
					department_cache=department_cache,
				)
				if not recipients:
					summary["without_recipient"] += 1
					continue
				overdue_days = max((today - getdate(row.due_date)).days, 1)
				_enqueue_overdue_notification(
					doctype=doctype,
					document_name=row.name,
					label=label,
					department=row.get("department"),
					due_date=getdate(row.due_date),
					overdue_days=overdue_days,
					recipients=recipients,
				)
				summary["recipients"] += len(recipients)
			except Exception as exc:
				failure_type = type(exc).__name__
			if failure_type is not None:
				summary["failed"] += 1
				_log_overdue_failure(doctype, row.name, failure_type)
		if remaining_batches > 0:
			if len(rows) > limit:
				_enqueue_overdue_continuation(
					limit,
					index,
					rows[limit - 1].name,
					remaining_batches - 1,
				)
			else:
				_enqueue_overdue_continuation(
					limit,
					index + 1,
					None,
					remaining_batches - 1,
				)
	return summary


def _pending_events(cursor: str | None, limit: int) -> list[str]:
	doctype = "IONE Clinical Quality Event"
	filters: dict[str, Any] = {}
	if _has_field(doctype, "processing_status"):
		filters["processing_status"] = ["in", ["Pending", "Error"]]
	if cursor:
		filters["name"] = [">", cursor]
	return frappe.get_all(
		doctype,
		filters=filters,
		pluck="name",
		order_by="name asc",
		limit_page_length=limit,
	)


def _overdue_rows(
	doctype: str,
	terminal_statuses: frozenset[str],
	today,
	cursor: str | None,
	limit: int,
) -> list[Any]:
	filters: dict[str, Any] = {
		"due_date": ["<", today],
		"status": ["not in", sorted(terminal_statuses)],
	}
	if cursor:
		filters["name"] = [">", cursor]
	meta = frappe.get_meta(doctype)
	fields = [
		fieldname
		for fieldname in (
			"name",
			"department",
			"due_date",
			"assigned_to",
			"verifier",
			"responsible_staff",
		)
		if fieldname == "name" or meta.has_field(fieldname)
	]
	return frappe.get_all(
		doctype,
		filters=filters,
		fields=fields,
		order_by="name asc",
		limit_page_length=limit,
	)


def _overdue_recipients(
	row,
	*,
	role_users: dict[str, set[str]],
	global_recipients: set[str],
	department_cache: dict[str, set[str]],
) -> set[str]:
	user_names: set[str] = set()
	if row.get("assigned_to"):
		user_names.add(str(row.get("assigned_to")))
	if row.get("verifier"):
		user_names.add(str(row.get("verifier")))
	if row.get("responsible_staff"):
		staff_user = frappe.db.get_value("IONE Medical Staff", row.get("responsible_staff"), "user")
		if staff_user:
			user_names.add(str(staff_user))
	recipients = _enabled_user_emails(user_names)
	department = str(row.get("department") or "")
	if department:
		if department not in department_cache:
			department_cache[department] = _department_role_emails(
				department,
				role_users["department"],
			)
		recipients.update(department_cache[department])
	recipients.update(global_recipients)
	return recipients


def _department_role_emails(department: str, role_user_names: set[str]) -> set[str]:
	if not role_user_names or not frappe.db.exists("DocType", "IONE Medical Staff"):
		return set()
	filters: dict[str, Any] = {
		"department": department,
		"user": ["in", sorted(role_user_names)],
	}
	if _has_field("IONE Medical Staff", "practice_status"):
		filters["practice_status"] = "Active"
	user_names = set(
		frappe.get_all(
			"IONE Medical Staff",
			filters=filters,
			pluck="user",
			limit_page_length=10_000,
		)
	)
	return _enabled_user_emails(user_names)


def _user_names_with_roles(roles: frozenset[str]) -> set[str]:
	if not roles:
		return set()
	return set(
		frappe.get_all(
			"Has Role",
			filters={
				"parenttype": "User",
				"role": ["in", sorted(roles)],
			},
			pluck="parent",
			limit_page_length=10_000,
		)
	)


def _enabled_user_emails(user_names: set[str]) -> set[str]:
	if not user_names:
		return set()
	return {
		str(row.email)
		for row in frappe.get_all(
			"User",
			filters={"name": ["in", sorted(user_names)], "enabled": 1},
			fields=["email"],
			limit_page_length=10_000,
		)
		if row.get("email")
	}


def _enqueue_overdue_notification(
	*,
	doctype: str,
	document_name: str,
	label: str,
	department: str | None,
	due_date,
	overdue_days: int,
	recipients: set[str],
) -> None:
	subject = f"IONE QMS {label}逾期 {overdue_days} 天"
	description = (
		f"{label}已逾期 {overdue_days} 天 (截止日期 {due_date})。"
		f"责任科室: {department or '未指定'}。请进入 IONE QMS 完成处置。"
	)
	enqueue_create_notification(
		sorted(recipients),
		{
			"title": subject,
			"description": description,
			"subject": subject,
			"email_content": description,
			"document_type": doctype,
			"document_name": document_name,
		},
		dedupe_on=["document_type", "document_name", "subject"],
	)


def _enqueue_overdue_continuation(
	batch_size: int,
	source_index: int,
	cursor: str | None,
	remaining_batches: int,
) -> None:
	if source_index >= len(_OVERDUE_SOURCES) or remaining_batches <= 0:
		return
	job_key = hashlib.sha256(f"{nowdate()}|{source_index}|{cursor}|{remaining_batches}".encode()).hexdigest()[
		:24
	]
	frappe.enqueue(
		"ione_qms.tasks.quality.escalate_overdue_items",
		queue="long",
		enqueue_after_commit=True,
		job_id=f"ione-qms:overdue:{job_key}",
		deduplicate=True,
		batch_size=batch_size,
		source_index=source_index,
		cursor=cursor,
		remaining_batches=remaining_batches,
	)


def _mark_event_error(event_name: str, exc: Exception) -> None:
	doctype = "IONE Clinical Quality Event"
	values = _supported_values(
		doctype,
		{
			"processing_status": _select_value(doctype, "processing_status", ("Error", "Failed")),
			"error_message": f"{type(exc).__name__}: deterministic rule evaluation failed",
			"processed_at": now_datetime(),
		},
	)
	if values:
		frappe.db.set_value(doctype, event_name, values, update_modified=False)


def _upsert_data_quality_issue(event_name: str, exc: Exception) -> None:
	doctype = "IONE Data Quality Issue"
	if not frappe.db.exists("DocType", doctype):
		return
	event = frappe.get_doc("IONE Clinical Quality Event", event_name)
	issue_key = hashlib.sha256(f"quality-scan|{event_name}|{type(exc).__name__}".encode()).hexdigest()
	values = _supported_values(
		doctype,
		{
			"issue_key": issue_key,
			"source_system": event.get("source_system"),
			"event": event_name,
			"hospital": event.get("hospital"),
			"campus": event.get("campus"),
			"department": event.get("department"),
			"ward": event.get("ward"),
			"source_record_type": event.get("source_record_type"),
			"source_record_id": event.get("source_record_id"),
			"issue_type": _select_value(doctype, "issue_type", ("Rule Execution Error", "Processing Error")),
			"severity": _select_value(doctype, "severity", ("High", "Error")),
			"status": _select_value(doctype, "status", ("Open", "Pending")),
			"description": (
				f"{type(exc).__name__}: deterministic rule evaluation failed. "
				"Clinical payload was not copied into this issue."
			),
			"detected_at": now_datetime(),
		},
	)
	existing = (
		frappe.db.get_value(doctype, {"issue_key": issue_key}, "name")
		if _has_field(doctype, "issue_key")
		else None
	)
	if existing:
		return
	frappe.get_doc({"doctype": doctype, **values}).insert(ignore_permissions=True)


def _enqueue_continuation(batch_size: int, cursor: str, remaining_batches: int) -> None:
	job_key = hashlib.sha256(f"{cursor}|{remaining_batches}".encode()).hexdigest()[:24]
	frappe.enqueue(
		"ione_qms.tasks.quality.execute_daily_quality_scan",
		queue="long",
		enqueue_after_commit=True,
		job_id=f"ione-qms:quality:{job_key}",
		deduplicate=True,
		batch_size=batch_size,
		cursor=cursor,
		remaining_batches=remaining_batches,
	)


def _select_value(doctype: str, fieldname: str, candidates: tuple[str, ...]) -> str:
	field = frappe.get_meta(doctype).get_field(fieldname)
	if not field or field.fieldtype != "Select" or not field.options:
		return candidates[0]
	options = {option.strip() for option in str(field.options).splitlines() if option.strip()}
	return next((candidate for candidate in candidates if candidate in options), candidates[0])


def _supported_values(doctype: str, values: dict[str, Any]) -> dict[str, Any]:
	meta = frappe.get_meta(doctype)
	return {fieldname: value for fieldname, value in values.items() if meta.has_field(fieldname)}


def _has_field(doctype: str, fieldname: str) -> bool:
	return bool(frappe.get_meta(doctype).has_field(fieldname))


def _log_overdue_failure(doctype: str, document_name: str, failure_type: str) -> None:
	frappe.log_error(
		title="IONE QMS overdue escalation failed",
		message=f"{failure_type}: overdue notification failed; clinical content was not logged.",
		reference_doctype=doctype,
		reference_name=document_name,
	)
