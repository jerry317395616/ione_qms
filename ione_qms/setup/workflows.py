from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import frappe

from ione_qms.constants import APP_ROLES
from ione_qms.services.findings import (
	_SELF_APPROVAL_EDGES as FINDING_SELF_APPROVAL_EDGES,
)
from ione_qms.services.findings import (
	_TRANSITION_ROLES as FINDING_TRANSITION_ROLES,
)
from ione_qms.services.improvement import (
	PDCA_EDIT_ROLE_ADDITIONS,
	PDCA_SELF_APPROVAL_EDGES,
	PDCA_TRANSITION_ROLES,
	RECTIFICATION_SELF_APPROVAL_EDGES,
	RECTIFICATION_TRANSITION_ROLES,
	VERIFICATION_SELF_APPROVAL_EDGES,
	VERIFICATION_TRANSITION_ROLES,
)
from ione_qms.services.versions import (
	AGENT_RELEASE_EDIT_ROLE_ADDITIONS,
	AGENT_RELEASE_SELF_APPROVAL_EDGES,
	AGENT_RELEASE_TRANSITION_ROLES,
	INDICATOR_VERSION_SELF_APPROVAL_EDGES,
	INDICATOR_VERSION_TRANSITION_ROLES,
	RULE_VERSION_SELF_APPROVAL_EDGES,
	RULE_VERSION_TRANSITION_ROLES,
	STANDARD_VERSION_SELF_APPROVAL_EDGES,
	STANDARD_VERSION_TRANSITION_ROLES,
)

FINDING_REVIEW_ACTION = "Submit for QC Review"
RECTIFICATION_SUBMIT_ACTION = "Submit Rectification"

ACTIVE_RECTIFICATION_CONDITION = (
	'frappe.db.get_value("IONE QC Rectification", '
	'{"finding": doc.name, "status": ["not in", ["Closed", "Rejected", "Cancelled"]]}, '
	'"name")'
)
VERIFIED_RECTIFICATION_CONDITION = (
	'frappe.db.get_value("IONE QC Verification", {"rectification": doc.name, "status": "Verified"}, "name")'
)
VERIFIED_FINDING_CONDITION = (
	'frappe.db.get_value("IONE QC Verification", '
	'{"rectification": frappe.db.get_value("IONE QC Rectification", '
	'{"finding": doc.name, "status": "Closed"}, "name"), "status": "Verified"}, "name")'
)

_STATE_STYLES = {
	"Draft": "Info",
	"Proposed": "Info",
	"Candidate": "Info",
	"Under Review": "Warning",
	"Expert Review": "Warning",
	"Test": "Warning",
	"Historical Replay": "Warning",
	"Shadow Run": "Warning",
	"Approval": "Warning",
	"Pending QC Review": "Warning",
	"Pending Rectification": "Warning",
	"Pending Department Approval": "Warning",
	"Pending Functional Review": "Warning",
	"Submitted": "Warning",
	"Rework": "Warning",
	"Appealed": "Warning",
	"In Progress": "Primary",
	"Rectifying": "Primary",
	"Active": "Primary",
	"Measuring": "Primary",
	"Confirmed": "Success",
	"Approved": "Success",
	"Published": "Success",
	"Verified": "Success",
	"Closed": "Success",
	"Appeal Approved": "Success",
	"Rejected": "Danger",
	"Appeal Rejected": "Danger",
	"Cancelled": "Inverse",
	"Retired": "Inverse",
}


@dataclass(frozen=True)
class WorkflowStateSpec:
	name: str
	edit_roles: tuple[str, ...]
	style: str


@dataclass(frozen=True)
class WorkflowTransitionSpec:
	state: str
	action: str
	next_state: str
	roles: tuple[str, ...]
	allow_self_approval: bool = False
	condition: str | None = None


@dataclass(frozen=True)
class WorkflowSpec:
	name: str
	document_type: str
	state_field: str
	states: tuple[WorkflowStateSpec, ...]
	transitions: tuple[WorkflowTransitionSpec, ...]

	@property
	def state_names(self) -> tuple[str, ...]:
		return tuple(state.name for state in self.states)


def _roles(values: Iterable[str]) -> tuple[str, ...]:
	return tuple(sorted(set(values)))


def _build_spec(
	*,
	name: str,
	document_type: str,
	state_field: str,
	state_names: tuple[str, ...],
	actions: dict[tuple[str, str], str],
	role_map: dict[tuple[str, str], frozenset[str]],
	self_approval_edges: frozenset[tuple[str, str]] = frozenset(),
	conditions: dict[tuple[str, str], str] | None = None,
	edit_role_additions: dict[str, frozenset[str]] | None = None,
) -> WorkflowSpec:
	conditions = conditions or {}
	edit_role_additions = edit_role_additions or {}
	transitions = tuple(
		WorkflowTransitionSpec(
			state=source,
			action=action,
			next_state=target,
			roles=_roles(role_map[(source, target)]),
			allow_self_approval=(source, target) in self_approval_edges,
			condition=conditions.get((source, target)),
		)
		for (source, target), action in actions.items()
	)
	edit_roles: dict[str, set[str]] = {state: set() for state in state_names}
	for transition in transitions:
		edit_roles[transition.state].update(transition.roles)
	for state, roles in edit_role_additions.items():
		if state not in edit_roles:
			raise ValueError(f"{name} edit-role addition references undeclared state {state}")
		edit_roles[state].update(roles)
	states = tuple(
		WorkflowStateSpec(
			name=state,
			edit_roles=_roles(edit_roles[state] or {"IONE QMS Auditor"}),
			style=_STATE_STYLES.get(state, "Info"),
		)
		for state in state_names
	)
	return WorkflowSpec(
		name=name,
		document_type=document_type,
		state_field=state_field,
		states=states,
		transitions=transitions,
	)


FINDING_WORKFLOW = _build_spec(
	name="IONE QC Finding Workflow",
	document_type="IONE QC Finding",
	state_field="status",
	state_names=(
		"Candidate",
		"Pending QC Review",
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
	),
	actions={
		("Candidate", "Pending QC Review"): FINDING_REVIEW_ACTION,
		("Pending QC Review", "Confirmed"): "Confirm Finding",
		("Confirmed", "Pending Rectification"): "Send Finding for Rectification",
		("Confirmed", "Appealed"): "Appeal Finding",
		("Appealed", "Confirmed"): "Withdraw Finding Appeal",
		("Appealed", "Appeal Approved"): "Approve Finding Appeal",
		("Appealed", "Appeal Rejected"): "Reject Finding Appeal",
		("Appeal Rejected", "Pending Rectification"): "Send Rejected Appeal for Rectification",
		("Confirmed", "Rectifying"): "Start Finding Rectification",
		("Pending Rectification", "Rectifying"): "Start Finding Rectification",
		("Rectifying", "Pending Department Approval"): "Submit Finding for Department Approval",
		("Pending Department Approval", "Pending Functional Review"): ("Approve Finding at Department"),
		("Pending Department Approval", "Rework"): "Return Finding for Rework",
		("Pending Functional Review", "Closed"): "Close Finding",
		("Pending Functional Review", "Rework"): "Return Finding for Rework",
		("Rework", "Rectifying"): "Resume Finding Rectification",
	},
	role_map=FINDING_TRANSITION_ROLES,
	self_approval_edges=FINDING_SELF_APPROVAL_EDGES,
	conditions={
		("Confirmed", "Rectifying"): ACTIVE_RECTIFICATION_CONDITION,
		("Pending Functional Review", "Closed"): VERIFIED_FINDING_CONDITION,
	},
)

RECTIFICATION_WORKFLOW = _build_spec(
	name="IONE QC Rectification Workflow",
	document_type="IONE QC Rectification",
	state_field="status",
	state_names=(
		"Draft",
		"Submitted",
		"In Progress",
		"Pending Department Approval",
		"Pending Functional Review",
		"Rework",
		"Closed",
		"Rejected",
		"Cancelled",
	),
	actions={
		("Draft", "Submitted"): RECTIFICATION_SUBMIT_ACTION,
		("Draft", "Cancelled"): "Cancel Rectification",
		("Submitted", "In Progress"): "Start Rectification Work",
		("Submitted", "Rejected"): "Reject Rectification",
		("Submitted", "Cancelled"): "Cancel Rectification",
		("In Progress", "Pending Department Approval"): ("Submit Rectification for Department Approval"),
		("Pending Department Approval", "Pending Functional Review"): ("Approve Rectification at Department"),
		("Pending Department Approval", "Rework"): "Return Rectification for Rework",
		("Pending Functional Review", "Closed"): "Close Rectification",
		("Pending Functional Review", "Rework"): "Return Rectification for Rework",
		("Rework", "In Progress"): "Resume Rectification Work",
		("Rework", "Cancelled"): "Cancel Rectification",
	},
	role_map=RECTIFICATION_TRANSITION_ROLES,
	self_approval_edges=RECTIFICATION_SELF_APPROVAL_EDGES,
	conditions={
		("Pending Functional Review", "Closed"): VERIFIED_RECTIFICATION_CONDITION,
	},
)

VERIFICATION_WORKFLOW = _build_spec(
	name="IONE QC Verification Workflow",
	document_type="IONE QC Verification",
	state_field="status",
	state_names=("Draft", "Submitted", "Verified", "Rejected"),
	actions={
		("Draft", "Submitted"): "Submit Verification",
		("Submitted", "Verified"): "Verify Rectification",
		("Submitted", "Rejected"): "Reject Verification",
	},
	role_map=VERIFICATION_TRANSITION_ROLES,
	self_approval_edges=VERIFICATION_SELF_APPROVAL_EDGES,
)

PDCA_WORKFLOW = _build_spec(
	name="IONE PDCA Project Workflow",
	document_type="IONE PDCA Project",
	state_field="status",
	state_names=("Proposed", "Approved", "Active", "Measuring", "Closed", "Rejected", "Cancelled"),
	actions={
		("Proposed", "Approved"): "Approve PDCA Project",
		("Proposed", "Rejected"): "Reject PDCA Project",
		("Approved", "Active"): "Activate PDCA Project",
		("Active", "Measuring"): "Begin PDCA Measurement",
		("Active", "Cancelled"): "Cancel PDCA Project",
		("Measuring", "Closed"): "Close PDCA Project",
		("Measuring", "Active"): "Resume PDCA Action",
	},
	role_map=PDCA_TRANSITION_ROLES,
	self_approval_edges=PDCA_SELF_APPROVAL_EDGES,
	edit_role_additions=PDCA_EDIT_ROLE_ADDITIONS,
)

STANDARD_VERSION_WORKFLOW = _build_spec(
	name="IONE QC Standard Version Workflow",
	document_type="IONE QC Standard Version",
	state_field="approval_status",
	state_names=("Draft", "Under Review", "Approved", "Retired"),
	actions={
		("Draft", "Under Review"): "Submit Standard for Review",
		("Under Review", "Draft"): "Return Standard to Draft",
		("Under Review", "Approved"): "Approve Standard Version",
		("Approved", "Retired"): "Retire Standard Version",
	},
	role_map=STANDARD_VERSION_TRANSITION_ROLES,
	self_approval_edges=STANDARD_VERSION_SELF_APPROVAL_EDGES,
)

RULE_VERSION_WORKFLOW = _build_spec(
	name="IONE QC Rule Version Workflow",
	document_type="IONE QC Rule Version",
	state_field="status",
	state_names=(
		"Draft",
		"Expert Review",
		"Test",
		"Historical Replay",
		"Shadow Run",
		"Approval",
		"Published",
		"Retired",
	),
	actions={
		("Draft", "Expert Review"): "Submit Rule for Expert Review",
		("Expert Review", "Test"): "Complete Rule Expert Review",
		("Test", "Historical Replay"): "Confirm Rule Tests",
		("Historical Replay", "Shadow Run"): "Accept Rule Historical Replay",
		("Shadow Run", "Approval"): "Accept Rule Shadow Run",
		("Approval", "Published"): "Publish Rule Version",
		("Published", "Retired"): "Retire Rule Version",
	},
	role_map=RULE_VERSION_TRANSITION_ROLES,
	self_approval_edges=RULE_VERSION_SELF_APPROVAL_EDGES,
)

INDICATOR_VERSION_WORKFLOW = _build_spec(
	name="IONE QC Indicator Version Workflow",
	document_type="IONE QC Indicator Version",
	state_field="status",
	state_names=("Draft", "Under Review", "Published", "Retired"),
	actions={
		("Draft", "Under Review"): "Submit Indicator for Review",
		("Under Review", "Draft"): "Return Indicator to Draft",
		("Under Review", "Published"): "Publish Indicator Version",
		("Published", "Retired"): "Retire Indicator Version",
	},
	role_map=INDICATOR_VERSION_TRANSITION_ROLES,
	self_approval_edges=INDICATOR_VERSION_SELF_APPROVAL_EDGES,
)

AGENT_RELEASE_WORKFLOW = _build_spec(
	name="IONE Agent Release Workflow",
	document_type="IONE Agent Release",
	state_field="status",
	state_names=("Draft", "Approved", "Retired"),
	actions={
		("Draft", "Approved"): "Approve Agent Release",
		("Approved", "Retired"): "Retire Agent Release",
	},
	role_map=AGENT_RELEASE_TRANSITION_ROLES,
	self_approval_edges=AGENT_RELEASE_SELF_APPROVAL_EDGES,
	edit_role_additions=AGENT_RELEASE_EDIT_ROLE_ADDITIONS,
)

WORKFLOW_SPECS = (
	FINDING_WORKFLOW,
	RECTIFICATION_WORKFLOW,
	VERIFICATION_WORKFLOW,
	PDCA_WORKFLOW,
	STANDARD_VERSION_WORKFLOW,
	RULE_VERSION_WORKFLOW,
	INDICATOR_VERSION_WORKFLOW,
	AGENT_RELEASE_WORKFLOW,
)


def validate_workflow_install_preflight() -> None:
	"""Read-only collision audit performed before IONE DocType synchronization."""
	validate_workflow_specifications()
	for spec in WORKFLOW_SPECS:
		if frappe.db.exists("Workflow", spec.name):
			frappe.throw(
				f"Reserved Workflow '{spec.name}' already exists. A fresh IONE installation "
				"will not claim or overwrite an unknown Workflow before schema synchronization."
			)
	state_specs = {state.name: state for spec in WORKFLOW_SPECS for state in spec.states}
	for state in state_specs.values():
		if frappe.db.exists("Workflow State", state.name):
			_validate_existing_workflow_state(state)
	action_names = {transition.action for spec in WORKFLOW_SPECS for transition in spec.transitions}
	for action in sorted(action_names):
		if frappe.db.exists("Workflow Action Master", action):
			_validate_existing_workflow_action(action)


def ensure_workflows() -> dict[str, int]:
	"""Idempotently install the code-owned IONE workflows.

	The managed workflow names are stable. Each migration reconciles those records to
	the reviewed code definition while leaving unrelated global Workflow States and
	Actions untouched.
	"""
	validate_workflow_specifications()
	missing = [
		spec.document_type for spec in WORKFLOW_SPECS if not frappe.db.exists("DocType", spec.document_type)
	]
	if missing:
		frappe.throw("IONE workflow installation requires these DocTypes: " + ", ".join(sorted(missing)))
	for spec in WORKFLOW_SPECS:
		_validate_runtime_state_field(spec)

	state_specs = {state.name: state for spec in WORKFLOW_SPECS for state in spec.states}
	action_names = {transition.action for spec in WORKFLOW_SPECS for transition in spec.transitions}
	created_states = sum(_ensure_workflow_state(state) for state in state_specs.values())
	created_actions = sum(_ensure_workflow_action(action) for action in sorted(action_names))
	created_workflows = 0
	updated_workflows = 0
	for spec in WORKFLOW_SPECS:
		created = _upsert_workflow(spec)
		created_workflows += int(created)
		updated_workflows += int(not created)
	frappe.clear_cache()
	return {
		"created_states": created_states,
		"created_actions": created_actions,
		"created_workflows": created_workflows,
		"updated_workflows": updated_workflows,
	}


def validate_workflow_specifications() -> None:
	workflow_names: set[str] = set()
	document_types: set[str] = set()
	for spec in WORKFLOW_SPECS:
		if spec.name in workflow_names:
			raise ValueError(f"Duplicate managed Workflow name: {spec.name}")
		if spec.document_type in document_types:
			raise ValueError(f"Duplicate managed Workflow DocType: {spec.document_type}")
		workflow_names.add(spec.name)
		document_types.add(spec.document_type)
		if not spec.states:
			raise ValueError(f"{spec.name} has no states")
		state_names = spec.state_names
		if len(state_names) != len(set(state_names)):
			raise ValueError(f"{spec.name} has duplicate states")
		transition_keys: set[tuple[str, str, str]] = set()
		for state in spec.states:
			if not state.edit_roles:
				raise ValueError(f"{spec.name} state {state.name} has no edit role")
			_validate_roles(spec.name, state.edit_roles)
		for transition in spec.transitions:
			if transition.state not in state_names or transition.next_state not in state_names:
				raise ValueError(
					f"{spec.name} transition references an undeclared state: "
					f"{transition.state} -> {transition.next_state}"
				)
			if not transition.action.strip():
				raise ValueError(f"{spec.name} has a blank action")
			_validate_roles(spec.name, transition.roles)
			for role in transition.roles:
				key = (transition.state, transition.action, role)
				if key in transition_keys:
					raise ValueError(
						f"{spec.name} has an ambiguous action for {transition.state}, "
						f"{transition.action}, {role}"
					)
				transition_keys.add(key)


def _validate_roles(workflow_name: str, roles: tuple[str, ...]) -> None:
	if "IONE Agent Service" in roles:
		raise ValueError(f"{workflow_name} must never grant a decision to IONE Agent Service")
	unknown = set(roles).difference(APP_ROLES)
	if unknown:
		raise ValueError(f"{workflow_name} references unknown roles: {', '.join(sorted(unknown))}")


def _validate_runtime_state_field(spec: WorkflowSpec) -> None:
	meta = frappe.get_meta(spec.document_type)
	field = meta.get_field(spec.state_field)
	if not field:
		frappe.throw(f"{spec.document_type} is missing workflow state field {spec.state_field}")
	if field.fieldtype != "Select":
		frappe.throw(f"{spec.document_type}.{spec.state_field} must be a Select field")
	options = tuple(line.strip() for line in str(field.options or "").splitlines() if line.strip())
	if options != spec.state_names:
		frappe.throw(
			f"{spec.document_type}.{spec.state_field} options do not match the managed "
			f"workflow. Expected: {', '.join(spec.state_names)}; got: {', '.join(options)}"
		)


def _ensure_workflow_state(state: WorkflowStateSpec) -> int:
	if frappe.db.exists("Workflow State", state.name):
		_validate_existing_workflow_state(state)
		return 0
	frappe.get_doc(
		{
			"doctype": "Workflow State",
			"workflow_state_name": state.name,
			"style": state.style,
		}
	).insert(ignore_permissions=True)
	return 1


def _ensure_workflow_action(action: str) -> int:
	if frappe.db.exists("Workflow Action Master", action):
		_validate_existing_workflow_action(action)
		return 0
	frappe.get_doc(
		{
			"doctype": "Workflow Action Master",
			"workflow_action_name": action,
		}
	).insert(ignore_permissions=True)
	return 1


def _validate_existing_workflow_state(state: WorkflowStateSpec) -> None:
	doc = frappe.get_doc("Workflow State", state.name)
	mismatches = [
		fieldname
		for fieldname, expected in {
			"workflow_state_name": state.name,
			"style": state.style,
		}.items()
		if str(doc.get(fieldname) or "") != expected
	]
	if mismatches:
		frappe.throw(
			f"Shared Workflow State '{state.name}' is incompatible with IONE. "
			"IONE reuses but never modifies shared Workflow States. Fields: " + ", ".join(sorted(mismatches))
		)


def _validate_existing_workflow_action(action: str) -> None:
	doc = frappe.get_doc("Workflow Action Master", action)
	if str(doc.get("workflow_action_name") or "") != action:
		frappe.throw(
			f"Shared Workflow Action '{action}' is incompatible with IONE. "
			"IONE reuses but never modifies shared Workflow Actions."
		)


def _upsert_workflow(spec: WorkflowSpec) -> bool:
	existing = frappe.db.exists("Workflow", spec.name)
	doc = (
		frappe.get_doc("Workflow", spec.name)
		if existing
		else frappe.get_doc({"doctype": "Workflow", "workflow_name": spec.name})
	)
	doc.update(
		{
			"document_type": spec.document_type,
			"is_active": 1,
			"override_status": 0,
			"send_email_alert": 0,
			"enable_action_confirmation": 1,
			"workflow_state_field": spec.state_field,
		}
	)
	doc.set("states", [])
	for state in spec.states:
		for role in state.edit_roles:
			doc.append(
				"states",
				{
					"state": state.name,
					"doc_status": "0",
					"update_field": spec.state_field,
					"update_value": state.name,
					"evaluate_as_expression": 0,
					"allow_edit": role,
					"send_email": 0,
					"avoid_status_override": 0,
				},
			)
	doc.set("transitions", [])
	for transition in spec.transitions:
		for role in transition.roles:
			doc.append(
				"transitions",
				{
					"state": transition.state,
					"action": transition.action,
					"next_state": transition.next_state,
					"allowed": role,
					"allow_self_approval": int(transition.allow_self_approval),
					"condition": transition.condition,
				},
			)
	doc.save(ignore_permissions=True)
	frappe.clear_cache(doctype=spec.document_type)
	return not bool(existing)
