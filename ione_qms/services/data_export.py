from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Iterable
from contextlib import contextmanager
from typing import Any

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime
from frappe.utils.file_manager import save_file

from ione_qms.constants import SENSITIVE_FIELD_NAMES
from ione_qms.services.runtime_settings import require_post_migrate_runtime_ready

EXPORT_REQUESTER_ROLES = frozenset(
	{
		"IONE Physician",
		"IONE Department Director",
		"IONE Department QC Officer",
		"IONE Nursing/Pharmacy/IC QC",
		"IONE QC Reviewer",
		"IONE Agent Reviewer",
		"IONE Medical Affairs",
	}
)
EXPORT_APPROVER_ROLES = frozenset({"IONE Auditor", "IONE Medical Affairs"})
EXPORT_GLOBAL_APPROVER_ROLES = frozenset({"IONE Medical Affairs"})
EXPORT_SCOPED_APPROVER_ROLE = "IONE Auditor"

EXPORT_SCOPE_MODES = frozenset({"Department", "Campus", "Hospital", "Multiple", "Unscoped"})
AUDITOR_EXPORT_SCOPE_MODES = frozenset({"Department", "Campus"})

EXPORTABLE_DOCTYPES = frozenset(
	{
		"IONE Patient Index",
		"IONE Encounter Index",
		"IONE Clinical Quality Event",
		"IONE QC Execution",
		"IONE QC Rule Test Execution",
		"IONE QC Rule Shadow Execution",
		"IONE QC Rule Shadow Feedback",
		"IONE QC Finding",
		"IONE QC Finding Evidence",
		"IONE Medical Record QC",
		"IONE Medical Record Sampling Policy",
		"IONE Medical Record Sampling Policy Operation",
		"IONE Medical Record Review Batch",
		"IONE Medical Record Review Assignment",
		"IONE Medical Record Review Decision",
		"IONE Medical Record Archive Decision",
		"IONE Medical Record Archive Acknowledgement",
		"IONE Medical Record Archive Delivery",
		"IONE Medical Record Completeness Watermark",
		"IONE Surgery QC",
		"IONE Surgery Procedure Policy",
		"IONE Surgery MDT Record",
		"IONE Surgery MDT Participant",
		"IONE Surgery Safety Checklist",
		"IONE Surgery Emergency Exception",
		"IONE Medical Safety Event",
		"IONE QC Rectification",
		"IONE QC Verification",
		"IONE PDCA Project",
		"IONE Finding Recurrence Policy",
		"IONE Finding Recurrence Run",
		"IONE Finding Recurrence Evaluation",
		"IONE QC Indicator",
		"IONE QC Indicator Version",
		"IONE Indicator Calculation",
		"IONE Indicator Result",
		"IONE Indicator Result Detail",
		"IONE Indicator Result Pointer",
		"IONE Indicator Alert",
		"IONE AI Analysis Task",
		"IONE AI Candidate Finding",
		"IONE AI Report Draft",
		"IONE Daily Quality Fact",
		"IONE Monthly Quality Fact",
		"IONE Surgery Quality Fact",
		"IONE Finding Analysis Fact",
		"IONE Agent Analysis Fact",
	}
)

SENSITIVE_EXPORT_DOCTYPES = frozenset(
	{
		"IONE Patient Index",
		"IONE Encounter Index",
		"IONE Clinical Quality Event",
		"IONE QC Execution",
		"IONE QC Rule Test Execution",
		"IONE QC Rule Shadow Execution",
		"IONE QC Rule Shadow Feedback",
		"IONE QC Finding",
		"IONE QC Finding Evidence",
		"IONE Medical Record QC",
		"IONE Medical Record Sampling Policy",
		"IONE Medical Record Sampling Policy Operation",
		"IONE Medical Record Review Batch",
		"IONE Medical Record Review Assignment",
		"IONE Medical Record Review Decision",
		"IONE Medical Record Archive Decision",
		"IONE Medical Record Archive Acknowledgement",
		"IONE Medical Record Archive Delivery",
		"IONE Medical Record Completeness Watermark",
		"IONE Source Document Locator",
		"IONE Source Document Access Log",
		"IONE Surgery QC",
		"IONE Surgery Procedure Policy",
		"IONE Surgery MDT Record",
		"IONE Surgery MDT Participant",
		"IONE Surgery Safety Checklist",
		"IONE Surgery Emergency Exception",
		"IONE Medical Safety Event",
		"IONE QC Rectification",
		"IONE QC Verification",
		"IONE Indicator Calculation",
		"IONE Indicator Result Pointer",
		"IONE Indicator Quarantine Receipt",
		"IONE Indicator Quarantine Disposition",
		"IONE Finding Recurrence Policy",
		"IONE Finding Recurrence Run",
		"IONE Finding Recurrence Evaluation",
		"IONE Quality Meeting",
		"IONE Meeting Minute",
		"IONE Meeting Decision",
		"IONE Quality Action Item",
		"IONE Quality Action Verification Round",
		"IONE Quality Experience Share",
		"IONE AI Analysis Task",
		"IONE AI Candidate Finding",
		"IONE Quality Report Snapshot",
		"IONE AI Report Recovery Authorization",
		"IONE PHI Disclosure Policy",
		"IONE PHI Access Receipt",
		"IONE Batch Run",
		"IONE Batch Work Item",
		"IONE Batch Recovery Receipt",
		"IONE Batch Source Epoch",
	}
)

GOVERNED_NATIVE_EXPORT_DOCTYPES = frozenset(
	{
		"IONE Medical Staff",
		"IONE Staff Qualification",
		"IONE Surgery Authorization",
		"IONE Surgery Procedure Policy",
		"IONE Patient Index",
		"IONE Encounter Index",
		"IONE Clinical Quality Event",
		"IONE QC Execution",
		"IONE QC Rule Test Execution",
		"IONE QC Rule Shadow Execution",
		"IONE QC Rule Shadow Feedback",
		"IONE QC Finding",
		"IONE QC Finding Evidence",
		"IONE QC Finding Appeal",
		"IONE QC Finding Appeal Evidence",
		"IONE Medical Record QC",
		"IONE Medical Record Sampling Policy",
		"IONE Medical Record Sampling Policy Operation",
		"IONE Medical Record Review Batch",
		"IONE Medical Record Review Assignment",
		"IONE Medical Record Review Decision",
		"IONE Medical Record Archive Decision",
		"IONE Medical Record Archive Acknowledgement",
		"IONE Medical Record Archive Delivery",
		"IONE Medical Record Completeness Watermark",
		"IONE Source Document Locator",
		"IONE Source Document Access Log",
		"IONE Surgery QC",
		"IONE Surgery MDT Record",
		"IONE Surgery MDT Participant",
		"IONE Surgery Safety Checklist",
		"IONE Surgery Emergency Exception",
		"IONE Medical Safety Event",
		"IONE QC Rectification",
		"IONE QC Verification",
		"IONE Finding Recurrence Policy",
		"IONE Finding Recurrence Run",
		"IONE Finding Recurrence Evaluation",
		"IONE Quality Meeting",
		"IONE Meeting Minute",
		"IONE Meeting Decision",
		"IONE Quality Action Item",
		"IONE Quality Action Verification Round",
		"IONE Quality Experience Share",
		"IONE Indicator Calculation",
		"IONE Indicator Result Detail",
		"IONE Indicator Result Pointer",
		"IONE Indicator Quarantine Receipt",
		"IONE Indicator Quarantine Disposition",
		"IONE Finding Analysis Fact",
		"IONE Surgery Quality Fact",
		"IONE Agent Analysis Fact",
		"IONE AI Analysis Task",
		"IONE AI Candidate Finding",
		"IONE AI Report Draft",
		"IONE AI Report Schedule",
		"IONE Quality Report Snapshot",
		"IONE AI Report Recovery Authorization",
		"IONE Agent Release",
		"IONE AI Tool Approval",
		"IONE Flow Run Link",
		"IONE AI Execution Event",
		"IONE AI Data Access Log",
		"IONE Agent Incident",
		"IONE PDCA Project",
		"IONE QC Indicator",
		"IONE QC Indicator Version",
		"IONE Definition Lineage Receipt",
		"IONE Indicator Result",
		"IONE Indicator Alert",
		"IONE Daily Quality Fact",
		"IONE Monthly Quality Fact",
		"IONE PHI Disclosure Policy",
		"IONE PHI Access Receipt",
		"IONE Batch Run",
		"IONE Batch Work Item",
		"IONE Batch Recovery Receipt",
		"IONE Batch Source Epoch",
	}
)

MAX_EXPORT_FIELDS = 30
MAX_EXPORT_FILTERS = 20
MAX_EXPORT_ROWS = 10_000
MAX_EXPORT_BYTES = 10 * 1024 * 1024
EXPORT_TTL_HOURS = 24

_FILTER_OPERATORS = frozenset(
	{
		"=",
		"!=",
		">",
		">=",
		"<",
		"<=",
		"in",
		"not in",
		"between",
		"like",
		"not like",
		"is",
	}
)
_NON_EXPORTABLE_FIELDTYPES = frozenset(
	{
		"Password",
		"Attach",
		"Attach Image",
		"Button",
		"HTML",
		"Image",
		"Table",
		"Table MultiSelect",
	}
)
_IMMUTABLE_REQUEST_FIELDS = frozenset(
	{
		"request_id",
		"reference_doctype",
		"filters_json",
		"fields_json",
		"reason",
		"classification",
		"scope_mode",
		"hospital",
		"campus",
		"department",
		"requested_by",
		"requested_at",
	}
)
_MANAGED_RESULT_FIELDS = frozenset(
	{
		"status",
		"approved_by",
		"approved_at",
		"approval_comment",
		"expires_at",
		"record_count",
		"output_file",
		"watermark",
		"record_hash",
	}
)
_STATUS_TRANSITIONS = {
	"Pending": frozenset({"Approved", "Rejected"}),
	"Approved": frozenset({"Processing", "Expired"}),
	"Processing": frozenset({"Completed", "Failed"}),
	"Completed": frozenset({"Expired"}),
	"Rejected": frozenset(),
	"Failed": frozenset(),
	"Expired": frozenset(),
}
_CLASSIFICATION_RANK = {"Public": 0, "Hospital Internal": 1, "Sensitive Medical": 2}
_SCOPE_LINKS = {
	"hospital": "IONE Hospital",
	"campus": "IONE Hospital Campus",
	"department": "IONE Medical Department",
}


def validate_data_export_request(doc, method: str | None = None) -> None:
	"""Fail-closed validation for both Desk and API-created export requests."""
	del method
	previous = doc.get_doc_before_save()
	if previous is None:
		if getattr(doc.flags, "ione_export_initialized", False):
			doc.requested_by = frappe.session.user
			doc.status = "Pending"
			for fieldname in _MANAGED_RESULT_FIELDS.difference({"status"}):
				doc.set(fieldname, None)
			normalize_export_spec(doc)
			return
		_require_export_requester()
		doc.request_id = _new_request_id()
		doc.requested_by = frappe.session.user
		doc.requested_at = now_datetime()
		doc.status = "Pending"
		for fieldname in _MANAGED_RESULT_FIELDS.difference({"status"}):
			doc.set(fieldname, None)
		normalize_export_spec(doc)
		doc.flags.ione_export_initialized = True
		return

	changed_request_fields = _changed_fields(doc, previous, _IMMUTABLE_REQUEST_FIELDS)
	if changed_request_fields:
		frappe.throw(
			"Submitted export request scope is immutable. Create a new request to change: "
			+ ", ".join(sorted(changed_request_fields))
		)
	if not getattr(doc.flags, "ione_export_transition", False):
		managed_changes = _changed_fields(doc, previous, _MANAGED_RESULT_FIELDS)
		if managed_changes:
			frappe.throw(
				"Export approval and result fields may only be changed through the controlled export API."
			)
		return
	_validate_status_transition(str(previous.status or ""), str(doc.status or ""))


def normalize_export_spec(doc) -> tuple[dict[str, Any], list[str]]:
	doctype = str(doc.get("reference_doctype") or "").strip()
	if doctype not in EXPORTABLE_DOCTYPES or not frappe.db.exists("DocType", doctype):
		frappe.throw("The requested DocType is not approved for controlled export.")
	frappe.has_permission(
		doctype,
		ptype="read",
		user=frappe.session.user,
		throw=True,
	)
	_assert_classification(doctype, str(doc.get("classification") or ""))
	reason = str(doc.get("reason") or "").strip()
	if len(reason) < 10 or len(reason) > 1000:
		frappe.throw("Export reason must contain between 10 and 1,000 characters.")
	filters = _parse_filters(doc.get("filters_json"), doctype, frappe.session.user)
	fields = _parse_fields(doc.get("fields_json"), doctype, frappe.session.user)
	scope = _derive_export_scope(filters, doctype, frappe.session.user)
	doc.filters_json = _canonical_json(filters)
	doc.fields_json = _canonical_json(fields)
	doc.reason = reason
	for fieldname, value in scope.items():
		doc.set(fieldname, value)
	return filters, fields


def _derive_export_scope(
	filters: dict[str, Any],
	doctype: str,
	user: str,
) -> dict[str, str | None]:
	"""Derive a trustworthy organizational scope from already-normalized filters."""
	meta = frappe.get_meta(doctype)
	link_fields = {
		fieldname
		for fieldname, linked_doctype in _SCOPE_LINKS.items()
		if (field := meta.get_field(fieldname))
		and field.fieldtype == "Link"
		and field.options == linked_doctype
	}
	present_scope_filters = set(filters).intersection(_SCOPE_LINKS)
	exact = {
		fieldname: value
		for fieldname in link_fields
		if (value := _single_scope_filter_value(filters.get(fieldname))) is not None
	}

	if department := exact.get("department"):
		values = frappe.db.get_value(
			"IONE Medical Department",
			department,
			["hospital", "campus"],
			as_dict=True,
		)
		if not values:
			frappe.throw("The export department scope does not exist.")
		hospital = str(values.get("hospital") or "") or None
		campus = str(values.get("campus") or "") or None
		_assert_scope_parent_consistency(exact, hospital=hospital, campus=campus)
		_require_requester_scope(
			doctype=doctype,
			user=user,
			hospital=hospital,
			campus=campus,
			department=department,
		)
		return {
			"scope_mode": "Department",
			"hospital": hospital,
			"campus": campus,
			"department": department,
		}

	if campus := exact.get("campus"):
		hospital = frappe.db.get_value("IONE Hospital Campus", campus, "hospital")
		if not hospital:
			frappe.throw("The export campus scope does not exist or has no hospital.")
		hospital = str(hospital)
		_assert_scope_parent_consistency(exact, hospital=hospital, campus=campus)
		_require_requester_scope(
			doctype=doctype,
			user=user,
			hospital=hospital,
			campus=campus,
		)
		return {
			"scope_mode": "Campus",
			"hospital": hospital,
			"campus": campus,
			"department": None,
		}

	if hospital := exact.get("hospital"):
		if not frappe.db.exists("IONE Hospital", hospital):
			frappe.throw("The export hospital scope does not exist.")
		_require_requester_scope(
			doctype=doctype,
			user=user,
			hospital=hospital,
		)
		return {
			"scope_mode": "Hospital",
			"hospital": hospital,
			"campus": None,
			"department": None,
		}

	return {
		"scope_mode": "Multiple" if present_scope_filters else "Unscoped",
		"hospital": None,
		"campus": None,
		"department": None,
	}


def _single_scope_filter_value(value: Any) -> str | None:
	if isinstance(value, str):
		normalized = value.strip()
		return normalized or None
	if not isinstance(value, list) or len(value) != 2:
		return None
	operator, operand = value
	if not isinstance(operator, str):
		return None
	operator = operator.strip().lower()
	if operator == "=" and isinstance(operand, str):
		normalized = operand.strip()
		return normalized or None
	if operator == "in" and isinstance(operand, list) and len(operand) == 1 and isinstance(operand[0], str):
		normalized = operand[0].strip()
		return normalized or None
	return None


def _assert_scope_parent_consistency(
	exact: dict[str, str],
	*,
	hospital: str | None,
	campus: str | None,
) -> None:
	if exact.get("hospital") and exact["hospital"] != hospital:
		frappe.throw("The requested hospital filter conflicts with the selected organizational scope.")
	if exact.get("campus") and exact["campus"] != campus:
		frappe.throw("The requested campus filter conflicts with the selected organizational scope.")


def _require_requester_scope(
	*,
	doctype: str,
	user: str,
	hospital: str | None = None,
	campus: str | None = None,
	department: str | None = None,
) -> None:
	# Imported lazily to keep the permission-hook module free of import cycles.
	from ione_qms.permissions import require_scope_read

	require_scope_read(
		hospital=hospital,
		campus=campus,
		department=department,
		user=user,
		target_doctype=doctype,
	)


def _assert_frozen_export_scope(doc, filters: dict[str, Any]) -> None:
	expected = _derive_export_scope(filters, doc.reference_doctype, doc.requested_by)
	actual = {
		"scope_mode": str(doc.get("scope_mode") or ""),
		"hospital": str(doc.get("hospital") or "") or None,
		"campus": str(doc.get("campus") or "") or None,
		"department": str(doc.get("department") or "") or None,
	}
	if actual != expected:
		frappe.throw("The frozen export scope no longer matches its validated filters. Create a new request.")


def approve_export_request(request_name: str, comment: str) -> dict[str, Any]:
	comment = _review_comment(comment)
	with _export_request_lock(request_name):
		doc = frappe.get_doc("IONE Data Export Request", request_name, for_update=True)
		doc.check_permission("read")
		_require_export_approver(doc)
		if doc.status != "Pending":
			frappe.throw("Only Pending export requests may be approved.")
		if doc.requested_by == frappe.session.user:
			frappe.throw("Export requests require separation between requester and approver.")
		_validate_request_as_requester(doc)
		doc.flags.ione_export_transition = True
		doc.status = "Approved"
		doc.approved_by = frappe.session.user
		doc.approved_at = now_datetime()
		doc.approval_comment = comment
		doc.expires_at = add_to_date(doc.approved_at, hours=EXPORT_TTL_HOURS)
		doc.save(ignore_permissions=True)
		frappe.enqueue(
			"ione_qms.services.data_export.generate_export_file",
			queue="long",
			enqueue_after_commit=True,
			job_id=f"ione-qms:export:{doc.name}",
			deduplicate=True,
			request_name=doc.name,
		)
		return {"request": doc.name, "status": doc.status, "expires_at": doc.expires_at}


def reject_export_request(request_name: str, comment: str) -> dict[str, Any]:
	comment = _review_comment(comment)
	with _export_request_lock(request_name):
		doc = frappe.get_doc("IONE Data Export Request", request_name, for_update=True)
		doc.check_permission("read")
		_require_export_approver(doc)
		if doc.status != "Pending":
			frappe.throw("Only Pending export requests may be rejected.")
		if doc.requested_by == frappe.session.user:
			frappe.throw("Export requests require separation between requester and approver.")
		_validate_request_as_requester(doc)
		doc.flags.ione_export_transition = True
		doc.status = "Rejected"
		doc.approved_by = frappe.session.user
		doc.approved_at = now_datetime()
		doc.approval_comment = comment
		doc.save(ignore_permissions=True)
		return {"request": doc.name, "status": doc.status}


def generate_export_file(request_name: str) -> dict[str, Any]:
	"""Materialize an approved CSV under the original requester's permissions."""
	require_post_migrate_runtime_ready("generate a controlled data export")
	with _export_request_lock(request_name):
		doc = frappe.get_doc("IONE Data Export Request", request_name, for_update=True)
		if doc.status == "Completed":
			return {
				"request": doc.name,
				"status": doc.status,
				"file": doc.output_file,
				"record_count": doc.record_count,
			}
		if doc.status != "Approved":
			frappe.throw("Only Approved export requests may be generated.")
		if doc.expires_at and get_datetime(doc.expires_at) <= now_datetime():
			_transition_export(doc, "Expired")
			return {"request": doc.name, "status": doc.status}
		_transition_export(doc, "Processing")
		file_doc = None
		failure_type: str | None = None
		try:
			content, record_count, watermark = _build_csv_as_requester(doc)
			with _export_file_generation(doc.name):
				file_doc = save_file(
					f"{doc.request_id}.csv",
					content,
					doc.doctype,
					doc.name,
					is_private=1,
				)
			doc.reload()
			doc.flags.ione_export_transition = True
			doc.status = "Completed"
			doc.output_file = file_doc.name
			doc.record_count = record_count
			doc.watermark = watermark
			doc.record_hash = hashlib.sha256(content).hexdigest()
			doc.save(ignore_permissions=True)
			return {
				"request": doc.name,
				"status": doc.status,
				"file": file_doc.name,
				"record_count": record_count,
			}
		except Exception as exc:
			# Never log from the active export exception context: CSV rows and
			# permission-filtered query values may be present in traceback locals.
			failure_type = type(exc).__name__
		if file_doc and frappe.db.exists("File", file_doc.name):
			frappe.delete_doc("File", file_doc.name, ignore_permissions=True)
		doc.reload()
		if doc.status == "Processing":
			_transition_export(doc, "Failed")
		frappe.log_error(
			title=f"IONE controlled export failed: {doc.request_id}",
			message=f"{failure_type}: controlled export generation failed; row data was not logged.",
		)
		return {"request": doc.name, "status": doc.status}


def expire_data_exports() -> dict[str, int]:
	require_post_migrate_runtime_ready("expire controlled data exports")
	now = now_datetime()
	expired = 0
	rows = frappe.get_all(
		"IONE Data Export Request",
		filters={
			"status": ["in", ["Approved", "Completed"]],
			"expires_at": ["<=", now],
		},
		fields=["name", "output_file"],
		limit_page_length=500,
	)
	for row in rows:
		with _export_request_lock(row.name, timeout=1):
			doc = frappe.get_doc("IONE Data Export Request", row.name, for_update=True)
			if doc.status not in {"Approved", "Completed"}:
				continue
			file_name = doc.output_file
			_transition_export(doc, "Expired", output_file=None)
			if file_name and frappe.db.exists("File", file_name):
				frappe.delete_doc("File", file_name, ignore_permissions=True)
			expired += 1
	return {"expired": expired}


def _build_csv_as_requester(doc) -> tuple[bytes, int, str]:
	original_user = frappe.session.user
	try:
		frappe.set_user(doc.requested_by)
		filters, fields = _validate_request_as_requester(doc)
		rows = _read_rows(doc.reference_doctype, filters, fields)
	finally:
		frappe.set_user(original_user)

	exported_at = now_datetime().isoformat()
	watermark = (
		f"IONE-QMS|request={doc.request_id}|requester={doc.requested_by}|"
		f"approved_by={doc.approved_by}|exported_at={exported_at}"
	)
	output = io.StringIO(newline="")
	writer = csv.DictWriter(
		output,
		fieldnames=[*fields, "_ione_export_request", "_ione_watermark", "_ione_exported_at"],
		extrasaction="ignore",
	)
	writer.writeheader()
	for row in rows:
		safe_row = {fieldname: _safe_csv_cell(row.get(fieldname)) for fieldname in fields}
		safe_row.update(
			{
				"_ione_export_request": doc.request_id,
				"_ione_watermark": watermark,
				"_ione_exported_at": exported_at,
			}
		)
		writer.writerow(safe_row)
	content = ("\ufeff" + output.getvalue()).encode("utf-8")
	if len(content) > MAX_EXPORT_BYTES:
		frappe.throw("Export exceeds the 10 MiB controlled-export limit; narrow the request scope.")
	return content, len(rows), watermark


def _validate_request_as_requester(doc) -> tuple[dict[str, Any], list[str]]:
	original_user = frappe.session.user
	try:
		frappe.set_user(doc.requested_by)
		_require_export_requester()
		frappe.has_permission(doc.reference_doctype, ptype="read", throw=True)
		filters = _parse_filters(doc.filters_json, doc.reference_doctype, doc.requested_by)
		fields = _parse_fields(doc.fields_json, doc.reference_doctype, doc.requested_by)
		_assert_frozen_export_scope(doc, filters)
		return filters, fields
	finally:
		frappe.set_user(original_user)


def _read_rows(
	doctype: str,
	filters: dict[str, Any],
	fields: list[str],
) -> list[dict[str, Any]]:
	if doctype == "IONE Indicator Result Detail":
		from ione_qms.indicator_engine.calculator import (
			indicator_result_detail_governance_schema_ready,
		)

		if not indicator_result_detail_governance_schema_ready(frappe):
			frappe.throw(
				"Indicator detail governance schema is unavailable; export is denied.",
				frappe.PermissionError,
			)
	rows: list[dict[str, Any]] = []
	page_length = 500
	for start in range(0, MAX_EXPORT_ROWS + 1, page_length):
		batch = frappe.get_list(
			doctype,
			filters=filters,
			fields=fields,
			# A stable tie-breaker is required for paged exports. Many records can
			# share the same modified timestamp, and an unspecified order could
			# otherwise duplicate or omit rows between pages.
			order_by="modified desc, name desc",
			limit_start=start,
			limit_page_length=page_length,
		)
		rows.extend(dict(row) for row in batch)
		if len(rows) > MAX_EXPORT_ROWS:
			frappe.throw(
				"Export exceeds the 10,000-record controlled-export limit; narrow the request scope."
			)
		if len(batch) < page_length:
			break
	return rows


def _parse_filters(value: Any, doctype: str, user: str) -> dict[str, Any]:
	parsed = _parse_json(value, expected=dict, label="filters_json")
	if len(parsed) > MAX_EXPORT_FILTERS:
		frappe.throw(f"Export filters may contain at most {MAX_EXPORT_FILTERS} fields.")
	meta = frappe.get_meta(doctype)
	valid_fields = set(
		meta.get_permitted_fieldnames(
			user=user,
			permission_type="read",
			with_virtual_fields=False,
		)
	)
	valid_fields.update({"name", "creation", "modified", "owner", "modified_by", "docstatus"})
	normalized: dict[str, Any] = {}
	for fieldname, filter_value in parsed.items():
		if not isinstance(fieldname, str) or fieldname not in valid_fields:
			frappe.throw(f"Invalid export filter field: {fieldname!s}")
		normalized[fieldname] = _normalize_filter_value(filter_value)
	return dict(sorted(normalized.items()))


def _normalize_filter_value(value: Any) -> Any:
	if isinstance(value, list):
		if len(value) != 2 or not isinstance(value[0], str):
			frappe.throw("Each list-form export filter must be [operator, value].")
		operator = value[0].strip().lower()
		if operator not in _FILTER_OPERATORS:
			frappe.throw(f"Unsupported export filter operator: {operator}")
		operand = value[1]
		if operator in {"in", "not in", "between"}:
			if not isinstance(operand, list) or len(operand) > 100:
				frappe.throw(f"Export filter operator {operator} requires a bounded list.")
			return [operator, [_bounded_scalar(item) for item in operand]]
		return [operator, _bounded_scalar(operand)]
	return _bounded_scalar(value)


def _bounded_scalar(value: Any) -> Any:
	if value is None or isinstance(value, bool | int | float):
		return value
	if isinstance(value, str):
		if len(value) > 500:
			frappe.throw("Export filter values may not exceed 500 characters.")
		return value
	frappe.throw("Export filters support only scalar values and reviewed operators.")


def _parse_fields(value: Any, doctype: str, user: str) -> list[str]:
	parsed = _parse_json(value, expected=list, label="fields_json")
	if not 1 <= len(parsed) <= MAX_EXPORT_FIELDS:
		frappe.throw(f"Export fields must contain between 1 and {MAX_EXPORT_FIELDS} entries.")
	if len(parsed) != len(set(parsed)):
		frappe.throw("Export fields must not contain duplicates.")
	meta = frappe.get_meta(doctype)
	permitted = set(
		meta.get_permitted_fieldnames(
			user=user,
			permission_type="read",
			with_virtual_fields=False,
		)
	)
	permitted.update({"name", "creation", "modified", "owner", "modified_by", "docstatus"})
	fields: list[str] = []
	for fieldname in parsed:
		if not isinstance(fieldname, str) or fieldname not in permitted:
			frappe.throw(f"Export field is not readable by the requester: {fieldname!s}")
		field = meta.get_field(fieldname)
		if field and field.fieldtype in _NON_EXPORTABLE_FIELDTYPES:
			frappe.throw(f"Export field type is not supported: {fieldname}")
		if _is_unsafe_generic_export_field(fieldname):
			frappe.throw(
				f"Field requires a purpose-built clinical export and is not available here: {fieldname}"
			)
		fields.append(fieldname)
	return fields


def _is_unsafe_generic_export_field(fieldname: str) -> bool:
	normalized = fieldname.strip().lower()
	return (
		normalized in SENSITIVE_FIELD_NAMES
		or normalized.endswith("_payload")
		or normalized.endswith("_payload_json")
		or normalized in {"payload", "payload_json", "raw_payload", "medical_record_text", "evidence_json"}
	)


def _parse_json(value: Any, *, expected: type, label: str) -> Any:
	try:
		parsed = json.loads(value) if isinstance(value, str) else value
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError(f"{label} must contain valid JSON.") from exc
	if not isinstance(parsed, expected):
		frappe.throw(f"{label} has an invalid JSON shape.")
	return parsed


def _assert_classification(doctype: str, classification: str) -> None:
	if classification not in _CLASSIFICATION_RANK:
		frappe.throw("Invalid export data classification.")
	minimum = "Sensitive Medical" if doctype in SENSITIVE_EXPORT_DOCTYPES else "Hospital Internal"
	if _CLASSIFICATION_RANK[classification] < _CLASSIFICATION_RANK[minimum]:
		frappe.throw(f"{doctype} exports must be classified as {minimum} or higher.")


def _require_export_requester() -> None:
	if frappe.session.user in {"Administrator", "Guest", None, ""}:
		frappe.throw("A named business user is required for controlled export.", frappe.PermissionError)
	if not frappe.db.get_value("User", frappe.session.user, "enabled"):
		frappe.throw("Disabled users cannot request or generate data exports.", frappe.PermissionError)
	roles = set(frappe.get_roles(frappe.session.user))
	if not roles.intersection(EXPORT_REQUESTER_ROLES) or "IONE Agent Service" in roles:
		frappe.throw("Current user is not authorized to request data exports.", frappe.PermissionError)


def _require_export_approver(doc) -> None:
	if frappe.session.user in {"Administrator", "Guest", None, ""}:
		frappe.throw("A named business approver is required for controlled export.", frappe.PermissionError)
	if not frappe.db.get_value("User", frappe.session.user, "enabled"):
		frappe.throw("Disabled users cannot approve data exports.", frappe.PermissionError)
	roles = set(frappe.get_roles(frappe.session.user))
	if not roles.intersection(EXPORT_APPROVER_ROLES) or "IONE Agent Service" in roles:
		frappe.throw("Current user is not an authorized export approver.", frappe.PermissionError)
	if roles.intersection(EXPORT_GLOBAL_APPROVER_ROLES):
		return
	if EXPORT_SCOPED_APPROVER_ROLE in roles and auditor_can_access_export_request(
		doc,
		frappe.session.user,
	):
		return
	frappe.throw(
		"Auditors may approve only a single department or campus export covered by an "
		"explicit User Permission. Hospital-wide, multi-scope, and unscoped exports "
		"require Medical Affairs.",
		frappe.PermissionError,
	)


def auditor_can_access_export_request(doc, user: str) -> bool:
	"""Authorize an Auditor only through explicit, applicable User Permission grants."""
	mode = str(getattr(doc, "scope_mode", "") or "")
	if mode not in AUDITOR_EXPORT_SCOPE_MODES:
		return False
	reference_doctype = str(getattr(doc, "reference_doctype", "") or "")
	department = str(getattr(doc, "department", "") or "")
	campus = str(getattr(doc, "campus", "") or "")
	hospital = str(getattr(doc, "hospital", "") or "")
	if mode == "Department" and (not department or not hospital):
		return False
	if mode == "Campus" and (not campus or not hospital or department):
		return False

	for linked_doctype, document, applicable_for in _explicit_scope_grants(user):
		if not _scope_grant_applies(applicable_for, reference_doctype):
			continue
		if linked_doctype == "IONE Medical Department":
			if mode == "Department" and document == department:
				return True
		elif linked_doctype == "IONE Hospital Campus":
			if campus and document == campus:
				return True
		elif linked_doctype == "IONE Hospital" and document == hospital:
			return True
	return False


def auditor_export_request_query(user: str, table: str = "`tabIONE Data Export Request`") -> str:
	"""Return the exact SQL scope condition used by list/report permission hooks."""
	clauses: list[str] = []
	for linked_doctype, document, applicable_for in _explicit_scope_grants(user):
		applicability = _scope_grant_sql(applicable_for, table)
		if applicability is None:
			continue
		value = frappe.db.escape(document)
		if linked_doctype == "IONE Medical Department":
			scope = f"({table}.scope_mode = 'Department' and {table}.department = {value})"
		elif linked_doctype == "IONE Hospital Campus":
			scope = f"({table}.scope_mode in ('Department', 'Campus') and {table}.campus = {value})"
		elif linked_doctype == "IONE Hospital":
			scope = f"({table}.scope_mode in ('Department', 'Campus') and {table}.hospital = {value})"
		else:
			continue
		clauses.append(f"({scope}{applicability})")
	return "(" + " or ".join(sorted(set(clauses))) + ")" if clauses else "1=0"


def _explicit_scope_grants(user: str) -> tuple[tuple[str, str, str], ...]:
	if user in {"", "Guest", "Administrator"}:
		return ()
	try:
		user_permissions = frappe.permissions.get_user_permissions(user)
	except Exception:
		return ()
	grants: set[tuple[str, str, str]] = set()
	for linked_doctype in _SCOPE_LINKS.values():
		for permission in user_permissions.get(linked_doctype, ()):
			document = str(permission.get("doc") or "")
			if not document:
				continue
			grants.add(
				(
					linked_doctype,
					document,
					str(permission.get("applicable_for") or ""),
				)
			)
	return tuple(sorted(grants))


def _scope_grant_applies(applicable_for: str, reference_doctype: str) -> bool:
	return not applicable_for or applicable_for in {
		reference_doctype,
		"IONE Data Export Request",
	}


def _scope_grant_sql(applicable_for: str, table: str) -> str | None:
	if not applicable_for or applicable_for == "IONE Data Export Request":
		return ""
	if applicable_for not in EXPORTABLE_DOCTYPES:
		return None
	return f" and {table}.reference_doctype = {frappe.db.escape(applicable_for)}"


def _review_comment(value: str) -> str:
	comment = str(value or "").strip()
	if len(comment) < 5 or len(comment) > 1000:
		frappe.throw("Approval comment must contain between 5 and 1,000 characters.")
	return comment


def _transition_export(doc, status: str, **values: Any) -> None:
	doc.flags.ione_export_transition = True
	doc.status = status
	for fieldname, value in values.items():
		doc.set(fieldname, value)
	doc.save(ignore_permissions=True)


def _export_request_lock(request_name: str, *, timeout: int = 10):
	return frappe.db.advisory_lock(
		f"ione-qms:data-export:{request_name}",
		timeout=timeout,
	)


@contextmanager
def _export_file_generation(request_name: str):
	flag_name = "ione_data_export_generation"
	had_previous = hasattr(frappe.flags, flag_name)
	previous = getattr(frappe.flags, flag_name, None)
	setattr(frappe.flags, flag_name, request_name)
	try:
		yield
	finally:
		if had_previous:
			setattr(frappe.flags, flag_name, previous)
		else:
			frappe.flags.pop(flag_name, None)


def _validate_status_transition(previous: str, current: str) -> None:
	if previous == current:
		return
	if current not in _STATUS_TRANSITIONS.get(previous, frozenset()):
		frappe.throw(f"Invalid export request transition: {previous} -> {current}.")


def _changed_fields(doc, previous, fieldnames: Iterable[str]) -> list[str]:
	return [fieldname for fieldname in fieldnames if doc.get(fieldname) != previous.get(fieldname)]


def _new_request_id() -> str:
	for _attempt in range(5):
		value = f"IONE-EXP-{frappe.generate_hash(length=16).upper()}"
		if not frappe.db.exists("IONE Data Export Request", {"request_id": value}):
			return value
	frappe.throw("Could not allocate a unique export request identifier.")


def _safe_csv_cell(value: Any) -> str:
	if value is None:
		return ""
	if isinstance(value, dict | list | tuple):
		text = _canonical_json(value)
	else:
		text = str(value)
	text = text.replace("\x00", "")
	if text.startswith(("=", "+", "-", "@", "\t", "\r")):
		text = "'" + text
	return text


def _canonical_json(value: Any) -> str:
	return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
