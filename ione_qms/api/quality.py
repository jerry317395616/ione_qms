from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

from ione_qms.integration.projections import materialize_event_projection
from ione_qms.permissions import require_role, require_scope_read
from ione_qms.rule_engine.executor import enqueue_event_rules, evaluate_event
from ione_qms.services.integration_scope import source_scope_policy_checksum
from ione_qms.services.projections import system_audit_identity
from ione_qms.services.runtime_settings import require_realtime_rules_enabled


@frappe.whitelist(methods=["POST"])
def evaluate_encounter(
	encounter: str,
	rule_codes: list[str] | str | None = None,
	request_id: str | None = None,
) -> dict[str, Any]:
	if frappe.session.user in {"", "Guest", "Administrator", None}:
		frappe.throw(
			"Manual clinical rule evaluation requires a named accountable user.",
			frappe.PermissionError,
		)
	require_realtime_rules_enabled()
	require_role(
		"IONE Physician",
		"IONE Department Director",
		"IONE Department QC Officer",
		"IONE QC Reviewer",
		"IONE Medical Affairs",
	)
	encounter_doc = frappe.get_doc("IONE Encounter Index", encounter)
	encounter_doc.check_permission("read")
	payload = {
		"encounter": {
			"encounter_key": encounter_doc.get("encounter_key"),
			"encounter_type": encounter_doc.get("encounter_type"),
			"status": encounter_doc.get("status"),
			"admission_time": encounter_doc.get("admission_time"),
			"discharge_time": encounter_doc.get("discharge_time"),
		}
	}
	event_time = now_datetime()
	idempotency_key = hashlib.sha256(
		f"manual|{encounter}|{request_id or event_time.isoformat()}".encode()
	).hexdigest()
	existing_event = frappe.db.get_value(
		"IONE Clinical Quality Event",
		{"idempotency_key": idempotency_key},
		"name",
	)
	if existing_event:
		results = evaluate_event(existing_event, rule_codes=rule_codes, save=True)
		return {"event": existing_event, "results": results, "duplicate": True}
	event = frappe.get_doc(
		{
			"doctype": "IONE Clinical Quality Event",
			"event_type": "ManualEncounterEvaluation",
			"event_time": event_time,
			"source_system": encounter_doc.get("source_system"),
			"source_namespace": encounter_doc.get("source_namespace"),
			"scope_policy_hash": source_scope_policy_checksum(
				frappe.get_cached_doc("IONE Source System", encounter_doc.get("source_system"))
			),
			"source_record_type": "IONE Encounter Index",
			"source_record_id": encounter,
			"source_version": str(encounter_doc.modified),
			"patient_index": encounter_doc.get("patient"),
			"encounter_index": encounter_doc.name,
			"hospital": encounter_doc.get("hospital"),
			"campus": encounter_doc.get("campus"),
			"department": encounter_doc.get("department"),
			"ward": encounter_doc.get("ward"),
			"responsible_staff": encounter_doc.get("responsible_staff"),
			"medical_staff": encounter_doc.get("medical_staff"),
			"payload_json": json.dumps(payload, ensure_ascii=False, default=str),
			"idempotency_key": idempotency_key,
			"processing_status": "Pending",
		}
	)
	event.flags.skip_rule_enqueue = True
	event.flags.ione_internal_clinical_event = True
	event.insert(ignore_permissions=True)
	results = evaluate_event(event.name, rule_codes=rule_codes, save=True)
	return {"event": event.name, "results": results, "duplicate": False}


@frappe.whitelist(methods=["GET"])
def get_patient_alerts(
	patient: str | None = None,
	encounter: str | None = None,
	limit: int = 100,
) -> list[dict[str, Any]]:
	if not patient and not encounter:
		frappe.throw("patient or encounter is required")
	if patient:
		frappe.get_doc("IONE Patient Index", patient).check_permission("read")
	if encounter:
		frappe.get_doc("IONE Encounter Index", encounter).check_permission("read")
	limit = min(max(int(limit or 100), 1), 500)
	filters: dict[str, Any] = {
		"status": [
			"not in",
			["Closed", "Appeal Approved"],
		],
	}
	if patient:
		filters["patient"] = patient
	if encounter:
		filters["encounter"] = encounter
	return frappe.get_list(
		"IONE QC Finding",
		filters=filters,
		fields=[
			"name",
			"title",
			"severity",
			"status",
			"department",
			"due_date",
			"rule",
			"rule_version",
			"detected_at",
		],
		order_by="severity desc, detected_at desc",
		limit_page_length=limit,
	)


@frappe.whitelist(methods=["POST"])
def report_medical_safety_event(
	request_id: str,
	event_time: str,
	event_type: str,
	severity: str,
	narrative: str,
	department: str,
	hospital: str | None = None,
	campus: str | None = None,
	ward: str | None = None,
	patient: str | None = None,
	encounter: str | None = None,
	immediate_action: str | None = None,
	anonymous: int | bool = 0,
	near_miss: int | bool = 0,
) -> dict[str, Any]:
	"""Submit a confidential safety event through a governed, idempotent portal path."""
	if frappe.session.user in {"Guest", "Administrator"}:
		frappe.throw(
			"Safety reports require a named accountable staff session; Administrator is not eligible.",
			frappe.PermissionError,
		)
	require_role(
		"IONE Physician",
		"IONE Department Director",
		"IONE Department QC Officer",
		"IONE Nursing/Pharmacy/IC QC",
		"IONE QC Reviewer",
		"IONE Medical Affairs",
	)
	request_id = str(request_id or "").strip()
	if not 8 <= len(request_id) <= 128:
		frappe.throw("request_id must contain 8-128 characters")
	event_type = str(event_type or "").strip()
	narrative = str(narrative or "").strip()
	immediate_action = str(immediate_action or "").strip()
	if not 2 <= len(event_type) <= 200:
		frappe.throw("Safety event type must contain 2-200 characters")
	if not 10 <= len(narrative) <= 20_000:
		frappe.throw("Safety event narrative must contain 10-20,000 characters")
	if len(immediate_action) > 20_000:
		frappe.throw("Immediate action exceeds 20,000 characters")
	if severity not in {"Low", "Medium", "High", "Critical"}:
		frappe.throw("Invalid safety event severity")
	occurred_at = get_datetime(event_time)
	now = now_datetime()
	if occurred_at > add_to_date(now, minutes=5) or occurred_at < add_to_date(now, years=-2):
		frappe.throw("Safety event time must be within the past two years and not in the future")
	scope = _safety_report_scope(
		hospital=hospital,
		campus=campus,
		department=department,
		ward=ward,
		patient=patient,
		encounter=encounter,
	)
	require_scope_read(
		hospital=scope["hospital"],
		campus=scope["campus"],
		department=scope["department"],
		ward=scope["ward"],
		include_personal=True,
	)
	anonymous_report = _as_boolean_flag(anonymous, "anonymous")
	near_miss_flag = _as_boolean_flag(near_miss, "near_miss")
	report_scope = {
		fieldname: scope.get(fieldname)
		for fieldname in ("hospital", "campus", "department", "ward", "patient", "encounter")
	}
	payload = {
		"projection_type": "Medical Safety Event",
		"safety_event_type": event_type,
		"safety_event_time": occurred_at,
		"severity": severity,
		"narrative": narrative,
		"immediate_action": immediate_action or None,
		"anonymous_report": anonymous_report,
		"near_miss": near_miss_flag,
		"report_channel": "Anonymous Portal" if anonymous_report else "Staff Portal",
		"report_scope": report_scope,
	}
	projection_payload = {
		**payload,
		"reported_by": None if anonymous_report else frappe.session.user,
		# Exact submission time can re-identify an anonymous reporter through
		# infrastructure logs. The incident occurrence time remains available.
		"reported_at": None if anonymous_report else now,
	}
	payload_json = _canonical_json(payload)
	idempotency_key = _safety_report_idempotency_key(
		request_id=request_id,
		reporter=frappe.session.user,
		anonymous=bool(anonymous_report),
	)
	existing_event = frappe.db.get_value(
		"IONE Clinical Quality Event",
		{"idempotency_key": idempotency_key},
		["name", "payload_json"],
		as_dict=True,
	)
	if existing_event:
		_assert_same_safety_payload(existing_event.payload_json, payload_json)
		safety_event = frappe.db.get_value(
			"IONE Medical Safety Event",
			{"clinical_event": existing_event.name},
			"name",
		)
		if not safety_event:
			frappe.throw(
				"The existing safety report is incomplete; contact Medical Affairs before retrying.",
				frappe.ValidationError,
			)
		return {"event": existing_event.name, "safety_event": safety_event, "duplicate": True}
	try:
		source_system = _source_system_for_scope(scope)
		source_namespace = frappe.db.get_value(
			"IONE Source System",
			source_system,
			"identity_namespace",
		)
		source_doc = frappe.get_cached_doc("IONE Source System", source_system)
		# Frappe overwrites owner and modified_by during insert. All portal
		# reports therefore persist under a technical audit identity; an
		# identified reporter exists only in the restricted projection field.
		with system_audit_identity():
			event = frappe.get_doc(
				{
					"doctype": "IONE Clinical Quality Event",
					"event_type": "SafetyEvent.Reported",
					"event_time": occurred_at,
					"source_system": source_system,
					"source_namespace": source_namespace,
					"scope_policy_hash": source_scope_policy_checksum(source_doc),
					"source_record_type": "MedicalSafetyReport",
					"source_record_id": idempotency_key,
					"source_version": "1",
					"patient_index": scope["patient"],
					"encounter_index": scope["encounter"],
					"hospital": scope["hospital"],
					"campus": scope["campus"],
					"department": scope["department"],
					"ward": scope["ward"],
					"responsible_staff": scope["responsible_staff"],
					"payload_json": payload_json,
					"idempotency_key": idempotency_key,
					"processing_status": "Pending",
				}
			)
			event.flags.skip_rule_enqueue = True
			event.flags.ione_internal_clinical_event = True
			event.insert(ignore_permissions=True)
			safety_event = materialize_event_projection(event.name, event.event_type, projection_payload)
			if not safety_event:
				raise frappe.ValidationError("Safety report projection was not created")
			event.flags.skip_rule_enqueue = False
			enqueue_event_rules(event)
	except frappe.DuplicateEntryError:
		existing_event = frappe.db.get_value(
			"IONE Clinical Quality Event",
			{"idempotency_key": idempotency_key},
			["name", "payload_json"],
			as_dict=True,
		)
		if not existing_event:
			raise
		_assert_same_safety_payload(existing_event.payload_json, payload_json)
		safety_event = frappe.db.get_value(
			"IONE Medical Safety Event",
			{"clinical_event": existing_event.name},
			"name",
		)
		if not safety_event:
			frappe.throw(
				"The concurrent safety report is incomplete; contact Medical Affairs before retrying.",
				frappe.ValidationError,
			)
		return {"event": existing_event.name, "safety_event": safety_event, "duplicate": True}
	return {"event": event.name, "safety_event": safety_event, "duplicate": False}


@frappe.whitelist(methods=["POST"])
def report_safety_event(
	request_id: str,
	event_time: str,
	event_type: str,
	severity: str,
	narrative: str,
	department: str,
	hospital: str | None = None,
	campus: str | None = None,
	ward: str | None = None,
	patient: str | None = None,
	encounter: str | None = None,
	immediate_action: str | None = None,
	anonymous: int | bool = 0,
	near_miss: int | bool = 0,
) -> dict[str, Any]:
	"""Compatibility endpoint for the governed medical safety reporting page."""
	return report_medical_safety_event(
		request_id=request_id,
		event_time=event_time,
		event_type=event_type,
		severity=severity,
		narrative=narrative,
		department=department,
		hospital=hospital,
		campus=campus,
		ward=ward,
		patient=patient,
		encounter=encounter,
		immediate_action=immediate_action,
		anonymous=anonymous,
		near_miss=near_miss,
	)


def _safety_report_scope(
	*,
	hospital: str | None,
	campus: str | None,
	department: str,
	ward: str | None,
	patient: str | None,
	encounter: str | None,
) -> dict[str, Any]:
	scope: dict[str, Any] = {
		"hospital": hospital,
		"campus": campus,
		"department": department,
		"ward": ward,
		"patient": patient,
		"encounter": encounter,
		"responsible_staff": None,
	}
	for doctype, name in (
		("IONE Patient Index", patient),
		("IONE Encounter Index", encounter),
	):
		if not name:
			continue
		doc = frappe.get_doc(doctype, name)
		doc.check_permission("read")
		values = {
			"patient": doc.name if doctype == "IONE Patient Index" else doc.get("patient"),
			"encounter": doc.name if doctype == "IONE Encounter Index" else None,
			"hospital": (
				doc.get("tenant_hospital") if doctype == "IONE Patient Index" else doc.get("hospital")
			),
			"campus": doc.get("campus"),
			"department": doc.get("department"),
			"ward": doc.get("ward"),
			"responsible_staff": doc.get("responsible_staff"),
		}
		for fieldname, value in values.items():
			if value in (None, ""):
				continue
			if scope.get(fieldname) not in (None, "", value):
				frappe.throw(f"Safety report {fieldname} conflicts with the referenced record")
			scope[fieldname] = value
	if not scope["department"]:
		frappe.throw("Safety report requires an explicit department")
	department_scope = frappe.db.get_value(
		"IONE Medical Department",
		scope["department"],
		["hospital", "campus"],
		as_dict=True,
	)
	if not department_scope:
		frappe.throw("Safety report references an unknown department")
	for fieldname in ("hospital", "campus"):
		value = department_scope.get(fieldname)
		if value and scope.get(fieldname) not in (None, "", value):
			frappe.throw(f"Safety report {fieldname} conflicts with its department")
		if value:
			scope[fieldname] = value
	if scope["ward"]:
		ward_scope = frappe.db.get_value(
			"IONE Ward",
			scope["ward"],
			["hospital", "campus", "department"],
			as_dict=True,
		)
		if not ward_scope:
			frappe.throw("Safety report references an unknown ward")
		if not ward_scope.get("department"):
			frappe.throw("Safety report ward is not assigned to a department")
		for fieldname in ("hospital", "campus", "department"):
			value = ward_scope.get(fieldname)
			if value and scope.get(fieldname) not in (None, "", value):
				frappe.throw(f"Safety report {fieldname} conflicts with its ward")
			if value:
				scope[fieldname] = value
	if scope["campus"]:
		campus_hospital = frappe.db.get_value(
			"IONE Hospital Campus",
			scope["campus"],
			"hospital",
		)
		if not campus_hospital:
			frappe.throw("Safety report references an unknown or unscoped campus")
		if scope.get("hospital") != campus_hospital:
			frappe.throw("Safety report hospital conflicts with its campus")
	return scope


def _as_boolean_flag(value: Any, fieldname: str) -> int:
	if value in (True, 1, "1", "true", "True"):
		return 1
	if value in (False, 0, "0", "false", "False", None, ""):
		return 0
	frappe.throw(f"{fieldname} must be a boolean flag")
	return 0


def _canonical_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)


def _safety_report_idempotency_key(*, request_id: str, reporter: str, anonymous: bool) -> str:
	"""Return a stable receipt key without encoding an anonymous principal."""
	principal = "anonymous" if anonymous else f"identified:{reporter}"
	material = _canonical_json(
		{
			"contract": "IONE Medical Safety Report/v1",
			"principal": principal,
			"request_id": request_id,
		}
	)
	return hashlib.sha256(material.encode()).hexdigest()


def _assert_same_safety_payload(existing: str | None, expected: str) -> None:
	try:
		parsed = json.loads(str(existing or ""))
		expected_parsed = json.loads(expected)
	except ValueError:
		parsed = None
		expected_parsed = None
	if isinstance(parsed, dict):
		parsed.pop("reported_at", None)
	if isinstance(expected_parsed, dict):
		expected_parsed.pop("reported_at", None)
	if (
		not isinstance(parsed, dict)
		or not isinstance(expected_parsed, dict)
		or _canonical_json(parsed) != _canonical_json(expected_parsed)
	):
		frappe.local.response["http_status_code"] = 409
		frappe.throw(
			"request_id was reused with different safety report content",
			frappe.DuplicateEntryError,
		)


def _ensure_internal_source_system() -> str:
	code = "IONE-QMS-INTERNAL"
	name = frappe.db.get_value("IONE Source System", {"system_code": code}, "name")
	if name:
		return name
	frappe.throw(
		"The governed IONE QMS internal source system is not configured; "
		"a named Integration Administrator must create and approve it"
	)


def _source_system_for_scope(scope: dict[str, Any]) -> str:
	if scope.get("encounter"):
		source = frappe.db.get_value("IONE Encounter Index", scope["encounter"], "source_system")
		if source:
			return str(source)
	if scope.get("patient"):
		source = frappe.db.get_value("IONE Patient Index", scope["patient"], "source_system")
		if source:
			return str(source)
	return _ensure_internal_source_system()
