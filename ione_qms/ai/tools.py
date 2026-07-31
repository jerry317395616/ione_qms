from __future__ import annotations

import hashlib
import hmac
import json
import math
from typing import Annotated, Any

import frappe
from flow import tool

from ione_qms.ai.audit import record_tool_access
from ione_qms.ai.evidence import validate_evidence_references
from ione_qms.ai.governance import validate_policy_runtime
from ione_qms.ai.output_quarantine import flag_aggregate_output_violation
from ione_qms.ai.output_safety import aggregate_report_output_violation
from ione_qms.ai.privacy import (
	MEDICAL_RECORD_AI_PROFILE_VERSION,
	MEDICAL_RECORD_AI_SECTION_PATHS,
	AIPrivacyViolation,
	assert_model_output_privacy,
	deidentified_content_hash,
	deidentify_ai_value,
)
from ione_qms.ai.privacy_runtime import task_known_identifiers
from ione_qms.permissions import require_scope_read
from ione_qms.services.ai_report_schedules import validated_quality_report_snapshot_for_task
from ione_qms.services.indicators import (
	current_indicator_result_lock,
	indicator_artifact_is_governed_excluded,
	verify_indicator_result_detail_integrity,
)

MAX_TEXT_PER_SECTION = 4000
MAX_TOTAL_TEXT = 16000
MAX_RECTIFICATION_CONTEXT_BYTES = 128 * 1024
MAX_RECTIFICATION_EVIDENCE_ROWS = 100
MAX_RECTIFICATION_ROWS = 20
MAX_VERIFICATION_ROWS = 40
MAX_PDCA_ROWS = 20
MAX_RECTIFICATION_ACTION_ROWS = 200
MAX_PDCA_ROOT_CAUSE_ROWS = 100
MAX_PDCA_MEASURE_ROWS = 100
TASK_SCOPE_FIELDS = (
	"hospital",
	"campus",
	"department",
	"ward",
	"patient",
	"encounter",
	"responsible_staff",
	"indicator_result",
	"finding",
)
RECTIFICATION_REQUESTER_ROLES = frozenset(
	{
		"IONE Agent Reviewer",
		"IONE QC Reviewer",
		"IONE Medical Affairs",
		"IONE Department Director",
		"IONE Department QC Officer",
	}
)
RECTIFICATION_ELIGIBLE_FINDING_STATUSES = frozenset(
	{
		"Confirmed",
		"Appeal Rejected",
		"Pending Rectification",
		"Rectifying",
		"Pending Department Approval",
		"Pending Functional Review",
		"Rework",
		"Closed",
	}
)
FINDING_SEVERITIES = frozenset({"Low", "Medium", "High", "Critical"})
FINDING_EVIDENCE_TYPES = frozenset(
	{"Rule Evidence", "Source Snapshot", "Workflow Transition", "Review Note", "Other"}
)
RECTIFICATION_STATUSES = frozenset(
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
RECTIFICATION_ACTION_STATUSES = frozenset({"Pending", "In Progress", "Completed", "Cancelled"})
VERIFICATION_STATUSES = frozenset({"Draft", "Submitted", "Verified", "Rejected"})
VERIFICATION_RESULTS = frozenset({"", "Effective", "Partially Effective", "Ineffective"})
PDCA_STATUSES = frozenset({"Proposed", "Approved", "Active", "Measuring", "Closed", "Rejected", "Cancelled"})
PDCA_MEASURE_STATUSES = frozenset({"Planned", "In Progress", "Completed", "Cancelled"})


@tool
def ione_search_quality_standard(
	query: Annotated[str, "Search phrase for a published IONE quality standard or clause"],
	limit: Annotated[int, "Maximum records, from 1 to 20"] = 10,
) -> list[dict[str, Any]]:
	"""Search published quality standards and clauses without patient data."""
	_assert_authenticated()
	policy = _active_policy_for_tool("ione_search_quality_standard")
	task_name = str(getattr(frappe.flags, "ione_ai_task", None) or "")
	task_doc = _get_active_task(task_name)
	limit = min(max(int(limit), 1), 20)
	escaped = f"%{query.strip()}%"
	results = frappe.get_all(
		"IONE QC Standard Clause",
		filters={"content": ["like", escaped], "status": "Published"},
		fields=["name", "standard_version", "clause_code", "heading", "content", "modified"],
		limit_page_length=limit,
	)
	output: list[dict[str, Any]] = []
	request = {"query_hash": _stable_hash(query), "limit": limit}
	for row in results:
		snapshot = {
			"standard_version": row.get("standard_version"),
			"clause_code": row.get("clause_code"),
			"heading": row.get("heading"),
			"content": row.get("content"),
		}
		reference = record_tool_access(
			tool_slug="ione_search_quality_standard",
			task=task_name,
			policy=policy.name,
			purpose="Quality standard search",
			accessed_fields=list(snapshot),
			result="Success",
			hospital=task_doc.get("hospital"),
			campus=task_doc.get("campus"),
			department=task_doc.get("department"),
			ward=task_doc.get("ward"),
			patient=task_doc.get("patient"),
			encounter=task_doc.get("encounter"),
			request=request,
			source_doctype="IONE QC Standard Clause",
			source_name=row.name,
			source_version=row.get("modified"),
			snapshot=snapshot,
		)
		output.append({**snapshot, "evidence_reference": reference})
	if not output:
		record_tool_access(
			tool_slug="ione_search_quality_standard",
			task=task_name,
			policy=policy.name,
			purpose="Quality standard search",
			accessed_fields=[],
			result="Success",
			request=request,
		)
	return output


@tool
def ione_get_encounter_context(
	task: Annotated[str, "Governed IONE AI Analysis Task"],
	encounter: Annotated[str | None, "Optional encounter; defaults to the task scope"] = None,
) -> dict[str, Any]:
	"""Read the minimum authorized quality context for a single encounter."""
	_assert_authenticated()
	task_doc = _get_active_task(task)
	encounter = _task_reference(task_doc, "encounter", encounter)
	doc = frappe.get_doc("IONE Encounter Index", encounter)
	_require_requester_read(task_doc, doc)
	_assert_scope_exact(
		task_doc,
		source_label="encounter",
		hospital=doc.get("hospital"),
		campus=doc.get("campus"),
		department=doc.get("department"),
		ward=doc.get("ward"),
		patient=doc.get("patient"),
		encounter=doc.name,
		responsible_staff=doc.get("responsible_staff"),
	)
	data = {
		"encounter_reference": _stable_hash(doc.name),
		"encounter_type": doc.get("encounter_type"),
		"status": doc.get("status"),
		"hospital": doc.get("hospital"),
		"campus": doc.get("campus"),
		"department": doc.get("department"),
		"ward": doc.get("ward"),
		"admission_time": doc.get("admission_time"),
		"discharge_time": doc.get("discharge_time"),
		"responsible_staff_reference": _hashed_reference(doc.get("responsible_staff")),
	}
	reference = record_tool_access(
		tool_slug="ione_get_encounter_context",
		task=task,
		policy=_task_policy(task),
		purpose="Encounter quality analysis",
		accessed_fields=list(data),
		result="Success",
		hospital=doc.get("hospital"),
		campus=doc.get("campus"),
		department=doc.get("department"),
		ward=doc.get("ward"),
		patient=doc.get("patient"),
		encounter=doc.name,
		request={"encounter": encounter},
		source_doctype="IONE Encounter Index",
		source_name=doc.name,
		source_version=doc.get("last_synced_at") or doc.get("modified"),
		source_record_hash=doc.get("encounter_key"),
		snapshot=data,
	)
	data["evidence_reference"] = reference
	return data


@tool
def ione_get_medical_record_sections(
	task: Annotated[str, "Governed IONE AI Analysis Task"],
	sections: Annotated[list[str], "Explicit payload section names to retrieve"],
	encounter: Annotated[str | None, "Optional encounter; defaults to the task scope"] = None,
) -> dict[str, Any]:
	"""Read bounded medical-record sections already received through approved integrations."""
	_assert_authenticated()
	task_doc = _get_active_task(task)
	encounter = _task_reference(task_doc, "encounter", encounter)
	encounter_doc = frappe.get_doc("IONE Encounter Index", encounter)
	_require_requester_read(task_doc, encounter_doc)
	_assert_scope_exact(
		task_doc,
		source_label="encounter",
		hospital=encounter_doc.get("hospital"),
		campus=encounter_doc.get("campus"),
		department=encounter_doc.get("department"),
		ward=encounter_doc.get("ward"),
		patient=encounter_doc.get("patient"),
		encounter=encounter_doc.name,
		responsible_staff=encounter_doc.get("responsible_staff"),
	)
	requested = [str(item).strip() for item in sections if str(item).strip()]
	if (
		not requested
		or len(requested) > 20
		or len(set(requested)) != len(requested)
		or not set(requested).issubset(MEDICAL_RECORD_AI_SECTION_PATHS)
	):
		frappe.throw(
			"Medical-record sections must be unique paths from the release-approved "
			f"{MEDICAL_RECORD_AI_PROFILE_VERSION} profile."
		)
	known_identifiers = task_known_identifiers(task_doc)
	event_filters = {"encounter_index": encounter}
	event_filters.update(
		_scope_query_filters(
			task_doc,
			{
				"hospital": "hospital",
				"campus": "campus",
				"department": "department",
				"ward": "ward",
				"patient": "patient_index",
				"responsible_staff": "responsible_staff",
			},
		)
	)
	events = frappe.get_all(
		"IONE Clinical Quality Event",
		filters=event_filters,
		fields=[
			"name",
			"hospital",
			"campus",
			"department",
			"ward",
			"patient_index",
			"encounter_index",
			"responsible_staff",
			"payload_json",
			"source_version",
			"payload_reference",
			"idempotency_key",
			"modified",
		],
		order_by="event_time desc",
		limit_page_length=20,
	)
	output: dict[str, Any] = {}
	event_manifests: dict[str, dict[str, str]] = {}
	event_rows: dict[str, Any] = {}
	total = 0
	for event in events:
		_assert_scope_exact(
			task_doc,
			source_label="clinical event",
			hospital=event.get("hospital"),
			campus=event.get("campus"),
			department=event.get("department"),
			ward=event.get("ward"),
			patient=event.get("patient_index"),
			encounter=event.get("encounter_index"),
			responsible_staff=event.get("responsible_staff"),
		)
		payload = _json_dict(event.payload_json)
		for section in requested:
			if section in output or section not in payload:
				continue
			try:
				value = deidentify_ai_value(
					payload[section],
					known_identifiers=known_identifiers,
				)
			except AIPrivacyViolation as exc:
				flag_aggregate_output_violation(task, exc.code)
				frappe.throw(
					f"Medical-record AI input failed the de-identification gate. code={exc.code}",
					frappe.ValidationError,
				)
			text = json.dumps(
				value,
				ensure_ascii=False,
				sort_keys=True,
				separators=(",", ":"),
				default=str,
			)
			if len(text) > MAX_TEXT_PER_SECTION or total + len(text) > MAX_TOTAL_TEXT:
				continue
			output[section] = value
			event_manifests.setdefault(event.name, {})[section] = deidentified_content_hash(value)
			event_rows[event.name] = event
			total += len(text)
	request = {
		"encounter_reference": _stable_hash(encounter, 32),
		"profile_version": MEDICAL_RECORD_AI_PROFILE_VERSION,
		"sections": requested,
	}
	references: list[str] = []
	for event_name, section_hashes in event_manifests.items():
		event = event_rows[event_name]
		references.append(
			record_tool_access(
				tool_slug="ione_get_medical_record_sections",
				task=task,
				policy=_task_policy(task),
				purpose="Medical-record semantic quality analysis",
				accessed_fields=[f"payload_json.{fieldname}" for fieldname in sorted(section_hashes)],
				result="Success",
				hospital=encounter_doc.get("hospital"),
				campus=encounter_doc.get("campus"),
				department=encounter_doc.get("department"),
				ward=encounter_doc.get("ward"),
				patient=encounter_doc.get("patient"),
				encounter=encounter,
				request=request,
				source_doctype="IONE Clinical Quality Event",
				source_name=event_name,
				source_version=event.get("source_version") or event.get("modified"),
				source_record_hash=event.get("payload_reference") or event.get("idempotency_key"),
				snapshot={
					"contract": "ione-qms-deidentified-content-manifest-v1",
					"profile_version": MEDICAL_RECORD_AI_PROFILE_VERSION,
					"section_hashes": dict(sorted(section_hashes.items())),
				},
			)
		)
	if not references:
		record_tool_access(
			tool_slug="ione_get_medical_record_sections",
			task=task,
			policy=_task_policy(task),
			purpose="Medical-record semantic quality analysis",
			accessed_fields=[],
			result="Success",
			hospital=encounter_doc.get("hospital"),
			campus=encounter_doc.get("campus"),
			department=encounter_doc.get("department"),
			ward=encounter_doc.get("ward"),
			patient=encounter_doc.get("patient"),
			encounter=encounter,
			request=request,
		)
	return {
		"profile_version": MEDICAL_RECORD_AI_PROFILE_VERSION,
		"sections": output,
		"evidence_references": references,
	}


@tool
def ione_get_indicator_trend(
	indicator_code: Annotated[str, "IONE quality indicator code"],
	task: Annotated[str, "Governed IONE AI Analysis Task"],
	department: Annotated[str | None, "Optional department scope"] = None,
	periods: Annotated[int, "Number of periods, from 1 to 24"] = 6,
) -> dict[str, Any]:
	"""Read an authorized indicator trend."""
	_assert_authenticated()
	task_doc = _get_active_task(task)
	department = _task_reference(task_doc, "department", department, required=False)
	require_scope_read(
		hospital=task_doc.get("hospital"),
		campus=task_doc.get("campus"),
		department=department,
		ward=task_doc.get("ward"),
		user=task_doc.requested_by,
	)
	indicator = frappe.db.get_value(
		"IONE QC Indicator",
		{"indicator_code": indicator_code},
		"name",
	)
	if not indicator:
		frappe.throw("Indicator not found", frappe.DoesNotExistError)
	versions = frappe.get_all(
		"IONE QC Indicator Version",
		filters={"indicator": indicator},
		pluck="name",
		limit_page_length=100,
	)
	pointer_filters: dict[str, Any] = {
		"indicator_version": ["in", versions or ["__none__"]],
	}
	pointer_filters.update(
		_scope_query_filters(
			task_doc,
			{
				"hospital": "hospital",
				"campus": "campus",
				"department": "department",
				"ward": "ward",
				"indicator_result": "current_result",
			},
		)
	)
	if department:
		pointer_filters["department"] = department
	pointers = frappe.get_all(
		"IONE Indicator Result Pointer",
		filters=pointer_filters,
		fields=["current_result"],
		order_by="period_end desc",
		limit_page_length=min(max(int(periods), 1), 24),
	)
	current_results = [row.current_result for row in pointers if row.get("current_result")]
	detail_scope_map = {
		"hospital": "hospital",
		"campus": "campus",
		"department": "department",
		"ward": "ward",
		"patient": "patient",
		"encounter": "encounter",
		"responsible_staff": "responsible_staff",
		"indicator_result": "indicator_result",
	}
	if any(task_doc.get(fieldname) for fieldname in ("patient", "encounter", "responsible_staff")):
		detail_filters = _scope_query_filters(task_doc, detail_scope_map)
		detail_filters["indicator_result"] = ["in", current_results or ["__none__"]]
		eligible_results = frappe.get_all(
			"IONE Indicator Result Detail",
			filters=detail_filters,
			pluck="indicator_result",
			limit_page_length=1000,
		)
		if task_doc.get("indicator_result"):
			eligible_results = [name for name in eligible_results if name == task_doc.get("indicator_result")]
		current_results = list(dict.fromkeys(eligible_results))
	rows = frappe.get_all(
		"IONE Indicator Result",
		filters={"name": ["in", current_results or ["__none__"]]},
		fields=[
			"name",
			"indicator_version",
			"period_start",
			"period_end",
			"department",
			"numerator",
			"denominator",
			"indicator_value",
			"target_value",
			"status",
			"computed_at",
			"hospital",
			"campus",
			"ward",
			"result_key",
			"dimension_hash",
			"calculator_version",
			"lineage_json",
			"modified",
		],
		order_by="period_end desc",
		limit_page_length=min(max(int(periods), 1), 24),
	)
	output_rows: list[dict[str, Any]] = []
	request = {"indicator_code": indicator_code, "department": department, "periods": periods}
	for row in reversed(rows):
		with current_indicator_result_lock(row.name) as (_pointer, current_result):
			row = current_result
		_assert_scope_exact(
			task_doc,
			source_label="indicator result",
			hospital=row.get("hospital"),
			campus=row.get("campus"),
			department=row.get("department"),
			ward=row.get("ward"),
			indicator_result=row.name,
		)
		snapshot = {
			"indicator_version": row.get("indicator_version"),
			"period_start": row.get("period_start"),
			"period_end": row.get("period_end"),
			"department": row.get("department"),
			"numerator": row.get("numerator"),
			"denominator": row.get("denominator"),
			"indicator_value": row.get("indicator_value"),
			"target_value": row.get("target_value"),
			"status": row.get("status"),
			"computed_at": row.get("computed_at"),
			"lineage": _json_value(row.get("lineage_json")),
		}
		reference = record_tool_access(
			tool_slug="ione_get_indicator_trend",
			task=task,
			policy=task_doc.policy,
			purpose="Indicator trend analysis",
			accessed_fields=list(snapshot),
			result="Success",
			hospital=row.get("hospital"),
			campus=row.get("campus"),
			department=row.get("department"),
			ward=row.get("ward"),
			request=request,
			source_doctype="IONE Indicator Result",
			source_name=row.name,
			source_version=row.get("modified"),
			source_record_hash=row.get("result_key") or row.get("dimension_hash"),
			snapshot=snapshot,
		)
		public_snapshot = {key: value for key, value in snapshot.items() if key != "lineage"}
		output_rows.append({**public_snapshot, "evidence_reference": reference})
	result = {
		"indicator": indicator,
		"indicator_code": indicator_code,
		"results": output_rows,
	}
	if not output_rows:
		record_tool_access(
			tool_slug="ione_get_indicator_trend",
			task=task,
			policy=task_doc.policy,
			purpose="Indicator trend analysis",
			accessed_fields=[],
			result="Success",
			hospital=task_doc.get("hospital"),
			campus=task_doc.get("campus"),
			department=department,
			ward=task_doc.get("ward"),
			request=request,
		)
	return result


@tool
def ione_get_quality_summary(
	task: Annotated[str, "Governed scheduled IONE Quality Report task"],
) -> dict[str, Any]:
	"""Read the one immutable, aggregate-only snapshot bound to a scheduled report task."""
	_assert_authenticated()
	task_doc = _get_active_task(task)
	policy = _active_policy_for_tool("ione_get_quality_summary")
	snapshot, summary = validated_quality_report_snapshot_for_task(task_doc)
	_require_requester_read(task_doc, snapshot)
	_assert_scope_exact(
		task_doc,
		source_label="quality report snapshot",
		hospital=snapshot.get("hospital"),
		campus=snapshot.get("campus"),
		department=snapshot.get("department"),
		ward=snapshot.get("ward"),
	)
	reference = record_tool_access(
		tool_slug="ione_get_quality_summary",
		task=task,
		policy=policy.name,
		purpose="Approved scheduled quality-report summary",
		accessed_fields=sorted(summary),
		result="Success",
		hospital=snapshot.get("hospital"),
		campus=snapshot.get("campus"),
		department=snapshot.get("department"),
		ward=snapshot.get("ward"),
		request={
			"snapshot": snapshot.name,
			"data_hash": snapshot.get("data_hash"),
			"period_start": snapshot.get("period_start"),
			"period_end": snapshot.get("period_end"),
		},
		source_doctype="IONE Quality Report Snapshot",
		source_name=snapshot.name,
		source_version=(f"{snapshot.get('source_cutoff') or ''}|{snapshot.get('generated_at') or ''}"),
		source_record_hash=snapshot.get("data_hash"),
		snapshot=summary,
	)
	return {"summary": summary, "evidence_reference": reference}


@tool
def ione_get_rectification_context(
	task: Annotated[str, "Governed IONE Rectification task bound to exactly one finding"],
) -> dict[str, Any]:
	"""Read bounded, structured context for one confirmed finding without clinical free text."""
	_assert_authenticated()
	task_doc = _get_active_task(task)
	policy = _active_policy_for_tool("ione_get_rectification_context")
	finding = _validated_rectification_finding(task_doc)

	evidence_docs = _bounded_related_documents(
		task_doc,
		finding,
		"IONE QC Finding Evidence",
		MAX_RECTIFICATION_EVIDENCE_ROWS,
	)
	rectification_docs = _bounded_related_documents(
		task_doc,
		finding,
		"IONE QC Rectification",
		MAX_RECTIFICATION_ROWS,
	)
	verification_docs = _bounded_related_documents(
		task_doc,
		finding,
		"IONE QC Verification",
		MAX_VERIFICATION_ROWS,
	)
	pdca_docs = _bounded_related_documents(
		task_doc,
		finding,
		"IONE PDCA Project",
		MAX_PDCA_ROWS,
	)
	rectification_names = {doc.name for doc in rectification_docs}
	for verification in verification_docs:
		if str(verification.get("rectification") or "") not in rectification_names:
			frappe.throw(
				"A finding verification is not bound to a returned rectification.",
				frappe.PermissionError,
			)

	request = {
		"finding_reference": _stable_hash(finding.name, 32),
		"task_type": "Rectification",
	}
	evidence = [
		_receipted_rectification_snapshot(
			task_doc,
			policy.name,
			finding,
			doc,
			_build_finding_evidence_snapshot(doc),
			request=request,
			purpose="Confirmed finding minimum evidence",
		)
		for doc in evidence_docs
	]
	rectifications = [
		_receipted_rectification_snapshot(
			task_doc,
			policy.name,
			finding,
			doc,
			_build_rectification_snapshot(doc),
			request=request,
			purpose="Current rectification state",
		)
		for doc in rectification_docs
	]
	verifications = [
		_receipted_rectification_snapshot(
			task_doc,
			policy.name,
			finding,
			doc,
			_build_verification_snapshot(doc),
			request=request,
			purpose="Current rectification verification state",
		)
		for doc in verification_docs
	]
	pdca_projects = [
		_receipted_rectification_snapshot(
			task_doc,
			policy.name,
			finding,
			doc,
			_build_pdca_snapshot(doc),
			request=request,
			purpose="Current finding-linked PDCA state",
		)
		for doc in pdca_docs
	]

	finding_snapshot = _build_confirmed_finding_snapshot(
		finding,
		evidence_count=len(evidence),
		rectification_count=len(rectifications),
		verification_count=len(verifications),
		pdca_count=len(pdca_projects),
	)
	finding_output = _receipted_rectification_snapshot(
		task_doc,
		policy.name,
		finding,
		finding,
		finding_snapshot,
		request=request,
		purpose="Confirmed finding rectification anchor",
	)
	result = {
		"finding": finding_output,
		"minimum_evidence": {
			"total": len(evidence),
			"records": evidence,
		},
		"rectification": _rectification_aggregate(rectifications),
		"verification": _rectification_aggregate(verifications),
		"pdca": _rectification_aggregate(pdca_projects),
		"historical_cases": {
			"available": False,
			"reason_code": "NO_APPROVED_DEIDENTIFIED_RECTIFICATION_CASE_MODEL",
			"records": [],
		},
		"output_contract": {
			"draft_only": True,
			"patient_identifiers_returned": False,
			"clinical_free_text_returned": False,
		},
	}
	if len(json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")).encode()) > (
		MAX_RECTIFICATION_CONTEXT_BYTES
	):
		frappe.throw("Rectification context exceeds the governed 128 KiB response limit.")
	return result


@tool(requires_confirmation=True)
def ione_get_contributing_cases(
	task: Annotated[str, "Governed IONE AI Analysis Task"],
	indicator_result: Annotated[
		str | None,
		"Optional indicator result; defaults to the task scope",
	] = None,
	limit: Annotated[int, "Maximum de-identified cases, from 1 to 20"] = 10,
) -> list[dict[str, Any]]:
	"""Read a bounded, de-identified set of cases contributing to an indicator result."""
	_assert_authenticated()
	task_doc = _get_active_task(task)
	indicator_result = _task_reference(task_doc, "indicator_result", indicator_result)
	with current_indicator_result_lock(indicator_result) as (_pointer, current_result):
		result_doc = current_result
	_require_requester_read(task_doc, result_doc)
	_assert_scope_exact(
		task_doc,
		source_label="indicator result",
		hospital=result_doc.get("hospital"),
		campus=result_doc.get("campus"),
		department=result_doc.get("department"),
		ward=result_doc.get("ward"),
		indicator_result=result_doc.name,
	)
	limit = min(max(int(limit), 1), 20)
	case_filters: dict[str, Any] = {
		"indicator_result": indicator_result,
		**_scope_query_filters(
			task_doc,
			{
				"hospital": "hospital",
				"campus": "campus",
				"department": "department",
				"ward": "ward",
				"patient": "patient",
				"encounter": "encounter",
				"responsible_staff": "responsible_staff",
			},
		),
	}
	detail_fields = [
		"name",
		"hospital",
		"campus",
		"department",
		"ward",
		"patient",
		"encounter",
		"responsible_staff",
		"contribution_value",
		"reason_code",
		"source_reference_hash",
		"detail_key",
		"lineage_json",
		"modified",
	]
	rows = _verified_current_indicator_details(
		indicator_result,
		filters=case_filters,
		fields=detail_fields,
		limit=limit,
	)
	request = {"indicator_result": indicator_result, "limit": limit}
	output_rows = []
	for row in rows:
		_assert_scope_exact(
			task_doc,
			source_label="indicator contribution",
			hospital=row.get("hospital"),
			campus=row.get("campus"),
			department=row.get("department"),
			ward=row.get("ward"),
			patient=row.get("patient"),
			encounter=row.get("encounter"),
			responsible_staff=row.get("responsible_staff"),
			indicator_result=indicator_result,
		)
		public_row = {
			"encounter": _stable_hash(row["encounter"]) if row.get("encounter") else None,
			"contribution_value": row.get("contribution_value"),
			"reason_code": row.get("reason_code"),
			"source_reference_hash": row.get("source_reference_hash"),
		}
		snapshot = {
			**public_row,
			"indicator_result": indicator_result,
			"lineage": _json_value(row.get("lineage_json")),
		}
		reference = record_tool_access(
			tool_slug="ione_get_contributing_cases",
			task=task,
			policy=_task_policy(task),
			purpose="Indicator contribution analysis",
			accessed_fields=list(snapshot),
			result="Success",
			hospital=row.get("hospital") or result_doc.get("hospital"),
			campus=row.get("campus") or result_doc.get("campus"),
			department=row.get("department") or result_doc.get("department"),
			ward=row.get("ward") or result_doc.get("ward"),
			patient=row.get("patient"),
			encounter=row.get("encounter"),
			request=request,
			source_doctype="IONE Indicator Result Detail",
			source_name=row.name,
			source_version=row.get("modified"),
			source_record_hash=row.get("detail_key") or row.get("source_reference_hash"),
			snapshot=snapshot,
		)
		output_rows.append({**public_row, "evidence_reference": reference})
	if not output_rows:
		record_tool_access(
			tool_slug="ione_get_contributing_cases",
			task=task,
			policy=_task_policy(task),
			purpose="Indicator contribution analysis",
			accessed_fields=[],
			result="Success",
			hospital=result_doc.get("hospital"),
			campus=result_doc.get("campus"),
			department=result_doc.get("department"),
			ward=result_doc.get("ward"),
			request=request,
		)
	return output_rows


@tool(requires_confirmation=True)
def ione_create_analysis_draft(
	task: Annotated[str, "Governed IONE AI Analysis Task"],
	content: Annotated[str, "Analysis draft text"],
	citations: Annotated[list[str], "Traceable IONE evidence or result references"],
) -> dict[str, str]:
	"""Create a reviewable analysis draft; never creates a formal quality finding."""
	_assert_authenticated()
	task_doc = _assert_writable_task(task)
	_assert_safe_model_output(task_doc, content)
	validated_references = validate_evidence_references(task_doc, citations)
	execution_attempt, flow_run = _report_draft_execution_provenance(task_doc)
	doc = frappe.get_doc(
		{
			"doctype": "IONE AI Report Draft",
			"task": task,
			"execution_attempt": execution_attempt,
			"flow_run": flow_run,
			"report_type": "Analysis",
			"hospital": task_doc.get("hospital"),
			"campus": task_doc.get("campus"),
			"department": task_doc.get("department"),
			"content": content[:100000],
			"source_references": _reference_json(validated_references),
			"status": "Draft",
		}
	)
	doc.insert(ignore_permissions=True)
	return {"draft": doc.name, "status": doc.status}


@tool(requires_confirmation=True)
def ione_create_candidate_finding(
	task: Annotated[str, "Governed IONE AI Analysis Task"],
	title: Annotated[str, "Candidate quality issue title"],
	rationale: Annotated[str, "Reasoned explanation tied to evidence"],
	severity: Annotated[str, "Low, Medium, High, or Critical"],
	evidence_references: Annotated[list[str], "Traceable evidence references"],
) -> dict[str, str]:
	"""Create a candidate issue for mandatory human review, never a formal finding."""
	_assert_authenticated()
	task_doc = _assert_writable_task(task)
	_assert_safe_model_output(task_doc, title, rationale)
	if severity not in {"Low", "Medium", "High", "Critical"}:
		frappe.throw("Invalid candidate severity")
	validated_references = validate_evidence_references(
		task_doc,
		evidence_references,
		require_clinical=True,
	)
	doc = frappe.get_doc(
		{
			"doctype": "IONE AI Candidate Finding",
			"task": task,
			"hospital": task_doc.get("hospital"),
			"campus": task_doc.get("campus"),
			"department": task_doc.get("department"),
			"ward": task_doc.get("ward"),
			"patient": task_doc.get("patient"),
			"encounter": task_doc.get("encounter"),
			"title": title[:300],
			"rationale": rationale[:10000],
			"severity": severity,
			"evidence_references": _reference_json(validated_references),
			"status": "Pending Review",
		}
	)
	doc.insert(ignore_permissions=True)
	return {"candidate": doc.name, "status": doc.status}


@tool
def ione_create_report_draft(
	task: Annotated[str, "Governed IONE AI Analysis Task"],
	report_type: Annotated[str, "Department, hospital, meeting, or special-topic report"],
	content: Annotated[str, "Report draft text"],
	source_references: Annotated[list[str], "Traceable indicator, finding, and PDCA references"],
) -> dict[str, str]:
	"""Create one reviewable draft from a pre-approved scheduled aggregate snapshot."""
	_assert_authenticated()
	task_doc = _assert_writable_task(task)
	_active_policy_for_tool("ione_create_report_draft")
	if (
		str(task_doc.get("task_type") or "") != "Quality Report"
		or str(task_doc.get("origin") or "") != "Scheduled"
		or not int(task_doc.get("requires_human_review") or 0)
		or any(
			task_doc.get(fieldname)
			for fieldname in ("patient", "encounter", "responsible_staff", "indicator_result", "finding")
		)
	):
		frappe.throw(
			"Automatic report drafts require a non-patient scheduled Quality Report task "
			"with mandatory human review.",
			frappe.PermissionError,
		)
	if not task_doc.get("report_reviewer") or str(task_doc.get("report_reviewer")) == str(
		task_doc.get("requested_by") or ""
	):
		frappe.throw("Scheduled report draft reviewer provenance is invalid.")
	snapshot, _summary = validated_quality_report_snapshot_for_task(task_doc)
	_require_requester_read(task_doc, snapshot)
	expected_report_type = str(
		frappe.db.get_value(
			"IONE AI Report Schedule",
			task_doc.get("report_schedule"),
			"report_type",
		)
		or ""
	)
	normalized_report_type = str(report_type or "").strip()
	normalized_content = str(content or "").strip()
	if (
		not normalized_report_type
		or len(normalized_report_type) > 100
		or normalized_report_type != expected_report_type
	):
		frappe.throw("Report draft type must exactly match the approved schedule.")
	if not normalized_content or len(normalized_content) > 200_000:
		frappe.throw("Report draft content must contain 1-200,000 characters.")
	_assert_safe_model_output(task_doc, normalized_report_type, normalized_content)
	output_violation = aggregate_report_output_violation(normalized_content)
	if output_violation:
		flag_aggregate_output_violation(task, output_violation)
		frappe.throw(
			f"Report draft output failed the aggregate-only safety gate. code={output_violation}",
			frappe.ValidationError,
		)
	validated_references = validate_evidence_references(task_doc, source_references)
	if not validated_references or any(
		reference.source_doctype != "IONE Quality Report Snapshot"
		or reference.source_name != snapshot.name
		or not hmac.compare_digest(reference.source_record_hash, str(snapshot.get("data_hash") or ""))
		or not hmac.compare_digest(reference.content_hash, str(snapshot.get("data_hash") or ""))
		for reference in validated_references
	):
		frappe.throw(
			"Scheduled report drafts may cite only the exact task-bound quality summary snapshot.",
			frappe.PermissionError,
		)
	with frappe.db.advisory_lock(f"ione-qms:ai-report-draft:{task}", timeout=10):
		existing = frappe.db.get_value(
			"IONE AI Report Draft",
			{"task": task},
			["name", "status"],
			as_dict=True,
		)
		if existing:
			return {
				"draft": str(existing.get("name")),
				"status": str(existing.get("status")),
			}
		execution_attempt, flow_run = _report_draft_execution_provenance(task_doc)
		doc = frappe.get_doc(
			{
				"doctype": "IONE AI Report Draft",
				"task": task,
				"execution_attempt": execution_attempt,
				"flow_run": flow_run,
				"report_type": normalized_report_type,
				"hospital": task_doc.get("hospital"),
				"campus": task_doc.get("campus"),
				"department": task_doc.get("department"),
				"ward": task_doc.get("ward"),
				"content": normalized_content,
				"source_references": _reference_json(validated_references),
				"status": "Draft",
			}
		)
		doc.insert(ignore_permissions=True)
		return {"draft": doc.name, "status": doc.status}


def _validated_rectification_finding(task_doc):
	if (
		str(task_doc.get("task_type") or "") != "Rectification"
		or not task_doc.get("finding")
		or int(task_doc.get("record_count") or 0) != 1
		or not int(task_doc.get("requires_human_review") or 0)
	):
		frappe.throw(
			"Rectification context requires one human-reviewed Rectification task "
			"bound to exactly one finding.",
			frappe.PermissionError,
		)
	requested_by = str(task_doc.get("requested_by") or "")
	if (
		requested_by in {"", "Guest", "Administrator"}
		or not int(frappe.db.get_value("User", requested_by, "enabled") or 0)
		or not RECTIFICATION_REQUESTER_ROLES.intersection(frappe.get_roles(requested_by))
	):
		frappe.throw(
			"The original Rectification task requester is no longer eligible.",
			frappe.PermissionError,
		)
	require_scope_read(
		hospital=task_doc.get("hospital"),
		campus=task_doc.get("campus"),
		department=task_doc.get("department"),
		ward=task_doc.get("ward"),
		user=requested_by,
		target_doctype="IONE QC Finding",
	)
	finding = frappe.get_doc("IONE QC Finding", task_doc.finding)
	_require_requester_read(task_doc, finding)
	_assert_scope_exact(
		task_doc,
		source_label="rectification finding",
		hospital=finding.get("hospital"),
		campus=finding.get("campus"),
		department=finding.get("department"),
		ward=finding.get("ward"),
		patient=finding.get("patient"),
		encounter=finding.get("encounter"),
		responsible_staff=finding.get("responsible_staff"),
		finding=finding.name,
	)
	_safe_enum(
		finding.get("status"),
		RECTIFICATION_ELIGIBLE_FINDING_STATUSES,
		"Finding status",
	)
	_safe_enum(finding.get("severity"), FINDING_SEVERITIES, "Finding severity")
	return finding


def _bounded_related_documents(
	task_doc,
	finding,
	doctype: str,
	limit: int,
) -> list[Any]:
	names = frappe.get_all(
		doctype,
		filters={"finding": finding.name},
		pluck="name",
		order_by="modified desc",
		limit_page_length=limit + 1,
	)
	if len(names) > limit:
		frappe.throw(f"{doctype} exceeds the governed {limit}-row Rectification context limit.")
	documents = []
	for name in names:
		doc = frappe.get_doc(doctype, name)
		_require_requester_read(task_doc, doc)
		if str(doc.get("finding") or "") != str(finding.name):
			frappe.throw(
				f"{doctype} is not bound to the task finding.",
				frappe.PermissionError,
			)
		source_scope: dict[str, str | None] = {"finding": doc.get("finding")}
		for fieldname in ("hospital", "campus", "department", "ward"):
			if doc.meta.has_field(fieldname):
				source_scope[fieldname] = doc.get(fieldname)
		_assert_scope_exact(task_doc, source_label=doctype, **source_scope)
		documents.append(doc)
	return documents


def _build_confirmed_finding_snapshot(
	finding,
	*,
	evidence_count: int,
	rectification_count: int,
	verification_count: int,
	pdca_count: int,
) -> dict[str, Any]:
	return {
		"finding_reference": _stable_hash(finding.name, 32),
		"status": _safe_enum(
			finding.get("status"),
			RECTIFICATION_ELIGIBLE_FINDING_STATUSES,
			"Finding status",
		),
		"severity": _safe_enum(finding.get("severity"), FINDING_SEVERITIES, "Finding severity"),
		"detected_at": finding.get("detected_at"),
		"due_date": finding.get("due_date"),
		"rule_reference": _hashed_reference(finding.get("rule")),
		"rule_version_reference": _hashed_reference(finding.get("rule_version")),
		"standard_reference": _hashed_reference(finding.get("standard")),
		"standard_clause_reference": _hashed_reference(finding.get("standard_clause")),
		"evidence_hash": _optional_sha256(finding.get("evidence_hash"), "Finding evidence hash"),
		"evidence_count": evidence_count,
		"rectification_count": rectification_count,
		"verification_count": verification_count,
		"pdca_count": pdca_count,
	}


def _build_finding_evidence_snapshot(doc) -> dict[str, Any]:
	return {
		"evidence_reference_hash": _stable_hash(doc.name, 32),
		"evidence_type": _safe_enum(
			doc.get("evidence_type"),
			FINDING_EVIDENCE_TYPES,
			"Finding evidence type",
		),
		"matched": bool(int(doc.get("matched") or 0)),
		"captured_at": doc.get("captured_at"),
		"recorded_at": doc.get("recorded_at"),
		"rule_version_reference": _hashed_reference(doc.get("rule_version")),
		"standard_clause_reference": _hashed_reference(doc.get("standard_clause")),
		"evidence_hash": _required_sha256(doc.get("evidence_hash"), "Finding evidence hash"),
	}


def _build_rectification_snapshot(doc) -> dict[str, Any]:
	action_counts = _bounded_child_status_counts(
		doc.get("actions") or [],
		allowed=RECTIFICATION_ACTION_STATUSES,
		limit=MAX_RECTIFICATION_ACTION_ROWS,
		label="Rectification actions",
	)
	return {
		"rectification_reference": _stable_hash(doc.name, 32),
		"status": _safe_enum(doc.get("status"), RECTIFICATION_STATUSES, "Rectification status"),
		"due_date": doc.get("due_date"),
		"submitted_at": doc.get("submitted_at"),
		"department_approved": bool(doc.get("department_approved_at")),
		"functional_reviewed": bool(doc.get("functional_reviewed_at")),
		"action_count": sum(action_counts.values()),
		"action_status_counts": action_counts,
	}


def _build_verification_snapshot(doc) -> dict[str, Any]:
	return {
		"verification_reference": _stable_hash(doc.name, 32),
		"rectification_reference": _hashed_reference(doc.get("rectification")),
		"status": _safe_enum(doc.get("status"), VERIFICATION_STATUSES, "Verification status"),
		"verification_result": _safe_enum(
			doc.get("verification_result") or "",
			VERIFICATION_RESULTS,
			"Verification result",
		),
		"verified_at": doc.get("verified_at"),
	}


def _build_pdca_snapshot(doc) -> dict[str, Any]:
	root_causes = list(doc.get("root_causes") or [])
	if len(root_causes) > MAX_PDCA_ROOT_CAUSE_ROWS:
		frappe.throw(f"PDCA root causes exceed the governed {MAX_PDCA_ROOT_CAUSE_ROWS}-row limit.")
	measure_counts = _bounded_child_status_counts(
		doc.get("improvement_measures") or [],
		allowed=PDCA_MEASURE_STATUSES,
		limit=MAX_PDCA_MEASURE_ROWS,
		label="PDCA improvement measures",
	)
	return {
		"pdca_reference": _stable_hash(doc.name, 32),
		"status": _safe_enum(doc.get("status"), PDCA_STATUSES, "PDCA status"),
		"indicator_reference": _hashed_reference(doc.get("indicator")),
		"start_date": doc.get("start_date"),
		"end_date": doc.get("end_date"),
		"baseline_value": _optional_finite_number(doc.get("baseline_value"), "PDCA baseline value"),
		"target_value": _optional_finite_number(doc.get("target_value"), "PDCA target value"),
		"actual_value": _optional_finite_number(doc.get("actual_value"), "PDCA actual value"),
		"root_cause_count": len(root_causes),
		"confirmed_root_cause_count": sum(1 for row in root_causes if bool(int(row.get("confirmed") or 0))),
		"measure_count": sum(measure_counts.values()),
		"measure_status_counts": measure_counts,
	}


def _receipted_rectification_snapshot(
	task_doc,
	policy: str,
	finding,
	source_doc,
	snapshot: dict[str, Any],
	*,
	request: dict[str, Any],
	purpose: str,
) -> dict[str, Any]:
	reference = record_tool_access(
		tool_slug="ione_get_rectification_context",
		task=task_doc.name,
		policy=policy,
		purpose=purpose,
		accessed_fields=sorted(snapshot),
		result="Success",
		hospital=finding.get("hospital"),
		campus=finding.get("campus"),
		department=finding.get("department"),
		ward=finding.get("ward"),
		patient=finding.get("patient"),
		encounter=finding.get("encounter"),
		request=request,
		source_doctype=source_doc.doctype,
		source_name=source_doc.name,
		source_version=source_doc.get("modified"),
		source_record_hash=(
			source_doc.get("evidence_hash")
			or source_doc.get("active_key")
			or source_doc.get("project_code")
			or source_doc.name
		),
		snapshot=snapshot,
	)
	return {**snapshot, "evidence_reference": reference}


def _rectification_aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
	status_counts: dict[str, int] = {}
	for record in records:
		status = str(record.get("status") or "")
		status_counts[status] = status_counts.get(status, 0) + 1
	return {
		"total": len(records),
		"status_counts": dict(sorted(status_counts.items())),
		"records": records,
	}


def _bounded_child_status_counts(
	rows,
	*,
	allowed: frozenset[str],
	limit: int,
	label: str,
) -> dict[str, int]:
	rows = list(rows)
	if len(rows) > limit:
		frappe.throw(f"{label} exceed the governed {limit}-row limit.")
	counts: dict[str, int] = {}
	for row in rows:
		status = _safe_enum(row.get("status"), allowed, f"{label} status")
		counts[status] = counts.get(status, 0) + 1
	return dict(sorted(counts.items()))


def _safe_enum(value: Any, allowed: frozenset[str], label: str) -> str:
	normalized = str(value or "")
	if normalized not in allowed:
		frappe.throw(f"{label} is outside the governed value set.")
	return normalized


def _hashed_reference(value: Any) -> str | None:
	return _stable_hash(value, 32) if value not in (None, "") else None


def _required_sha256(value: Any, label: str) -> str:
	normalized = str(value or "").strip().lower()
	if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
		frappe.throw(f"{label} is not a SHA-256 digest.")
	return normalized


def _optional_sha256(value: Any, label: str) -> str | None:
	return _required_sha256(value, label) if value not in (None, "") else None


def _optional_finite_number(value: Any, label: str) -> float | None:
	if value in (None, ""):
		return None
	try:
		number = float(value)
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError(f"{label} is not numeric.") from exc
	if not math.isfinite(number):
		frappe.throw(f"{label} must be finite.")
	return number


def _assert_writable_task(task: str):
	doc = _get_active_task(task)
	if doc.status not in {"Running", "Pending Confirmation"}:
		frappe.throw("AI task is not writable")
	return doc


def _assert_safe_model_output(task_doc, *values: Any) -> None:
	try:
		assert_model_output_privacy(
			*values,
			known_identifiers=task_known_identifiers(task_doc),
		)
	except AIPrivacyViolation as exc:
		flag_aggregate_output_violation(str(task_doc.name), exc.code)
		frappe.throw(
			f"Model output failed the patient-identity safety gate. code={exc.code}",
			frappe.ValidationError,
		)


def _report_draft_execution_provenance(task_doc) -> tuple[int, str]:
	"""Bind every draft to the exact active attempt and Flow Run that created it."""
	attempt = int(task_doc.get("execution_attempt") or 0)
	if attempt < 1:
		frappe.throw("AI report draft execution attempt provenance is invalid.")

	linked_run = str(task_doc.get("flow_run") or "")
	if str(task_doc.get("execution_phase") or "Initial") == "Resume":
		if not linked_run:
			frappe.throw("Resumed AI report draft has no governed Flow Run.")
		lineage = frappe.db.get_value(
			"Flow Run",
			linked_run,
			["reference_doctype", "reference_name"],
			as_dict=True,
		)
		if (
			not lineage
			or str(lineage.get("reference_doctype") or "") != "IONE AI Analysis Task"
			or str(lineage.get("reference_name") or "") != str(task_doc.name)
		):
			frappe.throw("Resumed AI report draft Flow Run lineage is invalid.")
		return attempt, linked_run

	rows = frappe.get_all(
		"Flow Run",
		filters={
			"reference_doctype": "IONE AI Analysis Task",
			"reference_name": task_doc.name,
			"status": "Running",
		},
		fields=["name", "input"],
		order_by="creation desc, name desc",
		limit_page_length=4,
	)
	matches = []
	for row in rows:
		scope = _prompt_task_scope(str(row.get("input") or ""))
		if (
			str(scope.get("task") or "") == str(task_doc.name)
			and str(scope.get("policy") or "") == str(task_doc.get("policy") or "")
			and int(scope.get("execution_attempt") or 0) == attempt
		):
			matches.append(str(row.get("name") or ""))
	if len(matches) != 1 or not matches[0]:
		frappe.throw("AI report draft does not have one exact active Flow Run.")
	return attempt, matches[0]


def _prompt_task_scope(prompt: str) -> dict[str, Any]:
	start_tag = "<task_scope>"
	end_tag = "</task_scope>"
	start = prompt.find(start_tag)
	if start < 0:
		return {}
	start += len(start_tag)
	end = prompt.find(end_tag, start)
	if end < 0 or end - start > 4_096:
		return {}
	try:
		value = json.loads(prompt[start:end])
	except TypeError, ValueError:
		return {}
	return value if isinstance(value, dict) else {}


def _reference_json(references) -> str:
	return json.dumps(
		[reference.reference for reference in references],
		ensure_ascii=False,
		separators=(",", ":"),
	)


def _assert_task_scope(
	task: str | None,
	**source_scope: str | None,
) -> None:
	if not task:
		return
	task_doc = _get_active_task(task)
	_assert_scope_exact(task_doc, source_label="record", **source_scope)


def _assert_scope_exact(task_doc, *, source_label: str, **source_scope: str | None) -> None:
	"""Fail closed when an applicable source field does not exactly match task scope."""
	for fieldname in TASK_SCOPE_FIELDS:
		expected = task_doc.get(fieldname)
		if not expected or fieldname not in source_scope:
			continue
		actual = source_scope[fieldname]
		if not actual or str(actual) != str(expected):
			frappe.throw(
				f"The {source_label} {fieldname} is outside the governed AI task scope",
				frappe.PermissionError,
			)


def _scope_query_filters(task_doc, field_map: dict[str, str]) -> dict[str, Any]:
	"""Push every applicable non-empty task scope field into the database query."""
	return {
		query_field: task_doc.get(task_field)
		for task_field, query_field in field_map.items()
		if task_doc.get(task_field)
	}


def _validate_task_anchor_scopes(task_doc) -> None:
	"""Validate task-linked anchor records before any tool can use their scope."""
	if task_doc.get("indicator_result"):
		with current_indicator_result_lock(task_doc.indicator_result) as (_pointer, result):
			_require_requester_read(task_doc, result)
		_assert_scope_exact(
			task_doc,
			source_label="task indicator result",
			hospital=result.get("hospital"),
			campus=result.get("campus"),
			department=result.get("department"),
			ward=result.get("ward"),
			indicator_result=result.name,
		)
	if task_doc.get("finding"):
		finding = frappe.get_doc("IONE QC Finding", task_doc.finding)
		_require_requester_read(task_doc, finding)
		_assert_scope_exact(
			task_doc,
			source_label="task finding",
			hospital=finding.get("hospital"),
			campus=finding.get("campus"),
			department=finding.get("department"),
			ward=finding.get("ward"),
			patient=finding.get("patient"),
			encounter=finding.get("encounter"),
			responsible_staff=finding.get("responsible_staff"),
			finding=finding.name,
		)


def _verified_current_indicator_details(
	indicator_result: str,
	*,
	filters: dict[str, Any],
	fields: list[str],
	limit: int,
) -> list[Any]:
	"""Snapshot current-only contribution rows under the pointer lock."""
	with current_indicator_result_lock(indicator_result):
		output: list[Any] = []
		cursor = ""
		scanned = 0
		page_size = min(max(int(limit) * 2, 20), 100)
		while len(output) < limit:
			page_filters = dict(filters)
			if cursor:
				page_filters["name"] = [">", cursor]
			rows = frappe.get_all(
				"IONE Indicator Result Detail",
				filters=page_filters,
				fields=fields,
				order_by="name asc",
				limit_page_length=page_size,
			)
			if not rows:
				break
			for row in rows:
				cursor = str(row.name)
				scanned += 1
				detail = frappe.get_doc("IONE Indicator Result Detail", row.name)
				if indicator_artifact_is_governed_excluded(detail):
					continue
				verify_indicator_result_detail_integrity(detail)
				output.append(row)
				if len(output) >= limit:
					break
			if len(rows) < page_size:
				break
			if scanned >= 1_000:
				frappe.throw("Indicator detail exclusion scan exceeds the governed 1,000-row bound.")
		return output


def _task_policy(task: str | None) -> str | None:
	return frappe.db.get_value("IONE AI Analysis Task", task, "policy") if task else None


def _json_dict(value: Any) -> dict[str, Any]:
	if not value:
		return {}
	if isinstance(value, dict):
		return value
	parsed = json.loads(value)
	return parsed if isinstance(parsed, dict) else {}


def _json_value(value: Any) -> Any:
	if not isinstance(value, str):
		return value
	try:
		return json.loads(value)
	except ValueError:
		return value


def _stable_hash(value: Any, length: int = 16) -> str:
	return hashlib.sha256(str(value).encode()).hexdigest()[:length]


def _assert_authenticated() -> None:
	if frappe.session.user in (None, "", "Guest"):
		frappe.throw("Authentication is required", frappe.AuthenticationError)


def _get_active_task(task: str):
	active_task = str(getattr(frappe.flags, "ione_ai_task", None) or "")
	active_policy = str(getattr(frappe.flags, "ione_ai_policy", None) or "")
	if not active_task or str(task) != active_task:
		frappe.throw(
			"Tool task does not match the active governed Flow runtime.",
			frappe.PermissionError,
		)
	task_doc = frappe.get_doc("IONE AI Analysis Task", task)
	if task_doc.status not in {"Running", "Pending Confirmation"}:
		frappe.throw("IONE AI task is not active")
	if not active_policy or str(task_doc.policy) != active_policy:
		frappe.throw(
			"Tool policy does not match the active governed Flow runtime.",
			frappe.PermissionError,
		)
	policy = frappe.get_cached_doc("IONE Agent Policy", task_doc.policy)
	from ione_qms.ai.evaluation import governed_evaluation_runtime_active

	if (
		policy.status != "Active" and not governed_evaluation_runtime_active()
	) or frappe.session.user != policy.service_user:
		frappe.throw(
			"Only the active policy service user may execute task tools",
			frappe.PermissionError,
		)
	validate_policy_runtime(policy, task_doc)
	_validate_task_anchor_scopes(task_doc)
	return task_doc


def _active_policy_for_tool(tool_slug: str):
	task_name = str(getattr(frappe.flags, "ione_ai_task", None) or "")
	if not task_name:
		frappe.throw("No active governed IONE task is bound to this tool call", frappe.PermissionError)
	task = _get_active_task(task_name)
	policy = frappe.get_cached_doc("IONE Agent Policy", task.policy)
	if tool_slug not in {row.tool for row in policy.get("allowed_tools") or []}:
		frappe.throw("Tool is outside the active IONE Agent Policy", frappe.PermissionError)
	validate_policy_runtime(policy, task)
	return policy


def _require_requester_read(task_doc, doc) -> None:
	requested_by = str(task_doc.get("requested_by") or "")
	if not requested_by or requested_by in {"Guest", "Administrator"}:
		frappe.throw("AI task has no eligible human requester", frappe.PermissionError)
	if not doc.has_permission("read", user=requested_by):
		frappe.throw(
			"The original AI task requester no longer has access to this record",
			frappe.PermissionError,
		)


def _task_reference(
	task_doc,
	fieldname: str,
	provided: str | None,
	*,
	required: bool = True,
) -> str | None:
	scoped = task_doc.get(fieldname)
	if provided and scoped and provided != scoped:
		frappe.throw(
			f"Requested {fieldname} is outside the governed task scope",
			frappe.PermissionError,
		)
	value = scoped or provided
	if required and not value:
		frappe.throw(f"AI task has no {fieldname} scope")
	return value
