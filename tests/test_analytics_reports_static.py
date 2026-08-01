from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = ROOT / "ione_qms" / "services" / "analytics_reports.py"
REPORT_ROOT = ROOT / "ione_qms" / "ione_quality_analytics" / "report"
WORKSPACE_GENERATOR = ROOT / "tools" / "generate_workspaces.py"
WORKSPACE_JSON = (
	ROOT / "ione_qms" / "ione_quality_analytics" / "workspace" / "ione_analytics" / "ione_analytics.json"
)
HOOKS_PATH = ROOT / "ione_qms" / "hooks.py"

REPORTS = {
	"ione_rule_quality": (
		"IONE Rule Quality",
		"IONE QC Execution",
		{
			"IONE Physician",
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
		},
		"execute_rule_quality_report",
	),
	"ione_indicator_trend_and_quality_profile": (
		"IONE Indicator Trend and Quality Profile",
		"IONE Indicator Result",
		{
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
			"IONE Nursing/Pharmacy/IC QC",
			"IONE QMS Auditor",
		},
		"execute_indicator_profile_report",
	),
	"ione_finding_distribution_and_closure": (
		"IONE Finding Distribution and Closure",
		"IONE QC Finding",
		{
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
		},
		"execute_finding_closure_report",
	),
	"ione_integration_reconciliation": (
		"IONE Integration Reconciliation",
		"IONE Data Reconciliation",
		{
			"IONE Integration Administrator",
			"IONE Integration Operator",
			"IONE QC Administrator",
			"IONE QMS Auditor",
		},
		"execute_integration_reconciliation_report",
	),
	"ione_ai_usage_and_adoption": (
		"IONE AI Usage and Adoption",
		"IONE Agent Analysis Fact",
		{
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
			"IONE Nursing/Pharmacy/IC QC",
			"IONE QMS Auditor",
		},
		"execute_ai_usage_report",
	),
}

DOMAIN_REPORTS = {
	"ione_medical_record_review_governance": (
		"IONE Medical Record Review Governance",
		"IONE Medical Record Review Batch",
		{
			"IONE QMS Medical Record Coder",
			"IONE Medical Record Expert Reviewer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
			"IONE QMS Auditor",
		},
		"execute_medical_record_review_report",
	),
	"ione_surgery_governance_and_outcomes": (
		"IONE Surgery Governance and Outcomes",
		"IONE Surgery QC",
		{"IONE QC Reviewer", "IONE Medical Affairs"},
		"execute_surgery_governance_report",
	),
	"ione_2026_national_ten_goal_progress": (
		"IONE 2026 National Ten-Goal Progress",
		"IONE Indicator Result",
		{
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
			"IONE Nursing/Pharmacy/IC QC",
			"IONE QMS Auditor",
		},
		"execute_national_ten_goal_report",
	),
	"ione_improvement_governance_overview": (
		"IONE Improvement Governance Overview",
		"IONE PDCA Project",
		{
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
		},
		"execute_improvement_governance_report",
	),
}

DOMAIN_REPORT_SOURCES = {
	"ione_medical_record_review_governance": {
		"IONE Medical Record Sampling Policy",
		"IONE Medical Record Review Batch",
		"IONE Medical Record Review Assignment",
		"IONE Medical Record Review Decision",
		"IONE Medical Record Archive Decision",
		"IONE Medical Record Archive Acknowledgement",
	},
	"ione_surgery_governance_and_outcomes": {
		"IONE Surgery QC",
		"IONE Surgery Procedure Policy",
		"IONE Surgery Authorization",
		"IONE Surgery MDT Record",
		"IONE Surgery Safety Checklist",
		"IONE Surgery Emergency Exception",
	},
	"ione_2026_national_ten_goal_progress": {
		"IONE QC Standard",
		"IONE QC Standard Version",
		"IONE QC Standard Clause",
		"IONE QC Indicator",
		"IONE QC Indicator Version",
		"IONE Indicator Result",
	},
	"ione_improvement_governance_overview": {
		"IONE Finding Recurrence Evaluation",
		"IONE PDCA Project",
		"IONE Quality Meeting",
		"IONE Meeting Minute",
		"IONE Meeting Decision",
		"IONE Quality Action Item",
		"IONE Quality Action Verification Round",
		"IONE Quality Experience Share",
	},
}


class TestAnalyticsReportsStaticContract(TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		cls.source = SERVICE_PATH.read_text(encoding="utf-8")
		cls.tree = ast.parse(cls.source)

	def test_standard_script_reports_have_closed_roles_and_client_filters(self) -> None:
		for folder, (name, ref_doctype, roles, service_function) in REPORTS.items():
			with self.subTest(report=name):
				root = REPORT_ROOT / folder
				payload = json.loads((root / f"{folder}.json").read_text(encoding="utf-8"))
				self.assertEqual(payload["name"], name)
				self.assertEqual(payload["report_name"], name)
				self.assertEqual(payload["report_type"], "Script Report")
				self.assertEqual(payload["is_standard"], "Yes")
				self.assertEqual(payload["apply_user_permissions"], 1)
				self.assertEqual(payload["ref_doctype"], ref_doctype)
				self.assertEqual({row["role"] for row in payload["roles"]}, roles)
				self.assertNotIn("Administrator", roles)
				self.assertNotIn("IONE Agent Service", roles)

				python_source = (root / f"{folder}.py").read_text(encoding="utf-8")
				self.assertIn(f"return {service_function}(filters)", python_source)
				javascript = (root / f"{folder}.js").read_text(encoding="utf-8")
				self.assertIn(f'frappe.query_reports["{name}"]', javascript)
				self.assertIn('fieldname: "from_date"', javascript)
				self.assertIn('fieldname: "to_date"', javascript)
				self.assertIn("frappe.datetime.add_days", javascript)

	def test_domain_script_reports_are_standard_permission_filtered_and_closed_role(self) -> None:
		for folder, (name, ref_doctype, roles, service_function) in DOMAIN_REPORTS.items():
			with self.subTest(report=name):
				root = REPORT_ROOT / folder
				payload = json.loads((root / f"{folder}.json").read_text(encoding="utf-8"))
				self.assertEqual(payload["name"], name)
				self.assertEqual(payload["report_name"], name)
				self.assertEqual(payload["report_type"], "Script Report")
				self.assertEqual(payload["is_standard"], "Yes")
				self.assertEqual(payload["apply_user_permissions"], 1)
				self.assertEqual(payload["disable_prepared_report_automation"], 1)
				self.assertEqual(payload["ref_doctype"], ref_doctype)
				self.assertEqual({row["role"] for row in payload["roles"]}, roles)
				self.assertNotIn("Administrator", roles)
				self.assertNotIn("IONE Agent Service", roles)

				python_source = (root / f"{folder}.py").read_text(encoding="utf-8")
				self.assertIn(f"return {service_function}(filters)", python_source)
				javascript = (root / f"{folder}.js").read_text(encoding="utf-8")
				self.assertIn(f'frappe.query_reports["{name}"]', javascript)
				self.assertIn('fieldname: "from_date"', javascript)
				self.assertIn('fieldname: "to_date"', javascript)
				self.assertIn("frappe.datetime.add_days", javascript)

		national_js = (
			REPORT_ROOT / "ione_2026_national_ten_goal_progress" / "ione_2026_national_ten_goal_progress.js"
		).read_text(encoding="utf-8")
		self.assertIn('fieldname: "standard"', national_js)
		self.assertIn('source_type: "National Policy"', national_js)
		self.assertIn('status: "Published"', national_js)
		self.assertRegex(national_js, r'fieldname: "standard"[\s\S]+?reqd: 1')

	def test_domain_report_roles_can_read_every_declared_source_doctype(self) -> None:
		payloads = {}
		for path in (ROOT / "ione_qms").glob("**/doctype/*/*.json"):
			payload = json.loads(path.read_text(encoding="utf-8"))
			if payload.get("name") in {
				doctype for sources in DOMAIN_REPORT_SOURCES.values() for doctype in sources
			}:
				payloads[payload["name"]] = payload
		for folder, sources in DOMAIN_REPORT_SOURCES.items():
			roles = DOMAIN_REPORTS[folder][2]
			for doctype in sources:
				with self.subTest(report=folder, doctype=doctype):
					self.assertIn(doctype, payloads)
					read_roles = {
						row["role"]
						for row in payloads[doctype].get("permissions", [])
						if int(row.get("read") or 0) == 1 and int(row.get("permlevel") or 0) == 0
					}
					self.assertTrue(roles.issubset(read_roles), (roles, read_roles))

	def test_service_uses_only_permission_query_reads(self) -> None:
		for forbidden in (
			"frappe.get_all",
			"frappe.db.",
			"ignore_permissions",
			"frappe.get_doc",
			"frappe.get_value",
			"frappe.db.sql",
		):
			self.assertNotIn(forbidden, self.source)
		self.assertIn("frappe.get_list(doctype, **kwargs)", self.source)
		self.assertIn('frappe.has_permission(doctype, "read")', self.source)
		self.assertIn('frappe.has_permission(doctype, "read", user=user)', self.source)
		self.assertIn('"Guest", "Administrator"', self.source)
		self.assertIn('"IONE Agent Service" in roles', self.source)

	def test_clinical_queries_never_select_raw_patient_or_narrative_fields(self) -> None:
		for forbidden in (
			'"patient"',
			'"encounter"',
			'"source_record_id"',
			'"source_record_id_hash"',
			'"responsible_staff"',
			'"medical_staff"',
			'"description"',
			'"reason"',
			'"review_comment"',
			'"input_summary"',
			'"error_message"',
			'"lineage_json"',
			'"details_json"',
			'"requested_by"',
		):
			self.assertNotIn(forbidden, self.source)

	def test_date_and_row_limits_are_hard_failures(self) -> None:
		expected = {
			"REPORT_MAX_DAYS": 366,
			"REPORT_MAX_OUTPUT_ROWS": 500,
			"REPORT_MAX_SOURCE_ROWS": 10_000,
			"REPORT_MAX_GROUP_ROWS": 5_000,
			"REPORT_MAX_AGGREGATED_RECORDS": 1_000_000,
		}
		assignments = {
			target.id: ast.literal_eval(node.value)
			for node in self.tree.body
			if isinstance(node, ast.Assign)
			for target in node.targets
			if isinstance(target, ast.Name) and target.id in expected
		}
		self.assertEqual(assignments, expected)
		self.assertIn("limit_page_length", self.source)
		self.assertIn("limit + 1", self.source)
		self.assertIn("if len(rows) > limit:", self.source)
		self.assertIn("_report_limit_error(", self.source)
		self.assertIn("Report date range cannot exceed", self.source)

	def test_human_review_metrics_are_not_called_generic_false_positive_rates(self) -> None:
		self.assertIn(
			'"human_appeal_overturn_rate": _percentage(',
			self.source,
		)
		self.assertIn(
			'metric["appeal_approved"] + metric["appeal_rejected"]',
			self.source,
		)
		self.assertIn("Submitted, withdrawn, or otherwise unreviewed appeals", self.source)
		self.assertNotIn("false_positive_rate", self.source)
		self.assertNotIn("False Positive Rate", self.source)

	def test_all_reports_return_columns_data_message_chart_and_summary(self) -> None:
		for function_name in (
			"execute_rule_quality_report",
			"execute_indicator_profile_report",
			"execute_finding_closure_report",
			"execute_integration_reconciliation_report",
			"execute_ai_usage_report",
			"execute_medical_record_review_report",
			"execute_surgery_governance_report",
			"execute_national_ten_goal_report",
			"execute_improvement_governance_report",
		):
			function = next(
				node
				for node in self.tree.body
				if isinstance(node, ast.FunctionDef) and node.name == function_name
			)
			returns = [node for node in ast.walk(function) if isinstance(node, ast.Return)]
			self.assertTrue(
				any(isinstance(node.value, ast.Tuple) and len(node.value.elts) == 5 for node in returns),
				function_name,
			)

	def test_domain_reports_are_checksum_versioned_no_data_and_source_counted(self) -> None:
		for function_name in (
			"execute_medical_record_review_report",
			"execute_surgery_governance_report",
			"execute_national_ten_goal_report",
			"execute_improvement_governance_report",
		):
			function = next(
				node
				for node in self.tree.body
				if isinstance(node, ast.FunctionDef) and node.name == function_name
			)
			source = ast.get_source_segment(self.source, function) or ""
			self.assertIn("source_", source, function_name)
			self.assertIn("No Data", source, function_name)
			self.assertIn("checksum", source, function_name)
			self.assertIn("_permissioned_rows(", source, function_name)

		self.assertIn("versions are never mixed or averaged", self.source)
		self.assertIn("must be a Published National Policy", self.source)
		self.assertIn("false positives", self.source)
		self.assertNotIn("false_positive", self.source)

	def test_workspace_generator_and_fixture_link_every_report(self) -> None:
		generator = WORKSPACE_GENERATOR.read_text(encoding="utf-8")
		workspace = json.loads(WORKSPACE_JSON.read_text(encoding="utf-8"))
		hooks = HOOKS_PATH.read_text(encoding="utf-8")
		report_links = {
			item["link_to"] for item in workspace["sidebar_items"] if item.get("link_type") == "Report"
		}
		for _folder, (name, _doctype, _roles, _function) in {
			**REPORTS,
			**DOMAIN_REPORTS,
		}.items():
			self.assertIn(f'"link_to": "{name}"', generator)
			self.assertIn(name, report_links)
			self.assertIn(f'"{name}"', hooks)
