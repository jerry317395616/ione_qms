from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import frappe
from frappe.utils import get_datetime, getdate, now_datetime, nowdate

from ione_qms.constants import (
	QUALITY_ACTION_OVERSIGHT_ROLES,
	QUALITY_ACTION_OWNER_ROLES,
	QUALITY_MEETING_APPROVER_ROLES,
	QUALITY_MEETING_AUTHOR_ROLES,
)
from ione_qms.permissions import get_access_context, require_scope_read

MEETING_STATUSES = frozenset(
	{"Draft", "Submitted", "Approved", "Held", "Minutes Pending", "Closed", "Cancelled"}
)
MEETING_TRANSITIONS: dict[str, frozenset[str]] = {
	"Draft": frozenset({"Submitted", "Cancelled"}),
	"Submitted": frozenset({"Approved", "Cancelled"}),
	"Approved": frozenset({"Held", "Cancelled"}),
	"Held": frozenset({"Minutes Pending"}),
	"Minutes Pending": frozenset({"Closed"}),
	"Closed": frozenset(),
	"Cancelled": frozenset(),
}
MINUTE_STATUSES = frozenset({"Draft", "Submitted", "Approved", "Rejected"})
MINUTE_TRANSITIONS: dict[str, frozenset[str]] = {
	"Draft": frozenset({"Submitted"}),
	"Submitted": frozenset({"Approved", "Rejected"}),
	"Approved": frozenset(),
	"Rejected": frozenset(),
}
ACTION_STATUSES = frozenset({"Open", "In Progress", "Pending Verification", "Closed", "Cancelled"})
ACTION_TRANSITIONS: dict[str, frozenset[str]] = {
	"Open": frozenset({"In Progress", "Cancelled"}),
	"In Progress": frozenset({"Pending Verification", "Cancelled"}),
	"Pending Verification": frozenset({"In Progress", "Closed", "Cancelled"}),
	"Closed": frozenset(),
	"Cancelled": frozenset(),
}
ACTION_VERIFICATION_STATUS_AFTER = {
	"Close": "Closed",
	"Rework": "In Progress",
	"Cancel": "Cancelled",
}
EXPERIENCE_STATUSES = frozenset({"Draft", "Submitted", "Published", "Rejected", "Retired"})
EXPERIENCE_TRANSITIONS: dict[str, frozenset[str]] = {
	"Draft": frozenset({"Submitted"}),
	"Submitted": frozenset({"Published", "Rejected"}),
	"Published": frozenset({"Retired"}),
	"Rejected": frozenset(),
	"Retired": frozenset(),
}

MEETING_AUTHOR_ROLES = QUALITY_MEETING_AUTHOR_ROLES
GOVERNANCE_APPROVER_ROLES = QUALITY_MEETING_APPROVER_ROLES
AI_MATERIAL_REVIEW_ROLES = frozenset({"IONE Agent Reviewer", "IONE QC Reviewer", "IONE Medical Affairs"})
ACTION_OWNER_ROLES = QUALITY_ACTION_OWNER_ROLES
ACTION_OVERSIGHT_ROLES = QUALITY_ACTION_OVERSIGHT_ROLES
ACTION_READ_ROLES = ACTION_OWNER_ROLES | ACTION_OVERSIGHT_ROLES
EXPERIENCE_AUTHOR_ROLES = MEETING_AUTHOR_ROLES

MAX_AGENDA_ITEMS = 100
MAX_ATTENDEES = 200
MAX_DECISIONS = 100
MAX_JSON_BYTES = 128 * 1024
MAX_WORKBENCH_ROWS = 200
MAX_ACTION_VERIFICATION_ROUNDS = 100
_SCOPE_FIELDS = ("hospital", "campus", "department", "ward")
_TERMINAL_MEETING_STATES = frozenset({"Closed", "Cancelled"})
_TERMINAL_ACTION_STATES = frozenset({"Closed", "Cancelled"})
_MEETING_AUDIT_FIELDS = frozenset(
	{
		"submitted_by",
		"submitted_at",
		"approved_by",
		"approved_at",
		"approval_comment",
		"agenda_checksum",
		"actual_start",
		"actual_end",
		"held_by",
		"held_at",
		"held_evidence_reference",
		"minutes_pending_by",
		"minutes_pending_at",
		"closed_by",
		"closed_at",
		"cancelled_by",
		"cancelled_at",
		"cancellation_reason",
	}
)
_MINUTE_AUDIT_FIELDS = frozenset(
	{
		"approved_meeting_key",
		"submitted_by",
		"submitted_at",
		"reviewed_by",
		"reviewed_at",
		"review_comment",
		"checksum",
	}
)
_ACTION_AUDIT_FIELDS = frozenset(
	{
		"started_by",
		"started_at",
		"completion_evidence",
		"effect_result",
		"current_submission_key",
		"current_submission_checksum",
		"verification_round_no",
		"latest_verification_round",
		"latest_verification_receipt_checksum",
		"submitted_for_verification_by",
		"submitted_for_verification_at",
		"verification_comment",
		"verified_by",
		"verified_at",
		"closed_by",
		"closed_at",
		"cancelled_by",
		"cancelled_at",
		"cancellation_reason",
	}
)
_EXPERIENCE_AUDIT_FIELDS = frozenset(
	{
		"active_publication_key",
		"submitted_by",
		"submitted_at",
		"published_by",
		"published_at",
		"review_comment",
		"checksum",
		"retired_by",
		"retired_at",
		"retirement_reason",
	}
)
_EXPERIENCE_CONTENT_KEYS = frozenset(
	{
		"context_summary",
		"issue_pattern",
		"improvement_actions",
		"verified_outcome",
		"lessons",
		"applicability",
	}
)
_PROHIBITED_EXPERIENCE_KEYS = frozenset(
	{
		"patient",
		"patient_id",
		"patient_name",
		"encounter",
		"encounter_id",
		"medical_record",
		"medical_record_number",
		"mrn",
		"source_record_id",
		"identification_number",
		"phone",
		"address",
		"narrative",
		"raw_text",
		"source_text",
	}
)
_DIRECT_IDENTIFIER_PATTERNS = (
	re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
	re.compile(r"(?<![0-9A-Za-z])\d{17}[0-9Xx](?![0-9A-Za-z])"),
	re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
	re.compile(r"https?://\S+", re.IGNORECASE),
)


def validate_quality_meeting(doc, method: str | None = None) -> None:
	del method
	status = _status(doc, MEETING_STATUSES, "quality meeting")
	_validate_scope_shape(doc)
	_validate_meeting_content(doc)
	_validate_configured_user(
		doc.get("meeting_approver"),
		"meeting_approver",
		GOVERNANCE_APPROVER_ROLES,
		doc,
	)
	_validate_configured_user(
		doc.get("minute_approver"),
		"minute_approver",
		GOVERNANCE_APPROVER_ROLES,
		doc,
	)
	_validate_configured_user(doc.get("chair"), "chair", MEETING_AUTHOR_ROLES, doc)
	_validate_meeting_status_gates(doc, status)
	if str(doc.get("meeting_approver") or "") == str(doc.get("owner") or ""):
		frappe.throw("Meeting creator and configured meeting approver must be independent.")

	previous = doc.get_doc_before_save()
	if not previous:
		if status != "Draft":
			frappe.throw("New quality meetings must start in Draft status.")
		_require_named_actor(MEETING_AUTHOR_ROLES, scope_doc=doc)
		_reject_audit_values(doc, _MEETING_AUDIT_FIELDS)
		return
	_validate_transition(previous.get("status"), status, MEETING_TRANSITIONS, "quality meeting")
	transitioned = str(previous.get("status") or "") != status
	if transitioned and not _has_capability("meeting", doc.name):
		frappe.throw("Quality meeting transitions must use the governed POST API.")
	if not transitioned and str(previous.get("status") or "") != "Draft":
		if _changed_business_fields(doc, previous):
			frappe.throw("Submitted and approved meeting records cannot be edited directly.")
	if not transitioned and status == "Draft":
		_require_named_actor(MEETING_AUTHOR_ROLES, scope_doc=doc)
		if frappe.session.user != str(doc.get("owner") or ""):
			frappe.throw("Only the named meeting creator may edit its Draft content.")
		_reject_changed_fields(doc, previous, _MEETING_AUDIT_FIELDS)
	if str(previous.get("status") or "") in _TERMINAL_MEETING_STATES:
		frappe.throw("Terminal quality meeting records are immutable.")
	if str(previous.get("status") or "") in {"Approved", "Held", "Minutes Pending"}:
		_validate_frozen_agenda(doc, expected_checksum=str(previous.get("agenda_checksum") or ""))


def validate_meeting_minute(doc, method: str | None = None) -> None:
	del method
	status = _status(doc, MINUTE_STATUSES, "meeting minute")
	meeting = _meeting_for_minute(doc)
	_validate_minute_content(doc)
	_inherit_and_validate_scope(doc, meeting, "quality meeting")
	expected_approved_key = str(doc.get("meeting") or "") if status == "Approved" else ""
	if doc.get("approved_meeting_key") and not hmac.compare_digest(
		str(doc.get("approved_meeting_key")),
		expected_approved_key,
	):
		frappe.throw("Meeting minute approved_meeting_key does not match its workflow state.")
	doc.approved_meeting_key = expected_approved_key or None
	_validate_minute_status_gates(doc, status)
	previous = doc.get_doc_before_save()
	if not previous:
		if status != "Draft":
			frappe.throw("New meeting minutes must start in Draft status.")
		_require_named_actor(MEETING_AUTHOR_ROLES, scope_doc=meeting)
		if str(meeting.get("status") or "") not in {"Held", "Minutes Pending"}:
			frappe.throw("Meeting minutes may only be drafted after the meeting is recorded as Held.")
		_reject_audit_values(doc, _MINUTE_AUDIT_FIELDS)
		return
	_validate_transition(previous.get("status"), status, MINUTE_TRANSITIONS, "meeting minute")
	transitioned = str(previous.get("status") or "") != status
	if transitioned and not _has_capability("minute", doc.name):
		frappe.throw("Meeting minute transitions must use the governed POST API.")
	if not transitioned:
		if status != "Draft":
			if _changed_business_fields(doc, previous):
				frappe.throw("Submitted and decided meeting minutes are immutable.")
		else:
			_require_named_actor(MEETING_AUTHOR_ROLES, scope_doc=meeting)
			if frappe.session.user != str(doc.get("owner") or ""):
				frappe.throw("Only the named minute creator may edit its Draft content.")
			_reject_changed_fields(doc, previous, _MINUTE_AUDIT_FIELDS)
	if str(previous.get("status") or "") in {"Approved", "Rejected"}:
		frappe.throw("Decided meeting minutes are immutable.")
	if previous.get("meeting") != doc.get("meeting"):
		frappe.throw("A meeting minute cannot be relinked to another meeting.")


def validate_meeting_decision(doc, method: str | None = None) -> None:
	del method
	if not doc.is_new():
		frappe.throw("Meeting decisions are immutable append-only records.")
	if not _has_capability("decision", str(doc.get("meeting_minute") or "")):
		frappe.throw("Meeting decisions may only be derived from an approved meeting minute.")
	minute = frappe.get_doc("IONE Meeting Minute", doc.get("meeting_minute"))
	if str(minute.get("status") or "") != "Approved":
		frappe.throw("Meeting decisions require an Approved meeting minute.")
	meeting = frappe.get_doc("IONE Quality Meeting", minute.get("meeting"))
	if str(doc.get("meeting") or "") != str(meeting.name):
		frappe.throw("Meeting decision linkage does not match its approved minute.")
	_inherit_and_validate_scope(doc, meeting, "quality meeting")
	if not 1 <= len(str(doc.get("decision_code") or "").strip()) <= 100:
		frappe.throw("Meeting decision code must contain 1-100 characters.")
	_bounded_text(doc.get("decision_text"), "decision_text", 20_000, minimum=5)
	_bounded_text(doc.get("rationale"), "rationale", 20_000, minimum=5)
	expected = _checksum(_decision_snapshot(doc))
	if not hmac.compare_digest(str(doc.get("checksum") or ""), expected):
		frappe.throw("Meeting decision checksum does not match its derived content.")


def validate_quality_action_item(doc, method: str | None = None) -> None:
	del method
	status = _status(doc, ACTION_STATUSES, "quality action item")
	_validate_action_content(doc)
	_validate_action_links(doc)
	_validate_configured_user(doc.get("assigned_to"), "assigned_to", ACTION_OWNER_ROLES, doc)
	_validate_configured_user(doc.get("verifier"), "verifier", GOVERNANCE_APPROVER_ROLES, doc)
	if str(doc.get("assigned_to") or "") == str(doc.get("verifier") or ""):
		frappe.throw("Action owner and independent verifier must be different named users.")
	_validate_action_status_gates(doc, status)
	previous = doc.get_doc_before_save()
	if not previous:
		if not _has_capability("action", "new"):
			frappe.throw("Quality action items must be created through the governed POST API.")
		if status != "Open":
			frappe.throw("New quality action items must start in Open status.")
		_reject_audit_values(doc, _ACTION_AUDIT_FIELDS)
		_validate_quality_action_chain_state(doc, status)
		return
	if not _has_capability("action", doc.name):
		frappe.throw("Quality action item changes must use the governed POST API.")
	_validate_transition(previous.get("status"), status, ACTION_TRANSITIONS, "quality action item")
	if str(previous.get("status") or "") in _TERMINAL_ACTION_STATES:
		frappe.throw("Terminal quality action items are immutable.")
	for fieldname in (
		"meeting",
		"meeting_decision",
		"finding",
		"pdca_project",
		"safety_event",
		"assigned_to",
		"verifier",
		"due_date",
		*_SCOPE_FIELDS,
	):
		if _normalized(previous.get(fieldname)) != _normalized(doc.get(fieldname)):
			frappe.throw(f"Quality action item provenance field {fieldname} is immutable.")
	if status in {"Pending Verification", "Closed"}:
		_bounded_text(
			doc.get("completion_evidence"),
			"completion_evidence",
			20_000,
			minimum=10,
		)
		_bounded_text(doc.get("effect_result"), "effect_result", 20_000, minimum=10)
	_validate_quality_action_chain_state(doc, status)


def validate_quality_action_verification_round(doc, method: str | None = None) -> None:
	del method
	if not doc.is_new():
		frappe.throw("Quality action verification rounds are immutable append-only receipts.")
	action_name = str(doc.get("action_item") or "")
	if not _has_capability("action-verification-round", action_name):
		frappe.throw("Quality action verification rounds may only be created by the governed POST API.")
	action = frappe.get_doc("IONE Quality Action Item", action_name)
	_inherit_and_validate_scope(doc, action, "quality action item")
	if str(action.get("status") or "") != "Pending Verification":
		frappe.throw("A verification receipt requires a Pending Verification action item.")
	decision = str(doc.get("decision") or "")
	if decision not in ACTION_VERIFICATION_STATUS_AFTER:
		frappe.throw("Unsupported quality action verification decision.")
	status_after = ACTION_VERIFICATION_STATUS_AFTER[decision]
	if str(doc.get("action_status_after") or "") != status_after:
		frappe.throw("Verification receipt action_status_after does not match its decision.")
	if str(doc.get("assigned_to_snapshot") or "") != str(action.get("assigned_to") or ""):
		frappe.throw("Verification receipt action-owner snapshot is inconsistent.")
	if str(doc.get("verifier_snapshot") or "") != str(action.get("verifier") or ""):
		frappe.throw("Verification receipt verifier snapshot is inconsistent.")
	if str(doc.get("submitted_by") or "") != str(action.get("submitted_for_verification_by") or ""):
		frappe.throw("Verification receipt submission actor is inconsistent.")
	if not _same_datetime(
		doc.get("submitted_at"),
		action.get("submitted_for_verification_at"),
	):
		frappe.throw("Verification receipt submission time is inconsistent.")
	if str(doc.get("owner_submission_key") or "") != str(action.get("current_submission_key") or ""):
		frappe.throw("Verification receipt owner submission key is inconsistent.")
	if not hmac.compare_digest(
		str(doc.get("completion_evidence_snapshot") or ""),
		str(action.get("completion_evidence") or ""),
	) or not hmac.compare_digest(
		str(doc.get("effect_result_snapshot") or ""),
		str(action.get("effect_result") or ""),
	):
		frappe.throw("Verification receipt owner evidence does not match the pending submission.")
	if str(doc.get("verified_by") or "") != str(action.get("verifier") or ""):
		frappe.throw("Verification receipt must name the configured independent verifier.")
	if str(doc.get("verified_by") or "") in {
		str(action.get("assigned_to") or ""),
		str(action.get("created_by") or ""),
		str(action.get("submitted_for_verification_by") or ""),
	}:
		frappe.throw("Quality action verification requires an independent named verifier.")
	_bounded_text(doc.get("verification_comment"), "verification_comment", 10_000, minimum=5)
	_idempotency_key(doc.get("owner_submission_key"))
	_idempotency_key(doc.get("verifier_decision_key"))
	if not doc.get("verified_at"):
		frappe.throw("Verification receipt requires a verifier decision time.")
	if get_datetime(doc.get("verified_at")) < get_datetime(doc.get("submitted_at")):
		frappe.throw("Verification receipt decision time cannot precede owner submission.")

	rows = _validated_action_verification_chain(action)
	expected_round = len(rows) + 1
	if expected_round > MAX_ACTION_VERIFICATION_ROUNDS:
		frappe.throw("Quality action verification-round limit has been reached.")
	if int(doc.get("round_no") or 0) != expected_round:
		frappe.throw("Quality action verification round_no is not the next append-only sequence.")
	if int(action.get("verification_round_no") or 0) != expected_round:
		frappe.throw("Pending action verification_round_no does not match its receipt.")
	previous_checksum = str(rows[-1].get("receipt_checksum") or "") if rows else ""
	if not hmac.compare_digest(
		str(doc.get("previous_receipt_checksum") or ""),
		previous_checksum,
	):
		frappe.throw("Verification receipt does not extend the current checksum chain.")
	expected_evidence_hash = _action_evidence_hash(doc)
	if not hmac.compare_digest(
		str(doc.get("completion_evidence_hash") or ""),
		expected_evidence_hash,
	):
		frappe.throw("Verification receipt completion evidence hash is invalid.")
	expected_submission = _action_owner_submission_checksum(doc)
	if not hmac.compare_digest(
		str(doc.get("owner_submission_checksum") or ""),
		expected_submission,
	) or not hmac.compare_digest(
		expected_submission,
		str(action.get("current_submission_checksum") or ""),
	):
		frappe.throw("Verification receipt owner submission checksum is invalid.")
	expected_receipt = _checksum(_action_verification_round_snapshot(doc))
	if not hmac.compare_digest(str(doc.get("receipt_checksum") or ""), expected_receipt):
		frappe.throw("Quality action verification receipt checksum is invalid.")


def validate_quality_experience_share(doc, method: str | None = None) -> None:
	del method
	status = _status(doc, EXPERIENCE_STATUSES, "quality experience share")
	_validate_scope_shape(doc)
	_validate_experience_source(doc)
	_validate_configured_user(doc.get("publisher"), "publisher", GOVERNANCE_APPROVER_ROLES, doc)
	content = _experience_content(doc.get("content_json"))
	doc.content_json = _canonical_json(content)
	_validate_deidentification(doc, content)
	_validate_experience_version(doc)
	expected_publication_key = str(doc.get("experience_code") or "") if status == "Published" else ""
	if doc.get("active_publication_key") and not hmac.compare_digest(
		str(doc.get("active_publication_key")),
		expected_publication_key,
	):
		frappe.throw("Experience active_publication_key does not match its workflow state.")
	doc.active_publication_key = expected_publication_key or None
	_validate_effective_period(doc)
	_validate_experience_status_gates(doc, status)
	previous = doc.get_doc_before_save()
	if not previous:
		if status != "Draft":
			frappe.throw("New quality experience shares must start in Draft status.")
		_require_named_actor(EXPERIENCE_AUTHOR_ROLES, scope_doc=doc)
		if str(doc.get("publisher") or "") == str(doc.get("owner") or ""):
			frappe.throw("Experience author and configured publisher must be independent.")
		_reject_audit_values(doc, _EXPERIENCE_AUDIT_FIELDS)
		if doc.get("checksum"):
			frappe.throw("Draft experience shares cannot supply a publication checksum.")
		return
	_validate_transition(previous.get("status"), status, EXPERIENCE_TRANSITIONS, "experience share")
	transitioned = str(previous.get("status") or "") != status
	if transitioned and not _has_capability("experience", doc.name):
		frappe.throw("Experience share transitions must use the governed POST API.")
	if not transitioned:
		if status != "Draft":
			if _changed_business_fields(doc, previous):
				frappe.throw("Submitted and published experience shares are immutable.")
		else:
			_require_named_actor(EXPERIENCE_AUTHOR_ROLES, scope_doc=doc)
			if frappe.session.user != str(doc.get("owner") or ""):
				frappe.throw("Only the named experience author may edit its Draft content.")
			_reject_changed_fields(doc, previous, _EXPERIENCE_AUDIT_FIELDS)
	if str(previous.get("status") or "") in {"Rejected", "Retired"}:
		frappe.throw("Terminal experience share records are immutable.")
	if str(previous.get("status") or "") == "Published":
		_validate_published_experience_immutability(doc, previous)


def submit_quality_meeting(meeting: str) -> dict[str, str]:
	_require_post()
	actor = _require_named_actor(MEETING_AUTHOR_ROLES)
	with frappe.db.advisory_lock(f"ione-qms:quality-meeting:{meeting}", timeout=5):
		doc = frappe.get_doc("IONE Quality Meeting", meeting)
		_require_scope(doc, actor)
		if str(doc.status or "") != "Draft":
			frappe.throw("Only a Draft quality meeting may be submitted.")
		if actor != str(doc.owner or ""):
			frappe.throw("Only the named meeting creator may submit it.")
		if actor == str(doc.meeting_approver or ""):
			frappe.throw("Meeting submitter and configured approver must be independent.")
		doc.status = "Submitted"
		doc.submitted_by = actor
		doc.submitted_at = now_datetime()
		_save_governed(doc, "meeting")
	return {"meeting": str(doc.name), "status": str(doc.status)}


def review_quality_meeting(
	meeting: str,
	decision: str,
	review_comment: str,
) -> dict[str, str]:
	_require_post()
	actor = _require_named_actor(GOVERNANCE_APPROVER_ROLES)
	if decision not in {"Approve", "Cancel"}:
		frappe.throw("Meeting review decision must be Approve or Cancel.")
	comment = _bounded_text(review_comment, "review_comment", 2_000, minimum=5)
	with frappe.db.advisory_lock(f"ione-qms:quality-meeting:{meeting}", timeout=5):
		doc = frappe.get_doc("IONE Quality Meeting", meeting)
		_require_scope(doc, actor)
		if str(doc.status or "") != "Submitted":
			frappe.throw("Only a Submitted quality meeting may be reviewed.")
		if actor != str(doc.meeting_approver or ""):
			frappe.throw("Only the explicitly configured meeting approver may review this meeting.")
		if actor in {str(doc.owner or ""), str(doc.submitted_by or "")}:
			frappe.throw("Meeting approval must be performed by an independent named approver.")
		if decision == "Approve":
			doc.status = "Approved"
			doc.approved_by = actor
			doc.approved_at = now_datetime()
			doc.approval_comment = comment
			doc.agenda_checksum = _meeting_agenda_checksum(doc)
		else:
			doc.status = "Cancelled"
			doc.cancelled_by = actor
			doc.cancelled_at = now_datetime()
			doc.cancellation_reason = comment
		_save_governed(doc, "meeting")
	return {"meeting": str(doc.name), "status": str(doc.status)}


def cancel_quality_meeting(meeting: str, reason: str) -> dict[str, str]:
	_require_post()
	actor = _require_named_actor(MEETING_AUTHOR_ROLES | GOVERNANCE_APPROVER_ROLES)
	comment = _bounded_text(reason, "reason", 2_000, minimum=5)
	with frappe.db.advisory_lock(f"ione-qms:quality-meeting:{meeting}", timeout=5):
		doc = frappe.get_doc("IONE Quality Meeting", meeting)
		_require_scope(doc, actor)
		if str(doc.status or "") not in {"Draft", "Submitted", "Approved"}:
			frappe.throw("Only Draft, Submitted, or Approved meetings may be cancelled.")
		if str(doc.status or "") == "Draft" and actor != str(doc.owner or ""):
			frappe.throw("Only the meeting creator may cancel its Draft.")
		if str(doc.status or "") in {"Submitted", "Approved"} and actor != str(doc.meeting_approver or ""):
			frappe.throw("Only the configured meeting approver may cancel a submitted meeting.")
		doc.status = "Cancelled"
		doc.cancelled_by = actor
		doc.cancelled_at = now_datetime()
		doc.cancellation_reason = comment
		_save_governed(doc, "meeting")
	return {"meeting": str(doc.name), "status": str(doc.status)}


def record_quality_meeting_held(
	meeting: str,
	actual_start: str,
	actual_end: str,
	attendance: Sequence[Mapping[str, Any]] | str,
	evidence_reference: str,
) -> dict[str, str]:
	_require_post()
	actor = _require_named_actor(MEETING_AUTHOR_ROLES)
	start = get_datetime(actual_start)
	end = get_datetime(actual_end)
	if end <= start:
		frappe.throw("Meeting actual_end must be later than actual_start.")
	evidence = _bounded_text(evidence_reference, "evidence_reference", 2_000, minimum=5)
	attendance_rows = _attendance_rows(attendance, start, end)
	with frappe.db.advisory_lock(f"ione-qms:quality-meeting:{meeting}", timeout=5):
		doc = frappe.get_doc("IONE Quality Meeting", meeting)
		_require_scope(doc, actor)
		if str(doc.status or "") != "Approved":
			frappe.throw("Only an Approved quality meeting may be recorded as Held.")
		if actor not in {str(doc.chair or ""), str(doc.owner or "")}:
			frappe.throw("Only the configured chair or meeting creator may record attendance.")
		_validate_frozen_agenda(doc, expected_checksum=str(doc.agenda_checksum or ""))
		_apply_attendance(doc, attendance_rows, start, end)
		doc.actual_start = start
		doc.actual_end = end
		doc.held_by = actor
		doc.held_at = now_datetime()
		doc.held_evidence_reference = evidence
		doc.status = "Held"
		_save_governed(doc, "meeting")
	return {"meeting": str(doc.name), "status": str(doc.status)}


def mark_meeting_minutes_pending(meeting: str) -> dict[str, str]:
	_require_post()
	actor = _require_named_actor(MEETING_AUTHOR_ROLES)
	with frappe.db.advisory_lock(f"ione-qms:quality-meeting:{meeting}", timeout=5):
		doc = frappe.get_doc("IONE Quality Meeting", meeting)
		_require_scope(doc, actor)
		if str(doc.status or "") != "Held":
			frappe.throw("Only a Held quality meeting may enter Minutes Pending.")
		if actor not in {str(doc.chair or ""), str(doc.owner or "")}:
			frappe.throw("Only the configured chair or meeting creator may open minute preparation.")
		doc.status = "Minutes Pending"
		doc.minutes_pending_by = actor
		doc.minutes_pending_at = now_datetime()
		_save_governed(doc, "meeting")
	return {"meeting": str(doc.name), "status": str(doc.status)}


def submit_meeting_minute(minute: str) -> dict[str, str]:
	_require_post()
	actor = _require_named_actor(MEETING_AUTHOR_ROLES)
	with frappe.db.advisory_lock(f"ione-qms:meeting-minute:{minute}", timeout=5):
		doc = frappe.get_doc("IONE Meeting Minute", minute)
		meeting = frappe.get_doc("IONE Quality Meeting", doc.meeting)
		_require_scope(meeting, actor)
		if str(meeting.status or "") != "Minutes Pending":
			frappe.throw("Minutes may only be submitted while the meeting is Minutes Pending.")
		if str(doc.status or "") != "Draft":
			frappe.throw("Only Draft meeting minutes may be submitted.")
		if actor != str(doc.owner or ""):
			frappe.throw("Only the named minute creator may submit it.")
		if actor == str(meeting.minute_approver or ""):
			frappe.throw("Minute author and configured approver must be independent.")
		doc.status = "Submitted"
		doc.submitted_by = actor
		doc.submitted_at = now_datetime()
		_save_governed(doc, "minute")
	return {"minute": str(doc.name), "status": str(doc.status)}


def review_meeting_minute(
	minute: str,
	decision: str,
	review_comment: str,
) -> dict[str, Any]:
	_require_post()
	actor = _require_named_actor(GOVERNANCE_APPROVER_ROLES)
	if decision not in {"Approve", "Reject"}:
		frappe.throw("Minute review decision must be Approve or Reject.")
	comment = _bounded_text(review_comment, "review_comment", 2_000, minimum=5)
	derived: list[str] = []
	meeting_name = str(frappe.db.get_value("IONE Meeting Minute", minute, "meeting") or "")
	if not meeting_name:
		frappe.throw("Meeting minute does not exist or has no meeting linkage.")
	meeting_lock = hashlib.sha256(meeting_name.encode()).hexdigest()[:32]
	with frappe.db.advisory_lock(f"ione-qms:meeting-minute-review:{meeting_lock}", timeout=5):
		savepoint = f"ione_meeting_minute_{hashlib.sha256(minute.encode()).hexdigest()[:16]}"
		frappe.db.savepoint(savepoint)
		try:
			if not frappe.db.get_value(
				"IONE Quality Meeting",
				meeting_name,
				"name",
				for_update=True,
			):
				frappe.throw("The linked quality meeting no longer exists.")
			if not frappe.db.get_value(
				"IONE Meeting Minute",
				minute,
				"name",
				for_update=True,
			):
				frappe.throw("The meeting minute no longer exists.")
			doc = frappe.get_doc("IONE Meeting Minute", minute)
			if str(doc.get("meeting") or "") != meeting_name:
				frappe.throw("Meeting minute linkage changed during review.")
			meeting = frappe.get_doc("IONE Quality Meeting", doc.meeting)
			_require_scope(meeting, actor)
			if str(meeting.status or "") != "Minutes Pending":
				frappe.throw("Minutes may only be reviewed while the meeting is Minutes Pending.")
			if str(doc.status or "") != "Submitted":
				frappe.throw("Only Submitted meeting minutes may be reviewed.")
			if actor != str(meeting.minute_approver or ""):
				frappe.throw("Only the explicitly configured minute approver may review this minute.")
			if actor in {str(doc.owner or ""), str(doc.submitted_by or "")}:
				frappe.throw("Minute approval must be performed by an independent named approver.")
			if decision == "Approve" and frappe.db.exists(
				"IONE Meeting Minute",
				{"meeting": meeting.name, "status": "Approved", "name": ["!=", doc.name]},
			):
				frappe.throw("A quality meeting may have only one Approved minute.")
			doc.status = "Approved" if decision == "Approve" else "Rejected"
			doc.reviewed_by = actor
			doc.reviewed_at = now_datetime()
			doc.review_comment = comment
			doc.approved_meeting_key = str(meeting.name) if decision == "Approve" else None
			doc.checksum = _checksum(_minute_snapshot(doc)) if decision == "Approve" else ""
			_save_governed(doc, "minute")
			if decision == "Approve":
				derived = _derive_meeting_decisions(doc, meeting, actor)
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			raise
		else:
			frappe.db.release_savepoint(savepoint)
	return {"minute": str(doc.name), "status": str(doc.status), "decisions": derived}


def close_quality_meeting(meeting: str) -> dict[str, str]:
	_require_post()
	actor = _require_named_actor(GOVERNANCE_APPROVER_ROLES)
	with frappe.db.advisory_lock(f"ione-qms:quality-meeting:{meeting}", timeout=5):
		doc = frappe.get_doc("IONE Quality Meeting", meeting)
		_require_scope(doc, actor)
		if str(doc.status or "") != "Minutes Pending":
			frappe.throw("Only a Minutes Pending quality meeting may be closed.")
		if actor != str(doc.minute_approver or ""):
			frappe.throw("Only the configured minute approver may close this meeting.")
		minute = frappe.db.get_value(
			"IONE Meeting Minute",
			{"meeting": doc.name, "status": "Approved"},
			["name", "checksum"],
			as_dict=True,
		)
		if not minute or not minute.get("checksum"):
			frappe.throw("Quality meeting closure requires one Approved, checksummed minute.")
		doc.status = "Closed"
		doc.closed_by = actor
		doc.closed_at = now_datetime()
		_save_governed(doc, "meeting")
	return {"meeting": str(doc.name), "status": str(doc.status), "minute": str(minute.get("name"))}


def create_quality_action_item(
	title: str,
	description: str,
	assigned_to: str,
	verifier: str,
	due_date: str,
	*,
	meeting: str | None = None,
	meeting_decision: str | None = None,
	finding: str | None = None,
	pdca_project: str | None = None,
	safety_event: str | None = None,
) -> dict[str, str]:
	_require_post()
	actor = _require_named_actor(MEETING_AUTHOR_ROLES)
	links, scope = _resolved_action_links(
		meeting=meeting,
		meeting_decision=meeting_decision,
		finding=finding,
		pdca_project=pdca_project,
		safety_event=safety_event,
	)
	_require_scope(scope, actor)
	if getdate(due_date) < getdate(nowdate()):
		frappe.throw("Quality action item due_date cannot be in the past.")
	_validate_configured_user(assigned_to, "assigned_to", ACTION_OWNER_ROLES, scope)
	_validate_configured_user(verifier, "verifier", GOVERNANCE_APPROVER_ROLES, scope)
	if str(assigned_to) == str(verifier):
		frappe.throw("Action owner and independent verifier must be different named users.")
	if str(verifier) == actor:
		frappe.throw("Action creator and independent verifier must be different named users.")
	payload = {
		"doctype": "IONE Quality Action Item",
		"title": _bounded_text(title, "title", 300, minimum=3),
		"description": _bounded_text(description, "description", 20_000, minimum=10),
		"assigned_to": assigned_to,
		"verifier": verifier,
		"due_date": getdate(due_date),
		"status": "Open",
		**links,
		**_scope_values(scope),
		"created_by": actor,
		"created_at": now_datetime(),
	}
	with _capability("action", "new"):
		doc = frappe.get_doc(payload)
		doc.insert(ignore_permissions=True)
	return {"action_item": str(doc.name), "status": str(doc.status)}


def start_quality_action_item(action_item: str) -> dict[str, str]:
	_require_post()
	actor = _require_named_actor(ACTION_OWNER_ROLES)
	with frappe.db.advisory_lock(f"ione-qms:quality-action:{action_item}", timeout=5):
		doc = frappe.get_doc("IONE Quality Action Item", action_item)
		_require_scope(doc, actor)
		if str(doc.status or "") != "Open":
			frappe.throw("Only an Open quality action item may be started.")
		if actor != str(doc.assigned_to or ""):
			frappe.throw("Only the assigned action owner may start this item.")
		doc.status = "In Progress"
		doc.started_by = actor
		doc.started_at = now_datetime()
		_save_governed(doc, "action")
	return {"action_item": str(doc.name), "status": str(doc.status)}


def submit_quality_action_verification(
	action_item: str,
	completion_evidence: str,
	effect_result: str,
	request_key: str,
) -> dict[str, str]:
	_require_post()
	actor = _require_named_actor(ACTION_OWNER_ROLES)
	evidence = _bounded_text(
		completion_evidence,
		"completion_evidence",
		20_000,
		minimum=10,
	)
	effect = _bounded_text(effect_result, "effect_result", 20_000, minimum=10)
	submission_key = _idempotency_key(request_key)
	with frappe.db.advisory_lock(f"ione-qms:quality-action:{action_item}", timeout=5):
		doc = frappe.get_doc("IONE Quality Action Item", action_item)
		_require_scope(doc, actor)
		prior = _idempotent_action_submission(
			doc,
			submission_key,
			evidence,
			effect,
			actor,
		)
		if prior:
			return prior
		if str(doc.status or "") != "In Progress":
			frappe.throw("Only an In Progress action item may enter Pending Verification.")
		if actor != str(doc.assigned_to or ""):
			frappe.throw("Only the assigned action owner may submit completion evidence.")
		rows = _validated_action_verification_chain(doc)
		round_no = len(rows) + 1
		if round_no > MAX_ACTION_VERIFICATION_ROUNDS:
			frappe.throw("Quality action verification-round limit has been reached.")
		submitted_at = now_datetime()
		evidence_hash = _checksum(
			{
				"completion_evidence": evidence,
				"effect_result": effect,
			}
		)
		submission_checksum = _checksum(
			{
				"action_item": str(doc.name),
				"round_no": round_no,
				"owner_submission_key": submission_key,
				"completion_evidence_hash": evidence_hash,
				"submitted_by": actor,
				"submitted_at": submitted_at,
			}
		)
		doc.completion_evidence = evidence
		doc.effect_result = effect
		doc.current_submission_key = submission_key
		doc.current_submission_checksum = submission_checksum
		doc.verification_round_no = round_no
		doc.submitted_for_verification_by = actor
		doc.submitted_for_verification_at = submitted_at
		doc.status = "Pending Verification"
		_save_governed(doc, "action")
	return {
		"action_item": str(doc.name),
		"status": str(doc.status),
		"round_no": str(round_no),
		"submission_checksum": submission_checksum,
	}


def review_quality_action_item(
	action_item: str,
	decision: str,
	verification_comment: str,
	request_key: str,
) -> dict[str, str]:
	_require_post()
	actor = _require_named_actor(GOVERNANCE_APPROVER_ROLES)
	if decision not in {"Close", "Rework"}:
		frappe.throw("Action verification decision must be Close or Rework.")
	comment = _bounded_text(
		verification_comment,
		"verification_comment",
		10_000,
		minimum=5,
	)
	decision_key = _idempotency_key(request_key)
	with frappe.db.advisory_lock(f"ione-qms:quality-action:{action_item}", timeout=5):
		doc = frappe.get_doc("IONE Quality Action Item", action_item)
		_require_scope(doc, actor)
		prior = _idempotent_action_decision(
			doc,
			decision_key,
			decision,
			comment,
			actor,
		)
		if prior:
			return prior
		if str(doc.status or "") != "Pending Verification":
			frappe.throw("Only a Pending Verification action item may be reviewed.")
		if actor != str(doc.verifier or ""):
			frappe.throw("Only the explicitly configured independent verifier may review this item.")
		if actor in {
			str(doc.assigned_to or ""),
			str(doc.created_by or ""),
			str(doc.submitted_for_verification_by or ""),
		}:
			frappe.throw("Quality action closure requires an independent named verifier.")
		round_doc = _append_action_verification_round(
			doc,
			decision=decision,
			comment=comment,
			actor=actor,
			decision_key=decision_key,
		)
		doc.latest_verification_round = round_doc.name
		doc.latest_verification_receipt_checksum = round_doc.receipt_checksum
		if decision == "Close":
			doc.status = "Closed"
			doc.closed_by = actor
			doc.closed_at = round_doc.verified_at
		else:
			doc.status = "In Progress"
		_save_governed(doc, "action")
	return {
		"action_item": str(doc.name),
		"status": str(doc.status),
		"verification_round": str(round_doc.name),
		"receipt_checksum": str(round_doc.receipt_checksum),
	}


def cancel_quality_action_item(action_item: str, reason: str, request_key: str) -> dict[str, str]:
	_require_post()
	actor = _require_named_actor(GOVERNANCE_APPROVER_ROLES)
	comment = _bounded_text(reason, "reason", 2_000, minimum=5)
	decision_key = _idempotency_key(request_key)
	with frappe.db.advisory_lock(f"ione-qms:quality-action:{action_item}", timeout=5):
		doc = frappe.get_doc("IONE Quality Action Item", action_item)
		_require_scope(doc, actor)
		prior = _idempotent_action_decision(
			doc,
			decision_key,
			"Cancel",
			comment,
			actor,
		)
		if prior:
			return prior
		if str(doc.status or "") not in {"Open", "In Progress", "Pending Verification"}:
			frappe.throw("Only an active quality action item may be cancelled.")
		if actor != str(doc.verifier or ""):
			frappe.throw("Only the configured independent verifier may cancel this item.")
		round_doc = None
		if str(doc.status or "") == "Pending Verification":
			round_doc = _append_action_verification_round(
				doc,
				decision="Cancel",
				comment=comment,
				actor=actor,
				decision_key=decision_key,
			)
			doc.latest_verification_round = round_doc.name
			doc.latest_verification_receipt_checksum = round_doc.receipt_checksum
		doc.status = "Cancelled"
		doc.cancelled_by = actor
		doc.cancelled_at = round_doc.verified_at if round_doc else now_datetime()
		doc.cancellation_reason = comment
		_save_governed(doc, "action")
	result = {"action_item": str(doc.name), "status": str(doc.status)}
	if round_doc:
		result.update(
			{
				"verification_round": str(round_doc.name),
				"receipt_checksum": str(round_doc.receipt_checksum),
			}
		)
	return result


def quality_action_workbench(
	status: str | None = None,
	role: str | None = None,
	limit: int = 100,
) -> dict[str, Any]:
	_require_post()
	actor = _require_named_actor(ACTION_READ_ROLES)
	if status and status not in ACTION_STATUSES:
		frappe.throw("Unsupported quality action item status filter.")
	if role not in {None, "", "Owner", "Verifier", "All"}:
		frappe.throw("Action workbench role must be Owner, Verifier, or All.")
	context = get_access_context(actor)
	if role in {None, ""}:
		role = "All" if context.roles.intersection(ACTION_OVERSIGHT_ROLES) else "Owner"
	if role == "All" and not context.roles.intersection(ACTION_OVERSIGHT_ROLES):
		frappe.throw("The All action workbench requires an explicit oversight role.")
	filters: dict[str, Any] = {}
	if status:
		filters["status"] = status
	if role == "Owner":
		filters["assigned_to"] = actor
	elif role == "Verifier":
		filters["verifier"] = actor
	rows = frappe.get_list(
		"IONE Quality Action Item",
		filters=filters,
		fields=[
			"name",
			"title",
			"status",
			"hospital",
			"campus",
			"department",
			"ward",
			"assigned_to",
			"verifier",
			"due_date",
			"modified",
		],
		order_by="due_date asc, modified desc",
		limit_page_length=min(max(int(limit or 100), 1), MAX_WORKBENCH_ROWS),
	)
	today = getdate(nowdate())
	items = []
	for row in rows:
		item = dict(row)
		due = getdate(row.get("due_date"))
		item["overdue"] = bool(due < today and str(row.get("status") or "") not in _TERMINAL_ACTION_STATES)
		items.append(item)
	return {
		"items": items,
		"counts": {
			"total": len(items),
			"overdue": sum(1 for item in items if item["overdue"]),
			"pending_verification": sum(1 for item in items if item["status"] == "Pending Verification"),
		},
	}


def submit_quality_experience_share(experience: str) -> dict[str, str]:
	_require_post()
	actor = _require_named_actor(EXPERIENCE_AUTHOR_ROLES)
	with frappe.db.advisory_lock(f"ione-qms:experience:{experience}", timeout=5):
		doc = frappe.get_doc("IONE Quality Experience Share", experience)
		_require_scope(doc, actor)
		if str(doc.status or "") != "Draft":
			frappe.throw("Only a Draft experience share may be submitted.")
		if actor != str(doc.owner or ""):
			frappe.throw("Only the named experience author may submit it.")
		if actor == str(doc.publisher or ""):
			frappe.throw("Experience author and configured publisher must be independent.")
		doc.status = "Submitted"
		doc.submitted_by = actor
		doc.submitted_at = now_datetime()
		_save_governed(doc, "experience")
	return {"experience": str(doc.name), "status": str(doc.status)}


def review_quality_experience_share(
	experience: str,
	decision: str,
	review_comment: str,
) -> dict[str, str]:
	_require_post()
	actor = _require_named_actor(GOVERNANCE_APPROVER_ROLES)
	if decision not in {"Publish", "Reject"}:
		frappe.throw("Experience review decision must be Publish or Reject.")
	comment = _bounded_text(review_comment, "review_comment", 2_000, minimum=5)
	experience_code = str(
		frappe.db.get_value("IONE Quality Experience Share", experience, "experience_code") or ""
	)
	if not experience_code:
		frappe.throw("Experience share does not exist or has no governed experience_code.")
	code_lock = hashlib.sha256(experience_code.encode()).hexdigest()[:32]
	with frappe.db.advisory_lock(f"ione-qms:experience-publication:{code_lock}", timeout=5):
		if not frappe.db.get_value(
			"IONE Quality Experience Share",
			experience,
			"name",
			for_update=True,
		):
			frappe.throw("The experience share no longer exists.")
		doc = frappe.get_doc("IONE Quality Experience Share", experience)
		if str(doc.get("experience_code") or "") != experience_code:
			frappe.throw("Experience code changed during publication review.")
		_require_scope(doc, actor)
		if str(doc.status or "") != "Submitted":
			frappe.throw("Only a Submitted experience share may be reviewed.")
		if actor != str(doc.publisher or ""):
			frappe.throw("Only the explicitly configured publisher may review this experience.")
		if actor in {str(doc.owner or ""), str(doc.submitted_by or "")}:
			frappe.throw("Experience publication requires an independent named reviewer.")
		if decision == "Publish":
			_validate_no_other_published_version(doc)
			doc.status = "Published"
			doc.published_by = actor
			doc.published_at = now_datetime()
			doc.review_comment = comment
			doc.active_publication_key = experience_code
			doc.checksum = _checksum(_experience_snapshot(doc))
		else:
			doc.status = "Rejected"
			doc.review_comment = comment
			doc.published_by = actor
			doc.published_at = now_datetime()
		_save_governed(doc, "experience")
	return {"experience": str(doc.name), "status": str(doc.status)}


def retire_quality_experience_share(experience: str, reason: str) -> dict[str, str]:
	_require_post()
	actor = _require_named_actor(GOVERNANCE_APPROVER_ROLES)
	comment = _bounded_text(reason, "reason", 2_000, minimum=5)
	with frappe.db.advisory_lock(f"ione-qms:experience:{experience}", timeout=5):
		doc = frappe.get_doc("IONE Quality Experience Share", experience)
		_require_scope(doc, actor)
		if str(doc.status or "") != "Published":
			frappe.throw("Only a Published experience share may be retired.")
		if actor != str(doc.publisher or ""):
			frappe.throw("Only the configured publisher may retire this experience.")
		doc.status = "Retired"
		doc.retired_by = actor
		doc.retired_at = now_datetime()
		doc.retirement_reason = comment
		doc.active_publication_key = None
		_save_governed(doc, "experience")
	return {"experience": str(doc.name), "status": str(doc.status)}


def prevent_quality_meeting_deletion(doc, method: str | None = None) -> None:
	del method
	if bool(getattr(frappe.flags, "in_uninstall", False)):
		return
	frappe.throw(f"{doc.doctype} records are retained governance records and cannot be deleted.")


def _meeting_for_minute(doc):
	if not doc.get("meeting"):
		frappe.throw("Meeting minute requires a quality meeting.")
	return frappe.get_doc("IONE Quality Meeting", doc.get("meeting"))


def _validate_meeting_content(doc) -> None:
	_bounded_text(doc.get("meeting_code"), "meeting_code", 100, minimum=1)
	_bounded_text(doc.get("title"), "title", 300, minimum=3)
	_bounded_text(doc.get("meeting_type"), "meeting_type", 200, minimum=1)
	start = get_datetime(doc.get("scheduled_start"))
	end = get_datetime(doc.get("scheduled_end"))
	if end <= start:
		frappe.throw("Meeting scheduled_end must be later than scheduled_start.")
	agenda = list(doc.get("agenda_items") or [])
	attendees = list(doc.get("attendees") or [])
	if not 1 <= len(agenda) <= MAX_AGENDA_ITEMS:
		frappe.throw("Quality meeting requires 1-100 agenda items.")
	if not 1 <= len(attendees) <= MAX_ATTENDEES:
		frappe.throw("Quality meeting requires 1-200 explicitly configured attendees.")
	agenda_keys: set[str] = set()
	for index, row in enumerate(agenda, start=1):
		key = str(row.get("agenda_key") or "").strip()
		if not key:
			key = hashlib.sha256(f"{doc.get('meeting_code')}|agenda|{index}".encode()).hexdigest()
			row.agenda_key = key
		if key in agenda_keys:
			frappe.throw("Meeting agenda_key values must be unique.")
		agenda_keys.add(key)
		_bounded_text(row.get("subject"), "agenda subject", 300, minimum=3)
		_bounded_text(row.get("objective"), "agenda objective", 2_000, minimum=5)
		if int(row.get("duration_minutes") or 0) < 1:
			frappe.throw("Every agenda item requires a positive duration in minutes.")
		_validate_agenda_material(row, doc)
	attendee_users: set[str] = set()
	for row in attendees:
		user = str(row.get("user") or "").strip()
		if not user or user in attendee_users:
			frappe.throw("Meeting attendee users must be present and unique.")
		attendee_users.add(user)
		_validate_active_user(user, "meeting attendee")
		_bounded_text(row.get("participation_role"), "participation_role", 200, minimum=1)
		status = str(row.get("attendance_status") or "Pending")
		if status not in {"Pending", "Attended", "Absent"}:
			frappe.throw("Unsupported meeting attendance status.")
	if str(doc.get("chair") or "") not in attendee_users:
		frappe.throw("Configured meeting chair must also be an explicit attendee.")
	secretary = str(doc.get("secretary") or "").strip()
	if secretary:
		_validate_active_user(secretary, "meeting secretary")
		if secretary not in attendee_users:
			frappe.throw("Configured meeting secretary must also be an explicit attendee.")


def _validate_meeting_status_gates(doc, status: str) -> None:
	if status in {"Draft", "Submitted", "Approved"}:
		for row in doc.get("attendees") or []:
			if str(row.get("attendance_status") or "Pending") != "Pending" or any(
				row.get(fieldname)
				for fieldname in (
					"joined_at",
					"left_at",
					"attendance_evidence",
					"absence_reason",
				)
			):
				frappe.throw("Attendance evidence may only be recorded when an Approved meeting is Held.")
	if status in {"Submitted", "Approved", "Held", "Minutes Pending", "Closed"} and (
		not doc.get("submitted_by") or not doc.get("submitted_at")
	):
		frappe.throw("Submitted meetings require system-maintained submission evidence.")
	if status in {"Approved", "Held", "Minutes Pending", "Closed"}:
		if not doc.get("approved_by") or not doc.get("approved_at"):
			frappe.throw("Approved meetings require system-maintained approval evidence.")
		_validate_frozen_agenda(doc, expected_checksum=str(doc.get("agenda_checksum") or ""))
		_bounded_text(doc.get("approval_comment"), "approval_comment", 2_000, minimum=5)
	if status in {"Held", "Minutes Pending", "Closed"}:
		start = get_datetime(doc.get("actual_start"))
		end = get_datetime(doc.get("actual_end"))
		if end <= start:
			frappe.throw("Held meetings require a valid actual start and end interval.")
		_bounded_text(
			doc.get("held_evidence_reference"),
			"held_evidence_reference",
			2_000,
			minimum=5,
		)
		if not doc.get("held_by") or not doc.get("held_at"):
			frappe.throw("Held meetings require system-maintained actor and timestamp evidence.")
		if not any(
			str(row.get("attendance_status") or "") == "Attended" for row in doc.get("attendees") or []
		):
			frappe.throw("Held meetings require at least one evidenced participant.")
	if status == "Minutes Pending" and (
		not doc.get("minutes_pending_by") or not doc.get("minutes_pending_at")
	):
		frappe.throw("Minutes Pending meetings require system-maintained audit evidence.")
	if status == "Closed" and (not doc.get("closed_by") or not doc.get("closed_at")):
		frappe.throw("Closed meetings require system-maintained closure evidence.")
	if status == "Cancelled":
		_bounded_text(doc.get("cancellation_reason"), "cancellation_reason", 2_000, minimum=5)
		if not doc.get("cancelled_by") or not doc.get("cancelled_at"):
			frappe.throw("Cancelled meetings require system-maintained cancellation evidence.")


def _validate_agenda_material(row, meeting) -> None:
	material_type = str(row.get("material_type") or "Manual Reference")
	if material_type not in {"Manual Reference", "AI Report Draft", "None"}:
		frappe.throw("Unsupported agenda material type.")
	ai_draft = str(row.get("ai_report_draft") or "").strip()
	reference = str(row.get("material_reference") or "").strip()
	if material_type == "AI Report Draft":
		if not ai_draft or reference:
			frappe.throw("AI report agenda material must use only the governed ai_report_draft link.")
		draft = frappe.db.get_value(
			"IONE AI Report Draft",
			ai_draft,
			[
				"name",
				"status",
				"hospital",
				"campus",
				"department",
				"ward",
				"reviewed_by",
				"reviewed_at",
				"content",
				"source_references",
			],
			as_dict=True,
		)
		if (
			not draft
			or str(draft.get("status") or "") != "Approved"
			or not draft.get("reviewed_by")
			or not draft.get("reviewed_at")
		):
			frappe.throw("AI report meeting material requires an independently human-approved draft.")
		if str(draft.get("reviewed_by") or "") in {"", "Guest", "Administrator"}:
			frappe.throw("AI report meeting material has invalid human review provenance.")
		reviewer_roles = frozenset(frappe.get_roles(str(draft.get("reviewed_by"))))
		if "IONE Agent Service" in reviewer_roles or not reviewer_roles.intersection(
			AI_MATERIAL_REVIEW_ROLES
		):
			frappe.throw("AI report meeting material was not reviewed by an approved human role.")
		_assert_compatible_scope(meeting, draft, "AI report draft")
		expected = _checksum(
			{
				"name": draft.get("name"),
				"status": draft.get("status"),
				"reviewed_by": draft.get("reviewed_by"),
				"reviewed_at": draft.get("reviewed_at"),
				"content": draft.get("content"),
				"source_references": draft.get("source_references"),
			}
		)
		if row.get("material_checksum") and not hmac.compare_digest(
			str(row.get("material_checksum")), expected
		):
			frappe.throw("AI report meeting material changed after it was linked.")
		row.material_checksum = expected
	elif material_type == "Manual Reference":
		if ai_draft:
			frappe.throw("Manual meeting material cannot link an AI report draft.")
		_bounded_text(reference, "material_reference", 2_000, minimum=1)
		row.material_checksum = _checksum({"reference": reference})
	else:
		if ai_draft or reference:
			frappe.throw("Agenda material fields must be empty when material_type is None.")
		row.material_checksum = ""


def _validate_minute_content(doc) -> None:
	_bounded_text(doc.get("summary"), "summary", 50_000, minimum=10)
	_bounded_text(doc.get("discussion_summary"), "discussion_summary", 100_000, minimum=10)
	_bounded_text(doc.get("evidence_reference"), "evidence_reference", 2_000, minimum=5)
	decisions = _decision_rows(doc.get("decisions_json"))
	doc.decisions_json = _canonical_json(decisions)
	no_decision_reason = str(doc.get("no_decision_reason") or "").strip()
	if decisions and no_decision_reason:
		frappe.throw("Meeting minute cannot contain both decisions and a no_decision_reason.")
	if not decisions:
		_bounded_text(no_decision_reason, "no_decision_reason", 2_000, minimum=10)


def _validate_minute_status_gates(doc, status: str) -> None:
	if status in {"Submitted", "Approved", "Rejected"} and (
		not doc.get("submitted_by") or not doc.get("submitted_at")
	):
		frappe.throw("Submitted meeting minutes require system-maintained submission evidence.")
	if status in {"Approved", "Rejected"}:
		if not doc.get("reviewed_by") or not doc.get("reviewed_at"):
			frappe.throw("Decided meeting minutes require system-maintained review evidence.")
		_bounded_text(doc.get("review_comment"), "review_comment", 2_000, minimum=5)
	if status == "Approved":
		expected = _checksum(_minute_snapshot(doc))
		if not hmac.compare_digest(str(doc.get("checksum") or ""), expected):
			frappe.throw("Approved meeting minute checksum does not match its frozen content.")
	elif doc.get("checksum"):
		frappe.throw("Only Approved meeting minutes may contain an approval checksum.")


def _decision_rows(value: Any) -> list[dict[str, str]]:
	rows = _json_value(value, "decisions_json")
	if not isinstance(rows, list) or len(rows) > MAX_DECISIONS:
		frappe.throw("decisions_json must be an array with no more than 100 rows.")
	result: list[dict[str, str]] = []
	codes: set[str] = set()
	for row in rows:
		if not isinstance(row, Mapping) or set(row) != {
			"decision_code",
			"decision_text",
			"rationale",
		}:
			frappe.throw(
				"Every minute decision must contain exactly decision_code, decision_text, and rationale."
			)
		code = _bounded_text(row.get("decision_code"), "decision_code", 100, minimum=1)
		if code in codes:
			frappe.throw("Minute decision_code values must be unique.")
		codes.add(code)
		result.append(
			{
				"decision_code": code,
				"decision_text": _bounded_text(
					row.get("decision_text"),
					"decision_text",
					20_000,
					minimum=5,
				),
				"rationale": _bounded_text(
					row.get("rationale"),
					"rationale",
					20_000,
					minimum=5,
				),
			}
		)
	if len(_canonical_json(result).encode()) > MAX_JSON_BYTES:
		frappe.throw("decisions_json exceeds 128 KiB.")
	return result


def _derive_meeting_decisions(minute, meeting, actor: str) -> list[str]:
	names: list[str] = []
	for index, row in enumerate(_decision_rows(minute.get("decisions_json")), start=1):
		payload = {
			"doctype": "IONE Meeting Decision",
			"decision_key": _checksum(
				{
					"minute": minute.name,
					"minute_checksum": minute.checksum,
					"decision_code": row["decision_code"],
				}
			),
			"meeting": meeting.name,
			"meeting_minute": minute.name,
			"decision_index": index,
			**row,
			**_scope_values(meeting),
			"derived_by": actor,
			"derived_at": now_datetime(),
		}
		doc = frappe.get_doc(payload)
		doc.checksum = _checksum(_decision_snapshot(doc))
		with _capability("decision", minute.name):
			doc.insert(ignore_permissions=True)
		names.append(str(doc.name))
	return names


def _validate_action_content(doc) -> None:
	_bounded_text(doc.get("title"), "title", 300, minimum=3)
	_bounded_text(doc.get("description"), "description", 20_000, minimum=10)
	if not doc.get("due_date"):
		frappe.throw("Quality action item due_date is required.")


def _validate_action_status_gates(doc, status: str) -> None:
	if status in {"In Progress", "Pending Verification", "Closed"} and (
		not doc.get("started_by") or not doc.get("started_at")
	):
		frappe.throw("Started action items require system-maintained start evidence.")
	if status in {"Pending Verification", "Closed"}:
		if not doc.get("submitted_for_verification_by") or not doc.get("submitted_for_verification_at"):
			frappe.throw("Pending verification requires accountable submission evidence.")
	if status == "Closed":
		if (
			not doc.get("latest_verification_round")
			or not doc.get("latest_verification_receipt_checksum")
			or not doc.get("closed_by")
			or not doc.get("closed_at")
		):
			frappe.throw(
				"Closed action items require an immutable verification receipt and closure evidence."
			)
	if status == "Cancelled":
		_bounded_text(doc.get("cancellation_reason"), "cancellation_reason", 2_000, minimum=5)
		if not doc.get("cancelled_by") or not doc.get("cancelled_at"):
			frappe.throw("Cancelled action items require system-maintained audit evidence.")


def _append_action_verification_round(
	action,
	*,
	decision: str,
	comment: str,
	actor: str,
	decision_key: str,
):
	rows = _validated_action_verification_chain(action)
	round_no = len(rows) + 1
	if round_no > MAX_ACTION_VERIFICATION_ROUNDS:
		frappe.throw("Quality action verification-round limit has been reached.")
	if int(action.get("verification_round_no") or 0) != round_no:
		frappe.throw("Pending action verification round is inconsistent with its receipt chain.")
	verified_at = now_datetime()
	payload = {
		"doctype": "IONE Quality Action Verification Round",
		"round_key": _checksum({"action_item": str(action.name), "round_no": round_no}),
		"action_item": str(action.name),
		"round_no": round_no,
		**_scope_values(action),
		"assigned_to_snapshot": str(action.get("assigned_to") or ""),
		"verifier_snapshot": str(action.get("verifier") or ""),
		"completion_evidence_snapshot": str(action.get("completion_evidence") or ""),
		"effect_result_snapshot": str(action.get("effect_result") or ""),
		"completion_evidence_hash": _checksum(
			{
				"completion_evidence": str(action.get("completion_evidence") or ""),
				"effect_result": str(action.get("effect_result") or ""),
			}
		),
		"owner_submission_key": str(action.get("current_submission_key") or ""),
		"owner_submission_checksum": str(action.get("current_submission_checksum") or ""),
		"submitted_by": str(action.get("submitted_for_verification_by") or ""),
		"submitted_at": action.get("submitted_for_verification_at"),
		"decision": decision,
		"verification_comment": comment,
		"verifier_decision_key": decision_key,
		"verified_by": actor,
		"verified_at": verified_at,
		"action_status_after": ACTION_VERIFICATION_STATUS_AFTER[decision],
		"previous_receipt_checksum": str(rows[-1].get("receipt_checksum") or "") if rows else "",
	}
	round_doc = frappe.get_doc(payload)
	round_doc.receipt_checksum = _checksum(_action_verification_round_snapshot(round_doc))
	with _capability("action-verification-round", str(action.name)):
		round_doc.insert(ignore_permissions=True)
	return round_doc


def _idempotent_action_submission(
	action,
	submission_key: str,
	evidence: str,
	effect: str,
	actor: str,
) -> dict[str, str] | None:
	name = frappe.db.get_value(
		"IONE Quality Action Verification Round",
		{"owner_submission_key": submission_key},
		"name",
	)
	if name:
		round_doc = frappe.get_doc("IONE Quality Action Verification Round", name)
		if (
			str(round_doc.action_item or "") != str(action.name)
			or str(round_doc.submitted_by or "") != actor
			or not hmac.compare_digest(str(round_doc.completion_evidence_snapshot or ""), evidence)
			or not hmac.compare_digest(str(round_doc.effect_result_snapshot or ""), effect)
		):
			frappe.throw("request_key is already bound to a different owner submission.")
		_validated_action_verification_chain(action)
		return {
			"action_item": str(action.name),
			"status": str(round_doc.action_status_after),
			"round_no": str(round_doc.round_no),
			"verification_round": str(round_doc.name),
			"receipt_checksum": str(round_doc.receipt_checksum),
		}
	if str(action.get("current_submission_key") or "") != submission_key:
		return None
	if (
		str(action.get("status") or "") != "Pending Verification"
		or str(action.get("submitted_for_verification_by") or "") != actor
		or not hmac.compare_digest(str(action.get("completion_evidence") or ""), evidence)
		or not hmac.compare_digest(str(action.get("effect_result") or ""), effect)
	):
		frappe.throw("request_key is already bound to a different pending submission.")
	_validate_current_action_submission_checksum(action)
	return {
		"action_item": str(action.name),
		"status": str(action.status),
		"round_no": str(action.verification_round_no),
		"submission_checksum": str(action.current_submission_checksum),
	}


def _idempotent_action_decision(
	action,
	decision_key: str,
	decision: str,
	comment: str,
	actor: str,
) -> dict[str, str] | None:
	name = frappe.db.get_value(
		"IONE Quality Action Verification Round",
		{"verifier_decision_key": decision_key},
		"name",
	)
	if not name:
		return None
	round_doc = frappe.get_doc("IONE Quality Action Verification Round", name)
	if (
		str(round_doc.action_item or "") != str(action.name)
		or str(round_doc.decision or "") != decision
		or str(round_doc.verified_by or "") != actor
		or not hmac.compare_digest(str(round_doc.verification_comment or ""), comment)
	):
		frappe.throw("request_key is already bound to a different verifier decision.")
	_validated_action_verification_chain(action)
	return {
		"action_item": str(action.name),
		"status": str(round_doc.action_status_after),
		"verification_round": str(round_doc.name),
		"receipt_checksum": str(round_doc.receipt_checksum),
	}


def _validate_quality_action_chain_state(action, status: str) -> None:
	if action.is_new():
		for fieldname in (
			"current_submission_key",
			"current_submission_checksum",
			"latest_verification_round",
			"latest_verification_receipt_checksum",
		):
			if action.get(fieldname):
				frappe.throw("New quality actions cannot supply verification-chain fields.")
		if int(action.get("verification_round_no") or 0):
			frappe.throw("New quality actions must start before verification round one.")
		return
	rows = _validated_action_verification_chain(action)
	latest = rows[-1] if rows else None
	expected_round = len(rows) + (1 if status == "Pending Verification" else 0)
	if int(action.get("verification_round_no") or 0) != expected_round:
		frappe.throw("Quality action verification_round_no does not match its append-only chain.")
	if latest:
		if str(action.get("latest_verification_round") or "") != str(
			latest.get("name") or ""
		) or not hmac.compare_digest(
			str(action.get("latest_verification_receipt_checksum") or ""),
			str(latest.get("receipt_checksum") or ""),
		):
			frappe.throw("Quality action latest verification receipt pointer is inconsistent.")
	else:
		if action.get("latest_verification_round") or action.get("latest_verification_receipt_checksum"):
			frappe.throw("Quality action cannot point to a verification receipt before round one.")
	if status == "Pending Verification":
		if latest and str(latest.get("decision") or "") != "Rework":
			frappe.throw("Only a Rework receipt may be followed by another owner submission.")
		if latest and str(action.get("current_submission_key") or "") == str(
			latest.get("owner_submission_key") or ""
		):
			frappe.throw("A new verification round requires a new owner request_key.")
		_validate_current_action_submission_checksum(action)
	elif latest:
		if str(latest.get("decision") or "") == "Rework" and status not in {
			"In Progress",
			"Cancelled",
		}:
			frappe.throw("A Rework receipt must leave the action In Progress before any cancellation.")
		if str(latest.get("decision") or "") == "Close" and status != "Closed":
			frappe.throw("A Close receipt requires a Closed action.")
		if str(latest.get("decision") or "") == "Cancel" and status != "Cancelled":
			frappe.throw("A Cancel receipt requires a Cancelled action.")
		for action_field, round_field in (
			("completion_evidence", "completion_evidence_snapshot"),
			("effect_result", "effect_result_snapshot"),
			("current_submission_key", "owner_submission_key"),
			("current_submission_checksum", "owner_submission_checksum"),
			("submitted_for_verification_by", "submitted_by"),
		):
			if not hmac.compare_digest(
				str(action.get(action_field) or ""),
				str(latest.get(round_field) or ""),
			):
				frappe.throw("Quality action current submission cache does not match its latest receipt.")
		if not _same_datetime(
			action.get("submitted_for_verification_at"),
			latest.get("submitted_at"),
		):
			frappe.throw("Quality action submission time does not match its latest receipt.")
	elif any(
		action.get(fieldname)
		for fieldname in (
			"completion_evidence",
			"effect_result",
			"current_submission_key",
			"current_submission_checksum",
			"submitted_for_verification_by",
			"submitted_for_verification_at",
		)
	):
		frappe.throw("Quality action cannot retain a submission cache before round one.")
	if status == "Closed":
		if (
			not latest
			or str(latest.get("decision") or "") != "Close"
			or str(latest.get("action_status_after") or "") != "Closed"
			or str(action.get("closed_by") or "") != str(latest.get("verified_by") or "")
			or not _same_datetime(action.get("closed_at"), latest.get("verified_at"))
		):
			frappe.throw("Closed quality action does not match its final verification receipt.")


def _validate_current_action_submission_checksum(action) -> None:
	for fieldname in (
		"completion_evidence",
		"effect_result",
		"current_submission_key",
		"current_submission_checksum",
		"submitted_for_verification_by",
		"submitted_for_verification_at",
	):
		if not action.get(fieldname):
			frappe.throw("Pending verification is missing its accountable owner submission.")
	round_no = int(action.get("verification_round_no") or 0)
	evidence_hash = _checksum(
		{
			"completion_evidence": str(action.get("completion_evidence") or ""),
			"effect_result": str(action.get("effect_result") or ""),
		}
	)
	expected = _checksum(
		{
			"action_item": str(action.name),
			"round_no": round_no,
			"owner_submission_key": str(action.get("current_submission_key") or ""),
			"completion_evidence_hash": evidence_hash,
			"submitted_by": str(action.get("submitted_for_verification_by") or ""),
			"submitted_at": action.get("submitted_for_verification_at"),
		}
	)
	if not hmac.compare_digest(str(action.get("current_submission_checksum") or ""), expected):
		frappe.throw("Pending quality action owner submission checksum is invalid.")


def _validated_action_verification_chain(action) -> list[Any]:
	rows = list(
		frappe.get_all(
			"IONE Quality Action Verification Round",
			filters={"action_item": str(action.name)},
			fields=[
				"name",
				"round_key",
				"action_item",
				"round_no",
				*_SCOPE_FIELDS,
				"assigned_to_snapshot",
				"verifier_snapshot",
				"completion_evidence_snapshot",
				"effect_result_snapshot",
				"completion_evidence_hash",
				"owner_submission_key",
				"owner_submission_checksum",
				"submitted_by",
				"submitted_at",
				"decision",
				"verification_comment",
				"verifier_decision_key",
				"verified_by",
				"verified_at",
				"action_status_after",
				"previous_receipt_checksum",
				"receipt_checksum",
			],
			order_by="round_no asc, name asc",
			limit_page_length=MAX_ACTION_VERIFICATION_ROUNDS + 1,
		)
	)
	if len(rows) > MAX_ACTION_VERIFICATION_ROUNDS:
		frappe.throw("Quality action verification-round chain exceeds its governed bound.")
	previous_checksum = ""
	previous_verified_at = None
	for expected_round, row in enumerate(rows, start=1):
		if (
			str(row.get("action_item") or "") != str(action.name)
			or int(row.get("round_no") or 0) != expected_round
			or str(row.get("round_key") or "")
			!= _checksum({"action_item": str(action.name), "round_no": expected_round})
		):
			frappe.throw("Quality action verification chain sequence is invalid.")
		for fieldname in _SCOPE_FIELDS:
			if _normalized(row.get(fieldname)) != _normalized(action.get(fieldname)):
				frappe.throw("Quality action verification receipt scope is inconsistent.")
		if str(row.get("assigned_to_snapshot") or "") != str(action.get("assigned_to") or "") or str(
			row.get("verifier_snapshot") or ""
		) != str(action.get("verifier") or ""):
			frappe.throw("Quality action verification receipt actor snapshots are inconsistent.")
		decision = str(row.get("decision") or "")
		if decision not in ACTION_VERIFICATION_STATUS_AFTER or str(
			row.get("action_status_after") or ""
		) != ACTION_VERIFICATION_STATUS_AFTER.get(decision):
			frappe.throw("Quality action verification receipt decision state is invalid.")
		if expected_round < len(rows) and decision != "Rework":
			frappe.throw("A terminal quality action verification receipt cannot have a successor.")
		if (
			str(row.get("submitted_by") or "") != str(row.get("assigned_to_snapshot") or "")
			or str(row.get("verified_by") or "") != str(row.get("verifier_snapshot") or "")
			or str(row.get("verified_by") or "")
			in {
				str(row.get("submitted_by") or ""),
				str(row.get("assigned_to_snapshot") or ""),
				str(action.get("created_by") or ""),
			}
		):
			frappe.throw("Quality action verification receipt actor separation is invalid.")
		if str(row.get("owner_submission_key") or "") == str(row.get("verifier_decision_key") or ""):
			frappe.throw("Owner submission and verifier decision require different request keys.")
		_idempotency_key(row.get("owner_submission_key"))
		_idempotency_key(row.get("verifier_decision_key"))
		_bounded_text(
			row.get("completion_evidence_snapshot"),
			"completion_evidence_snapshot",
			20_000,
			minimum=10,
		)
		_bounded_text(
			row.get("effect_result_snapshot"),
			"effect_result_snapshot",
			20_000,
			minimum=10,
		)
		_bounded_text(
			row.get("verification_comment"),
			"verification_comment",
			10_000,
			minimum=5,
		)
		if not row.get("submitted_at") or not row.get("verified_at"):
			frappe.throw("Quality action verification receipt timestamps are required.")
		submitted_at = get_datetime(row.get("submitted_at"))
		verified_at = get_datetime(row.get("verified_at"))
		if verified_at < submitted_at:
			frappe.throw("Quality action verification decision cannot precede submission.")
		if previous_verified_at and submitted_at < previous_verified_at:
			frappe.throw("Quality action verification rounds are not chronologically ordered.")
		if not hmac.compare_digest(
			str(row.get("previous_receipt_checksum") or ""),
			previous_checksum,
		):
			frappe.throw("Quality action verification receipt chain is broken.")
		if not hmac.compare_digest(
			str(row.get("completion_evidence_hash") or ""),
			_action_evidence_hash(row),
		):
			frappe.throw("Quality action verification evidence hash is invalid.")
		if not hmac.compare_digest(
			str(row.get("owner_submission_checksum") or ""),
			_action_owner_submission_checksum(row),
		):
			frappe.throw("Quality action owner submission checksum is invalid.")
		if not hmac.compare_digest(
			str(row.get("receipt_checksum") or ""),
			_checksum(_action_verification_round_snapshot(row)),
		):
			frappe.throw("Quality action verification receipt checksum is invalid.")
		previous_checksum = str(row.get("receipt_checksum") or "")
		previous_verified_at = verified_at
	return rows


def _action_evidence_hash(round_doc) -> str:
	return _checksum(
		{
			"completion_evidence": str(round_doc.get("completion_evidence_snapshot") or ""),
			"effect_result": str(round_doc.get("effect_result_snapshot") or ""),
		}
	)


def _action_owner_submission_checksum(round_doc) -> str:
	return _checksum(
		{
			"action_item": str(round_doc.get("action_item") or ""),
			"round_no": int(round_doc.get("round_no") or 0),
			"owner_submission_key": str(round_doc.get("owner_submission_key") or ""),
			"completion_evidence_hash": str(round_doc.get("completion_evidence_hash") or ""),
			"submitted_by": str(round_doc.get("submitted_by") or ""),
			"submitted_at": round_doc.get("submitted_at"),
		}
	)


def _action_verification_round_snapshot(round_doc) -> dict[str, Any]:
	return {
		"round_key": str(round_doc.get("round_key") or ""),
		"action_item": str(round_doc.get("action_item") or ""),
		"round_no": int(round_doc.get("round_no") or 0),
		**_scope_values(round_doc),
		"assigned_to_snapshot": str(round_doc.get("assigned_to_snapshot") or ""),
		"verifier_snapshot": str(round_doc.get("verifier_snapshot") or ""),
		"completion_evidence_snapshot": str(round_doc.get("completion_evidence_snapshot") or ""),
		"effect_result_snapshot": str(round_doc.get("effect_result_snapshot") or ""),
		"completion_evidence_hash": str(round_doc.get("completion_evidence_hash") or ""),
		"owner_submission_key": str(round_doc.get("owner_submission_key") or ""),
		"owner_submission_checksum": str(round_doc.get("owner_submission_checksum") or ""),
		"submitted_by": str(round_doc.get("submitted_by") or ""),
		"submitted_at": round_doc.get("submitted_at"),
		"decision": str(round_doc.get("decision") or ""),
		"verification_comment": str(round_doc.get("verification_comment") or ""),
		"verifier_decision_key": str(round_doc.get("verifier_decision_key") or ""),
		"verified_by": str(round_doc.get("verified_by") or ""),
		"verified_at": round_doc.get("verified_at"),
		"action_status_after": str(round_doc.get("action_status_after") or ""),
		"previous_receipt_checksum": str(round_doc.get("previous_receipt_checksum") or ""),
	}


def _validate_action_links(doc) -> None:
	links, scope = _resolved_action_links(
		meeting=doc.get("meeting"),
		meeting_decision=doc.get("meeting_decision"),
		finding=doc.get("finding"),
		pdca_project=doc.get("pdca_project"),
		safety_event=doc.get("safety_event"),
	)
	for fieldname, expected in links.items():
		if str(doc.get(fieldname) or "") != str(expected or ""):
			frappe.throw(f"Quality action item {fieldname} linkage is inconsistent.")
	_inherit_and_validate_scope(doc, scope, "linked governance source")


def _resolved_action_links(
	*,
	meeting: str | None,
	meeting_decision: str | None,
	finding: str | None,
	pdca_project: str | None,
	safety_event: str | None,
) -> tuple[dict[str, str | None], Any]:
	values = {
		"meeting": str(meeting or "").strip() or None,
		"meeting_decision": str(meeting_decision or "").strip() or None,
		"finding": str(finding or "").strip() or None,
		"pdca_project": str(pdca_project or "").strip() or None,
		"safety_event": str(safety_event or "").strip() or None,
	}
	if not any(values.values()):
		frappe.throw(
			"Quality action item requires at least one meeting, decision, finding, PDCA, or safety-event link."
		)
	sources: list[Any] = []
	if values["meeting_decision"]:
		decision = frappe.get_doc("IONE Meeting Decision", values["meeting_decision"])
		values["meeting"] = str(decision.meeting)
		sources.append(decision)
	if values["meeting"]:
		meeting_doc = frappe.get_doc("IONE Quality Meeting", values["meeting"])
		if str(meeting_doc.status or "") not in {"Held", "Minutes Pending", "Closed"}:
			frappe.throw("Quality action items require a Held or completed meeting.")
		sources.append(meeting_doc)
	for fieldname, doctype in (
		("finding", "IONE QC Finding"),
		("pdca_project", "IONE PDCA Project"),
		("safety_event", "IONE Medical Safety Event"),
	):
		if values[fieldname]:
			sources.append(frappe.get_doc(doctype, values[fieldname]))
	sources.sort(
		key=lambda source: sum(1 for fieldname in _SCOPE_FIELDS if source.get(fieldname)),
		reverse=True,
	)
	scope = sources[0]
	for source in sources[1:]:
		_assert_compatible_scope(scope, source, "linked action source")
	return values, scope


def _validate_experience_source(doc) -> None:
	safety_event = str(doc.get("safety_event") or "").strip()
	minute_name = str(doc.get("meeting_minute") or "").strip()
	if bool(safety_event) == bool(minute_name):
		frappe.throw(
			"Experience share requires exactly one Confirmed/Closed safety event or Approved meeting minute."
		)
	if safety_event:
		source = frappe.get_doc("IONE Medical Safety Event", safety_event)
		if str(source.status or "") not in {"Confirmed", "Closed"}:
			frappe.throw("Safety-event experience sources must be Confirmed or Closed.")
		_inherit_and_validate_scope(doc, source, "medical safety event")
		return
	minute = frappe.get_doc("IONE Meeting Minute", minute_name)
	if str(minute.status or "") != "Approved" or not minute.get("checksum"):
		frappe.throw("Meeting-minute experience sources must be Approved and checksummed.")
	meeting = frappe.get_doc("IONE Quality Meeting", minute.meeting)
	_inherit_and_validate_scope(doc, meeting, "quality meeting")


def _experience_content(value: Any) -> dict[str, Any]:
	content = _json_value(value, "content_json")
	if not isinstance(content, Mapping) or set(content) != _EXPERIENCE_CONTENT_KEYS:
		frappe.throw(
			"content_json must contain exactly context_summary, issue_pattern, improvement_actions, "
			"verified_outcome, lessons, and applicability."
		)
	actions = content.get("improvement_actions")
	if not isinstance(actions, list) or not 1 <= len(actions) <= 50:
		frappe.throw("Experience improvement_actions must contain 1-50 text items.")
	result: dict[str, Any] = {
		"context_summary": _bounded_text(
			content.get("context_summary"),
			"context_summary",
			10_000,
			minimum=10,
		),
		"issue_pattern": _bounded_text(
			content.get("issue_pattern"),
			"issue_pattern",
			10_000,
			minimum=10,
		),
		"improvement_actions": [
			_bounded_text(item, "improvement action", 2_000, minimum=5) for item in actions
		],
		"verified_outcome": _bounded_text(
			content.get("verified_outcome"),
			"verified_outcome",
			10_000,
			minimum=10,
		),
		"lessons": _bounded_text(content.get("lessons"), "lessons", 10_000, minimum=10),
		"applicability": _bounded_text(
			content.get("applicability"),
			"applicability",
			10_000,
			minimum=10,
		),
	}
	serialized = _canonical_json(result)
	if len(serialized.encode()) > MAX_JSON_BYTES:
		frappe.throw("Experience content_json exceeds 128 KiB.")
	return result


def _validate_deidentification(doc, content: Mapping[str, Any]) -> None:
	if int(doc.get("deidentification_attested") or 0) != 1:
		frappe.throw("Experience sharing requires an explicit deidentification attestation.")
	_bounded_text(
		doc.get("deidentification_method"),
		"deidentification_method",
		2_000,
		minimum=10,
	)
	serialized = _canonical_json(content)
	lowered_keys = {str(key).strip().lower() for key in _all_mapping_keys(content)}
	if lowered_keys.intersection(_PROHIBITED_EXPERIENCE_KEYS):
		frappe.throw("Experience content contains a prohibited clinical identity or raw-source field.")
	if any(pattern.search(serialized) for pattern in _DIRECT_IDENTIFIER_PATTERNS):
		frappe.throw("Experience content appears to contain a direct identifier or external deep link.")
	for source_field in ("safety_event", "meeting_minute"):
		source_value = str(doc.get(source_field) or "").strip()
		if source_value and source_value in serialized:
			frappe.throw("Experience content cannot repeat its source record identity.")
	# No source narrative/content is loaded here. Publication always uses only the
	# author's separately supplied, structured, attested content.


def _validate_experience_version(doc) -> None:
	code = _bounded_text(doc.get("experience_code"), "experience_code", 100, minimum=1)
	version = int(doc.get("version_number") or 0)
	if version < 1:
		frappe.throw("Experience share version_number must be a positive integer.")
	expected_key = _checksum({"experience_code": code, "version_number": version})
	if doc.get("version_key") and not hmac.compare_digest(str(doc.version_key), expected_key):
		frappe.throw("Experience version_key does not match experience_code and version_number.")
	doc.version_key = expected_key
	supersedes = str(doc.get("supersedes") or "").strip()
	if version == 1:
		if supersedes:
			frappe.throw("Experience version 1 cannot supersede another record.")
		return
	if not supersedes:
		frappe.throw("Experience versions after 1 require the immediately preceding version.")
	previous = frappe.db.get_value(
		"IONE Quality Experience Share",
		supersedes,
		["experience_code", "version_number", "status"],
		as_dict=True,
	)
	if (
		not previous
		or str(previous.get("experience_code") or "") != code
		or int(previous.get("version_number") or 0) != version - 1
		or str(previous.get("status") or "") not in {"Published", "Retired"}
	):
		frappe.throw("Experience supersedes must reference version_number - 1 of the same code.")


def _validate_effective_period(doc) -> None:
	if not doc.get("effective_from"):
		frappe.throw("Experience share effective_from is required.")
	if doc.get("effective_to") and getdate(doc.effective_to) < getdate(doc.effective_from):
		frappe.throw("Experience effective_to cannot precede effective_from.")


def _validate_experience_status_gates(doc, status: str) -> None:
	if status in {"Submitted", "Published", "Rejected", "Retired"} and (
		not doc.get("submitted_by") or not doc.get("submitted_at")
	):
		frappe.throw("Submitted experience shares require system-maintained submission evidence.")
	if status in {"Published", "Rejected", "Retired"}:
		if not doc.get("published_by") or not doc.get("published_at"):
			frappe.throw("Reviewed experience shares require system-maintained review evidence.")
		_bounded_text(doc.get("review_comment"), "review_comment", 2_000, minimum=5)
	if status in {"Published", "Retired"}:
		expected = _checksum(_experience_snapshot(doc))
		if not hmac.compare_digest(str(doc.get("checksum") or ""), expected):
			frappe.throw("Published experience checksum does not match its frozen content.")
	elif doc.get("checksum"):
		frappe.throw("Only Published or Retired experience shares may contain a publication checksum.")
	if status == "Retired":
		_bounded_text(doc.get("retirement_reason"), "retirement_reason", 2_000, minimum=5)
		if not doc.get("retired_by") or not doc.get("retired_at"):
			frappe.throw("Retired experience shares require system-maintained retirement evidence.")


def _validate_no_other_published_version(doc) -> None:
	existing = frappe.db.get_value(
		"IONE Quality Experience Share",
		{
			"experience_code": doc.experience_code,
			"status": "Published",
			"name": ["!=", doc.name],
		},
		"name",
	)
	if existing:
		frappe.throw(
			"Another version of this experience is still Published; retire it before publishing a replacement."
		)


def _validate_published_experience_immutability(doc, previous) -> None:
	allowed = {
		"status",
		"active_publication_key",
		"retired_by",
		"retired_at",
		"retirement_reason",
		"modified",
		"modified_by",
	}
	changed = _changed_business_fields(doc, previous)
	if not changed.issubset(allowed):
		frappe.throw("Published experience content and provenance are immutable.")
	if not hmac.compare_digest(str(doc.get("checksum") or ""), str(previous.get("checksum") or "")):
		frappe.throw("Published experience checksum is immutable.")


def _apply_attendance(doc, attendance: list[dict[str, Any]], start, end) -> None:
	expected = {str(row.get("user") or ""): row for row in doc.get("attendees") or []}
	observed = {row["user"]: row for row in attendance}
	if set(expected) != set(observed):
		frappe.throw("Attendance must exactly cover every approved meeting attendee.")
	for user, child in expected.items():
		row = observed[user]
		child.attendance_status = row["attendance_status"]
		child.joined_at = row.get("joined_at")
		child.left_at = row.get("left_at")
		child.attendance_evidence = row["attendance_evidence"]
		child.absence_reason = row.get("absence_reason")
		if row["attendance_status"] == "Attended":
			if row["joined_at"] < start or row["left_at"] > end:
				frappe.throw("Attendee participation times must fall within the recorded meeting interval.")
	if observed.get(str(doc.chair or ""), {}).get("attendance_status") != "Attended":
		frappe.throw("A meeting cannot be recorded as Held unless its configured chair attended.")


def _attendance_rows(value: Any, start, end) -> list[dict[str, Any]]:
	rows = _json_value(value, "attendance")
	if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ATTENDEES:
		frappe.throw("attendance must contain 1-200 rows.")
	result: list[dict[str, Any]] = []
	users: set[str] = set()
	for row in rows:
		if not isinstance(row, Mapping):
			frappe.throw("Every attendance row must be an object.")
		if set(row) - {
			"user",
			"attendance_status",
			"joined_at",
			"left_at",
			"attendance_evidence",
			"absence_reason",
		}:
			frappe.throw("Attendance contains unsupported fields.")
		user = str(row.get("user") or "").strip()
		if not user or user in users:
			frappe.throw("Attendance users must be present and unique.")
		users.add(user)
		status = str(row.get("attendance_status") or "")
		evidence = _bounded_text(
			row.get("attendance_evidence"),
			"attendance_evidence",
			2_000,
			minimum=5,
		)
		item: dict[str, Any] = {
			"user": user,
			"attendance_status": status,
			"attendance_evidence": evidence,
		}
		if status == "Attended":
			joined = get_datetime(row.get("joined_at"))
			left = get_datetime(row.get("left_at"))
			if left <= joined or joined < start or left > end:
				frappe.throw("Attended rows require valid participation times within the meeting.")
			item.update({"joined_at": joined, "left_at": left, "absence_reason": ""})
		elif status == "Absent":
			item.update(
				{
					"joined_at": None,
					"left_at": None,
					"absence_reason": _bounded_text(
						row.get("absence_reason"),
						"absence_reason",
						2_000,
						minimum=3,
					),
				}
			)
		else:
			frappe.throw("Held meeting attendance must be explicitly Attended or Absent.")
		result.append(item)
	return result


def _meeting_agenda_checksum(doc) -> str:
	return _checksum(_meeting_agenda_snapshot(doc))


def _meeting_agenda_snapshot(doc) -> dict[str, Any]:
	return {
		"meeting_code": doc.get("meeting_code"),
		"title": doc.get("title"),
		"meeting_type": doc.get("meeting_type"),
		**_scope_values(doc),
		"scheduled_start": doc.get("scheduled_start"),
		"scheduled_end": doc.get("scheduled_end"),
		"chair": doc.get("chair"),
		"secretary": doc.get("secretary"),
		"meeting_approver": doc.get("meeting_approver"),
		"minute_approver": doc.get("minute_approver"),
		"agenda_items": [
			{
				"agenda_key": row.get("agenda_key"),
				"subject": row.get("subject"),
				"objective": row.get("objective"),
				"presenter": row.get("presenter"),
				"duration_minutes": int(row.get("duration_minutes") or 0),
				"material_type": row.get("material_type"),
				"ai_report_draft": row.get("ai_report_draft"),
				"material_reference": row.get("material_reference"),
				"material_checksum": row.get("material_checksum"),
			}
			for row in doc.get("agenda_items") or []
		],
		"attendees": [
			{
				"user": row.get("user"),
				"participation_role": row.get("participation_role"),
				"required_attendee": int(row.get("required_attendee") or 0),
			}
			for row in doc.get("attendees") or []
		],
	}


def _validate_frozen_agenda(doc, expected_checksum: str) -> None:
	if not expected_checksum:
		frappe.throw("Approved meeting is missing its frozen agenda checksum.")
	current = _meeting_agenda_checksum(doc)
	if not hmac.compare_digest(current, expected_checksum):
		frappe.throw("Approved meeting agenda, attendees, scope, or material references changed.")


def _minute_snapshot(doc) -> dict[str, Any]:
	return {
		"meeting": doc.get("meeting"),
		"approved_meeting_key": doc.get("approved_meeting_key"),
		**_scope_values(doc),
		"summary": doc.get("summary"),
		"discussion_summary": doc.get("discussion_summary"),
		"decisions_json": _json_value(doc.get("decisions_json"), "decisions_json"),
		"no_decision_reason": doc.get("no_decision_reason"),
		"evidence_reference": doc.get("evidence_reference"),
		"submitted_by": doc.get("submitted_by"),
		"submitted_at": doc.get("submitted_at"),
		"reviewed_by": doc.get("reviewed_by"),
		"reviewed_at": doc.get("reviewed_at"),
	}


def _decision_snapshot(doc) -> dict[str, Any]:
	return {
		"decision_key": doc.get("decision_key"),
		"meeting": doc.get("meeting"),
		"meeting_minute": doc.get("meeting_minute"),
		"decision_index": int(doc.get("decision_index") or 0),
		"decision_code": doc.get("decision_code"),
		"decision_text": doc.get("decision_text"),
		"rationale": doc.get("rationale"),
		**_scope_values(doc),
		"derived_by": doc.get("derived_by"),
		"derived_at": doc.get("derived_at"),
	}


def _experience_snapshot(doc) -> dict[str, Any]:
	return {
		"experience_code": doc.get("experience_code"),
		"version_number": int(doc.get("version_number") or 0),
		"version_key": doc.get("version_key"),
		"supersedes": doc.get("supersedes"),
		"safety_event": doc.get("safety_event"),
		"meeting_minute": doc.get("meeting_minute"),
		**_scope_values(doc),
		"title": doc.get("title"),
		"content_json": _json_value(doc.get("content_json"), "content_json"),
		"deidentification_method": doc.get("deidentification_method"),
		"deidentification_attested": int(doc.get("deidentification_attested") or 0),
		"effective_from": doc.get("effective_from"),
		"effective_to": doc.get("effective_to"),
		"submitted_by": doc.get("submitted_by"),
		"submitted_at": doc.get("submitted_at"),
		"publisher": doc.get("publisher"),
		"published_by": doc.get("published_by"),
		"published_at": doc.get("published_at"),
		"review_comment": doc.get("review_comment"),
	}


def _validate_configured_user(
	user: Any,
	label: str,
	required_roles: frozenset[str],
	scope_doc,
) -> None:
	user_name = _validate_active_user(user, label)
	roles = frozenset(frappe.get_roles(user_name))
	if "IONE Agent Service" in roles or not roles.intersection(required_roles):
		frappe.throw(f"{label} must be an enabled named user with an explicitly allowed governance role.")
	require_scope_read(
		**_scope_values(scope_doc),
		user=user_name,
		target_doctype=str(scope_doc.get("doctype") or getattr(scope_doc, "doctype", "") or ""),
		include_personal=True,
	)


def _validate_active_user(user: Any, label: str) -> str:
	name = str(user or "").strip()
	if name in {"", "Guest", "Administrator"}:
		frappe.throw(f"{label} must be a named accountable user.")
	if not int(frappe.db.get_value("User", name, "enabled") or 0):
		frappe.throw(f"{label} user is disabled or does not exist.")
	return name


def _require_named_actor(
	roles: frozenset[str],
	*,
	scope_doc=None,
) -> str:
	user = _validate_active_user(frappe.session.user, "workflow actor")
	actual_roles = frozenset(frappe.get_roles(user))
	if "IONE Agent Service" in actual_roles or not actual_roles.intersection(roles):
		frappe.throw("Current user is not an authorized quality-governance actor.", frappe.PermissionError)
	if scope_doc is not None:
		_require_scope(scope_doc, user)
	return user


def _require_scope(doc, user: str) -> None:
	require_scope_read(
		**_scope_values(doc),
		user=user,
		target_doctype=str(doc.get("doctype") or getattr(doc, "doctype", "") or ""),
		include_personal=True,
	)


def _scope_values(doc) -> dict[str, Any]:
	return {fieldname: doc.get(fieldname) for fieldname in _SCOPE_FIELDS}


def _validate_scope_shape(doc) -> None:
	values = _scope_values(doc)
	if not values["hospital"]:
		frappe.throw("Governed quality records require an explicit hospital scope.")
	if values["ward"] and not values["department"]:
		frappe.throw("Ward-scoped quality records require an explicit department.")
	if values["department"] and not values["campus"]:
		frappe.throw("Department-scoped quality records require an explicit campus.")
	if values["campus"] and not values["hospital"]:
		frappe.throw("Campus-scoped quality records require an explicit hospital.")


def _inherit_and_validate_scope(doc, source, source_label: str) -> None:
	for fieldname in _SCOPE_FIELDS:
		expected = source.get(fieldname)
		actual = doc.get(fieldname)
		if not actual and expected:
			doc.set(fieldname, expected)
		elif _normalized(actual) != _normalized(expected):
			frappe.throw(f"{fieldname} must match the linked {source_label}.")
	_validate_scope_shape(doc)


def _assert_compatible_scope(left, right, label: str) -> None:
	for fieldname in _SCOPE_FIELDS:
		left_value = _normalized(left.get(fieldname))
		right_value = _normalized(right.get(fieldname))
		if left_value and right_value and left_value != right_value:
			frappe.throw(f"{label} {fieldname} is outside the meeting/action scope.")


def _status(doc, values: frozenset[str], label: str) -> str:
	status = str(doc.get("status") or "")
	if status not in values:
		frappe.throw(f"Unsupported {label} status.")
	return status


def _validate_transition(
	previous: Any,
	current: Any,
	transitions: Mapping[str, frozenset[str]],
	label: str,
) -> None:
	before = str(previous or "")
	after = str(current or "")
	if before == after:
		return
	if after not in transitions.get(before, frozenset()):
		frappe.throw(f"Invalid {label} transition: {before} -> {after}.")


def _save_governed(doc, domain: str) -> None:
	with _capability(domain, str(doc.name)):
		doc.save(ignore_permissions=True)


@contextmanager
def _capability(domain: str, name: str):
	key = "ione_quality_meeting_capability"
	previous = getattr(frappe.flags, key, None)
	setattr(frappe.flags, key, {"domain": domain, "name": name})
	try:
		yield
	finally:
		if previous is None:
			try:
				delattr(frappe.flags, key)
			except AttributeError:
				pass
		else:
			setattr(frappe.flags, key, previous)


def _has_capability(domain: str, name: str) -> bool:
	value = getattr(frappe.flags, "ione_quality_meeting_capability", None)
	return bool(
		isinstance(value, Mapping)
		and hmac.compare_digest(str(value.get("domain") or ""), domain)
		and hmac.compare_digest(str(value.get("name") or ""), str(name or ""))
	)


def _require_post() -> None:
	request = getattr(frappe.local, "request", None)
	if request is not None and str(getattr(request, "method", "") or "").upper() != "POST":
		frappe.throw("Governed quality meeting mutations require HTTP POST.", frappe.PermissionError)


def _reject_audit_values(doc, fields: frozenset[str]) -> None:
	if any(doc.get(fieldname) not in (None, "") for fieldname in fields):
		frappe.throw("Workflow audit fields are maintained only by the governed service.")


def _reject_changed_fields(doc, previous, fields: frozenset[str]) -> None:
	if any(_normalized(doc.get(name)) != _normalized(previous.get(name)) for name in fields):
		frappe.throw("Workflow audit fields are maintained only by the governed service.")


def _changed_business_fields(doc, previous) -> set[str]:
	excluded = {
		"name",
		"creation",
		"modified",
		"modified_by",
		"idx",
		"docstatus",
		"_comments",
		"_assign",
		"_liked_by",
		"_user_tags",
	}
	return {
		field.fieldname
		for field in doc.meta.fields
		if field.fieldname not in excluded
		and not field.fieldtype.endswith("Break")
		and _normalized(doc.get(field.fieldname)) != _normalized(previous.get(field.fieldname))
	}


def _bounded_text(
	value: Any,
	label: str,
	maximum: int,
	*,
	minimum: int = 0,
) -> str:
	text = str(value or "").strip()
	if not minimum <= len(text) <= maximum:
		frappe.throw(f"{label} must contain {minimum}-{maximum} characters.")
	return text


def _idempotency_key(value: Any) -> str:
	key = _bounded_text(value, "request_key", 100, minimum=16)
	if not re.fullmatch(r"[A-Za-z0-9._:-]+", key):
		frappe.throw("request_key must be an opaque 16-100 character identifier.")
	return key


def _same_datetime(left: Any, right: Any) -> bool:
	if not left or not right:
		return not left and not right
	return get_datetime(left) == get_datetime(right)


def _json_value(value: Any, label: str) -> Any:
	if isinstance(value, str):
		if len(value.encode()) > MAX_JSON_BYTES:
			frappe.throw(f"{label} exceeds 128 KiB.")
		try:
			return json.loads(value)
		except (TypeError, ValueError) as exc:
			frappe.throw(f"{label} must contain valid JSON.")
			raise AssertionError from exc
	return value


def _canonical_json(value: Any) -> str:
	return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _checksum(value: Any) -> str:
	return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _all_mapping_keys(value: Any) -> list[str]:
	keys: list[str] = []
	if isinstance(value, Mapping):
		for key, child in value.items():
			keys.append(str(key))
			keys.extend(_all_mapping_keys(child))
	elif isinstance(value, list):
		for child in value:
			keys.extend(_all_mapping_keys(child))
	return keys


def _normalized(value: Any) -> Any:
	return None if value in (None, "") else value
