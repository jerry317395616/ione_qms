from __future__ import annotations

import frappe
from frappe.utils import now_datetime

_TRANSITIONS = {
	"Reported": {"Under Review", "Closed"},
	"Under Review": {"Confirmed", "Closed"},
	"Confirmed": {"Improving", "Closed"},
	"Improving": {"Closed"},
	"Closed": set(),
}
_REVIEW_ROLES = frozenset(
	{
		"IONE Department Director",
		"IONE Department QC Officer",
		"IONE QC Reviewer",
		"IONE Medical Affairs",
	}
)
_CLOSE_ROLES = frozenset({"IONE QC Reviewer", "IONE Medical Affairs"})
_SEVERITIES = frozenset({"Low", "Medium", "High", "Critical"})
_IMMUTABLE_PROVENANCE_FIELDS = (
	"clinical_event",
	"hospital",
	"campus",
	"department",
	"ward",
	"patient",
	"encounter",
	"responsible_staff",
	"event_time",
	"event_type",
	"anonymous_report",
	"near_miss",
	"report_channel",
	"narrative",
	"immediate_action",
	"reported_by",
	"reported_at",
	"owner",
)
_REVIEW_FIELDS = (
	"severity",
	"finding",
	"investigation_summary",
	"root_cause_analysis",
	"improvement_action",
	"verification_outcome",
	"lessons_learned",
	"closed_by",
	"closed_at",
)


def validate_medical_safety_event(doc, method: str | None = None) -> None:
	del method
	_validate_content(doc)
	_validate_reporter_contract(doc)
	if doc.is_new():
		if not bool(getattr(doc.flags, "ione_projection_materializer", False)):
			frappe.throw(
				"Medical safety events may only be created through the governed report or integration path",
				frappe.PermissionError,
			)
		if str(doc.get("status") or "Reported") != "Reported":
			frappe.throw("New medical safety events must enter the workflow in Reported status")
		if doc.get("closed_by") or doc.get("closed_at"):
			frappe.throw("New medical safety events cannot contain closure audit fields")
		return
	before = doc.get_doc_before_save()
	if not before:
		return
	for fieldname in _IMMUTABLE_PROVENANCE_FIELDS:
		if _normalized(before.get(fieldname)) != _normalized(doc.get(fieldname)):
			frappe.throw(f"Medical safety event provenance field {fieldname} is immutable")
	previous_status = str(before.get("status") or "Reported")
	current_status = str(doc.get("status") or "Reported")
	if previous_status not in _TRANSITIONS or current_status not in _TRANSITIONS:
		frappe.throw("Medical safety event status is not an approved workflow state")
	if previous_status == current_status:
		_validate_status_gates(doc, current_status)
		if current_status == "Closed":
			for fieldname in _REVIEW_FIELDS:
				if _normalized(before.get(fieldname)) != _normalized(doc.get(fieldname)):
					frappe.throw("Closed medical safety events are immutable")
		elif _normalized(before.get("closed_by")) != _normalized(doc.get("closed_by")) or _normalized(
			before.get("closed_at")
		) != _normalized(doc.get("closed_at")):
			frappe.throw("Closure audit fields are maintained by the system")
		return
	if current_status not in _TRANSITIONS.get(previous_status, set()):
		frappe.throw(f"Invalid medical safety event transition: {previous_status} -> {current_status}")
	if frappe.session.user == "Administrator":
		frappe.throw(
			"Medical safety event workflow transitions require a named accountable reviewer.",
			frappe.PermissionError,
		)
	if frappe.session.user != "Administrator":
		roles = frozenset(frappe.get_roles())
		required = _CLOSE_ROLES if current_status == "Closed" else _REVIEW_ROLES
		if not roles.intersection(required):
			frappe.throw(
				"Current user cannot perform this medical safety event transition", frappe.PermissionError
			)
	if current_status == "Closed":
		if doc.get("closed_by") not in (None, "", frappe.session.user):
			frappe.throw("closed_by is maintained by the system")
		doc.closed_by = frappe.session.user
		doc.closed_at = now_datetime()
	elif _normalized(before.get("closed_by")) != _normalized(doc.get("closed_by")) or _normalized(
		before.get("closed_at")
	) != _normalized(doc.get("closed_at")):
		frappe.throw("Closure audit fields are maintained by the system")
	_validate_status_gates(doc, current_status)


def _validate_reporter_contract(doc) -> None:
	anonymous_report = int(doc.get("anonymous_report") or 0)
	report_channel = str(doc.get("report_channel") or "")
	if anonymous_report:
		if doc.get("reported_by"):
			frappe.throw("Anonymous medical safety events cannot store a reporter identity")
		if doc.get("reported_at"):
			frappe.throw("Anonymous medical safety events cannot store an exact report timestamp")
		if report_channel != "Anonymous Portal":
			frappe.throw("Anonymous medical safety events require the Anonymous Portal channel")
		if doc.is_new() and doc.get("owner") != "Administrator":
			frappe.throw("Anonymous medical safety events require the technical audit identity")
	elif report_channel == "Anonymous Portal":
		frappe.throw("Anonymous Portal reports must be marked anonymous")
	elif report_channel == "Staff Portal":
		if not doc.get("reported_by") or not doc.get("reported_at"):
			frappe.throw("Identified staff portal reports require reported_by and reported_at")
		if doc.is_new() and doc.get("owner") != "Administrator":
			frappe.throw("Staff portal reports require the technical audit identity")
	elif report_channel in {"Clinical Integration", "Imported"}:
		if doc.get("reported_by"):
			frappe.throw("Integration and imported reports cannot assert an internal reporter identity")
	elif report_channel:
		frappe.throw("Medical safety event report channel is not approved")
	else:
		frappe.throw("Medical safety event report channel is required")


def _validate_content(doc) -> None:
	event_type = str(doc.get("event_type") or "").strip()
	narrative = str(doc.get("narrative") or "").strip()
	immediate_action = str(doc.get("immediate_action") or "").strip()
	if not 1 <= len(event_type) <= 200:
		frappe.throw("Medical safety event type must contain 1-200 characters")
	if doc.get("severity") not in _SEVERITIES:
		frappe.throw("Medical safety event severity is required and must be approved")
	if not 10 <= len(narrative) <= 20_000:
		frappe.throw("Medical safety event narrative must contain 10-20,000 characters")
	if len(immediate_action) > 20_000:
		frappe.throw("Medical safety event immediate action exceeds 20,000 characters")
	for fieldname in (
		"investigation_summary",
		"root_cause_analysis",
		"improvement_action",
		"verification_outcome",
		"lessons_learned",
	):
		if len(str(doc.get(fieldname) or "")) > 50_000:
			frappe.throw(f"Medical safety event {fieldname} exceeds 50,000 characters")


def _validate_status_gates(doc, status: str) -> None:
	if status in {"Confirmed", "Improving", "Closed"}:
		_require_review_text(
			doc,
			"investigation_summary",
			"Confirmed medical safety events require an investigation summary",
		)
	if status in {"Improving", "Closed"}:
		_require_review_text(
			doc,
			"root_cause_analysis",
			"Improving medical safety events require a root-cause analysis",
		)
		_require_review_text(
			doc,
			"improvement_action",
			"Improving medical safety events require a documented improvement action",
		)
	if status == "Closed":
		_require_review_text(
			doc,
			"verification_outcome",
			"Closing a medical safety event requires a verification outcome",
		)
		_require_review_text(
			doc,
			"lessons_learned",
			"Closing a medical safety event requires documented lessons learned",
		)


def _require_review_text(doc, fieldname: str, message: str) -> None:
	if len(str(doc.get(fieldname) or "").strip()) < 10:
		frappe.throw(message)


def _normalized(value):
	return None if value in (None, "") else value
