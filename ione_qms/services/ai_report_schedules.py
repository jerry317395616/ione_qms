from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any

import frappe
from frappe.utils import get_datetime, getdate, now_datetime, nowdate

from ione_qms.ai.governance import validate_policy_runtime
from ione_qms.permissions import require_scope_read
from ione_qms.services.ai_tasks import controlled_analysis_task_creation
from ione_qms.services.runtime_settings import ai_runtime_enabled, require_post_migrate_runtime_ready

REPORT_SCHEDULE_LIMIT = 500
REPORT_MAX_CATCHUP_MONTHS = 12
REPORT_SNAPSHOT_MAX_BYTES = 256 * 1024
REPORT_SNAPSHOT_MAX_INDICATOR_ROWS = 250
REPORT_SNAPSHOT_MAX_FINDINGS = 10_000
REPORT_SNAPSHOT_MAX_GROUPS = 100
REPORT_SNAPSHOT_MAX_SOURCE_RECORDS = 1_000_000
REPORT_MAX_RECOVERY_AUTHORIZATIONS = 2
REPORT_RECOVERY_DOCTYPE = "IONE AI Report Recovery Authorization"

_SCHEDULE_REVIEW_ROLES = frozenset({"IONE Agent Reviewer", "IONE Medical Affairs"})
_SCHEDULE_OPERATION_ROLES = frozenset({"IONE Agent Administrator", "IONE Medical Affairs"})
_SCHEDULE_REQUESTER_ROLES = frozenset(
	{
		"IONE QC Reviewer",
		"IONE Medical Affairs",
		"IONE Department Director",
		"IONE Department QC Officer",
	}
)
_REPORT_REVIEWER_ROLES = frozenset({"IONE Agent Reviewer", "IONE QC Reviewer", "IONE Medical Affairs"})
_SCHEDULE_SEMANTIC_FIELDS = (
	"schedule_code",
	"schedule_title",
	"report_type",
	"policy",
	"requested_by",
	"report_reviewer",
	"hospital",
	"campus",
	"department",
	"ward",
	"run_day",
	"coverage_start_period",
	"required_indicator_codes_json",
)
_SCHEDULE_GOVERNANCE_FIELDS = frozenset(
	{
		"status",
		"enabled",
		"coverage_start_period",
		"reviewed_by",
		"reviewed_at",
		"review_comment",
		"approval_checksum",
		"operated_by",
		"operated_at",
		"operation_comment",
	}
)
_SCHEDULE_RUNTIME_FIELDS = frozenset(
	{
		"last_period_start",
		"last_period_end",
		"last_dispatch_key",
		"last_task",
		"last_run_at",
		"last_result",
		"last_error_code",
	}
)
_SCHEDULE_TRANSITION_CAPABILITY = object()
_SNAPSHOT_CREATION_CAPABILITY = object()
_SCHEDULE_RUNTIME_CAPABILITY = object()
_RECOVERY_CREATION_CAPABILITY = object()
_RECOVERY_TERMINAL_STATUSES = frozenset({"Failed", "Rejected", "Cancelled"})
_RECOVERY_REASON_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_.:-]{1,63}$")
_RECOVERY_SEMANTIC_FIELDS = (
	"schedule",
	"prior_task",
	"report_snapshot",
	"period_start",
	"period_end",
	"hospital",
	"campus",
	"department",
	"ward",
	"schedule_approval_checksum",
	"prior_dispatch_key",
	"recovery_sequence",
	"prior_terminal_status",
	"authorized_by",
	"authorized_at",
	"reason_code",
	"reason",
	"recovery_dispatch_key",
)


def prepare_report_schedule(doc, method: str | None = None) -> None:
	del method
	if not doc.is_new():
		return
	user = _named_user()
	doc.requested_by = user
	doc.status = "Draft"
	doc.enabled = 0
	for fieldname in _SCHEDULE_GOVERNANCE_FIELDS | _SCHEDULE_RUNTIME_FIELDS:
		if fieldname not in {"status", "enabled"}:
			doc.set(fieldname, None)


def validate_report_schedule(doc, method: str | None = None) -> None:
	del method
	if str(doc.get("status") or "Draft") == "Draft":
		doc.required_indicator_codes_json = _canonical_json(_required_indicator_codes(doc))
	_validate_schedule_definition(doc, require_active_policy=False)
	previous = doc.get_doc_before_save()
	if previous is None:
		if str(doc.get("requested_by") or "") != _named_user():
			frappe.throw(
				"Report schedule requester must match the named creating user.",
				frappe.PermissionError,
			)
		if str(doc.get("status") or "") != "Draft" or int(doc.get("enabled") or 0):
			frappe.throw("New report schedules must start as disabled Draft records.")
		if any(
			doc.get(fieldname) not in (None, "", 0, "0")
			for fieldname in (_SCHEDULE_GOVERNANCE_FIELDS | _SCHEDULE_RUNTIME_FIELDS) - {"status", "enabled"}
		):
			frappe.throw("New report schedules cannot contain approval or runtime state.")
		return

	if str(doc.get("requested_by") or "") != str(previous.get("requested_by") or ""):
		frappe.throw("Report schedule requester is immutable.")
	changed_semantics = _changed_fields(doc, previous, frozenset(_SCHEDULE_SEMANTIC_FIELDS))
	changed_governance = _changed_fields(doc, previous, _SCHEDULE_GOVERNANCE_FIELDS)
	changed_runtime = _changed_fields(doc, previous, _SCHEDULE_RUNTIME_FIELDS)
	transition_capability = (
		getattr(frappe.flags, "ione_report_schedule_transition", None) is _SCHEDULE_TRANSITION_CAPABILITY
	)
	runtime_capability = (
		getattr(frappe.flags, "ione_report_schedule_runtime", None) is _SCHEDULE_RUNTIME_CAPABILITY
	)

	previous_status = str(previous.get("status") or "")
	if previous_status == "Draft":
		if changed_runtime:
			frappe.throw("Draft report schedules cannot contain runtime state.")
		if changed_governance and not transition_capability:
			frappe.throw("Report schedule review state may only change through the governed API.")
		if changed_semantics - {"coverage_start_period"} and transition_capability:
			frappe.throw("Review cannot alter report schedule semantics.")
	else:
		if changed_semantics:
			frappe.throw("Reviewed report schedule semantics are immutable; create a new schedule.")
		if changed_governance and not transition_capability:
			frappe.throw("Report schedule state may only change through the governed API.")
		if changed_runtime and not runtime_capability:
			frappe.throw("Report schedule runtime state is system-managed.")
	if changed_runtime and not runtime_capability:
		frappe.throw("Report schedule runtime state is system-managed.")
	if str(doc.get("status") or "") == "Approved":
		_assert_approval_checksum(doc)
		if not doc.get("reviewed_by") or not doc.get("reviewed_at") or not doc.get("coverage_start_period"):
			frappe.throw("Approved report schedules require review provenance.")
	if str(doc.get("status") or "") != "Approved" and int(doc.get("enabled") or 0):
		frappe.throw("Only Approved report schedules may be enabled.")


def prevent_report_schedule_deletion(doc, method: str | None = None) -> None:
	del doc, method
	frappe.throw("Quality report schedules are governance records and cannot be deleted.")


def validate_quality_report_snapshot(doc, method: str | None = None) -> None:
	del method
	if not doc.is_new():
		frappe.throw("Quality report snapshots are immutable.")
	if getattr(frappe.flags, "ione_report_snapshot_creation", None) is not _SNAPSHOT_CREATION_CAPABILITY:
		frappe.throw(
			"Quality report snapshots may only be created by the governed dispatcher.",
			frappe.PermissionError,
		)
	if (
		not doc.get("schedule")
		or not doc.get("hospital")
		or not doc.get("generated_for")
		or not doc.get("report_reviewer")
	):
		frappe.throw("Quality report snapshot provenance and hospital scope are required.")
	if not doc.get("period_start") or not doc.get("period_end"):
		frappe.throw("Quality report snapshot period provenance is incomplete.")
	start = getdate(doc.get("period_start"))
	end = getdate(doc.get("period_end"))
	if not _is_closed_calendar_month(start, end):
		frappe.throw("Quality report snapshot period must be one closed calendar month.")
	if not _is_sha256(str(doc.get("snapshot_key") or "")):
		frappe.throw("Quality report snapshot key is invalid.")
	schedule = frappe.get_doc("IONE AI Report Schedule", doc.get("schedule"))
	if str(schedule.get("status") or "") != "Approved" or not int(schedule.get("enabled") or 0):
		frappe.throw("Quality report snapshots require an enabled Approved schedule.")
	_assert_approval_checksum(schedule)
	_validate_existing_snapshot(doc, schedule, start, end)


def prevent_quality_report_snapshot_deletion(doc, method: str | None = None) -> None:
	del doc, method
	frappe.throw("Quality report snapshots cannot be cancelled or deleted.")


def validate_report_recovery_authorization(doc, method: str | None = None) -> None:
	"""Accept only one capability-created, immutable monthly-report recovery receipt."""
	del method
	if not doc.is_new():
		frappe.throw("Quality report recovery authorizations are immutable.")
	if getattr(frappe.flags, "ione_report_recovery_creation", None) is not _RECOVERY_CREATION_CAPABILITY:
		frappe.throw(
			"Quality report recovery authorizations may only be created by the governed API.",
			frappe.PermissionError,
		)
	schedule = frappe.get_doc("IONE AI Report Schedule", doc.get("schedule"))
	_assert_recovery_authorization_integrity(
		doc,
		schedule,
		require_current_runtime=True,
	)


def prevent_report_recovery_authorization_deletion(
	doc,
	method: str | None = None,
) -> None:
	del doc, method
	frappe.throw("Quality report recovery authorizations cannot be cancelled or deleted.")


def validated_quality_report_snapshot_for_task(task) -> tuple[Any, dict[str, Any]]:
	"""Load an immutable scheduled-report snapshot and revalidate all provenance."""
	if str(task.get("task_type") or "") != "Quality Report" or str(task.get("origin") or "") != "Scheduled":
		frappe.throw(
			"Quality summary snapshots are only available to scheduled Quality Report tasks.",
			frappe.PermissionError,
		)
	snapshot_name = str(task.get("report_snapshot") or "")
	schedule_name = str(task.get("report_schedule") or "")
	if not snapshot_name or not schedule_name:
		frappe.throw("Scheduled Quality Report snapshot provenance is incomplete.")
	if not task.get("report_period_start") or not task.get("report_period_end"):
		frappe.throw("Scheduled Quality Report period provenance is incomplete.")
	period_start = getdate(task.get("report_period_start"))
	period_end = getdate(task.get("report_period_end"))
	if not _is_closed_calendar_month(period_start, period_end):
		frappe.throw("Scheduled Quality Report period is invalid.")

	schedule = frappe.get_doc("IONE AI Report Schedule", schedule_name)
	if str(schedule.get("status") or "") != "Approved" or not int(schedule.get("enabled") or 0):
		frappe.throw("Scheduled Quality Report execution is disabled because its schedule is not active.")
	_validate_schedule_definition(schedule, require_active_policy=True)
	_assert_approval_checksum(schedule)
	if not hmac.compare_digest(
		str(task.get("schedule_approval_checksum") or ""),
		str(schedule.get("approval_checksum") or ""),
	):
		frappe.throw("Scheduled Quality Report approval provenance has drifted.")
	_validate_task_dispatch_provenance(schedule, task, period_start, period_end)
	if any(
		str(task.get(fieldname) or "") != str(schedule.get(fieldname) or "")
		for fieldname in (
			"requested_by",
			"report_reviewer",
			"hospital",
			"campus",
			"department",
			"ward",
		)
	):
		frappe.throw("Scheduled Quality Report task no longer matches its approved schedule.")
	requester = _eligible_user(str(schedule.get("requested_by") or ""), _SCHEDULE_REQUESTER_ROLES)
	reviewer = _eligible_user(str(schedule.get("report_reviewer") or ""), _REPORT_REVIEWER_ROLES)
	scope = _schedule_scope(schedule)
	require_scope_read(**scope, user=requester)
	require_scope_read(**scope, user=reviewer)

	snapshot = frappe.get_doc("IONE Quality Report Snapshot", snapshot_name)
	_validate_existing_snapshot(snapshot, schedule, period_start, period_end)
	data = _validated_snapshot_payload(snapshot)
	return snapshot, data


def review_report_schedule(
	schedule: str,
	decision: str,
	review_comment: str,
) -> dict[str, str]:
	user = _named_role_user(_SCHEDULE_REVIEW_ROLES)
	if decision not in {"Approve", "Reject"}:
		frappe.throw("Report schedule decision must be Approve or Reject.")
	comment = _bounded_comment(review_comment)
	with frappe.db.advisory_lock(f"ione-qms:report-schedule-review:{schedule}", timeout=10):
		doc = frappe.get_doc("IONE AI Report Schedule", schedule, for_update=True)
		doc.check_permission("read")
		if str(doc.get("status") or "") != "Draft":
			frappe.throw("Report schedule has already been reviewed.")
		if str(doc.get("requested_by") or "") == user:
			frappe.throw("Report schedule requesters cannot approve their own schedule.")
		if decision == "Approve":
			_validate_schedule_definition(doc, require_active_policy=True)
			doc.status = "Approved"
			doc.enabled = 1
			doc.coverage_start_period = _previous_month(getdate(nowdate()))[0]
			doc.approval_checksum = _schedule_checksum(doc)
		else:
			doc.status = "Rejected"
			doc.enabled = 0
			doc.coverage_start_period = None
			doc.approval_checksum = None
		doc.reviewed_by = user
		doc.reviewed_at = now_datetime()
		doc.review_comment = comment
		with _schedule_transition():
			doc.save(ignore_permissions=True)
		frappe.db.commit()
	return {"schedule": doc.name, "status": str(doc.status)}


def change_report_schedule_state(
	schedule: str,
	action: str,
	operation_comment: str,
) -> dict[str, str]:
	user = _named_role_user(_SCHEDULE_OPERATION_ROLES)
	if action not in {"Suspend", "Resume", "Retire"}:
		frappe.throw("Report schedule action must be Suspend, Resume, or Retire.")
	comment = _bounded_comment(operation_comment)
	with frappe.db.advisory_lock(f"ione-qms:report-schedule-operation:{schedule}", timeout=10):
		doc = frappe.get_doc("IONE AI Report Schedule", schedule, for_update=True)
		doc.check_permission("read")
		current = str(doc.get("status") or "")
		targets = {
			("Approved", "Suspend"): ("Suspended", 0),
			("Suspended", "Resume"): ("Approved", 1),
			("Approved", "Retire"): ("Retired", 0),
			("Suspended", "Retire"): ("Retired", 0),
		}
		target = targets.get((current, action))
		if target is None:
			frappe.throw(f"Report schedule cannot {action.lower()} from {current}.")
		if action == "Resume":
			_validate_schedule_definition(doc, require_active_policy=True)
			_assert_approval_checksum(doc)
		doc.status, doc.enabled = target
		doc.operated_by = user
		doc.operated_at = now_datetime()
		doc.operation_comment = comment
		with _schedule_transition():
			doc.save(ignore_permissions=True)
		frappe.db.commit()
	return {"schedule": doc.name, "status": str(doc.status)}


def authorize_report_recovery(
	schedule: str,
	prior_task: str,
	reason_code: str,
	reason: str,
) -> dict[str, str | int]:
	"""Authorize one immutable replacement task for the current terminal month."""
	user = _named_user()
	expected_prior_task = str(prior_task or "").strip()
	if not expected_prior_task:
		frappe.throw("Recovery authorization must identify the terminal task seen by the reviewer.")
	code = _normalized_recovery_reason_code(reason_code)
	comment = _bounded_recovery_reason(reason)
	lock_name = f"ione-qms:monthly-report:{schedule}"
	with frappe.db.advisory_lock(lock_name, timeout=10):
		doc = frappe.get_doc("IONE AI Report Schedule", schedule, for_update=True)
		doc.check_permission("read")
		if str(doc.get("status") or "") != "Approved" or not int(doc.get("enabled") or 0):
			frappe.throw("Only an enabled Approved quality report schedule can be recovered.")
		_validate_schedule_definition(doc, require_active_policy=True)
		_assert_approval_checksum(doc)
		reviewer = _eligible_user(str(doc.get("report_reviewer") or ""), _REPORT_REVIEWER_ROLES)
		if user != reviewer:
			frappe.throw(
				"Only the exact configured report reviewer may authorize recovery.",
				frappe.PermissionError,
			)
		if user == str(doc.get("requested_by") or ""):
			frappe.throw("The report requester cannot authorize report recovery.")

		period_start, period_end, current_task = _current_recovery_target(doc)
		replay = _idempotent_recovery_replay(
			doc,
			expected_prior_task,
			authorized_by=user,
			reason_code=code,
			reason=comment,
		)
		if replay:
			# A Pending replay may have registered another deterministic queue
			# callback. Commit the read transaction so the repair can run.
			frappe.db.commit()
			return replay
		if expected_prior_task != str(current_task.name):
			frappe.throw(
				"The monthly report recovery target changed; reload the schedule before authorizing.",
				frappe.ValidationError,
			)
		task_state = _existing_dispatch_task_state(
			doc,
			current_task,
			period_start,
			period_end,
		)
		if task_state == "waiting":
			frappe.throw("The current monthly report task is still running and cannot be recovered.")
		if task_state == "completed":
			frappe.throw("The current monthly report task completed and does not require recovery.")
		if task_state != "failed":
			frappe.throw("The current monthly report task is not in a recoverable terminal state.")

		snapshot, _payload = validated_quality_report_snapshot_for_task(current_task)
		sequence = int(current_task.get("recovery_sequence") or 0) + 1
		existing_receipts = _period_recovery_receipts(doc, period_start, period_end)
		if len(existing_receipts) != sequence - 1:
			frappe.throw("Quality report recovery authorization chain is incomplete.")
		if sequence > REPORT_MAX_RECOVERY_AUTHORIZATIONS:
			incident = _ensure_recovery_exhaustion_incident(
				doc,
				current_task,
				period_start,
				period_end,
				detected_by=user,
			)
			_update_schedule_runtime(
				doc,
				period_start=period_start,
				period_end=period_end,
				dispatch_key=str(current_task.get("dispatch_key") or ""),
				task=str(current_task.name),
				result="Failed",
				error_code="RECOVERY_LIMIT_EXHAUSTED",
			)
			frappe.db.commit()
			return {
				"schedule": str(doc.name),
				"task": str(current_task.name),
				"incident": incident,
				"recovery_sequence": sequence - 1,
				"status": "Exhausted",
			}

		authorized_at = now_datetime().replace(microsecond=0)
		authorization_key = _recovery_authorization_key(
			doc,
			current_task,
			snapshot,
			period_start,
			period_end,
			sequence,
		)
		recovery_dispatch_key = _recovery_dispatch_key(authorization_key)
		receipt = frappe.get_doc(
			{
				"doctype": REPORT_RECOVERY_DOCTYPE,
				"authorization_key": authorization_key,
				"schedule": doc.name,
				"schedule_approval_checksum": doc.approval_checksum,
				"report_snapshot": snapshot.name,
				"period_start": period_start,
				"period_end": period_end,
				**_schedule_scope(doc),
				"prior_task": current_task.name,
				"prior_terminal_status": str(current_task.get("status") or ""),
				"prior_dispatch_key": str(current_task.get("dispatch_key") or ""),
				"recovery_sequence": sequence,
				"authorized_by": user,
				"authorized_at": authorized_at,
				"reason_code": code,
				"reason": comment,
				"recovery_dispatch_key": recovery_dispatch_key,
			}
		)
		receipt.authorization_checksum = _recovery_checksum(receipt)

		savepoint = "ione_monthly_report_recovery"
		frappe.db.savepoint(savepoint)
		try:
			with _recovery_creation():
				receipt.insert(ignore_permissions=True)
			with _accountable_user(str(doc.requested_by)):
				recovery_task = _create_scheduled_task(
					doc,
					snapshot,
					period_start,
					period_end,
					recovery_dispatch_key,
					prior_task=current_task,
					recovery_authorization=receipt,
				)
			_update_schedule_runtime(
				doc,
				period_start=period_start,
				period_end=period_end,
				dispatch_key=recovery_dispatch_key,
				task=recovery_task.name,
				result="Success",
				error_code=None,
			)
			_enqueue_scheduled_task_after_commit(recovery_task)
		except Exception:
			# A full rollback also clears any enqueue-after-commit callback that
			# may have been registered immediately before the failure.
			frappe.db.rollback()
			raise
		frappe.db.release_savepoint(savepoint)
		frappe.db.commit()
		return {
			"schedule": str(doc.name),
			"authorization": str(receipt.name),
			"task": str(recovery_task.name),
			"recovery_sequence": sequence,
			"status": "Created",
		}


def dispatch_monthly_quality_reports() -> dict[str, int]:
	"""Create one idempotent, scoped report task per approved due schedule."""
	require_post_migrate_runtime_ready("dispatch monthly quality reports")
	if not ai_runtime_enabled():
		return {"due": 0, "created": 0, "existing": 0, "failed": 0, "disabled": 1}
	today = getdate(nowdate())
	rows = frappe.get_all(
		"IONE AI Report Schedule",
		filters={"status": "Approved", "enabled": 1, "run_day": ["<=", today.day]},
		pluck="name",
		order_by="name asc",
		limit_page_length=REPORT_SCHEDULE_LIMIT + 1,
	)
	if len(rows) > REPORT_SCHEDULE_LIMIT:
		frappe.throw(
			f"Due quality report schedules exceed the hard limit of {REPORT_SCHEDULE_LIMIT}.",
			frappe.ValidationError,
		)
	counts = {"due": len(rows), "created": 0, "existing": 0, "failed": 0, "disabled": 0}
	for schedule_name in rows:
		dispatch_failure = None
		try:
			result = _dispatch_one_schedule(str(schedule_name), today)
		except Exception as exc:
			# A deleted, corrupt, or concurrently changed schedule must not prevent
			# other approved schedules from running.  Do not include exception text
			# or the schedule name in the operational log because both are
			# administrator-controlled and may contain sensitive content.
			frappe.db.rollback()
			dispatch_failure = (
				_dispatch_error_code(exc),
				hashlib.sha256(str(schedule_name).encode()).hexdigest()[:16],
			)
			result = "failed"
		if dispatch_failure:
			frappe.log_error(
				message=(
					"Monthly quality-report dispatch failed before a governed runtime "
					f"result could be recorded. code={dispatch_failure[0]} "
					f"schedule_hash={dispatch_failure[1]}"
				),
				title="IONE monthly quality-report dispatch failure",
			)
		counts[result] += 1
	return counts


def _dispatch_one_schedule(schedule_name: str, today: date) -> str:
	latest_period_start, latest_period_end = _previous_month(today)
	period_start, period_end = latest_period_start, latest_period_end
	lock_name = f"ione-qms:monthly-report:{schedule_name}"
	with frappe.db.advisory_lock(lock_name, timeout=10):
		schedule = frappe.get_doc("IONE AI Report Schedule", schedule_name, for_update=True)
		period_start, period_end = _runtime_failure_period(
			schedule,
			latest_period_start,
			latest_period_end,
		)
		if (
			str(schedule.get("status") or "") != "Approved"
			or not int(schedule.get("enabled") or 0)
			or int(schedule.get("run_day") or 0) > today.day
		):
			return "existing"
		try:
			_validate_schedule_definition(schedule, require_active_policy=True)
			_assert_approval_checksum(schedule)
			current_state = _current_runtime_task_state(schedule)
			if current_state == "failed":
				error_code = "EXISTING_TASK_TERMINAL"
				current_task = frappe.get_doc(
					"IONE AI Analysis Task",
					str(schedule.get("last_task") or ""),
				)
				if int(current_task.get("recovery_sequence") or 0) >= (REPORT_MAX_RECOVERY_AUTHORIZATIONS):
					_ensure_recovery_exhaustion_incident(
						schedule,
						current_task,
						getdate(schedule.get("last_period_start")),
						getdate(schedule.get("last_period_end")),
						detected_by=None,
					)
					error_code = "RECOVERY_LIMIT_EXHAUSTED"
				_update_schedule_runtime(
					schedule,
					period_start=getdate(schedule.get("last_period_start")),
					period_end=getdate(schedule.get("last_period_end")),
					dispatch_key=str(schedule.get("last_dispatch_key") or ""),
					task=str(schedule.get("last_task") or ""),
					result="Failed",
					error_code=error_code,
				)
				frappe.db.commit()
				return "failed"
			due_period = _next_due_period(
				schedule,
				latest_period_start,
				latest_period_end,
			)
			if due_period is None:
				return "existing"
			period_start, period_end = due_period
			dispatch_key = _dispatch_key(schedule, period_start, period_end)
			existing = frappe.db.get_value(
				"IONE AI Analysis Task",
				{"dispatch_key": dispatch_key},
				"name",
			)
			if existing:
				existing_task = frappe.get_doc("IONE AI Analysis Task", existing)
				existing_state = _existing_dispatch_task_state(
					schedule,
					existing_task,
					period_start,
					period_end,
				)
				if existing_state == "failed":
					_update_schedule_runtime(
						schedule,
						period_start=period_start,
						period_end=period_end,
						dispatch_key=dispatch_key,
						task=str(existing),
						result="Failed",
						error_code="EXISTING_TASK_TERMINAL",
					)
					frappe.db.commit()
					return "failed"
				_update_schedule_runtime(
					schedule,
					period_start=period_start,
					period_end=period_end,
					dispatch_key=dispatch_key,
					task=str(existing),
					result="Success",
					error_code=None,
				)
				frappe.db.commit()
				return "existing"
			savepoint = "ione_monthly_report_dispatch"
			frappe.db.savepoint(savepoint)
			try:
				with _accountable_user(str(schedule.requested_by)):
					snapshot = _get_or_create_snapshot(schedule, period_start, period_end)
					task = _create_scheduled_task(
						schedule,
						snapshot,
						period_start,
						period_end,
						dispatch_key,
					)
				_update_schedule_runtime(
					schedule,
					period_start=period_start,
					period_end=period_end,
					dispatch_key=dispatch_key,
					task=task.name,
					result="Success",
					error_code=None,
				)
				_enqueue_scheduled_task_after_commit(task)
			except Exception as exc:
				# A savepoint rollback is insufficient for Frappe transaction
				# callbacks. Clear the whole unit before recording a no-task
				# failure, so a rolled-back task can never be enqueued.
				frappe.db.rollback()
				schedule = frappe.get_doc(
					"IONE AI Report Schedule",
					schedule_name,
					for_update=True,
				)
				_update_schedule_runtime(
					schedule,
					period_start=period_start,
					period_end=period_end,
					dispatch_key=dispatch_key,
					task=None,
					result="Failed",
					error_code=_dispatch_error_code(exc),
				)
				frappe.db.commit()
				return "failed"
			frappe.db.release_savepoint(savepoint)
			frappe.db.commit()
			return "created"
		except Exception as exc:
			frappe.db.rollback()
			schedule = frappe.get_doc(
				"IONE AI Report Schedule",
				schedule_name,
				for_update=True,
			)
			preserved_task = None
			preserved_dispatch_key = None
			if (
				schedule.get("last_task")
				and schedule.get("last_dispatch_key")
				and schedule.get("last_period_start")
				and schedule.get("last_period_end")
				and str(schedule.get("last_period_start")) == period_start.isoformat()
				and str(schedule.get("last_period_end")) == period_end.isoformat()
			):
				preserved_task = str(schedule.get("last_task") or "")
				preserved_dispatch_key = str(schedule.get("last_dispatch_key") or "")
			try:
				_update_schedule_runtime(
					schedule,
					period_start=period_start,
					period_end=period_end,
					dispatch_key=preserved_dispatch_key,
					task=preserved_task,
					result="Failed",
					error_code=_dispatch_error_code(exc),
				)
				frappe.db.commit()
			except Exception:
				# The task/snapshot transaction has already been rolled back.
				# Leave no dirty in-memory runtime pointer if failure recording
				# itself is unavailable; the dispatcher emits a sanitized alert.
				frappe.db.rollback()
			return "failed"


def _get_or_create_snapshot(schedule, period_start: date, period_end: date):
	snapshot_key = _snapshot_key(schedule, period_start, period_end)
	existing = frappe.db.get_value(
		"IONE Quality Report Snapshot",
		{"snapshot_key": snapshot_key},
		"name",
	)
	if existing:
		doc = frappe.get_doc("IONE Quality Report Snapshot", existing)
		_validate_existing_snapshot(doc, schedule, period_start, period_end)
		return doc
	# Persist a second-precision timestamp so the timestamp embedded in the
	# canonical JSON remains byte-stable across MariaDB/Frappe round trips.
	snapshot_at = now_datetime().replace(microsecond=0)
	data = _quality_summary_data(schedule, period_start, period_end, snapshot_at)
	data_json = _canonical_json(data)
	if len(data_json.encode()) > REPORT_SNAPSHOT_MAX_BYTES:
		frappe.throw("Quality report summary exceeds the 256 KiB snapshot limit.")
	doc = frappe.get_doc(
		{
			"doctype": "IONE Quality Report Snapshot",
			"snapshot_key": snapshot_key,
			"schedule": schedule.name,
			"period_start": period_start,
			"period_end": period_end,
			"hospital": schedule.hospital,
			"campus": schedule.get("campus"),
			"department": schedule.get("department"),
			"ward": schedule.get("ward"),
			"generated_for": schedule.requested_by,
			"report_reviewer": schedule.report_reviewer,
			"generated_at": snapshot_at,
			# This is an application-level capture marker, not a database commit
			# watermark. Exact snapshot integrity is bound by data_json/data_hash.
			"source_cutoff": snapshot_at,
			"source_group_count": _snapshot_group_count(data),
			"data_json": data_json,
			"data_hash": hashlib.sha256(data_json.encode()).hexdigest(),
		}
	)
	with _snapshot_creation():
		doc.insert(ignore_permissions=True)
	return doc


def _quality_summary_data(
	schedule,
	period_start: date,
	period_end: date,
	snapshot_at,
) -> dict[str, Any]:
	scope = _schedule_scope(schedule)
	require_scope_read(**scope, user=schedule.requested_by)
	require_scope_read(**scope, user=schedule.report_reviewer)
	if not schedule.get("required_indicator_codes_json"):
		frappe.throw("Quality report schedule has no approved required indicator contract.")
	required_codes = _required_indicator_codes(schedule)
	indicators = _permissioned_rows(
		"IONE QC Indicator",
		filters={"indicator_code": ["in", required_codes], "status": "Active"},
		fields=("name", "indicator_code", "indicator_name", "direction"),
		order_by="indicator_code asc, name asc",
		limit=len(required_codes),
	)
	indicator_map = {str(row.get("name")): row for row in indicators}
	approved_definition_codes = sorted(
		{str(row.get("indicator_code") or "") for row in indicators if row.get("indicator_code")}
	)
	if approved_definition_codes != required_codes:
		frappe.throw("Approved report indicator definitions are missing, retired, or inaccessible.")
	indicator_names = sorted(indicator_map)
	monthly_filters: dict[str, Any] = {
		"month": period_start,
		"indicator": ["in", indicator_names],
		"medical_staff": ["is", "not set"],
		**_monthly_fact_scope_filters(scope),
	}
	monthly_rows = _permissioned_rows(
		"IONE Monthly Quality Fact",
		filters=monthly_filters,
		fields=(
			"name",
			"indicator",
			"indicator_version",
			"numerator",
			"denominator",
			"indicator_value",
			"target_value",
			"target_min",
			"target_max",
			"status",
			"result_count",
		),
		order_by="indicator asc, indicator_version asc, name asc",
		limit=REPORT_SNAPSHOT_MAX_INDICATOR_ROWS,
	)
	unmapped_indicators = sorted(
		{
			str(row.get("indicator") or "")
			for row in monthly_rows
			if not indicator_map.get(str(row.get("indicator") or ""))
		}
	)
	if unmapped_indicators:
		frappe.throw("Quality report facts contain unmapped indicator definitions.")
	indicator_summary = [
		{
			"indicator_code": indicator_map.get(str(row.get("indicator")), {}).get("indicator_code"),
			"indicator_name": indicator_map.get(str(row.get("indicator")), {}).get("indicator_name"),
			"direction": indicator_map.get(str(row.get("indicator")), {}).get("direction"),
			"indicator_version": row.get("indicator_version"),
			"numerator": row.get("numerator"),
			"denominator": row.get("denominator"),
			"indicator_value": row.get("indicator_value"),
			"target_value": row.get("target_value"),
			"target_min": row.get("target_min"),
			"target_max": row.get("target_max"),
			"status": row.get("status"),
			"result_count": row.get("result_count"),
		}
		for row in monthly_rows
	]
	observed_codes = sorted(
		{str(row.get("indicator_code") or "") for row in indicator_summary if row.get("indicator_code")}
	)
	missing_codes = sorted(set(required_codes) - set(observed_codes))
	if observed_codes != required_codes:
		frappe.throw(
			"Quality report indicator facts do not exactly cover the approved indicator whitelist.",
			frappe.ValidationError,
		)

	finding_filters = _period_filters("detected_at", period_start, period_end, scope)
	_assert_permissioned_count("IONE QC Finding", finding_filters)
	finding_groups = _permissioned_rows(
		"IONE QC Finding",
		filters=finding_filters,
		fields=("severity", "status", "count(name) as total"),
		group_by="severity, status",
		order_by="severity asc, status asc",
		limit=REPORT_SNAPSHOT_MAX_GROUPS,
	)
	finding_names = _permissioned_rows(
		"IONE QC Finding",
		filters=finding_filters,
		fields=("name",),
		order_by="name asc",
		limit=REPORT_SNAPSHOT_MAX_FINDINGS,
	)
	finding_name_values = [str(row.get("name")) for row in finding_names]
	rectification_groups = (
		_permissioned_rows(
			"IONE QC Rectification",
			filters={
				"finding": ["in", finding_name_values],
				**{key: value for key, value in scope.items() if value},
			},
			fields=("status", "count(name) as total"),
			group_by="status",
			order_by="status asc",
			limit=REPORT_SNAPSHOT_MAX_GROUPS,
		)
		if finding_name_values
		else []
	)
	pdca_filters = {key: value for key, value in scope.items() if value}
	_assert_permissioned_count("IONE PDCA Project", pdca_filters)
	pdca_groups = _permissioned_rows(
		"IONE PDCA Project",
		filters=pdca_filters,
		fields=("status", "count(name) as total"),
		group_by="status",
		order_by="status asc",
		limit=REPORT_SNAPSHOT_MAX_GROUPS,
	)
	return {
		"contract_version": "1",
		"period": {
			"start": period_start.isoformat(),
			"end": period_end.isoformat(),
			"semantics": "closed calendar month",
		},
		"scope": scope,
		"generated_at": get_datetime(snapshot_at).isoformat(),
		"indicator_coverage": {
			"required_codes": required_codes,
			"observed_codes": observed_codes,
			"missing_codes": missing_codes,
			"complete": True,
		},
		"indicator_summary": indicator_summary,
		"finding_summary": [_clean_group(row, ("severity", "status", "total")) for row in finding_groups],
		"rectification_summary": [_clean_group(row, ("status", "total")) for row in rectification_groups],
		"pdca_current_summary": [_clean_group(row, ("status", "total")) for row in pdca_groups],
		"semantic_notes": {
			"findings": "Cohort detected inside the closed calendar month.",
			"rectifications": "Current states linked to that finding cohort.",
			"pdca": "Current scoped project states at snapshot time; not a monthly cohort.",
			"patient_identifiers": "Not included.",
			"capture_marker": (
				"generated_at/source_cutoff is an application capture marker, not an MVCC "
				"commit watermark; canonical content integrity is enforced by data_hash."
			),
		},
	}


def _create_scheduled_task(
	schedule,
	snapshot,
	period_start: date,
	period_end: date,
	dispatch_key: str,
	*,
	prior_task=None,
	recovery_authorization=None,
):
	if str(frappe.session.user or "") != str(schedule.requested_by):
		frappe.throw("Scheduled task creation is not running as its accountable requester.")
	policy = frappe.get_doc("IONE Agent Policy", schedule.policy)
	policy.check_permission("read")
	task_scope = _schedule_scope(schedule)
	runtime_task = {
		"task_type": "Quality Report",
		**task_scope,
		"record_count": 1,
		"patient": None,
		"encounter": None,
	}
	validate_policy_runtime(policy, runtime_task)
	summary = (
		f"Generate the approved {schedule.report_type} quality-report draft for the exact closed "
		f"calendar period {period_start.isoformat()} through {period_end.isoformat()}. "
		"Read only the task-bound immutable quality summary with ione_get_quality_summary. "
		"Cite its evidence_reference in the report draft. Do not include patient identifiers, "
		"invent missing causes, publish the report, approve records, or close findings."
	)
	recovery_provenance: dict[str, Any] = {}
	if prior_task is not None or recovery_authorization is not None:
		if prior_task is None or recovery_authorization is None:
			raise ValueError("Recovery task provenance must be supplied as one complete set.")
		recovery_provenance = {
			"prior_report_task": prior_task.name,
			"report_recovery_authorization": recovery_authorization.name,
			"recovery_sequence": int(recovery_authorization.recovery_sequence),
			"recovery_authorization_checksum": recovery_authorization.authorization_checksum,
		}
	doc = frappe.get_doc(
		{
			"doctype": "IONE AI Analysis Task",
			"task_type": "Quality Report",
			"policy": schedule.policy,
			"requested_by": schedule.requested_by,
			"requested_at": now_datetime(),
			**task_scope,
			"record_count": 1,
			"input_summary": summary,
			"input_hash": hashlib.sha256(summary.encode()).hexdigest(),
			"data_classification": "Hospital Internal",
			"requires_human_review": 1,
			"origin": "Scheduled",
			"report_schedule": schedule.name,
			"report_snapshot": snapshot.name,
			"report_period_start": period_start,
			"report_period_end": period_end,
			"report_reviewer": schedule.report_reviewer,
			"dispatch_key": dispatch_key,
			"schedule_approval_checksum": schedule.approval_checksum,
			**recovery_provenance,
			"status": "Pending",
		}
	)
	with controlled_analysis_task_creation(doc):
		doc.flags.skip_ai_enqueue = True
		doc.insert(ignore_permissions=True)
	return doc


def _enqueue_scheduled_task_after_commit(task) -> None:
	"""Register a Redis-free callback; a queue outage must not poison DB commit."""
	task_name = str(task.name)
	attempt_hint = int(task.get("execution_attempt") or 0) + 1

	def enqueue_committed_task() -> None:
		queue_failure_code = None
		try:
			from ione_qms.ai.orchestrator import _enqueue_task_name

			_enqueue_task_name(
				task_name,
				attempt_hint=attempt_hint,
				enqueue_after_commit=False,
			)
		except Exception as exc:
			# The task remains durably Pending and the bounded Pending sweep will
			# retry it. Leave the active exception context before logging so Frappe
			# cannot persist traceback locals containing governed report input.
			queue_failure_code = _dispatch_error_code(exc)
		if not queue_failure_code:
			return
		try:
			frappe.log_error(
				title="IONE monthly report queue callback failed",
				message=(
					f"code={queue_failure_code}: one governed task remains Pending "
					"for deterministic retry. No report input or model output was logged."
				),
				reference_doctype="IONE AI Analysis Task",
				reference_name=task_name,
			)
		except Exception:
			# A logging outage must never poison the already-committed task.
			return

	frappe.db.after_commit.add(enqueue_committed_task)


def _validate_schedule_definition(doc, *, require_active_policy: bool) -> None:
	if str(doc.get("report_type") or "") not in {
		"Hospital Monthly",
		"Department Monthly",
		"Quality Meeting",
	}:
		frappe.throw("Quality report schedule report_type is invalid.")
	run_day = int(doc.get("run_day") or 0)
	if run_day < 2 or run_day > 28:
		frappe.throw("Quality report schedule run_day must be from 2 through 28.")
	if str(doc.get("status") or "") in {"Approved", "Suspended", "Retired"}:
		_coverage_start_period(doc, _previous_month(getdate(nowdate()))[0])
	if not doc.get("hospital"):
		frappe.throw("Quality report schedules require a hospital scope.")
	report_type = str(doc.get("report_type") or "")
	if report_type == "Hospital Monthly" and any(
		doc.get(fieldname) for fieldname in ("campus", "department", "ward")
	):
		frappe.throw("Hospital monthly schedules must use hospital-only scope.")
	if report_type == "Department Monthly":
		if not doc.get("department"):
			frappe.throw("Department monthly schedules require a department.")
		if doc.get("ward"):
			frappe.throw("Department monthly schedules cannot narrow the approved scope to one ward.")
	required_codes = _required_indicator_codes(doc)
	if str(doc.get("required_indicator_codes_json") or "") != _canonical_json(required_codes):
		frappe.throw("Required indicator codes must use canonical sorted JSON.")
	active_rows = frappe.get_all(
		"IONE QC Indicator",
		filters={"indicator_code": ["in", required_codes], "status": "Active"},
		fields=["indicator_code"],
		limit_page_length=REPORT_SNAPSHOT_MAX_INDICATOR_ROWS + 1,
	)
	active_codes = {str(row.get("indicator_code") or "") for row in active_rows}
	if active_codes != set(required_codes):
		frappe.throw("Every required report indicator code must identify one Active indicator.")
	requester = _eligible_user(str(doc.get("requested_by") or ""), _SCHEDULE_REQUESTER_ROLES)
	reviewer = _eligible_user(str(doc.get("report_reviewer") or ""), _REPORT_REVIEWER_ROLES)
	if requester == reviewer:
		frappe.throw("Quality report requester and report reviewer must be different named users.")
	scope = _schedule_scope(doc)
	require_scope_read(**scope, user=requester)
	require_scope_read(**scope, user=reviewer)
	policy_name = str(doc.get("policy") or "").strip()
	if not policy_name:
		frappe.throw("Quality report schedules require a Quality Report Agent Policy.")
	policy = frappe.get_doc("IONE Agent Policy", policy_name)
	if str(policy.get("agent_category") or "") != "Quality Report":
		frappe.throw("Quality report schedules require a Quality Report Agent Policy.")
	if require_active_policy and str(policy.get("status") or "") != "Active":
		frappe.throw("Quality report schedule policy must be Active.")
	if (
		not int(policy.get("allow_scheduled_run") or 0)
		or int(policy.get("contains_patient_data") or 0)
		or not int(policy.get("requires_human_review") or 0)
	):
		frappe.throw(
			"Scheduled quality reports require non-patient, human-reviewed scheduled policy controls."
		)
	if str(policy.get("service_user") or "") in {requester, reviewer, "", "Administrator"}:
		frappe.throw("Quality report requester/reviewer must be independent of the Agent service user.")
	safe_tools = {"ione_get_quality_summary", "ione_create_report_draft"}
	allowed_tools = {str(row.tool) for row in policy.get("allowed_tools") or []}
	if allowed_tools != safe_tools:
		frappe.throw("Quality report policy must allow exactly the two aggregate report tools.")
	if not policy.get("flow_agent"):
		frappe.throw("Quality report policy has no governed Flow Agent.")
	agent = frappe.get_doc("Flow Agent", policy.flow_agent)
	agent_tools = {str(row.tool) for row in agent.get("tools") or []}
	if agent_tools != safe_tools:
		frappe.throw("Quality report Flow Agent must expose exactly the two aggregate report tools.")


def _schedule_scope(doc) -> dict[str, str | None]:
	return {
		"hospital": doc.get("hospital"),
		"campus": doc.get("campus"),
		"department": doc.get("department"),
		"ward": doc.get("ward"),
	}


def _monthly_fact_scope_filters(scope: Mapping[str, str | None]) -> dict[str, Any]:
	"""Select the exact organization aggregation level approved by the schedule."""
	filters: dict[str, Any] = {"hospital": scope.get("hospital")}
	campus = scope.get("campus")
	department = scope.get("department")
	ward = scope.get("ward")
	if campus:
		filters["campus"] = campus
	elif not department and not ward:
		filters["campus"] = ["is", "not set"]
	if department:
		filters["department"] = department
	elif not ward:
		filters["department"] = ["is", "not set"]
	if ward:
		filters["ward"] = ward
	else:
		filters["ward"] = ["is", "not set"]
	return filters


def _required_indicator_codes(doc) -> list[str]:
	raw = doc.get("required_indicator_codes_json")
	if not isinstance(raw, str) or not raw.strip() or len(raw.encode()) > 40_000:
		frappe.throw("Required indicator codes must be one bounded non-empty JSON array.")
	try:
		decoded = json.loads(raw)
	except ValueError as exc:
		raise frappe.ValidationError("Required indicator codes JSON is invalid.") from exc
	if not isinstance(decoded, list) or not decoded:
		frappe.throw("Required indicator codes must be one non-empty JSON array.")
	if len(decoded) > REPORT_SNAPSHOT_MAX_INDICATOR_ROWS:
		frappe.throw(
			f"Required indicator codes exceed the hard limit of {REPORT_SNAPSHOT_MAX_INDICATOR_ROWS}."
		)
	codes: list[str] = []
	for value in decoded:
		if not isinstance(value, str):
			frappe.throw("Every required indicator code must be a string.")
		code = value.strip()
		if (
			not code
			or len(code) > 140
			or any(ord(character) < 32 or ord(character) == 127 for character in code)
		):
			frappe.throw("Required indicator codes contain an invalid value.")
		codes.append(code)
	return sorted(set(codes))


def _schedule_checksum(doc) -> str:
	payload = {fieldname: doc.get(fieldname) for fieldname in _SCHEDULE_SEMANTIC_FIELDS}
	return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _assert_approval_checksum(doc) -> None:
	stored = str(doc.get("approval_checksum") or "")
	actual = _schedule_checksum(doc)
	if not _is_sha256(stored) or not hmac.compare_digest(stored, actual):
		frappe.throw("Quality report schedule approval checksum is missing or stale.")


def _dispatch_key(doc, period_start: date, period_end: date) -> str:
	return hashlib.sha256(
		f"{doc.name}|{doc.approval_checksum}|{period_start.isoformat()}|{period_end.isoformat()}".encode()
	).hexdigest()


def _recovery_authorization_key(
	schedule,
	prior_task,
	snapshot,
	period_start: date,
	period_end: date,
	sequence: int,
) -> str:
	return hashlib.sha256(
		(
			f"report-recovery-authorization|{schedule.name}|{schedule.approval_checksum}|"
			f"{period_start.isoformat()}|{period_end.isoformat()}|{snapshot.name}|"
			f"{prior_task.name}|{prior_task.get('status')}|{prior_task.get('dispatch_key')}|{sequence}"
		).encode()
	).hexdigest()


def _recovery_dispatch_key(authorization_key: str) -> str:
	return hashlib.sha256(f"report-recovery-dispatch|{authorization_key}".encode()).hexdigest()


def _recovery_checksum(doc) -> str:
	payload: dict[str, Any] = {}
	for fieldname in _RECOVERY_SEMANTIC_FIELDS:
		value = doc.get(fieldname)
		if fieldname in {"period_start", "period_end"}:
			value = getdate(value).isoformat() if value else None
		elif fieldname == "authorized_at":
			value = get_datetime(value).isoformat() if value else None
		elif fieldname == "recovery_sequence":
			value = int(value or 0)
		else:
			value = str(value) if value not in (None, "") else None
		payload[fieldname] = value
	return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _validate_task_dispatch_provenance(
	schedule,
	task,
	period_start: date,
	period_end: date,
) -> None:
	recovery_fields = (
		"prior_report_task",
		"report_recovery_authorization",
		"recovery_sequence",
		"recovery_authorization_checksum",
	)
	has_recovery = [task.get(fieldname) not in (None, "", 0, "0") for fieldname in recovery_fields]
	if not any(has_recovery):
		if not hmac.compare_digest(
			str(task.get("dispatch_key") or ""),
			_dispatch_key(schedule, period_start, period_end),
		):
			frappe.throw("Scheduled Quality Report dispatch provenance has drifted.")
		return
	if not all(has_recovery):
		frappe.throw("Scheduled Quality Report recovery provenance is incomplete.")

	receipt = frappe.get_doc(
		REPORT_RECOVERY_DOCTYPE,
		str(task.get("report_recovery_authorization") or ""),
	)
	_assert_recovery_authorization_integrity(
		receipt,
		schedule,
		require_current_runtime=False,
	)
	if (
		str(receipt.get("prior_task") or "") != str(task.get("prior_report_task") or "")
		or str(receipt.get("report_snapshot") or "") != str(task.get("report_snapshot") or "")
		or int(receipt.get("recovery_sequence") or 0) != int(task.get("recovery_sequence") or 0)
		or not hmac.compare_digest(
			str(receipt.get("authorization_checksum") or ""),
			str(task.get("recovery_authorization_checksum") or ""),
		)
		or not hmac.compare_digest(
			str(receipt.get("recovery_dispatch_key") or ""),
			str(task.get("dispatch_key") or ""),
		)
	):
		frappe.throw("Scheduled Quality Report recovery task provenance has drifted.")


def _assert_recovery_authorization_integrity(
	receipt,
	schedule,
	*,
	require_current_runtime: bool,
) -> None:
	required = {
		"authorization_key",
		"schedule",
		"schedule_approval_checksum",
		"report_snapshot",
		"period_start",
		"period_end",
		"hospital",
		"prior_task",
		"prior_terminal_status",
		"prior_dispatch_key",
		"recovery_sequence",
		"authorized_by",
		"authorized_at",
		"reason_code",
		"reason",
		"recovery_dispatch_key",
		"authorization_checksum",
	}
	if any(not receipt.get(fieldname) for fieldname in required):
		frappe.throw("Quality report recovery authorization provenance is incomplete.")
	if str(schedule.get("status") or "") != "Approved" or not int(schedule.get("enabled") or 0):
		frappe.throw("Quality report recovery authorization schedule is not active.")
	_validate_schedule_definition(schedule, require_active_policy=True)
	_assert_approval_checksum(schedule)
	if str(receipt.get("schedule") or "") != str(schedule.name) or not hmac.compare_digest(
		str(receipt.get("schedule_approval_checksum") or ""),
		str(schedule.get("approval_checksum") or ""),
	):
		frappe.throw("Quality report recovery authorization schedule provenance has drifted.")
	try:
		period_start = getdate(receipt.get("period_start"))
		period_end = getdate(receipt.get("period_end"))
		authorized_at = get_datetime(receipt.get("authorized_at"))
		sequence = int(receipt.get("recovery_sequence") or 0)
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError(
			"Quality report recovery authorization period or sequence is invalid."
		) from exc
	if not _is_closed_calendar_month(period_start, period_end) or not authorized_at:
		frappe.throw("Quality report recovery authorization period or timestamp is invalid.")
	if sequence < 1 or sequence > REPORT_MAX_RECOVERY_AUTHORIZATIONS:
		frappe.throw("Quality report recovery authorization sequence is outside its governed limit.")
	if any(
		str(receipt.get(fieldname) or "") != str(schedule.get(fieldname) or "")
		for fieldname in ("hospital", "campus", "department", "ward")
	):
		frappe.throw("Quality report recovery authorization scope has drifted.")
	authorized_by = _eligible_user(
		str(receipt.get("authorized_by") or ""),
		_REPORT_REVIEWER_ROLES,
	)
	if authorized_by != str(schedule.get("report_reviewer") or "") or authorized_by == str(
		schedule.get("requested_by") or ""
	):
		frappe.throw("Quality report recovery authorization actor is invalid.")
	if require_current_runtime and str(frappe.session.user or "") != authorized_by:
		frappe.throw(
			"Quality report recovery authorization must be inserted in its reviewer session.",
			frappe.PermissionError,
		)
	code = _normalized_recovery_reason_code(str(receipt.get("reason_code") or ""))
	comment = _bounded_recovery_reason(str(receipt.get("reason") or ""))
	if code != str(receipt.get("reason_code") or "") or comment != str(receipt.get("reason") or ""):
		frappe.throw("Quality report recovery authorization reason is not canonical.")

	prior_task = frappe.get_doc(
		"IONE AI Analysis Task",
		str(receipt.get("prior_task") or ""),
	)
	if str(prior_task.get("status") or "") not in _RECOVERY_TERMINAL_STATUSES:
		frappe.throw("Quality report recovery prior task is no longer terminal.")
	if str(prior_task.get("status") or "") != str(receipt.get("prior_terminal_status") or ""):
		frappe.throw("Quality report recovery prior terminal status has drifted.")
	if (
		str(prior_task.get("report_schedule") or "") != str(schedule.name)
		or str(prior_task.get("report_snapshot") or "") != str(receipt.get("report_snapshot") or "")
		or getdate(prior_task.get("report_period_start")) != period_start
		or getdate(prior_task.get("report_period_end")) != period_end
		or not hmac.compare_digest(
			str(prior_task.get("dispatch_key") or ""),
			str(receipt.get("prior_dispatch_key") or ""),
		)
	):
		frappe.throw("Quality report recovery prior task provenance has drifted.")
	try:
		expected_sequence = int(prior_task.get("recovery_sequence") or 0) + 1
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError("Quality report recovery prior task sequence is invalid.") from exc
	if sequence != expected_sequence:
		frappe.throw("Quality report recovery authorization sequence is not contiguous.")
	snapshot, _payload = validated_quality_report_snapshot_for_task(prior_task)
	expected_authorization_key = _recovery_authorization_key(
		schedule,
		prior_task,
		snapshot,
		period_start,
		period_end,
		sequence,
	)
	if not hmac.compare_digest(
		str(receipt.get("authorization_key") or ""),
		expected_authorization_key,
	):
		frappe.throw("Quality report recovery authorization key is invalid.")
	if not hmac.compare_digest(
		str(receipt.get("recovery_dispatch_key") or ""),
		_recovery_dispatch_key(expected_authorization_key),
	):
		frappe.throw("Quality report recovery dispatch key is invalid.")
	if not hmac.compare_digest(
		str(receipt.get("authorization_checksum") or ""),
		_recovery_checksum(receipt),
	):
		frappe.throw("Quality report recovery authorization checksum is stale.")
	if require_current_runtime and (
		str(schedule.get("last_task") or "") != str(prior_task.name)
		or getdate(schedule.get("last_period_start")) != period_start
		or getdate(schedule.get("last_period_end")) != period_end
		or not hmac.compare_digest(
			str(schedule.get("last_dispatch_key") or ""),
			str(prior_task.get("dispatch_key") or ""),
		)
	):
		frappe.throw("Quality report recovery target is no longer the current failed month.")


def _snapshot_key(doc, period_start: date, period_end: date) -> str:
	return hashlib.sha256(
		f"snapshot|{doc.name}|{doc.approval_checksum}|"
		f"{period_start.isoformat()}|{period_end.isoformat()}".encode()
	).hexdigest()


def _previous_month(today: date) -> tuple[date, date]:
	first_this_month = date(today.year, today.month, 1)
	end = first_this_month - timedelta(days=1)
	return date(end.year, end.month, 1), end


def _month_end(month_start: date) -> date:
	if month_start.month == 12:
		next_month = date(month_start.year + 1, 1, 1)
	else:
		next_month = date(month_start.year, month_start.month + 1, 1)
	return next_month - timedelta(days=1)


def _month_distance(older: date, newer: date) -> int:
	return (newer.year - older.year) * 12 + (newer.month - older.month)


def _coverage_start_period(schedule, latest_period_start: date) -> date:
	value = schedule.get("coverage_start_period")
	if not value:
		frappe.throw("Quality report schedule coverage start provenance is missing.")
	start = getdate(value)
	if start.day != 1 or start > latest_period_start:
		frappe.throw("Quality report schedule coverage start provenance is invalid.")
	return start


def _current_recovery_target(schedule) -> tuple[date, date, Any]:
	if (
		not schedule.get("last_period_start")
		or not schedule.get("last_period_end")
		or not schedule.get("last_task")
		or not schedule.get("last_dispatch_key")
	):
		frappe.throw("Quality report schedule has no terminal task eligible for recovery.")
	try:
		period_start = getdate(schedule.get("last_period_start"))
		period_end = getdate(schedule.get("last_period_end"))
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError("Quality report recovery period is invalid.") from exc
	if not _is_closed_calendar_month(period_start, period_end):
		frappe.throw("Quality report recovery period must be one closed calendar month.")
	task = frappe.get_doc(
		"IONE AI Analysis Task",
		str(schedule.get("last_task") or ""),
	)
	if not hmac.compare_digest(
		str(schedule.get("last_dispatch_key") or ""),
		str(task.get("dispatch_key") or ""),
	):
		frappe.throw("Quality report recovery runtime dispatch provenance has drifted.")
	return period_start, period_end, task


def _current_runtime_task_state(schedule) -> str | None:
	if not schedule.get("last_task"):
		return None
	period_start, period_end, task = _current_recovery_target(schedule)
	return _existing_dispatch_task_state(
		schedule,
		task,
		period_start,
		period_end,
	)


def _idempotent_recovery_replay(
	schedule,
	prior_task: str,
	*,
	authorized_by: str,
	reason_code: str,
	reason: str,
) -> dict[str, str | int] | None:
	authorization_name = str(
		frappe.db.get_value(
			REPORT_RECOVERY_DOCTYPE,
			{
				"schedule": schedule.name,
				"schedule_approval_checksum": schedule.approval_checksum,
				"prior_task": prior_task,
			},
			"name",
		)
		or ""
	)
	if not authorization_name:
		return None
	receipt = frappe.get_doc(REPORT_RECOVERY_DOCTYPE, authorization_name)
	_assert_recovery_authorization_integrity(
		receipt,
		schedule,
		require_current_runtime=False,
	)
	if (
		str(receipt.get("authorized_by") or "") != authorized_by
		or str(receipt.get("reason_code") or "") != reason_code
		or str(receipt.get("reason") or "") != reason
	):
		frappe.throw("A different recovery authorization already governs the supplied prior task.")
	recovery_task_name = str(
		frappe.db.get_value(
			"IONE AI Analysis Task",
			{"report_recovery_authorization": receipt.name},
			"name",
		)
		or ""
	)
	if not recovery_task_name:
		frappe.throw("Quality report recovery authorization has no replacement task.")
	task = frappe.get_doc("IONE AI Analysis Task", recovery_task_name)
	validated_quality_report_snapshot_for_task(task)
	if str(task.get("status") or "") == "Pending":
		_enqueue_scheduled_task_after_commit(task)
	return {
		"schedule": str(schedule.name),
		"authorization": str(receipt.name),
		"task": str(task.name),
		"recovery_sequence": int(receipt.recovery_sequence),
		"status": "Existing",
	}


def _period_recovery_receipts(
	schedule,
	period_start: date,
	period_end: date,
) -> list[Any]:
	rows = list(
		frappe.get_all(
			REPORT_RECOVERY_DOCTYPE,
			filters={
				"schedule": schedule.name,
				"schedule_approval_checksum": schedule.approval_checksum,
				"period_start": period_start,
				"period_end": period_end,
			},
			fields=["name", "recovery_sequence"],
			order_by="recovery_sequence asc, name asc",
			limit_page_length=REPORT_MAX_RECOVERY_AUTHORIZATIONS + 1,
		)
	)
	if len(rows) > REPORT_MAX_RECOVERY_AUTHORIZATIONS:
		frappe.throw("Quality report recovery authorization count exceeds its governed limit.")
	sequences = [int(row.get("recovery_sequence") or 0) for row in rows]
	if sequences != list(range(1, len(rows) + 1)):
		frappe.throw("Quality report recovery authorization sequence history is incomplete.")
	return rows


def _ensure_recovery_exhaustion_incident(
	schedule,
	prior_task,
	period_start: date,
	period_end: date,
	*,
	detected_by: str | None,
) -> str:
	incident_key = hashlib.sha256(
		(
			f"quality-report-recovery-exhausted|{schedule.name}|{schedule.approval_checksum}|"
			f"{period_start.isoformat()}|{period_end.isoformat()}"
		).encode()
	).hexdigest()
	existing = frappe.db.get_value(
		"IONE Agent Incident",
		{"incident_key": incident_key},
		["name", "incident_type", "severity", "analysis_task", "summary"],
		as_dict=True,
	)
	if existing:
		if (
			str(existing.get("incident_type") or "") != "Availability"
			or str(existing.get("severity") or "") != "High"
			or str(existing.get("analysis_task") or "") != str(prior_task.name)
			or str(existing.get("summary") or "") != "Monthly report recovery authorization limit exhausted"
		):
			frappe.throw("Quality report recovery incident key is already bound to another record.")
		return str(existing.get("name") or "")
	incident = frappe.get_doc(
		{
			"doctype": "IONE Agent Incident",
			"incident_key": incident_key,
			"incident_type": "Availability",
			"severity": "High",
			"status": "Open",
			"detected_at": now_datetime().replace(microsecond=0),
			"detected_by": detected_by,
			"policy": schedule.policy,
			"analysis_task": prior_task.name,
			"summary": "Monthly report recovery authorization limit exhausted",
			"details": (
				"The governed monthly report remains failed after the maximum two "
				"human-authorized replacement tasks. Investigate before controlled backfill."
			),
			"patient_data_involved": 0,
			"external_notification_required": 0,
		}
	)
	incident.insert(ignore_permissions=True)
	return str(incident.name)


def _next_due_period(
	schedule,
	latest_period_start: date,
	latest_period_end: date,
) -> tuple[date, date] | None:
	last_result = str(schedule.get("last_result") or "")
	last_start_value = schedule.get("last_period_start")
	last_end_value = schedule.get("last_period_end")
	coverage_start = _coverage_start_period(schedule, latest_period_start)
	if not last_result and not last_start_value and not last_end_value:
		# The first and latest closed months are both pending, so the inclusive
		# backlog size is the month distance plus one.
		if _month_distance(coverage_start, latest_period_start) >= REPORT_MAX_CATCHUP_MONTHS:
			frappe.throw(
				"Quality report schedule backlog exceeds the governed 12-month catch-up limit; "
				"suspend it and complete an explicitly approved manual backfill procedure."
			)
		return coverage_start, _month_end(coverage_start)
	if last_result not in {"Success", "Failed"} or not last_start_value or not last_end_value:
		frappe.throw("Quality report schedule runtime period provenance is incomplete.")
	last_start = getdate(last_start_value)
	last_end = getdate(last_end_value)
	if not _is_closed_calendar_month(last_start, last_end) or last_start > latest_period_start:
		frappe.throw("Quality report schedule runtime period provenance is invalid.")
	month_distance = _month_distance(last_start, latest_period_start)
	if month_distance > REPORT_MAX_CATCHUP_MONTHS:
		frappe.throw(
			"Quality report schedule backlog exceeds the governed 12-month catch-up limit; "
			"suspend it and complete an explicitly approved manual backfill procedure."
		)
	if last_result == "Failed" and not schedule.get("last_task"):
		return last_start, last_end
	last_task_name = str(schedule.get("last_task") or "")
	if not last_task_name:
		frappe.throw("Quality report schedule task progression provenance is incomplete.")
	last_task = frappe.get_doc("IONE AI Analysis Task", last_task_name)
	if not hmac.compare_digest(
		str(schedule.get("last_dispatch_key") or ""),
		str(last_task.get("dispatch_key") or ""),
	):
		frappe.throw("Quality report schedule task progression provenance is incomplete.")
	task_state = _existing_dispatch_task_state(schedule, last_task, last_start, last_end)
	if task_state == "failed":
		return None
	if last_result == "Failed":
		frappe.throw("Quality report schedule result conflicts with its current task state.")
	if task_state == "waiting":
		return None
	next_start = last_end + timedelta(days=1)
	if next_start > latest_period_start:
		return None
	if next_start.month == 12:
		after_next = date(next_start.year + 1, 1, 1)
	else:
		after_next = date(next_start.year, next_start.month + 1, 1)
	return next_start, after_next - timedelta(days=1)


def _existing_dispatch_task_state(
	schedule,
	task,
	period_start: date,
	period_end: date,
) -> str:
	"""Revalidate an existing task before treating a period as dispatched."""
	if str(task.get("name") or "") != str(schedule.get("last_task") or task.get("name") or ""):
		frappe.throw("Existing scheduled Quality Report task does not match runtime provenance.")
	if (
		not task.get("report_period_start")
		or not task.get("report_period_end")
		or getdate(task.get("report_period_start")) != period_start
		or getdate(task.get("report_period_end")) != period_end
	):
		frappe.throw("Existing scheduled Quality Report task period provenance is invalid.")
	if schedule.get("last_task") and not hmac.compare_digest(
		str(schedule.get("last_dispatch_key") or ""),
		str(task.get("dispatch_key") or ""),
	):
		frappe.throw("Existing scheduled Quality Report runtime dispatch provenance is invalid.")
	_validate_task_dispatch_provenance(schedule, task, period_start, period_end)
	validated_quality_report_snapshot_for_task(task)
	status = str(task.get("status") or "")
	if status in {"Failed", "Rejected", "Cancelled"}:
		return "failed"
	if status in {"Pending", "Queued", "Running", "Pending Confirmation", "Retry"}:
		return "waiting"
	if status not in {"Completed", "Reviewed"}:
		frappe.throw("Existing scheduled Quality Report task has an unsupported state.")
	if not frappe.db.exists("IONE AI Report Draft", {"task": task.name}):
		frappe.throw("Completed scheduled Quality Report task has no governed report draft.")
	return "completed"


def _runtime_failure_period(
	schedule,
	default_start: date,
	default_end: date,
) -> tuple[date, date]:
	try:
		start_value = schedule.get("last_period_start")
		end_value = schedule.get("last_period_end")
		if start_value and end_value:
			start = getdate(start_value)
			end = getdate(end_value)
			if _is_closed_calendar_month(start, end):
				return start, end
		coverage_start = _coverage_start_period(schedule, default_start)
		return coverage_start, _month_end(coverage_start)
	except TypeError, ValueError:
		pass
	return default_start, default_end


def _is_closed_calendar_month(start: date | None, end: date | None) -> bool:
	if not start or not end or start.day != 1 or start > end:
		return False
	if start.month == 12:
		next_month = date(start.year + 1, 1, 1)
	else:
		next_month = date(start.year, start.month + 1, 1)
	return end == next_month - timedelta(days=1)


def _period_filters(
	date_field: str,
	period_start: date,
	period_end: date,
	scope: Mapping[str, str | None],
) -> list[list[Any]]:
	# Use a half-open interval so DateTime values on the final calendar day are
	# included without relying on database timestamp precision.
	return [
		[date_field, ">=", period_start],
		[date_field, "<", period_end + timedelta(days=1)],
		*[[key, "=", value] for key, value in scope.items() if value],
	]


def _permissioned_rows(
	doctype: str,
	*,
	filters: Any,
	fields: tuple[str, ...],
	order_by: str,
	limit: int,
	group_by: str | None = None,
) -> list[Any]:
	if limit < 1 or limit > REPORT_SNAPSHOT_MAX_FINDINGS:
		raise ValueError("Internal quality summary limit is outside its reviewed bound.")
	if not frappe.has_permission(doctype, "read"):
		frappe.throw(f"Requester has no read permission for {doctype}.", frappe.PermissionError)
	kwargs: dict[str, Any] = {
		"filters": filters,
		"fields": list(fields),
		"order_by": order_by,
		"limit_start": 0,
		"limit_page_length": limit + 1,
	}
	if group_by:
		kwargs["group_by"] = group_by
	rows = list(frappe.get_list(doctype, **kwargs))
	if len(rows) > limit:
		frappe.throw(
			f"{doctype} exceeds the governed quality summary limit of {limit}; narrow the schedule scope.",
			frappe.ValidationError,
		)
	return rows


def _assert_permissioned_count(doctype: str, filters: Any) -> None:
	rows = _permissioned_rows(
		doctype,
		filters=filters,
		fields=("count(name) as total",),
		order_by="",
		limit=1,
	)
	total = int(rows[0].get("total") or 0) if rows else 0
	if total < 0 or total > REPORT_SNAPSHOT_MAX_SOURCE_RECORDS:
		frappe.throw(
			f"{doctype} exceeds the governed quality summary source limit.",
			frappe.ValidationError,
		)


def _clean_group(row: Any, fields: tuple[str, ...]) -> dict[str, Any]:
	return {fieldname: row.get(fieldname) for fieldname in fields}


def _snapshot_group_count(data: Mapping[str, Any]) -> int:
	return sum(
		len(data.get(fieldname) or [])
		for fieldname in (
			"indicator_summary",
			"finding_summary",
			"rectification_summary",
			"pdca_current_summary",
		)
	)


def _validate_existing_snapshot(doc, schedule, period_start: date, period_end: date) -> None:
	if (
		str(doc.get("schedule") or "") != str(schedule.name)
		or getdate(doc.get("period_start")) != period_start
		or getdate(doc.get("period_end")) != period_end
		or str(doc.get("generated_for") or "") != str(schedule.requested_by)
		or str(doc.get("report_reviewer") or "") != str(schedule.report_reviewer)
		or any(
			str(doc.get(fieldname) or "") != str(schedule.get(fieldname) or "")
			for fieldname in ("hospital", "campus", "department", "ward")
		)
	):
		frappe.throw("Existing quality report snapshot provenance does not match the schedule.")
	if not hmac.compare_digest(
		str(doc.get("snapshot_key") or ""),
		_snapshot_key(schedule, period_start, period_end),
	):
		frappe.throw("Existing quality report snapshot key does not match the approved schedule.")
	data = _validated_snapshot_payload(doc)
	coverage = data.get("indicator_coverage")
	if not isinstance(coverage, dict) or coverage.get("required_codes") != _required_indicator_codes(
		schedule
	):
		frappe.throw("Quality report snapshot indicator coverage does not match the approved schedule.")


def _validated_snapshot_payload(doc) -> dict[str, Any]:
	payload = str(doc.get("data_json") or "")
	if not payload or len(payload.encode()) > REPORT_SNAPSHOT_MAX_BYTES:
		frappe.throw("Quality report snapshot is empty or exceeds 256 KiB.")
	try:
		decoded = json.loads(payload)
	except ValueError as exc:
		raise frappe.ValidationError("Quality report snapshot data_json is invalid.") from exc
	if not isinstance(decoded, dict) or _canonical_json(decoded) != payload:
		frappe.throw("Quality report snapshot data_json must be one canonical JSON object.")
	actual_hash = hashlib.sha256(payload.encode()).hexdigest()
	if not hmac.compare_digest(str(doc.get("data_hash") or ""), actual_hash):
		frappe.throw("Quality report snapshot data hash does not match its canonical content.")

	expected_keys = {
		"contract_version",
		"period",
		"scope",
		"generated_at",
		"indicator_coverage",
		"indicator_summary",
		"finding_summary",
		"rectification_summary",
		"pdca_current_summary",
		"semantic_notes",
	}
	if set(decoded) != expected_keys or str(decoded.get("contract_version") or "") != "1":
		frappe.throw("Quality report snapshot contract is unsupported or incomplete.")

	if not doc.get("period_start") or not doc.get("period_end"):
		frappe.throw("Quality report snapshot period provenance is incomplete.")
	period_start = getdate(doc.get("period_start"))
	period_end = getdate(doc.get("period_end"))
	period = decoded.get("period")
	if (
		not _is_closed_calendar_month(period_start, period_end)
		or not isinstance(period, dict)
		or set(period) != {"start", "end", "semantics"}
		or str(period.get("start") or "") != period_start.isoformat()
		or str(period.get("end") or "") != period_end.isoformat()
		or str(period.get("semantics") or "") != "closed calendar month"
	):
		frappe.throw("Quality report snapshot period contract does not match its provenance.")

	scope = decoded.get("scope")
	expected_scope = {
		fieldname: (str(doc.get(fieldname)) if doc.get(fieldname) else None)
		for fieldname in ("hospital", "campus", "department", "ward")
	}
	if not isinstance(scope, dict) or set(scope) != set(expected_scope) or scope != expected_scope:
		frappe.throw("Quality report snapshot scope does not match its provenance.")

	if not doc.get("generated_at") or not doc.get("source_cutoff"):
		frappe.throw("Quality report snapshot timestamps are incomplete.")
	try:
		generated_at = get_datetime(doc.get("generated_at"))
		source_cutoff = get_datetime(doc.get("source_cutoff"))
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError("Quality report snapshot timestamps are invalid.") from exc
	if (
		not generated_at
		or not source_cutoff
		or source_cutoff != generated_at
		or str(decoded.get("generated_at") or "") != generated_at.isoformat()
	):
		frappe.throw("Quality report snapshot timestamp contract does not match its provenance.")

	coverage = decoded.get("indicator_coverage")
	if not isinstance(coverage, dict) or set(coverage) != {
		"required_codes",
		"observed_codes",
		"missing_codes",
		"complete",
	}:
		frappe.throw("Quality report snapshot indicator coverage is malformed.")
	required_codes = coverage.get("required_codes")
	observed_codes = coverage.get("observed_codes")
	missing_codes = coverage.get("missing_codes")
	for code_list in (required_codes, observed_codes, missing_codes):
		if (
			not isinstance(code_list, list)
			or len(code_list) > REPORT_SNAPSHOT_MAX_INDICATOR_ROWS
			or any(not isinstance(code, str) or not code for code in code_list)
			or code_list != sorted(set(code_list))
		):
			frappe.throw("Quality report snapshot indicator coverage codes are malformed.")
	expected_missing = sorted(set(required_codes or []) - set(observed_codes or []))
	if (
		not required_codes
		or observed_codes != required_codes
		or missing_codes != expected_missing
		or missing_codes
		or coverage.get("complete") is not True
	):
		frappe.throw("Quality report snapshot indicator coverage is incomplete.")

	group_fields = {
		"indicator_summary": REPORT_SNAPSHOT_MAX_INDICATOR_ROWS,
		"finding_summary": REPORT_SNAPSHOT_MAX_GROUPS,
		"rectification_summary": REPORT_SNAPSHOT_MAX_GROUPS,
		"pdca_current_summary": REPORT_SNAPSHOT_MAX_GROUPS,
	}
	for fieldname, limit in group_fields.items():
		value = decoded.get(fieldname)
		if (
			not isinstance(value, list)
			or len(value) > limit
			or any(not isinstance(row, dict) for row in value)
		):
			frappe.throw(f"Quality report snapshot {fieldname} is malformed or exceeds its limit.")
	summary_codes: set[str] = set()
	for row in decoded["indicator_summary"]:
		code = row.get("indicator_code")
		if (
			not isinstance(code, str)
			or not code
			or len(code) > 140
			or any(ord(character) < 32 or ord(character) == 127 for character in code)
		):
			frappe.throw("Quality report snapshot indicator summary contains an invalid code.")
		summary_codes.add(code)
	if sorted(summary_codes) != observed_codes:
		frappe.throw("Quality report snapshot observed indicator coverage does not match its summary.")
	if not isinstance(decoded.get("semantic_notes"), dict):
		frappe.throw("Quality report snapshot semantic notes are malformed.")
	if _contains_patient_identifier_key(decoded):
		frappe.throw("Quality report snapshots cannot contain patient-level identifier fields.")
	try:
		stored_group_count = int(doc.get("source_group_count") or 0)
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError("Quality report snapshot group count is invalid.") from exc
	if stored_group_count != _snapshot_group_count(decoded):
		frappe.throw("Quality report snapshot group count does not match its canonical content.")
	return decoded


def _contains_patient_identifier_key(value: Any) -> bool:
	forbidden = {
		"patient",
		"patient_id",
		"patient_name",
		"encounter",
		"encounter_id",
		"medical_record",
		"medical_record_id",
		"inpatient_no",
		"outpatient_no",
		"id_card",
		"phone",
		"address",
	}
	if isinstance(value, Mapping):
		return any(
			str(key).strip().lower() in forbidden or _contains_patient_identifier_key(item)
			for key, item in value.items()
		)
	if isinstance(value, list):
		return any(_contains_patient_identifier_key(item) for item in value)
	return False


def _update_schedule_runtime(
	schedule,
	*,
	period_start: date,
	period_end: date,
	dispatch_key: str | None,
	task: str | None,
	result: str,
	error_code: str | None,
) -> None:
	if result not in {"Success", "Failed"}:
		raise ValueError("Unsupported report schedule runtime result.")
	values = {
		"last_period_start": period_start,
		"last_period_end": period_end,
		"last_dispatch_key": dispatch_key,
		"last_task": task,
		"last_run_at": now_datetime(),
		"last_result": result,
		"last_error_code": error_code,
	}
	with _schedule_runtime_update():
		schedule.update(values)
		schedule.save(ignore_permissions=True)


def _dispatch_error_code(exc: Exception) -> str:
	if isinstance(exc, frappe.PermissionError):
		return "PERMISSION_DENIED"
	if isinstance(exc, (frappe.ValidationError, ValueError, TypeError)):
		return "VALIDATION_FAILED"
	return "UNEXPECTED_FAILURE"


def _eligible_user(user: str, allowed_roles: frozenset[str]) -> str:
	if user in {"", "Guest", "Administrator"}:
		frappe.throw("Quality report governance requires a named user.", frappe.PermissionError)
	if not int(frappe.db.get_value("User", user, "enabled") or 0):
		frappe.throw("Quality report governance user is disabled.", frappe.PermissionError)
	roles = frozenset(frappe.get_roles(user))
	if "IONE Agent Service" in roles or not roles.intersection(allowed_roles):
		frappe.throw("Quality report governance user has no eligible role.", frappe.PermissionError)
	return user


def _named_user() -> str:
	user = str(frappe.session.user or "")
	if user in {"", "Guest", "Administrator"}:
		frappe.throw("A named accountable user is required.", frappe.PermissionError)
	return user


def _named_role_user(allowed_roles: frozenset[str]) -> str:
	return _eligible_user(_named_user(), allowed_roles)


def _bounded_comment(value: str) -> str:
	comment = str(value or "").strip()
	if len(comment) < 5 or len(comment) > 2_000:
		frappe.throw("Governance comment must contain 5-2,000 characters.")
	return comment


def _normalized_recovery_reason_code(value: str) -> str:
	code = str(value or "").strip().upper()
	if not _RECOVERY_REASON_CODE_RE.fullmatch(code):
		frappe.throw(
			"Recovery reason_code must be 2-64 uppercase letters, digits, dot, colon, dash, or underscore."
		)
	return code


def _bounded_recovery_reason(value: str) -> str:
	reason = _bounded_comment(value)
	if any(ord(character) < 32 or ord(character) == 127 for character in reason):
		frappe.throw("Recovery reason cannot contain hidden control characters.")
	return reason


def _changed_fields(doc, previous, fields: frozenset[str]) -> set[str]:
	return {fieldname for fieldname in fields if doc.get(fieldname) != previous.get(fieldname)}


def _canonical_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)


def _is_sha256(value: str) -> bool:
	return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


@contextmanager
def _schedule_transition() -> Iterator[None]:
	previous = getattr(frappe.flags, "ione_report_schedule_transition", None)
	frappe.flags.ione_report_schedule_transition = _SCHEDULE_TRANSITION_CAPABILITY
	try:
		yield
	finally:
		frappe.flags.ione_report_schedule_transition = previous


@contextmanager
def _schedule_runtime_update() -> Iterator[None]:
	previous = getattr(frappe.flags, "ione_report_schedule_runtime", None)
	frappe.flags.ione_report_schedule_runtime = _SCHEDULE_RUNTIME_CAPABILITY
	try:
		yield
	finally:
		frappe.flags.ione_report_schedule_runtime = previous


@contextmanager
def _snapshot_creation() -> Iterator[None]:
	previous = getattr(frappe.flags, "ione_report_snapshot_creation", None)
	frappe.flags.ione_report_snapshot_creation = _SNAPSHOT_CREATION_CAPABILITY
	try:
		yield
	finally:
		frappe.flags.ione_report_snapshot_creation = previous


@contextmanager
def _recovery_creation() -> Iterator[None]:
	previous = getattr(frappe.flags, "ione_report_recovery_creation", None)
	frappe.flags.ione_report_recovery_creation = _RECOVERY_CREATION_CAPABILITY
	try:
		yield
	finally:
		frappe.flags.ione_report_recovery_creation = previous


@contextmanager
def _accountable_user(user: str) -> Iterator[None]:
	original_user = str(frappe.session.user or "")
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(original_user)
