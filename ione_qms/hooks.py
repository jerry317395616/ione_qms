from ione_qms.services.scope_hierarchy import (
	CLINICAL_SCOPE_DOCTYPES as _CLINICAL_SCOPE_DOCTYPES,
)

app_name = "ione_qms"
app_title = "IONE QMS"
app_publisher = "IONE"
app_description = "医院医疗质量管理、持续改进、分析、集成与受治理 AI 平台"
app_email = "317395616@qq.com"
app_license = "mit"
required_apps = ["flow"]

app_home = "/app/ione-quality-command-center"
app_logo_url = "/assets/ione_qms/images/ione-qms-logo.svg"
app_include_css = "/assets/ione_qms/css/ione_qms.css"
doctype_js = {
	"IONE QC Finding": "public/js/qc_finding.js",
	"IONE QC Finding Evidence": "public/js/qc_finding_evidence.js",
	"IONE Source Document Locator": "public/js/source_document_locator.js",
	"IONE Staff Qualification": "public/js/surgery_governance.js",
	"IONE Surgery Authorization": "public/js/surgery_governance.js",
	"IONE Surgery Procedure Policy": "public/js/surgery_governance.js",
	"IONE Surgery QC": "public/js/surgery_governance.js",
	"IONE Surgery Emergency Exception": "public/js/surgery_governance.js",
	"IONE Finding Recurrence Policy": "public/js/finding_recurrence_policy.js",
	"IONE PDCA Project": "public/js/pdca_project.js",
	"IONE AI Report Schedule": "public/js/ai_report_governance.js",
	"IONE AI Report Draft": "public/js/ai_report_governance.js",
	"IONE Medical Record Sampling Policy": "public/js/medical_record_review.js",
	"IONE Medical Record Review Assignment": "public/js/medical_record_review.js",
	"IONE Medical Record Archive Decision": "public/js/medical_record_review.js",
	"IONE Quality Meeting": "public/js/quality_meetings.js",
	"IONE Meeting Minute": "public/js/quality_meetings.js",
	"IONE Meeting Decision": "public/js/quality_meetings.js",
	"IONE Quality Action Item": "public/js/quality_meetings.js",
	"IONE Quality Experience Share": "public/js/quality_meetings.js",
	"IONE PHI Disclosure Policy": "public/js/phi_disclosure_policy.js",
	"IONE Patient Index": "public/js/phi_identity.js",
	"IONE Encounter Index": "public/js/phi_identity.js",
	"IONE Production Readiness Assessment": "public/js/production_readiness.js",
	"IONE System Settings": "public/js/production_readiness.js",
}

add_to_apps_screen = [
	{
		"name": app_name,
		"title": app_title,
		"route": app_home,
		"logo": app_logo_url,
		"has_permission": "ione_qms.permissions.has_app_permission",
	}
]

before_install = "ione_qms.setup.install.before_install"
after_install = "ione_qms.setup.install.after_install"
after_migrate = "ione_qms.setup.install.after_migrate"
before_tests = "ione_qms.services.runtime_settings.verify_test_migration_runtime"
before_uninstall = "ione_qms.setup.install.before_uninstall"
before_request = ["ione_qms.overrides.phi_query_guard.prevent_unsafe_phi_query_request"]
after_request = ["ione_qms.overrides.security_headers.apply_security_headers"]

permission_query_conditions = {
	"File": "ione_qms.overrides.file.qms_file_query",
	"IONE Medical Staff": "ione_qms.permissions.medical_staff_query",
	"IONE Staff Qualification": "ione_qms.permissions.staff_qualification_query",
	"IONE Surgery Authorization": "ione_qms.permissions.surgery_authorization_query",
	"IONE Surgery Procedure Policy": "ione_qms.permissions.surgery_procedure_policy_query",
	"IONE Patient Index": "ione_qms.permissions.patient_query",
	"IONE Encounter Index": "ione_qms.permissions.encounter_query",
	"IONE Clinical Quality Event": "ione_qms.permissions.clinical_event_query",
	"IONE QC Execution": "ione_qms.permissions.qc_execution_query",
	"IONE QC Rule Shadow Execution": "ione_qms.permissions.rule_shadow_execution_query",
	"IONE QC Rule Shadow Feedback": "ione_qms.permissions.rule_shadow_feedback_query",
	"IONE QC Finding": "ione_qms.permissions.qc_finding_query",
	"IONE QC Finding Evidence": "ione_qms.permissions.qc_finding_evidence_query",
	"IONE Source Document Locator": "ione_qms.permissions.source_document_locator_query",
	"IONE Source Document Access Log": "ione_qms.permissions.source_document_access_log_query",
	"IONE QC Finding Appeal": "ione_qms.permissions.finding_appeal_query",
	"IONE QC Finding Appeal Evidence": "ione_qms.permissions.finding_appeal_evidence_query",
	"IONE Medical Record QC": "ione_qms.permissions.medical_record_qc_query",
	"IONE Medical Record Sampling Policy": "ione_qms.permissions.medical_record_sampling_policy_query",
	"IONE Medical Record Sampling Policy Operation": (
		"ione_qms.permissions.medical_record_sampling_policy_operation_query"
	),
	"IONE Medical Record Review Batch": "ione_qms.permissions.medical_record_review_batch_query",
	"IONE Medical Record Review Assignment": "ione_qms.permissions.medical_record_review_assignment_query",
	"IONE Medical Record Review Decision": "ione_qms.permissions.medical_record_review_decision_query",
	"IONE Medical Record Archive Decision": "ione_qms.permissions.medical_record_archive_decision_query",
	"IONE Medical Record Archive Acknowledgement": (
		"ione_qms.permissions.medical_record_archive_acknowledgement_query"
	),
	"IONE Medical Record Archive Delivery": ("ione_qms.permissions.medical_record_archive_delivery_query"),
	"IONE Medical Record Completeness Watermark": (
		"ione_qms.permissions.medical_record_completeness_watermark_query"
	),
	"IONE Surgery QC": "ione_qms.permissions.surgery_qc_query",
	"IONE Surgery MDT Record": "ione_qms.permissions.surgery_mdt_record_query",
	"IONE Surgery MDT Participant": "ione_qms.permissions.surgery_mdt_participant_query",
	"IONE Surgery Safety Checklist": "ione_qms.permissions.surgery_safety_checklist_query",
	"IONE Surgery Emergency Exception": "ione_qms.permissions.surgery_emergency_exception_query",
	"IONE Medical Safety Event": "ione_qms.permissions.medical_safety_event_query",
	"IONE Indicator Alert": "ione_qms.permissions.indicator_alert_query",
	"IONE QC Rectification": "ione_qms.permissions.rectification_query",
	"IONE QC Verification": "ione_qms.permissions.verification_query",
	"IONE PDCA Project": "ione_qms.permissions.pdca_project_query",
	"IONE Quality Meeting": "ione_qms.permissions.quality_meeting_query",
	"IONE Meeting Minute": "ione_qms.permissions.meeting_minute_query",
	"IONE Meeting Decision": "ione_qms.permissions.meeting_decision_query",
	"IONE Quality Action Item": "ione_qms.permissions.quality_action_item_query",
	"IONE Quality Action Verification Round": (
		"ione_qms.permissions.quality_action_verification_round_query"
	),
	"IONE Quality Experience Share": "ione_qms.permissions.quality_experience_share_query",
	"IONE Finding Recurrence Policy": "ione_qms.permissions.finding_recurrence_policy_query",
	"IONE Finding Recurrence Run": "ione_qms.permissions.finding_recurrence_run_query",
	"IONE Finding Recurrence Evaluation": ("ione_qms.permissions.finding_recurrence_evaluation_query"),
	"IONE Indicator Result": "ione_qms.permissions.indicator_result_query",
	"IONE Indicator Result Detail": "ione_qms.permissions.indicator_result_detail_query",
	"IONE Indicator Result Pointer": "ione_qms.permissions.indicator_result_pointer_query",
	"IONE Daily Quality Fact": "ione_qms.permissions.daily_quality_fact_query",
	"IONE Monthly Quality Fact": "ione_qms.permissions.monthly_quality_fact_query",
	"IONE Finding Analysis Fact": "ione_qms.permissions.finding_analysis_fact_query",
	"IONE Surgery Quality Fact": "ione_qms.permissions.surgery_quality_fact_query",
	"IONE Agent Analysis Fact": "ione_qms.permissions.agent_analysis_fact_query",
	"IONE AI Analysis Task": "ione_qms.permissions.analysis_task_query",
	"IONE AI Candidate Finding": "ione_qms.permissions.candidate_finding_query",
	"IONE AI Report Draft": "ione_qms.permissions.report_draft_query",
	"IONE AI Report Schedule": "ione_qms.permissions.quality_report_schedule_query",
	"IONE Quality Report Snapshot": "ione_qms.permissions.quality_report_snapshot_query",
	"IONE AI Report Recovery Authorization": (
		"ione_qms.permissions.quality_report_recovery_authorization_query"
	),
	"IONE AI Tool Approval": "ione_qms.permissions.tool_approval_query",
	"IONE AI Data Access Log": "ione_qms.permissions.ai_data_access_log_query",
	"IONE AI Execution Event": "ione_qms.permissions.ai_execution_event_query",
	"IONE Flow Run Link": "ione_qms.permissions.flow_run_link_query",
	"IONE Data Export Request": "ione_qms.permissions.data_export_request_query",
	"IONE Integration Message": "ione_qms.permissions.integration_message_query",
	"IONE Data Quality Issue": "ione_qms.permissions.data_quality_issue_query",
	"Flow Agent": "ione_qms.permissions.flow_agent_query",
	"Flow Model": "ione_qms.permissions.flow_model_query",
	"Flow Run": "ione_qms.permissions.flow_run_query",
	"Flow Session": "ione_qms.permissions.flow_session_query",
}

has_permission = {
	"File": "ione_qms.overrides.file.qms_file_permission",
	"IONE Medical Staff": "ione_qms.permissions.medical_staff_permission",
	"IONE Staff Qualification": "ione_qms.permissions.surgery_governance_definition_permission",
	"IONE Surgery Authorization": "ione_qms.permissions.surgery_governance_definition_permission",
	"IONE Surgery Procedure Policy": "ione_qms.permissions.surgery_governance_definition_permission",
	"IONE Patient Index": "ione_qms.permissions.patient_permission",
	"IONE Encounter Index": "ione_qms.permissions.encounter_permission",
	"IONE Clinical Quality Event": "ione_qms.permissions.clinical_permission",
	"IONE QC Execution": "ione_qms.permissions.clinical_permission",
	"IONE QC Rule Shadow Execution": "ione_qms.permissions.clinical_permission",
	"IONE QC Rule Shadow Feedback": "ione_qms.permissions.clinical_permission",
	"IONE QC Finding": "ione_qms.permissions.finding_permission",
	"IONE QC Finding Evidence": "ione_qms.permissions.qc_finding_evidence_permission",
	"IONE Source Document Locator": "ione_qms.permissions.source_document_locator_permission",
	"IONE Source Document Access Log": ("ione_qms.permissions.source_document_access_log_permission"),
	"IONE QC Finding Appeal": "ione_qms.permissions.finding_appeal_permission",
	"IONE QC Finding Appeal Evidence": "ione_qms.permissions.finding_appeal_evidence_permission",
	"IONE Medical Record QC": "ione_qms.permissions.medical_record_qc_permission",
	"IONE Medical Record Sampling Policy": "ione_qms.permissions.medical_record_sampling_policy_permission",
	"IONE Medical Record Sampling Policy Operation": (
		"ione_qms.permissions.medical_record_sampling_policy_operation_permission"
	),
	"IONE Medical Record Review Batch": "ione_qms.permissions.medical_record_review_batch_permission",
	"IONE Medical Record Review Assignment": (
		"ione_qms.permissions.medical_record_review_assignment_permission"
	),
	"IONE Medical Record Review Decision": "ione_qms.permissions.medical_record_review_decision_permission",
	"IONE Medical Record Archive Decision": (
		"ione_qms.permissions.medical_record_archive_decision_permission"
	),
	"IONE Medical Record Archive Acknowledgement": (
		"ione_qms.permissions.medical_record_archive_acknowledgement_permission"
	),
	"IONE Medical Record Archive Delivery": (
		"ione_qms.permissions.medical_record_archive_delivery_permission"
	),
	"IONE Medical Record Completeness Watermark": (
		"ione_qms.permissions.medical_record_completeness_watermark_permission"
	),
	"IONE Surgery QC": "ione_qms.permissions.surgery_governance_audit_permission",
	"IONE Surgery MDT Record": "ione_qms.permissions.surgery_governance_audit_permission",
	"IONE Surgery MDT Participant": "ione_qms.permissions.surgery_governance_audit_permission",
	"IONE Surgery Safety Checklist": "ione_qms.permissions.surgery_governance_audit_permission",
	"IONE Surgery Emergency Exception": "ione_qms.permissions.surgery_governance_audit_permission",
	"IONE Medical Safety Event": "ione_qms.permissions.clinical_permission",
	"IONE Indicator Alert": "ione_qms.permissions.analytics_permission",
	"IONE QC Rectification": "ione_qms.permissions.finding_permission",
	"IONE QC Verification": "ione_qms.permissions.finding_permission",
	"IONE PDCA Project": "ione_qms.permissions.clinical_permission",
	"IONE Quality Meeting": "ione_qms.permissions.quality_meeting_permission",
	"IONE Meeting Minute": "ione_qms.permissions.meeting_minute_permission",
	"IONE Meeting Decision": "ione_qms.permissions.meeting_decision_permission",
	"IONE Quality Action Item": "ione_qms.permissions.quality_action_item_permission",
	"IONE Quality Action Verification Round": (
		"ione_qms.permissions.quality_action_verification_round_permission"
	),
	"IONE Quality Experience Share": "ione_qms.permissions.quality_experience_share_permission",
	"IONE Finding Recurrence Policy": "ione_qms.permissions.clinical_permission",
	"IONE Finding Recurrence Run": "ione_qms.permissions.clinical_permission",
	"IONE Finding Recurrence Evaluation": "ione_qms.permissions.clinical_permission",
	"IONE Indicator Result": "ione_qms.permissions.analytics_permission",
	"IONE Indicator Result Detail": "ione_qms.permissions.analytics_permission",
	"IONE Indicator Result Pointer": "ione_qms.permissions.analytics_permission",
	"IONE Daily Quality Fact": "ione_qms.permissions.analytics_permission",
	"IONE Monthly Quality Fact": "ione_qms.permissions.analytics_permission",
	"IONE Finding Analysis Fact": "ione_qms.permissions.analytics_permission",
	"IONE Surgery Quality Fact": "ione_qms.permissions.analytics_permission",
	"IONE Agent Analysis Fact": "ione_qms.permissions.analytics_permission",
	"IONE AI Analysis Task": "ione_qms.permissions.ai_task_permission",
	"IONE AI Candidate Finding": "ione_qms.permissions.ai_task_permission",
	"IONE AI Report Draft": "ione_qms.permissions.ai_task_permission",
	"IONE AI Report Schedule": "ione_qms.permissions.quality_report_schedule_permission",
	"IONE Quality Report Snapshot": "ione_qms.permissions.quality_report_snapshot_permission",
	"IONE AI Report Recovery Authorization": (
		"ione_qms.permissions.quality_report_recovery_authorization_permission"
	),
	"IONE AI Tool Approval": "ione_qms.permissions.tool_approval_permission",
	"IONE AI Data Access Log": "ione_qms.permissions.ai_data_access_log_permission",
	"IONE AI Execution Event": "ione_qms.permissions.ai_execution_event_permission",
	"IONE Flow Run Link": "ione_qms.permissions.flow_run_link_permission",
	"IONE Data Export Request": "ione_qms.permissions.data_export_request_permission",
	"IONE Integration Message": "ione_qms.permissions.integration_data_permission",
	"IONE Data Quality Issue": "ione_qms.permissions.integration_data_permission",
	"Flow Agent": "ione_qms.permissions.flow_agent_permission",
	"Flow Model": "ione_qms.permissions.flow_model_permission",
	"Flow Run": "ione_qms.permissions.flow_run_permission",
	"Flow Session": "ione_qms.permissions.flow_session_permission",
}

override_whitelisted_methods = {
	"frappe.core.doctype.data_export.exporter.export_data": ("ione_qms.overrides.data_export.export_data"),
	"flow.api.start_run": "ione_qms.ai.flow_gateway.start_run",
	"flow.api.resume_run": "ione_qms.ai.flow_gateway.resume_run",
	"flow.api.stop_run": "ione_qms.ai.flow_gateway.stop_run",
	"flow.api.recover_session": "ione_qms.ai.flow_gateway.recover_session",
	"flow.api.submit_feedback": "ione_qms.ai.flow_gateway.submit_feedback",
	"flow.api.get_agent_tools": "ione_qms.ai.flow_gateway.get_agent_tools",
	"flow.api.api.start_run": "ione_qms.ai.flow_gateway.start_run",
	"flow.api.api.resume_run": "ione_qms.ai.flow_gateway.resume_run",
	"flow.api.api.stop_run": "ione_qms.ai.flow_gateway.stop_run",
	"flow.api.api.recover_session": "ione_qms.ai.flow_gateway.recover_session",
	"flow.api.api.submit_feedback": "ione_qms.ai.flow_gateway.submit_feedback",
	"flow.api.api.get_agent_tools": "ione_qms.ai.flow_gateway.get_agent_tools",
}

extend_doctype_class = {
	"File": ["ione_qms.overrides.file.IONEQMSFileMixin"],
	"Flow Session": ["ione_qms.overrides.flow_session.IONEGovernedFlowSessionMixin"],
}

scheduler_events = {
	"cron": {
		"*/2 * * * *": ["ione_qms.tasks.integration.retry_due_messages"],
		"*/5 * * * *": [
			"ione_qms.tasks.integration.sync_incremental_data",
			"ione_qms.ai.approvals.expire_tool_approvals",
			"ione_qms.ai.orchestrator.recover_stale_analysis_tasks",
			"ione_qms.services.batch_reliability.recover_due_batch_runs",
		],
		"30 1 * * *": ["ione_qms.tasks.integration.reconcile_integrations"],
		"0 2 * * *": ["ione_qms.tasks.indicators.calculate_daily_indicators"],
		"10 2 * * *": ["ione_qms.tasks.indicators.reconcile_indicator_backlog"],
		"20 2 * * *": ["ione_qms.tasks.indicators.revalidate_completed_indicator_snapshots"],
		"0 3 * * *": ["ione_qms.tasks.quality.execute_daily_quality_scan"],
		"0 4 1 * *": ["ione_qms.tasks.analytics.build_monthly_facts"],
		"15 4 * * *": ["ione_qms.tasks.analytics.reconcile_monthly_facts"],
		"0 5 * * *": ["ione_qms.tasks.security.enforce_retention"],
		"15 * * * *": ["ione_qms.tasks.migrations.run_post_migrate_backfills"],
		"* * * * *": ["ione_qms.services.phi_access.activate_due_phi_disclosure_policies"],
		"30 5 * * *": ["ione_qms.services.data_export.expire_data_exports"],
		"0 6 * * *": ["ione_qms.services.ai_report_schedules.dispatch_monthly_quality_reports"],
		"10 * * * *": ["ione_qms.services.surgery_governance.expire_surgery_emergency_exceptions"],
		"30 6 * * *": ["ione_qms.services.finding_recurrence.dispatch_finding_recurrence_scans"],
		"0 8 * * *": ["ione_qms.tasks.quality.escalate_overdue_items"],
	}
}

doc_events = {
	"Comment": {
		"validate": "ione_qms.overrides.phi_query_guard.prevent_ione_phi_sidecar_persistence",
	},
	"Communication": {
		"validate": "ione_qms.overrides.phi_query_guard.prevent_ione_phi_sidecar_persistence",
	},
	"Communication Link": {
		"validate": "ione_qms.overrides.phi_query_guard.prevent_ione_phi_sidecar_persistence",
	},
	"ToDo": {
		"validate": "ione_qms.overrides.phi_query_guard.prevent_ione_phi_sidecar_persistence",
	},
	"Version": {
		"validate": "ione_qms.overrides.phi_query_guard.prevent_ione_phi_sidecar_persistence",
	},
	"Custom DocPerm": {
		"validate": "ione_qms.overrides.phi_query_guard.prevent_phi_metadata_override",
	},
	"Custom Field": {
		"validate": "ione_qms.overrides.phi_query_guard.prevent_phi_metadata_override",
	},
	"Property Setter": {
		"validate": "ione_qms.overrides.phi_query_guard.prevent_phi_metadata_override",
	},
	"Report": {
		"validate": "ione_qms.overrides.phi_query_guard.validate_phi_query_artifact",
	},
	"Assignment Rule": {
		"validate": "ione_qms.overrides.phi_query_guard.validate_phi_query_artifact",
	},
	"Auto Email Report": {
		"validate": "ione_qms.overrides.phi_query_guard.validate_phi_query_artifact",
	},
	"Auto Repeat": {
		"validate": "ione_qms.overrides.phi_query_guard.validate_phi_query_artifact",
	},
	"Client Script": {
		"validate": "ione_qms.overrides.phi_query_guard.validate_phi_query_artifact",
	},
	"Document Naming Rule": {
		"validate": "ione_qms.overrides.phi_query_guard.validate_phi_query_artifact",
	},
	"Prepared Report": {
		"validate": "ione_qms.overrides.phi_query_guard.validate_phi_query_artifact",
	},
	"Notification": {
		"validate": "ione_qms.overrides.phi_query_guard.validate_phi_query_artifact",
	},
	"Print Format": {
		"validate": "ione_qms.overrides.phi_query_guard.validate_phi_query_artifact",
	},
	"Server Script": {
		"validate": "ione_qms.overrides.phi_query_guard.validate_phi_query_artifact",
	},
	"Web Form": {
		"validate": "ione_qms.overrides.phi_query_guard.validate_phi_query_artifact",
	},
	"Webhook": {
		"validate": "ione_qms.overrides.phi_query_guard.validate_phi_query_artifact",
	},
	"Workflow": {
		"validate": "ione_qms.overrides.phi_query_guard.validate_phi_query_artifact",
	},
	"Dashboard Chart": {
		"validate": "ione_qms.overrides.phi_query_guard.validate_phi_query_artifact",
	},
	"Number Card": {
		"validate": "ione_qms.overrides.phi_query_guard.validate_phi_query_artifact",
	},
	"List Filter": {
		"validate": "ione_qms.overrides.phi_query_guard.validate_phi_query_artifact",
	},
	"File": {
		"validate": "ione_qms.services.finding_appeals.validate_finding_appeal_evidence_file",
	},
	"IONE System Settings": {
		"validate": "ione_qms.services.runtime_settings.validate_runtime_enablement",
	},
	"IONE Production Readiness Assessment": {
		"validate": "ione_qms.services.production_readiness.validate_production_readiness_assessment",
		"on_trash": "ione_qms.services.immutability.prevent_delete",
	},
	"IONE Production Activation Event": {
		"validate": [
			"ione_qms.services.production_readiness.validate_production_activation_event",
			"ione_qms.services.immutability.validate_append_only",
		],
		"on_cancel": "ione_qms.services.immutability.prevent_delete",
		"on_trash": "ione_qms.services.immutability.prevent_delete",
	},
	"IONE AI Settings": {
		"validate": "ione_qms.services.runtime_settings.validate_runtime_enablement",
	},
	"IONE PHI Disclosure Policy": {
		"before_insert": "ione_qms.services.phi_access.prepare_phi_disclosure_policy",
		"validate": "ione_qms.services.phi_access.validate_phi_disclosure_policy",
		"on_cancel": "ione_qms.services.phi_access.prevent_phi_record_deletion",
		"on_trash": "ione_qms.services.phi_access.prevent_phi_record_deletion",
	},
	"IONE PHI Access Receipt": {
		"onload": "ione_qms.services.phi_access.verify_phi_access_receipt_integrity",
		"validate": "ione_qms.services.phi_access.validate_phi_access_receipt",
		"on_cancel": "ione_qms.services.phi_access.prevent_phi_record_deletion",
		"on_trash": "ione_qms.services.phi_access.prevent_phi_record_deletion",
	},
	"IONE Migration Completion Receipt": {
		"validate": "ione_qms.services.migration_state.validate_completion_receipt",
		"on_cancel": "ione_qms.services.immutability.prevent_delete",
		"on_trash": "ione_qms.services.immutability.prevent_delete",
	},
	"IONE Batch Run": {
		"validate": "ione_qms.services.batch_reliability.validate_batch_run",
		"on_cancel": "ione_qms.services.batch_reliability.prevent_batch_record_deletion",
		"on_trash": "ione_qms.services.batch_reliability.prevent_batch_record_deletion",
	},
	"IONE Batch Work Item": {
		"validate": "ione_qms.services.batch_reliability.validate_batch_work_item",
		"on_cancel": "ione_qms.services.batch_reliability.prevent_batch_record_deletion",
		"on_trash": "ione_qms.services.batch_reliability.prevent_batch_record_deletion",
	},
	"IONE Batch Recovery Receipt": {
		"validate": "ione_qms.services.batch_reliability.validate_batch_recovery_receipt",
		"on_cancel": "ione_qms.services.batch_reliability.prevent_batch_record_deletion",
		"on_trash": "ione_qms.services.batch_reliability.prevent_batch_record_deletion",
	},
	"IONE Batch Source Epoch": {
		"validate": "ione_qms.services.batch_reliability.validate_batch_source_epoch",
		"on_cancel": "ione_qms.services.batch_reliability.prevent_batch_record_deletion",
		"on_trash": "ione_qms.services.batch_reliability.prevent_batch_record_deletion",
	},
	"IONE QC Standard Version": {
		"validate": "ione_qms.services.versions.validate_standard_version",
		"on_update": "ione_qms.services.versions.on_standard_version_update",
		"on_trash": "ione_qms.services.versions.prevent_definition_version_deletion",
	},
	"IONE QC Standard": {
		"validate": "ione_qms.services.versions.validate_standard",
		"on_trash": "ione_qms.services.versions.prevent_definition_parent_deletion",
	},
	"IONE QC Standard Clause": {
		"validate": "ione_qms.services.versions.validate_standard_clause",
		"on_trash": "ione_qms.services.versions.prevent_standard_clause_deletion",
	},
	"IONE QC Rule": {
		"validate": "ione_qms.services.versions.validate_rule",
		"on_trash": "ione_qms.services.versions.prevent_definition_parent_deletion",
	},
	"IONE QC Rule Version": {
		"validate": "ione_qms.services.versions.validate_rule_version",
		"on_update": "ione_qms.services.versions.on_rule_version_update",
		"on_trash": "ione_qms.services.versions.prevent_definition_version_deletion",
	},
	"IONE QC Rule Test Case": {
		"validate": "ione_qms.rule_engine.testing.validate_rule_test_case",
		"on_trash": "ione_qms.rule_engine.testing.prevent_rule_test_case_delete",
	},
	"IONE QC Indicator": {
		"validate": "ione_qms.services.versions.validate_indicator",
		"on_trash": "ione_qms.services.versions.prevent_definition_parent_deletion",
	},
	"IONE QC Indicator Version": {
		"validate": "ione_qms.services.versions.validate_indicator_version",
		"on_update": "ione_qms.services.versions.on_indicator_version_update",
		"on_trash": "ione_qms.services.versions.prevent_definition_version_deletion",
	},
	"IONE Source System": {
		"before_validate": "ione_qms.services.integration_config.prepare_source_system",
		"validate": "ione_qms.services.integration_config.validate_source_system",
	},
	"IONE Integration Endpoint": {
		"validate": "ione_qms.services.integration_config.validate_integration_endpoint",
	},
	"IONE Integration Mapping": {
		"validate": "ione_qms.services.integration_config.validate_integration_mapping",
		"on_trash": "ione_qms.services.integration_config.prevent_integration_mapping_deletion",
	},
	"IONE Integration Job": {
		"validate": "ione_qms.services.integration_config.validate_integration_job",
	},
	"IONE Integration Settings": {
		"validate": "ione_qms.services.integration_config.validate_integration_settings",
	},
	"IONE Integration Message": {
		"validate": [
			"ione_qms.services.integration_scope.validate_integration_audit_identity",
			"ione_qms.services.integration_config.validate_mapping_lineage_identity",
		],
	},
	"IONE Agent Policy": {
		"validate": "ione_qms.services.ai_policy.validate_agent_policy",
	},
	"IONE Agent Release": {
		"validate": "ione_qms.services.versions.validate_agent_release",
	},
	"IONE Agent Evaluation Threshold Policy": {
		"validate": "ione_qms.services.agent_evaluations.validate_evaluation_threshold_policy",
		"on_cancel": "ione_qms.services.immutability.prevent_delete",
		"on_trash": "ione_qms.services.immutability.prevent_delete",
	},
	"IONE Agent Test Case": {
		"validate": "ione_qms.services.agent_evaluations.validate_agent_test_case",
	},
	"IONE Agent Evaluation": {
		"validate": "ione_qms.services.agent_evaluations.validate_agent_evaluation",
		"on_cancel": "ione_qms.services.immutability.prevent_delete",
		"on_trash": "ione_qms.services.immutability.prevent_delete",
	},
	"IONE Data Export Request": {
		"before_insert": "ione_qms.services.data_export.validate_data_export_request",
		"validate": "ione_qms.services.data_export.validate_data_export_request",
	},
	"IONE Clinical Quality Event": {
		"before_insert": "ione_qms.services.integration_scope.validate_event_tenant_binding",
		"validate": [
			"ione_qms.services.integration_scope.validate_integration_audit_identity",
			"ione_qms.services.integration_config.validate_mapping_lineage_identity",
		],
		"after_insert": "ione_qms.rule_engine.executor.enqueue_event_rules",
	},
	"IONE QC Finding": {
		"validate": [
			"ione_qms.services.findings.validate_finding",
			"ione_qms.services.finding_appeals.validate_finding_appeal_transition",
		],
		"on_update": "ione_qms.services.findings.on_finding_update",
		"on_cancel": "ione_qms.services.immutability.prevent_delete",
		"on_trash": "ione_qms.services.immutability.prevent_delete",
	},
	"IONE QC Finding Appeal": {
		"validate": "ione_qms.services.finding_appeals.validate_finding_appeal",
		"on_cancel": "ione_qms.services.finding_appeals.prevent_finding_appeal_deletion",
		"on_trash": "ione_qms.services.finding_appeals.prevent_finding_appeal_deletion",
	},
	"IONE QC Finding Appeal Evidence": {
		"validate": "ione_qms.services.finding_appeals.validate_finding_appeal_evidence",
		"on_cancel": "ione_qms.services.finding_appeals.prevent_finding_appeal_evidence_deletion",
		"on_trash": "ione_qms.services.finding_appeals.prevent_finding_appeal_evidence_deletion",
	},
	"IONE Medical Record QC": {
		"validate": [
			"ione_qms.services.projections.validate_projection_identity",
			"ione_qms.services.medical_record_review.validate_medical_record_review_anchor",
		],
		"on_cancel": "ione_qms.services.medical_record_review.prevent_sampled_medical_record_deletion",
		"on_trash": "ione_qms.services.medical_record_review.prevent_sampled_medical_record_deletion",
	},
	"IONE Medical Record Sampling Policy": {
		"before_insert": "ione_qms.services.medical_record_review.prepare_sampling_policy",
		"validate": "ione_qms.services.medical_record_review.validate_sampling_policy",
		"on_cancel": "ione_qms.services.medical_record_review.prevent_medical_record_review_deletion",
		"on_trash": "ione_qms.services.medical_record_review.prevent_medical_record_review_deletion",
	},
	"IONE Medical Record Sampling Policy Operation": {
		"validate": "ione_qms.services.medical_record_review.validate_sampling_policy_operation",
		"on_cancel": "ione_qms.services.medical_record_review.prevent_medical_record_review_deletion",
		"on_trash": "ione_qms.services.medical_record_review.prevent_medical_record_review_deletion",
	},
	"IONE Medical Record Review Batch": {
		"validate": "ione_qms.services.medical_record_review.validate_review_batch",
		"on_cancel": "ione_qms.services.medical_record_review.prevent_medical_record_review_deletion",
		"on_trash": "ione_qms.services.medical_record_review.prevent_medical_record_review_deletion",
	},
	"IONE Medical Record Review Assignment": {
		"validate": "ione_qms.services.medical_record_review.validate_review_assignment",
		"on_cancel": "ione_qms.services.medical_record_review.prevent_medical_record_review_deletion",
		"on_trash": "ione_qms.services.medical_record_review.prevent_medical_record_review_deletion",
	},
	"IONE Medical Record Review Decision": {
		"validate": "ione_qms.services.medical_record_review.validate_review_decision",
		"on_cancel": "ione_qms.services.medical_record_review.prevent_medical_record_review_deletion",
		"on_trash": "ione_qms.services.medical_record_review.prevent_medical_record_review_deletion",
	},
	"IONE Medical Record Archive Decision": {
		"validate": "ione_qms.services.medical_record_review.validate_archive_decision",
		"on_cancel": "ione_qms.services.medical_record_review.prevent_medical_record_review_deletion",
		"on_trash": "ione_qms.services.medical_record_review.prevent_medical_record_review_deletion",
	},
	"IONE Medical Record Archive Acknowledgement": {
		"validate": "ione_qms.services.medical_record_review.validate_archive_acknowledgement",
		"on_cancel": "ione_qms.services.medical_record_review.prevent_medical_record_review_deletion",
		"on_trash": "ione_qms.services.medical_record_review.prevent_medical_record_review_deletion",
	},
	"IONE Medical Record Archive Delivery": {
		"validate": "ione_qms.services.medical_record_review.validate_archive_delivery",
		"on_cancel": "ione_qms.services.medical_record_review.prevent_medical_record_review_deletion",
		"on_trash": "ione_qms.services.medical_record_review.prevent_medical_record_review_deletion",
	},
	"IONE Medical Record Completeness Watermark": {
		"validate": (
			"ione_qms.services.medical_record_review.validate_medical_record_completeness_watermark"
		),
		"on_cancel": "ione_qms.services.medical_record_review.prevent_medical_record_review_deletion",
		"on_trash": "ione_qms.services.medical_record_review.prevent_medical_record_review_deletion",
	},
	"IONE Staff Qualification": {
		"validate": "ione_qms.services.surgery_governance.validate_staff_qualification",
		"on_cancel": "ione_qms.services.surgery_governance.prevent_surgery_governance_deletion",
		"on_trash": "ione_qms.services.surgery_governance.prevent_surgery_governance_deletion",
	},
	"IONE Surgery Authorization": {
		"validate": "ione_qms.services.surgery_governance.validate_surgery_authorization",
		"on_cancel": "ione_qms.services.surgery_governance.prevent_surgery_governance_deletion",
		"on_trash": "ione_qms.services.surgery_governance.prevent_surgery_governance_deletion",
	},
	"IONE Surgery Procedure Policy": {
		"validate": "ione_qms.services.surgery_governance.validate_surgery_procedure_policy",
		"on_cancel": "ione_qms.services.surgery_governance.prevent_surgery_governance_deletion",
		"on_trash": "ione_qms.services.surgery_governance.prevent_surgery_governance_deletion",
	},
	"IONE Surgery QC": {
		"validate": [
			"ione_qms.services.projections.validate_projection_identity",
			"ione_qms.services.surgery_governance.validate_surgery_qc",
		],
		"on_cancel": "ione_qms.services.surgery_governance.prevent_surgery_governance_deletion",
		"on_trash": "ione_qms.services.surgery_governance.prevent_surgery_governance_deletion",
	},
	"IONE Surgery MDT Record": {
		"validate": "ione_qms.services.surgery_governance.validate_surgery_mdt_record",
		"on_cancel": "ione_qms.services.surgery_governance.prevent_surgery_governance_deletion",
		"on_trash": "ione_qms.services.surgery_governance.prevent_surgery_governance_deletion",
	},
	"IONE Surgery MDT Participant": {
		"validate": "ione_qms.services.surgery_governance.validate_surgery_mdt_participant",
		"on_cancel": "ione_qms.services.surgery_governance.prevent_surgery_governance_deletion",
		"on_trash": "ione_qms.services.surgery_governance.prevent_surgery_governance_deletion",
	},
	"IONE Surgery Safety Checklist": {
		"validate": "ione_qms.services.surgery_governance.validate_surgery_safety_checklist",
		"on_cancel": "ione_qms.services.surgery_governance.prevent_surgery_governance_deletion",
		"on_trash": "ione_qms.services.surgery_governance.prevent_surgery_governance_deletion",
	},
	"IONE Surgery Emergency Exception": {
		"validate": "ione_qms.services.surgery_governance.validate_surgery_emergency_exception",
		"on_cancel": "ione_qms.services.surgery_governance.prevent_surgery_governance_deletion",
		"on_trash": "ione_qms.services.surgery_governance.prevent_surgery_governance_deletion",
	},
	"IONE Medical Safety Event": {
		"validate": [
			"ione_qms.services.projections.validate_projection_identity",
			"ione_qms.services.safety_events.validate_medical_safety_event",
		],
	},
	"IONE Data Reconciliation": {
		"validate": "ione_qms.services.projections.validate_projection_identity",
	},
	"IONE QC Rectification": {
		"validate": "ione_qms.services.improvement.validate_rectification",
		"on_update": "ione_qms.services.improvement.on_rectification_update",
	},
	"IONE QC Verification": {
		"validate": "ione_qms.services.improvement.validate_verification",
		"on_update": "ione_qms.services.improvement.on_verification_update",
	},
	"IONE PDCA Project": {
		"validate": "ione_qms.services.improvement.validate_pdca_project",
	},
	"IONE Quality Meeting": {
		"validate": "ione_qms.services.quality_meetings.validate_quality_meeting",
		"on_cancel": "ione_qms.services.quality_meetings.prevent_quality_meeting_deletion",
		"on_trash": "ione_qms.services.quality_meetings.prevent_quality_meeting_deletion",
	},
	"IONE Meeting Minute": {
		"validate": "ione_qms.services.quality_meetings.validate_meeting_minute",
		"on_cancel": "ione_qms.services.quality_meetings.prevent_quality_meeting_deletion",
		"on_trash": "ione_qms.services.quality_meetings.prevent_quality_meeting_deletion",
	},
	"IONE Meeting Decision": {
		"validate": "ione_qms.services.quality_meetings.validate_meeting_decision",
		"on_cancel": "ione_qms.services.quality_meetings.prevent_quality_meeting_deletion",
		"on_trash": "ione_qms.services.quality_meetings.prevent_quality_meeting_deletion",
	},
	"IONE Quality Action Item": {
		"validate": "ione_qms.services.quality_meetings.validate_quality_action_item",
		"on_cancel": "ione_qms.services.quality_meetings.prevent_quality_meeting_deletion",
		"on_trash": "ione_qms.services.quality_meetings.prevent_quality_meeting_deletion",
	},
	"IONE Quality Action Verification Round": {
		"validate": ("ione_qms.services.quality_meetings.validate_quality_action_verification_round"),
		"on_cancel": "ione_qms.services.quality_meetings.prevent_quality_meeting_deletion",
		"on_trash": "ione_qms.services.quality_meetings.prevent_quality_meeting_deletion",
	},
	"IONE Quality Experience Share": {
		"validate": "ione_qms.services.quality_meetings.validate_quality_experience_share",
		"on_cancel": "ione_qms.services.quality_meetings.prevent_quality_meeting_deletion",
		"on_trash": "ione_qms.services.quality_meetings.prevent_quality_meeting_deletion",
	},
	"IONE Finding Recurrence Policy": {
		"before_insert": "ione_qms.services.finding_recurrence.prepare_recurrence_policy",
		"validate": "ione_qms.services.finding_recurrence.validate_recurrence_policy",
		"on_cancel": "ione_qms.services.finding_recurrence.prevent_recurrence_record_deletion",
		"on_trash": "ione_qms.services.finding_recurrence.prevent_recurrence_record_deletion",
	},
	"IONE Finding Recurrence Run": {
		"validate": "ione_qms.services.finding_recurrence.validate_recurrence_run",
		"on_cancel": "ione_qms.services.finding_recurrence.prevent_recurrence_record_deletion",
		"on_trash": "ione_qms.services.finding_recurrence.prevent_recurrence_record_deletion",
	},
	"IONE Finding Recurrence Evaluation": {
		"validate": "ione_qms.services.finding_recurrence.validate_recurrence_evaluation",
		"on_cancel": "ione_qms.services.finding_recurrence.prevent_recurrence_record_deletion",
		"on_trash": "ione_qms.services.finding_recurrence.prevent_recurrence_record_deletion",
	},
	"IONE Indicator Result": {
		"validate": "ione_qms.services.indicators.validate_indicator_result_revision",
		"after_insert": "ione_qms.services.indicators.on_indicator_result",
		"on_cancel": "ione_qms.services.indicators.prevent_indicator_revision_deletion",
		"on_trash": "ione_qms.services.indicators.prevent_indicator_revision_deletion",
	},
	"IONE Indicator Result Detail": {
		"validate": "ione_qms.services.indicators.validate_indicator_result_detail",
		"on_cancel": "ione_qms.services.indicators.prevent_indicator_revision_deletion",
		"on_trash": "ione_qms.services.indicators.prevent_indicator_revision_deletion",
	},
	"IONE Indicator Calculation": {
		"validate": "ione_qms.services.indicators.validate_indicator_calculation_receipt",
		"on_cancel": "ione_qms.services.indicators.prevent_indicator_revision_deletion",
		"on_trash": "ione_qms.services.indicators.prevent_indicator_revision_deletion",
	},
	"IONE Indicator Result Pointer": {
		"validate": "ione_qms.services.indicators.validate_indicator_result_pointer",
		"on_cancel": "ione_qms.services.indicators.prevent_indicator_revision_deletion",
		"on_trash": "ione_qms.services.indicators.prevent_indicator_revision_deletion",
	},
	"IONE Indicator Quarantine Receipt": {
		"validate": "ione_qms.services.indicators.validate_indicator_quarantine_receipt",
		"on_cancel": "ione_qms.services.indicators.prevent_indicator_revision_deletion",
		"on_trash": "ione_qms.services.indicators.prevent_indicator_revision_deletion",
	},
	"IONE Indicator Quarantine Disposition": {
		"validate": "ione_qms.services.indicators.validate_indicator_quarantine_disposition",
		"on_cancel": "ione_qms.services.indicators.prevent_indicator_revision_deletion",
		"on_trash": "ione_qms.services.indicators.prevent_indicator_revision_deletion",
	},
	"IONE AI Analysis Task": {
		"before_insert": "ione_qms.services.ai_tasks.validate_analysis_task",
		"validate": "ione_qms.services.ai_tasks.validate_analysis_task",
		"after_insert": "ione_qms.ai.orchestrator.enqueue_analysis_task",
		"on_cancel": "ione_qms.services.immutability.prevent_delete",
		"on_trash": "ione_qms.services.immutability.prevent_delete",
	},
	"IONE AI Candidate Finding": {
		"validate": "ione_qms.services.immutability.validate_reviewed_ai_artifact",
	},
	"IONE AI Report Draft": {
		"validate": "ione_qms.services.immutability.validate_reviewed_ai_artifact",
	},
	"IONE AI Report Schedule": {
		"before_insert": "ione_qms.services.ai_report_schedules.prepare_report_schedule",
		"validate": "ione_qms.services.ai_report_schedules.validate_report_schedule",
		"on_cancel": "ione_qms.services.ai_report_schedules.prevent_report_schedule_deletion",
		"on_trash": "ione_qms.services.ai_report_schedules.prevent_report_schedule_deletion",
	},
	"IONE Quality Report Snapshot": {
		"validate": "ione_qms.services.ai_report_schedules.validate_quality_report_snapshot",
		"on_cancel": "ione_qms.services.ai_report_schedules.prevent_quality_report_snapshot_deletion",
		"on_trash": "ione_qms.services.ai_report_schedules.prevent_quality_report_snapshot_deletion",
	},
	"IONE AI Report Recovery Authorization": {
		"validate": ("ione_qms.services.ai_report_schedules.validate_report_recovery_authorization"),
		"on_cancel": ("ione_qms.services.ai_report_schedules.prevent_report_recovery_authorization_deletion"),
		"on_trash": ("ione_qms.services.ai_report_schedules.prevent_report_recovery_authorization_deletion"),
	},
	"IONE AI Tool Approval": {
		"validate": "ione_qms.services.ai_approvals.validate_ai_tool_approval",
		"on_cancel": "ione_qms.services.immutability.prevent_delete",
		"on_trash": "ione_qms.services.immutability.prevent_delete",
	},
	"IONE QC Execution": {
		"validate": "ione_qms.services.immutability.validate_append_only",
		"on_cancel": "ione_qms.services.immutability.prevent_delete",
		"on_trash": "ione_qms.services.immutability.prevent_delete",
	},
	"IONE QC Rule Test Execution": {
		"validate": "ione_qms.services.immutability.validate_append_only",
		"on_cancel": "ione_qms.services.immutability.prevent_delete",
		"on_trash": "ione_qms.services.immutability.prevent_delete",
	},
	"IONE QC Rule Shadow Execution": {
		"validate": "ione_qms.services.immutability.validate_append_only",
		"on_cancel": "ione_qms.services.immutability.prevent_delete",
		"on_trash": "ione_qms.services.immutability.prevent_delete",
	},
	"IONE QC Rule Shadow Feedback": {
		"validate": [
			"ione_qms.rule_engine.validation.validate_shadow_feedback",
			"ione_qms.services.immutability.validate_append_only",
		],
		"on_cancel": "ione_qms.services.immutability.prevent_delete",
		"on_trash": "ione_qms.services.immutability.prevent_delete",
	},
	"IONE QC Rule Validation Run": {
		"validate": "ione_qms.services.immutability.validate_append_only",
		"on_cancel": "ione_qms.services.immutability.prevent_delete",
		"on_trash": "ione_qms.services.immutability.prevent_delete",
	},
	"IONE Definition Lineage Receipt": {
		"validate": "ione_qms.services.immutability.validate_append_only",
		"on_cancel": "ione_qms.services.immutability.prevent_delete",
		"on_trash": "ione_qms.services.immutability.prevent_delete",
	},
	"IONE QC Finding Evidence": {
		"validate": "ione_qms.services.immutability.validate_append_only",
		"on_cancel": "ione_qms.services.immutability.prevent_delete",
		"on_trash": "ione_qms.services.immutability.prevent_delete",
	},
	"IONE Source Document Locator": {
		"before_insert": "ione_qms.services.source_document_locator.prepare_source_document_locator",
		"validate": "ione_qms.services.source_document_locator.validate_source_document_locator",
		"on_cancel": ("ione_qms.services.source_document_locator.prevent_source_document_record_deletion"),
		"on_trash": ("ione_qms.services.source_document_locator.prevent_source_document_record_deletion"),
	},
	"IONE Source Document Access Log": {
		"validate": "ione_qms.services.source_document_locator.validate_source_document_access_log",
		"on_cancel": ("ione_qms.services.source_document_locator.prevent_source_document_record_deletion"),
		"on_trash": ("ione_qms.services.source_document_locator.prevent_source_document_record_deletion"),
	},
	"IONE AI Data Access Log": {
		"validate": "ione_qms.services.immutability.validate_append_only",
		"on_cancel": "ione_qms.services.immutability.prevent_delete",
		"on_trash": "ione_qms.services.immutability.prevent_delete",
	},
	"IONE AI Execution Event": {
		"validate": "ione_qms.services.immutability.validate_append_only",
		"on_cancel": "ione_qms.services.immutability.prevent_delete",
		"on_trash": "ione_qms.services.immutability.prevent_delete",
	},
	"IONE Flow Run Link": {
		"validate": "ione_qms.services.immutability.validate_append_only",
		"on_cancel": "ione_qms.services.immutability.prevent_delete",
		"on_trash": "ione_qms.services.immutability.prevent_delete",
	},
	"IONE Release Record": {
		"validate": "ione_qms.services.immutability.validate_append_only",
		"on_cancel": "ione_qms.services.immutability.prevent_delete",
		"on_trash": "ione_qms.services.immutability.prevent_delete",
	},
	"Flow Agent": {
		"validate": "ione_qms.ai.governance.validate_ione_agent",
	},
	"Flow Tool": {
		"validate": "ione_qms.ai.tool_registry.validate_ione_flow_tool",
	},
	"Flow Trigger": {
		"validate": "ione_qms.ai.governance.validate_ione_trigger",
	},
	"Flow Session": {
		"validate": "ione_qms.ai.flow_gateway.validate_ione_flow_session",
	},
	"Flow Run": {
		"validate": "ione_qms.ai.flow_gateway.validate_ione_flow_run",
	},
}

_BATCH_SOURCE_EPOCH_DOCTYPES = (
	"IONE AI Analysis Task",
	"IONE Clinical Quality Event",
	"IONE Encounter Index",
	"IONE Indicator Result",
	"IONE Indicator Result Detail",
	"IONE Medical Record QC",
	"IONE QC Finding",
	"IONE Medical Safety Event",
	"IONE Surgery QC",
)
_BATCH_SOURCE_EPOCH_HANDLER = "ione_qms.services.batch_reliability.bump_indicator_source_epoch"
for _epoch_doctype in _BATCH_SOURCE_EPOCH_DOCTYPES:
	_epoch_events = doc_events.setdefault(_epoch_doctype, {})
	for _epoch_event in ("before_save", "on_trash"):
		_epoch_handler = _epoch_events.get(_epoch_event)
		if not _epoch_handler:
			_epoch_events[_epoch_event] = _BATCH_SOURCE_EPOCH_HANDLER
		elif isinstance(_epoch_handler, list):
			_epoch_handler.append(_BATCH_SOURCE_EPOCH_HANDLER)
		else:
			_epoch_events[_epoch_event] = [_epoch_handler, _BATCH_SOURCE_EPOCH_HANDLER]

_CLINICAL_SCOPE_VALIDATOR = "ione_qms.services.scope_hierarchy.validate_scope_hierarchy"
for _scope_doctype in sorted(_CLINICAL_SCOPE_DOCTYPES):
	_scope_events = doc_events.setdefault(_scope_doctype, {})
	_scope_validate = _scope_events.get("validate")
	if not _scope_validate:
		_scope_events["validate"] = _CLINICAL_SCOPE_VALIDATOR
	elif isinstance(_scope_validate, list):
		_scope_validate.append(_CLINICAL_SCOPE_VALIDATOR)
	else:
		_scope_events["validate"] = [_scope_validate, _CLINICAL_SCOPE_VALIDATOR]

for _tenant_index_doctype in ("IONE Patient Index", "IONE Encounter Index"):
	_index_events = doc_events.setdefault(_tenant_index_doctype, {})
	_index_validate = _index_events.get("validate")
	_tenant_validator = "ione_qms.services.integration_scope.validate_index_tenant_identity"
	if not _index_validate:
		_index_events["validate"] = _tenant_validator
	elif isinstance(_index_validate, list):
		_index_validate.append(_tenant_validator)
	else:
		_index_events["validate"] = [_index_validate, _tenant_validator]

fixtures = [
	{
		"dt": "Custom DocPerm",
		"filters": [["role", "like", "IONE %"]],
	},
	{
		"dt": "Report",
		"filters": [
			[
				"name",
				"in",
				[
					"IONE Rule Quality",
					"IONE Indicator Trend and Quality Profile",
					"IONE Finding Distribution and Closure",
					"IONE Integration Reconciliation",
					"IONE AI Usage and Adoption",
					"IONE Medical Record Review Governance",
					"IONE Surgery Governance and Outcomes",
					"IONE 2026 National Ten-Goal Progress",
					"IONE Improvement Governance Overview",
				],
			]
		],
	},
]
