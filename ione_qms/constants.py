from __future__ import annotations

APP_ROLES = frozenset(
	{
		"IONE QC Administrator",
		"IONE QC Reviewer",
		"IONE Medical Affairs",
		"IONE Department Director",
		"IONE Department QC Officer",
		"IONE Physician",
		"IONE Nursing/Pharmacy/IC QC",
		"IONE Agent Administrator",
		"IONE Agent Reviewer",
		"IONE Integration Administrator",
		"IONE Integration Operator",
		"IONE Auditor",
		"IONE Agent Service",
		"IONE Medical Record Coder",
		"IONE Medical Record Expert Reviewer",
		"IONE PHI Identity Reader",
	}
)

GLOBAL_CLINICAL_READ_ROLES = frozenset(
	{
		"IONE QC Reviewer",
		"IONE Medical Affairs",
	}
)

GLOBAL_CLINICAL_WRITE_ROLES = frozenset(
	{
		"IONE QC Reviewer",
		"IONE Medical Affairs",
	}
)

DEPARTMENT_SCOPED_ROLES = frozenset(
	{
		"IONE Department Director",
		"IONE Department QC Officer",
		"IONE Nursing/Pharmacy/IC QC",
		"IONE Medical Record Coder",
		"IONE Medical Record Expert Reviewer",
	}
)

PERSONAL_CLINICAL_ROLES = frozenset({"IONE Physician"})

EXPLICIT_CLINICAL_SCOPE_ROLES = frozenset({"IONE Auditor", "IONE Integration Operator"})

AI_REVIEW_SCOPED_ROLES = frozenset({"IONE Agent Reviewer"})

QUALITY_MEETING_AUTHOR_ROLES = frozenset(
	{
		"IONE QC Reviewer",
		"IONE Medical Affairs",
		"IONE Department Director",
		"IONE Department QC Officer",
	}
)

QUALITY_MEETING_APPROVER_ROLES = frozenset({"IONE QC Reviewer", "IONE Medical Affairs"})

QUALITY_ACTION_OWNER_ROLES = frozenset(
	{
		"IONE Physician",
		"IONE Department Director",
		"IONE Department QC Officer",
		"IONE Nursing/Pharmacy/IC QC",
		"IONE QC Reviewer",
		"IONE Medical Affairs",
	}
)

QUALITY_ACTION_OVERSIGHT_ROLES = frozenset(
	{
		"IONE Department Director",
		"IONE Department QC Officer",
		"IONE QC Reviewer",
		"IONE Medical Affairs",
		"IONE Auditor",
	}
)

AGENT_FORBIDDEN_TOOL_SLUGS = frozenset(
	{
		"create",
		"update",
		"delete",
		"run_action",
		"execute",
	}
)

AGENT_WRITE_TOOL_SLUGS = frozenset(
	{
		"ione_create_analysis_draft",
		"ione_create_candidate_finding",
		"ione_create_report_draft",
	}
)

QWEN_FLOW_MODEL = "I-ONE Qwen 35B"
QWEN_MODEL_ID = "openai/qwen3.6-35b-a3b-fp8"

FINDING_TERMINAL_STATES = frozenset({"Closed", "Appeal Approved"})
FINDING_CONFIRMED_STATES = frozenset(
	{
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

RULE_RESULTS = frozenset({"Passed", "Failed", "Excluded", "Insufficient Data", "Error"})

SENSITIVE_FIELD_NAMES = frozenset(
	{
		"patient_name",
		"date_of_birth",
		"source_patient_id",
		"source_encounter_id",
		"source_record_id",
		"encounter_no",
		"patient_reference",
		"encounter_reference",
		"identification_hash",
		"phone_hash",
		"identification_number",
		"phone",
		"address",
		"medical_record_text",
		"evidence_text",
		"input_payload",
		"output_payload",
	}
)
