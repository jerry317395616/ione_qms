from __future__ import annotations

import csv
import hashlib
import json
import re
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import frappe
import requests

from ione_qms.integration.connectors import (
	BaseConnector,
	ConnectorItem,
	ConnectorQuarantineRecord,
	ConnectorRecordError,
	ReconciliationSnapshot,
	decode_connector_quarantine_record,
	register_connector,
)
from ione_qms.integration.schemas import MAX_PAYLOAD_BYTES

MAX_HTTP_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_FILE_ROW_BYTES = MAX_PAYLOAD_BYTES
ORACLE_LOB_CHUNK_UNITS = 64 * 1024
FHIR_WATERMARK_OVERLAP_SECONDS = 5
FHIR_WATERMARK_OVERLAP_MAX_SECONDS = 300
SAFE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,500}$")
SAFE_FIELD_PATH = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*$")
SAFE_HEADER = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,99}$")
SAFE_FILE_PATTERN = re.compile(r"^[A-Za-z0-9*?_.-]{1,100}$")
FHIR_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
FORBIDDEN_PARAM_MARKERS = ("token", "secret", "password", "key", "credential")

OracleQueryBuilder = Callable[[str | None, int], tuple[str, dict[str, Any]]]
OracleReconciliationQueryBuilder = Callable[[Any, Any], tuple[str, dict[str, Any]]]
_ORACLE_QUERIES: dict[str, OracleQueryBuilder] = {}
_ORACLE_RECONCILIATION_QUERIES: dict[str, OracleReconciliationQueryBuilder] = {}
_ORACLE_POOLS: dict[str, Any] = {}
_ORACLE_ENDPOINT_POOL_KEYS: dict[str, str] = {}


@register_connector("rest_json")
class RESTJSONConnector(BaseConnector):
	def fetch(self, cursor: str | None, limit: int) -> tuple[list[ConnectorItem], str | None]:
		limit = min(max(int(limit), 1), 5000)
		options = _options(self.endpoint)
		path = str(options.get("path") or "").strip()
		if path and (not SAFE_PATH.fullmatch(path) or ".." in path.split("/")):
			raise frappe.ValidationError("REST connector path is invalid")
		url = urljoin(str(self.endpoint.base_url).rstrip("/") + "/", path)
		_assert_same_endpoint_host(url, self.endpoint.base_url)
		params = _public_params(options.get("params"))
		params[str(options.get("limit_param") or "limit")] = limit
		mode = str(options.get("cursor_mode") or "token").lower()
		cursor_param = str(options.get("cursor_param") or "cursor")
		if cursor:
			params[cursor_param] = cursor
		elif mode in {"page", "offset"}:
			params[cursor_param] = 1 if mode == "page" else 0

		response = requests.get(
			url,
			params=params,
			headers=_headers(self.endpoint, options),
			auth=_basic_auth(self.endpoint),
			timeout=(
				3.05,
				min(max(int(self.endpoint.get("timeout_seconds") or 30), 1), 300),
			),
			verify=bool(self.endpoint.get("tls_verify") != 0),
			allow_redirects=False,
			stream=True,
		)
		if response.is_redirect:
			raise frappe.ValidationError("REST connector redirects are not allowed")
		response.raise_for_status()
		payload = _bounded_json(response)
		raw_items = _extract(payload, str(options.get("items_path") or "items"))
		if not isinstance(raw_items, list):
			raise frappe.ValidationError("REST connector items_path must resolve to an array")
		if len(raw_items) > limit:
			raise frappe.ValidationError("REST connector returned more records than the requested limit")
		next_cursor = _next_rest_cursor(
			payload,
			items=raw_items,
			cursor=cursor,
			count=len(raw_items),
			mode=mode,
			options=options,
		)
		items: list[ConnectorItem] = [
			item
			if isinstance(item, dict)
			else _quarantine_marker(
				connector="rest_json",
				reason_code="REST_ITEM_NOT_OBJECT",
				position={
					"page_index": index,
					"cursor_sha256": _cursor_hash(cursor),
				},
				value=item,
			)
			for index, item in enumerate(raw_items)
		]
		return items, next_cursor


@register_connector("fhir")
class FHIRConnector(BaseConnector):
	def fetch(self, cursor: str | None, limit: int) -> tuple[list[ConnectorItem], str | None]:
		limit = min(max(int(limit), 1), 1000)
		options = _options(self.endpoint)
		overlap_seconds = _fhir_watermark_overlap_seconds(options)
		public_params = _public_params(options.get("params"))
		if any(key in public_params for key in {"_count", "_sort", "_lastUpdated"}):
			raise frappe.ValidationError("FHIR connector params cannot override paging or watermark controls")
		resource_type = str(options.get("resource_type") or "").strip()
		if not re.fullmatch(r"[A-Z][A-Za-z0-9]{0,99}", resource_type):
			raise frappe.ValidationError("FHIR resource_type is invalid")
		state = _cursor_object(cursor)
		next_url = state.get("next_url")
		if next_url:
			url = str(next_url)
			params = None
		else:
			url = urljoin(str(self.endpoint.base_url).rstrip("/") + "/", resource_type)
			params = dict(public_params)
			params.update({"_count": limit, "_sort": "_lastUpdated,_id"})
			if state.get("watermark"):
				params["_lastUpdated"] = f"ge{_fhir_overlap_start(state['watermark'], overlap_seconds)}"
		_assert_same_endpoint_host(url, self.endpoint.base_url)
		response = requests.get(
			url,
			params=params,
			headers={
				**_headers(self.endpoint, options),
				"Accept": "application/fhir+json, application/json",
			},
			auth=_basic_auth(self.endpoint),
			timeout=(
				3.05,
				min(max(int(self.endpoint.get("timeout_seconds") or 30), 1), 300),
			),
			verify=bool(self.endpoint.get("tls_verify") != 0),
			allow_redirects=False,
			stream=True,
		)
		if response.is_redirect:
			raise frappe.ValidationError("FHIR connector redirects are not allowed")
		response.raise_for_status()
		bundle = _bounded_json(response)
		if bundle.get("resourceType") != "Bundle":
			raise frappe.ValidationError("FHIR endpoint did not return a Bundle")
		entries = bundle.get("entry")
		if entries is None:
			entries = []
		if not isinstance(entries, list):
			raise frappe.ValidationError("FHIR Bundle entry must be an array")
		if len(entries) > limit:
			raise frappe.ValidationError("FHIR connector returned more records than the requested limit")
		envelopes: list[ConnectorItem] = []
		resources: list[dict[str, Any]] = []
		page_cursor_hash = _fhir_page_cursor_hash(state)
		for index, entry in enumerate(entries):
			if not isinstance(entry, dict):
				envelopes.append(
					_quarantine_marker(
						connector="fhir",
						reason_code="FHIR_ENTRY_NOT_OBJECT",
						position={
							"page_index": index,
							"cursor_sha256": page_cursor_hash,
						},
						value=entry,
					)
				)
				continue
			resource = entry.get("resource")
			if not isinstance(resource, dict):
				envelopes.append(
					_quarantine_marker(
						connector="fhir",
						reason_code="FHIR_ENTRY_RESOURCE_NOT_OBJECT",
						position={
							"page_index": index,
							"cursor_sha256": page_cursor_hash,
						},
						value=entry,
					)
				)
				continue
			try:
				envelopes.append(_fhir_envelope(resource, options))
				resources.append(resource)
			except ConnectorRecordError:
				envelopes.append(
					_quarantine_marker(
						connector="fhir",
						reason_code="FHIR_RESOURCE_ENVELOPE_INVALID",
						position={
							"page_index": index,
							"cursor_sha256": page_cursor_hash,
						},
						value=resource,
					)
				)
		watermark, watermark_advanced = _fhir_page_watermark(resources, state)
		next_link = next(
			(
				str(link.get("url"))
				for link in bundle.get("link") or []
				if isinstance(link, dict) and link.get("relation") == "next" and link.get("url")
			),
			None,
		)
		if next_link:
			next_link = urljoin(url, next_link)
			_assert_same_endpoint_host(next_link, self.endpoint.base_url)
		if not next_link and not watermark_advanced:
			# The page's record-local errors can still be durably quarantined,
			# but no local annotation or receive timestamp may impersonate a
			# source-side incremental boundary.
			return envelopes, cursor
		next_state: dict[str, Any] = {"next_url": next_link, "watermark": watermark}
		return envelopes, json.dumps(next_state, sort_keys=True, separators=(",", ":"))


@register_connector("file_drop")
class FileDropConnector(BaseConnector):
	def fetch(self, cursor: str | None, limit: int) -> tuple[list[ConnectorItem], str | None]:
		limit = min(max(int(limit), 1), 5000)
		options = _options(self.endpoint)
		pattern = str(options.get("file_pattern") or "*.jsonl")
		if not SAFE_FILE_PATTERN.fullmatch(pattern):
			raise frappe.ValidationError("File connector pattern is invalid")
		file_format = str(options.get("format") or "jsonl").lower()
		if file_format not in {"jsonl", "csv"}:
			raise frappe.ValidationError("File connector format must be jsonl or csv")
		root = Path(
			frappe.get_site_path(
				"private",
				"files",
				"ione_qms_ingress",
				str(self.endpoint.name),
			)
		).resolve()
		root.mkdir(parents=True, exist_ok=True)
		files = sorted(
			path for path in root.glob(pattern) if path.is_file() and _is_beneath(path.resolve(), root)
		)
		state = _cursor_object(cursor)
		rows: list[ConnectorItem] = []
		last_state = {
			"file": str(state.get("file") or ""),
			"line": int(state.get("line") or 0),
			"complete": bool(state.get("complete")),
		}
		for path in files:
			if last_state["file"] and path.name < last_state["file"]:
				continue
			if last_state["file"] == path.name and last_state["complete"]:
				continue
			start_line = (
				last_state["line"] if last_state["file"] == path.name and not last_state["complete"] else 0
			)
			for index, row in _iter_file_rows(path, file_format, start_line):
				rows.append(row)
				last_state = {"file": path.name, "line": index + 1, "complete": False}
				if len(rows) >= limit:
					return rows, json.dumps(last_state, sort_keys=True, separators=(",", ":"))
			last_state = {"file": path.name, "line": 0, "complete": True}
		return rows, json.dumps(last_state, sort_keys=True, separators=(",", ":"))


@register_connector("oracle")
class OracleConnector(BaseConnector):
	def fetch(self, cursor: str | None, limit: int) -> tuple[list[ConnectorItem], str | None]:
		limit = min(max(int(limit), 1), 5000)
		options = _options(self.endpoint)
		query_key = str(options.get("query_key") or "").strip().upper()
		builder = _ORACLE_QUERIES.get(query_key)
		if not builder:
			raise frappe.ValidationError(
				f"Oracle query '{query_key}' is not registered in reviewed application code"
			)
		host = str(options.get("host") or "").strip().lower()
		if host not in _allowed_hosts(self.endpoint):
			raise frappe.ValidationError("Oracle host is not allowlisted")
		try:
			import oracledb
		except ImportError as exc:
			raise frappe.ValidationError("Python-oracledb is required for the Oracle connector") from exc
		username = str(self.endpoint.get("username") or "")
		password = self.endpoint.get_password("password", raise_exception=False)
		if not username or not password:
			raise frappe.ValidationError("Oracle connector credentials are incomplete")
		port = min(max(int(options.get("port") or 1521), 1), 65535)
		service_name = str(options.get("service_name") or "").strip()
		if not service_name:
			raise frappe.ValidationError("Oracle service_name is required")
		sql, binds = builder(cursor, limit)
		_validate_read_only_sql(sql)
		dsn = oracledb.makedsn(host, port, service_name=service_name)
		_assert_oracle_circuit_closed(self.endpoint)
		started = time.perf_counter()
		try:
			pool = _oracle_pool(
				self.endpoint,
				oracledb=oracledb,
				username=username,
				password=password,
				dsn=dsn,
				options=options,
			)
			with pool.acquire() as connection:
				connection.call_timeout = (
					min(
						max(int(self.endpoint.get("timeout_seconds") or 30), 1),
						300,
					)
					* 1000
				)
				with connection.cursor() as oracle_cursor:
					oracle_cursor.arraysize = min(limit, 1000)
					oracle_cursor.execute(sql, binds)
					columns = [str(item[0]).lower() for item in oracle_cursor.description or []]
					rows = [
						dict(zip(columns, values, strict=True)) for values in oracle_cursor.fetchmany(limit)
					]
		except Exception:
			_record_oracle_failure(self.endpoint, options)
			raise
		_record_oracle_success(self.endpoint)
		_record_slow_oracle_query(
			self.endpoint,
			query_key=query_key,
			duration_ms=max(round((time.perf_counter() - started) * 1000), 0),
			threshold_ms=min(max(int(options.get("slow_query_ms") or 2000), 100), 60000),
		)
		next_cursor = _oracle_next_cursor(rows, cursor, options)
		return _oracle_items(rows), next_cursor

	def reconcile(self, period_start, period_end) -> ReconciliationSnapshot | None:
		"""Execute one hospital-reviewed aggregate query without reading row payloads."""
		options = _options(self.endpoint)
		query_key = str(options.get("reconciliation_query_key") or "").strip().upper()
		if not query_key:
			return None
		builder = _ORACLE_RECONCILIATION_QUERIES.get(query_key)
		if not builder:
			raise frappe.ValidationError(
				f"Oracle reconciliation query '{query_key}' is not registered in reviewed application code"
			)
		sql, binds = builder(period_start, period_end)
		_validate_read_only_sql(sql)
		_assert_oracle_circuit_closed(self.endpoint)
		started = time.perf_counter()
		try:
			row = _execute_oracle_reconciliation_query(
				self.endpoint,
				options=options,
				sql=sql,
				binds=binds,
			)
			snapshot = _oracle_reconciliation_snapshot(row, query_key=query_key)
		except Exception:
			_record_oracle_failure(self.endpoint, options)
			raise
		_record_oracle_success(self.endpoint)
		_record_slow_oracle_query(
			self.endpoint,
			query_key=query_key,
			duration_ms=max(round((time.perf_counter() - started) * 1000), 0),
			threshold_ms=min(max(int(options.get("slow_query_ms") or 2000), 100), 60000),
		)
		return snapshot


def register_oracle_query(
	key: str,
) -> Callable[[OracleQueryBuilder], OracleQueryBuilder]:
	normalized = str(key or "").strip().upper()
	if not normalized:
		raise ValueError("Oracle query key is required")

	def decorator(builder: OracleQueryBuilder) -> OracleQueryBuilder:
		if normalized in _ORACLE_QUERIES:
			raise ValueError(f"Duplicate Oracle query registration: {normalized}")
		_ORACLE_QUERIES[normalized] = builder
		return builder

	return decorator


def registered_oracle_queries() -> tuple[str, ...]:
	return tuple(sorted(_ORACLE_QUERIES))


def register_oracle_reconciliation_query(
	key: str,
) -> Callable[[OracleReconciliationQueryBuilder], OracleReconciliationQueryBuilder]:
	"""Register a source aggregate query during code review.

	The query must return exactly one row with a non-negative ``source_count``
	and may return a stable ``source_hash``. SQL and binds are never accepted
	from Desk configuration.
	"""
	normalized = str(key or "").strip().upper()
	if not normalized:
		raise ValueError("Oracle reconciliation query key is required")

	def decorator(
		builder: OracleReconciliationQueryBuilder,
	) -> OracleReconciliationQueryBuilder:
		if normalized in _ORACLE_RECONCILIATION_QUERIES:
			raise ValueError(f"Duplicate Oracle reconciliation query registration: {normalized}")
		_ORACLE_RECONCILIATION_QUERIES[normalized] = builder
		return builder

	return decorator


def registered_oracle_reconciliation_queries() -> tuple[str, ...]:
	return tuple(sorted(_ORACLE_RECONCILIATION_QUERIES))


@register_oracle_query("IONE_PATIENT_INDEX_DELTA")
def _patient_index_delta_query(
	cursor: str | None,
	limit: int,
) -> tuple[str, dict[str, Any]]:
	state = _oracle_cursor_state(cursor)
	return (
		"""
		SELECT *
		FROM (
			SELECT
				'Patient.Updated' AS event_type,
				v.updated_at AS event_time,
				'Patient' AS source_record_type,
				v.patient_id AS source_record_id,
				TO_CHAR(v.updated_at, 'YYYYMMDDHH24MISSFF6') || ':' || v.patient_id
					AS source_version,
				v.patient_id AS patient_reference,
				TO_CHAR(v.updated_at, 'YYYY-MM-DD"T"HH24:MI:SS.FF6') AS cursor_time,
				v.patient_id AS cursor_id,
				v.patient_id AS source_patient_id,
				v.patient_name,
				v.gender,
				v.date_of_birth,
				v.identification_number,
				v.phone,
				v.status
			FROM IONE_QMS_PATIENT_V v
			WHERE (
				:cursor_time IS NULL
				OR v.updated_at > TO_TIMESTAMP(
					:cursor_time,
					'YYYY-MM-DD"T"HH24:MI:SS.FF6'
				)
				OR (
					v.updated_at = TO_TIMESTAMP(
						:cursor_time,
						'YYYY-MM-DD"T"HH24:MI:SS.FF6'
					)
					AND v.patient_id > :cursor_id
				)
			)
			ORDER BY v.updated_at, v.patient_id
		)
		WHERE ROWNUM <= :row_limit
		""",
		{
			"cursor_time": state.get("time"),
			"cursor_id": state.get("id") or "",
			"row_limit": limit,
		},
	)


@register_oracle_query("IONE_ENCOUNTER_INDEX_DELTA")
def _encounter_index_delta_query(
	cursor: str | None,
	limit: int,
) -> tuple[str, dict[str, Any]]:
	state = _oracle_cursor_state(cursor)
	return (
		"""
		SELECT *
		FROM (
			SELECT
				'Encounter.Updated' AS event_type,
				v.updated_at AS event_time,
				'Encounter' AS source_record_type,
				v.encounter_id AS source_record_id,
				TO_CHAR(v.updated_at, 'YYYYMMDDHH24MISSFF6') || ':' || v.encounter_id
					AS source_version,
				v.patient_id AS patient_reference,
				v.encounter_id AS encounter_reference,
				v.department_code AS department_reference,
				TO_CHAR(v.updated_at, 'YYYY-MM-DD"T"HH24:MI:SS.FF6') AS cursor_time,
				v.encounter_id AS cursor_id,
				v.encounter_id AS source_encounter_id,
				v.encounter_no,
				v.patient_id AS patient_source_id,
				v.hospital_code AS hospital,
				v.campus_code AS campus,
				v.department_code AS department,
				v.ward_code AS ward,
				v.encounter_type,
				v.status,
				v.admission_time,
				v.discharge_time,
				v.responsible_staff_code AS responsible_staff
			FROM IONE_QMS_ENCOUNTER_V v
			WHERE (
				:cursor_time IS NULL
				OR v.updated_at > TO_TIMESTAMP(
					:cursor_time,
					'YYYY-MM-DD"T"HH24:MI:SS.FF6'
				)
				OR (
					v.updated_at = TO_TIMESTAMP(
						:cursor_time,
						'YYYY-MM-DD"T"HH24:MI:SS.FF6'
					)
					AND v.encounter_id > :cursor_id
				)
			)
			ORDER BY v.updated_at, v.encounter_id
		)
		WHERE ROWNUM <= :row_limit
		""",
		{
			"cursor_time": state.get("time"),
			"cursor_id": state.get("id") or "",
			"row_limit": limit,
		},
	)


def _options(endpoint) -> dict[str, Any]:
	raw = endpoint.get("connector_options") or "{}"
	try:
		value = json.loads(raw) if isinstance(raw, str) else raw
	except ValueError as exc:
		raise frappe.ValidationError("connector_options must be valid JSON") from exc
	if not isinstance(value, dict):
		raise frappe.ValidationError("connector_options must be a JSON object")
	return value


def _headers(endpoint, options: dict[str, Any]) -> dict[str, str]:
	headers = {"Accept": "application/json", "User-Agent": "IONE-QMS/1"}
	auth_type = str(endpoint.get("authentication_type") or "None")
	if auth_type == "Bearer":
		token = endpoint.get_password("api_key", raise_exception=False)
		if not token:
			raise frappe.ValidationError("Bearer token is not configured")
		headers["Authorization"] = f"Bearer {token}"
	elif auth_type == "API Key":
		header_name = str(options.get("api_key_header") or "X-API-Key")
		if not SAFE_HEADER.fullmatch(header_name) or header_name.lower() in {
			"host",
			"content-length",
		}:
			raise frappe.ValidationError("API-key header name is invalid")
		token = endpoint.get_password("api_key", raise_exception=False)
		if not token:
			raise frappe.ValidationError("API key is not configured")
		headers[header_name] = token
	elif auth_type not in {"None", "Basic"}:
		raise frappe.ValidationError("Unsupported connector authentication type")
	return headers


def _basic_auth(endpoint) -> tuple[str, str] | None:
	if str(endpoint.get("authentication_type") or "None") != "Basic":
		return None
	username = str(endpoint.get("username") or "")
	password = endpoint.get_password("password", raise_exception=False)
	if not username or not password:
		raise frappe.ValidationError("Basic authentication credentials are incomplete")
	return username, password


def _bounded_json(response: requests.Response) -> dict[str, Any]:
	body = bytearray()
	for chunk in response.iter_content(chunk_size=65536):
		body.extend(chunk)
		if len(body) > MAX_HTTP_RESPONSE_BYTES:
			raise frappe.ValidationError("Connector response exceeds the 10 MiB limit")
	try:
		payload = json.loads(body.decode("utf-8"))
	except (UnicodeDecodeError, ValueError) as exc:
		raise frappe.ValidationError("Connector response is not valid UTF-8 JSON") from exc
	if not isinstance(payload, dict):
		raise frappe.ValidationError("Connector response must be a JSON object")
	return payload


def _quarantine_marker(
	*,
	connector: str,
	reason_code: str,
	position: dict[str, str | int],
	value: Any = None,
	content_hash: str | None = None,
	size_bytes: int | None = None,
) -> ConnectorQuarantineRecord:
	"""Build an opaque marker that contains no rejected clinical values."""
	if content_hash is None or size_bytes is None:
		content_hash, size_bytes = _fingerprint_value(value)
	payload = {
		"connector_quarantine": 1,
		"connector": connector,
		"reason_code": reason_code,
		"position": position,
		"content_sha256": content_hash,
		"size_bytes": max(int(size_bytes), 0),
	}
	encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
	if len(encoded.encode()) > 2048:
		raise frappe.ValidationError("Connector quarantine metadata exceeded its bounded limit")
	marker = ConnectorQuarantineRecord(encoded)
	if decode_connector_quarantine_record(marker) is None:
		raise frappe.ValidationError("Connector quarantine metadata violated its reviewed contract")
	return marker


def _cursor_hash(cursor: str | None) -> str:
	return hashlib.sha256(str(cursor or "<initial>").encode()).hexdigest()


def _file_marker_position(path: Path, line: int) -> dict[str, str | int]:
	return {
		"file_name_sha256": hashlib.sha256(path.name.encode("utf-8")).hexdigest(),
		"line": line,
	}


def _fingerprint_value(value: Any) -> tuple[str, int]:
	try:
		encoded = json.dumps(
			value,
			ensure_ascii=False,
			sort_keys=True,
			separators=(",", ":"),
			default=str,
		).encode()
	except Exception:
		encoded = f"<{type(value).__name__}>".encode()
	return hashlib.sha256(encoded).hexdigest(), len(encoded)


def _oracle_row_fingerprint(row: dict[str, Any]) -> tuple[str, int]:
	"""Hash only structural/envelope identity material without reading payload LOBs."""
	try:
		identity: dict[str, str] = {}
		for fieldname in (
			"event_type",
			"source_record_type",
			"source_record_id",
			"source_version",
			"cursor_time",
			"cursor_id",
		):
			value = row.get(fieldname)
			if value in (None, ""):
				continue
			if hasattr(value, "read"):
				identity[fieldname] = f"<{type(value).__name__}>"
			elif hasattr(value, "isoformat"):
				identity[fieldname] = str(value.isoformat())
			else:
				identity[fieldname] = str(value)
		fingerprint_value = {
			"columns": sorted(str(fieldname) for fieldname in row),
			"identity": identity,
		}
	except Exception:
		fingerprint_value = {"row_type": type(row).__name__}
	return _fingerprint_value(fingerprint_value)


def _public_params(value: Any) -> dict[str, Any]:
	if value in (None, ""):
		return {}
	if not isinstance(value, dict):
		raise frappe.ValidationError("Connector params must be a JSON object")
	params: dict[str, Any] = {}
	for key, item in value.items():
		name = str(key)
		if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,99}", name):
			raise frappe.ValidationError("Connector parameter name is invalid")
		if any(marker in name.lower() for marker in FORBIDDEN_PARAM_MARKERS):
			raise frappe.ValidationError("Secrets cannot be stored in connector query parameters")
		if not isinstance(item, str | int | float | bool) and item is not None:
			raise frappe.ValidationError("Connector parameter values must be scalar")
		params[name] = item
	return params


def _extract(payload: dict[str, Any], path: str) -> Any:
	if not SAFE_FIELD_PATH.fullmatch(path) or "__" in path:
		raise frappe.ValidationError("Connector response path is invalid")
	value: Any = payload
	for part in path.split("."):
		if not isinstance(value, dict) or part not in value:
			raise frappe.ValidationError(f"Connector response path was not found: {path}")
		value = value[part]
	return value


def _extract_optional(payload: dict[str, Any], path: str | None) -> Any:
	if not path:
		return None
	if not SAFE_FIELD_PATH.fullmatch(path) or "__" in path:
		raise frappe.ValidationError("Connector response path is invalid")
	value: Any = payload
	for part in path.split("."):
		if not isinstance(value, dict) or part not in value:
			return None
		value = value[part]
	return value


def _parse_fhir_instant(value: Any) -> tuple[datetime, str]:
	text = str(value or "").strip()
	if not FHIR_INSTANT.fullmatch(text):
		raise ValueError("FHIR instant is invalid")
	normalized = f"{text[:-1]}+00:00" if text.endswith("Z") else text
	try:
		parsed = datetime.fromisoformat(normalized)
		if parsed.utcoffset() is None:
			raise ValueError("FHIR instant is invalid")
		parsed = parsed.astimezone(UTC)
	except (OverflowError, ValueError) as exc:
		raise ValueError("FHIR instant is invalid") from exc
	return parsed, parsed.isoformat().replace("+00:00", "Z")


def _fhir_watermark_overlap_seconds(options: dict[str, Any]) -> int:
	value = options.get("watermark_overlap_seconds", FHIR_WATERMARK_OVERLAP_SECONDS)
	if type(value) is not int or value < 1 or value > FHIR_WATERMARK_OVERLAP_MAX_SECONDS:
		raise frappe.ValidationError("FHIR watermark_overlap_seconds must be an integer from 1 through 300")
	return value


def _fhir_overlap_start(watermark: Any, overlap_seconds: int) -> str:
	try:
		parsed, _ = _parse_fhir_instant(watermark)
	except ValueError as exc:
		raise frappe.ValidationError("FHIR cursor watermark is invalid") from exc
	try:
		start = parsed - timedelta(seconds=overlap_seconds)
	except OverflowError:
		start = datetime.min.replace(tzinfo=UTC)
	return start.isoformat().replace("+00:00", "Z")


def _fhir_page_cursor_hash(state: dict[str, Any]) -> str:
	"""Hash the source paging identity, excluding local non-advance annotations."""
	identity = json.dumps(
		{
			"next_url": str(state.get("next_url") or ""),
			"watermark": str(state.get("watermark") or ""),
		},
		sort_keys=True,
		separators=(",", ":"),
	)
	return _cursor_hash(identity)


def _fhir_page_watermark(
	resources: list[dict[str, Any]],
	state: dict[str, Any],
) -> tuple[str, bool]:
	"""Return a source-derived monotonic watermark and whether it advanced."""
	previous_text = str(state.get("watermark") or "").strip()
	previous: tuple[datetime, str] | None = None
	if previous_text:
		try:
			previous = _parse_fhir_instant(previous_text)
		except ValueError as exc:
			raise frappe.ValidationError("FHIR cursor watermark is invalid") from exc
	candidates = [previous] if previous else []
	for resource in resources:
		meta = resource.get("meta") if isinstance(resource.get("meta"), dict) else {}
		value = meta.get("lastUpdated")
		if not value:
			continue
		try:
			candidates.append(_parse_fhir_instant(value))
		except ValueError:
			# The resource itself is returned as a record-local quarantine item.
			continue
	if not candidates:
		return "", False
	latest = max(candidates, key=lambda item: item[0])
	return latest[1], previous is None or latest[0] > previous[0]


def _fhir_envelope(resource: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
	resource_type = str(resource.get("resourceType") or "").strip()
	resource_id = str(resource.get("id") or "").strip()
	meta = resource.get("meta") if isinstance(resource.get("meta"), dict) else {}
	last_updated = str(meta.get("lastUpdated") or "").strip()
	if not resource_type or not resource_id or not last_updated:
		raise ConnectorRecordError("FHIR resource envelope is incomplete")
	try:
		_, last_updated = _parse_fhir_instant(last_updated)
	except ValueError as exc:
		raise ConnectorRecordError("FHIR resource lastUpdated is invalid") from exc
	version_id = str(meta.get("versionId") or last_updated).strip()
	event_type = str(options.get("event_type") or f"FHIR.{resource_type}.Updated")
	envelope: dict[str, Any] = {
		"event_type": event_type,
		"event_time": last_updated,
		"source_record_type": resource_type,
		"source_record_id": resource_id,
		"source_version": version_id,
		"payload": resource,
	}
	reference_paths = {
		"patient_reference": str(options.get("patient_reference_path") or "subject.reference"),
		"encounter_reference": str(options.get("encounter_reference_path") or "encounter.reference"),
		"department_reference": str(options.get("department_reference_path") or ""),
	}
	for fieldname, path in reference_paths.items():
		value = _extract_optional(resource, path)
		if value not in (None, ""):
			envelope[fieldname] = str(value)
	return envelope


def _next_rest_cursor(
	payload: dict[str, Any],
	*,
	items: list[Any],
	cursor: str | None,
	count: int,
	mode: str,
	options: dict[str, Any],
) -> str | None:
	path = str(options.get("next_cursor_path") or "").strip()
	if path:
		value = _extract(payload, path)
		if value not in (None, ""):
			return str(value)
		if count:
			raise frappe.ValidationError("REST response has records but no next cursor")
		return cursor
	if mode == "page":
		return str(int(cursor or 1) + 1) if count else cursor
	if mode == "offset":
		return str(int(cursor or 0) + count) if count else cursor
	if mode == "item":
		item_path = str(options.get("cursor_item_path") or "")
		if not item_path:
			raise frappe.ValidationError("Item cursor mode requires cursor_item_path")
		for item in reversed(items):
			if not isinstance(item, dict):
				continue
			try:
				value = _extract(item, item_path)
			except frappe.ValidationError:
				continue
			if value not in (None, ""):
				return str(value)
		# Keep the exact prior source cursor when a partial malformed page has
		# no reviewed item boundary. The outer task can quarantine its markers
		# without inventing progress; a full non-advancing page fails closed.
		return cursor
	if count:
		raise frappe.ValidationError("Token cursor mode requires next_cursor_path")
	return cursor


def _cursor_object(cursor: str | None) -> dict[str, Any]:
	if not cursor:
		return {}
	try:
		value = json.loads(cursor)
	except ValueError as exc:
		raise frappe.ValidationError("Connector cursor is invalid") from exc
	if not isinstance(value, dict):
		raise frappe.ValidationError("Connector cursor must be a JSON object")
	return value


def _assert_same_endpoint_host(url: str, base_url: str) -> None:
	parsed = urlparse(url)
	base = urlparse(str(base_url))
	parsed_port = parsed.port or (443 if parsed.scheme == "https" else 80)
	base_port = base.port or (443 if base.scheme == "https" else 80)
	if (
		parsed.scheme not in {"http", "https"}
		or not parsed.hostname
		or parsed.username
		or parsed.password
		or (parsed.query and any(marker in parsed.query.lower() for marker in FORBIDDEN_PARAM_MARKERS))
		or parsed.scheme != base.scheme
		or parsed_port != base_port
		or parsed.hostname.lower() != str(base.hostname).lower()
	):
		raise frappe.ValidationError("Connector URL left its approved endpoint host")


def _iter_file_rows(path: Path, file_format: str, start_line: int):
	if file_format == "csv":
		yield from _iter_csv_rows(path, start_line)
		return
	for physical_index, raw, content_hash, size_bytes, reason_code in _iter_bounded_file_lines(path):
		if physical_index < start_line:
			continue
		position = _file_marker_position(path, physical_index + 1)
		if reason_code:
			yield (
				physical_index,
				_quarantine_marker(
					connector="file_drop",
					reason_code=reason_code,
					position=position,
					content_hash=content_hash,
					size_bytes=size_bytes,
				),
			)
			continue
		try:
			text = raw.decode("utf-8-sig")
		except UnicodeDecodeError:
			yield (
				physical_index,
				_quarantine_marker(
					connector="file_drop",
					reason_code="FILE_LINE_INVALID_UTF8",
					position=position,
					content_hash=content_hash,
					size_bytes=size_bytes,
				),
			)
			continue
		try:
			row = json.loads(text)
		except ValueError:
			yield (
				physical_index,
				_quarantine_marker(
					connector="file_drop",
					reason_code="FILE_JSONL_INVALID_JSON",
					position=position,
					content_hash=content_hash,
					size_bytes=size_bytes,
				),
			)
			continue
		if not isinstance(row, dict):
			yield (
				physical_index,
				_quarantine_marker(
					connector="file_drop",
					reason_code="FILE_JSONL_NOT_OBJECT",
					position=position,
					content_hash=content_hash,
					size_bytes=size_bytes,
				),
			)
			continue
		yield physical_index, row


def _iter_csv_rows(path: Path, start_line: int):
	header: list[str] | None = None
	header_available = True
	record_index = 0
	for physical_index, raw, content_hash, size_bytes, reason_code in _iter_bounded_file_lines(path):
		position = _file_marker_position(path, physical_index + 1)
		if header is None and header_available:
			if reason_code:
				header_available = False
				marker = _quarantine_marker(
					connector="file_drop",
					reason_code="FILE_CSV_HEADER_TOO_LARGE",
					position=position,
					content_hash=content_hash,
					size_bytes=size_bytes,
				)
			else:
				try:
					text = raw.decode("utf-8-sig")
					values = next(csv.reader([text], strict=True))
					if not values or any(not value for value in values) or len(set(values)) != len(values):
						raise csv.Error
					header = values
					continue
				except UnicodeDecodeError, csv.Error, StopIteration:
					header_available = False
					marker = _quarantine_marker(
						connector="file_drop",
						reason_code="FILE_CSV_HEADER_INVALID",
						position=position,
						content_hash=content_hash,
						size_bytes=size_bytes,
					)
			if record_index >= start_line:
				yield record_index, marker
			record_index += 1
			continue

		if reason_code:
			marker = _quarantine_marker(
				connector="file_drop",
				reason_code=reason_code,
				position=position,
				content_hash=content_hash,
				size_bytes=size_bytes,
			)
			if record_index >= start_line:
				yield record_index, marker
			record_index += 1
			continue
		if not header_available:
			marker = _quarantine_marker(
				connector="file_drop",
				reason_code="FILE_CSV_HEADER_UNAVAILABLE",
				position=position,
				content_hash=content_hash,
				size_bytes=size_bytes,
			)
			if record_index >= start_line:
				yield record_index, marker
			record_index += 1
			continue
		try:
			text = raw.decode("utf-8-sig")
			values = next(csv.reader([text], strict=True))
			if len(values) != len(header or []):
				raise csv.Error
			row = dict(zip(header or [], values, strict=True))
		except UnicodeDecodeError, csv.Error, StopIteration:
			row = _quarantine_marker(
				connector="file_drop",
				reason_code="FILE_CSV_ROW_INVALID",
				position=position,
				content_hash=content_hash,
				size_bytes=size_bytes,
			)
		if record_index >= start_line:
			yield record_index, row
		record_index += 1


def _iter_bounded_file_lines(path: Path):
	"""Stream physical lines and hash oversized rows without retaining their body."""
	with path.open("rb") as handle:
		physical_index = 0
		while True:
			first = handle.readline(MAX_FILE_ROW_BYTES + 2)
			if not first:
				return
			hasher = hashlib.sha256(first)
			size_bytes = len(first)
			oversized = len(first) > MAX_FILE_ROW_BYTES
			complete = first.endswith(b"\n")
			while not complete:
				chunk = handle.readline(65536)
				if not chunk:
					complete = True
					break
				hasher.update(chunk)
				size_bytes += len(chunk)
				complete = chunk.endswith(b"\n")
				if size_bytes > MAX_FILE_ROW_BYTES:
					oversized = True
			yield (
				physical_index,
				None if oversized else first,
				hasher.hexdigest(),
				size_bytes,
				"FILE_LINE_TOO_LARGE" if oversized else None,
			)
			physical_index += 1


def _is_beneath(path: Path, root: Path) -> bool:
	try:
		path.relative_to(root)
		return True
	except ValueError:
		return False


def _allowed_hosts(endpoint) -> frozenset[str]:
	raw = endpoint.get("allowed_hosts") or "[]"
	try:
		value = json.loads(raw) if isinstance(raw, str) and raw.lstrip().startswith("[") else raw
	except ValueError as exc:
		raise frappe.ValidationError("allowed_hosts must be valid JSON") from exc
	if isinstance(value, str):
		value = value.replace(",", "\n").splitlines()
	if not isinstance(value, list | tuple):
		raise frappe.ValidationError("allowed_hosts must be an array or list")
	return frozenset(str(item).strip().lower() for item in value if str(item).strip())


def _validate_read_only_sql(sql: str) -> None:
	normalized = re.sub(r"/\*.*?\*/|--[^\r\n]*", " ", str(sql), flags=re.DOTALL).strip()
	if not re.match(r"^(select|with)\b", normalized, flags=re.IGNORECASE):
		raise frappe.ValidationError("Oracle connector queries must be SELECT statements")
	if ";" in normalized.rstrip(";") or re.search(
		r"\b(insert|update|delete|merge|alter|drop|truncate|grant|revoke|begin|execute)\b"
		r"|\bfor\s+update\b",
		normalized,
		flags=re.IGNORECASE,
	):
		raise frappe.ValidationError("Oracle connector query contains a forbidden statement")


def _oracle_cursor_state(cursor: str | None) -> dict[str, str]:
	if not cursor:
		return {}
	try:
		state = json.loads(cursor)
	except ValueError as exc:
		raise frappe.ValidationError("Oracle incremental cursor is not valid JSON") from exc
	if (
		not isinstance(state, dict)
		or set(state).difference({"time", "id"})
		or not isinstance(state.get("time"), str)
		or not isinstance(state.get("id"), str)
	):
		raise frappe.ValidationError("Oracle cursor must contain string time and id fields")
	if not state["time"] or not state["id"]:
		raise frappe.ValidationError("Oracle cursor time and id cannot be empty")
	return {"time": state["time"], "id": state["id"]}


def _oracle_next_cursor(
	rows: list[dict[str, Any]],
	cursor: str | None,
	options: dict[str, Any],
) -> str | None:
	if not rows:
		return cursor
	last = rows[-1]
	if last.get("cursor_time") not in (None, "") and last.get("cursor_id") not in (None, ""):
		return json.dumps(
			{
				"time": _scalar_text(last["cursor_time"]),
				"id": _scalar_text(last["cursor_id"]),
			},
			sort_keys=True,
			separators=(",", ":"),
		)
	cursor_field = str(options.get("cursor_field") or "").strip().lower()
	if not cursor_field:
		raise frappe.ValidationError(
			"Oracle query rows require cursor_time/cursor_id or a reviewed cursor_field"
		)
	if not SAFE_FIELD_PATH.fullmatch(cursor_field) or "." in cursor_field:
		raise frappe.ValidationError("Oracle cursor_field is invalid")
	value = last.get(cursor_field)
	if value in (None, ""):
		raise frappe.ValidationError("Oracle query returned no value for cursor_field")
	return _scalar_text(value)


def _oracle_items(rows: list[dict[str, Any]]) -> list[ConnectorItem]:
	items: list[ConnectorItem] = []
	for index, row in enumerate(rows):
		fingerprint, fingerprint_size = _oracle_row_fingerprint(row)
		try:
			items.append(_oracle_event_envelope(row))
		except ConnectorRecordError:
			items.append(
				_quarantine_marker(
					connector="oracle",
					reason_code="ORACLE_ROW_ENVELOPE_INVALID",
					position={"page_index": index},
					content_hash=fingerprint,
					size_bytes=fingerprint_size,
				)
			)
	return items


def _oracle_event_envelope(row: dict[str, Any]) -> dict[str, Any]:
	required = (
		"event_type",
		"event_time",
		"source_record_type",
		"source_record_id",
		"source_version",
	)
	missing = [fieldname for fieldname in required if row.get(fieldname) in (None, "")]
	if missing:
		raise ConnectorRecordError("Oracle row event envelope is incomplete")
	payload = _oracle_payload(row)
	envelope: dict[str, Any] = {
		fieldname: _scalar_text(row[fieldname]) if fieldname != "event_time" else row[fieldname]
		for fieldname in required
	}
	for fieldname in ("patient_reference", "encounter_reference", "department_reference"):
		if row.get(fieldname) not in (None, ""):
			envelope[fieldname] = _scalar_text(row[fieldname])
	envelope["payload"] = payload
	return envelope


def _read_oracle_lob_bounded(value: Any) -> str | bytes:
	"""Read an Oracle LOB in bounded chunks and enforce the ingress byte limit."""
	read = getattr(value, "read", None)
	if not callable(read):
		raise ConnectorRecordError("Oracle row LOB is not readable")
	size_method = getattr(value, "size", None)
	oracle_style = callable(size_method)
	declared_units: int | None = None
	if oracle_style:
		try:
			declared_units = int(size_method())
		except Exception as exc:
			raise ConnectorRecordError("Oracle row LOB size cannot be read") from exc
		if declared_units < 0:
			raise ConnectorRecordError("Oracle row LOB size is invalid")
		# One character is at least one UTF-8 byte, so this is a safe early
		# rejection for both CLOB and BLOB values.
		if declared_units > MAX_PAYLOAD_BYTES:
			raise ConnectorRecordError("Oracle row LOB exceeds the payload limit")

	chunks: list[str] | list[bytes] = []
	chunk_type: type[str] | type[bytes] | None = None
	total_bytes = 0
	offset = 1
	remaining = declared_units
	while remaining is None or remaining > 0:
		amount = ORACLE_LOB_CHUNK_UNITS if remaining is None else min(remaining, ORACLE_LOB_CHUNK_UNITS)
		try:
			chunk = read(offset=offset, amount=amount) if oracle_style else read(amount)
		except Exception as exc:
			raise ConnectorRecordError("Oracle row LOB cannot be read") from exc
		if chunk is None:
			if remaining not in (None, 0):
				raise ConnectorRecordError("Oracle row LOB ended before its declared size")
			break
		if not isinstance(chunk, str | bytes):
			raise ConnectorRecordError("Oracle row LOB returned an unsupported value")
		if not chunk:
			if remaining not in (None, 0):
				raise ConnectorRecordError("Oracle row LOB ended before its declared size")
			break
		if chunk_type is None:
			chunk_type = type(chunk)
		elif type(chunk) is not chunk_type:
			raise ConnectorRecordError("Oracle row LOB changed value type while reading")
		encoded_size = len(chunk.encode("utf-8")) if isinstance(chunk, str) else len(chunk)
		total_bytes += encoded_size
		if total_bytes > MAX_PAYLOAD_BYTES:
			raise ConnectorRecordError("Oracle row LOB exceeds the payload limit")
		chunks.append(chunk)
		units_read = len(chunk)
		if units_read <= 0:
			break
		if oracle_style:
			offset += units_read
			remaining = max(int(remaining or 0) - units_read, 0)

	if chunk_type is bytes:
		return b"".join(chunks)
	return "".join(chunks)


def _oracle_payload(row: dict[str, Any]) -> dict[str, Any]:
	raw_payload = row.get("payload_json")
	if raw_payload not in (None, ""):
		if hasattr(raw_payload, "read"):
			raw_payload = _read_oracle_lob_bounded(raw_payload)
		if isinstance(raw_payload, str):
			if len(raw_payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
				raise ConnectorRecordError("Oracle row payload exceeds the payload limit")
		elif isinstance(raw_payload, bytes) and len(raw_payload) > MAX_PAYLOAD_BYTES:
			raise ConnectorRecordError("Oracle row payload exceeds the payload limit")
		try:
			payload = json.loads(raw_payload) if isinstance(raw_payload, str | bytes) else raw_payload
		except (UnicodeDecodeError, ValueError) as exc:
			raise ConnectorRecordError("Oracle row payload JSON is invalid") from exc
		if not isinstance(payload, dict):
			raise ConnectorRecordError("Oracle row payload must contain an object")
		return payload
	reserved = {
		"event_type",
		"event_time",
		"source_record_type",
		"source_record_id",
		"source_version",
		"patient_reference",
		"encounter_reference",
		"department_reference",
		"cursor_time",
		"cursor_id",
		"payload_json",
	}
	return {fieldname: _oracle_value(value) for fieldname, value in row.items() if fieldname not in reserved}


def _oracle_value(value: Any) -> Any:
	try:
		if hasattr(value, "read"):
			value = _read_oracle_lob_bounded(value)
		if hasattr(value, "isoformat"):
			return value.isoformat()
		return value
	except ConnectorRecordError:
		raise
	except Exception as exc:
		raise ConnectorRecordError("Oracle row value cannot be converted") from exc


def _scalar_text(value: Any) -> str:
	try:
		if hasattr(value, "read"):
			value = _read_oracle_lob_bounded(value)
		if hasattr(value, "isoformat"):
			return value.isoformat()
		return str(value)
	except ConnectorRecordError:
		raise
	except Exception as exc:
		raise ConnectorRecordError("Oracle row scalar cannot be converted") from exc


def _execute_oracle_reconciliation_query(
	endpoint,
	*,
	options: dict[str, Any],
	sql: str,
	binds: dict[str, Any],
) -> dict[str, Any]:
	host = str(options.get("host") or "").strip().lower()
	if host not in _allowed_hosts(endpoint):
		raise frappe.ValidationError("Oracle host is not allowlisted")
	if not isinstance(binds, dict):
		raise frappe.ValidationError("Oracle reconciliation binds must be an object")
	try:
		import oracledb
	except ImportError as exc:
		raise frappe.ValidationError("Python-oracledb is required for the Oracle connector") from exc
	username = str(endpoint.get("username") or "")
	password = endpoint.get_password("password", raise_exception=False)
	if not username or not password:
		raise frappe.ValidationError("Oracle connector credentials are incomplete")
	port = min(max(int(options.get("port") or 1521), 1), 65535)
	service_name = str(options.get("service_name") or "").strip()
	if not service_name:
		raise frappe.ValidationError("Oracle service_name is required")
	dsn = oracledb.makedsn(host, port, service_name=service_name)
	pool = _oracle_pool(
		endpoint,
		oracledb=oracledb,
		username=username,
		password=password,
		dsn=dsn,
		options=options,
	)
	with pool.acquire() as connection:
		connection.call_timeout = min(max(int(endpoint.get("timeout_seconds") or 30), 1), 300) * 1000
		with connection.cursor() as oracle_cursor:
			oracle_cursor.arraysize = 2
			oracle_cursor.execute(sql, binds)
			columns = [str(item[0]).lower() for item in oracle_cursor.description or []]
			rows = oracle_cursor.fetchmany(2)
	if len(rows) != 1:
		raise frappe.ValidationError("Oracle reconciliation query must return exactly one aggregate row")
	if len(columns) != len(rows[0]):
		raise frappe.ValidationError("Oracle reconciliation result columns are invalid")
	return dict(zip(columns, rows[0], strict=True))


def _oracle_reconciliation_snapshot(
	row: dict[str, Any],
	*,
	query_key: str,
) -> ReconciliationSnapshot:
	if "source_count" not in row:
		raise frappe.ValidationError("Oracle reconciliation query must return a source_count column")
	source_count = _strict_source_count(row["source_count"])
	source_hash = row.get("source_hash")
	if source_hash not in (None, ""):
		if hasattr(source_hash, "read"):
			source_hash = _read_oracle_lob_bounded(source_hash)
		if not isinstance(source_hash, str):
			raise frappe.ValidationError("Oracle reconciliation source_hash must be text")
		if len(source_hash) > 64:
			raise frappe.ValidationError("Oracle reconciliation source_hash exceeds 64 characters")
	else:
		source_hash = None
	return ReconciliationSnapshot(
		source_count=source_count,
		source_hash=source_hash,
		details={
			"query_key": query_key,
			"period_semantics": "[period_start, period_end)",
		},
	)


def _strict_source_count(value: Any) -> int:
	if isinstance(value, bool):
		raise frappe.ValidationError("Oracle reconciliation source_count must be an integer")
	if isinstance(value, int):
		source_count = value
	elif isinstance(value, Decimal):
		if not value.is_finite() or value != value.to_integral_value():
			raise frappe.ValidationError("Oracle reconciliation source_count must be an integer")
		source_count = int(value)
	else:
		raise frappe.ValidationError("Oracle reconciliation source_count must be an integer")
	if source_count < 0:
		raise frappe.ValidationError("Oracle reconciliation source_count cannot be negative")
	return source_count


def _oracle_pool(
	endpoint,
	*,
	oracledb,
	username: str,
	password: str,
	dsn: str,
	options: dict[str, Any],
):
	pool_min = min(max(int(options.get("pool_min") or 1), 1), 5)
	pool_max = min(max(int(options.get("pool_max") or 4), pool_min), 20)
	pool_key = hashlib.sha256(
		f"{endpoint.name}|{endpoint.modified}|{username}|{dsn}|{pool_min}|{pool_max}".encode()
	).hexdigest()
	if pool_key in _ORACLE_POOLS:
		return _ORACLE_POOLS[pool_key]
	previous_key = _ORACLE_ENDPOINT_POOL_KEYS.get(endpoint.name)
	if previous_key and previous_key in _ORACLE_POOLS:
		previous_pool = _ORACLE_POOLS.pop(previous_key)
		with suppress(Exception):
			previous_pool.close(force=True)
	wait_timeout_ms = min(max(int(endpoint.get("timeout_seconds") or 30), 1), 300) * 1000
	pool = oracledb.create_pool(
		user=username,
		password=password,
		dsn=dsn,
		min=pool_min,
		max=pool_max,
		increment=1,
		getmode=oracledb.POOL_GETMODE_TIMEDWAIT,
		wait_timeout=wait_timeout_ms,
		timeout=min(max(int(options.get("pool_idle_timeout") or 60), 10), 3600),
		max_lifetime_session=min(
			max(int(options.get("pool_max_lifetime") or 3600), 60),
			86400,
		),
		ping_interval=min(max(int(options.get("pool_ping_interval") or 60), 0), 3600),
		stmtcachesize=min(max(int(options.get("statement_cache_size") or 50), 0), 500),
	)
	_ORACLE_POOLS[pool_key] = pool
	_ORACLE_ENDPOINT_POOL_KEYS[endpoint.name] = pool_key
	return pool


def _assert_oracle_circuit_closed(endpoint) -> None:
	state = frappe.cache.get_value(_oracle_circuit_key(endpoint), expires=True) or {}
	open_until = float(state.get("open_until") or 0) if isinstance(state, dict) else 0
	if open_until > time.time():
		raise frappe.ValidationError("Oracle connector circuit breaker is open; retry later")


def _record_oracle_failure(endpoint, options: dict[str, Any]) -> None:
	key = _oracle_circuit_key(endpoint)
	state = frappe.cache.get_value(key, expires=True) or {}
	failures = int(state.get("failures") or 0) + 1 if isinstance(state, dict) else 1
	threshold = min(max(int(options.get("circuit_breaker_failures") or 5), 1), 20)
	open_seconds = min(max(int(options.get("circuit_breaker_open_seconds") or 60), 10), 3600)
	open_until = time.time() + open_seconds if failures >= threshold else 0
	frappe.cache.set_value(
		key,
		{"failures": failures, "open_until": open_until},
		expires_in_sec=max(open_seconds * 2, 300),
	)


def _record_oracle_success(endpoint) -> None:
	frappe.cache.delete_value(_oracle_circuit_key(endpoint))


def _oracle_circuit_key(endpoint) -> str:
	return f"ione_qms:oracle_circuit:{endpoint.name}"


def _record_slow_oracle_query(
	endpoint,
	*,
	query_key: str,
	duration_ms: int,
	threshold_ms: int,
) -> None:
	if duration_ms < threshold_ms:
		return
	cache_key = f"ione_qms:oracle_slow_log:{endpoint.name}:{query_key}"
	if frappe.cache.get_value(cache_key, expires=True):
		return
	frappe.cache.set_value(cache_key, 1, expires_in_sec=300)
	frappe.log_error(
		title="IONE QMS slow Oracle query",
		message=(
			f"Reviewed query {query_key} on endpoint {endpoint.name} took "
			f"{duration_ms} ms (threshold {threshold_ms} ms). SQL, binds, credentials, "
			"and clinical payload were not logged."
		),
		reference_doctype="IONE Integration Endpoint",
		reference_name=endpoint.name,
	)
