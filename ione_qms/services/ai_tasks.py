from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any

import frappe
from frappe.utils import getdate

from ione_qms.ai.privacy import AIPrivacyViolation, deidentify_ai_value
from ione_qms.ai.privacy_runtime import task_known_identifiers

_CREATION_FLAG = "ione_ai_task_creation_token"
_IMMUTABLE_FIELDS = frozenset(
	{
		"task_type",
		"policy",
		"requested_by",
		"requested_at",
		"hospital",
		"campus",
		"department",
		"ward",
		"patient",
		"encounter",
		"responsible_staff",
		"indicator_result",
		"finding",
		"origin",
		"report_schedule",
		"report_snapshot",
		"report_period_start",
		"report_period_end",
		"report_reviewer",
		"dispatch_key",
		"schedule_approval_checksum",
		"prior_report_task",
		"report_recovery_authorization",
		"recovery_sequence",
		"recovery_authorization_checksum",
		"record_count",
		"input_summary",
		"input_hash",
		"data_classification",
		"requires_human_review",
	}
)
_INITIAL_RUNTIME_FIELDS = frozenset(
	{
		"execution_attempt",
		"execution_phase",
		"claimed_at",
		"heartbeat_at",
		"lease_expires_at",
		"execution_token",
		"last_execution_token_hash",
		"started_at",
		"completed_at",
		"flow_session",
		"flow_run",
		"output_hash",
		"error_message",
	}
)


@contextmanager
def controlled_analysis_task_creation(doc) -> Iterator[None]:
	"""Grant one in-process capability to the checked task-creation API."""
	token = secrets.token_urlsafe(32)
	previous = getattr(frappe.flags, _CREATION_FLAG, None)
	doc.flags.ione_ai_task_creation_token = token
	setattr(frappe.flags, _CREATION_FLAG, token)
	try:
		yield
	finally:
		setattr(frappe.flags, _CREATION_FLAG, previous)
		doc.flags.ione_ai_task_creation_token = None


def validate_analysis_task(doc, method: str | None = None) -> None:
	"""Deny generic CRUD and freeze the request/provenance boundary."""
	del method
	previous = doc.get_doc_before_save()
	if previous is None:
		_validate_controlled_insert(doc)
		return

	changed = sorted(
		fieldname for fieldname in _IMMUTABLE_FIELDS if doc.get(fieldname) != previous.get(fieldname)
	)
	if changed:
		frappe.throw(
			"AI Analysis Task request and provenance fields are immutable; "
			"create a new governed task. Changed: " + ", ".join(changed),
			frappe.PermissionError,
		)

	# Task lifecycle changes are made only by the fenced orchestrator through
	# row locks and compare-and-swap SQL. A document save would bypass that
	# state machine even when performed by Frappe's technical Administrator.
	runtime_changed = sorted(
		fieldname
		for fieldname in {"status", *_INITIAL_RUNTIME_FIELDS}
		if doc.get(fieldname) != previous.get(fieldname)
	)
	if runtime_changed:
		frappe.throw(
			"AI Analysis Task lifecycle fields may only change through the governed "
			"execution state machine. Changed: " + ", ".join(runtime_changed),
			frappe.PermissionError,
		)

	assert_analysis_task_input_integrity(doc)


def assert_analysis_task_input_integrity(task: Any) -> None:
	"""Verify the immutable model input before any execution claim is published."""
	summary = str(task.get("input_summary") or "")
	digest = str(task.get("input_hash") or "").lower()
	actual = hashlib.sha256(summary.encode()).hexdigest()
	if (
		not summary.strip()
		or len(summary) > 20_000
		or len(digest) != 64
		or not hmac.compare_digest(actual, digest)
	):
		frappe.throw(
			"AI Analysis Task input failed its immutable SHA-256 integrity check.",
			frappe.ValidationError,
		)
	try:
		safe_summary = deidentify_ai_value(
			summary,
			known_identifiers=task_known_identifiers(task),
		)
	except AIPrivacyViolation as exc:
		frappe.throw(
			f"AI Analysis Task input failed the patient-identity safety gate. code={exc.code}",
			frappe.ValidationError,
		)
	if str(safe_summary) != summary:
		frappe.throw(
			"AI Analysis Task input contains a direct identity value and must be "
			"de-identified before persistence.",
			frappe.ValidationError,
		)
	requested_by = str(task.get("requested_by") or "")
	if requested_by in {"", "Guest", "Administrator"} or not task.get("requested_at"):
		frappe.throw(
			"AI Analysis Task requires a named accountable requester and request time.",
			frappe.PermissionError,
		)
	_assert_task_provenance(task)


def _assert_task_provenance(task: Any) -> None:
	origin = str(task.get("origin") or "Manual")
	core_scheduled_fields = (
		"report_schedule",
		"report_snapshot",
		"report_period_start",
		"report_period_end",
		"report_reviewer",
		"dispatch_key",
		"schedule_approval_checksum",
	)
	recovery_fields = (
		"prior_report_task",
		"report_recovery_authorization",
		"recovery_sequence",
		"recovery_authorization_checksum",
	)
	scheduled_fields = (*core_scheduled_fields, *recovery_fields)
	if origin == "Manual":
		if any(task.get(fieldname) not in (None, "", 0, "0") for fieldname in scheduled_fields):
			frappe.throw("Manual AI tasks cannot contain scheduled-report provenance.")
		return
	if origin != "Scheduled":
		frappe.throw("AI Analysis Task origin is invalid.")
	if str(task.get("task_type") or "") != "Quality Report":
		frappe.throw("Only Quality Report tasks may use scheduled provenance.")
	if any(not task.get(fieldname) for fieldname in core_scheduled_fields):
		frappe.throw("Scheduled Quality Report task provenance is incomplete.")
	for fieldname in ("dispatch_key", "schedule_approval_checksum"):
		value = str(task.get(fieldname) or "")
		if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
			frappe.throw(f"Scheduled Quality Report {fieldname} is invalid.")
	recovery_values = [task.get(fieldname) not in (None, "", 0, "0") for fieldname in recovery_fields]
	if any(recovery_values) and not all(recovery_values):
		frappe.throw("Scheduled Quality Report recovery provenance is incomplete.")
	if all(recovery_values):
		try:
			sequence = int(task.get("recovery_sequence") or 0)
		except (TypeError, ValueError) as exc:
			raise frappe.ValidationError("Scheduled Quality Report recovery sequence is invalid.") from exc
		if sequence not in {1, 2}:
			frappe.throw("Scheduled Quality Report recovery sequence is outside its governed limit.")
		checksum = str(task.get("recovery_authorization_checksum") or "")
		if len(checksum) != 64 or any(character not in "0123456789abcdef" for character in checksum):
			frappe.throw("Scheduled Quality Report recovery authorization checksum is invalid.")
		if hmac.compare_digest(str(task.get("dispatch_key") or ""), checksum):
			frappe.throw("Scheduled Quality Report recovery dispatch and receipt checksums must differ.")
	try:
		start = getdate(task.get("report_period_start"))
		end = getdate(task.get("report_period_end"))
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError("Scheduled Quality Report period is invalid.") from exc
	if not start or not end:
		frappe.throw("Scheduled Quality Report period is invalid.")
	if start.month == 12:
		next_month = date(start.year + 1, 1, 1)
	else:
		next_month = date(start.year, start.month + 1, 1)
	if start.day != 1 or end != next_month - timedelta(days=1):
		frappe.throw("Scheduled Quality Report period must be one closed calendar month.")
	if (
		int(task.get("record_count") or 0) != 1
		or not int(task.get("requires_human_review") or 0)
		or str(task.get("data_classification") or "") != "Hospital Internal"
	):
		frappe.throw(
			"Scheduled Quality Report tasks require one aggregate internal snapshot "
			"and mandatory human review."
		)
	reviewer = str(task.get("report_reviewer") or "")
	if reviewer in {"", "Guest", "Administrator"} or reviewer == str(task.get("requested_by") or ""):
		frappe.throw("Scheduled Quality Report reviewer provenance is invalid.")
	if any(
		task.get(fieldname)
		for fieldname in ("patient", "encounter", "responsible_staff", "indicator_result", "finding")
	):
		frappe.throw("Scheduled Quality Report tasks cannot contain patient or record-level scope.")


def _validate_controlled_insert(doc) -> None:
	server_token = str(getattr(frappe.flags, _CREATION_FLAG, None) or "")
	document_token = str(getattr(doc.flags, "ione_ai_task_creation_token", None) or "")
	if not server_token or not document_token or not hmac.compare_digest(server_token, document_token):
		frappe.throw(
			"AI Analysis Tasks may only be created through the governed creation API.",
			frappe.PermissionError,
		)
	if str(frappe.session.user or "") != str(doc.get("requested_by") or ""):
		frappe.throw(
			"AI Analysis Task requester must match the accountable API session.",
			frappe.PermissionError,
		)
	if str(doc.get("status") or "") != "Pending":
		frappe.throw("New AI Analysis Tasks must start in Pending status.")
	if any(doc.get(fieldname) not in (None, "", 0, "0") for fieldname in _INITIAL_RUNTIME_FIELDS):
		frappe.throw("New AI Analysis Tasks cannot contain execution state.")
	assert_analysis_task_input_integrity(doc)
