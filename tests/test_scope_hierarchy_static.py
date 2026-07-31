from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCOPE = ROOT / "ione_qms" / "services" / "scope_hierarchy.py"
PERMISSIONS = ROOT / "ione_qms" / "permissions.py"
HOOKS = ROOT / "ione_qms" / "hooks.py"
QUALITY = ROOT / "ione_qms" / "api" / "quality.py"


def function_source(path: Path, name: str) -> str:
	source = path.read_text(encoding="utf-8")
	tree = ast.parse(source)
	for node in tree.body:
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
			return ast.get_source_segment(source, node) or ""
	raise AssertionError(f"{name} was not found")


class TestScopeHierarchyStatic(unittest.TestCase):
	def test_all_patient_ai_and_analytics_records_share_one_governed_set(self) -> None:
		scope = SCOPE.read_text(encoding="utf-8")
		hooks = HOOKS.read_text(encoding="utf-8")
		for doctype in (
			"IONE Patient Index",
			"IONE Encounter Index",
			"IONE AI Analysis Task",
			"IONE AI Candidate Finding",
			"IONE AI Data Access Log",
			"IONE AI Execution Event",
			"IONE AI Report Draft",
			"IONE AI Tool Approval",
			"IONE Flow Run Link",
			"IONE Agent Analysis Fact",
		):
			self.assertIn(f'"{doctype}"', scope)
			self.assertGreaterEqual(
				hooks.count(f'"{doctype}": "ione_qms.permissions.'),
				2,
			)
		self.assertIn(
			"CLINICAL_SCOPE_DOCTYPES as _CLINICAL_SCOPE_DOCTYPES",
			hooks,
		)
		self.assertIn("for _scope_doctype in sorted(_CLINICAL_SCOPE_DOCTYPES)", hooks)

	def test_patient_and_encounter_sql_validate_tenant_keys_and_source_namespace(self) -> None:
		scope = SCOPE.read_text(encoding="utf-8")
		patient = function_source(SCOPE, "_patient_identity_sql")
		encounter = function_source(SCOPE, "_encounter_identity_sql")
		identity_contract = function_source(SCOPE, "_identity_contract_sql")
		for source in (patient, encounter):
			self.assertIn("_identity_contract_sql", source)
			self.assertIn("source_namespace", source)
			self.assertIn("identity_namespace", source)
			self.assertIn("source_system", source)
			self.assertNotIn("sha2(concat(", source)
		self.assertIn("acceptable_identity_key_contracts()", identity_contract)
		self.assertIn("identity_key_contract", identity_contract)
		self.assertIn("regexp", identity_contract)
		self.assertIn("verify_stored_identity_key(", scope)
		self.assertIn("tenant_hospital", patient)
		self.assertIn("source_patient_id", patient)
		self.assertIn("source_encounter_id", encounter)
		self.assertIn("scope_encounter_patient", encounter)

	def test_linked_identity_projections_include_the_key_contract(self) -> None:
		for function_name in (
			"_index_identity_errors",
			"_encounter_reference_errors",
			"_patient_reference_errors",
		):
			source = function_source(SCOPE, function_name)
			self.assertIn(
				'"identity_key_contract"',
				source,
				f"{function_name} must project the contract used to verify its HMAC",
			)
		phi_authorization = function_source(PERMISSIONS, "resolve_phi_identity_authorization")
		for fieldname in (
			"encounter_key",
			"identity_key_contract",
			"source_system",
			"source_namespace",
			"source_encounter_id",
			"patient",
		):
			self.assertIn(fieldname, phi_authorization)
		self.assertIn("clinical_scope_errors(encounter)", phi_authorization)

	def test_python_and_sql_enforce_ai_task_child_lineage(self) -> None:
		scope = SCOPE.read_text(encoding="utf-8")
		python_guard = function_source(SCOPE, "_ai_task_lineage_errors")
		sql_guard = function_source(SCOPE, "_ai_task_lineage_sql")
		self.assertIn('"IONE AI Data Access Log": ("task", True)', scope)
		for fieldname in ("hospital", "campus", "department", "ward", "patient", "encounter"):
			self.assertIn(f'"{fieldname}"', scope)
		self.assertIn("allow_narrower_patient", python_guard)
		self.assertIn("AI_TASK_", python_guard)
		self.assertIn("coalesce(", sql_guard)
		self.assertIn("IONE AI Analysis Task", sql_guard)

	def test_staff_scope_is_active_effective_and_hierarchy_valid(self) -> None:
		context = function_source(PERMISSIONS, "get_access_context")
		guard = function_source(PERMISSIONS, "_staff_assignment_is_current_and_consistent")
		self.assertIn('"practice_status": "Active"', context)
		self.assertIn("_staff_assignment_is_current_and_consistent", context)
		self.assertIn("effective_from", guard)
		self.assertIn("effective_to", guard)
		self.assertIn("clinical_scope_errors(record)", guard)
		self.assertIn('!= "Active"', guard)

	def test_patient_query_anchors_hospital_directly_and_lower_scope_via_encounter(self) -> None:
		query = function_source(PERMISSIONS, "patient_query")
		permission = function_source(PERMISSIONS, "patient_permission")
		self.assertIn(".tenant_hospital in", query)
		self.assertIn("IONE Encounter Index", query)
		self.assertIn('scope_integrity_sql("IONE Patient Index"', query)
		self.assertIn("_scope_document_is_consistent(doc)", permission)
		self.assertIn("tenant_hospital", permission)

	def test_ai_list_and_document_paths_never_bypass_integrity(self) -> None:
		query = function_source(PERMISSIONS, "_ai_query")
		document = function_source(PERMISSIONS, "ai_task_permission")
		access_log = function_source(PERMISSIONS, "ai_data_access_log_permission")
		self.assertLess(query.index("scope_integrity_sql("), query.index("IONE Agent Administrator"))
		self.assertIn("_scope_document_is_consistent(doc)", document)
		self.assertIn("_scope_document_is_consistent(doc)", access_log)

	def test_personal_scope_is_opt_in_only_for_safety_event_creation(self) -> None:
		require_scope = function_source(PERMISSIONS, "require_scope_read")
		safety = function_source(QUALITY, "report_medical_safety_event")
		self.assertIn("include_personal: bool = False", require_scope)
		self.assertIn("include_personal=True", safety)
		self.assertIn("ward=scope[", safety)


if __name__ == "__main__":
	unittest.main()
