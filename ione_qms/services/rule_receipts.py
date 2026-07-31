from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any

import frappe

from ione_qms.services.crypto_keys import active_hmac_key, verification_hmac_key

RULE_RECEIPT_SIGNATURE_VERSION = "IONE-RULE-RECEIPT-HMAC-SHA256-V1"
RULE_RECEIPT_HMAC_CONFIG_PREFIX = "ione_rule_receipt_hmac"
MAX_RULE_RECEIPT_CHAIN_ROWS = 100_000
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_CHAIN_FIELDS = (
	"receipt_chain_scope",
	"receipt_chain_sequence",
	"receipt_chain_key",
	"previous_receipt",
	"previous_receipt_hmac",
	"signature_version",
	"hmac_key_id",
)

_SIGNED_FIELDS: dict[str, tuple[str, ...]] = {
	"IONE QC Rule Validation Run": (
		"run_key",
		"rule_version",
		"rule_checksum",
		"run_type",
		"status",
		"window_start",
		"window_end",
		"event_count",
		"passed_count",
		"failed_count",
		"excluded_count",
		"insufficient_data_count",
		"error_count",
		"sample_hash",
		"result_hash",
		"outcome_manifest_json",
		"summary_json",
		"executor_version",
		"requested_by",
		"started_at",
		"completed_at",
		*_CHAIN_FIELDS,
	),
	"IONE QC Rule Shadow Execution": (
		"execution_key",
		"event",
		"rule",
		"rule_version",
		"rule_checksum",
		"hospital",
		"campus",
		"department",
		"ward",
		"result",
		"event_hash",
		"context_hash",
		"evaluation_hash",
		"executor_version",
		"observed_at",
		"completed_at",
		"duration_ms",
		*_CHAIN_FIELDS,
	),
	"IONE QC Rule Shadow Feedback": (
		"feedback_key",
		"execution_feedback_key",
		"shadow_execution",
		"rule_version",
		"rule_checksum",
		"hospital",
		"campus",
		"department",
		"ward",
		"gold_label",
		"assessment",
		"comment",
		"reviewed_by",
		"reviewed_at",
		*_CHAIN_FIELDS,
	),
}


def receipt_chain_scope(doctype: str, rule_version: str, rule_checksum: str) -> str:
	_require_supported_doctype(doctype)
	return hashlib.sha256(
		_canonical_json(
			{
				"contract_version": 1,
				"doctype": doctype,
				"rule_version": str(rule_version or ""),
				"rule_checksum": str(rule_checksum or ""),
			}
		).encode()
	).hexdigest()


def prepare_signed_receipt(doctype: str, values: Any) -> Any:
	"""Append HMAC and predecessor-chain fields while the caller holds the rule-version row lock."""
	_require_supported_doctype(doctype)
	rule_version = str(values.get("rule_version") or "")
	rule_checksum = str(values.get("rule_checksum") or "")
	if not rule_version or not _SHA256_PATTERN.fullmatch(rule_checksum):
		frappe.throw("Rule receipt requires an exact rule version and SHA-256 checksum")
	scope = receipt_chain_scope(doctype, rule_version, rule_checksum)
	previous_rows = frappe.get_all(
		doctype,
		filters={"receipt_chain_scope": scope},
		fields=_receipt_query_fields(doctype),
		order_by="receipt_chain_sequence desc, name desc",
		limit_page_length=2,
	)
	if len(previous_rows) > 1 and int(previous_rows[0].get("receipt_chain_sequence") or 0) == int(
		previous_rows[1].get("receipt_chain_sequence") or 0
	):
		frappe.throw(f"{doctype} receipt chain has a duplicate sequence")
	previous = previous_rows[0] if previous_rows else None
	if previous:
		verify_signed_receipt(doctype, previous)
	sequence = int(previous.get("receipt_chain_sequence") or 0) + 1 if previous else 1
	key_id, secret = _receipt_hmac_key()
	values.update(
		{
			"receipt_chain_scope": scope,
			"receipt_chain_sequence": sequence,
			"receipt_chain_key": _receipt_chain_key(scope, sequence),
			"previous_receipt": str(previous.get("name") or "") if previous else None,
			"previous_receipt_hmac": str(previous.get("receipt_hmac") or "") if previous else None,
			"signature_version": RULE_RECEIPT_SIGNATURE_VERSION,
			"hmac_key_id": key_id,
		}
	)
	values["receipt_hmac"] = compute_receipt_hmac(doctype, values, secret)
	return values


def verify_signed_receipt(doctype: str, values: Any) -> None:
	_require_supported_doctype(doctype)
	rule_version = str(_value(values, "rule_version") or "")
	rule_checksum = str(_value(values, "rule_checksum") or "")
	scope = receipt_chain_scope(doctype, rule_version, rule_checksum)
	sequence = int(_value(values, "receipt_chain_sequence") or 0)
	if (
		str(_value(values, "receipt_chain_scope") or "") != scope
		or sequence < 1
		or str(_value(values, "receipt_chain_key") or "") != _receipt_chain_key(scope, sequence)
		or str(_value(values, "signature_version") or "") != RULE_RECEIPT_SIGNATURE_VERSION
	):
		frappe.throw(f"{doctype} receipt chain identity is invalid")
	previous_receipt = str(_value(values, "previous_receipt") or "")
	previous_hmac = str(_value(values, "previous_receipt_hmac") or "")
	if (sequence == 1 and (previous_receipt or previous_hmac)) or (
		sequence > 1 and (not previous_receipt or not _SHA256_PATTERN.fullmatch(previous_hmac))
	):
		frappe.throw(f"{doctype} receipt predecessor identity is invalid")
	key_id = str(_value(values, "hmac_key_id") or "")
	_configured_key_id, secret = _receipt_hmac_key(key_id)
	stored = str(_value(values, "receipt_hmac") or "")
	expected = compute_receipt_hmac(doctype, values, secret)
	if not _SHA256_PATTERN.fullmatch(stored) or not hmac.compare_digest(stored, expected):
		frappe.throw(f"{doctype} receipt HMAC is invalid")


def verify_receipt_chain(
	doctype: str,
	*,
	rule_version: str,
	rule_checksum: str,
	expected_names: set[str] | None = None,
	required_head: str | None = None,
	max_rows: int = MAX_RULE_RECEIPT_CHAIN_ROWS,
) -> list[Any]:
	"""Verify the complete retained chain for one rule checksum, including its selected head."""
	_require_supported_doctype(doctype)
	limit = min(max(int(max_rows or 0), 1), MAX_RULE_RECEIPT_CHAIN_ROWS)
	scope = receipt_chain_scope(doctype, rule_version, rule_checksum)
	rows = frappe.get_all(
		doctype,
		filters={"receipt_chain_scope": scope},
		fields=_receipt_query_fields(doctype),
		order_by="receipt_chain_sequence asc, name asc",
		limit_page_length=limit + 1,
	)
	if not rows:
		frappe.throw(f"{doctype} receipt chain is unavailable")
	if len(rows) > limit:
		frappe.throw(f"{doctype} receipt chain exceeds its governed verification bound")
	previous = None
	for expected_sequence, row in enumerate(rows, start=1):
		verify_signed_receipt(doctype, row)
		if int(row.get("receipt_chain_sequence") or 0) != expected_sequence:
			frappe.throw(f"{doctype} receipt chain contains a sequence gap")
		if previous is None:
			if row.get("previous_receipt") or row.get("previous_receipt_hmac"):
				frappe.throw(f"{doctype} receipt chain genesis is invalid")
		elif str(row.get("previous_receipt") or "") != str(
			previous.get("name") or ""
		) or not hmac.compare_digest(
			str(row.get("previous_receipt_hmac") or ""),
			str(previous.get("receipt_hmac") or ""),
		):
			frappe.throw(f"{doctype} receipt predecessor chain is broken")
		previous = row
	actual_names = {str(row.get("name") or "") for row in rows}
	if expected_names is not None and actual_names != {str(name) for name in expected_names}:
		frappe.throw(f"{doctype} receipt chain does not match the governed receipt population")
	if required_head and str(rows[-1].get("name") or "") != str(required_head):
		frappe.throw(f"{doctype} selected receipt is not the current signed chain head")
	return rows


def compute_receipt_hmac(doctype: str, values: Any, secret: str | bytes) -> str:
	"""Pure deterministic HMAC primitive used by both writers and release gates."""
	_require_supported_doctype(doctype)
	if isinstance(secret, str):
		key_material = secret.encode()
	elif isinstance(secret, bytes):
		key_material = secret
	else:
		raise ValueError("Rule receipt HMAC keys must be bytes or text")
	if len(key_material) < 32:
		raise ValueError("Rule receipt HMAC keys must contain at least 32 bytes")
	payload = {
		"doctype": doctype,
		**{fieldname: _value(values, fieldname) for fieldname in _SIGNED_FIELDS[doctype]},
	}
	return hmac.new(key_material, _canonical_json(payload).encode(), hashlib.sha256).hexdigest()


def _receipt_hmac_key(key_id: str | None = None) -> tuple[str, bytes]:
	key = (
		verification_hmac_key(RULE_RECEIPT_HMAC_CONFIG_PREFIX, key_id)
		if key_id
		else active_hmac_key(RULE_RECEIPT_HMAC_CONFIG_PREFIX)
	)
	return key.key_id, key.secret


def _receipt_query_fields(doctype: str) -> list[str]:
	return ["name", *_SIGNED_FIELDS[doctype], "receipt_hmac"]


def _receipt_chain_key(scope: str, sequence: int) -> str:
	return hashlib.sha256(
		_canonical_json(
			{
				"contract_version": 1,
				"scope": scope,
				"sequence": int(sequence),
			}
		).encode()
	).hexdigest()


def _require_supported_doctype(doctype: str) -> None:
	if doctype not in _SIGNED_FIELDS:
		raise ValueError(f"Unsupported governed rule receipt DocType: {doctype}")


def _value(values: Any, fieldname: str) -> Any:
	if isinstance(values, dict):
		return values.get(fieldname)
	getter = getattr(values, "get", None)
	return getter(fieldname) if callable(getter) else getattr(values, fieldname, None)


def _canonical_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)
