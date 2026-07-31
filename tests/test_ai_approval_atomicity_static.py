from __future__ import annotations

import ast
import unittest
from pathlib import Path

APPROVALS = Path(__file__).resolve().parents[1] / "ione_qms" / "ai" / "approvals.py"
ORCHESTRATOR = Path(__file__).resolve().parents[1] / "ione_qms" / "ai" / "orchestrator.py"


def function_source(path: Path, name: str) -> str:
	source = path.read_text(encoding="utf-8")
	tree = ast.parse(source)
	for node in tree.body:
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
			return ast.get_source_segment(source, node) or ""
	raise AssertionError(f"{name} was not found in {path}")


class TestAIApprovalAtomicitySourceContract(unittest.TestCase):
	def test_resume_claim_is_committed_before_flow_resume(self) -> None:
		source = function_source(APPROVALS, "resume_tool_approval_run")
		self.assertLess(source.index("claim_resume_attempt("), source.index(".resume(answers"))
		claim = function_source(ORCHESTRATOR, "claim_resume_attempt")
		self.assertIn("for_update=True", claim)
		self.assertLess(claim.index("_append_execution_event("), claim.index("frappe.db.commit()"))

	def test_approval_consumption_and_task_revision_share_final_commit(self) -> None:
		source = function_source(APPROVALS, "resume_tool_approval_run")
		for marker in (
			'_consume_approvals(current, "Failed")',
			"_consume_approvals(current, resumed.status)",
		):
			position = source.index(marker)
			self.assertLess(position, source.index("orchestrator._finalize_attempt(", position))

	def test_superseded_model_results_are_rolled_back(self) -> None:
		approval_source = function_source(APPROVALS, "resume_tool_approval_run")
		orchestrator_source = function_source(ORCHESTRATOR, "run_analysis_task")
		self.assertGreaterEqual(approval_source.count("if not finalized:"), 2)
		self.assertGreaterEqual(approval_source.count("frappe.db.rollback()"), 2)
		self.assertGreaterEqual(orchestrator_source.count("if not finalized:"), 3)
		self.assertGreaterEqual(orchestrator_source.count("frappe.db.rollback()"), 3)

	def test_resume_lease_does_not_retry_approved_tools(self) -> None:
		recovery = function_source(ORCHESTRATOR, "recover_stale_analysis_tasks")
		self.assertIn('execution_phase == "Resume"', recovery)
		self.assertIn('"ResumeLeaseExpired"', recovery)
		renewal = function_source(ORCHESTRATOR, "_renew_live_attempt_lease")
		self.assertIn("is_job_enqueued(job_id)", renewal)
		self.assertIn("execution_token = %s", renewal)

	def test_resume_uses_the_same_governed_ai_queue_as_initial_execution(self) -> None:
		source = function_source(APPROVALS, "_enqueue_resume_if_ready")
		self.assertIn("from ione_qms.ai import orchestrator", source)
		self.assertIn("queue=orchestrator.get_ai_queue()", source)

	def test_reconciliation_error_cannot_rollback_prior_expiry_transitions(self) -> None:
		expiry = function_source(APPROVALS, "expire_tool_approvals")
		reconciliation = function_source(APPROVALS, "reconcile_reviewed_tool_approvals")
		self.assertLess(
			expiry.index("frappe.db.commit()"), expiry.index("reconcile_reviewed_tool_approvals(")
		)
		self.assertIn("frappe.db.savepoint(savepoint)", reconciliation)
		self.assertIn("frappe.db.rollback(save_point=savepoint)", reconciliation)
		self.assertNotIn("frappe.db.rollback()", reconciliation)


if __name__ == "__main__":
	unittest.main()
