from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import frappe
from frappe.utils import get_datetime, getdate, now_datetime

from ione_qms.constants import (
	AI_REVIEW_SCOPED_ROLES,
	APP_ROLES,
	DEPARTMENT_SCOPED_ROLES,
	EXPLICIT_CLINICAL_SCOPE_ROLES,
	GLOBAL_CLINICAL_READ_ROLES,
	GLOBAL_CLINICAL_WRITE_ROLES,
	PERSONAL_CLINICAL_ROLES,
	QUALITY_ACTION_OVERSIGHT_ROLES,
	QUALITY_ACTION_OWNER_ROLES,
	QUALITY_MEETING_APPROVER_ROLES,
	QUALITY_MEETING_AUTHOR_ROLES,
	QWEN_FLOW_MODEL,
)
from ione_qms.services.data_export import (
	EXPORT_GLOBAL_APPROVER_ROLES,
	EXPORT_REQUESTER_ROLES,
	EXPORT_SCOPED_APPROVER_ROLE,
	auditor_can_access_export_request,
	auditor_export_request_query,
)
from ione_qms.services.scope_hierarchy import (
	CLINICAL_SCOPE_DOCTYPES,
	clinical_scope_errors,
	scope_integrity_sql,
)

FINDING_APPEAL_READ_ROLES = frozenset(
	{
		"IONE Physician",
		"IONE Department Director",
		"IONE Department QC Officer",
		"IONE QC Reviewer",
		"IONE Medical Affairs",
		"IONE Auditor",
	}
)
FINDING_APPEAL_EVIDENCE_READ_ROLES = FINDING_APPEAL_READ_ROLES - {"IONE Auditor"}
MEDICAL_RECORD_POLICY_AUTHOR_ROLES = frozenset({"IONE QC Reviewer", "IONE Medical Affairs"})
MEDICAL_RECORD_REVIEW_ACTOR_ROLES = frozenset(
	{"IONE Medical Record Coder", "IONE Medical Record Expert Reviewer"}
)
SOURCE_DOCUMENT_LOCATOR_AUTHOR_ROLES = frozenset({"IONE Integration Administrator"})
SOURCE_DOCUMENT_LOCATOR_APPROVER_ROLES = frozenset({"IONE QC Administrator", "IONE Medical Affairs"})
SOURCE_DOCUMENT_LOCATOR_READ_ROLES = (
	SOURCE_DOCUMENT_LOCATOR_AUTHOR_ROLES
	| SOURCE_DOCUMENT_LOCATOR_APPROVER_ROLES
	| frozenset({"IONE Auditor"})
)
SOURCE_DOCUMENT_ACCESS_LOG_READ_ROLES = frozenset(
	{
		"IONE QC Reviewer",
		"IONE Medical Affairs",
		"IONE Department Director",
		"IONE Department QC Officer",
		"IONE Auditor",
	}
)
QUALITY_MEETING_READ_ROLES = (
	QUALITY_MEETING_AUTHOR_ROLES
	| QUALITY_MEETING_APPROVER_ROLES
	| QUALITY_ACTION_OWNER_ROLES
	| frozenset({"IONE Auditor"})
)
QUALITY_ACTION_READ_ROLES = QUALITY_ACTION_OWNER_ROLES | QUALITY_ACTION_OVERSIGHT_ROLES
PHI_IDENTITY_BUSINESS_ROLES = frozenset(
	GLOBAL_CLINICAL_READ_ROLES | DEPARTMENT_SCOPED_ROLES | PERSONAL_CLINICAL_ROLES | {"IONE Auditor"}
)
_PHI_ORGANIZATION_DOCTYPES = {
	"hospital": "IONE Hospital",
	"campus": "IONE Hospital Campus",
	"department": "IONE Medical Department",
	"ward": "IONE Ward",
}
_PHI_DEPENDENCY_LOCK_FIELDS = {
	"IONE Patient Index": (
		"tenant_hospital",
		"source_system",
		"source_namespace",
		"identity_key_contract",
		"status",
		"modified",
	),
	"IONE Medical Staff": (
		"hospital",
		"campus",
		"department",
		"ward",
		"practice_status",
		"effective_from",
		"effective_to",
		"modified",
	),
	"IONE Source System": ("enabled", "identity_namespace", "modified"),
	"IONE Hospital": ("status", "modified"),
	"IONE Hospital Campus": ("hospital", "status", "modified"),
	"IONE Medical Department": ("hospital", "campus", "status", "modified"),
	"IONE Ward": ("hospital", "campus", "department", "status", "modified"),
}
_PHI_DEPENDENCY_LOCK_ORDER = (
	"IONE Patient Index",
	"IONE Medical Staff",
	"IONE Source System",
	"IONE Hospital",
	"IONE Hospital Campus",
	"IONE Medical Department",
	"IONE Ward",
)
_MAX_PHI_DEPENDENCIES_PER_DOCTYPE = 2_000


@dataclass(frozen=True)
class AccessContext:
	user: str
	roles: frozenset[str]
	hospitals: frozenset[str]
	campuses: frozenset[str]
	departments: frozenset[str]
	wards: frozenset[str]
	staff_records: frozenset[str]
	scope_grants: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)

	@property
	def is_administrator(self) -> bool:
		return self.user == "Administrator"

	@property
	def has_global_clinical_read(self) -> bool:
		return bool(self.roles.intersection(GLOBAL_CLINICAL_READ_ROLES))

	@property
	def has_global_clinical_write(self) -> bool:
		return bool(self.roles.intersection(GLOBAL_CLINICAL_WRITE_ROLES))

	@property
	def has_department_clinical_scope(self) -> bool:
		return bool(self.roles.intersection(DEPARTMENT_SCOPED_ROLES))

	@property
	def has_personal_clinical_scope(self) -> bool:
		return bool(self.roles.intersection(PERSONAL_CLINICAL_ROLES))

	@property
	def has_explicit_clinical_scope(self) -> bool:
		return bool(self.roles.intersection(EXPLICIT_CLINICAL_SCOPE_ROLES))

	@property
	def has_ai_review_scope(self) -> bool:
		return bool(self.roles.intersection(AI_REVIEW_SCOPED_ROLES))


def has_app_permission(user: str | None = None) -> bool:
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	return bool(APP_ROLES.intersection(frappe.get_roles(user)))


def get_access_context(user: str | None = None) -> AccessContext:
	user = user or frappe.session.user
	roles = frozenset(frappe.get_roles(user))
	rows: list[dict[str, Any]] = []
	if _doctype_exists("IONE Medical Staff"):
		rows = frappe.get_all(
			"IONE Medical Staff",
			filters={"user": user, "practice_status": "Active"},
			fields=[
				"name",
				"hospital",
				"campus",
				"department",
				"ward",
				"effective_from",
				"effective_to",
			],
			limit_page_length=10_000,
		)
		current_date = getdate(now_datetime())
		rows = [row for row in rows if _staff_assignment_is_current_and_consistent(row, current_date)]
	scope_grants = _load_scope_grants(user)
	return AccessContext(
		user=user,
		roles=roles,
		hospitals=frozenset(
			row.hospital for row in rows if row.hospital and not row.campus and not row.department
		),
		campuses=frozenset(row.campus for row in rows if row.campus and not row.department),
		departments=frozenset(row.department for row in rows if row.department),
		wards=frozenset(row.ward for row in rows if row.ward),
		staff_records=frozenset(row.name for row in rows if row.name),
		scope_grants=scope_grants,
	)


def require_role(*roles: str, user: str | None = None) -> None:
	context = get_access_context(user)
	if context.is_administrator or context.roles.intersection(roles):
		return
	frappe.throw("当前用户没有执行此操作所需的 IONE QMS 角色。", frappe.PermissionError)


def require_department(department: str | None, user: str | None = None) -> None:
	context = get_access_context(user)
	if context.is_administrator:
		frappe.throw(
			"Administrator cannot perform governed clinical actions; use a named accountable business user.",
			frappe.PermissionError,
		)
	if context.has_global_clinical_write:
		return
	if department and department in _authorized_departments(context, None, include_personal=True):
		return
	frappe.throw("当前用户无权访问该科室的数据。", frappe.PermissionError)


def require_department_read(department: str | None, user: str | None = None) -> None:
	context = get_access_context(user)
	if context.is_administrator:
		frappe.throw(
			"Administrator does not receive clinical business data access; use a named scoped user.",
			frappe.PermissionError,
		)
	if context.has_global_clinical_read:
		return
	if department and department in _authorized_departments(context, None):
		return
	frappe.throw("当前用户无权读取该科室的数据。", frappe.PermissionError)


def require_scope_read(
	*,
	hospital: str | None = None,
	campus: str | None = None,
	department: str | None = None,
	ward: str | None = None,
	user: str | None = None,
	target_doctype: str | None = None,
	include_personal: bool = False,
) -> None:
	"""Require an explicit authorized clinical scope, most-specific first."""
	context = get_access_context(user)
	if context.is_administrator:
		frappe.throw(
			"Administrator does not receive clinical business data access; use a named scoped user.",
			frappe.PermissionError,
		)
	if context.has_global_clinical_read:
		return
	if ward and ward in _authorized_wards(
		context,
		target_doctype,
		include_personal=include_personal,
		include_ai_review=True,
	):
		return
	if department and department in _authorized_departments(
		context,
		target_doctype,
		include_personal=include_personal,
		include_ai_review=True,
	):
		return
	if campus and campus in _authorized_campuses(
		context,
		target_doctype,
		include_personal=include_personal,
		include_ai_review=True,
	):
		return
	if hospital and hospital in _authorized_hospitals(
		context,
		target_doctype,
		include_personal=include_personal,
		include_ai_review=True,
	):
		return
	frappe.throw("当前用户无权读取所请求的医院、院区或科室范围。", frappe.PermissionError)


def patient_query(user: str | None = None) -> str:
	context = get_access_context(user)
	if context.is_administrator:
		return "1=0"
	table = "`tabIONE Patient Index`"
	integrity = scope_integrity_sql("IONE Patient Index", table)
	if context.has_global_clinical_read:
		return integrity
	direct_conditions: list[str] = []
	encounter_conditions: list[str] = []
	hospitals = _authorized_hospitals(context, "IONE Patient Index")
	campuses = _authorized_campuses(context, "IONE Patient Index")
	departments = _authorized_departments(context, "IONE Patient Index")
	wards = _authorized_wards(context, "IONE Patient Index")
	if hospitals:
		direct_conditions.append(f"{table}.tenant_hospital in ({_sql_values(hospitals)})")
	if campuses:
		encounter_conditions.append(f"enc.campus in ({_sql_values(campuses)})")
	if departments:
		encounter_conditions.append(f"enc.department in ({_sql_values(departments)})")
	if wards:
		encounter_conditions.append(f"enc.ward in ({_sql_values(wards)})")
	if context.has_personal_clinical_scope and context.staff_records:
		encounter_conditions.append(f"enc.responsible_staff in ({_sql_values(context.staff_records)})")
		encounter_conditions.append(f"enc.medical_staff in ({_sql_values(context.staff_records)})")
	if encounter_conditions:
		encounter_integrity = scope_integrity_sql("IONE Encounter Index", "enc")
		direct_conditions.append(
			"exists (select 1 from `tabIONE Encounter Index` enc "  # noqa: S608
			f"where enc.patient = {table}.name and ({encounter_integrity}) "
			f"and ({' or '.join(encounter_conditions)}))"
		)
	if not direct_conditions:
		return "1=0"
	return f"({integrity}) and ({' or '.join(direct_conditions)})"


def encounter_query(user: str | None = None) -> str:
	return _clinical_condition("IONE Encounter Index", user)


def clinical_event_query(user: str | None = None) -> str:
	return _clinical_condition("IONE Clinical Quality Event", user)


def integration_message_query(user: str | None = None) -> str:
	return _integration_data_query("IONE Integration Message", user)


def data_quality_issue_query(user: str | None = None) -> str:
	return _integration_data_query("IONE Data Quality Issue", user)


def _integration_data_query(doctype: str, user: str | None = None) -> str:
	context = get_access_context(user)
	if context.is_administrator or "IONE Integration Administrator" in context.roles:
		return "1=0"
	if "IONE QC Administrator" in context.roles:
		integrity = scope_integrity_sql(doctype, _table_name(doctype))
		return "" if integrity == "1=1" else integrity
	return _clinical_condition(doctype, user)


def qc_execution_query(user: str | None = None) -> str:
	return _clinical_condition("IONE QC Execution", user)


def rule_shadow_execution_query(user: str | None = None) -> str:
	return _clinical_condition("IONE QC Rule Shadow Execution", user)


def rule_shadow_feedback_query(user: str | None = None) -> str:
	return _clinical_condition("IONE QC Rule Shadow Feedback", user)


def qc_finding_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if user == "Administrator":
		return "1=0"
	condition = _clinical_condition("IONE QC Finding", user)
	if "IONE Agent Reviewer" not in frappe.get_roles(user):
		return condition
	reviewed_candidate = (
		"exists (select 1 from `tabIONE AI Candidate Finding` candidate "  # noqa: S608
		"where candidate.formal_finding = `tabIONE QC Finding`.name "
		f"and candidate.reviewed_by = {frappe.db.escape(user)} "
		"and candidate.status = 'Accepted')"
	)
	integrity = scope_integrity_sql("IONE QC Finding", "`tabIONE QC Finding`")
	if not condition:
		return integrity
	return f"({integrity}) and (({condition}) or {reviewed_candidate})"


def qc_finding_evidence_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	if user == "Administrator":
		return "1=0"
	finding_condition = qc_finding_query(user)
	if finding_condition == "1=0":
		return "1=0"
	link_condition = "`tabIONE QC Finding`.name = `tabIONE QC Finding Evidence`.finding"
	if finding_condition:
		link_condition += f" and ({finding_condition})"
	return (
		"exists (select 1 from `tabIONE QC Finding` "  # noqa: S608
		f"where {link_condition})"
	)


def source_document_locator_query(user: str | None = None) -> str:
	context = get_access_context(user)
	if context.user in {"Guest", "Administrator"} or not context.roles.intersection(
		SOURCE_DOCUMENT_LOCATOR_READ_ROLES
	):
		return "1=0"
	if context.roles.intersection(
		SOURCE_DOCUMENT_LOCATOR_AUTHOR_ROLES | SOURCE_DOCUMENT_LOCATOR_APPROVER_ROLES
	):
		return ""
	return _clinical_condition("IONE Source Document Locator", context.user)


def source_document_access_log_query(user: str | None = None) -> str:
	context = get_access_context(user)
	if context.user in {"Guest", "Administrator"} or not context.roles.intersection(
		SOURCE_DOCUMENT_ACCESS_LOG_READ_ROLES
	):
		return "1=0"
	return _clinical_condition("IONE Source Document Access Log", context.user)


def finding_appeal_query(user: str | None = None) -> str:
	context = get_access_context(user)
	if (
		context.user in {"Guest", "Administrator"}
		or "IONE Agent Service" in context.roles
		or not context.roles.intersection(FINDING_APPEAL_READ_ROLES)
	):
		return "1=0"
	finding_condition = qc_finding_query(context.user)
	if finding_condition == "1=0":
		return "1=0"
	link_condition = "`tabIONE QC Finding`.name = `tabIONE QC Finding Appeal`.finding"
	if finding_condition:
		link_condition += f" and ({finding_condition})"
	return (
		"exists (select 1 from `tabIONE QC Finding` "  # noqa: S608
		f"where {link_condition})"
	)


def finding_appeal_evidence_query(user: str | None = None) -> str:
	context = get_access_context(user)
	if (
		context.user in {"Guest", "Administrator"}
		or "IONE Agent Service" in context.roles
		or not context.roles.intersection(FINDING_APPEAL_EVIDENCE_READ_ROLES)
	):
		return "1=0"
	finding_condition = qc_finding_query(context.user)
	if finding_condition == "1=0":
		return "1=0"
	link_condition = "`tabIONE QC Finding`.name = `tabIONE QC Finding Appeal Evidence`.finding"
	if finding_condition:
		link_condition += f" and ({finding_condition})"
	return (
		"exists (select 1 from `tabIONE QC Finding` "  # noqa: S608
		f"where {link_condition})"
	)


def medical_record_qc_query(user: str | None = None) -> str:
	return _clinical_condition("IONE Medical Record QC", user)


def medical_record_qc_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	if ptype not in {"read", "select"}:
		return False
	return _clinical_permission(doc, ptype, user)


def medical_record_sampling_policy_query(user: str | None = None) -> str:
	return _clinical_condition("IONE Medical Record Sampling Policy", user)


def medical_record_sampling_policy_operation_query(user: str | None = None) -> str:
	return _clinical_condition("IONE Medical Record Sampling Policy Operation", user)


def medical_record_review_batch_query(user: str | None = None) -> str:
	return _clinical_condition("IONE Medical Record Review Batch", user)


def medical_record_review_assignment_query(user: str | None = None) -> str:
	user = user or frappe.session.user
	base = _clinical_condition("IONE Medical Record Review Assignment", user)
	actor = _medical_record_assignment_actor_condition(get_access_context(user))
	return _combine_read_conditions(base, actor)


def medical_record_review_decision_query(user: str | None = None) -> str:
	context = get_access_context(user)
	base = _clinical_condition("IONE Medical Record Review Decision", context.user)
	if context.has_global_clinical_read or "IONE Auditor" in context.roles:
		return base
	if not context.roles.intersection(MEDICAL_RECORD_REVIEW_ACTOR_ROLES):
		return "1=0"
	table = "`tabIONE Medical Record Review Decision`"
	return _combine_read_conditions(
		base,
		f"{table}.reviewed_by = {frappe.db.escape(context.user)}",
	)


def medical_record_archive_decision_query(user: str | None = None) -> str:
	return _medical_record_linked_assignment_query(
		"IONE Medical Record Archive Decision",
		user,
	)


def medical_record_archive_acknowledgement_query(user: str | None = None) -> str:
	return _medical_record_linked_assignment_query(
		"IONE Medical Record Archive Acknowledgement",
		user,
	)


def medical_record_archive_delivery_query(user: str | None = None) -> str:
	return _clinical_condition("IONE Medical Record Archive Delivery", user)


def medical_record_completeness_watermark_query(user: str | None = None) -> str:
	return _clinical_condition("IONE Medical Record Completeness Watermark", user)


def surgery_qc_query(user: str | None = None) -> str:
	return _clinical_condition("IONE Surgery QC", user)


def staff_qualification_query(user: str | None = None) -> str:
	return _surgery_governance_definition_query("IONE Staff Qualification", user)


def surgery_authorization_query(user: str | None = None) -> str:
	return _surgery_governance_definition_query("IONE Surgery Authorization", user)


def surgery_procedure_policy_query(user: str | None = None) -> str:
	return _surgery_governance_definition_query("IONE Surgery Procedure Policy", user)


def surgery_mdt_record_query(user: str | None = None) -> str:
	return _clinical_condition("IONE Surgery MDT Record", user)


def surgery_mdt_participant_query(user: str | None = None) -> str:
	return _clinical_condition("IONE Surgery MDT Participant", user)


def surgery_safety_checklist_query(user: str | None = None) -> str:
	return _clinical_condition("IONE Surgery Safety Checklist", user)


def surgery_emergency_exception_query(user: str | None = None) -> str:
	return _clinical_condition("IONE Surgery Emergency Exception", user)


def _surgery_governance_definition_query(
	doctype: str,
	user: str | None = None,
) -> str:
	context = get_access_context(user)
	if context.is_administrator:
		return "1=0"
	if context.roles.intersection({"System Manager", "IONE QC Administrator"}):
		integrity = scope_integrity_sql(doctype, _table_name(doctype))
		return "" if integrity == "1=1" else integrity
	return _clinical_condition(doctype, context.user)


def medical_safety_event_query(user: str | None = None) -> str:
	return _clinical_condition("IONE Medical Safety Event", user)


def indicator_alert_query(user: str | None = None) -> str:
	scoped = _clinical_condition("IONE Indicator Alert", user)
	return _combine_required_conditions(
		scoped,
		_completed_indicator_batch_authority_sql("`tabIONE Indicator Alert`.indicator_result"),
	)


def rectification_query(user: str | None = None) -> str:
	return _clinical_condition("IONE QC Rectification", user)


def verification_query(user: str | None = None) -> str:
	return _clinical_condition("IONE QC Verification", user)


def pdca_project_query(user: str | None = None) -> str:
	return _clinical_condition("IONE PDCA Project", user)


def quality_meeting_query(user: str | None = None) -> str:
	return _quality_governance_query("IONE Quality Meeting", user, QUALITY_MEETING_READ_ROLES)


def meeting_minute_query(user: str | None = None) -> str:
	return _quality_governance_query("IONE Meeting Minute", user, QUALITY_MEETING_READ_ROLES)


def meeting_decision_query(user: str | None = None) -> str:
	return _quality_governance_query("IONE Meeting Decision", user, QUALITY_MEETING_READ_ROLES)


def quality_action_item_query(user: str | None = None) -> str:
	return _quality_governance_query("IONE Quality Action Item", user, QUALITY_ACTION_READ_ROLES)


def quality_action_verification_round_query(user: str | None = None) -> str:
	return _quality_governance_query(
		"IONE Quality Action Verification Round",
		user,
		QUALITY_ACTION_READ_ROLES,
	)


def quality_experience_share_query(user: str | None = None) -> str:
	return _quality_governance_query(
		"IONE Quality Experience Share",
		user,
		QUALITY_MEETING_READ_ROLES,
	)


def finding_recurrence_policy_query(user: str | None = None) -> str:
	return _clinical_condition("IONE Finding Recurrence Policy", user)


def finding_recurrence_run_query(user: str | None = None) -> str:
	return _clinical_condition("IONE Finding Recurrence Run", user)


def finding_recurrence_evaluation_query(user: str | None = None) -> str:
	return _clinical_condition("IONE Finding Recurrence Evaluation", user)


def _completed_indicator_batch_authority_sql(result_expression: str) -> str:
	if result_expression not in {
		"`tabIONE Indicator Alert`.indicator_result",
		"`tabIONE Indicator Result`.name",
		"`tabIONE Indicator Result Pointer`.current_result",
		"`tabIONE Daily Quality Fact`.indicator_result",
	}:
		raise ValueError("Indicator batch authority requested an unsupported result expression.")
	return (
		"(not exists ("  # noqa: S608 - expression is selected from the fixed allowlist above.
		"select 1 from `tabIONE Batch Work Item` governed_work "
		f"where governed_work.result_reference = {result_expression}"
		") or exists ("
		"select 1 from `tabIONE Batch Work Item` governed_work "
		"inner join `tabIONE Batch Run` governed_batch "
		"on governed_batch.name = governed_work.batch_run "
		f"where governed_work.result_reference = {result_expression} "
		"and governed_work.status = 'Succeeded' "
		"and governed_batch.batch_type = 'Indicator' "
		"and governed_batch.status = 'Completed'"
		"))"
	)


def indicator_result_query(user: str | None = None) -> str:
	scoped = _clinical_condition("IONE Indicator Result", user)
	current = (
		"exists (select 1 from `tabIONE Indicator Result Pointer` pointer "
		"where pointer.current_result = `tabIONE Indicator Result`.name)"
	)
	return _combine_required_conditions(
		_combine_required_conditions(scoped, current),
		_completed_indicator_batch_authority_sql("`tabIONE Indicator Result`.name"),
	)


def indicator_result_detail_query(user: str | None = None) -> str:
	from ione_qms.indicator_engine.calculator import (
		indicator_result_detail_governance_schema_ready,
		indicator_result_detail_governance_sql,
	)

	if not indicator_result_detail_governance_schema_ready(frappe):
		return "1=0"
	scoped = _clinical_condition("IONE Indicator Result Detail", user)
	governed = indicator_result_detail_governance_sql("`tabIONE Indicator Result Detail`")
	return _combine_required_conditions(scoped, governed)


def indicator_result_pointer_query(user: str | None = None) -> str:
	scoped = _clinical_condition("IONE Indicator Result Pointer", user)
	return _combine_required_conditions(
		scoped,
		_completed_indicator_batch_authority_sql("`tabIONE Indicator Result Pointer`.current_result"),
	)


def daily_quality_fact_query(user: str | None = None) -> str:
	scoped = _clinical_condition("IONE Daily Quality Fact", user)
	return _combine_required_conditions(
		scoped,
		_completed_indicator_batch_authority_sql("`tabIONE Daily Quality Fact`.indicator_result"),
	)


def monthly_quality_fact_query(user: str | None = None) -> str:
	return _clinical_condition("IONE Monthly Quality Fact", user)


def finding_analysis_fact_query(user: str | None = None) -> str:
	return _clinical_condition("IONE Finding Analysis Fact", user)


def surgery_quality_fact_query(user: str | None = None) -> str:
	return _clinical_condition("IONE Surgery Quality Fact", user)


def agent_analysis_fact_query(user: str | None = None) -> str:
	return _clinical_condition("IONE Agent Analysis Fact", user)


def medical_staff_query(user: str | None = None) -> str:
	context = get_access_context(user)
	table = "`tabIONE Medical Staff`"
	integrity = scope_integrity_sql("IONE Medical Staff", table)
	if context.user == "Administrator":
		return "1=0"
	if context.roles.intersection({"System Manager", "IONE QC Administrator", "IONE Auditor"}):
		return integrity
	if context.has_department_clinical_scope:
		departments = _authorized_departments(context, "IONE Medical Staff")
		if departments:
			scoped = f"{table}.department in ({_sql_values(departments)})"
			return scoped if integrity == "1=1" else f"({integrity}) and {scoped}"
	if context.has_personal_clinical_scope:
		scoped = f"{table}.user = {frappe.db.escape(context.user)}"
		return scoped if integrity == "1=1" else f"({integrity}) and {scoped}"
	return "1=0"


def analysis_task_query(user: str | None = None) -> str:
	return _ai_query("IONE AI Analysis Task", user)


def candidate_finding_query(user: str | None = None) -> str:
	return _ai_query("IONE AI Candidate Finding", user)


def report_draft_query(user: str | None = None) -> str:
	return _ai_query("IONE AI Report Draft", user)


def quality_report_schedule_query(user: str | None = None) -> str:
	context = get_access_context(user)
	if context.is_administrator or "IONE Agent Service" in context.roles:
		return "1=0"
	table = "`tabIONE AI Report Schedule`"
	integrity = scope_integrity_sql("IONE AI Report Schedule", table)
	base = _ai_query("IONE AI Report Schedule", context.user)
	reviewer = f"{table}.report_reviewer = {frappe.db.escape(context.user)}"
	reviewer_access = reviewer if integrity == "1=1" else f"({integrity}) and ({reviewer})"
	if base == "1=0":
		return reviewer_access
	return f"({base}) or ({reviewer_access})"


def quality_report_snapshot_query(user: str | None = None) -> str:
	context = get_access_context(user)
	if context.is_administrator or "IONE Agent Service" in context.roles:
		return "1=0"
	doctype = "IONE Quality Report Snapshot"
	table = _table_name(doctype)
	integrity = scope_integrity_sql(doctype, table)
	if context.has_global_clinical_read:
		return integrity
	scoped = _scope_clauses(doctype, table, context, include_ai_review=True)
	if not scoped:
		return "1=0"
	return f"({integrity}) and ({' or '.join(scoped)})"


def quality_report_recovery_authorization_query(user: str | None = None) -> str:
	context = get_access_context(user)
	if context.is_administrator or "IONE Agent Service" in context.roles:
		return "1=0"
	doctype = "IONE AI Report Recovery Authorization"
	table = _table_name(doctype)
	integrity = scope_integrity_sql(doctype, table)
	actor = f"{table}.authorized_by = {frappe.db.escape(context.user)}"
	scoped = _scope_clauses(doctype, table, context, include_ai_review=True)
	if context.has_global_clinical_read:
		actor_access = actor
	else:
		if not scoped:
			return "1=0"
		actor_access = f"({actor}) and ({' or '.join(scoped)})"
	authorized = [actor_access]
	if "IONE Auditor" in context.roles:
		authorized.append("1=1" if context.has_global_clinical_read else f"({' or '.join(scoped)})")
	return f"({integrity}) and ({' or '.join(authorized)})"


def tool_approval_query(user: str | None = None) -> str:
	return _ai_query("IONE AI Tool Approval", user)


def ai_data_access_log_query(user: str | None = None) -> str:
	return _ai_query("IONE AI Data Access Log", user)


def ai_execution_event_query(user: str | None = None) -> str:
	return _ai_query("IONE AI Execution Event", user)


def flow_run_link_query(user: str | None = None) -> str:
	return _ai_query("IONE Flow Run Link", user)


def data_export_request_query(user: str | None = None) -> str:
	context = get_access_context(user)
	if context.user == "Administrator":
		return "1=0"
	table = "`tabIONE Data Export Request`"
	integrity = scope_integrity_sql("IONE Data Export Request", table)
	if context.roles.intersection(EXPORT_GLOBAL_APPROVER_ROLES):
		return integrity
	clauses: list[str] = []
	if EXPORT_SCOPED_APPROVER_ROLE in context.roles:
		clauses.append(auditor_export_request_query(context.user))
	if context.roles.intersection(EXPORT_REQUESTER_ROLES):
		clauses.append(f"{table}.requested_by = " + frappe.db.escape(context.user))
	clauses = [clause for clause in clauses if clause != "1=0"]
	authorized = "(" + " or ".join(clauses) + ")" if clauses else "1=0"
	return f"({integrity}) and ({authorized})"


def flow_agent_query(user: str | None = None) -> str:
	context = get_access_context(user)
	if context.is_administrator or context.roles.intersection(
		{"IONE Agent Administrator", "IONE QC Reviewer", "IONE Medical Affairs", "IONE Auditor"}
	):
		return ""
	non_ione = "`tabFlow Agent`.title not like 'IONE %'"
	if "IONE Agent Service" not in context.roles:
		return non_ione
	task_name = str(getattr(frappe.flags, "ione_ai_task", None) or "")
	policy_name = str(getattr(frappe.flags, "ione_ai_policy", None) or "")
	if not task_name or not policy_name:
		return non_ione
	return (
		f"({non_ione} or exists (select 1 from `tabIONE Agent Policy` policy "  # noqa: S608
		f"where policy.flow_agent = `tabFlow Agent`.name "
		f"and policy.name = {frappe.db.escape(policy_name)} "
		f"and policy.service_user = {frappe.db.escape(context.user)} "
		"and policy.status = 'Active' "
		"and exists (select 1 from `tabIONE AI Analysis Task` task "
		f"where task.name = {frappe.db.escape(task_name)} "
		"and task.policy = policy.name)))"
	)


def flow_model_query(user: str | None = None) -> str:
	context = get_access_context(user)
	if context.is_administrator:
		return ""
	return (
		f"(`tabFlow Model`.name != {frappe.db.escape(QWEN_FLOW_MODEL)} "  # noqa: S608
		"and not exists (select 1 from `tabIONE Agent Policy` policy "
		"inner join `tabFlow Agent` agent on agent.name = policy.flow_agent "
		"where agent.model = `tabFlow Model`.name))"
	)


def flow_run_query(user: str | None = None) -> str:
	context = get_access_context(user)
	table = "`tabFlow Run`"
	governed = _governed_flow_run_condition(table)
	if context.is_administrator:
		return f"not ({governed})"
	task_name, policy_name = _active_ai_runtime_names()
	service_access = "1=0"
	if task_name and policy_name and "IONE Agent Service" in context.roles:
		service_access = (
			f"{table}.owner = {frappe.db.escape(context.user)} "  # noqa: S608
			"and exists (select 1 from `tabFlow Session` session "
			"inner join `tabIONE Agent Policy` policy on policy.flow_agent = session.agent "
			"inner join `tabIONE AI Analysis Task` task on task.policy = policy.name "
			f"where session.name = {table}.session "
			f"and policy.name = {frappe.db.escape(policy_name)} "
			f"and task.name = {frappe.db.escape(task_name)} "
			f"and policy.service_user = {frappe.db.escape(context.user)} "
			"and policy.status = 'Active' "
			f"and (task.flow_run = {table}.name "
			f"or ({table}.reference_doctype = 'IONE AI Analysis Task' "
			f"and {table}.reference_name = task.name)))"
		)
	return f"(not ({governed}) or ({service_access}))"


def flow_session_query(user: str | None = None) -> str:
	context = get_access_context(user)
	table = "`tabFlow Session`"
	governed = _governed_flow_session_condition(table)
	if context.is_administrator:
		return f"not ({governed})"
	task_name, policy_name = _active_ai_runtime_names()
	service_access = "1=0"
	if task_name and policy_name and "IONE Agent Service" in context.roles:
		service_access = (
			f"{table}.owner = {frappe.db.escape(context.user)} "  # noqa: S608
			"and exists (select 1 from `tabIONE Agent Policy` policy "
			"inner join `tabIONE AI Analysis Task` task on task.policy = policy.name "
			f"where policy.flow_agent = {table}.agent "
			f"and policy.name = {frappe.db.escape(policy_name)} "
			f"and task.name = {frappe.db.escape(task_name)} "
			f"and policy.service_user = {frappe.db.escape(context.user)} "
			"and policy.status = 'Active' "
			f"and (task.flow_session = {table}.name "
			"or exists (select 1 from `tabFlow Run` run "
			f"where run.session = {table}.name "
			"and run.reference_doctype = 'IONE AI Analysis Task' "
			"and run.reference_name = task.name)))"
		)
	return f"(not ({governed}) or ({service_access}))"


def _ai_query(doctype: str, user: str | None = None) -> str:
	context = get_access_context(user)
	if context.is_administrator:
		return "1=0"
	table = _table_name(doctype)
	integrity = scope_integrity_sql(doctype, table)
	if context.roles.intersection({"IONE Agent Administrator", "IONE QC Reviewer", "IONE Medical Affairs"}):
		return integrity
	scoped = _scope_clauses(doctype, table, context, include_ai_review=True)
	if "IONE Auditor" in context.roles:
		if not scoped:
			return "1=0"
		authorized = "(" + " or ".join(scoped) + ")"
		return f"({integrity}) and ({authorized})"
	if _has_field(doctype, "requested_by"):
		scoped.append(f"{table}.requested_by = {frappe.db.escape(context.user)}")
	scoped.append(f"{table}.owner = {frappe.db.escape(context.user)}")
	authorized = "(" + " or ".join(scoped) + ")" if scoped else "1=0"
	return f"({integrity}) and ({authorized})"


def patient_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	context = get_access_context(user)
	if context.is_administrator:
		return False
	if not _scope_document_is_consistent(doc):
		return False
	if ptype not in {"read", "select"} and not context.has_global_clinical_write:
		return False
	if ptype in {"read", "select"} and context.has_global_clinical_read:
		return True
	if ptype not in {"read", "select"} and context.has_global_clinical_write:
		return True
	if not getattr(doc, "name", None):
		return False
	hospitals = _authorized_hospitals(context, "IONE Patient Index")
	if getattr(doc, "tenant_hospital", None) in hospitals:
		return True
	campuses = _authorized_campuses(context, "IONE Patient Index")
	departments = _authorized_departments(context, "IONE Patient Index")
	wards = _authorized_wards(context, "IONE Patient Index")
	clauses: list[str] = []
	if context.has_personal_clinical_scope and context.staff_records:
		staff = _sql_values(context.staff_records)
		clauses.extend(
			(
				f"enc.responsible_staff in ({staff})",
				f"enc.medical_staff in ({staff})",
			)
		)
	if departments:
		clauses.append(f"enc.department in ({_sql_values(departments)})")
	if campuses:
		clauses.append(f"enc.campus in ({_sql_values(campuses)})")
	if wards:
		clauses.append(f"enc.ward in ({_sql_values(wards)})")
	if not clauses:
		return False
	integrity = scope_integrity_sql("IONE Encounter Index", "enc")
	return bool(
		frappe.db.sql(
			"select enc.name from `tabIONE Encounter Index` enc "  # noqa: S608
			f"where enc.patient = {frappe.db.escape(doc.name)} "
			f"and ({integrity}) and ({' or '.join(clauses)}) limit 1"
		)
	)


def resolve_phi_identity_authorization(
	reference_doctype: str,
	reference_name: str,
	user: str,
):
	"""Lock and return the exact role/scope basis that independently authorizes PHI.

	The returned role set never borrows record access from one role and policy
	eligibility from another. Patient-level scoped access intentionally requires
	an active, not-yet-discharged encounter; historical patient identity requires
	an independently governed global clinical-reader role.
	"""
	if user in {"", "Guest", "Administrator"}:
		frappe.throw(
			"The requested identity record is unavailable or outside the authorized scope.",
			frappe.PermissionError,
		)
	if reference_doctype not in {"IONE Patient Index", "IONE Encounter Index"}:
		frappe.throw("The identity authorization target is not governed.")
	user_rows = frappe.db.sql(
		"select name, enabled, modified from `tabUser` where name = %s for update",
		(user,),
		as_dict=True,
	)
	if not user_rows or not int(user_rows[0].get("enabled") or 0):
		frappe.throw(
			"The requested identity record is unavailable or outside the authorized scope.",
			frappe.PermissionError,
		)
	role_rows = frappe.db.sql(
		"select name, role, modified from `tabHas Role` "
		"where parent = %s and parenttype = 'User' order by name asc for update",
		(user,),
		as_dict=True,
	)
	locked_roles = {
		str(row.get("role") or "")
		for row in role_rows
		if str(row.get("role") or "") in PHI_IDENTITY_BUSINESS_ROLES
	}
	if not locked_roles:
		frappe.throw(
			"The requested identity record is unavailable or outside the authorized scope.",
			frappe.PermissionError,
		)
	permission_rows = frappe.db.sql(
		"select name, allow, for_value, applicable_for, modified "
		"from `tabUser Permission` where user = %s order by name asc limit 1001 for update",
		(user,),
		as_dict=True,
	)
	if len(permission_rows) > 1_000:
		frappe.throw("PHI authorization has too many User Permission records.")
	staff_rows = frappe.db.sql(
		"select name, hospital, campus, department, ward, effective_from, effective_to, "
		"practice_status, modified from `tabIONE Medical Staff` "
		"where user = %s order by name asc limit 1001 for update",
		(user,),
		as_dict=True,
	)
	if len(staff_rows) > 1_000:
		frappe.throw("PHI authorization has too many medical-staff assignments.")
	linked_patient = None
	encounter_rows: list[Any] = []
	try:
		if reference_doctype == "IONE Encounter Index":
			prelock_patient = str(
				frappe.db.get_value("IONE Encounter Index", reference_name, "patient") or ""
			)
			if prelock_patient:
				linked_patient = frappe.get_doc(
					"IONE Patient Index",
					prelock_patient,
					for_update=True,
				)
			doc = frappe.get_doc(reference_doctype, reference_name, for_update=True)
			if str(doc.get("patient") or "") != prelock_patient:
				frappe.throw(
					"PHI identity lineage changed while acquiring its lock; retry the request.",
					frappe.PermissionError,
				)
		else:
			doc = frappe.get_doc(reference_doctype, reference_name, for_update=True)
			encounter_rows = frappe.db.sql(
				"select name, encounter_key, identity_key_contract, source_system, "
				"source_namespace, source_encounter_id, patient, hospital, campus, "
				"department, ward, responsible_staff, medical_staff, status, "
				"discharge_time, modified "
				"from `tabIONE Encounter Index` where patient = %s "
				"order by name asc limit 1001 for update",
				(str(doc.name),),
				as_dict=True,
			)
			if len(encounter_rows) > 1_000:
				frappe.throw("PHI patient authorization has too many encounter candidates.")
	except frappe.DoesNotExistError:
		frappe.throw(
			"The requested identity record is unavailable or outside the authorized scope.",
			frappe.PermissionError,
		)
	dependency_snapshot, active_scope_values = _lock_phi_authorization_dependencies(
		records=[
			*staff_rows,
			doc,
			*encounter_rows,
			*([linked_patient] if linked_patient is not None else []),
		],
		permission_rows=permission_rows,
	)
	current_date = getdate(now_datetime())
	active_staff = [
		row
		for row in staff_rows
		if str(row.get("practice_status") or "") == "Active"
		and _staff_assignment_is_current_and_consistent(frappe._dict(row), current_date)
	]
	if not _scope_document_is_consistent(doc):
		frappe.throw(
			"The requested identity record is unavailable or outside the authorized scope.",
			frappe.PermissionError,
		)

	global_roles = locked_roles.intersection(GLOBAL_CLINICAL_READ_ROLES)
	if reference_doctype == "IONE Encounter Index":
		scope = _phi_scope_from_record(doc)
		scoped_roles, staff_names, permission_names = _independent_phi_scope_roles(
			scope=scope,
			doctype=reference_doctype,
			doc=doc,
			locked_roles=locked_roles,
			active_staff=active_staff,
			permission_rows=permission_rows,
			active_scope_values=active_scope_values,
			allow_personal=True,
		)
		authorizing_roles = global_roles | scoped_roles
		basis_encounter = doc
	else:
		candidates: list[tuple[int, str, Any, frozenset[str], frozenset[str], frozenset[str]]] = []
		for raw_row in encounter_rows:
			encounter = frappe._dict({"doctype": "IONE Encounter Index", **dict(raw_row)})
			if str(encounter.get("status") or "") != "Active" or encounter.get("discharge_time"):
				continue
			if clinical_scope_errors(encounter):
				continue
			candidate_scope = _phi_scope_from_record(encounter)
			roles, candidate_staff, candidate_permissions = _independent_phi_scope_roles(
				scope=candidate_scope,
				doctype=reference_doctype,
				doc=encounter,
				locked_roles=locked_roles,
				active_staff=active_staff,
				permission_rows=permission_rows,
				active_scope_values=active_scope_values,
				allow_personal=True,
			)
			if not roles:
				continue
			specificity = sum(bool(candidate_scope[field]) for field in ("campus", "department", "ward"))
			candidates.append(
				(
					-specificity,
					str(encounter.name),
					encounter,
					roles,
					candidate_staff,
					candidate_permissions,
				)
			)
		if candidates:
			(
				_specificity,
				_name,
				basis_encounter,
				scoped_roles,
				staff_names,
				permission_names,
			) = sorted(candidates, key=lambda item: (item[0], item[1]))[0]
			scope = _phi_scope_from_record(basis_encounter)
		else:
			basis_encounter = None
			scoped_roles = frozenset()
			staff_names = frozenset()
			permission_names = frozenset()
			scope = {
				"hospital": str(doc.get("tenant_hospital") or ""),
				"campus": "",
				"department": "",
				"ward": "",
			}
		authorizing_roles = global_roles | scoped_roles
	if not authorizing_roles:
		frappe.throw(
			"The requested identity record is unavailable or outside the authorized scope.",
			frappe.PermissionError,
		)
	role_snapshot = {
		"contract": "ione-qms-phi-role-snapshot-v1",
		"user": user,
		"authorizing_roles": sorted(authorizing_roles),
		"assignments": [
			{
				"name": str(row.get("name") or ""),
				"role": str(row.get("role") or ""),
				"modified": str(row.get("modified") or ""),
			}
			for row in role_rows
			if str(row.get("role") or "") in authorizing_roles
		],
	}
	basis = {
		"contract": "ione-qms-phi-authorization-basis-v1",
		"reference_doctype": reference_doctype,
		"reference_name": str(doc.name),
		"authorized_scope": scope,
		"authorizing_roles": sorted(authorizing_roles),
		"encounter_basis": str(getattr(basis_encounter, "name", "") or ""),
		"patient_scope_rule": (
			"active-undischarged-encounter-v1"
			if reference_doctype == "IONE Patient Index" and basis_encounter
			else "not-applicable"
		),
		"medical_staff_records": sorted(staff_names),
		"user_permission_records": sorted(permission_names),
		"locked_record_versions": {
			"identity": str(doc.get("modified") or ""),
			"encounter": str(getattr(basis_encounter, "modified", "") if basis_encounter else ""),
			"user": str(user_rows[0].get("modified") or ""),
			"dependencies": dependency_snapshot,
		},
	}
	return doc, basis, role_snapshot


def _lock_phi_authorization_dependencies(
	*,
	records: list[Any],
	permission_rows: list[Any],
) -> tuple[list[dict[str, Any]], dict[str, frozenset[str]]]:
	"""Lock every mutable row that contributes to a scoped PHI decision."""
	names: dict[str, set[str]] = {doctype: set() for doctype in _PHI_DEPENDENCY_LOCK_ORDER}

	def collect(record) -> None:
		if record is None:
			return
		for fieldname, doctype in _PHI_ORGANIZATION_DOCTYPES.items():
			value = str(record.get(fieldname) or "")
			if value:
				names[doctype].add(value)
		tenant_value = str(record.get("tenant_hospital") or "")
		if tenant_value:
			names["IONE Hospital"].add(tenant_value)
		for fieldname, doctype in (
			("source_system", "IONE Source System"),
			("patient", "IONE Patient Index"),
			("responsible_staff", "IONE Medical Staff"),
			("medical_staff", "IONE Medical Staff"),
		):
			value = str(record.get(fieldname) or "")
			if value:
				names[doctype].add(value)

	for record in records:
		collect(record)
	for row in permission_rows:
		allow = str(row.get("allow") or "")
		value = str(row.get("for_value") or "")
		if allow in _PHI_DEPENDENCY_LOCK_FIELDS and value:
			names[allow].add(value)

	locked_rows: dict[str, list[Any]] = {}
	locked_names: dict[str, set[str]] = {doctype: set() for doctype in _PHI_DEPENDENCY_LOCK_ORDER}
	for doctype in _PHI_DEPENDENCY_LOCK_ORDER:
		if len(names[doctype]) > _MAX_PHI_DEPENDENCIES_PER_DOCTYPE:
			frappe.throw(f"PHI authorization has too many {doctype} dependencies.")
		rows = _lock_phi_dependency_rows(doctype, names[doctype])
		locked_rows[doctype] = rows
		locked_names[doctype].update(str(row.get("name") or "") for row in rows)
		for row in rows:
			collect(row)
		for earlier in _PHI_DEPENDENCY_LOCK_ORDER:
			if earlier == doctype:
				break
			if not names[earlier].issubset(locked_names[earlier]):
				frappe.throw(
					"PHI authorization dependency graph changed while acquiring locks; retry the request.",
					frappe.PermissionError,
				)

	active_scope_values = {
		doctype: frozenset(
			str(row.get("name") or "")
			for row in locked_rows.get(doctype, [])
			if str(row.get("status") or "") == "Active"
		)
		for doctype in _PHI_ORGANIZATION_DOCTYPES.values()
	}
	snapshot = [
		{
			"doctype": doctype,
			"name": str(row.get("name") or ""),
			"modified": str(row.get("modified") or ""),
			"governance": {
				fieldname: row.get(fieldname)
				for fieldname in _PHI_DEPENDENCY_LOCK_FIELDS[doctype]
				if fieldname != "modified"
			},
		}
		for doctype in _PHI_DEPENDENCY_LOCK_ORDER
		for row in locked_rows.get(doctype, [])
	]
	return snapshot, active_scope_values


def _lock_phi_dependency_rows(doctype: str, names: set[str]) -> list[Any]:
	if not names:
		return []
	fields = _PHI_DEPENDENCY_LOCK_FIELDS.get(doctype)
	if fields is None:
		frappe.throw("PHI authorization requested an unsupported dependency type.")
	ordered = sorted(names)
	rows: list[Any] = []
	for offset in range(0, len(ordered), 100):
		chunk = ordered[offset : offset + 100]
		placeholders = ", ".join(["%s"] * len(chunk))
		query_fields = ", ".join(("name", *fields))
		rows.extend(
			frappe.db.sql(
				f"select {query_fields} from `tab{doctype}` "  # noqa: S608
				f"where name in ({placeholders}) order by name asc for update",
				tuple(chunk),
				as_dict=True,
			)
		)
	found = {str(row.get("name") or "") for row in rows}
	if found != set(ordered):
		frappe.throw(
			"The requested identity record is unavailable or outside the authorized scope.",
			frappe.PermissionError,
		)
	return rows


def _independent_phi_scope_roles(
	*,
	scope: dict[str, str],
	doctype: str,
	doc,
	locked_roles: set[str],
	active_staff: list[Any],
	permission_rows: list[Any],
	active_scope_values: dict[str, frozenset[str]],
	allow_personal: bool,
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
	staff_names: set[str] = set()
	permission_names: set[str] = set()
	staff_match = False
	for row in active_staff:
		if _phi_staff_scope_matches(row, scope):
			staff_match = True
			staff_names.add(str(row.get("name") or ""))
	grant_match = False
	for row in permission_rows:
		if _phi_user_permission_matches(
			row,
			scope,
			doctype,
			active_scope_values=active_scope_values,
		):
			grant_match = True
			permission_names.add(str(row.get("name") or ""))
	roles: set[str] = set()
	if staff_match or grant_match:
		roles.update(locked_roles.intersection(DEPARTMENT_SCOPED_ROLES))
	if grant_match:
		roles.update(locked_roles.intersection({"IONE Auditor"}))
	if allow_personal and "IONE Physician" in locked_roles:
		linked_staff = {
			str(doc.get("responsible_staff") or ""),
			str(doc.get("medical_staff") or ""),
		}.intersection({str(row.get("name") or "") for row in active_staff})
		if linked_staff:
			roles.add("IONE Physician")
			staff_names.update(linked_staff)
	return frozenset(roles), frozenset(staff_names), frozenset(permission_names)


def _phi_staff_scope_matches(row, scope: dict[str, str]) -> bool:
	department = str(row.get("department") or "")
	campus = str(row.get("campus") or "")
	ward = str(row.get("ward") or "")
	hospital = str(row.get("hospital") or "")
	return bool(
		(ward and ward == scope["ward"])
		or (department and department == scope["department"])
		or (campus and not department and campus == scope["campus"])
		or (hospital and not campus and not department and hospital == scope["hospital"])
	)


def _phi_user_permission_matches(
	row,
	scope: dict[str, str],
	doctype: str,
	*,
	active_scope_values: dict[str, frozenset[str]],
) -> bool:
	applicable_for = str(row.get("applicable_for") or "")
	if applicable_for and applicable_for != doctype:
		return False
	allow_to_scope = {
		"IONE Hospital": "hospital",
		"IONE Hospital Campus": "campus",
		"IONE Medical Department": "department",
		"IONE Ward": "ward",
	}
	allow = str(row.get("allow") or "")
	fieldname = allow_to_scope.get(allow)
	value = str(row.get("for_value") or "")
	return bool(
		fieldname
		and value
		and value in active_scope_values.get(allow, frozenset())
		and value == scope[fieldname]
	)


def _phi_scope_from_record(doc) -> dict[str, str]:
	return {
		"hospital": str(doc.get("hospital") or ""),
		"campus": str(doc.get("campus") or ""),
		"department": str(doc.get("department") or ""),
		"ward": str(doc.get("ward") or ""),
	}


def encounter_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	return _clinical_permission(doc, ptype, user)


def clinical_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	return _clinical_permission(doc, ptype, user)


def surgery_governance_definition_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	context = get_access_context(user)
	if context.user in {"Guest", "Administrator"} or "IONE Agent Service" in context.roles:
		return False
	if not _scope_document_is_consistent(doc):
		return False
	if ptype in {"delete", "cancel", "amend", "submit"}:
		return False
	if ptype in {"read", "select"}:
		if context.roles.intersection(
			{
				"System Manager",
				"IONE QC Administrator",
				"IONE QC Reviewer",
				"IONE Medical Affairs",
			}
		):
			return True
		return _clinical_permission(doc, ptype, context.user)
	if str(getattr(doc, "status", None) or "Draft") != "Draft":
		return False
	if context.roles.intersection({"System Manager", "IONE QC Administrator"}):
		return True
	if "IONE Medical Affairs" in context.roles:
		return _clinical_permission(doc, "write", context.user)
	return False


def surgery_governance_audit_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	if ptype not in {"read", "select"}:
		return False
	context = get_access_context(user)
	if context.user in {"Guest", "Administrator"} or "IONE Agent Service" in context.roles:
		return False
	return _clinical_permission(doc, ptype, context.user)


def integration_data_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	context = get_access_context(user)
	if context.is_administrator or "IONE Integration Administrator" in context.roles:
		return False
	if not _scope_document_is_consistent(doc):
		return False
	if "IONE QC Administrator" in context.roles:
		return ptype in {"read", "select"}
	return _clinical_permission(doc, ptype, user)


def finding_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	user = user or frappe.session.user
	if user == "Administrator":
		return False
	if not _scope_document_is_consistent(doc):
		return False
	if (
		doc.doctype == "IONE QC Finding"
		and user == getattr(frappe.flags, "ione_candidate_reviewer", None)
		and doc.name == getattr(frappe.flags, "ione_candidate_review_finding", None)
		and ptype in {"read", "select", "write"}
	):
		return True
	if (
		doc.doctype == "IONE QC Finding"
		and ptype in {"read", "select"}
		and "IONE Agent Reviewer" in frappe.get_roles(user)
		and getattr(doc, "name", None)
		and frappe.db.exists(
			"IONE AI Candidate Finding",
			{
				"formal_finding": doc.name,
				"reviewed_by": user,
				"status": "Accepted",
			},
		)
	):
		return True
	return _clinical_permission(doc, ptype, user)


def qc_finding_evidence_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	if ptype not in {"read", "select"}:
		return False
	user = user or frappe.session.user
	if user in {"Guest", "Administrator"}:
		return False
	finding_name = str(getattr(doc, "finding", None) or "")
	if not finding_name:
		return False
	try:
		finding = frappe.get_doc("IONE QC Finding", finding_name)
	except frappe.DoesNotExistError:
		return False
	return finding_permission(finding, "read", user=user)


def source_document_locator_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	context = get_access_context(user)
	if context.user in {"Guest", "Administrator"} or not context.roles.intersection(
		SOURCE_DOCUMENT_LOCATOR_READ_ROLES
	):
		return False
	if ptype in {"delete", "cancel", "amend", "submit"}:
		return False
	if ptype in {"read", "select"}:
		if context.roles.intersection(
			SOURCE_DOCUMENT_LOCATOR_AUTHOR_ROLES | SOURCE_DOCUMENT_LOCATOR_APPROVER_ROLES
		):
			return True
		return _clinical_permission(doc, "read", context.user)
	if ptype in {"create", "write"}:
		return bool(
			context.roles.intersection(SOURCE_DOCUMENT_LOCATOR_AUTHOR_ROLES)
			and str(getattr(doc, "status", None) or "Draft") == "Draft"
		)
	return False


def source_document_access_log_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	context = get_access_context(user)
	if (
		ptype not in {"read", "select"}
		or context.user in {"Guest", "Administrator"}
		or not context.roles.intersection(SOURCE_DOCUMENT_ACCESS_LOG_READ_ROLES)
	):
		return False
	return _clinical_permission(doc, "read", context.user)


def finding_appeal_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	context = get_access_context(user)
	if (
		context.user in {"Guest", "Administrator"}
		or "IONE Agent Service" in context.roles
		or not context.roles.intersection(FINDING_APPEAL_READ_ROLES)
		or ptype not in {"read", "select"}
	):
		return False
	finding_name = str(getattr(doc, "finding", None) or "")
	if not finding_name:
		return False
	try:
		finding = frappe.get_doc("IONE QC Finding", finding_name)
	except frappe.DoesNotExistError:
		return False
	return finding_permission(finding, "read", user=context.user)


def finding_appeal_evidence_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	context = get_access_context(user)
	if (
		context.user in {"Guest", "Administrator"}
		or "IONE Agent Service" in context.roles
		or not context.roles.intersection(FINDING_APPEAL_EVIDENCE_READ_ROLES)
		or ptype not in {"read", "select"}
	):
		return False
	finding_name = str(getattr(doc, "finding", None) or "")
	if not finding_name:
		return False
	try:
		finding = frappe.get_doc("IONE QC Finding", finding_name)
	except frappe.DoesNotExistError:
		return False
	return finding_permission(finding, "read", user=context.user)


def finding_appeal_evidence_file_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
	debug: bool = False,
) -> bool:
	"""Deny sharing/mutation and authorize reads through the isolated evidence parent."""
	del debug
	user = user or frappe.session.user
	if any(
		getattr(frappe.flags, flag_name, False) for flag_name in ("in_install", "in_migrate", "in_uninstall")
	):
		return True
	export_decision = _data_export_file_permission(doc, ptype, user)
	if export_decision is not None:
		return export_decision
	if not _doctype_exists("IONE QC Finding Appeal Evidence"):
		return True
	file_name = str(getattr(doc, "name", None) or "")
	content_hash = str(getattr(doc, "content_hash", None) or "")
	evidence_name = (
		frappe.db.get_value(
			"IONE QC Finding Appeal Evidence",
			{"evidence_file": file_name},
			"name",
		)
		if file_name
		else None
	)
	if isinstance(evidence_name, dict):
		evidence_name = evidence_name.get("name")
	if not evidence_name and content_hash:
		evidence_name = frappe.db.get_value(
			"IONE QC Finding Appeal Evidence",
			{"content_hash": content_hash},
			"name",
		)
		if isinstance(evidence_name, dict):
			evidence_name = evidence_name.get("name")
	if not evidence_name:
		return True
	if ptype not in {"read", "select"}:
		return False
	try:
		evidence = frappe.get_doc("IONE QC Finding Appeal Evidence", evidence_name)
	except frappe.DoesNotExistError:
		return False
	return finding_appeal_evidence_permission(evidence, "read", user=user)


def _data_export_file_permission(doc, ptype: str, user: str) -> bool | None:
	"""Return None for unrelated files; otherwise enforce the governed export parent."""
	if not _doctype_exists("IONE Data Export Request"):
		return None
	file_name = str(getattr(doc, "name", None) or "")
	attached_doctype = str(getattr(doc, "attached_to_doctype", None) or "")
	attached_name = str(getattr(doc, "attached_to_name", None) or "")
	is_export_file = attached_doctype == "IONE Data Export Request"
	request_name = attached_name if is_export_file else ""
	if not request_name and file_name:
		request_name = str(
			frappe.db.get_value(
				"IONE Data Export Request",
				{"output_file": file_name},
				"name",
			)
			or ""
		)
		is_export_file = bool(request_name)
	if not is_export_file:
		return None
	if not request_name:
		return False

	active_generation = str(getattr(frappe.flags, "ione_data_export_generation", None) or "")
	if active_generation == request_name:
		return ptype in {"create", "read", "select", "write"}
	if ptype not in {"read", "select"} or not file_name:
		return False
	try:
		request = frappe.get_doc("IONE Data Export Request", request_name)
	except frappe.DoesNotExistError:
		return False
	if (
		str(getattr(request, "output_file", None) or "") != file_name
		or str(getattr(request, "status", None) or "") != "Completed"
		or (getattr(request, "expires_at", None) and get_datetime(request.expires_at) <= now_datetime())
	):
		return False
	return data_export_request_permission(request, "read", user=user)


def analytics_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	if not _clinical_permission(doc, ptype, user):
		return False
	if ptype not in {"read", "select"}:
		return False
	if doc.doctype in {
		"IONE Indicator Alert",
		"IONE Indicator Result",
		"IONE Indicator Result Pointer",
		"IONE Daily Quality Fact",
	}:
		from ione_qms.services.indicators import require_completed_batch_authority

		result_name = (
			doc.name
			if doc.doctype == "IONE Indicator Result"
			else str(doc.get("current_result") or doc.get("indicator_result") or "")
		)
		if not result_name:
			return False
		try:
			require_completed_batch_authority(result_name)
		except ValueError, frappe.DoesNotExistError:
			return False
	if doc.doctype == "IONE Indicator Result":
		return bool(
			frappe.db.exists(
				"IONE Indicator Result Pointer",
				{"current_result": doc.name},
			)
		)
	if doc.doctype == "IONE Indicator Result Detail":
		from ione_qms.services.indicators import indicator_result_detail_is_governed_readable

		return indicator_result_detail_is_governed_readable(doc)
	return True


def quality_meeting_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	return _quality_authorable_permission(
		doc,
		ptype,
		user,
		author_roles=QUALITY_MEETING_AUTHOR_ROLES,
		read_roles=QUALITY_MEETING_READ_ROLES,
	)


def meeting_minute_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	return _quality_authorable_permission(
		doc,
		ptype,
		user,
		author_roles=QUALITY_MEETING_AUTHOR_ROLES,
		read_roles=QUALITY_MEETING_READ_ROLES,
	)


def meeting_decision_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	return _quality_readonly_permission(doc, ptype, user, QUALITY_MEETING_READ_ROLES)


def quality_action_item_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	return _quality_readonly_permission(doc, ptype, user, QUALITY_ACTION_READ_ROLES)


def quality_action_verification_round_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	return _quality_readonly_permission(doc, ptype, user, QUALITY_ACTION_READ_ROLES)


def quality_experience_share_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	return _quality_authorable_permission(
		doc,
		ptype,
		user,
		author_roles=QUALITY_MEETING_AUTHOR_ROLES,
		read_roles=QUALITY_MEETING_READ_ROLES,
	)


def quality_report_schedule_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	context = get_access_context(user)
	if context.is_administrator or "IONE Agent Service" in context.roles:
		return False
	if not _scope_document_is_consistent(doc):
		return False
	requester_roles = {
		"IONE QC Reviewer",
		"IONE Medical Affairs",
		"IONE Department Director",
		"IONE Department QC Officer",
	}
	if ptype == "create":
		return bool(context.roles.intersection(requester_roles)) and _clinical_permission(
			doc,
			"write",
			context.user,
			include_ai_review=True,
		)
	if ptype in {"delete", "cancel", "amend", "submit"}:
		return False
	if ptype in {"write", "save"}:
		return bool(
			str(getattr(doc, "status", None) or "Draft") == "Draft"
			and str(getattr(doc, "requested_by", None) or "") == context.user
			and context.roles.intersection(requester_roles)
			and _clinical_permission(
				doc,
				"write",
				context.user,
				include_ai_review=True,
			)
		)
	if ptype not in {"read", "select"}:
		return False
	if "IONE Agent Administrator" in context.roles:
		return True
	if (
		str(getattr(doc, "requested_by", None) or "")
		not in {
			context.user,
			"",
		}
		and str(getattr(doc, "report_reviewer", None) or "") != context.user
	):
		if not context.roles.intersection({"IONE QC Reviewer", "IONE Medical Affairs", "IONE Auditor"}):
			return False
	return _clinical_permission(
		doc,
		"read",
		context.user,
		include_ai_review=True,
	)


def quality_report_snapshot_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	if ptype not in {"read", "select"}:
		return False
	context = get_access_context(user)
	if context.is_administrator or "IONE Agent Service" in context.roles:
		return False
	return _clinical_permission(
		doc,
		ptype,
		context.user,
		include_ai_review=True,
	)


def quality_report_recovery_authorization_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	context = get_access_context(user)
	if (
		ptype not in {"read", "select"}
		or context.is_administrator
		or "IONE Agent Service" in context.roles
		or not _scope_document_is_consistent(doc)
	):
		return False
	if str(getattr(doc, "authorized_by", None) or "") == context.user:
		return _clinical_permission(
			doc,
			ptype,
			context.user,
			include_ai_review=True,
		)
	if "IONE Auditor" in context.roles:
		return _clinical_permission(doc, ptype, context.user)
	return False


def medical_record_sampling_policy_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	context = get_access_context(user)
	if (
		context.is_administrator
		or "IONE Agent Service" in context.roles
		or not _scope_document_is_consistent(doc)
	):
		return False
	if ptype == "create":
		return bool(context.roles.intersection(MEDICAL_RECORD_POLICY_AUTHOR_ROLES)) and _clinical_permission(
			doc,
			"write",
			context.user,
		)
	if ptype in {"write", "save"}:
		return bool(
			str(getattr(doc, "status", None) or "Draft") == "Draft"
			and str(getattr(doc, "requested_by", None) or context.user) == context.user
			and context.roles.intersection(MEDICAL_RECORD_POLICY_AUTHOR_ROLES)
			and _clinical_permission(doc, "write", context.user)
		)
	if ptype not in {"read", "select"}:
		return False
	return _clinical_permission(doc, ptype, context.user)


def medical_record_review_batch_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	return ptype in {"read", "select"} and _medical_record_review_scope_permission(doc, user)


def medical_record_sampling_policy_operation_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	return ptype in {"read", "select"} and _medical_record_review_scope_permission(doc, user)


def medical_record_review_assignment_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	if ptype not in {"read", "select"}:
		return False
	context = get_access_context(user)
	if not _medical_record_review_scope_permission(doc, context.user):
		return False
	if context.has_global_clinical_read or "IONE Auditor" in context.roles:
		return True
	if "IONE Medical Record Coder" in context.roles and (
		str(getattr(doc, "assigned_coder", None) or "") == context.user
		or (
			str(getattr(doc, "status", None) or "") == "Coder Review"
			and not getattr(doc, "assigned_coder", None)
		)
	):
		return True
	return bool(
		"IONE Medical Record Expert Reviewer" in context.roles
		and (
			str(getattr(doc, "assigned_expert", None) or "") == context.user
			or (
				str(getattr(doc, "status", None) or "") == "Expert Review"
				and not getattr(doc, "assigned_expert", None)
			)
		)
	)


def medical_record_review_decision_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	if ptype not in {"read", "select"}:
		return False
	context = get_access_context(user)
	if not _medical_record_review_scope_permission(doc, context.user):
		return False
	if context.has_global_clinical_read or "IONE Auditor" in context.roles:
		return True
	return bool(
		context.roles.intersection(MEDICAL_RECORD_REVIEW_ACTOR_ROLES)
		and str(getattr(doc, "reviewed_by", None) or "") == context.user
	)


def medical_record_archive_decision_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	return _medical_record_linked_assignment_permission(doc, ptype, user)


def medical_record_archive_acknowledgement_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	return _medical_record_linked_assignment_permission(doc, ptype, user)


def medical_record_archive_delivery_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	if ptype not in {"read", "select"}:
		return False
	context = get_access_context(user)
	if not context.roles.intersection(
		{
			"IONE Integration Operator",
			"IONE Integration Administrator",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
			"IONE Auditor",
		}
	):
		return False
	return _clinical_permission(doc, ptype, context.user)


def medical_record_completeness_watermark_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	if ptype not in {"read", "select"}:
		return False
	context = get_access_context(user)
	if not context.roles.intersection(
		{"IONE Integration Operator", "IONE Integration Administrator", "IONE Auditor"}
	):
		return False
	return _clinical_permission(doc, ptype, context.user)


def medical_staff_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	context = get_access_context(user)
	if not _scope_document_is_consistent(doc):
		return False
	if context.user == "Administrator":
		return False
	if ptype not in {"read", "select"}:
		return bool(context.roles.intersection({"System Manager", "IONE QC Administrator"}))
	if context.roles.intersection({"System Manager", "IONE QC Administrator", "IONE Auditor"}):
		return True
	if context.has_department_clinical_scope:
		return bool(
			getattr(doc, "department", None)
			and getattr(doc, "department", None) in _authorized_departments(context, "IONE Medical Staff")
		)
	return bool(context.has_personal_clinical_scope and getattr(doc, "user", None) == context.user)


def ai_task_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	context = get_access_context(user)
	if context.is_administrator:
		return False
	if not _scope_document_is_consistent(doc):
		return False
	doctype = str(getattr(doc, "doctype", "") or "")
	if doctype in {"IONE AI Candidate Finding", "IONE AI Report Draft"} and ptype not in {
		"read",
		"select",
	}:
		return bool(
			getattr(frappe.flags, "ione_ai_artifact_review", None) == f"{doctype}:{getattr(doc, 'name', '')}"
		)
	if "IONE Auditor" in context.roles:
		return ptype in {"read", "select"} and _clinical_permission(doc, ptype, context.user)
	if context.roles.intersection({"IONE Agent Administrator", "IONE QC Reviewer", "IONE Medical Affairs"}):
		return ptype in {"read", "select"}
	if context.has_ai_review_scope:
		return ptype in {"read", "select"} and _clinical_permission(
			doc,
			ptype,
			context.user,
			include_ai_review=True,
		)
	if getattr(doc, "requested_by", None) == context.user and ptype in {"read", "select"}:
		return True
	if "IONE Agent Service" in context.roles and getattr(doc, "owner", None) == context.user:
		return ptype not in {"delete", "cancel"}
	return _clinical_permission(doc, ptype, user)


def tool_approval_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	return ai_task_permission(doc, ptype, user)


def ai_data_access_log_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	context = get_access_context(user)
	if context.is_administrator or ptype not in {"read", "select"}:
		return False
	if not _scope_document_is_consistent(doc):
		return False
	if context.roles.intersection({"IONE Agent Administrator", "IONE QC Reviewer", "IONE Medical Affairs"}):
		return True
	if "IONE Auditor" in context.roles:
		return _clinical_permission(doc, ptype, context.user)
	if context.has_ai_review_scope:
		return _clinical_permission(
			doc,
			ptype,
			context.user,
			include_ai_review=True,
		)
	return False


def ai_execution_event_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	return ptype in {"read", "select"} and ai_task_permission(doc, ptype, user)


def flow_run_link_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	return ptype in {"read", "select"} and ai_task_permission(doc, ptype, user)


def data_export_request_permission(
	doc,
	ptype: str = "read",
	user: str | None = None,
) -> bool:
	context = get_access_context(user)
	if context.user == "Administrator" or "IONE Agent Service" in context.roles:
		return False
	if ptype == "create":
		return bool(context.roles.intersection(EXPORT_REQUESTER_ROLES))
	if not _scope_document_is_consistent(doc):
		return False
	if ptype not in {"read", "select"}:
		return False
	if context.roles.intersection(EXPORT_GLOBAL_APPROVER_ROLES):
		return True
	if EXPORT_SCOPED_APPROVER_ROLE in context.roles and auditor_can_access_export_request(doc, context.user):
		return True
	return bool(
		context.roles.intersection(EXPORT_REQUESTER_ROLES)
		and getattr(doc, "requested_by", None) == context.user
	)


def flow_agent_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	if not str(getattr(doc, "title", None) or getattr(doc, "name", "")).startswith("IONE "):
		return True
	context = get_access_context(user)
	if context.is_administrator or context.roles.intersection(
		{"IONE Agent Administrator", "IONE QC Reviewer", "IONE Medical Affairs", "IONE Auditor"}
	):
		return ptype in {"read", "select"} or "IONE Agent Administrator" in context.roles
	if ptype not in {"read", "select"} or "IONE Agent Service" not in context.roles:
		return False
	task_name = str(getattr(frappe.flags, "ione_ai_task", None) or "")
	policy_name = str(getattr(frappe.flags, "ione_ai_policy", None) or "")
	if not task_name or not policy_name:
		return False
	return bool(
		frappe.db.exists(
			"IONE Agent Policy",
			{
				"name": policy_name,
				"flow_agent": doc.name,
				"service_user": context.user,
				"status": "Active",
			},
		)
		and frappe.db.exists(
			"IONE AI Analysis Task",
			{"name": task_name, "policy": policy_name},
		)
	)


def flow_model_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	if not _flow_model_is_governed(str(getattr(doc, "name", "") or "")):
		return True
	context = get_access_context(user)
	if ptype not in {"read", "select"} or "IONE Agent Service" not in context.roles:
		return False
	task_name, policy_name = _active_ai_runtime_names()
	if not task_name or not policy_name:
		return False
	return bool(
		frappe.db.exists(
			"IONE AI Analysis Task",
			{"name": task_name, "policy": policy_name},
		)
		and frappe.db.exists(
			"IONE Agent Policy",
			{
				"name": policy_name,
				"service_user": context.user,
				"status": "Active",
			},
		)
		and frappe.db.exists(
			"Flow Agent",
			{
				"name": frappe.db.get_value("IONE Agent Policy", policy_name, "flow_agent"),
				"model": doc.name,
			},
		)
	)


def flow_run_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	if not _flow_run_is_governed(doc):
		return True
	if ptype in {"delete", "cancel", "amend", "share", "export", "print", "email"}:
		return False
	context = get_access_context(user)
	if "IONE Agent Service" not in context.roles or getattr(doc, "owner", None) != context.user:
		return False
	task_name, policy_name = _active_ai_runtime_names()
	if not task_name or not policy_name:
		return False
	session = frappe.db.get_value("Flow Session", doc.get("session"), ["agent"], as_dict=True)
	task = frappe.db.get_value(
		"IONE AI Analysis Task",
		task_name,
		["policy", "flow_run", "flow_session"],
		as_dict=True,
	)
	return bool(
		session
		and task
		and task.get("policy") == policy_name
		and task.get("flow_session") in {None, "", doc.get("session")}
		and (
			task.get("flow_run") == doc.name
			or (
				doc.get("reference_doctype") == "IONE AI Analysis Task"
				and doc.get("reference_name") == task_name
			)
		)
		and frappe.db.exists(
			"IONE Agent Policy",
			{
				"name": policy_name,
				"flow_agent": session.get("agent"),
				"service_user": context.user,
				"status": "Active",
			},
		)
	)


def flow_session_permission(doc, ptype: str = "read", user: str | None = None) -> bool:
	if not _flow_session_is_governed(doc):
		return True
	if ptype in {"delete", "cancel", "amend", "share", "export", "print", "email"}:
		return False
	context = get_access_context(user)
	if "IONE Agent Service" not in context.roles or getattr(doc, "owner", None) != context.user:
		return False
	task_name, policy_name = _active_ai_runtime_names()
	if not task_name or not policy_name:
		return False
	task = frappe.db.get_value(
		"IONE AI Analysis Task",
		task_name,
		["policy", "flow_session"],
		as_dict=True,
	)
	return bool(
		task
		and task.get("policy") == policy_name
		and (
			task.get("flow_session") == doc.name
			or frappe.db.exists(
				"Flow Run",
				{
					"session": doc.name,
					"reference_doctype": "IONE AI Analysis Task",
					"reference_name": task_name,
				},
			)
		)
		and frappe.db.exists(
			"IONE Agent Policy",
			{
				"name": policy_name,
				"flow_agent": doc.get("agent"),
				"service_user": context.user,
				"status": "Active",
			},
		)
	)


def _medical_record_assignment_actor_condition(context: AccessContext) -> str:
	if context.is_administrator or "IONE Agent Service" in context.roles:
		return "1=0"
	if context.has_global_clinical_read or "IONE Auditor" in context.roles:
		return "1=1"
	table = "`tabIONE Medical Record Review Assignment`"
	clauses: list[str] = []
	user = frappe.db.escape(context.user)
	if "IONE Medical Record Coder" in context.roles:
		clauses.append(
			f"({table}.assigned_coder = {user} or "
			f"({table}.status = 'Coder Review' and coalesce({table}.assigned_coder, '') = ''))"
		)
	if "IONE Medical Record Expert Reviewer" in context.roles:
		clauses.append(
			f"({table}.assigned_expert = {user} or "
			f"({table}.status = 'Expert Review' and coalesce({table}.assigned_expert, '') = ''))"
		)
	return f"({' or '.join(clauses)})" if clauses else "1=0"


def _medical_record_linked_assignment_query(
	doctype: str,
	user: str | None,
) -> str:
	assignment_condition = medical_record_review_assignment_query(user)
	if assignment_condition == "1=0":
		return "1=0"
	table = _table_name(doctype)
	return (
		"exists (select 1 from `tabIONE Medical Record Review Assignment` "  # noqa: S608
		f"where `tabIONE Medical Record Review Assignment`.name = {table}.assignment "
		f"and ({assignment_condition}))"
	)


def _combine_read_conditions(base: str, narrowed: str) -> str:
	if base == "1=0" or narrowed == "1=0":
		return "1=0"
	if not base or base == "1=1":
		return narrowed
	if not narrowed or narrowed == "1=1":
		return base
	return f"({base}) and ({narrowed})"


def _medical_record_review_scope_permission(doc, user: str | None) -> bool:
	context = get_access_context(user)
	if (
		context.is_administrator
		or "IONE Agent Service" in context.roles
		or not context.roles.intersection(
			MEDICAL_RECORD_REVIEW_ACTOR_ROLES | GLOBAL_CLINICAL_READ_ROLES | frozenset({"IONE Auditor"})
		)
	):
		return False
	return _clinical_permission(doc, "read", context.user)


def _medical_record_linked_assignment_permission(
	doc,
	ptype: str,
	user: str | None,
) -> bool:
	if ptype not in {"read", "select"}:
		return False
	assignment_name = str(getattr(doc, "assignment", None) or "")
	if not assignment_name:
		return False
	try:
		assignment = frappe.get_doc("IONE Medical Record Review Assignment", assignment_name)
	except frappe.DoesNotExistError:
		return False
	return medical_record_review_assignment_permission(assignment, "read", user)


def _quality_governance_query(
	doctype: str,
	user: str | None,
	allowed_roles: frozenset[str],
) -> str:
	context = get_access_context(user)
	if (
		context.user in {"Guest", "Administrator"}
		or "IONE Agent Service" in context.roles
		or not context.roles.intersection(allowed_roles)
	):
		return "1=0"
	return _clinical_condition(doctype, context.user, include_personal=True)


def _quality_authorable_permission(
	doc,
	ptype: str,
	user: str | None,
	*,
	author_roles: frozenset[str],
	read_roles: frozenset[str],
) -> bool:
	context = get_access_context(user)
	if (
		context.user in {"Guest", "Administrator"}
		or "IONE Agent Service" in context.roles
		or not context.roles.intersection(read_roles)
	):
		return False
	if ptype == "create":
		# Scope and configured-approver validation run before insert. Allowing the
		# empty new-form shell here does not authorize an out-of-scope record.
		return bool(context.roles.intersection(author_roles))
	if ptype in {"write", "save"}:
		return bool(
			str(getattr(doc, "status", None) or "Draft") == "Draft"
			and str(getattr(doc, "owner", None) or "") == context.user
			and context.roles.intersection(author_roles)
			and _clinical_permission(
				doc,
				"write",
				context.user,
				include_personal=True,
			)
		)
	if ptype not in {"read", "select"}:
		return False
	return _clinical_permission(
		doc,
		ptype,
		context.user,
		include_personal=True,
	)


def _quality_readonly_permission(
	doc,
	ptype: str,
	user: str | None,
	read_roles: frozenset[str],
) -> bool:
	context = get_access_context(user)
	if (
		ptype not in {"read", "select"}
		or context.user in {"Guest", "Administrator"}
		or "IONE Agent Service" in context.roles
		or not context.roles.intersection(read_roles)
	):
		return False
	return _clinical_permission(
		doc,
		ptype,
		context.user,
		include_personal=True,
	)


def _clinical_condition(
	doctype: str,
	user: str | None = None,
	*,
	include_personal: bool = False,
) -> str:
	context = get_access_context(user)
	if context.is_administrator:
		return "1=0"
	table = _table_name(doctype)
	integrity = scope_integrity_sql(doctype, table)
	if context.has_global_clinical_read:
		return "" if integrity == "1=1" else integrity
	if (
		context.has_personal_clinical_scope
		and not context.staff_records
		and not context.has_department_clinical_scope
		and not context.has_explicit_clinical_scope
	):
		# A personal clinical role is activated by an authoritative Medical
		# Staff mapping. Without it, even a matching user-valued field must not
		# become an alternate route into clinical records.
		return "1=0"
	clauses = _scope_clauses(doctype, table, context, include_personal=include_personal)
	if not clauses:
		return "1=0"
	scoped = "(" + " or ".join(clauses) + ")"
	return scoped if integrity == "1=1" else f"({integrity}) and {scoped}"


def _combine_required_conditions(left: str, right: str) -> str:
	if left == "1=0":
		return left
	if not left:
		return right
	return f"({left}) and ({right})"


def _scope_clauses(
	doctype: str,
	table: str,
	context: AccessContext,
	*,
	include_ai_review: bool = False,
	include_personal: bool = False,
) -> list[str]:
	clauses: list[str] = []
	departments = _authorized_departments(
		context,
		doctype,
		include_personal=include_personal,
		include_ai_review=include_ai_review,
	)
	hospitals = _authorized_hospitals(
		context,
		doctype,
		include_personal=include_personal,
		include_ai_review=include_ai_review,
	)
	campuses = _authorized_campuses(
		context,
		doctype,
		include_personal=include_personal,
		include_ai_review=include_ai_review,
	)
	wards = _authorized_wards(
		context,
		doctype,
		include_personal=include_personal,
		include_ai_review=include_ai_review,
	)
	if departments and _has_field(doctype, "department"):
		clauses.append(f"{table}.department in ({_sql_values(departments)})")
	if hospitals and _has_field(doctype, "hospital"):
		clauses.append(f"{table}.hospital in ({_sql_values(hospitals)})")
	if campuses and _has_field(doctype, "campus"):
		clauses.append(f"{table}.campus in ({_sql_values(campuses)})")
	if wards and _has_field(doctype, "ward"):
		clauses.append(f"{table}.ward in ({_sql_values(wards)})")
	if (
		context.has_personal_clinical_scope
		and context.staff_records
		and _has_field(doctype, "responsible_staff")
	):
		clauses.append(f"{table}.responsible_staff in ({_sql_values(context.staff_records)})")
	if context.has_personal_clinical_scope and context.staff_records and _has_field(doctype, "medical_staff"):
		clauses.append(f"{table}.medical_staff in ({_sql_values(context.staff_records)})")
	if context.has_personal_clinical_scope and _has_field(doctype, "assigned_to"):
		clauses.append(f"{table}.assigned_to = {frappe.db.escape(context.user)}")
	if context.has_personal_clinical_scope and _has_field(doctype, "project_owner"):
		clauses.append(f"{table}.project_owner = {frappe.db.escape(context.user)}")
	return clauses


def _clinical_permission(
	doc,
	ptype: str,
	user: str | None,
	*,
	include_ai_review: bool = False,
	include_personal: bool = False,
) -> bool:
	context = get_access_context(user)
	if context.is_administrator:
		return False
	if not _scope_document_is_consistent(doc):
		return False
	if ptype in {"read", "select"} and context.has_global_clinical_read:
		return True
	if ptype not in {"read", "select"} and context.has_global_clinical_write:
		return True
	if ptype in {"delete", "cancel", "amend"}:
		return False
	if (
		context.has_personal_clinical_scope
		and not context.staff_records
		and not context.has_department_clinical_scope
		and not context.has_explicit_clinical_scope
	):
		return False
	department = getattr(doc, "department", None)
	hospital = getattr(doc, "hospital", None)
	campus = getattr(doc, "campus", None)
	ward = getattr(doc, "ward", None)
	responsible_staff = getattr(doc, "responsible_staff", None)
	medical_staff = getattr(doc, "medical_staff", None)
	doctype = str(getattr(doc, "doctype", "") or "")
	departments = _authorized_departments(
		context,
		doctype,
		include_personal=include_personal,
		include_ai_review=include_ai_review,
	)
	hospitals = _authorized_hospitals(
		context,
		doctype,
		include_personal=include_personal,
		include_ai_review=include_ai_review,
	)
	campuses = _authorized_campuses(
		context,
		doctype,
		include_personal=include_personal,
		include_ai_review=include_ai_review,
	)
	wards = _authorized_wards(
		context,
		doctype,
		include_personal=include_personal,
		include_ai_review=include_ai_review,
	)
	return bool(
		(department and department in departments)
		or (hospital and hospital in hospitals)
		or (campus and campus in campuses)
		or (ward and ward in wards)
		or (
			context.has_personal_clinical_scope
			and responsible_staff
			and responsible_staff in context.staff_records
		)
		or (context.has_personal_clinical_scope and medical_staff and medical_staff in context.staff_records)
		or (context.has_personal_clinical_scope and getattr(doc, "assigned_to", None) == context.user)
		or (context.has_personal_clinical_scope and getattr(doc, "project_owner", None) == context.user)
	)


def _scope_document_is_consistent(doc) -> bool:
	if str(getattr(doc, "doctype", "") or "") not in CLINICAL_SCOPE_DOCTYPES:
		return True
	try:
		return not clinical_scope_errors(doc)
	except Exception:
		return False


def _staff_assignment_is_current_and_consistent(row, current_date) -> bool:
	try:
		if not getattr(row, "hospital", None):
			return False
		effective_from = getdate(row.effective_from) if row.effective_from else None
		effective_to = getdate(row.effective_to) if row.effective_to else None
		if effective_from and current_date < effective_from:
			return False
		if effective_to and current_date > effective_to:
			return False
		record = frappe._dict(
			{
				"doctype": "IONE Medical Staff",
				"name": row.name,
				"hospital": row.hospital,
				"campus": row.campus,
				"department": row.department,
				"ward": row.ward,
			}
		)
		if clinical_scope_errors(record):
			return False
		for doctype, name, status_field in (
			("IONE Hospital", row.hospital, "status"),
			("IONE Hospital Campus", row.campus, "status"),
			("IONE Medical Department", row.department, "status"),
			("IONE Ward", row.ward, "status"),
		):
			if name and frappe.db.get_value(doctype, name, status_field) != "Active":
				return False
		return True
	except Exception:
		return False


def _load_scope_grants(user: str) -> tuple[tuple[str, str, str], ...]:
	if user in {"", "Guest", "Administrator"}:
		return ()
	try:
		user_permissions = frappe.permissions.get_user_permissions(user)
	except Exception:
		return ()
	grants: set[tuple[str, str, str]] = set()
	for linked_doctype in (
		"IONE Hospital",
		"IONE Hospital Campus",
		"IONE Medical Department",
		"IONE Ward",
	):
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


def _granted_scope_values(
	context: AccessContext,
	linked_doctype: str,
	target_doctype: str | None,
) -> frozenset[str]:
	return frozenset(
		document
		for grant_doctype, document, applicable_for in context.scope_grants
		if grant_doctype == linked_doctype
		and (not applicable_for or (target_doctype and applicable_for == target_doctype))
	)


def _may_use_organizational_scope(
	context: AccessContext,
	*,
	include_personal: bool,
	include_ai_review: bool,
) -> bool:
	return bool(
		context.has_department_clinical_scope
		or context.has_explicit_clinical_scope
		or (include_personal and context.has_personal_clinical_scope)
		or (include_ai_review and context.has_ai_review_scope)
	)


def _authorized_departments(
	context: AccessContext,
	target_doctype: str | None,
	*,
	include_personal: bool = False,
	include_ai_review: bool = False,
) -> frozenset[str]:
	if not _may_use_organizational_scope(
		context,
		include_personal=include_personal,
		include_ai_review=include_ai_review,
	):
		return frozenset()
	values = set(_granted_scope_values(context, "IONE Medical Department", target_doctype))
	if (
		context.has_department_clinical_scope
		or (include_personal and context.has_personal_clinical_scope)
		or (include_ai_review and context.has_ai_review_scope)
	):
		values.update(context.departments)
	return frozenset(values)


def _authorized_campuses(
	context: AccessContext,
	target_doctype: str | None,
	*,
	include_personal: bool = False,
	include_ai_review: bool = False,
) -> frozenset[str]:
	if not _may_use_organizational_scope(
		context,
		include_personal=include_personal,
		include_ai_review=include_ai_review,
	):
		return frozenset()
	values = set(_granted_scope_values(context, "IONE Hospital Campus", target_doctype))
	if (
		context.has_department_clinical_scope
		or (include_personal and context.has_personal_clinical_scope)
		or (include_ai_review and context.has_ai_review_scope)
	):
		values.update(context.campuses)
	return frozenset(values)


def _authorized_hospitals(
	context: AccessContext,
	target_doctype: str | None,
	*,
	include_personal: bool = False,
	include_ai_review: bool = False,
) -> frozenset[str]:
	if not _may_use_organizational_scope(
		context,
		include_personal=include_personal,
		include_ai_review=include_ai_review,
	):
		return frozenset()
	values = set(_granted_scope_values(context, "IONE Hospital", target_doctype))
	if (
		context.has_department_clinical_scope
		or (include_personal and context.has_personal_clinical_scope)
		or (include_ai_review and context.has_ai_review_scope)
	):
		values.update(context.hospitals)
	return frozenset(values)


def _authorized_wards(
	context: AccessContext,
	target_doctype: str | None,
	*,
	include_personal: bool = False,
	include_ai_review: bool = False,
) -> frozenset[str]:
	if not _may_use_organizational_scope(
		context,
		include_personal=include_personal,
		include_ai_review=include_ai_review,
	):
		return frozenset()
	values = set(_granted_scope_values(context, "IONE Ward", target_doctype))
	if (
		context.has_department_clinical_scope
		or (include_personal and context.has_personal_clinical_scope)
		or (include_ai_review and context.has_ai_review_scope)
	):
		values.update(context.wards)
	return frozenset(values)


def _sql_values(values: frozenset[str]) -> str:
	return ", ".join(frappe.db.escape(value) for value in sorted(values))


def _table_name(doctype: str) -> str:
	return f"`tab{doctype.replace('`', '``')}`"


def _doctype_exists(doctype: str) -> bool:
	try:
		return bool(frappe.db.exists("DocType", doctype))
	except Exception:
		return False


def _has_field(doctype: str, fieldname: str) -> bool:
	try:
		return bool(frappe.get_meta(doctype).has_field(fieldname))
	except Exception:
		return False


def _flow_model_is_governed(model: str) -> bool:
	if not model:
		return False
	if model == QWEN_FLOW_MODEL:
		return True
	agents = frappe.get_all(
		"Flow Agent",
		filters={"model": model},
		pluck="name",
		limit_page_length=500,
	)
	return bool(
		agents
		and frappe.db.exists(
			"IONE Agent Policy",
			{"flow_agent": ["in", agents]},
		)
	)


def _flow_session_is_governed(doc) -> bool:
	agent = str(doc.get("agent") or "")
	model = str(doc.get("model") or "")
	return bool(
		(
			agent
			and (agent.startswith("IONE ") or frappe.db.exists("IONE Agent Policy", {"flow_agent": agent}))
		)
		or (model and _flow_model_is_governed(model))
	)


def _flow_run_is_governed(doc) -> bool:
	if doc.get("reference_doctype") == "IONE AI Analysis Task":
		return True
	if getattr(doc, "name", None) and frappe.db.exists(
		"IONE Flow Run Link",
		{"flow_run": doc.name},
	):
		return True
	session = frappe.get_doc("Flow Session", doc.get("session")) if doc.get("session") else None
	return bool(session and _flow_session_is_governed(session))


def _governed_flow_session_condition(table: str) -> str:
	return (
		f"({table}.model = {frappe.db.escape(QWEN_FLOW_MODEL)} "  # noqa: S608
		f"or {table}.agent like 'IONE %%' "
		"or exists (select 1 from `tabIONE Agent Policy` policy "
		f"where policy.flow_agent = {table}.agent) "
		"or exists (select 1 from `tabIONE Agent Policy` model_policy "
		"inner join `tabFlow Agent` model_agent on model_agent.name = model_policy.flow_agent "
		f"where model_agent.model = {table}.model))"
	)


def _governed_flow_run_condition(table: str) -> str:
	return (
		f"({table}.reference_doctype = 'IONE AI Analysis Task' "  # noqa: S608
		"or exists (select 1 from `tabIONE Flow Run Link` link "
		f"where link.flow_run = {table}.name) "
		"or exists (select 1 from `tabFlow Session` session "
		f"where session.name = {table}.session "
		f"and {_governed_flow_session_condition('session')}))"
	)


def _active_ai_runtime_names() -> tuple[str, str]:
	return (
		str(getattr(frappe.flags, "ione_ai_task", None) or ""),
		str(getattr(frappe.flags, "ione_ai_policy", None) or ""),
	)
