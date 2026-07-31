from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import frappe
from frappe.utils import add_days, getdate, nowdate

from ione_qms.services.indicators import current_indicator_result_lock

REPORT_MAX_DAYS = 366
REPORT_MAX_OUTPUT_ROWS = 500
REPORT_MAX_SOURCE_ROWS = 10_000
REPORT_MAX_GROUP_ROWS = 5_000
REPORT_MAX_AGGREGATED_RECORDS = 1_000_000
REPORT_MAX_FILTER_TEXT = 140
_RESULT_POINTER_FILTER_FIELDS = frozenset(
	{
		"indicator",
		"indicator_version",
		"period_start",
		"period_end",
		"dimension_hash",
		"hospital",
		"campus",
		"department",
		"ward",
		"medical_group",
		"physician",
		"disease",
		"surgery",
		"drg",
	}
)

REPORT_ROLES: dict[str, frozenset[str]] = {
	"rule_quality": frozenset(
		{
			"IONE Physician",
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
		}
	),
	"indicator_profile": frozenset(
		{
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
			"IONE Nursing/Pharmacy/IC QC",
			"IONE Auditor",
		}
	),
	"finding_closure": frozenset(
		{
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
		}
	),
	"integration_reconciliation": frozenset(
		{
			"IONE Integration Administrator",
			"IONE Integration Operator",
			"IONE QC Administrator",
			"IONE Auditor",
		}
	),
	"ai_usage": frozenset(
		{
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
			"IONE Nursing/Pharmacy/IC QC",
			"IONE Auditor",
		}
	),
	"medical_record_review": frozenset(
		{
			"IONE Medical Record Coder",
			"IONE Medical Record Expert Reviewer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
			"IONE Auditor",
		}
	),
	"surgery_governance": frozenset({"IONE QC Reviewer", "IONE Medical Affairs"}),
	"national_ten_goal": frozenset(
		{
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
			"IONE Nursing/Pharmacy/IC QC",
			"IONE Auditor",
		}
	),
	"improvement_governance": frozenset(
		{
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
		}
	),
}

REPORT_REQUIRED_DOCTYPES: dict[str, tuple[str, ...]] = {
	"rule_quality": (
		"IONE QC Execution",
		"IONE QC Finding",
		"IONE QC Finding Appeal",
		"IONE QC Rule",
		"IONE QC Rule Version",
	),
	"indicator_profile": (
		"IONE Indicator Result",
		"IONE Indicator Result Pointer",
		"IONE QC Indicator",
		"IONE QC Indicator Version",
	),
	"finding_closure": (
		"IONE QC Finding",
		"IONE QC Rectification",
		"IONE QC Verification",
	),
	"integration_reconciliation": ("IONE Data Reconciliation",),
	"ai_usage": ("IONE Agent Analysis Fact",),
	"medical_record_review": (
		"IONE Medical Record Sampling Policy",
		"IONE Medical Record Review Batch",
		"IONE Medical Record Review Assignment",
		"IONE Medical Record Review Decision",
		"IONE Medical Record Archive Decision",
		"IONE Medical Record Archive Acknowledgement",
	),
	"surgery_governance": (
		"IONE Surgery QC",
		"IONE Surgery Procedure Policy",
		"IONE Surgery Authorization",
		"IONE Surgery MDT Record",
		"IONE Surgery Safety Checklist",
		"IONE Surgery Emergency Exception",
	),
	"national_ten_goal": (
		"IONE QC Standard",
		"IONE QC Standard Version",
		"IONE QC Standard Clause",
		"IONE QC Indicator",
		"IONE QC Indicator Version",
		"IONE Indicator Result",
		"IONE Indicator Result Pointer",
	),
	"improvement_governance": (
		"IONE Finding Recurrence Evaluation",
		"IONE PDCA Project",
		"IONE Quality Meeting",
		"IONE Meeting Minute",
		"IONE Meeting Decision",
		"IONE Quality Action Item",
		"IONE Quality Action Verification Round",
		"IONE Quality Experience Share",
	),
}

_SCOPE_FIELDS = ("hospital", "campus", "department", "ward")
_EXECUTION_RESULTS = ("Passed", "Failed", "Excluded", "Insufficient Data", "Error")
_APPEAL_STATUSES = ("Submitted", "Approved", "Rejected", "Withdrawn")
_FINDING_CLOSED_STATUS = "Closed"
_FINDING_OVERTURNED_STATUS = "Appeal Approved"
_RECTIFICATION_TERMINAL_NONCLOSED = frozenset({"Rejected", "Cancelled"})
_FINDING_STATUSES = frozenset(
	{
		"Candidate",
		"Pending QC Review",
		"Confirmed",
		"Appealed",
		"Appeal Approved",
		"Appeal Rejected",
		"Pending Rectification",
		"Rectifying",
		"Pending Department Approval",
		"Pending Functional Review",
		"Rework",
		"Closed",
	}
)
_FINDING_SEVERITIES = frozenset({"Low", "Medium", "High", "Critical"})
_RECTIFICATION_STATUSES = frozenset(
	{
		"Draft",
		"Submitted",
		"In Progress",
		"Pending Department Approval",
		"Pending Functional Review",
		"Rework",
		"Closed",
		"Rejected",
		"Cancelled",
	}
)
_VERIFICATION_STATUSES = frozenset({"Draft", "Submitted", "Verified", "Rejected"})
_VERIFICATION_RESULTS = frozenset({"", "Effective", "Partially Effective", "Ineffective"})
_INDICATOR_RESULT_STATUSES = frozenset(
	{"Calculated", "Met", "Below Target", "Warning", "Critical", "No Data", "Failed"}
)
_RECONCILIATION_STATUSES = frozenset({"Pending", "Matched", "Mismatch", "Failed", "Reviewed"})
_AI_TASK_STATUSES = frozenset(
	{
		"Pending",
		"Queued",
		"Running",
		"Pending Confirmation",
		"Retry",
		"Completed",
		"Failed",
		"Rejected",
		"Cancelled",
		"Reviewed",
	}
)
_MEDICAL_REVIEW_STATUSES = frozenset({"Coder Review", "Expert Review", "Closed"})
_MEDICAL_REVIEW_STAGES = frozenset({"Coder", "Expert"})
_MEDICAL_REVIEW_OUTCOMES = frozenset({"Pass", "Defect", "Needs Correction", "Unable to Determine"})
_ARCHIVE_DECISIONS = frozenset({"Allow", "Hold"})
_ARCHIVE_ACK_STATUSES = frozenset({"Applied", "Rejected", "Failed"})
_SURGERY_LEVELS = frozenset({"Level I", "Level II", "Level III", "Level IV"})
_SURGERY_PHASES = frozenset({"Scheduled", "Occurred", "Postoperative Finalized", "Cancelled"})
_SURGERY_GOVERNANCE_STATES = frozenset(
	{"Pending Prerequisites", "Passed", "Noncompliant", "Blocked", "Cancelled"}
)
_SURGERY_CARE_STATES = frozenset({"Not Applicable", "Pending", "Completed", "Failed"})
_SURGERY_OBSERVATION_STATES = frozenset({"Not Observed", "No", "Yes"})
_SURGERY_CANCELLATION_STATES = frozenset({"Not Cancelled", "Confirmed"})
_RECURRENCE_OUTCOMES = frozenset({"Below Threshold", "Triggered"})
_PDCA_STATUSES = frozenset({"Proposed", "Approved", "Active", "Measuring", "Closed", "Rejected", "Cancelled"})
_MEETING_STATUSES = frozenset(
	{"Draft", "Submitted", "Approved", "Held", "Minutes Pending", "Closed", "Cancelled"}
)
_MINUTE_STATUSES = frozenset({"Draft", "Submitted", "Approved", "Rejected"})
_ACTION_STATUSES = frozenset({"Open", "In Progress", "Pending Verification", "Closed", "Cancelled"})
_ACTION_VERIFICATION_DECISIONS = frozenset({"Close", "Rework", "Cancel"})
_EXPERIENCE_STATUSES = frozenset({"Draft", "Submitted", "Published", "Rejected", "Retired"})


def execute_rule_quality_report(
	filters: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, dict[str, Any], list[dict[str, Any]]]:
	values, start, end = _report_context("rule_quality", filters)
	scope_filters = _scope_filters(values)
	execution_filters = _dated_filters(
		"IONE QC Execution",
		"completed_at",
		start,
		end,
		scope_filters,
	)
	_require_aggregate_source_bound("IONE QC Execution", execution_filters)
	execution_groups = _permissioned_rows(
		"IONE QC Execution",
		filters=execution_filters,
		fields=("rule", "rule_version", "result", "count(name) as total"),
		order_by="rule asc, rule_version asc, result asc",
		group_by="rule, rule_version, result",
		limit=REPORT_MAX_GROUP_ROWS,
	)
	finding_filters = _dated_filters(
		"IONE QC Finding",
		"detected_at",
		start,
		end,
		scope_filters,
	)
	_require_aggregate_source_bound("IONE QC Finding", finding_filters)
	finding_groups = _permissioned_rows(
		"IONE QC Finding",
		filters=finding_filters,
		fields=("rule", "rule_version", "count(name) as total"),
		order_by="rule asc, rule_version asc",
		group_by="rule, rule_version",
		limit=REPORT_MAX_GROUP_ROWS,
	)
	appeals = _permissioned_rows(
		"IONE QC Finding Appeal",
		filters=_dated_filters(
			"IONE QC Finding Appeal",
			"submitted_at",
			start,
			end,
			scope_filters,
		),
		fields=("name", "finding", "status"),
		order_by="submitted_at asc, name asc",
		limit=REPORT_MAX_SOURCE_ROWS,
	)
	appeal_finding_names = sorted(
		{str(_value(row, "finding") or "") for row in appeals if _value(row, "finding")}
	)
	appeal_findings = (
		_permissioned_rows(
			"IONE QC Finding",
			filters=[["IONE QC Finding", "name", "in", appeal_finding_names]],
			fields=("name", "rule", "rule_version"),
			order_by="name asc",
			limit=REPORT_MAX_SOURCE_ROWS,
		)
		if appeal_finding_names
		else []
	)
	finding_lineage = {
		str(_value(row, "name")): (
			str(_value(row, "rule") or ""),
			str(_value(row, "rule_version") or ""),
		)
		for row in appeal_findings
	}

	metrics: dict[tuple[str, str], dict[str, int]] = {}
	for row in execution_groups:
		key = _rule_key(row)
		result = str(_value(row, "result") or "")
		if result not in _EXECUTION_RESULTS:
			frappe.throw("Rule quality report encountered an invalid execution result")
		metric = metrics.setdefault(key, _new_rule_metric())
		metric[_execution_metric_name(result)] += _nonnegative_int(_value(row, "total"), "total")
	for row in finding_groups:
		key = _rule_key(row)
		metrics.setdefault(key, _new_rule_metric())["finding_count"] += _nonnegative_int(
			_value(row, "total"),
			"total",
		)
	unattributed_appeals = 0
	for appeal in appeals:
		finding_name = str(_value(appeal, "finding") or "")
		key = finding_lineage.get(finding_name)
		if key is None:
			unattributed_appeals += 1
			continue
		status = str(_value(appeal, "status") or "")
		if status not in _APPEAL_STATUSES:
			frappe.throw("Rule quality report encountered an invalid appeal status")
		metrics.setdefault(key, _new_rule_metric())[f"appeal_{status.lower()}"] += 1

	if len(metrics) > REPORT_MAX_OUTPUT_ROWS:
		_report_limit_error("rule-quality output rows", REPORT_MAX_OUTPUT_ROWS)
	rule_names = sorted({key[0] for key in metrics if key[0]})
	version_names = sorted({key[1] for key in metrics if key[1]})
	rules = _definition_map(
		"IONE QC Rule",
		rule_names,
		("name", "rule_code", "rule_name"),
	)
	versions = _definition_map(
		"IONE QC Rule Version",
		version_names,
		("name", "version", "status", "checksum"),
	)

	data: list[dict[str, Any]] = []
	for (rule, rule_version), metric in metrics.items():
		execution_count = sum(metric[_execution_metric_name(result)] for result in _EXECUTION_RESULTS)
		reviewed_appeals = metric["appeal_approved"] + metric["appeal_rejected"]
		rule_row = rules.get(rule, {})
		version_row = versions.get(rule_version, {})
		data.append(
			{
				"rule": rule or None,
				"rule_code": _value(rule_row, "rule_code"),
				"rule_name": _value(rule_row, "rule_name"),
				"rule_version": rule_version or None,
				"version": _value(version_row, "version"),
				"version_status": _value(version_row, "status"),
				"version_checksum": _value(version_row, "checksum"),
				"execution_count": execution_count,
				"passed_count": metric["passed_count"],
				"failed_count": metric["failed_count"],
				"failed_hit_rate": _percentage(metric["failed_count"], execution_count),
				"excluded_count": metric["excluded_count"],
				"excluded_exemption_rate": _percentage(metric["excluded_count"], execution_count),
				"insufficient_data_count": metric["insufficient_data_count"],
				"error_count": metric["error_count"],
				"finding_count": metric["finding_count"],
				"appeal_count": sum(metric[f"appeal_{status.lower()}"] for status in _APPEAL_STATUSES),
				"appeal_reviewed_count": reviewed_appeals,
				"appeal_submitted_count": metric["appeal_submitted"],
				"appeal_approved_count": metric["appeal_approved"],
				"appeal_rejected_count": metric["appeal_rejected"],
				"appeal_withdrawn_count": metric["appeal_withdrawn"],
				"human_appeal_overturn_rate": _percentage(
					metric["appeal_approved"],
					reviewed_appeals,
				),
			}
		)
	data.sort(
		key=lambda row: (
			str(row.get("rule_code") or ""),
			str(row.get("rule") or ""),
			str(row.get("version") or ""),
			str(row.get("rule_version") or ""),
		)
	)
	totals = _sum_fields(
		data,
		(
			"execution_count",
			"passed_count",
			"failed_count",
			"excluded_count",
			"insufficient_data_count",
			"error_count",
			"finding_count",
			"appeal_reviewed_count",
			"appeal_submitted_count",
			"appeal_approved_count",
			"appeal_rejected_count",
			"appeal_withdrawn_count",
		),
	)
	message = (
		"Human Appeal Overturn Rate is exactly Approved / (Approved + Rejected) appeals. "
		"Submitted, withdrawn, or otherwise unreviewed appeals are never classified as false positives. "
		f"{unattributed_appeals} permission-filtered appeals could not be attributed to a visible rule."
	)
	chart = _category_chart(
		list(_EXECUTION_RESULTS),
		[
			totals["passed_count"],
			totals["failed_count"],
			totals["excluded_count"],
			totals["insufficient_data_count"],
			totals["error_count"],
		],
		"Executions",
	)
	summary = [
		_summary("Executions", totals["execution_count"]),
		_summary("Failed Hits", totals["failed_count"]),
		_summary("Excluded (Exemptions)", totals["excluded_count"]),
		_summary("Reviewed Appeals", totals["appeal_reviewed_count"]),
		_summary("Approved Appeals (Human Overturns)", totals["appeal_approved_count"]),
	]
	return _rule_quality_columns(), data, message, chart, summary


def execute_indicator_profile_report(
	filters: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, dict[str, Any], list[dict[str, Any]]]:
	values, start, end = _report_context("indicator_profile", filters)
	extra = _scope_filters(values)
	indicator_filter = _optional_filter(values, "indicator")
	if indicator_filter:
		extra["indicator"] = indicator_filter
	rows = _permissioned_rows(
		"IONE Indicator Result",
		filters=_dated_filters(
			"IONE Indicator Result",
			"period_start",
			start,
			end,
			extra,
		),
		fields=(
			"name",
			"indicator",
			"indicator_version",
			"calculation",
			"period",
			"period_start",
			"period_end",
			"hospital",
			"campus",
			"department",
			"ward",
			"numerator",
			"denominator",
			"indicator_value",
			"target_value",
			"target_min",
			"target_max",
			"status",
			"computed_at",
			"calculator_key",
			"calculator_version",
			"result_revision",
			"input_receipt_hash",
			"result_checksum",
		),
		order_by="period_start asc, indicator asc, name asc",
		limit=REPORT_MAX_OUTPUT_ROWS,
	)
	indicators = _definition_map(
		"IONE QC Indicator",
		sorted({str(_value(row, "indicator")) for row in rows if _value(row, "indicator")}),
		("name", "indicator_code", "indicator_name", "direction"),
	)
	versions = _definition_map(
		"IONE QC Indicator Version",
		sorted({str(_value(row, "indicator_version")) for row in rows if _value(row, "indicator_version")}),
		("name", "version", "status", "effective_from", "effective_to", "checksum"),
	)
	data: list[dict[str, Any]] = []
	for row in rows:
		indicator = str(_value(row, "indicator") or "")
		version = str(_value(row, "indicator_version") or "")
		definition = indicators.get(indicator, {})
		version_definition = versions.get(version, {})
		result_status = str(_value(row, "status") or "")
		if result_status not in _INDICATOR_RESULT_STATUSES:
			frappe.throw("Indicator profile encountered an invalid result status")
		data.append(
			{
				"indicator_result": _value(row, "name"),
				"period": _value(row, "period"),
				"period_start": _value(row, "period_start"),
				"period_end": _value(row, "period_end"),
				"indicator": indicator or None,
				"indicator_code": _value(definition, "indicator_code"),
				"indicator_name": _value(definition, "indicator_name"),
				"indicator_version": version or None,
				"version": _value(version_definition, "version"),
				"version_status": _value(version_definition, "status"),
				"version_checksum": _value(version_definition, "checksum"),
				"effective_from": _value(version_definition, "effective_from"),
				"effective_to": _value(version_definition, "effective_to"),
				"direction": _value(definition, "direction"),
				"hospital": _value(row, "hospital"),
				"campus": _value(row, "campus"),
				"department": _value(row, "department"),
				"ward": _value(row, "ward"),
				"numerator": _value(row, "numerator"),
				"denominator": _value(row, "denominator"),
				"indicator_value": _value(row, "indicator_value"),
				"target_value": _value(row, "target_value"),
				"target_min": _value(row, "target_min"),
				"target_max": _value(row, "target_max"),
				"result_status": result_status,
				"calculation": _value(row, "calculation"),
				"computed_at": _value(row, "computed_at"),
				"calculator_key": _value(row, "calculator_key"),
				"calculator_version": _value(row, "calculator_version"),
				"result_revision": _value(row, "result_revision"),
				"input_receipt_hash": _value(row, "input_receipt_hash"),
				"result_checksum": _value(row, "result_checksum"),
			}
		)
	status_counts = _count_values(data, "result_status")
	chart = {
		"data": {
			"labels": [
				" | ".join(
					(
						str(row.get("period_start") or row.get("period") or ""),
						str(row.get("indicator_code") or row.get("indicator") or ""),
						str(row.get("department") or row.get("hospital") or "Organization"),
					)
				)
				for row in data
			],
			"datasets": [
				{
					"name": "Recorded Indicator Value",
					"values": [row.get("indicator_value") for row in data],
				}
			],
		},
		"type": "line",
	}
	summary = [
		_summary("Result Records", len(data)),
		_summary("Met", status_counts.get("Met", 0)),
		_summary(
			"Below/Warning/Critical",
			sum(status_counts.get(status, 0) for status in ("Below Target", "Warning", "Critical")),
		),
		_summary("No Data", status_counts.get("No Data", 0)),
		_summary("Failed", status_counts.get("Failed", 0)),
	]
	message = (
		"Every chart point is one permission-filtered Indicator Result; values are not averaged "
		"or otherwise reinterpreted. Result revision, input receipt, result checksum, "
		"calculation, and version checksum columns provide the governed lineage links."
	)
	return _indicator_profile_columns(), data, message, chart, summary


def execute_finding_closure_report(
	filters: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, dict[str, Any], list[dict[str, Any]]]:
	values, start, end = _report_context("finding_closure", filters)
	scope_filters = _scope_filters(values)
	findings = _permissioned_rows(
		"IONE QC Finding",
		filters=_dated_filters(
			"IONE QC Finding",
			"detected_at",
			start,
			end,
			scope_filters,
		),
		fields=("name", "department", "severity", "status", "due_date"),
		order_by="department asc, severity asc, name asc",
		limit=REPORT_MAX_SOURCE_ROWS,
	)
	finding_names = sorted({str(_value(row, "name")) for row in findings if _value(row, "name")})
	rectifications = (
		_permissioned_rows(
			"IONE QC Rectification",
			filters=_linked_scope_filters(
				"IONE QC Rectification",
				"finding",
				finding_names,
				scope_filters,
			),
			fields=("name", "finding", "department", "status", "due_date"),
			order_by="finding asc, name asc",
			limit=REPORT_MAX_SOURCE_ROWS,
		)
		if finding_names
		else []
	)
	verifications = (
		_permissioned_rows(
			"IONE QC Verification",
			filters=_linked_scope_filters(
				"IONE QC Verification",
				"finding",
				finding_names,
				scope_filters,
			),
			fields=("name", "finding", "department", "status", "verification_result"),
			order_by="finding asc, name asc",
			limit=REPORT_MAX_SOURCE_ROWS,
		)
		if finding_names
		else []
	)
	finding_groups: dict[str, tuple[str, str]] = {}
	groups: dict[tuple[str, str], dict[str, int]] = {}
	as_of = getdate(nowdate())
	for row in findings:
		department = str(_value(row, "department") or "")
		severity = str(_value(row, "severity") or "")
		status = str(_value(row, "status") or "")
		if severity not in _FINDING_SEVERITIES or status not in _FINDING_STATUSES:
			frappe.throw("Finding closure report encountered invalid finding semantics")
		key = (department, severity)
		finding_groups[str(_value(row, "name"))] = key
		metric = groups.setdefault(key, _new_closure_metric())
		metric["finding_count"] += 1
		if status == _FINDING_CLOSED_STATUS:
			metric["closed_findings"] += 1
		elif status == _FINDING_OVERTURNED_STATUS:
			metric["appeal_overturned_findings"] += 1
		else:
			metric["active_findings"] += 1
			if _is_overdue(_value(row, "due_date"), as_of):
				metric["overdue_findings"] += 1
	for row in rectifications:
		key = finding_groups.get(str(_value(row, "finding") or ""))
		if key is None:
			continue
		metric = groups[key]
		metric["rectification_count"] += 1
		status = str(_value(row, "status") or "")
		if status not in _RECTIFICATION_STATUSES:
			frappe.throw("Finding closure report encountered an invalid rectification status")
		if status == "Closed":
			metric["closed_rectifications"] += 1
		elif status in _RECTIFICATION_TERMINAL_NONCLOSED:
			metric["rejected_cancelled_rectifications"] += 1
		else:
			metric["active_rectifications"] += 1
			if _is_overdue(_value(row, "due_date"), as_of):
				metric["overdue_rectifications"] += 1
	for row in verifications:
		key = finding_groups.get(str(_value(row, "finding") or ""))
		if key is None:
			continue
		metric = groups[key]
		metric["verification_count"] += 1
		status = str(_value(row, "status") or "")
		result = str(_value(row, "verification_result") or "")
		if status not in _VERIFICATION_STATUSES or result not in _VERIFICATION_RESULTS:
			frappe.throw("Finding closure report encountered invalid verification semantics")
		if status == "Verified" and result == "Effective":
			metric["effective_verifications"] += 1
		elif status == "Verified" and result == "Partially Effective":
			metric["partially_effective_verifications"] += 1
		elif status == "Verified" and result == "Ineffective":
			metric["ineffective_verifications"] += 1
		elif status == "Rejected":
			metric["rejected_verifications"] += 1
		else:
			metric["pending_verifications"] += 1
	if len(groups) > REPORT_MAX_OUTPUT_ROWS:
		_report_limit_error("finding-closure output rows", REPORT_MAX_OUTPUT_ROWS)
	data = [
		{"department": department or None, "severity": severity, **metric}
		for (department, severity), metric in sorted(groups.items())
	]
	departments = sorted({str(row.get("department") or "Unassigned") for row in data})
	severities = ("Low", "Medium", "High", "Critical", "Unspecified")
	chart = {
		"data": {
			"labels": departments,
			"datasets": [
				{
					"name": severity,
					"values": [
						sum(
							int(row["finding_count"])
							for row in data
							if str(row.get("department") or "Unassigned") == department
							and row["severity"] == severity
						)
						for department in departments
					],
				}
				for severity in severities
				if any(row["severity"] == severity for row in data)
			],
		},
		"type": "bar",
	}
	totals = _sum_fields(
		data,
		(
			"finding_count",
			"active_findings",
			"closed_findings",
			"appeal_overturned_findings",
			"overdue_findings",
			"active_rectifications",
			"overdue_rectifications",
		),
	)
	summary = [
		_summary("Findings", totals["finding_count"]),
		_summary("Active Findings", totals["active_findings"]),
		_summary("Closed Findings", totals["closed_findings"]),
		_summary("Human Appeal Overturns", totals["appeal_overturned_findings"]),
		_summary("Overdue Findings", totals["overdue_findings"]),
		_summary("Overdue Rectifications", totals["overdue_rectifications"]),
	]
	message = (
		f"The report is a cohort of findings detected from {start.isoformat()} through "
		f"{end.isoformat()}. Rectification and verification state is current as of {as_of.isoformat()}. "
		"Appeal Approved is reported separately from operational closure."
	)
	return _finding_closure_columns(), data, message, chart, summary


def execute_integration_reconciliation_report(
	filters: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, dict[str, Any], list[dict[str, Any]]]:
	values, start, end = _report_context("integration_reconciliation", filters)
	extra: dict[str, Any] = {}
	for fieldname in ("source_system", "endpoint", "status"):
		value = _optional_filter(values, fieldname)
		if value:
			extra[fieldname] = value
	rows = _permissioned_rows(
		"IONE Data Reconciliation",
		filters=_dated_filters(
			"IONE Data Reconciliation",
			"period_start",
			start,
			end,
			extra,
		),
		fields=(
			"name",
			"source_system",
			"endpoint",
			"period_start",
			"period_end",
			"status",
			"source_count",
			"message_count",
			"event_count",
			"execution_count",
			"error_count",
			"difference_count",
			"source_hash",
			"qms_hash",
			"started_at",
			"completed_at",
		),
		order_by="period_start asc, source_system asc, endpoint asc, name asc",
		limit=REPORT_MAX_OUTPUT_ROWS,
	)
	data = [
		{
			"reconciliation": _value(row, "name"),
			"source_system": _value(row, "source_system"),
			"endpoint": _value(row, "endpoint"),
			"period_start": _value(row, "period_start"),
			"period_end": _value(row, "period_end"),
			"status": _value(row, "status"),
			"source_count": _value(row, "source_count"),
			"message_count": _value(row, "message_count"),
			"event_count": _value(row, "event_count"),
			"execution_count": _value(row, "execution_count"),
			"error_count": _value(row, "error_count"),
			"difference_count": _value(row, "difference_count"),
			"source_hash": _value(row, "source_hash"),
			"qms_hash": _value(row, "qms_hash"),
			"started_at": _value(row, "started_at"),
			"completed_at": _value(row, "completed_at"),
		}
		for row in rows
	]
	for row in data:
		if str(row.get("status") or "") not in _RECONCILIATION_STATUSES:
			frappe.throw("Reconciliation report encountered an invalid lifecycle status")
	status_counts = _count_values(data, "status")
	chart = _category_chart(
		sorted(status_counts),
		[status_counts[status] for status in sorted(status_counts)],
		"Reconciliation Records",
	)
	summary = [
		_summary("Reconciliation Records", len(data)),
		_summary("Matched", status_counts.get("Matched", 0)),
		_summary("Mismatch", status_counts.get("Mismatch", 0)),
		_summary("Failed", status_counts.get("Failed", 0)),
		_summary("Pending/Reviewed", status_counts.get("Pending", 0) + status_counts.get("Reviewed", 0)),
	]
	message = (
		"Counts are shown per reconciliation record. The report does not sum source volumes "
		"across potentially overlapping reconciliation windows."
	)
	return _integration_reconciliation_columns(), data, message, chart, summary


def execute_ai_usage_report(
	filters: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, dict[str, Any], list[dict[str, Any]]]:
	values, start, end = _report_context("ai_usage", filters)
	extra = _scope_filters(values)
	for fieldname in ("task_type", "policy", "flow_agent"):
		value = _optional_filter(values, fieldname)
		if value:
			extra[fieldname] = value
	facts = _permissioned_rows(
		"IONE Agent Analysis Fact",
		filters=_dated_filters(
			"IONE Agent Analysis Fact",
			"fact_date",
			start,
			end,
			extra,
		),
		fields=(
			"name",
			"analysis_task",
			"fact_date",
			"flow_agent",
			"policy",
			"flow_model",
			"model_id",
			"task_type",
			"task_status",
			"requires_human_review",
			"duration_ms",
			"task_count",
			"success_count",
			"failure_count",
			"input_tokens",
			"output_tokens",
			"total_tokens",
			"adopted_count",
			"hospital",
			"campus",
			"department",
			"ward",
		),
		order_by="fact_date asc, task_type asc, policy asc, name asc",
		limit=REPORT_MAX_SOURCE_ROWS,
	)
	groups: dict[tuple[str, ...], dict[str, Any]] = {}
	for fact in facts:
		fact_date = _stored_date(_value(fact, "fact_date"), "fact_date")
		task_count = _nonnegative_int(_value(fact, "task_count"), "task_count")
		success_count = _nonnegative_int(_value(fact, "success_count"), "success_count")
		failure_count = _nonnegative_int(_value(fact, "failure_count"), "failure_count")
		task_status = str(_value(fact, "task_status") or "")
		if task_status not in _AI_TASK_STATUSES:
			frappe.throw("AI usage fact has an invalid governed task status")
		expected_success = int(task_status in {"Completed", "Reviewed"})
		expected_failure = int(task_status in {"Failed", "Rejected", "Cancelled"})
		if task_count != 1 or success_count != expected_success or failure_count != expected_failure:
			frappe.throw(
				"AI usage fact does not satisfy the one-governed-task fact contract",
				frappe.ValidationError,
			)
		adopted_count = _nonnegative_int(_value(fact, "adopted_count"), "adopted_count")
		key = (
			fact_date.isoformat(),
			str(_value(fact, "task_type") or ""),
			str(_value(fact, "policy") or ""),
			str(_value(fact, "flow_agent") or ""),
			str(_value(fact, "flow_model") or ""),
			str(_value(fact, "model_id") or ""),
			str(_value(fact, "hospital") or ""),
			str(_value(fact, "campus") or ""),
			str(_value(fact, "department") or ""),
			str(_value(fact, "ward") or ""),
		)
		metric = groups.setdefault(key, _new_ai_metric())
		metric["task_count"] += task_count
		metric["success_count"] += success_count
		metric["failure_count"] += failure_count
		requires_human_review = _nonnegative_int(
			_value(fact, "requires_human_review"),
			"requires_human_review",
		)
		if requires_human_review not in {0, 1}:
			frappe.throw("requires_human_review must be 0 or 1", frappe.ValidationError)
		metric["human_review_required_count"] += requires_human_review
		metric["accepted_candidate_count"] += adopted_count
		metric["successful_tasks_with_accepted_candidate"] += int(success_count > 0 and adopted_count > 0)
		for fieldname in ("input_tokens", "output_tokens", "total_tokens"):
			metric[fieldname] += _nonnegative_int(_value(fact, fieldname), fieldname)
		duration = _optional_nonnegative_int(_value(fact, "duration_ms"), "duration_ms")
		if duration is not None:
			metric["duration_total_ms"] += duration
			metric["duration_observation_count"] += 1
	if len(groups) > REPORT_MAX_OUTPUT_ROWS:
		_report_limit_error("AI-usage output rows", REPORT_MAX_OUTPUT_ROWS)
	data: list[dict[str, Any]] = []
	for key, metric in sorted(groups.items()):
		(
			fact_date,
			task_type,
			policy,
			flow_agent,
			flow_model,
			model_id,
			hospital,
			campus,
			department,
			ward,
		) = key
		data.append(
			{
				"fact_date": fact_date,
				"task_type": task_type or None,
				"policy": policy or None,
				"flow_agent": flow_agent or None,
				"flow_model": flow_model or None,
				"model_id": model_id or None,
				"hospital": hospital or None,
				"campus": campus or None,
				"department": department or None,
				"ward": ward or None,
				"task_count": metric["task_count"],
				"success_count": metric["success_count"],
				"failure_count": metric["failure_count"],
				"other_task_count": (
					metric["task_count"] - metric["success_count"] - metric["failure_count"]
				),
				"human_review_required_count": metric["human_review_required_count"],
				"successful_tasks_with_accepted_candidate": metric[
					"successful_tasks_with_accepted_candidate"
				],
				"accepted_candidate_count": metric["accepted_candidate_count"],
				"successful_task_acceptance_rate": _percentage(
					metric["successful_tasks_with_accepted_candidate"],
					metric["success_count"],
				),
				"average_duration_ms": (
					round(
						metric["duration_total_ms"] / metric["duration_observation_count"],
						2,
					)
					if metric["duration_observation_count"]
					else None
				),
				"input_tokens": metric["input_tokens"],
				"output_tokens": metric["output_tokens"],
				"total_tokens": metric["total_tokens"],
			}
		)
	daily: dict[str, dict[str, int]] = {}
	for row in data:
		metric = daily.setdefault(
			str(row["fact_date"]),
			{"task_count": 0, "success_count": 0, "accepted_tasks": 0},
		)
		metric["task_count"] += int(row["task_count"])
		metric["success_count"] += int(row["success_count"])
		metric["accepted_tasks"] += int(row["successful_tasks_with_accepted_candidate"])
	chart = {
		"data": {
			"labels": sorted(daily),
			"datasets": [
				{
					"name": "Governed Tasks",
					"values": [daily[day]["task_count"] for day in sorted(daily)],
				},
				{
					"name": "Successful Tasks",
					"values": [daily[day]["success_count"] for day in sorted(daily)],
				},
				{
					"name": "Successful Tasks With Accepted Candidate",
					"values": [daily[day]["accepted_tasks"] for day in sorted(daily)],
				},
			],
		},
		"type": "line",
	}
	totals = _sum_fields(
		data,
		(
			"task_count",
			"success_count",
			"failure_count",
			"successful_tasks_with_accepted_candidate",
			"accepted_candidate_count",
			"total_tokens",
		),
	)
	summary = [
		_summary("Governed AI Tasks", totals["task_count"]),
		_summary("Successful Tasks", totals["success_count"]),
		_summary("Failed/Rejected/Cancelled Tasks", totals["failure_count"]),
		_summary(
			"Successful Tasks With Accepted Candidate",
			totals["successful_tasks_with_accepted_candidate"],
		),
		_summary("Accepted Candidates", totals["accepted_candidate_count"]),
		_summary("Total Tokens", totals["total_tokens"]),
	]
	message = (
		"Successful Task Acceptance Rate is exactly successful tasks with at least one accepted "
		"candidate divided by successful tasks. Accepted Candidate Count may exceed task count and "
		"is therefore reported separately, never as a percentage."
	)
	return _ai_usage_columns(), data, message, chart, summary


def execute_medical_record_review_report(
	filters: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, dict[str, Any], list[dict[str, Any]]]:
	values, start, end = _report_context("medical_record_review", filters)
	scope = _scope_filters(values)
	policy_filter = _optional_filter(values, "policy")
	if policy_filter:
		scope["policy"] = policy_filter
	batches = _permissioned_rows(
		"IONE Medical Record Review Batch",
		filters=_period_overlap_filters(
			"IONE Medical Record Review Batch",
			"period_start",
			"period_end",
			start,
			end,
			scope,
		),
		fields=(
			"name",
			"policy",
			"policy_checksum",
			"period_start",
			"period_end",
			"hospital",
			"campus",
			"department",
			"ward",
			"population_count",
			"sample_count",
			"expert_sample_count",
			"status",
		),
		order_by="period_start asc, policy asc, name asc",
		limit=REPORT_MAX_OUTPUT_ROWS,
	)
	if not batches:
		data = [_medical_review_no_data_row(start, end, values)]
		return (
			_medical_record_review_columns(),
			data,
			"No Data: no permission-filtered review batch overlaps the selected period and scope.",
			_category_chart(["No Data"], [0], "Visible Assignments"),
			[_summary("Review Batches", 0), _summary("Visible Assignments", 0)],
		)

	batch_names = [str(_value(row, "name")) for row in batches]
	assignments = _permissioned_rows(
		"IONE Medical Record Review Assignment",
		filters=_linked_scope_filters(
			"IONE Medical Record Review Assignment",
			"batch",
			batch_names,
			_scope_filters(values),
		),
		fields=(
			"name",
			"batch",
			"status",
			"coder_required",
			"expert_required",
			"deterministic_gate_status",
			"latest_archive_decision",
			"archive_acknowledgement",
		),
		order_by="batch asc, name asc",
		limit=REPORT_MAX_SOURCE_ROWS,
	)
	assignment_names = [str(_value(row, "name")) for row in assignments]
	decisions = (
		_permissioned_rows(
			"IONE Medical Record Review Decision",
			filters=[["IONE Medical Record Review Decision", "assignment", "in", assignment_names]],
			fields=(
				"batch",
				"stage",
				"outcome",
				"decision_checksum",
			),
			order_by="batch asc, assignment asc, stage asc, name asc",
			limit=REPORT_MAX_SOURCE_ROWS,
		)
		if assignment_names
		else []
	)
	archive_names = sorted(
		{
			str(_value(row, "latest_archive_decision"))
			for row in assignments
			if _value(row, "latest_archive_decision")
		}
	)
	archives = (
		_permissioned_rows(
			"IONE Medical Record Archive Decision",
			filters=[["IONE Medical Record Archive Decision", "name", "in", archive_names]],
			fields=(
				"name",
				"decision",
				"decision_checksum",
			),
			order_by="batch asc, assignment asc, name asc",
			limit=REPORT_MAX_SOURCE_ROWS,
		)
		if archive_names
		else []
	)
	ack_names = sorted(
		{
			str(_value(row, "archive_acknowledgement"))
			for row in assignments
			if _value(row, "archive_acknowledgement")
		}
	)
	acknowledgements = (
		_permissioned_rows(
			"IONE Medical Record Archive Acknowledgement",
			filters=[["IONE Medical Record Archive Acknowledgement", "name", "in", ack_names]],
			fields=(
				"name",
				"ack_status",
				"request_hash",
				"decision_checksum",
			),
			order_by="assignment asc, name asc",
			limit=REPORT_MAX_SOURCE_ROWS,
		)
		if ack_names
		else []
	)
	policies = _definition_map(
		"IONE Medical Record Sampling Policy",
		sorted({str(_value(row, "policy")) for row in batches if _value(row, "policy")}),
		(
			"name",
			"policy_code",
			"policy_version",
			"effective_from",
			"effective_to",
			"status",
			"archive_gate_enabled",
			"approval_checksum",
		),
	)

	assignments_by_batch = _rows_by_value(assignments, "batch")
	decisions_by_batch = _rows_by_value(decisions, "batch")
	archives_by_name = {str(_value(row, "name")): row for row in archives}
	acks_by_name = {str(_value(row, "name")): row for row in acknowledgements}
	data: list[dict[str, Any]] = []
	for batch in batches:
		batch_name = str(_value(batch, "name") or "")
		policy_name = str(_value(batch, "policy") or "")
		policy = policies.get(policy_name, {})
		metric = _new_medical_review_metric()
		visible_assignments = assignments_by_batch.get(batch_name, [])
		for assignment in visible_assignments:
			status = str(_value(assignment, "status") or "")
			if status not in _MEDICAL_REVIEW_STATUSES:
				frappe.throw("Medical record review report encountered an invalid assignment status")
			metric["visible_assignment_count"] += 1
			metric[_medical_assignment_status_field(status)] += 1
			if int(_value(assignment, "coder_required") or 0):
				metric["coder_required_count"] += 1
			if int(_value(assignment, "expert_required") or 0):
				metric["expert_required_count"] += 1
			gate_status = str(_value(assignment, "deterministic_gate_status") or "")
			if gate_status not in {"Passed", "Held"}:
				frappe.throw("Medical record review report encountered an invalid deterministic gate")
			metric[f"deterministic_{gate_status.lower()}_count"] += 1
			archive_name = str(_value(assignment, "latest_archive_decision") or "")
			archive = archives_by_name.get(archive_name)
			if archive_name:
				metric["latest_archive_decision_count"] += 1
			if archive:
				archive_decision = str(_value(archive, "decision") or "")
				if archive_decision not in _ARCHIVE_DECISIONS:
					frappe.throw("Medical record review report encountered an invalid archive decision")
				metric[f"archive_{archive_decision.lower()}_count"] += 1
				if _has_checksum(_value(archive, "decision_checksum")):
					metric["archive_checksum_count"] += 1
			ack_name = str(_value(assignment, "archive_acknowledgement") or "")
			ack = acks_by_name.get(ack_name)
			if ack_name:
				metric["archive_ack_count"] += 1
			if ack:
				ack_status = str(_value(ack, "ack_status") or "")
				if ack_status not in _ARCHIVE_ACK_STATUSES:
					frappe.throw(
						"Medical record review report encountered an invalid archive acknowledgement"
					)
				metric[f"archive_ack_{ack_status.lower()}_count"] += 1
				if _has_checksum(_value(ack, "request_hash")) and _has_checksum(
					_value(ack, "decision_checksum")
				):
					metric["archive_ack_receipt_count"] += 1
		for decision in decisions_by_batch.get(batch_name, []):
			stage = str(_value(decision, "stage") or "")
			outcome = str(_value(decision, "outcome") or "")
			if stage not in _MEDICAL_REVIEW_STAGES or outcome not in _MEDICAL_REVIEW_OUTCOMES:
				frappe.throw("Medical record review report encountered invalid review semantics")
			prefix = stage.lower()
			metric[f"{prefix}_decision_count"] += 1
			metric[f"{prefix}_{_outcome_field(outcome)}_count"] += 1
			if _has_checksum(_value(decision, "decision_checksum")):
				metric["review_decision_checksum_count"] += 1

		population_count = _nonnegative_int(_value(batch, "population_count"), "population_count")
		sample_count = _nonnegative_int(_value(batch, "sample_count"), "sample_count")
		expert_sample_count = _nonnegative_int(
			_value(batch, "expert_sample_count"),
			"expert_sample_count",
		)
		if sample_count > population_count or expert_sample_count > sample_count:
			frappe.throw("Medical record review batch stored counts are inconsistent")
		data.append(
			{
				"data_state": "Data",
				"batch": batch_name,
				"period_start": _value(batch, "period_start"),
				"period_end": _value(batch, "period_end"),
				"hospital": _value(batch, "hospital"),
				"campus": _value(batch, "campus"),
				"department": _value(batch, "department"),
				"ward": _value(batch, "ward"),
				"policy": policy_name or None,
				"policy_code": _value(policy, "policy_code"),
				"policy_version": _value(policy, "policy_version"),
				"policy_status": _value(policy, "status"),
				"policy_checksum": _value(batch, "policy_checksum") or _value(policy, "approval_checksum"),
				"archive_gate_enabled": int(_value(policy, "archive_gate_enabled") or 0),
				"batch_status": _value(batch, "status"),
				"population_count": population_count,
				"sample_count": sample_count,
				"expert_sample_count": expert_sample_count,
				**metric,
				"source_row_count": len(visible_assignments)
				+ len(decisions_by_batch.get(batch_name, []))
				+ sum(
					1
					for row in visible_assignments
					if str(_value(row, "latest_archive_decision") or "") in archives_by_name
				)
				+ sum(
					1
					for row in visible_assignments
					if str(_value(row, "archive_acknowledgement") or "") in acks_by_name
				),
			}
		)
	totals = _sum_fields(
		data,
		(
			"population_count",
			"sample_count",
			*_new_medical_review_metric(),
		),
	)
	chart = _category_chart(
		["Coder Review", "Expert Review", "Closed"],
		[
			totals["coder_review_assignment_count"],
			totals["expert_review_assignment_count"],
			totals["closed_assignment_count"],
		],
		"Permission-filtered Assignments",
	)
	summary = [
		_summary("Review Batches", len(data)),
		_summary("Declared Population", totals["population_count"]),
		_summary("Declared Sample", totals["sample_count"]),
		_summary("Visible Assignments", totals["visible_assignment_count"]),
		_summary("Deterministic Holds", totals["deterministic_held_count"]),
		_summary("Coder Decisions", totals["coder_decision_count"]),
		_summary("Expert Decisions", totals["expert_decision_count"]),
		_summary("Applied Archive ACKs", totals["archive_ack_applied_count"]),
	]
	message = (
		"Counts are grouped by immutable sampling batch and frozen policy checksum. Assignment, "
		"review-decision, latest archive-decision, and acknowledgement reads are permission-filtered; "
		"visible counts can therefore be lower than declared batch counts. Review outcomes are human "
		"decisions and are never labelled as false positives. Every visible population assignment "
		"has a deterministic gate state and durable archive disposition. No clinical identifiers or narratives "
		"are selected."
	)
	return _medical_record_review_columns(), data, message, chart, summary


def execute_surgery_governance_report(
	filters: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, dict[str, Any], list[dict[str, Any]]]:
	values, start, end = _report_context("surgery_governance", filters)
	extra = _scope_filters(values)
	level_filter = _optional_filter(values, "surgery_level")
	if level_filter:
		if level_filter not in _SURGERY_LEVELS:
			frappe.throw("surgery_level is not supported", frappe.ValidationError)
		extra["surgery_level"] = level_filter
	rows = _permissioned_rows(
		"IONE Surgery QC",
		filters=_dated_filters("IONE Surgery QC", "surgery_time", start, end, extra),
		fields=(
			"surgery_time",
			"surgery_level",
			"surgery_phase",
			"procedure_policy",
			"surgery_authorization",
			"mdt_record",
			"safety_checklist",
			"emergency_exception",
			"governance_state",
			"governance_checksum",
			"compliant",
			"preanesthesia_status",
			"intraoperative_monitoring_status",
			"recovery_status",
			"postoperative_followup_status",
			"cancellation_status",
			"unplanned_surgery_status",
			"unplanned_return_status",
			"complication_status",
			"mortality_status",
			"hospital",
			"campus",
			"department",
			"ward",
		),
		order_by="surgery_time asc, department asc, surgery_level asc, name asc",
		limit=REPORT_MAX_SOURCE_ROWS,
	)
	if not rows:
		data = [_surgery_no_data_row(start, end, values)]
		return (
			_surgery_governance_columns(),
			data,
			"No Data: no permission-filtered surgery governance record is in the selected period.",
			_category_chart(["No Data"], [0], "Surgery Records"),
			[_summary("Surgery Records", 0)],
		)
	policy_names = sorted(
		{str(_value(row, "procedure_policy")) for row in rows if _value(row, "procedure_policy")}
	)
	policies = _definition_map(
		"IONE Surgery Procedure Policy",
		policy_names,
		(
			"name",
			"policy_code",
			"policy_version",
			"surgery_level",
			"effective_from",
			"effective_to",
			"require_mdt",
			"status",
			"checksum",
		),
	)
	authorizations = _definition_map(
		"IONE Surgery Authorization",
		sorted(
			{
				str(_value(row, "surgery_authorization"))
				for row in rows
				if _value(row, "surgery_authorization")
			}
		),
		("name", "status", "effective_from", "effective_to", "checksum"),
	)
	mdt_records = _definition_map(
		"IONE Surgery MDT Record",
		sorted({str(_value(row, "mdt_record")) for row in rows if _value(row, "mdt_record")}),
		("name", "status", "conclusion", "checksum"),
	)
	checklists = _definition_map(
		"IONE Surgery Safety Checklist",
		sorted({str(_value(row, "safety_checklist")) for row in rows if _value(row, "safety_checklist")}),
		("name", "status", "checksum"),
	)
	exceptions = _definition_map(
		"IONE Surgery Emergency Exception",
		sorted(
			{str(_value(row, "emergency_exception")) for row in rows if _value(row, "emergency_exception")}
		),
		("name", "status", "checksum"),
	)

	groups: dict[tuple[str, ...], dict[str, int]] = {}
	for row in rows:
		level = str(_value(row, "surgery_level") or "")
		phase = str(_value(row, "surgery_phase") or "")
		governance_state = str(_value(row, "governance_state") or "")
		if (
			level not in _SURGERY_LEVELS
			or phase not in _SURGERY_PHASES
			or governance_state not in _SURGERY_GOVERNANCE_STATES
		):
			frappe.throw("Surgery governance report encountered invalid governed semantics")
		for fieldname in (
			"preanesthesia_status",
			"intraoperative_monitoring_status",
			"recovery_status",
			"postoperative_followup_status",
		):
			if str(_value(row, fieldname) or "") not in _SURGERY_CARE_STATES:
				frappe.throw("Surgery governance report encountered an invalid care-stage status")
		for fieldname in (
			"unplanned_surgery_status",
			"unplanned_return_status",
			"complication_status",
			"mortality_status",
		):
			if str(_value(row, fieldname) or "") not in _SURGERY_OBSERVATION_STATES:
				frappe.throw("Surgery governance report encountered an invalid outcome status")
		if str(_value(row, "cancellation_status") or "") not in _SURGERY_CANCELLATION_STATES:
			frappe.throw("Surgery governance report encountered an invalid cancellation status")
		policy_name = str(_value(row, "procedure_policy") or "")
		key = (
			str(_value(row, "hospital") or ""),
			str(_value(row, "campus") or ""),
			str(_value(row, "department") or ""),
			str(_value(row, "ward") or ""),
			policy_name,
			level,
		)
		metric = groups.setdefault(key, _new_surgery_metric())
		metric["source_record_count"] += 1
		metric[f"governance_{_safe_metric_token(governance_state)}_count"] += 1
		if _has_checksum(_value(row, "governance_checksum")):
			metric["governance_checksum_count"] += 1
		if int(_value(row, "compliant") or 0):
			metric["compliant_count"] += 1
		authorization = authorizations.get(str(_value(row, "surgery_authorization") or ""))
		if _value(row, "surgery_authorization"):
			metric["authorization_linked_count"] += 1
		if (
			authorization
			and str(_value(authorization, "status") or "") == "Approved"
			and _has_checksum(_value(authorization, "checksum"))
		):
			metric["approved_authorization_count"] += 1
		policy = policies.get(policy_name, {})
		if int(_value(policy, "require_mdt") or 0):
			metric["mdt_required_count"] += 1
		mdt = mdt_records.get(str(_value(row, "mdt_record") or ""))
		if _value(row, "mdt_record"):
			metric["mdt_linked_count"] += 1
		if mdt and str(_value(mdt, "status") or "") == "Completed" and _has_checksum(_value(mdt, "checksum")):
			metric["completed_mdt_count"] += 1
			conclusion = str(_value(mdt, "conclusion") or "")
			if conclusion in {"Proceed", "Proceed with Conditions", "Do Not Proceed"}:
				metric[f"mdt_{_safe_metric_token(conclusion)}_count"] += 1
		checklist = checklists.get(str(_value(row, "safety_checklist") or ""))
		if _value(row, "safety_checklist"):
			metric["checklist_linked_count"] += 1
		if (
			checklist
			and str(_value(checklist, "status") or "") == "Completed"
			and _has_checksum(_value(checklist, "checksum"))
		):
			metric["completed_checklist_count"] += 1
		exception = exceptions.get(str(_value(row, "emergency_exception") or ""))
		if (
			exception
			and str(_value(exception, "status") or "") == "Approved"
			and _has_checksum(_value(exception, "checksum"))
		):
			metric["approved_emergency_exception_count"] += 1
		for source_field, prefix in (
			("preanesthesia_status", "preanesthesia"),
			("intraoperative_monitoring_status", "intraoperative"),
			("recovery_status", "recovery"),
			("postoperative_followup_status", "followup"),
		):
			state = str(_value(row, source_field) or "")
			metric[f"{prefix}_{_safe_metric_token(state)}_count"] += 1
		if str(_value(row, "cancellation_status") or "") == "Confirmed":
			metric["cancelled_count"] += 1
		for source_field, output_field in (
			("unplanned_surgery_status", "unplanned_surgery_count"),
			("unplanned_return_status", "unplanned_return_count"),
			("complication_status", "complication_count"),
			("mortality_status", "mortality_count"),
		):
			if str(_value(row, source_field) or "") == "Yes":
				metric[output_field] += 1
	if len(groups) > REPORT_MAX_OUTPUT_ROWS:
		_report_limit_error("surgery-governance output rows", REPORT_MAX_OUTPUT_ROWS)
	data = []
	for (hospital, campus, department, ward, policy_name, level), metric in sorted(groups.items()):
		policy = policies.get(policy_name, {})
		data.append(
			{
				"data_state": "Data",
				"period_start": start,
				"period_end": end,
				"hospital": hospital or None,
				"campus": campus or None,
				"department": department or None,
				"ward": ward or None,
				"surgery_level": level,
				"procedure_policy": policy_name or None,
				"policy_code": _value(policy, "policy_code"),
				"policy_version": _value(policy, "policy_version"),
				"policy_status": _value(policy, "status"),
				"policy_effective_from": _value(policy, "effective_from"),
				"policy_effective_to": _value(policy, "effective_to"),
				"policy_checksum": _value(policy, "checksum"),
				"policy_require_mdt": int(_value(policy, "require_mdt") or 0),
				**metric,
			}
		)
	totals = _sum_fields(data, tuple(_new_surgery_metric()))
	chart = _category_chart(
		[
			"Governance Passed",
			"Governance Noncompliant",
			"Governance Blocked",
			"Pending Prerequisites",
			"Cancelled",
		],
		[
			totals["governance_passed_count"],
			totals["governance_noncompliant_count"],
			totals["governance_blocked_count"],
			totals["governance_pending_prerequisites_count"],
			totals["governance_cancelled_count"],
		],
		"Surgery Records",
	)
	summary = [
		_summary("Surgery Records", totals["source_record_count"]),
		_summary("Governance Passed", totals["governance_passed_count"]),
		_summary("Governance Noncompliant", totals["governance_noncompliant_count"]),
		_summary("Governance Blocked", totals["governance_blocked_count"]),
		_summary("Approved Authorizations", totals["approved_authorization_count"]),
		_summary("Completed MDT Records", totals["completed_mdt_count"]),
		_summary("Completed Safety Checklists", totals["completed_checklist_count"]),
		_summary("Unplanned Returns", totals["unplanned_return_count"]),
		_summary("Complications", totals["complication_count"]),
		_summary("Mortality Observations", totals["mortality_count"]),
	]
	message = (
		"Each row aggregates permission-filtered surgery governance records by exact organization "
		"scope, surgery level, and approved procedure-policy link. Authorization, MDT, checklist, "
		"emergency-exception, care-stage, cancellation, unplanned surgery/return, complication, and "
		"mortality fields are counted exactly as governed states. No patient, encounter, staff, raw "
		"source identifier, or clinical narrative is selected."
	)
	return _surgery_governance_columns(), data, message, chart, summary


def execute_national_ten_goal_report(
	filters: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, dict[str, Any], list[dict[str, Any]]]:
	values, start, end = _report_context("national_ten_goal", filters)
	standard_name = _required_filter(values, "standard")
	standard_rows = _permissioned_rows(
		"IONE QC Standard",
		filters=[["IONE QC Standard", "name", "=", standard_name]],
		fields=(
			"name",
			"standard_code",
			"standard_name",
			"standard_category",
			"source_type",
			"status",
			"issue_date",
		),
		order_by="name asc",
		limit=1,
	)
	if len(standard_rows) != 1:
		frappe.throw("The selected national-goal standard is not visible or does not exist")
	standard = standard_rows[0]
	if (
		str(_value(standard, "source_type") or "") != "National Policy"
		or str(_value(standard, "status") or "") != "Published"
	):
		frappe.throw(
			"The selected standard must be a Published National Policy",
			frappe.ValidationError,
		)
	indicator_filter = _optional_filter(values, "indicator")
	indicator_filters: list[list[Any]] = [
		["IONE QC Indicator", "standard", "=", standard_name],
		["IONE QC Indicator", "status", "=", "Active"],
	]
	if indicator_filter:
		indicator_filters.append(["IONE QC Indicator", "name", "=", indicator_filter])
	indicators = _permissioned_rows(
		"IONE QC Indicator",
		filters=indicator_filters,
		fields=(
			"name",
			"indicator_code",
			"indicator_name",
			"category",
			"standard",
			"standard_clause",
			"direction",
			"status",
		),
		order_by="category asc, indicator_code asc, name asc",
		limit=REPORT_MAX_OUTPUT_ROWS,
	)
	if not indicators:
		data = [_national_goal_no_data_row(start, end, standard)]
		return (
			_national_ten_goal_columns(),
			data,
			"No Data: the selected Published National Policy has no visible Active indicators.",
			_category_chart(["No Data"], [1], "Definition Rows"),
			[_summary("Approved Definition Rows", 0), _summary("No Data Rows", 1)],
		)
	indicator_names = [str(_value(row, "name")) for row in indicators]
	clauses = _definition_map(
		"IONE QC Standard Clause",
		sorted({str(_value(row, "standard_clause")) for row in indicators if _value(row, "standard_clause")}),
		("name", "standard_version", "clause_code", "heading", "status"),
	)
	standard_versions = _definition_map(
		"IONE QC Standard Version",
		sorted(
			{
				str(_value(clause, "standard_version"))
				for clause in clauses.values()
				if _value(clause, "standard_version")
			}
		),
		(
			"name",
			"standard",
			"version",
			"approval_status",
			"effective_from",
			"effective_to",
			"checksum",
		),
	)
	for indicator in indicators:
		clause = clauses.get(str(_value(indicator, "standard_clause") or ""))
		version = standard_versions.get(str(_value(clause, "standard_version") or "")) if clause else None
		if (
			not clause
			or str(_value(clause, "status") or "") != "Published"
			or not version
			or str(_value(version, "standard") or "") != standard_name
			or str(_value(version, "approval_status") or "") != "Approved"
			or not _has_checksum(_value(version, "checksum"))
			or not _periods_overlap(
				_value(version, "effective_from"),
				_value(version, "effective_to"),
				start,
				end,
			)
		):
			frappe.throw(
				"Every reported national-goal indicator requires a Published clause from an "
				"Approved standard version with an immutable checksum"
			)
	all_versions = _permissioned_rows(
		"IONE QC Indicator Version",
		filters=[
			["IONE QC Indicator Version", "indicator", "in", indicator_names],
			["IONE QC Indicator Version", "status", "=", "Published"],
		],
		fields=(
			"name",
			"indicator",
			"version",
			"status",
			"calculation_frequency",
			"unit",
			"target_value",
			"target_min",
			"target_max",
			"direction",
			"effective_from",
			"effective_to",
			"checksum",
		),
		order_by="indicator asc, effective_from asc, version asc, name asc",
		limit=REPORT_MAX_OUTPUT_ROWS,
	)
	versions = [
		row
		for row in all_versions
		if _periods_overlap(
			_value(row, "effective_from"),
			_value(row, "effective_to"),
			start,
			end,
		)
	]
	versions_by_indicator = _rows_by_value(versions, "indicator")
	for indicator in indicators:
		if not versions_by_indicator.get(str(_value(indicator, "name") or "")):
			frappe.throw(
				"Every reported national-goal indicator requires a Published indicator version "
				"effective during the selected report period"
			)
	for version in versions:
		if not _has_checksum(_value(version, "checksum")):
			frappe.throw("Published indicator versions require an immutable checksum")
	result_filters = _period_overlap_filters(
		"IONE Indicator Result",
		"period_start",
		"period_end",
		start,
		end,
		_scope_filters(values),
	)
	result_filters.append(["IONE Indicator Result", "indicator", "in", indicator_names])
	results = _permissioned_rows(
		"IONE Indicator Result",
		filters=result_filters,
		fields=(
			"name",
			"indicator",
			"indicator_version",
			"calculation",
			"period",
			"period_start",
			"period_end",
			"hospital",
			"campus",
			"department",
			"ward",
			"numerator",
			"denominator",
			"indicator_value",
			"target_value",
			"target_min",
			"target_max",
			"status",
			"computed_at",
			"calculator_key",
			"calculator_version",
			"result_revision",
			"input_receipt_hash",
			"result_checksum",
		),
		order_by="indicator asc, period_start asc, name asc",
		limit=REPORT_MAX_OUTPUT_ROWS,
	)
	indicator_map = {str(_value(row, "name")): row for row in indicators}
	version_map = {str(_value(row, "name")): row for row in versions}
	results_by_version = _rows_by_value(results, "indicator_version")
	version_sets: dict[tuple[str, ...], set[str]] = {}
	for result in results:
		indicator_name = str(_value(result, "indicator") or "")
		version_name = str(_value(result, "indicator_version") or "")
		version = version_map.get(version_name)
		if not version or str(_value(version, "indicator") or "") != indicator_name:
			frappe.throw(
				"National-goal results must reference a Published indicator version effective "
				"during the selected report period"
			)
		status = str(_value(result, "status") or "")
		if status not in _INDICATOR_RESULT_STATUSES:
			frappe.throw("National-goal report encountered an invalid Indicator Result status")
		if not _result_within_version(result, version):
			frappe.throw("Indicator Result period is outside its approved definition period")
		key = (
			indicator_name,
			str(_value(result, "period_start") or ""),
			str(_value(result, "period_end") or ""),
			str(_value(result, "hospital") or ""),
			str(_value(result, "campus") or ""),
			str(_value(result, "department") or ""),
			str(_value(result, "ward") or ""),
		)
		version_sets.setdefault(key, set()).add(version_name)
	if any(len(names) > 1 for names in version_sets.values()):
		frappe.throw("National-goal progress cannot mix indicator versions for the same period and scope")

	data: list[dict[str, Any]] = []
	for version in versions:
		indicator_name = str(_value(version, "indicator") or "")
		indicator = indicator_map[indicator_name]
		clause = clauses[str(_value(indicator, "standard_clause") or "")]
		standard_version = standard_versions[str(_value(clause, "standard_version") or "")]
		version_results = results_by_version.get(str(_value(version, "name") or ""), [])
		if not version_results:
			data.append(
				_national_goal_row(
					standard,
					standard_version,
					clause,
					indicator,
					version,
					None,
					start,
					end,
				)
			)
			continue
		for result in version_results:
			data.append(
				_national_goal_row(
					standard,
					standard_version,
					clause,
					indicator,
					version,
					result,
					start,
					end,
				)
			)
	if len(data) > REPORT_MAX_OUTPUT_ROWS:
		_report_limit_error("national-ten-goal output rows", REPORT_MAX_OUTPUT_ROWS)
	status_counts = _count_values(data, "result_status")
	chart = _category_chart(
		sorted(status_counts),
		[status_counts[status] for status in sorted(status_counts)],
		"Exact Indicator Result Rows",
	)
	summary = [
		_summary("Approved Definition Rows", len(versions)),
		_summary("Indicator Result Rows", sum(int(row["source_result_count"]) for row in data)),
		_summary("Met", status_counts.get("Met", 0)),
		_summary(
			"Below/Warning/Critical",
			sum(status_counts.get(status, 0) for status in ("Below Target", "Warning", "Critical")),
		),
		_summary("No Data", status_counts.get("No Data", 0)),
		_summary("Failed", status_counts.get("Failed", 0)),
	]
	message = (
		"The selected standard is required and must be a Published National Policy. This report "
		"does not invent the ten objectives, targets, or mappings: it shows only Active indicators "
		"linked to Published clauses and Approved standard versions, separated by immutable Published "
		"indicator-version checksum. Every value is one exact permission-filtered current Indicator "
		"Result revision with its input receipt and result checksum; "
		"versions are never mixed or averaged. No Data and unconfigured targets are explicit."
	)
	return _national_ten_goal_columns(), data, message, chart, summary


def execute_improvement_governance_report(
	filters: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, dict[str, Any], list[dict[str, Any]]]:
	values, start, end = _report_context("improvement_governance", filters)
	scope = _scope_filters(values)
	recurrences = _permissioned_rows(
		"IONE Finding Recurrence Evaluation",
		filters=_dated_filters(
			"IONE Finding Recurrence Evaluation",
			"evaluated_at",
			start,
			end,
			scope,
		),
		fields=(
			"policy_checksum",
			"outcome",
			"occurrence_count",
			"evaluation_hash",
			"hospital",
			"campus",
			"department",
			"ward",
		),
		order_by="evaluated_at asc, name asc",
		limit=REPORT_MAX_SOURCE_ROWS,
	)
	projects = _permissioned_rows(
		"IONE PDCA Project",
		filters=_dated_filters("IONE PDCA Project", "start_date", start, end, scope),
		fields=(
			"project_type",
			"recurrence_evaluation",
			"recurrence_policy_checksum",
			"recurrence_snapshot_hash",
			"status",
			"analysis_method",
			"root_cause_analysis_complete",
			"standardization_decision",
			"standardization_evidence_hash",
			"standardized_at",
			"hospital",
			"campus",
			"department",
			"ward",
		),
		order_by="start_date asc, name asc",
		limit=REPORT_MAX_SOURCE_ROWS,
	)
	meetings = _permissioned_rows(
		"IONE Quality Meeting",
		filters=_dated_filters("IONE Quality Meeting", "scheduled_start", start, end, scope),
		fields=(
			"name",
			"status",
			"agenda_checksum",
			"actual_start",
			"actual_end",
			"closed_at",
			"hospital",
			"campus",
			"department",
			"ward",
		),
		order_by="scheduled_start asc, name asc",
		limit=REPORT_MAX_SOURCE_ROWS,
	)
	meeting_names = [str(_value(row, "name")) for row in meetings]
	minutes = (
		_permissioned_rows(
			"IONE Meeting Minute",
			filters=[["IONE Meeting Minute", "meeting", "in", meeting_names]],
			fields=(
				"meeting",
				"status",
				"checksum",
				"reviewed_at",
				"hospital",
				"campus",
				"department",
				"ward",
			),
			order_by="meeting asc, name asc",
			limit=REPORT_MAX_SOURCE_ROWS,
		)
		if meeting_names
		else []
	)
	decisions = (
		_permissioned_rows(
			"IONE Meeting Decision",
			filters=[["IONE Meeting Decision", "meeting", "in", meeting_names]],
			fields=(
				"meeting",
				"meeting_minute",
				"checksum",
				"hospital",
				"campus",
				"department",
				"ward",
			),
			order_by="meeting asc, name asc",
			limit=REPORT_MAX_SOURCE_ROWS,
		)
		if meeting_names
		else []
	)
	actions = _permissioned_rows(
		"IONE Quality Action Item",
		filters=_dated_filters("IONE Quality Action Item", "created_at", start, end, scope),
		fields=(
			"name",
			"status",
			"due_date",
			"closed_at",
			"latest_verification_round",
			"latest_verification_receipt_checksum",
			"hospital",
			"campus",
			"department",
			"ward",
		),
		order_by="created_at asc, name asc",
		limit=REPORT_MAX_SOURCE_ROWS,
	)
	action_names = [str(_value(row, "name")) for row in actions]
	action_rounds = (
		_permissioned_rows(
			"IONE Quality Action Verification Round",
			filters=[
				[
					"IONE Quality Action Verification Round",
					"action_item",
					"in",
					action_names,
				]
			],
			fields=(
				"name",
				"action_item",
				"round_no",
				"decision",
				"submitted_by",
				"verified_by",
				"verified_at",
				"previous_receipt_checksum",
				"receipt_checksum",
				"hospital",
				"campus",
				"department",
				"ward",
			),
			order_by="action_item asc, round_no asc, name asc",
			limit=REPORT_MAX_SOURCE_ROWS,
		)
		if action_names
		else []
	)
	experiences = _permissioned_rows(
		"IONE Quality Experience Share",
		filters=_dated_filters(
			"IONE Quality Experience Share",
			"creation",
			start,
			end,
			scope,
		),
		fields=(
			"status",
			"checksum",
			"published_at",
			"effective_from",
			"effective_to",
			"hospital",
			"campus",
			"department",
			"ward",
		),
		order_by="creation asc, name asc",
		limit=REPORT_MAX_SOURCE_ROWS,
	)
	if not any(
		(
			recurrences,
			projects,
			meetings,
			minutes,
			decisions,
			actions,
			action_rounds,
			experiences,
		)
	):
		data = [_improvement_no_data_row(start, end, values)]
		return (
			_improvement_governance_columns(),
			data,
			"No Data: no permission-filtered improvement-governance source is in the selected period.",
			_category_chart(["No Data"], [0], "Governance Records"),
			[_summary("Governance Source Rows", 0)],
		)

	groups: dict[tuple[str, str, str, str], dict[str, int]] = {}
	for row in recurrences:
		outcome = str(_value(row, "outcome") or "")
		if outcome not in _RECURRENCE_OUTCOMES:
			frappe.throw("Improvement report encountered an invalid recurrence outcome")
		metric = groups.setdefault(_row_scope_key(row), _new_improvement_metric())
		metric["recurrence_evaluation_count"] += 1
		metric[f"recurrence_{_safe_metric_token(outcome)}_count"] += 1
		metric["recurrence_occurrence_count"] += _nonnegative_int(
			_value(row, "occurrence_count"),
			"occurrence_count",
		)
		if _has_checksum(_value(row, "policy_checksum")) and _has_checksum(_value(row, "evaluation_hash")):
			metric["recurrence_lineage_complete_count"] += 1
	for row in projects:
		status = str(_value(row, "status") or "")
		if status not in _PDCA_STATUSES:
			frappe.throw("Improvement report encountered an invalid PDCA status")
		metric = groups.setdefault(_row_scope_key(row), _new_improvement_metric())
		metric["pdca_project_count"] += 1
		metric[f"pdca_{_safe_metric_token(status)}_count"] += 1
		if str(_value(row, "project_type") or "") == "Special Recurrence":
			metric["special_recurrence_pdca_count"] += 1
			if (
				_value(row, "recurrence_evaluation")
				and _has_checksum(_value(row, "recurrence_policy_checksum"))
				and _has_checksum(_value(row, "recurrence_snapshot_hash"))
			):
				metric["special_recurrence_lineage_complete_count"] += 1
		if (
			status in {"Active", "Measuring", "Closed"}
			and int(_value(row, "root_cause_analysis_complete") or 0)
			and str(_value(row, "analysis_method") or "")
		):
			metric["structured_root_cause_complete_count"] += 1
		if status in {"Measuring", "Closed"}:
			metric["effect_verification_gate_count"] += 1
		if (
			status == "Closed"
			and str(_value(row, "standardization_decision") or "") in {"Adopted", "Not Adopted"}
			and _has_checksum(_value(row, "standardization_evidence_hash"))
			and _value(row, "standardized_at")
		):
			metric["standardization_receipt_count"] += 1
	for row in meetings:
		status = str(_value(row, "status") or "")
		if status not in _MEETING_STATUSES:
			frappe.throw("Improvement report encountered an invalid meeting status")
		metric = groups.setdefault(_row_scope_key(row), _new_improvement_metric())
		metric["meeting_count"] += 1
		metric[f"meeting_{_safe_metric_token(status)}_count"] += 1
		if status in {"Approved", "Held", "Minutes Pending", "Closed"} and _has_checksum(
			_value(row, "agenda_checksum")
		):
			metric["frozen_agenda_count"] += 1
	for row in minutes:
		status = str(_value(row, "status") or "")
		if status not in _MINUTE_STATUSES:
			frappe.throw("Improvement report encountered an invalid meeting-minute status")
		metric = groups.setdefault(_row_scope_key(row), _new_improvement_metric())
		metric["meeting_minute_count"] += 1
		if status == "Approved":
			metric["approved_minute_count"] += 1
			if _has_checksum(_value(row, "checksum")) and _value(row, "reviewed_at"):
				metric["approved_minute_receipt_count"] += 1
	for row in decisions:
		metric = groups.setdefault(_row_scope_key(row), _new_improvement_metric())
		metric["meeting_decision_count"] += 1
		if _has_checksum(_value(row, "checksum")):
			metric["meeting_decision_checksum_count"] += 1
	rounds_by_name: dict[str, Any] = {}
	for row in action_rounds:
		decision = str(_value(row, "decision") or "")
		if decision not in _ACTION_VERIFICATION_DECISIONS:
			frappe.throw("Improvement report encountered an invalid action verification decision")
		rounds_by_name[str(_value(row, "name") or "")] = row
		metric = groups.setdefault(_row_scope_key(row), _new_improvement_metric())
		metric["action_verification_round_count"] += 1
		if decision == "Rework":
			metric["action_rework_round_count"] += 1
		if (
			_has_checksum(_value(row, "receipt_checksum"))
			and int(_value(row, "round_no") or 0) > 0
			and (
				int(_value(row, "round_no") or 0) == 1
				or _has_checksum(_value(row, "previous_receipt_checksum"))
			)
		):
			metric["action_chained_receipt_count"] += 1
	as_of = getdate(nowdate())
	for row in actions:
		status = str(_value(row, "status") or "")
		if status not in _ACTION_STATUSES:
			frappe.throw("Improvement report encountered an invalid action-item status")
		metric = groups.setdefault(_row_scope_key(row), _new_improvement_metric())
		metric["action_item_count"] += 1
		metric[f"action_{_safe_metric_token(status)}_count"] += 1
		if status not in {"Closed", "Cancelled"} and _is_overdue(_value(row, "due_date"), as_of):
			metric["overdue_action_count"] += 1
		latest = rounds_by_name.get(str(_value(row, "latest_verification_round") or ""))
		if (
			status == "Closed"
			and latest
			and str(_value(latest, "decision") or "") == "Close"
			and _value(row, "closed_at")
			and str(_value(latest, "submitted_by") or "") != str(_value(latest, "verified_by") or "")
			and _has_checksum(_value(latest, "receipt_checksum"))
			and str(_value(row, "latest_verification_receipt_checksum") or "")
			== str(_value(latest, "receipt_checksum") or "")
		):
			metric["independently_verified_action_count"] += 1
	for row in experiences:
		status = str(_value(row, "status") or "")
		if status not in _EXPERIENCE_STATUSES:
			frappe.throw("Improvement report encountered an invalid experience status")
		metric = groups.setdefault(_row_scope_key(row), _new_improvement_metric())
		metric["experience_version_count"] += 1
		metric[f"experience_{_safe_metric_token(status)}_count"] += 1
		if (
			status in {"Published", "Retired"}
			and _has_checksum(_value(row, "checksum"))
			and _value(row, "published_at")
		):
			metric["experience_publication_receipt_count"] += 1
	if len(groups) > REPORT_MAX_OUTPUT_ROWS:
		_report_limit_error("improvement-governance output rows", REPORT_MAX_OUTPUT_ROWS)
	data = [
		{
			"data_state": "Data",
			"period_start": start,
			"period_end": end,
			"hospital": hospital or None,
			"campus": campus or None,
			"department": department or None,
			"ward": ward or None,
			**metric,
			"source_row_count": sum(
				metric[fieldname]
				for fieldname in (
					"recurrence_evaluation_count",
					"pdca_project_count",
					"meeting_count",
					"meeting_minute_count",
					"meeting_decision_count",
					"action_item_count",
					"action_verification_round_count",
					"experience_version_count",
				)
			),
		}
		for (hospital, campus, department, ward), metric in sorted(groups.items())
	]
	totals = _sum_fields(data, (*_new_improvement_metric(), "source_row_count"))
	chart = _category_chart(
		["Recurrence", "PDCA", "Meetings", "Actions", "Experience Versions"],
		[
			totals["recurrence_evaluation_count"],
			totals["pdca_project_count"],
			totals["meeting_count"],
			totals["action_item_count"],
			totals["experience_version_count"],
		],
		"Governance Source Rows",
	)
	summary = [
		_summary("Governance Source Rows", totals["source_row_count"]),
		_summary("Triggered Recurrences", totals["recurrence_triggered_count"]),
		_summary("Special Recurrence PDCAs", totals["special_recurrence_pdca_count"]),
		_summary("Projects Past Effect Verification Gate", totals["effect_verification_gate_count"]),
		_summary("Closed Meetings", totals["meeting_closed_count"]),
		_summary("Approved Minutes", totals["approved_minute_count"]),
		_summary("Action Verification Rounds", totals["action_verification_round_count"]),
		_summary("Action Rework Rounds", totals["action_rework_round_count"]),
		_summary("Verified Closed Actions", totals["independently_verified_action_count"]),
		_summary("Published Experience Versions", totals["experience_published_count"]),
	]
	message = (
		"Rows aggregate permission-filtered governance receipts by exact organization scope. A "
		"project is counted past the effect-verification gate only when its governed state is "
		"Measuring or Closed; the workflow requires verified immutable measurements before those "
		"states. Closed actions require an independent verifier and a matching append-only receipt; "
		"rework rounds remain counted rather than overwritten. The report reads statuses, links, "
		"dates, and checksums only, never findings, clinical identifiers, evidence text, meeting "
		"discussion, action narratives, or experience content."
	)
	return _improvement_governance_columns(), data, message, chart, summary


def _report_context(
	report_key: str,
	filters: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], date, date]:
	_require_report_access(report_key)
	values = _normalized_filters(filters)
	today = getdate(nowdate())
	start = _filter_date(values.get("from_date"), add_days(today, -29), "from_date")
	end = _filter_date(values.get("to_date"), today, "to_date")
	if start > end:
		frappe.throw("from_date cannot be after to_date", frappe.ValidationError)
	if (end - start).days + 1 > REPORT_MAX_DAYS:
		frappe.throw(
			f"Report date range cannot exceed {REPORT_MAX_DAYS} inclusive days",
			frappe.ValidationError,
		)
	return values, start, end


def _require_report_access(report_key: str) -> None:
	user = str(frappe.session.user or "")
	if user in {"", "Guest", "Administrator"}:
		frappe.throw(
			"Reports require a named accountable IONE business user.",
			frappe.PermissionError,
		)
	roles = frozenset(frappe.get_roles(user))
	allowed = REPORT_ROLES.get(report_key, frozenset())
	if not allowed or "IONE Agent Service" in roles or not roles.intersection(allowed):
		frappe.throw("Current user does not have an allowed report role.", frappe.PermissionError)
	for doctype in REPORT_REQUIRED_DOCTYPES.get(report_key, ()):
		if not frappe.has_permission(doctype, "read", user=user):
			frappe.throw(
				f"Read permission is required for report source {doctype}.",
				frappe.PermissionError,
			)


def _permissioned_rows(
	doctype: str,
	*,
	filters: Any,
	fields: tuple[str, ...],
	order_by: str,
	limit: int,
	group_by: str | None = None,
	_current_pointer_resolved: bool = False,
) -> list[Any]:
	"""Read bounded rows only through Frappe's permission-query and has_permission path."""
	if limit < 1 or limit > REPORT_MAX_SOURCE_ROWS:
		raise ValueError("Internal report row limit is outside its reviewed bound")
	if doctype == "IONE Indicator Result" and not _current_pointer_resolved:
		if group_by:
			raise ValueError("Indicator Result reports must resolve exact current revisions before grouping.")
		pointer_filters = _retarget_indicator_filters(
			filters,
			source_doctype=doctype,
			target_doctype="IONE Indicator Result Pointer",
		)
		pointers = _permissioned_rows(
			"IONE Indicator Result Pointer",
			filters=pointer_filters,
			fields=("current_result",),
			order_by=_retarget_indicator_order_by(order_by),
			limit=limit,
		)
		current_names = [
			str(_value(pointer, "current_result") or "")
			for pointer in pointers
			if _value(pointer, "current_result")
		]
		if not current_names:
			return []
		rows = _permissioned_rows(
			doctype,
			filters=[[doctype, "name", "in", current_names]],
			fields=fields,
			order_by=order_by,
			limit=limit,
			_current_pointer_resolved=True,
		)
		for row in rows:
			result_name = str(_value(row, "name") or "")
			if not result_name:
				raise ValueError("Current Indicator Result report rows require an exact result identity.")
			with current_indicator_result_lock(result_name):
				pass
		return rows
	if not frappe.has_permission(doctype, "read"):
		frappe.throw(
			f"Read permission is required for report source {doctype}.",
			frappe.PermissionError,
		)
	kwargs: dict[str, Any] = {
		"filters": filters,
		"fields": list(fields),
		"order_by": order_by,
		"limit_start": 0,
		"limit_page_length": limit + 1,
	}
	if group_by:
		kwargs["group_by"] = group_by
	rows = list(frappe.get_list(doctype, **kwargs))
	if len(rows) > limit:
		_report_limit_error(f"{doctype} source rows", limit)
	return rows


def _retarget_indicator_filters(
	filters: Any,
	*,
	source_doctype: str,
	target_doctype: str,
) -> Any:
	"""Retarget the Result report filter tree to its isomorphic current-pointer fields."""
	if isinstance(filters, Mapping):
		unsupported = set(filters) - _RESULT_POINTER_FILTER_FIELDS
		if unsupported:
			raise ValueError(
				"Indicator Result report filter cannot be resolved through its current pointer: "
				+ ", ".join(sorted(str(fieldname) for fieldname in unsupported))
			)
		return dict(filters)
	if isinstance(filters, list | tuple):
		if len(filters) == 4 and isinstance(filters[0], str) and str(filters[0]) == source_doctype:
			if str(filters[1]) not in _RESULT_POINTER_FILTER_FIELDS:
				raise ValueError(f"Indicator Result filter {filters[1]} has no current-pointer projection.")
			return [target_doctype, *filters[1:]]
		if len(filters) == 3 and isinstance(filters[0], str):
			if str(filters[0]) not in _RESULT_POINTER_FILTER_FIELDS:
				raise ValueError(f"Indicator Result filter {filters[0]} has no current-pointer projection.")
			return list(filters)
		return [
			_retarget_indicator_filters(
				item,
				source_doctype=source_doctype,
				target_doctype=target_doctype,
			)
			for item in filters
		]
	raise ValueError("Indicator Result report filters must be a bounded Frappe filter tree.")


def _retarget_indicator_order_by(order_by: str) -> str:
	parts: list[str] = []
	for raw in str(order_by or "").split(","):
		tokens = raw.strip().split()
		if not tokens:
			continue
		fieldname = tokens[0]
		if fieldname == "name":
			fieldname = "current_result"
		elif fieldname not in _RESULT_POINTER_FILTER_FIELDS:
			raise ValueError(
				f"Indicator Result ordering field {fieldname} has no current-pointer projection."
			)
		direction = tokens[1].lower() if len(tokens) > 1 else "asc"
		if len(tokens) > 2 or direction not in {"asc", "desc"}:
			raise ValueError("Indicator Result report ordering is outside the reviewed contract.")
		parts.append(f"{fieldname} {direction}")
	return ", ".join(parts)


def _definition_map(
	doctype: str,
	names: list[str],
	fields: tuple[str, ...],
) -> dict[str, Any]:
	if not names:
		return {}
	rows = _permissioned_rows(
		doctype,
		filters=[[doctype, "name", "in", names]],
		fields=fields,
		order_by="name asc",
		limit=min(REPORT_MAX_SOURCE_ROWS, max(len(names), 1)),
	)
	return {str(_value(row, "name")): row for row in rows}


def _require_aggregate_source_bound(doctype: str, filters: Any) -> None:
	rows = _permissioned_rows(
		doctype,
		filters=filters,
		fields=("count(name) as total",),
		order_by="",
		limit=1,
	)
	total = _nonnegative_int(_value(rows[0], "total"), "total") if rows else 0
	if total > REPORT_MAX_AGGREGATED_RECORDS:
		_report_limit_error(f"{doctype} permission-filtered records", REPORT_MAX_AGGREGATED_RECORDS)


def _normalized_filters(filters: Mapping[str, Any] | None) -> dict[str, Any]:
	if filters is None:
		return {}
	if not isinstance(filters, Mapping):
		frappe.throw("Report filters must be an object", frappe.ValidationError)
	return dict(filters)


def _filter_date(value: Any, default: Any, fieldname: str) -> date:
	if value in (None, ""):
		value = default
	try:
		parsed = getdate(value)
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError(f"{fieldname} must be a valid date") from exc
	if parsed is None:
		frappe.throw(f"{fieldname} must be a valid date", frappe.ValidationError)
	return parsed


def _stored_date(value: Any, fieldname: str) -> date:
	if value in (None, ""):
		frappe.throw(f"Stored {fieldname} is required", frappe.ValidationError)
	try:
		parsed = getdate(value)
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError(f"Stored {fieldname} is invalid") from exc
	if parsed is None:
		frappe.throw(f"Stored {fieldname} is invalid", frappe.ValidationError)
	return parsed


def _scope_filters(values: Mapping[str, Any]) -> dict[str, str]:
	return {fieldname: value for fieldname in _SCOPE_FIELDS if (value := _optional_filter(values, fieldname))}


def _optional_filter(values: Mapping[str, Any], fieldname: str) -> str | None:
	value = values.get(fieldname)
	if value in (None, ""):
		return None
	if not isinstance(value, str):
		frappe.throw(f"{fieldname} must be a bounded text filter", frappe.ValidationError)
	candidate = value.strip()
	if (
		not candidate
		or candidate != value
		or len(candidate) > REPORT_MAX_FILTER_TEXT
		or any(ord(character) < 32 for character in candidate)
	):
		frappe.throw(f"{fieldname} must be a bounded text filter", frappe.ValidationError)
	return candidate


def _required_filter(values: Mapping[str, Any], fieldname: str) -> str:
	value = _optional_filter(values, fieldname)
	if value is None:
		frappe.throw(f"{fieldname} is required", frappe.ValidationError)
	return value


def _dated_filters(
	doctype: str,
	date_field: str,
	start: date,
	end: date,
	extra: Mapping[str, Any],
) -> list[list[Any]]:
	filters: list[list[Any]] = [
		[doctype, date_field, ">=", start],
		[doctype, date_field, "<", add_days(end, 1)],
	]
	filters.extend([doctype, fieldname, "=", value] for fieldname, value in sorted(extra.items()))
	return filters


def _period_overlap_filters(
	doctype: str,
	start_field: str,
	end_field: str,
	start: date,
	end: date,
	extra: Mapping[str, Any],
) -> list[list[Any]]:
	filters: list[list[Any]] = [
		[doctype, end_field, ">=", start],
		[doctype, start_field, "<", add_days(end, 1)],
	]
	filters.extend([doctype, fieldname, "=", value] for fieldname, value in sorted(extra.items()))
	return filters


def _linked_scope_filters(
	doctype: str,
	link_field: str,
	names: list[str],
	scope: Mapping[str, str],
) -> list[list[Any]]:
	filters: list[list[Any]] = [[doctype, link_field, "in", names]]
	filters.extend([doctype, fieldname, "=", value] for fieldname, value in sorted(scope.items()))
	return filters


def _rows_by_value(rows: list[Any], fieldname: str) -> dict[str, list[Any]]:
	result: dict[str, list[Any]] = {}
	for row in rows:
		result.setdefault(str(_value(row, fieldname) or ""), []).append(row)
	return result


def _has_checksum(value: Any) -> bool:
	candidate = str(value or "")
	return len(candidate) == 64 and all(character in "0123456789abcdef" for character in candidate)


def _safe_metric_token(value: str) -> str:
	return value.lower().replace(" ", "_").replace("/", "_").replace("-", "_")


def _medical_assignment_status_field(status: str) -> str:
	return {
		"Coder Review": "coder_review_assignment_count",
		"Expert Review": "expert_review_assignment_count",
		"Closed": "closed_assignment_count",
	}[status]


def _outcome_field(outcome: str) -> str:
	return {
		"Pass": "pass",
		"Defect": "defect",
		"Needs Correction": "needs_correction",
		"Unable to Determine": "unable_to_determine",
	}[outcome]


def _new_medical_review_metric() -> dict[str, int]:
	return {
		"visible_assignment_count": 0,
		"coder_review_assignment_count": 0,
		"expert_review_assignment_count": 0,
		"closed_assignment_count": 0,
		"coder_required_count": 0,
		"expert_required_count": 0,
		"deterministic_passed_count": 0,
		"deterministic_held_count": 0,
		"coder_decision_count": 0,
		"coder_pass_count": 0,
		"coder_defect_count": 0,
		"coder_needs_correction_count": 0,
		"coder_unable_to_determine_count": 0,
		"expert_decision_count": 0,
		"expert_pass_count": 0,
		"expert_defect_count": 0,
		"expert_needs_correction_count": 0,
		"expert_unable_to_determine_count": 0,
		"review_decision_checksum_count": 0,
		"latest_archive_decision_count": 0,
		"archive_allow_count": 0,
		"archive_hold_count": 0,
		"archive_checksum_count": 0,
		"archive_ack_count": 0,
		"archive_ack_applied_count": 0,
		"archive_ack_rejected_count": 0,
		"archive_ack_failed_count": 0,
		"archive_ack_receipt_count": 0,
	}


def _new_surgery_metric() -> dict[str, int]:
	metric = {
		"source_record_count": 0,
		"governance_pending_prerequisites_count": 0,
		"governance_passed_count": 0,
		"governance_noncompliant_count": 0,
		"governance_blocked_count": 0,
		"governance_cancelled_count": 0,
		"governance_checksum_count": 0,
		"compliant_count": 0,
		"authorization_linked_count": 0,
		"approved_authorization_count": 0,
		"mdt_required_count": 0,
		"mdt_linked_count": 0,
		"completed_mdt_count": 0,
		"mdt_proceed_count": 0,
		"mdt_proceed_with_conditions_count": 0,
		"mdt_do_not_proceed_count": 0,
		"checklist_linked_count": 0,
		"completed_checklist_count": 0,
		"approved_emergency_exception_count": 0,
		"cancelled_count": 0,
		"unplanned_surgery_count": 0,
		"unplanned_return_count": 0,
		"complication_count": 0,
		"mortality_count": 0,
	}
	for prefix in ("preanesthesia", "intraoperative", "recovery", "followup"):
		for status in ("not_applicable", "pending", "completed", "failed"):
			metric[f"{prefix}_{status}_count"] = 0
	return metric


def _new_improvement_metric() -> dict[str, int]:
	metric = {
		"recurrence_evaluation_count": 0,
		"recurrence_below_threshold_count": 0,
		"recurrence_triggered_count": 0,
		"recurrence_occurrence_count": 0,
		"recurrence_lineage_complete_count": 0,
		"pdca_project_count": 0,
		"special_recurrence_pdca_count": 0,
		"special_recurrence_lineage_complete_count": 0,
		"structured_root_cause_complete_count": 0,
		"effect_verification_gate_count": 0,
		"standardization_receipt_count": 0,
		"meeting_count": 0,
		"frozen_agenda_count": 0,
		"meeting_minute_count": 0,
		"approved_minute_count": 0,
		"approved_minute_receipt_count": 0,
		"meeting_decision_count": 0,
		"meeting_decision_checksum_count": 0,
		"action_item_count": 0,
		"overdue_action_count": 0,
		"action_verification_round_count": 0,
		"action_rework_round_count": 0,
		"action_chained_receipt_count": 0,
		"independently_verified_action_count": 0,
		"experience_version_count": 0,
		"experience_publication_receipt_count": 0,
	}
	for status in _PDCA_STATUSES:
		metric[f"pdca_{_safe_metric_token(status)}_count"] = 0
	for status in _MEETING_STATUSES:
		metric[f"meeting_{_safe_metric_token(status)}_count"] = 0
	for status in _ACTION_STATUSES:
		metric[f"action_{_safe_metric_token(status)}_count"] = 0
	for status in _EXPERIENCE_STATUSES:
		metric[f"experience_{_safe_metric_token(status)}_count"] = 0
	return metric


def _row_scope_key(row: Any) -> tuple[str, str, str, str]:
	return (
		str(_value(row, "hospital") or ""),
		str(_value(row, "campus") or ""),
		str(_value(row, "department") or ""),
		str(_value(row, "ward") or ""),
	)


def _periods_overlap(
	effective_from: Any,
	effective_to: Any,
	start: date,
	end: date,
) -> bool:
	version_start = _stored_date(effective_from, "effective_from")
	version_end = _stored_date(effective_to, "effective_to") if effective_to else None
	if version_end is not None and version_end < version_start:
		frappe.throw("Approved definition effective period is invalid")
	return version_start <= end and (version_end is None or version_end >= start)


def _result_within_version(result: Any, version: Any) -> bool:
	result_start = _stored_date(_value(result, "period_start"), "period_start")
	result_end = _stored_date(_value(result, "period_end"), "period_end")
	version_start = _stored_date(_value(version, "effective_from"), "effective_from")
	version_end_value = _value(version, "effective_to")
	version_end = _stored_date(version_end_value, "effective_to") if version_end_value else None
	if version_end is not None and version_end < version_start:
		frappe.throw("Published indicator version effective period is invalid")
	return (
		result_start >= version_start
		and result_end >= result_start
		and (version_end is None or result_end <= version_end)
	)


def _national_goal_row(
	standard: Any,
	standard_version: Any,
	clause: Any,
	indicator: Any,
	indicator_version: Any,
	result: Any | None,
	start: date,
	end: date,
) -> dict[str, Any]:
	has_result = result is not None
	return {
		"data_state": "Data" if has_result else "No Data",
		"standard": _value(standard, "name"),
		"standard_code": _value(standard, "standard_code"),
		"standard_name": _value(standard, "standard_name"),
		"standard_issue_date": _value(standard, "issue_date"),
		"standard_version": _value(standard_version, "name"),
		"standard_version_label": _value(standard_version, "version"),
		"standard_version_checksum": _value(standard_version, "checksum"),
		"standard_clause": _value(clause, "name"),
		"clause_code": _value(clause, "clause_code"),
		"clause_heading": _value(clause, "heading"),
		"objective_category": _value(indicator, "category"),
		"indicator": _value(indicator, "name"),
		"indicator_code": _value(indicator, "indicator_code"),
		"indicator_name": _value(indicator, "indicator_name"),
		"indicator_version": _value(indicator_version, "name"),
		"indicator_version_label": _value(indicator_version, "version"),
		"indicator_version_checksum": _value(indicator_version, "checksum"),
		"definition_effective_from": _value(indicator_version, "effective_from"),
		"definition_effective_to": _value(indicator_version, "effective_to"),
		"calculation_frequency": _value(indicator_version, "calculation_frequency"),
		"unit": _value(indicator_version, "unit"),
		"direction": _value(indicator_version, "direction"),
		"definition_target_value": _value(indicator_version, "target_value"),
		"definition_target_min": _value(indicator_version, "target_min"),
		"definition_target_max": _value(indicator_version, "target_max"),
		"indicator_result": _value(result, "name") if has_result else None,
		"calculation": _value(result, "calculation") if has_result else None,
		"period": _value(result, "period") if has_result else None,
		"period_start": _value(result, "period_start") if has_result else start,
		"period_end": _value(result, "period_end") if has_result else end,
		"hospital": _value(result, "hospital") if has_result else None,
		"campus": _value(result, "campus") if has_result else None,
		"department": _value(result, "department") if has_result else None,
		"ward": _value(result, "ward") if has_result else None,
		"numerator": _value(result, "numerator") if has_result else None,
		"denominator": _value(result, "denominator") if has_result else None,
		"indicator_value": _value(result, "indicator_value") if has_result else None,
		"result_target_value": _value(result, "target_value") if has_result else None,
		"result_target_min": _value(result, "target_min") if has_result else None,
		"result_target_max": _value(result, "target_max") if has_result else None,
		"target_state": _target_state(indicator_version, result),
		"result_status": str(_value(result, "status") or "") if has_result else "No Data",
		"computed_at": _value(result, "computed_at") if has_result else None,
		"calculator_key": _value(result, "calculator_key") if has_result else None,
		"calculator_version": _value(result, "calculator_version") if has_result else None,
		"result_revision": _value(result, "result_revision") if has_result else None,
		"input_receipt_hash": _value(result, "input_receipt_hash") if has_result else None,
		"result_checksum": _value(result, "result_checksum") if has_result else None,
		"source_result_count": 1 if has_result else 0,
	}


def _target_state(indicator_version: Any, result: Any | None) -> str:
	version_target = _target_tuple(indicator_version)
	version_has_target = any(value is not None for value in version_target)
	if result is None:
		return "Approved Definition Target" if version_has_target else "Target Not Configured"
	result_target = _target_tuple(result)
	result_has_target = any(value is not None for value in result_target)
	if not result_has_target:
		return "Result Target Missing" if version_has_target else "Target Not Configured"
	if not version_has_target:
		return "Result Target Without Definition"
	if result_target == version_target:
		return "Stored Result Target Matches Definition"
	return "Stored Result Target Differs from Definition"


def _target_tuple(row: Any) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
	return (
		_optional_decimal(_value(row, "target_value"), "target_value"),
		_optional_decimal(_value(row, "target_min"), "target_min"),
		_optional_decimal(_value(row, "target_max"), "target_max"),
	)


def _optional_decimal(value: Any, fieldname: str) -> Decimal | None:
	if value in (None, ""):
		return None
	try:
		number = Decimal(str(value))
	except (InvalidOperation, ValueError) as exc:
		raise frappe.ValidationError(f"Stored {fieldname} is invalid") from exc
	if not number.is_finite():
		frappe.throw(f"Stored {fieldname} is invalid", frappe.ValidationError)
	return number.normalize()


def _medical_review_no_data_row(
	start: date,
	end: date,
	values: Mapping[str, Any],
) -> dict[str, Any]:
	return {
		"data_state": "No Data",
		"period_start": start,
		"period_end": end,
		"hospital": values.get("hospital"),
		"campus": values.get("campus"),
		"department": values.get("department"),
		"ward": values.get("ward"),
		"policy": values.get("policy"),
		"population_count": 0,
		"sample_count": 0,
		"expert_sample_count": 0,
		**_new_medical_review_metric(),
		"source_row_count": 0,
	}


def _surgery_no_data_row(
	start: date,
	end: date,
	values: Mapping[str, Any],
) -> dict[str, Any]:
	return {
		"data_state": "No Data",
		"period_start": start,
		"period_end": end,
		"hospital": values.get("hospital"),
		"campus": values.get("campus"),
		"department": values.get("department"),
		"ward": values.get("ward"),
		"surgery_level": values.get("surgery_level"),
		**_new_surgery_metric(),
	}


def _national_goal_no_data_row(start: date, end: date, standard: Any) -> dict[str, Any]:
	return {
		"data_state": "No Data",
		"standard": _value(standard, "name"),
		"standard_code": _value(standard, "standard_code"),
		"standard_name": _value(standard, "standard_name"),
		"standard_issue_date": _value(standard, "issue_date"),
		"period_start": start,
		"period_end": end,
		"result_status": "No Data",
		"target_state": "No Approved Indicator Definition",
		"source_result_count": 0,
	}


def _improvement_no_data_row(
	start: date,
	end: date,
	values: Mapping[str, Any],
) -> dict[str, Any]:
	return {
		"data_state": "No Data",
		"period_start": start,
		"period_end": end,
		"hospital": values.get("hospital"),
		"campus": values.get("campus"),
		"department": values.get("department"),
		"ward": values.get("ward"),
		**_new_improvement_metric(),
		"source_row_count": 0,
	}


def _rule_key(row: Any) -> tuple[str, str]:
	return str(_value(row, "rule") or ""), str(_value(row, "rule_version") or "")


def _new_rule_metric() -> dict[str, int]:
	return {
		"passed_count": 0,
		"failed_count": 0,
		"excluded_count": 0,
		"insufficient_data_count": 0,
		"error_count": 0,
		"finding_count": 0,
		"appeal_submitted": 0,
		"appeal_approved": 0,
		"appeal_rejected": 0,
		"appeal_withdrawn": 0,
	}


def _execution_metric_name(result: str) -> str:
	return {
		"Passed": "passed_count",
		"Failed": "failed_count",
		"Excluded": "excluded_count",
		"Insufficient Data": "insufficient_data_count",
		"Error": "error_count",
	}[result]


def _new_closure_metric() -> dict[str, int]:
	return {
		"finding_count": 0,
		"active_findings": 0,
		"closed_findings": 0,
		"appeal_overturned_findings": 0,
		"overdue_findings": 0,
		"rectification_count": 0,
		"active_rectifications": 0,
		"closed_rectifications": 0,
		"rejected_cancelled_rectifications": 0,
		"overdue_rectifications": 0,
		"verification_count": 0,
		"effective_verifications": 0,
		"partially_effective_verifications": 0,
		"ineffective_verifications": 0,
		"rejected_verifications": 0,
		"pending_verifications": 0,
	}


def _new_ai_metric() -> dict[str, int]:
	return {
		"task_count": 0,
		"success_count": 0,
		"failure_count": 0,
		"human_review_required_count": 0,
		"successful_tasks_with_accepted_candidate": 0,
		"accepted_candidate_count": 0,
		"duration_total_ms": 0,
		"duration_observation_count": 0,
		"input_tokens": 0,
		"output_tokens": 0,
		"total_tokens": 0,
	}


def _nonnegative_int(value: Any, fieldname: str) -> int:
	try:
		result = int(value or 0)
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError(f"{fieldname} must be a non-negative integer") from exc
	if result < 0:
		frappe.throw(f"{fieldname} must be a non-negative integer", frappe.ValidationError)
	return result


def _optional_nonnegative_int(value: Any, fieldname: str) -> int | None:
	if value in (None, ""):
		return None
	return _nonnegative_int(value, fieldname)


def _percentage(numerator: int, denominator: int) -> float | None:
	if denominator <= 0:
		return None
	return round((numerator / denominator) * 100, 2)


def _is_overdue(value: Any, as_of: date) -> bool:
	if value in (None, ""):
		return False
	try:
		return getdate(value) < as_of
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError("Stored due_date is invalid") from exc


def _value(row: Any, fieldname: str) -> Any:
	if isinstance(row, Mapping):
		return row.get(fieldname)
	return getattr(row, fieldname, None)


def _sum_fields(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> dict[str, int]:
	return {
		fieldname: sum(_nonnegative_int(row.get(fieldname), fieldname) for row in rows)
		for fieldname in fields
	}


def _count_values(rows: list[dict[str, Any]], fieldname: str) -> dict[str, int]:
	result: dict[str, int] = {}
	for row in rows:
		value = str(row.get(fieldname) or "Unspecified")
		result[value] = result.get(value, 0) + 1
	return result


def _report_limit_error(dataset: str, limit: int) -> None:
	frappe.throw(
		f"{dataset} exceeds the reviewed hard limit of {limit}; narrow the date or scope filters.",
		frappe.ValidationError,
	)


def _column(
	label: str,
	fieldname: str,
	fieldtype: str = "Data",
	*,
	options: str | None = None,
	width: int = 120,
) -> dict[str, Any]:
	column: dict[str, Any] = {
		"label": label,
		"fieldname": fieldname,
		"fieldtype": fieldtype,
		"width": width,
	}
	if options:
		column["options"] = options
	return column


def _summary(label: str, value: int) -> dict[str, Any]:
	return {"label": label, "value": value, "datatype": "Int"}


def _category_chart(labels: list[str], values: list[int], name: str) -> dict[str, Any]:
	return {
		"data": {"labels": labels, "datasets": [{"name": name, "values": values}]},
		"type": "bar",
	}


def _rule_quality_columns() -> list[dict[str, Any]]:
	return [
		_column("Rule", "rule", "Link", options="IONE QC Rule", width=150),
		_column("Rule Code", "rule_code", width=110),
		_column("Rule Name", "rule_name", width=190),
		_column("Rule Version", "rule_version", "Link", options="IONE QC Rule Version", width=150),
		_column("Version", "version", width=80),
		_column("Version Status", "version_status", width=110),
		_column("Version Checksum", "version_checksum", width=160),
		_column("Executions", "execution_count", "Int"),
		_column("Passed", "passed_count", "Int"),
		_column("Failed Hits", "failed_count", "Int"),
		_column("Failed Hit Rate (%)", "failed_hit_rate", "Percent", width=140),
		_column("Excluded (Exemptions)", "excluded_count", "Int", width=150),
		_column("Excluded Exemption Rate (%)", "excluded_exemption_rate", "Percent", width=180),
		_column("Insufficient Data", "insufficient_data_count", "Int", width=130),
		_column("Errors", "error_count", "Int"),
		_column("Findings", "finding_count", "Int"),
		_column("Appeals", "appeal_count", "Int"),
		_column("Reviewed Appeals", "appeal_reviewed_count", "Int", width=130),
		_column("Submitted Appeals", "appeal_submitted_count", "Int", width=130),
		_column("Approved Appeals", "appeal_approved_count", "Int", width=130),
		_column("Rejected Appeals", "appeal_rejected_count", "Int", width=130),
		_column("Withdrawn Appeals", "appeal_withdrawn_count", "Int", width=130),
		_column(
			"Human Appeal Overturn Rate (%)",
			"human_appeal_overturn_rate",
			"Percent",
			width=210,
		),
	]


def _indicator_profile_columns() -> list[dict[str, Any]]:
	return [
		_column(
			"Indicator Result",
			"indicator_result",
			"Link",
			options="IONE Indicator Result",
			width=150,
		),
		_column("Period", "period", width=100),
		_column("Period Start", "period_start", "Date"),
		_column("Period End", "period_end", "Date"),
		_column("Indicator", "indicator", "Link", options="IONE QC Indicator", width=150),
		_column("Indicator Code", "indicator_code", width=120),
		_column("Indicator Name", "indicator_name", width=190),
		_column(
			"Indicator Version",
			"indicator_version",
			"Link",
			options="IONE QC Indicator Version",
			width=160,
		),
		_column("Version", "version", width=80),
		_column("Version Status", "version_status", width=110),
		_column("Version Checksum", "version_checksum", width=160),
		_column("Effective From", "effective_from", "Date"),
		_column("Effective To", "effective_to", "Date"),
		_column("Direction", "direction", width=120),
		_column("Hospital", "hospital", "Link", options="IONE Hospital", width=130),
		_column("Campus", "campus", "Link", options="IONE Hospital Campus", width=130),
		_column("Department", "department", "Link", options="IONE Medical Department", width=150),
		_column("Ward", "ward", "Link", options="IONE Ward", width=120),
		_column("Numerator", "numerator", "Float"),
		_column("Denominator", "denominator", "Float"),
		_column("Value", "indicator_value", "Float"),
		_column("Target", "target_value", "Float"),
		_column("Target Min", "target_min", "Float"),
		_column("Target Max", "target_max", "Float"),
		_column("Result Status", "result_status", width=110),
		_column(
			"Calculation",
			"calculation",
			"Link",
			options="IONE Indicator Calculation",
			width=150,
		),
		_column("Computed At", "computed_at", "Datetime", width=150),
		_column("Calculator Key", "calculator_key", width=130),
		_column("Calculator Version", "calculator_version", width=130),
		_column("Result Revision", "result_revision", "Int", width=110),
		_column("Input Receipt Hash", "input_receipt_hash", width=190),
		_column("Result Checksum", "result_checksum", width=190),
	]


def _finding_closure_columns() -> list[dict[str, Any]]:
	return [
		_column("Department", "department", "Link", options="IONE Medical Department", width=160),
		_column("Severity", "severity", width=100),
		_column("Findings", "finding_count", "Int"),
		_column("Active Findings", "active_findings", "Int", width=120),
		_column("Closed Findings", "closed_findings", "Int", width=120),
		_column("Human Appeal Overturns", "appeal_overturned_findings", "Int", width=160),
		_column("Overdue Findings", "overdue_findings", "Int", width=130),
		_column("Rectifications", "rectification_count", "Int", width=120),
		_column("Active Rectifications", "active_rectifications", "Int", width=150),
		_column("Closed Rectifications", "closed_rectifications", "Int", width=150),
		_column(
			"Rejected/Cancelled Rectifications",
			"rejected_cancelled_rectifications",
			"Int",
			width=210,
		),
		_column("Overdue Rectifications", "overdue_rectifications", "Int", width=170),
		_column("Verifications", "verification_count", "Int", width=120),
		_column("Effective", "effective_verifications", "Int"),
		_column("Partially Effective", "partially_effective_verifications", "Int", width=140),
		_column("Ineffective", "ineffective_verifications", "Int"),
		_column("Rejected Verifications", "rejected_verifications", "Int", width=150),
		_column("Pending Verifications", "pending_verifications", "Int", width=150),
	]


def _integration_reconciliation_columns() -> list[dict[str, Any]]:
	return [
		_column(
			"Reconciliation",
			"reconciliation",
			"Link",
			options="IONE Data Reconciliation",
			width=160,
		),
		_column("Source System", "source_system", "Link", options="IONE Source System", width=140),
		_column(
			"Endpoint",
			"endpoint",
			"Link",
			options="IONE Integration Endpoint",
			width=150,
		),
		_column("Period Start", "period_start", "Datetime", width=150),
		_column("Period End", "period_end", "Datetime", width=150),
		_column("Status", "status", width=100),
		_column("Source Count", "source_count", "Int"),
		_column("Message Count", "message_count", "Int"),
		_column("Event Count", "event_count", "Int"),
		_column("Execution Count", "execution_count", "Int"),
		_column("Error Count", "error_count", "Int"),
		_column("Difference Count", "difference_count", "Int", width=130),
		_column("Source Hash", "source_hash", width=160),
		_column("QMS Hash", "qms_hash", width=160),
		_column("Started At", "started_at", "Datetime", width=150),
		_column("Completed At", "completed_at", "Datetime", width=150),
	]


def _ai_usage_columns() -> list[dict[str, Any]]:
	return [
		_column("Fact Date", "fact_date", "Date"),
		_column("Task Type", "task_type", width=140),
		_column("Policy", "policy", "Link", options="IONE Agent Policy", width=150),
		_column("Flow Agent", "flow_agent", "Link", options="Flow Agent", width=140),
		_column("Flow Model", "flow_model", "Link", options="Flow Model", width=140),
		_column("Model ID", "model_id", width=150),
		_column("Hospital", "hospital", "Link", options="IONE Hospital", width=130),
		_column("Campus", "campus", "Link", options="IONE Hospital Campus", width=130),
		_column("Department", "department", "Link", options="IONE Medical Department", width=150),
		_column("Ward", "ward", "Link", options="IONE Ward", width=120),
		_column("Governed Tasks", "task_count", "Int", width=120),
		_column("Successful Tasks", "success_count", "Int", width=120),
		_column("Failed/Rejected/Cancelled", "failure_count", "Int", width=180),
		_column("Pending/Other Tasks", "other_task_count", "Int", width=140),
		_column("Human Review Required", "human_review_required_count", "Int", width=160),
		_column(
			"Successful Tasks With Accepted Candidate",
			"successful_tasks_with_accepted_candidate",
			"Int",
			width=240,
		),
		_column("Accepted Candidates", "accepted_candidate_count", "Int", width=150),
		_column(
			"Successful Task Acceptance Rate (%)",
			"successful_task_acceptance_rate",
			"Percent",
			width=230,
		),
		_column("Average Duration (ms)", "average_duration_ms", "Float", width=160),
		_column("Input Tokens", "input_tokens", "Int"),
		_column("Output Tokens", "output_tokens", "Int"),
		_column("Total Tokens", "total_tokens", "Int"),
	]


def _medical_record_review_columns() -> list[dict[str, Any]]:
	return [
		_column("Data State", "data_state", width=90),
		_column("Batch", "batch", "Link", options="IONE Medical Record Review Batch", width=160),
		_column("Period Start", "period_start", "Date"),
		_column("Period End", "period_end", "Date"),
		_column("Hospital", "hospital", "Link", options="IONE Hospital", width=130),
		_column("Campus", "campus", "Link", options="IONE Hospital Campus", width=130),
		_column("Department", "department", "Link", options="IONE Medical Department", width=150),
		_column("Ward", "ward", "Link", options="IONE Ward", width=120),
		_column(
			"Sampling Policy",
			"policy",
			"Link",
			options="IONE Medical Record Sampling Policy",
			width=170,
		),
		_column("Policy Code", "policy_code", width=120),
		_column("Policy Version", "policy_version", width=110),
		_column("Policy Status", "policy_status", width=100),
		_column("Policy Checksum", "policy_checksum", width=180),
		_column("Archive Gate Enabled", "archive_gate_enabled", "Check", width=140),
		_column("Batch Status", "batch_status", width=100),
		_column("Declared Population", "population_count", "Int", width=140),
		_column("Declared Sample", "sample_count", "Int", width=120),
		_column("Declared Expert Sample", "expert_sample_count", "Int", width=160),
		_column("Visible Assignments", "visible_assignment_count", "Int", width=140),
		_column("Coder Review", "coder_review_assignment_count", "Int", width=110),
		_column("Expert Review", "expert_review_assignment_count", "Int", width=110),
		_column("Closed Assignments", "closed_assignment_count", "Int", width=140),
		_column("Coder Required", "coder_required_count", "Int", width=120),
		_column("Expert Required", "expert_required_count", "Int", width=120),
		_column("Deterministic Passed", "deterministic_passed_count", "Int", width=155),
		_column("Deterministic Held", "deterministic_held_count", "Int", width=145),
		_column("Coder Decisions", "coder_decision_count", "Int", width=120),
		_column("Coder Pass", "coder_pass_count", "Int"),
		_column("Coder Defect", "coder_defect_count", "Int"),
		_column("Coder Needs Correction", "coder_needs_correction_count", "Int", width=160),
		_column(
			"Coder Unable to Determine",
			"coder_unable_to_determine_count",
			"Int",
			width=180,
		),
		_column("Expert Decisions", "expert_decision_count", "Int", width=120),
		_column("Expert Pass", "expert_pass_count", "Int"),
		_column("Expert Defect", "expert_defect_count", "Int"),
		_column("Expert Needs Correction", "expert_needs_correction_count", "Int", width=165),
		_column(
			"Expert Unable to Determine",
			"expert_unable_to_determine_count",
			"Int",
			width=185,
		),
		_column(
			"Review Decision Checksums",
			"review_decision_checksum_count",
			"Int",
			width=175,
		),
		_column("Latest Archive Decisions", "latest_archive_decision_count", "Int", width=170),
		_column("Archive Allow", "archive_allow_count", "Int"),
		_column("Archive Hold", "archive_hold_count", "Int"),
		_column("Archive Decision Checksums", "archive_checksum_count", "Int", width=180),
		_column("Archive ACKs", "archive_ack_count", "Int"),
		_column("ACK Applied", "archive_ack_applied_count", "Int"),
		_column("ACK Rejected", "archive_ack_rejected_count", "Int"),
		_column("ACK Failed", "archive_ack_failed_count", "Int"),
		_column("Complete ACK Receipts", "archive_ack_receipt_count", "Int", width=155),
		_column("Permission-filtered Source Rows", "source_row_count", "Int", width=210),
	]


def _surgery_governance_columns() -> list[dict[str, Any]]:
	return [
		_column("Data State", "data_state", width=90),
		_column("Period Start", "period_start", "Date"),
		_column("Period End", "period_end", "Date"),
		_column("Hospital", "hospital", "Link", options="IONE Hospital", width=130),
		_column("Campus", "campus", "Link", options="IONE Hospital Campus", width=130),
		_column("Department", "department", "Link", options="IONE Medical Department", width=150),
		_column("Ward", "ward", "Link", options="IONE Ward", width=120),
		_column("Surgery Level", "surgery_level", width=100),
		_column(
			"Procedure Policy",
			"procedure_policy",
			"Link",
			options="IONE Surgery Procedure Policy",
			width=160,
		),
		_column("Policy Code", "policy_code", width=120),
		_column("Policy Version", "policy_version", width=110),
		_column("Policy Status", "policy_status", width=100),
		_column("Policy Effective From", "policy_effective_from", "Date", width=140),
		_column("Policy Effective To", "policy_effective_to", "Date", width=140),
		_column("Policy Checksum", "policy_checksum", width=180),
		_column("Policy Requires MDT", "policy_require_mdt", "Check", width=135),
		_column("Surgery Records", "source_record_count", "Int", width=120),
		_column("Governance Passed", "governance_passed_count", "Int", width=135),
		_column(
			"Governance Noncompliant",
			"governance_noncompliant_count",
			"Int",
			width=170,
		),
		_column("Governance Blocked", "governance_blocked_count", "Int", width=145),
		_column(
			"Pending Prerequisites",
			"governance_pending_prerequisites_count",
			"Int",
			width=150,
		),
		_column("Governance Cancelled", "governance_cancelled_count", "Int", width=145),
		_column("Governance Checksums", "governance_checksum_count", "Int", width=155),
		_column("Compliant", "compliant_count", "Int"),
		_column("Authorization Linked", "authorization_linked_count", "Int", width=145),
		_column("Approved Authorization", "approved_authorization_count", "Int", width=165),
		_column("MDT Required", "mdt_required_count", "Int"),
		_column("MDT Linked", "mdt_linked_count", "Int"),
		_column("Completed MDT", "completed_mdt_count", "Int", width=115),
		_column("MDT Proceed", "mdt_proceed_count", "Int"),
		_column(
			"MDT Proceed With Conditions",
			"mdt_proceed_with_conditions_count",
			"Int",
			width=190,
		),
		_column("MDT Do Not Proceed", "mdt_do_not_proceed_count", "Int", width=150),
		_column("Checklist Linked", "checklist_linked_count", "Int", width=120),
		_column("Completed Checklist", "completed_checklist_count", "Int", width=140),
		_column(
			"Approved Emergency Exception",
			"approved_emergency_exception_count",
			"Int",
			width=200,
		),
		_column("Preanesthesia Completed", "preanesthesia_completed_count", "Int", width=170),
		_column("Preanesthesia Pending", "preanesthesia_pending_count", "Int", width=155),
		_column("Preanesthesia Failed", "preanesthesia_failed_count", "Int", width=145),
		_column(
			"Intraoperative Completed",
			"intraoperative_completed_count",
			"Int",
			width=175,
		),
		_column("Intraoperative Pending", "intraoperative_pending_count", "Int", width=160),
		_column("Intraoperative Failed", "intraoperative_failed_count", "Int", width=150),
		_column("Recovery Completed", "recovery_completed_count", "Int", width=140),
		_column("Recovery Pending", "recovery_pending_count", "Int", width=130),
		_column("Recovery Failed", "recovery_failed_count", "Int", width=120),
		_column("Follow-up Completed", "followup_completed_count", "Int", width=140),
		_column("Follow-up Pending", "followup_pending_count", "Int", width=130),
		_column("Follow-up Failed", "followup_failed_count", "Int", width=120),
		_column("Cancellations", "cancelled_count", "Int"),
		_column("Unplanned Surgery", "unplanned_surgery_count", "Int", width=135),
		_column("Unplanned Return", "unplanned_return_count", "Int", width=130),
		_column("Complications", "complication_count", "Int"),
		_column("Mortality Observations", "mortality_count", "Int", width=150),
	]


def _national_ten_goal_columns() -> list[dict[str, Any]]:
	return [
		_column("Data State", "data_state", width=90),
		_column("Standard", "standard", "Link", options="IONE QC Standard", width=150),
		_column("Standard Code", "standard_code", width=125),
		_column("Standard Name", "standard_name", width=210),
		_column("Standard Issue Date", "standard_issue_date", "Date", width=135),
		_column(
			"Approved Standard Version",
			"standard_version",
			"Link",
			options="IONE QC Standard Version",
			width=185,
		),
		_column("Standard Version", "standard_version_label", width=120),
		_column("Standard Version Checksum", "standard_version_checksum", width=190),
		_column(
			"Published Clause",
			"standard_clause",
			"Link",
			options="IONE QC Standard Clause",
			width=155,
		),
		_column("Clause Code", "clause_code", width=110),
		_column("Clause Heading", "clause_heading", width=190),
		_column("Configured Objective Category", "objective_category", width=190),
		_column("Indicator", "indicator", "Link", options="IONE QC Indicator", width=150),
		_column("Indicator Code", "indicator_code", width=120),
		_column("Indicator Name", "indicator_name", width=200),
		_column(
			"Published Indicator Version",
			"indicator_version",
			"Link",
			options="IONE QC Indicator Version",
			width=190,
		),
		_column("Indicator Version", "indicator_version_label", width=125),
		_column("Indicator Version Checksum", "indicator_version_checksum", width=190),
		_column("Definition Effective From", "definition_effective_from", "Date", width=155),
		_column("Definition Effective To", "definition_effective_to", "Date", width=145),
		_column("Frequency", "calculation_frequency", width=100),
		_column("Unit", "unit", width=80),
		_column("Direction", "direction", width=120),
		_column("Definition Target", "definition_target_value", "Float", width=125),
		_column("Definition Target Min", "definition_target_min", "Float", width=145),
		_column("Definition Target Max", "definition_target_max", "Float", width=145),
		_column(
			"Indicator Result",
			"indicator_result",
			"Link",
			options="IONE Indicator Result",
			width=155,
		),
		_column(
			"Calculation",
			"calculation",
			"Link",
			options="IONE Indicator Calculation",
			width=145,
		),
		_column("Period", "period", width=90),
		_column("Period Start", "period_start", "Date"),
		_column("Period End", "period_end", "Date"),
		_column("Hospital", "hospital", "Link", options="IONE Hospital", width=130),
		_column("Campus", "campus", "Link", options="IONE Hospital Campus", width=130),
		_column("Department", "department", "Link", options="IONE Medical Department", width=150),
		_column("Ward", "ward", "Link", options="IONE Ward", width=120),
		_column("Numerator", "numerator", "Float"),
		_column("Denominator", "denominator", "Float"),
		_column("Indicator Value", "indicator_value", "Float", width=120),
		_column("Result Target", "result_target_value", "Float", width=110),
		_column("Result Target Min", "result_target_min", "Float", width=130),
		_column("Result Target Max", "result_target_max", "Float", width=130),
		_column("Target State", "target_state", width=190),
		_column("Result Status", "result_status", width=110),
		_column("Computed At", "computed_at", "Datetime", width=150),
		_column("Calculator Key", "calculator_key", width=130),
		_column("Calculator Version", "calculator_version", width=130),
		_column("Result Revision", "result_revision", "Int", width=110),
		_column("Input Receipt Hash", "input_receipt_hash", width=190),
		_column("Result Checksum", "result_checksum", width=190),
		_column("Source Result Count", "source_result_count", "Int", width=135),
	]


def _improvement_governance_columns() -> list[dict[str, Any]]:
	return [
		_column("Data State", "data_state", width=90),
		_column("Period Start", "period_start", "Date"),
		_column("Period End", "period_end", "Date"),
		_column("Hospital", "hospital", "Link", options="IONE Hospital", width=130),
		_column("Campus", "campus", "Link", options="IONE Hospital Campus", width=130),
		_column("Department", "department", "Link", options="IONE Medical Department", width=150),
		_column("Ward", "ward", "Link", options="IONE Ward", width=120),
		_column("Recurrence Evaluations", "recurrence_evaluation_count", "Int", width=150),
		_column("Triggered Recurrences", "recurrence_triggered_count", "Int", width=150),
		_column("Below Threshold", "recurrence_below_threshold_count", "Int", width=125),
		_column("Recorded Occurrences", "recurrence_occurrence_count", "Int", width=145),
		_column(
			"Complete Recurrence Lineage",
			"recurrence_lineage_complete_count",
			"Int",
			width=185,
		),
		_column("PDCA Projects", "pdca_project_count", "Int", width=110),
		_column("Special Recurrence PDCA", "special_recurrence_pdca_count", "Int", width=170),
		_column(
			"Complete Special PDCA Lineage",
			"special_recurrence_lineage_complete_count",
			"Int",
			width=200,
		),
		_column(
			"Structured Root Cause Complete",
			"structured_root_cause_complete_count",
			"Int",
			width=205,
		),
		_column("PDCA Proposed", "pdca_proposed_count", "Int", width=110),
		_column("PDCA Approved", "pdca_approved_count", "Int", width=110),
		_column("PDCA Active", "pdca_active_count", "Int"),
		_column("PDCA Measuring", "pdca_measuring_count", "Int", width=115),
		_column("PDCA Closed", "pdca_closed_count", "Int"),
		_column(
			"Past Effect Verification Gate",
			"effect_verification_gate_count",
			"Int",
			width=190,
		),
		_column(
			"Standardization Receipts",
			"standardization_receipt_count",
			"Int",
			width=165,
		),
		_column("Quality Meetings", "meeting_count", "Int", width=120),
		_column("Meeting Held", "meeting_held_count", "Int"),
		_column("Minutes Pending", "meeting_minutes_pending_count", "Int", width=120),
		_column("Meeting Closed", "meeting_closed_count", "Int", width=110),
		_column("Frozen Agendas", "frozen_agenda_count", "Int", width=110),
		_column("Meeting Minutes", "meeting_minute_count", "Int", width=120),
		_column("Approved Minutes", "approved_minute_count", "Int", width=120),
		_column(
			"Approved Minute Receipts",
			"approved_minute_receipt_count",
			"Int",
			width=175,
		),
		_column("Meeting Decisions", "meeting_decision_count", "Int", width=130),
		_column(
			"Decision Checksums",
			"meeting_decision_checksum_count",
			"Int",
			width=135,
		),
		_column("Action Items", "action_item_count", "Int"),
		_column(
			"Action Verification Rounds",
			"action_verification_round_count",
			"Int",
			width=180,
		),
		_column("Action Rework Rounds", "action_rework_round_count", "Int", width=155),
		_column(
			"Chained Action Receipts",
			"action_chained_receipt_count",
			"Int",
			width=170,
		),
		_column("Action Open", "action_open_count", "Int"),
		_column("Action In Progress", "action_in_progress_count", "Int", width=130),
		_column(
			"Action Pending Verification",
			"action_pending_verification_count",
			"Int",
			width=180,
		),
		_column("Action Closed", "action_closed_count", "Int"),
		_column("Overdue Actions", "overdue_action_count", "Int", width=115),
		_column(
			"Independently Verified Actions",
			"independently_verified_action_count",
			"Int",
			width=200,
		),
		_column("Experience Versions", "experience_version_count", "Int", width=135),
		_column("Experience Published", "experience_published_count", "Int", width=145),
		_column("Experience Retired", "experience_retired_count", "Int", width=130),
		_column(
			"Publication Receipts",
			"experience_publication_receipt_count",
			"Int",
			width=140,
		),
		_column("Permission-filtered Source Rows", "source_row_count", "Int", width=210),
	]
