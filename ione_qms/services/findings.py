from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe.utils import add_days, now_datetime, nowdate

from ione_qms.constants import FINDING_TERMINAL_STATES
from ione_qms.services.runtime_settings import default_finding_due_days

_FINDING_TRANSITIONS: dict[str, frozenset[str]] = {
	"Candidate": frozenset({"Pending QC Review"}),
	"Pending QC Review": frozenset({"Confirmed"}),
	"Confirmed": frozenset({"Pending Rectification", "Appealed"}),
	"Appealed": frozenset({"Confirmed", "Appeal Approved", "Appeal Rejected"}),
	"Appeal Rejected": frozenset({"Pending Rectification"}),
	"Pending Rectification": frozenset({"Rectifying"}),
	"Rectifying": frozenset({"Pending Department Approval"}),
	"Pending Department Approval": frozenset({"Pending Functional Review", "Rework"}),
	"Pending Functional Review": frozenset({"Closed", "Rework"}),
	"Rework": frozenset({"Rectifying"}),
	"Closed": frozenset(),
	"Appeal Approved": frozenset(),
}

_TRANSITION_ROLES: dict[tuple[str, str], frozenset[str]] = {
	("Candidate", "Pending QC Review"): frozenset(
		{"IONE Agent Reviewer", "IONE QC Reviewer", "IONE Medical Affairs"}
	),
	("Pending QC Review", "Confirmed"): frozenset({"IONE QC Reviewer", "IONE Medical Affairs"}),
	("Confirmed", "Pending Rectification"): frozenset({"IONE QC Reviewer", "IONE Medical Affairs"}),
	("Confirmed", "Appealed"): frozenset(
		{"IONE Physician", "IONE Department Director", "IONE Department QC Officer"}
	),
	("Appealed", "Appeal Approved"): frozenset({"IONE QC Reviewer", "IONE Medical Affairs"}),
	("Appealed", "Appeal Rejected"): frozenset({"IONE QC Reviewer", "IONE Medical Affairs"}),
	("Appealed", "Confirmed"): frozenset(
		{"IONE Physician", "IONE Department Director", "IONE Department QC Officer"}
	),
	("Appeal Rejected", "Pending Rectification"): frozenset({"IONE QC Reviewer", "IONE Medical Affairs"}),
	("Confirmed", "Rectifying"): frozenset(
		{
			"IONE Physician",
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
		}
	),
	("Pending Rectification", "Rectifying"): frozenset(
		{
			"IONE Physician",
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
		}
	),
	("Rectifying", "Pending Department Approval"): frozenset(
		{"IONE Physician", "IONE Department QC Officer"}
	),
	("Pending Department Approval", "Pending Functional Review"): frozenset(
		{"IONE Department Director", "IONE Department QC Officer"}
	),
	("Pending Department Approval", "Rework"): frozenset(
		{"IONE Department Director", "IONE Department QC Officer"}
	),
	("Pending Functional Review", "Closed"): frozenset({"IONE QC Reviewer", "IONE Medical Affairs"}),
	("Pending Functional Review", "Rework"): frozenset({"IONE QC Reviewer", "IONE Medical Affairs"}),
	("Rework", "Rectifying"): frozenset(
		{
			"IONE Physician",
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
		}
	),
}

_SELF_APPROVAL_EDGES = frozenset(
	{
		("Candidate", "Pending QC Review"),
		("Confirmed", "Appealed"),
		("Appealed", "Confirmed"),
		("Confirmed", "Rectifying"),
		("Pending Rectification", "Rectifying"),
		("Rectifying", "Pending Department Approval"),
		("Rework", "Rectifying"),
	}
)


def _build_state_edit_roles() -> dict[str, frozenset[str]]:
	state_roles: dict[str, set[str]] = {}
	for (state, _next_state), roles in _TRANSITION_ROLES.items():
		state_roles.setdefault(state, set()).update(roles)
	return {state: frozenset(roles) for state, roles in state_roles.items()}


_FINDING_STATE_EDIT_ROLES = _build_state_edit_roles()

_DEPARTMENT_TRANSITION_ONLY_ROLES = frozenset(
	{
		"IONE Physician",
		"IONE Department Director",
		"IONE Department QC Officer",
	}
)

_GLOBAL_FINDING_EDIT_ROLES = frozenset({"IONE QC Reviewer", "IONE Medical Affairs"})

_CONFIRMED_IMMUTABLE_FIELDS = frozenset(
	{
		"deduplication_key",
		"title",
		"description",
		"severity",
		"event",
		"execution",
		"rule",
		"rule_version",
		"standard",
		"standard_clause",
		"hospital",
		"campus",
		"department",
		"ward",
		"patient",
		"encounter",
		"responsible_staff",
		"detected_at",
		"ai_origin",
		"ai_task",
		"ai_candidate",
		"evidence_hash",
	}
)

_SYSTEM_MANAGED_FIELDS = frozenset(
	{
		"name",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"docstatus",
		"idx",
		"parent",
		"parentfield",
		"parenttype",
		"_user_tags",
		"_comments",
		"_assign",
		"_liked_by",
	}
)

_RECTIFICATION_FINDING_STATUS = {
	"Submitted": "Rectifying",
	"In Progress": "Rectifying",
	"Pending Department Approval": "Pending Department Approval",
	"Pending Functional Review": "Pending Functional Review",
	"Rework": "Rework",
	"Closed": "Closed",
}

_EVIDENCE_CONTENT_FIELDS = frozenset(
	{
		"finding",
		"source_system",
		"source_record_type",
		"source_record_id",
		"source_version",
		"field_path",
		"evidence_text",
		"evidence_json",
		"context_summary",
		"captured_at",
		"rule_version",
		"standard_clause",
		"evidence_hash",
	}
)


def validate_finding(doc, method: str | None = None) -> None:
	"""Fail closed before persistence when a finding bypasses the workflow UI."""
	del method
	_validate_finding(doc)


def on_finding_update(doc, method: str | None = None) -> None:
	"""Recheck the finding contract and append an immutable transition snapshot."""
	del method
	transition = _validate_finding(doc)
	if transition:
		_record_transition_snapshot(doc, *transition)


def _validate_finding(doc) -> tuple[str, str] | None:
	previous = doc.get_doc_before_save()
	current_status = str(doc.get("status") or "").strip()
	if previous is None:
		if current_status not in FINDING_TERMINAL_STATES and not doc.get("due_date"):
			doc.due_date = add_days(nowdate(), default_finding_due_days())
		if int(doc.get("ai_origin") or 0) and not _is_human_accepted_ai_candidate(doc):
			frappe.throw("AI-origin findings may only be created from a pending, human-reviewed candidate.")
		if current_status == "Pending QC Review" and _is_human_accepted_ai_candidate(doc):
			return ("AI Candidate Accepted", current_status)
		if current_status and current_status != "Candidate":
			frappe.throw(
				"New quality findings must start in Candidate status. A human-accepted AI "
				"candidate may start in Pending QC Review."
			)
		return None
	if current_status not in FINDING_TERMINAL_STATES and not doc.get("due_date"):
		frappe.throw("Every open quality finding requires an accountable due_date.")

	previous_status = str(previous.get("status") or "").strip()
	if current_status == previous_status:
		_require_same_state_editor(doc, previous, current_status)
		_validate_department_transition_only(doc, previous)
		_validate_confirmed_fields(doc, previous, previous_status)
		return None

	if (
		previous_status == "Candidate"
		and current_status == "Pending QC Review"
		and int(doc.get("ai_origin") or 0)
		and not _is_human_accepted_ai_candidate(doc)
	):
		frappe.throw("AI-origin findings require their unchanged pending candidate provenance.")
	_validate_transition(doc, previous_status, current_status)
	if (previous_status, current_status) == ("Pending QC Review", "Confirmed"):
		_require_substantive_evidence(doc)
	internal_sync = _is_controlled_rectification_sync(doc, current_status)
	if not internal_sync:
		_reject_agent_decision(previous_status, current_status)
		_require_transition_role(previous_status, current_status)
		_reject_self_approval(doc, previous_status, current_status)
	_validate_department_transition_only(doc, previous)
	_validate_confirmed_fields(doc, previous, previous_status)
	if (previous_status, current_status) == ("Pending Functional Review", "Closed"):
		_require_verified_closed_rectification(doc.name)
	return previous_status, current_status


def validate_evidence_immutable(doc, method: str | None = None) -> None:
	"""Validate evidence content before save.

	DocType controllers can call this function from ``validate``. The hash is based on
	the normalized content, so later attempts to overwrite a snapshot are rejected.
	"""
	del method
	content_hash = _evidence_content_hash(doc)
	previous = doc.get_doc_before_save()
	if previous is None:
		existing_hash = str(doc.get("evidence_hash") or "")
		if existing_hash and existing_hash != content_hash:
			frappe.throw("Evidence hash does not match the submitted evidence content.")
		if _has_field(doc.doctype, "evidence_hash"):
			doc.set("evidence_hash", content_hash)
		return

	changed = [
		fieldname
		for fieldname in _EVIDENCE_CONTENT_FIELDS
		if _has_field(doc.doctype, fieldname) and doc.get(fieldname) != previous.get(fieldname)
	]
	if changed:
		frappe.throw(
			"Quality evidence is immutable. Append a new evidence or review record instead of "
			f"changing: {', '.join(sorted(changed))}."
		)


def prevent_evidence_deletion(doc, method: str | None = None) -> None:
	"""Block deletion of clinical evidence, including by background service users."""
	del doc, method
	frappe.throw("Clinical quality evidence cannot be deleted.", frappe.PermissionError)


def allowed_next_statuses(status: str) -> tuple[str, ...]:
	"""Return deterministic workflow choices for clients and tests."""
	return tuple(sorted(_FINDING_TRANSITIONS.get(status, ())))


def _validate_transition(doc, previous_status: str, current_status: str) -> None:
	if previous_status in FINDING_TERMINAL_STATES:
		frappe.throw(f"Finding status {previous_status} is terminal and cannot be changed.")
	if (
		previous_status == "Confirmed"
		and current_status == "Rectifying"
		and _has_active_rectification(doc.name)
	):
		return
	allowed = _FINDING_TRANSITIONS.get(previous_status)
	if allowed is None or current_status not in allowed:
		choices = ", ".join(sorted(allowed or ())) or "none"
		frappe.throw(
			f"Invalid quality finding transition: {previous_status} → {current_status}. "
			f"Allowed next states: {choices}."
		)


def _require_substantive_evidence(doc) -> None:
	evidence_types = ["Rule Evidence", "Source Snapshot", "Review Note", "Other"]
	if int(doc.get("ai_origin") or 0):
		evidence_types = ["Source Snapshot"]
	if frappe.db.exists(
		"IONE QC Finding Evidence",
		{
			"finding": doc.name,
			"evidence_type": ["in", evidence_types],
		},
	):
		return
	frappe.throw(
		"Finding confirmation requires immutable source evidence; workflow-transition audit "
		"records alone are not clinical evidence."
	)


def _is_human_accepted_ai_candidate(doc) -> bool:
	if not int(doc.get("ai_origin") or 0) or not doc.get("ai_candidate"):
		return False
	roles = set(frappe.get_roles(frappe.session.user))
	if not roles.intersection({"IONE Agent Reviewer", "IONE QC Reviewer", "IONE Medical Affairs"}):
		return False
	candidate_name = str(doc.get("ai_candidate") or "")
	if not frappe.db.exists("IONE AI Candidate Finding", candidate_name):
		return False
	candidate = frappe.get_doc("IONE AI Candidate Finding", candidate_name)
	if candidate.get("status") != "Pending Review" or candidate.get("formal_finding"):
		return False
	expected_key = hashlib.sha256(f"ai-candidate|{candidate.name}".encode()).hexdigest()
	if str(doc.get("deduplication_key") or "") != expected_key:
		return False
	expected_values = {
		"ai_task": candidate.get("task"),
		"hospital": candidate.get("hospital"),
		"campus": candidate.get("campus"),
		"department": candidate.get("department"),
		"ward": candidate.get("ward"),
		"patient": candidate.get("patient"),
		"encounter": candidate.get("encounter"),
		"title": candidate.get("title"),
		"description": candidate.get("rationale"),
		"severity": candidate.get("severity"),
	}
	return all(
		_same_optional_value(doc.get(fieldname), value) for fieldname, value in expected_values.items()
	)


def _has_active_rectification(finding_name: str) -> bool:
	doctype = "IONE QC Rectification"
	if not frappe.db.exists("DocType", doctype):
		return False
	filters: dict[str, Any] = {"finding": finding_name}
	if _has_field(doctype, "status"):
		filters["status"] = ["not in", ["Closed", "Cancelled", "Rejected"]]
	return bool(frappe.db.exists(doctype, filters))


def _reject_agent_decision(previous_status: str, current_status: str) -> None:
	if frappe.session.user == "Administrator":
		frappe.throw(
			"Administrator cannot perform governed clinical finding decisions; use a named "
			"accountable business user.",
			frappe.PermissionError,
		)
	roles = set(frappe.get_roles(frappe.session.user))
	if "IONE Agent Service" not in roles:
		return
	frappe.throw(
		"AI service users may only create candidate findings; they cannot review, approve, "
		f"rectify, or close them ({previous_status} → {current_status}).",
		frappe.PermissionError,
	)


def _require_transition_role(previous_status: str, current_status: str) -> None:
	required = _TRANSITION_ROLES.get((previous_status, current_status))
	if not required:
		frappe.throw(f"No reviewed business-role policy exists for {previous_status} → {current_status}.")
	if frappe.session.user == "Administrator":
		frappe.throw(
			"Administrator cannot perform governed clinical finding transitions; use a named "
			"accountable business user.",
			frappe.PermissionError,
		)
	roles = set(frappe.get_roles(frappe.session.user))
	if roles.intersection(required):
		return
	frappe.throw(
		f"Current user lacks the clinical business role required for {previous_status} → {current_status}.",
		frappe.PermissionError,
	)


def _reject_self_approval(doc, previous_status: str, current_status: str) -> None:
	transition = (previous_status, current_status)
	user = frappe.session.user
	if user == "Administrator":
		frappe.throw(
			"Administrator cannot perform governed clinical finding transitions; use a named "
			"accountable business user.",
			frappe.PermissionError,
		)
	if user == doc.get("owner") and transition not in _SELF_APPROVAL_EDGES:
		frappe.throw("Self approval is not allowed", frappe.PermissionError)


def _validate_department_transition_only(doc, previous) -> None:
	roles = set(frappe.get_roles(frappe.session.user))
	if not roles.intersection(_DEPARTMENT_TRANSITION_ONLY_ROLES) or roles.intersection(
		_GLOBAL_FINDING_EDIT_ROLES
	):
		return
	changed = _changed_business_fields(doc, previous)
	if changed.difference({"status"}):
		frappe.throw(
			"Department workflow actors may only change a finding through an approved "
			"status transition. Changed fields: " + ", ".join(sorted(changed.difference({"status"}))),
			frappe.PermissionError,
		)


def _require_same_state_editor(doc, previous, current_status: str) -> None:
	changed = _changed_business_fields(doc, previous)
	if not changed:
		return
	user = frappe.session.user
	if user == "Administrator":
		frappe.throw(
			"Administrator cannot edit governed clinical finding content; use a named accountable "
			"business user.",
			frappe.PermissionError,
		)
	required = _FINDING_STATE_EDIT_ROLES.get(current_status, frozenset())
	roles = set(frappe.get_roles(user))
	if "IONE Agent Service" in roles or not roles.intersection(required):
		frappe.throw(
			"Current user cannot edit content in this finding workflow state. Changed: "
			+ ", ".join(sorted(changed)),
			frappe.PermissionError,
		)


def _changed_business_fields(doc, previous) -> set[str]:
	return {
		field.fieldname
		for field in doc.meta.fields
		if field.fieldname not in _SYSTEM_MANAGED_FIELDS
		and doc.get(field.fieldname) != previous.get(field.fieldname)
	}


def _is_controlled_rectification_sync(doc, current_status: str) -> bool:
	context = getattr(frappe.flags, "ione_rectification_finding_sync", None)
	if not isinstance(context, dict):
		return False
	if (
		str(context.get("finding") or "") != str(doc.name or "")
		or str(context.get("target_status") or "") != current_status
	):
		return False
	rectification_name = str(context.get("rectification") or "")
	if not rectification_name:
		return False
	rectification = frappe.db.get_value(
		"IONE QC Rectification",
		rectification_name,
		["finding", "status"],
		as_dict=True,
	)
	if not rectification or str(rectification.get("finding") or "") != str(doc.name or ""):
		return False
	return _RECTIFICATION_FINDING_STATUS.get(str(rectification.get("status") or "")) == current_status


def _require_verified_closed_rectification(finding_name: str) -> None:
	closed_rectifications = frappe.get_all(
		"IONE QC Rectification",
		filters={"finding": finding_name, "status": "Closed"},
		pluck="name",
		limit_page_length=1000,
	)
	if closed_rectifications and frappe.db.exists(
		"IONE QC Verification",
		{
			"rectification": ["in", closed_rectifications],
			"status": "Verified",
		},
	):
		return
	frappe.throw("Finding closure requires a closed rectification with a verified QC verification.")


def _validate_confirmed_fields(doc, previous, previous_status: str) -> None:
	if previous_status not in {
		"Confirmed",
		"Appealed",
		"Appeal Approved",
		"Appeal Rejected",
		"Pending Rectification",
		"Rectifying",
		"Pending Department Approval",
		"Pending Functional Review",
		"Rework",
		"Closed",
	}:
		return
	changed = [
		fieldname
		for fieldname in _CONFIRMED_IMMUTABLE_FIELDS
		if _has_field(doc.doctype, fieldname) and doc.get(fieldname) != previous.get(fieldname)
	]
	if changed:
		frappe.throw(
			"Confirmed finding provenance is immutable. Record later changes through appeal, "
			f"rectification, or verification records. Changed fields: {', '.join(sorted(changed))}."
		)


def _record_transition_snapshot(doc, previous_status: str, current_status: str) -> None:
	doctype = "IONE QC Finding Evidence"
	if not frappe.db.exists("DocType", doctype):
		return
	snapshot = {
		"finding": doc.name,
		"from_status": previous_status,
		"to_status": current_status,
		"reviewed_by": frappe.session.user,
		"reviewed_at": now_datetime(),
		"rule_version": doc.get("rule_version"),
		"standard_clause": doc.get("standard_clause"),
	}
	serialized = _canonical_json(snapshot)
	values = {
		"doctype": doctype,
		"finding": doc.name,
		"evidence_type": "Workflow Transition",
		"source_system": "IONE QMS",
		"source_record_type": "Finding Status Transition",
		"source_record_id": doc.name,
		"field_path": "status",
		"evidence_text": f"{previous_status} → {current_status}",
		"evidence_json": serialized,
		"context_summary": "Immutable human workflow transition audit.",
		"captured_at": snapshot["reviewed_at"],
		"recorded_at": snapshot["reviewed_at"],
		"rule_version": doc.get("rule_version"),
		"standard_clause": doc.get("standard_clause"),
	}
	evidence = frappe.get_doc(_supported_values(doctype, values))
	evidence_hash = _evidence_content_hash(evidence)
	if _has_field(doctype, "evidence_hash"):
		if frappe.db.exists(
			doctype,
			{"finding": doc.name, "evidence_hash": evidence_hash},
		):
			return
		evidence.set("evidence_hash", evidence_hash)
	evidence.insert(ignore_permissions=True)


def _evidence_content_hash(doc) -> str:
	excluded = {
		"name",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"idx",
		"docstatus",
		"evidence_hash",
		"record_hash",
	}
	values = {
		field.fieldname: doc.get(field.fieldname)
		for field in doc.meta.fields
		if field.fieldname not in excluded
	}
	return hashlib.sha256(_canonical_json(values).encode()).hexdigest()


def _supported_values(doctype: str, values: dict[str, Any]) -> dict[str, Any]:
	meta = frappe.get_meta(doctype)
	return {
		fieldname: value
		for fieldname, value in values.items()
		if fieldname == "doctype" or meta.has_field(fieldname)
	}


def _has_field(doctype: str, fieldname: str) -> bool:
	try:
		return bool(frappe.get_meta(doctype).has_field(fieldname))
	except Exception:
		return False


def _canonical_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)


def _same_optional_value(left: Any, right: Any) -> bool:
	return (left if left not in (None, "") else None) == (right if right not in (None, "") else None)
