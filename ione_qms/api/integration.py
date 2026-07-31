from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import now_datetime

from ione_qms.integration.hl7_v2 import (
	MAX_HL7_PAYLOAD_BYTES,
	HL7V2ContractError,
	normalize_hl7_v2_batch,
	normalize_hl7_v2_batch_size,
	normalize_hl7_v2_content_type,
	normalize_hl7_v2_transport_options,
)
from ione_qms.integration.schemas import validate_payload_size
from ione_qms.integration.service import receive_clinical_event
from ione_qms.integration.signing import verify_request
from ione_qms.permissions import require_role
from ione_qms.services.integration_config import assert_integration_endpoint_runtime
from ione_qms.tasks.integration import replay_dead_letter


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_event(
	endpoint: str | None = None,
	payload: dict[str, Any] | str | None = None,
	**event_fields: Any,
):
	endpoint_name = (
		endpoint or frappe.get_request_header("X-IONE-Endpoint") or frappe.form_dict.get("endpoint")
	)
	if not endpoint_name:
		frappe.throw("Integration endpoint is required")
	endpoint_doc = frappe.get_cached_doc("IONE Integration Endpoint", endpoint_name)
	if endpoint_doc.get("direction") != "Inbound":
		frappe.throw(
			"Only enabled inbound endpoints may receive pushed events",
			frappe.PermissionError,
		)
	raw_body = _raw_body(payload)
	guest_request = str(frappe.session.user or "") == "Guest"
	_require_json_request(endpoint_doc, force=guest_request)
	try:
		validate_payload_size(raw_body)
	except ValueError as exc:
		frappe.local.response["http_status_code"] = 413
		frappe.throw(str(exc), frappe.ValidationError)
	verify_request(endpoint_doc, raw_body, force_signature=guest_request)
	return receive_clinical_event(
		endpoint_name,
		_parse_payload(raw_body, endpoint_name),
	)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_hl7_v2(endpoint: str | None = None) -> dict[str, Any]:
	"""Receive a signed ER7 batch over HTTP without interpreting clinical identifiers."""
	endpoint_name = _hl7_endpoint_name(endpoint)
	endpoint_doc = frappe.get_cached_doc("IONE Integration Endpoint", endpoint_name)
	_assert_hl7_endpoint_contract(endpoint_doc)
	raw_body = _exact_request_body()
	if len(raw_body) > MAX_HL7_PAYLOAD_BYTES:
		frappe.local.response["http_status_code"] = 413
		frappe.throw("HL7 v2 payload exceeds the 2 MiB ingress limit", frappe.ValidationError)
	verify_request(endpoint_doc, raw_body, force_signature=True)

	try:
		options = normalize_hl7_v2_transport_options(endpoint_doc.get("connector_options"))
	except HL7V2ContractError as exc:
		raise frappe.ValidationError(str(exc)) from exc
	_require_hl7_content_type(options["encoding"])
	assert_integration_endpoint_runtime(endpoint_doc)
	try:
		events = normalize_hl7_v2_batch(
			raw_body,
			source_timezone=options["source_timezone"],
			encoding=options["encoding"],
		)
	except HL7V2ContractError as exc:
		frappe.local.response["http_status_code"] = 422
		raise frappe.ValidationError(str(exc)) from exc

	batch_size = _hl7_batch_size(endpoint_doc)
	if len(events) > batch_size:
		frappe.local.response["http_status_code"] = 413
		frappe.throw(
			f"HL7 v2 batch exceeds this endpoint's {batch_size}-message limit",
			frappe.ValidationError,
		)

	# Parsing and every batch-level limit check complete before the first
	# clinical receipt is created. Frappe owns the request transaction.
	results = [receive_clinical_event(endpoint_name, event) for event in events]
	return {
		"endpoint": endpoint_name,
		"message_count": len(events),
		"results": results,
	}


@frappe.whitelist(methods=["POST"])
def replay_message(message: str, reason: str) -> dict[str, Any]:
	if frappe.session.user == "Administrator":
		frappe.throw(
			"Administrator cannot replay clinical integration messages; use a named operator.",
			frappe.PermissionError,
		)
	require_role("IONE Integration Operator")
	doc = frappe.get_doc("IONE Integration Message", message)
	doc.check_permission("read")
	return replay_dead_letter(
		message,
		reason=reason,
		requested_by=frappe.session.user,
	)


@frappe.whitelist(methods=["POST"])
def review_data_quality_issue(
	issue: str,
	decision: str,
	comment: str,
) -> dict[str, Any]:
	if frappe.session.user == "Administrator":
		frappe.throw(
			"Administrator cannot decide data-quality issues; use a named operator.",
			frappe.PermissionError,
		)
	require_role("IONE Integration Operator")
	status = {
		"Investigate": "Investigating",
		"Resolve": "Resolved",
		"Accept": "Accepted",
	}.get(str(decision or ""))
	if not status:
		frappe.throw("Decision must be Investigate, Resolve, or Accept")
	comment = str(comment or "").strip()
	if len(comment) < 10 or len(comment) > 5_000:
		frappe.throw("Data-quality review comment must contain 10-5,000 characters")
	with frappe.db.advisory_lock(f"ione-qms:data-quality-review:{issue}", timeout=5):
		doc = frappe.get_doc("IONE Data Quality Issue", issue, for_update=True)
		doc.check_permission("read")
		if doc.status in {"Resolved", "Accepted"}:
			frappe.throw("Data-quality issue is already terminal")
		values = {
			"status": status,
			"resolution": comment,
		}
		if status in {"Resolved", "Accepted"}:
			values.update(
				{
					"resolved_by": frappe.session.user,
					"resolved_at": now_datetime(),
				}
			)
		doc.db_set(values, update_modified=True)
		frappe.db.commit()
	return {"issue": doc.name, "status": status}


def _raw_body(payload: dict[str, Any] | str | None) -> bytes:
	request = getattr(frappe.local, "request", None)
	if request:
		body = request.get_data(cache=True)
		if body:
			return body
	if payload is not None:
		if isinstance(payload, str):
			return payload.encode()
		return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
	return b"{}"


def _parse_payload(raw_body: bytes, endpoint_name: str) -> dict[str, Any]:
	try:
		parsed = json.loads(raw_body.decode("utf-8"), object_pairs_hook=_unique_json_object)
	except (UnicodeDecodeError, ValueError) as exc:
		raise frappe.ValidationError("Request body must contain one valid JSON object") from exc
	if not isinstance(parsed, dict):
		frappe.throw("Request payload must be a JSON object")
	body_endpoint = str(parsed.get("endpoint") or "").strip()
	if body_endpoint and body_endpoint != endpoint_name:
		frappe.throw("Signed request endpoint does not match its JSON body")
	if isinstance(parsed.get("payload"), dict) and set(parsed).issubset({"endpoint", "payload"}):
		return parsed["payload"]
	return {key: value for key, value in parsed.items() if key != "endpoint"}


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
	output: dict[str, Any] = {}
	for key, value in pairs:
		if key in output:
			raise ValueError("Duplicate JSON object key")
		output[key] = value
	return output


def _require_json_request(endpoint_doc, *, force: bool = False) -> None:
	if not force and not int(endpoint_doc.get("require_signature") or 0):
		return
	request = getattr(frappe.local, "request", None)
	if request is not None and not bool(getattr(request, "is_json", False)):
		frappe.local.response["http_status_code"] = 415
		frappe.throw("Signed integration requests require application/json")


def _hl7_endpoint_name(endpoint: str | None) -> str:
	argument = str(endpoint or "")
	header = str(frappe.get_request_header("X-IONE-Endpoint") or "")
	if argument != argument.strip() or header != header.strip():
		frappe.throw("Integration endpoint is invalid")
	if argument and header and argument != header:
		frappe.throw("Integration endpoint selectors do not match")
	name = argument or header
	if not name:
		frappe.throw("Integration endpoint is required")
	return name


def _assert_hl7_endpoint_contract(endpoint_doc) -> None:
	if (
		not int(endpoint_doc.get("enabled") or 0)
		or endpoint_doc.get("direction") != "Inbound"
		or endpoint_doc.get("connector_type") != "HL7 v2"
		or str(endpoint_doc.get("connector_key") or "") != "hl7_v2"
		or endpoint_doc.get("authentication_type") != "HMAC"
		or not int(endpoint_doc.get("require_signature") or 0)
	):
		frappe.throw(
			"Only enabled, signed HL7 v2 inbound endpoints may receive HL7 v2 messages",
			frappe.PermissionError,
		)
	_hl7_batch_size(endpoint_doc)


def _hl7_batch_size(endpoint_doc) -> int:
	try:
		return normalize_hl7_v2_batch_size(endpoint_doc.get("batch_size"))
	except HL7V2ContractError as exc:
		raise frappe.ValidationError(str(exc)) from exc


def _exact_request_body() -> bytes:
	request = getattr(frappe.local, "request", None)
	if request is None:
		frappe.throw("HL7 v2 ingress requires an HTTP request body")
	content_length = getattr(request, "content_length", None)
	if content_length is not None:
		if type(content_length) is not int or content_length < 0:
			raise frappe.ValidationError("HL7 v2 Content-Length is invalid")
		if content_length > MAX_HL7_PAYLOAD_BYTES:
			frappe.local.response["http_status_code"] = 413
			frappe.throw("HL7 v2 payload exceeds the 2 MiB ingress limit", frappe.ValidationError)
	body = request.get_data(cache=True, as_text=False)
	if type(body) is not bytes:
		raise frappe.ValidationError("HL7 v2 request body is not an exact byte sequence")
	if content_length is not None and len(body) != content_length:
		raise frappe.ValidationError("HL7 v2 request body does not match Content-Length")
	return body


def _require_hl7_content_type(configured_encoding: str) -> None:
	header = str(frappe.get_request_header("Content-Type") or "")
	try:
		normalize_hl7_v2_content_type(header, encoding=configured_encoding)
	except HL7V2ContractError:
		_unsupported_hl7_media_type()


def _unsupported_hl7_media_type() -> None:
	frappe.local.response["http_status_code"] = 415
	frappe.throw(
		"HL7 v2 ingress requires application/hl7-v2+er7 "
		"(or application/hl7-v2) with one explicit configured charset",
		frappe.ValidationError,
	)
