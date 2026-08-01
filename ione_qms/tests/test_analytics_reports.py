from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from ione_qms.services import analytics_reports as reports


def _raise_runtime(message: str, *args, **kwargs) -> None:
	del args, kwargs
	raise RuntimeError(message)


class TestAnalyticsReports(TestCase):
	def test_access_requires_named_business_role_and_every_source_permission(self) -> None:
		denied = (
			("Guest", ["Guest"]),
			("Administrator", ["IONE QC Reviewer"]),
			("agent@example.test", ["IONE Agent Service", "IONE QC Reviewer"]),
			("other@example.test", ["System Manager"]),
		)
		for user, roles in denied:
			with (
				self.subTest(user=user),
				patch.object(reports.frappe, "session", SimpleNamespace(user=user)),
				patch.object(reports.frappe, "get_roles", return_value=roles),
				patch.object(reports.frappe, "has_permission", return_value=True),
				patch.object(reports.frappe, "throw", side_effect=_raise_runtime),
				self.assertRaises(RuntimeError),
			):
				reports._require_report_access("rule_quality")

		with (
			patch.object(
				reports.frappe,
				"session",
				SimpleNamespace(user="reviewer@example.test"),
			),
			patch.object(reports.frappe, "get_roles", return_value=["IONE QC Reviewer"]),
			patch.object(
				reports.frappe,
				"has_permission",
				side_effect=[True, True, False, True, True],
			),
			patch.object(reports.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "Read permission is required"),
		):
			reports._require_report_access("rule_quality")

	def test_date_window_defaults_to_30_days_and_rejects_more_than_366(self) -> None:
		with (
			patch.object(reports, "_require_report_access"),
			patch.object(reports, "nowdate", return_value="2026-07-30"),
		):
			_values, start, end = reports._report_context("rule_quality", None)
		self.assertEqual(start, date(2026, 7, 1))
		self.assertEqual(end, date(2026, 7, 30))

		with (
			patch.object(reports, "_require_report_access"),
			patch.object(reports.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "cannot exceed 366"),
		):
			reports._report_context(
				"rule_quality",
				{"from_date": "2025-01-01", "to_date": "2026-01-02"},
			)

	def test_permissioned_rows_uses_get_list_and_rejects_one_extra_row(self) -> None:
		with (
			patch.object(reports.frappe, "has_permission", return_value=True),
			patch.object(reports.frappe, "get_list", return_value=[{"name": "A"}]) as get_list,
		):
			rows = reports._permissioned_rows(
				"IONE QC Finding",
				filters={},
				fields=("name",),
				order_by="name asc",
				limit=1,
			)
		self.assertEqual(rows, [{"name": "A"}])
		self.assertEqual(get_list.call_args.kwargs["limit_page_length"], 2)
		self.assertEqual(get_list.call_args.kwargs["limit_start"], 0)

		with (
			patch.object(reports.frappe, "has_permission", return_value=True),
			patch.object(
				reports.frappe,
				"get_list",
				return_value=[{"name": "A"}, {"name": "B"}],
			),
			patch.object(reports.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "hard limit of 1"),
		):
			reports._permissioned_rows(
				"IONE QC Finding",
				filters={},
				fields=("name",),
				order_by="name asc",
				limit=1,
			)

	def test_rule_quality_uses_reviewed_appeals_only_for_human_overturn_rate(self) -> None:
		def rows(doctype: str, **kwargs):
			fields = tuple(kwargs["fields"])
			if doctype == "IONE QC Execution":
				return [
					{"rule": "RULE-1", "rule_version": "RV-1", "result": "Failed", "total": 2},
					{"rule": "RULE-1", "rule_version": "RV-1", "result": "Excluded", "total": 1},
					{"rule": "RULE-1", "rule_version": "RV-1", "result": "Passed", "total": 1},
				]
			if (
				doctype == "IONE QC Finding"
				and {
					"COUNT": "name",
					"as": "total",
				}
				in fields
			):
				return [{"rule": "RULE-1", "rule_version": "RV-1", "total": 2}]
			if doctype == "IONE QC Finding Appeal":
				return [
					{"name": "A-1", "finding": "F-1", "status": "Approved"},
					{"name": "A-2", "finding": "F-1", "status": "Rejected"},
					{"name": "A-3", "finding": "F-1", "status": "Submitted"},
				]
			if doctype == "IONE QC Finding":
				return [{"name": "F-1", "rule": "RULE-1", "rule_version": "RV-1"}]
			if doctype == "IONE QC Rule":
				return [{"name": "RULE-1", "rule_code": "R-1", "rule_name": "Rule One"}]
			if doctype == "IONE QC Rule Version":
				return [
					{
						"name": "RV-1",
						"version": "1",
						"status": "Published",
						"checksum": "a" * 64,
					}
				]
			raise AssertionError((doctype, fields))

		with (
			patch.object(reports, "_report_context", return_value=({}, date(2026, 7, 1), date(2026, 7, 30))),
			patch.object(reports, "_permissioned_rows", side_effect=rows),
		):
			columns, data, message, chart, summary = reports.execute_rule_quality_report()
		self.assertTrue(columns)
		self.assertEqual(len(data), 1)
		row = data[0]
		self.assertEqual(row["execution_count"], 4)
		self.assertEqual(row["failed_hit_rate"], 50.0)
		self.assertEqual(row["excluded_exemption_rate"], 25.0)
		self.assertEqual(row["appeal_count"], 3)
		self.assertEqual(row["appeal_reviewed_count"], 2)
		self.assertEqual(row["human_appeal_overturn_rate"], 50.0)
		self.assertIn("Submitted, withdrawn", message)
		self.assertEqual(chart["data"]["datasets"][0]["values"], [1, 2, 1, 0, 0])
		self.assertTrue(summary)

	def test_indicator_profile_returns_exact_result_and_governed_lineage_links(self) -> None:
		def rows(doctype: str, **kwargs):
			if doctype == "IONE Indicator Result":
				return [
					{
						"name": "RESULT-1",
						"indicator": "IND-1",
						"indicator_version": "IV-1",
						"calculation": "CALC-1",
						"period": "2026-07",
						"period_start": "2026-07-01",
						"period_end": "2026-07-31",
						"department": "DEPT-1",
						"indicator_value": 91.5,
						"target_value": 90,
						"status": "Met",
						"calculator_key": "CALC",
						"calculator_version": "1",
					}
				]
			if doctype == "IONE QC Indicator":
				return [
					{
						"name": "IND-1",
						"indicator_code": "I-1",
						"indicator_name": "Indicator One",
						"direction": "Higher is Better",
					}
				]
			if doctype == "IONE QC Indicator Version":
				return [
					{
						"name": "IV-1",
						"version": "1",
						"status": "Published",
						"checksum": "b" * 64,
					}
				]
			raise AssertionError(doctype)

		with (
			patch.object(reports, "_report_context", return_value=({}, date(2026, 7, 1), date(2026, 7, 30))),
			patch.object(reports, "_permissioned_rows", side_effect=rows),
		):
			_columns, data, message, chart, summary = reports.execute_indicator_profile_report()
		self.assertEqual(data[0]["indicator_result"], "RESULT-1")
		self.assertEqual(data[0]["calculation"], "CALC-1")
		self.assertEqual(data[0]["indicator_version"], "IV-1")
		self.assertEqual(data[0]["version_checksum"], "b" * 64)
		self.assertEqual(chart["data"]["datasets"][0]["values"], [91.5])
		self.assertIn("not averaged", message)
		self.assertEqual(summary[1]["value"], 1)
		self.assertNotIn("lineage_json", data[0])

	def test_finding_closure_keeps_appeal_overturns_separate_from_closure(self) -> None:
		def rows(doctype: str, **kwargs):
			if doctype == "IONE QC Finding":
				return [
					{
						"name": "F-1",
						"department": "DEPT-1",
						"severity": "High",
						"status": "Closed",
						"due_date": "2026-07-01",
					},
					{
						"name": "F-2",
						"department": "DEPT-1",
						"severity": "High",
						"status": "Appeal Approved",
						"due_date": "2026-07-01",
					},
					{
						"name": "F-3",
						"department": "DEPT-1",
						"severity": "High",
						"status": "Rectifying",
						"due_date": "2000-01-01",
					},
				]
			if doctype == "IONE QC Rectification":
				return [
					{
						"name": "R-1",
						"finding": "F-3",
						"department": "DEPT-1",
						"status": "In Progress",
						"due_date": "2000-01-01",
					}
				]
			if doctype == "IONE QC Verification":
				return [
					{
						"name": "V-1",
						"finding": "F-1",
						"department": "DEPT-1",
						"status": "Verified",
						"verification_result": "Effective",
					}
				]
			raise AssertionError(doctype)

		with (
			patch.object(reports, "_report_context", return_value=({}, date(2026, 7, 1), date(2026, 7, 30))),
			patch.object(reports, "_permissioned_rows", side_effect=rows),
			patch.object(reports, "nowdate", return_value="2026-07-30"),
		):
			_columns, data, message, _chart, summary = reports.execute_finding_closure_report()
		self.assertEqual(data[0]["finding_count"], 3)
		self.assertEqual(data[0]["closed_findings"], 1)
		self.assertEqual(data[0]["appeal_overturned_findings"], 1)
		self.assertEqual(data[0]["active_findings"], 1)
		self.assertEqual(data[0]["overdue_findings"], 1)
		self.assertEqual(data[0]["overdue_rectifications"], 1)
		self.assertEqual(data[0]["effective_verifications"], 1)
		self.assertIn("reported separately", message)
		self.assertEqual(summary[2]["value"], 1)

	def test_reconciliation_does_not_sum_overlapping_window_volumes(self) -> None:
		record = {
			"name": "REC-1",
			"source_system": "SRC-1",
			"endpoint": "END-1",
			"period_start": "2026-07-01",
			"period_end": "2026-07-02",
			"status": "Mismatch",
			"source_count": 10,
			"message_count": 9,
			"event_count": 8,
			"execution_count": 7,
			"error_count": 1,
			"difference_count": 2,
		}
		with (
			patch.object(reports, "_report_context", return_value=({}, date(2026, 7, 1), date(2026, 7, 30))),
			patch.object(reports, "_permissioned_rows", return_value=[record]),
		):
			_columns, data, message, chart, summary = reports.execute_integration_reconciliation_report()
		self.assertEqual(data[0]["difference_count"], 2)
		self.assertIn("does not sum source volumes", message)
		self.assertEqual(chart["data"]["labels"], ["Mismatch"])
		self.assertEqual(summary[2]["value"], 1)
		self.assertFalse(any(item["label"] == "Source Count" for item in summary))

	def test_ai_adoption_rate_counts_successful_tasks_with_any_accepted_candidate(self) -> None:
		facts = [
			{
				"name": "FACT-1",
				"analysis_task": "TASK-1",
				"fact_date": "2026-07-01",
				"task_type": "Medical Record QC",
				"task_status": "Completed",
				"policy": "POLICY-1",
				"task_count": 1,
				"success_count": 1,
				"failure_count": 0,
				"adopted_count": 2,
				"total_tokens": 100,
				"input_tokens": 60,
				"output_tokens": 40,
				"duration_ms": 1000,
			},
			{
				"name": "FACT-2",
				"analysis_task": "TASK-2",
				"fact_date": "2026-07-01",
				"task_type": "Medical Record QC",
				"task_status": "Completed",
				"policy": "POLICY-1",
				"task_count": 1,
				"success_count": 1,
				"failure_count": 0,
				"adopted_count": 0,
				"total_tokens": 50,
				"input_tokens": 30,
				"output_tokens": 20,
				"duration_ms": 500,
			},
		]
		with (
			patch.object(reports, "_report_context", return_value=({}, date(2026, 7, 1), date(2026, 7, 30))),
			patch.object(reports, "_permissioned_rows", return_value=facts),
		):
			_columns, data, message, chart, summary = reports.execute_ai_usage_report()
		self.assertEqual(data[0]["task_count"], 2)
		self.assertEqual(data[0]["successful_tasks_with_accepted_candidate"], 1)
		self.assertEqual(data[0]["accepted_candidate_count"], 2)
		self.assertEqual(data[0]["successful_task_acceptance_rate"], 50.0)
		self.assertEqual(data[0]["average_duration_ms"], 750.0)
		self.assertEqual(data[0]["total_tokens"], 150)
		self.assertIn("may exceed task count", message)
		self.assertEqual(chart["data"]["datasets"][2]["values"], [1])
		self.assertEqual(summary[4]["value"], 2)

	def test_scope_filters_are_exact_and_never_include_clinical_identifiers(self) -> None:
		filters = reports._dated_filters(
			"IONE QC Finding",
			"detected_at",
			date(2026, 7, 1),
			date(2026, 7, 30),
			{
				"hospital": "H-1",
				"campus": "C-1",
				"department": "D-1",
				"ward": "W-1",
			},
		)
		self.assertEqual(
			filters[2:],
			[
				["IONE QC Finding", "campus", "=", "C-1"],
				["IONE QC Finding", "department", "=", "D-1"],
				["IONE QC Finding", "hospital", "=", "H-1"],
				["IONE QC Finding", "ward", "=", "W-1"],
			],
		)
		self.assertNotIn("patient", str(filters))
		self.assertNotIn("encounter", str(filters))

	def test_medical_record_review_report_aggregates_human_and_archive_receipts(self) -> None:
		checksum = "a" * 64

		def rows(doctype: str, **kwargs):
			del kwargs
			if doctype == "IONE Medical Record Review Batch":
				return [
					{
						"name": "BATCH-1",
						"policy": "POLICY-1",
						"policy_checksum": checksum,
						"period_start": "2026-07-01",
						"period_end": "2026-07-31",
						"department": "DEPT-1",
						"population_count": 100,
						"sample_count": 10,
						"expert_sample_count": 2,
						"status": "Completed",
					}
				]
			if doctype == "IONE Medical Record Review Assignment":
				return [
					{
						"name": "ASSIGN-1",
						"batch": "BATCH-1",
						"policy": "POLICY-1",
						"status": "Closed",
						"coder_required": 1,
						"expert_required": 1,
						"deterministic_gate_status": "Passed",
						"coder_decision": "DEC-C",
						"expert_decision": "DEC-E",
						"latest_archive_decision": "ARCH-1",
						"archive_acknowledgement": "ACK-1",
						"archive_ack_status": "Applied",
					}
				]
			if doctype == "IONE Medical Record Review Decision":
				return [
					{
						"name": "DEC-C",
						"assignment": "ASSIGN-1",
						"batch": "BATCH-1",
						"policy": "POLICY-1",
						"stage": "Coder",
						"outcome": "Needs Correction",
						"decision_checksum": checksum,
					},
					{
						"name": "DEC-E",
						"assignment": "ASSIGN-1",
						"batch": "BATCH-1",
						"policy": "POLICY-1",
						"stage": "Expert",
						"outcome": "Pass",
						"decision_checksum": checksum,
					},
				]
			if doctype == "IONE Medical Record Archive Decision":
				return [
					{
						"name": "ARCH-1",
						"assignment": "ASSIGN-1",
						"batch": "BATCH-1",
						"policy": "POLICY-1",
						"policy_checksum": checksum,
						"decision": "Allow",
						"decision_type": "Policy",
						"decision_checksum": checksum,
					}
				]
			if doctype == "IONE Medical Record Archive Acknowledgement":
				return [
					{
						"name": "ACK-1",
						"archive_decision": "ARCH-1",
						"assignment": "ASSIGN-1",
						"ack_status": "Applied",
						"request_hash": checksum,
						"decision_checksum": checksum,
						"received_at": "2026-07-31 12:00:00",
					}
				]
			if doctype == "IONE Medical Record Sampling Policy":
				return [
					{
						"name": "POLICY-1",
						"policy_code": "MR-TERMINAL",
						"policy_version": "1",
						"status": "Approved",
						"archive_gate_enabled": 1,
						"approval_checksum": checksum,
					}
				]
			raise AssertionError(doctype)

		with (
			patch.object(
				reports,
				"_report_context",
				return_value=({}, date(2026, 7, 1), date(2026, 7, 31)),
			),
			patch.object(reports, "_permissioned_rows", side_effect=rows),
		):
			_columns, data, message, chart, summary = reports.execute_medical_record_review_report()
		row = data[0]
		self.assertEqual(row["population_count"], 100)
		self.assertEqual(row["visible_assignment_count"], 1)
		self.assertEqual(row["closed_assignment_count"], 1)
		self.assertEqual(row["coder_required_count"], 1)
		self.assertEqual(row["deterministic_passed_count"], 1)
		self.assertEqual(row["coder_needs_correction_count"], 1)
		self.assertEqual(row["expert_pass_count"], 1)
		self.assertEqual(row["archive_allow_count"], 1)
		self.assertEqual(row["archive_ack_applied_count"], 1)
		self.assertEqual(row["archive_ack_receipt_count"], 1)
		self.assertEqual(chart["data"]["datasets"][0]["values"], [0, 0, 1])
		self.assertIn("never labelled as false positives", message)
		self.assertTrue(summary)

	def test_surgery_governance_report_counts_exact_states_without_clinical_identity(self) -> None:
		checksum = "b" * 64

		def rows(doctype: str, **kwargs):
			del kwargs
			if doctype == "IONE Surgery QC":
				return [
					{
						"name": "SURG-1",
						"surgery_time": "2026-07-20 08:00:00",
						"surgery_level": "Level IV",
						"surgery_phase": "Postoperative Finalized",
						"procedure_policy": "SP-1",
						"surgery_authorization": "AUTH-1",
						"mdt_record": "MDT-1",
						"safety_checklist": "CHECK-1",
						"emergency_exception": None,
						"governance_state": "Passed",
						"governance_checksum": checksum,
						"compliant": 1,
						"status": "Passed",
						"preanesthesia_status": "Completed",
						"intraoperative_monitoring_status": "Completed",
						"recovery_status": "Completed",
						"postoperative_followup_status": "Completed",
						"cancellation_status": "Not Cancelled",
						"unplanned_surgery_status": "No",
						"unplanned_return_status": "Yes",
						"complication_status": "Yes",
						"mortality_status": "No",
						"department": "DEPT-1",
					}
				]
			if doctype == "IONE Surgery Procedure Policy":
				return [
					{
						"name": "SP-1",
						"policy_code": "LEVEL-IV",
						"policy_version": "1",
						"surgery_level": "Level IV",
						"require_mdt": 1,
						"status": "Approved",
						"checksum": checksum,
					}
				]
			if doctype == "IONE Surgery Authorization":
				return [{"name": "AUTH-1", "status": "Approved", "checksum": checksum}]
			if doctype == "IONE Surgery MDT Record":
				return [
					{
						"name": "MDT-1",
						"status": "Completed",
						"conclusion": "Proceed with Conditions",
						"checksum": checksum,
					}
				]
			if doctype == "IONE Surgery Safety Checklist":
				return [{"name": "CHECK-1", "status": "Completed", "checksum": checksum}]
			if doctype == "IONE Surgery Emergency Exception":
				return []
			raise AssertionError(doctype)

		with (
			patch.object(
				reports,
				"_report_context",
				return_value=({}, date(2026, 7, 1), date(2026, 7, 31)),
			),
			patch.object(reports, "_permissioned_rows", side_effect=rows),
		):
			_columns, data, message, _chart, summary = reports.execute_surgery_governance_report()
		row = data[0]
		self.assertEqual(row["source_record_count"], 1)
		self.assertEqual(row["approved_authorization_count"], 1)
		self.assertEqual(row["mdt_required_count"], 1)
		self.assertEqual(row["completed_mdt_count"], 1)
		self.assertEqual(row["mdt_proceed_with_conditions_count"], 1)
		self.assertEqual(row["completed_checklist_count"], 1)
		self.assertEqual(row["followup_completed_count"], 1)
		self.assertEqual(row["unplanned_return_count"], 1)
		self.assertEqual(row["complication_count"], 1)
		self.assertIn("No patient", message)
		self.assertTrue(summary)

	def test_national_goal_report_keeps_approved_versions_and_no_target_invention(self) -> None:
		checksum = "c" * 64

		def rows(doctype: str, **kwargs):
			del kwargs
			if doctype == "IONE QC Standard":
				return [
					{
						"name": "STD-2026",
						"standard_code": "APPROVED-BY-HOSPITAL",
						"standard_name": "Configured National Goals",
						"source_type": "National Policy",
						"status": "Published",
						"issue_date": "2026-03-01",
					}
				]
			if doctype == "IONE QC Indicator":
				return [
					{
						"name": "IND-1",
						"indicator_code": "CONFIGURED-1",
						"indicator_name": "Configured Indicator",
						"category": "Configured Objective",
						"standard": "STD-2026",
						"standard_clause": "CLAUSE-1",
						"direction": "Higher is Better",
						"status": "Active",
					}
				]
			if doctype == "IONE QC Standard Clause":
				return [
					{
						"name": "CLAUSE-1",
						"standard_version": "SV-1",
						"clause_code": "C-1",
						"heading": "Configured Objective",
						"status": "Published",
					}
				]
			if doctype == "IONE QC Standard Version":
				return [
					{
						"name": "SV-1",
						"standard": "STD-2026",
						"version": "1",
						"approval_status": "Approved",
						"effective_from": "2026-01-01",
						"checksum": checksum,
					}
				]
			if doctype == "IONE QC Indicator Version":
				return [
					{
						"name": "IV-1",
						"indicator": "IND-1",
						"version": "1",
						"status": "Published",
						"calculation_frequency": "Monthly",
						"unit": "%",
						"target_value": 90,
						"direction": "Higher is Better",
						"effective_from": "2026-01-01",
						"checksum": checksum,
					}
				]
			if doctype == "IONE Indicator Result":
				return [
					{
						"name": "RESULT-1",
						"indicator": "IND-1",
						"indicator_version": "IV-1",
						"calculation": "CALC-1",
						"period": "2026-07",
						"period_start": "2026-07-01",
						"period_end": "2026-07-31",
						"department": "DEPT-1",
						"numerator": 91,
						"denominator": 100,
						"indicator_value": 91,
						"target_value": 90,
						"status": "Met",
						"calculator_key": "CONFIGURED",
						"calculator_version": "1",
					}
				]
			raise AssertionError(doctype)

		with (
			patch.object(
				reports,
				"_report_context",
				return_value=(
					{"standard": "STD-2026"},
					date(2026, 7, 1),
					date(2026, 7, 31),
				),
			),
			patch.object(reports, "_permissioned_rows", side_effect=rows),
		):
			_columns, data, message, chart, summary = reports.execute_national_ten_goal_report()
		row = data[0]
		self.assertEqual(row["standard_version_checksum"], checksum)
		self.assertEqual(row["indicator_version_checksum"], checksum)
		self.assertEqual(row["indicator_result"], "RESULT-1")
		self.assertEqual(row["indicator_value"], 91)
		self.assertEqual(row["result_target_value"], 90)
		self.assertEqual(row["target_state"], "Stored Result Target Matches Definition")
		self.assertEqual(row["source_result_count"], 1)
		self.assertIn("does not invent", message)
		self.assertEqual(chart["data"]["labels"], ["Met"])
		self.assertTrue(summary)

	def test_improvement_governance_report_joins_receipt_counts_without_narratives(self) -> None:
		checksum = "d" * 64

		def rows(doctype: str, **kwargs):
			del kwargs
			if doctype == "IONE Finding Recurrence Evaluation":
				return [
					{
						"name": "REC-1",
						"policy": "RP-1",
						"policy_checksum": checksum,
						"window_start": "2026-06-01",
						"window_end": "2026-07-01",
						"outcome": "Triggered",
						"occurrence_count": 3,
						"evaluation_hash": checksum,
						"department": "DEPT-1",
					}
				]
			if doctype == "IONE PDCA Project":
				return [
					{
						"name": "PDCA-1",
						"project_type": "Special Recurrence",
						"recurrence_evaluation": "REC-1",
						"recurrence_policy_checksum": checksum,
						"recurrence_snapshot_hash": checksum,
						"status": "Closed",
						"analysis_method": "Combined",
						"root_cause_analysis_complete": 1,
						"standardization_decision": "Adopted",
						"standardization_evidence_hash": checksum,
						"standardized_at": "2026-07-20 12:00:00",
						"department": "DEPT-1",
					}
				]
			if doctype == "IONE Quality Meeting":
				return [
					{
						"name": "MEETING-1",
						"meeting_code": "M-1",
						"meeting_type": "Quality",
						"status": "Closed",
						"agenda_checksum": checksum,
						"closed_at": "2026-07-21 12:00:00",
						"department": "DEPT-1",
					}
				]
			if doctype == "IONE Meeting Minute":
				return [
					{
						"name": "MINUTE-1",
						"meeting": "MEETING-1",
						"status": "Approved",
						"checksum": checksum,
						"reviewed_at": "2026-07-21 11:00:00",
						"department": "DEPT-1",
					}
				]
			if doctype == "IONE Meeting Decision":
				return [
					{
						"name": "DECISION-1",
						"meeting": "MEETING-1",
						"meeting_minute": "MINUTE-1",
						"checksum": checksum,
						"department": "DEPT-1",
					}
				]
			if doctype == "IONE Quality Action Item":
				return [
					{
						"name": "ACTION-1",
						"meeting": "MEETING-1",
						"meeting_decision": "DECISION-1",
						"pdca_project": "PDCA-1",
						"status": "Closed",
						"due_date": "2026-07-25",
						"closed_at": "2026-07-24 12:00:00",
						"latest_verification_round": "ACTION-1-R2",
						"latest_verification_receipt_checksum": checksum,
						"department": "DEPT-1",
					}
				]
			if doctype == "IONE Quality Action Verification Round":
				return [
					{
						"name": "ACTION-1-R1",
						"action_item": "ACTION-1",
						"round_no": 1,
						"decision": "Rework",
						"submitted_by": "owner@example.com",
						"verified_by": "verifier@example.com",
						"verified_at": "2026-07-23 11:00:00",
						"previous_receipt_checksum": "",
						"receipt_checksum": "e" * 64,
						"department": "DEPT-1",
					},
					{
						"name": "ACTION-1-R2",
						"action_item": "ACTION-1",
						"round_no": 2,
						"decision": "Close",
						"submitted_by": "owner@example.com",
						"verified_by": "verifier@example.com",
						"verified_at": "2026-07-24 11:00:00",
						"previous_receipt_checksum": "e" * 64,
						"receipt_checksum": checksum,
						"department": "DEPT-1",
					},
				]
			if doctype == "IONE Quality Experience Share":
				return [
					{
						"name": "EXP-1",
						"experience_code": "E-1",
						"version_number": 1,
						"status": "Published",
						"checksum": checksum,
						"published_at": "2026-07-25 12:00:00",
						"effective_from": "2026-07-25",
						"department": "DEPT-1",
					}
				]
			raise AssertionError(doctype)

		with (
			patch.object(
				reports,
				"_report_context",
				return_value=({}, date(2026, 7, 1), date(2026, 7, 31)),
			),
			patch.object(reports, "_permissioned_rows", side_effect=rows),
			patch.object(reports, "nowdate", return_value="2026-07-30"),
		):
			_columns, data, message, chart, summary = reports.execute_improvement_governance_report()
		row = data[0]
		self.assertEqual(row["recurrence_triggered_count"], 1)
		self.assertEqual(row["special_recurrence_pdca_count"], 1)
		self.assertEqual(row["effect_verification_gate_count"], 1)
		self.assertEqual(row["standardization_receipt_count"], 1)
		self.assertEqual(row["meeting_closed_count"], 1)
		self.assertEqual(row["approved_minute_receipt_count"], 1)
		self.assertEqual(row["meeting_decision_checksum_count"], 1)
		self.assertEqual(row["action_verification_round_count"], 2)
		self.assertEqual(row["action_rework_round_count"], 1)
		self.assertEqual(row["action_chained_receipt_count"], 2)
		self.assertEqual(row["independently_verified_action_count"], 1)
		self.assertEqual(row["experience_publication_receipt_count"], 1)
		self.assertIn("never findings", message)
		self.assertEqual(chart["data"]["datasets"][0]["values"], [1, 1, 1, 1, 1])
		self.assertTrue(summary)
