from __future__ import annotations

import hashlib
from unittest import TestCase
from unittest.mock import patch

from ione_qms.services import scope_hierarchy


def _tenant_key(namespace: str, hospital: str, source_id: str) -> str:
	return hashlib.sha256(f"{namespace}|{hospital}|{source_id}".encode()).hexdigest()


class TestScopeHierarchy(TestCase):
	def test_patient_identity_requires_exact_source_namespace_and_tenant_key(self) -> None:
		patient = {
			"doctype": "IONE Patient Index",
			"name": "PATIENT-1",
			"tenant_hospital": "HOSPITAL-1",
			"source_system": "SOURCE-1",
			"source_namespace": "namespace-1",
			"source_patient_id": "source-patient-1",
			"identity_key_contract": "test-contract",
			"patient_key": _tenant_key("namespace-1", "HOSPITAL-1", "source-patient-1"),
		}

		def linked(doctype, _name, _fields):
			if doctype == "IONE Source System":
				return {"identity_namespace": "namespace-1"}
			return None

		with (
			patch.object(scope_hierarchy, "_linked_scope", side_effect=linked),
			patch.object(scope_hierarchy, "_record_exists", return_value=True),
			patch.object(
				scope_hierarchy,
				"verify_stored_identity_key",
				side_effect=lambda _kind, stored, contract, namespace, hospital, source_id: (
					contract == "test-contract" and stored == _tenant_key(namespace, hospital, source_id)
				),
			),
		):
			self.assertEqual(scope_hierarchy.clinical_scope_errors(patient), ())
			patient["source_namespace"] = "namespace-2"
			errors = scope_hierarchy.clinical_scope_errors(patient)
		self.assertIn("SOURCE_NAMESPACE_MISMATCH", errors)
		self.assertIn("PATIENT_KEY_MISMATCH", errors)

	def test_encounter_rejects_patient_from_another_hospital(self) -> None:
		encounter = {
			"doctype": "IONE Encounter Index",
			"name": "ENCOUNTER-1",
			"hospital": "HOSPITAL-1",
			"source_system": "SOURCE-1",
			"source_namespace": "namespace-1",
			"source_encounter_id": "source-encounter-1",
			"identity_key_contract": "test-contract",
			"encounter_key": _tenant_key("namespace-1", "HOSPITAL-1", "source-encounter-1"),
			"patient": "PATIENT-OTHER",
		}
		patient = {
			"name": "PATIENT-OTHER",
			"tenant_hospital": "HOSPITAL-2",
			"source_system": "SOURCE-1",
			"source_namespace": "namespace-1",
			"source_patient_id": "source-patient-2",
			"identity_key_contract": "test-contract",
			"patient_key": _tenant_key("namespace-1", "HOSPITAL-2", "source-patient-2"),
		}

		def linked(doctype, _name, _fields):
			if doctype == "IONE Source System":
				return {"identity_namespace": "namespace-1"}
			if doctype == "IONE Patient Index":
				return patient
			return None

		with (
			patch.object(scope_hierarchy, "_linked_scope", side_effect=linked),
			patch.object(scope_hierarchy, "_record_exists", return_value=True),
			patch.object(
				scope_hierarchy,
				"verify_stored_identity_key",
				side_effect=lambda _kind, stored, contract, namespace, hospital, source_id: (
					contract == "test-contract" and stored == _tenant_key(namespace, hospital, source_id)
				),
			),
		):
			errors = scope_hierarchy.clinical_scope_errors(encounter)
		self.assertIn("PATIENT_HOSPITAL_MISMATCH", errors)

	def test_ai_child_organization_must_match_its_task(self) -> None:
		child = {
			"doctype": "IONE AI Candidate Finding",
			"task": "TASK-1",
			"hospital": "HOSPITAL-2",
			"campus": "",
			"department": "",
			"ward": "",
			"patient": "",
			"encounter": "",
		}
		task = {
			"name": "TASK-1",
			"hospital": "HOSPITAL-1",
			"campus": "",
			"department": "",
			"ward": "",
			"patient": "",
			"encounter": "",
			"responsible_staff": "",
		}
		with (
			patch.object(scope_hierarchy, "_linked_scope", return_value=task),
			patch.object(scope_hierarchy, "_clinical_scope_errors", return_value=()),
		):
			errors = scope_hierarchy._ai_task_lineage_errors(
				child,
				task_field="task",
				allow_narrower_patient=False,
				visited=frozenset(),
			)
		self.assertIn("AI_TASK_HOSPITAL_MISMATCH", errors)

	def test_data_access_log_may_narrow_an_unpinned_task_but_not_change_a_pinned_patient(self) -> None:
		log = {
			"doctype": "IONE AI Data Access Log",
			"task": "TASK-1",
			"hospital": "HOSPITAL-1",
			"campus": "",
			"department": "",
			"ward": "",
			"patient": "PATIENT-2",
			"encounter": "",
		}
		task = {
			"name": "TASK-1",
			"hospital": "HOSPITAL-1",
			"campus": "",
			"department": "",
			"ward": "",
			"patient": "",
			"encounter": "",
			"responsible_staff": "",
		}
		with (
			patch.object(scope_hierarchy, "_linked_scope", return_value=task),
			patch.object(scope_hierarchy, "_clinical_scope_errors", return_value=()),
		):
			self.assertEqual(
				scope_hierarchy._ai_task_lineage_errors(
					log,
					task_field="task",
					allow_narrower_patient=True,
					visited=frozenset(),
				),
				(),
			)
			task["patient"] = "PATIENT-1"
			errors = scope_hierarchy._ai_task_lineage_errors(
				log,
				task_field="task",
				allow_narrower_patient=True,
				visited=frozenset(),
			)
		self.assertIn("AI_TASK_PATIENT_MISMATCH", errors)

	def test_identity_sql_is_fail_closed_and_never_uses_local_time(self) -> None:
		with (
			patch.object(
				scope_hierarchy,
				"acceptable_identity_key_contracts",
				return_value=("ione-qms-identity-key-v1:active", "ione-qms-identity-key-v1:old"),
			),
			patch.object(
				scope_hierarchy.frappe.db,
				"escape",
				side_effect=lambda value: f"'{value}'",
			),
		):
			patient = scope_hierarchy._patient_identity_sql("patient")
			encounter = scope_hierarchy._encounter_identity_sql("encounter")
		for predicate in (patient, encounter):
			self.assertNotIn("sha2(concat(", predicate)
			self.assertIn("identity_key_contract in", predicate)
			self.assertIn("ione-qms-identity-key-v1:active", predicate)
			self.assertIn("ione-qms-identity-key-v1:old", predicate)
			self.assertIn("regexp '^[0-9a-f]{64}$'", predicate)
			self.assertIn("identity_namespace", predicate)
			self.assertNotIn("now(", predicate.lower())
