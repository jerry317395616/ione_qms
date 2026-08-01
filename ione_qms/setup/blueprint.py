from __future__ import annotations

import hashlib
import json
from typing import Any

from ione_qms.indicator_engine import validate_formula_schema
from ione_qms.rule_engine.evaluator import validate_rule_definition_schema

BLUEPRINT_VALIDATOR_NAMES = {
	"IONE QC Standard": "validate_standard",
	"IONE QC Standard Version": "validate_standard_version",
	"IONE QC Standard Clause": "validate_standard_clause",
	"IONE QC Indicator": "validate_indicator",
	"IONE QC Indicator Version": "validate_indicator_version",
	"IONE QC Rule": "validate_rule",
	"IONE QC Rule Version": "validate_rule_version",
}

NATIONAL_GOALS = (
	{
		"code": "NIT-2026-I",
		"name": "脑血管病急性期规范诊疗完成率",
		"topic": "卒中流程、关键时间点、检查与治疗完成情况",
		"numerator_event": "NationalGoalStrokeAcuteCareCompliant",
		"denominator_event": "NationalGoalStrokeAcuteCareEligible",
		"frequency": "Daily",
		"direction": "Higher is Better",
	},
	{
		"code": "NIT-2026-II",
		"name": "肿瘤治疗前临床分期评估率",
		"topic": "初治患者识别、分期记录、治疗前置校验",
		"numerator_event": "NationalGoalTumorStagingCompleted",
		"denominator_event": "NationalGoalTumorStagingEligible",
		"frequency": "Daily",
		"direction": "Higher is Better",
	},
	{
		"code": "NIT-2026-III",
		"name": "VTE 规范预防率",
		"topic": "风险/出血评估、预防措施匹配、信息化提醒",
		"numerator_event": "NationalGoalVTEPreventionCompliant",
		"denominator_event": "NationalGoalVTEPreventionEligible",
		"frequency": "Daily",
		"direction": "Higher is Better",
	},
	{
		"code": "NIT-2026-IV",
		"name": "感染性休克集束化治疗完成率",
		"topic": "1/3/6 小时关键措施与多部门监测",
		"numerator_event": "NationalGoalSepticShockBundleCompleted",
		"denominator_event": "NationalGoalSepticShockBundleEligible",
		"frequency": "Daily",
		"direction": "Higher is Better",
	},
	{
		"code": "NIT-2026-V",
		"name": "住院患者静脉输液使用率",
		"topic": "使用率、床日频次、液体量、品种和预警",
		"numerator_event": "NationalGoalInpatientIVInfusionDay",
		"denominator_event": "NationalGoalInpatientBedDay",
		"frequency": "Daily",
		"direction": "Lower is Better",
	},
	{
		"code": "NIT-2026-VI",
		"name": "医疗安全不良事件报告率",
		"topic": "主动报告、分级分类、近乎差错、原因分析",
		"numerator_event": "NationalGoalSafetyEventReported",
		"denominator_event": "NationalGoalDischargedEncounter",
		"frequency": "Monthly",
		"direction": "Higher is Better",
	},
	{
		"code": "NIT-2026-VII",
		"name": "四级手术术前多学科讨论完成率",
		"topic": "发起、邀请、讨论、记录、可追溯性",
		"numerator_event": "NationalGoalLevel4SurgeryMDTCompleted",
		"denominator_event": "NationalGoalLevel4SurgeryEligible",
		"frequency": "Daily",
		"direction": "Higher is Better",
	},
	{
		"code": "NIT-2026-VIII",
		"name": "关键诊疗行为记录完整率",
		"topic": "医嘱、病程、查房、讨论、同意书、核查表一致性",
		"numerator_event": "NationalGoalCriticalRecordComplete",
		"denominator_event": "NationalGoalCriticalRecordExpected",
		"frequency": "Daily",
		"direction": "Higher is Better",
	},
	{
		"code": "NIT-2026-IX",
		"name": "非计划重返手术室再手术率",
		"topic": "病例识别、原因分类、专项改进",
		"numerator_event": "NationalGoalUnplannedReturnToOR",
		"denominator_event": "NationalGoalSurgeryCompleted",
		"frequency": "Monthly",
		"direction": "Lower is Better",
	},
	{
		"code": "NIT-2026-X",
		"name": "检查检验结果互认率",
		"topic": "互认目录、互认记录、不互认原因、月度反馈",
		"numerator_event": "NationalGoalResultRecognized",
		"denominator_event": "NationalGoalResultRecognitionEligible",
		"frequency": "Monthly",
		"direction": "Higher is Better",
	},
)

CORE_RULE_TEMPLATES = (
	{
		"code": "IONE-QCR-MR-001",
		"name": "入院记录 24 小时完整性检查",
		"goal": "NIT-2026-VIII",
		"trigger": "MedicalRecordUpdated",
		"risk": "High",
		"condition": {
			"all": [
				{"field": "hours_after_admission", "operator": ">", "value": 24},
				{"field": "admission_record_status", "operator": "is_empty"},
			]
		},
	},
	{
		"code": "IONE-QCR-MR-002",
		"name": "首次病程记录时限检查",
		"goal": "NIT-2026-VIII",
		"trigger": "MedicalRecordUpdated",
		"risk": "High",
		"condition": {
			"all": [
				{"field": "first_progress_note_due", "operator": "=", "value": True},
				{"field": "first_progress_note_status", "operator": "is_empty"},
			]
		},
	},
	{
		"code": "IONE-QCR-VTE-001",
		"name": "VTE 入院 24 小时评估",
		"goal": "NIT-2026-III",
		"trigger": "EncounterAdmitted",
		"risk": "High",
		"condition": {
			"all": [
				{"field": "patient_age", "operator": ">=", "value": 18},
				{"field": "hours_after_admission", "operator": ">", "value": 24},
				{"field": "vte_assessment_status", "operator": "is_empty"},
			]
		},
		"exclusions": {
			"all": [
				{
					"field": "encounter_type",
					"operator": "in",
					"value": ["Day Care", "Observation"],
				}
			]
		},
	},
	{
		"code": "IONE-QCR-TUMOR-001",
		"name": "肿瘤初治前临床分期记录检查",
		"goal": "NIT-2026-II",
		"trigger": "TreatmentPlanned",
		"risk": "High",
		"condition": {
			"all": [
				{"field": "initial_tumor_treatment", "operator": "=", "value": True},
				{"field": "clinical_stage", "operator": "is_empty"},
			]
		},
	},
	{
		"code": "IONE-QCR-SEPSIS-001",
		"name": "感染性休克 6 小时集束化措施检查",
		"goal": "NIT-2026-IV",
		"trigger": "SepticShockBundleUpdated",
		"risk": "Critical",
		"condition": {
			"all": [
				{"field": "hours_after_recognition", "operator": ">", "value": 6},
				{"field": "bundle_completed", "operator": "=", "value": False},
			]
		},
	},
	{
		"code": "IONE-QCR-INFUSION-001",
		"name": "静脉输液适应证记录检查",
		"goal": "NIT-2026-V",
		"trigger": "IVInfusionOrdered",
		"risk": "Medium",
		"condition": {
			"all": [
				{"field": "inpatient", "operator": "=", "value": True},
				{"field": "infusion_indication", "operator": "is_empty"},
			]
		},
	},
	{
		"code": "IONE-QCR-SURGERY-001",
		"name": "四级手术术前 MDT 记录检查",
		"goal": "NIT-2026-VII",
		"trigger": "SurgeryScheduled",
		"risk": "Critical",
		"condition": {
			"all": [
				{"field": "surgery_level", "operator": "=", "value": "4"},
				{"field": "preoperative_mdt_record", "operator": "is_empty"},
			]
		},
	},
	{
		"code": "IONE-QCR-SURGERY-002",
		"name": "非计划重返手术室事件提醒",
		"goal": "NIT-2026-IX",
		"trigger": "SurgeryCompleted",
		"risk": "Critical",
		"action": "Alert Only",
		"condition": {
			"all": [
				{"field": "unplanned_return_to_or", "operator": "=", "value": True},
				{"field": "return_reason", "operator": "not_empty"},
			]
		},
	},
	{
		"code": "IONE-QCR-CRITICAL-001",
		"name": "危急值确认处置时限检查",
		"goal": "NIT-2026-VIII",
		"trigger": "CriticalResultUpdated",
		"risk": "Critical",
		"condition": {
			"all": [
				{"field": "critical_result", "operator": "=", "value": True},
				{"field": "acknowledgement_overdue", "operator": "=", "value": True},
			]
		},
	},
	{
		"code": "IONE-QCR-RECOGNITION-001",
		"name": "检查检验结果不互认原因检查",
		"goal": "NIT-2026-X",
		"trigger": "DiagnosticOrderCreated",
		"risk": "Medium",
		"condition": {
			"all": [
				{"field": "recognition_eligible", "operator": "=", "value": True},
				{"field": "result_recognized", "operator": "=", "value": False},
				{"field": "non_recognition_reason", "operator": "is_empty"},
			]
		},
	},
)


_BLUEPRINT_STANDARD_CODE = "NHC-QSA-2026"


def seed_quality_blueprint() -> dict[str, int]:
	"""Create disabled clinical templates only in an empty reserved namespace.

	The templates translate the ten 2026 national goals into versioned clauses,
	executable aggregate indicator definitions, and representative deterministic
	rules. All records remain Draft/Shadow until local mappings, replay tests, and
	clinical approval are complete. Upgrades never update an existing standard,
	indicator, or rule: any reserved-code collision skips the whole blueprint so
	hospital-maintained content cannot be silently attached to or overwritten.
	"""
	import frappe

	if not frappe.db.exists("DocType", "IONE QC Standard"):
		return {}
	collisions = _reserved_blueprint_collisions()
	if collisions:
		frappe.logger("ione_qms").warning(
			"Quality blueprint seed skipped because reserved records already exist: %s",
			", ".join(collisions),
		)
		return {
			"standards": 0,
			"clauses": 0,
			"indicators": 0,
			"rules": 0,
			"skipped_existing": len(collisions),
		}
	standard = _insert_reserved_blueprint(
		"IONE QC Standard",
		{"standard_code": _BLUEPRINT_STANDARD_CODE},
		{
			"standard_code": _BLUEPRINT_STANDARD_CODE,
			"standard_name": "2026 年国家医疗质量安全改进目标",
			"standard_category": "国家医疗质量安全改进目标",
			"source_type": "National Policy",
			"status": "Draft",
			"responsible_department": "医务/质量管理部门",
			"issue_date": "2026-03-01",
			"source_url": (
				"https://www.nhc.gov.cn/yzygj/c100068/202603/"
				"9f642951b99c447f8cff9da8abb74dc3/files/"
				"1.2026年国家医疗质量安全改进目标.pdf"
			),
			"description": (
				"安装时创建的受治理模板。源文件、医院口径、源系统映射、历史回放和临床审批完成前不得发布。"
			),
		},
	)
	version = _insert_reserved_blueprint(
		"IONE QC Standard Version",
		{"standard": standard, "version": "2026.1"},
		{
			"standard": standard,
			"version": "2026.1",
			"approval_status": "Draft",
			"effective_from": "2026-01-01",
			"scope_json": _canonical_json(
				{
					"template": True,
					"requires_hospital_validation": True,
					"goals": [goal["code"] for goal in NATIONAL_GOALS],
				}
			),
		},
	)

	clauses: dict[str, str] = {}
	indicators = 0
	for goal in NATIONAL_GOALS:
		clause = _insert_reserved_blueprint(
			"IONE QC Standard Clause",
			{"standard_version": version, "clause_code": goal["code"]},
			{
				"clause_key": f"NHC-QSA-2026:2026.1:{goal['code']}",
				"standard_version": version,
				"clause_code": goal["code"],
				"heading": goal["name"],
				"content": goal["topic"],
				"status": "Draft",
				"keywords": goal["topic"].replace("、", ","),
			},
		)
		clauses[str(goal["code"])] = clause
		formula = _event_rate_formula(
			str(goal["numerator_event"]),
			str(goal["denominator_event"]),
		)
		validate_formula_schema(formula)
		indicator = _insert_reserved_blueprint(
			"IONE QC Indicator",
			{"indicator_code": goal["code"]},
			{
				"indicator_code": goal["code"],
				"indicator_name": goal["name"],
				"category": "2026 National Quality Improvement Goal",
				"standard": standard,
				"standard_clause": clause,
				"definition": (
					f"{goal['topic']}。基于规范化事件 {goal['numerator_event']} / "
					f"{goal['denominator_event']} 计算; 上线前必须完成医院口径和源映射验证。"
				),
				"calculator_key": "IONE_RECORD_AGGREGATE",
				"formula_json": _canonical_json(formula),
				"direction": goal["direction"],
				"status": "Draft",
			},
		)
		_insert_reserved_blueprint(
			"IONE QC Indicator Version",
			{"indicator": indicator, "version": "2026.1"},
			{
				"indicator": indicator,
				"version": "2026.1",
				"status": "Draft",
				"calculator_key": "IONE_RECORD_AGGREGATE",
				"numerator_definition": (f"经医院映射审核后产生的 {goal['numerator_event']} 去重事件数。"),
				"denominator_definition": (
					f"经医院映射审核后产生的 {goal['denominator_event']} 去重事件数。"
				),
				"formula_json": _canonical_json(formula),
				"dimensions_json": _canonical_json(formula["dimensions"]),
				"calculation_frequency": goal["frequency"],
				"unit": "%",
				"multiplier": 100,
				"precision": 2,
				"direction": goal["direction"],
				"effective_from": "2026-01-01",
			},
		)
		indicators += 1

	rules = 0
	for template in CORE_RULE_TEMPLATES:
		condition = template["condition"]
		exclusions = template.get("exclusions")
		validate_rule_definition_schema(condition)
		if exclusions:
			validate_rule_definition_schema(exclusions)
		clause = clauses[str(template["goal"])]
		rule = _insert_reserved_blueprint(
			"IONE QC Rule",
			{"rule_code": template["code"]},
			{
				"rule_code": template["code"],
				"rule_name": template["name"],
				"rule_type": "Deterministic",
				"standard": standard,
				"standard_clause": clause,
				"category": "National Goal Draft Template",
				"risk_level": template["risk"],
				"status": "Draft",
				"description": (
					"代码审查后的确定性规则模板; 医院字段映射、排除条件、回放测试和临床审批"
					"完成前保持 Draft/Shadow。"
				),
			},
		)
		_insert_reserved_blueprint(
			"IONE QC Rule Version",
			{"rule": rule, "version": "2026.1"},
			{
				"rule": rule,
				"version": "2026.1",
				"trigger_event": template["trigger"],
				"condition_json": _canonical_json(condition),
				"exclusion_json": _canonical_json(exclusions) if exclusions else None,
				"action": template.get("action", "Create Finding"),
				"severity": template["risk"],
				"status": "Draft",
				"shadow_mode": 1,
				"effective_from": "2026-01-01",
			},
		)
		rules += 1
	return {"standards": 1, "clauses": len(clauses), "indicators": indicators, "rules": rules}


def _reserved_blueprint_collisions() -> list[str]:
	"""Return existing reserved parent records without reading or mutating content."""
	import frappe

	reserved = [
		("IONE QC Standard", "standard_code", _BLUEPRINT_STANDARD_CODE),
		*(("IONE QC Indicator", "indicator_code", str(goal["code"])) for goal in NATIONAL_GOALS),
		*(("IONE QC Rule", "rule_code", str(template["code"])) for template in CORE_RULE_TEMPLATES),
	]
	return [
		f"{doctype}:{code}"
		for doctype, fieldname, code in reserved
		if frappe.db.exists(doctype, {fieldname: code})
	]


def _event_rate_formula(numerator_event: str, denominator_event: str) -> dict[str, Any]:
	return {
		"measure": "Rate",
		"numerator": {
			"dataset": "clinical_events",
			"date_field": "event_time",
			"filters": {"event_type": numerator_event},
		},
		"denominator": {
			"dataset": "clinical_events",
			"date_field": "event_time",
			"filters": {"event_type": denominator_event},
		},
		"multiplier": 100,
		"precision": 2,
		"dimensions": ["campus", "department", "hospital", "physician", "ward"],
	}


def _insert_reserved_blueprint(
	doctype: str,
	filters: dict[str, Any],
	values: dict[str, Any],
) -> str:
	"""Insert one template and fail closed if a concurrent collision appears."""
	import frappe

	name = frappe.db.get_value(doctype, filters, "name")
	if name:
		frappe.throw(
			f"Reserved quality blueprint record appeared during seed: {doctype}:{name}. "
			"No existing record was overwritten."
		)
	doc = frappe.get_doc({"doctype": doctype, **values})
	_validate_new_blueprint_document(doc)
	doc.insert(ignore_permissions=True)
	return doc.name


def _validate_new_blueprint_document(doc) -> None:
	"""Prime mandatory governed fields when install-time doc_events are not cached yet."""
	import frappe

	from ione_qms.services import versions

	validator_name = BLUEPRINT_VALIDATOR_NAMES.get(str(doc.doctype))
	if not validator_name:
		frappe.throw(f"Blueprint DocType is missing a governed validator: {doc.doctype}")
	getattr(versions, validator_name)(doc)


def _canonical_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)


def blueprint_checksum() -> str:
	"""Stable checksum used by release evidence and static tests."""
	payload = {
		"goals": NATIONAL_GOALS,
		"rules": CORE_RULE_TEMPLATES,
	}
	return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()
