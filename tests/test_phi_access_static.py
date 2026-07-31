from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "ione_qms" / "services" / "phi_access.py"
API = ROOT / "ione_qms" / "api" / "privacy.py"
GENERATOR = ROOT / "tools" / "generate_doctypes.py"
HOOKS = ROOT / "ione_qms" / "hooks.py"
CONSTANTS = ROOT / "ione_qms" / "constants.py"
EXPORT = ROOT / "ione_qms" / "services" / "data_export.py"
QUALITY_API = ROOT / "ione_qms" / "api" / "quality.py"
POLICY_JS = ROOT / "ione_qms" / "public" / "js" / "phi_disclosure_policy.js"
IDENTITY_JS = ROOT / "ione_qms" / "public" / "js" / "phi_identity.js"
QUERY_GUARD = ROOT / "ione_qms" / "overrides" / "phi_query_guard.py"
INSTALL = ROOT / "ione_qms" / "setup" / "install.py"
PERMISSIONS = ROOT / "ione_qms" / "permissions.py"
PRIVACY_REGISTRY = ROOT / "ione_qms" / "privacy_registry.py"


def _function_source(path: Path, name: str) -> str:
	source = path.read_text(encoding="utf-8")
	tree = ast.parse(source)
	for node in tree.body:
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
			return ast.get_source_segment(source, node) or ""
	raise AssertionError(f"{name} was not found in {path}")


class TestPHIAccessStatic(unittest.TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		cls.service = SERVICE.read_text(encoding="utf-8")
		cls.api = API.read_text(encoding="utf-8")
		cls.generator = GENERATOR.read_text(encoding="utf-8")
		cls.hooks = HOOKS.read_text(encoding="utf-8")

	def test_direct_identifiers_are_high_permlevel_masked_and_not_search_titles(self) -> None:
		registry = PRIVACY_REGISTRY.read_text(encoding="utf-8")
		normalize = _function_source(GENERATOR, "_normalized_fields")
		for fieldname in (
			"source_patient_id",
			"patient_name",
			"date_of_birth",
			"identification_hash",
			"phone_hash",
			"source_encounter_id",
			"encounter_no",
		):
			self.assertIn(f'"{fieldname}"', registry)
		self.assertIn("permlevel", normalize)
		self.assertIn("= 9", normalize)
		self.assertIn('item["mask"] = 1', normalize)
		for unsafe_index in ("in_list_view", "in_standard_filter", "search_index"):
			self.assertIn(f'item.pop("{unsafe_index}"', normalize)
		self.assertIn("IDENTITY_BEARING_DOCTYPES", self.generator)
		self.assertIn('payload["track_changes"] = 0', self.generator)
		patient_schema = self.generator[
			self.generator.index('"IONE Patient Index": schema(') : self.generator.index(
				'"IONE Encounter Index": schema('
			)
		]
		encounter_schema = self.generator[
			self.generator.index('"IONE Encounter Index": schema(') : self.generator.index(
				'"IONE QC Standard": schema('
			)
		]
		self.assertIn('title_field="patient_key"', patient_schema)
		self.assertIn('search_fields="patient_key"', patient_schema)
		self.assertIn('title_field="encounter_key"', encounter_schema)
		self.assertIn('search_fields="encounter_key"', encounter_schema)

	def test_raw_fields_have_no_generic_export_or_manual_evaluation_bypass(self) -> None:
		constants = CONSTANTS.read_text(encoding="utf-8")
		export = EXPORT.read_text(encoding="utf-8")
		for fieldname in (
			"source_patient_id",
			"patient_name",
			"date_of_birth",
			"source_encounter_id",
			"encounter_no",
		):
			self.assertIn(f'"{fieldname}"', constants)
		self.assertIn("SENSITIVE_FIELD_NAMES", export)
		evaluate = _function_source(QUALITY_API, "evaluate_encounter")
		self.assertNotIn('"encounter_no"', evaluate)
		self.assertIn('"encounter_key"', evaluate)

	def test_disclosure_requires_named_dual_role_scope_and_minimum_fields(self) -> None:
		wrapper = _function_source(SERVICE, "disclose_phi_identity")
		disclose = _function_source(SERVICE, "_disclose_phi_identity_locked")
		authorize = _function_source(SERVICE, "_authorized_identity_document")
		resolver = _function_source(PERMISSIONS, "resolve_phi_identity_authorization")
		dependency_locks = _function_source(PERMISSIONS, "_lock_phi_authorization_dependencies")
		lock_rows = _function_source(PERMISSIONS, "_lock_phi_dependency_rows")
		policy_authorizes = _function_source(SERVICE, "_assert_policy_authorizes")
		named_actor = _function_source(SERVICE, "_require_named_actor")
		self.assertIn("_PHI_AUTHORIZATION_GLOBAL_LOCK", wrapper)
		self.assertIn("advisory_lock", wrapper)
		self.assertIn("_disclose_phi_identity_locked(", wrapper)
		self.assertIn("require_post_migrate_runtime_ready", disclose)
		self.assertIn("PHI_READER_ROLE", disclose)
		self.assertIn('"IONE Agent Service"', disclose)
		self.assertIn("_normalize_requested_fields", disclose)
		self.assertIn("_resolve_active_policy", disclose)
		self.assertIn("_enforce_disclosure_rate_limit", disclose)
		self.assertIn("resolve_phi_identity_authorization", authorize)
		for locked_table in ("tabUser", "tabHas Role", "tabUser Permission", "tabIONE Medical Staff"):
			self.assertIn(locked_table, resolver)
		self.assertIn("for update", resolver)
		self.assertIn('"Active"', resolver)
		self.assertIn("discharge_time", resolver)
		self.assertIn("_independent_phi_scope_roles", resolver)
		self.assertIn("authorizing_roles", resolver)
		self.assertIn("_lock_phi_authorization_dependencies", resolver)
		self.assertIn('"dependencies": dependency_snapshot', resolver)
		for locked_dependency in (
			"IONE Patient Index",
			"IONE Medical Staff",
			"IONE Source System",
			"IONE Hospital",
			"IONE Hospital Campus",
			"IONE Medical Department",
			"IONE Ward",
		):
			self.assertIn(f'"{locked_dependency}"', PERMISSIONS.read_text(encoding="utf-8"))
		self.assertIn("_PHI_DEPENDENCY_LOCK_ORDER", dependency_locks)
		self.assertIn("for update", lock_rows)
		self.assertIn("allowed_reader_roles_json", policy_authorizes)
		self.assertIn("authorizing_roles.intersection", policy_authorizes)
		self.assertIn('"Administrator"', named_actor)
		rate_limit = _function_source(SERVICE, "_enforce_disclosure_rate_limit")
		self.assertIn("cache.incr", rate_limit)
		self.assertIn("http_status_code", rate_limit)
		self.assertIn("RateLimitExceededError", rate_limit)

	def test_policy_is_external_approval_bound_independently_reviewed_and_immutable(self) -> None:
		normalize = _function_source(SERVICE, "_normalize_policy_configuration")
		submit = _function_source(SERVICE, "submit_phi_disclosure_policy")
		approve = _function_source(SERVICE, "approve_phi_disclosure_policy")
		lifecycle = _function_source(SERVICE, "_validate_policy_lifecycle")
		resolve = _function_source(SERVICE, "_resolve_active_policy")
		checksum = _function_source(SERVICE, "_policy_checksum")
		self.assertIn("external_approval_reference", normalize)
		self.assertIn("_policy_checksum", submit)
		self.assertIn("advisory_lock", submit)
		self.assertIn("requested_by", approve)
		self.assertIn("submitted_by", approve)
		self.assertIn("_assert_policy_checksum", approve)
		self.assertIn("_assert_policy_family_lock_is_current", approve)
		self.assertIn("_retire_active_exact_scope", approve)
		self.assertIn('"Scheduled"', approve)
		self.assertIn("advisory_lock", approve)
		self.assertIn("_POLICY_SEMANTIC_FIELDS", lifecycle)
		self.assertIn("immutable", lifecycle)
		self.assertIn("maximum", resolve)
		self.assertIn("len(winners) != 1", resolve)
		self.assertIn("_assert_policy_checksum", resolve)
		self.assertIn("_site_hmac", checksum)

	def test_authorized_receipt_is_hmac_bound_inserted_before_return_and_contains_no_phi(self) -> None:
		wrapper = _function_source(SERVICE, "disclose_phi_identity")
		disclose = _function_source(SERVICE, "_disclose_phi_identity_locked")
		validator = _function_source(SERVICE, "validate_phi_access_receipt")
		auth_tag = _function_source(SERVICE, "_phi_receipt_auth_tag")
		insert_position = disclose.index("receipt.insert(ignore_permissions=True)")
		return_position = disclose.index("return {", insert_position)
		self.assertLess(insert_position, return_position)
		self.assertIn("_PHI_AUTHORIZATION_GLOBAL_LOCK", wrapper)
		self.assertIn("advisory_lock", disclose)
		self.assertIn("already been consumed", disclose)
		self.assertIn("_site_hmac", disclose)
		self.assertIn("_set_no_store_response_headers", disclose)
		for raw_key in ("reference_name", "source_patient_id", "patient_name", "encounter_no"):
			receipt_schema_start = self.generator.index('"IONE PHI Access Receipt": schema(')
			receipt_schema_end = self.generator.index('"IONE Feature Flag": schema(')
			self.assertNotIn(f'"{raw_key}"', self.generator[receipt_schema_start:receipt_schema_end])
		self.assertIn("_RECEIPT_INSERT_TOKEN", validator)
		self.assertIn("validate_append_only", validator)
		self.assertIn("session binding", validator)
		self.assertIn("_phi_receipt_auth_tag", validator)
		self.assertIn("_site_hmac", auth_tag)
		self.assertIn("verify_phi_access_receipt_integrity", self.hooks)

	def test_only_post_endpoints_and_escaped_client_rendering_are_exposed(self) -> None:
		self.assertEqual(self.api.count('@frappe.whitelist(methods=["POST"])'), 4)
		self.assertNotIn('methods=["GET"]', self.api)
		policy_js = POLICY_JS.read_text(encoding="utf-8")
		identity_js = IDENTITY_JS.read_text(encoding="utf-8")
		for source in (policy_js, identity_js):
			self.assertIn('type: "POST"', source)
			self.assertNotIn("eval(", source)
			self.assertNotIn("innerHTML", source)
		self.assertIn("frappe.call", source)
		self.assertIn("frappe.utils.escape_html", identity_js)
		self.assertIn("window.crypto.getRandomValues", identity_js)

	def test_hooks_and_non_exportable_governance_records_are_complete(self) -> None:
		for doctype in ("IONE PHI Disclosure Policy", "IONE PHI Access Receipt"):
			self.assertIn(f'"{doctype}"', self.hooks)
			self.assertIn(f'"{doctype}"', self.generator)
			self.assertIn(f'"{doctype}"', EXPORT.read_text(encoding="utf-8"))
		self.assertIn("prepare_phi_disclosure_policy", self.hooks)
		self.assertIn("validate_phi_disclosure_policy", self.hooks)
		self.assertIn("validate_phi_access_receipt", self.hooks)
		self.assertGreaterEqual(self.hooks.count("prevent_phi_record_deletion"), 4)

	def test_generic_filter_inference_and_runtime_metadata_bypasses_are_blocked(self) -> None:
		guard = QUERY_GUARD.read_text(encoding="utf-8")
		request_guard = _function_source(QUERY_GUARD, "prevent_unsafe_phi_query_request")
		artifact_guard = _function_source(QUERY_GUARD, "validate_phi_query_artifact")
		metadata_guard = _function_source(QUERY_GUARD, "prevent_phi_metadata_override")
		legacy_audit = _function_source(QUERY_GUARD, "assert_no_legacy_phi_query_bypasses")
		for contract in (
			"PROTECTED_FIELDS_BY_DOCTYPE",
			"_PHI_FIELD_PATTERN",
			"_DOTTED_FIELD_PATTERN",
			"_contains_phi_field_reference",
			"_root_doctypes",
		):
			self.assertIn(contract, guard)
		self.assertIn('path.startswith("/api/")', request_guard)
		self.assertIn("command.startswith", request_guard)
		self.assertIn("_is_governed_configuration_write", request_guard)
		self.assertIn("PermissionError", request_guard)
		for doctype in ("Report", "Dashboard Chart", "Number Card", "List Filter"):
			self.assertIn(f'"{doctype}"', guard)
			self.assertIn(f'"{doctype}"', self.hooks)
		for doctype in ("Custom DocPerm", "Custom Field", "Property Setter"):
			self.assertIn(f'"{doctype}"', guard)
			self.assertIn(f'"{doctype}"', self.hooks)
		self.assertIn("_contains_phi_field_reference", artifact_guard)
		self.assertIn("_metadata_target", metadata_guard)
		self.assertIn("assert_no_legacy_phi_query_bypasses", INSTALL.read_text(encoding="utf-8"))
		self.assertIn("_bounded_artifact_names", legacy_audit)
		self.assertIn("before_request", self.hooks)


if __name__ == "__main__":
	unittest.main()
