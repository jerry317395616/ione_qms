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

permission_query_conditions = {
	"IONE Patient Index": "ione_qms.permissions.patient_query",
	"IONE Encounter Index": "ione_qms.permissions.encounter_query",
	"IONE Clinical Quality Event": "ione_qms.permissions.clinical_query",
	"IONE QC Execution": "ione_qms.permissions.clinical_query",
	"IONE QC Finding": "ione_qms.permissions.finding_query",
	"IONE QC Finding Evidence": "ione_qms.permissions.finding_query",
	"IONE Medical Record QC": "ione_qms.permissions.clinical_query",
	"IONE Surgery QC": "ione_qms.permissions.clinical_query",
	"IONE Medical Safety Event": "ione_qms.permissions.clinical_query",
	"IONE QC Rectification": "ione_qms.permissions.finding_query",
	"IONE QC Verification": "ione_qms.permissions.finding_query",
	"IONE Indicator Result": "ione_qms.permissions.analytics_query",
	"IONE Indicator Result Detail": "ione_qms.permissions.analytics_query",
	"IONE AI Analysis Task": "ione_qms.permissions.ai_task_query",
	"IONE AI Candidate Finding": "ione_qms.permissions.ai_task_query",
	"IONE AI Report Draft": "ione_qms.permissions.ai_task_query",
}

has_permission = {
	"IONE Patient Index": "ione_qms.permissions.patient_permission",
	"IONE Encounter Index": "ione_qms.permissions.encounter_permission",
	"IONE Clinical Quality Event": "ione_qms.permissions.clinical_permission",
	"IONE QC Execution": "ione_qms.permissions.clinical_permission",
	"IONE QC Finding": "ione_qms.permissions.finding_permission",
	"IONE QC Finding Evidence": "ione_qms.permissions.finding_permission",
	"IONE Medical Record QC": "ione_qms.permissions.clinical_permission",
	"IONE Surgery QC": "ione_qms.permissions.clinical_permission",
	"IONE Medical Safety Event": "ione_qms.permissions.clinical_permission",
	"IONE QC Rectification": "ione_qms.permissions.finding_permission",
	"IONE QC Verification": "ione_qms.permissions.finding_permission",
	"IONE Indicator Result": "ione_qms.permissions.analytics_permission",
	"IONE Indicator Result Detail": "ione_qms.permissions.analytics_permission",
	"IONE AI Analysis Task": "ione_qms.permissions.ai_task_permission",
	"IONE AI Candidate Finding": "ione_qms.permissions.ai_task_permission",
	"IONE AI Report Draft": "ione_qms.permissions.ai_task_permission",
}

scheduler_events = {
	"cron": {
		"*/5 * * * *": ["ione_qms.tasks.integration.sync_incremental_data"],
		"0 2 * * *": ["ione_qms.tasks.indicators.calculate_daily_indicators"],
		"0 3 * * *": ["ione_qms.tasks.quality.execute_daily_quality_scan"],
		"0 4 1 * *": ["ione_qms.tasks.analytics.build_monthly_facts"],
		"0 5 * * *": ["ione_qms.tasks.security.enforce_retention"],
	}
}

doc_events = {
	"IONE Clinical Quality Event": {
		"after_insert": "ione_qms.rule_engine.executor.enqueue_event_rules",
	},
	"IONE QC Finding": {
		"on_update": "ione_qms.services.findings.on_finding_update",
	},
	"IONE Indicator Result": {
		"after_insert": "ione_qms.services.indicators.on_indicator_result",
	},
	"IONE AI Analysis Task": {
		"after_insert": "ione_qms.ai.orchestrator.enqueue_analysis_task",
	},
	"Flow Agent": {
		"validate": "ione_qms.ai.governance.validate_ione_agent",
	},
	"Flow Trigger": {
		"validate": "ione_qms.ai.governance.validate_ione_trigger",
	},
}

fixtures = [
	{
		"dt": "Custom DocPerm",
		"filters": [["role", "like", "IONE %"]],
	},
]
