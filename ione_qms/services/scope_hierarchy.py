from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import frappe

from ione_qms.services.identity_keys import (
	acceptable_identity_key_contracts,
	verify_stored_identity_key,
)

CLINICAL_SCOPE_DOCTYPES = frozenset(
	{
		"IONE Hospital Campus",
		"IONE Medical Department",
		"IONE Ward",
		"IONE Medical Staff",
		"IONE Staff Qualification",
		"IONE Patient Index",
		"IONE Encounter Index",
		"IONE Surgery Authorization",
		"IONE Surgery Procedure Policy",
		"IONE Clinical Quality Event",
		"IONE QC Execution",
		"IONE QC Rule Shadow Execution",
		"IONE QC Rule Shadow Feedback",
		"IONE QC Finding",
		"IONE Medical Record QC",
		"IONE Medical Record Sampling Policy",
		"IONE Medical Record Sampling Policy Operation",
		"IONE Medical Record Review Batch",
		"IONE Medical Record Review Assignment",
		"IONE Medical Record Review Decision",
		"IONE Medical Record Archive Decision",
		"IONE Medical Record Archive Delivery",
		"IONE Medical Record Completeness Watermark",
		"IONE Surgery QC",
		"IONE Surgery MDT Record",
		"IONE Surgery MDT Participant",
		"IONE Surgery Safety Checklist",
		"IONE Surgery Emergency Exception",
		"IONE Medical Safety Event",
		"IONE QC Finding Appeal",
		"IONE QC Rectification",
		"IONE QC Verification",
		"IONE PDCA Project",
		"IONE Quality Meeting",
		"IONE Meeting Minute",
		"IONE Meeting Decision",
		"IONE Quality Action Item",
		"IONE Quality Action Verification Round",
		"IONE Quality Experience Share",
		"IONE Finding Recurrence Policy",
		"IONE Finding Recurrence Run",
		"IONE Finding Recurrence Evaluation",
		"IONE Indicator Result",
		"IONE Indicator Result Detail",
		"IONE Indicator Alert",
		"IONE Daily Quality Fact",
		"IONE Monthly Quality Fact",
		"IONE Finding Analysis Fact",
		"IONE Surgery Quality Fact",
		"IONE Agent Analysis Fact",
		"IONE AI Analysis Task",
		"IONE AI Candidate Finding",
		"IONE AI Report Schedule",
		"IONE AI Data Access Log",
		"IONE AI Execution Event",
		"IONE AI Report Draft",
		"IONE Quality Report Snapshot",
		"IONE AI Report Recovery Authorization",
		"IONE AI Tool Approval",
		"IONE Flow Run Link",
		"IONE Data Quality Issue",
		"IONE Integration Message",
		"IONE Source Document Locator",
		"IONE Source Document Access Log",
		"IONE Data Export Request",
		"IONE PHI Disclosure Policy",
	}
)

_ORGANIZATION_FIELDS = ("hospital", "campus", "department", "ward")
_STAFF_FIELDS = ("responsible_staff", "medical_staff")
_PATIENT_FIELDS = ("patient", "patient_index")
_ENCOUNTER_FIELDS = ("encounter", "encounter_index")
_SCOPE_FIELDS = (
	*_ORGANIZATION_FIELDS,
	*_STAFF_FIELDS,
	*_PATIENT_FIELDS,
	*_ENCOUNTER_FIELDS,
)
_REFERENCE_FIELDS = {
	"campus": ("IONE Hospital Campus", ("hospital",)),
	"department": ("IONE Medical Department", ("hospital", "campus")),
	"ward": ("IONE Ward", _ORGANIZATION_FIELDS[:-1]),
	"responsible_staff": (
		"IONE Medical Staff",
		(*_ORGANIZATION_FIELDS,),
	),
	"medical_staff": (
		"IONE Medical Staff",
		(*_ORGANIZATION_FIELDS,),
	),
}
_TASK_SCOPE_FIELDS = (
	*_ORGANIZATION_FIELDS,
	"patient",
	"encounter",
	"responsible_staff",
)
_AI_TASK_CHILDREN = {
	"IONE AI Candidate Finding": ("task", False),
	"IONE AI Data Access Log": ("task", True),
	"IONE AI Execution Event": ("task", False),
	"IONE AI Report Draft": ("task", False),
	"IONE AI Report Recovery Authorization": ("prior_task", False),
	"IONE AI Tool Approval": ("task", False),
	"IONE Flow Run Link": ("task", False),
	"IONE Agent Analysis Fact": ("analysis_task", False),
}
_IDENTITY_FIELDS = (
	"tenant_hospital",
	"source_system",
	"source_namespace",
	"source_patient_id",
	"source_encounter_id",
	"patient_key",
	"encounter_key",
)
_AUDIT_FIELDS = tuple(
	dict.fromkeys(
		(
			*_SCOPE_FIELDS,
			*_IDENTITY_FIELDS,
			"task",
			"analysis_task",
		)
	)
)


def validate_scope_hierarchy(doc, method: str | None = None) -> None:
	"""Reject cross-organization and cross-tenant records before they are written."""
	del method
	if str(_value(doc, "doctype") or "") not in CLINICAL_SCOPE_DOCTYPES:
		return
	errors = clinical_scope_errors(doc)
	if errors:
		frappe.throw(
			"Clinical scope hierarchy is inconsistent: " + ", ".join(errors),
			frappe.ValidationError,
		)


def clinical_scope_errors(doc) -> tuple[str, ...]:
	"""Return stable, non-sensitive reason codes for an invalid clinical scope tuple."""
	return _clinical_scope_errors(doc, visited=frozenset())


def _clinical_scope_errors(
	doc,
	*,
	visited: frozenset[tuple[str, str]],
) -> tuple[str, ...]:
	doctype = _text(_value(doc, "doctype"))
	if doctype not in CLINICAL_SCOPE_DOCTYPES:
		return ()
	name = _text(_value(doc, "name"))
	identity = (doctype, name)
	if name and identity in visited:
		return ()
	visited = visited | ({identity} if name else set())

	if doctype in {"IONE Patient Index", "IONE Encounter Index"}:
		errors = list(_index_identity_errors(doctype, doc))
	else:
		errors = []

	values = {fieldname: _text(_value(doc, fieldname)) for fieldname in _SCOPE_FIELDS}
	hospital_field = "tenant_hospital" if doctype == "IONE Patient Index" else "hospital"
	hospital = _text(_value(doc, hospital_field))
	if _has_field(doc, hospital_field):
		if not hospital and doctype in {"IONE Patient Index", "IONE Encounter Index"}:
			errors.append("HOSPITAL_REQUIRED")
		elif hospital and not _record_exists("IONE Hospital", hospital):
			errors.append("HOSPITAL_MISSING")

	for fieldname, (linked_doctype, parent_fields) in _REFERENCE_FIELDS.items():
		reference = values[fieldname]
		if not reference or not _has_field(doc, fieldname):
			continue
		parent = _linked_scope(linked_doctype, reference, parent_fields)
		if parent is None:
			errors.append(f"{fieldname.upper()}_MISSING")
			continue
		for parent_field in parent_fields:
			if not _has_field(doc, parent_field):
				continue
			expected = values[parent_field]
			if _text(parent.get(parent_field)) != expected:
				errors.append(f"{fieldname.upper()}_{parent_field.upper()}_MISMATCH")

	encounter_field = _first_field(doc, *_ENCOUNTER_FIELDS)
	patient_field = _first_field(doc, *_PATIENT_FIELDS)
	encounter = values[encounter_field] if encounter_field else ""
	patient = values[patient_field] if patient_field else ""
	if doctype != "IONE Encounter Index" and encounter:
		errors.extend(
			_encounter_reference_errors(
				doc,
				doctype=doctype,
				encounter=encounter,
				patient_field=patient_field,
				visited=visited,
			)
		)
	if doctype not in {"IONE Patient Index", "IONE Encounter Index"} and patient:
		errors.extend(
			_patient_reference_errors(
				doc,
				doctype=doctype,
				patient=patient,
				has_explicit_encounter=bool(encounter),
			)
		)

	child_spec = _AI_TASK_CHILDREN.get(doctype)
	if child_spec:
		errors.extend(
			_ai_task_lineage_errors(
				doc,
				task_field=child_spec[0],
				allow_narrower_patient=child_spec[1],
				visited=visited,
			)
		)

	return tuple(dict.fromkeys(errors))


def scope_integrity_sql(doctype: str, table: str) -> str:
	"""Build a fail-closed SQL predicate that quarantines legacy inconsistent rows."""
	return _scope_integrity_sql(doctype, table, visited=frozenset())


def _scope_integrity_sql(
	doctype: str,
	table: str,
	*,
	visited: frozenset[str],
) -> str:
	if doctype not in CLINICAL_SCOPE_DOCTYPES or not _doctype_exists(doctype):
		return "1=1"
	if doctype in visited:
		return "1=1"
	visited = visited | {doctype}
	meta = frappe.get_meta(doctype)
	clauses: list[str] = []

	if doctype == "IONE Patient Index":
		clauses.append(_patient_identity_sql(table))
	elif doctype == "IONE Encounter Index":
		clauses.append(_encounter_identity_sql(table))

	hospital_field = "tenant_hospital" if doctype == "IONE Patient Index" else "hospital"
	if meta.has_field(hospital_field):
		clauses.append(
			f"({table}.{hospital_field} is not null and {table}.{hospital_field} != '' "  # noqa: S608
			f"and exists (select 1 from `tabIONE Hospital` scope_hospital "
			f"where scope_hospital.name = {table}.{hospital_field}))"
			if doctype in {"IONE Patient Index", "IONE Encounter Index"}
			else (
				f"(({table}.{hospital_field} is null or {table}.{hospital_field} = '') "  # noqa: S608
				f"or exists (select 1 from `tabIONE Hospital` scope_hospital "
				f"where scope_hospital.name = {table}.{hospital_field}))"
			)
		)

	for fieldname, (linked_doctype, parent_fields) in _REFERENCE_FIELDS.items():
		if not meta.has_field(fieldname):
			continue
		comparisons = [
			_sql_exact_equal(table, parent_field, f"scope_{fieldname}.{parent_field}")
			for parent_field in parent_fields
			if meta.has_field(parent_field)
		]
		clauses.append(
			_sql_reference_exists(
				table,
				fieldname,
				linked_doctype,
				f"scope_{fieldname}",
				comparisons,
			)
		)

	if doctype not in {"IONE Patient Index", "IONE Encounter Index"}:
		encounter_field = _first_meta_field(meta, *_ENCOUNTER_FIELDS)
		patient_field = _first_meta_field(meta, *_PATIENT_FIELDS)
		if encounter_field:
			clauses.append(
				_encounter_reference_sql(
					doctype,
					table,
					meta,
					encounter_field,
					patient_field,
				)
			)
		if patient_field:
			clauses.append(
				_patient_reference_sql(
					doctype,
					table,
					meta,
					patient_field,
					encounter_field,
				)
			)

	child_spec = _AI_TASK_CHILDREN.get(doctype)
	if child_spec:
		clauses.append(
			_ai_task_lineage_sql(
				doctype,
				table,
				meta,
				task_field=child_spec[0],
				allow_narrower_patient=child_spec[1],
				visited=visited,
			)
		)

	return "(" + " and ".join(f"({clause})" for clause in clauses) + ")" if clauses else "1=1"


def audit_scope_integrity(batch_size: int = 200) -> dict[str, int | bool]:
	"""Audit bounded pages; permission predicates quarantine every invalid row immediately."""
	limit = min(max(int(batch_size or 200), 1), 1_000)
	doctypes = [doctype for doctype in sorted(CLINICAL_SCOPE_DOCTYPES) if _doctype_exists(doctype)]
	if not doctypes:
		return {"scanned": 0, "quarantined": 0, "has_more": False}
	page_size = max(1, limit // min(len(doctypes), 10))
	scanned = 0
	quarantined = 0
	has_more = False
	for doctype in doctypes:
		meta = frappe.get_meta(doctype)
		fields = ["name", *[fieldname for fieldname in _AUDIT_FIELDS if meta.has_field(fieldname)]]
		cursor_key = f"ione_qms:scope_integrity_cursor:{doctype}"
		cursor = frappe.cache.get_value(cursor_key) or ""
		if isinstance(cursor, bytes):
			cursor = cursor.decode()
		rows = frappe.get_all(
			doctype,
			filters={"name": [">", str(cursor)]} if cursor else None,
			fields=fields,
			order_by="name asc",
			limit_page_length=page_size,
		)
		if not rows:
			if cursor:
				frappe.cache.delete_value(cursor_key)
			continue
		has_more = has_more or len(rows) >= page_size
		frappe.cache.set_value(
			cursor_key,
			str(_value(rows[-1], "name") or ""),
			expires_in_sec=604_800,
		)
		for row in rows:
			scanned += 1
			record = frappe._dict({"doctype": doctype, **dict(row)})
			errors = clinical_scope_errors(record)
			if not errors:
				continue
			quarantined += 1
			_log_scope_integrity_issue(doctype, str(_value(row, "name") or ""), errors)
	return {
		"scanned": scanned,
		"quarantined": quarantined,
		"has_more": has_more,
	}


def _index_identity_errors(doctype: str, doc) -> tuple[str, ...]:
	if doctype == "IONE Patient Index":
		hospital = _text(_value(doc, "tenant_hospital"))
		source_id = _text(_value(doc, "source_patient_id"))
		key_field = "patient_key"
	else:
		hospital = _text(_value(doc, "hospital"))
		source_id = _text(_value(doc, "source_encounter_id"))
		key_field = "encounter_key"
	source_system = _text(_value(doc, "source_system"))
	namespace = _text(_value(doc, "source_namespace"))
	errors: list[str] = []
	if not hospital:
		errors.append("TENANT_HOSPITAL_REQUIRED")
	if not source_system:
		errors.append("SOURCE_SYSTEM_REQUIRED")
	if not namespace:
		errors.append("SOURCE_NAMESPACE_REQUIRED")
	if not source_id:
		errors.append("SOURCE_RECORD_ID_REQUIRED")
	source = (
		_linked_scope("IONE Source System", source_system, ("identity_namespace",)) if source_system else None
	)
	if source is None:
		errors.append("SOURCE_SYSTEM_MISSING")
	elif _text(source.get("identity_namespace")) != namespace:
		errors.append("SOURCE_NAMESPACE_MISMATCH")
	if not (
		namespace
		and hospital
		and source_id
		and verify_stored_identity_key(
			"patient" if doctype == "IONE Patient Index" else "encounter",
			_value(doc, key_field),
			_value(doc, "identity_key_contract"),
			namespace,
			hospital,
			source_id,
		)
	):
		errors.append(f"{key_field.upper()}_MISMATCH")

	if doctype == "IONE Encounter Index":
		patient = _text(_value(doc, "patient"))
		if patient:
			row = _linked_scope(
				"IONE Patient Index",
				patient,
				(
					"name",
					"patient_key",
					"identity_key_contract",
					"tenant_hospital",
					"source_system",
					"source_namespace",
					"source_patient_id",
				),
			)
			if row is None:
				errors.append("PATIENT_MISSING")
			else:
				patient_doc = {"doctype": "IONE Patient Index", **dict(row)}
				errors.extend(
					f"PATIENT_{code}" for code in _index_identity_errors("IONE Patient Index", patient_doc)
				)
				if _text(row.get("tenant_hospital")) != hospital:
					errors.append("PATIENT_HOSPITAL_MISMATCH")
				if _text(row.get("source_system")) != source_system:
					errors.append("PATIENT_SOURCE_SYSTEM_MISMATCH")
				if _text(row.get("source_namespace")) != namespace:
					errors.append("PATIENT_SOURCE_NAMESPACE_MISMATCH")
	return tuple(dict.fromkeys(errors))


def _encounter_reference_errors(
	doc,
	*,
	doctype: str,
	encounter: str,
	patient_field: str | None,
	visited: frozenset[tuple[str, str]],
) -> tuple[str, ...]:
	fields = (
		"name",
		"encounter_key",
		"identity_key_contract",
		"source_system",
		"source_namespace",
		"source_encounter_id",
		"patient",
		*_ORGANIZATION_FIELDS,
		*_STAFF_FIELDS,
	)
	parent = _linked_scope("IONE Encounter Index", encounter, fields)
	if parent is None:
		return ("ENCOUNTER_MISSING",)
	errors = [
		f"ENCOUNTER_{code}"
		for code in _clinical_scope_errors(
			{"doctype": "IONE Encounter Index", **dict(parent)},
			visited=visited,
		)
	]
	allow_narrower = doctype == "IONE AI Data Access Log"
	comparisons: list[tuple[str, str]] = []
	if patient_field:
		comparisons.append((patient_field, "patient"))
	for fieldname in (*_ORGANIZATION_FIELDS, *_STAFF_FIELDS):
		if _has_field(doc, fieldname):
			comparisons.append((fieldname, fieldname))
	for record_field, encounter_field in comparisons:
		expected = _text(_value(doc, record_field))
		actual = _text(parent.get(encounter_field))
		if allow_narrower and record_field not in {"hospital"} and not expected:
			continue
		if expected != actual:
			errors.append(f"ENCOUNTER_{encounter_field.upper()}_MISMATCH")
	return tuple(errors)


def _patient_reference_errors(
	doc,
	*,
	doctype: str,
	patient: str,
	has_explicit_encounter: bool,
) -> tuple[str, ...]:
	fields = (
		"name",
		"patient_key",
		"identity_key_contract",
		"tenant_hospital",
		"source_system",
		"source_namespace",
		"source_patient_id",
	)
	parent = _linked_scope("IONE Patient Index", patient, fields)
	if parent is None:
		return ("PATIENT_MISSING",)
	errors = [
		f"PATIENT_{code}"
		for code in _index_identity_errors(
			"IONE Patient Index",
			{"doctype": "IONE Patient Index", **dict(parent)},
		)
	]
	if _has_field(doc, "hospital") and _text(_value(doc, "hospital")) != _text(parent.get("tenant_hospital")):
		errors.append("PATIENT_HOSPITAL_MISMATCH")
	for fieldname in ("source_system", "source_namespace"):
		if _has_field(doc, fieldname) and _text(_value(doc, fieldname)) != _text(parent.get(fieldname)):
			errors.append(f"PATIENT_{fieldname.upper()}_MISMATCH")
	if not has_explicit_encounter and any(
		_text(_value(doc, fieldname))
		for fieldname in ("campus", "department", "ward", *_STAFF_FIELDS)
		if _has_field(doc, fieldname)
	):
		if not _patient_has_consistent_encounter(doc, patient):
			errors.append("PATIENT_SCOPE_UNANCHORED")
	return tuple(errors)


def _ai_task_lineage_errors(
	doc,
	*,
	task_field: str,
	allow_narrower_patient: bool,
	visited: frozenset[tuple[str, str]],
) -> tuple[str, ...]:
	task_name = _text(_value(doc, task_field))
	if not task_name:
		return ("AI_TASK_REQUIRED",)
	task = _linked_scope(
		"IONE AI Analysis Task",
		task_name,
		("name", *_TASK_SCOPE_FIELDS),
	)
	if task is None:
		return ("AI_TASK_MISSING",)
	errors = [
		f"AI_TASK_{code}"
		for code in _clinical_scope_errors(
			{"doctype": "IONE AI Analysis Task", **dict(task)},
			visited=visited,
		)
	]
	for fieldname in _TASK_SCOPE_FIELDS:
		if not _has_field(doc, fieldname):
			continue
		child_value = _text(_value(doc, fieldname))
		task_value = _text(task.get(fieldname))
		if allow_narrower_patient and fieldname in {"patient", "encounter"}:
			if task_value and child_value != task_value:
				errors.append(f"AI_TASK_{fieldname.upper()}_MISMATCH")
			continue
		if child_value != task_value:
			errors.append(f"AI_TASK_{fieldname.upper()}_MISMATCH")
	return tuple(errors)


def _patient_has_consistent_encounter(doc, patient: str) -> bool:
	filters: dict[str, Any] = {"patient": patient}
	for fieldname in (*_ORGANIZATION_FIELDS, *_STAFF_FIELDS):
		if _has_field(doc, fieldname) and _text(_value(doc, fieldname)):
			filters[fieldname] = _text(_value(doc, fieldname))
	conditions = [f"scope_anchor.{fieldname} = %({fieldname})s" for fieldname in filters]
	integrity = scope_integrity_sql("IONE Encounter Index", "scope_anchor")
	return bool(
		frappe.db.sql(
			"select scope_anchor.name from `tabIONE Encounter Index` scope_anchor "  # noqa: S608
			f"where {' and '.join(conditions)} and ({integrity}) limit 1",
			filters,
		)
	)


def _patient_identity_sql(table: str) -> str:
	return (
		f"{table}.tenant_hospital is not null and {table}.tenant_hospital != '' "  # noqa: S608
		f"and {table}.source_system is not null and {table}.source_system != '' "
		f"and {table}.source_namespace is not null and {table}.source_namespace != '' "
		f"and {table}.source_patient_id is not null and {table}.source_patient_id != '' "
		f"and {_identity_contract_sql(table, 'patient_key')} "
		"and exists (select 1 from `tabIONE Source System` scope_patient_source "
		f"where scope_patient_source.name = {table}.source_system "
		f"and binary scope_patient_source.identity_namespace = binary {table}.source_namespace)"
	)


def _encounter_identity_sql(table: str) -> str:
	patient = "scope_encounter_patient"
	return (
		f"{table}.hospital is not null and {table}.hospital != '' "  # noqa: S608
		f"and {table}.source_system is not null and {table}.source_system != '' "
		f"and {table}.source_namespace is not null and {table}.source_namespace != '' "
		f"and {table}.source_encounter_id is not null and {table}.source_encounter_id != '' "
		f"and {_identity_contract_sql(table, 'encounter_key')} "
		"and exists (select 1 from `tabIONE Source System` scope_encounter_source "
		f"where scope_encounter_source.name = {table}.source_system "
		f"and binary scope_encounter_source.identity_namespace = binary {table}.source_namespace) "
		f"and (({table}.patient is null or {table}.patient = '') "
		f"or exists (select 1 from `tabIONE Patient Index` {patient} "
		f"where {patient}.name = {table}.patient "
		f"and {patient}.tenant_hospital = {table}.hospital "
		f"and {patient}.source_system = {table}.source_system "
		f"and binary {patient}.source_namespace = binary {table}.source_namespace "
		f"and ({_patient_identity_sql(patient)})))"
	)


def _identity_contract_sql(table: str, key_field: str) -> str:
	contracts = ", ".join(frappe.db.escape(contract) for contract in acceptable_identity_key_contracts())
	return (
		f"binary {table}.identity_key_contract in ({contracts}) "
		f"and {table}.{key_field} regexp '^[0-9a-f]{{64}}$'"
	)


def _encounter_reference_sql(
	doctype: str,
	table: str,
	meta,
	encounter_field: str,
	patient_field: str | None,
) -> str:
	alias = "scope_record_encounter"
	allow_narrower = doctype == "IONE AI Data Access Log"
	comparisons: list[str] = []
	if patient_field:
		comparisons.append(
			_sql_narrowable_equal(
				table,
				patient_field,
				f"{alias}.patient",
				allow_blank=allow_narrower,
			)
		)
	for fieldname in (*_ORGANIZATION_FIELDS, *_STAFF_FIELDS):
		if not meta.has_field(fieldname):
			continue
		comparisons.append(
			_sql_narrowable_equal(
				table,
				fieldname,
				f"{alias}.{fieldname}",
				allow_blank=allow_narrower and fieldname != "hospital",
			)
		)
	extra = " and ".join((_encounter_identity_sql(alias), *comparisons))
	return (
		f"(({table}.{encounter_field} is null or {table}.{encounter_field} = '') "  # noqa: S608
		f"or exists (select 1 from `tabIONE Encounter Index` {alias} "
		f"where {alias}.name = {table}.{encounter_field} and {extra}))"
	)


def _patient_reference_sql(
	doctype: str,
	table: str,
	meta,
	patient_field: str,
	encounter_field: str | None,
) -> str:
	del doctype
	alias = "scope_record_patient"
	comparisons = [_patient_identity_sql(alias)]
	if meta.has_field("hospital"):
		comparisons.append(f"{alias}.tenant_hospital = {table}.hospital")
	for fieldname in ("source_system", "source_namespace"):
		if meta.has_field(fieldname):
			operator = "binary " if fieldname == "source_namespace" else ""
			comparisons.append(f"{operator}{alias}.{fieldname} = {operator}{table}.{fieldname}")
	patient_exists = (
		f"exists (select 1 from `tabIONE Patient Index` {alias} "  # noqa: S608
		f"where {alias}.name = {table}.{patient_field} "
		f"and {' and '.join(f'({item})' for item in comparisons)})"
	)
	clauses = [f"(({table}.{patient_field} is null or {table}.{patient_field} = '') or ({patient_exists}))"]
	lower_fields = [
		fieldname
		for fieldname in ("campus", "department", "ward", *_STAFF_FIELDS)
		if meta.has_field(fieldname)
	]
	if lower_fields:
		lower_present = " or ".join(
			f"({table}.{fieldname} is not null and {table}.{fieldname} != '')" for fieldname in lower_fields
		)
		anchor_alias = "scope_patient_encounter"
		anchor_matches = [
			f"{anchor_alias}.patient = {table}.{patient_field}",
			_encounter_identity_sql(anchor_alias),
			*[
				f"{anchor_alias}.{fieldname} = {table}.{fieldname}"
				for fieldname in lower_fields
				if meta.has_field(fieldname)
			],
		]
		if meta.has_field("hospital"):
			anchor_matches.append(f"{anchor_alias}.hospital = {table}.hospital")
		no_explicit_encounter = (
			f"({table}.{encounter_field} is null or {table}.{encounter_field} = '')"
			if encounter_field
			else "1=1"
		)
		clauses.append(
			f"(({table}.{patient_field} is null or {table}.{patient_field} = '') "  # noqa: S608
			f"or not ({no_explicit_encounter}) or not ({lower_present}) "
			f"or exists (select 1 from `tabIONE Encounter Index` {anchor_alias} "
			f"where {' and '.join(f'({item})' for item in anchor_matches)}))"
		)
	return " and ".join(f"({clause})" for clause in clauses)


def _ai_task_lineage_sql(
	doctype: str,
	table: str,
	meta,
	*,
	task_field: str,
	allow_narrower_patient: bool,
	visited: frozenset[str],
) -> str:
	del doctype
	alias = "scope_ai_task"
	comparisons = [
		_scope_integrity_sql("IONE AI Analysis Task", alias, visited=visited),
	]
	task_meta = frappe.get_meta("IONE AI Analysis Task")
	for fieldname in _TASK_SCOPE_FIELDS:
		if not meta.has_field(fieldname) or not task_meta.has_field(fieldname):
			continue
		if allow_narrower_patient and fieldname in {"patient", "encounter"}:
			comparisons.append(
				f"(({alias}.{fieldname} is null or {alias}.{fieldname} = '') "
				f"or {table}.{fieldname} = {alias}.{fieldname})"
			)
		else:
			comparisons.append(f"coalesce({table}.{fieldname}, '') = coalesce({alias}.{fieldname}, '')")
	return (
		f"({table}.{task_field} is not null and {table}.{task_field} != '' "  # noqa: S608
		f"and exists (select 1 from `tabIONE AI Analysis Task` {alias} "
		f"where {alias}.name = {table}.{task_field} "
		f"and {' and '.join(f'({item})' for item in comparisons)}))"
	)


def _log_scope_integrity_issue(
	doctype: str,
	name: str,
	errors: tuple[str, ...],
) -> None:
	reason = ",".join(errors)
	cache_key = f"ione_qms:scope_integrity_issue:{doctype}:{name}:{reason}"
	if frappe.cache.get_value(cache_key):
		return
	frappe.cache.set_value(cache_key, 1, expires_in_sec=86_400)
	frappe.log_error(
		title="IONE clinical scope record quarantined",
		message=(
			f"{doctype} {name} failed scope integrity checks ({reason}). "
			"Permission predicates keep it inaccessible until its hierarchy is repaired. "
			"No clinical content was copied into this log."
		),
		reference_doctype=doctype,
		reference_name=name,
	)


def _linked_scope(
	doctype: str,
	name: str,
	fields: tuple[str, ...],
) -> Mapping[str, Any] | None:
	result = frappe.db.get_value(doctype, name, list(fields), as_dict=True)
	if not result:
		return None
	if isinstance(result, Mapping):
		return result
	return {fieldname: getattr(result, fieldname, None) for fieldname in fields}


def _record_exists(doctype: str, name: str) -> bool:
	try:
		return bool(frappe.db.exists(doctype, name))
	except Exception:
		return False


def _sql_reference_exists(
	table: str,
	fieldname: str,
	linked_doctype: str,
	alias: str,
	comparisons: list[str],
) -> str:
	extra = f" and {' and '.join(comparisons)}" if comparisons else ""
	return (
		f"(({table}.{fieldname} is null or {table}.{fieldname} = '') "  # noqa: S608
		f"or exists (select 1 from `tab{linked_doctype}` {alias} "
		f"where {alias}.name = {table}.{fieldname}{extra}))"
	)


def _sql_exact_equal(table: str, fieldname: str, reference: str) -> str:
	return f"coalesce({table}.{fieldname}, '') = coalesce({reference}, '')"


def _sql_narrowable_equal(
	table: str,
	fieldname: str,
	reference: str,
	*,
	allow_blank: bool,
) -> str:
	if allow_blank:
		return (
			f"({table}.{fieldname} is null or {table}.{fieldname} = '' or {reference} = {table}.{fieldname})"
		)
	return _sql_exact_equal(table, fieldname, reference)


def _first_field(doc, *fieldnames: str) -> str | None:
	return next((fieldname for fieldname in fieldnames if _has_field(doc, fieldname)), None)


def _first_meta_field(meta, *fieldnames: str) -> str | None:
	return next((fieldname for fieldname in fieldnames if meta.has_field(fieldname)), None)


def _has_field(doc, fieldname: str) -> bool:
	meta = getattr(doc, "meta", None)
	if meta is not None and hasattr(meta, "has_field"):
		return bool(meta.has_field(fieldname))
	if isinstance(doc, Mapping):
		return fieldname in doc
	return hasattr(doc, fieldname)


def _value(doc, fieldname: str):
	if isinstance(doc, Mapping):
		return doc.get(fieldname)
	getter = getattr(doc, "get", None)
	if callable(getter):
		return getter(fieldname)
	return getattr(doc, fieldname, None)


def _text(value: Any) -> str:
	return str(value or "").strip()


def _doctype_exists(doctype: str) -> bool:
	try:
		return bool(frappe.db.exists("DocType", doctype))
	except Exception:
		return False
