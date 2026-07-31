from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

import frappe

from ione_qms.api import integration as integration_api
from ione_qms.services.integration_config import _validate_hl7_v2_endpoint_contract


def _message(control_id: str = "MSG-0001") -> bytes:
	return (
		"MSH|^~\\&|EMR|HOSP|IONE|HOSP|20260730121530+0800||"
		f"ADT^A01^ADT_A01|{control_id}|P|2.5.1|7\r"
		"EVN|A01|20260730121500+0800\r"
		"PID|1||P-0001^^^MPI||Zhang^San\r"
	).encode()


def _endpoint(**overrides):
	values = {
		"name": "HL7-ENDPOINT",
		"enabled": 1,
		"direction": "Inbound",
		"connector_type": "HL7 v2",
		"connector_key": "hl7_v2",
		"authentication_type": "HMAC",
		"require_signature": 1,
		"batch_size": 2,
		"connector_options": ('{"encoding":"utf-8","source_timezone":"Asia/Shanghai"}'),
	}
	values.update(overrides)
	return frappe._dict(values)


class _Request:
	def __init__(self, body: bytes):
		self.body = body
		self.content_length = len(body)
		self.calls: list[tuple[bool, bool]] = []

	def get_data(self, *, cache: bool, as_text: bool) -> bytes:
		self.calls.append((cache, as_text))
		return self.body


class TestHL7V2Ingress(TestCase):
	def test_route_is_guest_reachable_and_post_only(self) -> None:
		self.assertIn(integration_api.receive_hl7_v2, frappe.guest_methods)
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[integration_api.receive_hl7_v2],
			("POST",),
		)

	def test_endpoint_contract_requires_exact_hmac_identity(self) -> None:
		_validate_hl7_v2_endpoint_contract(_endpoint())
		integration_api._assert_hl7_endpoint_contract(_endpoint())
		for values in (
			{"connector_type": "Signed Event"},
			{"connector_key": "rest_json"},
			{"authentication_type": "Bearer"},
			{"require_signature": 0},
			{"direction": "Pull"},
			{"batch_size": 501},
		):
			doc = _endpoint(**values)
			with (
				self.subTest(layer="configuration", values=values),
				self.assertRaises((frappe.PermissionError, frappe.ValidationError)),
			):
				_validate_hl7_v2_endpoint_contract(doc)
			with (
				self.subTest(layer="ingress", values=values),
				self.assertRaises((frappe.PermissionError, frappe.ValidationError)),
			):
				integration_api._assert_hl7_endpoint_contract(doc)

	def test_signed_exact_body_batch_is_normalized_before_receipt(self) -> None:
		body = _message("MSG-1") + _message("MSG-2")
		request = _Request(body)
		headers = {
			"X-IONE-Endpoint": "HL7-ENDPOINT",
			"Content-Type": "application/hl7-v2+er7; charset=utf-8",
		}
		endpoint = _endpoint()
		with (
			patch.object(integration_api.frappe, "get_request_header", side_effect=headers.get),
			patch.object(integration_api.frappe, "get_cached_doc", return_value=endpoint),
			patch.object(integration_api, "assert_integration_endpoint_runtime") as runtime,
			patch.object(integration_api, "verify_request") as verify,
			patch.object(
				integration_api,
				"receive_clinical_event",
				side_effect=lambda _endpoint, event: {"source_record_id": event["source_record_id"]},
			) as receive,
		):
			previous_request = getattr(frappe.local, "request", None)
			frappe.local.request = request
			try:
				result = integration_api.receive_hl7_v2("HL7-ENDPOINT")
			finally:
				frappe.local.request = previous_request
		verify.assert_called_once_with(endpoint, body, force_signature=True)
		runtime.assert_called_once_with(endpoint)
		self.assertEqual(request.calls, [(True, False)])
		self.assertEqual(receive.call_count, 2)
		self.assertEqual(
			result,
			{
				"endpoint": "HL7-ENDPOINT",
				"message_count": 2,
				"results": [
					{"source_record_id": "MSG-1"},
					{"source_record_id": "MSG-2"},
				],
			},
		)

	def test_declared_oversize_is_rejected_before_body_read(self) -> None:
		request = _Request(b"")
		request.content_length = integration_api.MAX_HL7_PAYLOAD_BYTES + 1
		previous_request = getattr(frappe.local, "request", None)
		frappe.local.request = request
		try:
			with self.assertRaises(frappe.ValidationError):
				integration_api._exact_request_body()
		finally:
			frappe.local.request = previous_request
		self.assertEqual(request.calls, [])

	def test_malformed_later_message_creates_no_clinical_receipt(self) -> None:
		body = _message("MSG-1") + _message("bad id")
		request = _Request(body)
		headers = {
			"X-IONE-Endpoint": "HL7-ENDPOINT",
			"Content-Type": "application/hl7-v2+er7; charset=utf-8",
		}
		endpoint = _endpoint()
		with (
			patch.object(integration_api.frappe, "get_request_header", side_effect=headers.get),
			patch.object(integration_api.frappe, "get_cached_doc", return_value=endpoint),
			patch.object(integration_api, "assert_integration_endpoint_runtime"),
			patch.object(integration_api, "verify_request"),
			patch.object(integration_api, "receive_clinical_event") as receive,
		):
			previous_request = getattr(frappe.local, "request", None)
			frappe.local.request = request
			try:
				with self.assertRaises(frappe.ValidationError):
					integration_api.receive_hl7_v2("HL7-ENDPOINT")
			finally:
				frappe.local.request = previous_request
		receive.assert_not_called()
