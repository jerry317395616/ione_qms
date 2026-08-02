from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable, Mapping
from typing import Any

import frappe

from ione_qms.services.identity_keys import (
	stable_source_identity_key,
	verify_stored_identity_key,
)

SCOPE_FIELDS = ("hospital", "campus", "department", "ward")
STAFF_FIELDS = ("responsible_staff", "medical_staff")
INDEX_FIELDS = ("patient_index", "encounter_index")
INTEGRATION_AUDIT_IDENTITY_FIELDS = {
	"IONE Integration Message": (
		"source_system",
		"source_namespace",
		"endpoint",
		"event",
		"hospital",
		"campus",
		"department",
		"ward",
		"scope_policy_hash",
		"idempotency_key",
		"source_record_type",
		"source_record_id",
		"source_version",
		"payload_hash",
	),
	"IONE Clinical Quality Event": (
		"source_system",
		"source_namespace",
		"endpoint",
		"payload_reference",
		"hospital",
		"campus",
		"department",
		"ward",
		"scope_policy_hash",
		"idempotency_key",
		"source_record_type",
		"source_record_id",
		"source_version",
		"patient_index",
		"encounter_index",
		"responsible_staff",
		"medical_staff",
		"medical_group",
		"disease",
		"surgery",
		"drg",
		"dip",
	),
}


class EndpointScopeViolationError(frappe.PermissionError):
	"""A signed endpoint attempted to address data outside its approved tenant scope."""


def canonicalize_scope(scope: Mapping[str, Any] | None) -> dict[str, str]:
	"""Return one hierarchy-consistent scope, deriving trusted parents from Link records."""
	values = {
		fieldname: str((scope or {}).get(fieldname) or "").strip()
		for fieldname in (
			*SCOPE_FIELDS,
			*STAFF_FIELDS,
			*INDEX_FIELDS,
			"source_system",
			"source_namespace",
		)
	}
	encounter_index = values["encounter_index"]
	if encounter_index:
		encounter = frappe.db.get_value(
			"IONE Encounter Index",
			encounter_index,
			[
				"patient",
				"hospital",
				"campus",
				"department",
				"ward",
				"responsible_staff",
				"medical_staff",
				"source_system",
				"source_namespace",
			],
			as_dict=True,
		)
		if not encounter:
			raise EndpointScopeViolationError("Integration scope references an unknown encounter index")
		for fieldname in (
			"hospital",
			"campus",
			"department",
			"ward",
			"responsible_staff",
			"medical_staff",
			"source_system",
			"source_namespace",
		):
			_merge_parent(values, fieldname, encounter.get(fieldname), "encounter_index")
		_merge_parent(values, "patient_index", encounter.get("patient"), "encounter_index")
	patient_index = values["patient_index"]
	if patient_index:
		patient = frappe.db.get_value(
			"IONE Patient Index",
			patient_index,
			["tenant_hospital", "source_system", "source_namespace"],
			as_dict=True,
		)
		if not patient:
			raise EndpointScopeViolationError("Integration scope references an unknown patient index")
		_merge_parent(values, "hospital", patient.get("tenant_hospital"), "patient_index")
		_merge_parent(values, "source_system", patient.get("source_system"), "patient_index")
		_merge_parent(values, "source_namespace", patient.get("source_namespace"), "patient_index")
	for fieldname, doctype, parents, status_field in (
		("campus", "IONE Hospital Campus", ("hospital",), "status"),
		("department", "IONE Medical Department", ("hospital", "campus"), "status"),
		("ward", "IONE Ward", ("hospital", "campus", "department"), "status"),
		("responsible_staff", "IONE Medical Staff", SCOPE_FIELDS, "practice_status"),
		("medical_staff", "IONE Medical Staff", SCOPE_FIELDS, "practice_status"),
	):
		name = values[fieldname]
		if not name:
			continue
		row = frappe.db.get_value(doctype, name, [*parents, status_field], as_dict=True)
		if not row:
			raise EndpointScopeViolationError(f"Integration scope references an unknown {doctype}")
		_assert_active(doctype, row.get(status_field))
		for parent in parents:
			_merge_parent(values, parent, row.get(parent), fieldname)
	hospital = values["hospital"]
	if hospital:
		row = frappe.db.get_value("IONE Hospital", hospital, ["status"], as_dict=True)
		if not row:
			raise EndpointScopeViolationError("Integration scope references an unknown hospital")
		_assert_active("IONE Hospital", row.get("status"))
	return {fieldname: value for fieldname, value in values.items() if value}


def validate_allowed_scopes(doc, *, require_nonempty: bool | None = None) -> tuple[tuple[str, ...], ...]:
	rows = tuple(sorted(_normalized_scope_rows(doc.get("allowed_scopes") or ())))
	if require_nonempty is None:
		require_nonempty = bool(int(doc.get("enabled") or 0))
	if require_nonempty and not rows:
		frappe.throw("Enabled integration configuration requires at least one allowed scope")
	if len(rows) != len(set(rows)):
		frappe.throw("Integration allowed scopes cannot contain duplicate hierarchy rows")
	return rows


def validate_endpoint_scope_policy(endpoint, source) -> None:
	endpoint_rows = validate_allowed_scopes(endpoint)
	source_rows = validate_allowed_scopes(source, require_nonempty=bool(int(source.get("enabled") or 0)))
	if int(endpoint.get("enabled") or 0):
		if not int(source.get("enabled") or 0):
			frappe.throw("An enabled integration endpoint requires an enabled source system")
		if not _governed_source_namespace(source):
			frappe.throw("An enabled integration endpoint requires a source identity namespace")
	for endpoint_row in endpoint_rows:
		if not any(_row_covers(source_row, endpoint_row) for source_row in source_rows):
			frappe.throw("Integration endpoint allowed scope exceeds its source-system authority")


def authorize_endpoint_scope(
	endpoint,
	source,
	scope: Mapping[str, Any] | None,
	*,
	require_hospital: bool = True,
) -> dict[str, str]:
	if not int(source.get("enabled") or 0):
		raise EndpointScopeViolationError("Clinical integration source is disabled")
	source_namespace = _governed_source_namespace(source)
	validate_endpoint_scope_policy(endpoint, source)
	endpoint_rows = validate_allowed_scopes(endpoint, require_nonempty=True)
	candidate = canonicalize_scope(scope)
	if candidate.get("source_system") not in {"", str(source.name)}:
		raise EndpointScopeViolationError("Clinical index belongs to another source system")
	if candidate.get("source_namespace") not in {"", source_namespace}:
		raise EndpointScopeViolationError("Clinical index belongs to another source namespace")
	if not candidate.get("hospital"):
		hospitals = {row[0] for row in endpoint_rows}
		if len(hospitals) != 1:
			raise EndpointScopeViolationError(
				"Multi-hospital integration endpoints require an explicit event hospital"
			)
		candidate["hospital"] = next(iter(hospitals))
		candidate = canonicalize_scope(candidate)
	if require_hospital and not candidate.get("hospital"):
		raise EndpointScopeViolationError("Clinical integration events require an exact hospital")
	candidate_row = tuple(candidate.get(fieldname, "") for fieldname in SCOPE_FIELDS)
	if not any(_row_covers(row, candidate_row) for row in endpoint_rows):
		raise EndpointScopeViolationError("Clinical event is outside the endpoint allowed scope")
	return candidate


def authorize_source_scope(source, scope: Mapping[str, Any] | None) -> dict[str, str]:
	if not int(source.get("enabled") or 0):
		raise EndpointScopeViolationError("Internal clinical event source is disabled")
	source_namespace = _governed_source_namespace(source)
	rows = validate_allowed_scopes(source, require_nonempty=True)
	candidate = canonicalize_scope(scope)
	if candidate.get("source_system") not in {"", str(source.name)}:
		raise EndpointScopeViolationError("Clinical index belongs to another source system")
	if candidate.get("source_namespace") not in {"", source_namespace}:
		raise EndpointScopeViolationError("Clinical index belongs to another source namespace")
	if not candidate.get("hospital"):
		raise EndpointScopeViolationError("Internal clinical events require an exact hospital")
	candidate_row = tuple(candidate.get(fieldname, "") for fieldname in SCOPE_FIELDS)
	if not any(_row_covers(row, candidate_row) for row in rows):
		raise EndpointScopeViolationError("Internal clinical event is outside its source allowed scope")
	return candidate


def validate_mapping_constant_scope(endpoint, source, scope: Mapping[str, Any]) -> None:
	validate_endpoint_scope_policy(endpoint, source)
	candidate = canonicalize_scope(scope)
	endpoint_rows = validate_allowed_scopes(
		endpoint, require_nonempty=bool(int(endpoint.get("enabled") or 0))
	)
	if not endpoint_rows:
		return
	candidate_row = tuple(candidate.get(fieldname, "") for fieldname in SCOPE_FIELDS)
	if not any(
		all(
			not actual or not allowed or actual == allowed
			for allowed, actual in zip(row, candidate_row, strict=True)
		)
		for row in endpoint_rows
	):
		raise EndpointScopeViolationError("Mapping constants exceed the endpoint allowed scope")


def scope_policy_checksum(endpoint, source) -> str:
	payload = {
		"endpoint": str(endpoint.name),
		"source_system": str(source.name),
		"identity_namespace": _governed_source_namespace(source),
		"source_scopes": validate_allowed_scopes(source, require_nonempty=True),
		"endpoint_scopes": validate_allowed_scopes(endpoint, require_nonempty=True),
	}
	return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_scope_policy_checksum(source) -> str:
	payload = {
		"source_system": str(source.name),
		"identity_namespace": _governed_source_namespace(source),
		"source_scopes": validate_allowed_scopes(source, require_nonempty=True),
	}
	return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def resolve_department_reference(reference: str | None) -> dict[str, str]:
	if not reference:
		return {}
	names = frappe.get_all(
		"IONE Medical Department",
		filters={"department_code": str(reference)},
		pluck="name",
		limit_page_length=2,
	)
	if not names:
		return {}
	if len(names) != 1:
		raise EndpointScopeViolationError("Department reference is ambiguous")
	return canonicalize_scope({"department": names[0]})


def validate_event_tenant_binding(doc, method: str | None = None) -> None:
	"""Recheck Message -> Endpoint -> Source -> tenant immediately before Event insertion."""
	del method
	source = frappe.get_cached_doc("IONE Source System", doc.get("source_system"))
	if not doc.get("payload_reference") and (
		source.get("system_type") == "Internal"
		or bool(getattr(doc.flags, "ione_internal_clinical_event", False))
	):
		scope = authorize_source_scope(source, _document_scope(doc))
		if doc.get("scope_policy_hash") != source_scope_policy_checksum(source):
			raise EndpointScopeViolationError("Internal clinical event source scope policy changed")
		_assert_document_scope(doc, scope)
		_assert_index_tenant(
			doc.get("patient_index"),
			doc.get("encounter_index"),
			scope["hospital"],
			source.name,
			str(source.get("identity_namespace") or ""),
		)
		return
	if not doc.get("payload_reference"):
		raise EndpointScopeViolationError("External clinical events require a governed integration message")
	message = frappe.get_doc("IONE Integration Message", doc.payload_reference)
	if message.get("source_system") != source.name:
		raise EndpointScopeViolationError("Integration event source does not match its message")
	if doc.get("source_namespace") != source.get("identity_namespace") or message.get(
		"source_namespace"
	) != source.get("identity_namespace"):
		raise EndpointScopeViolationError("Integration event source namespace is inconsistent")
	endpoint = frappe.get_cached_doc("IONE Integration Endpoint", message.endpoint)
	if endpoint.get("source_system") != source.name:
		raise EndpointScopeViolationError("Integration message endpoint has a different source")
	if doc.get("endpoint") != endpoint.name:
		raise EndpointScopeViolationError("Integration event endpoint does not match its message")
	scope = authorize_endpoint_scope(endpoint, source, _document_scope(doc))
	current_hash = scope_policy_checksum(endpoint, source)
	if message.get("scope_policy_hash") != current_hash or doc.get("scope_policy_hash") != current_hash:
		raise EndpointScopeViolationError("Integration scope policy changed before event insertion")
	_assert_document_scope(doc, scope)
	_assert_document_scope(message, scope)
	_assert_index_tenant(
		doc.get("patient_index"),
		doc.get("encounter_index"),
		scope["hospital"],
		source.name,
		str(source.get("identity_namespace") or ""),
	)


def validate_integration_audit_identity(doc, method: str | None = None) -> None:
	"""Lock the database row and reject generic saves that rebind integration lineage."""
	del method
	fields = INTEGRATION_AUDIT_IDENTITY_FIELDS.get(doc.doctype)
	if not fields or doc.is_new():
		return
	previous = frappe.get_doc(doc.doctype, doc.name, for_update=True)
	changed = [
		fieldname
		for fieldname in fields
		if str(previous.get(fieldname) or "") != str(doc.get(fieldname) or "")
	]
	if changed:
		frappe.throw(
			"Integration audit identity is immutable after insertion: " + ", ".join(changed),
			frappe.PermissionError,
		)


def validate_index_tenant_identity(doc, method: str | None = None) -> None:
	del method
	if doc.doctype == "IONE Patient Index":
		if not doc.get("tenant_hospital") or not doc.get("source_namespace"):
			frappe.throw("Patient indexes require an immutable tenant hospital and source namespace")
		hospital = doc.get("tenant_hospital")
		source_id = doc.get("source_patient_id")
		key_field = "patient_key"
		key_kind = "patient"
	elif doc.doctype == "IONE Encounter Index":
		if not doc.get("hospital") or not doc.get("source_namespace"):
			frappe.throw("Encounter indexes require an immutable hospital and source namespace")
		hospital = doc.get("hospital")
		source_id = doc.get("source_encounter_id")
		key_field = "encounter_key"
		key_kind = "encounter"
	else:
		return
	source = frappe.db.get_value(
		"IONE Source System",
		doc.get("source_system"),
		["identity_namespace"],
		as_dict=True,
	)
	if not source or source.get("identity_namespace") != doc.get("source_namespace"):
		raise EndpointScopeViolationError("Clinical index namespace does not match its source system")
	if not verify_stored_identity_key(
		key_kind,
		doc.get(key_field),
		doc.get("identity_key_contract"),
		doc.source_namespace,
		hospital,
		source_id,
	):
		raise EndpointScopeViolationError("Clinical index key does not match its tenant identity")
	expected_stable_key = stable_source_identity_key(
		key_kind,
		doc.get("source_system"),
		doc.get("source_namespace"),
		hospital,
		source_id,
	)
	if not hmac.compare_digest(
		str(doc.get("stable_source_key") or ""),
		expected_stable_key,
	):
		raise EndpointScopeViolationError(
			"Clinical index stable source key does not match its exact tenant identity"
		)
	if not doc.is_new():
		fields = [
			"source_system",
			"source_namespace",
			"identity_key_contract",
			"stable_source_key",
			key_field,
		]
		fields.append("tenant_hospital" if doc.doctype == "IONE Patient Index" else "hospital")
		if doc.doctype == "IONE Encounter Index":
			fields.append("patient")
		existing = frappe.db.get_value(doc.doctype, doc.name, fields, as_dict=True)
		if not existing or any(
			str(existing.get(field) or "") != str(doc.get(field) or "") for field in fields
		):
			raise EndpointScopeViolationError("Clinical index tenant identity is immutable")
	if doc.doctype != "IONE Encounter Index":
		return
	if doc.get("patient"):
		patient = frappe.db.get_value(
			"IONE Patient Index",
			doc.patient,
			["tenant_hospital", "source_namespace"],
			as_dict=True,
		)
		if not patient:
			frappe.throw("Encounter references an unknown patient index")
		if patient.tenant_hospital != doc.hospital or patient.source_namespace != doc.source_namespace:
			raise EndpointScopeViolationError("Encounter and patient tenant identities differ")


def _normalized_scope_rows(rows: Iterable[Any]) -> Iterable[tuple[str, ...]]:
	for row in rows:
		raw = {fieldname: str(_value(row, fieldname) or "").strip() for fieldname in SCOPE_FIELDS}
		if not raw["hospital"]:
			frappe.throw("Every integration allowed scope requires a hospital")
		canonical = canonicalize_scope(raw)
		for fieldname in SCOPE_FIELDS:
			if raw[fieldname] and canonical.get(fieldname) != raw[fieldname]:
				frappe.throw("Integration allowed scope hierarchy is inconsistent")
		yield tuple(canonical.get(fieldname, "") for fieldname in SCOPE_FIELDS)


def _row_covers(authority: tuple[str, ...], candidate: tuple[str, ...]) -> bool:
	return all(not allowed or allowed == actual for allowed, actual in zip(authority, candidate, strict=True))


def _merge_parent(values: dict[str, str], fieldname: str, linked_value: Any, child: str) -> None:
	value = str(linked_value or "").strip()
	if not value:
		return
	if values[fieldname] and values[fieldname] != value:
		raise EndpointScopeViolationError(f"Integration {fieldname} conflicts with its {child} hierarchy")
	values[fieldname] = value


def _assert_active(doctype: str, status: Any) -> None:
	if str(status or "") != "Active":
		raise EndpointScopeViolationError(f"Integration scope references an inactive {doctype}")


def _governed_source_namespace(source) -> str:
	raw = str(source.get("identity_namespace") or "")
	normalized = raw.strip()
	if not normalized or normalized != raw:
		raise EndpointScopeViolationError("Clinical integration source has no canonical identity namespace")
	return normalized


def _value(row: Any, fieldname: str) -> Any:
	if isinstance(row, Mapping):
		return row.get(fieldname)
	return getattr(row, fieldname, None)


def _document_scope(doc) -> dict[str, Any]:
	return {fieldname: doc.get(fieldname) for fieldname in (*SCOPE_FIELDS, *STAFF_FIELDS, *INDEX_FIELDS)}


def _assert_document_scope(doc, scope: Mapping[str, str]) -> None:
	for fieldname in SCOPE_FIELDS:
		if str(doc.get(fieldname) or "") != str(scope.get(fieldname) or ""):
			raise EndpointScopeViolationError(
				f"Integration event {fieldname} does not match its authorized scope"
			)


def _assert_index_tenant(
	patient: str | None,
	encounter: str | None,
	hospital: str,
	source_system: str,
	source_namespace: str,
) -> None:
	patient_row = None
	if patient:
		patient_row = frappe.db.get_value(
			"IONE Patient Index",
			patient,
			["tenant_hospital", "source_system", "source_namespace"],
			as_dict=True,
		)
		if (
			not patient_row
			or patient_row.get("tenant_hospital") != hospital
			or patient_row.get("source_system") != source_system
			or patient_row.get("source_namespace") != source_namespace
		):
			raise EndpointScopeViolationError("Integration patient belongs to another hospital")
	if encounter:
		encounter_row = frappe.db.get_value(
			"IONE Encounter Index",
			encounter,
			["hospital", "source_system", "source_namespace", "patient"],
			as_dict=True,
		)
		if (
			not encounter_row
			or encounter_row.get("hospital") != hospital
			or encounter_row.get("source_system") != source_system
			or encounter_row.get("source_namespace") != source_namespace
		):
			raise EndpointScopeViolationError("Integration encounter belongs to another hospital")
		if patient and encounter_row.get("patient") != patient:
			raise EndpointScopeViolationError("Integration encounter and patient references differ")
