from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import call, patch

from ione_qms.ai import orchestrator


class _Document(SimpleNamespace):
	def get(self, key: str, default=None):
		return getattr(self, key, default)

	def db_set(self, values, value=None, *, update_modified: bool = True) -> None:
		if isinstance(values, dict):
			for key, item in values.items():
				setattr(self, key, item)
		else:
			setattr(self, values, value)
		self.db_set_update_modified = update_modified

	def reload(self) -> None:
		self.reloaded = True

	def insert(self, *, ignore_permissions: bool = False):
		self.inserted_ignore_permissions = ignore_permissions
		return self


def _task(**overrides) -> _Document:
	values = {
		"name": "TASK-1",
		"doctype": "IONE AI Analysis Task",
		"policy": "POLICY-1",
		"status": "Pending",
		"execution_attempt": 0,
		"execution_token": None,
		"last_execution_token_hash": None,
		"lease_expires_at": None,
		"hospital": "HOSPITAL-1",
		"campus": None,
		"department": "DEPARTMENT-1",
		"ward": None,
		"patient": None,
		"encounter": None,
		"responsible_staff": None,
	}
	values.update(overrides)
	return _Document(**values)


class TestAIExecutionAtomicity(TestCase):
	def test_two_workers_can_claim_only_one_attempt(self) -> None:
		task = _task()
		claimed_at = datetime(2026, 7, 30, 8, 0, 0)
		lease_expires_at = claimed_at + timedelta(minutes=orchestrator.EXECUTION_LEASE_MINUTES)
		with (
			patch.object(orchestrator, "_task_advisory_lock", return_value=nullcontext()) as lock,
			patch.object(orchestrator.frappe, "get_doc", return_value=task) as get_doc,
			patch.object(orchestrator.secrets, "token_urlsafe", return_value="one-random-token"),
			patch.object(orchestrator, "now_datetime", return_value=claimed_at),
			patch.object(orchestrator, "add_to_date", return_value=lease_expires_at),
			patch.object(orchestrator, "_append_execution_event") as append_event,
			patch.object(orchestrator.frappe.db, "commit") as commit,
		):
			first = orchestrator._claim_analysis_task(task.name)
			second = orchestrator._claim_analysis_task(task.name)

		self.assertIsNotNone(first)
		self.assertIsNone(second)
		self.assertEqual(task.status, "Running")
		self.assertEqual(task.execution_attempt, 1)
		self.assertEqual(task.execution_token, "one-random-token")
		self.assertEqual(lock.call_count, 2)
		self.assertEqual(
			get_doc.call_args_list,
			[
				call("IONE AI Analysis Task", task.name, for_update=True),
				call("IONE AI Analysis Task", task.name, for_update=True),
			],
		)
		append_event.assert_called_once()
		self.assertEqual(append_event.call_args.kwargs["event_type"], "Claimed")
		# The second worker observes Running and neither appends nor commits a
		# second claim before any model call can begin.
		commit.assert_called_once_with()

	def test_completion_is_a_status_attempt_and_token_cas(self) -> None:
		execution_fence = "x" * 43
		task = _task(status="Running", execution_attempt=2, execution_token=execution_fence)
		claim = orchestrator.ExecutionClaim(
			task=task,
			attempt=2,
			token=execution_fence,
			token_hash=orchestrator._hash_text(execution_fence),
			claimed_at=datetime(2026, 7, 30, 8, 0, 0),
			lease_expires_at=datetime(2026, 7, 30, 8, 30, 0),
		)
		run = _Document(name="RUN-2", session="SESSION-2", output="safe output")
		with (
			patch.object(orchestrator, "now_datetime", return_value=datetime(2026, 7, 30, 8, 5, 0)),
			patch.object(orchestrator.frappe.db, "sql") as sql,
			patch.object(orchestrator, "_affected_rows", return_value=1),
		):
			self.assertTrue(
				orchestrator._cas_attempt_outcome(
					task,
					claim=claim,
					run=run,
					status="Completed",
					detail_code="Completed",
				)
			)

		query, values = sql.call_args.args
		self.assertIn("status = 'Running'", query)
		self.assertIn("execution_attempt = %s", query)
		self.assertIn("execution_token = %s", query)
		self.assertEqual(values[-3:], (task.name, claim.attempt, claim.token))
		self.assertIn(claim.token_hash, values)

		with (
			patch.object(orchestrator.frappe.db, "sql"),
			patch.object(orchestrator, "_affected_rows", return_value=0),
		):
			self.assertFalse(
				orchestrator._cas_attempt_outcome(
					task,
					claim=claim,
					run=run,
					status="Completed",
					detail_code="Completed",
				)
			)

	def test_stale_running_lease_appends_event_and_queues_next_attempt(self) -> None:
		cutoff = datetime(2026, 7, 30, 9, 0, 0)
		expired_fence = "y" * 43
		task = _task(
			status="Running",
			execution_attempt=1,
			execution_token=expired_fence,
			lease_expires_at=cutoff - timedelta(seconds=1),
		)
		with (
			patch.object(orchestrator, "now_datetime", return_value=cutoff),
			patch.object(orchestrator.frappe, "get_all", return_value=[{"name": task.name}]),
			patch.object(orchestrator, "_task_advisory_lock", return_value=nullcontext()),
			patch.object(orchestrator, "_reconcile_current_attempt_flow_run", return_value=False),
			patch.object(orchestrator, "_renew_live_attempt_lease", return_value=False),
			patch.object(orchestrator.frappe, "get_doc", return_value=task),
			patch.object(orchestrator, "_cas_expire_lease", return_value=True) as expire,
			patch.object(orchestrator, "_atomic_savepoint", return_value=nullcontext()),
			patch.object(orchestrator, "_append_execution_event") as append_event,
			patch.object(orchestrator.frappe.db, "commit"),
			patch.object(orchestrator, "_enqueue_task_name") as enqueue,
			patch.object(
				orchestrator,
				"reconcile_orphan_flow_runs",
				return_value={"linked": 0, "skipped": 0, "errors": 0},
			),
			patch.object(orchestrator, "_enqueue_retry_tasks", return_value=0),
			patch.object(orchestrator, "_enqueue_pending_tasks", return_value=0),
		):
			result = orchestrator.recover_stale_analysis_tasks(limit=1)

		expire.assert_called_once()
		self.assertEqual(append_event.call_args.kwargs["event_type"], "Lease Expired")
		self.assertEqual(append_event.call_args.kwargs["outcome_status"], "Retry")
		enqueue.assert_called_once_with(
			task.name,
			attempt_hint=2,
			enqueue_after_commit=False,
		)
		self.assertEqual(result["retried"], 1)
		self.assertEqual(result["recovered"], 0)
		self.assertEqual(result["failed"], 0)

	def test_terminal_orphan_is_recovered_before_expired_lease_is_retried(self) -> None:
		cutoff = datetime(2026, 7, 30, 9, 0, 0)
		task = _task(
			status="Running",
			execution_attempt=1,
			execution_token="r" * 43,
			lease_expires_at=cutoff - timedelta(seconds=1),
		)
		with (
			patch.object(orchestrator, "now_datetime", return_value=cutoff),
			patch.object(orchestrator.frappe, "get_all", return_value=[{"name": task.name}]),
			patch.object(orchestrator, "_reconcile_current_attempt_flow_run", return_value=True) as reconcile,
			patch.object(orchestrator, "_cas_expire_lease") as expire,
			patch.object(
				orchestrator,
				"reconcile_orphan_flow_runs",
				return_value={"linked": 0, "skipped": 0, "errors": 0},
			),
			patch.object(orchestrator, "_enqueue_retry_tasks", return_value=0),
			patch.object(orchestrator, "_enqueue_pending_tasks", return_value=0),
		):
			result = orchestrator.recover_stale_analysis_tasks(limit=1)

		reconcile.assert_called_once_with(task.name)
		expire.assert_not_called()
		self.assertEqual(result["recovered"], 1)
		self.assertEqual(result["retried"], 0)

	def test_pending_task_sweep_repairs_lost_after_commit_enqueue(self) -> None:
		rows = [
			{"name": "TASK-PENDING-1", "execution_attempt": 0},
			{"name": "TASK-PENDING-2", "execution_attempt": 1},
		]
		with (
			patch.object(orchestrator, "ai_runtime_enabled", return_value=True),
			patch.object(orchestrator.frappe, "get_all", return_value=rows),
			patch.object(orchestrator, "_enqueue_task_name") as enqueue,
		):
			self.assertEqual(orchestrator._enqueue_pending_tasks(limit=2), 2)

		self.assertEqual(
			enqueue.call_args_list,
			[
				call(
					"TASK-PENDING-1",
					attempt_hint=1,
					enqueue_after_commit=True,
				),
				call(
					"TASK-PENDING-2",
					attempt_hint=2,
					enqueue_after_commit=True,
				),
			],
		)

	def test_agent_exception_sanitizes_and_links_exact_failed_run(self) -> None:
		execution_fence = "z" * 43
		task = _task(status="Running", execution_attempt=1, execution_token=execution_fence)
		claim = orchestrator.ExecutionClaim(
			task=task,
			attempt=1,
			token=execution_fence,
			token_hash=orchestrator._hash_text(execution_fence),
			claimed_at=datetime(2026, 7, 30, 8, 0, 0),
			lease_expires_at=datetime(2026, 7, 30, 8, 30, 0),
		)
		failed_run = _Document(name="FLOW-RUN-1")
		policy = _Document(
			name="POLICY-1",
			flow_agent="AGENT-1",
			service_user="service@example.test",
			allow_auto_approve=0,
		)
		agent = _Document(name="AGENT-1")
		agent.run = lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("secret payload"))
		order: list[str] = []
		with (
			patch.object(orchestrator, "require_ai_runtime_enabled"),
			patch.object(orchestrator, "_claim_analysis_task", return_value=claim),
			patch.object(
				orchestrator.frappe,
				"session",
				SimpleNamespace(user="scheduler@example.test"),
			),
			patch.object(orchestrator.frappe, "flags", SimpleNamespace()),
			patch.object(
				orchestrator.frappe,
				"get_doc",
				side_effect=lambda doctype, name: policy if doctype == "IONE Agent Policy" else agent,
			),
			patch.object(orchestrator, "validate_policy_runtime"),
			patch.object(orchestrator, "_build_prompt", return_value="governed prompt"),
			patch.object(
				orchestrator,
				"_capture_flow_run_fence",
				return_value=orchestrator.FlowRunFence(
					created_after=datetime(2026, 7, 30, 8, 0, 1),
					existing_runs=frozenset(),
					expected_input_hash=orchestrator._hash_text("governed prompt"),
				),
			),
			patch.object(orchestrator.frappe, "set_user"),
			patch.object(orchestrator, "_find_new_failed_flow_run", return_value=failed_run),
			patch.object(
				orchestrator,
				"_sanitize_failed_run",
				side_effect=lambda *args: order.append("sanitize"),
			) as sanitize,
			patch.object(
				orchestrator,
				"_finalize_attempt",
				side_effect=lambda *args, **kwargs: order.append("finalize") or True,
			) as finalize,
			patch.object(orchestrator.frappe, "log_error"),
		):
			result = orchestrator.run_analysis_task(task.name)

		sanitize.assert_called_once_with(failed_run.name, "TimeoutError")
		self.assertEqual(order, ["sanitize", "finalize"])
		self.assertEqual(finalize.call_args.kwargs["status"], "Failed")
		self.assertIs(finalize.call_args.kwargs["run"], failed_run)
		self.assertEqual(result["flow_run"], failed_run.name)
		self.assertEqual(result["status"], "Failed")

	def test_aggregate_output_signal_quarantines_before_failed_incident_finalization(self) -> None:
		task = _task(
			task_type="Quality Report",
			origin="Scheduled",
		)
		claim = orchestrator.ExecutionClaim(
			task=task,
			attempt=1,
			token="s" * 43,
			token_hash="a" * 64,
			claimed_at=datetime(2026, 7, 30, 8, 0, 0),
			lease_expires_at=datetime(2026, 7, 30, 8, 30, 0),
		)
		prompt = "governed aggregate prompt"
		fence = orchestrator.FlowRunFence(
			created_after=datetime(2026, 7, 30, 8, 0, 1),
			existing_runs=frozenset(),
			expected_input_hash=orchestrator._hash_text(prompt),
		)
		run = _Document(
			name="RUN-1",
			status="Completed",
			reference_doctype="IONE AI Analysis Task",
			reference_name=task.name,
			input=prompt,
			creation=datetime(2026, 7, 30, 8, 0, 2),
		)
		policy = _Document(
			name="POLICY-1",
			flow_agent="AGENT-1",
			service_user="service@example.test",
			allow_auto_approve=0,
		)
		agent = _Document(name="AGENT-1")

		def run_with_violation(*args, **kwargs):
			del args, kwargs
			setattr(
				orchestrator.frappe.flags,
				orchestrator.OUTPUT_SAFETY_FLAG,
				{
					"task": task.name,
					"reason_code": "DIRECT_IDENTITY_NUMBER",
				},
			)
			return run

		agent.run = run_with_violation
		with (
			patch.object(orchestrator, "require_ai_runtime_enabled"),
			patch.object(orchestrator, "_claim_analysis_task", return_value=claim),
			patch.object(
				orchestrator.frappe,
				"session",
				SimpleNamespace(user="scheduler@example.test"),
			),
			patch.object(orchestrator.frappe, "flags", SimpleNamespace()),
			patch.object(
				orchestrator.frappe,
				"get_doc",
				side_effect=lambda doctype, name: policy if doctype == "IONE Agent Policy" else agent,
			),
			patch.object(orchestrator, "validate_policy_runtime"),
			patch.object(orchestrator, "_build_prompt", return_value=prompt),
			patch.object(orchestrator, "_capture_flow_run_fence", return_value=fence),
			patch.object(orchestrator.frappe, "set_user"),
			patch.object(orchestrator, "_assert_returned_run_matches_claim"),
			patch.object(
				orchestrator,
				"quarantine_aggregate_output_run",
				return_value=1,
			) as quarantine,
			patch.object(orchestrator, "_finalize_attempt", return_value=True) as finalize,
		):
			result = orchestrator.run_analysis_task(task.name)

		quarantine.assert_called_once_with(
			flow_run=run.name,
			task=task.name,
			execution_attempt=claim.attempt,
			reason_code="DIRECT_IDENTITY_NUMBER",
		)
		self.assertEqual(finalize.call_args.kwargs["status"], "Failed")
		self.assertEqual(
			finalize.call_args.kwargs["output_safety_violation"],
			"DIRECT_IDENTITY_NUMBER",
		)
		self.assertEqual(finalize.call_args.kwargs["quarantined_draft_count"], 1)
		self.assertTrue(result["quarantined"])
		self.assertEqual(result["status"], "Failed")

	def test_failed_run_discovery_excludes_other_attempts_and_preexisting_rows(self) -> None:
		task = _task()
		execution_fence = "w" * 43
		claim = orchestrator.ExecutionClaim(
			task=task,
			attempt=2,
			token=execution_fence,
			token_hash=orchestrator._hash_text(execution_fence),
			claimed_at=datetime(2026, 7, 30, 8, 0, 0),
			lease_expires_at=datetime(2026, 7, 30, 8, 30, 0),
		)
		prompt = "attempt-two-prompt"
		fence = orchestrator.FlowRunFence(
			created_after=datetime(2026, 7, 30, 8, 0, 1),
			existing_runs=frozenset({"OLD-RUN"}),
			expected_input_hash=orchestrator._hash_text(prompt),
		)
		expected = _Document(name="RUN-2")
		with (
			patch.object(
				orchestrator.frappe,
				"get_all",
				return_value=[
					{"name": "OLD-RUN", "input": prompt},
					{"name": "RUN-OTHER-ATTEMPT", "input": "attempt-one-prompt"},
					{"name": expected.name, "input": prompt},
				],
			),
			patch.object(orchestrator.frappe, "get_doc", return_value=expected),
		):
			actual = orchestrator._find_new_failed_flow_run(claim, fence)
		self.assertIs(actual, expected)

	def test_flow_link_snapshots_scope_attempt_and_every_artifact_hash(self) -> None:
		task = _task()
		policy = _Document(name="POLICY-1")
		agent = _Document(name="AGENT-1", model="MODEL-1")
		run = _Document(
			name="RUN-1",
			session="SESSION-1",
			status="Failed",
			tool_calls=[],
			input="governed prompt",
			output=None,
			usage=None,
			creation=datetime(2026, 7, 30, 8, 0, 0),
			modified=datetime(2026, 7, 30, 8, 5, 0),
		)
		link = _Document(name="LINK-1")
		artifacts = {
			"input_messages_hash": "a" * 64,
			"output_messages_hash": "b" * 64,
			"tool_trace_hash": "c" * 64,
			"attachment_text_hash": "d" * 64,
		}
		payloads: list[dict] = []

		def db_get_value(doctype, name_or_filters, fields, *, as_dict=False):
			del name_or_filters, fields, as_dict
			if doctype == "IONE Flow Run Link":
				return None
			if doctype == "Flow Session":
				return {"agent": agent.name, "model": None}
			if doctype == "Flow Run":
				return f"{orchestrator.SANITIZED_FLOW_ERROR}:TimeoutError"
			raise AssertionError(doctype)

		def get_doc(payload):
			payloads.append(payload)
			return link

		execution_fingerprint = "e" * 64
		with (
			patch.object(orchestrator.frappe.db, "get_value", side_effect=db_get_value),
			patch.object(
				orchestrator.frappe,
				"get_cached_doc",
				return_value=_Document(model_id="qwen-model"),
			),
			patch.object(orchestrator, "flow_run_artifact_hashes", return_value=artifacts),
			patch.object(orchestrator.frappe, "get_doc", side_effect=get_doc),
		):
			name = orchestrator._link_flow_run(
				task,
				policy,
				agent,
				run,
				attempt=2,
				token_hash=execution_fingerprint,
			)

		self.assertEqual(name, link.name)
		self.assertTrue(link.inserted_ignore_permissions)
		self.assertEqual(len(payloads), 1)
		payload = payloads[0]
		self.assertEqual(payload["execution_attempt"], 2)
		self.assertEqual(payload["execution_token_hash"], execution_fingerprint)
		self.assertEqual(payload["hospital"], task.hospital)
		self.assertEqual(payload["department"], task.department)
		for fieldname, expected in artifacts.items():
			self.assertEqual(payload[fieldname], expected)

	def test_orphan_reconciler_uses_claimed_attempt_not_newer_concurrent_attempt(self) -> None:
		task = _task(status="Running", execution_attempt=2)
		prompt_scope = {
			"task": task.name,
			"policy": task.policy,
			"execution_attempt": 1,
		}
		run = _Document(
			name="RUN-ATTEMPT-1",
			status="Failed",
			reference_doctype="IONE AI Analysis Task",
			reference_name=task.name,
			input=f"<task_scope>{json.dumps(prompt_scope)}</task_scope>",
			session="SESSION-1",
			creation=datetime(2026, 7, 30, 8, 1, 0),
		)
		policy = _Document(name=task.policy, flow_agent="AGENT-1")
		agent = _Document(name="AGENT-1", model="MODEL-1")
		execution_fingerprint = "f" * 64
		claim_event = {
			"policy": task.policy,
			"event_at": datetime(2026, 7, 30, 8, 0, 0),
			"execution_token_hash": execution_fingerprint,
			**{fieldname: task.get(fieldname) for fieldname in orchestrator.SCOPE_FIELDS},
		}
		order: list[str] = []

		def get_doc(doctype, name, *, for_update=False):
			del name, for_update
			return {
				"Flow Run": run,
				"IONE AI Analysis Task": task,
				"IONE Agent Policy": policy,
				"Flow Agent": agent,
			}[doctype]

		with (
			patch.object(orchestrator.frappe.db, "exists", return_value=False),
			patch.object(orchestrator.frappe, "get_doc", side_effect=get_doc),
			patch.object(orchestrator, "_task_advisory_lock", return_value=nullcontext()),
			patch.object(orchestrator.frappe, "get_all", return_value=[claim_event]),
			patch.object(
				orchestrator.frappe.db,
				"get_value",
				return_value={"agent": agent.name, "model": None},
			),
			patch.object(
				orchestrator,
				"_sanitize_failed_run",
				side_effect=lambda *args: order.append("sanitize"),
			),
			patch.object(
				orchestrator,
				"_link_flow_run",
				side_effect=lambda *args, **kwargs: order.append("link") or "LINK-1",
			) as link,
			patch.object(orchestrator.frappe.db, "commit"),
		):
			self.assertTrue(orchestrator._reconcile_one_flow_run(run.name))

		self.assertEqual(order, ["sanitize", "link"])
		self.assertEqual(link.call_args.kwargs["attempt"], 1)
		self.assertEqual(link.call_args.kwargs["token_hash"], execution_fingerprint)

	def test_unsafe_old_orphan_cannot_target_attempt_two_draft(self) -> None:
		task = _task(status="Running", execution_attempt=2)
		run = _Document(
			name="RUN-ATTEMPT-1",
			status="Failed",
			reference_doctype="IONE AI Analysis Task",
			reference_name=task.name,
			input=(
				"<task_scope>"
				f"{json.dumps({'task': task.name, 'policy': task.policy, 'execution_attempt': 1})}"
				"</task_scope>"
			),
			session="SESSION-1",
			creation=datetime(2026, 7, 30, 8, 1, 0),
		)
		policy = _Document(name=task.policy, flow_agent="AGENT-1")
		agent = _Document(name="AGENT-1", model="MODEL-1")
		claim_event = {
			"policy": task.policy,
			"event_at": datetime(2026, 7, 30, 8, 0, 0),
			"execution_token_hash": "f" * 64,
			**{fieldname: task.get(fieldname) for fieldname in orchestrator.SCOPE_FIELDS},
		}

		def get_doc(doctype, name, *, for_update=False):
			del name, for_update
			return {
				"Flow Run": run,
				"IONE AI Analysis Task": task,
				"IONE Agent Policy": policy,
				"Flow Agent": agent,
			}[doctype]

		with (
			patch.object(orchestrator.frappe.db, "exists", return_value=False),
			patch.object(orchestrator.frappe, "get_doc", side_effect=get_doc),
			patch.object(orchestrator, "_task_advisory_lock", return_value=nullcontext()),
			patch.object(orchestrator.frappe, "get_all", return_value=[claim_event]),
			patch.object(
				orchestrator.frappe.db,
				"get_value",
				return_value={"agent": agent.name, "model": None},
			),
			patch.object(
				orchestrator,
				"_aggregate_output_violation_or_fail_closed",
				return_value="DIRECT_MOBILE_NUMBER",
			),
			patch.object(orchestrator, "quarantine_aggregate_output_run", return_value=1) as quarantine,
			patch.object(
				orchestrator,
				"aggregate_output_quarantine_receipt",
				return_value=("DIRECT_MOBILE_NUMBER", 1),
			),
			patch.object(orchestrator, "_link_flow_run", return_value="LINK-1"),
			patch.object(orchestrator, "append_aggregate_output_incident"),
			patch.object(orchestrator.frappe.db, "commit"),
		):
			self.assertTrue(orchestrator._reconcile_one_flow_run(run.name))

		quarantine.assert_called_once_with(
			flow_run=run.name,
			task=task.name,
			execution_attempt=1,
			reason_code="DIRECT_MOBILE_NUMBER",
		)
		self.assertNotEqual(
			quarantine.call_args.kwargs["execution_attempt"],
			task.execution_attempt,
		)

	def test_superseded_token_quarantine_is_fenced_to_claim_attempt(self) -> None:
		execution_token = "x" * 43
		task = _task(status="Running", execution_attempt=1, execution_token=execution_token)
		claim = orchestrator.ExecutionClaim(
			task=task,
			attempt=1,
			token=execution_token,
			token_hash=orchestrator._hash_text(execution_token),
			claimed_at=datetime(2026, 7, 30, 8, 0, 0),
			lease_expires_at=datetime(2026, 7, 30, 8, 30, 0),
		)
		prompt = "attempt-one-prompt"
		run = _Document(
			name="RUN-ATTEMPT-1",
			status="Completed",
			reference_doctype="IONE AI Analysis Task",
			reference_name=task.name,
			input=prompt,
			creation=datetime(2026, 7, 30, 8, 1, 0),
		)
		fence = orchestrator.FlowRunFence(
			created_after=datetime(2026, 7, 30, 8, 0, 30),
			existing_runs=frozenset(),
			expected_input_hash=orchestrator._hash_text(prompt),
		)
		with (
			patch.object(orchestrator, "quarantine_aggregate_output_run", return_value=0) as quarantine,
			patch.object(orchestrator, "_finalize_attempt", return_value=False),
			patch.object(orchestrator, "_task_status", return_value="Running"),
			patch.object(orchestrator.frappe.db, "rollback"),
		):
			result = orchestrator._finalize_aggregate_output_safety_violation(
				claim,
				policy=_Document(name=task.policy),
				agent=_Document(name="AGENT-1"),
				run=run,
				fence=fence,
				reason_code="DIRECT_MOBILE_NUMBER",
			)

		self.assertTrue(result["superseded"])
		quarantine.assert_called_once_with(
			flow_run=run.name,
			task=task.name,
			execution_attempt=1,
			reason_code="DIRECT_MOBILE_NUMBER",
		)

	def test_current_terminal_orphan_finalizes_the_exact_live_claim(self) -> None:
		execution_token = "q" * 43
		execution_fingerprint = orchestrator._hash_text(execution_token)
		task = _task(
			status="Running",
			execution_attempt=1,
			execution_token=execution_token,
			claimed_at=datetime(2026, 7, 30, 8, 0, 0),
			lease_expires_at=datetime(2026, 7, 30, 8, 30, 0),
		)
		prompt_scope = {
			"task": task.name,
			"policy": task.policy,
			"execution_attempt": 1,
		}
		run = _Document(
			name="RUN-ATTEMPT-1",
			status="Completed",
			reference_doctype="IONE AI Analysis Task",
			reference_name=task.name,
			input=f"<task_scope>{json.dumps(prompt_scope)}</task_scope>",
			session="SESSION-1",
			creation=datetime(2026, 7, 30, 8, 1, 0),
		)
		policy = _Document(name=task.policy, flow_agent="AGENT-1")
		agent = _Document(name="AGENT-1", model="MODEL-1")
		claim_event = {
			"policy": task.policy,
			"event_at": task.claimed_at,
			"execution_token_hash": execution_fingerprint,
			**{fieldname: task.get(fieldname) for fieldname in orchestrator.SCOPE_FIELDS},
		}

		def get_doc(doctype, name, *, for_update=False):
			del name, for_update
			return {
				"Flow Run": run,
				"IONE AI Analysis Task": task,
				"IONE Agent Policy": policy,
				"Flow Agent": agent,
			}[doctype]

		with (
			patch.object(orchestrator.frappe.db, "exists", return_value=False),
			patch.object(orchestrator.frappe, "get_doc", side_effect=get_doc),
			patch.object(orchestrator, "_task_advisory_lock", return_value=nullcontext()),
			patch.object(orchestrator.frappe, "get_all", return_value=[claim_event]),
			patch.object(
				orchestrator.frappe.db,
				"get_value",
				return_value={"agent": agent.name, "model": None},
			),
			patch.object(orchestrator, "_finalize_attempt", return_value=True) as finalize,
			patch.object(orchestrator, "_link_flow_run") as link,
		):
			self.assertTrue(orchestrator._reconcile_one_flow_run(run.name))

		link.assert_not_called()
		recovered_claim = finalize.call_args.args[0]
		self.assertEqual(recovered_claim.attempt, 1)
		self.assertEqual(recovered_claim.token, execution_token)
		self.assertEqual(recovered_claim.token_hash, execution_fingerprint)
		self.assertEqual(finalize.call_args.kwargs["status"], "Completed")
		self.assertEqual(finalize.call_args.kwargs["detail_code"], "ReconciledCompleted")

	def test_non_terminal_returned_run_is_rejected(self) -> None:
		task = _task()
		claim = orchestrator.ExecutionClaim(
			task=task,
			attempt=1,
			token="t" * 43,
			token_hash="a" * 64,
			claimed_at=datetime(2026, 7, 30, 8, 0, 0),
			lease_expires_at=datetime(2026, 7, 30, 8, 30, 0),
		)
		prompt = "governed prompt"
		fence = orchestrator.FlowRunFence(
			created_after=datetime(2026, 7, 30, 8, 0, 1),
			existing_runs=frozenset(),
			expected_input_hash=orchestrator._hash_text(prompt),
		)
		run = _Document(
			name="RUN-RUNNING",
			status="Running",
			reference_doctype="IONE AI Analysis Task",
			reference_name=task.name,
			input=prompt,
			creation=datetime(2026, 7, 30, 8, 0, 2),
		)
		with (
			patch.object(orchestrator.frappe, "throw", side_effect=ValueError("not terminal")),
			self.assertRaisesRegex(ValueError, "not terminal"),
		):
			orchestrator._assert_returned_run_matches_claim(run, claim, fence)
