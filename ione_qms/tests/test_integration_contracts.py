from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from ione_qms.api.integration import _parse_payload, receive_event
from ione_qms.integration import builtin_connectors, signing
from ione_qms.integration.builtin_connectors import (
	MAX_FILE_ROW_BYTES,
	FHIRConnector,
	RESTJSONConnector,
	_fhir_envelope,
	_iter_file_rows,
	_next_rest_cursor,
	_oracle_event_envelope,
	_oracle_items,
	_oracle_next_cursor,
	_patient_index_delta_query,
	_read_oracle_lob_bounded,
	_validate_read_only_sql,
)
from ione_qms.integration.connectors import (
	ConnectorQuarantineRecord,
	decode_connector_quarantine_record,
)
from ione_qms.integration.mapping import apply_mapping
from ione_qms.integration.master_data import (
	ENCOUNTER_TYPES,
	SourceVersionConflictError,
	StaleSourceVersionError,
	_choice,
	_should_apply_source_version,
	compare_source_versions,
)
from ione_qms.integration.schemas import (
	MAX_PAYLOAD_BYTES,
	IncomingClinicalEvent,
	validate_payload_size,
)
from ione_qms.services import integration_config
from ione_qms.tasks import integration as integration_tasks
from ione_qms.tasks.integration import _message_payload


class TestIntegrationContracts(TestCase):
	def test_push_method_is_guest_reachable_but_post_only(self) -> None:
		self.assertIn(receive_event, frappe.guest_methods)
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[receive_event],
			("POST",),
		)

	def test_signature_binds_endpoint_and_exact_raw_body(self) -> None:
		body = b'{"event_type":"Encounter.Updated"}'
		message = signing.signature_message("ENDPOINT-1", "100", "nonce-1234567890", body)
		self.assertEqual(
			message,
			b'100.nonce-1234567890.ENDPOINT-1.{"event_type":"Encounter.Updated"}',
		)
		self.assertNotEqual(
			message,
			signing.signature_message("ENDPOINT-2", "100", "nonce-1234567890", body),
		)

	def test_signed_body_parser_ignores_no_query_or_form_override(self) -> None:
		body = (
			b'{"event_type":"Encounter.Updated","source_record_id":"SIGNED",'
			b'"payload":{"status":"Discharged"}}'
		)
		self.assertEqual(_parse_payload(body, "ENDPOINT-1")["source_record_id"], "SIGNED")

	def test_signed_body_parser_rejects_duplicate_keys_and_endpoint_mismatch(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			_parse_payload(
				b'{"event_type":"A","event_type":"B"}',
				"ENDPOINT-1",
			)
		with self.assertRaises(frappe.ValidationError):
			_parse_payload(
				b'{"endpoint":"ENDPOINT-2","event_type":"A"}',
				"ENDPOINT-1",
			)

	def test_unsigned_inbound_request_is_rejected_by_global_policy(self) -> None:
		endpoint = frappe._dict(name="ENDPOINT-1", require_signature=0)
		with (
			patch.object(signing, "_verify_source_ip"),
			patch.object(signing, "_enforce_rate_limit"),
			patch.object(signing.frappe.db, "exists", return_value=True),
			patch.object(
				signing.frappe.db,
				"get_single_value",
				side_effect=lambda doctype, _field: 1 if doctype == "IONE Integration Settings" else 0,
			),
			self.assertRaises(frappe.AuthenticationError),
		):
			signing.verify_request(endpoint, b"{}")

	def test_guest_ingress_forces_hmac_even_when_nonproduction_endpoint_is_unsigned(self) -> None:
		endpoint = frappe._dict(name="ENDPOINT-1", require_signature=0)
		with (
			patch.object(signing, "_verify_source_ip"),
			patch.object(signing, "_enforce_rate_limit"),
			patch.object(signing, "unsigned_requests_rejected", return_value=False),
			self.assertRaises(frappe.AuthenticationError),
		):
			signing.verify_request(endpoint, b"{}", force_signature=True)

	def test_production_inbound_runtime_requires_source_cidr(self) -> None:
		endpoint = frappe._dict(name="ENDPOINT-1", allowed_ip_cidrs="")
		with (
			patch.object(signing, "production_mode_enabled", return_value=True),
			patch.object(signing, "_verify_forwarded_chain"),
			self.assertRaises(frappe.AuthenticationError),
		):
			signing._verify_source_ip(endpoint)

	def test_production_rejects_multi_hop_forwarded_chain_before_cidr_check(self) -> None:
		with (
			patch.object(signing, "production_mode_enabled", return_value=True),
			patch.object(
				signing.frappe,
				"get_request_header",
				return_value="10.0.0.8, 203.0.113.10",
			),
			self.assertRaises(frappe.AuthenticationError),
		):
			signing._verify_forwarded_chain()

	def test_enabled_endpoint_security_is_revalidated_at_runtime(self) -> None:
		endpoint = frappe._dict(name="ENDPOINT-1", enabled=1)
		with patch.object(integration_config, "validate_integration_endpoint") as validate:
			integration_config.assert_integration_endpoint_runtime(endpoint)
		validate.assert_called_once_with(endpoint)

	def test_event_contract_normalizes_required_fields(self) -> None:
		event = IncomingClinicalEvent.from_payload(
			{
				"event_type": "Encounter.Updated",
				"event_time": "2026-07-29T10:00:00+08:00",
				"source_record_type": "encounter",
				"source_record_id": "E-1001",
				"source_version": "7",
				"payload": {"status": "Discharged"},
			}
		)
		self.assertEqual(event.source_record_id, "E-1001")
		self.assertEqual(event.payload["status"], "Discharged")

	def test_event_rejects_unsafe_identifier(self) -> None:
		with self.assertRaisesRegex(ValueError, "Invalid source_record_id"):
			IncomingClinicalEvent.from_payload(
				{
					"event_type": "EncounterUpdated",
					"event_time": "2026-07-29T10:00:00+08:00",
					"source_record_type": "encounter",
					"source_record_id": "../../etc/passwd",
					"payload": {},
				}
			)

	def test_mapping_only_allows_reviewed_target_fields(self) -> None:
		with self.assertRaisesRegex(ValueError, "Unsupported integration target"):
			apply_mapping({"source": {"value": "x"}}, {"owner": "source.value"})

	def test_mapping_does_not_execute_expression_paths(self) -> None:
		with self.assertRaisesRegex(ValueError, "Unsafe source field path"):
			apply_mapping(
				{"source": {"department": "Cardiology"}},
				{"department": "source.__class__"},
			)

	def test_encounter_mapping_accepts_all_governed_select_values(self) -> None:
		self.assertEqual(
			ENCOUNTER_TYPES,
			{
				"Outpatient",
				"Emergency",
				"Inpatient",
				"Day Care",
				"Day Surgery",
				"Observation",
				"Other",
			},
		)
		for encounter_type in ENCOUNTER_TYPES:
			self.assertEqual(_choice(encounter_type, set(ENCOUNTER_TYPES), "Inpatient"), encounter_type)

	def test_source_versions_use_reviewed_monotonic_semantics(self) -> None:
		self.assertEqual(compare_source_versions("7", "8", "integer"), 1)
		self.assertEqual(compare_source_versions("8", "7", "integer"), -1)
		self.assertEqual(
			compare_source_versions(
				"2026-07-29T10:00:00Z",
				"2026-07-29T10:00:01Z",
				"datetime",
			),
			1,
		)
		for invalid in (
			"2026-07-29T10:00:00",
			"2026-07-29T18:00:00+08:00",
			"2026-07-29T09:00:00-01:00",
			"2026",
			"1",
		):
			with self.subTest(invalid=invalid), self.assertRaises(frappe.ValidationError):
				compare_source_versions(
					"2026-07-29T10:00:00Z",
					invalid,
					"datetime",
				)

	def test_stale_and_same_version_different_payload_are_rejected(self) -> None:
		class ExistingIndex(frappe._dict):
			def is_new(self):
				return False

		doc = ExistingIndex(
			applied_source_version="8",
			applied_version_strategy="integer",
			applied_payload_hash="a" * 64,
		)
		incoming = SimpleNamespace(source_version="7")
		with self.assertRaises(StaleSourceVersionError):
			_should_apply_source_version(
				doc,
				incoming,
				version_strategy="integer",
				payload_hash="b" * 64,
			)
		incoming.source_version = "8"
		with self.assertRaises(SourceVersionConflictError):
			_should_apply_source_version(
				doc,
				incoming,
				version_strategy="integer",
				payload_hash="b" * 64,
			)
		self.assertFalse(
			_should_apply_source_version(
				doc,
				incoming,
				version_strategy="integer",
				payload_hash="a" * 64,
			)
		)

	def test_quarantined_bad_record_does_not_block_later_good_record_or_cursor(self) -> None:
		endpoint = frappe._dict(
			name="ENDPOINT-1",
			doctype="IONE Integration Endpoint",
			source_system="SOURCE-1",
			connector_key="rest",
			last_cursor="before",
		)
		source = frappe._dict(name="SOURCE-1", enabled=1, read_only=1)
		connector = SimpleNamespace(fetch=lambda _cursor, _limit: ([None, {"valid": True}], "after"))
		with (
			patch.object(integration_tasks, "assert_integration_endpoint_runtime"),
			patch.object(integration_tasks.frappe, "get_cached_doc", return_value=source),
			patch.object(integration_tasks, "registered_connectors", return_value={"rest"}),
			patch.object(integration_tasks, "get_connector", return_value=connector),
			patch.object(integration_tasks, "_cursor_field", return_value="last_cursor"),
			patch.object(integration_tasks, "_start_job", return_value=None),
			patch.object(
				integration_tasks,
				"quarantine_invalid_clinical_event",
				return_value={"status": "Dead Letter", "quarantined": True},
			) as quarantine,
			patch.object(
				integration_tasks,
				"receive_clinical_event",
				return_value={"status": "Processed", "duplicate": False},
			) as receive,
			patch.object(integration_tasks, "update_endpoint_runtime_state") as update,
			patch.object(integration_tasks, "_finish_job") as finish,
		):
			result = integration_tasks._sync_endpoint(endpoint, 10)
		self.assertEqual(
			result,
			{
				"received": 2,
				"succeeded": 1,
				"duplicates": 0,
				"quarantined": 1,
				"failed": 0,
			},
		)
		quarantine.assert_called_once()
		receive.assert_called_once_with(endpoint.name, {"valid": True})
		update.assert_called_once()
		self.assertEqual(update.call_args.args[1]["last_cursor"], "after")
		finish.assert_called_once_with(None, "Partial", result, "after")

	def test_rest_rejects_an_oversized_raw_page_instead_of_slicing(self) -> None:
		endpoint = frappe._dict(
			name="REST-1",
			base_url="https://source.example",
			connector_options={
				"items_path": "items",
				"cursor_mode": "token",
				"next_cursor_path": "next",
			},
			authentication_type="None",
			timeout_seconds=30,
			tls_verify=1,
		)
		response = SimpleNamespace(is_redirect=False, raise_for_status=lambda: None)
		with (
			patch.object(builtin_connectors.requests, "get", return_value=response),
			patch.object(
				builtin_connectors,
				"_bounded_json",
				return_value={"items": [{"id": 1}, {"id": 2}], "next": "after"},
			),
			self.assertRaisesRegex(frappe.ValidationError, "more records"),
		):
			RESTJSONConnector(endpoint).fetch(None, 1)

	def test_rest_non_object_item_is_a_sanitized_quarantine_record(self) -> None:
		endpoint = frappe._dict(
			name="REST-1",
			base_url="https://source.example",
			connector_options={
				"items_path": "items",
				"cursor_mode": "token",
				"next_cursor_path": "next",
			},
			authentication_type="None",
			timeout_seconds=30,
			tls_verify=1,
		)
		response = SimpleNamespace(is_redirect=False, raise_for_status=lambda: None)
		clinical_value = "must-not-appear-in-marker"
		with (
			patch.object(builtin_connectors.requests, "get", return_value=response),
			patch.object(
				builtin_connectors,
				"_bounded_json",
				return_value={"items": [clinical_value, {"valid": True}], "next": "after"},
			),
		):
			items, cursor = RESTJSONConnector(endpoint).fetch(None, 2)
		self.assertEqual(cursor, "after")
		self.assertIsInstance(items[0], ConnectorQuarantineRecord)
		self.assertNotIn(clinical_value, str(items[0]))
		self.assertEqual(json.loads(str(items[0]))["reason_code"], "REST_ITEM_NOT_OBJECT")
		self.assertEqual(items[1], {"valid": True})

	def test_fhir_rejects_oversized_page_and_quarantines_bad_entries(self) -> None:
		endpoint = frappe._dict(
			name="FHIR-1",
			base_url="https://fhir.example",
			connector_options={"resource_type": "Observation"},
			authentication_type="None",
			timeout_seconds=30,
			tls_verify=1,
		)
		response = SimpleNamespace(is_redirect=False, raise_for_status=lambda: None)
		with (
			patch.object(builtin_connectors.requests, "get", return_value=response),
			patch.object(
				builtin_connectors,
				"_bounded_json",
				return_value={"resourceType": "Bundle", "entry": [{}, {}]},
			),
			self.assertRaisesRegex(frappe.ValidationError, "more records"),
		):
			FHIRConnector(endpoint).fetch(None, 1)

		bundle = {
			"resourceType": "Bundle",
			"timestamp": "2026-07-30T12:00:00Z",
			"entry": [
				"not-an-entry-object",
				{"resource": "not-a-resource-object"},
				{
					"resource": {
						"resourceType": "Observation",
						"id": "missing-meta",
					}
				},
				{
					"resource": {
						"resourceType": "Observation",
						"id": "obs-1",
						"meta": {"lastUpdated": "2026-07-30T11:00:00Z", "versionId": "1"},
					}
				},
			],
		}
		with (
			patch.object(builtin_connectors.requests, "get", return_value=response),
			patch.object(builtin_connectors, "_bounded_json", return_value=bundle),
		):
			items, cursor = FHIRConnector(endpoint).fetch(None, 4)
		self.assertIsInstance(items[0], ConnectorQuarantineRecord)
		self.assertIsInstance(items[1], ConnectorQuarantineRecord)
		self.assertIsInstance(items[2], ConnectorQuarantineRecord)
		self.assertNotIn("not-an-entry-object", str(items[0]))
		self.assertNotIn("not-a-resource-object", str(items[1]))
		self.assertNotIn("missing-meta", str(items[2]))
		self.assertEqual(
			json.loads(str(items[2]))["reason_code"],
			"FHIR_RESOURCE_ENVELOPE_INVALID",
		)
		self.assertEqual(items[3]["source_record_id"], "obs-1")
		# The untrusted Bundle timestamp and local receive clock cannot advance
		# source truth; only a reviewed resource lastUpdated may do so.
		self.assertEqual(json.loads(cursor)["watermark"], "2026-07-30T11:00:00Z")

	def test_fhir_bad_final_page_keeps_cursor_without_guessing_a_watermark(self) -> None:
		endpoint = frappe._dict(
			name="FHIR-1",
			base_url="https://fhir.example",
			connector_options={"resource_type": "Observation"},
			authentication_type="None",
			timeout_seconds=30,
			tls_verify=1,
		)
		response = SimpleNamespace(is_redirect=False, raise_for_status=lambda: None)
		bundle = {
			"resourceType": "Bundle",
			"timestamp": "2099-01-01T00:00:00Z",
			"entry": [{"resource": {"resourceType": "Observation", "id": "no-watermark"}}],
		}
		with (
			patch.object(builtin_connectors.requests, "get", return_value=response),
			patch.object(builtin_connectors, "_bounded_json", return_value=bundle),
		):
			items, cursor = FHIRConnector(endpoint).fetch(None, 2)
			retried_items, retried_cursor = FHIRConnector(endpoint).fetch(cursor, 2)
		self.assertIsInstance(items[0], ConnectorQuarantineRecord)
		self.assertIsNone(cursor)
		self.assertEqual(retried_cursor, cursor)
		self.assertEqual(retried_items, items)

	def test_fhir_replays_source_time_overlap_without_moving_cursor_backwards(self) -> None:
		endpoint = frappe._dict(
			name="FHIR-1",
			base_url="https://fhir.example",
			connector_options={"resource_type": "Observation"},
			authentication_type="None",
			timeout_seconds=30,
			tls_verify=1,
		)
		response = SimpleNamespace(is_redirect=False, raise_for_status=lambda: None)
		request_params: list[dict] = []

		def fake_get(_url, **kwargs):
			request_params.append(kwargs["params"])
			return response

		bundle = {
			"resourceType": "Bundle",
			"entry": [
				{
					"resource": {
						"resourceType": "Observation",
						"id": "late-same-timestamp",
						"meta": {"lastUpdated": "2026-07-30T11:00:00Z", "versionId": "1"},
					}
				},
				{
					"resource": {
						"resourceType": "Observation",
						"id": "overlap-replay",
						"meta": {"lastUpdated": "2026-07-30T10:59:58Z", "versionId": "1"},
					}
				},
			],
		}
		cursor_before = '{"next_url":null,"watermark":"2026-07-30T11:00:00Z"}'
		with (
			patch.object(builtin_connectors.requests, "get", side_effect=fake_get),
			patch.object(builtin_connectors, "_bounded_json", return_value=bundle),
		):
			items, cursor_after = FHIRConnector(endpoint).fetch(cursor_before, 3)
		self.assertEqual(
			request_params[0]["_lastUpdated"],
			"ge2026-07-30T10:59:55Z",
		)
		self.assertEqual(
			[item["source_record_id"] for item in items],
			["late-same-timestamp", "overlap-replay"],
		)
		self.assertEqual(cursor_after, cursor_before)

	def test_fhir_overlap_option_is_a_strict_bounded_integer(self) -> None:
		for invalid in (0, 301, True, "5", 5.0):
			endpoint = frappe._dict(
				name="FHIR-1",
				base_url="https://fhir.example",
				connector_options={
					"resource_type": "Observation",
					"watermark_overlap_seconds": invalid,
				},
				authentication_type="None",
				timeout_seconds=30,
				tls_verify=1,
			)
			with (
				self.subTest(invalid=invalid),
				patch.object(builtin_connectors.requests, "get") as request,
				self.assertRaisesRegex(frappe.ValidationError, "integer from 1 through 300"),
			):
				FHIRConnector(endpoint).fetch(None, 1)
			request.assert_not_called()

	def test_partial_nonadvancing_page_is_quarantined_without_cursor_update(self) -> None:
		endpoint = frappe._dict(
			name="FHIR-1",
			doctype="IONE Integration Endpoint",
			source_system="SOURCE-1",
			connector_key="fhir",
			last_cursor="",
		)
		source = frappe._dict(name="SOURCE-1", enabled=1, read_only=1)
		marker = ConnectorQuarantineRecord(
			'{"connector":"fhir","connector_quarantine":1,'
			'"content_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
			'"position":{"cursor_sha256":'
			'"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
			'"page_index":0},"reason_code":"FHIR_RESOURCE_ENVELOPE_INVALID","size_bytes":1}'
		)
		self.assertIsNotNone(decode_connector_quarantine_record(marker))
		connector = SimpleNamespace(fetch=lambda _cursor, _limit: ([marker], None))
		with (
			patch.object(integration_tasks, "assert_integration_endpoint_runtime"),
			patch.object(integration_tasks.frappe, "get_cached_doc", return_value=source),
			patch.object(integration_tasks, "registered_connectors", return_value={"fhir"}),
			patch.object(integration_tasks, "get_connector", return_value=connector),
			patch.object(integration_tasks, "_cursor_field", return_value="last_cursor"),
			patch.object(integration_tasks, "_start_job", return_value=None),
			patch.object(
				integration_tasks,
				"quarantine_invalid_clinical_event",
				return_value={"status": "Dead Letter", "quarantined": True},
			) as quarantine,
			patch.object(integration_tasks, "receive_clinical_event") as receive,
			patch.object(integration_tasks, "update_endpoint_runtime_state") as update,
			patch.object(integration_tasks, "_finish_job") as finish,
		):
			result = integration_tasks._sync_endpoint(endpoint, 2)
		self.assertEqual(
			result,
			{
				"received": 1,
				"succeeded": 0,
				"duplicates": 0,
				"quarantined": 1,
				"failed": 0,
			},
		)
		quarantine.assert_called_once_with(
			endpoint.name,
			marker,
			failure_type="InvalidConnectorRecord",
		)
		receive.assert_not_called()
		self.assertNotIn("last_cursor", update.call_args.args[1])
		finish.assert_called_once_with(None, "Partial", result, None)

	def test_file_bad_and_oversized_lines_are_bounded_quarantine_records(self) -> None:
		secret = b"must-not-survive-in-marker"
		oversized = secret + b"x" * (MAX_FILE_ROW_BYTES - len(secret) + 1) + b"\n"
		content = b'{"valid":true}\n' + b"{bad-json}\n" + b"42\n" + oversized
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "events.jsonl"
			path.write_bytes(content)
			rows = list(_iter_file_rows(path, "jsonl", 0))
		self.assertEqual(rows[0][1], {"valid": True})
		self.assertEqual(
			json.loads(str(rows[1][1]))["reason_code"],
			"FILE_JSONL_INVALID_JSON",
		)
		self.assertEqual(json.loads(str(rows[2][1]))["reason_code"], "FILE_JSONL_NOT_OBJECT")
		oversized_marker = rows[3][1]
		self.assertIsInstance(oversized_marker, ConnectorQuarantineRecord)
		self.assertNotIn(secret.decode(), str(oversized_marker))
		metadata = json.loads(str(oversized_marker))
		self.assertEqual(metadata["reason_code"], "FILE_LINE_TOO_LARGE")
		self.assertEqual(
			metadata["position"],
			{
				"file_name_sha256": hashlib.sha256(b"events.jsonl").hexdigest(),
				"line": 4,
			},
		)
		self.assertNotIn("events.jsonl", str(oversized_marker))
		self.assertEqual(metadata["size_bytes"], len(oversized))
		self.assertEqual(metadata["content_sha256"], hashlib.sha256(oversized).hexdigest())
		self.assertEqual(decode_connector_quarantine_record(oversized_marker), metadata)

	def test_connector_quarantine_decoder_rejects_noncanonical_or_tampered_markers(self) -> None:
		payload = {
			"connector_quarantine": 1,
			"connector": "fhir",
			"reason_code": "FHIR_ENTRY_NOT_OBJECT",
			"position": {
				"page_index": 0,
				"cursor_sha256": "a" * 64,
			},
			"content_sha256": "b" * 64,
			"size_bytes": 12,
		}

		def marker(value):
			return ConnectorQuarantineRecord(
				json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
			)

		self.assertEqual(decode_connector_quarantine_record(marker(payload)), payload)
		self.assertIsNone(decode_connector_quarantine_record(str(marker(payload))))
		for fieldname, invalid in (
			("connector", "oracle"),
			("reason_code", "UNKNOWN_REASON"),
			("content_sha256", "B" * 64),
			("size_bytes", -1),
			("size_bytes", True),
		):
			with self.subTest(fieldname=fieldname, invalid=invalid):
				tampered = {**payload, fieldname: invalid}
				self.assertIsNone(decode_connector_quarantine_record(marker(tampered)))
		self.assertIsNone(
			decode_connector_quarantine_record(
				marker(
					{
						**payload,
						"position": {"page_index": 0, "cursor_sha256": "a" * 64, "raw": "PHI"},
					}
				)
			)
		)
		noncanonical = ConnectorQuarantineRecord(json.dumps(payload, ensure_ascii=True, sort_keys=True))
		self.assertIsNone(decode_connector_quarantine_record(noncanonical))

	def test_oracle_bad_row_does_not_discard_later_valid_row(self) -> None:
		bad = {
			"source_record_type": "Encounter",
			"source_record_id": "bad-id-must-not-leak",
			"source_version": "1",
			"cursor_time": "2026-07-30T10:00:00",
			"cursor_id": "1",
		}
		valid = {
			"event_type": "Encounter.Updated",
			"event_time": "2026-07-30T10:00:01",
			"source_record_type": "Encounter",
			"source_record_id": "E-2",
			"source_version": "2",
			"cursor_time": "2026-07-30T10:00:01",
			"cursor_id": "2",
		}
		items = _oracle_items([bad, valid])
		self.assertIsInstance(items[0], ConnectorQuarantineRecord)
		self.assertNotIn("bad-id-must-not-leak", str(items[0]))
		self.assertEqual(
			json.loads(str(items[0]))["reason_code"],
			"ORACLE_ROW_ENVELOPE_INVALID",
		)
		self.assertEqual(items[1]["source_record_id"], "E-2")

	def test_oracle_lob_reads_are_chunked_and_oversize_is_record_local(self) -> None:
		class FakeLOB:
			def __init__(self, value: str):
				self.value = value
				self.calls: list[tuple[int, int]] = []

			def size(self):
				return len(self.value)

			def read(self, *, offset: int, amount: int):
				self.calls.append((offset, amount))
				start = offset - 1
				return self.value[start : start + amount]

		payload = json.dumps({"clinical": "x" * 70_000})
		lob = FakeLOB(payload)
		self.assertEqual(json.loads(_read_oracle_lob_bounded(lob)), json.loads(payload))
		self.assertGreater(len(lob.calls), 1)
		self.assertTrue(all(amount <= 64 * 1024 for _, amount in lob.calls))

		clinical_value = "must-not-appear-in-marker"
		oversized = FakeLOB(clinical_value + "x" * MAX_PAYLOAD_BYTES)
		row = {
			"event_type": "Encounter.Updated",
			"event_time": "2026-07-30T10:00:00",
			"source_record_type": "Encounter",
			"source_record_id": "E-oversized",
			"source_version": "1",
			"cursor_time": "2026-07-30T10:00:00",
			"cursor_id": "1",
			"payload_json": oversized,
		}
		item = _oracle_items([row])[0]
		self.assertIsInstance(item, ConnectorQuarantineRecord)
		self.assertNotIn(clinical_value, str(item))
		metadata = json.loads(str(item))
		self.assertEqual(metadata["reason_code"], "ORACLE_ROW_ENVELOPE_INVALID")
		self.assertEqual(len(metadata["content_sha256"]), 64)
		self.assertGreater(metadata["size_bytes"], 0)
		self.assertLessEqual(len(str(item).encode()), 2048)
		self.assertEqual(oversized.calls, [])

	def test_payload_limit(self) -> None:
		validate_payload_size(b"x" * MAX_PAYLOAD_BYTES)
		with self.assertRaisesRegex(ValueError, "Payload exceeds"):
			validate_payload_size(b"x" * (MAX_PAYLOAD_BYTES + 1))

	def test_fhir_resource_is_normalized_to_event_envelope(self) -> None:
		envelope = _fhir_envelope(
			{
				"resourceType": "Observation",
				"id": "obs-1",
				"meta": {
					"versionId": "3",
					"lastUpdated": "2026-07-29T10:00:00+08:00",
				},
				"subject": {"reference": "Patient/p-1"},
				"encounter": {"reference": "Encounter/e-1"},
			},
			{},
		)
		self.assertEqual(envelope["event_type"], "FHIR.Observation.Updated")
		self.assertEqual(envelope["source_record_id"], "obs-1")
		self.assertEqual(envelope["source_version"], "3")
		self.assertEqual(envelope["patient_reference"], "Patient/p-1")

	def test_rest_item_cursor_must_advance_from_reviewed_field(self) -> None:
		self.assertEqual(
			_next_rest_cursor(
				{},
				items=[{"updated_at": "2026-07-29T10:00:00+08:00"}],
				cursor=None,
				count=1,
				mode="item",
				options={"cursor_item_path": "updated_at"},
			),
			"2026-07-29T10:00:00+08:00",
		)
		self.assertEqual(
			_next_rest_cursor(
				{},
				items=["malformed"],
				cursor="before",
				count=1,
				mode="item",
				options={"cursor_item_path": "updated_at"},
			),
			"before",
		)

	def test_oracle_connector_sql_is_read_only(self) -> None:
		_validate_read_only_sql("select id, modified from encounters where modified > :cursor")
		with self.assertRaises(frappe.ValidationError):
			_validate_read_only_sql("update encounters set status = 'x'")

	def test_reviewed_oracle_delta_query_uses_binds_and_stable_cursor(self) -> None:
		sql, binds = _patient_index_delta_query(None, 100)
		_validate_read_only_sql(sql)
		self.assertEqual(binds["row_limit"], 100)
		self.assertIsNone(binds["cursor_time"])
		cursor = _oracle_next_cursor(
			[{"cursor_time": "2026-07-29T10:00:00.000000", "cursor_id": "P-2"}],
			None,
			{},
		)
		self.assertEqual(
			cursor,
			'{"id":"P-2","time":"2026-07-29T10:00:00.000000"}',
		)

	def test_oracle_cursor_lob_failure_fails_closed_before_row_conversion(self) -> None:
		class OversizedCursorLOB:
			def __init__(self):
				self.calls = 0

			def size(self):
				return MAX_PAYLOAD_BYTES + 1

			def read(self, *, offset: int, amount: int):
				self.calls += 1
				return "x" * amount

		cursor_lob = OversizedCursorLOB()
		with self.assertRaises(frappe.ValidationError):
			_oracle_next_cursor(
				[{"cursor_time": cursor_lob, "cursor_id": "P-2"}],
				None,
				{},
			)
		self.assertEqual(cursor_lob.calls, 0)

	def test_oracle_rows_are_normalized_to_event_envelopes(self) -> None:
		event = _oracle_event_envelope(
			{
				"event_type": "Patient.Updated",
				"event_time": "2026-07-29T10:00:00",
				"source_record_type": "Patient",
				"source_record_id": "P-2",
				"source_version": "7",
				"patient_reference": "P-2",
				"cursor_time": "2026-07-29T10:00:00.000000",
				"cursor_id": "P-2",
				"patient_name": "测试患者",
			}
		)
		self.assertEqual(event["patient_reference"], "P-2")
		self.assertEqual(event["payload"], {"patient_name": "测试患者"})

	def test_retry_payload_requires_exact_canonical_hash(self) -> None:
		payload = {"event_type": "Encounter.Updated", "payload": {"status": "Inpatient"}}
		canonical = json.dumps(
			payload,
			ensure_ascii=False,
			sort_keys=True,
			separators=(",", ":"),
		)
		message = SimpleNamespace(
			payload_hash=hashlib.sha256(canonical.encode()).hexdigest(),
			get=lambda fieldname: {
				"payload_json": canonical,
				"payload_hash": hashlib.sha256(canonical.encode()).hexdigest(),
			}.get(fieldname),
		)
		self.assertEqual(_message_payload(message), payload)
		message.payload_hash = "0" * 64
		with self.assertRaises(frappe.ValidationError):
			_message_payload(message)
