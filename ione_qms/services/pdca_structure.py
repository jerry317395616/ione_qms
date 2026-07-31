from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import frappe
from frappe.utils import getdate, now_datetime

from ione_qms.permissions import require_role
from ione_qms.services.finding_recurrence import (
	RECURRENCE_GOAL_PLACEHOLDER,
	RECURRENCE_PROBLEM_PLACEHOLDER,
	is_controlled_recurrence_pdca_proposal,
)
from ione_qms.services.indicators import (
	current_indicator_result_lock,
	current_indicator_results_lock,
)

MAX_ROOT_CAUSES = 100
MAX_PARETO_ITEMS = 100
MAX_IMPROVEMENT_MEASURES = 100
MAX_EFFECT_MEASUREMENTS = 50
MAX_GOVERNANCE_TEXT = 4_000
MAX_WHY_LEVEL = 20

ROOT_CAUSE_METHODS = frozenset({"Five Why", "Fishbone", "Other"})
PROJECT_ANALYSIS_METHODS = frozenset({"Five Why", "Fishbone", "Pareto", "Combined", "Other"})
FISHBONE_CATEGORIES = frozenset({"People", "Process", "Technology", "Environment", "Policy", "Data", "Other"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PDCA_TERMINAL = frozenset({"Closed", "Rejected", "Cancelled"})
_RECURRENCE_FIELDS = (
	"recurrence_policy",
	"recurrence_evaluation",
	"recurrence_rule",
	"recurrence_policy_checksum",
	"recurrence_group_key",
	"recurrence_case_key",
	"recurrence_snapshot_hash",
)
_STANDARDIZATION_FIELDS = (
	"standardization_decision",
	"standardization_conclusion",
	"standardization_evidence_reference",
	"standardization_evidence_hash",
	"standardized_by",
	"standardized_at",
)
_EFFECT_VERIFICATION_FLAG = "ione_pdca_effect_verification"
_STANDARDIZATION_FLAG = "ione_pdca_standardization"


def validate_pdca_structure(doc, previous=None) -> None:
	"""Validate structured analysis, action, measurement, and standardization records."""
	previous = previous if previous is not None else doc.get_doc_before_save()
	status = str(doc.get("status") or "Proposed")
	previous_status = str(previous.get("status") or "") if previous else ""
	transition = (previous_status, status) if previous and previous_status != status else None

	_validate_recurrence_lineage(doc, previous)
	_validate_root_causes(doc, previous)
	_validate_pareto(doc, previous)
	_validate_improvement_measures(doc, previous)
	_validate_effect_measurements(doc, previous)
	_validate_standardization_mutation(doc, previous)

	if doc.get("start_date") and doc.get("end_date") and getdate(doc.end_date) < getdate(doc.start_date):
		frappe.throw("PDCA project end date cannot precede start date")
	if transition == ("Proposed", "Approved"):
		_validate_project_approval_content(doc)
	if transition == ("Approved", "Active"):
		_validate_action_plan_ready(doc)
	if transition == ("Active", "Measuring") or status in {"Measuring", "Closed"}:
		_require_verified_effect_measurements(doc)
		_require_completed_measures(doc)
	if transition == ("Measuring", "Closed") or status == "Closed":
		_require_standardization_record(doc)


def verify_pdca_effect_measurements(
	project: str,
	measurement_codes: list[str] | str,
	verification_comment: str,
) -> dict[str, Any]:
	_require_named_pdca_reviewer()
	codes = _normalize_code_list(measurement_codes)
	comment = _bounded_text(verification_comment, "Effect verification comment")
	with frappe.db.advisory_lock(_pdca_lock(project), timeout=15):
		doc = frappe.get_doc("IONE PDCA Project", project, for_update=True)
		doc.check_permission("write")
		if doc.status != "Active":
			frappe.throw("PDCA effect measurements may only be verified while the project is Active")
		if frappe.session.user in {str(doc.owner or ""), str(doc.get("project_owner") or "")}:
			frappe.throw("PDCA project owner cannot independently verify effect measurements")
		rows = {str(row.get("measurement_code") or ""): row for row in doc.get("effect_measurements") or []}
		if set(codes).difference(rows):
			frappe.throw("One or more requested effect measurement codes do not exist")
		pending_bindings = {
			code: _validated_effect_binding(rows[code]) for code in codes if not rows[code].get("verified_at")
		}
		source_names = {
			binding[fieldname]
			for binding in pending_bindings.values()
			for fieldname in ("baseline_source", "actual_source")
		}
		with current_indicator_results_lock(source_names) as locked_sources:
			source_snapshots = {
				name: _indicator_result_snapshot_payload(source) for name, source in locked_sources.items()
			}
			verified: list[str] = []
			now = now_datetime()
			for code in codes:
				row = rows[code]
				if row.get("verified_at"):
					_validate_complete_effect_row(
						doc,
						row,
						require_verified=True,
						resolve_current_sources=False,
					)
					verified.append(code)
					continue
				source_hash, measurement_hash = _validated_effect_hashes(
					doc,
					row,
					source_snapshots=source_snapshots,
				)
				row.source_snapshot_hash = source_hash
				row.verified_by = frappe.session.user
				row.verified_at = now
				row.verification_comment = comment
				row.measurement_hash = measurement_hash
				verified.append(code)
			with _controlled_effect_verification(doc.name, tuple(codes)):
				doc.save()
	return {"project": doc.name, "verified_measurements": verified}


def record_pdca_standardization(
	project: str,
	decision: str,
	conclusion: str,
	evidence_reference: str,
	evidence_hash: str,
) -> dict[str, str]:
	_require_named_pdca_reviewer()
	if decision not in {"Adopted", "Not Adopted"}:
		frappe.throw("Standardization decision must be Adopted or Not Adopted")
	conclusion_text = _bounded_text(conclusion, "Standardization conclusion")
	reference = _bounded_text(evidence_reference, "Standardization evidence reference")
	digest = str(evidence_hash or "")
	if not _HEX64.fullmatch(digest):
		frappe.throw("Standardization evidence hash must be an exact lowercase SHA-256 digest")
	with frappe.db.advisory_lock(_pdca_lock(project), timeout=15):
		doc = frappe.get_doc("IONE PDCA Project", project, for_update=True)
		doc.check_permission("write")
		if doc.status != "Measuring":
			frappe.throw("Standardization may only be recorded while the PDCA project is Measuring")
		if frappe.session.user in {str(doc.owner or ""), str(doc.get("project_owner") or "")}:
			frappe.throw("PDCA project owner cannot independently record the standardization decision")
		if doc.get("standardized_at"):
			if all(
				(
					doc.get("standardization_decision") == decision,
					doc.get("standardization_conclusion") == conclusion_text,
					doc.get("standardization_evidence_reference") == reference,
					doc.get("standardization_evidence_hash") == digest,
				)
			):
				return {"project": doc.name, "decision": decision}
			frappe.throw("PDCA standardization receipt is immutable once recorded")
		with _controlled_standardization(doc.name):
			doc.standardization_decision = decision
			doc.standardization_conclusion = conclusion_text
			doc.standardization_evidence_reference = reference
			doc.standardization_evidence_hash = digest
			doc.standardized_by = frappe.session.user
			doc.standardized_at = now_datetime()
			doc.save()
	return {"project": doc.name, "decision": decision}


def _validate_recurrence_lineage(doc, previous) -> None:
	project_type = str(doc.get("project_type") or "Regular")
	if project_type not in {"Regular", "Special Recurrence"}:
		frappe.throw("PDCA project type is invalid")
	if project_type == "Regular":
		if any(doc.get(fieldname) for fieldname in (*_RECURRENCE_FIELDS, "active_recurrence_key")):
			frappe.throw("Regular PDCA projects cannot contain recurrence-system provenance")
		return
	if not all(doc.get(fieldname) for fieldname in _RECURRENCE_FIELDS):
		frappe.throw("Special recurrence PDCA projects require complete immutable recurrence provenance")
	if previous is None:
		if not is_controlled_recurrence_pdca_proposal(doc):
			frappe.throw(
				"Special recurrence PDCA projects may only be proposed by the governed recurrence evaluator",
				frappe.PermissionError,
			)
	else:
		changed = [
			fieldname
			for fieldname in ("project_type", *_RECURRENCE_FIELDS)
			if _canonical_value(doc.get(fieldname)) != _canonical_value(previous.get(fieldname))
		]
		if changed:
			frappe.throw("PDCA recurrence provenance is immutable: " + ", ".join(changed))
		_validate_persisted_recurrence_lineage(doc)
	active_key = (
		str(doc.get("recurrence_group_key"))
		if str(doc.get("status") or "Proposed") not in _PDCA_TERMINAL
		else None
	)
	doc.set("active_recurrence_key", active_key)


def _validate_persisted_recurrence_lineage(doc) -> None:
	evaluation = frappe.db.get_value(
		"IONE Finding Recurrence Evaluation",
		doc.recurrence_evaluation,
		[
			"policy",
			"policy_checksum",
			"rule",
			"group_key",
			"finding_set_hash",
			"evaluation_hash",
			"outcome",
			"hospital",
			"campus",
			"department",
			"ward",
		],
		as_dict=True,
	)
	if not evaluation or evaluation.outcome != "Triggered":
		frappe.throw("PDCA recurrence evaluation is missing or is not a triggered immutable evaluation")
	expected = {
		"recurrence_policy": evaluation.policy,
		"recurrence_policy_checksum": evaluation.policy_checksum,
		"recurrence_rule": evaluation.rule,
		"recurrence_group_key": evaluation.group_key,
		"recurrence_case_key": _hash_payload(
			{"group_key": evaluation.group_key, "finding_set_hash": evaluation.finding_set_hash}
		),
		"recurrence_snapshot_hash": evaluation.evaluation_hash,
		"hospital": evaluation.hospital,
		"campus": evaluation.campus,
		"department": evaluation.department,
		"ward": evaluation.ward,
	}
	if not all(_same_optional(doc.get(fieldname), value) for fieldname, value in expected.items()):
		frappe.throw("PDCA project recurrence lineage no longer matches its immutable evaluation")


def _validate_root_causes(doc, previous) -> None:
	rows = list(doc.get("root_causes") or [])
	if len(rows) > MAX_ROOT_CAUSES:
		frappe.throw("PDCA root cause rows exceed the hard limit")
	by_code: dict[str, Any] = {}
	for row in rows:
		code = _validated_code(row.get("cause_code"), "Root cause code")
		if code in by_code:
			frappe.throw("PDCA root cause codes must be unique")
		by_code[code] = row
		method = str(row.get("analysis_method") or "")
		if method not in ROOT_CAUSE_METHODS:
			frappe.throw("Every root cause requires a supported structured analysis method")
		cause = str(row.get("cause") or "").strip()
		if not cause or len(cause) > MAX_GOVERNANCE_TEXT:
			frappe.throw("Every root cause requires bounded cause text")
		row.cause = cause
		evidence = str(row.get("evidence") or "").strip()
		row.evidence_hash = _hash_text(evidence) if evidence else None
		if int(row.get("confirmed") or 0) and not evidence:
			frappe.throw("A confirmed root cause requires evidence")
		if method == "Fishbone":
			if str(row.get("category") or "") not in FISHBONE_CATEGORIES:
				frappe.throw("Fishbone causes require a supported fishbone category")
			if row.get("parent_cause_code") or row.get("why_level"):
				frappe.throw("Fishbone causes cannot contain Five Why hierarchy fields")
		elif method == "Other":
			if row.get("parent_cause_code") or row.get("why_level"):
				frappe.throw("Other root causes cannot contain Five Why hierarchy fields")
		else:
			level = _exact_int(row.get("why_level"), "Five Why level", 1, MAX_WHY_LEVEL)
			row.why_level = level
			parent_code = str(row.get("parent_cause_code") or "")
			if level == 1 and parent_code:
				frappe.throw("A level-one Five Why cause cannot have a parent")
			if level > 1 and not parent_code:
				frappe.throw("A deeper Five Why cause requires its immediate parent code")
	for code, row in by_code.items():
		if str(row.get("analysis_method")) != "Five Why" or int(row.get("why_level") or 0) == 1:
			continue
		parent_code = str(row.get("parent_cause_code") or "")
		parent = by_code.get(parent_code)
		if (
			not parent
			or str(parent.get("analysis_method") or "") != "Five Why"
			or int(parent.get("why_level") or 0) != int(row.get("why_level") or 0) - 1
		):
			frappe.throw(f"Five Why cause {code} must reference a cause at the immediately preceding level")
	_assert_frozen_analysis_rows(doc, previous, "root_causes", _root_cause_payload)


def _validate_pareto(doc, previous) -> None:
	rows = list(doc.get("pareto_items") or [])
	if len(rows) > MAX_PARETO_ITEMS:
		frappe.throw("PDCA Pareto rows exceed the hard limit")
	if not rows:
		_assert_frozen_analysis_rows(doc, previous, "pareto_items", _pareto_payload)
		return
	by_code: dict[str, Any] = {}
	for row in rows:
		code = _validated_code(row.get("cause_code"), "Pareto cause code")
		if code in by_code:
			frappe.throw("Pareto cause codes must be unique")
		by_code[code] = row
		count = _exact_int(row.get("occurrence_count"), "Pareto occurrence count", 1, 1_000_000_000)
		row.occurrence_count = count
		if str(row.get("cause") or "").strip() == "":
			frappe.throw("Pareto rows require a cause label")
	expected_order = sorted(by_code.items(), key=lambda item: (-int(item[1].occurrence_count), item[0]))
	total = sum(int(row.occurrence_count) for _, row in expected_order)
	cumulative = 0
	for rank, (code, row) in enumerate(expected_order, start=1):
		cumulative += int(row.occurrence_count)
		expected_percent = (Decimal(cumulative) * Decimal(100) / Decimal(total)).quantize(
			Decimal("0.01"),
			rounding=ROUND_HALF_UP,
		)
		if (
			_exact_int(row.get("rank"), "Pareto rank", 1, MAX_PARETO_ITEMS) != rank
			or _exact_int(
				row.get("cumulative_count"),
				"Pareto cumulative count",
				1,
				1_000_000_000,
			)
			!= cumulative
			or _decimal(row.get("cumulative_percent"), "Pareto cumulative percent") != expected_percent
		):
			frappe.throw(
				f"Pareto row {code} rank, cumulative count, or cumulative percent is not deterministic"
			)
	_assert_frozen_analysis_rows(doc, previous, "pareto_items", _pareto_payload)


def _validate_improvement_measures(doc, previous) -> None:
	rows = list(doc.get("improvement_measures") or [])
	if len(rows) > MAX_IMPROVEMENT_MEASURES:
		frappe.throw("PDCA improvement measure rows exceed the hard limit")
	seen: set[str] = set()
	previous_rows = _rows_by_code(previous, "improvement_measures", "measure_code")
	for row in rows:
		code = _validated_code(row.get("measure_code"), "Improvement measure code")
		if code in seen:
			frappe.throw("PDCA improvement measure codes must be unique")
		seen.add(code)
		if str(row.get("measure_type") or "") not in {
			"Corrective",
			"Preventive",
			"Standardization",
		}:
			frappe.throw("Improvement measure type is invalid")
		if not str(row.get("measure") or "").strip():
			frappe.throw("Improvement measure description is required")
		status = str(row.get("status") or "Planned")
		if status not in {"Planned", "In Progress", "Completed", "Cancelled"}:
			frappe.throw("Improvement measure status is invalid")
		if status != "Cancelled" and (not row.get("owner_user") or not row.get("due_date")):
			frappe.throw("Every active improvement measure requires an owner and due date")
		if (
			row.get("planned_start")
			and row.get("due_date")
			and getdate(row.due_date) < getdate(row.planned_start)
		):
			frappe.throw("Improvement measure due date cannot precede planned start")
		evidence = str(row.get("completion_evidence") or "").strip()
		row.completion_evidence_hash = _hash_text(evidence) if evidence else None
		if status == "Completed" and not evidence:
			frappe.throw("Completed improvement measures require completion evidence")
		if status == "Cancelled" and not str(row.get("cancellation_reason") or "").strip():
			frappe.throw("Cancelled improvement measures require a reason")
		previous_row = previous_rows.get(code)
		if previous_row and str(previous_row.get("status") or "") == "Completed":
			if _measure_payload(row) != _measure_payload(previous_row):
				frappe.throw(f"Completed improvement measure {code} is immutable")


def _validate_effect_measurements(doc, previous) -> None:
	rows = list(doc.get("effect_measurements") or [])
	if len(rows) > MAX_EFFECT_MEASUREMENTS:
		frappe.throw("PDCA effect measurement rows exceed the hard limit")
	seen: set[str] = set()
	previous_rows = _rows_by_code(previous, "effect_measurements", "measurement_code")
	context = getattr(frappe.flags, _EFFECT_VERIFICATION_FLAG, None)
	allowed_verifications = (
		set(context.get("measurement_codes") or ())
		if isinstance(context, dict) and context.get("project") == doc.name
		else set()
	)
	if (
		previous
		and any(row.get("verified_at") for row in previous_rows.values())
		and any(
			not _same_optional(doc.get(fieldname), previous.get(fieldname))
			for fieldname in ("hospital", "campus", "department", "ward")
		)
	):
		frappe.throw("PDCA project scope is immutable once effect evidence is verified")
	for row in rows:
		code = _validated_code(row.get("measurement_code"), "Effect measurement code")
		if code in seen:
			frappe.throw("PDCA effect measurement codes must be unique")
		seen.add(code)
		previous_row = previous_rows.get(code)
		was_verified = bool(previous_row and previous_row.get("verified_at"))
		is_verified = bool(row.get("verified_at"))
		if was_verified and _effect_payload(row) != _effect_payload(previous_row):
			frappe.throw(f"Verified effect measurement {code} is immutable")
		if not was_verified and is_verified and code not in allowed_verifications:
			frappe.throw("Effect measurements may only be verified through the POST-only API")
		if is_verified:
			_validate_complete_effect_row(
				doc,
				row,
				require_verified=True,
				resolve_current_sources=not was_verified,
			)
		elif any(
			row.get(fieldname)
			for fieldname in (
				"source_snapshot_hash",
				"verified_by",
				"verification_comment",
				"measurement_hash",
			)
		):
			frappe.throw("Unverified effect measurements cannot contain verification receipt fields")
	removed_verified = [
		code
		for code, previous_row in previous_rows.items()
		if code not in seen and previous_row.get("verified_at")
	]
	if removed_verified:
		frappe.throw("Verified effect measurements cannot be removed from the PDCA evidence record")


def _validate_complete_effect_row(
	doc,
	row,
	*,
	require_verified: bool,
	resolve_current_sources: bool = True,
) -> None:
	if resolve_current_sources:
		source_hash, measurement_hash = _validated_effect_hashes(doc, row)
	else:
		source_hash, measurement_hash = _validated_frozen_effect_hashes(row)
	if not require_verified:
		return
	if (
		not row.get("verified_by")
		or not row.get("verified_at")
		or not str(row.get("verification_comment") or "").strip()
		or str(row.get("source_snapshot_hash") or "") != source_hash
		or str(row.get("measurement_hash") or "") != measurement_hash
	):
		frappe.throw("Effect measurement verification receipt is incomplete or drifted")


def _validated_effect_hashes(
	doc,
	row,
	*,
	source_snapshots: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, str]:
	binding = _validated_effect_binding(row)
	indicator = binding["indicator"]
	baseline_name = binding["baseline_source"]
	actual_name = binding["actual_source"]
	baseline_start = getdate(binding["baseline_period_start"])
	baseline_end = getdate(binding["baseline_period_end"])
	measurement_start = getdate(binding["measurement_period_start"])
	measurement_end = getdate(binding["measurement_period_end"])
	if source_snapshots is None:
		baseline = _indicator_result_snapshot(baseline_name)
		actual = _indicator_result_snapshot(actual_name)
	else:
		baseline = source_snapshots.get(baseline_name)
		actual = source_snapshots.get(actual_name)
		if baseline is None or actual is None:
			frappe.throw("Effect measurement source is outside the globally locked result set")
	for label, source, expected_start, expected_end, expected_value in (
		("baseline", baseline, baseline_start, baseline_end, row.get("baseline_value")),
		("actual", actual, measurement_start, measurement_end, row.get("actual_value")),
	):
		if source["indicator"] != indicator:
			frappe.throw(f"Effect measurement {label} source indicator does not match")
		if getdate(source["period_start"]) != expected_start or getdate(source["period_end"]) != expected_end:
			frappe.throw(f"Effect measurement {label} source period does not match")
		if _decimal(source["indicator_value"], f"{label} source value") != _decimal(
			expected_value,
			f"{label} stored value",
		):
			frappe.throw(f"Effect measurement {label} source value does not match")
		if any(
			not _same_optional(source.get(fieldname), doc.get(fieldname))
			for fieldname in ("hospital", "campus", "department", "ward")
		):
			frappe.throw(f"Effect measurement {label} source scope does not match the PDCA project")
	source_hash = _hash_payload({"baseline": baseline, "actual": actual})
	return source_hash, _effect_measurement_hash(binding, source_hash)


def _validated_frozen_effect_hashes(row) -> tuple[str, str]:
	"""Verify a persisted evidence receipt without reopening mutable current pointers."""
	binding = _validated_effect_binding(row)
	source_hash = str(row.get("source_snapshot_hash") or "")
	if not _HEX64.fullmatch(source_hash):
		frappe.throw("Effect measurement source snapshot hash is not an exact SHA-256 digest")
	return source_hash, _effect_measurement_hash(binding, source_hash)


def _validated_effect_binding(row) -> dict[str, str]:
	code = _validated_code(row.get("measurement_code"), "Effect measurement code")
	indicator = str(row.get("indicator") or "")
	baseline_name = str(row.get("baseline_source") or "")
	actual_name = str(row.get("actual_source") or "")
	if not indicator or not baseline_name or not actual_name:
		frappe.throw(f"Effect measurement {code} requires indicator, baseline source, and actual source")
	if (
		_is_blank(row.get("baseline_value"))
		or _is_blank(row.get("target_value"))
		or _is_blank(row.get("actual_value"))
	):
		frappe.throw(f"Effect measurement {code} requires baseline, target, and actual values")
	if not all(
		row.get(fieldname)
		for fieldname in (
			"baseline_period_start",
			"baseline_period_end",
			"measurement_period_start",
			"measurement_period_end",
		)
	):
		frappe.throw(f"Effect measurement {code} requires baseline and measurement periods")
	baseline_start = getdate(row.baseline_period_start)
	baseline_end = getdate(row.baseline_period_end)
	measurement_start = getdate(row.measurement_period_start)
	measurement_end = getdate(row.measurement_period_end)
	if baseline_end < baseline_start or measurement_end < measurement_start:
		frappe.throw("Effect measurement period end cannot precede period start")
	if baseline_end >= measurement_start:
		frappe.throw("PDCA baseline period must end before its measurement period starts")
	return {
		"measurement_code": code,
		"indicator": indicator,
		"baseline_source": baseline_name,
		"actual_source": actual_name,
		"baseline_period_start": str(baseline_start),
		"baseline_period_end": str(baseline_end),
		"measurement_period_start": str(measurement_start),
		"measurement_period_end": str(measurement_end),
		"baseline_value": _decimal_text(row.get("baseline_value")),
		"target_value": _decimal_text(row.get("target_value")),
		"actual_value": _decimal_text(row.get("actual_value")),
	}


def _effect_measurement_hash(binding: dict[str, str], source_hash: str) -> str:
	return _hash_payload(
		{
			**binding,
			"source_snapshot_hash": source_hash,
		}
	)


def _indicator_result_snapshot(name: str) -> dict[str, Any]:
	with current_indicator_result_lock(name) as (_pointer, row):
		snapshot = _indicator_result_snapshot_payload(row)
	return snapshot


def _indicator_result_snapshot_payload(row) -> dict[str, Any]:
	snapshot = {
		"name": str(row.name),
		"result_key": str(row.get("result_key") or ""),
		"indicator": str(row.get("indicator") or ""),
		"indicator_version": str(row.get("indicator_version") or ""),
		"series_key": str(row.get("series_key") or ""),
		"result_revision": int(row.get("result_revision") or 0),
		"input_receipt_hash": str(row.get("input_receipt_hash") or ""),
		"result_checksum": str(row.get("result_checksum") or ""),
		"period_start": str(row.get("period_start")),
		"period_end": str(row.get("period_end")),
		"indicator_value": _decimal_text(row.get("indicator_value")),
		"status": str(row.get("status") or ""),
		"hospital": str(row.get("hospital") or ""),
		"campus": str(row.get("campus") or ""),
		"department": str(row.get("department") or ""),
		"ward": str(row.get("ward") or ""),
		"calculator_key": str(row.get("calculator_key") or ""),
		"calculator_version": str(row.get("calculator_version") or ""),
		"lineage_status": str(row.get("lineage_status") or ""),
		"lineage_hash": _hash_text(str(row.get("lineage_json") or "")),
	}
	if snapshot["status"] in {"Failed", "No Data"}:
		frappe.throw("PDCA effect source must be an available governed Indicator Result")
	return snapshot


def _validate_standardization_mutation(doc, previous) -> None:
	if previous is None:
		if any(doc.get(fieldname) for fieldname in _STANDARDIZATION_FIELDS):
			frappe.throw("New PDCA projects cannot contain a standardization decision receipt")
		return
	changed = {
		fieldname
		for fieldname in _STANDARDIZATION_FIELDS
		if _canonical_value(doc.get(fieldname)) != _canonical_value(previous.get(fieldname))
	}
	if not changed:
		return
	if previous.get("standardized_at"):
		frappe.throw("PDCA standardization receipt is immutable once recorded")
	context = getattr(frappe.flags, _STANDARDIZATION_FLAG, None)
	if not isinstance(context, dict) or str(context.get("project") or "") != str(doc.name or ""):
		frappe.throw("PDCA standardization must be recorded through the POST-only API")


def _validate_project_approval_content(doc) -> None:
	if not doc.get("project_owner"):
		frappe.throw("Approved PDCA projects require an accountable project owner")
	problem = str(doc.get("problem_statement") or "").strip()
	goal = str(doc.get("goal") or "").strip()
	if not problem or not goal:
		frappe.throw("Approved PDCA projects require a problem statement and measurable goal")
	if doc.get("project_type") == "Special Recurrence" and (
		problem == RECURRENCE_PROBLEM_PLACEHOLDER or goal == RECURRENCE_GOAL_PLACEHOLDER
	):
		frappe.throw("A human reviewer must replace recurrence proposal placeholders before approval")


def _validate_action_plan_ready(doc) -> None:
	method = str(doc.get("analysis_method") or "")
	if method not in PROJECT_ANALYSIS_METHODS:
		frappe.throw("PDCA activation requires a supported root cause analysis method")
	if not int(doc.get("root_cause_analysis_complete") or 0):
		frappe.throw("PDCA activation requires human attestation that root cause analysis is complete")
	if not str(doc.get("root_cause_conclusion") or "").strip():
		frappe.throw("PDCA activation requires a root cause analysis conclusion")
	root_methods = {
		str(row.get("analysis_method") or "")
		for row in doc.get("root_causes") or []
		if row.get("analysis_method")
	}
	has_pareto = bool(doc.get("pareto_items"))
	if method in {"Five Why", "Fishbone", "Other"} and method not in root_methods:
		frappe.throw(f"PDCA analysis method {method} requires corresponding structured root cause rows")
	if method == "Pareto" and not has_pareto:
		frappe.throw("PDCA Pareto analysis requires validated Pareto rows")
	if method == "Combined":
		present = set(root_methods)
		if has_pareto:
			present.add("Pareto")
		if len(present) < 2:
			frappe.throw("Combined PDCA analysis requires at least two explicitly selected methods")
	active_measures = [
		row for row in doc.get("improvement_measures") or [] if row.get("status") != "Cancelled"
	]
	if not active_measures:
		frappe.throw("PDCA activation requires an accountable improvement measure plan")


def _require_completed_measures(doc) -> None:
	rows = [row for row in doc.get("improvement_measures") or [] if row.get("status") != "Cancelled"]
	if not rows or any(row.get("status") != "Completed" for row in rows):
		frappe.throw("PDCA measurement requires every non-cancelled improvement measure to be completed")


def _require_verified_effect_measurements(doc) -> None:
	rows = list(doc.get("effect_measurements") or [])
	if not rows:
		frappe.throw("PDCA Measuring and Closed states require at least one effect measurement")
	for row in rows:
		_validate_complete_effect_row(
			doc,
			row,
			require_verified=True,
			resolve_current_sources=False,
		)


def _require_standardization_record(doc) -> None:
	if (
		doc.get("standardization_decision") not in {"Adopted", "Not Adopted"}
		or not str(doc.get("standardization_conclusion") or "").strip()
		or not str(doc.get("standardization_evidence_reference") or "").strip()
		or not _HEX64.fullmatch(str(doc.get("standardization_evidence_hash") or ""))
		or not doc.get("standardized_by")
		or not doc.get("standardized_at")
	):
		frappe.throw("PDCA closure requires a complete immutable standardization conclusion and evidence")


def _assert_frozen_analysis_rows(doc, previous, table_field: str, payload) -> None:
	if not previous or str(previous.get("status") or "") not in {"Active", "Measuring", "Closed"}:
		return
	before = sorted(
		(payload(row) for row in previous.get(table_field) or []),
		key=lambda item: _canonical_json(item),
	)
	after = sorted(
		(payload(row) for row in doc.get(table_field) or []),
		key=lambda item: _canonical_json(item),
	)
	if before != after:
		frappe.throw(f"PDCA {table_field.replace('_', ' ')} are immutable after activation")


def _root_cause_payload(row) -> dict[str, Any]:
	return {
		"cause_code": row.get("cause_code"),
		"analysis_method": row.get("analysis_method"),
		"why_level": row.get("why_level"),
		"parent_cause_code": row.get("parent_cause_code"),
		"category": row.get("category"),
		"cause": row.get("cause"),
		"evidence": row.get("evidence"),
		"evidence_hash": row.get("evidence_hash"),
		"confirmed": int(row.get("confirmed") or 0),
	}


def _pareto_payload(row) -> dict[str, Any]:
	return {
		"cause_code": row.get("cause_code"),
		"category": row.get("category"),
		"cause": row.get("cause"),
		"occurrence_count": row.get("occurrence_count"),
		"rank": row.get("rank"),
		"cumulative_count": row.get("cumulative_count"),
		"cumulative_percent": _decimal_text(row.get("cumulative_percent")),
	}


def _measure_payload(row) -> dict[str, Any]:
	return {
		"measure_code": row.get("measure_code"),
		"measure_type": row.get("measure_type"),
		"measure": row.get("measure"),
		"owner_user": row.get("owner_user"),
		"planned_start": _canonical_value(row.get("planned_start")),
		"due_date": _canonical_value(row.get("due_date")),
		"status": row.get("status"),
		"completion_evidence": row.get("completion_evidence"),
		"completion_evidence_hash": row.get("completion_evidence_hash"),
		"cancellation_reason": row.get("cancellation_reason"),
		"standardization_candidate": int(row.get("standardization_candidate") or 0),
	}


def _effect_payload(row) -> dict[str, Any]:
	return {
		"measurement_code": row.get("measurement_code"),
		"indicator": row.get("indicator"),
		"baseline_source": row.get("baseline_source"),
		"actual_source": row.get("actual_source"),
		"baseline_period_start": _canonical_value(row.get("baseline_period_start")),
		"baseline_period_end": _canonical_value(row.get("baseline_period_end")),
		"measurement_period_start": _canonical_value(row.get("measurement_period_start")),
		"measurement_period_end": _canonical_value(row.get("measurement_period_end")),
		"baseline_value": _decimal_text(row.get("baseline_value")),
		"target_value": _decimal_text(row.get("target_value")),
		"actual_value": _decimal_text(row.get("actual_value")),
		"source_snapshot_hash": row.get("source_snapshot_hash"),
		"verified_by": row.get("verified_by"),
		"verified_at": _canonical_value(row.get("verified_at")),
		"verification_comment": row.get("verification_comment"),
		"measurement_hash": row.get("measurement_hash"),
	}


def _rows_by_code(parent, fieldname: str, code_field: str) -> dict[str, Any]:
	if not parent:
		return {}
	return {str(row.get(code_field) or ""): row for row in parent.get(fieldname) or [] if row.get(code_field)}


def _normalize_code_list(value: list[str] | str) -> tuple[str, ...]:
	if isinstance(value, str):
		try:
			parsed = json.loads(value)
		except TypeError, ValueError:
			frappe.throw("Measurement codes must be a JSON array")
	else:
		parsed = value
	if type(parsed) is not list or not parsed or len(parsed) > MAX_EFFECT_MEASUREMENTS:
		frappe.throw("Measurement codes must be a non-empty bounded array")
	codes = tuple(_validated_code(item, "Effect measurement code") for item in parsed)
	if len(set(codes)) != len(codes):
		frappe.throw("Measurement codes cannot contain duplicates")
	return codes


def _validated_code(value: Any, label: str) -> str:
	code = str(value or "")
	if code != code.strip() or not _CODE_PATTERN.fullmatch(code):
		frappe.throw(f"{label} must be 1-64 letters, digits, dots, underscores, or hyphens")
	return code


def _exact_int(value: Any, label: str, minimum: int, maximum: int) -> int:
	if type(value) is int:
		number = value
	elif type(value) is str and value and value == value.strip() and value.isascii() and value.isdigit():
		number = int(value)
	else:
		frappe.throw(f"{label} must be an exact integer")
	if number < minimum or number > maximum:
		frappe.throw(f"{label} must be between {minimum} and {maximum}")
	return number


def _decimal(value: Any, label: str) -> Decimal:
	if type(value) is bool or value in (None, ""):
		frappe.throw(f"{label} must be an exact finite number")
	try:
		number = Decimal(str(value))
	except InvalidOperation, ValueError:
		frappe.throw(f"{label} must be an exact finite number")
	if not number.is_finite():
		frappe.throw(f"{label} must be an exact finite number")
	return number


def _decimal_text(value: Any) -> str:
	number = _decimal(value, "Numeric value")
	text = format(number.normalize(), "f")
	return "0" if text in {"-0", ""} else text


def _is_blank(value: Any) -> bool:
	return value is None or value == ""


def _bounded_text(value: Any, label: str) -> str:
	text = str(value or "").strip()
	if not text or len(text) > MAX_GOVERNANCE_TEXT:
		frappe.throw(f"{label} is required and cannot exceed {MAX_GOVERNANCE_TEXT} characters")
	return text


def _pdca_lock(project: str) -> str:
	name = str(project or "").strip()
	if not name or len(name) > 140:
		frappe.throw("PDCA project identifier is invalid")
	return f"ione-qms:pdca-structure:{_hash_text(name)[:24]}"


def _require_named_pdca_reviewer() -> None:
	if frappe.session.user in {"", "Guest", "Administrator"}:
		frappe.throw("PDCA verification requires a named accountable reviewer", frappe.PermissionError)
	require_role("IONE QC Reviewer", "IONE Medical Affairs")
	if "IONE Agent Service" in set(frappe.get_roles(frappe.session.user)):
		frappe.throw("AI service users cannot verify PDCA outcomes", frappe.PermissionError)


def _canonical_value(value: Any) -> Any:
	return None if value in (None, "") else str(value)


def _same_optional(left: Any, right: Any) -> bool:
	return _canonical_value(left) == _canonical_value(right)


def _canonical_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)


def _hash_payload(value: Any) -> str:
	return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _hash_text(value: str) -> str:
	return hashlib.sha256(value.encode("utf-8")).hexdigest()


@contextmanager
def _controlled_effect_verification(project: str, measurement_codes: tuple[str, ...]):
	previous = getattr(frappe.flags, _EFFECT_VERIFICATION_FLAG, None)
	setattr(
		frappe.flags,
		_EFFECT_VERIFICATION_FLAG,
		{"project": project, "measurement_codes": measurement_codes},
	)
	try:
		yield
	finally:
		setattr(frappe.flags, _EFFECT_VERIFICATION_FLAG, previous)


@contextmanager
def _controlled_standardization(project: str):
	previous = getattr(frappe.flags, _STANDARDIZATION_FLAG, None)
	setattr(frappe.flags, _STANDARDIZATION_FLAG, {"project": project})
	try:
		yield
	finally:
		setattr(frappe.flags, _STANDARDIZATION_FLAG, previous)
