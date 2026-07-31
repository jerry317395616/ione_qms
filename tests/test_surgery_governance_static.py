from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "ione_qms" / "services" / "surgery_governance.py"
API = ROOT / "ione_qms" / "api" / "surgery.py"
HOOKS = ROOT / "ione_qms" / "hooks.py"
PERMISSIONS = ROOT / "ione_qms" / "permissions.py"
SCOPE = ROOT / "ione_qms" / "services" / "scope_hierarchy.py"
EXPORT = ROOT / "ione_qms" / "services" / "data_export.py"
PROJECTION = ROOT / "ione_qms" / "integration" / "projections.py"
MATERIALIZER = ROOT / "ione_qms" / "services" / "projections.py"
GENERATOR = ROOT / "tools" / "generate_doctypes.py"
WORKSPACES = ROOT / "tools" / "generate_workspaces.py"
DESK = ROOT / "ione_qms" / "public" / "js" / "surgery_governance.js"

SURGERY_AUDIT_TYPES = {
	"IONE Surgery Procedure Policy",
	"IONE Surgery MDT Record",
	"IONE Surgery MDT Participant",
	"IONE Surgery Safety Checklist",
	"IONE Surgery Emergency Exception",
}


def _function_source(source: str, name: str) -> str:
	tree = ast.parse(source)
	node = next(
		item
		for item in tree.body
		if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
	)
	return ast.get_source_segment(source, node) or ""


def _whitelist_methods(source: str, function_name: str) -> list[str]:
	tree = ast.parse(source)
	function = next(
		item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == function_name
	)
	decorator = next(
		item
		for item in function.decorator_list
		if isinstance(item, ast.Call)
		and isinstance(item.func, ast.Attribute)
		and item.func.attr == "whitelist"
	)
	methods = next(item.value for item in decorator.keywords if item.arg == "methods")
	return ast.literal_eval(methods)


class TestSurgeryGovernanceStaticContract(TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		cls.service = SERVICE.read_text(encoding="utf-8")
		cls.api = API.read_text(encoding="utf-8")
		cls.hooks = HOOKS.read_text(encoding="utf-8")
		cls.permissions = PERMISSIONS.read_text(encoding="utf-8")
		cls.scope = SCOPE.read_text(encoding="utf-8")
		cls.export = EXPORT.read_text(encoding="utf-8")
		cls.projection = PROJECTION.read_text(encoding="utf-8")
		cls.materializer = MATERIALIZER.read_text(encoding="utf-8")
		cls.generator = GENERATOR.read_text(encoding="utf-8")
		cls.workspaces = WORKSPACES.read_text(encoding="utf-8")
		cls.desk = DESK.read_text(encoding="utf-8")

	def test_approval_is_separated_versioned_immutable_and_non_overlapping(self) -> None:
		for name in (
			"validate_staff_qualification",
			"validate_surgery_authorization",
			"validate_surgery_procedure_policy",
			"_validate_governed_transition",
			"_reject_overlapping_qualification",
			"_reject_overlapping_authorization",
			"_reject_overlapping_policy",
			"prevent_surgery_governance_deletion",
		):
			self.assertIn(f"def {name}(", self.service)
		review = _function_source(self.service, "_review_governed_definition")
		self.assertIn("doc.owner", review)
		self.assertIn("cannot approve or reject their own definition", review)
		self.assertIn("surgery-definition-slot", review)
		self.assertIn('doc.status = "Approved"', review)
		self.assertIn("doc.checksum = _checksum(snapshot(doc))", review)

	def test_occurrence_validation_is_exact_and_fail_closed(self) -> None:
		release = _function_source(self.service, "_evaluate_preoperative_release")
		evaluate = _function_source(self.service, "_evaluate_occurrence")
		policy = _function_source(self.service, "_approved_policy_for_values")
		authorization = _function_source(self.service, "_approved_authorization_for_values")
		for fieldname in (
			"procedure_code",
			"surgery_level",
			"surgeon",
			"hospital",
			"campus",
			"department",
			"surgery_time",
		):
			self.assertIn(fieldname, policy + authorization + release + evaluate)
		self.assertIn("len(live_names) != 1", policy)
		self.assertIn("len(live) != 1", authorization)
		self.assertNotIn("default", policy)
		self.assertIn("Surgery is blocked", policy)
		self.assertIn("_approved_authorization_for_values", release)
		self.assertIn("_completed_mdt", release)
		self.assertIn("_completed_checklist", release)

	def test_occurred_facts_are_captured_with_stable_noncompliance_evidence(self) -> None:
		prepare = _function_source(self.service, "prepare_surgery_projection")
		evaluate = _function_source(self.service, "_evaluate_occurrence")
		validate = _function_source(self.service, "validate_surgery_qc")
		frozen = _function_source(self.service, "_frozen_occurrence_evidence")
		for token in (
			"Noncompliant",
			"Blocked",
			"governance_reason_codes_json",
			"SURGEON_AUTHORIZATION_MISSING_AMBIGUOUS_OR_INVALID",
			"REQUIRED_MDT_MISSING_AMBIGUOUS_OR_INVALID",
			"SAFETY_CHECKLIST_MISSING_AMBIGUOUS_OR_INVALID",
			"EMERGENCY_EXCEPTION_USED",
		):
			self.assertIn(token, prepare + evaluate + validate)
		self.assertIn("capture_factual_noncompliance", prepare)
		self.assertIn("FROZEN_OCCURRENCE_EVIDENCE_INVALID", evaluate)
		self.assertIn("allow_historical_retired=True", evaluate)
		self.assertIn("_surgery_evaluation_snapshot(prior)", frozen)
		self.assertNotIn("_validate_finalized_care_path(", prepare)
		snapshot = _function_source(self.service, "_surgery_evaluation_snapshot")
		self.assertIn('"policy": policy or None', snapshot)
		self.assertIn("if policy", snapshot)

	def test_delayed_events_use_occurrence_time_approval_not_current_status(self) -> None:
		policy = _function_source(self.service, "_approved_policy_for_values")
		authorization = _function_source(self.service, "_approved_authorization_for_values")
		temporal = _function_source(self.service, "_governed_definition_status_contains")
		self.assertIn('["Approved", "Retired"] if allow_historical_retired', policy)
		self.assertIn('["Approved", "Retired"] if allow_historical_retired', authorization)
		self.assertIn("approved_at", temporal)
		self.assertIn("retired_at", temporal)
		self.assertIn("get_datetime(doc.retired_at) > get_datetime(at)", temporal)

	def test_event_parser_allows_missing_factual_stages_for_noncompliance_capture(self) -> None:
		dispatch = _function_source(self.projection, "_surgery_values")
		for fieldname in (
			"preanesthesia_status",
			"intraoperative_monitoring_status",
			"recovery_status",
			"postoperative_followup_status",
			"unplanned_surgery_status",
		):
			start = dispatch.index(f'"{fieldname}"')
			self.assertIn("required=False", dispatch[start : start + 300])

	def test_level_four_mdt_and_checklist_use_frozen_approved_configuration(self) -> None:
		validate_policy = _function_source(self.service, "validate_surgery_procedure_policy")
		for evidence in (
			"minimum_mdt_participants",
			"required_mdt_roles_json",
			"required_check_items_json",
			"required_care_stages_json",
		):
			self.assertIn(evidence, validate_policy)
		self.assertIn('"Level IV"', validate_policy)
		self.assertIn("must explicitly require pre-operative MDT", validate_policy)
		for function_name in (
			"create_mdt_record",
			"complete_preoperative_checklist",
			"_completed_mdt",
			"_completed_checklist",
		):
			source = _function_source(self.service, function_name)
			self.assertIn("procedure_policy", source)
			self.assertIn("checksum", source)

	def test_emergency_exception_is_disabled_by_default_and_manually_approved(self) -> None:
		request = _function_source(self.service, "request_emergency_exception")
		review = _function_source(self.service, "review_emergency_exception")
		expiry = _function_source(self.service, "expire_surgery_emergency_exceptions")
		expiry_snapshot = _function_source(self.service, "_exception_expiration_snapshot")
		historical = _function_source(self.service, "_approved_exception")
		for token in (
			"allow_emergency_exception",
			"allowed_exception_scopes_json",
			"maximum_exception_validity_hours",
		):
			self.assertIn(token, request)
		self.assertIn("requested_by", request)
		self.assertIn("requested_by", review)
		self.assertIn("cannot approve or reject their own request", review)
		self.assertIn("valid_to", review)
		self.assertIn("Approved", review)
		self.assertIn('"IONE Surgery QC"', review)
		self.assertIn("for_update=True", review)
		self.assertIn("_require_surgery_pending", review)
		self.assertIn('["reviewed_at", "<=", at]', historical)
		self.assertIn("get_datetime(doc.reviewed_at) > get_datetime(at)", historical)
		self.assertIn("_exception_request_snapshot", expiry_snapshot)
		for token in (
			"for_update=True",
			"ione_surgery_governance_expiry",
			"expired_from_status",
			"expiration_checksum",
			"frappe.db.commit()",
		):
			self.assertIn(token, expiry)
		self.assertIn('["status", "=", "Approved"]', historical)
		self.assertIn('["status", "=", "Expired"]', historical)
		self.assertIn('["expired_from_status", "=", "Approved"]', historical)
		self.assertIn("_validate_expired_exception", historical)
		self.assertIn(
			"ione_qms.services.surgery_governance.expire_surgery_emergency_exceptions",
			self.hooks,
		)

	def test_surgery_qc_binds_full_care_path_outcomes_and_source_snapshot(self) -> None:
		prepare = _function_source(self.service, "prepare_surgery_projection")
		validate = _function_source(self.service, "_validate_structured_surgery_values")
		for fieldname in (
			"source_record_id_hash",
			"source_finalized_at",
			"record_snapshot_hash",
			"preanesthesia_status",
			"intraoperative_monitoring_status",
			"recovery_status",
			"postoperative_followup_status",
			"cancellation_status",
			"unplanned_surgery_status",
			"unplanned_return_status",
			"complication_status",
			"mortality_status",
		):
			self.assertIn(fieldname, prepare + validate)
			self.assertIn(fieldname, self.generator)
		self.assertIn("source_finalized_at cannot precede surgery occurrence", validate)

	def test_every_mutation_is_post_only_and_has_desk_actions(self) -> None:
		tree = ast.parse(self.api)
		functions = [
			item.name
			for item in tree.body
			if isinstance(item, ast.FunctionDef)
			and any(
				isinstance(decorator, ast.Call)
				and isinstance(decorator.func, ast.Attribute)
				and decorator.func.attr == "whitelist"
				for decorator in item.decorator_list
			)
		]
		self.assertEqual(len(functions), 9)
		self.assertEqual(
			_whitelist_methods(self.api, "check_surgery_preoperative_release"),
			["GET"],
		)
		for function_name in functions:
			if function_name == "check_surgery_preoperative_release":
				continue
			self.assertEqual(_whitelist_methods(self.api, function_name), ["POST"])
		for action in (
			"Approve Definition",
			"Retire Definition",
			"Record Completed Pre-operative MDT",
			"Complete Pre-operative Safety Checklist",
			"Request Emergency Exception",
			"review_surgery_emergency_exception",
		):
			self.assertIn(action, self.desk)
		self.assertIn('type: "POST"', self.desk)
		self.assertIn("await frm.reload_doc()", self.desk)
		self.assertIn("public/js/surgery_governance.js", self.hooks)

	def test_preoperative_release_has_an_explicit_read_only_decision_api(self) -> None:
		check = _function_source(self.service, "check_preoperative_release")
		api = _function_source(self.api, "check_surgery_preoperative_release")
		for token in (
			"_require_named_actor",
			"_require_scope_write",
			"_require_surgery_pending",
			"_evaluate_preoperative_release",
			"decision_at=checked_at",
			"release_allowed",
			"release_checksum",
		):
			self.assertIn(token, check)
		self.assertIn("check_preoperative_release", api)

	def test_wiring_covers_permissions_scope_export_hooks_and_workspace(self) -> None:
		for doctype in SURGERY_AUDIT_TYPES:
			for source in (
				self.hooks,
				self.permissions,
				self.scope,
				self.export,
				self.generator,
				self.workspaces,
			):
				self.assertIn(f'"{doctype}"', source)
		for hook in (
			"validate_surgery_qc",
			"validate_surgery_mdt_record",
			"validate_surgery_mdt_participant",
			"validate_surgery_safety_checklist",
			"validate_surgery_emergency_exception",
			"prevent_surgery_governance_deletion",
		):
			self.assertIn(hook, self.hooks)

	def test_normalized_event_contract_has_no_implicit_surgery_phase(self) -> None:
		dispatch = _function_source(self.projection, "_surgery_values")
		self.assertIn("phase_by_event", dispatch)
		self.assertIn("Unknown Surgery event type requires an explicit governed surgery_phase", dispatch)
		self.assertIn("Surgery event type and surgery_phase do not match", dispatch)
		self.assertIn("procedure_code", dispatch)
		self.assertIn("source_finalized_at", dispatch)
		materializer = _function_source(self.materializer, "materialize_surgery_qc")
		self.assertIn("prepare_surgery_projection", materializer)

	def test_generated_metadata_remains_machine_readable_when_present(self) -> None:
		for module, slug in (
			("ione_foundation", "ione_surgery_procedure_policy"),
			("ione_clinical_quality", "ione_surgery_mdt_record"),
			("ione_clinical_quality", "ione_surgery_mdt_participant"),
			("ione_clinical_quality", "ione_surgery_safety_checklist"),
			("ione_clinical_quality", "ione_surgery_emergency_exception"),
		):
			path = ROOT / "ione_qms" / module / "doctype" / slug / f"{slug}.json"
			if path.exists():
				payload = json.loads(path.read_text(encoding="utf-8"))
				self.assertEqual(payload["allow_rename"], 0)
				self.assertEqual(payload["module"].replace(" ", "_").lower(), module)
