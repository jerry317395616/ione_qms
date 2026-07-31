from __future__ import annotations

import ast
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "ione_qms" / "integration" / "hl7_v2.py"
API_PATH = ROOT / "ione_qms" / "api" / "integration.py"
CONFIG_PATH = ROOT / "ione_qms" / "services" / "integration_config.py"
GENERATOR_PATH = ROOT / "tools" / "generate_doctypes.py"
SPEC = importlib.util.spec_from_file_location("_ione_hl7_v2_static_test", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
	raise RuntimeError("Could not load the HL7 v2 normalization module")
HL7 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HL7)
HL7V2ContractError = HL7.HL7V2ContractError
normalize_hl7_v2_batch = HL7.normalize_hl7_v2_batch
normalize_hl7_v2_batch_size = HL7.normalize_hl7_v2_batch_size
normalize_hl7_v2_content_type = HL7.normalize_hl7_v2_content_type
normalize_hl7_v2_transport_options = HL7.normalize_hl7_v2_transport_options


def _message(
	*,
	control_id: str = "MSG-0001",
	timestamp: str = "20260730121530+0800",
	message_type: str = "ADT^A01^ADT_A01",
	sequence: str = "7",
) -> bytes:
	return (
		"MSH|^~\\&|EMR|HOSP|IONE|HOSP|"
		f"{timestamp}||{message_type}|{control_id}|P|2.5.1|{sequence}\r"
		"EVN|A01|20260730121500+0800\r"
		"PID|1||P-0001^^^MPI||Zhang^San\r"
		"PV1|1|I|WARD-A^ROOM-1^BED-1\r"
	).encode()


class TestHL7V2Normalization(unittest.TestCase):
	def test_message_normalizes_without_guessing_clinical_links(self) -> None:
		events = normalize_hl7_v2_batch(
			_message(),
			source_timezone="Asia/Shanghai",
		)
		self.assertEqual(len(events), 1)
		event = events[0]
		self.assertEqual(event["event_type"], "HL7.ADT.A01")
		self.assertEqual(event["source_record_type"], "HL7v2.ADT.A01")
		self.assertEqual(event["source_record_id"], "MSG-0001")
		self.assertEqual(event["source_version"], "7")
		self.assertEqual(event["event_time"], "2026-07-30T12:15:30+08:00")
		self.assertNotIn("patient_reference", event)
		self.assertNotIn("encounter_reference", event)
		hl7 = event["payload"]["hl7"]
		self.assertEqual(hl7["PID"]["field_3_component_1"], "P-0001")
		self.assertEqual(hl7["PV1"]["field_3_component_1"], "WARD-A")

	def test_repeated_segments_get_stable_mapping_keys(self) -> None:
		raw = _message().replace(
			b"PV1|",
			b"OBX|1|NM|TEST^Value||12.5\rOBX|2|ST|NOTE^Comment||Stable\rPV1|",
		)
		hl7 = normalize_hl7_v2_batch(
			raw,
			source_timezone="Asia/Shanghai",
		)[0]["payload"]["hl7"]
		self.assertIn("OBX", hl7)
		self.assertIn("OBX_2", hl7)
		self.assertEqual(hl7["OBX_2"]["field_5"], "Stable")

	def test_exact_mllp_batch_is_supported(self) -> None:
		first = b"\x0b" + _message(control_id="MSG-1") + b"\x1c\r"
		second = b"\x0b" + _message(control_id="MSG-2") + b"\x1c\r"
		events = normalize_hl7_v2_batch(
			first + second,
			source_timezone="Asia/Shanghai",
		)
		self.assertEqual(
			[event["source_record_id"] for event in events],
			["MSG-1", "MSG-2"],
		)

	def test_unframed_batch_splits_only_on_msh(self) -> None:
		events = normalize_hl7_v2_batch(
			_message(control_id="MSG-1") + _message(control_id="MSG-2"),
			source_timezone="Asia/Shanghai",
		)
		self.assertEqual(len(events), 2)

	def test_timestamp_without_offset_uses_explicit_source_timezone(self) -> None:
		event = normalize_hl7_v2_batch(
			_message(timestamp="20260730121530"),
			source_timezone="Asia/Shanghai",
		)[0]
		self.assertEqual(event["event_time"], "2026-07-30T12:15:30+08:00")

	def test_explicit_gb18030_decodes_without_fallback(self) -> None:
		raw = _message().decode().replace("Zhang^San", "张^三").encode("gb18030")
		event = normalize_hl7_v2_batch(
			raw,
			source_timezone="Asia/Shanghai",
			encoding="gb18030",
		)[0]
		self.assertEqual(event["payload"]["hl7"]["PID"]["field_5_component_1"], "张")
		with self.assertRaises(HL7V2ContractError):
			normalize_hl7_v2_batch(
				raw,
				source_timezone="Asia/Shanghai",
				encoding="utf-8",
			)

	def test_bad_encoding_version_identifier_and_frame_fail_closed(self) -> None:
		cases = (
			(_message(), {"source_timezone": "", "encoding": "utf-8"}),
			(_message(), {"source_timezone": "Asia/Shanghai", "encoding": "latin-1"}),
			(_message(control_id="bad id"), {"source_timezone": "Asia/Shanghai"}),
			(
				_message().replace(b"|2.5.1|", b"|2.2|"),
				{"source_timezone": "Asia/Shanghai"},
			),
			(b"\x0b" + _message(), {"source_timezone": "Asia/Shanghai"}),
			(
				_message().replace(b"Zhang^San", b"Zhang^San\xc2\x80"),
				{"source_timezone": "Asia/Shanghai"},
			),
		)
		for raw, kwargs in cases:
			with self.subTest(kwargs=kwargs):
				with self.assertRaises(HL7V2ContractError):
					normalize_hl7_v2_batch(raw, **kwargs)

	def test_transport_options_are_exact_bounded_and_duplicate_safe(self) -> None:
		self.assertEqual(
			normalize_hl7_v2_transport_options('{"encoding":"utf-8","source_timezone":"Asia/Shanghai"}'),
			{"encoding": "utf-8", "source_timezone": "Asia/Shanghai"},
		)
		for value in (
			{},
			{"encoding": "utf-8"},
			{
				"encoding": "utf-8",
				"source_timezone": "Asia/Shanghai",
				"secret": "forbidden",
			},
			'{"encoding":"utf-8","encoding":"gb18030","source_timezone":"Asia/Shanghai"}',
		):
			with self.subTest(value=value):
				with self.assertRaises(HL7V2ContractError):
					normalize_hl7_v2_transport_options(value)

	def test_batch_size_is_an_exact_bounded_integer(self) -> None:
		self.assertEqual(normalize_hl7_v2_batch_size(1), 1)
		self.assertEqual(normalize_hl7_v2_batch_size(500), 500)
		for value in (None, True, 1.0, "1", 0, 501):
			with self.subTest(value=value), self.assertRaises(HL7V2ContractError):
				normalize_hl7_v2_batch_size(value)

	def test_content_type_requires_one_matching_explicit_charset(self) -> None:
		for value, encoding in (
			("application/hl7-v2+er7; charset=utf-8", "utf-8"),
			('APPLICATION/HL7-V2; charset="GB18030"', "gb18030"),
		):
			with self.subTest(value=value):
				self.assertIn(
					normalize_hl7_v2_content_type(value, encoding=encoding),
					{"application/hl7-v2", "application/hl7-v2+er7"},
				)
		for value, encoding in (
			("application/hl7-v2+er7", "utf-8"),
			("application/hl7-v2+er7; charset=gb18030", "utf-8"),
			("application/hl7-v2+er7; charset=utf-8; charset=utf-8", "utf-8"),
			("application/hl7-v2+er7; profile=adt; charset=utf-8", "utf-8"),
			("application/json; charset=utf-8", "utf-8"),
			(" application/hl7-v2+er7; charset=utf-8", "utf-8"),
		):
			with self.subTest(value=value), self.assertRaises(HL7V2ContractError):
				normalize_hl7_v2_content_type(value, encoding=encoding)

	def test_optional_msh_sequence_defaults_to_zero(self) -> None:
		raw = _message(sequence="").replace(b"|2.5.1|\r", b"|2.5.1\r")
		event = normalize_hl7_v2_batch(
			raw,
			source_timezone="Asia/Shanghai",
		)[0]
		self.assertEqual(event["source_version"], "0")

	def test_duplicate_message_control_ids_are_not_silently_deduplicated_in_parser(self) -> None:
		events = normalize_hl7_v2_batch(
			_message() + _message(),
			source_timezone="Asia/Shanghai",
		)
		self.assertEqual(len(events), 2)
		self.assertEqual(events[0]["source_record_id"], events[1]["source_record_id"])
		self.assertEqual(events[0]["source_version"], events[1]["source_version"])


class TestHL7V2IngressStaticContract(unittest.TestCase):
	def test_api_hmacs_exact_body_and_parses_every_message_before_clinical_write(self) -> None:
		source = API_PATH.read_text(encoding="utf-8")
		tree = ast.parse(source)
		receive = next(
			node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "receive_hl7_v2"
		)
		receive_source = ast.get_source_segment(source, receive) or ""
		decorators = [ast.get_source_segment(source, decorator) or "" for decorator in receive.decorator_list]
		self.assertIn('frappe.whitelist(allow_guest=True, methods=["POST"])', decorators)
		self.assertIn("raw_body = _exact_request_body()", receive_source)
		self.assertIn(
			"verify_request(endpoint_doc, raw_body, force_signature=True)",
			receive_source,
		)
		self.assertLess(
			receive_source.index("verify_request(endpoint_doc, raw_body, force_signature=True)"),
			receive_source.index("events = normalize_hl7_v2_batch("),
		)
		self.assertLess(
			receive_source.index("events = normalize_hl7_v2_batch("),
			receive_source.index("receive_clinical_event(endpoint_name, event)"),
		)
		self.assertNotIn("frappe.db.commit", receive_source)

	def test_endpoint_media_mapping_and_no_listener_contracts_are_explicit(self) -> None:
		api_source = API_PATH.read_text(encoding="utf-8")
		config_source = CONFIG_PATH.read_text(encoding="utf-8")
		parser_source = MODULE_PATH.read_text(encoding="utf-8")
		self.assertIn('endpoint_doc.get("connector_type") != "HL7 v2"', api_source)
		self.assertIn('str(endpoint_doc.get("connector_key") or "") != "hl7_v2"', api_source)
		self.assertIn('endpoint_doc.get("authentication_type") != "HMAC"', api_source)
		self.assertIn('endpoint_doc.get("direction") != "Inbound"', api_source)
		self.assertIn("normalize_hl7_v2_content_type", api_source)
		self.assertIn("content_length > MAX_HL7_PAYLOAD_BYTES", api_source)
		self.assertIn("_validate_hl7_v2_endpoint_contract(doc)", config_source)
		self.assertIn('doc.get("authentication_type") != "HMAC"', config_source)
		self.assertIn("normalize_hl7_v2_transport_options", config_source)
		self.assertNotIn("patient_reference", parser_source)
		self.assertNotIn("encounter_reference", parser_source)
		self.assertNotIn("import socket", parser_source)
		self.assertNotIn(".listen(", parser_source)

	def test_generated_source_and_endpoint_options_expose_hl7_v2(self) -> None:
		generator = GENERATOR_PATH.read_text(encoding="utf-8")
		self.assertIn(r"HRP\nHL7 v2\nFHIR", generator)
		self.assertIn(r"Signed Event\nHL7 v2\nREST JSON", generator)
		self.assertIn(r"\nhl7_v2\nrest_json", generator)
		self.assertIn(r"None\nBearer\nAPI Key\nBasic\nHMAC", generator)
		source_fixture = json.loads(
			(
				ROOT
				/ "ione_qms"
				/ "ione_integration"
				/ "doctype"
				/ "ione_source_system"
				/ "ione_source_system.json"
			).read_text(encoding="utf-8")
		)
		endpoint_fixture = json.loads(
			(
				ROOT
				/ "ione_qms"
				/ "ione_integration"
				/ "doctype"
				/ "ione_integration_endpoint"
				/ "ione_integration_endpoint.json"
			).read_text(encoding="utf-8")
		)
		source_type = next(field for field in source_fixture["fields"] if field["fieldname"] == "system_type")
		connector_type = next(
			field for field in endpoint_fixture["fields"] if field["fieldname"] == "connector_type"
		)
		connector_key = next(
			field for field in endpoint_fixture["fields"] if field["fieldname"] == "connector_key"
		)
		authentication_type = next(
			field for field in endpoint_fixture["fields"] if field["fieldname"] == "authentication_type"
		)
		connector_options = next(
			field for field in endpoint_fixture["fields"] if field["fieldname"] == "connector_options"
		)
		self.assertIn("HL7 v2", source_type["options"].splitlines())
		self.assertIn("HL7 v2", connector_type["options"].splitlines())
		self.assertIn("hl7_v2", connector_key["options"].splitlines())
		self.assertIn("HMAC", authentication_type["options"].splitlines())
		self.assertIn("source_timezone", connector_options["description"])
		self.assertIn("Content-Type", connector_options["description"])


if __name__ == "__main__":
	unittest.main()
