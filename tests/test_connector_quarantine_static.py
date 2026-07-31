from __future__ import annotations

import ast
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BUILTINS = ROOT / "ione_qms" / "integration" / "builtin_connectors.py"
CONNECTORS = ROOT / "ione_qms" / "integration" / "connectors.py"


def function_source(path: Path, name: str) -> str:
	source = path.read_text(encoding="utf-8")
	tree = ast.parse(source)
	for node in tree.body:
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
			return ast.get_source_segment(source, node) or ""
	raise AssertionError(f"{name} was not found in {path}")


def load_builtin_connector_harness():
	class ValidationError(Exception):
		pass

	frappe = ModuleType("frappe")
	frappe.ValidationError = ValidationError
	requests = ModuleType("requests")
	requests.Response = object

	def unexpected_request(*_args, **_kwargs):
		raise AssertionError("The harness must install a fake HTTP response")

	requests.get = unexpected_request
	connectors = ModuleType("ione_qms.integration.connectors")

	class BaseConnector:
		def __init__(self, endpoint):
			self.endpoint = endpoint

	class ConnectorQuarantineRecord(str):
		pass

	class ConnectorRecordError(ValidationError):
		pass

	class ReconciliationSnapshot:
		def __init__(self, source_count, source_hash=None, details=None):
			self.source_count = source_count
			self.source_hash = source_hash
			self.details = details

	def register_connector(_key):
		def decorator(connector):
			return connector

		return decorator

	def decode_connector_quarantine_record(_value):
		return {}

	connectors.BaseConnector = BaseConnector
	connectors.ConnectorItem = object
	connectors.ConnectorQuarantineRecord = ConnectorQuarantineRecord
	connectors.ConnectorRecordError = ConnectorRecordError
	connectors.ReconciliationSnapshot = ReconciliationSnapshot
	connectors.decode_connector_quarantine_record = decode_connector_quarantine_record
	connectors.register_connector = register_connector
	schemas = ModuleType("ione_qms.integration.schemas")
	schemas.MAX_PAYLOAD_BYTES = 2 * 1024 * 1024

	module_name = "_ione_qms_builtin_connector_harness"
	spec = importlib.util.spec_from_file_location(module_name, BUILTINS)
	if spec is None or spec.loader is None:
		raise AssertionError("Unable to load connector harness")
	module = importlib.util.module_from_spec(spec)
	with patch.dict(
		sys.modules,
		{
			module_name: module,
			"frappe": frappe,
			"requests": requests,
			"ione_qms.integration.connectors": connectors,
			"ione_qms.integration.schemas": schemas,
		},
	):
		spec.loader.exec_module(module)
	return module, ValidationError


def load_connectors_harness():
	class ValidationError(Exception):
		pass

	frappe = ModuleType("frappe")
	frappe.ValidationError = ValidationError
	module_name = "_ione_qms_connectors_harness"
	spec = importlib.util.spec_from_file_location(module_name, CONNECTORS)
	if spec is None or spec.loader is None:
		raise AssertionError("Unable to load connector marker harness")
	module = importlib.util.module_from_spec(spec)
	with patch.dict(sys.modules, {module_name: module, "frappe": frappe}):
		spec.loader.exec_module(module)
	return module


class TestConnectorQuarantineStaticContract(unittest.TestCase):
	def test_connectors_return_an_opaque_non_dict_quarantine_item(self) -> None:
		source = CONNECTORS.read_text(encoding="utf-8")
		self.assertIn("class ConnectorQuarantineRecord(str)", source)
		self.assertIn("ConnectorItem = dict[str, Any] | ConnectorQuarantineRecord", source)
		self.assertIn("tuple[list[ConnectorItem], str | None]", source)

	def test_rest_and_fhir_reject_raw_oversized_pages_before_any_slice(self) -> None:
		source = BUILTINS.read_text(encoding="utf-8")
		self.assertIn("if len(raw_items) > limit:", source)
		self.assertIn("if len(entries) > limit:", source)
		self.assertNotIn("items[:limit]", source)
		self.assertNotIn("resources[:limit]", source)
		self.assertNotIn("datetime.now", source)
		self.assertNotIn('bundle.get("timestamp")', source)
		self.assertNotIn('next_state["stalled"]', source)
		self.assertIn("return envelopes, cursor", source)
		self.assertIn('f"ge{_fhir_overlap_start', source)
		self.assertNotIn('f"gt{state[', source)

	def test_record_local_contract_errors_produce_sanitized_markers(self) -> None:
		source = BUILTINS.read_text(encoding="utf-8")
		for reason_code in (
			"REST_ITEM_NOT_OBJECT",
			"FHIR_ENTRY_NOT_OBJECT",
			"FHIR_ENTRY_RESOURCE_NOT_OBJECT",
			"FHIR_RESOURCE_ENVELOPE_INVALID",
			"FILE_JSONL_INVALID_JSON",
			"FILE_JSONL_NOT_OBJECT",
			"ORACLE_ROW_ENVELOPE_INVALID",
		):
			self.assertIn(reason_code, source)
		marker = function_source(BUILTINS, "_quarantine_marker")
		self.assertIn('"content_sha256"', marker)
		self.assertNotIn("str(value)", marker)
		self.assertIn("> 2048", marker)

	def test_file_rows_are_streamed_with_the_push_payload_limit(self) -> None:
		source = BUILTINS.read_text(encoding="utf-8")
		self.assertIn("MAX_FILE_ROW_BYTES = MAX_PAYLOAD_BYTES", source)
		bounded = function_source(BUILTINS, "_iter_bounded_file_lines")
		self.assertIn("handle.readline(MAX_FILE_ROW_BYTES + 2)", bounded)
		self.assertIn("None if oversized else first", bounded)
		self.assertIn("hasher.hexdigest()", bounded)
		file_rows = function_source(BUILTINS, "_iter_file_rows")
		self.assertNotIn("raise frappe.ValidationError", file_rows)
		file_position = function_source(BUILTINS, "_file_marker_position")
		self.assertIn('"file_name_sha256"', file_position)
		self.assertNotIn('"file"', file_position)

	def test_oracle_lobs_use_bounded_reads_without_a_no_argument_fallback(self) -> None:
		lob_reader = function_source(BUILTINS, "_read_oracle_lob_bounded")
		self.assertIn("ORACLE_LOB_CHUNK_UNITS", lob_reader)
		self.assertIn("MAX_PAYLOAD_BYTES", lob_reader)
		self.assertIn("read(offset=offset, amount=amount)", lob_reader)
		self.assertIn("read(amount)", lob_reader)
		self.assertNotIn("read()", lob_reader)
		self.assertNotIn(".read()", function_source(BUILTINS, "_oracle_payload"))

	def test_fhir_overlap_harness_replays_boundary_without_cursor_regression(self) -> None:
		module, validation_error = load_builtin_connector_harness()

		class Endpoint(dict):
			def __getattr__(self, name):
				return self[name]

		endpoint = Endpoint(
			name="FHIR-1",
			base_url="https://fhir.example",
			connector_options={
				"resource_type": "Observation",
				"watermark_overlap_seconds": 30,
			},
			authentication_type="None",
			timeout_seconds=30,
			tls_verify=1,
		)
		response = SimpleNamespace(is_redirect=False, raise_for_status=lambda: None)
		requests_seen = []

		def fake_get(_url, **kwargs):
			requests_seen.append(kwargs)
			return response

		def fake_bundle(_response):
			return {
				"resourceType": "Bundle",
				"entry": [
					{
						"resource": {
							"resourceType": "Observation",
							"id": "late-at-boundary",
							"meta": {
								"lastUpdated": "2026-07-30T11:00:00Z",
								"versionId": "1",
							},
						}
					},
					{
						"resource": {
							"resourceType": "Observation",
							"id": "overlap-replay",
							"meta": {
								"lastUpdated": "2026-07-30T10:59:45Z",
								"versionId": "1",
							},
						}
					},
				],
			}

		module.requests.get = fake_get
		module._bounded_json = fake_bundle
		cursor_before = '{"next_url":null,"watermark":"2026-07-30T11:00:00Z"}'
		items, cursor_after = module.FHIRConnector(endpoint).fetch(cursor_before, 3)
		self.assertEqual(
			requests_seen[0]["params"]["_lastUpdated"],
			"ge2026-07-30T10:59:30Z",
		)
		self.assertEqual(
			[item["source_record_id"] for item in items],
			["late-at-boundary", "overlap-replay"],
		)
		self.assertEqual(cursor_after, cursor_before)
		watermark, advanced = module._fhir_page_watermark(
			[
				{"meta": {"lastUpdated": "2026-07-30T10:59:45Z"}},
				{"meta": {"lastUpdated": "2026-07-30T11:00:01Z"}},
			],
			{"watermark": "2026-07-30T11:00:00Z"},
		)
		self.assertEqual(watermark, "2026-07-30T11:00:01Z")
		self.assertTrue(advanced)
		self.assertEqual(module._fhir_watermark_overlap_seconds({}), 5)
		self.assertEqual(
			module._fhir_watermark_overlap_seconds({"watermark_overlap_seconds": 300}),
			300,
		)
		for invalid in (0, 301, True, "5", 5.0):
			with self.subTest(invalid=invalid), self.assertRaises(validation_error):
				module._fhir_watermark_overlap_seconds({"watermark_overlap_seconds": invalid})
		endpoint["connector_options"] = {
			"resource_type": "Observation",
			"params": {"_lastUpdated": "gt2099-01-01T00:00:00Z"},
		}
		request_count = len(requests_seen)
		with self.assertRaises(validation_error):
			module.FHIRConnector(endpoint).fetch(cursor_before, 3)
		self.assertEqual(len(requests_seen), request_count)

	def test_connector_marker_decoder_accepts_only_exact_canonical_contract(self) -> None:
		module = load_connectors_harness()
		payload = {
			"connector_quarantine": 1,
			"connector": "file_drop",
			"reason_code": "FILE_LINE_TOO_LARGE",
			"position": {
				"file_name_sha256": "a" * 64,
				"line": 9,
			},
			"content_sha256": "b" * 64,
			"size_bytes": 2 * 1024 * 1024 + 1,
		}

		def marker(value, *, canonical=True):
			encoded = json.dumps(
				value,
				ensure_ascii=True,
				sort_keys=True,
				separators=(",", ":") if canonical else None,
			)
			return module.ConnectorQuarantineRecord(encoded)

		valid = marker(payload)
		self.assertEqual(module.decode_connector_quarantine_record(valid), payload)
		self.assertIsNone(module.decode_connector_quarantine_record(str(valid)))

		class MarkerSubclass(module.ConnectorQuarantineRecord):
			pass

		self.assertIsNone(module.decode_connector_quarantine_record(MarkerSubclass(valid)))
		duplicate = module.ConnectorQuarantineRecord(
			str(valid).replace(
				'"connector":"file_drop"',
				'"connector":"file_drop","connector":"file_drop"',
				1,
			)
		)
		self.assertIsNone(module.decode_connector_quarantine_record(duplicate))
		self.assertIsNone(
			module.decode_connector_quarantine_record(
				module.ConnectorQuarantineRecord(str(valid) + " " * 2048)
			)
		)
		for tampered in (
			{**payload, "connector": "fhir"},
			{**payload, "reason_code": "UNKNOWN"},
			{**payload, "content_sha256": "B" * 64},
			{**payload, "size_bytes": -1},
			{**payload, "extra": "PHI"},
			{**payload, "position": {"file": "patient-123.jsonl", "line": 9}},
		):
			with self.subTest(tampered=tampered):
				self.assertIsNone(module.decode_connector_quarantine_record(marker(tampered)))
		self.assertIsNone(module.decode_connector_quarantine_record(marker(payload, canonical=False)))


if __name__ == "__main__":
	unittest.main()
