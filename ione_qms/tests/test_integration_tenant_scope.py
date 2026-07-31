from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from ione_qms.integration.master_data import _stable_key
from ione_qms.integration.schemas import IncomingClinicalEvent
from ione_qms.integration.service import _idempotency_key
from ione_qms.services import integration_config, integration_scope
from ione_qms.services.identity_keys import identity_key_contract
from ione_qms.services.integration_config import (
	_require_named_integration_administrator,
	trusted_integration_configuration_maintenance,
)
from ione_qms.services.integration_scope import EndpointScopeViolationError


class TestIntegrationTenantScope(TestCase):
	def test_same_source_ids_are_partitioned_by_hospital(self) -> None:
		self.assertNotEqual(
			_stable_key("patient", "namespace-1", "HOSP-1", "P-1"),
			_stable_key("patient", "namespace-1", "HOSP-2", "P-1"),
		)
		incoming = IncomingClinicalEvent.from_payload(
			{
				"event_type": "Encounter.Updated",
				"event_time": "2026-07-30T00:00:00Z",
				"source_record_type": "Encounter",
				"source_record_id": "E-1",
				"source_version": "1",
				"payload": {},
			}
		)
		self.assertNotEqual(
			_idempotency_key("namespace-1", "HOSP-1", incoming),
			_idempotency_key("namespace-1", "HOSP-2", incoming),
		)
		self.assertEqual(
			_idempotency_key("namespace-1", "HOSP-1", incoming),
			_idempotency_key("namespace-1", "HOSP-1", incoming),
		)

	def test_multi_hospital_endpoint_requires_explicit_hospital(self) -> None:
		endpoint = frappe._dict(name="ENDPOINT-1")
		source = frappe._dict(name="SOURCE-1", identity_namespace="namespace-1")
		with (
			patch.object(integration_scope, "validate_endpoint_scope_policy"),
			patch.object(
				integration_scope,
				"validate_allowed_scopes",
				return_value=(("HOSP-1", "", "", ""), ("HOSP-2", "", "", "")),
			),
			patch.object(integration_scope, "canonicalize_scope", return_value={}),
			self.assertRaises(EndpointScopeViolationError),
		):
			integration_scope.authorize_endpoint_scope(endpoint, source, {})

	def test_patient_link_derives_hospital_and_namespace_before_message(self) -> None:
		def get_value(doctype, _name, _fields, *, as_dict=False):
			self.assertTrue(as_dict)
			if doctype == "IONE Patient Index":
				return frappe._dict(
					tenant_hospital="HOSP-2",
					source_namespace="namespace-2",
				)
			if doctype == "IONE Hospital":
				return frappe._dict(status="Active")
			raise AssertionError(doctype)

		with patch.object(integration_scope.frappe.db, "get_value", side_effect=get_value):
			scope = integration_scope.canonicalize_scope({"patient_index": "PATIENT-2"})
			self.assertEqual(scope["hospital"], "HOSP-2")
			self.assertEqual(scope["source_namespace"], "namespace-2")

	def test_encounter_link_rechecks_its_patient_tenant_before_message(self) -> None:
		def get_value(doctype, _name, _fields, *, as_dict=False):
			self.assertTrue(as_dict)
			if doctype == "IONE Encounter Index":
				return frappe._dict(
					patient="PATIENT-2",
					hospital="HOSP-2",
					source_namespace="namespace-2",
				)
			if doctype == "IONE Patient Index":
				return frappe._dict(
					tenant_hospital="HOSP-1",
					source_namespace="namespace-1",
				)
			raise AssertionError(doctype)

		with (
			patch.object(integration_scope.frappe.db, "get_value", side_effect=get_value),
			self.assertRaises(EndpointScopeViolationError),
		):
			integration_scope.canonicalize_scope({"encounter_index": "ENCOUNTER-2"})

	def test_endpoint_scope_row_requires_every_restricted_dimension(self) -> None:
		self.assertTrue(
			integration_scope._row_covers(
				("HOSP-1", "", "", ""),
				("HOSP-1", "CAMPUS-1", "DEPT-1", ""),
			)
		)
		self.assertFalse(
			integration_scope._row_covers(
				("HOSP-1", "", "DEPT-1", ""),
				("HOSP-1", "", "", ""),
			)
		)

	def test_administrator_cannot_bypass_configuration_role_with_ignore_permissions(self) -> None:
		with (
			patch.object(
				integration_config.frappe,
				"session",
				SimpleNamespace(user="Administrator"),
			),
			self.assertRaises(frappe.PermissionError),
		):
			_require_named_integration_administrator()

	def test_install_maintenance_token_is_scoped_and_restored(self) -> None:
		attribute = "ione_integration_configuration_maintenance"
		self.assertNotIn(attribute, frappe.flags)
		with trusted_integration_configuration_maintenance():
			_require_named_integration_administrator()
			self.assertIn(attribute, frappe.flags)
		self.assertNotIn(attribute, frappe.flags)

	def test_message_cannot_be_rebound_by_permission_ignoring_save(self) -> None:
		previous = frappe._dict(
			doctype="IONE Integration Message",
			name="MESSAGE-1",
			source_system="SOURCE-1",
			source_namespace="namespace-1",
			endpoint="ENDPOINT-1",
			hospital="HOSP-1",
			scope_policy_hash="policy-1",
			idempotency_key="identity-1",
		)
		changed = frappe._dict(previous)
		changed.is_new = lambda: False
		changed.hospital = "HOSP-2"
		with (
			patch.object(integration_scope.frappe, "get_doc", return_value=previous) as get_doc,
			self.assertRaises(frappe.PermissionError),
		):
			integration_scope.validate_integration_audit_identity(changed)
		get_doc.assert_called_once_with(
			"IONE Integration Message",
			"MESSAGE-1",
			for_update=True,
		)

	def test_encounter_patient_is_part_of_immutable_tenant_identity(self) -> None:
		source = frappe._dict(identity_namespace="namespace-1")
		encounter_key = _stable_key("encounter", "namespace-1", "HOSP-1", "E-1")
		contract = identity_key_contract()
		existing = frappe._dict(
			source_system="SOURCE-1",
			source_namespace="namespace-1",
			encounter_key=encounter_key,
			identity_key_contract=contract,
			hospital="HOSP-1",
			patient="PATIENT-1",
		)
		doc = frappe._dict(
			doctype="IONE Encounter Index",
			name="ENCOUNTER-1",
			source_system="SOURCE-1",
			source_namespace="namespace-1",
			source_encounter_id="E-1",
			encounter_key=encounter_key,
			identity_key_contract=contract,
			hospital="HOSP-1",
			patient="PATIENT-2",
		)
		doc.is_new = lambda: False
		with (
			patch.object(integration_scope.frappe.db, "get_value") as get_value,
			self.assertRaises(EndpointScopeViolationError),
		):
			get_value.side_effect = [source, existing]
			integration_scope.validate_index_tenant_identity(doc)

	def test_ai_task_rejects_patient_tenant_conflicts(self) -> None:
		from ione_qms.api.ai import _resolve_task_scope

		patient = frappe._dict(
			doctype="IONE Patient Index",
			name="PATIENT-1",
			tenant_hospital="HOSP-1",
		)
		patient.check_permission = lambda _permission: None
		for explicit_hospital, department in (("HOSP-2", None), (None, "DEPT-2")):
			with (
				self.subTest(explicit_hospital=explicit_hospital, department=department),
				patch("ione_qms.api.ai.frappe.get_doc", return_value=patient),
				patch(
					"ione_qms.api.ai.frappe.db.get_value",
					return_value=frappe._dict(hospital="HOSP-2", campus=None),
				),
				self.assertRaises(frappe.ValidationError),
			):
				_resolve_task_scope(
					hospital=explicit_hospital,
					campus=None,
					department=department,
					ward=None,
					patient=patient.name,
					encounter=None,
					indicator_result=None,
					finding=None,
				)

	def test_safety_report_rejects_patient_tenant_conflicts(self) -> None:
		from ione_qms.api.quality import _safety_report_scope

		patient = frappe._dict(
			doctype="IONE Patient Index",
			name="PATIENT-1",
			tenant_hospital="HOSP-1",
		)
		patient.check_permission = lambda _permission: None
		for explicit_hospital in ("HOSP-2", None):
			with (
				self.subTest(explicit_hospital=explicit_hospital),
				patch("ione_qms.api.quality.frappe.get_doc", return_value=patient),
				patch(
					"ione_qms.api.quality.frappe.db.get_value",
					return_value=frappe._dict(hospital="HOSP-2", campus=None),
				),
				self.assertRaises(frappe.ValidationError),
			):
				_safety_report_scope(
					hospital=explicit_hospital,
					campus=None,
					department="DEPT-2",
					ward=None,
					patient=patient.name,
					encounter=None,
				)

	def test_only_exact_reviewed_connector_marker_can_be_retained(self) -> None:
		from ione_qms.integration.connectors import ConnectorQuarantineRecord
		from ione_qms.integration.service import _retained_connector_marker

		endpoint = frappe._dict(connector_key="oracle")
		encoded = (
			'{"connector":"oracle","connector_quarantine":1,'
			'"content_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
			'"position":{"page_index":0},"reason_code":"ORACLE_ROW_ENVELOPE_INVALID",'
			'"size_bytes":12}'
		)
		marker = ConnectorQuarantineRecord(encoded)
		self.assertEqual(
			_retained_connector_marker(endpoint, marker, "InvalidConnectorRecord"),
			encoded,
		)

		class MalformedSubclass(ConnectorQuarantineRecord):
			pass

		for sentinel in (
			encoded,
			{"marker": encoded},
			MalformedSubclass(encoded),
			ConnectorQuarantineRecord(encoded[:-1] + ',"extra":"forbidden"}'),
		):
			with self.subTest(sentinel=type(sentinel).__name__):
				self.assertIsNone(_retained_connector_marker(endpoint, sentinel, "InvalidConnectorRecord"))

	def test_cross_endpoint_duplicate_keeps_first_endpoint_and_defers_inflight(self) -> None:
		from ione_qms.integration.service import _cross_endpoint_duplicate_outcome

		incoming = IncomingClinicalEvent.from_payload(
			{
				"event_type": "Encounter.Updated",
				"event_time": "2026-07-30T00:00:00Z",
				"source_record_type": "Encounter",
				"source_record_id": "E-1",
				"source_version": "1",
				"payload": {},
			}
		)
		source = frappe._dict(name="SOURCE-1", identity_namespace="namespace-1")
		later_endpoint = frappe._dict(name="ENDPOINT-2")
		for status, expected_deferred in (("Received", True), ("Processed", False)):
			message = frappe._dict(
				name="MESSAGE-1",
				status=status,
				source_system=source.name,
				source_namespace=source.identity_namespace,
				endpoint="ENDPOINT-1",
				hospital="HOSP-1",
				payload_hash="a" * 64,
				source_record_type=incoming.source_record_type,
				source_record_id=incoming.source_record_id,
				source_version=incoming.source_version,
			)
			with (
				self.subTest(status=status),
				patch.object(frappe.db, "advisory_lock", return_value=nullcontext()),
				patch.object(frappe, "get_doc", return_value=message),
			):
				outcome = _cross_endpoint_duplicate_outcome(
					message_name=message.name,
					endpoint=later_endpoint,
					source=source,
					tenant_hospital="HOSP-1",
					payload_hash="a" * 64,
					incoming=incoming,
				)
			self.assertEqual(message.endpoint, "ENDPOINT-1")
			self.assertTrue(outcome["duplicate"])
			self.assertEqual(outcome["deferred"], expected_deferred)
