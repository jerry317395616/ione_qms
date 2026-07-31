from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "ione_qms" / "integration" / "service.py"
MASTER = ROOT / "ione_qms" / "integration" / "master_data.py"
SCOPE = ROOT / "ione_qms" / "services" / "integration_scope.py"
GENERATOR = ROOT / "tools" / "generate_doctypes.py"
CONFIG = ROOT / "ione_qms" / "services" / "integration_config.py"
INSTALL = ROOT / "ione_qms" / "setup" / "install.py"
IDENTITY_KEYS = ROOT / "ione_qms" / "services" / "identity_keys.py"


def function_source(path: Path, name: str) -> str:
	source = path.read_text(encoding="utf-8")
	tree = ast.parse(source)
	for node in tree.body:
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
			return ast.get_source_segment(source, node) or ""
	raise AssertionError(f"{name} was not found")


class TestIntegrationTenantScopeStatic(unittest.TestCase):
	def test_schema_has_hierarchical_allowlist_and_tenant_audit_fields(self) -> None:
		source = GENERATOR.read_text(encoding="utf-8")
		self.assertIn('"IONE Integration Allowed Scope": schema(', source)
		for fieldname in ("hospital", "campus", "department", "ward"):
			self.assertIn(f'"{fieldname}"', source)
		self.assertIn('"identity_namespace"', source)
		self.assertIn('"tenant_hospital"', source)
		self.assertIn('"source_namespace"', source)
		self.assertIn('"scope_policy_hash"', source)

	def test_generated_schema_count_and_child_contract(self) -> None:
		paths = list((ROOT / "ione_qms").glob("*/doctype/*/*.json"))
		doctypes = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
		names = {item["name"] for item in doctypes}
		self.assertEqual(len(names), len(doctypes))
		self.assertTrue(
			{
				"IONE Integration Allowed Scope",
				"IONE Integration Endpoint",
				"IONE Integration Mapping",
				"IONE Integration Message",
			}.issubset(names)
		)
		child = next(item for item in doctypes if item["name"] == "IONE Integration Allowed Scope")
		self.assertEqual(child.get("istable"), 1)

	def test_preauthorization_precedes_message_identity_and_resolves_index_links(self) -> None:
		receive = function_source(SERVICE, "receive_clinical_event")
		source_runtime = function_source(SERVICE, "_assert_source_runtime")
		self.assertLess(receive.index("_assert_source_runtime("), receive.index("_canonical_json("))
		self.assertLess(receive.index("_preauthorize_scope("), receive.index("_idempotency_key("))
		self.assertIn("identity_namespace", source_runtime)
		canonicalize = function_source(SCOPE, "canonicalize_scope")
		self.assertIn('"IONE Patient Index"', canonicalize)
		self.assertIn('"IONE Encounter Index"', canonicalize)
		self.assertIn('"source_namespace"', canonicalize)

	def test_invalid_oversize_and_scope_violation_never_retain_raw_payload(self) -> None:
		receive = function_source(SERVICE, "receive_clinical_event")
		connector_quarantine = function_source(SERVICE, "quarantine_invalid_clinical_event")
		self.assertIn("_invalid_schema_manifest(", receive)
		self.assertIn("_oversized_payload_manifest(", receive)
		self.assertIn("_invalid_schema_manifest(", connector_quarantine)
		self.assertIn("sanitize_identity=True", connector_quarantine)
		invalid_manifest = function_source(SERVICE, "_invalid_schema_manifest")
		scope_manifest = function_source(SERVICE, "_scope_violation_manifest")
		self.assertIn('"payload_retained": False', invalid_manifest)
		self.assertIn('"payload_retained": False', scope_manifest)
		quarantine = function_source(SERVICE, "_quarantine_scope_violation")
		self.assertNotIn("payload_json=payload_json", quarantine)
		self.assertIn("_scope_violation_manifest(", quarantine)

	def test_patient_encounter_and_message_keys_include_tenant_partition(self) -> None:
		patient_upsert = function_source(MASTER, "_upsert_patient")
		encounter_upsert = function_source(MASTER, "_upsert_encounter")
		candidates = function_source(IDENTITY_KEYS, "identity_key_candidates")
		for upsert in (patient_upsert, encounter_upsert):
			self.assertIn("identity_key_candidates(", upsert)
			self.assertIn("source_namespace", upsert)
			self.assertIn("tenant_hospital", upsert)
			self.assertIn("_load_or_new_identity(", upsert)
		for fieldname in ("source_namespace", "tenant_hospital", "source_id"):
			self.assertIn(fieldname, candidates)
		self.assertIn("retained_hmac_keys", candidates)
		idempotency = function_source(SERVICE, "_idempotency_key")
		self.assertIn("source_namespace", idempotency)
		self.assertIn("tenant_hospital", idempotency)
		self.assertNotIn("endpoint", idempotency)

	def test_cross_endpoint_duplicate_never_rebinds_or_reprocesses_message(self) -> None:
		receive = function_source(SERVICE, "receive_clinical_event")
		cross_endpoint = function_source(SERVICE, "_cross_endpoint_duplicate_outcome")
		self.assertIn("existing.endpoint != endpoint.name", receive)
		self.assertIn('message.status in {"Received", "Error"}', cross_endpoint)
		self.assertIn("deferred=True", cross_endpoint)
		self.assertNotIn("_process_message(", cross_endpoint)

	def test_only_reviewed_connector_markers_bypass_generic_invalid_manifest(self) -> None:
		connector_quarantine = function_source(SERVICE, "quarantine_invalid_clinical_event")
		marker = function_source(SERVICE, "_retained_connector_marker")
		self.assertIn("_retained_connector_marker(", connector_quarantine)
		self.assertIn("decode_connector_quarantine_record(payload)", marker)
		self.assertIn('failure_type != "InvalidConnectorRecord"', marker)
		self.assertIn('marker.get("connector") != connector_key', marker)

	def test_legacy_configuration_is_disabled_without_scope_guessing(self) -> None:
		migrations = (ROOT / "ione_qms" / "tasks" / "migrations.py").read_text(encoding="utf-8")
		self.assertIn("enforce_integration_tenant_fail_closed", migrations)
		self.assertNotIn("tenant_hospital =", migrations)

	def test_install_uses_narrow_restored_configuration_maintenance_token(self) -> None:
		config = CONFIG.read_text(encoding="utf-8")
		maintenance = function_source(CONFIG, "trusted_integration_configuration_maintenance")
		authorization = function_source(CONFIG, "_require_named_integration_administrator")
		ensure_settings = function_source(INSTALL, "ensure_settings")
		self.assertIn("_INTEGRATION_CONFIGURATION_MAINTENANCE_TOKEN = object()", config)
		self.assertIn("try:", maintenance)
		self.assertIn("finally:", maintenance)
		self.assertIn("is _INTEGRATION_CONFIGURATION_MAINTENANCE_TOKEN", authorization)
		self.assertIn('doctype == "IONE Integration Settings"', ensure_settings)
		self.assertIn("with trusted_integration_configuration_maintenance():", ensure_settings)

	def test_message_and_event_audit_identity_is_immutable(self) -> None:
		hooks = (ROOT / "ione_qms" / "hooks.py").read_text(encoding="utf-8")
		validator = function_source(SCOPE, "validate_integration_audit_identity")
		index_validator = function_source(SCOPE, "validate_index_tenant_identity")
		self.assertGreaterEqual(hooks.count("validate_integration_audit_identity"), 2)
		self.assertIn("for_update=True", validator)
		for fieldname in (
			"source_system",
			"source_namespace",
			"endpoint",
			"payload_reference",
			"hospital",
			"scope_policy_hash",
			"idempotency_key",
		):
			self.assertIn(f'"{fieldname}"', SCOPE.read_text(encoding="utf-8"))
		self.assertIn('fields.append("patient")', index_validator)

	def test_patient_tenant_hospital_is_used_by_ai_and_safety_scope_resolution(self) -> None:
		ai_api = (ROOT / "ione_qms" / "api" / "ai.py").read_text(encoding="utf-8")
		quality_api = (ROOT / "ione_qms" / "api" / "quality.py").read_text(encoding="utf-8")
		self.assertIn('mappings["hospital"] = doc.get("tenant_hospital")', ai_api)
		self.assertIn('doc.get("tenant_hospital")', quality_api)


if __name__ == "__main__":
	unittest.main()
