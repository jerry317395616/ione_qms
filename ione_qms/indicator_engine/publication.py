from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

PUBLICATION_CONTRACT_VERSION = "ione-indicator-publication-v1"

# This list is intentionally explicit. Runtime reproduction must not depend on
# Frappe's mutable DocType field order or on later non-governance UI fields.
INDICATOR_PUBLICATION_CONTENT_FIELDS = (
	"indicator",
	"version",
	"indicator_code_snapshot",
	"indicator_name_snapshot",
	"category_snapshot",
	"definition_snapshot",
	"parent_calculator_key_snapshot",
	"parent_formula_json_snapshot",
	"standard_snapshot",
	"standard_version_snapshot",
	"standard_version_checksum_snapshot",
	"standard_clause_snapshot",
	"standard_clause_hash_snapshot",
	"authority_snapshot_json",
	"authority_snapshot_hash",
	"lineage_status",
	"calculator_key",
	"numerator_definition",
	"denominator_definition",
	"formula_json",
	"dimensions_json",
	"source_mapping",
	"source_system_snapshot",
	"mapping_version_snapshot",
	"mapping_checksum_snapshot",
	"query_contract_hash",
	"physical_query_contract_json",
	"physical_query_contract_hash",
	"schema_signature_hash",
	"calculator_version_snapshot",
	"calculator_code_hash",
	"calculation_frequency",
	"rolling_window_days",
	"unit",
	"multiplier",
	"precision",
	"target_value",
	"target_min",
	"target_max",
	"warning_threshold",
	"critical_threshold",
	"direction",
	"effective_from",
)


def build_indicator_publication_contract(source) -> dict[str, Any]:
	"""Build the immutable contract signed at the first Published transition."""
	return {
		"contract_version": PUBLICATION_CONTRACT_VERSION,
		"content": {
			fieldname: _json_safe(source.get(fieldname)) for fieldname in INDICATOR_PUBLICATION_CONTENT_FIELDS
		},
		# Retirement may later close the operational interval. Preserve the
		# originally reviewed endpoint so that historical receipts never drift.
		"published_effective_to": _json_safe(source.get("effective_to")),
	}


def publication_contract_checksum(contract: dict[str, Any]) -> str:
	if (
		not isinstance(contract, dict)
		or contract.get("contract_version") != PUBLICATION_CONTRACT_VERSION
		or not isinstance(contract.get("content"), dict)
	):
		raise ValueError("Indicator publication contract is invalid.")
	return hashlib.sha256(canonical_publication_json(contract).encode("utf-8")).hexdigest()


def freeze_indicator_publication_contract(source) -> tuple[str, str]:
	contract = build_indicator_publication_contract(source)
	contract_json = canonical_publication_json(contract)
	return contract_json, publication_contract_checksum(contract)


def verify_indicator_publication_contract(source) -> dict[str, Any]:
	"""Verify immutable publication content while allowing a reviewed retirement end."""
	raw = source.get("publication_contract_json")
	try:
		contract = json.loads(raw) if isinstance(raw, str) else raw
	except ValueError as exc:
		raise ValueError("Indicator publication contract is not valid JSON.") from exc
	if not isinstance(contract, dict):
		raise ValueError("Indicator publication contract is required.")
	observed = publication_contract_checksum(contract)
	published = str(source.get("publication_checksum") or "").strip().lower()
	checksum = str(source.get("checksum") or "").strip().lower()
	if not _is_sha256(published) or not hmac.compare_digest(observed, published):
		raise ValueError("Indicator publication checksum verification failed.")
	if not _is_sha256(checksum) or not hmac.compare_digest(published, checksum):
		raise ValueError("Indicator version checksum must equal its publication checksum.")
	content = contract.get("content")
	for fieldname in INDICATOR_PUBLICATION_CONTENT_FIELDS:
		if canonical_publication_json(_json_safe(source.get(fieldname))) != canonical_publication_json(
			content.get(fieldname)
		):
			raise ValueError(f"Indicator publication field {fieldname} drifted.")
	status = str(source.get("status") or source.get("approval_status") or "").strip()
	if status not in {"Published", "Retired"}:
		raise ValueError("Indicator publication contract is authoritative only when Published or Retired.")
	if status == "Retired" and not source.get("effective_to"):
		raise ValueError("Retired indicator publication requires an operational effective_to.")
	return contract


def canonical_publication_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)


def _json_safe(value: Any) -> Any:
	if value is None or isinstance(value, str | bool | int | float):
		return value
	if isinstance(value, dict):
		return {str(key): _json_safe(item) for key, item in value.items()}
	if isinstance(value, list | tuple):
		return [_json_safe(item) for item in value]
	return str(value)


def _is_sha256(value: str) -> bool:
	return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
