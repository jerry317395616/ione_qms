from __future__ import annotations

import json
from typing import Any

import frappe

from ione_qms.services.projections import (
	materialize_medical_record_qc,
	materialize_medical_safety_event,
	materialize_surgery_qc,
)

_PROJECTION_TYPES = frozenset({"Medical Record", "Surgery", "Medical Safety Event"})
_RECORD_STATUSES = frozenset({"Active", "Final", "Archived", "Cancelled"})
_QC_STATUSES = frozenset({"Pending", "Passed", "Failed", "Needs Review"})
_RISK_LEVELS = frozenset({"Low", "Medium", "High", "Critical"})
_SAFETY_STATUSES = frozenset({"Reported", "Under Review", "Confirmed", "Improving", "Closed"})


def materialize_event_projection(event_name: str, event_type: str, payload: dict[str, Any]) -> str | None:
	"""Materialize reviewed clinical projections from a normalized event payload.

	The source contract may explicitly set ``projection_type``. Otherwise only
	reviewed event namespaces are recognized. Unknown clinical events remain in
	the immutable event stream for rules and indicators without creating a
	domain projection.
	"""
	if not isinstance(payload, dict):
		raise frappe.ValidationError("Clinical event payload must be a JSON object")
	projection_type = str(payload.get("projection_type") or "").strip()
	if projection_type and projection_type not in _PROJECTION_TYPES:
		raise frappe.ValidationError("Unsupported clinical projection_type")
	if not projection_type:
		if event_type.startswith("MedicalRecord.") or event_type == "MedicalRecordUpdated":
			projection_type = "Medical Record"
		elif event_type.startswith("Surgery."):
			projection_type = "Surgery"
		elif event_type.startswith("SafetyEvent.") or event_type == "MedicalSafetyEventReported":
			projection_type = "Medical Safety Event"
		else:
			return None
	if projection_type == "Medical Record":
		return materialize_medical_record_qc(_medical_record_values(event_name, payload))
	if projection_type == "Surgery":
		return materialize_surgery_qc(_surgery_values(event_name, event_type, payload))
	return materialize_medical_safety_event(_safety_event_values(event_name, event_type, payload))


def _medical_record_values(event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
	record_type = _required_text(payload, "record_type", 200)
	values: dict[str, Any] = {
		"event": event_name,
		"record_type": record_type,
		"record_status": _choice(payload, "record_status", _RECORD_STATUSES, required=False),
		"completeness_score": _percent(payload, "completeness_score"),
		"timeliness_score": _percent(payload, "timeliness_score"),
		"status": _choice(payload, "qc_status", _QC_STATUSES, required=False) or "Pending",
		"details_json": _details(payload),
	}
	return _without_empty(values)


def _surgery_values(
	event_name: str,
	event_type: str,
	payload: dict[str, Any],
) -> dict[str, Any]:
	phase_by_event = {
		"Surgery.Scheduled": "Scheduled",
		"Surgery.Occurred": "Occurred",
		"Surgery.Performed": "Occurred",
		"Surgery.Completed": "Occurred",
		"Surgery.PostoperativeFinalized": "Postoperative Finalized",
		"Surgery.Cancelled": "Cancelled",
	}
	declared_phase = _choice(
		payload,
		"surgery_phase",
		frozenset({"Scheduled", "Occurred", "Postoperative Finalized", "Cancelled"}),
		required=False,
	)
	event_phase = phase_by_event.get(event_type)
	if not event_phase and not declared_phase:
		raise frappe.ValidationError("Unknown Surgery event type requires an explicit governed surgery_phase")
	if event_phase and declared_phase and event_phase != declared_phase:
		raise frappe.ValidationError("Surgery event type and surgery_phase do not match")
	phase = declared_phase or event_phase
	values: dict[str, Any] = {
		"event": event_name,
		"surgery_no": _required_text(payload, "surgery_no", 200),
		"surgery_type": _text(payload, "surgery_type", 200),
		"procedure_code": _required_text(payload, "procedure_code", 100),
		"surgery_level": _choice(
			payload,
			"surgery_level",
			frozenset({"Level I", "Level II", "Level III", "Level IV"}),
			required=True,
		),
		"surgery_time": payload.get("surgery_time"),
		"surgeon": _required_text(payload, "surgeon", 140),
		"surgery_phase": phase,
		"source_finalized_at": payload.get("source_finalized_at"),
		"anesthesia_type": _text(payload, "anesthesia_type", 200),
		"risk_level": _choice(payload, "risk_level", _RISK_LEVELS, required=False),
		"preanesthesia_status": _choice(
			payload,
			"preanesthesia_status",
			frozenset({"Not Applicable", "Pending", "Completed", "Failed"}),
			required=False,
		),
		"preanesthesia_completed_at": payload.get("preanesthesia_completed_at"),
		"intraoperative_monitoring_status": _choice(
			payload,
			"intraoperative_monitoring_status",
			frozenset({"Not Applicable", "Pending", "Completed", "Failed"}),
			required=False,
		),
		"monitoring_started_at": payload.get("monitoring_started_at"),
		"monitoring_completed_at": payload.get("monitoring_completed_at"),
		"recovery_status": _choice(
			payload,
			"recovery_status",
			frozenset({"Not Applicable", "Pending", "Completed", "Failed"}),
			required=False,
		),
		"recovery_completed_at": payload.get("recovery_completed_at"),
		"postoperative_followup_status": _choice(
			payload,
			"postoperative_followup_status",
			frozenset({"Not Applicable", "Pending", "Completed", "Failed"}),
			required=False,
		),
		"postoperative_followup_completed_at": payload.get("postoperative_followup_completed_at"),
		"cancellation_status": _choice(
			payload,
			"cancellation_status",
			frozenset({"Not Cancelled", "Confirmed"}),
			required=phase == "Cancelled",
		),
		"cancellation_reason_code": _text(payload, "cancellation_reason_code", 100),
		"cancelled_at": payload.get("cancelled_at"),
		"unplanned_surgery_status": _choice(
			payload,
			"unplanned_surgery_status",
			frozenset({"Not Observed", "No", "Yes"}),
			required=False,
		),
		"unplanned_surgery_reason_code": _text(
			payload,
			"unplanned_surgery_reason_code",
			100,
		),
		"unplanned_return_status": _choice(
			payload,
			"unplanned_return_status",
			frozenset({"Not Observed", "No", "Yes"}),
			required=False,
		),
		"unplanned_return_at": payload.get("unplanned_return_at"),
		"complication_status": _choice(
			payload,
			"complication_status",
			frozenset({"Not Observed", "No", "Yes"}),
			required=False,
		),
		"complication_code": _text(payload, "complication_code", 100),
		"complication_at": payload.get("complication_at"),
		"mortality_status": _choice(
			payload,
			"mortality_status",
			frozenset({"Not Observed", "No", "Yes"}),
			required=False,
		),
		"mortality_at": payload.get("mortality_at"),
		"status": _choice(payload, "qc_status", _QC_STATUSES, required=False) or "Pending",
		"details_json": _details(payload),
	}
	if not values["surgery_time"]:
		raise frappe.ValidationError("Surgery projection requires surgery_time")
	return _without_empty(values)


def _safety_event_values(
	event_name: str,
	event_type: str,
	payload: dict[str, Any],
) -> dict[str, Any]:
	status = _choice(payload, "safety_status", _SAFETY_STATUSES, required=False)
	if status not in (None, "Reported"):
		raise frappe.ValidationError(
			"New medical safety projections must enter the local workflow in Reported status"
		)
	anonymous_report = _boolean(payload, "anonymous_report")
	report_channel = (
		_choice(
			payload,
			"report_channel",
			frozenset(
				{
					"Clinical Integration",
					"Staff Portal",
					"Anonymous Portal",
					"Imported",
				}
			),
			required=False,
		)
		or "Clinical Integration"
	)
	reported_by = _text(payload, "reported_by", 140)
	reported_at = payload.get("reported_at")
	if anonymous_report:
		if report_channel != "Anonymous Portal" or reported_by:
			raise frappe.ValidationError(
				"Anonymous safety projections require Anonymous Portal and cannot contain reported_by"
			)
		if reported_at:
			raise frappe.ValidationError("Anonymous safety projections cannot persist reported_at")
	elif report_channel == "Anonymous Portal":
		raise frappe.ValidationError("Anonymous Portal safety projections must be marked anonymous")
	elif report_channel == "Staff Portal":
		if not reported_by or not reported_at:
			raise frappe.ValidationError(
				"Staff Portal safety projections require reported_by and reported_at"
			)
	elif reported_by:
		raise frappe.ValidationError(
			"Integration and imported safety projections cannot assert an internal reporter identity"
		)
	safety_event_type = _text(payload, "safety_event_type", 200) or str(event_type or "").strip()
	if not 1 <= len(safety_event_type) <= 200:
		raise frappe.ValidationError("Medical safety event type must contain 1-200 characters")
	narrative = _text(payload, "narrative", 20_000)
	if not narrative or len(narrative) < 10:
		raise frappe.ValidationError("Medical safety event narrative must contain 10-20,000 characters")
	values: dict[str, Any] = {
		"clinical_event": event_name,
		"event_time": payload.get("safety_event_time") or payload.get("event_time"),
		"event_type": safety_event_type,
		"severity": _choice(payload, "severity", _RISK_LEVELS, required=True),
		"status": "Reported",
		"narrative": narrative,
		"immediate_action": _text(payload, "immediate_action", 20_000),
		"anonymous_report": anonymous_report,
		"near_miss": _boolean(payload, "near_miss"),
		"report_channel": report_channel,
		"reported_by": reported_by,
		"reported_at": reported_at,
		"_system_owner": anonymous_report == 1,
	}
	if not values["event_time"]:
		event = frappe.db.get_value("IONE Clinical Quality Event", event_name, ["event_time"], as_dict=True)
		values["event_time"] = event.event_time if event else None
	if not values["event_time"]:
		raise frappe.ValidationError("Medical safety event projection requires event_time")
	return _without_empty(values)


def _required_text(payload: dict[str, Any], fieldname: str, maximum: int) -> str:
	value = _text(payload, fieldname, maximum)
	if not value:
		raise frappe.ValidationError(f"Clinical projection requires {fieldname}")
	return value


def _text(payload: dict[str, Any], fieldname: str, maximum: int) -> str | None:
	value = payload.get(fieldname)
	if value in (None, ""):
		return None
	if not isinstance(value, str):
		raise frappe.ValidationError(f"Clinical projection {fieldname} must be text")
	value = value.strip()
	if not value or len(value) > maximum:
		raise frappe.ValidationError(f"Clinical projection {fieldname} must contain 1-{maximum} characters")
	return value


def _choice(
	payload: dict[str, Any],
	fieldname: str,
	allowed: frozenset[str],
	*,
	required: bool,
) -> str | None:
	value = _text(payload, fieldname, 100)
	if not value:
		if required:
			raise frappe.ValidationError(f"Clinical projection requires {fieldname}")
		return None
	if value not in allowed:
		raise frappe.ValidationError(f"Clinical projection {fieldname} is not an approved value")
	return value


def _percent(payload: dict[str, Any], fieldname: str) -> float | None:
	value = payload.get(fieldname)
	if value in (None, ""):
		return None
	if isinstance(value, bool):
		raise frappe.ValidationError(f"Clinical projection {fieldname} must be numeric")
	try:
		number = float(value)
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError(f"Clinical projection {fieldname} must be numeric") from exc
	if not 0 <= number <= 100:
		raise frappe.ValidationError(f"Clinical projection {fieldname} must be between 0 and 100")
	return number


def _boolean(payload: dict[str, Any], fieldname: str) -> int | None:
	value = payload.get(fieldname)
	if value in (None, ""):
		return None
	if value in (True, 1, "1", "true", "True"):
		return 1
	if value in (False, 0, "0", "false", "False"):
		return 0
	raise frappe.ValidationError(f"Clinical projection {fieldname} must be boolean")


def _details(payload: dict[str, Any]) -> str | None:
	value = payload.get("projection_details")
	if value in (None, ""):
		return None
	if not isinstance(value, dict):
		raise frappe.ValidationError("projection_details must be a JSON object")
	encoded = json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)
	if len(encoded.encode()) > 64 * 1024:
		raise frappe.ValidationError("projection_details exceeds 64 KiB")
	return encoded


def _without_empty(values: dict[str, Any]) -> dict[str, Any]:
	return {key: value for key, value in values.items() if value not in (None, "")}
