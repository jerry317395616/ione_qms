from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from ione_qms.ai import flow_artifacts
from ione_qms.overrides import flow_session
from ione_qms.tasks import security


class TestFlowRetentionContract(TestCase):
	def test_artifact_hashes_cover_input_output_and_tools_without_attachments(self) -> None:
		messages = [
			frappe._dict(
				idx=1,
				role="user",
				content="INPUT-MARKER",
				tool_call_id=None,
				tool_calls=None,
				run="RUN-1",
			),
			frappe._dict(
				idx=2,
				role="assistant",
				content="OUTPUT-MARKER",
				tool_call_id=None,
				tool_calls='[{"name":"read"}]',
				run="RUN-1",
			),
			frappe._dict(
				idx=3,
				role="tool",
				content="TOOL-RESULT-MARKER",
				tool_call_id="call-1",
				tool_calls=None,
				run="RUN-1",
			),
		]

		def get_all(doctype, **_kwargs):
			return messages if doctype == "Flow Session Message" else []

		with (
			patch.object(flow_artifacts, "_doctype_exists", return_value=True),
			patch.object(flow_artifacts.frappe, "get_all", side_effect=get_all),
			patch.object(
				flow_artifacts.frappe.db,
				"get_value",
				return_value=frappe._dict(
					tool_calls='[{"result":"TOOL-RESULT-MARKER"}]',
					questions='[{"prompt":"CONFIRM-MARKER"}]',
					error=None,
				),
			),
		):
			hashes = flow_artifacts.flow_run_artifact_hashes("RUN-1")
		self.assertEqual(
			set(hashes),
			{
				"input_messages_hash",
				"output_messages_hash",
				"tool_trace_hash",
				"attachment_text_hash",
			},
		)
		self.assertTrue(all(len(digest) == 64 for digest in hashes.values()))

	def test_governed_flow_attachments_fail_closed_before_hashing(self) -> None:
		attachment = frappe._dict(
			idx=1,
			file="FILE-1",
			file_name="patient.txt",
			file_size=10,
			run="RUN-1",
			mode="Inline",
			extracted_text="ATTACHMENT-MARKER",
		)
		with (
			patch.object(flow_artifacts, "_doctype_exists", return_value=True),
			patch.object(flow_artifacts.frappe, "get_all", return_value=[attachment]),
			self.assertRaises(frappe.ValidationError),
		):
			flow_artifacts.flow_run_artifact_hashes("RUN-1")

	def test_failed_error_is_sanitized_without_original_text(self) -> None:
		with (
			patch.object(flow_artifacts, "_doctype_exists", return_value=True),
			patch.object(flow_artifacts.frappe.db, "set_value") as set_value,
		):
			flow_artifacts.sanitize_flow_run_error("RUN-1", "ProviderTimeout")
		values = set_value.call_args.args
		self.assertEqual(values[:2], ("Flow Run", "RUN-1"))
		self.assertIn("GovernedExecutionFailed:ProviderTimeout", values)

	def test_flow_retention_selects_runs_directly_not_only_existing_links(self) -> None:
		settings = SimpleNamespace(
			get=lambda fieldname: {
				"input_retention_days": 30,
				"output_retention_days": 30,
			}.get(fieldname)
		)
		summary = {"reviewed": 0, "redacted": 0, "skipped": 0}
		with (
			patch.object(security.frappe.db, "exists", return_value=True),
			patch.object(security.frappe.db, "sql", return_value=[]) as sql,
		):
			security._redact_flow_runs(settings, 10, summary)
		self.assertEqual(sql.call_count, 2)
		for call in sql.call_args_list:
			query = call.args[0]
			self.assertIn("reference_doctype = 'IONE AI Analysis Task'", query)
			self.assertIn("coalesce(", query)
			self.assertIn("'Running'", query)

	def test_completed_flow_is_not_redacted_before_task_is_terminal(self) -> None:
		settings = SimpleNamespace(
			get=lambda fieldname: {
				"input_retention_days": 30,
				"output_retention_days": 30,
			}.get(fieldname)
		)
		run = frappe._dict(
			name="RUN-1",
			reference_name="TASK-1",
			status="Completed",
			input="clinical input",
			output="clinical output",
		)
		summary = {"reviewed": 0, "redacted": 0, "skipped": 0}
		with (
			patch.object(security.frappe.db, "exists", return_value=True),
			patch.object(security, "_aged_unredacted_flow_runs", return_value=[run]),
			patch.object(security, "_task_is_final", return_value=False),
			patch.object(security.frappe.db, "savepoint") as savepoint,
			patch.object(security.frappe.db, "set_value") as set_value,
		):
			security._redact_flow_runs(settings, 10, summary)
		savepoint.assert_not_called()
		set_value.assert_not_called()
		self.assertEqual(summary, {"reviewed": 0, "redacted": 0, "skipped": 0})

	def test_governed_session_cleanup_requires_complete_verified_markers(self) -> None:
		run = frappe._dict(
			name="RUN-1",
			reference_doctype="IONE AI Analysis Task",
			reference_name="TASK-1",
			input=security._REDACTED,
			output=security._REDACTED,
			tool_calls=security._REDACTED_JSON,
			questions=security._REDACTED_JSON,
			error=None,
		)
		link = frappe._dict(
			{
				fieldname: "a" * 64
				for fieldname in (
					"input_hash",
					"output_hash",
					"input_messages_hash",
					"output_messages_hash",
					"tool_trace_hash",
					"attachment_text_hash",
				)
			}
		)

		def get_all(doctype, **_kwargs):
			if doctype == "Flow Run":
				return [run]
			if doctype == "Flow Session Message":
				return [
					frappe._dict(
						role="user",
						content=security._REDACTED,
						tool_calls=security._REDACTED_JSON,
					)
				]
			raise AssertionError(doctype)

		def exists(doctype, _filters):
			return doctype == "IONE Flow Run Link"

		with (
			patch.object(security.frappe, "get_all", side_effect=get_all),
			patch.object(security.frappe.db, "exists", side_effect=exists),
			patch.object(security, "_task_is_final", return_value=True),
			patch.object(security, "_flow_run_link", return_value=link),
		):
			self.assertTrue(security.governed_flow_session_retention_verified("SESSION-1"))
			run.input = "unverified clinical input"
			self.assertFalse(security.governed_flow_session_retention_verified("SESSION-1"))

	def test_generic_flow_cleanup_holds_unverified_governed_session(self) -> None:
		with (
			patch.object(flow_session.frappe, "get_all", return_value=["SESSION-1"]),
			patch.object(flow_session, "_session_is_governed", return_value=True),
			patch.object(
				flow_session,
				"_session_has_governed_run_lineage",
				return_value=True,
			),
			patch.object(
				flow_session,
				"governed_flow_session_retention_verified",
				return_value=False,
			),
			patch.object(flow_session.frappe.db, "delete") as delete,
		):
			flow_session.IONEGovernedFlowSessionMixin.clear_old_logs(days=30)
		delete.assert_not_called()

	def test_generic_flow_cleanup_uses_run_lineage_after_agent_configuration_is_gone(self) -> None:
		with (
			patch.object(flow_session.frappe, "get_all", return_value=["SESSION-1"]),
			patch.object(flow_session, "_session_is_governed", return_value=False),
			patch.object(
				flow_session,
				"_session_has_governed_run_lineage",
				return_value=True,
			),
			patch.object(
				flow_session,
				"governed_flow_session_retention_verified",
				return_value=False,
			) as verified,
			patch.object(flow_session.frappe.db, "delete") as delete,
		):
			flow_session.IONEGovernedFlowSessionMixin.clear_old_logs(days=30)
		verified.assert_called_once_with("SESSION-1")
		delete.assert_not_called()

	def test_generic_flow_cleanup_preserves_official_behavior_for_ordinary_session(self) -> None:
		with (
			patch.object(flow_session.frappe, "get_all", return_value=["SESSION-ORDINARY"]),
			patch.object(flow_session, "_session_is_governed", return_value=False),
			patch.object(
				flow_session,
				"_session_has_governed_run_lineage",
				return_value=False,
			),
			patch.object(
				flow_session.frappe.utils,
				"create_batch",
				return_value=[["SESSION-ORDINARY"]],
			),
			patch.object(flow_session.frappe.db, "delete") as delete,
			patch("flow.flow.doctype.flow_session.flow_session._delete_attachment_files") as delete_files,
			patch("flow.flow.doctype.flow_session.flow_session._purge_attachment_chunks") as purge_chunks,
		):
			flow_session.IONEGovernedFlowSessionMixin.clear_old_logs(days=30)
		self.assertEqual(delete.call_count, 4)
		delete_files.assert_called_once_with(["SESSION-ORDINARY"])
		purge_chunks.assert_called_once_with(["SESSION-ORDINARY"])
