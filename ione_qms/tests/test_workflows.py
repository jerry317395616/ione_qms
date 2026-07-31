from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from ione_qms.services import findings, improvement, versions
from ione_qms.services.findings import (
	_FINDING_STATE_EDIT_ROLES,
	_FINDING_TRANSITIONS,
	_SELF_APPROVAL_EDGES,
	_TRANSITION_ROLES,
)
from ione_qms.services.improvement import (
	PDCA_SELF_APPROVAL_EDGES,
	PDCA_STATE_EDIT_ROLES,
	PDCA_TRANSITION_ROLES,
	PDCA_TRANSITIONS,
	RECTIFICATION_SELF_APPROVAL_EDGES,
	RECTIFICATION_STATE_EDIT_ROLES,
	RECTIFICATION_TRANSITION_ROLES,
	RECTIFICATION_TRANSITIONS,
	VERIFICATION_SELF_APPROVAL_EDGES,
	VERIFICATION_STATE_EDIT_ROLES,
	VERIFICATION_TRANSITION_ROLES,
	VERIFICATION_TRANSITIONS,
)
from ione_qms.services.versions import (
	AGENT_RELEASE_SELF_APPROVAL_EDGES,
	AGENT_RELEASE_STATE_EDIT_ROLES,
	AGENT_RELEASE_TRANSITION_ROLES,
	AGENT_RELEASE_TRANSITIONS,
	INDICATOR_VERSION_SELF_APPROVAL_EDGES,
	INDICATOR_VERSION_STATE_EDIT_ROLES,
	INDICATOR_VERSION_TRANSITION_ROLES,
	INDICATOR_VERSION_TRANSITIONS,
	RULE_VERSION_SELF_APPROVAL_EDGES,
	RULE_VERSION_STATE_EDIT_ROLES,
	RULE_VERSION_TRANSITION_ROLES,
	RULE_VERSION_TRANSITIONS,
	STANDARD_VERSION_SELF_APPROVAL_EDGES,
	STANDARD_VERSION_STATE_EDIT_ROLES,
	STANDARD_VERSION_TRANSITION_ROLES,
	STANDARD_VERSION_TRANSITIONS,
)
from ione_qms.setup import workflows
from ione_qms.setup.workflows import (
	ACTIVE_RECTIFICATION_CONDITION,
	AGENT_RELEASE_WORKFLOW,
	FINDING_WORKFLOW,
	INDICATOR_VERSION_WORKFLOW,
	PDCA_WORKFLOW,
	RECTIFICATION_WORKFLOW,
	RULE_VERSION_WORKFLOW,
	STANDARD_VERSION_WORKFLOW,
	VERIFICATION_WORKFLOW,
	VERIFIED_FINDING_CONDITION,
	VERIFIED_RECTIFICATION_CONDITION,
	WORKFLOW_SPECS,
	validate_workflow_install_preflight,
	validate_workflow_specifications,
)


class TestWorkflowSpecifications(TestCase):
	def test_specs_are_internally_valid(self) -> None:
		validate_workflow_specifications()
		self.assertEqual(len(WORKFLOW_SPECS), 8)

	def test_install_preflight_rejects_every_existing_owned_workflow(self) -> None:
		reserved = WORKFLOW_SPECS[0].name
		with (
			patch.object(
				workflows.frappe.db,
				"exists",
				side_effect=lambda doctype, name: doctype == "Workflow" and name == reserved,
			),
			patch.object(workflows.frappe, "throw", side_effect=RuntimeError),
			self.assertRaisesRegex(RuntimeError, "will not claim or overwrite"),
		):
			validate_workflow_install_preflight()

	def test_shared_state_and_action_are_reused_only_when_canonical(self) -> None:
		state = WORKFLOW_SPECS[0].states[0]
		action = WORKFLOW_SPECS[0].transitions[0].action

		def exists(doctype: str, name: str) -> bool:
			return (doctype, name) in {
				("Workflow State", state.name),
				("Workflow Action Master", action),
			}

		def get_doc(doctype: str, name: str):
			if doctype == "Workflow State":
				return {
					"workflow_state_name": state.name,
					"style": state.style,
				}
			return {"workflow_action_name": action}

		with (
			patch.object(workflows.frappe.db, "exists", side_effect=exists),
			patch.object(workflows.frappe, "get_doc", side_effect=get_doc),
		):
			validate_workflow_install_preflight()

	def test_shared_state_drift_is_rejected_without_mutation(self) -> None:
		state = WORKFLOW_SPECS[0].states[0]

		def exists(doctype: str, name: str) -> bool:
			return doctype == "Workflow State" and name == state.name

		with (
			patch.object(workflows.frappe.db, "exists", side_effect=exists),
			patch.object(
				workflows.frappe,
				"get_doc",
				return_value={
					"workflow_state_name": state.name,
					"style": "Danger",
				},
			),
			patch.object(workflows.frappe.db, "set_value") as set_value,
			patch.object(workflows.frappe, "throw", side_effect=RuntimeError),
			self.assertRaisesRegex(RuntimeError, "never modifies shared Workflow States"),
		):
			validate_workflow_install_preflight()
		set_value.assert_not_called()

	def test_finding_workflow_matches_service_state_machine(self) -> None:
		service_edges = _edges(_FINDING_TRANSITIONS) | {("Confirmed", "Rectifying")}
		self.assertEqual(_spec_edges(FINDING_WORKFLOW), service_edges)
		self.assertEqual(_spec_roles(FINDING_WORKFLOW), _TRANSITION_ROLES)

	def test_improvement_workflows_match_service_state_machines(self) -> None:
		for spec, transitions, roles in (
			(
				RECTIFICATION_WORKFLOW,
				RECTIFICATION_TRANSITIONS,
				RECTIFICATION_TRANSITION_ROLES,
			),
			(
				VERIFICATION_WORKFLOW,
				VERIFICATION_TRANSITIONS,
				VERIFICATION_TRANSITION_ROLES,
			),
			(PDCA_WORKFLOW, PDCA_TRANSITIONS, PDCA_TRANSITION_ROLES),
		):
			with self.subTest(workflow=spec.name):
				self.assertEqual(_spec_edges(spec), _edges(transitions))
				self.assertEqual(_spec_roles(spec), roles)

	def test_version_workflows_match_service_state_machines(self) -> None:
		for spec, transitions, roles in (
			(
				STANDARD_VERSION_WORKFLOW,
				STANDARD_VERSION_TRANSITIONS,
				STANDARD_VERSION_TRANSITION_ROLES,
			),
			(
				RULE_VERSION_WORKFLOW,
				RULE_VERSION_TRANSITIONS,
				RULE_VERSION_TRANSITION_ROLES,
			),
			(
				INDICATOR_VERSION_WORKFLOW,
				INDICATOR_VERSION_TRANSITIONS,
				INDICATOR_VERSION_TRANSITION_ROLES,
			),
			(
				AGENT_RELEASE_WORKFLOW,
				AGENT_RELEASE_TRANSITIONS,
				AGENT_RELEASE_TRANSITION_ROLES,
			),
		):
			with self.subTest(workflow=spec.name):
				self.assertEqual(_spec_edges(spec), _edges(transitions))
				self.assertEqual(_spec_roles(spec), roles)

	def test_service_and_workflow_self_approval_edges_match(self) -> None:
		for spec, self_approval_edges in (
			(FINDING_WORKFLOW, _SELF_APPROVAL_EDGES),
			(RECTIFICATION_WORKFLOW, RECTIFICATION_SELF_APPROVAL_EDGES),
			(VERIFICATION_WORKFLOW, VERIFICATION_SELF_APPROVAL_EDGES),
			(PDCA_WORKFLOW, PDCA_SELF_APPROVAL_EDGES),
			(STANDARD_VERSION_WORKFLOW, STANDARD_VERSION_SELF_APPROVAL_EDGES),
			(RULE_VERSION_WORKFLOW, RULE_VERSION_SELF_APPROVAL_EDGES),
			(INDICATOR_VERSION_WORKFLOW, INDICATOR_VERSION_SELF_APPROVAL_EDGES),
			(AGENT_RELEASE_WORKFLOW, AGENT_RELEASE_SELF_APPROVAL_EDGES),
		):
			with self.subTest(workflow=spec.name):
				self.assertEqual(_spec_self_approval_edges(spec), self_approval_edges)

	def test_rule_lifecycle_is_the_blueprint_sequence(self) -> None:
		self.assertEqual(
			RULE_VERSION_WORKFLOW.state_names,
			(
				"Draft",
				"Expert Review",
				"Test",
				"Historical Replay",
				"Shadow Run",
				"Approval",
				"Published",
				"Retired",
			),
		)
		self.assertEqual(
			_spec_edges(RULE_VERSION_WORKFLOW),
			set(
				zip(
					RULE_VERSION_WORKFLOW.state_names[:-1],
					RULE_VERSION_WORKFLOW.state_names[1:],
					strict=True,
				)
			),
		)

	def test_agent_service_never_receives_a_workflow_decision(self) -> None:
		for spec in WORKFLOW_SPECS:
			for transition in spec.transitions:
				self.assertNotIn("IONE Agent Service", transition.roles)
			for state in spec.states:
				self.assertNotIn("IONE Agent Service", state.edit_roles)

	def test_administrator_cannot_perform_governed_business_actions(self) -> None:
		administrator_roles = ["IONE Agent Service", "IONE QC Reviewer"]
		with (
			patch.object(improvement.frappe.session, "user", "Administrator"),
			patch.object(improvement.frappe, "get_roles", return_value=administrator_roles),
			patch.object(findings.frappe, "get_roles", return_value=administrator_roles),
			patch.object(versions.frappe, "get_roles", return_value=administrator_roles),
		):
			with self.assertRaises(improvement.frappe.PermissionError):
				improvement._require_roles({"IONE QC Reviewer"})
			with self.assertRaises(findings.frappe.PermissionError):
				findings._reject_agent_decision("Pending QC Review", "Confirmed")
			with self.assertRaises(findings.frappe.PermissionError):
				findings._require_transition_role("Pending QC Review", "Confirmed")
			with self.assertRaises(versions.frappe.PermissionError):
				versions._require_transition_actor(
					{"owner": "Administrator"},
					("Under Review", "Approved"),
					STANDARD_VERSION_TRANSITION_ROLES,
					STANDARD_VERSION_SELF_APPROVAL_EDGES,
				)

	def test_review_and_terminal_actions_disallow_self_approval(self) -> None:
		protected_targets = {
			"Confirmed",
			"Approved",
			"Published",
			"Retired",
			"Verified",
			"Rejected",
			"Closed",
			"Pending Functional Review",
		}
		for spec in WORKFLOW_SPECS:
			for transition in spec.transitions:
				if transition.next_state in protected_targets:
					self.assertFalse(
						transition.allow_self_approval,
						f"{spec.name}: {transition.state} -> {transition.next_state}",
					)

	def test_rectification_roles_match_synchronized_finding_transitions(self) -> None:
		exact_matches = {
			("In Progress", "Pending Department Approval"): (
				"Rectifying",
				"Pending Department Approval",
			),
			("Pending Department Approval", "Pending Functional Review"): (
				"Pending Department Approval",
				"Pending Functional Review",
			),
			("Pending Department Approval", "Rework"): (
				"Pending Department Approval",
				"Rework",
			),
			("Pending Functional Review", "Closed"): (
				"Pending Functional Review",
				"Closed",
			),
			("Pending Functional Review", "Rework"): (
				"Pending Functional Review",
				"Rework",
			),
		}
		for rectification_edge, finding_edge in exact_matches.items():
			with self.subTest(rectification_edge=rectification_edge):
				self.assertEqual(
					RECTIFICATION_TRANSITION_ROLES[rectification_edge],
					_TRANSITION_ROLES[finding_edge],
				)

	def test_workflow_conditions_use_only_frappe_safe_get_value(self) -> None:
		conditions = {
			ACTIVE_RECTIFICATION_CONDITION,
			VERIFIED_FINDING_CONDITION,
			VERIFIED_RECTIFICATION_CONDITION,
		}
		for condition in conditions:
			with self.subTest(condition=condition):
				tree = ast.parse(condition, mode="eval")
				self.assertTrue(any(isinstance(node, ast.Call) for node in ast.walk(tree)))
				for node in ast.walk(tree):
					if isinstance(node, ast.Call):
						self.assertEqual(_dotted_name(node.func), "frappe.db.get_value")
					if isinstance(node, ast.Name):
						self.assertIn(node.id, {"doc", "frappe"})

		self.assertEqual(
			_transition(FINDING_WORKFLOW, "Confirmed", "Rectifying").condition,
			ACTIVE_RECTIFICATION_CONDITION,
		)
		self.assertEqual(
			_transition(FINDING_WORKFLOW, "Pending Functional Review", "Closed").condition,
			VERIFIED_FINDING_CONDITION,
		)
		self.assertEqual(
			_transition(RECTIFICATION_WORKFLOW, "Pending Functional Review", "Closed").condition,
			VERIFIED_RECTIFICATION_CONDITION,
		)

	def test_draft_authors_retain_edit_access_before_review(self) -> None:
		self.assertTrue(
			{
				"IONE Department Director",
				"IONE Department QC Officer",
			}.issubset(set(_state(PDCA_WORKFLOW, "Proposed").edit_roles))
		)
		self.assertIn(
			"IONE Agent Administrator",
			_state(AGENT_RELEASE_WORKFLOW, "Draft").edit_roles,
		)

	def test_server_state_edit_roles_match_workflow_allow_edit_roles(self) -> None:
		for workflow, state_role_map in (
			(FINDING_WORKFLOW, _FINDING_STATE_EDIT_ROLES),
			(RECTIFICATION_WORKFLOW, RECTIFICATION_STATE_EDIT_ROLES),
			(VERIFICATION_WORKFLOW, VERIFICATION_STATE_EDIT_ROLES),
			(PDCA_WORKFLOW, PDCA_STATE_EDIT_ROLES),
			(STANDARD_VERSION_WORKFLOW, STANDARD_VERSION_STATE_EDIT_ROLES),
			(RULE_VERSION_WORKFLOW, RULE_VERSION_STATE_EDIT_ROLES),
			(INDICATOR_VERSION_WORKFLOW, INDICATOR_VERSION_STATE_EDIT_ROLES),
			(AGENT_RELEASE_WORKFLOW, AGENT_RELEASE_STATE_EDIT_ROLES),
		):
			for state in workflow.states:
				with self.subTest(workflow=workflow.name, state=state.name):
					expected = state_role_map.get(
						state.name,
						frozenset({"IONE Auditor"}),
					)
					self.assertEqual(frozenset(state.edit_roles), expected)

	def test_doctype_select_options_match_workflows(self) -> None:
		package_root = Path(__file__).resolve().parents[1]
		cases = (
			("ione_clinical_quality", "ione_qc_finding", "status", FINDING_WORKFLOW),
			("ione_improvement", "ione_qc_rectification", "status", RECTIFICATION_WORKFLOW),
			("ione_improvement", "ione_qc_verification", "status", VERIFICATION_WORKFLOW),
			("ione_improvement", "ione_pdca_project", "status", PDCA_WORKFLOW),
			(
				"ione_quality_standards",
				"ione_qc_standard_version",
				"approval_status",
				STANDARD_VERSION_WORKFLOW,
			),
			("ione_quality_standards", "ione_qc_rule_version", "status", RULE_VERSION_WORKFLOW),
			("ione_indicators", "ione_qc_indicator_version", "status", INDICATOR_VERSION_WORKFLOW),
			("ione_flow_ai", "ione_agent_release", "status", AGENT_RELEASE_WORKFLOW),
		)
		for module, doctype, state_fieldname, workflow in cases:
			with self.subTest(workflow=workflow.name):
				path = package_root / module / "doctype" / doctype / f"{doctype}.json"
				payload = json.loads(path.read_text(encoding="utf-8"))
				state_field = next(
					field for field in payload["fields"] if field["fieldname"] == state_fieldname
				)
				options = tuple(line.strip() for line in state_field["options"].splitlines() if line.strip())
				self.assertEqual(options, workflow.state_names)
				self.assertEqual(state_field.get("default"), workflow.state_names[0])
				self.assertFalse(payload.get("is_submittable"))
				write_roles = {
					permission["role"]
					for permission in payload.get("permissions", [])
					if permission.get("write")
				}
				transition_roles = {role for transition in workflow.transitions for role in transition.roles}
				controlled_entry_exceptions = (
					{"IONE Agent Reviewer"} if workflow is FINDING_WORKFLOW else set()
				)
				self.assertEqual(
					transition_roles.difference(write_roles),
					controlled_entry_exceptions,
				)


def _edges(transitions: dict[str, frozenset[str]]) -> set[tuple[str, str]]:
	return {(source, target) for source, targets in transitions.items() for target in targets}


def _spec_edges(spec) -> set[tuple[str, str]]:
	return {(transition.state, transition.next_state) for transition in spec.transitions}


def _spec_roles(spec) -> dict[tuple[str, str], frozenset[str]]:
	return {
		(transition.state, transition.next_state): frozenset(transition.roles)
		for transition in spec.transitions
	}


def _spec_self_approval_edges(spec) -> frozenset[tuple[str, str]]:
	return frozenset(
		(transition.state, transition.next_state)
		for transition in spec.transitions
		if transition.allow_self_approval
	)


def _transition(spec, state: str, next_state: str):
	return next(
		transition
		for transition in spec.transitions
		if transition.state == state and transition.next_state == next_state
	)


def _state(spec, name: str):
	return next(state for state in spec.states if state.name == name)


def _dotted_name(node: ast.expr) -> str:
	if isinstance(node, ast.Name):
		return node.id
	if isinstance(node, ast.Attribute):
		return f"{_dotted_name(node.value)}.{node.attr}"
	raise AssertionError(f"Unsupported callable in workflow condition: {ast.dump(node)}")
