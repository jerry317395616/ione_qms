from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import frappe
from frappe.utils import get_datetime

_SCOPE_FIELDS = (
	"hospital",
	"campus",
	"department",
	"ward",
	"patient",
	"encounter",
	"responsible_staff",
)


@dataclass(frozen=True)
class ProjectionSpec:
	doctype: str
	key_field: str
	reference_fields: tuple[str, ...]


_PROJECTIONS = {
	"IONE Medical Record QC": ProjectionSpec(
		doctype="IONE Medical Record QC",
		key_field="record_key",
		reference_fields=("record_type",),
	),
	"IONE Surgery QC": ProjectionSpec(
		doctype="IONE Surgery QC",
		key_field="surgery_key",
		reference_fields=("surgery_no", "surgery_time"),
	),
	"IONE Medical Safety Event": ProjectionSpec(
		doctype="IONE Medical Safety Event",
		key_field="safety_event_key",
		reference_fields=("event_type", "event_time"),
	),
	"IONE Data Reconciliation": ProjectionSpec(
		doctype="IONE Data Reconciliation",
		key_field="reconciliation_key",
		reference_fields=("source_system", "endpoint", "period_start", "period_end"),
	),
}

_PROJECTION_MATERIALIZER_CAPABILITY = object()
_MEDICAL_RECORD_ARCHIVE_APPLICATION_CAPABILITY = object()
_MEDICAL_RECORD_RELEASE_LOCK_CAPABILITY = object()


def materialize_medical_record_qc(values: Mapping[str, Any]) -> str:
	return materialize_projection("IONE Medical Record QC", values)


def materialize_surgery_qc(values: Mapping[str, Any]) -> str:
	from ione_qms.services.surgery_governance import prepare_surgery_projection

	return materialize_projection(
		"IONE Surgery QC",
		prepare_surgery_projection(values),
	)


def materialize_medical_safety_event(values: Mapping[str, Any]) -> str:
	return materialize_projection("IONE Medical Safety Event", values)


def materialize_data_reconciliation(values: Mapping[str, Any]) -> str:
	return materialize_projection("IONE Data Reconciliation", values)


def materialize_projection(doctype: str, values: Mapping[str, Any]) -> str:
	"""Idempotently create or update a reviewed projection through one controlled path."""
	spec = _projection_spec(doctype)
	system_owner = values.get("_system_owner") is True
	payload = _validated_payload(spec, values)
	payload.pop("_system_owner", None)
	_hydrate_scope(payload)
	if doctype in {
		"IONE Medical Record QC",
		"IONE Surgery QC",
		"IONE Medical Safety Event",
	}:
		missing_lineage = [
			fieldname
			for fieldname in (
				"source_system",
				"mapping_record",
				"mapping_version",
				"mapping_checksum",
			)
			if not payload.get(fieldname)
		]
		if missing_lineage:
			frappe.throw(
				"Projection cannot become an indicator source without complete mapping lineage: "
				+ ", ".join(missing_lineage)
			)
	projection_key = _projection_key(spec, payload)
	payload.pop("_source_identity", None)
	submitted_key = str(payload.get(spec.key_field) or "")
	if submitted_key and submitted_key != projection_key:
		frappe.throw(f"{spec.key_field} does not match the canonical projection identity")
	payload[spec.key_field] = projection_key

	lock_name = f"ione-qms:projection:{doctype}:{projection_key}"
	audit_context = system_audit_identity() if system_owner else nullcontext()
	with audit_context:
		with frappe.db.advisory_lock(lock_name, timeout=5):
			name = frappe.db.get_value(doctype, {spec.key_field: projection_key}, "name")
			doc = frappe.get_doc(doctype, name) if name else frappe.get_doc({"doctype": doctype})
			doc.update(payload)
			if doc.is_new() and system_owner:
				doc.owner = "Administrator"
			doc.flags.ione_projection_materializer = _PROJECTION_MATERIALIZER_CAPABILITY
			doc.flags.ignore_permissions = True
			try:
				if doc.is_new():
					doc.insert()
				else:
					doc.save()
			except frappe.DuplicateEntryError:
				# The unique key is the final arbiter if another worker did not honor the lock.
				name = frappe.db.get_value(doctype, {spec.key_field: projection_key}, "name")
				if not name:
					raise
				doc = frappe.get_doc(doctype, name)
				doc.update(payload)
				doc.flags.ione_projection_materializer = _PROJECTION_MATERIALIZER_CAPABILITY
				doc.flags.ignore_permissions = True
				doc.save()
	return str(doc.name)


def apply_medical_record_archive_projection(medical_record_qc: str) -> str:
	"""Reflect an externally applied archive only after its signed Allow receipt is durable.

	This updates the local read-only projection. It never writes to the hospital
	source system; the source remains responsible for applying the decision and
	returning the signed acknowledgement.
	"""
	name = str(medical_record_qc or "").strip()
	if not name:
		frappe.throw("Medical-record archive application requires a projection.")
	with medical_record_release_lock(name) as doc:
		if str(doc.get("record_status") or "") == "Archived":
			return str(doc.name)
		if str(doc.get("record_status") or "") != "Final":
			frappe.throw("Only a Final medical-record projection may be archived.")
		from ione_qms.services.medical_record_review import assert_medical_record_archive_ready

		assert_medical_record_archive_ready(doc)
		doc.record_status = "Archived"
		doc.flags.ione_medical_record_archive_application = _MEDICAL_RECORD_ARCHIVE_APPLICATION_CAPABILITY
		doc.flags.ignore_permissions = True
		doc.save()
	return str(doc.name)


@contextmanager
def medical_record_release_lock(medical_record_qc: str) -> Iterator[Any]:
	"""Share one re-entrant record-first lock across batching and archive publication."""
	name = str(medical_record_qc or "").strip()
	if not name:
		frappe.throw("Medical-record release locking requires an exact projection.")
	active = getattr(frappe.flags, "ione_medical_record_release_lock", None)
	if (
		isinstance(active, tuple)
		and len(active) == 2
		and active[0] is _MEDICAL_RECORD_RELEASE_LOCK_CAPABILITY
		and active[1] == name
	):
		yield frappe.get_doc("IONE Medical Record QC", name, for_update=True)
		return
	with frappe.db.advisory_lock(f"ione-qms:medical-record-release:{name}", timeout=30):
		previous = active
		frappe.flags.ione_medical_record_release_lock = (
			_MEDICAL_RECORD_RELEASE_LOCK_CAPABILITY,
			name,
		)
		try:
			yield frappe.get_doc("IONE Medical Record QC", name, for_update=True)
		finally:
			frappe.flags.ione_medical_record_release_lock = previous


def assert_medical_record_release_lock(medical_record_qc: str) -> None:
	"""Require the exact record-first release capability for split helper code."""
	name = str(medical_record_qc or "").strip()
	active = getattr(frappe.flags, "ione_medical_record_release_lock", None)
	if not (
		name
		and isinstance(active, tuple)
		and len(active) == 2
		and active[0] is _MEDICAL_RECORD_RELEASE_LOCK_CAPABILITY
		and active[1] == name
	):
		frappe.throw(
			"Medical-record release publication requires its exact record lock.",
			frappe.PermissionError,
		)


@contextmanager
def system_audit_identity() -> Iterator[None]:
	"""Write sensitive system-created records without persisting the request principal.

	Only the current user attribute is changed. Session identifiers, request data,
	and authentication state are deliberately left intact and the principal is
	restored even if validation or persistence fails.
	"""
	original_user = frappe.local.session.user
	frappe.local.session.user = "Administrator"
	try:
		yield
	finally:
		frappe.local.session.user = original_user


def validate_projection_identity(doc, method: str | None = None) -> None:
	"""Reject every projection write outside one exact in-process capability."""
	del method
	spec = _projection_spec(doc.doctype)
	materializer_allowed = (
		getattr(doc.flags, "ione_projection_materializer", None) is _PROJECTION_MATERIALIZER_CAPABILITY
	)
	archive_application_allowed = (
		doc.doctype == "IONE Medical Record QC"
		and getattr(doc.flags, "ione_medical_record_archive_application", None)
		is _MEDICAL_RECORD_ARCHIVE_APPLICATION_CAPABILITY
	)
	if not materializer_allowed and not archive_application_allowed:
		frappe.throw(
			f"{doc.doctype} may only be written by its controlled projection service",
			frappe.PermissionError,
		)
	previous = doc.get_doc_before_save()
	if archive_application_allowed:
		if previous is None or (
			str(previous.get("record_status") or "") != "Final"
			or str(doc.get("record_status") or "") != "Archived"
		):
			frappe.throw(
				"Medical-record archive capability permits only Final to Archived.",
				frappe.PermissionError,
			)
	elif (
		doc.doctype == "IONE Medical Record QC"
		and previous is not None
		and str(previous.get("record_status") or "") == "Final"
		and str(doc.get("record_status") or "") == "Archived"
	):
		frappe.throw(
			"Final medical records require the distinct archive-application service.",
			frappe.PermissionError,
		)
	payload = {field.fieldname: doc.get(field.fieldname) for field in doc.meta.fields}
	_hydrate_scope(payload)
	expected_key = _projection_key(spec, payload)
	if str(doc.get(spec.key_field) or "") != expected_key:
		frappe.throw(f"{spec.key_field} is immutable and must match the canonical projection identity")


def _validated_payload(spec: ProjectionSpec, values: Mapping[str, Any]) -> dict[str, Any]:
	if not isinstance(values, Mapping):
		raise TypeError("Projection values must be a mapping")
	meta = frappe.get_meta(spec.doctype)
	allowed = {field.fieldname for field in meta.fields}
	unknown = set(values).difference(allowed.union({"_system_owner"}))
	if unknown:
		frappe.throw(f"Unsupported {spec.doctype} projection fields: {', '.join(sorted(unknown))}")
	return {
		fieldname: value
		for fieldname, value in values.items()
		if (fieldname in allowed or fieldname == "_system_owner") and value not in (None, "")
	}


def _hydrate_scope(payload: dict[str, Any]) -> None:
	for link_field, doctype in (
		("event", "IONE Clinical Quality Event"),
		("clinical_event", "IONE Clinical Quality Event"),
		("encounter", "IONE Encounter Index"),
		("finding", "IONE QC Finding"),
	):
		name = payload.get(link_field)
		if not name:
			continue
		_merge_linked_scope(payload, doctype, str(name))


def _merge_linked_scope(payload: dict[str, Any], doctype: str, name: str) -> None:
	meta = frappe.get_meta(doctype)
	fields = [
		fieldname
		for fieldname in (
			"hospital",
			"campus",
			"department",
			"ward",
			"patient",
			"patient_index",
			"encounter",
			"encounter_index",
			"responsible_staff",
			"medical_staff",
			"medical_group",
			"disease",
			"surgery",
			"drg",
			"source_system",
			"mapping_record",
			"mapping_version",
			"mapping_checksum",
			"source_record_type",
			"source_record_id",
			"source_version",
		)
		if meta.has_field(fieldname)
	]
	row = frappe.db.get_value(doctype, name, fields, as_dict=True)
	if not row:
		frappe.throw(f"Projection references an unknown {doctype}")
	mappings = {
		"hospital": row.get("hospital"),
		"campus": row.get("campus"),
		"department": row.get("department"),
		"ward": row.get("ward"),
		"patient": row.get("patient") or row.get("patient_index"),
		"encounter": row.get("encounter") or row.get("encounter_index"),
		"responsible_staff": row.get("responsible_staff") or row.get("medical_staff"),
		"medical_group": row.get("medical_group"),
		"disease": row.get("disease"),
		"surgery": row.get("surgery"),
		"drg": row.get("drg"),
		"source_system": row.get("source_system"),
		"mapping_record": row.get("mapping_record"),
		"mapping_version": row.get("mapping_version"),
		"mapping_checksum": row.get("mapping_checksum"),
	}
	for fieldname, value in mappings.items():
		if value in (None, ""):
			continue
		if payload.get(fieldname) not in (None, "", value):
			frappe.throw(f"Projection {fieldname} conflicts with its linked {doctype} scope")
		payload[fieldname] = value
	if doctype == "IONE Clinical Quality Event":
		payload["_source_identity"] = {
			"event": name,
			"source_system": row.get("source_system"),
			"source_record_type": row.get("source_record_type"),
			"source_record_id": row.get("source_record_id"),
			"source_version": row.get("source_version"),
		}


def _projection_key(spec: ProjectionSpec, payload: Mapping[str, Any]) -> str:
	if spec.doctype == "IONE Data Reconciliation":
		source_identity = {
			fieldname: _reference_value(fieldname, payload.get(fieldname))
			for fieldname in spec.reference_fields
			if payload.get(fieldname) not in (None, "")
		}
		if any(
			fieldname not in source_identity for fieldname in ("source_system", "period_start", "period_end")
		):
			frappe.throw("Data reconciliation identity requires source_system, period_start, and period_end")
		if get_datetime(payload.get("period_end")) < get_datetime(payload.get("period_start")):
			frappe.throw("Data reconciliation period_end cannot precede period_start")
		reference: dict[str, Any] = {}
	else:
		source_identity = dict(payload.get("_source_identity") or {})
		reference = {
			fieldname: _reference_value(fieldname, payload.get(fieldname))
			for fieldname in spec.reference_fields
			if payload.get(fieldname) not in (None, "")
		}
		_validate_clinical_source_identity(spec, payload, source_identity, reference)
		if spec.doctype == "IONE Surgery QC":
			# Surgery events are immutable versions of one source operation.
			# Materialize them into one governed surgery anchor so analytics do
			# not count Scheduled/Occurred/Finalized as separate operations.
			source_identity = {
				fieldname: source_identity.get(fieldname)
				for fieldname in (
					"source_system",
					"source_record_type",
					"source_record_id",
				)
			}
			reference = {}
	identity = {
		"key_version": 1,
		"doctype": spec.doctype,
		"source": source_identity,
		"reference": reference,
		"scope": {
			fieldname: _canonical_value(payload.get(fieldname))
			for fieldname in _SCOPE_FIELDS
			if payload.get(fieldname) not in (None, "")
		},
	}
	return hashlib.sha256(_canonical_json(identity).encode()).hexdigest()


def _validate_clinical_source_identity(
	spec: ProjectionSpec,
	payload: Mapping[str, Any],
	source_identity: Mapping[str, Any],
	reference: Mapping[str, Any],
) -> None:
	if source_identity:
		return
	if spec.doctype == "IONE Medical Record QC":
		if payload.get("encounter") and reference.get("record_type"):
			return
	elif spec.doctype == "IONE Surgery QC":
		if payload.get("encounter") and reference.get("surgery_no") and reference.get("surgery_time"):
			return
	elif spec.doctype == "IONE Medical Safety Event":
		frappe.throw("Medical safety event materialization requires a linked clinical_event")
	frappe.throw(f"{spec.doctype} materialization requires a clinical event or a complete reviewed reference")


def _projection_spec(doctype: str) -> ProjectionSpec:
	try:
		return _PROJECTIONS[doctype]
	except KeyError as exc:
		raise ValueError(f"Unsupported projection DocType: {doctype}") from exc


def _canonical_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)


def _canonical_value(value: Any) -> Any:
	if value in (None, ""):
		return None
	if isinstance(value, datetime):
		return value.isoformat(timespec="microseconds")
	if isinstance(value, date):
		return value.isoformat()
	if isinstance(value, str):
		return value.strip()
	return value


def _reference_value(fieldname: str, value: Any) -> Any:
	if fieldname in {"surgery_time", "event_time", "period_start", "period_end"}:
		return get_datetime(value).isoformat(timespec="microseconds")
	return _canonical_value(value)
