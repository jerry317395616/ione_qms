from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from ione_qms.ai import approvals


class _Document(SimpleNamespace):
	def get(self, key: str, default=None):
		return getattr(self, key, default)

	def check_permission(self, permission_type: str) -> None:
		self.checked_permission = permission_type

	def save(self, *, ignore_permissions: bool = False) -> None:
		self.saved_ignore_permissions = ignore_permissions

	def db_set(self, values, value=None, *, update_modified: bool = True) -> None:
		if isinstance(values, dict):
			for key, item in values.items():
				setattr(self, key, item)
		else:
			setattr(self, values, value)
		self.db_set_update_modified = update_modified


class TestAIToolApprovalConcurrency(TestCase):
	def test_run_row_is_locked_until_frappe_commits_the_transaction(self) -> None:
		run = _Document(name="FLOW-RUN-1")
		with patch.object(approvals.frappe, "get_doc", return_value=run) as get_doc:
			self.assertIs(approvals._flow_run_for_update(run.name), run)
		get_doc.assert_called_once_with("Flow Run", run.name, for_update=True)

	def test_every_transition_uses_one_canonical_run_lock_key(self) -> None:
		sentinel = nullcontext()
		with patch.object(
			approvals.frappe.db,
			"advisory_lock",
			return_value=sentinel,
		) as advisory_lock:
			self.assertIs(approvals._run_advisory_lock("FLOW-RUN-1"), sentinel)
		advisory_lock.assert_called_once_with(
			"ione-qms:ai-tool-run:FLOW-RUN-1",
			timeout=approvals.APPROVAL_RUN_LOCK_TIMEOUT_SECONDS,
		)

	def test_serialized_last_review_schedules_exactly_one_resume_job(self) -> None:
		run = _Document(name="FLOW-RUN-1", status="Paused", session="FLOW-SESSION-1")
		first = _Document(name="APPROVAL-1", task="TASK-1", status="Reviewed")
		last = _Document(name="APPROVAL-2", task="TASK-1", status="Pending")
		task = _Document(
			name="TASK-1",
			status="Pending Confirmation",
			flow_run=run.name,
			flow_session=run.session,
			execution_attempt=1,
		)
		with (
			patch.object(approvals.frappe, "enqueue") as enqueue,
			patch.object(approvals.frappe, "get_doc", return_value=task) as get_doc,
		):
			self.assertFalse(
				approvals._enqueue_resume_if_ready(
					run.name,
					run=run,
					current=[first, last],
				)
			)
			enqueue.assert_not_called()
			get_doc.assert_not_called()

			last.status = "Reviewed"
			self.assertTrue(
				approvals._enqueue_resume_if_ready(
					run.name,
					run=run,
					current=[first, last],
				)
			)
		get_doc.assert_called_once_with("IONE AI Analysis Task", task.name, for_update=True)
		enqueue.assert_called_once_with(
			"ione_qms.ai.approvals.resume_tool_approval_run",
			queue="long",
			enqueue_after_commit=True,
			job_id="ione-qms:ai-tool-resume:FLOW-RUN-1:attempt:2",
			deduplicate=True,
			flow_run="FLOW-RUN-1",
			expected_attempt=2,
		)

	def test_each_confirmation_generation_uses_the_next_attempt_job_fence(self) -> None:
		run = _Document(name="FLOW-RUN-1", status="Paused", session="FLOW-SESSION-1")
		current = [_Document(name="APPROVAL-2", task="TASK-1", status="Reviewed")]
		task = _Document(
			name="TASK-1",
			status="Pending Confirmation",
			flow_run=run.name,
			flow_session=run.session,
			execution_attempt=2,
		)
		with (
			patch.object(approvals.frappe, "enqueue") as enqueue,
			patch.object(approvals.frappe, "get_doc", return_value=task),
		):
			self.assertTrue(
				approvals._enqueue_resume_if_ready(
					run.name,
					run=run,
					current=current,
				)
			)
		self.assertEqual(
			enqueue.call_args.kwargs["job_id"],
			"ione-qms:ai-tool-resume:FLOW-RUN-1:attempt:3",
		)
		self.assertEqual(enqueue.call_args.kwargs["expected_attempt"], 3)

	def test_review_locks_run_and_reloads_mutable_rows_for_update(self) -> None:
		now = datetime(2026, 7, 30, 12, 0, 0)
		preview = _Document(name="APPROVAL-2", flow_run="FLOW-RUN-1")
		run = _Document(name="FLOW-RUN-1", status="Paused")
		doc = _Document(
			name="APPROVAL-2",
			flow_run=run.name,
			task="TASK-1",
			status="Pending",
			expires_at=now + timedelta(minutes=10),
		)
		task = _Document(
			name="TASK-1",
			hospital="HOSPITAL-1",
			campus=None,
			department=None,
			requested_by="requester@example.com",
		)
		get_doc_calls: list[tuple[str, str, bool]] = []

		def get_doc(doctype: str, name: str, *, for_update: bool = False):
			get_doc_calls.append((doctype, name, for_update))
			if doctype == "IONE AI Tool Approval" and not for_update:
				return preview
			if doctype == "IONE AI Tool Approval":
				return doc
			if doctype == "IONE AI Analysis Task":
				return task
			raise AssertionError((doctype, name, for_update))

		with (
			patch.object(approvals.frappe, "session", SimpleNamespace(user="reviewer@example.com")),
			patch.object(approvals.frappe, "flags", SimpleNamespace()),
			patch.object(approvals.frappe, "get_doc", side_effect=get_doc),
			patch.object(approvals, "_require_named_reviewer"),
			patch.object(approvals, "_run_advisory_lock", return_value=nullcontext()) as run_lock,
			patch.object(approvals, "_flow_run_for_update", return_value=run) as run_for_update,
			patch.object(approvals, "_assert_current_approval") as assert_current,
			patch.object(approvals, "_enqueue_resume_if_ready", return_value=True) as enqueue_ready,
			patch.object(approvals, "require_scope_read"),
			patch.object(approvals, "now_datetime", return_value=now),
		):
			result = approvals.review_tool_approval(
				doc.name,
				"Approve",
				"Evidence checked.",
			)

		run_lock.assert_called_once_with(run.name)
		run_for_update.assert_called_once_with(run.name)
		self.assertIn(("IONE AI Tool Approval", doc.name, True), get_doc_calls)
		self.assertIn(("IONE AI Analysis Task", task.name, True), get_doc_calls)
		assert_current.assert_called_once_with(doc, run=run, task=task)
		enqueue_ready.assert_called_once_with(run.name, run=run)
		self.assertEqual(doc.status, "Reviewed")
		self.assertEqual(result["resume_queued"], True)

	def test_expiry_rechecks_snapshot_candidate_and_preserves_reviewed_approval(self) -> None:
		cutoff = datetime(2026, 7, 30, 12, 0, 0)
		run = _Document(name="FLOW-RUN-1", status="Paused")
		reviewed = _Document(
			name="APPROVAL-1",
			flow_run=run.name,
			status="Reviewed",
			expires_at=cutoff - timedelta(seconds=1),
		)
		rows = [{"name": reviewed.name, "flow_run": run.name}]
		with (
			patch.object(approvals, "now_datetime", return_value=cutoff),
			patch.object(approvals.frappe, "get_all", return_value=rows),
			patch.object(approvals, "_run_advisory_lock", return_value=nullcontext()) as run_lock,
			patch.object(approvals, "_flow_run_for_update", return_value=run),
			patch.object(approvals.frappe, "get_doc", return_value=reviewed) as get_doc,
			patch.object(approvals, "_expire_approval") as expire,
			patch.object(approvals, "_current_approval_documents") as current,
			patch.object(
				approvals,
				"reconcile_reviewed_tool_approvals",
				return_value={"consumed": 0, "errors": 0},
			),
		):
			result = approvals.expire_tool_approvals()

		run_lock.assert_called_once_with(run.name)
		get_doc.assert_called_once_with(
			"IONE AI Tool Approval",
			reviewed.name,
			for_update=True,
		)
		expire.assert_not_called()
		current.assert_not_called()
		self.assertEqual(
			result,
			{
				"expired": 0,
				"tasks_cancelled": 0,
				"reviewed_consumed": 0,
				"reconciliation_errors": 0,
			},
		)

	def test_expiry_and_resume_take_the_same_run_lock(self) -> None:
		run = _Document(name="FLOW-RUN-1", status="Completed")
		with (
			patch.object(approvals, "_run_advisory_lock", return_value=nullcontext()) as run_lock,
			patch.object(approvals, "_flow_run_for_update", return_value=run),
			patch.object(approvals, "_consume_reviewed_terminal_approvals", return_value=0),
		):
			result = approvals.resume_tool_approval_run(run.name)
		run_lock.assert_called_once_with(run.name)
		self.assertEqual(
			result,
			{
				"flow_run": run.name,
				"status": "Completed",
				"skipped": True,
				"approvals_consumed": 0,
			},
		)
