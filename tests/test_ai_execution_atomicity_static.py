from __future__ import annotations

import ast
import unittest
from pathlib import Path

ORCHESTRATOR = Path(__file__).resolve().parents[1] / "ione_qms" / "ai" / "orchestrator.py"


class TestAIExecutionAtomicitySourceContract(unittest.TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		cls.source = ORCHESTRATOR.read_text(encoding="utf-8")
		cls.tree = ast.parse(cls.source)
		cls.functions = {
			node.name: ast.get_source_segment(cls.source, node) or ""
			for node in cls.tree.body
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
		}

	def test_claim_is_acquired_before_the_only_model_call(self) -> None:
		run_task = self.functions["run_analysis_task"]
		self.assertLess(run_task.index("_claim_analysis_task("), run_task.index("agent.run("))
		self.assertEqual(run_task.count("agent.run("), 1)

		claim = self.functions["_claim_analysis_task"]
		self.assertIn("for_update=True", claim)
		self.assertIn("assert_analysis_task_input_integrity(task)", claim)
		self.assertLess(claim.index("_append_execution_event("), claim.index("frappe.db.commit()"))

	def test_completion_and_lease_expiry_are_compare_and_swap_updates(self) -> None:
		completion = self.functions["_cas_attempt_outcome"]
		for predicate in (
			"status = 'Running'",
			"execution_attempt = %s",
			"execution_token = %s",
		):
			self.assertIn(predicate, completion)
		self.assertIn("_affected_rows()", completion)
		self.assertIn('getattr(frappe.db, "_cursor", None)', self.source)

		expiry = self.functions["_cas_expire_lease"]
		self.assertIn("lease_expires_at <= %s", expiry)
		self.assertIn("execution_token = %s", expiry)

	def test_failed_flow_rows_are_fenced_sanitized_and_linked(self) -> None:
		run_task = self.functions["run_analysis_task"]
		self.assertIn("_find_new_failed_flow_run(", run_task)
		self.assertIn("_sanitize_failed_run(", run_task)
		self.assertIn("_finalize_attempt(", run_task)

		discovery = self.functions["_find_new_failed_flow_run"]
		self.assertIn("reference_doctype", discovery)
		self.assertIn("reference_name", discovery)
		self.assertIn("fence.existing_runs", discovery)
		self.assertIn("fence.expected_input_hash", discovery)

	def test_recovery_and_orphan_reconciliation_are_bounded(self) -> None:
		recovery = self.functions["recover_stale_analysis_tasks"]
		self.assertIn("STALE_RECOVERY_LIMIT", recovery)
		self.assertIn('"Lease Expired"', recovery)
		self.assertIn("_enqueue_task_name(", recovery)
		self.assertIn("_enqueue_pending_tasks(", recovery)
		pending = self.functions["_enqueue_pending_tasks"]
		self.assertIn('"status": "Pending"', pending)
		self.assertIn("enqueue_after_commit=True", pending)
		self.assertIn("ai_runtime_enabled()", pending)

		reconciler = self.functions["reconcile_orphan_flow_runs"]
		self.assertIn("ORPHAN_RECONCILIATION_LIMIT", reconciler)
		self.assertIn("limit %s", reconciler)
		self.assertIn("link.name is null", reconciler)

		abandoned = self.functions["reconcile_abandoned_running_flow_runs"]
		self.assertIn("run.status = 'Running'", abandoned)
		self.assertIn("task.status in ('Completed', 'Failed'", abandoned)
		self.assertIn("is_job_enqueued(job_id)", abandoned)
		self.assertIn("AbandonedRunningRun", abandoned)


if __name__ == "__main__":
	unittest.main()
