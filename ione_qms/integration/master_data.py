from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import frappe
from frappe.utils import get_datetime, getdate, now_datetime

from ione_qms.integration.mapping import (
	ENCOUNTER_MASTER_DATA_FIELDS,
	PATIENT_MASTER_DATA_FIELDS,
	normalize_master_data_configuration,
)
from ione_qms.integration.schemas import IncomingClinicalEvent
from ione_qms.services.crypto_keys import active_hmac_key
from ione_qms.services.identity_keys import (
	identity_key_candidates,
	identity_key_contract,
	site_identity_key,
	stable_source_identity_key,
	verify_stored_identity_key,
)
from ione_qms.services.integration_scope import (
	authorize_endpoint_scope,
	canonicalize_scope,
)

SAFE_FIELD_PATH = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*$")
INTEGER_SOURCE_VERSION = re.compile(r"^(?:0|[1-9][0-9]*)$")
UTC_DATETIME_SOURCE_VERSION = re.compile(
	r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
	r"(?:\.[0-9]{1,6})?Z$"
)
SOURCE_VERSION_STRATEGIES = frozenset({"integer", "datetime"})
MASTER_DATA_LOCK_TIMEOUT_SECONDS = 15
INDICATOR_DIMENSION_FIELDS = ("medical_group", "disease", "surgery", "drg", "dip")

PATIENT_FIELDS = PATIENT_MASTER_DATA_FIELDS
ENCOUNTER_FIELDS = ENCOUNTER_MASTER_DATA_FIELDS
ENCOUNTER_TYPES = frozenset(
	{
		"Outpatient",
		"Emergency",
		"Inpatient",
		"Day Care",
		"Day Surgery",
		"Observation",
		"Other",
	}
)

LINK_CODE_FIELDS = {
	"hospital": ("IONE Hospital", "hospital_code"),
	"campus": ("IONE Hospital Campus", "campus_code"),
	"department": ("IONE Medical Department", "department_code"),
	"ward": ("IONE Ward", "ward_code"),
	"responsible_staff": ("IONE Medical Staff", "staff_code"),
	"medical_staff": ("IONE Medical Staff", "staff_code"),
}


class StaleSourceVersionError(frappe.ValidationError):
	pass


class SourceVersionConflictError(frappe.ValidationError):
	pass


def resolve_and_materialize_indexes(
	*,
	endpoint,
	source,
	incoming: IncomingClinicalEvent,
	payload_hash: str,
	tenant_hospital: str,
	source_namespace: str,
	master_data_config: dict[str, Any] | None,
	mapping_lineage: dict[str, str] | None = None,
) -> dict[str, str]:
	"""Resolve stable source references and optionally maintain minimal local indexes.

	Materialization is opt-in through the frozen governed Mapping snapshot. This keeps
	retries deterministic while allowing all other events to resolve previously
	synchronized patient/encounter rows.
	"""
	config = validate_master_data_configuration(master_data_config)
	if not tenant_hospital or not source_namespace:
		raise frappe.ValidationError(
			"Master-data resolution requires an authorized hospital and source namespace"
		)
	entity = str((config or {}).get("entity") or "").strip()
	if config and int(config.get("enabled") or 0):
		if entity not in {"Patient", "Encounter"}:
			raise frappe.ValidationError("master_data.entity must be Patient or Encounter")
		if str(incoming.source_record_type or "").casefold() != entity.casefold():
			raise SourceVersionConflictError("Master-data entity must match the incoming source record type")
	materializing_encounter = bool(config and int(config.get("enabled") or 0) and entity == "Encounter")
	resolved = _resolve_existing_references(
		source.name,
		incoming,
		tenant_hospital=tenant_hospital,
		source_namespace=source_namespace,
		include_encounter=not materializing_encounter,
	)
	if not config or not int(config.get("enabled") or 0):
		return resolved

	field_mapping = config.get("fields")
	if not isinstance(field_mapping, dict):
		raise frappe.ValidationError("master_data.fields must be a JSON object")
	version_strategy = _source_version_strategy(config)
	_validate_payload_hash(payload_hash)
	allowed = PATIENT_FIELDS if entity == "Patient" else ENCOUNTER_FIELDS
	values = _map_fields(incoming.payload, field_mapping, allowed)

	if entity == "Patient":
		patient = _upsert_patient(
			source.name,
			values,
			incoming,
			tenant_hospital=tenant_hospital,
			source_namespace=source_namespace,
			version_strategy=version_strategy,
			payload_hash=payload_hash,
		)
		_merge_reference(resolved, "patient_index", patient)
	else:
		frozen_lineage = _validated_indicator_mapping_lineage(source.name, mapping_lineage)
		encounter_scope = _upsert_encounter(
			endpoint,
			source,
			source.name,
			values,
			incoming,
			tenant_hospital=tenant_hospital,
			source_namespace=source_namespace,
			version_strategy=version_strategy,
			payload_hash=payload_hash,
			mapping_lineage=frozen_lineage,
		)
		for fieldname, value in encounter_scope.items():
			_merge_reference(resolved, fieldname, value)
	return resolved


def validate_resolved_scope(scope: dict[str, Any]) -> dict[str, str]:
	"""Reject untrusted Link values instead of letting them fail deep in event insertion."""
	targets = {
		"patient_index": "IONE Patient Index",
		"encounter_index": "IONE Encounter Index",
		"hospital": "IONE Hospital",
		"campus": "IONE Hospital Campus",
		"department": "IONE Medical Department",
		"ward": "IONE Ward",
		"responsible_staff": "IONE Medical Staff",
		"medical_staff": "IONE Medical Staff",
	}
	validated: dict[str, str] = {}
	for fieldname, value in scope.items():
		if value in (None, ""):
			continue
		if fieldname not in targets:
			raise frappe.ValidationError(f"Unsupported clinical event scope field: {fieldname}")
		name = str(value)
		if not frappe.db.exists(targets[fieldname], name):
			raise frappe.ValidationError(
				f"Mapped {fieldname} does not reference an existing {targets[fieldname]}"
			)
		validated[fieldname] = name
	return validated


def merge_scopes(*scopes: dict[str, Any]) -> dict[str, str]:
	merged: dict[str, str] = {}
	for scope in scopes:
		for fieldname, value in scope.items():
			if value in (None, ""):
				continue
			_merge_reference(merged, fieldname, str(value))
	return merged


def validate_master_data_configuration(value: Any) -> dict[str, Any]:
	try:
		return normalize_master_data_configuration(value)
	except ValueError as exc:
		raise frappe.ValidationError(str(exc)) from exc


def _map_fields(
	payload: dict[str, Any],
	mapping: dict[str, Any],
	allowed: frozenset[str],
) -> dict[str, Any]:
	values: dict[str, Any] = {}
	for target, source in mapping.items():
		if target not in allowed:
			raise frappe.ValidationError(f"Unsupported master-data target field: {target}")
		if isinstance(source, dict) and set(source) == {"constant"}:
			value = source["constant"]
		elif isinstance(source, str) and SAFE_FIELD_PATH.fullmatch(source) and "__" not in source:
			value = _resolve_path(payload, source)
		else:
			raise frappe.ValidationError(f"Unsafe master-data mapping for {target}")
		if value not in (None, ""):
			values[target] = value
	return values


def _resolve_path(payload: dict[str, Any], path: str) -> Any:
	value: Any = payload
	for part in path.split("."):
		if not isinstance(value, dict) or part not in value:
			return None
		value = value[part]
	return value


def _resolve_existing_references(
	source_system: str,
	incoming: IncomingClinicalEvent,
	*,
	tenant_hospital: str,
	source_namespace: str,
	include_encounter: bool = True,
) -> dict[str, str]:
	resolved: dict[str, str] = {}
	if incoming.patient_reference:
		patient = frappe.db.get_value(
			"IONE Patient Index",
			{
				"stable_source_key": stable_source_identity_key(
					"patient",
					source_system,
					source_namespace,
					tenant_hospital,
					incoming.patient_reference,
				)
			},
			"name",
		)
		if patient:
			resolved["patient_index"] = patient
	if include_encounter and incoming.encounter_reference:
		encounter = frappe.db.get_value(
			"IONE Encounter Index",
			{
				"stable_source_key": stable_source_identity_key(
					"encounter",
					source_system,
					source_namespace,
					tenant_hospital,
					incoming.encounter_reference,
				)
			},
			[
				"name",
				"patient",
				"hospital",
				"campus",
				"department",
				"ward",
				"responsible_staff",
				"medical_staff",
				*INDICATOR_DIMENSION_FIELDS,
			],
			as_dict=True,
		)
		if encounter:
			for fieldname, value in {
				"encounter_index": encounter.name,
				"patient_index": encounter.patient,
				"hospital": encounter.hospital,
				"campus": encounter.campus,
				"department": encounter.department,
				"ward": encounter.ward,
				"responsible_staff": encounter.responsible_staff,
				"medical_staff": encounter.medical_staff,
				**{fieldname: encounter.get(fieldname) for fieldname in INDICATOR_DIMENSION_FIELDS},
			}.items():
				if value:
					_merge_reference(resolved, fieldname, value)
	if incoming.department_reference:
		departments = frappe.get_all(
			"IONE Medical Department",
			filters={"department_code": incoming.department_reference},
			fields=["name", "hospital"],
			limit_page_length=2,
		)
		departments = [row for row in departments if row.get("hospital") == tenant_hospital]
		if len(departments) > 1:
			raise frappe.ValidationError("Department reference is ambiguous within the tenant hospital")
		if departments:
			_merge_reference(resolved, "department", departments[0].name)
	return resolved


def _upsert_patient(
	source_system: str,
	values: dict[str, Any],
	incoming: IncomingClinicalEvent,
	*,
	tenant_hospital: str,
	source_namespace: str,
	version_strategy: str,
	payload_hash: str,
) -> str:
	source_patient_id = str(values.get("source_patient_id") or incoming.patient_reference or "").strip()
	if not source_patient_id:
		raise frappe.ValidationError("Patient materialization requires source_patient_id")
	if source_patient_id != incoming.source_record_id:
		raise SourceVersionConflictError(
			"Patient source identity must match the incoming source record identity"
		)
	key_candidates = identity_key_candidates(
		"patient",
		source_namespace,
		tenant_hospital,
		source_patient_id,
	)
	patient_key = key_candidates[0][1]
	stable_source_key = stable_source_identity_key(
		"patient",
		source_system,
		source_namespace,
		tenant_hospital,
		source_patient_id,
	)
	with _identity_entity_locks(
		"patient",
		[key for _contract, key in key_candidates],
		source_system=source_system,
		source_namespace=source_namespace,
		tenant_hospital=tenant_hospital,
		source_id=source_patient_id,
	):
		doc = _load_or_new_identity(
			"IONE Patient Index",
			key_field="patient_key",
			key_kind="patient",
			key_candidates=key_candidates,
			source_system=source_system,
			source_namespace=source_namespace,
			tenant_hospital=tenant_hospital,
			source_id=source_patient_id,
			values={
				"patient_key": patient_key,
				"identity_key_contract": identity_key_contract(),
				"stable_source_key": stable_source_key,
				"tenant_hospital": tenant_hospital,
				"source_namespace": source_namespace,
				"source_system": source_system,
				"source_patient_id": source_patient_id,
			},
		)
		is_new = doc.is_new()
		_assert_index_identity(
			doc,
			source_system=source_system,
			source_namespace=source_namespace,
			tenant_hospital=tenant_hospital,
			source_identity_field="source_patient_id",
			source_identity=source_patient_id,
		)
		if not _should_apply_source_version(
			doc,
			incoming,
			version_strategy=version_strategy,
			payload_hash=payload_hash,
		):
			return doc.name
		if "patient_name" in values:
			doc.patient_name = _bounded_text(values["patient_name"], 140)
		if "gender" in values or is_new:
			doc.gender = _choice(values.get("gender"), {"Male", "Female", "Unknown"}, "Unknown")
		if "date_of_birth" in values:
			doc.date_of_birth = getdate(values["date_of_birth"])
		if "identification_number" in values:
			doc.identification_hash = _sensitive_hash(
				"identification",
				values["identification_number"],
			)
		if "phone" in values:
			doc.phone_hash = _sensitive_hash("phone", values["phone"])
		if "status" in values or is_new:
			doc.status = _choice(values.get("status"), {"Active", "Merged", "Inactive"}, "Active")
		_set_source_watermark(
			doc,
			incoming,
			version_strategy=version_strategy,
			payload_hash=payload_hash,
		)
		_save_index(doc)
		return doc.name


def _upsert_encounter(
	endpoint,
	source,
	source_system: str,
	values: dict[str, Any],
	incoming: IncomingClinicalEvent,
	*,
	tenant_hospital: str,
	source_namespace: str,
	version_strategy: str,
	payload_hash: str,
	mapping_lineage: dict[str, str],
) -> dict[str, str]:
	source_encounter_id = str(values.get("source_encounter_id") or incoming.encounter_reference or "").strip()
	if not source_encounter_id:
		raise frappe.ValidationError("Encounter materialization requires source_encounter_id")
	if source_encounter_id != incoming.source_record_id:
		raise SourceVersionConflictError(
			"Encounter source identity must match the incoming source record identity"
		)
	patient_source_id = str(values.get("patient_source_id") or incoming.patient_reference or "").strip()
	if not patient_source_id:
		raise frappe.ValidationError(
			"Encounter materialization requires an existing patient index for patient_source_id"
		)
	patient_key_candidates = identity_key_candidates(
		"patient",
		source_namespace,
		tenant_hospital,
		patient_source_id,
	)
	with _identity_entity_locks(
		"patient",
		[key for _contract, key in patient_key_candidates],
		source_system=source_system,
		source_namespace=source_namespace,
		tenant_hospital=tenant_hospital,
		source_id=patient_source_id,
	):
		patient_doc = _load_or_new_identity(
			"IONE Patient Index",
			key_field="patient_key",
			key_kind="patient",
			key_candidates=patient_key_candidates,
			source_system=source_system,
			source_namespace=source_namespace,
			tenant_hospital=tenant_hospital,
			source_id=patient_source_id,
			values={},
			allow_new=False,
		)
		patient = patient_doc.name
	key_candidates = identity_key_candidates(
		"encounter",
		source_namespace,
		tenant_hospital,
		source_encounter_id,
	)
	encounter_key = key_candidates[0][1]
	stable_source_key = stable_source_identity_key(
		"encounter",
		source_system,
		source_namespace,
		tenant_hospital,
		source_encounter_id,
	)
	with _identity_entity_locks(
		"encounter",
		[key for _contract, key in key_candidates],
		source_system=source_system,
		source_namespace=source_namespace,
		tenant_hospital=tenant_hospital,
		source_id=source_encounter_id,
	):
		doc = _load_or_new_identity(
			"IONE Encounter Index",
			key_field="encounter_key",
			key_kind="encounter",
			key_candidates=key_candidates,
			source_system=source_system,
			source_namespace=source_namespace,
			tenant_hospital=tenant_hospital,
			source_id=source_encounter_id,
			values={
				"encounter_key": encounter_key,
				"identity_key_contract": identity_key_contract(),
				"stable_source_key": stable_source_key,
				"source_namespace": source_namespace,
				"source_system": source_system,
				"source_encounter_id": source_encounter_id,
				"hospital": tenant_hospital,
			},
		)
		is_new = doc.is_new()
		_assert_index_identity(
			doc,
			source_system=source_system,
			source_namespace=source_namespace,
			tenant_hospital=tenant_hospital,
			source_identity_field="source_encounter_id",
			source_identity=source_encounter_id,
		)
		if not is_new and str(doc.get("patient") or "") != patient:
			raise SourceVersionConflictError("Existing encounter cannot be rebound to another patient index")
		apply_values = _should_apply_source_version(
			doc,
			incoming,
			version_strategy=version_strategy,
			payload_hash=payload_hash,
		)
		for fieldname, expected in mapping_lineage.items():
			existing_value = str(doc.get(fieldname) or "")
			if not is_new and existing_value and not hmac.compare_digest(existing_value, expected):
				raise SourceVersionConflictError(
					"Existing encounter cannot be rebound to another mapping lineage"
				)
		if apply_values:
			for fieldname, expected in mapping_lineage.items():
				doc.set(fieldname, expected)
			doc.patient = patient
			if "encounter_no" in values:
				doc.encounter_no = _bounded_text(values["encounter_no"], 140)
			for fieldname, (doctype, code_field) in LINK_CODE_FIELDS.items():
				if values.get(fieldname) not in (None, ""):
					doc.set(fieldname, _resolve_link(doctype, code_field, values[fieldname]))
			for fieldname in INDICATOR_DIMENSION_FIELDS:
				if values.get(fieldname) not in (None, ""):
					doc.set(fieldname, _bounded_text(values[fieldname], 140))
			if str(doc.get("hospital") or "") != tenant_hospital:
				raise SourceVersionConflictError(
					"Encounter mapping cannot change its preauthorized tenant hospital"
				)
			scope = authorize_endpoint_scope(
				endpoint,
				source,
				canonicalize_scope(
					{
						"hospital": doc.get("hospital"),
						"campus": doc.get("campus"),
						"department": doc.get("department"),
						"ward": doc.get("ward"),
						"responsible_staff": doc.get("responsible_staff"),
						"medical_staff": doc.get("medical_staff"),
					}
				),
			)
			for fieldname in ("hospital", "campus", "department", "ward"):
				doc.set(fieldname, scope.get(fieldname))
			if "encounter_type" in values or is_new:
				doc.encounter_type = _choice(
					values.get("encounter_type"),
					set(ENCOUNTER_TYPES),
					"Inpatient",
				)
			if "status" in values or is_new:
				doc.status = _choice(
					values.get("status"),
					{"Active", "Discharged", "Closed", "Cancelled"},
					"Active",
				)
			if "admission_time" in values:
				doc.admission_time = get_datetime(values["admission_time"])
			if "discharge_time" in values:
				doc.discharge_time = get_datetime(values["discharge_time"])
			if doc.admission_time and doc.discharge_time and doc.discharge_time < doc.admission_time:
				raise frappe.ValidationError("Encounter discharge_time cannot precede admission_time")
			_set_source_watermark(
				doc,
				incoming,
				version_strategy=version_strategy,
				payload_hash=payload_hash,
			)
			_save_index(doc)
		return {
			key: value
			for key, value in {
				"encounter_index": doc.name,
				"patient_index": doc.get("patient"),
				"hospital": doc.get("hospital"),
				"campus": doc.get("campus"),
				"department": doc.get("department"),
				"ward": doc.get("ward"),
				"responsible_staff": doc.get("responsible_staff"),
				"medical_staff": doc.get("medical_staff"),
				**{fieldname: doc.get(fieldname) for fieldname in INDICATOR_DIMENSION_FIELDS},
			}.items()
			if value
		}


def _validated_indicator_mapping_lineage(
	source_system: str,
	value: dict[str, str] | None,
) -> dict[str, str]:
	if not isinstance(value, dict):
		raise frappe.ValidationError(
			"Encounter materialization requires a frozen integration mapping lineage"
		)
	lineage = {
		"source_system": str(source_system or "").strip(),
		"mapping_record": str(value.get("mapping_record") or "").strip(),
		"mapping_version": str(value.get("mapping_version") or "").strip(),
		"mapping_checksum": str(value.get("mapping_checksum") or "").strip().lower(),
	}
	if any(not item for item in lineage.values()):
		raise frappe.ValidationError(
			"Encounter materialization requires source system, mapping record/version/checksum"
		)
	if len(lineage["mapping_checksum"]) != 64 or any(
		character not in "0123456789abcdef" for character in lineage["mapping_checksum"]
	):
		raise frappe.ValidationError("Encounter materialization mapping checksum is invalid")
	return lineage


def _load_or_new_identity(
	doctype: str,
	*,
	key_field: str,
	key_kind: str,
	key_candidates: tuple[tuple[str, str], ...],
	source_system: str,
	source_namespace: str,
	tenant_hospital: str,
	source_id: str,
	values: dict[str, Any],
	allow_new: bool = True,
):
	"""Resolve one stable source identity and lazily rekey under transaction locks.

	The raw source tuple is deliberately queried in addition to the active/retained
	HMAC ring. If an operator removes a retained key too early, the existing row is
	still found and verification fails closed instead of creating a second identity.
	"""
	candidate_keys = [key for _contract, key in key_candidates]
	stable_source_key = stable_source_identity_key(
		key_kind,
		source_system,
		source_namespace,
		tenant_hospital,
		source_id,
	)
	key_rows = frappe.get_all(
		doctype,
		filters={key_field: ["in", candidate_keys]},
		fields=["name", key_field, "identity_key_contract"],
		order_by="name asc",
		limit_page_length=3,
	)
	stable_filters = _stable_source_identity_filters(
		doctype,
		source_system=source_system,
		source_namespace=source_namespace,
		tenant_hospital=tenant_hospital,
		source_id=source_id,
	)
	stable_rows = frappe.get_all(
		doctype,
		filters={"stable_source_key": stable_source_key},
		fields=["name", key_field, "identity_key_contract", "stable_source_key"],
		order_by="name asc",
		limit_page_length=3,
	)
	legacy_rows = _binary_stable_source_identity_rows(doctype, stable_filters)
	rows_by_name = {
		str(row.get("name") or ""): row
		for row in (*key_rows, *stable_rows, *legacy_rows)
		if str(row.get("name") or "")
	}
	if len(rows_by_name) > 1:
		raise SourceVersionConflictError("Identity-key rotation found multiple rows for one source identity")
	if not rows_by_name:
		if not allow_new:
			raise frappe.ValidationError(
				"Encounter materialization requires an existing patient index for patient_source_id"
			)
		return frappe.get_doc({"doctype": doctype, **values})
	row = next(iter(rows_by_name.values()))
	doc = frappe.get_doc(doctype, row.name, for_update=True)
	for fieldname, expected in stable_filters.items():
		if not hmac.compare_digest(str(doc.get(fieldname) or ""), str(expected)):
			raise SourceVersionConflictError(
				"Stored identity key is bound to a different stable source identity"
			)
	stored_stable_key = str(doc.get("stable_source_key") or "")
	if stored_stable_key and not hmac.compare_digest(stored_stable_key, stable_source_key):
		raise SourceVersionConflictError("Stored stable source key is bound to a different source identity")
	if not verify_stored_identity_key(
		key_kind,
		doc.get(key_field),
		doc.get("identity_key_contract"),
		source_namespace,
		tenant_hospital,
		source_id,
	):
		raise SourceVersionConflictError("Stored identity key failed retained-key verification")
	active_contract, active_key = key_candidates[0]
	if (
		str(doc.get("identity_key_contract") or "") != active_contract
		or str(doc.get(key_field) or "") != active_key
		or stored_stable_key != stable_source_key
	):
		conflicts = frappe.db.sql(
			f"select name from `tab{doctype}` where {key_field} = %s "  # noqa: S608
			"order by name asc limit 2 for update",
			(active_key,),
			as_dict=True,
		)
		if any(str(conflict.get("name") or "") != str(doc.name) for conflict in conflicts):
			raise SourceVersionConflictError("Active identity key collides with another index row")
		frappe.db.set_value(
			doctype,
			doc.name,
			{
				key_field: active_key,
				"identity_key_contract": active_contract,
				"stable_source_key": stable_source_key,
			},
			update_modified=False,
		)
		doc.set(key_field, active_key)
		doc.set("identity_key_contract", active_contract)
		doc.set("stable_source_key", stable_source_key)
	return doc


def _binary_stable_source_identity_rows(
	doctype: str,
	stable_filters: dict[str, str],
) -> list[dict[str, Any]]:
	"""Find legacy rows using exact binary predicates, independent of DB collation."""
	if doctype == "IONE Patient Index":
		hospital_field = "tenant_hospital"
		source_id_field = "source_patient_id"
	elif doctype == "IONE Encounter Index":
		hospital_field = "hospital"
		source_id_field = "source_encounter_id"
	else:
		raise frappe.ValidationError("Unsupported stable source identity DocType")
	return frappe.db.sql(
		f"select name, stable_source_key from `tab{doctype}` "  # noqa: S608
		"where binary source_system = binary %s "
		"and binary source_namespace = binary %s "
		f"and binary `{hospital_field}` = binary %s "
		f"and binary `{source_id_field}` = binary %s "
		"order by name asc limit 3 for update",
		(
			stable_filters["source_system"],
			stable_filters["source_namespace"],
			stable_filters[hospital_field],
			stable_filters[source_id_field],
		),
		as_dict=True,
	)


def _stable_source_identity_filters(
	doctype: str,
	*,
	source_system: str,
	source_namespace: str,
	tenant_hospital: str,
	source_id: str,
) -> dict[str, str]:
	common = {
		"source_system": str(source_system or "").strip(),
		"source_namespace": str(source_namespace or "").strip(),
	}
	if doctype == "IONE Patient Index":
		common.update(
			{
				"tenant_hospital": str(tenant_hospital or "").strip(),
				"source_patient_id": str(source_id or "").strip(),
			}
		)
	elif doctype == "IONE Encounter Index":
		common.update(
			{
				"hospital": str(tenant_hospital or "").strip(),
				"source_encounter_id": str(source_id or "").strip(),
			}
		)
	else:
		raise frappe.ValidationError("Unsupported stable source identity DocType")
	if any(not value for value in common.values()):
		raise frappe.ValidationError("Stable source identity requires four non-empty components")
	return common


def _save_index(doc) -> None:
	doc.flags.ignore_permissions = True
	if doc.is_new():
		doc.insert()
	else:
		doc.save()


def _resolve_link(doctype: str, code_field: str, value: Any) -> str:
	raw = str(value).strip()
	if frappe.db.exists(doctype, raw):
		return raw
	names = frappe.get_all(
		doctype,
		filters={code_field: raw},
		pluck="name",
		limit_page_length=2,
	)
	if not names:
		raise frappe.ValidationError(f"Unknown {doctype} code or name")
	if len(names) > 1:
		raise frappe.ValidationError(f"Ambiguous {doctype} code; use the exact document name")
	return names[0]


def _assert_index_identity(
	doc,
	*,
	source_system: str,
	source_namespace: str,
	tenant_hospital: str,
	source_identity_field: str,
	source_identity: str,
) -> None:
	for fieldname, expected in (
		("source_system", source_system),
		("source_namespace", source_namespace),
	):
		if str(doc.get(fieldname) or "") != str(expected):
			raise SourceVersionConflictError(f"Existing master-data {fieldname} cannot change")
	hospital_field = "tenant_hospital" if doc.doctype == "IONE Patient Index" else "hospital"
	if str(doc.get(hospital_field) or "") != str(tenant_hospital):
		raise SourceVersionConflictError("Existing master-data tenant hospital cannot change")
	if str(doc.get(source_identity_field) or "") != source_identity:
		raise SourceVersionConflictError("Existing master-data source identity cannot change")


def compare_source_versions(applied: str, incoming: str, strategy: str) -> int:
	"""Compare two source-defined monotonic versions without lexical guessing."""
	left = _source_version_key(applied, strategy)
	right = _source_version_key(incoming, strategy)
	return (right > left) - (right < left)


def _source_version_key(value: str, strategy: str) -> int | datetime:
	normalized = str(value or "").strip()
	if strategy == "integer":
		if not INTEGER_SOURCE_VERSION.fullmatch(normalized):
			raise frappe.ValidationError("Integer source versions must be canonical non-negative integers")
		return int(normalized)
	if strategy == "datetime":
		if not UTC_DATETIME_SOURCE_VERSION.fullmatch(normalized):
			raise frappe.ValidationError(
				"Datetime source versions must use canonical RFC3339 UTC form ending in Z"
			)
		try:
			parsed = datetime.fromisoformat(normalized[:-1] + "+00:00")
		except ValueError as exc:
			raise frappe.ValidationError("Datetime source version is invalid") from exc
		return parsed
	raise frappe.ValidationError("Unsupported master-data source_version_strategy")


def _source_version_strategy(config: dict[str, Any]) -> str:
	strategy = str(config.get("source_version_strategy") or "").strip().lower()
	if strategy not in SOURCE_VERSION_STRATEGIES:
		raise frappe.ValidationError(
			"Enabled master-data mapping requires source_version_strategy integer or datetime"
		)
	return strategy


def _should_apply_source_version(
	doc,
	incoming: IncomingClinicalEvent,
	*,
	version_strategy: str,
	payload_hash: str,
) -> bool:
	if doc.is_new():
		_source_version_key(incoming.source_version, version_strategy)
		return True
	applied_version = str(doc.get("applied_source_version") or "")
	applied_strategy = str(doc.get("applied_version_strategy") or "")
	applied_hash = str(doc.get("applied_payload_hash") or "")
	if not applied_version or not applied_strategy or not applied_hash:
		raise SourceVersionConflictError(
			"Existing master-data index has no governed source-version watermark"
		)
	if applied_strategy != version_strategy:
		raise SourceVersionConflictError("Master-data source-version strategy changed after first apply")
	comparison = compare_source_versions(applied_version, incoming.source_version, version_strategy)
	if comparison < 0:
		raise StaleSourceVersionError("Stale source version cannot overwrite a newer master-data index")
	if comparison == 0:
		if hmac.compare_digest(applied_hash, payload_hash):
			return False
		raise SourceVersionConflictError("One source version was reused with different clinical data")
	return True


def _set_source_watermark(
	doc,
	incoming: IncomingClinicalEvent,
	*,
	version_strategy: str,
	payload_hash: str,
) -> None:
	doc.applied_source_version = incoming.source_version
	doc.applied_version_strategy = version_strategy
	doc.applied_event_time = incoming.event_time
	doc.applied_payload_hash = payload_hash
	doc.last_synced_at = now_datetime()


def _validate_payload_hash(payload_hash: str) -> None:
	if not re.fullmatch(r"[0-9a-f]{64}", str(payload_hash or "")):
		raise frappe.ValidationError("Master-data materialization requires a canonical payload hash")


@contextmanager
def _identity_entity_locks(
	entity: str,
	candidate_keys: list[str],
	*,
	source_system: str,
	source_namespace: str,
	tenant_hospital: str,
	source_id: str,
):
	"""Hold compatible candidate and stable locks through outer commit/rollback.

	Frappe's advisory-lock context releases at block exit, which is earlier than
	the request transaction commit. These locks are acquired with the exact same
	hashed MariaDB name but are released only by transaction callbacks. A stable
	source-tuple lock remains common even after an accidental retained-key removal.
	"""
	normalized_entity = str(entity or "").strip().lower()
	if normalized_entity not in {"patient", "encounter"}:
		raise frappe.ValidationError("Master-data identity lock kind is not governed")
	stable_token = hashlib.sha256(
		json.dumps(
			{
				"entity": normalized_entity,
				"source_system": str(source_system or "").strip(),
				"source_namespace": str(source_namespace or "").strip(),
				"tenant_hospital": str(tenant_hospital or "").strip(),
				"source_id": str(source_id or "").strip(),
			},
			ensure_ascii=False,
			sort_keys=True,
			separators=(",", ":"),
		).encode()
	).hexdigest()
	lock_keys = {
		f"ione-qms:master-data:{normalized_entity}:{candidate_key}" for candidate_key in candidate_keys
	}
	lock_keys.add(f"ione-qms:master-data:{normalized_entity}:stable:{stable_token}")
	for lock_key in sorted(lock_keys):
		_acquire_transaction_identity_lock(lock_key)
	yield


def _acquire_transaction_identity_lock(lock_key: str) -> None:
	"""Acquire one MariaDB named lock once and defer release to transaction end."""
	state = getattr(frappe.flags, "ione_identity_transaction_locks", None)
	if not isinstance(state, dict):
		state = {"names": set()}
		frappe.flags.ione_identity_transaction_locks = state

		def release_transaction_locks() -> None:
			active = state.get("names")
			if not isinstance(active, set):
				active = set()
			release_failures = 0
			for lock_name in sorted(active, reverse=True):
				try:
					result = frappe.db.sql("select release_lock(%s)", (lock_name,))
					if not result or result[0][0] != 1:
						release_failures += 1
				except Exception:
					# A closed connection releases MariaDB named locks itself. Never
					# turn an already committed clinical transaction into a false error.
					release_failures += 1
			active.clear()
			frappe.flags.ione_identity_transaction_locks = None
			if release_failures:
				try:
					frappe.log_error(
						title="IONE identity transaction lock release failed",
						message=(f"count={release_failures}; no source identity or lock name was logged."),
					)
				except Exception:
					# Logging must not poison an already committed transaction.
					return

		def rearm_commit_failure_release() -> None:
			# Frappe clears ``after_rollback`` before issuing SQL COMMIT.  If
			# COMMIT itself then fails, the request wrapper rolls back but only
			# callbacks registered during ``before_commit`` survive to release
			# session-scoped MariaDB named locks.
			frappe.db.after_rollback.add(release_transaction_locks)

		frappe.db.before_commit.add(rearm_commit_failure_release)
		frappe.db.after_commit.add(release_transaction_locks)
		frappe.db.after_rollback.add(release_transaction_locks)
	names = state.get("names")
	if not isinstance(names, set):
		raise RuntimeError("Identity transaction lock registry is invalid")
	lock_name = hashlib.sha256(str(lock_key).encode()).hexdigest()
	if lock_name in names:
		return
	result = frappe.db.sql(
		"select get_lock(%s, %s)",
		(lock_name, MASTER_DATA_LOCK_TIMEOUT_SECONDS),
	)
	if not result or result[0][0] != 1:
		from frappe.exceptions import QueryTimeoutError

		raise QueryTimeoutError(
			f"Could not acquire governed master-data identity lock within {MASTER_DATA_LOCK_TIMEOUT_SECONDS}s"
		)
	names.add(lock_name)


def _stable_key(
	kind: str,
	source_namespace: Any,
	tenant_hospital: Any,
	source_id: Any,
) -> str:
	"""Backward-compatible private helper for the active site-keyed surrogate."""
	return site_identity_key(kind, source_namespace, tenant_hospital, source_id)


def _sensitive_hash(kind: str, value: Any) -> str | None:
	if value in (None, ""):
		return None
	if kind == "phone":
		normalized = re.sub(
			r"[\s().-]+",
			"",
			unicodedata.normalize("NFKC", str(value)).strip(),
		)
		if not re.fullmatch(r"\+?[0-9]{3,32}", normalized):
			raise frappe.ValidationError("Patient phone must use a canonical international number.")
	elif kind == "identification":
		normalized = unicodedata.normalize("NFKC", str(value)).strip().upper()
		if not 2 <= len(normalized) <= 128 or any(
			unicodedata.category(character).startswith("C") for character in normalized
		):
			raise frappe.ValidationError("Patient identification number has an unsafe format.")
	else:
		raise frappe.ValidationError("Sensitive identifier hash kind is not governed.")
	key = active_hmac_key("ione_sensitive_hash_hmac")
	message = json.dumps(
		{
			"contract": "ione-qms-sensitive-hash-v1",
			"key_id": key.key_id,
			"kind": kind,
			"value": normalized,
		},
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
	).encode("utf-8")
	return hmac.new(key.secret, message, hashlib.sha256).hexdigest()


def _choice(value: Any, allowed: set[str], default: str) -> str:
	normalized = str(value or default).strip()
	if normalized not in allowed:
		raise frappe.ValidationError(f"Unsupported master-data choice: {normalized}")
	return normalized


def _bounded_text(value: Any, length: int) -> str | None:
	if value in (None, ""):
		return None
	text = str(value).strip()
	if len(text) > length:
		raise frappe.ValidationError(f"Master-data value exceeds {length} characters")
	return text


def _merge_reference(target: dict[str, str], fieldname: str, value: str) -> None:
	if target.get(fieldname) and target[fieldname] != value:
		raise frappe.ValidationError(f"Conflicting {fieldname} references in the clinical event")
	target[fieldname] = value
