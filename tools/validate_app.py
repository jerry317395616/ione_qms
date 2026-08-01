from __future__ import annotations

import ast
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
	sys.path.insert(0, str(ROOT))

from ione_qms.privacy_registry import PROTECTED_FIELDS_BY_DOCTYPE  # noqa: E402

PACKAGE = ROOT / "ione_qms"

DOCTYPE_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]*$")
FIELDNAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
FORMAT_VARIABLE_PATTERN = re.compile(r"\{([^{}]+)\}")

FIELD_TYPES = frozenset(
	{
		"Attach",
		"Attach Image",
		"Attachment Gallery",
		"Autocomplete",
		"Barcode",
		"Button",
		"Check",
		"Code",
		"Color",
		"Column Break",
		"Currency",
		"Data",
		"Date",
		"Datetime",
		"Duration",
		"Dynamic Link",
		"Float",
		"Fold",
		"Geolocation",
		"Heading",
		"HTML",
		"HTML Editor",
		"Icon",
		"Image",
		"Int",
		"JSON",
		"Link",
		"Long Int",
		"Long Text",
		"Markdown Editor",
		"Password",
		"Percent",
		"Phone",
		"Rating",
		"Read Only",
		"Section Break",
		"Select",
		"Signature",
		"Small Text",
		"Tab Break",
		"Table",
		"Table MultiSelect",
		"Text",
		"Text Editor",
		"Time",
	}
)
NO_VALUE_FIELD_TYPES = frozenset(
	{
		"Attachment Gallery",
		"Button",
		"Column Break",
		"Fold",
		"Heading",
		"HTML",
		"Image",
		"Section Break",
		"Tab Break",
		"Table",
		"Table MultiSelect",
	}
)
TEXT_FIELD_TYPES = frozenset({"Code", "Long Text", "Small Text", "Text", "Text Editor"})
UNIQUE_FIELD_TYPES = frozenset({"Data", "Int", "Link", "Read Only"})
EXTERNAL_LINK_TARGETS = frozenset(
	{"DocType", "File", "Flow Agent", "Flow Model", "Flow Run", "Flow Session", "Flow Tool", "User"}
)
RESERVED_FIELDNAMES = frozenset(
	{
		"_doc_before_save",
		"_non_computed_table_fieldnames",
		"_parent_doc",
		"_table_fieldnames",
		"_weakref",
		"autoname",
		"doctype",
		"dont_update_if_missing",
		"flags",
		"name",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"docstatus",
		"idx",
		"parent",
		"parentfield",
		"parenttype",
		"meta",
		"permitted_fieldnames",
	}
)
APP_ROLES = frozenset(
	{
		"System Manager",
		"IONE Agent Administrator",
		"IONE Agent Reviewer",
		"IONE Agent Service",
		"IONE QMS Auditor",
		"IONE Department Director",
		"IONE Department QC Officer",
		"IONE Integration Administrator",
		"IONE Integration Operator",
		"IONE Medical Affairs",
		"IONE QMS Medical Record Coder",
		"IONE Medical Record Expert Reviewer",
		"IONE Nursing/Pharmacy/IC QC",
		"IONE PHI Identity Reader",
		"IONE Physician",
		"IONE QC Administrator",
		"IONE QC Reviewer",
	}
)

REQUIRED_DOCTYPES_BY_MODULE = {
	"IONE Foundation": {
		"IONE Diagnosis Procedure Mapping",
		"IONE Encounter Index",
		"IONE Hospital",
		"IONE Hospital Campus",
		"IONE Medical Department",
		"IONE Medical Staff",
		"IONE Patient Index",
		"IONE Staff Qualification",
		"IONE Surgery Authorization",
		"IONE Surgery Procedure Policy",
		"IONE Ward",
	},
	"IONE Quality Standards": {
		"IONE Definition Lineage Receipt",
		"IONE QC Rule",
		"IONE QC Rule Shadow Execution",
		"IONE QC Rule Shadow Feedback",
		"IONE QC Rule Test Case",
		"IONE QC Rule Test Execution",
		"IONE QC Rule Validation Run",
		"IONE QC Rule Version",
		"IONE QC Standard",
		"IONE QC Standard Clause",
		"IONE QC Standard Version",
	},
	"IONE Clinical Quality": {
		"IONE Clinical Quality Event",
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
		"IONE Medical Safety Event",
		"IONE QC Execution",
		"IONE QC Finding",
		"IONE QC Finding Evidence",
		"IONE Surgery QC",
		"IONE Surgery MDT Record",
		"IONE Surgery MDT Participant",
		"IONE Surgery Safety Checklist",
		"IONE Surgery Emergency Exception",
	},
	"IONE Indicators": {
		"IONE Indicator Alert",
		"IONE Indicator Calculation",
		"IONE Indicator Result",
		"IONE Indicator Result Detail",
		"IONE Indicator Result Pointer",
		"IONE Indicator Quarantine Receipt",
		"IONE Indicator Quarantine Disposition",
		"IONE Indicator Historical Audit Authorization",
		"IONE Indicator Historical Audit Access Receipt",
		"IONE QC Indicator",
		"IONE QC Indicator Version",
	},
	"IONE Improvement": {
		"IONE Finding Recurrence Evaluation",
		"IONE Finding Recurrence Policy",
		"IONE Finding Recurrence Run",
		"IONE Improvement Measure",
		"IONE Meeting Agenda Item",
		"IONE Meeting Attendee",
		"IONE Meeting Decision",
		"IONE Meeting Minute",
		"IONE Pareto Cause Item",
		"IONE PDCA Effect Measurement",
		"IONE PDCA Project",
		"IONE Quality Action Item",
		"IONE Quality Action Verification Round",
		"IONE Quality Experience Share",
		"IONE Quality Meeting",
		"IONE QC Finding Appeal",
		"IONE QC Finding Appeal Evidence",
		"IONE QC Rectification",
		"IONE QC Verification",
		"IONE Rectification Action",
		"IONE Root Cause",
	},
	"IONE Integration": {
		"IONE Data Quality Issue",
		"IONE Data Reconciliation",
		"IONE Integration Allowed Scope",
		"IONE Integration Endpoint",
		"IONE Integration Job",
		"IONE Integration Mapping",
		"IONE Integration Message",
		"IONE Source Document Access Log",
		"IONE Source Document Locator",
		"IONE Source System",
	},
	"IONE Flow AI": {
		"IONE AI Analysis Task",
		"IONE AI Candidate Finding",
		"IONE AI Data Access Log",
		"IONE AI Execution Event",
		"IONE AI Report Draft",
		"IONE AI Report Recovery Authorization",
		"IONE AI Report Schedule",
		"IONE AI Tool Approval",
		"IONE Agent Allowed Department",
		"IONE Agent Allowed Hospital",
		"IONE Agent Allowed Tool",
		"IONE Agent Evaluation",
		"IONE Agent Evaluation Threshold Policy",
		"IONE Agent Incident",
		"IONE Agent Policy",
		"IONE Agent Release",
		"IONE Agent Test Case",
		"IONE Flow Run Link",
		"IONE Quality Report Snapshot",
	},
	"IONE Analytics": {
		"IONE Agent Analysis Fact",
		"IONE Daily Quality Fact",
		"IONE Finding Analysis Fact",
		"IONE Monthly Quality Fact",
		"IONE Surgery Quality Fact",
	},
	"IONE Administration": {
		"IONE AI Settings",
		"IONE Batch Recovery Receipt",
		"IONE Batch Run",
		"IONE Batch Source Epoch",
		"IONE Batch Work Item",
		"IONE Data Export Request",
		"IONE Feature Flag",
		"IONE Integration Settings",
		"IONE Migration Completion Receipt",
		"IONE Migration State",
		"IONE PHI Access Receipt",
		"IONE PHI Disclosure Policy",
		"IONE Production Activation Event",
		"IONE Production Gate Evidence",
		"IONE Production Readiness Assessment",
		"IONE QC Settings",
		"IONE Release Record",
		"IONE Security Policy",
		"IONE System Settings",
	},
}
REQUIRED_DOCTYPES = frozenset().union(*REQUIRED_DOCTYPES_BY_MODULE.values())

CHILD_DOCTYPES = frozenset(
	{
		"IONE Agent Allowed Department",
		"IONE Agent Allowed Hospital",
		"IONE Agent Allowed Tool",
		"IONE Improvement Measure",
		"IONE Integration Allowed Scope",
		"IONE Meeting Agenda Item",
		"IONE Meeting Attendee",
		"IONE Pareto Cause Item",
		"IONE PDCA Effect Measurement",
		"IONE Production Gate Evidence",
		"IONE Rectification Action",
		"IONE Root Cause",
	}
)
SINGLE_DOCTYPES = frozenset(
	{
		"IONE AI Settings",
		"IONE Integration Settings",
		"IONE Migration State",
		"IONE QC Settings",
		"IONE System Settings",
	}
)
SAFETY_CRITICAL_DEFAULTS = {
	("IONE System Settings", "enable_realtime_rules"): "0",
	("IONE System Settings", "enable_ai"): "0",
	("IONE System Settings", "production_mode"): "0",
	("IONE AI Settings", "enabled"): "0",
}
AGENT_CATEGORIES = frozenset(
	{
		"Policy Governance",
		"Medical Record QC",
		"Indicator Analysis",
		"Rectification",
		"Quality Report",
		"Quality Analysis",
		"Report Draft",
		"Special Topic",
	}
)
EVALUATION_CATEGORIES = frozenset(
	{
		"accuracy",
		"recall",
		"false_positive",
		"evidence_consistency",
		"hallucination",
		"expert_adoption",
		"cross_scope_tool_overreach",
		"forbidden_tool",
		"pii_leakage",
		"missing_evidence",
	}
)
SENSITIVE_EXPORT_DOCTYPES = frozenset(
	{
		"IONE Medical Staff",
		"IONE Staff Qualification",
		"IONE Surgery Authorization",
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
		"IONE Indicator Result Detail",
		"IONE Indicator Calculation",
		"IONE Indicator Result Pointer",
		"IONE Indicator Quarantine Receipt",
		"IONE Indicator Quarantine Disposition",
		"IONE Indicator Historical Audit Authorization",
		"IONE Indicator Historical Audit Access Receipt",
		"IONE Finding Analysis Fact",
		"IONE Surgery Quality Fact",
		"IONE Agent Analysis Fact",
		"IONE AI Analysis Task",
		"IONE AI Candidate Finding",
		"IONE AI Tool Approval",
		"IONE AI Report Draft",
		"IONE AI Report Recovery Authorization",
		"IONE AI Report Schedule",
		"IONE Quality Report Snapshot",
		"IONE Agent Release",
		"IONE Flow Run Link",
		"IONE AI Execution Event",
		"IONE AI Data Access Log",
		"IONE Agent Incident",
		"IONE Finding Recurrence Policy",
		"IONE Finding Recurrence Run",
		"IONE Finding Recurrence Evaluation",
		"IONE Quality Meeting",
		"IONE Meeting Minute",
		"IONE Meeting Decision",
		"IONE Quality Action Item",
		"IONE Quality Experience Share",
		"IONE PHI Access Receipt",
		"IONE PHI Disclosure Policy",
	}
)
PATIENT_BEARING_DOCTYPES = frozenset(
	{
		"IONE Patient Index",
		"IONE Encounter Index",
		"IONE Clinical Quality Event",
		"IONE QC Execution",
		"IONE QC Finding",
		"IONE QC Finding Evidence",
		"IONE QC Finding Appeal",
		"IONE QC Finding Appeal Evidence",
		"IONE Medical Record QC",
		"IONE Medical Record Review Assignment",
		"IONE Surgery QC",
		"IONE Surgery MDT Record",
		"IONE Surgery MDT Participant",
		"IONE Surgery Safety Checklist",
		"IONE Surgery Emergency Exception",
		"IONE Medical Safety Event",
		"IONE QC Rectification",
		"IONE QC Verification",
		"IONE Indicator Result Detail",
		"IONE AI Analysis Task",
		"IONE AI Candidate Finding",
		"IONE AI Tool Approval",
		"IONE AI Execution Event",
		"IONE AI Data Access Log",
		"IONE Agent Incident",
	}
)
PHI_IDENTITY_FIELDS = {
	"IONE Patient Index": {
		"source_patient_id",
		"patient_name",
		"date_of_birth",
		"identification_hash",
		"phone_hash",
	},
	"IONE Encounter Index": {"source_encounter_id", "encounter_no"},
}
SAFE_IDENTITY_TITLE_AND_SEARCH = {
	"IONE Patient Index": ("patient_key", {"patient_key"}),
	"IONE Encounter Index": ("encounter_key", {"encounter_key"}),
}

REQUIRED_FIELDS = {
	"IONE Migration State": {
		"schema_revision",
		"full_fingerprint",
		"status",
		"requested_at",
		"started_at",
		"last_batch_at",
		"completed_at",
		"batch_count",
		"continuation_sequence",
		"last_summary_json",
		"completion_receipt",
		"last_error_code",
		"indicator_verification_cursor",
		"indicator_verification_complete",
	},
	"IONE Migration Completion Receipt": {
		"migration_key",
		"schema_revision",
		"full_fingerprint",
		"app_version",
		"completed_at",
		"batch_count",
		"summary_json",
		"previous_receipt",
		"record_hash",
	},
	"IONE QC Standard": {
		"standard_code",
		"standard_name",
		"source_type",
		"source_url",
		"source_file",
		"status",
	},
	"IONE QC Standard Version": {
		"standard",
		"version",
		"standard_code_snapshot",
		"standard_name_snapshot",
		"source_type_snapshot",
		"source_url_snapshot",
		"source_file_snapshot",
		"source_file_hash",
		"lineage_status",
		"approval_status",
		"effective_from",
		"effective_to",
		"checksum",
	},
	"IONE QC Standard Clause": {
		"clause_key",
		"standard_version",
		"clause_code",
		"heading",
		"content",
		"status",
	},
	"IONE QC Rule": {
		"rule_code",
		"rule_name",
		"rule_type",
		"standard",
		"standard_clause",
		"risk_level",
		"status",
	},
	"IONE QC Rule Version": {
		"rule",
		"version",
		"rule_code_snapshot",
		"rule_name_snapshot",
		"rule_type_snapshot",
		"standard_snapshot",
		"standard_version_snapshot",
		"standard_clause_snapshot",
		"standard_version_checksum_snapshot",
		"standard_clause_hash_snapshot",
		"authority_snapshot_json",
		"authority_snapshot_hash",
		"lineage_status",
		"trigger_event",
		"condition_json",
		"action",
		"severity",
		"status",
		"shadow_mode",
		"shadow_observation_start",
		"shadow_observation_end",
		"shadow_min_events",
		"shadow_min_feedback",
		"shadow_max_false_positive_rate",
		"shadow_max_false_negative_rate",
		"shadow_min_feedback_coverage_rate",
		"shadow_min_passed_feedback",
		"shadow_min_excluded_feedback",
		"effective_from",
		"effective_to",
		"checksum",
	},
	"IONE QC Rule Test Case": {
		"rule_version",
		"case_name",
		"sample_type",
		"input_json",
		"expected_result",
		"actual_result",
		"test_status",
		"result_json",
		"definition_hash",
		"latest_execution_receipt",
	},
	"IONE Meeting Agenda Item": {
		"agenda_key",
		"subject",
		"objective",
		"presenter",
		"duration_minutes",
		"material_type",
		"ai_report_draft",
		"material_reference",
		"material_checksum",
	},
	"IONE Meeting Attendee": {
		"user",
		"participation_role",
		"required_attendee",
		"attendance_status",
		"joined_at",
		"left_at",
		"attendance_evidence",
		"absence_reason",
	},
	"IONE Quality Meeting": {
		"meeting_code",
		"title",
		"meeting_type",
		"hospital",
		"campus",
		"department",
		"ward",
		"scheduled_start",
		"scheduled_end",
		"chair",
		"secretary",
		"meeting_approver",
		"minute_approver",
		"agenda_items",
		"attendees",
		"status",
		"submitted_by",
		"submitted_at",
		"approved_by",
		"approved_at",
		"approval_comment",
		"agenda_checksum",
		"actual_start",
		"actual_end",
		"held_by",
		"held_at",
		"held_evidence_reference",
		"minutes_pending_by",
		"minutes_pending_at",
		"closed_by",
		"closed_at",
		"cancelled_by",
		"cancelled_at",
		"cancellation_reason",
	},
	"IONE Meeting Minute": {
		"meeting",
		"approved_meeting_key",
		"hospital",
		"campus",
		"department",
		"ward",
		"summary",
		"discussion_summary",
		"decisions_json",
		"no_decision_reason",
		"evidence_reference",
		"status",
		"submitted_by",
		"submitted_at",
		"reviewed_by",
		"reviewed_at",
		"review_comment",
		"checksum",
	},
	"IONE Meeting Decision": {
		"decision_key",
		"meeting",
		"meeting_minute",
		"decision_index",
		"decision_code",
		"decision_text",
		"rationale",
		"hospital",
		"campus",
		"department",
		"ward",
		"derived_by",
		"derived_at",
		"checksum",
	},
	"IONE Quality Action Item": {
		"meeting",
		"meeting_decision",
		"finding",
		"pdca_project",
		"safety_event",
		"hospital",
		"campus",
		"department",
		"ward",
		"title",
		"description",
		"assigned_to",
		"verifier",
		"due_date",
		"status",
		"created_by",
		"created_at",
		"started_by",
		"started_at",
		"current_submission_key",
		"current_submission_checksum",
		"verification_round_no",
		"latest_verification_round",
		"latest_verification_receipt_checksum",
		"completion_evidence",
		"effect_result",
		"submitted_for_verification_by",
		"submitted_for_verification_at",
		"verification_comment",
		"verified_by",
		"verified_at",
		"closed_by",
		"closed_at",
		"cancelled_by",
		"cancelled_at",
		"cancellation_reason",
	},
	"IONE Quality Action Verification Round": {
		"round_key",
		"action_item",
		"round_no",
		"hospital",
		"campus",
		"department",
		"ward",
		"assigned_to_snapshot",
		"verifier_snapshot",
		"completion_evidence_snapshot",
		"effect_result_snapshot",
		"completion_evidence_hash",
		"owner_submission_key",
		"owner_submission_checksum",
		"submitted_by",
		"submitted_at",
		"decision",
		"verification_comment",
		"verifier_decision_key",
		"verified_by",
		"verified_at",
		"action_status_after",
		"previous_receipt_checksum",
		"receipt_checksum",
	},
	"IONE Quality Experience Share": {
		"experience_code",
		"version_number",
		"version_key",
		"active_publication_key",
		"supersedes",
		"safety_event",
		"meeting_minute",
		"hospital",
		"campus",
		"department",
		"ward",
		"title",
		"content_json",
		"deidentification_method",
		"deidentification_attested",
		"effective_from",
		"effective_to",
		"publisher",
		"status",
		"submitted_by",
		"submitted_at",
		"published_by",
		"published_at",
		"review_comment",
		"checksum",
		"retired_by",
		"retired_at",
		"retirement_reason",
	},
	"IONE Finding Recurrence Policy": {
		"policy_code",
		"policy_name",
		"hospital",
		"aggregation_level",
		"grouping_mode",
		"threshold",
		"window_days",
		"effective_from",
		"effective_to",
		"status",
		"enabled",
		"requested_by",
		"requested_at",
		"request_checksum",
		"approved_by",
		"approved_at",
		"policy_checksum",
	},
	"IONE Finding Recurrence Run": {
		"run_key",
		"policy",
		"policy_checksum",
		"as_of_date",
		"window_start",
		"window_end",
		"hospital",
		"trigger_source",
		"group_count",
		"finding_count",
		"snapshot_json",
		"snapshot_hash",
	},
	"IONE Finding Recurrence Evaluation": {
		"evaluation_key",
		"run_key",
		"policy",
		"policy_checksum",
		"group_key",
		"rule",
		"threshold",
		"occurrence_count",
		"outcome",
		"finding_links_json",
		"finding_set_hash",
		"evaluation_hash",
	},
	"IONE PDCA Project": {
		"project_code",
		"project_type",
		"recurrence_policy",
		"recurrence_evaluation",
		"recurrence_group_key",
		"recurrence_case_key",
		"active_recurrence_key",
		"analysis_method",
		"root_causes",
		"pareto_items",
		"improvement_measures",
		"effect_measurements",
		"standardization_decision",
		"standardization_evidence_hash",
	},
	"IONE Root Cause": {
		"cause_code",
		"analysis_method",
		"why_level",
		"parent_cause_code",
		"category",
		"cause",
		"evidence_hash",
	},
	"IONE Pareto Cause Item": {
		"cause_code",
		"occurrence_count",
		"rank",
		"cumulative_count",
		"cumulative_percent",
	},
	"IONE Improvement Measure": {
		"measure_code",
		"measure_type",
		"measure",
		"owner_user",
		"due_date",
		"completion_evidence_hash",
	},
	"IONE PDCA Effect Measurement": {
		"measurement_code",
		"indicator",
		"baseline_source",
		"actual_source",
		"baseline_value",
		"target_value",
		"actual_value",
		"source_snapshot_hash",
		"verified_by",
		"verified_at",
		"measurement_hash",
	},
	"IONE Source Document Locator": {
		"locator_key",
		"locator_code",
		"version",
		"source_system",
		"endpoint",
		"source_record_type",
		"hospital",
		"campus",
		"department",
		"ward",
		"path_prefix",
		"path_suffix",
		"sso_uat_reference",
		"redirect_uat_reference",
		"scope_family_key",
		"scope_key",
		"active_slot_key",
		"status",
		"enabled",
		"requested_by",
		"requested_at",
		"approved_by",
		"approved_at",
		"approval_checksum",
	},
	"IONE Source Document Access Log": {
		"access_key",
		"request_id",
		"finding",
		"evidence",
		"locator",
		"source_system",
		"source_record_type",
		"record_id_hash",
		"hospital",
		"campus",
		"department",
		"ward",
		"accessed_by",
		"accessed_at",
		"result_code",
	},
	"IONE Medical Record QC": {
		"record_key",
		"record_status",
		"source_system",
		"source_record_type",
		"source_record_id_hash",
		"source_finalized_at",
		"record_snapshot_hash",
		"hospital",
		"patient",
		"encounter",
	},
	"IONE Medical Record Sampling Policy": {
		"policy_code",
		"policy_version",
		"source_system",
		"hospital",
		"campus",
		"department",
		"ward",
		"effective_from",
		"effective_to",
		"eligible_record_status",
		"population_date_field",
		"sample_rate_basis_points",
		"minimum_sample_size",
		"maximum_sample_size",
		"expert_sample_rate_basis_points",
		"expert_minimum_sample_size",
		"expert_maximum_sample_size",
		"rounding_mode",
		"undersized_population_action",
		"expert_non_pass_policy",
		"archive_gate_enabled",
		"mandatory_rule_manifest_json",
		"mandatory_rule_manifest_hash",
		"auto_allow_deterministic_pass",
		"coder_allow_outcomes_json",
		"expert_allow_outcomes_json",
		"sampling_key_id",
		"sampling_key_fingerprint",
		"status",
		"enabled",
		"requested_by",
		"requested_at",
		"reviewed_by",
		"reviewed_at",
		"approval_checksum",
		"operated_by",
		"operated_at",
		"operation_comment",
		"latest_operation",
	},
	"IONE Medical Record Sampling Policy Operation": {
		"operation_key",
		"operation_checksum",
		"policy",
		"policy_checksum",
		"source_system",
		"hospital",
		"campus",
		"department",
		"ward",
		"action",
		"from_status",
		"from_enabled",
		"to_status",
		"to_enabled",
		"operation_comment",
		"operated_by",
		"operated_at",
		"previous_operation",
	},
	"IONE Medical Record Review Batch": {
		"batch_key",
		"policy",
		"policy_checksum",
		"batch_kind",
		"parent_batch",
		"supplement_sequence",
		"source_system",
		"source_completeness_watermark",
		"source_complete_through",
		"source_completeness_hash",
		"period_start",
		"period_end",
		"hospital",
		"campus",
		"department",
		"ward",
		"population_count",
		"population_manifest_json",
		"population_hash",
		"sample_count",
		"selection_manifest_json",
		"selection_hash",
		"expert_sample_count",
		"expert_selection_manifest_json",
		"expert_selection_hash",
		"sampling_key_id",
		"sampling_key_fingerprint",
		"status",
		"completed_at",
		"created_by",
		"created_at",
	},
	"IONE Medical Record Review Assignment": {
		"assignment_key",
		"batch",
		"policy",
		"medical_record_qc",
		"clinical_event",
		"population_token",
		"record_snapshot_hash",
		"sampling_score_hash",
		"coder_required",
		"expert_required",
		"mandatory_rule_manifest_hash",
		"mandatory_execution_manifest_json",
		"mandatory_execution_manifest_hash",
		"deterministic_gate_status",
		"deterministic_gate_reason_code",
		"hospital",
		"campus",
		"department",
		"ward",
		"patient",
		"encounter",
		"responsible_staff",
		"source_system",
		"source_record_type",
		"source_record_id_hash",
		"status",
		"assigned_coder",
		"coder_decision",
		"assigned_expert",
		"expert_decision",
		"latest_archive_decision",
		"archive_acknowledgement",
		"pending_archive_delivery",
		"pending_archive_envelope_hash",
		"archive_ack_status",
	},
	"IONE Medical Record Review Decision": {
		"decision_key",
		"assignment",
		"batch",
		"policy",
		"medical_record_qc",
		"stage",
		"outcome",
		"comment",
		"finding",
		"evidence_references_json",
		"reviewed_by",
		"reviewed_at",
		"record_snapshot_hash",
		"decision_checksum",
	},
	"IONE Medical Record Archive Decision": {
		"archive_key",
		"assignment",
		"batch",
		"policy",
		"policy_checksum",
		"medical_record_qc",
		"review_decision",
		"decision",
		"decision_type",
		"reason_code",
		"rationale",
		"triggered_by",
		"decided_at",
		"supersedes",
		"record_snapshot_hash",
		"source_system",
		"source_record_type",
		"source_record_id_hash",
		"decision_checksum",
	},
	"IONE Medical Record Archive Acknowledgement": {
		"ack_key",
		"archive_decision",
		"assignment",
		"delivery",
		"envelope_hash",
		"endpoint",
		"source_system",
		"source_ack_id",
		"source_ack_at",
		"ack_status",
		"message_code",
		"request_hash",
		"decision_checksum",
		"source_record_id_hash",
		"received_at",
	},
	"IONE Medical Record Archive Delivery": {
		"delivery_key",
		"request_id",
		"request_hash",
		"endpoint",
		"source_system",
		"hospital",
		"campus",
		"department",
		"ward",
		"delivery_mode",
		"decision_count",
		"envelope_count",
		"decision_manifest_json",
		"envelope_manifest_json",
		"response_json",
		"response_hash",
		"delivery_status",
		"delivered_by",
		"delivered_at",
	},
	"IONE Medical Record Completeness Watermark": {
		"watermark_key",
		"source_watermark_id",
		"endpoint",
		"source_system",
		"hospital",
		"campus",
		"department",
		"ward",
		"complete_through",
		"period_start",
		"period_end",
		"eligible_record_status",
		"population_date_field",
		"source_sequence",
		"source_record_count",
		"source_manifest_hash",
		"request_hash",
		"watermark_checksum",
		"source_asserted_at",
		"received_at",
	},
	"IONE Staff Qualification": {
		"qualification_key",
		"medical_staff",
		"hospital",
		"campus",
		"department",
		"qualification_type",
		"qualification_code",
		"effective_from",
		"effective_to",
		"status",
		"approved_by",
		"approved_at",
		"approval_comment",
		"retired_by",
		"retired_at",
		"retirement_reason",
		"checksum",
	},
	"IONE Surgery Authorization": {
		"authorization_key",
		"medical_staff",
		"procedure_mapping",
		"procedure_code",
		"surgery_level",
		"authorization_scope",
		"hospital",
		"campus",
		"department",
		"qualification",
		"effective_from",
		"effective_to",
		"status",
		"approved_by",
		"approved_at",
		"approval_comment",
		"retired_by",
		"retired_at",
		"retirement_reason",
		"checksum",
	},
	"IONE Surgery Procedure Policy": {
		"policy_code",
		"policy_version",
		"hospital",
		"campus",
		"department",
		"procedure_code",
		"procedure_name",
		"surgery_level",
		"effective_from",
		"effective_to",
		"require_mdt",
		"minimum_mdt_participants",
		"required_mdt_roles_json",
		"required_check_items_json",
		"required_care_stages_json",
		"postoperative_followup_due_hours",
		"allow_emergency_exception",
		"allowed_exception_scopes_json",
		"maximum_exception_validity_hours",
		"status",
		"approved_by",
		"approved_at",
		"approval_comment",
		"retired_by",
		"retired_at",
		"retirement_reason",
		"checksum",
	},
	"IONE Surgery QC": {
		"surgery_key",
		"event",
		"hospital",
		"campus",
		"department",
		"procedure_code",
		"surgery_level",
		"surgery_time",
		"surgeon",
		"surgery_phase",
		"source_system",
		"source_record_type",
		"source_record_id_hash",
		"source_version",
		"source_event_time",
		"source_finalized_at",
		"record_snapshot_hash",
		"procedure_policy",
		"governance_state",
		"governance_checked_at",
		"governance_reason_codes_json",
		"governance_checksum",
		"preanesthesia_status",
		"intraoperative_monitoring_status",
		"recovery_status",
		"postoperative_followup_status",
		"cancellation_status",
		"unplanned_surgery_status",
		"unplanned_return_status",
		"complication_status",
		"mortality_status",
	},
	"IONE Surgery MDT Record": {
		"mdt_key",
		"surgery_qc",
		"surgery_source_id_hash",
		"procedure_policy",
		"hospital",
		"campus",
		"department",
		"meeting_at",
		"completed_at",
		"chair",
		"conclusion",
		"conclusion_summary",
		"evidence_reference",
		"participant_count",
		"recorded_by",
		"recorded_at",
		"status",
		"checksum",
	},
	"IONE Surgery MDT Participant": {
		"participant_key",
		"mdt_record",
		"surgery_qc",
		"surgery_source_id_hash",
		"hospital",
		"campus",
		"department",
		"medical_staff",
		"participant_role",
		"attended",
		"confirmed_at",
		"evidence_reference",
		"recorded_by",
		"recorded_at",
		"checksum",
	},
	"IONE Surgery Safety Checklist": {
		"checklist_key",
		"surgery_qc",
		"surgery_source_id_hash",
		"procedure_policy",
		"hospital",
		"campus",
		"department",
		"required_items_json",
		"results_json",
		"completed_by",
		"completed_at",
		"evidence_reference",
		"status",
		"checksum",
	},
	"IONE Surgery Emergency Exception": {
		"exception_key",
		"surgery_qc",
		"surgery_source_id_hash",
		"procedure_policy",
		"hospital",
		"campus",
		"department",
		"requested_scopes_json",
		"reason_code",
		"reason",
		"requested_by",
		"requested_at",
		"valid_from",
		"valid_to",
		"status",
		"reviewed_by",
		"reviewed_at",
		"review_comment",
		"checksum",
	},
	"IONE Diagnosis Procedure Mapping": {
		"mapping_key",
		"source_system",
		"mapping_type",
		"source_code",
		"standard_system",
		"standard_code",
		"code_system_version",
		"status",
	},
	"IONE Indicator Calculation": {
		"calculation_key",
		"input_receipt_hash",
		"input_receipt_json",
		"receipt_checksum",
		"receipt_status",
		"series_key",
		"result_revision",
		"indicator_version",
		"period_start",
		"period_end",
		"dimension_json",
		"result",
		"status",
	},
	"IONE QC Indicator Version": {
		"indicator",
		"version",
		"indicator_code_snapshot",
		"indicator_name_snapshot",
		"standard_version_snapshot",
		"standard_clause_snapshot",
		"authority_snapshot_hash",
		"lineage_status",
		"calculation_frequency",
		"rolling_window_days",
		"formula_json",
		"dimensions_json",
		"source_mapping",
		"source_system_snapshot",
		"mapping_version_snapshot",
		"mapping_checksum_snapshot",
		"query_contract_hash",
		"physical_query_contract_json",
		"physical_query_contract_hash",
		"schema_signature_hash",
		"calculator_version_snapshot",
		"calculator_code_hash",
		"publication_contract_json",
		"publication_checksum",
		"status",
		"target_min",
		"target_max",
	},
	"IONE QC Indicator": {
		"indicator_code",
		"indicator_name",
		"category",
		"definition",
		"calculator_key",
		"formula_json",
		"standard",
		"standard_clause",
		"direction",
		"target_value",
		"target_min",
		"target_max",
		"warning_threshold",
		"critical_threshold",
		"status",
	},
	"IONE QC Rule Validation Run": {
		"run_key",
		"rule_version",
		"rule_checksum",
		"run_type",
		"status",
		"event_count",
		"error_count",
		"sample_hash",
		"result_hash",
		"outcome_manifest_json",
		"summary_json",
		"requested_by",
		"started_at",
		"completed_at",
		"record_hash",
	},
	"IONE QC Rule Test Execution": {
		"receipt_key",
		"test_case",
		"rule_version",
		"rule_checksum",
		"sample_type",
		"definition_hash",
		"input_hash",
		"expected_result",
		"expected_hash",
		"actual_result",
		"output_hash",
		"executor_version",
		"lifecycle_state",
		"passed",
		"executed_by",
		"executed_at",
		"record_hash",
	},
	"IONE QC Rule Shadow Execution": {
		"execution_key",
		"event",
		"rule_version",
		"rule_checksum",
		"hospital",
		"campus",
		"department",
		"ward",
		"result",
		"event_hash",
		"context_hash",
		"evaluation_hash",
		"executor_version",
		"observed_at",
		"completed_at",
		"duration_ms",
		"record_hash",
	},
	"IONE QC Rule Shadow Feedback": {
		"feedback_key",
		"execution_feedback_key",
		"shadow_execution",
		"rule_version",
		"rule_checksum",
		"hospital",
		"campus",
		"department",
		"ward",
		"gold_label",
		"assessment",
		"comment",
		"reviewed_by",
		"reviewed_at",
		"record_hash",
	},
	"IONE Definition Lineage Receipt": {
		"receipt_key",
		"target_doctype",
		"target_name",
		"outcome",
		"reason_code",
		"migration_run_id",
		"recorded_at",
		"record_hash",
	},
	"IONE Indicator Result": {
		"indicator",
		"indicator_version",
		"series_key",
		"series_revision_key",
		"result_revision",
		"input_receipt_hash",
		"input_receipt_json",
		"result_checksum",
		"query_contract_hash",
		"physical_query_contract_hash",
		"calculator_code_hash",
		"source_system",
		"mapping_record",
		"mapping_version",
		"mapping_checksum",
		"source_manifest_hash",
		"source_manifest_count",
		"lineage_status",
		"medical_group",
		"physician",
		"disease",
		"surgery",
		"drg",
		"target_value",
		"target_min",
		"target_max",
	},
	"IONE Indicator Result Pointer": {
		"series_key",
		"indicator",
		"indicator_version",
		"period_start",
		"period_end",
		"dimension_hash",
		"current_result",
		"current_revision",
		"current_input_receipt_hash",
		"current_result_checksum",
		"pointer_checksum",
	},
	"IONE Indicator Result Detail": {
		"detail_key",
		"indicator_result",
		"result_revision",
		"input_receipt_hash",
		"source_system",
		"mapping_record",
		"mapping_version",
		"mapping_checksum",
		"detail_checksum",
	},
	"IONE Indicator Quarantine Receipt": {
		"quarantine_receipt_key",
		"target_doctype",
		"target_name",
		"target_fingerprint",
		"reason_code",
		"detected_at",
		"migration_key",
		"quarantine_receipt_checksum",
	},
	"IONE Indicator Quarantine Disposition": {
		"disposition_key",
		"quarantine_receipt",
		"action",
		"request_reason",
		"requested_by",
		"requested_at",
		"status",
		"disposition_checksum",
	},
	"IONE Indicator Historical Audit Authorization": {
		"authorization_key",
		"request_hmac_key_id",
		"authorization_hmac_key_id",
		"indicator_result",
		"result_series_key",
		"result_revision",
		"result_checksum",
		"input_receipt_hash",
		"source_manifest_count",
		"purpose",
		"hospital",
		"campus",
		"department",
		"ward",
		"max_source_records",
		"max_reproductions",
		"requested_ttl_hours",
		"requested_by",
		"requested_at",
		"request_checksum",
		"status",
		"reviewed_by",
		"reviewed_at",
		"review_comment",
		"expires_at",
		"used_reproductions",
		"last_accessed_at",
		"authorization_checksum",
	},
	"IONE Indicator Historical Audit Access Receipt": {
		"access_key",
		"hmac_key_id",
		"authorization",
		"indicator_result",
		"accessed_by",
		"accessed_at",
		"purpose_hash",
		"sequence_no",
		"source_manifest_count",
		"result_checksum",
		"verified",
		"reason_code",
		"outcome_hash",
		"receipt_checksum",
	},
	"IONE Daily Quality Fact": {
		"fact_key",
		"indicator_result",
		"indicator_result_revision",
		"series_key",
		"input_receipt_hash",
		"result_checksum",
		"lineage_json",
	},
	"IONE Data Quality Issue": {
		"issue_key",
		"source_system",
		"event",
		"source_record_type",
		"source_record_id",
		"description",
		"detected_at",
		"status",
	},
	"IONE Monthly Quality Fact": {
		"fact_key",
		"month",
		"indicator",
		"indicator_version",
		"dimension_json",
		"dimension_hash",
		"medical_staff",
		"medical_group",
		"physician",
		"disease",
		"surgery",
		"drg",
		"target_value",
		"target_min",
		"target_max",
		"status",
		"result_count",
		"source_revision_manifest_hash",
		"source_revision_count",
		"lineage_json",
	},
	"IONE Finding Analysis Fact": {
		"fact_key",
		"finding",
		"fact_date",
		"rule",
		"rule_version",
		"standard",
		"finding_status",
		"ai_origin",
		"responsible_staff",
	},
	"IONE Surgery Quality Fact": {
		"fact_key",
		"surgery_qc",
		"fact_date",
		"encounter",
		"finding",
		"surgery_type",
		"surgery_level",
		"surgeon",
		"surgery_status",
		"compliant",
		"unplanned_return",
		"complication",
	},
	"IONE Agent Analysis Fact": {
		"fact_key",
		"analysis_task",
		"flow_run_link",
		"fact_date",
		"flow_run",
		"flow_agent",
		"policy",
		"flow_model",
		"model_id",
		"task_type",
		"task_status",
		"requested_by",
		"requires_human_review",
		"duration_ms",
		"input_tokens",
		"output_tokens",
		"total_tokens",
	},
	"IONE AI Analysis Task": {
		"hospital",
		"campus",
		"department",
		"ward",
		"patient",
		"encounter",
		"responsible_staff",
		"origin",
		"report_schedule",
		"report_snapshot",
		"report_period_start",
		"report_period_end",
		"report_reviewer",
		"dispatch_key",
		"schedule_approval_checksum",
		"prior_report_task",
		"report_recovery_authorization",
		"recovery_sequence",
		"recovery_authorization_checksum",
		"execution_attempt",
		"execution_phase",
		"claimed_at",
		"heartbeat_at",
		"lease_expires_at",
		"execution_token",
		"last_execution_token_hash",
		"status",
		"flow_session",
		"flow_run",
	},
	"IONE AI Report Draft": {
		"task",
		"execution_attempt",
		"flow_run",
		"report_type",
		"content",
		"status",
	},
	"IONE AI Report Schedule": {
		"schedule_code",
		"schedule_title",
		"report_type",
		"policy",
		"requested_by",
		"report_reviewer",
		"hospital",
		"campus",
		"department",
		"ward",
		"run_day",
		"coverage_start_period",
		"required_indicator_codes_json",
		"status",
		"enabled",
		"reviewed_by",
		"reviewed_at",
		"approval_checksum",
		"last_dispatch_key",
		"last_task",
		"last_result",
	},
	"IONE Quality Report Snapshot": {
		"snapshot_key",
		"schedule",
		"period_start",
		"period_end",
		"hospital",
		"campus",
		"department",
		"ward",
		"generated_for",
		"report_reviewer",
		"generated_at",
		"source_cutoff",
		"source_group_count",
		"data_json",
		"data_hash",
	},
	"IONE AI Report Recovery Authorization": {
		"authorization_key",
		"schedule",
		"schedule_approval_checksum",
		"report_snapshot",
		"period_start",
		"period_end",
		"hospital",
		"campus",
		"department",
		"ward",
		"prior_task",
		"prior_terminal_status",
		"prior_dispatch_key",
		"recovery_sequence",
		"authorized_by",
		"authorized_at",
		"reason_code",
		"reason",
		"recovery_dispatch_key",
		"authorization_checksum",
	},
	"IONE AI Data Access Log": {
		"task",
		"policy",
		"user",
		"tool_slug",
		"hospital",
		"campus",
		"department",
		"ward",
		"patient",
		"encounter",
		"purpose",
		"accessed_fields",
		"accessed_at",
		"result",
		"request_hash",
		"source_doctype",
		"source_name",
		"source_version",
		"source_record_hash",
		"content_hash",
		"snapshot_json",
		"record_hash",
	},
	"IONE AI Execution Event": {
		"execution_event_key",
		"task",
		"policy",
		"hospital",
		"campus",
		"department",
		"ward",
		"patient",
		"encounter",
		"responsible_staff",
		"execution_attempt",
		"execution_token_hash",
		"event_type",
		"event_at",
		"flow_run",
		"outcome_status",
		"detail_code",
		"record_hash",
	},
	"IONE Flow Run Link": {
		"revision_key",
		"run_iteration",
		"task",
		"policy",
		"hospital",
		"campus",
		"department",
		"ward",
		"patient",
		"encounter",
		"responsible_staff",
		"execution_attempt",
		"execution_token_hash",
		"flow_agent",
		"flow_model",
		"model_id",
		"flow_session",
		"flow_run",
		"status",
		"tools_used",
		"input_hash",
		"output_hash",
		"input_messages_hash",
		"output_messages_hash",
		"tool_trace_hash",
		"attachment_text_hash",
		"usage_json",
		"started_at",
		"completed_at",
		"record_hash",
	},
	"IONE AI Candidate Finding": {
		"hospital",
		"campus",
		"department",
		"ward",
		"patient",
		"encounter",
	},
	"IONE QC Finding Appeal": {
		"finding",
		"active_key",
		"appeal_type",
		"reason",
		"evidence_summary",
		"evidence_record",
		"evidence_file",
		"evidence_content_hash",
		"hospital",
		"campus",
		"department",
		"patient",
		"encounter",
		"submission_hash",
		"status",
		"submitted_by",
		"submitted_at",
		"reviewed_by",
		"reviewed_at",
		"review_comment",
	},
	"IONE QC Finding Appeal Evidence": {
		"finding",
		"evidence_file",
		"content_hash",
		"file_name",
		"file_size",
		"appeal_submission_hash",
		"submitted_by",
		"submitted_at",
	},
	"IONE Agent Incident": {
		"incident_key",
		"incident_type",
		"severity",
		"status",
		"detected_at",
		"summary",
		"containment_actions",
		"record_hash",
	},
	"IONE Data Export Request": {
		"request_id",
		"reference_doctype",
		"filters_json",
		"fields_json",
		"reason",
		"classification",
		"requested_by",
		"requested_at",
		"status",
		"approved_by",
		"approved_at",
		"approval_comment",
		"expires_at",
		"record_count",
		"output_file",
		"watermark",
		"record_hash",
	},
	"IONE Agent Policy": {
		"managed_by_app",
	},
	"IONE Agent Release": {
		"active_parent_key",
		"checksum",
		"commit_sha",
		"configuration_json",
		"flow_agent",
		"flow_model",
		"policy",
		"release_key",
		"status",
	},
	"IONE Agent Evaluation Threshold Policy": {
		"threshold_policy_key",
		"active_parent_key",
		"version",
		"categories_json",
		"thresholds_json",
		"checksum",
		"status",
		"approved_by",
		"approved_at",
		"approval_signature",
	},
	"IONE Agent Evaluation": {
		"agent",
		"agent_release",
		"category_matrix_hash",
		"evaluated_at",
		"evaluated_by",
		"evaluation_key",
		"evaluator_code_hash",
		"failed_count",
		"flow_model",
		"instructions_hash",
		"knowledge_base_manifest_hash",
		"metrics_json",
		"model_configuration_hash",
		"passed_count",
		"policy",
		"policy_checksum",
		"prompt_hash",
		"receipt_hash",
		"release_checksum",
		"requested_by",
		"result_hash",
		"review_comment",
		"review_status",
		"reviewed_at",
		"reviewed_by",
		"sample_count",
		"status",
		"test_suite_hash",
		"threshold_policy",
		"threshold_policy_hash",
		"tool_schema_hash",
	},
	"IONE AI Tool Approval": {
		"approval_key",
		"arguments_hash",
		"arguments_json",
		"decision",
		"expires_at",
		"flow_run",
		"flow_session",
		"policy",
		"question_hash",
		"question_key",
		"question_prompt",
		"question_prompt_hash",
		"requested_at",
		"run_iteration",
		"status",
		"task",
		"tool_slug",
	},
	"IONE PHI Disclosure Policy": {
		"policy_code",
		"policy_version",
		"policy_key",
		"policy_name",
		"purpose_code",
		"purpose_description",
		"hospital",
		"allowed_fields_json",
		"allowed_reader_roles_json",
		"max_disclosures_per_user_per_minute",
		"max_disclosures_per_record_per_minute",
		"effective_from",
		"external_approval_reference",
		"status",
		"policy_scope_key",
		"active_slot_key",
		"requested_by",
		"requested_at",
		"hmac_key_id",
		"policy_checksum",
	},
	"IONE PHI Access Receipt": {
		"receipt_key",
		"request_id_hash",
		"hmac_key_id",
		"reader_user",
		"purpose_code",
		"reference_doctype",
		"reference_hash",
		"field_manifest_json",
		"field_manifest_hash",
		"disclosure_policy",
		"policy_checksum",
		"authorized_scope_json",
		"authorization_basis_json",
		"authorization_basis_hash",
		"role_snapshot_json",
		"role_snapshot_hash",
		"session_hash",
		"accessed_at",
		"result_code",
		"record_hash",
	},
}

REQUIRED_SELECT_OPTIONS = {
	("IONE PHI Disclosure Policy", "status"): {
		"Draft",
		"Under Review",
		"Scheduled",
		"Active",
		"Retired",
	},
	("IONE PHI Access Receipt", "result_code"): {"AUTHORIZED"},
	("IONE Medical Record Review Batch", "batch_kind"): {"Initial", "Supplemental"},
	("IONE Medical Record Sampling Policy Operation", "action"): {
		"Activate",
		"Suspend",
		"Retire",
	},
	("IONE Medical Record Sampling Policy Operation", "from_status"): {
		"Approved",
		"Suspended",
	},
	("IONE Medical Record Sampling Policy Operation", "to_status"): {
		"Approved",
		"Suspended",
		"Retired",
	},
	("IONE Meeting Agenda Item", "material_type"): {
		"None",
		"Manual Reference",
		"AI Report Draft",
	},
	("IONE Meeting Attendee", "attendance_status"): {"Pending", "Attended", "Absent"},
	("IONE Quality Meeting", "status"): {
		"Draft",
		"Submitted",
		"Approved",
		"Held",
		"Minutes Pending",
		"Closed",
		"Cancelled",
	},
	("IONE Meeting Minute", "status"): {"Draft", "Submitted", "Approved", "Rejected"},
	("IONE Quality Action Item", "status"): {
		"Open",
		"In Progress",
		"Pending Verification",
		"Closed",
		"Cancelled",
	},
	("IONE Quality Action Verification Round", "decision"): {
		"Close",
		"Rework",
		"Cancel",
	},
	("IONE Quality Action Verification Round", "action_status_after"): {
		"In Progress",
		"Closed",
		"Cancelled",
	},
	("IONE Quality Experience Share", "status"): {
		"Draft",
		"Submitted",
		"Published",
		"Rejected",
		"Retired",
	},
	("IONE Finding Recurrence Policy", "status"): {
		"Draft",
		"Approved",
		"Suspended",
		"Retired",
		"Rejected",
	},
	("IONE Finding Recurrence Evaluation", "outcome"): {
		"Below Threshold",
		"Triggered",
	},
	("IONE PDCA Project", "project_type"): {"Regular", "Special Recurrence"},
	("IONE Root Cause", "analysis_method"): {"Five Why", "Fishbone", "Other"},
	("IONE Improvement Measure", "measure_type"): {
		"Corrective",
		"Preventive",
		"Standardization",
	},
	("IONE Encounter Index", "encounter_type"): {
		"Outpatient",
		"Emergency",
		"Inpatient",
		"Day Care",
		"Day Surgery",
		"Observation",
		"Other",
	},
	("IONE QC Rule Version", "status"): {
		"Draft",
		"Expert Review",
		"Test",
		"Historical Replay",
		"Shadow Run",
		"Approval",
		"Published",
		"Retired",
	},
	("IONE QC Standard Version", "approval_status"): {
		"Draft",
		"Under Review",
		"Approved",
		"Retired",
	},
	("IONE QC Indicator Version", "status"): {
		"Draft",
		"Under Review",
		"Published",
		"Retired",
	},
	("IONE QC Standard Version", "lineage_status"): {"Verified", "Quarantined"},
	("IONE QC Rule Version", "lineage_status"): {"Verified", "Quarantined"},
	("IONE QC Indicator Version", "lineage_status"): {"Verified", "Quarantined"},
	("IONE QC Rule Validation Run", "run_type"): {"Historical Replay"},
	("IONE QC Rule Validation Run", "status"): {"Completed", "Failed"},
	("IONE QC Rule Test Case", "sample_type"): {
		"Positive",
		"Negative",
		"Exclusion",
		"Boundary",
		"Missing Data",
		"Real World",
		"Regression",
	},
	("IONE QC Rule Test Case", "expected_result"): {
		"Passed",
		"Failed",
		"Excluded",
		"Insufficient Data",
		"Error",
	},
	("IONE QC Rule Test Execution", "sample_type"): {
		"Positive",
		"Negative",
		"Exclusion",
		"Boundary",
		"Missing Data",
		"Real World",
		"Regression",
	},
	("IONE QC Rule Shadow Execution", "result"): {
		"Passed",
		"Failed",
		"Excluded",
		"Insufficient Data",
		"Error",
	},
	("IONE QC Rule Shadow Feedback", "assessment"): {
		"Confirmed Finding",
		"Correct Pass",
		"Correct Exclusion",
		"False Positive",
		"False Negative",
		"Missing Data",
		"Rule Error",
		"Needs Review",
	},
	("IONE Definition Lineage Receipt", "outcome"): {"Backfilled", "Quarantined"},
	("IONE QC Finding", "status"): {
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
	},
	("IONE QC Finding Appeal", "appeal_type"): {
		"Factual Dispute",
		"Rule Applicability",
		"Clinical Exception",
		"Evidence Dispute",
		"Other",
	},
	("IONE QC Finding Appeal", "status"): {
		"Submitted",
		"Approved",
		"Rejected",
		"Withdrawn",
	},
	("IONE QC Indicator Version", "calculation_frequency"): {
		"Daily",
		"Weekly",
		"Monthly",
		"Quarterly",
		"Annual",
		"Rolling",
	},
	("IONE Integration Message", "status"): {"Received", "Processed", "Error", "Dead Letter"},
	("IONE AI Candidate Finding", "status"): {"Pending Review", "Accepted", "Rejected"},
	("IONE AI Report Draft", "status"): {
		"Draft",
		"Pending Review",
		"Approved",
		"Rejected",
		"Revision Requested",
	},
	("IONE AI Report Recovery Authorization", "prior_terminal_status"): {
		"Failed",
		"Rejected",
		"Cancelled",
	},
	("IONE Agent Policy", "agent_category"): AGENT_CATEGORIES,
	("IONE AI Analysis Task", "task_type"): AGENT_CATEGORIES,
	("IONE Agent Test Case", "evaluation_category"): EVALUATION_CATEGORIES,
	("IONE Agent Evaluation Threshold Policy", "status"): {"Draft", "Approved", "Retired"},
	("IONE Agent Evaluation", "status"): {"Passed", "Failed"},
	("IONE Agent Evaluation", "review_status"): {"Pending Review", "Approved", "Rejected"},
	("IONE AI Tool Approval", "status"): {"Pending", "Reviewed", "Consumed", "Expired"},
	("IONE Agent Incident", "status"): {"Open", "Investigating", "Contained", "Resolved", "Closed"},
	("IONE Staff Qualification", "status"): {
		"Draft",
		"Approved",
		"Retired",
	},
	("IONE Surgery Authorization", "status"): {
		"Draft",
		"Approved",
		"Retired",
	},
	("IONE Surgery Authorization", "surgery_level"): {
		"Level I",
		"Level II",
		"Level III",
		"Level IV",
	},
	("IONE Surgery Procedure Policy", "status"): {"Draft", "Approved", "Retired"},
	("IONE Surgery Procedure Policy", "surgery_level"): {
		"Level I",
		"Level II",
		"Level III",
		"Level IV",
	},
	("IONE Surgery QC", "surgery_phase"): {
		"Scheduled",
		"Occurred",
		"Postoperative Finalized",
		"Cancelled",
	},
	("IONE Surgery QC", "governance_state"): {
		"Pending Prerequisites",
		"Passed",
		"Noncompliant",
		"Blocked",
		"Cancelled",
	},
	("IONE Surgery MDT Record", "status"): {"Completed"},
	("IONE Surgery Safety Checklist", "status"): {"Completed"},
	("IONE Surgery Emergency Exception", "status"): {
		"Pending",
		"Approved",
		"Rejected",
		"Expired",
	},
	("IONE Data Export Request", "status"): {
		"Pending",
		"Approved",
		"Processing",
		"Rejected",
		"Completed",
		"Failed",
		"Expired",
	},
}

for _indicator_source_doctype in (
	"IONE Clinical Quality Event",
	"IONE Encounter Index",
	"IONE Indicator Result Detail",
	"IONE Medical Record QC",
	"IONE QC Finding",
	"IONE Medical Safety Event",
	"IONE Surgery QC",
):
	REQUIRED_FIELDS.setdefault(_indicator_source_doctype, set()).update(
		{
			"medical_group",
			"disease",
			"surgery",
			"drg",
			"source_system",
			"mapping_record",
			"mapping_version",
			"mapping_checksum",
		}
	)


UNIQUE_REQUIRED_FIELDS = {
	"IONE Meeting Agenda Item": {"agenda_key"},
	"IONE Quality Meeting": {"meeting_code"},
	"IONE Meeting Minute": {"approved_meeting_key"},
	"IONE Meeting Decision": {"decision_key"},
	"IONE Quality Experience Share": {"version_key", "active_publication_key"},
	"IONE Quality Action Verification Round": {
		"round_key",
		"owner_submission_key",
		"verifier_decision_key",
	},
	"IONE Finding Recurrence Policy": {"policy_code"},
	"IONE Finding Recurrence Run": {"run_key"},
	"IONE Finding Recurrence Evaluation": {"evaluation_key"},
	"IONE PDCA Project": {
		"project_code",
		"recurrence_case_key",
		"active_recurrence_key",
	},
	"IONE Source Document Locator": {"locator_key", "active_slot_key"},
	"IONE Source Document Access Log": {"access_key", "request_id"},
	"IONE PHI Disclosure Policy": {"policy_key", "active_slot_key"},
	"IONE PHI Access Receipt": {"receipt_key"},
	"IONE Patient Index": {"patient_key"},
	"IONE Encounter Index": {"encounter_key"},
	"IONE Clinical Quality Event": {"idempotency_key"},
	"IONE QC Execution": {"execution_key"},
	"IONE QC Rule Test Execution": {"receipt_key"},
	"IONE QC Rule Shadow Execution": {"execution_key"},
	"IONE QC Rule Shadow Feedback": {"feedback_key", "execution_feedback_key"},
	"IONE QC Rule Validation Run": {"run_key"},
	"IONE Definition Lineage Receipt": {"receipt_key"},
	"IONE QC Finding": {"deduplication_key"},
	"IONE QC Finding Evidence": {"evidence_hash"},
	"IONE QC Finding Appeal": {"active_key"},
	"IONE QC Finding Appeal Evidence": {"evidence_file", "content_hash"},
	"IONE Medical Record QC": {"record_key"},
	"IONE Medical Record Sampling Policy Operation": {"operation_key"},
	"IONE Medical Record Review Batch": {"batch_key"},
	"IONE Medical Record Review Assignment": {"assignment_key"},
	"IONE Medical Record Review Decision": {"decision_key"},
	"IONE Medical Record Archive Decision": {"archive_key"},
	"IONE Medical Record Archive Acknowledgement": {"ack_key"},
	"IONE Medical Record Archive Delivery": {"delivery_key"},
	"IONE Medical Record Completeness Watermark": {"watermark_key"},
	"IONE Surgery QC": {"surgery_key"},
	"IONE Surgery MDT Record": {"mdt_key", "surgery_source_id_hash"},
	"IONE Surgery MDT Participant": {"participant_key"},
	"IONE Surgery Safety Checklist": {"checklist_key", "surgery_source_id_hash"},
	"IONE Surgery Emergency Exception": {"exception_key"},
	"IONE Medical Safety Event": {"safety_event_key"},
	"IONE Indicator Calculation": {"calculation_key"},
	"IONE Indicator Result": {
		"result_key",
		"series_revision_key",
	},
	"IONE Indicator Result Pointer": {"series_key"},
	"IONE Indicator Result Detail": {"detail_key"},
	"IONE Indicator Quarantine Receipt": {"quarantine_receipt_key"},
	"IONE Indicator Quarantine Disposition": {"disposition_key"},
	"IONE Indicator Historical Audit Authorization": {"authorization_key"},
	"IONE Indicator Historical Audit Access Receipt": {"access_key"},
	"IONE Indicator Alert": {"alert_key"},
	"IONE Integration Message": {"idempotency_key"},
	"IONE Data Quality Issue": {"issue_key"},
	"IONE Data Reconciliation": {"reconciliation_key"},
	"IONE Agent Release": {"release_key", "active_parent_key"},
	"IONE Agent Evaluation Threshold Policy": {
		"threshold_policy_key",
		"active_parent_key",
	},
	"IONE Agent Evaluation": {"evaluation_key"},
	"IONE AI Tool Approval": {"approval_key"},
	"IONE Flow Run Link": {"revision_key"},
	"IONE AI Execution Event": {"execution_event_key"},
	"IONE AI Analysis Task": {
		"dispatch_key",
		"prior_report_task",
		"report_recovery_authorization",
	},
	"IONE AI Report Recovery Authorization": {
		"authorization_key",
		"prior_task",
		"recovery_dispatch_key",
	},
	"IONE Staff Qualification": {"qualification_key"},
	"IONE Surgery Authorization": {"authorization_key"},
	"IONE Diagnosis Procedure Mapping": {"mapping_key"},
	"IONE Agent Incident": {"incident_key"},
	"IONE Data Export Request": {"request_id"},
	"IONE Daily Quality Fact": {"fact_key"},
	"IONE Monthly Quality Fact": {"fact_key"},
	"IONE Finding Analysis Fact": {"fact_key", "finding"},
	"IONE Surgery Quality Fact": {"fact_key", "surgery_qc"},
	"IONE Agent Analysis Fact": {"fact_key", "analysis_task"},
}
MATERIALIZER_ONLY_DOCTYPES = frozenset(
	{
		"IONE Finding Recurrence Run",
		"IONE Finding Recurrence Evaluation",
		"IONE Meeting Decision",
		"IONE Quality Action Item",
		"IONE Quality Action Verification Round",
		"IONE Medical Record QC",
		"IONE Medical Record Sampling Policy Operation",
		"IONE Medical Record Review Batch",
		"IONE Medical Record Review Assignment",
		"IONE Medical Record Review Decision",
		"IONE Medical Record Archive Decision",
		"IONE Medical Record Archive Acknowledgement",
		"IONE Medical Record Archive Delivery",
		"IONE Medical Record Completeness Watermark",
		"IONE Source Document Access Log",
		"IONE Surgery QC",
		"IONE Surgery MDT Record",
		"IONE Surgery MDT Participant",
		"IONE Surgery Safety Checklist",
		"IONE Surgery Emergency Exception",
		"IONE Medical Safety Event",
		"IONE Data Reconciliation",
		"IONE AI Report Recovery Authorization",
	}
)

LONG_IDENTIFIER_FIELDS = frozenset(
	{
		"source_patient_id",
		"source_encounter_id",
		"source_record_type",
		"source_record_id",
		"source_version",
		"patient_reference",
		"encounter_reference",
		"department_reference",
		"source_reference",
	}
)
DIGEST_FIELDNAMES = frozenset(
	{
		"alert_key",
		"agenda_checksum",
		"agenda_key",
		"approval_key",
		"archive_key",
		"arguments_hash",
		"assignment_key",
		"authorization_key",
		"authorization_checksum",
		"batch_key",
		"calculation_key",
		"commit_sha",
		"deduplication_key",
		"decision_checksum",
		"decision_key",
		"detail_key",
		"encounter_key",
		"evaluation_key",
		"execution_event_key",
		"execution_key",
		"fact_key",
		"idempotency_key",
		"incident_key",
		"issue_key",
		"mapping_key",
		"material_checksum",
		"patient_key",
		"qualification_key",
		"question_hash",
		"reconciliation_key",
		"release_key",
		"release_checksum",
		"result_key",
		"result_hash",
		"safety_event_key",
		"surgery_key",
		"test_suite_hash",
		"version_key",
		"policy_checksum",
		"policy_key",
		"population_token",
		"ack_key",
		"access_key",
		"active_slot_key",
		"locator_key",
		"scope_family_key",
		"scope_key",
		"request_checksum",
		"group_key",
		"recurrence_group_key",
		"recurrence_case_key",
		"active_recurrence_key",
		"mdt_key",
		"participant_key",
		"checklist_key",
		"exception_key",
		"governance_reason_codes_json",
		"governance_checksum",
		"round_key",
		"completion_evidence_hash",
		"owner_submission_checksum",
		"current_submission_checksum",
		"latest_verification_receipt_checksum",
		"previous_receipt_checksum",
		"receipt_key",
		"receipt_checksum",
		"prior_dispatch_key",
		"recovery_dispatch_key",
		"recovery_authorization_checksum",
		"watermark_key",
		"watermark_checksum",
		"operation_key",
		"operation_checksum",
	}
)
INDEX_REQUIRED_FIELDS = {
	"IONE Quality Meeting": {"meeting_code", "scheduled_start"},
	"IONE Meeting Minute": {"meeting"},
	"IONE Meeting Decision": {"meeting", "meeting_minute", "decision_code"},
	"IONE Quality Action Item": {"assigned_to", "verifier", "due_date"},
	"IONE Quality Action Verification Round": {"action_item", "round_no"},
	"IONE Quality Experience Share": {"experience_code"},
	"IONE Finding Recurrence Policy": {"policy_code", "hospital", "effective_from"},
	"IONE Finding Recurrence Run": {"run_key", "policy", "as_of_date"},
	"IONE Finding Recurrence Evaluation": {
		"run_key",
		"policy",
		"group_key",
		"rule",
	},
	"IONE Source Document Locator": {
		"locator_key",
		"source_record_type",
		"scope_family_key",
		"scope_key",
	},
	"IONE Source Document Access Log": {
		"access_key",
		"request_id",
		"finding",
		"evidence",
		"accessed_at",
	},
	"IONE PHI Disclosure Policy": {
		"purpose_code",
		"hospital",
		"effective_from",
		"policy_scope_key",
		"active_slot_key",
	},
	"IONE PHI Access Receipt": {
		"request_id_hash",
		"reference_hash",
		"reader_user",
		"accessed_at",
	},
	"IONE Patient Index": {"source_system", "patient_key"},
	"IONE Encounter Index": {"source_system", "encounter_key", "patient"},
	"IONE Clinical Quality Event": {"event_time", "source_system", "idempotency_key"},
	"IONE Integration Message": {"source_system", "idempotency_key"},
	"IONE QC Finding Evidence": {"finding", "evidence_hash"},
	"IONE QC Finding Appeal": {"finding"},
	"IONE QC Finding Appeal Evidence": {"finding", "content_hash"},
	"IONE Data Quality Issue": {"issue_key", "detected_at"},
	"IONE Monthly Quality Fact": {"month", "indicator", "indicator_version", "dimension_hash"},
	"IONE Finding Analysis Fact": {"finding", "fact_date"},
	"IONE Surgery Quality Fact": {"surgery_qc", "fact_date"},
	"IONE Surgery Procedure Policy": {"procedure_code", "hospital", "effective_from"},
	"IONE Surgery QC": {
		"procedure_code",
		"source_record_id_hash",
		"surgery_time",
		"surgeon",
	},
	"IONE Surgery MDT Record": {"surgery_source_id_hash", "surgery_qc", "completed_at"},
	"IONE Surgery MDT Participant": {"mdt_record", "medical_staff"},
	"IONE Surgery Safety Checklist": {
		"surgery_source_id_hash",
		"surgery_qc",
		"completed_at",
	},
	"IONE Surgery Emergency Exception": {
		"surgery_source_id_hash",
		"surgery_qc",
		"valid_to",
	},
	"IONE Medical Record Completeness Watermark": {
		"watermark_key",
		"source_watermark_id",
		"endpoint",
		"source_system",
		"hospital",
		"complete_through",
		"source_sequence",
	},
	"IONE Medical Record Sampling Policy Operation": {"operation_key", "policy"},
	"IONE Agent Analysis Fact": {"analysis_task", "fact_date"},
	"IONE AI Execution Event": {"task", "event_at"},
	"IONE AI Report Recovery Authorization": {"schedule", "period_start", "prior_task"},
}
EXPECTED_AUTONAMES = {
	"IONE Quality Meeting": "field:meeting_code",
	"IONE Meeting Decision": "field:decision_key",
	"IONE Finding Recurrence Policy": "field:policy_code",
	"IONE Finding Recurrence Run": "field:run_key",
	"IONE Finding Recurrence Evaluation": "field:evaluation_key",
	"IONE Source Document Access Log": "field:access_key",
	"IONE QC Standard Version": "format:{standard}-{version}",
	"IONE QC Rule Version": "format:{rule}-{version}",
	"IONE QC Indicator Version": "format:{indicator}-{version}",
	"IONE Indicator Calculation": "field:calculation_key",
	"IONE Indicator Result": "field:result_key",
	"IONE Indicator Result Pointer": "field:series_key",
	"IONE Indicator Result Detail": "field:detail_key",
	"IONE Indicator Quarantine Receipt": "field:quarantine_receipt_key",
	"IONE Indicator Quarantine Disposition": "field:disposition_key",
	"IONE Indicator Historical Audit Authorization": "field:authorization_key",
	"IONE Indicator Historical Audit Access Receipt": "field:access_key",
	"IONE QC Standard Clause": "field:clause_key",
	"IONE Staff Qualification": "field:qualification_key",
	"IONE Surgery Authorization": "field:authorization_key",
	"IONE Surgery Procedure Policy": "format:{policy_code}-{policy_version}",
	"IONE Surgery MDT Record": "field:mdt_key",
	"IONE Surgery MDT Participant": "field:participant_key",
	"IONE Surgery Safety Checklist": "field:checklist_key",
	"IONE Surgery Emergency Exception": "field:exception_key",
	"IONE Diagnosis Procedure Mapping": "field:mapping_key",
	"IONE Agent Release": "field:release_key",
	"IONE Agent Evaluation Threshold Policy": "field:threshold_policy_key",
	"IONE Agent Evaluation": "field:evaluation_key",
	"IONE AI Tool Approval": "field:approval_key",
	"IONE AI Execution Event": "field:execution_event_key",
	"IONE AI Report Recovery Authorization": "field:authorization_key",
	"IONE Agent Incident": "field:incident_key",
	"IONE Data Export Request": "field:request_id",
	"IONE Medical Record Sampling Policy": "format:{policy_code}-{policy_version}",
	"IONE Medical Record Sampling Policy Operation": "field:operation_key",
	"IONE Medical Record Review Batch": "field:batch_key",
	"IONE Medical Record Review Assignment": "field:assignment_key",
	"IONE Medical Record Review Decision": "field:decision_key",
	"IONE Medical Record Archive Decision": "field:archive_key",
	"IONE Medical Record Archive Acknowledgement": "field:ack_key",
	"IONE Medical Record Archive Delivery": "field:delivery_key",
	"IONE Medical Record Completeness Watermark": "field:watermark_key",
	"IONE PHI Access Receipt": "field:receipt_key",
}

REQUIRED_ARTIFACTS = (
	".github/workflows/ci.yml",
	"README.md",
	"docs/requirements-traceability.md",
	"docs/privacy-identity-access.md",
	"ione_qms/__init__.py",
	"ione_qms/hooks.py",
	"ione_qms/modules.txt",
	"ione_qms/patches.txt",
	"ione_qms/setup/install.py",
	"pyproject.toml",
	"requirements.txt",
	"tools/generate_doctypes.py",
)


def scrub(value: str) -> str:
	return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def class_name(value: str) -> str:
	return "".join(part for part in re.split(r"[^A-Za-z0-9]+", value) if part)


def load_doctypes(errors: list[str]) -> dict[str, tuple[Path, dict[str, Any]]]:
	doctypes: dict[str, tuple[Path, dict[str, Any]]] = {}
	for path in sorted(PACKAGE.glob("*/doctype/*/*.json")):
		try:
			payload = json.loads(path.read_text(encoding="utf-8"))
		except (OSError, json.JSONDecodeError) as exc:
			errors.append(f"{path.relative_to(ROOT)}: invalid JSON: {exc}")
			continue
		name = payload.get("name")
		if not isinstance(name, str) or not name:
			errors.append(f"{path.relative_to(ROOT)}: missing DocType name")
			continue
		if name in doctypes:
			errors.append(f"duplicate DocType JSON for {name}")
			continue
		doctypes[name] = (path, payload)
	return doctypes


def validate_artifacts(errors: list[str]) -> None:
	for relative in REQUIRED_ARTIFACTS:
		if not (ROOT / relative).is_file():
			errors.append(f"missing required artifact: {relative}")


def validate_modules(
	doctypes: dict[str, tuple[Path, dict[str, Any]]],
	errors: list[str],
) -> None:
	modules_path = PACKAGE / "modules.txt"
	if not modules_path.is_file():
		return
	modules = [line.strip() for line in modules_path.read_text(encoding="utf-8").splitlines() if line.strip()]
	if len(modules) != len(set(modules)):
		errors.append("ione_qms/modules.txt contains duplicate modules")
	expected_modules = set(REQUIRED_DOCTYPES_BY_MODULE)
	if set(modules) != expected_modules:
		errors.append(
			f"module catalog mismatch: expected={sorted(expected_modules)}, actual={sorted(set(modules))}"
		)
	for module in modules:
		module_dir = PACKAGE / scrub(module)
		if not (module_dir / "__init__.py").is_file():
			errors.append(f"{module}: missing module __init__.py")
		if not (module_dir / "doctype" / "__init__.py").is_file():
			errors.append(f"{module}: missing doctype package __init__.py")
	for name, (path, payload) in doctypes.items():
		module = payload.get("module")
		if module not in expected_modules:
			errors.append(f"{name}: unknown module {module!r}")
			continue
		expected_path = PACKAGE / scrub(module) / "doctype" / scrub(name) / f"{scrub(name)}.json"
		if path.resolve() != expected_path.resolve():
			errors.append(f"{name}: JSON path does not match module/name scrub convention")
		if name not in REQUIRED_DOCTYPES_BY_MODULE[module]:
			errors.append(f"{name}: not declared in the {module} module contract")


def validate_catalog(
	doctypes: dict[str, tuple[Path, dict[str, Any]]],
	errors: list[str],
) -> None:
	actual = set(doctypes)
	missing = REQUIRED_DOCTYPES - actual
	extra = actual - REQUIRED_DOCTYPES
	if missing:
		errors.append(f"missing required DocTypes: {', '.join(sorted(missing))}")
	if extra:
		errors.append(f"undeclared DocTypes: {', '.join(sorted(extra))}")
	if len(actual) != len(REQUIRED_DOCTYPES):
		errors.append(f"expected exactly {len(REQUIRED_DOCTYPES)} governed DocTypes, found {len(actual)}")


def validate_doctype(
	name: str,
	path: Path,
	payload: dict[str, Any],
	doctypes: dict[str, tuple[Path, dict[str, Any]]],
	errors: list[str],
) -> None:
	location = str(path.relative_to(ROOT))
	if payload.get("doctype") != "DocType":
		errors.append(f"{location}: root doctype must be 'DocType'")
	if len(name) > 61 or not DOCTYPE_NAME_PATTERN.fullmatch(name):
		errors.append(f"{name}: name violates Frappe v17 DocType naming constraints")
	fields = payload.get("fields")
	if not isinstance(fields, list):
		errors.append(f"{name}: fields must be an array")
		return
	fieldnames = [field.get("fieldname") for field in fields if isinstance(field, dict)]
	if len(fieldnames) != len(fields):
		errors.append(f"{name}: every field entry must be an object")
		return
	duplicates = sorted(item for item, count in Counter(fieldnames).items() if count > 1)
	if duplicates:
		errors.append(f"{name}: duplicate fieldnames: {', '.join(duplicates)}")
	if payload.get("field_order") != fieldnames:
		errors.append(f"{name}: field_order must exactly match fields order")
	field_map = {field["fieldname"]: field for field in fields}
	for field in fields:
		validate_field(name, field, field_map, doctypes, errors)
	validate_protected_field_registry(name, payload, field_map, errors)
	validate_meta_properties(name, payload, field_map, errors)
	validate_permissions(name, payload, errors)
	validate_contracts(name, payload, field_map, errors)
	validate_controller(name, path, errors)


def validate_protected_field_registry(
	name: str,
	payload: dict[str, Any],
	field_map: dict[str, dict[str, Any]],
	errors: list[str],
) -> None:
	protected_fields = PROTECTED_FIELDS_BY_DOCTYPE.get(name, frozenset())
	if not protected_fields:
		return
	if int(payload.get("track_changes") or 0):
		errors.append(f"{name}: protected records must disable Frappe Version sidecars with track_changes=0")
	for fieldname in sorted(protected_fields):
		field = field_map.get(fieldname)
		if not field:
			errors.append(f"{name}.{fieldname}: release privacy registry field is missing")
			continue
		if int(field.get("permlevel") or 0) != 9:
			errors.append(f"{name}.{fieldname}: protected field must remain at permlevel 9")
		if not int(field.get("mask") or 0):
			errors.append(f"{name}.{fieldname}: protected field must remain masked")
		for property_name in ("in_list_view", "in_standard_filter", "search_index"):
			if int(field.get(property_name) or 0):
				errors.append(f"{name}.{fieldname}: protected field cannot enable {property_name}")
	search_fields = {
		item.strip() for item in str(payload.get("search_fields") or "").split(",") if item.strip()
	}
	leaked_search_fields = sorted(search_fields.intersection(protected_fields))
	if leaked_search_fields:
		errors.append(f"{name}: protected search_fields are forbidden: {', '.join(leaked_search_fields)}")
	if any(
		int(permission.get("permlevel") or 0) == 9 and permission.get("read")
		for permission in payload.get("permissions", [])
	):
		errors.append(f"{name}: protected permlevel 9 must have no generic reader")


def validate_field(
	doctype: str,
	field: dict[str, Any],
	field_map: dict[str, dict[str, Any]],
	doctypes: dict[str, tuple[Path, dict[str, Any]]],
	errors: list[str],
) -> None:
	fieldname = field.get("fieldname")
	fieldtype = field.get("fieldtype")
	if not isinstance(fieldname, str) or len(fieldname) > 64 or not FIELDNAME_PATTERN.fullmatch(fieldname):
		errors.append(f"{doctype}.{fieldname}: invalid Frappe fieldname")
		return
	if fieldname in RESERVED_FIELDNAMES:
		errors.append(f"{doctype}.{fieldname}: reserved Frappe fieldname")
	if fieldtype not in FIELD_TYPES:
		errors.append(f"{doctype}.{fieldname}: unsupported Frappe v17 fieldtype {fieldtype!r}")
	if field.get("reqd") and fieldtype in NO_VALUE_FIELD_TYPES - {"Table", "Table MultiSelect"}:
		errors.append(f"{doctype}.{fieldname}: {fieldtype} cannot be mandatory")
	if field.get("in_list_view") and fieldtype in NO_VALUE_FIELD_TYPES:
		errors.append(f"{doctype}.{fieldname}: {fieldtype} cannot be in list/grid view")
	if field.get("search_index") and fieldtype in TEXT_FIELD_TYPES:
		errors.append(f"{doctype}.{fieldname}: text-like fields cannot have search_index")
	if field.get("unique") and fieldtype not in UNIQUE_FIELD_TYPES:
		errors.append(f"{doctype}.{fieldname}: unique is invalid for {fieldtype}")
	if fieldtype == "Check" and str(field.get("default", "0")) not in {"0", "1"}:
		errors.append(f"{doctype}.{fieldname}: Check default must be 0 or 1")
	if fieldtype == "Select":
		options = select_options(field)
		if not options:
			errors.append(f"{doctype}.{fieldname}: Select requires non-empty options")
		default = field.get("default")
		if default not in (None, "") and str(default) not in options:
			errors.append(f"{doctype}.{fieldname}: default {default!r} is not a Select option")
	if fieldtype in {"Link", "Table", "Table MultiSelect"}:
		target = field.get("options")
		if not isinstance(target, str) or not target:
			errors.append(f"{doctype}.{fieldname}: {fieldtype} requires an options target")
			return
		if target not in doctypes and target not in EXTERNAL_LINK_TARGETS:
			errors.append(f"{doctype}.{fieldname}: unknown target DocType {target!r}")
		if fieldtype in {"Table", "Table MultiSelect"}:
			if target in doctypes and not doctypes[target][1].get("istable"):
				errors.append(f"{doctype}.{fieldname}: Table target {target} is not istable")
		if fieldtype == "Link" and target in doctypes and doctypes[target][1].get("istable"):
			errors.append(f"{doctype}.{fieldname}: Link cannot target child DocType {target}")
	if fieldtype == "Dynamic Link":
		target_field = field_map.get(str(field.get("options") or ""))
		if not target_field or target_field.get("fieldtype") not in {"Link", "Select"}:
			errors.append(f"{doctype}.{fieldname}: invalid Dynamic Link options pointer")


def validate_meta_properties(
	name: str,
	payload: dict[str, Any],
	field_map: dict[str, dict[str, Any]],
	errors: list[str],
) -> None:
	is_child = bool(payload.get("istable"))
	is_single = bool(payload.get("issingle"))
	if is_child and is_single:
		errors.append(f"{name}: DocType cannot be both istable and issingle")
	if is_child != (name in CHILD_DOCTYPES):
		errors.append(f"{name}: istable does not match the child-table contract")
	if is_single != (name in SINGLE_DOCTYPES):
		errors.append(f"{name}: issingle does not match the Single contract")
	if is_child:
		if payload.get("permissions"):
			errors.append(f"{name}: child DocType must not define permissions")
		if payload.get("autoname"):
			errors.append(f"{name}: child DocType must not define autoname")
		if payload.get("track_changes"):
			errors.append(f"{name}: child DocType must not track changes")
	elif is_single:
		if payload.get("autoname"):
			errors.append(f"{name}: Single DocType must not define autoname")
		for field in field_map.values():
			if field.get("unique") or field.get("search_index"):
				errors.append(f"{name}.{field['fieldname']}: Single fields cannot be unique/indexed")
	else:
		validate_autoname(name, payload.get("autoname"), field_map, errors)
	title_field = payload.get("title_field")
	if title_field and title_field not in field_map:
		errors.append(f"{name}: title_field {title_field!r} is missing")
	search_fields = [
		item.strip() for item in str(payload.get("search_fields") or "").split(",") if item.strip()
	]
	for fieldname in search_fields:
		field = field_map.get(fieldname)
		if not field:
			errors.append(f"{name}: search_field {fieldname!r} is missing")
		elif field.get("fieldtype") in NO_VALUE_FIELD_TYPES:
			errors.append(f"{name}: search_field {fieldname!r} has no value")
	if payload.get("sort_field") not in {
		None,
		"",
		"name",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"docstatus",
		"idx",
	} | set(field_map):
		errors.append(f"{name}: invalid sort_field {payload.get('sort_field')!r}")


def validate_autoname(
	doctype: str,
	autoname: Any,
	field_map: dict[str, dict[str, Any]],
	errors: list[str],
) -> None:
	if not isinstance(autoname, str) or not autoname:
		errors.append(f"{doctype}: normal DocType requires autoname")
		return
	if autoname in {"hash", "UUID"}:
		return
	if autoname.startswith("field:"):
		fieldname = autoname.removeprefix("field:")
		if fieldname not in field_map:
			errors.append(f"{doctype}: autoname references missing field {fieldname!r}")
		elif not field_map[fieldname].get("unique"):
			errors.append(f"{doctype}: field autoname must also be marked unique")
		return
	if autoname.startswith("format:"):
		variables = FORMAT_VARIABLE_PATTERN.findall(autoname)
		field_variables: list[str] = []
		for variable in variables:
			if set(variable) == {"#"} or variable in {"DD", "JJJ", "MM", "WW", "YY", "YYYY"}:
				continue
			if variable not in field_map:
				errors.append(f"{doctype}: autoname uses unknown variable {variable!r}")
			else:
				field_variables.append(variable)
		if "#" not in autoname and len(field_variables) < 2:
			errors.append(f"{doctype}: format autoname needs a series or a reviewed composite key")
		return
	errors.append(f"{doctype}: unsupported or unreviewed autoname expression {autoname!r}")


def validate_permissions(name: str, payload: dict[str, Any], errors: list[str]) -> None:
	permissions = payload.get("permissions") or []
	if not isinstance(permissions, list):
		errors.append(f"{name}: permissions must be an array")
		return
	roles = [permission.get("role") for permission in permissions if isinstance(permission, dict)]
	permission_keys = [
		(permission.get("role"), int(permission.get("permlevel") or 0))
		for permission in permissions
		if isinstance(permission, dict)
	]
	duplicates = sorted(
		f"{role}@{permlevel}" for (role, permlevel), count in Counter(permission_keys).items() if count > 1
	)
	if duplicates:
		errors.append(f"{name}: duplicate DocPerm role/permlevel entries: {', '.join(duplicates)}")
	for permission in permissions:
		if not isinstance(permission, dict):
			errors.append(f"{name}: permission entry must be an object")
			continue
		role = permission.get("role")
		if role not in APP_ROLES:
			errors.append(f"{name}: undeclared role in permissions: {role!r}")
		if role == "IONE QMS Auditor" and any(
			int(permission.get(action) or 0) for action in ("create", "delete", "share", "write")
		):
			errors.append(f"{name}: IONE QMS Auditor must remain read-only")
		if name in SENSITIVE_EXPORT_DOCTYPES and any(
			int(permission.get(action) or 0) for action in ("export", "print")
		):
			errors.append(f"{name}: standard export/print must be disabled pending approved watermark flow")
		if name in SINGLE_DOCTYPES and any(
			int(permission.get(action) or 0) for action in ("export", "report")
		):
			errors.append(f"{name}: Single DocType cannot grant report/export")
	if name in PATIENT_BEARING_DOCTYPES and "System Manager" in roles:
		errors.append(f"{name}: System Manager role must not automatically receive patient-data access")
	if name in MATERIALIZER_ONLY_DOCTYPES and any(
		int(permission.get("create") or 0) for permission in permissions if isinstance(permission, dict)
	):
		errors.append(f"{name}: create must remain disabled; use the controlled materializer")
	if name in {"IONE Agent Incident", "IONE Flow Run Link"}:
		service_permission = next(
			(permission for permission in permissions if permission.get("role") == "IONE Agent Service"),
			None,
		)
		if (
			not service_permission
			or not service_permission.get("create")
			or service_permission.get("read")
			or service_permission.get("write")
		):
			errors.append(f"{name}: Agent Service must have create-only DocPerm")
	if name in {"IONE AI Data Access Log", "IONE AI Execution Event"} and any(
		int(permission.get(action) or 0)
		for permission in permissions
		for action in ("create", "delete", "share", "write", "export", "print")
	):
		errors.append(f"{name}: append-only audit records must have no generic mutation/export permission")
	if name in {
		"IONE QC Rule Test Execution",
		"IONE QC Rule Shadow Execution",
		"IONE Definition Lineage Receipt",
		"IONE PHI Access Receipt",
	} and any(
		int(permission.get(action) or 0)
		for permission in permissions
		for action in ("create", "delete", "share", "write", "export", "print")
	):
		errors.append(f"{name}: append-only governance receipts must have no generic mutation/export")
	if name == "IONE QC Rule Shadow Feedback" and any(
		int(permission.get(action) or 0)
		for permission in permissions
		for action in ("delete", "share", "write", "export", "print")
	):
		errors.append(
			"IONE QC Rule Shadow Feedback: reviewers may create feedback but never mutate/export it"
		)
	if name in {
		"IONE AI Analysis Task",
		"IONE AI Candidate Finding",
		"IONE AI Report Draft",
	}:
		service_permission = next(
			(permission for permission in permissions if permission.get("role") == "IONE Agent Service"),
			None,
		)
		if service_permission and any(
			int(service_permission.get(permission_type) or 0)
			for permission_type in ("read", "write", "create", "delete", "export", "print", "share")
		):
			errors.append(
				f"{name}: Agent Service must use the governed API/tool path without generic DocPerm"
			)


def validate_contracts(
	name: str,
	payload: dict[str, Any],
	field_map: dict[str, dict[str, Any]],
	errors: list[str],
) -> None:
	required = REQUIRED_FIELDS.get(name, set())
	missing = required - set(field_map)
	if missing:
		errors.append(f"{name}: missing runtime-required fields: {', '.join(sorted(missing))}")
	if name in PHI_IDENTITY_FIELDS:
		for fieldname in PHI_IDENTITY_FIELDS[name]:
			field = field_map.get(fieldname)
			if (
				not field
				or int(field.get("permlevel") or 0) != 9
				or int(field.get("mask") or 0) != 1
				or int(field.get("in_list_view") or 0) != 0
				or int(field.get("in_standard_filter") or 0) != 0
				or int(field.get("search_index") or 0) != 0
			):
				errors.append(
					f"{name}.{fieldname}: direct identity fields must be masked, level-9, "
					"and absent from list/filter/index metadata"
				)
		if any(
			int(permission.get("permlevel") or 0) == 9 and int(permission.get("read") or 0)
			for permission in payload.get("permissions", [])
		):
			errors.append(f"{name}: no generic level-9 DocPerm may expose direct identity fields")
		expected_title, expected_search = SAFE_IDENTITY_TITLE_AND_SEARCH[name]
		actual_search = {
			fieldname.strip()
			for fieldname in str(payload.get("search_fields") or "").split(",")
			if fieldname.strip()
		}
		if payload.get("title_field") != expected_title or actual_search != expected_search:
			errors.append(f"{name}: title/search must use only the pseudonymous identity key")
	if name in {"IONE Migration State", "IONE Migration Completion Receipt"}:
		fingerprint = field_map.get("full_fingerprint")
		if (
			not fingerprint
			or fingerprint.get("fieldtype") != "Data"
			or int(fingerprint.get("length") or 140) != 64
			or not fingerprint.get("reqd")
			or not fingerprint.get("read_only")
		):
			errors.append(f"{name}.full_fingerprint: must be a required read-only 64-character Data field")
	for (doctype, fieldname), expected_default in SAFETY_CRITICAL_DEFAULTS.items():
		if doctype != name:
			continue
		field = field_map.get(fieldname)
		if not field or str(field.get("default") or "0") != expected_default:
			errors.append(f"{name}.{fieldname}: safety-critical default must remain {expected_default!r}")
	expected_autoname = EXPECTED_AUTONAMES.get(name)
	if expected_autoname and payload.get("autoname") != expected_autoname:
		errors.append(f"{name}: autoname must be {expected_autoname!r}")
	for fieldname in UNIQUE_REQUIRED_FIELDS.get(name, set()):
		field = field_map.get(fieldname)
		if not field or not field.get("unique"):
			errors.append(f"{name}.{fieldname}: idempotency/projection key must be unique")
	for fieldname in INDEX_REQUIRED_FIELDS.get(name, set()):
		field = field_map.get(fieldname)
		if not field or not (field.get("search_index") or field.get("unique")):
			errors.append(f"{name}.{fieldname}: high-volume lookup field must be indexed")
	for (doctype, fieldname), required_options in REQUIRED_SELECT_OPTIONS.items():
		if doctype != name:
			continue
		field = field_map.get(fieldname)
		if not field or field.get("fieldtype") != "Select":
			errors.append(f"{name}.{fieldname}: required Select field is missing")
			continue
		missing_options = required_options - select_options(field)
		if missing_options:
			errors.append(
				f"{name}.{fieldname}: missing code-required options: " + ", ".join(sorted(missing_options))
			)
	agent_category_field = {
		"IONE Agent Policy": "agent_category",
		"IONE AI Analysis Task": "task_type",
	}.get(name)
	if agent_category_field:
		field = field_map.get(agent_category_field)
		if not field or not field.get("reqd") or select_options(field) != AGENT_CATEGORIES:
			errors.append(
				f"{name}.{agent_category_field}: must be required and exactly match the shared "
				"agent-category contract"
			)
	for fieldname, field in field_map.items():
		if fieldname in LONG_IDENTIFIER_FIELDS and field.get("fieldtype") == "Data":
			if int(field.get("length") or 140) < 200:
				errors.append(
					f"{name}.{fieldname}: length must support the 200-character integration contract"
				)
		if (
			field.get("fieldtype") == "Data"
			and (fieldname in DIGEST_FIELDNAMES or fieldname.endswith("_hash") or fieldname == "checksum")
			and int(field.get("length") or 140) != 64
		):
			errors.append(f"{name}.{fieldname}: SHA-256 field length must be 64")
	if name == "IONE QC Finding":
		physician = next(
			(
				permission
				for permission in payload.get("permissions", [])
				if permission.get("role") == "IONE Physician"
			),
			None,
		)
		if (
			not physician
			or not physician.get("read")
			or not physician.get("write")
			or physician.get("create")
			or physician.get("delete")
		):
			errors.append("IONE QC Finding: scoped Physician requires read/write without create/delete")
		agent_reviewer = next(
			(
				permission
				for permission in payload.get("permissions", [])
				if permission.get("role") == "IONE Agent Reviewer"
			),
			None,
		)
		if not agent_reviewer or not agent_reviewer.get("read"):
			errors.append("IONE QC Finding: Agent Reviewer needs scoped read access")
		elif any(agent_reviewer.get(action) for action in ("create", "delete", "write")):
			errors.append("IONE QC Finding: Agent Reviewer must remain strictly read-only")
	if name == "IONE QC Finding Appeal":
		permissions = payload.get("permissions", [])
		level_zero_readers = {
			permission.get("role")
			for permission in permissions
			if int(permission.get("permlevel") or 0) == 0 and permission.get("read")
		}
		expected_level_zero = {
			"IONE Physician",
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
			"IONE QMS Auditor",
		}
		if level_zero_readers != expected_level_zero:
			errors.append(
				"IONE QC Finding Appeal: metadata readers must be exactly the scoped "
				"submitter, reviewer, and auditor roles"
			)
		level_one_readers = {
			permission.get("role")
			for permission in permissions
			if int(permission.get("permlevel") or 0) == 1 and permission.get("read")
		}
		expected_level_one = expected_level_zero - {"IONE QMS Auditor"}
		if level_one_readers != expected_level_one:
			errors.append(
				"IONE QC Finding Appeal: clinical rationale may only be read by scoped "
				"submitter and reviewer roles"
			)
		if any(
			permission.get(action)
			for permission in permissions
			for action in ("create", "delete", "export", "print", "share", "write")
		):
			errors.append("IONE QC Finding Appeal: all mutations must use governed APIs")
		active_key = field_map.get("active_key")
		if (
			not active_key
			or not active_key.get("unique")
			or not active_key.get("read_only")
			or not active_key.get("hidden")
		):
			errors.append(
				"IONE QC Finding Appeal.active_key: active appeal singleton key must be "
				"hidden, read-only, and unique"
			)
		for fieldname in (
			"reason",
			"evidence_summary",
			"evidence_record",
			"evidence_file",
			"evidence_content_hash",
			"review_comment",
			"withdraw_comment",
		):
			field = field_map.get(fieldname)
			if not field or int(field.get("permlevel") or 0) != 1 or not field.get("read_only"):
				errors.append(
					f"IONE QC Finding Appeal.{fieldname}: clinical appeal content must be "
					"read-only at permlevel 1"
				)
		for fieldname in (
			"finding",
			"appeal_type",
			"hospital",
			"campus",
			"department",
			"patient",
			"encounter",
			"submission_hash",
			"status",
			"submitted_by",
			"submitted_at",
			"reviewed_by",
			"reviewed_at",
		):
			if not field_map.get(fieldname, {}).get("read_only"):
				errors.append(f"IONE QC Finding Appeal.{fieldname}: audit field must be read-only")
		if payload.get("track_changes"):
			errors.append(
				"IONE QC Finding Appeal: Version tracking must remain disabled to prevent "
				"permlevel-1 comment leakage through docinfo"
			)
	if name == "IONE QC Finding Appeal Evidence":
		readers = {
			permission.get("role") for permission in payload.get("permissions", []) if permission.get("read")
		}
		expected_readers = {
			"IONE Physician",
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
		}
		if readers != expected_readers or "IONE QMS Auditor" in readers:
			errors.append(
				"IONE QC Finding Appeal Evidence: readers must be exactly scoped "
				"submitter/reviewer roles and exclude Auditor"
			)
		if any(
			permission.get(action)
			for permission in payload.get("permissions", [])
			for action in ("create", "delete", "export", "print", "share", "write")
		):
			errors.append("IONE QC Finding Appeal Evidence: all mutations must use governed APIs")
		if payload.get("track_changes"):
			errors.append("IONE QC Finding Appeal Evidence: Version tracking must remain disabled")
		for fieldname in (
			"finding",
			"evidence_file",
			"content_hash",
			"file_name",
			"file_size",
			"appeal_submission_hash",
			"submitted_by",
			"submitted_at",
		):
			if not field_map.get(fieldname, {}).get("read_only"):
				errors.append(f"IONE QC Finding Appeal Evidence.{fieldname}: audit field must be read-only")
		evidence_file = field_map.get("evidence_file")
		if (
			not evidence_file
			or evidence_file.get("fieldtype") != "Link"
			or evidence_file.get("options") != "File"
			or not evidence_file.get("unique")
		):
			errors.append("IONE QC Finding Appeal Evidence.evidence_file: exact File link must be unique")
	if name == "IONE AI Analysis Task":
		permissions = payload.get("permissions", [])
		if any(permission.get("role") == "IONE Agent Administrator" for permission in permissions):
			errors.append("IONE AI Analysis Task: Agent Administrator must not receive clinical task access")
	if name == "IONE Agent Release":
		permissions = {permission.get("role"): permission for permission in payload.get("permissions", [])}
		agent_administrator = permissions.get("IONE Agent Administrator")
		if (
			not agent_administrator
			or not agent_administrator.get("read")
			or not agent_administrator.get("write")
			or not agent_administrator.get("create")
		):
			errors.append("IONE Agent Release: Agent Administrator requires read/write/create access")
		for role in ("IONE Agent Reviewer", "IONE QC Reviewer", "IONE Medical Affairs"):
			permission = permissions.get(role)
			if not permission or not permission.get("read") or not permission.get("write"):
				errors.append(f"IONE Agent Release: {role} requires approval write access")
		for role in ("IONE QMS Auditor", "System Manager"):
			permission = permissions.get(role)
			if not permission or not permission.get("read") or permission.get("write"):
				errors.append(f"IONE Agent Release: {role} must remain read-only")
		for permission in permissions.values():
			if any(permission.get(action) for action in ("delete", "export", "print")):
				errors.append(
					"IONE Agent Release: delete and standard export/print permissions must remain disabled"
				)
				break
		active_key = field_map.get("active_parent_key")
		if (
			not active_key
			or not active_key.get("unique")
			or not active_key.get("read_only")
			or not active_key.get("hidden")
		):
			errors.append(
				"IONE Agent Release.active_parent_key: approved release singleton key must be "
				"hidden, read-only, and unique"
			)
	if name == "IONE Agent Evaluation Threshold Policy":
		permissions = payload.get("permissions", [])
		for role in (
			"IONE Agent Administrator",
			"IONE Agent Reviewer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
		):
			permission = next(
				(item for item in permissions if item.get("role") == role and item.get("read")),
				None,
			)
			if not permission or not permission.get("write"):
				errors.append(
					f"IONE Agent Evaluation Threshold Policy: {role} requires governed write access"
				)
		for permission in permissions:
			if any(permission.get(action) for action in ("delete", "export", "print", "share")):
				errors.append(
					"IONE Agent Evaluation Threshold Policy: destructive/export/share permissions "
					"must remain disabled"
				)
				break
		active_key = field_map.get("active_parent_key")
		if (
			not active_key
			or not active_key.get("unique")
			or not active_key.get("read_only")
			or not active_key.get("hidden")
		):
			errors.append(
				"IONE Agent Evaluation Threshold Policy.active_parent_key must be hidden, "
				"read-only, and unique"
			)
		for fieldname in ("checksum", "approved_by", "approved_at", "approval_signature"):
			if not field_map.get(fieldname, {}).get("read_only"):
				errors.append(
					f"IONE Agent Evaluation Threshold Policy.{fieldname}: approval artifact must be read-only"
				)
	if name == "IONE Agent Evaluation":
		permissions = payload.get("permissions", [])
		if any(
			permission.get(action)
			for permission in permissions
			for action in ("create", "delete", "share", "write")
		):
			errors.append("IONE Agent Evaluation: governed result/review has no generic mutation permission")
		for fieldname in (
			"release_checksum",
			"test_suite_hash",
			"model_configuration_hash",
			"prompt_hash",
			"instructions_hash",
			"tool_schema_hash",
			"policy_checksum",
			"knowledge_base_manifest_hash",
			"threshold_policy",
			"threshold_policy_hash",
			"evaluator_code_hash",
			"category_matrix_hash",
			"result_hash",
			"receipt_hash",
			"status",
			"metrics_json",
			"sample_count",
			"passed_count",
			"failed_count",
			"evaluated_by",
			"evaluated_at",
			"requested_by",
			"review_status",
			"reviewed_by",
			"reviewed_at",
			"review_comment",
		):
			if not field_map.get(fieldname, {}).get("read_only"):
				errors.append(f"IONE Agent Evaluation.{fieldname}: governed artifact must be read-only")
	if name == "IONE AI Tool Approval":
		permissions = payload.get("permissions", [])
		for forbidden_role in (
			"System Manager",
			"IONE Agent Administrator",
			"IONE Agent Service",
			"IONE Integration Operator",
		):
			if any(permission.get("role") == forbidden_role for permission in permissions):
				errors.append(f"IONE AI Tool Approval: {forbidden_role} must not receive access")
		if any(
			permission.get(action)
			for permission in permissions
			for action in ("create", "delete", "export", "print", "share", "write")
		):
			errors.append("IONE AI Tool Approval: state changes must use only governed APIs")
		level_one_readers = {
			permission.get("role")
			for permission in permissions
			if int(permission.get("permlevel") or 0) == 1 and permission.get("read")
		}
		if level_one_readers != {
			"IONE Agent Reviewer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
		}:
			errors.append("IONE AI Tool Approval: raw arguments may only be read by accountable reviewers")
		for fieldname in ("arguments_json", "question_prompt"):
			field = field_map.get(fieldname)
			if not field or int(field.get("permlevel") or 0) != 1 or not field.get("read_only"):
				errors.append(
					f"IONE AI Tool Approval.{fieldname}: raw approval content must be read-only "
					"at permlevel 1"
				)
	if name == "IONE Data Export Request":
		permissions = {permission.get("role"): permission for permission in payload.get("permissions", [])}
		requester_roles = {
			"IONE Agent Reviewer",
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE Nursing/Pharmacy/IC QC",
			"IONE Physician",
			"IONE QC Reviewer",
		}
		for role in requester_roles:
			permission = permissions.get(role)
			if (
				not permission
				or not permission.get("read")
				or not permission.get("create")
				or not permission.get("if_owner")
				or permission.get("write")
			):
				errors.append(f"IONE Data Export Request: {role} must have owner-only read/create")
		medical_affairs = permissions.get("IONE Medical Affairs")
		if not medical_affairs or not medical_affairs.get("read") or not medical_affairs.get("write"):
			errors.append("IONE Data Export Request: Medical Affairs requires approval write access")
		auditor = permissions.get("IONE QMS Auditor")
		if not auditor or not auditor.get("read") or auditor.get("write"):
			errors.append("IONE Data Export Request: Auditor requires read-only audit access")
		if "System Manager" in permissions:
			errors.append("IONE Data Export Request: System Manager must not receive export-request access")
		for permission in permissions.values():
			if any(permission.get(action) for action in ("export", "print")):
				errors.append("IONE Data Export Request: standard export/print must remain disabled")
				break
		status = field_map.get("status")
		expected_statuses = {
			"Pending",
			"Approved",
			"Processing",
			"Completed",
			"Rejected",
			"Failed",
			"Expired",
		}
		if not status or select_options(status) != expected_statuses:
			errors.append("IONE Data Export Request.status: controlled-export lifecycle must be exact")
		scope_mode = field_map.get("scope_mode")
		if (
			not scope_mode
			or select_options(scope_mode) != {"Department", "Campus", "Hospital", "Multiple", "Unscoped"}
			or not scope_mode.get("reqd")
			or not scope_mode.get("read_only")
		):
			errors.append("IONE Data Export Request.scope_mode: immutable governed scope modes must be exact")
		for fieldname, linked_doctype in (
			("hospital", "IONE Hospital"),
			("campus", "IONE Hospital Campus"),
			("department", "IONE Medical Department"),
		):
			field = field_map.get(fieldname)
			if (
				not field
				or field.get("fieldtype") != "Link"
				or field.get("options") != linked_doctype
				or not field.get("read_only")
				or not field.get("ignore_user_permissions")
			):
				errors.append(
					f"IONE Data Export Request.{fieldname}: frozen scope must be a read-only "
					f"Link to {linked_doctype} with explicit permission handling"
				)
		output_file = field_map.get("output_file")
		if not output_file or output_file.get("fieldtype") != "Link" or output_file.get("options") != "File":
			errors.append("IONE Data Export Request.output_file: must remain a Link to File")
		for fieldname in (
			"approved_at",
			"approved_by",
			"output_file",
			"record_hash",
			"watermark",
		):
			if not field_map.get(fieldname, {}).get("read_only"):
				errors.append(f"IONE Data Export Request.{fieldname}: approval output must be read-only")


def validate_controller(name: str, json_path: Path, errors: list[str]) -> None:
	controller_path = json_path.with_suffix(".py")
	if not controller_path.is_file():
		errors.append(f"{name}: missing controller {controller_path.relative_to(ROOT)}")
		return
	try:
		tree = ast.parse(controller_path.read_text(encoding="utf-8"))
	except (OSError, SyntaxError) as exc:
		errors.append(f"{name}: controller is not valid Python: {exc}")
		return
	classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
	expected = class_name(name)
	if expected not in classes:
		errors.append(f"{name}: controller class {expected} is missing")
	if not (controller_path.parent / "__init__.py").is_file():
		errors.append(f"{name}: missing DocType package __init__.py")


def validate_hooks(
	doctypes: dict[str, tuple[Path, dict[str, Any]]],
	errors: list[str],
) -> None:
	hooks_path = PACKAGE / "hooks.py"
	if not hooks_path.is_file():
		return
	try:
		tree = ast.parse(hooks_path.read_text(encoding="utf-8"))
	except (OSError, SyntaxError) as exc:
		errors.append(f"ione_qms/hooks.py: invalid Python: {exc}")
		return
	hook_doctypes = extract_hook_doctype_keys(tree)
	known_hook_doctypes = set(doctypes) | {
		"Assignment Rule",
		"Auto Email Report",
		"Auto Repeat",
		"Client Script",
		"Comment",
		"Communication",
		"Communication Link",
		"Custom DocPerm",
		"Custom Field",
		"Dashboard Chart",
		"Document Naming Rule",
		"File",
		"Flow Agent",
		"Flow Model",
		"Flow Run",
		"Flow Session",
		"Flow Tool",
		"Flow Trigger",
		"List Filter",
		"Notification",
		"Number Card",
		"Prepared Report",
		"Print Format",
		"Property Setter",
		"Report",
		"Server Script",
		"ToDo",
		"Version",
		"Web Form",
		"Webhook",
		"Workflow",
	}
	for doctype in sorted(hook_doctypes - known_hook_doctypes):
		errors.append(f"ione_qms/hooks.py: hook references unknown DocType {doctype}")
	targets = {
		node.value
		for node in ast.walk(tree)
		if isinstance(node, ast.Constant)
		and isinstance(node.value, str)
		and node.value.startswith("ione_qms.")
	}
	for target in sorted(targets):
		validate_python_target(target, errors)
	required_apps = literal_assignment(tree, "required_apps")
	if not isinstance(required_apps, list) or "flow" not in required_apps:
		errors.append("ione_qms/hooks.py: required_apps must include flow")


def extract_hook_doctype_keys(tree: ast.Module) -> set[str]:
	keys: set[str] = set()
	for assignment_name in (
		"doc_events",
		"has_permission",
		"permission_query_conditions",
	):
		value = literal_assignment(tree, assignment_name)
		if isinstance(value, dict):
			keys.update(str(item) for item in value)
	return keys


def literal_assignment(tree: ast.Module, name: str) -> Any:
	for node in tree.body:
		if not isinstance(node, ast.Assign):
			continue
		if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
			try:
				return ast.literal_eval(node.value)
			except ValueError, SyntaxError:
				return None
	return None


def validate_python_target(target: str, errors: list[str]) -> None:
	parts = target.split(".")
	if len(parts) < 3 or parts[0] != "ione_qms":
		errors.append(f"unreviewed local hook target: {target}")
		return
	module_path = ROOT.joinpath(*parts[:-1]).with_suffix(".py")
	if not module_path.is_file():
		errors.append(f"hook target module does not exist: {target}")
		return
	try:
		tree = ast.parse(module_path.read_text(encoding="utf-8"))
	except (OSError, SyntaxError) as exc:
		errors.append(f"hook target module is invalid Python ({target}): {exc}")
		return
	symbol = parts[-1]
	defined = {
		node.name
		for node in tree.body
		if isinstance(node, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef))
	}
	if symbol not in defined:
		errors.append(f"hook target symbol does not exist: {target}")


def select_options(field: dict[str, Any]) -> set[str]:
	return {option.strip() for option in str(field.get("options") or "").splitlines() if option.strip()}


def main() -> int:
	errors: list[str] = []
	validate_artifacts(errors)
	doctypes = load_doctypes(errors)
	validate_catalog(doctypes, errors)
	validate_modules(doctypes, errors)
	for name, (path, payload) in sorted(doctypes.items()):
		validate_doctype(name, path, payload, doctypes, errors)
	validate_hooks(doctypes, errors)
	if errors:
		print(f"IONE QMS metadata validation failed with {len(errors)} error(s):")
		for error in errors:
			print(f"- {error}")
		return 1
	print(
		f"IONE QMS metadata validation passed: {len(doctypes)} DocTypes, "
		f"{len(REQUIRED_DOCTYPES_BY_MODULE)} modules."
	)
	return 0


if __name__ == "__main__":
	sys.exit(main())
