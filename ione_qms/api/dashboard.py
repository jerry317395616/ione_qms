from __future__ import annotations

from datetime import timedelta
from typing import Any

import frappe
from frappe.utils import add_days, get_datetime, getdate, now_datetime, nowdate

from ione_qms.permissions import get_access_context, has_app_permission, require_department_read
from ione_qms.services.indicators import current_indicator_result_lock

_WORKBENCH_LIMIT = 20
_WORKBENCH_ROLES = frozenset(
	{
		"IONE Physician",
		"IONE Department Director",
		"IONE Department QC Officer",
		"IONE Nursing/Pharmacy/IC QC",
		"IONE QC Reviewer",
		"IONE Medical Affairs",
		"IONE Agent Reviewer",
		"IONE QMS Auditor",
	}
)
_FUNCTIONAL_WORKBENCH_ROLES = frozenset(
	{
		"IONE Nursing/Pharmacy/IC QC",
		"IONE QC Reviewer",
		"IONE Medical Affairs",
		"IONE Agent Reviewer",
	}
)
_DEPARTMENT_WORKBENCH_ROLES = frozenset(
	{
		"IONE Department Director",
		"IONE Department QC Officer",
	}
)
_WORKBENCH_DATASETS: tuple[dict[str, Any], ...] = (
	{
		"key": "findings",
		"doctype": "IONE QC Finding",
		"fields": ("name", "severity", "status", "department", "due_date", "detected_at"),
		"filters": {"status": ["not in", ["Closed", "Appeal Approved"]]},
		"order_by": "detected_at desc",
		"physician_filter": ("responsible_staff", "staff"),
	},
	{
		"key": "appeals",
		"doctype": "IONE QC Finding Appeal",
		"fields": ("name", "finding", "appeal_type", "status", "department", "submitted_at"),
		"filters": {"status": "Submitted"},
		"order_by": "submitted_at desc",
		"physician_filter": ("responsible_staff", "staff"),
	},
	{
		"key": "rectifications",
		"doctype": "IONE QC Rectification",
		"fields": ("name", "finding", "status", "department", "due_date", "modified"),
		"filters": {"status": ["not in", ["Closed", "Rejected", "Cancelled"]]},
		"order_by": "due_date asc, modified desc",
		"physician_filter": ("assigned_to", "user"),
	},
	{
		"key": "verifications",
		"doctype": "IONE QC Verification",
		"fields": ("name", "finding", "rectification", "status", "department", "modified"),
		"filters": {"status": ["in", ["Draft", "Submitted"]]},
		"order_by": "modified desc",
		"physician_filter": None,
	},
	{
		"key": "indicator_alerts",
		"doctype": "IONE Indicator Alert",
		"fields": (
			"name",
			"indicator",
			"alert_level",
			"severity",
			"status",
			"department",
			"triggered_at",
		),
		"filters": {"status": ["not in", ["Resolved", "Closed"]]},
		"order_by": "triggered_at desc",
		"physician_filter": ("medical_staff", "staff"),
	},
	{
		"key": "safety_events",
		"doctype": "IONE Medical Safety Event",
		"fields": (
			"name",
			"event_type",
			"severity",
			"status",
			"department",
			"event_time",
			"near_miss",
			"anonymous_report",
		),
		"filters": {"status": ["!=", "Closed"]},
		"order_by": "event_time desc",
		"physician_filter": ("responsible_staff", "staff"),
	},
	{
		"key": "pdca_projects",
		"doctype": "IONE PDCA Project",
		"fields": ("name", "project_code", "status", "department", "end_date", "modified"),
		"filters": {"status": ["not in", ["Closed", "Rejected", "Cancelled"]]},
		"order_by": "modified desc",
		"physician_filter": ("project_owner", "user"),
	},
	{
		"key": "ai_candidates",
		"doctype": "IONE AI Candidate Finding",
		"fields": ("name", "severity", "status", "department", "creation", "formal_finding"),
		"filters": {"status": "Pending Review"},
		"order_by": "creation desc",
		"physician_filter": None,
	},
)


@frappe.whitelist(methods=["GET", "POST"])
def get_command_center(
	department: str | None = None,
	days: int = 30,
) -> dict[str, Any]:
	user = str(frappe.session.user or "")
	roles = frozenset(frappe.get_roles(user)) if user not in {"", "Guest", "Administrator"} else frozenset()
	if not has_app_permission(user) or "IONE Agent Service" in roles:
		frappe.throw("IONE QMS access is required", frappe.PermissionError)
	_resolve_workbench_persona(user, roles)
	if department:
		require_department_read(department, user=user)
	days = min(max(int(days or 30), 7), 90)
	finding_filters: dict[str, Any] = {}
	if department:
		finding_filters["department"] = department

	open_filters = {
		**finding_filters,
		"status": ["not in", ["Closed", "Appeal Approved"]],
	}
	high_risk_filters = {
		**open_filters,
		"severity": ["in", ["High", "Critical"]],
	}
	overdue_filters = {
		**open_filters,
		"due_date": ["<", nowdate()],
	}
	return {
		"generated_at": now_datetime(),
		"scope": {"department": department, "days": days},
		"cards": {
			"open_findings": _count("IONE QC Finding", open_filters),
			"high_risk": _count("IONE QC Finding", high_risk_filters),
			"overdue": _count("IONE QC Finding", overdue_filters),
			"pending_rectification": _count(
				"IONE QC Rectification",
				_scope_filter(
					department,
					{"status": ["in", ["Draft", "Submitted", "In Progress", "Rework"]]},
				),
			),
			"pending_ai_review": _count(
				"IONE AI Candidate Finding",
				_scope_filter(department, {"status": "Pending Review"}),
			),
			"integration_errors_24h": _count(
				"IONE Integration Message",
				{
					"status": ["in", ["Error", "Dead Letter"]],
					"modified": [">=", add_days(now_datetime(), -1)],
				},
			),
		},
		"finding_trend": _finding_trend(finding_filters, days),
		"status_distribution": _status_distribution(open_filters),
		"indicator_alerts": _indicator_alerts(department),
		"priority_findings": _priority_findings(open_filters),
	}


@frappe.whitelist(methods=["GET", "POST"])
def get_role_workbench() -> dict[str, Any]:
	"""Return one permission-filtered, field-minimized work queue for the current user."""
	user = str(frappe.session.user or "")
	roles = frozenset(frappe.get_roles(user)) if user not in {"", "Guest", "Administrator"} else frozenset()
	persona = _resolve_workbench_persona(user, roles)
	staff_records: tuple[str, ...] = ()
	departments: tuple[str, ...] = ()
	if persona in {"Physician", "Department"}:
		staff_records, departments = _workbench_staff_scope(user)
		if not staff_records or (persona == "Department" and not departments):
			return _empty_workbench_response(
				persona=persona,
				staff_mapped=bool(staff_records),
				department_count=len(departments),
			)

	sections: dict[str, list[dict[str, Any]]] = {}
	for dataset in _WORKBENCH_DATASETS:
		filters = dict(dataset["filters"])
		if persona == "Physician":
			physician_filter = dataset["physician_filter"]
			if not physician_filter:
				sections[str(dataset["key"])] = []
				continue
			fieldname, scope_type = physician_filter
			filters[str(fieldname)] = ["in", list(staff_records)] if scope_type == "staff" else user
		elif persona == "Department":
			filters["department"] = ["in", list(departments)]
		sections[str(dataset["key"])] = _permissioned_workbench_list(
			doctype=str(dataset["doctype"]),
			filters=filters,
			fields=tuple(dataset["fields"]),
			order_by=str(dataset["order_by"]),
		)
	return {
		"generated_at": now_datetime(),
		"persona": persona,
		"read_only": persona == "Auditor",
		"scope": {
			"mode": {
				"Physician": "Personal",
				"Department": "Authorized Departments",
				"Functional": "Permission-filtered Organization",
				"Auditor": "Read-only Permission-filtered Organization",
			}[persona],
			"staff_mapped": bool(staff_records) if persona in {"Physician", "Department"} else None,
			"department_count": len(departments) if persona == "Department" else None,
		},
		"sections": sections,
	}


def _resolve_workbench_persona(user: str, roles: frozenset[str]) -> str:
	if user in {"", "Guest"} or (user == "Administrator" and not _administrator_workbench_enabled()):
		frappe.throw(
			"Role workbench access requires a named accountable IONE business user.",
			frappe.PermissionError,
		)
	if user == "Administrator":
		return "Functional"
	if "IONE Agent Service" in roles or not roles.intersection(_WORKBENCH_ROLES):
		frappe.throw(
			"Current user does not have an eligible IONE business workbench role.",
			frappe.PermissionError,
		)
	if "IONE QMS Auditor" in roles:
		return "Auditor"
	if roles.intersection(_FUNCTIONAL_WORKBENCH_ROLES):
		return "Functional"
	if roles.intersection(_DEPARTMENT_WORKBENCH_ROLES):
		return "Department"
	return "Physician"


def _administrator_workbench_enabled() -> bool:
	conf = getattr(frappe, "conf", None) or getattr(getattr(frappe, "local", None), "conf", None) or {}
	value = conf.get("ione_qms_allow_technical_administrator")
	if isinstance(value, bool):
		return value
	if isinstance(value, (int, float)):
		return value != 0
	return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _workbench_staff_scope(user: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
	context = get_access_context(user)
	return tuple(sorted(context.staff_records)), tuple(sorted(context.departments))


def _permissioned_workbench_list(
	*,
	doctype: str,
	filters: dict[str, Any],
	fields: tuple[str, ...],
	order_by: str,
) -> list[dict[str, Any]]:
	"""Read one bounded dataset through Frappe's permission and query-hook path."""
	try:
		frappe.get_meta(doctype)
		if not frappe.has_permission(doctype, "read"):
			return []
		rows = frappe.get_list(
			doctype,
			filters=filters,
			fields=list(fields),
			order_by=order_by,
			limit_page_length=_WORKBENCH_LIMIT,
		)
	except frappe.DoesNotExistError:
		return []
	return [
		{
			fieldname: row.get(fieldname) if hasattr(row, "get") else getattr(row, fieldname, None)
			for fieldname in fields
		}
		for row in rows[:_WORKBENCH_LIMIT]
	]


def _empty_workbench_response(
	*,
	persona: str,
	staff_mapped: bool,
	department_count: int,
) -> dict[str, Any]:
	return {
		"generated_at": now_datetime(),
		"persona": persona,
		"read_only": False,
		"scope": {
			"mode": "Personal" if persona == "Physician" else "Authorized Departments",
			"staff_mapped": staff_mapped,
			"department_count": department_count if persona == "Department" else None,
		},
		"sections": {str(dataset["key"]): [] for dataset in _WORKBENCH_DATASETS},
	}


def _count(doctype: str, filters: dict[str, Any]) -> int:
	if not frappe.db.exists("DocType", doctype) or not frappe.has_permission(doctype, "read"):
		return 0
	rows = frappe.get_list(
		doctype,
		filters=filters,
		fields=[{"COUNT": "name", "as": "total"}],
	)
	return int(rows[0].total or 0) if rows else 0


def _finding_trend(filters: dict[str, Any], days: int) -> list[dict[str, Any]]:
	start = getdate(add_days(nowdate(), -(days - 1)))
	detected_rows = _permissioned_daily_counts(
		"IONE QC Finding",
		date_field="detected_at",
		filters={**filters, "detected_at": [">=", start]},
	)
	closed_rows = _permissioned_daily_counts(
		"IONE QC Finding",
		date_field="modified",
		filters={
			**filters,
			"modified": [">=", start],
			"status": ["in", ["Closed", "Appeal Approved"]],
		},
	)
	detected = {getdate(row.day).isoformat(): int(row.total or 0) for row in detected_rows if row.day}
	closed = {getdate(row.day).isoformat(): int(row.total or 0) for row in closed_rows if row.day}
	return [
		{
			"date": (start + timedelta(days=offset)).isoformat(),
			"detected": detected.get((start + timedelta(days=offset)).isoformat(), 0),
			"closed": closed.get((start + timedelta(days=offset)).isoformat(), 0),
		}
		for offset in range(days)
	]


def _status_distribution(filters: dict[str, Any]) -> list[dict[str, Any]]:
	rows = frappe.get_list(
		"IONE QC Finding",
		filters=filters,
		fields=["status", {"COUNT": "name", "as": "total"}],
		group_by="status",
		order_by="total desc",
	)
	return [{"status": str(row.status or "未分类"), "count": int(row.total or 0)} for row in rows]


def _permissioned_daily_counts(
	doctype: str,
	*,
	date_field: str,
	filters: dict[str, Any],
) -> list[dict[str, Any]]:
	"""Aggregate calendar-day counts through Frappe's permission-aware query path."""
	from frappe.query_builder.functions import Count, Date

	table = frappe.qb.DocType(doctype)
	day = Date(table[date_field])
	query = frappe.qb.get_query(
		doctype,
		fields=[day.as_("day"), Count(table.name).as_("total")],
		filters=filters,
		order_by="",
		ignore_permissions=False,
		user=frappe.session.user,
	)
	return query.groupby(day).orderby(day).run(as_dict=True)


def _indicator_alerts(department: str | None) -> list[dict[str, Any]]:
	if not frappe.db.exists("DocType", "IONE Indicator Alert") or not frappe.has_permission(
		"IONE Indicator Alert", "read"
	):
		return []
	rows = frappe.get_list(
		"IONE Indicator Alert",
		filters=_scope_filter(
			department,
			{"status": ["not in", ["Closed", "Resolved"]]},
		),
		fields=[
			"name",
			"indicator_result",
			"alert_level",
			"department",
			"status",
			"creation",
		],
		order_by="creation desc",
		limit_page_length=20,
	)
	for row in rows:
		with current_indicator_result_lock(str(row.indicator_result)):
			pass
	return rows


def _priority_findings(filters: dict[str, Any]) -> list[dict[str, Any]]:
	rows = frappe.get_list(
		"IONE QC Finding",
		filters=filters,
		fields=[
			"name",
			"title",
			"severity",
			"status",
			"department",
			"due_date",
			"detected_at",
		],
		order_by="detected_at desc",
		limit_page_length=50,
	)
	severity_rank = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
	rows.sort(
		key=lambda row: (
			severity_rank.get(str(row.severity), 9),
			get_datetime(row.due_date or "2999-12-31"),
			get_datetime(row.detected_at or "1970-01-01"),
		)
	)
	return rows[:20]


def _scope_filter(department: str | None, filters: dict[str, Any]) -> dict[str, Any]:
	return {**filters, **({"department": department} if department else {})}
