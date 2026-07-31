from __future__ import annotations

import hashlib
import hmac
import importlib
import ipaddress
import json
import re
import uuid
from contextlib import contextmanager
from urllib.parse import urlparse

import frappe
from frappe.utils import getdate, nowdate

from ione_qms.integration.connectors import registered_connectors
from ione_qms.integration.hl7_v2 import (
	HL7V2ContractError,
	normalize_hl7_v2_batch_size,
	normalize_hl7_v2_transport_options,
)
from ione_qms.integration.mapping import (
	CHECKSUM_PATTERN,
	MappingDefinition,
	apply_mapping,
	frozen_mapping_definition,
	mapping_definition,
)
from ione_qms.integration.signing import unsigned_requests_rejected
from ione_qms.services.integration_scope import (
	validate_allowed_scopes,
	validate_endpoint_scope_policy,
	validate_mapping_constant_scope,
)
from ione_qms.services.runtime_settings import production_mode_enabled

_INTEGRATION_CONFIGURATION_MAINTENANCE_TOKEN = object()
_INTEGRATION_RUNTIME_UPDATE_TOKEN = object()
_MAPPING_LINEAGE_FIELDS = (
	"mapping_record",
	"mapping_code",
	"mapping_version",
	"mapping_checksum",
	"mapping_snapshot",
)
_MAPPING_PUBLISHED_FIELDS = (
	"mapping_code",
	"source_system",
	"endpoint",
	"version",
	"mapping_json",
	"master_data_json",
	"canonical_snapshot",
	"checksum",
	"mapping_version_key",
	"effective_from",
	"effective_to",
)
_JOB_FINISH_FIELDS = frozenset(
	{
		"status",
		"cursor_after_hash",
		"received_count",
		"success_count",
		"duplicate_count",
		"quarantined_count",
		"error_count",
		"completed_at",
		"error_message",
	}
)
_JOB_AUDIT_FIELDS = frozenset(
	{
		"endpoint",
		"source_system",
		"job_type",
		"status",
		"cursor_before_hash",
		"cursor_after_hash",
		"requested_batch_size",
		"received_count",
		"success_count",
		"duplicate_count",
		"quarantined_count",
		"error_count",
		"started_at",
		"completed_at",
		"error_message",
	}
)
_ENDPOINT_RUNTIME_FIELDS = frozenset(
	{
		"cursor",
		"last_sync_at",
		"last_attempt_at",
		"next_retry_at",
	}
)


@contextmanager
def trusted_integration_configuration_maintenance():
	"""Permit only the app's bounded install/migrate settings initialization."""
	attribute = "ione_integration_configuration_maintenance"
	previous_exists = attribute in frappe.flags
	previous = frappe.flags.get(attribute)
	setattr(frappe.flags, attribute, _INTEGRATION_CONFIGURATION_MAINTENANCE_TOKEN)
	try:
		yield
	finally:
		if not previous_exists:
			frappe.flags.pop(attribute, None)
		else:
			setattr(frappe.flags, attribute, previous)


@contextmanager
def trusted_integration_runtime_updates():
	"""Authorize only bounded application-owned endpoint/job state writes."""
	attribute = "ione_integration_runtime_update"
	previous_exists = attribute in frappe.flags
	previous = frappe.flags.get(attribute)
	setattr(frappe.flags, attribute, _INTEGRATION_RUNTIME_UPDATE_TOKEN)
	try:
		yield
	finally:
		if not previous_exists:
			frappe.flags.pop(attribute, None)
		else:
			setattr(frappe.flags, attribute, previous)


def prepare_source_system(doc, method: str | None = None) -> None:
	del method
	if doc.is_new() and not str(doc.get("identity_namespace") or "").strip():
		doc.identity_namespace = uuid.uuid4().hex


def validate_source_system(doc, method: str | None = None) -> None:
	if method:
		_require_named_integration_administrator()
	if int(doc.get("enabled") or 0):
		from ione_qms.services.migration_state import assert_migration_complete

		assert_migration_complete("enable an integration source system")
	prepare_source_system(doc)
	namespace = str(doc.get("identity_namespace") or "")
	if not namespace or namespace != namespace.strip():
		frappe.throw("Source-system identity_namespace must be non-empty without surrounding whitespace")
	if not doc.is_new():
		existing = frappe.db.get_value("IONE Source System", doc.name, "identity_namespace")
		if not existing or str(doc.get("identity_namespace") or "") != str(existing):
			frappe.throw("Source-system identity_namespace is permanent after insertion")
	if doc.get("system_type") != "Internal" and not int(doc.get("read_only") or 0):
		frappe.throw("External clinical source systems must be read-only")
	_validate_hosts(doc.get("allowed_hosts"))
	validate_allowed_scopes(doc)


def validate_integration_endpoint(doc, method: str | None = None) -> None:
	if method:
		_require_named_integration_administrator()
		_validate_endpoint_runtime_fields(doc)
	if int(doc.get("enabled") or 0):
		from ione_qms.services.migration_state import assert_migration_complete

		assert_migration_complete("enable an integration endpoint")
	importlib.import_module("ione_qms.integration.builtin_connectors")
	if doc.get("direction") in {"Outbound", "Bidirectional"}:
		frappe.throw("IONE QMS cannot write to a source clinical system")
	source = (
		frappe.get_cached_doc("IONE Source System", doc.source_system) if doc.get("source_system") else None
	)
	if source and not int(source.get("read_only") or 0):
		frappe.throw("Integration endpoint source must be read-only")
	if source:
		validate_endpoint_scope_policy(doc, source)
	if doc.get("base_url"):
		_validate_endpoint_url(doc.base_url, doc.get("allowed_hosts"))
		if (
			urlparse(str(doc.base_url)).scheme == "https"
			and doc.get("tls_verify") == 0
			and _production_mode()
		):
			frappe.throw("TLS verification cannot be disabled in production")
	options = _connector_options(doc)
	_reject_embedded_secrets(options)
	_validate_hl7_v2_endpoint_contract(doc)
	if int(doc.get("enabled") or 0) and "master_data" in options:
		frappe.throw(
			"Enabled endpoints cannot define connector_options.master_data; "
			"use Mapping Record master_data_json"
		)
	inline_mapping = doc.get("mapping")
	mapping = None
	if inline_mapping:
		try:
			mapping = json.loads(inline_mapping) if isinstance(inline_mapping, str) else inline_mapping
		except ValueError as exc:
			raise frappe.ValidationError("Integration mapping must be valid JSON") from exc
		apply_mapping({}, mapping)
		constant_scope = apply_mapping({}, mapping)
		if any(constant_scope.get(fieldname) for fieldname in ("hospital", "campus", "department", "ward")):
			validate_mapping_constant_scope(doc, source, constant_scope)
	if doc.get("mapping_record") and inline_mapping:
		frappe.throw(
			"Endpoint inline mapping is a legacy migration field and cannot coexist with Mapping Record"
		)
	if doc.get("mapping_record"):
		resolve_endpoint_mapping(
			doc,
			source,
			require_active=bool(int(doc.get("enabled") or 0)),
			lock=bool(method),
		)
	elif int(doc.get("enabled") or 0):
		frappe.throw("Enabled integration endpoints require a governed Mapping Record")
	if str(doc.get("connector_key") or "").strip().lower() == "oracle":
		_validate_oracle_options(doc, options)
	if doc.get("allowed_ip_cidrs"):
		for item in str(doc.allowed_ip_cidrs).replace(",", "\n").splitlines():
			if item.strip():
				try:
					ipaddress.ip_network(item.strip(), strict=False)
				except ValueError as exc:
					raise frappe.ValidationError(f"Invalid allowed CIDR: {item.strip()}") from exc
	if int(doc.get("require_signature") or 0):
		if doc.is_new() and not doc.get("shared_secret"):
			frappe.throw("Signed endpoints require a shared secret")
		if not doc.is_new() and not doc.get_password("shared_secret", raise_exception=False):
			frappe.throw("Signed endpoints require a shared secret")
	if (
		int(doc.get("enabled") or 0)
		and doc.get("direction") == "Inbound"
		and unsigned_requests_rejected()
		and not int(doc.get("require_signature") or 0)
	):
		frappe.throw("Enabled inbound endpoints must require signed requests")
	if (
		int(doc.get("enabled") or 0)
		and doc.get("direction") == "Inbound"
		and int(doc.get("require_signature") or 0)
		and len(_shared_secret(doc)) < 32
	):
		frappe.throw("Inbound endpoint signing secrets must contain at least 32 characters")
	if (
		int(doc.get("enabled") or 0)
		and doc.get("direction") == "Inbound"
		and _production_mode()
		and not str(doc.get("allowed_ip_cidrs") or "").strip()
	):
		frappe.throw("Production inbound endpoints require an explicit source CIDR allowlist")
	if int(doc.get("timeout_seconds") or 10) not in range(1, 301):
		frappe.throw("Integration timeout must be between 1 and 300 seconds")
	if int(doc.get("batch_size") or 500) not in range(1, 5001):
		frappe.throw("Integration batch size must be between 1 and 5,000")
	retry_count = doc.get("retry_count")
	retry_count = 5 if retry_count in (None, "") else int(retry_count)
	if retry_count not in range(1, 21):
		frappe.throw("Integration retry count must be between 1 and 20")
	retry_backoff = doc.get("retry_backoff_seconds")
	retry_backoff = 30 if retry_backoff in (None, "") else int(retry_backoff)
	if retry_backoff not in range(1, 3601):
		frappe.throw("Integration retry backoff must be between 1 and 3,600 seconds")
	if int(doc.get("rate_limit_per_minute") or 600) not in range(1, 100001):
		frappe.throw("Integration rate limit must be between 1 and 100,000 per minute")
	if int(doc.get("enabled") or 0) and doc.get("direction") == "Pull":
		key = str(doc.get("connector_key") or "").strip().lower()
		if key not in registered_connectors():
			frappe.throw(f"Enabled pull endpoint connector '{key}' is not registered")


def assert_integration_endpoint_runtime(doc) -> None:
	"""Recheck security invariants immediately before ingress or source access."""
	if not int(doc.get("enabled") or 0):
		frappe.throw("Integration endpoint is disabled")
	validate_integration_endpoint(doc)


def validate_integration_mapping(doc, method: str | None = None) -> None:
	if method:
		_require_named_integration_administrator()
	code = str(doc.get("mapping_code") or "")
	if not code or code != code.strip() or len(code) > 140:
		frappe.throw("Integration mapping code must contain 1-140 canonical characters")
	if not doc.get("endpoint"):
		frappe.throw("Integration mappings must be bound to exactly one endpoint")
	endpoint = frappe.get_cached_doc("IONE Integration Endpoint", doc.endpoint)
	if endpoint.get("source_system") != doc.get("source_system"):
		frappe.throw("Integration mapping endpoint and source system must match")
	try:
		definition = mapping_definition(
			record=doc.name or "new-mapping",
			code=code,
			version=doc.get("version"),
			mapping=doc.get("mapping_json"),
			master_data=doc.get("master_data_json"),
		)
	except ValueError as exc:
		raise frappe.ValidationError(str(exc)) from exc
	constant_scope = apply_mapping({}, definition.values)
	if any(constant_scope.get(fieldname) for fieldname in ("hospital", "campus", "department", "ward")):
		source = frappe.get_cached_doc("IONE Source System", doc.source_system)
		validate_mapping_constant_scope(endpoint, source, constant_scope)
	doc.mapping_json = _canonical_json(definition.values)
	doc.master_data_json = _canonical_json(definition.master_data)
	doc.canonical_snapshot = definition.snapshot
	doc.checksum = definition.checksum
	doc.mapping_version_key = _mapping_version_key(
		doc.get("source_system"),
		doc.get("endpoint"),
		code,
		definition.version,
	)
	effective_from = getdate(doc.effective_from) if doc.get("effective_from") else None
	effective_to = getdate(doc.effective_to) if doc.get("effective_to") else None
	if effective_from and effective_to and effective_to < effective_from:
		frappe.throw("Integration mapping effective_to must not precede effective_from")
	status = str(doc.get("status") or "Draft")
	if status not in {"Draft", "Active", "Retired"}:
		frappe.throw("Integration mapping status is invalid")
	doc.status = status
	if status == "Active" and not effective_from:
		frappe.throw("Active integration mappings require an effective_from date")
	if doc.is_new():
		if status == "Retired":
			frappe.throw("New integration mapping versions cannot start Retired")
		return
	previous = frappe.get_doc("IONE Integration Mapping", doc.name, for_update=True)
	if not previous:
		frappe.throw("Integration mapping disappeared during validation")
	previous_status = str(previous.get("status") or "")
	if previous_status == "Draft":
		if status not in {"Draft", "Active"}:
			frappe.throw("Draft integration mappings may only remain Draft or become Active")
		return
	changed = [
		fieldname
		for fieldname in _MAPPING_PUBLISHED_FIELDS
		if str(previous.get(fieldname) or "") != str(doc.get(fieldname) or "")
	]
	if changed:
		frappe.throw("Published integration mapping content is immutable; create a new version instead")
	if previous_status == "Active" and status not in {"Active", "Retired"}:
		frappe.throw("Active integration mappings may only remain Active or become Retired")
	if (
		previous_status == "Active"
		and status == "Retired"
		and frappe.db.exists(
			"IONE Integration Endpoint",
			{"mapping_record": doc.name, "enabled": 1},
		)
	):
		frappe.throw("Switch or disable every endpoint before retiring its active mapping version")
	if (
		previous_status == "Active"
		and status == "Retired"
		and frappe.db.exists(
			"IONE QC Indicator Version",
			{"source_mapping": doc.name, "status": "Published"},
		)
	):
		frappe.throw("Retire or replace every Published indicator version before retiring its source mapping")
	if previous_status == "Retired" and status != "Retired":
		frappe.throw("Retired integration mappings cannot be reactivated")


def prevent_integration_mapping_deletion(doc, method: str | None = None) -> None:
	del doc, method
	frappe.throw("Integration mapping versions are audit records and cannot be deleted")


def resolve_endpoint_mapping(
	endpoint,
	source,
	*,
	require_active: bool = True,
	lock: bool = False,
) -> MappingDefinition:
	record_name = str(endpoint.get("mapping_record") or "").strip()
	if not record_name:
		frappe.throw("Integration endpoint has no governed Mapping Record")
	mapping_doc = frappe.get_doc("IONE Integration Mapping", record_name, for_update=lock)
	if mapping_doc.get("source_system") != source.name or mapping_doc.get("endpoint") != endpoint.name:
		frappe.throw("Integration Mapping Record does not belong to this endpoint and source")
	try:
		definition = mapping_definition(
			record=mapping_doc.name,
			code=mapping_doc.get("mapping_code"),
			version=mapping_doc.get("version"),
			mapping=mapping_doc.get("mapping_json"),
			master_data=mapping_doc.get("master_data_json"),
			stored_checksum=mapping_doc.get("checksum"),
			stored_snapshot=mapping_doc.get("canonical_snapshot"),
		)
	except ValueError as exc:
		raise frappe.ValidationError(str(exc)) from exc
	if mapping_doc.get("mapping_version_key") != _mapping_version_key(
		source.name,
		endpoint.name,
		definition.code,
		definition.version,
	):
		frappe.throw("Integration Mapping Record version identity is invalid")
	constant_scope = apply_mapping({}, definition.values)
	if any(constant_scope.get(fieldname) for fieldname in ("hospital", "campus", "department", "ward")):
		validate_mapping_constant_scope(endpoint, source, constant_scope)
	if require_active:
		if str(mapping_doc.get("status") or "") != "Active":
			frappe.throw("Enabled integration endpoint Mapping Record must be Active")
		today = getdate(nowdate())
		effective_from = getdate(mapping_doc.effective_from) if mapping_doc.get("effective_from") else None
		effective_to = getdate(mapping_doc.effective_to) if mapping_doc.get("effective_to") else None
		if not effective_from or effective_from > today or (effective_to and effective_to < today):
			frappe.throw("Integration endpoint Mapping Record is outside its effective period")
	return definition


def frozen_mapping_from_audit_record(doc) -> MappingDefinition:
	try:
		definition = frozen_mapping_definition(
			record=doc.get("mapping_record"),
			code=doc.get("mapping_code"),
			version=doc.get("mapping_version"),
			checksum=doc.get("mapping_checksum"),
			snapshot=doc.get("mapping_snapshot"),
		)
	except ValueError as exc:
		raise frappe.ValidationError(str(exc)) from exc
	row = frappe.db.get_value(
		"IONE Integration Mapping",
		definition.record,
		[
			"mapping_code",
			"version",
			"checksum",
			"mapping_json",
			"master_data_json",
			"canonical_snapshot",
			"mapping_version_key",
			"source_system",
			"endpoint",
			"status",
		],
		as_dict=True,
	)
	if not row or row.get("status") not in {"Active", "Retired"}:
		frappe.throw("Frozen integration mapping lineage has no published source record")
	try:
		published = mapping_definition(
			record=definition.record,
			code=row.get("mapping_code"),
			version=row.get("version"),
			mapping=row.get("mapping_json"),
			master_data=row.get("master_data_json"),
			stored_checksum=row.get("checksum"),
			stored_snapshot=row.get("canonical_snapshot"),
		)
	except ValueError as exc:
		raise frappe.ValidationError(str(exc)) from exc
	if (
		published.code != definition.code
		or published.version != definition.version
		or not hmac.compare_digest(published.checksum, definition.checksum)
		or not hmac.compare_digest(published.snapshot, definition.snapshot)
		or row.get("source_system") != doc.get("source_system")
		or row.get("endpoint") != doc.get("endpoint")
		or row.get("mapping_version_key")
		!= _mapping_version_key(
			row.get("source_system"),
			row.get("endpoint"),
			published.code,
			published.version,
		)
	):
		frappe.throw("Frozen integration mapping lineage does not match its published version")
	return definition


def validate_mapping_lineage_identity(doc, method: str | None = None) -> None:
	del method
	has_lineage = any(doc.get(fieldname) not in (None, "") for fieldname in _MAPPING_LINEAGE_FIELDS)
	if doc.doctype == "IONE Clinical Quality Event" and not doc.get("payload_reference"):
		if has_lineage:
			frappe.throw("Internal clinical events cannot claim an external mapping lineage")
		return
	if not all(doc.get(fieldname) not in (None, "") for fieldname in _MAPPING_LINEAGE_FIELDS):
		frappe.throw("External integration audit records require a complete frozen mapping lineage")
	definition = frozen_mapping_from_audit_record(doc)
	if doc.doctype == "IONE Clinical Quality Event":
		message = frappe.get_doc("IONE Integration Message", doc.payload_reference)
		for fieldname in _MAPPING_LINEAGE_FIELDS:
			if str(message.get(fieldname) or "") != str(doc.get(fieldname) or ""):
				frappe.throw("Clinical event mapping lineage does not match its integration message")
		frozen_mapping_from_audit_record(message)
	if doc.is_new():
		return
	previous = frappe.get_doc(doc.doctype, doc.name, for_update=True)
	changed = [
		fieldname
		for fieldname in _MAPPING_LINEAGE_FIELDS
		if str(previous.get(fieldname) or "") != str(doc.get(fieldname) or "")
	]
	if changed:
		frappe.throw("Frozen integration mapping lineage is immutable after insertion")
	if not CHECKSUM_PATTERN.fullmatch(definition.checksum):
		frappe.throw("Frozen integration mapping checksum is invalid")


def validate_integration_job(doc, method: str | None = None) -> None:
	del method
	_require_runtime_update_token()
	for fieldname in ("cursor_before_hash", "cursor_after_hash"):
		value = str(doc.get(fieldname) or "")
		if value and not CHECKSUM_PATTERN.fullmatch(value):
			frappe.throw(f"{fieldname} must contain only a SHA-256 fingerprint")
	if doc.is_new():
		if doc.get("status") != "Running" or not doc.get("started_at"):
			frappe.throw("Integration runtime jobs must start in Running state with a timestamp")
		if not doc.get("endpoint") or not doc.get("source_system"):
			frappe.throw("Integration runtime jobs require an endpoint and source")
		if (
			frappe.db.get_value("IONE Integration Endpoint", doc.endpoint, "source_system")
			!= doc.source_system
		):
			frappe.throw("Integration runtime job source does not match its endpoint")
		if not 1 <= int(doc.get("requested_batch_size") or 0) <= 1_000:
			frappe.throw("Integration runtime job batch size must be between 1 and 1,000")
		if (
			doc.get("cursor_after_hash")
			or doc.get("completed_at")
			or doc.get("error_message")
			or any(
				int(doc.get(fieldname) or 0)
				for fieldname in (
					"received_count",
					"success_count",
					"duplicate_count",
					"quarantined_count",
					"error_count",
				)
			)
		):
			frappe.throw("A running integration job cannot contain terminal audit values")
		return
	previous = frappe.get_doc(doc.doctype, doc.name, for_update=True)
	changed = {
		fieldname
		for fieldname in _JOB_AUDIT_FIELDS
		if str(previous.get(fieldname) or "") != str(doc.get(fieldname) or "")
	}
	unexpected = changed - _JOB_FINISH_FIELDS
	if unexpected:
		frappe.throw("Unsupported integration job runtime update: " + ", ".join(sorted(unexpected)))
	if previous.get("status") != "Running" or doc.get("status") not in {
		"Completed",
		"Partial",
		"Failed",
	}:
		frappe.throw("Integration jobs may only transition once from Running to a terminal state")
	if not doc.get("completed_at"):
		frappe.throw("Terminal integration jobs require completed_at")
	counts = {}
	for fieldname in (
		"received_count",
		"success_count",
		"duplicate_count",
		"quarantined_count",
		"error_count",
	):
		counts[fieldname] = int(doc.get(fieldname) or 0)
		if counts[fieldname] < 0:
			frappe.throw("Integration job counters cannot be negative")
	if counts["received_count"] > int(doc.get("requested_batch_size") or 0):
		frappe.throw("Integration job received count exceeds its requested batch")
	if counts["duplicate_count"] > counts["success_count"]:
		frappe.throw("Integration job duplicate count cannot exceed successful receipts")
	if counts["quarantined_count"] > counts["error_count"]:
		frappe.throw("Integration job quarantine count cannot exceed its error count")
	if doc.get("status") in {"Completed", "Partial"} and counts["received_count"] != (
		counts["success_count"] + counts["error_count"]
	):
		frappe.throw("Integration job terminal counters do not reconcile")
	if doc.get("status") == "Completed" and counts["error_count"]:
		frappe.throw("A completed integration job cannot contain errors")
	if doc.get("status") == "Failed" and not counts["error_count"]:
		frappe.throw("A failed integration job requires a sanitized error count")


def update_endpoint_runtime_state(endpoint, values: dict[str, object]) -> None:
	unexpected = set(values) - _ENDPOINT_RUNTIME_FIELDS
	if unexpected:
		raise frappe.ValidationError(
			"Unsupported integration endpoint runtime update: " + ", ".join(sorted(unexpected))
		)
	with trusted_integration_runtime_updates():
		frappe.db.set_value(
			endpoint.doctype,
			endpoint.name,
			values,
			update_modified=False,
		)
		for fieldname, value in values.items():
			endpoint.set(fieldname, value)


def validate_integration_settings(doc, method: str | None = None) -> None:
	del doc
	if method:
		_require_named_integration_administrator()


def _require_named_integration_administrator() -> None:
	if (
		getattr(frappe.flags, "ione_integration_configuration_maintenance", None)
		is _INTEGRATION_CONFIGURATION_MAINTENANCE_TOKEN
	):
		return
	user = str(frappe.session.user or "")
	if user in {"", "Guest", "Administrator"}:
		frappe.throw(
			"Integration configuration requires a named Integration Administrator",
			frappe.PermissionError,
		)
	if "IONE Integration Administrator" not in set(frappe.get_roles(user)):
		frappe.throw(
			"Only an IONE Integration Administrator may change integration configuration",
			frappe.PermissionError,
		)


def _require_runtime_update_token() -> None:
	if (
		getattr(frappe.flags, "ione_integration_runtime_update", None)
		is not _INTEGRATION_RUNTIME_UPDATE_TOKEN
	):
		frappe.throw(
			"Integration operational audit records are application-managed",
			frappe.PermissionError,
		)


def _mapping_version_key(
	source_system,
	endpoint,
	mapping_code,
	version,
) -> str:
	return hashlib.sha256(
		json.dumps(
			[
				str(source_system or ""),
				str(endpoint or ""),
				str(mapping_code or ""),
				str(version or ""),
			],
			ensure_ascii=False,
			separators=(",", ":"),
		).encode()
	).hexdigest()


def _canonical_json(value: dict) -> str:
	return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_endpoint_runtime_fields(doc) -> None:
	if doc.is_new():
		if any(doc.get(fieldname) not in (None, "") for fieldname in _ENDPOINT_RUNTIME_FIELDS):
			_require_runtime_update_token()
		return
	previous = frappe.db.get_value(
		doc.doctype,
		doc.name,
		list(_ENDPOINT_RUNTIME_FIELDS),
		as_dict=True,
	)
	if not previous:
		return
	if any(
		str(previous.get(fieldname) or "") != str(doc.get(fieldname) or "")
		for fieldname in _ENDPOINT_RUNTIME_FIELDS
	):
		_require_runtime_update_token()


def _validate_endpoint_url(url: str, allowed_hosts) -> None:
	parsed = urlparse(str(url))
	if (
		parsed.scheme not in {"http", "https"}
		or not parsed.hostname
		or parsed.username
		or parsed.password
		or parsed.query
		or parsed.fragment
	):
		frappe.throw("Integration URL must be an absolute HTTP(S) URL without credentials")
	hosts = _validate_hosts(allowed_hosts)
	if parsed.hostname.lower() not in hosts:
		frappe.throw("Integration URL host is not allowlisted")
	if parsed.scheme == "http" and not _is_private_host(parsed.hostname):
		frappe.throw("Non-private integration endpoints require HTTPS")


def _connector_options(doc) -> dict:
	raw = doc.get("connector_options") or "{}"
	try:
		options = json.loads(raw) if isinstance(raw, str) else raw
	except ValueError as exc:
		raise frappe.ValidationError("connector_options must be valid JSON") from exc
	if not isinstance(options, dict):
		raise frappe.ValidationError("connector_options must be a JSON object")
	return options


def _reject_embedded_secrets(value, path: str = "connector_options") -> None:
	if isinstance(value, dict):
		for key, item in value.items():
			name = str(key)
			if re.search(
				r"(?:^|_)(?:password|passwd|secret|token|api_key|credential)(?:$|_)",
				name,
				flags=re.IGNORECASE,
			):
				frappe.throw(f"Secrets must use endpoint Password fields, not {path}.{name}")
			_reject_embedded_secrets(item, f"{path}.{name}")
	elif isinstance(value, list):
		for index, item in enumerate(value):
			_reject_embedded_secrets(item, f"{path}[{index}]")


def _validate_hl7_v2_endpoint_contract(doc) -> None:
	"""Fail closed when either half of the inbound HL7 v2 identity is selected."""
	connector_type = str(doc.get("connector_type") or "")
	connector_key = str(doc.get("connector_key") or "")
	is_hl7_v2 = connector_type.strip().casefold() == "hl7 v2" or connector_key.strip().casefold() == "hl7_v2"
	if not is_hl7_v2:
		return
	if connector_type != "HL7 v2" or connector_key != "hl7_v2":
		frappe.throw("HL7 v2 endpoints require Connector Type HL7 v2 and Connector Key hl7_v2")
	if doc.get("direction") != "Inbound":
		frappe.throw("HL7 v2 endpoints are signed inbound HTTP endpoints")
	if doc.get("authentication_type") != "HMAC":
		frappe.throw("HL7 v2 endpoints require Authentication Type HMAC")
	if type(doc.get("require_signature")) is not int or doc.get("require_signature") != 1:
		frappe.throw("HL7 v2 endpoints must require an HMAC signature")
	try:
		normalize_hl7_v2_transport_options(doc.get("connector_options"))
		normalize_hl7_v2_batch_size(doc.get("batch_size"))
	except HL7V2ContractError as exc:
		raise frappe.ValidationError(str(exc)) from exc


def _validate_oracle_options(doc, options: dict) -> None:
	from ione_qms.integration.builtin_connectors import registered_oracle_queries

	query_key = str(options.get("query_key") or "").strip().upper()
	if query_key not in registered_oracle_queries():
		frappe.throw(f"Oracle query '{query_key}' is not registered in reviewed application code")
	host = str(options.get("host") or "").strip().lower()
	if not host or host not in _validate_hosts(doc.get("allowed_hosts")):
		frappe.throw("Oracle connector host must be present in allowed_hosts")
	if not str(options.get("service_name") or "").strip():
		frappe.throw("Oracle connector service_name is required")
	_bounded_integer(options, "port", default=1521, minimum=1, maximum=65535)
	pool_min = _bounded_integer(options, "pool_min", default=1, minimum=1, maximum=5)
	_bounded_integer(options, "pool_max", default=4, minimum=pool_min, maximum=20)
	_bounded_integer(options, "pool_idle_timeout", default=60, minimum=10, maximum=3600)
	_bounded_integer(options, "pool_max_lifetime", default=3600, minimum=60, maximum=86400)
	_bounded_integer(options, "pool_ping_interval", default=60, minimum=0, maximum=3600)
	_bounded_integer(options, "statement_cache_size", default=50, minimum=0, maximum=500)
	_bounded_integer(options, "slow_query_ms", default=2000, minimum=100, maximum=60000)
	_bounded_integer(
		options,
		"circuit_breaker_failures",
		default=5,
		minimum=1,
		maximum=20,
	)
	_bounded_integer(
		options,
		"circuit_breaker_open_seconds",
		default=60,
		minimum=10,
		maximum=3600,
	)


def _bounded_integer(
	options: dict,
	fieldname: str,
	*,
	default: int,
	minimum: int,
	maximum: int,
) -> int:
	try:
		value = int(options.get(fieldname) if options.get(fieldname) is not None else default)
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError(f"{fieldname} must be an integer") from exc
	if not minimum <= value <= maximum:
		frappe.throw(f"{fieldname} must be between {minimum} and {maximum}")
	return value


def _validate_hosts(value) -> frozenset[str]:
	if not value:
		return frozenset()
	try:
		hosts = json.loads(value) if isinstance(value, str) and value.lstrip().startswith("[") else value
	except ValueError as exc:
		raise frappe.ValidationError("allowed_hosts must be valid JSON or line-separated hosts") from exc
	if isinstance(hosts, str):
		hosts = hosts.replace(",", "\n").splitlines()
	if not isinstance(hosts, list | tuple):
		frappe.throw("allowed_hosts must be an array or line-separated list")
	normalized = frozenset(str(host).strip().lower() for host in hosts if str(host).strip())
	for host in normalized:
		if "://" in host or "/" in host or "@" in host:
			frappe.throw("allowed_hosts entries must contain hostnames or IP addresses only")
	return normalized


def _is_private_host(host: str) -> bool:
	if host.lower().endswith(".internal"):
		return True
	try:
		address = ipaddress.ip_address(host)
		return address.is_private or address.is_loopback
	except ValueError:
		return False


def _production_mode() -> bool:
	return production_mode_enabled()


def _shared_secret(doc) -> str:
	try:
		value = doc.get_password("shared_secret", raise_exception=False)
	except Exception:
		value = doc.get("shared_secret")
	return str(value or "")
