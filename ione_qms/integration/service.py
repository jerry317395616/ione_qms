from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

from ione_qms.integration.connectors import decode_connector_quarantine_record
from ione_qms.integration.mapping import MappingDefinition, apply_mapping
from ione_qms.integration.master_data import (
	SourceVersionConflictError,
	StaleSourceVersionError,
	merge_scopes,
	resolve_and_materialize_indexes,
	validate_resolved_scope,
)
from ione_qms.integration.projections import materialize_event_projection
from ione_qms.integration.schemas import (
	EVENT_TYPE_PATTERN,
	IDENTIFIER_PATTERN,
	MAX_PAYLOAD_BYTES,
	IncomingClinicalEvent,
)
from ione_qms.rule_engine.executor import enqueue_event_rules
from ione_qms.services.integration_config import (
	assert_integration_endpoint_runtime,
	frozen_mapping_from_audit_record,
	resolve_endpoint_mapping,
)
from ione_qms.services.integration_scope import (
	EndpointScopeViolationError,
	authorize_endpoint_scope,
	resolve_department_reference,
	scope_policy_checksum,
)

MESSAGE_RUNTIME_UPDATE_FIELDS = frozenset(
	{
		"attempts",
		"campus",
		"department",
		"error_message",
		"event",
		"hospital",
		"next_retry_at",
		"payload_json",
		"processed_at",
		"scope_policy_hash",
		"scope_resolution_status",
		"source_record_id",
		"status",
		"ward",
	}
)
MESSAGE_MAPPING_LINEAGE_FIELDS = (
	"mapping_record",
	"mapping_code",
	"mapping_version",
	"mapping_checksum",
	"mapping_snapshot",
)
INDICATOR_SOURCE_DIMENSION_FIELDS = ("medical_group", "disease", "surgery", "drg")
MAPPED_SCOPE_FIELDS = frozenset(
	{
		"patient_index",
		"encounter_index",
		"hospital",
		"campus",
		"department",
		"ward",
		"responsible_staff",
		"medical_staff",
	}
)


def receive_clinical_event(
	endpoint_name: str,
	payload: dict[str, Any],
) -> dict[str, Any]:
	endpoint = frappe.get_cached_doc("IONE Integration Endpoint", endpoint_name)
	assert_integration_endpoint_runtime(endpoint)
	source = frappe.get_cached_doc("IONE Source System", endpoint.source_system)
	source_namespace = _assert_source_runtime(source)
	mapping_definition = resolve_endpoint_mapping(endpoint, source)

	payload_json = _canonical_json(payload)
	payload_bytes = payload_json.encode()
	payload_hash = hashlib.sha256(payload_bytes).hexdigest()
	raw_scope = _preauthorize_raw_scope(endpoint, source, payload, mapping_definition.values)
	if len(payload_bytes) > MAX_PAYLOAD_BYTES:
		return _quarantine_invalid_payload(
			endpoint=endpoint,
			source=source,
			payload=payload,
			payload_json=_oversized_payload_manifest(
				payload_hash=payload_hash,
				payload_bytes=len(payload_bytes),
			),
			payload_hash=payload_hash,
			failure_type="OversizedPayloadError",
			scope=raw_scope,
			scope_rejected=raw_scope is None,
			sanitize_identity=True,
			mapping_definition=mapping_definition,
		)
	try:
		incoming = IncomingClinicalEvent.from_payload(payload)
	except (TypeError, ValueError) as exc:
		retained_json = _invalid_schema_manifest(
			payload_hash=payload_hash,
			scope_resolved=raw_scope is not None,
		)
		return _quarantine_invalid_payload(
			endpoint=endpoint,
			source=source,
			payload=payload,
			payload_json=retained_json,
			payload_hash=payload_hash,
			failure_type=(type(exc).__name__ if raw_scope is not None else "EndpointScopeViolationError"),
			scope=raw_scope,
			scope_rejected=raw_scope is None,
			sanitize_identity=True,
			mapping_definition=mapping_definition,
		)
	try:
		preauthorized_scope = _preauthorize_scope(
			endpoint,
			source,
			incoming,
			payload,
			mapping_definition.values,
		)
	except EndpointScopeViolationError:
		return _quarantine_scope_violation(
			endpoint=endpoint,
			source=source,
			incoming=incoming,
			payload_hash=payload_hash,
			mapping_definition=mapping_definition,
		)
	policy_hash = scope_policy_checksum(endpoint, source)
	idempotency_key = _idempotency_key(
		source_namespace,
		preauthorized_scope["hospital"],
		incoming,
	)
	existing = _message_by_idempotency_key(idempotency_key)
	if existing:
		if existing.payload_hash and not hmac.compare_digest(
			str(existing.payload_hash),
			payload_hash,
		):
			return _quarantine_source_version_conflict(
				endpoint=endpoint,
				source=source,
				incoming=incoming,
				payload_json=payload_json,
				payload_hash=payload_hash,
				canonical_idempotency_key=idempotency_key,
				scope=preauthorized_scope,
				mapping_definition=mapping_definition,
			)
		if existing.endpoint != endpoint.name:
			return _cross_endpoint_duplicate_outcome(
				message_name=existing.name,
				endpoint=endpoint,
				source=source,
				tenant_hospital=preauthorized_scope["hospital"],
				payload_hash=payload_hash,
				incoming=incoming,
			)
		return _process_with_lock(
			message_name=existing.name,
			endpoint=endpoint,
			source=source,
			incoming=incoming,
			payload=payload,
			policy_hash=policy_hash,
			duplicate=True,
		)
	message = frappe.get_doc(
		{
			"doctype": "IONE Integration Message",
			"source_system": source.name,
			"endpoint": endpoint.name,
			"idempotency_key": idempotency_key,
			"source_record_type": incoming.source_record_type,
			"source_record_id": incoming.source_record_id,
			"source_version": incoming.source_version,
			"event_type": incoming.event_type,
			"event_time": incoming.event_time,
			"source_namespace": source_namespace,
			**{
				fieldname: preauthorized_scope.get(fieldname)
				for fieldname in ("hospital", "campus", "department", "ward")
			},
			"scope_policy_hash": policy_hash,
			"scope_resolution_status": "Resolved",
			"payload_hash": payload_hash,
			"payload_json": payload_json,
			"received_at": now_datetime(),
			"status": "Received",
			"attempts": 0,
			**mapping_definition.lineage_values(),
		}
	)
	try:
		_insert_message(message, idempotency_key)
	except frappe.DuplicateEntryError:
		existing = _message_by_idempotency_key(idempotency_key, current_read=True)
		if not existing or not hmac.compare_digest(
			str(existing.payload_hash or ""),
			payload_hash,
		):
			return _quarantine_source_version_conflict(
				endpoint=endpoint,
				source=source,
				incoming=incoming,
				payload_json=payload_json,
				payload_hash=payload_hash,
				canonical_idempotency_key=idempotency_key,
				scope=preauthorized_scope,
				mapping_definition=mapping_definition,
			)
		if existing.endpoint != endpoint.name:
			return _cross_endpoint_duplicate_outcome(
				message_name=existing.name,
				endpoint=endpoint,
				source=source,
				tenant_hospital=preauthorized_scope["hospital"],
				payload_hash=payload_hash,
				incoming=incoming,
			)
		return _process_with_lock(
			message_name=existing.name,
			endpoint=endpoint,
			source=source,
			incoming=incoming,
			payload=payload,
			policy_hash=policy_hash,
			duplicate=True,
		)
	return _process_with_lock(
		message_name=message.name,
		endpoint=endpoint,
		source=source,
		incoming=incoming,
		payload=payload,
		policy_hash=policy_hash,
		duplicate=False,
	)


def retry_clinical_event_message(
	message_name: str,
	payload: dict[str, Any],
) -> dict[str, Any]:
	"""Retry one existing receipt with its frozen mapping under current scope policy."""
	message = frappe.get_doc("IONE Integration Message", message_name)
	endpoint = frappe.get_cached_doc("IONE Integration Endpoint", message.endpoint)
	assert_integration_endpoint_runtime(endpoint)
	source = frappe.get_cached_doc("IONE Source System", endpoint.source_system)
	_assert_source_runtime(source)
	if message.get("source_system") != source.name:
		frappe.throw("Integration retry source no longer matches its frozen receipt")
	payload_json = _canonical_json(payload)
	payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
	if not message.get("payload_hash") or not hmac.compare_digest(
		str(message.payload_hash),
		payload_hash,
	):
		frappe.throw("Integration retry payload does not match its frozen receipt")
	incoming = IncomingClinicalEvent.from_payload(payload)
	return _process_with_lock(
		message_name=message.name,
		endpoint=endpoint,
		source=source,
		incoming=incoming,
		payload=payload,
		policy_hash=scope_policy_checksum(endpoint, source),
		duplicate=True,
	)


def quarantine_invalid_clinical_event(
	endpoint_name: str,
	payload: Any,
	*,
	failure_type: str = "InvalidConnectorRecord",
) -> dict[str, Any]:
	"""Durably quarantine one connector item that cannot enter the event schema."""
	endpoint = frappe.get_cached_doc("IONE Integration Endpoint", endpoint_name)
	assert_integration_endpoint_runtime(endpoint)
	source = frappe.get_cached_doc("IONE Source System", endpoint.source_system)
	_assert_source_runtime(source)
	mapping_definition = resolve_endpoint_mapping(endpoint, source)
	payload_json = _canonical_json(payload)
	payload_bytes = payload_json.encode()
	payload_hash = hashlib.sha256(payload_bytes).hexdigest()
	retained_marker = _retained_connector_marker(endpoint, payload, failure_type)
	if retained_marker is not None:
		payload_json = retained_marker
		payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
		raw_scope = None
		scope_rejected = False
	elif len(payload_bytes) > MAX_PAYLOAD_BYTES:
		raw_scope = _preauthorize_raw_scope(endpoint, source, payload, mapping_definition.values)
		payload_json = _oversized_payload_manifest(
			payload_hash=payload_hash,
			payload_bytes=len(payload_bytes),
		)
		failure_type = "OversizedPayloadError"
		scope_rejected = raw_scope is None
	else:
		raw_scope = _preauthorize_raw_scope(endpoint, source, payload, mapping_definition.values)
		payload_json = _invalid_schema_manifest(
			payload_hash=payload_hash,
			scope_resolved=raw_scope is not None,
		)
		scope_rejected = raw_scope is None
	return _quarantine_invalid_payload(
		endpoint=endpoint,
		source=source,
		payload=payload,
		payload_json=payload_json,
		payload_hash=payload_hash,
		failure_type=failure_type,
		scope=raw_scope,
		scope_rejected=scope_rejected,
		sanitize_identity=True,
		mapping_definition=mapping_definition,
	)


def reject_retained_message_scope(message) -> None:
	"""Terminally sanitize a retained retry whose current endpoint policy rejects it."""
	_update_message(
		message,
		{
			"status": "Dead Letter",
			"next_retry_at": None,
			"scope_resolution_status": "Rejected",
			"payload_json": _scope_violation_manifest(payload_hash=str(message.get("payload_hash") or "")),
			"source_record_id": _hashed_identity(message.get("source_record_id")),
			"error_message": "EndpointScopeViolationError: current scope policy rejected replay",
		},
	)


def _process_with_lock(
	*,
	message_name: str,
	endpoint,
	source,
	incoming: IncomingClinicalEvent,
	payload: dict[str, Any],
	policy_hash: str,
	duplicate: bool,
) -> dict[str, Any]:
	try:
		with frappe.db.advisory_lock(
			f"ione-qms:message:{message_name}",
			timeout=0,
		):
			message = frappe.get_doc("IONE Integration Message", message_name, for_update=True)
			mapping_definition = frozen_mapping_from_audit_record(message)
			if (
				message.get("source_system") != source.name
				or message.get("endpoint") != endpoint.name
				or message.get("source_namespace") != source.get("identity_namespace")
			):
				frappe.throw(
					"Integration receipt no longer matches its immutable endpoint identity",
					frappe.PermissionError,
				)
			if message.status == "Processed":
				return _message_outcome(message, duplicate=True)
			if message.status == "Dead Letter":
				return _message_outcome(message, duplicate=duplicate, retryable=False)
			if (
				message.get("source_record_type") != incoming.source_record_type
				or message.get("source_record_id") != incoming.source_record_id
				or message.get("source_version") != incoming.source_version
			):
				frappe.throw(
					"Integration receipt no longer matches its immutable source identity",
					frappe.PermissionError,
				)
			try:
				preauthorized_scope = _preauthorize_scope(
					endpoint,
					source,
					incoming,
					payload,
					mapping_definition.values,
				)
			except EndpointScopeViolationError:
				reject_retained_message_scope(message)
				message.reload()
				return _message_outcome(message, duplicate=duplicate, retryable=False)
			if (
				message.get("hospital") != preauthorized_scope.get("hospital")
				or message.get("scope_policy_hash") != policy_hash
			):
				reject_retained_message_scope(message)
				message.reload()
				return _message_outcome(message, duplicate=duplicate, retryable=False)
			if message.status == "Error":
				retry_count = min(max(int(endpoint.get("retry_count") or 5), 0), 20)
				if int(message.get("attempts") or 0) >= retry_count:
					_update_message(message, {"status": "Dead Letter"})
					message.reload()
					return _message_outcome(message, duplicate=duplicate, retryable=False)
				if message.get("next_retry_at") and get_datetime(message.next_retry_at) > now_datetime():
					return _message_outcome(message, duplicate=duplicate, deferred=True)
			return _process_message(
				message=message,
				endpoint=endpoint,
				source=source,
				incoming=incoming,
				payload=payload,
				preauthorized_scope=preauthorized_scope,
				mapping_definition=mapping_definition,
				policy_hash=policy_hash,
				duplicate=duplicate,
			)
	except frappe.QueryTimeoutError:
		message = frappe.get_doc("IONE Integration Message", message_name, for_update=True)
		return _message_outcome(message, duplicate=True, deferred=True)


def _cross_endpoint_duplicate_outcome(
	*,
	message_name: str,
	endpoint,
	source,
	tenant_hospital: str,
	payload_hash: str,
	incoming: IncomingClinicalEvent,
) -> dict[str, Any]:
	"""Never reprocess an existing business identity with a later endpoint."""
	try:
		with frappe.db.advisory_lock(f"ione-qms:message:{message_name}", timeout=0):
			message = frappe.get_doc("IONE Integration Message", message_name, for_update=True)
			if (
				message.get("source_system") != source.name
				or message.get("source_namespace") != source.get("identity_namespace")
				or message.get("hospital") != tenant_hospital
				or message.get("endpoint") == endpoint.name
				or not hmac.compare_digest(str(message.get("payload_hash") or ""), payload_hash)
				or message.get("source_record_type") != incoming.source_record_type
				or message.get("source_record_id") != incoming.source_record_id
				or message.get("source_version") != incoming.source_version
			):
				frappe.throw(
					"Cross-endpoint duplicate conflicts with the immutable first receipt",
					frappe.PermissionError,
				)
			if message.status in {"Received", "Error"}:
				return _message_outcome(
					message,
					duplicate=True,
					retryable=message.status == "Error",
					deferred=True,
				)
			return _message_outcome(message, duplicate=True, retryable=False)
	except frappe.QueryTimeoutError:
		message = frappe.get_doc("IONE Integration Message", message_name)
		return _message_outcome(message, duplicate=True, deferred=True)


def _process_message(
	*,
	message,
	endpoint,
	source,
	incoming: IncomingClinicalEvent,
	payload: dict[str, Any],
	preauthorized_scope: dict[str, str],
	mapping_definition: MappingDefinition,
	policy_hash: str,
	duplicate: bool,
) -> dict[str, Any]:
	savepoint = f"ione_qms_msg_{hashlib.sha256(message.name.encode()).hexdigest()[:16]}"
	frappe.db.savepoint(savepoint)
	failure_type: str | None = None
	terminal_failure = False
	try:
		if message.get("endpoint") != endpoint.name:
			raise EndpointScopeViolationError("Integration message endpoint identity changed")
		if message.get("hospital") != preauthorized_scope["hospital"]:
			raise EndpointScopeViolationError("Integration message hospital identity changed")
		if message.get("scope_policy_hash") != policy_hash:
			raise EndpointScopeViolationError("Integration scope policy changed before processing")
		mapped_values = apply_mapping(payload, mapping_definition.values)
		mapped_scope = validate_resolved_scope(_mapping_scope_values(mapped_values))
		index_scope = resolve_and_materialize_indexes(
			endpoint=endpoint,
			source=source,
			incoming=incoming,
			payload_hash=str(message.get("payload_hash") or ""),
			tenant_hospital=preauthorized_scope["hospital"],
			source_namespace=str(source.get("identity_namespace") or ""),
			master_data_config=mapping_definition.master_data,
			mapping_lineage=mapping_definition.lineage_values(),
		)
		scope = authorize_endpoint_scope(
			endpoint,
			source,
			merge_scopes(
				preauthorized_scope,
				_mapping_scope_values(index_scope),
				mapped_scope,
			),
		)
		indicator_dimensions = _indicator_dimension_values(index_scope, mapped_values)
		_update_message(
			message,
			{
				**{
					fieldname: scope.get(fieldname)
					for fieldname in ("hospital", "campus", "department", "ward")
				},
				"scope_policy_hash": policy_hash,
				"scope_resolution_status": "Resolved",
			},
		)
		message.reload()
		event_name = frappe.db.get_value(
			"IONE Clinical Quality Event",
			{"idempotency_key": message.idempotency_key},
			"name",
		)
		if not event_name:
			event = frappe.get_doc(
				{
					"doctype": "IONE Clinical Quality Event",
					"event_type": incoming.event_type,
					"event_time": incoming.event_time,
					"source_system": source.name,
					"endpoint": endpoint.name,
					"source_namespace": str(source.get("identity_namespace") or ""),
					"scope_policy_hash": policy_hash,
					"source_record_type": incoming.source_record_type,
					"source_record_id": incoming.source_record_id,
					"source_version": incoming.source_version,
					"patient_reference": incoming.patient_reference,
					"encounter_reference": incoming.encounter_reference,
					"department_reference": incoming.department_reference,
					"payload_reference": message.name,
					"payload_json": _canonical_json(incoming.payload),
					"idempotency_key": message.idempotency_key,
					"processing_status": "Pending",
					**mapping_definition.lineage_values(),
					**{
						fieldname: scope.get(fieldname)
						for fieldname in (
							"patient_index",
							"encounter_index",
							"hospital",
							"campus",
							"department",
							"ward",
							"responsible_staff",
							"medical_staff",
						)
					},
					**indicator_dimensions,
				}
			)
			event.flags.skip_rule_enqueue = True
			try:
				event.insert(ignore_permissions=True)
				event_name = event.name
			except frappe.DuplicateEntryError:
				event_name = frappe.db.get_value(
					"IONE Clinical Quality Event",
					{"idempotency_key": message.idempotency_key},
					"name",
				)
				if not event_name:
					raise
		existing_event = frappe.get_doc("IONE Clinical Quality Event", event_name, for_update=True)
		for fieldname, expected in {
			"payload_reference": message.name,
			"source_system": source.name,
			"endpoint": endpoint.name,
			"source_namespace": str(source.get("identity_namespace") or ""),
			"idempotency_key": message.idempotency_key,
			**mapping_definition.lineage_values(),
			**{
				fieldname: indicator_dimensions.get(fieldname)
				for fieldname in INDICATOR_SOURCE_DIMENSION_FIELDS
			},
		}.items():
			if str(existing_event.get(fieldname) or "") != str(expected or ""):
				raise EndpointScopeViolationError(
					"Existing clinical event does not match its frozen integration lineage"
				)
		frozen_mapping_from_audit_record(existing_event)
		materialize_event_projection(event_name, incoming.event_type, incoming.payload)
		_update_message(
			message,
			{
				"event": event_name,
				"status": "Processed",
				"processed_at": now_datetime(),
				"next_retry_at": None,
				"error_message": None,
			},
		)
		message.reload()
		_resolve_processing_issue(message.name)
		enqueue_event_rules(frappe.get_doc("IONE Clinical Quality Event", event_name))
		return _message_outcome(message, duplicate=duplicate)
	except Exception as exc:
		# Do not invoke Frappe/Sentry logging while the clinical-processing
		# exception is active: traceback locals can contain the source payload.
		failure_type = type(exc).__name__
		terminal_failure = isinstance(
			exc,
			(StaleSourceVersionError, SourceVersionConflictError, EndpointScopeViolationError),
		)

	frappe.db.rollback(save_point=savepoint)
	message = frappe.get_doc("IONE Integration Message", message.name)
	attempts = int(message.get("attempts") or 0) + 1
	retry_count = min(max(int(endpoint.get("retry_count") or 5), 0), 20)
	status = "Dead Letter" if terminal_failure or attempts >= retry_count else "Error"
	base_delay = min(max(int(endpoint.get("retry_backoff_seconds") or 30), 1), 3600)
	delay = min(base_delay * (2 ** max(attempts - 1, 0)), 3600)
	_update_message(
		message,
		{
			"status": status,
			"error_message": f"{failure_type}: processing failed",
			"attempts": attempts,
			"next_retry_at": (add_to_date(now_datetime(), seconds=delay) if status == "Error" else None),
			**(
				{
					"payload_json": _scope_violation_manifest(
						payload_hash=str(message.get("payload_hash") or "")
					),
					"source_record_id": hashlib.sha256(
						str(message.get("source_record_id") or "").encode()
					).hexdigest(),
					"scope_resolution_status": "Rejected",
				}
				if failure_type == "EndpointScopeViolationError"
				else {}
			),
		},
	)
	frappe.log_error(
		title="IONE QMS integration message processing failed",
		message=f"{failure_type}: processing failed; clinical payload was not logged.",
		reference_doctype="IONE Integration Message",
		reference_name=message.name,
	)
	issue_failure_type: str | None = None
	try:
		_record_processing_issue(message, failure_type)
	except Exception as exc:
		issue_failure_type = type(exc).__name__
	if issue_failure_type is not None:
		frappe.log_error(
			title="IONE QMS could not persist integration data-quality issue",
			message=(
				f"{issue_failure_type}: data-quality issue persistence failed; "
				"clinical payload was not logged."
			),
			reference_doctype="IONE Integration Message",
			reference_name=message.name,
		)
	message.reload()
	return _message_outcome(
		message,
		duplicate=duplicate,
		retryable=status == "Error",
	)


def _record_processing_issue(message, failure_type: str) -> None:
	issue_type = "Integration Processing Failure"
	issue_key = hashlib.sha256(f"{issue_type}|{message.name}".encode()).hexdigest()
	values = {
		"issue_key": issue_key,
		"source_system": message.get("source_system"),
		"message": message.name,
		"source_record_type": message.get("source_record_type"),
		"source_record_id": message.get("source_record_id"),
		**{fieldname: message.get(fieldname) for fieldname in ("hospital", "campus", "department", "ward")},
		"issue_type": issue_type,
		"severity": "High" if message.status == "Dead Letter" else "Medium",
		"status": "Open",
		"description": (
			f"{failure_type}: normalized integration processing failed. "
			"Clinical values and credentials are omitted; inspect the governed message."
		),
		"detected_at": now_datetime(),
	}
	name = frappe.db.get_value("IONE Data Quality Issue", {"issue_key": issue_key}, "name")
	if name:
		frappe.db.set_value(
			"IONE Data Quality Issue",
			name,
			{
				"severity": values["severity"],
				"status": "Open",
				"description": values["description"],
				"detected_at": values["detected_at"],
				"resolution": None,
				"resolved_by": None,
				"resolved_at": None,
			},
			update_modified=False,
		)
		return
	frappe.get_doc({"doctype": "IONE Data Quality Issue", **values}).insert(ignore_permissions=True)


def _resolve_processing_issue(message_name: str) -> None:
	issue_key = hashlib.sha256(f"Integration Processing Failure|{message_name}".encode()).hexdigest()
	name = frappe.db.get_value(
		"IONE Data Quality Issue",
		{"issue_key": issue_key, "status": ["in", ["Open", "Investigating"]]},
		"name",
	)
	if name:
		frappe.db.set_value(
			"IONE Data Quality Issue",
			name,
			{
				"status": "Resolved",
				"resolution": "The exact retained message was processed successfully by an idempotent retry.",
				"resolved_at": now_datetime(),
			},
			update_modified=False,
		)


def _update_message(message, values: dict[str, Any]) -> None:
	unexpected = set(values) - MESSAGE_RUNTIME_UPDATE_FIELDS
	if unexpected:
		raise frappe.ValidationError(
			"Unsupported integration message runtime update: " + ", ".join(sorted(unexpected))
		)
	current = frappe.get_doc("IONE Integration Message", message.name, for_update=True)
	_validate_message_identity_update(current, values)
	supported = {key: value for key, value in values.items() if current.meta.has_field(key)}
	if supported:
		current.db_set(supported, update_modified=False)


def _validate_message_identity_update(message, values: dict[str, Any]) -> None:
	_validate_message_state_update(message, values)
	for fieldname in ("hospital", "scope_policy_hash"):
		if fieldname in values and str(values[fieldname] or "") != str(message.get(fieldname) or ""):
			raise frappe.PermissionError(f"Integration message {fieldname} is immutable after insertion")
	for fieldname in ("campus", "department", "ward"):
		if fieldname not in values:
			continue
		current = str(message.get(fieldname) or "")
		proposed = str(values[fieldname] or "")
		if current and proposed != current:
			raise frappe.PermissionError(
				f"Integration message {fieldname} cannot be rebound after resolution"
			)
		if not current and not proposed:
			continue
	_validate_message_scope_fill(message, values)
	if "source_record_id" in values:
		current_identity = str(message.get("source_record_id") or "")
		proposed_identity = str(values["source_record_id"] or "")
		if proposed_identity != _hashed_identity(current_identity):
			raise frappe.PermissionError(
				"Integration message source_record_id may only be replaced by its SHA-256 digest"
			)
	if "payload_json" in values:
		_validate_runtime_quarantine_manifest(
			values["payload_json"],
			str(message.get("payload_hash") or ""),
		)


def _validate_message_state_update(message, values: dict[str, Any]) -> None:
	current_status = str(message.get("status") or "")
	proposed_status = str(values.get("status", current_status) or "")
	allowed_transitions = {
		"Received": {"Received", "Processed", "Error", "Dead Letter"},
		"Error": {"Error", "Processed", "Dead Letter"},
		"Processed": {"Processed"},
		"Dead Letter": {"Dead Letter"},
	}
	if current_status and proposed_status not in allowed_transitions.get(current_status, set()):
		raise frappe.PermissionError(
			f"Unsupported integration message state transition: {current_status} -> {proposed_status}"
		)
	current_event = str(message.get("event") or "")
	proposed_event = str(values.get("event", current_event) or "")
	if current_event and proposed_event != current_event:
		raise frappe.PermissionError("A processed integration message cannot be rebound to another event")
	if current_status == "Processed" and any(
		str(values[fieldname] or "") != str(message.get(fieldname) or "") for fieldname in values
	):
		raise frappe.PermissionError("Processed integration message history is immutable")
	if proposed_status == "Processed":
		if not proposed_event:
			raise frappe.PermissionError("A processed integration message requires its exact event")
		if not current_event:
			_validate_message_event_binding(message, proposed_event)
	elif proposed_event:
		raise frappe.PermissionError("Only a processed integration message may reference an event")
	if "attempts" in values:
		current_attempts = int(message.get("attempts") or 0)
		proposed_attempts = int(values["attempts"] or 0)
		if (
			proposed_attempts not in {current_attempts, current_attempts + 1}
			or not 0 <= proposed_attempts <= 20
		):
			raise frappe.PermissionError("Integration message attempts may only advance by one")
	if "scope_resolution_status" in values:
		current_scope_status = str(message.get("scope_resolution_status") or "")
		proposed_scope_status = str(values["scope_resolution_status"] or "")
		allowed_scope_transitions = {
			"": {"Resolved", "Unresolved", "Rejected"},
			"Unresolved": {"Unresolved", "Resolved", "Rejected"},
			"Resolved": {"Resolved", "Rejected"},
			"Rejected": {"Rejected"},
		}
		if proposed_scope_status not in allowed_scope_transitions.get(current_scope_status, set()):
			raise frappe.PermissionError("Integration message scope resolution cannot be reopened or rebound")


def _validate_message_event_binding(message, event_name: str) -> None:
	fields = [
		"payload_reference",
		"source_system",
		"endpoint",
		"source_namespace",
		"hospital",
		"scope_policy_hash",
		"idempotency_key",
		*MESSAGE_MAPPING_LINEAGE_FIELDS,
	]
	event = frappe.db.get_value(
		"IONE Clinical Quality Event",
		event_name,
		fields,
		as_dict=True,
	)
	if not event:
		raise frappe.PermissionError("Integration message event does not exist")
	expected = {
		"payload_reference": message.name,
		**{fieldname: message.get(fieldname) for fieldname in fields if fieldname != "payload_reference"},
	}
	if any(str(event.get(fieldname) or "") != str(value or "") for fieldname, value in expected.items()):
		raise frappe.PermissionError("Integration message event does not match its frozen receipt")


def _validate_message_scope_fill(message, values: dict[str, Any]) -> None:
	lower_fields = ("campus", "department", "ward")
	if not any(
		fieldname in values and not message.get(fieldname) and str(values.get(fieldname) or "")
		for fieldname in lower_fields
	):
		return
	endpoint = frappe.get_cached_doc("IONE Integration Endpoint", message.endpoint)
	source = frappe.get_cached_doc("IONE Source System", message.source_system)
	if endpoint.get("source_system") != source.name or message.get(
		"scope_policy_hash"
	) != scope_policy_checksum(endpoint, source):
		raise EndpointScopeViolationError(
			"Integration message scope policy changed before hierarchy resolution"
		)
	candidate = {
		fieldname: values.get(fieldname, message.get(fieldname)) for fieldname in ("hospital", *lower_fields)
	}
	authorized = authorize_endpoint_scope(endpoint, source, candidate)
	for fieldname in ("hospital", *lower_fields):
		if str(authorized.get(fieldname) or "") != str(candidate.get(fieldname) or ""):
			raise EndpointScopeViolationError(
				"Integration message hierarchy fill is not the authorized canonical scope"
			)


def _validate_runtime_quarantine_manifest(value: Any, payload_hash: str) -> None:
	if not isinstance(value, str):
		raise frappe.PermissionError("Integration payload replacement must be canonical JSON")
	try:
		manifest = json.loads(value)
	except ValueError as exc:
		raise frappe.PermissionError("Integration payload replacement must be canonical JSON") from exc
	if not isinstance(manifest, dict) or set(manifest) != {"_ione_qms_quarantine"}:
		raise frappe.PermissionError("Integration payload replacement must be a quarantine manifest")
	details = manifest["_ione_qms_quarantine"]
	if not isinstance(details, dict):
		raise frappe.PermissionError("Integration quarantine manifest is invalid")
	reason = details.get("reason")
	required_keys = {
		"EndpointScopeViolation": {"reason", "payload_hash", "payload_retained"},
		"TenantScopeUnresolved": {"reason", "payload_hash", "payload_retained"},
		"SchemaValidationFailed": {
			"reason",
			"payload_hash",
			"payload_retained",
			"scope_resolved",
		},
		"PayloadExceedsLimit": {
			"reason",
			"payload_hash",
			"payload_bytes",
			"maximum_bytes",
			"payload_retained",
		},
	}.get(reason)
	if (
		required_keys is None
		or set(details) != required_keys
		or details.get("payload_retained") is not False
		or not payload_hash
		or not hmac.compare_digest(str(details.get("payload_hash") or ""), payload_hash)
		or not hmac.compare_digest(value, _canonical_json(manifest))
	):
		raise frappe.PermissionError("Integration quarantine manifest is not canonical or hash-bound")
	if reason == "SchemaValidationFailed" and type(details.get("scope_resolved")) is not bool:
		raise frappe.PermissionError("Integration schema quarantine scope flag is invalid")
	if reason == "PayloadExceedsLimit" and (
		type(details.get("payload_bytes")) is not int
		or details["payload_bytes"] <= MAX_PAYLOAD_BYTES
		or type(details.get("maximum_bytes")) is not int
		or details["maximum_bytes"] != MAX_PAYLOAD_BYTES
	):
		raise frappe.PermissionError("Integration oversized-payload quarantine bounds are invalid")


def _message_outcome(
	message,
	*,
	duplicate: bool,
	retryable: bool | None = None,
	deferred: bool = False,
) -> dict[str, Any]:
	status = str(message.status)
	return {
		"accepted": status in {"Received", "Processed", "Error", "Dead Letter"},
		"processed": status == "Processed",
		"quarantined": status == "Dead Letter",
		"duplicate": duplicate,
		"message": message.name,
		"event": message.get("event"),
		"status": status,
		"retryable": (status == "Error") if retryable is None else retryable,
		"deferred": deferred,
		"next_retry_at": message.get("next_retry_at"),
		"scope_rejected": str(message.get("error_message") or "").startswith("EndpointScopeViolationError:"),
	}


def _insert_message(message, idempotency_key: str) -> None:
	savepoint = f"ione_msg_insert_{hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]}"
	frappe.db.savepoint(savepoint)
	try:
		message.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		frappe.db.rollback(save_point=savepoint)
		raise
	else:
		frappe.db.release_savepoint(savepoint)


def _message_by_idempotency_key(
	idempotency_key: str,
	*,
	current_read: bool = False,
):
	fields = [
		"name",
		"event",
		"status",
		"payload_hash",
		"attempts",
		"next_retry_at",
		"source_system",
		"endpoint",
		"source_namespace",
		"hospital",
		"scope_policy_hash",
	]
	if not current_read:
		return frappe.db.get_value(
			"IONE Integration Message",
			{"idempotency_key": idempotency_key},
			fields,
			as_dict=True,
		)
	rows = frappe.db.sql(
		"""
		select name, event, status, payload_hash, attempts, next_retry_at,
		source_system, endpoint, source_namespace, hospital, scope_policy_hash
		from `tabIONE Integration Message`
		where idempotency_key = %s
		limit 1
		for update
		""",
		(idempotency_key,),
		as_dict=True,
	)
	return rows[0] if rows else None


def _oversized_payload_manifest(*, payload_hash: str, payload_bytes: int) -> str:
	return _canonical_json(
		{
			"_ione_qms_quarantine": {
				"reason": "PayloadExceedsLimit",
				"payload_hash": payload_hash,
				"payload_bytes": payload_bytes,
				"maximum_bytes": MAX_PAYLOAD_BYTES,
				"payload_retained": False,
			}
		}
	)


def _scope_violation_manifest(*, payload_hash: str) -> str:
	return _canonical_json(
		{
			"_ione_qms_quarantine": {
				"reason": "EndpointScopeViolation",
				"payload_hash": payload_hash,
				"payload_retained": False,
			}
		}
	)


def _unresolved_scope_manifest(*, payload_hash: str) -> str:
	return _canonical_json(
		{
			"_ione_qms_quarantine": {
				"reason": "TenantScopeUnresolved",
				"payload_hash": payload_hash,
				"payload_retained": False,
			}
		}
	)


def _invalid_schema_manifest(*, payload_hash: str, scope_resolved: bool) -> str:
	return _canonical_json(
		{
			"_ione_qms_quarantine": {
				"reason": "SchemaValidationFailed",
				"payload_hash": payload_hash,
				"payload_retained": False,
				"scope_resolved": scope_resolved,
			}
		}
	)


def _mapping_scope_values(values: dict[str, Any]) -> dict[str, Any]:
	return {fieldname: value for fieldname, value in values.items() if fieldname in MAPPED_SCOPE_FIELDS}


def _indicator_dimension_values(*sources: dict[str, Any]) -> dict[str, str]:
	"""Canonicalize bounded governed dimensions from frozen mappings/indexes."""
	result: dict[str, str] = {}
	for source in sources:
		for fieldname in INDICATOR_SOURCE_DIMENSION_FIELDS:
			raw = source.get(fieldname)
			if raw in (None, ""):
				continue
			if isinstance(raw, (dict, list, tuple, set, bool)):
				raise frappe.ValidationError(f"Mapped indicator dimension {fieldname} must be a scalar code")
			value = str(raw).strip()
			if not value or len(value) > 140 or any(ord(character) < 32 for character in value):
				raise frappe.ValidationError(
					f"Mapped indicator dimension {fieldname} must be a bounded canonical code"
				)
			if result.get(fieldname) not in (None, value):
				raise frappe.ValidationError(f"Conflicting {fieldname} mappings in the clinical event")
			result[fieldname] = value
	return result


def _preauthorize_raw_scope(
	endpoint,
	source,
	payload: Any,
	mapping: dict[str, Any],
) -> dict[str, str] | None:
	if not isinstance(payload, dict):
		return None
	try:
		mapped = validate_resolved_scope(_mapping_scope_values(apply_mapping(payload, mapping)))
		department = resolve_department_reference(str(payload.get("department_reference") or "") or None)
		explicit = merge_scopes(mapped, department)
		if not any(
			explicit.get(fieldname)
			for fieldname in ("hospital", "campus", "department", "ward", "patient_index", "encounter_index")
		):
			return None
		return authorize_endpoint_scope(endpoint, source, explicit)
	except EndpointScopeViolationError, TypeError, ValueError, frappe.ValidationError:
		return None


def _preauthorize_scope(
	endpoint,
	source,
	incoming: IncomingClinicalEvent,
	payload: dict[str, Any],
	mapping: dict[str, Any],
) -> dict[str, str]:
	mapped = validate_resolved_scope(_mapping_scope_values(apply_mapping(payload, mapping)))
	department = resolve_department_reference(incoming.department_reference)
	return authorize_endpoint_scope(endpoint, source, merge_scopes(mapped, department))


def _quarantine_scope_violation(
	*,
	endpoint,
	source,
	incoming: IncomingClinicalEvent,
	payload_hash: str,
	mapping_definition: MappingDefinition,
) -> dict[str, Any]:
	policy_hash = scope_policy_checksum(endpoint, source)
	namespace = str(source.get("identity_namespace") or "")
	key = hashlib.sha256(f"{namespace}|{endpoint.name}|scope-violation|{payload_hash}".encode()).hexdigest()
	existing = _message_by_idempotency_key(key)
	if existing:
		return _message_outcome(
			frappe.get_doc("IONE Integration Message", existing.name),
			duplicate=True,
			retryable=False,
		)
	message = frappe.get_doc(
		{
			"doctype": "IONE Integration Message",
			"source_system": source.name,
			"endpoint": endpoint.name,
			"idempotency_key": key,
			"source_record_type": incoming.source_record_type,
			"source_record_id": hashlib.sha256(incoming.source_record_id.encode()).hexdigest(),
			"source_version": incoming.source_version,
			"event_type": incoming.event_type,
			"event_time": incoming.event_time,
			"source_namespace": namespace,
			"scope_policy_hash": policy_hash,
			"scope_resolution_status": "Rejected",
			"payload_hash": payload_hash,
			"payload_json": _scope_violation_manifest(payload_hash=payload_hash),
			"received_at": now_datetime(),
			"status": "Dead Letter",
			"attempts": 1,
			"error_message": "EndpointScopeViolationError: payload was outside the allowed scope",
			**mapping_definition.lineage_values(),
		}
	)
	try:
		_insert_message(message, key)
	except frappe.DuplicateEntryError:
		existing = _message_by_idempotency_key(key, current_read=True)
		if not existing:
			raise
		message = frappe.get_doc("IONE Integration Message", existing.name)
	issue_failure_type: str | None = None
	try:
		_record_processing_issue(message, "EndpointScopeViolationError")
	except Exception as exc:
		issue_failure_type = type(exc).__name__
	if issue_failure_type:
		frappe.log_error(
			title="IONE QMS could not persist scope-violation issue",
			message=f"{issue_failure_type}: sanitized scope-violation issue persistence failed.",
			reference_doctype="IONE Integration Message",
			reference_name=message.name,
		)
	return _message_outcome(message, duplicate=False, retryable=False)


def _quarantine_invalid_payload(
	*,
	endpoint,
	source,
	payload: Any,
	payload_json: str,
	payload_hash: str,
	failure_type: str,
	scope: dict[str, str] | None,
	scope_rejected: bool,
	sanitize_identity: bool,
	mapping_definition: MappingDefinition,
) -> dict[str, Any]:
	identity = _quarantine_identity(payload, payload_hash)
	if sanitize_identity:
		identity["source_record_id"] = hashlib.sha256(str(identity["source_record_id"]).encode()).hexdigest()
	if identity["canonical"]:
		idempotency_key = hashlib.sha256(
			"|".join(
				(
					str(source.get("identity_namespace") or source.name),
					endpoint.name,
					str((scope or {}).get("hospital") or "unresolved"),
					identity["source_record_type"],
					identity["source_record_id"],
					identity["source_version"],
				)
			).encode()
		).hexdigest()
	else:
		idempotency_key = hashlib.sha256(
			(
				f"{source.get('identity_namespace') or source.name}|{endpoint.name}|"
				f"{(scope or {}).get('hospital') or 'unresolved'}|invalid-envelope|{payload_hash}"
			).encode()
		).hexdigest()
	existing = _message_by_idempotency_key(idempotency_key)
	if existing:
		if not hmac.compare_digest(str(existing.payload_hash or ""), payload_hash):
			return _persist_conflict_quarantine(
				endpoint=endpoint,
				source=source,
				source_record_type=identity["source_record_type"],
				source_record_id=identity["source_record_id"],
				source_version=identity["source_version"],
				event_type=identity["event_type"],
				event_time=identity["event_time"],
				payload_json=payload_json,
				payload_hash=payload_hash,
				canonical_idempotency_key=idempotency_key,
				scope=scope,
				mapping_definition=mapping_definition,
			)
		message = frappe.get_doc("IONE Integration Message", existing.name)
		if message.status not in {"Processed", "Dead Letter"}:
			_update_message(
				message,
				{
					"status": "Dead Letter",
					"next_retry_at": None,
					"error_message": f"{failure_type}: schema validation failed",
				},
			)
			message.reload()
		return _message_outcome(message, duplicate=True, retryable=False)

	message = frappe.get_doc(
		{
			"doctype": "IONE Integration Message",
			"source_system": source.name,
			"endpoint": endpoint.name,
			"idempotency_key": idempotency_key,
			"source_record_type": identity["source_record_type"],
			"source_record_id": identity["source_record_id"],
			"source_version": identity["source_version"],
			"event_type": identity["event_type"],
			"event_time": identity["event_time"],
			"source_namespace": str(source.get("identity_namespace") or ""),
			**{
				fieldname: (scope or {}).get(fieldname)
				for fieldname in ("hospital", "campus", "department", "ward")
			},
			"scope_policy_hash": scope_policy_checksum(endpoint, source),
			"scope_resolution_status": (
				"Rejected" if scope_rejected else ("Resolved" if scope else "Unresolved")
			),
			"payload_hash": payload_hash,
			"payload_json": payload_json,
			"received_at": now_datetime(),
			"status": "Dead Letter",
			"attempts": 1,
			"error_message": f"{failure_type}: schema validation failed",
			**mapping_definition.lineage_values(),
		}
	)
	try:
		_insert_message(message, idempotency_key)
	except frappe.DuplicateEntryError:
		existing = _message_by_idempotency_key(idempotency_key, current_read=True)
		if not existing:
			frappe.throw(
				"Concurrent quarantine message could not be resolved",
				frappe.DuplicateEntryError,
			)
		if not hmac.compare_digest(str(existing.payload_hash or ""), payload_hash):
			return _persist_conflict_quarantine(
				endpoint=endpoint,
				source=source,
				source_record_type=identity["source_record_type"],
				source_record_id=identity["source_record_id"],
				source_version=identity["source_version"],
				event_type=identity["event_type"],
				event_time=identity["event_time"],
				payload_json=payload_json,
				payload_hash=payload_hash,
				canonical_idempotency_key=idempotency_key,
				scope=scope,
				mapping_definition=mapping_definition,
			)
		message = frappe.get_doc(
			"IONE Integration Message",
			existing.name,
			for_update=True,
		)
		if message.status not in {"Processed", "Dead Letter"}:
			_update_message(
				message,
				{
					"status": "Dead Letter",
					"next_retry_at": None,
					"error_message": f"{failure_type}: schema validation failed",
				},
			)
			message.reload()
		return _message_outcome(message, duplicate=True, retryable=False)
	issue_failure_type = None
	try:
		_record_processing_issue(message, failure_type)
	except Exception as exc:
		issue_failure_type = type(exc).__name__
	if issue_failure_type:
		frappe.log_error(
			title="IONE QMS could not persist quarantined-record issue",
			message=(
				f"{issue_failure_type}: quarantine issue persistence failed; clinical payload was not logged."
			),
			reference_doctype="IONE Integration Message",
			reference_name=message.name,
		)
	return _message_outcome(message, duplicate=False, retryable=False)


def _quarantine_source_version_conflict(
	*,
	endpoint,
	source,
	incoming: IncomingClinicalEvent,
	payload_json: str,
	payload_hash: str,
	canonical_idempotency_key: str,
	scope: dict[str, str],
	mapping_definition: MappingDefinition,
) -> dict[str, Any]:
	return _persist_conflict_quarantine(
		endpoint=endpoint,
		source=source,
		source_record_type=incoming.source_record_type,
		source_record_id=incoming.source_record_id,
		source_version=incoming.source_version,
		event_type=incoming.event_type,
		event_time=incoming.event_time,
		payload_json=payload_json,
		payload_hash=payload_hash,
		canonical_idempotency_key=canonical_idempotency_key,
		scope=scope,
		mapping_definition=mapping_definition,
	)


def _persist_conflict_quarantine(
	*,
	endpoint,
	source,
	source_record_type: str,
	source_record_id: str,
	source_version: str,
	event_type: str,
	event_time,
	payload_json: str,
	payload_hash: str,
	canonical_idempotency_key: str,
	scope: dict[str, str] | None,
	mapping_definition: MappingDefinition,
) -> dict[str, Any]:
	conflict_key = hashlib.sha256(
		f"{canonical_idempotency_key}|version-conflict|{payload_hash}".encode()
	).hexdigest()
	existing = _message_by_idempotency_key(conflict_key)
	if existing:
		if not hmac.compare_digest(str(existing.payload_hash or ""), payload_hash):
			frappe.throw("Conflict quarantine identity collision", frappe.DuplicateEntryError)
		_mark_http_conflict()
		return _message_outcome(
			frappe.get_doc("IONE Integration Message", existing.name),
			duplicate=True,
			retryable=False,
		)
	message = frappe.get_doc(
		{
			"doctype": "IONE Integration Message",
			"source_system": source.name,
			"endpoint": endpoint.name,
			"idempotency_key": conflict_key,
			"source_record_type": source_record_type,
			"source_record_id": source_record_id,
			"source_version": source_version,
			"event_type": event_type,
			"event_time": event_time,
			"source_namespace": str(source.get("identity_namespace") or ""),
			**{
				fieldname: (scope or {}).get(fieldname)
				for fieldname in ("hospital", "campus", "department", "ward")
			},
			"scope_policy_hash": scope_policy_checksum(endpoint, source),
			"scope_resolution_status": "Resolved" if scope else "Unresolved",
			"payload_hash": payload_hash,
			"payload_json": payload_json,
			"received_at": now_datetime(),
			"status": "Dead Letter",
			"attempts": 1,
			"error_message": "SourceVersionConflictError: source version reused with different data",
			**mapping_definition.lineage_values(),
		}
	)
	try:
		_insert_message(message, conflict_key)
	except frappe.DuplicateEntryError:
		existing = _message_by_idempotency_key(conflict_key, current_read=True)
		if not existing or not hmac.compare_digest(str(existing.payload_hash or ""), payload_hash):
			frappe.throw("Concurrent conflict quarantine collision", frappe.DuplicateEntryError)
		message = frappe.get_doc("IONE Integration Message", existing.name)
	issue_failure_type = None
	try:
		_record_processing_issue(message, "SourceVersionConflictError")
	except Exception as exc:
		issue_failure_type = type(exc).__name__
	if issue_failure_type:
		frappe.log_error(
			title="IONE QMS could not persist source-version conflict issue",
			message=(
				f"{issue_failure_type}: conflict issue persistence failed; clinical payload was not logged."
			),
			reference_doctype="IONE Integration Message",
			reference_name=message.name,
		)
	_mark_http_conflict()
	return _message_outcome(message, duplicate=False, retryable=False)


def _mark_http_conflict() -> None:
	if getattr(frappe.local, "request", None) is not None:
		frappe.local.response["http_status_code"] = 409


def _quarantine_identity(payload: Any, payload_hash: str) -> dict[str, Any]:
	values = payload if isinstance(payload, dict) else {}
	record_type = _safe_identifier(
		values.get("source_record_type"),
		IDENTIFIER_PATTERN,
		"InvalidEnvelope",
	)
	record_id = _safe_identifier(
		values.get("source_record_id"),
		IDENTIFIER_PATTERN,
		f"invalid-{payload_hash[:24]}",
	)
	version = _safe_identifier(
		values.get("source_version") or "0",
		IDENTIFIER_PATTERN,
		"invalid",
	)
	event_type = _safe_identifier(
		values.get("event_type"),
		EVENT_TYPE_PATTERN,
		"InvalidEnvelope",
	)
	try:
		event_time = get_datetime(values.get("event_time")) if values.get("event_time") else None
	except Exception:
		event_time = None
	return {
		"source_record_type": record_type,
		"source_record_id": record_id,
		"source_version": version,
		"event_type": event_type,
		"event_time": event_time or now_datetime(),
		"canonical": bool(
			IDENTIFIER_PATTERN.fullmatch(str(values.get("source_record_type") or "").strip())
			and IDENTIFIER_PATTERN.fullmatch(str(values.get("source_record_id") or "").strip())
			and IDENTIFIER_PATTERN.fullmatch(str(values.get("source_version") or "0").strip())
		),
	}


def _safe_identifier(
	value: Any,
	pattern,
	fallback: str,
) -> str:
	normalized = str(value or "").strip()
	return normalized if pattern.fullmatch(normalized) else fallback


def _retained_connector_marker(endpoint, payload: Any, failure_type: str) -> str | None:
	if failure_type != "InvalidConnectorRecord":
		return None
	marker = decode_connector_quarantine_record(payload)
	if not marker:
		return None
	connector_key = str(endpoint.get("connector_key") or "").strip().lower()
	if marker.get("connector") != connector_key:
		return None
	return str(payload)


def _assert_source_runtime(source) -> str:
	if not int(source.get("enabled") or 0):
		frappe.throw("Source system is disabled")
	if not int(source.get("read_only") if source.get("read_only") is not None else 1):
		frappe.throw("Clinical source systems must be configured as read-only")
	namespace = str(source.get("identity_namespace") or "")
	if not namespace or namespace != namespace.strip():
		frappe.throw("Source system has no governed identity namespace")
	return namespace


def _hashed_identity(value: Any) -> str:
	normalized = str(value or "")
	if len(normalized) == 64 and all(character in "0123456789abcdef" for character in normalized):
		return normalized
	return hashlib.sha256(normalized.encode()).hexdigest()


def _idempotency_key(
	source_namespace: str,
	tenant_hospital: str,
	incoming: IncomingClinicalEvent,
) -> str:
	raw = "|".join(
		(
			source_namespace,
			tenant_hospital,
			incoming.source_record_type,
			incoming.source_record_id,
			incoming.source_version,
		)
	)
	return hashlib.sha256(raw.encode()).hexdigest()


def _canonical_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)
