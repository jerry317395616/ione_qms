from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import frappe

_CONNECTORS: dict[str, type[BaseConnector]] = {}
_QUARANTINE_MARKER_KEYS = frozenset(
	{
		"connector_quarantine",
		"connector",
		"reason_code",
		"position",
		"content_sha256",
		"size_bytes",
	}
)
_QUARANTINE_MARKER_CONTRACTS = {
	"REST_ITEM_NOT_OBJECT": ("rest_json", frozenset({"page_index", "cursor_sha256"})),
	"FHIR_ENTRY_NOT_OBJECT": ("fhir", frozenset({"page_index", "cursor_sha256"})),
	"FHIR_ENTRY_RESOURCE_NOT_OBJECT": ("fhir", frozenset({"page_index", "cursor_sha256"})),
	"FHIR_RESOURCE_ENVELOPE_INVALID": ("fhir", frozenset({"page_index", "cursor_sha256"})),
	"FILE_LINE_TOO_LARGE": ("file_drop", frozenset({"file_name_sha256", "line"})),
	"FILE_LINE_INVALID_UTF8": ("file_drop", frozenset({"file_name_sha256", "line"})),
	"FILE_JSONL_INVALID_JSON": ("file_drop", frozenset({"file_name_sha256", "line"})),
	"FILE_JSONL_NOT_OBJECT": ("file_drop", frozenset({"file_name_sha256", "line"})),
	"FILE_CSV_HEADER_TOO_LARGE": ("file_drop", frozenset({"file_name_sha256", "line"})),
	"FILE_CSV_HEADER_INVALID": ("file_drop", frozenset({"file_name_sha256", "line"})),
	"FILE_CSV_HEADER_UNAVAILABLE": ("file_drop", frozenset({"file_name_sha256", "line"})),
	"FILE_CSV_ROW_INVALID": ("file_drop", frozenset({"file_name_sha256", "line"})),
	"ORACLE_ROW_ENVELOPE_INVALID": ("oracle", frozenset({"page_index"})),
}
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_MAX_QUARANTINE_MARKER_BYTES = 2048
_MAX_QUARANTINE_POSITION_INTEGER = (1 << 63) - 1
_RECONCILIATION_DETAIL_KEYS = frozenset(
	{
		"query_key",
		"period_semantics",
		"partition",
		"source_view_version",
		"count_semantics",
	}
)


class ConnectorQuarantineRecord(str):
	"""Sanitized, bounded record marker consumed by the outer quarantine path.

	The integration task deliberately treats non-dict connector items as invalid
	records.  A string subclass lets connectors preserve only non-clinical
	identity/hash/position metadata without teaching the task about connector
	implementations or accidentally retaining the rejected clinical body.
	"""


class ConnectorRecordError(frappe.ValidationError):
	"""A source-record contract error that may be quarantined independently."""


ConnectorItem = dict[str, Any] | ConnectorQuarantineRecord


def decode_connector_quarantine_record(value: Any) -> dict[str, Any] | None:
	"""Decode one canonical connector marker or reject it without retaining values."""
	if type(value) is not ConnectorQuarantineRecord:
		return None
	encoded = str(value)
	try:
		encoded_size = len(encoded.encode("utf-8"))
	except UnicodeEncodeError:
		return None
	if encoded_size > _MAX_QUARANTINE_MARKER_BYTES:
		return None
	try:
		payload = json.loads(encoded, object_pairs_hook=_unique_json_object)
	except TypeError, ValueError:
		return None
	if not isinstance(payload, dict) or frozenset(payload) != _QUARANTINE_MARKER_KEYS:
		return None
	if type(payload["connector_quarantine"]) is not int or payload["connector_quarantine"] != 1:
		return None
	connector = payload["connector"]
	reason_code = payload["reason_code"]
	if not isinstance(connector, str) or not isinstance(reason_code, str):
		return None
	contract = _QUARANTINE_MARKER_CONTRACTS.get(reason_code)
	if contract is None or connector != contract[0]:
		return None
	position = payload["position"]
	if not isinstance(position, dict) or frozenset(position) != contract[1]:
		return None
	if not _valid_quarantine_position(position):
		return None
	content_hash = payload["content_sha256"]
	if not isinstance(content_hash, str) or not _SHA256_HEX.fullmatch(content_hash):
		return None
	size_bytes = payload["size_bytes"]
	if type(size_bytes) is not int or size_bytes < 0 or size_bytes > _MAX_QUARANTINE_POSITION_INTEGER:
		return None
	canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
	if canonical != encoded:
		return None
	return payload


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
	value: dict[str, Any] = {}
	for key, item in pairs:
		if key in value:
			raise ValueError("Duplicate connector quarantine marker key")
		value[key] = item
	return value


def _valid_quarantine_position(position: dict[str, Any]) -> bool:
	for key, value in position.items():
		if key in {"cursor_sha256", "file_name_sha256"}:
			if not isinstance(value, str) or not _SHA256_HEX.fullmatch(value):
				return False
		elif key == "page_index":
			if type(value) is not int or value < 0 or value > 4999:
				return False
		elif key == "line":
			if type(value) is not int or value < 1 or value > _MAX_QUARANTINE_POSITION_INTEGER:
				return False
		else:
			return False
	return True


@dataclass(frozen=True)
class ReconciliationSnapshot:
	"""Authoritative, bounded source count returned by reviewed connector code."""

	source_count: int
	source_hash: str | None = None
	details: dict[str, Any] | None = None

	def __post_init__(self) -> None:
		if isinstance(self.source_count, bool) or not isinstance(self.source_count, int):
			raise TypeError("Reconciliation source_count must be an integer")
		if self.source_count < 0:
			raise ValueError("Reconciliation source_count cannot be negative")
		if self.source_hash is not None:
			if not isinstance(self.source_hash, str):
				raise TypeError("Reconciliation source_hash must be a string")
			if len(self.source_hash) > 64:
				raise ValueError("Reconciliation source_hash is too long")
		if self.details is not None and not isinstance(self.details, dict):
			raise TypeError("Reconciliation details must be an object")
		for key, value in (self.details or {}).items():
			if key not in _RECONCILIATION_DETAIL_KEYS:
				raise ValueError(f"Unsupported reconciliation detail: {key}")
			if not isinstance(value, str | int | bool) or len(str(value)) > 500:
				raise ValueError(f"Invalid reconciliation detail value: {key}")


class BaseConnector(ABC):
	def __init__(self, endpoint):
		self.endpoint = endpoint

	@abstractmethod
	def fetch(self, cursor: str | None, limit: int) -> tuple[list[ConnectorItem], str | None]:
		"""Fetch a bounded page from a read-only source."""

	def reconcile(
		self,
		period_start: datetime,
		period_end: datetime,
	) -> ReconciliationSnapshot | None:
		"""Return a reviewed source-of-truth aggregate, or ``None`` when not configured.

		A connector must never infer source truth from records already ingested by QMS.
		Hospital-specific source queries are registered in reviewed application code.
		"""
		del period_start, period_end
		return None


def register_connector(key: str) -> Callable[[type[BaseConnector]], type[BaseConnector]]:
	normalized = key.strip().lower()

	def decorator(connector: type[BaseConnector]) -> type[BaseConnector]:
		if normalized in _CONNECTORS:
			raise ValueError(f"Duplicate integration connector: {normalized}")
		if not issubclass(connector, BaseConnector):
			raise TypeError("Integration connectors must inherit BaseConnector")
		_CONNECTORS[normalized] = connector
		return connector

	return decorator


def get_connector(endpoint) -> BaseConnector:
	key = str(endpoint.get("connector_key") or "").strip().lower()
	connector = _CONNECTORS.get(key)
	if not connector:
		frappe.throw(
			f"Connector '{key}' is not registered in reviewed IONE QMS code. "
			"Arbitrary SQL or Python connectors are not permitted."
		)
	return connector(endpoint)


def registered_connectors() -> tuple[str, ...]:
	return tuple(sorted(_CONNECTORS))
