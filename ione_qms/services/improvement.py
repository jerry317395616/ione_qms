from __future__ import annotations

import frappe
from frappe.utils import getdate, now_datetime, nowdate

RECTIFICATION_TRANSITIONS: dict[str, frozenset[str]] = {
	"Draft": frozenset({"Submitted", "Cancelled"}),
	"Submitted": frozenset({"In Progress", "Rejected", "Cancelled"}),
	"In Progress": frozenset({"Pending Department Approval"}),
	"Pending Department Approval": frozenset({"Pending Functional Review", "Rework"}),
	"Pending Functional Review": frozenset({"Closed", "Rework"}),
	"Rework": frozenset({"In Progress", "Cancelled"}),
	"Closed": frozenset(),
	"Rejected": frozenset(),
	"Cancelled": frozenset(),
}

VERIFICATION_TRANSITIONS: dict[str, frozenset[str]] = {
	"Draft": frozenset({"Submitted"}),
	"Submitted": frozenset({"Verified", "Rejected"}),
	"Verified": frozenset(),
	"Rejected": frozenset(),
}

PDCA_TRANSITIONS: dict[str, frozenset[str]] = {
	"Proposed": frozenset({"Approved", "Rejected"}),
	"Approved": frozenset({"Active"}),
	"Active": frozenset({"Measuring", "Cancelled"}),
	"Measuring": frozenset({"Closed", "Active"}),
	"Closed": frozenset(),
	"Rejected": frozenset(),
	"Cancelled": frozenset(),
}

DEPARTMENT_EXECUTION_ROLES = frozenset(
	{
		"IONE Physician",
		"IONE Department Director",
		"IONE Department QC Officer",
	}
)
RECTIFICATION_SUBMIT_ROLES = frozenset(
	{
		"IONE Physician",
		"IONE Department QC Officer",
	}
)
DEPARTMENT_APPROVAL_ROLES = frozenset(
	{
		"IONE Department Director",
		"IONE Department QC Officer",
	}
)
FUNCTIONAL_REVIEW_ROLES = frozenset(
	{
		"IONE QC Reviewer",
		"IONE Medical Affairs",
	}
)
PDCA_OPERATION_ROLES = frozenset(
	{
		"IONE Department Director",
		"IONE Department QC Officer",
		"IONE QC Reviewer",
		"IONE Medical Affairs",
	}
)

RECTIFICATION_TRANSITION_ROLES = {
	("Draft", "Submitted"): DEPARTMENT_EXECUTION_ROLES,
	("Draft", "Cancelled"): DEPARTMENT_EXECUTION_ROLES,
	("Submitted", "In Progress"): DEPARTMENT_EXECUTION_ROLES,
	("Submitted", "Rejected"): FUNCTIONAL_REVIEW_ROLES,
	("Submitted", "Cancelled"): DEPARTMENT_EXECUTION_ROLES,
	("In Progress", "Pending Department Approval"): RECTIFICATION_SUBMIT_ROLES,
	("Pending Department Approval", "Pending Functional Review"): DEPARTMENT_APPROVAL_ROLES,
	("Pending Department Approval", "Rework"): DEPARTMENT_APPROVAL_ROLES,
	("Pending Functional Review", "Closed"): FUNCTIONAL_REVIEW_ROLES,
	("Pending Functional Review", "Rework"): FUNCTIONAL_REVIEW_ROLES,
	("Rework", "In Progress"): DEPARTMENT_EXECUTION_ROLES,
	("Rework", "Cancelled"): DEPARTMENT_EXECUTION_ROLES,
}

VERIFICATION_TRANSITION_ROLES = {
	("Draft", "Submitted"): FUNCTIONAL_REVIEW_ROLES,
	("Submitted", "Verified"): FUNCTIONAL_REVIEW_ROLES,
	("Submitted", "Rejected"): FUNCTIONAL_REVIEW_ROLES,
}

PDCA_TRANSITION_ROLES = {
	("Proposed", "Approved"): FUNCTIONAL_REVIEW_ROLES,
	("Proposed", "Rejected"): FUNCTIONAL_REVIEW_ROLES,
	("Approved", "Active"): PDCA_OPERATION_ROLES,
	("Active", "Measuring"): PDCA_OPERATION_ROLES,
	("Active", "Cancelled"): PDCA_OPERATION_ROLES,
	("Measuring", "Closed"): FUNCTIONAL_REVIEW_ROLES,
	("Measuring", "Active"): PDCA_OPERATION_ROLES,
}

RECTIFICATION_SELF_APPROVAL_EDGES = frozenset(
	{
		("Draft", "Submitted"),
		("Draft", "Cancelled"),
		("Submitted", "In Progress"),
		("Submitted", "Cancelled"),
		("In Progress", "Pending Department Approval"),
		("Rework", "In Progress"),
		("Rework", "Cancelled"),
	}
)
VERIFICATION_SELF_APPROVAL_EDGES = frozenset({("Draft", "Submitted")})
PDCA_SELF_APPROVAL_EDGES = frozenset(
	{
		("Approved", "Active"),
		("Active", "Measuring"),
		("Measuring", "Active"),
	}
)

PDCA_EDIT_ROLE_ADDITIONS = {
	"Proposed": frozenset({"IONE Department Director", "IONE Department QC Officer"}),
}


def _build_state_edit_roles(
	role_map: dict[tuple[str, str], frozenset[str]],
	additions: dict[str, frozenset[str]] | None = None,
) -> dict[str, frozenset[str]]:
	state_roles: dict[str, set[str]] = {}
	for (state, _next_state), roles in role_map.items():
		state_roles.setdefault(state, set()).update(roles)
	for state, roles in (additions or {}).items():
		state_roles.setdefault(state, set()).update(roles)
	return {state: frozenset(roles) for state, roles in state_roles.items()}


RECTIFICATION_STATE_EDIT_ROLES = _build_state_edit_roles(RECTIFICATION_TRANSITION_ROLES)
VERIFICATION_STATE_EDIT_ROLES = _build_state_edit_roles(VERIFICATION_TRANSITION_ROLES)
PDCA_STATE_EDIT_ROLES = _build_state_edit_roles(
	PDCA_TRANSITION_ROLES,
	PDCA_EDIT_ROLE_ADDITIONS,
)

_SCOPE_FIELDS = ("hospital", "campus", "department", "ward")
_RECTIFIABLE_FINDING_STATES = frozenset({"Confirmed", "Pending Rectification", "Rectifying", "Rework"})
_ACTIVE_RECTIFICATION_STATES = frozenset(
	{
		"Draft",
		"Submitted",
		"In Progress",
		"Pending Department Approval",
		"Pending Functional Review",
		"Rework",
	}
)
_ACTIVE_VERIFICATION_STATES = frozenset({"Draft", "Submitted"})


def active_rectification_name(finding: str) -> str | None:
	"""Return the active rectification, including rows created before active_key existed."""
	if not finding:
		return None
	name = frappe.db.get_value(
		"IONE QC Rectification",
		{"active_key": finding},
		"name",
	)
	if not name:
		name = frappe.db.get_value(
			"IONE QC Rectification",
			{
				"finding": finding,
				"status": ["in", sorted(_ACTIVE_RECTIFICATION_STATES)],
			},
			"name",
		)
	return str(name) if name else None


def active_verification_name(rectification: str) -> str | None:
	"""Return the active verification, including rows created before active_key existed."""
	if not rectification:
		return None
	name = frappe.db.get_value(
		"IONE QC Verification",
		{"active_key": rectification},
		"name",
	)
	if not name:
		name = frappe.db.get_value(
			"IONE QC Verification",
			{
				"rectification": rectification,
				"status": ["in", sorted(_ACTIVE_VERIFICATION_STATES)],
			},
			"name",
		)
	return str(name) if name else None


def validate_rectification(doc, method: str | None = None) -> None:
	if doc.get("due_date") and getdate(doc.due_date) < getdate(nowdate()) and doc.is_new():
		frappe.throw("Rectification due date cannot be in the past")
	if len(str(doc.get("plan") or "").strip()) < 10:
		frappe.throw("Rectification plan must contain at least 10 characters")
	previous = doc.get_doc_before_save()
	_validate_rectification_linkage(doc, previous)
	_enforce_active_key(
		doc,
		source_field="finding",
		active_states=_ACTIVE_RECTIFICATION_STATES,
	)
	if not previous:
		if doc.get("status") not in {None, "", "Draft"}:
			frappe.throw("New rectifications must start in Draft status")
		_require_roles(set(RECTIFICATION_STATE_EDIT_ROLES["Draft"]))
		return
	_validate_transition(previous.get("status"), doc.get("status"), RECTIFICATION_TRANSITIONS)
	transition = None
	if previous.get("status") != doc.get("status"):
		transition = (str(previous.get("status") or ""), str(doc.get("status") or ""))
		_require_transition_roles(
			doc,
			transition,
			RECTIFICATION_TRANSITION_ROLES,
			RECTIFICATION_SELF_APPROVAL_EDGES,
		)
	_require_same_state_editor(
		doc,
		status_field="status",
		transition=transition,
		state_role_map=RECTIFICATION_STATE_EDIT_ROLES,
	)
	if doc.get("status") == "Closed" and not frappe.db.exists(
		"IONE QC Verification",
		{"rectification": doc.name, "status": "Verified"},
	):
		frappe.throw("Rectification closure requires a verified QC Verification record")
	if previous.get("status") in {"Closed", "Rejected", "Cancelled"}:
		frappe.throw("Terminal rectification records are immutable")


def on_rectification_update(doc, method: str | None = None) -> None:
	if not doc.get("finding"):
		return
	target_status = {
		"Submitted": "Rectifying",
		"In Progress": "Rectifying",
		"Pending Department Approval": "Pending Department Approval",
		"Pending Functional Review": "Pending Functional Review",
		"Rework": "Rework",
		"Closed": "Closed",
	}.get(doc.get("status"))
	if not target_status:
		return
	finding = frappe.get_doc("IONE QC Finding", doc.finding)
	if finding.status == target_status:
		return
	finding.status = target_status
	flag_name = "ione_rectification_finding_sync"
	missing = object()
	previous_flag = frappe.flags.get(flag_name, missing)
	frappe.flags[flag_name] = {
		"finding": finding.name,
		"rectification": doc.name,
		"target_status": target_status,
	}
	try:
		finding.save()
	finally:
		if previous_flag is missing:
			frappe.flags.pop(flag_name, None)
		else:
			frappe.flags[flag_name] = previous_flag


def validate_verification(doc, method: str | None = None) -> None:
	previous = doc.get_doc_before_save()
	rectification_status = _validate_verification_linkage(doc, previous)
	_enforce_active_key(
		doc,
		source_field="rectification",
		active_states=_ACTIVE_VERIFICATION_STATES,
	)
	if not previous:
		if doc.get("status") not in {None, "", "Draft"}:
			frappe.throw("New verification records must start in Draft status")
		_require_roles(set(VERIFICATION_STATE_EDIT_ROLES["Draft"]))
		return
	if previous.get("status") in {"Verified", "Rejected"}:
		frappe.throw("Terminal verification records are immutable")
	_validate_transition(previous.get("status"), doc.get("status"), VERIFICATION_TRANSITIONS)
	transition = None
	if previous.get("status") != doc.get("status"):
		transition = (str(previous.get("status") or ""), str(doc.get("status") or ""))
		_require_transition_roles(
			doc,
			transition,
			VERIFICATION_TRANSITION_ROLES,
			VERIFICATION_SELF_APPROVAL_EDGES,
		)
	_require_same_state_editor(
		doc,
		status_field="status",
		transition=transition,
		state_role_map=VERIFICATION_STATE_EDIT_ROLES,
	)
	if doc.get("status") in {"Submitted", "Verified", "Rejected"} and (
		rectification_status != "Pending Functional Review"
	):
		frappe.throw(
			"QC Verification can only be submitted or decided while its rectification "
			"is pending functional review"
		)
	if doc.get("status") == "Verified":
		if len(str(doc.get("verification_comment") or "").strip()) < 5:
			frappe.throw("Verified records require an objective verification comment")
		if doc.meta.has_field("verified_by") and not doc.get("verified_by"):
			doc.verified_by = frappe.session.user
		if doc.meta.has_field("verified_at") and not doc.get("verified_at"):
			doc.verified_at = now_datetime()


def on_verification_update(doc, method: str | None = None) -> None:
	if doc.get("status") not in {"Verified", "Rejected"}:
		return
	rectification_name = doc.get("rectification")
	if not rectification_name:
		return
	rectification = frappe.get_doc("IONE QC Rectification", rectification_name)
	target = "Closed" if doc.status == "Verified" else "Rework"
	if rectification.status == target:
		return
	rectification.status = target
	rectification.save()


def validate_pdca_project(doc, method: str | None = None) -> None:
	previous = doc.get_doc_before_save()
	if previous and previous.get("status") in {"Closed", "Rejected", "Cancelled"}:
		frappe.throw("Terminal PDCA projects are immutable")
	from ione_qms.services.finding_recurrence import is_controlled_recurrence_pdca_proposal
	from ione_qms.services.pdca_structure import validate_pdca_structure

	validate_pdca_structure(doc, previous)
	if not previous:
		if doc.get("status") not in {None, "", "Proposed"}:
			frappe.throw("New PDCA projects must start in Proposed status")
		if not is_controlled_recurrence_pdca_proposal(doc):
			_require_roles(set(PDCA_STATE_EDIT_ROLES["Proposed"]))
		return
	_validate_transition(previous.get("status"), doc.get("status"), PDCA_TRANSITIONS)
	transition = None
	if previous.get("status") != doc.get("status"):
		transition = (str(previous.get("status") or ""), str(doc.get("status") or ""))
		_require_transition_roles(
			doc,
			transition,
			PDCA_TRANSITION_ROLES,
			PDCA_SELF_APPROVAL_EDGES,
		)
	_require_same_state_editor(
		doc,
		status_field="status",
		transition=transition,
		state_role_map=PDCA_STATE_EDIT_ROLES,
	)


def _validate_rectification_linkage(doc, previous) -> None:
	finding_name = str(doc.get("finding") or "")
	if previous and str(previous.get("finding") or "") != finding_name:
		frappe.throw("A rectification cannot be relinked to another finding")
	if not finding_name:
		return
	finding = frappe.db.get_value(
		"IONE QC Finding",
		finding_name,
		["status", *_SCOPE_FIELDS],
		as_dict=True,
	)
	if not finding:
		frappe.throw("The linked quality finding does not exist")
	_inherit_and_validate_scope(doc, finding, "quality finding")
	if not previous:
		if str(finding.get("status") or "") not in _RECTIFIABLE_FINDING_STATES:
			frappe.throw("The linked quality finding is not in a rectifiable state")
		if active_rectification_name(finding_name):
			frappe.throw("The quality finding already has an active rectification")


def _validate_verification_linkage(doc, previous) -> str:
	rectification_name = str(doc.get("rectification") or "")
	finding_name = str(doc.get("finding") or "")
	if previous:
		if str(previous.get("rectification") or "") != rectification_name:
			frappe.throw("A verification cannot be relinked to another rectification")
		if str(previous.get("finding") or "") != finding_name:
			frappe.throw("A verification cannot be relinked to another finding")
	if not rectification_name:
		return ""
	rectification = frappe.db.get_value(
		"IONE QC Rectification",
		rectification_name,
		["finding", "status", *_SCOPE_FIELDS],
		as_dict=True,
	)
	if not rectification:
		frappe.throw("The linked rectification does not exist")
	linked_finding = str(rectification.get("finding") or "")
	if finding_name and finding_name != linked_finding:
		frappe.throw("Verification finding must match the rectification finding")
	if not finding_name and linked_finding:
		doc.set("finding", linked_finding)
	_inherit_and_validate_scope(doc, rectification, "rectification")
	if not previous and active_verification_name(rectification_name):
		frappe.throw("The rectification already has an active verification")
	return str(rectification.get("status") or "")


def _enforce_active_key(
	doc,
	*,
	source_field: str,
	active_states: frozenset[str],
) -> None:
	status = str(doc.get("status") or "Draft")
	source = str(doc.get(source_field) or "")
	doc.set("active_key", source if source and status in active_states else None)


def _inherit_and_validate_scope(doc, source, source_label: str) -> None:
	for fieldname in _SCOPE_FIELDS:
		expected = source.get(fieldname)
		actual = doc.get(fieldname)
		if not actual and expected:
			doc.set(fieldname, expected)
		elif str(actual or "") != str(expected or ""):
			frappe.throw(f"{fieldname.replace('_', ' ').title()} must match the linked {source_label}")


def _validate_transition(
	previous_status: str | None,
	current_status: str | None,
	transitions: dict[str, frozenset[str]],
) -> None:
	previous = str(previous_status or "")
	current = str(current_status or "")
	if previous == current:
		return
	if current not in transitions.get(previous, frozenset()):
		allowed = ", ".join(sorted(transitions.get(previous, frozenset()))) or "none"
		frappe.throw(f"Invalid workflow transition {previous} → {current}. Allowed: {allowed}.")


def _require_transition_roles(
	doc,
	transition: tuple[str, str],
	role_map: dict[tuple[str, str], frozenset[str]],
	self_approval_edges: frozenset[tuple[str, str]],
) -> None:
	allowed = role_map.get(transition)
	if not allowed:
		frappe.throw(f"No reviewed business-role policy exists for {transition[0]} -> {transition[1]}.")
	_require_roles(set(allowed))
	_require_self_approval(doc, transition, self_approval_edges)


def _require_roles(allowed: set[str]) -> None:
	user = frappe.session.user
	if user == "Administrator":
		frappe.throw(
			"Administrator cannot perform governed improvement workflow actions; use a named "
			"accountable business user.",
			frappe.PermissionError,
		)
	roles = set(frappe.get_roles(user))
	if "IONE Agent Service" in roles or not roles.intersection(allowed):
		frappe.throw(
			"Current user is not an authorized clinical workflow actor",
			frappe.PermissionError,
		)


def _require_self_approval(
	doc,
	transition: tuple[str, str],
	self_approval_edges: frozenset[tuple[str, str]],
) -> None:
	user = frappe.session.user
	if user == "Administrator":
		frappe.throw(
			"Administrator cannot perform governed improvement workflow actions; use a named "
			"accountable business user.",
			frappe.PermissionError,
		)
	if user == doc.get("owner") and transition not in self_approval_edges:
		frappe.throw("Self approval is not allowed", frappe.PermissionError)


def _require_same_state_editor(
	doc,
	*,
	status_field: str,
	transition: tuple[str, str] | None,
	state_role_map: dict[str, frozenset[str]],
) -> None:
	previous = doc.get_doc_before_save()
	if not previous or transition is not None:
		return
	changed = _changed_business_fields(doc, previous)
	if not changed:
		return
	user = frappe.session.user
	if user == "Administrator":
		frappe.throw(
			"Administrator cannot edit governed improvement records; use a named accountable business user.",
			frappe.PermissionError,
		)
	required = state_role_map.get(str(doc.get(status_field) or ""), frozenset())
	roles = set(frappe.get_roles(user))
	if "IONE Agent Service" in roles or not roles.intersection(required):
		frappe.throw(
			"Current user cannot edit content in this workflow state. Changed: " + ", ".join(sorted(changed)),
			frappe.PermissionError,
		)


def _changed_business_fields(doc, previous) -> set[str]:
	excluded = {
		"name",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"docstatus",
		"idx",
		"submitted_at",
		"department_approved_by",
		"department_approved_at",
		"functional_reviewed_by",
		"functional_reviewed_at",
		"verified_by",
		"verified_at",
		"active_key",
		"_user_tags",
		"_comments",
		"_assign",
		"_liked_by",
	}
	return {
		field.fieldname
		for field in doc.meta.fields
		if field.fieldname not in excluded
		and not field.fieldtype.endswith("Break")
		and doc.get(field.fieldname) != previous.get(field.fieldname)
	}
