from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from ione_qms.ai import output_quarantine


class _Doc(SimpleNamespace):
	def get(self, key: str, default=None):
		return getattr(self, key, default)

	def insert(self, *, ignore_permissions: bool = False):
		self.inserted_ignore_permissions = ignore_permissions
		return self


class TestAggregateOutputQuarantine(TestCase):
	def test_request_local_signal_contains_only_task_and_stable_code(self) -> None:
		flags = SimpleNamespace()
		with patch.object(output_quarantine.frappe, "flags", flags):
			output_quarantine.flag_aggregate_output_violation(
				"TASK-1",
				"DIRECT_IDENTITY_NUMBER",
			)
			self.assertEqual(
				output_quarantine.read_aggregate_output_violation("TASK-1"),
				"DIRECT_IDENTITY_NUMBER",
			)
			self.assertEqual(
				getattr(flags, output_quarantine.OUTPUT_SAFETY_FLAG),
				{
					"task": "TASK-1",
					"reason_code": "DIRECT_IDENTITY_NUMBER",
				},
			)
			self.assertEqual(
				output_quarantine.read_aggregate_output_violation("TASK-OTHER"),
				"OUTPUT_SAFETY_SIGNAL_INVALID",
			)

	def test_quarantine_scrubs_flow_surfaces_and_removes_only_exact_attempt_draft(self) -> None:
		rows = [
			{
				"name": "DRAFT-ATTEMPT-1",
				"status": "Draft",
				"reviewed_by": None,
				"reviewed_at": None,
			},
		]
		with (
			patch.object(
				output_quarantine.frappe.db,
				"get_value",
				return_value={
					"reference_doctype": "IONE AI Analysis Task",
					"reference_name": "TASK-1",
				},
			),
			patch.object(output_quarantine.frappe, "get_all", return_value=rows) as get_all,
			patch.object(output_quarantine.frappe.db, "delete") as delete,
			patch.object(output_quarantine.frappe.db, "sql") as sql,
			patch.object(output_quarantine.frappe.db, "set_value") as set_value,
			patch.object(output_quarantine.frappe.db, "commit") as commit,
		):
			removed = output_quarantine.quarantine_aggregate_output_run(
				flow_run="RUN-1",
				task="TASK-1",
				execution_attempt=1,
				reason_code="DIRECT_IDENTITY_NUMBER",
			)

		self.assertEqual(removed, 1)
		self.assertEqual(
			get_all.call_args.kwargs["filters"],
			{
				"task": "TASK-1",
				"execution_attempt": 1,
				"flow_run": "RUN-1",
			},
		)
		delete.assert_called_once_with(
			"IONE AI Report Draft",
			{
				"name": "DRAFT-ATTEMPT-1",
				"task": "TASK-1",
				"execution_attempt": 1,
				"flow_run": "RUN-1",
			},
		)
		self.assertIn("tabFlow Session Message", sql.call_args.args[0])
		self.assertIn("tool_call_id = null", sql.call_args.args[0])
		run_values = set_value.call_args.args[2]
		self.assertEqual(run_values["status"], "Failed")
		self.assertEqual(run_values["output"], output_quarantine.QUARANTINED_OUTPUT)
		self.assertNotIn("DRAFT-ATTEMPT-2", str(run_values))
		self.assertIn("DIRECT_IDENTITY_NUMBER", run_values["error"])
		commit.assert_called_once_with()

	def test_quarantine_receipt_is_exact_and_recoverable_without_content(self) -> None:
		run = _Doc(
			name="RUN-1",
			status="Failed",
			output=output_quarantine.QUARANTINED_OUTPUT,
			questions=None,
			tool_calls=output_quarantine._quarantined_tool_calls(
				"DIRECT_EMAIL_ADDRESS",
				1,
			),
			error=(f"{output_quarantine.OUTPUT_SAFETY_ERROR_PREFIX}DIRECT_EMAIL_ADDRESS:1"),
		)
		with patch.object(output_quarantine.frappe.db, "sql", return_value=[(0,)]):
			self.assertEqual(
				output_quarantine.aggregate_output_quarantine_receipt(run),
				("DIRECT_EMAIL_ADDRESS", 1),
			)

	def test_outer_scanner_covers_final_output_and_message_tool_surfaces(self) -> None:
		task = _Doc(name="TASK-1", task_type="Quality Report", origin="Scheduled")
		run = _Doc(
			name="RUN-1",
			output="本月报告包含联系电话 13800138000",
			tool_calls=None,
			questions=None,
			error=None,
		)
		with patch.object(output_quarantine.frappe, "get_all", return_value=[]):
			self.assertEqual(
				output_quarantine.aggregate_flow_run_output_violation(run, task),
				"DIRECT_MOBILE_NUMBER",
			)

		run.output = "本月完成 120 例, 达标率 96%."
		messages = [
			{
				"role": "assistant",
				"content": "聚合结果",
				"tool_calls": None,
				"tool_call_id": "13800138000",
			}
		]
		with patch.object(output_quarantine.frappe, "get_all", return_value=messages):
			self.assertEqual(
				output_quarantine.aggregate_flow_run_output_violation(run, task),
				"DIRECT_MOBILE_NUMBER",
			)

	def test_outer_scanner_decodes_json_escaped_tool_arguments(self) -> None:
		task = _Doc(name="TASK-1", task_type="Quality Report", origin="Scheduled")
		run = _Doc(
			name="RUN-1",
			output="聚合结果",
			tool_calls=r'[{"name":"unknown","arguments":{"phone":"\u0031\u0033\u0038\u0030\u0030\u0031\u0033\u0038\u0030\u0030\u0030"}}]',
			questions=None,
			error=None,
		)
		with patch.object(output_quarantine.frappe, "get_all", return_value=[]):
			self.assertEqual(
				output_quarantine.aggregate_flow_run_output_violation(run, task),
				"DIRECT_MOBILE_NUMBER",
			)

	def test_incident_is_deterministic_and_contains_no_model_output(self) -> None:
		task = _Doc(name="TASK-1")
		policy = _Doc(name="POLICY-1", service_user="service@example.invalid")
		agent = _Doc(name="AGENT-1")
		run = _Doc(name="RUN-1")
		inserted = _Doc(name="INCIDENT-1")

		def get_value(doctype, *args, **kwargs):
			del args, kwargs
			if doctype == "IONE Agent Incident":
				return None
			if doctype == "IONE Agent Release":
				return "RELEASE-1"
			raise AssertionError(doctype)

		with (
			patch.object(output_quarantine.frappe.db, "get_value", side_effect=get_value),
			patch.object(output_quarantine, "now_datetime", return_value=datetime(2026, 7, 30, 9, 0)),
			patch.object(output_quarantine.frappe, "get_doc", return_value=inserted) as get_doc,
		):
			name = output_quarantine.append_aggregate_output_incident(
				task=task,
				policy=policy,
				agent=agent,
				run=run,
				attempt=1,
				token_hash="a" * 64,
				reason_code="DIRECT_MOBILE_NUMBER",
				quarantined_draft_count=1,
			)

		self.assertEqual(name, inserted.name)
		values = get_doc.call_args.args[0]
		self.assertEqual(values["incident_type"], "Patient Data Exposure")
		self.assertEqual(values["status"], "Contained")
		self.assertEqual(len(values["record_hash"]), 64)
		self.assertIn("DIRECT_MOBILE_NUMBER", values["details"])
		self.assertNotIn("13800138000", str(values))
		self.assertTrue(inserted.inserted_ignore_permissions)
