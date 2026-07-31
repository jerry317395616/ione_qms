from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from ione_qms.integration import master_data as integration_master_data
from ione_qms.integration import service as integration_service
from ione_qms.integration.mapping import (
	apply_mapping,
	canonicalize_mapping,
	frozen_mapping_definition,
	mapping_definition,
	normalize_master_data_configuration,
)
from ione_qms.integration.schemas import IncomingClinicalEvent
from ione_qms.integration.service import (
	_hashed_identity,
	_scope_violation_manifest,
	_update_message,
)
from ione_qms.services import integration_config


class TestIntegrationMappingLineage(TestCase):
	def test_mapping_snapshot_is_canonical_bounded_and_checksum_verified(self) -> None:
		values, master_data, snapshot, checksum = canonicalize_mapping(
			{
				"hospital": {"constant": "HOSP-1"},
				"department": "payload.department",
			}
		)
		self.assertEqual(
			snapshot,
			'{"master_data":{},"scope":{"department":"payload.department","hospital":{"constant":"HOSP-1"}}}',
		)
		self.assertEqual(master_data, {})
		self.assertEqual(checksum, hashlib.sha256(snapshot.encode()).hexdigest())
		self.assertEqual(
			apply_mapping({"payload": {"department": "DEPT-1"}}, values),
			{"department": "DEPT-1", "hospital": "HOSP-1"},
		)
		definition = frozen_mapping_definition(
			record="MAP-1",
			code="clinical-scope",
			version="1.0.0",
			checksum=checksum,
			snapshot=snapshot,
		)
		self.assertEqual(definition.values, values)
		with self.assertRaisesRegex(ValueError, "checksum"):
			frozen_mapping_definition(
				record="MAP-1",
				code="clinical-scope",
				version="1.0.0",
				checksum="0" * 64,
				snapshot=snapshot,
			)

	def test_mapping_rejects_ambiguous_constants_and_noncanonical_snapshot(self) -> None:
		with self.assertRaisesRegex(ValueError, "contain only"):
			canonicalize_mapping({"hospital": {"constant": "HOSP-1", "fallback": "HOSP-2"}})
		noncanonical = '{ "master_data": {}, "scope": {"hospital": "scope.hospital"} }'
		canonical = '{"master_data":{},"scope":{"hospital":"scope.hospital"}}'
		with self.assertRaisesRegex(ValueError, "not canonical"):
			frozen_mapping_definition(
				record="MAP-1",
				code="clinical-scope",
				version="1",
				checksum=hashlib.sha256(canonical.encode()).hexdigest(),
				snapshot=noncanonical,
			)

	def test_master_data_mapping_has_one_strict_canonical_schema(self) -> None:
		normalized = normalize_master_data_configuration(
			{
				"fields": {
					"status": {"constant": "Active"},
					"source_patient_id": "patient.identifier",
				},
				"source_version_strategy": "integer",
				"entity": "Patient",
				"enabled": True,
			}
		)
		self.assertEqual(
			normalized,
			{
				"enabled": 1,
				"entity": "Patient",
				"fields": {
					"status": {"constant": "Active"},
					"source_patient_id": "patient.identifier",
				},
				"source_version_strategy": "integer",
			},
		)
		with self.assertRaisesRegex(ValueError, "constant is not allowed"):
			normalize_master_data_configuration(
				{
					"enabled": 1,
					"entity": "Patient",
					"source_version_strategy": "integer",
					"fields": {"source_patient_id": {"constant": "same-patient"}},
				}
			)

	def test_master_data_entity_and_source_identity_cannot_be_rebound(self) -> None:
		incoming = IncomingClinicalEvent.from_payload(
			{
				"event_type": "Patient.Updated",
				"event_time": "2026-07-30T00:00:00Z",
				"source_record_type": "Encounter",
				"source_record_id": "E-1",
				"source_version": "1",
				"payload": {"patient_id": "P-2"},
			}
		)
		with (
			patch.object(integration_master_data, "_resolve_existing_references", return_value={}),
			self.assertRaisesRegex(
				integration_master_data.SourceVersionConflictError,
				"must match the incoming source record type",
			),
		):
			integration_master_data.resolve_and_materialize_indexes(
				endpoint=frappe._dict(),
				source=frappe._dict(name="SOURCE-1"),
				incoming=incoming,
				payload_hash="a" * 64,
				tenant_hospital="HOSP-1",
				source_namespace="namespace-1",
				master_data_config={
					"enabled": 1,
					"entity": "Patient",
					"source_version_strategy": "integer",
					"fields": {"source_patient_id": "patient_id"},
				},
			)

		incoming = IncomingClinicalEvent.from_payload(
			{
				"event_type": "Patient.Updated",
				"event_time": "2026-07-30T00:00:00Z",
				"source_record_type": "Patient",
				"source_record_id": "P-1",
				"source_version": "1",
				"payload": {"patient_id": "P-2"},
			}
		)
		with self.assertRaisesRegex(
			integration_master_data.SourceVersionConflictError,
			"must match the incoming source record identity",
		):
			integration_master_data._upsert_patient(
				"SOURCE-1",
				{"source_patient_id": "P-2"},
				incoming,
				tenant_hospital="HOSP-1",
				source_namespace="namespace-1",
				version_strategy="integer",
				payload_hash="a" * 64,
			)

	def test_identity_named_lock_is_released_only_by_transaction_callbacks(self) -> None:
		class _Callbacks:
			def __init__(self) -> None:
				self.functions = []

			def add(self, callback) -> None:
				self.functions.append(callback)

		class _DB:
			def __init__(self) -> None:
				self.before_commit = _Callbacks()
				self.after_commit = _Callbacks()
				self.after_rollback = _Callbacks()
				self.calls = []

			def sql(self, query, params):
				self.calls.append((query.lower(), params))
				return [(1,)]

		database = _DB()
		fake_frappe = SimpleNamespace(
			db=database,
			flags=SimpleNamespace(),
		)
		with patch.object(integration_master_data, "frappe", fake_frappe):
			integration_master_data._acquire_transaction_identity_lock(
				"ione-qms:master-data:patient:secret-source-id"
			)
			integration_master_data._acquire_transaction_identity_lock(
				"ione-qms:master-data:patient:secret-source-id"
			)
			self.assertEqual(
				sum("get_lock" in query for query, _params in database.calls),
				1,
			)
			self.assertEqual(
				sum("release_lock" in query for query, _params in database.calls),
				0,
			)
			self.assertEqual(len(database.after_commit.functions), 1)
			self.assertEqual(len(database.after_rollback.functions), 1)
			self.assertEqual(len(database.before_commit.functions), 1)
			database.after_commit.functions[0]()
			self.assertEqual(
				sum("release_lock" in query for query, _params in database.calls),
				1,
			)
			self.assertNotIn(
				"secret-source-id",
				str(database.calls),
			)

	def test_identity_named_lock_is_rearmed_for_sql_commit_failure_rollback(self) -> None:
		class _Callbacks:
			def __init__(self) -> None:
				self.functions = []

			def add(self, callback) -> None:
				self.functions.append(callback)

		class _DB:
			def __init__(self) -> None:
				self.before_commit = _Callbacks()
				self.after_commit = _Callbacks()
				self.after_rollback = _Callbacks()
				self.calls = []

			def sql(self, query, params):
				self.calls.append((query.lower(), params))
				return [(1,)]

		database = _DB()
		fake_frappe = SimpleNamespace(db=database, flags=SimpleNamespace())
		with patch.object(integration_master_data, "frappe", fake_frappe):
			integration_master_data._acquire_transaction_identity_lock(
				"ione-qms:master-data:patient:commit-failure"
			)
			# Mirror Frappe Database.commit(): it clears rollback callbacks,
			# then runs before_commit before the SQL COMMIT that fails.
			database.after_rollback.functions.clear()
			database.before_commit.functions.pop(0)()
			self.assertEqual(len(database.after_rollback.functions), 1)
			# Mirror the request wrapper's subsequent rollback callback.
			database.after_rollback.functions.pop(0)()
			self.assertEqual(
				sum("release_lock" in query for query, _params in database.calls),
				1,
			)

	def test_removed_retained_key_still_finds_raw_source_tuple_and_blocks_duplicate(self) -> None:
		old = frappe._dict(
			name="PATIENT-OLD",
			patient_key="a" * 64,
			identity_key_contract="ione-qms-identity-key-v1:removed",
			source_system="SOURCE-1",
			source_namespace="namespace-1",
			tenant_hospital="HOSP-1",
			source_patient_id="P-1",
		)
		with (
			patch.object(
				integration_master_data.frappe,
				"get_all",
				side_effect=[[], [old]],
			),
			patch.object(
				integration_master_data,
				"_binary_stable_source_identity_rows",
				return_value=[],
			),
			patch.object(integration_master_data.frappe, "get_doc", return_value=old),
			patch.object(
				integration_master_data,
				"verify_stored_identity_key",
				return_value=False,
			),
			self.assertRaisesRegex(
				integration_master_data.SourceVersionConflictError,
				"retained-key verification",
			),
		):
			integration_master_data._load_or_new_identity(
				"IONE Patient Index",
				key_field="patient_key",
				key_kind="patient",
				key_candidates=(("ione-qms-identity-key-v1:new", "b" * 64),),
				source_system="SOURCE-1",
				source_namespace="namespace-1",
				tenant_hospital="HOSP-1",
				source_id="P-1",
				values={"patient_key": "b" * 64},
			)

	def test_enabled_endpoint_rejects_inline_master_data_but_disabled_legacy_is_retained(
		self,
	) -> None:
		source = frappe._dict(name="SOURCE-1", read_only=1)
		common = {
			"doctype": "IONE Integration Endpoint",
			"name": "ENDPOINT-1",
			"source_system": source.name,
			"direction": "Inbound",
			"connector_options": '{"master_data":{}}',
		}
		enabled = frappe._dict(common, enabled=1)
		enabled.is_new = lambda: True
		disabled = frappe._dict(common, enabled=0)
		disabled.is_new = lambda: True
		with (
			patch.object(integration_config.frappe, "get_cached_doc", return_value=source),
			patch.object(integration_config, "validate_endpoint_scope_policy"),
			self.assertRaisesRegex(
				frappe.ValidationError,
				"cannot define connector_options.master_data",
			),
		):
			integration_config.validate_integration_endpoint(enabled)
		with (
			patch.object(integration_config.frappe, "get_cached_doc", return_value=source),
			patch.object(integration_config, "validate_endpoint_scope_policy"),
		):
			integration_config.validate_integration_endpoint(disabled)

	def test_endpoint_resolves_only_its_exact_published_mapping(self) -> None:
		mapping_json = '{"hospital":"scope.hospital"}'
		definition = mapping_definition(
			record="MAP-1",
			code="clinical-scope",
			version="1",
			mapping=mapping_json,
		)
		source = frappe._dict(name="SOURCE-1")
		endpoint = frappe._dict(name="ENDPOINT-1", mapping_record="MAP-1")
		mapping_doc = frappe._dict(
			name="MAP-1",
			mapping_code=definition.code,
			version=definition.version,
			mapping_json=json.dumps(definition.values, sort_keys=True, separators=(",", ":")),
			master_data_json="{}",
			canonical_snapshot=definition.snapshot,
			checksum=definition.checksum,
			mapping_version_key=integration_config._mapping_version_key(
				source.name,
				endpoint.name,
				definition.code,
				definition.version,
			),
			source_system=source.name,
			endpoint=endpoint.name,
			status="Active",
			effective_from="2020-01-01",
			effective_to=None,
		)
		with patch.object(integration_config.frappe, "get_doc", return_value=mapping_doc):
			resolved = integration_config.resolve_endpoint_mapping(endpoint, source)
		self.assertEqual(resolved, definition)
		mapping_doc.checksum = "0" * 64
		with (
			patch.object(integration_config.frappe, "get_doc", return_value=mapping_doc),
			self.assertRaises(frappe.ValidationError),
		):
			integration_config.resolve_endpoint_mapping(endpoint, source)

	def test_active_mapping_content_cannot_change_in_place(self) -> None:
		original = mapping_definition(
			record="MAP-1",
			code="clinical-scope",
			version="1",
			mapping={"hospital": "scope.hospital"},
		)
		version_key = integration_config._mapping_version_key(
			"SOURCE-1",
			"ENDPOINT-1",
			original.code,
			original.version,
		)
		previous = frappe._dict(
			name="MAP-1",
			mapping_code=original.code,
			source_system="SOURCE-1",
			endpoint="ENDPOINT-1",
			version=original.version,
			mapping_json=json.dumps(original.values, sort_keys=True, separators=(",", ":")),
			master_data_json="{}",
			canonical_snapshot=original.snapshot,
			checksum=original.checksum,
			mapping_version_key=version_key,
			effective_from="2020-01-01",
			effective_to=None,
			status="Active",
		)
		changed = frappe._dict(previous)
		changed.mapping_json = '{"hospital":"other.hospital"}'
		changed.is_new = lambda: False
		endpoint = frappe._dict(name="ENDPOINT-1", source_system="SOURCE-1")
		with (
			patch.object(integration_config.frappe, "get_cached_doc", return_value=endpoint),
			patch.object(integration_config.frappe, "get_doc", return_value=previous),
			self.assertRaisesRegex(frappe.ValidationError, "create a new version"),
		):
			integration_config.validate_integration_mapping(changed)

	def test_retry_processing_uses_receipt_snapshot_not_current_endpoint_mapping(self) -> None:
		frozen = mapping_definition(
			record="MAP-OLD",
			code="clinical-scope",
			version="1",
			mapping={"hospital": "old_scope.hospital"},
			master_data={
				"enabled": 1,
				"entity": "Patient",
				"source_version_strategy": "integer",
				"fields": {"source_patient_id": "patient.id"},
			},
		)
		current = mapping_definition(
			record="MAP-NEW",
			code="clinical-scope",
			version="2",
			mapping={"hospital": "new_scope.hospital"},
			master_data={
				"enabled": 1,
				"entity": "Encounter",
				"source_version_strategy": "datetime",
				"fields": {"source_encounter_id": "encounter.id"},
			},
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
		source = frappe._dict(name="SOURCE-1", identity_namespace="namespace-1")
		endpoint = frappe._dict(
			name="ENDPOINT-1",
			retry_count=5,
			mapping_record=current.record,
			connector_options=json.dumps({"master_data": current.master_data}),
		)
		message = frappe._dict(
			name="MESSAGE-1",
			status="Received",
			source_system=source.name,
			endpoint=endpoint.name,
			source_namespace=source.identity_namespace,
			hospital="HOSP-1",
			scope_policy_hash="a" * 64,
			source_record_type=incoming.source_record_type,
			source_record_id=incoming.source_record_id,
			source_version=incoming.source_version,
		)
		with (
			patch.object(frappe.db, "advisory_lock", return_value=nullcontext()),
			patch.object(frappe, "get_doc", return_value=message),
			patch.object(
				integration_service,
				"frozen_mapping_from_audit_record",
				return_value=frozen,
			),
			patch.object(
				integration_service,
				"_preauthorize_scope",
				return_value={"hospital": "HOSP-1"},
			) as preauthorize,
			patch.object(
				integration_service,
				"_process_message",
				return_value={"processed": True},
			) as process,
			patch.object(integration_service, "resolve_endpoint_mapping") as resolve_current,
		):
			outcome = integration_service._process_with_lock(
				message_name=message.name,
				endpoint=endpoint,
				source=source,
				incoming=incoming,
				payload={"old_scope": {"hospital": "HOSP-1"}},
				policy_hash=message.scope_policy_hash,
				duplicate=True,
			)
		self.assertTrue(outcome["processed"])
		self.assertEqual(preauthorize.call_args.args[4], frozen.values)
		self.assertEqual(process.call_args.kwargs["mapping_definition"], frozen)
		self.assertEqual(process.call_args.kwargs["mapping_definition"].master_data, frozen.master_data)
		self.assertNotEqual(frozen.master_data, current.master_data)
		resolve_current.assert_not_called()

	def test_processed_receipt_is_not_reopened_when_current_scope_policy_changes(self) -> None:
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
		endpoint = frappe._dict(name="ENDPOINT-1")
		message = frappe._dict(
			name="MESSAGE-1",
			status="Processed",
			event="EVENT-1",
			source_system=source.name,
			endpoint=endpoint.name,
			source_namespace=source.identity_namespace,
		)
		with (
			patch.object(frappe.db, "advisory_lock", return_value=nullcontext()),
			patch.object(frappe, "get_doc", return_value=message),
			patch.object(
				integration_service,
				"frozen_mapping_from_audit_record",
				return_value=mapping_definition(
					record="MAP-1",
					code="clinical-scope",
					version="1",
					mapping={"hospital": "scope.hospital"},
				),
			),
			patch.object(
				integration_service,
				"_preauthorize_scope",
				side_effect=AssertionError("processed receipt must not be reauthorized"),
			),
			patch.object(integration_service, "reject_retained_message_scope") as reject,
		):
			outcome = integration_service._process_with_lock(
				message_name=message.name,
				endpoint=endpoint,
				source=source,
				incoming=incoming,
				payload={"scope": {"hospital": "HOSP-2"}},
				policy_hash="changed-policy",
				duplicate=True,
			)
		self.assertTrue(outcome["processed"])
		reject.assert_not_called()

	def test_runtime_update_token_is_identity_scoped_and_restored(self) -> None:
		attribute = "ione_integration_runtime_update"
		previous_exists = attribute in frappe.flags
		previous = frappe.flags.get(attribute)
		if previous_exists:
			frappe.flags.pop(attribute, None)
		try:
			with self.assertRaises(frappe.PermissionError):
				integration_config._require_runtime_update_token()
			with integration_config.trusted_integration_runtime_updates():
				integration_config._require_runtime_update_token()
				self.assertIn(attribute, frappe.flags)
			self.assertNotIn(attribute, frappe.flags)
		finally:
			if previous_exists:
				setattr(frappe.flags, attribute, previous)
			else:
				frappe.flags.pop(attribute, None)

	def test_job_audit_cannot_be_created_without_runtime_capability(self) -> None:
		job = frappe._dict(
			doctype="IONE Integration Job",
			name="JOB-1",
			endpoint="ENDPOINT-1",
			source_system="SOURCE-1",
			status="Running",
			started_at="2026-07-30 00:00:00",
			requested_batch_size=100,
			cursor_before_hash="a" * 64,
		)
		job.is_new = lambda: True
		with self.assertRaises(frappe.PermissionError):
			integration_config.validate_integration_job(job)
		with (
			integration_config.trusted_integration_runtime_updates(),
			patch.object(
				integration_config.frappe.db,
				"get_value",
				return_value="SOURCE-1",
			),
		):
			integration_config.validate_integration_job(job)

	def test_job_audit_rejects_terminal_values_at_start_and_unreconciled_counts(self) -> None:
		job = frappe._dict(
			doctype="IONE Integration Job",
			name="JOB-1",
			endpoint="ENDPOINT-1",
			source_system="SOURCE-1",
			status="Running",
			started_at="2026-07-30 00:00:00",
			requested_batch_size=100,
			cursor_before_hash="a" * 64,
			completed_at="2026-07-30 00:01:00",
		)
		job.is_new = lambda: True
		with (
			integration_config.trusted_integration_runtime_updates(),
			patch.object(integration_config.frappe.db, "get_value", return_value="SOURCE-1"),
			self.assertRaisesRegex(frappe.ValidationError, "terminal audit values"),
		):
			integration_config.validate_integration_job(job)

		previous = frappe._dict(
			job,
			status="Running",
			completed_at=None,
			cursor_after_hash=None,
			received_count=0,
			success_count=0,
			duplicate_count=0,
			quarantined_count=0,
			error_count=0,
			error_message=None,
		)
		terminal = frappe._dict(
			previous,
			status="Partial",
			completed_at="2026-07-30 00:01:00",
			received_count=3,
			success_count=2,
			error_count=0,
		)
		terminal.is_new = lambda: False
		with (
			integration_config.trusted_integration_runtime_updates(),
			patch.object(integration_config.frappe, "get_doc", return_value=previous),
			self.assertRaisesRegex(frappe.ValidationError, "do not reconcile"),
		):
			integration_config.validate_integration_job(terminal)

	def test_message_update_locks_current_row_and_rejects_stale_tenant_rebind(self) -> None:
		stale = frappe._dict(name="MESSAGE-1", hospital="HOSP-2")
		current = frappe._dict(
			name="MESSAGE-1",
			hospital="HOSP-1",
			scope_policy_hash="a" * 64,
			payload_hash="b" * 64,
		)
		current.meta = SimpleNamespace(has_field=lambda _fieldname: True)
		current.db_set = MagicMock()
		with (
			patch.object(
				frappe,
				"get_doc",
				return_value=current,
			) as get_doc,
			self.assertRaises(frappe.PermissionError),
		):
			_update_message(stale, {"hospital": "HOSP-2"})
		get_doc.assert_called_once_with(
			"IONE Integration Message",
			"MESSAGE-1",
			for_update=True,
		)
		current.db_set.assert_not_called()

	def test_message_update_only_accepts_hash_bound_canonical_sanitization(self) -> None:
		payload_hash = "b" * 64
		current = frappe._dict(
			name="MESSAGE-1",
			hospital="HOSP-1",
			scope_policy_hash="a" * 64,
			payload_hash=payload_hash,
			source_record_id="clinical-identifier",
		)
		current.meta = SimpleNamespace(has_field=lambda _fieldname: True)
		current.db_set = MagicMock()
		with (
			patch.object(frappe, "get_doc", return_value=current),
			self.assertRaises(frappe.PermissionError),
		):
			_update_message(current, {"payload_json": '{"patient_name":"must-not-survive"}'})
		current.db_set.assert_not_called()

		manifest = _scope_violation_manifest(payload_hash=payload_hash)
		with patch.object(frappe, "get_doc", return_value=current):
			_update_message(
				current,
				{
					"source_record_id": _hashed_identity(current.source_record_id),
					"payload_json": manifest,
				},
			)
		current.db_set.assert_called_once()

	def test_processed_message_cannot_be_reopened_or_sanitized(self) -> None:
		current = frappe._dict(
			name="MESSAGE-1",
			status="Processed",
			event="EVENT-1",
			hospital="HOSP-1",
			scope_policy_hash="a" * 64,
			payload_hash="b" * 64,
			source_record_id="clinical-identifier",
		)
		current.meta = SimpleNamespace(has_field=lambda _fieldname: True)
		current.db_set = MagicMock()
		with (
			patch.object(frappe, "get_doc", return_value=current),
			self.assertRaisesRegex(frappe.PermissionError, "state transition"),
		):
			_update_message(current, {"status": "Dead Letter"})
		current.db_set.assert_not_called()
