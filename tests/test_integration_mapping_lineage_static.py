from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "ione_qms" / "integration" / "service.py"
CONFIG = ROOT / "ione_qms" / "services" / "integration_config.py"
TASKS = ROOT / "ione_qms" / "tasks" / "integration.py"
MIGRATIONS = ROOT / "ione_qms" / "tasks" / "migrations.py"
HOOKS = ROOT / "ione_qms" / "hooks.py"
GENERATOR = ROOT / "tools" / "generate_doctypes.py"


class TestIntegrationMappingLineageStatic(unittest.TestCase):
	def test_runtime_uses_frozen_mapping_record_not_endpoint_inline_json(self) -> None:
		service = SERVICE.read_text(encoding="utf-8")
		self.assertIn("resolve_endpoint_mapping(endpoint, source)", service)
		self.assertIn("frozen_mapping_from_audit_record(message)", service)
		self.assertIn("mapping_definition.values", service)
		self.assertIn("master_data_config=mapping_definition.master_data", service)
		self.assertNotIn('endpoint.get("mapping")', service)
		self.assertNotIn("def _json_dict(", service)
		self.assertIn("retry_clinical_event_message", service)

	def test_enabled_endpoint_fails_closed_without_exact_published_mapping(self) -> None:
		config = CONFIG.read_text(encoding="utf-8")
		self.assertIn("Enabled integration endpoints require a governed Mapping Record", config)
		self.assertIn("cannot coexist with Mapping Record", config)
		self.assertIn("must be Active", config)
		self.assertIn("outside its effective period", config)
		self.assertIn("Published integration mapping content is immutable", config)
		self.assertIn("create a new version instead", config)
		self.assertIn("Enabled endpoints cannot define connector_options.master_data", config)
		self.assertIn('stored_snapshot=mapping_doc.get("canonical_snapshot")', config)

	def test_message_and_event_freeze_complete_mapping_lineage(self) -> None:
		generator = GENERATOR.read_text(encoding="utf-8")
		for fieldname in (
			"mapping_record",
			"mapping_code",
			"mapping_version",
			"mapping_checksum",
			"mapping_snapshot",
		):
			self.assertGreaterEqual(generator.count(f'"{fieldname}"'), 2)
		self.assertIn('"mapping_snapshot",\n\t\t\t\t"Long Text"', generator)
		self.assertIn("permlevel=1", generator)
		hooks = HOOKS.read_text(encoding="utf-8")
		self.assertGreaterEqual(hooks.count("validate_mapping_lineage_identity"), 2)
		self.assertIn("prevent_integration_mapping_deletion", hooks)

	def test_operational_jobs_store_hashes_and_require_identity_capability(self) -> None:
		tasks = TASKS.read_text(encoding="utf-8")
		self.assertIn('"cursor_before_hash": _cursor_fingerprint(cursor)', tasks)
		self.assertIn('"cursor_after_hash": _cursor_fingerprint(cursor)', tasks)
		self.assertNotIn('"cursor_before": cursor', tasks)
		self.assertNotIn('"cursor_after": cursor', tasks)
		self.assertIn("trusted_integration_runtime_updates()", tasks)
		config = CONFIG.read_text(encoding="utf-8")
		self.assertIn("is not _INTEGRATION_RUNTIME_UPDATE_TOKEN", config)
		self.assertIn("_JOB_FINISH_FIELDS", config)
		self.assertIn("may only transition once", config)

	def test_message_runtime_updates_lock_and_cannot_rebind_or_restore_payload(self) -> None:
		service = SERVICE.read_text(encoding="utf-8")
		self.assertIn(
			'frappe.get_doc("IONE Integration Message", message.name, for_update=True)',
			service,
		)
		self.assertIn("Integration message {fieldname} is immutable after insertion", service)
		self.assertIn("cannot be rebound after resolution", service)
		self.assertIn("may only be replaced by its SHA-256 digest", service)
		self.assertIn("_validate_runtime_quarantine_manifest(", service)
		self.assertIn('set(manifest) != {"_ione_qms_quarantine"}', service)
		self.assertIn("hmac.compare_digest(value, _canonical_json(manifest))", service)

	def test_legacy_mapping_migration_is_bounded_reentrant_and_sanitized(self) -> None:
		migrations = MIGRATIONS.read_text(encoding="utf-8")
		self.assertIn("coalesce(trim(endpoint.mapping_record), '') = ''", migrations)
		self.assertIn("coalesce(trim(endpoint.mapping), '') <> ''", migrations)
		self.assertIn("LEGACY_INLINE_MASTER_DATA_PRESENT", migrations)
		self.assertIn("mapping.master_data_json", migrations)
		self.assertIn("mapping.canonical_snapshot", migrations)
		self.assertIn("sha2(mapping.canonical_snapshot, 256) <> mapping.checksum", migrations)
		self.assertIn("order by endpoint.name asc", migrations)
		self.assertIn("limit %s", migrations)
		self.assertIn("len(enabled_endpoints) >= limit", migrations)
		self.assertIn("_log_integration_endpoint_migration_block", migrations)
		self.assertIn("mapping content and clinical data were not logged", migrations)
		self.assertIn("json_quote(coalesce(mapping.source_system, ''))", migrations)
		self.assertIn("json_quote(coalesce(mapping.endpoint, ''))", migrations)
		self.assertIn("SOURCE_NAMESPACE_MISSING", migrations)
		self.assertNotIn('f"{endpoint.mapping', migrations)

	def test_master_data_identity_is_one_to_one_and_conflicts_fail_closed(self) -> None:
		master_data = (ROOT / "ione_qms" / "integration" / "master_data.py").read_text(encoding="utf-8")
		self.assertIn("entity.casefold()", master_data)
		self.assertIn("source_patient_id != incoming.source_record_id", master_data)
		self.assertIn("source_encounter_id != incoming.source_record_id", master_data)
		self.assertIn("Existing encounter cannot be rebound", master_data)
		self.assertIn("_merge_reference(resolved, fieldname, value)", master_data)

	def test_terminal_processed_receipt_is_never_reauthorized_or_sanitized(self) -> None:
		service = SERVICE.read_text(encoding="utf-8")
		processed_check = service.index('if message.status == "Processed":')
		preauthorization = service.index("preauthorized_scope = _preauthorize_scope(", processed_check)
		self.assertLess(processed_check, preauthorization)
		self.assertIn("Processed integration message history is immutable", service)
		self.assertIn("cannot be rebound to another event", service)

	def test_generated_metadata_hides_raw_endpoint_cursor_and_job_is_read_only(self) -> None:
		endpoint_path = (
			ROOT
			/ "ione_qms"
			/ "ione_integration"
			/ "doctype"
			/ "ione_integration_endpoint"
			/ "ione_integration_endpoint.json"
		)
		job_path = (
			ROOT
			/ "ione_qms"
			/ "ione_integration"
			/ "doctype"
			/ "ione_integration_job"
			/ "ione_integration_job.json"
		)
		mapping_path = (
			ROOT
			/ "ione_qms"
			/ "ione_integration"
			/ "doctype"
			/ "ione_integration_mapping"
			/ "ione_integration_mapping.json"
		)
		endpoint = json.loads(endpoint_path.read_text(encoding="utf-8"))
		cursor = next(field for field in endpoint["fields"] if field["fieldname"] == "cursor")
		self.assertEqual(cursor["read_only"], 1)
		self.assertEqual(cursor["permlevel"], 1)
		self.assertEqual(cursor["hidden"], 1)
		job = json.loads(job_path.read_text(encoding="utf-8"))
		fieldnames = {field["fieldname"] for field in job["fields"]}
		self.assertIn("cursor_before_hash", fieldnames)
		self.assertIn("cursor_after_hash", fieldnames)
		self.assertNotIn("cursor_before", fieldnames)
		self.assertNotIn("cursor_after", fieldnames)
		self.assertTrue(all(not row.get("write") and not row.get("create") for row in job["permissions"]))
		mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
		fields = {field["fieldname"]: field for field in mapping["fields"]}
		self.assertEqual(fields["master_data_json"]["permlevel"], 1)
		self.assertEqual(fields["canonical_snapshot"]["permlevel"], 1)
		self.assertEqual(fields["canonical_snapshot"]["read_only"], 1)


if __name__ == "__main__":
	unittest.main()
