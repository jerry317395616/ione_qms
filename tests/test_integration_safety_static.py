from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "ione_qms" / "api" / "integration.py"
SERVICE = ROOT / "ione_qms" / "integration" / "service.py"
MASTER = ROOT / "ione_qms" / "integration" / "master_data.py"
TASKS = ROOT / "ione_qms" / "tasks" / "integration.py"
INSTALL = ROOT / "ione_qms" / "setup" / "install.py"
K6 = ROOT / "tests" / "performance" / "k6-ingestion.js"


def function_source(path: Path, name: str) -> str:
	source = path.read_text(encoding="utf-8")
	tree = ast.parse(source)
	for node in tree.body:
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
			return ast.get_source_segment(source, node) or ""
	raise AssertionError(f"{name} was not found in {path}")


class TestIntegrationSafetySourceContract(unittest.TestCase):
	def test_guest_push_route_forces_exact_body_hmac(self) -> None:
		source = API.read_text(encoding="utf-8")
		self.assertIn('@frappe.whitelist(allow_guest=True, methods=["POST"])', source)
		receive = function_source(API, "receive_event")
		self.assertIn("force_signature=guest_request", receive)
		self.assertIn("_require_json_request(endpoint_doc, force=guest_request)", receive)

	def test_master_projection_has_entity_lock_and_monotonic_cas(self) -> None:
		source = MASTER.read_text(encoding="utf-8")
		patient_upsert = function_source(MASTER, "_upsert_patient")
		encounter_upsert = function_source(MASTER, "_upsert_encounter")
		identity_load = function_source(MASTER, "_load_or_new_identity")
		self.assertIn("source_version_strategy", source)
		self.assertIn("UTC_DATETIME_SOURCE_VERSION", source)
		self.assertIn("datetime.fromisoformat", function_source(MASTER, "_source_version_key"))
		self.assertNotIn("get_datetime(", function_source(MASTER, "_source_version_key"))
		for upsert in (patient_upsert, encounter_upsert):
			self.assertIn("identity_key_candidates(", upsert)
			self.assertIn("with _identity_entity_locks(", upsert)
			self.assertIn("_load_or_new_identity(", upsert)
		self.assertGreaterEqual(encounter_upsert.count("identity_key_candidates("), 2)
		self.assertIn('"patient_key"', encounter_upsert)
		self.assertNotIn('"source_patient_id": patient_source_id', encounter_upsert)
		self.assertIn("for_update=True", identity_load)
		self.assertIn("_stable_source_identity_filters(", identity_load)
		self.assertIn("stable_rows", identity_load)
		self.assertIn("verify_stored_identity_key(", identity_load)
		self.assertIn("Active identity key collides", identity_load)
		transaction_lock = function_source(MASTER, "_acquire_transaction_identity_lock")
		self.assertIn("frappe.db.before_commit.add", transaction_lock)
		self.assertIn("frappe.db.after_commit.add", transaction_lock)
		self.assertIn("frappe.db.after_rollback.add", transaction_lock)
		self.assertIn("get_lock(%s, %s)", transaction_lock.lower())
		self.assertIn("release_lock(%s)", transaction_lock.lower())
		entity_locks = function_source(MASTER, "_identity_entity_locks")
		self.assertIn('"source_system"', entity_locks)
		self.assertIn('"source_namespace"', entity_locks)
		self.assertIn('"tenant_hospital"', entity_locks)
		self.assertIn('"source_id"', entity_locks)
		self.assertIn("stable_token", entity_locks)
		self.assertIn("StaleSourceVersionError", function_source(MASTER, "_should_apply_source_version"))
		self.assertIn("SourceVersionConflictError", function_source(MASTER, "_should_apply_source_version"))
		resolve = function_source(MASTER, "resolve_and_materialize_indexes")
		self.assertIn("include_encounter=not materializing_encounter", resolve)

	def test_duplicate_and_oversized_payloads_use_bounded_current_read_quarantine(self) -> None:
		source = SERVICE.read_text(encoding="utf-8")
		quarantine = function_source(SERVICE, "_quarantine_invalid_payload")
		current_read = function_source(SERVICE, "_message_by_idempotency_key")
		self.assertEqual(quarantine.count("_quarantine_invalid_payload("), 1)
		self.assertIn("for update", current_read.lower())
		self.assertIn("_insert_message(", source)
		self.assertIn("rollback(save_point=savepoint)", function_source(SERVICE, "_insert_message"))
		self.assertIn("MAX_PAYLOAD_BYTES", function_source(SERVICE, "receive_clinical_event"))
		manifest = function_source(SERVICE, "_oversized_payload_manifest")
		self.assertIn('"payload_retained": False', manifest)
		self.assertNotIn("payload_json", manifest)

	def test_database_guards_make_raw_source_identity_unique_across_key_versions(self) -> None:
		guard = function_source(INSTALL, "ensure_identity_source_uniqueness")
		for fieldname in (
			"source_system",
			"source_namespace",
			"tenant_hospital",
			"source_patient_id",
			"hospital",
			"source_encounter_id",
		):
			self.assertIn(fieldname, guard)
		self.assertIn("stable_source_identity_key", guard)
		self.assertIn("stable_source_key", guard)
		self.assertIn("having count(*) > 1", guard)
		self.assertIn("frappe.db.add_unique", guard)
		self.assertIn("uniq_ione_patient_exact_source_identity", guard)
		self.assertIn("uniq_ione_encounter_exact_source_identity", guard)

	def test_schema_failure_is_persisted_before_pull_cursor_can_advance(self) -> None:
		receive = function_source(SERVICE, "receive_clinical_event")
		quarantine = function_source(SERVICE, "_quarantine_invalid_payload")
		sync = function_source(TASKS, "_sync_endpoint")
		self.assertIn("_quarantine_invalid_payload(", receive)
		self.assertIn('"status": "Dead Letter"', quarantine)
		self.assertIn("quarantine_invalid_clinical_event(", sync)
		self.assertIn('"Partial" if result["quarantined"] else "Completed"', sync)
		self.assertIn("advance_values", sync)
		self.assertIn("frappe.db.commit()", sync)
		self.assertIn("frappe.db.rollback()", sync)

	def test_k6_uses_schema_event_time_and_aborting_preflight(self) -> None:
		source = K6.read_text(encoding="utf-8")
		self.assertIn("event_time:", source)
		self.assertNotIn("occurred_at:", source)
		self.assertIn("export function setup()", source)
		self.assertIn("load phase was not started", source)


if __name__ == "__main__":
	unittest.main()
