from __future__ import annotations

import ast
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "ione_qms" / "ai" / "tools.py"
ORCHESTRATOR = ROOT / "ione_qms" / "ai" / "orchestrator.py"
QUARANTINE = ROOT / "ione_qms" / "ai" / "output_quarantine.py"
APPROVALS = ROOT / "ione_qms" / "ai" / "approvals.py"


def _function_source(path: Path, name: str) -> str:
	source = path.read_text(encoding="utf-8")
	tree = ast.parse(source)
	node = next(
		item
		for item in tree.body
		if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
	)
	return ast.get_source_segment(source, node) or ""


class TestAggregateOutputQuarantineStaticContract(TestCase):
	def test_draft_gate_signals_before_it_throws_or_persists(self) -> None:
		create = _function_source(TOOLS, "ione_create_report_draft")
		self.assertLess(
			create.index("flag_aggregate_output_violation("),
			create.index("frappe.throw(", create.index("output_violation")),
		)
		self.assertLess(
			create.index("aggregate_report_output_violation("),
			create.index('"doctype": "IONE AI Report Draft"'),
		)

	def test_orchestrator_contains_signal_before_normal_finalization(self) -> None:
		run = _function_source(ORCHESTRATOR, "run_analysis_task")
		self.assertIn("read_aggregate_output_violation(", run)
		self.assertIn("_aggregate_output_violation_or_fail_closed(", run)
		self.assertIn("_finalize_aggregate_output_safety_violation(", run)
		self.assertLess(
			run.index("_finalize_aggregate_output_safety_violation("),
			run.index("if failure_type is not None"),
		)
		contain = _function_source(ORCHESTRATOR, "_finalize_aggregate_output_safety_violation")
		self.assertIn("_assert_returned_run_matches_claim", contain)
		self.assertIn("quarantine_aggregate_output_run(", contain)
		self.assertIn("frappe.db.rollback()", contain)
		self.assertIn('status="Failed"', contain)
		reconcile = _function_source(ORCHESTRATOR, "_reconcile_one_flow_run")
		self.assertIn("_aggregate_output_violation_or_fail_closed(run, task)", reconcile)
		self.assertIn("execution_attempt=attempt", reconcile)
		self.assertIn("aggregate_output_quarantine_receipt(run)", reconcile)
		self.assertIn("output_safety_violation=reason_code", reconcile)
		resume = _function_source(APPROVALS, "resume_tool_approval_run")
		self.assertIn("_aggregate_output_violation_or_fail_closed(", resume)
		self.assertIn("quarantine_aggregate_output_run(", resume)
		self.assertIn("tool_call_id", _function_source(QUARANTINE, "aggregate_flow_run_output_violation"))

	def test_flow_trace_is_scrubbed_and_only_exact_attempt_draft_is_removed(self) -> None:
		quarantine = _function_source(QUARANTINE, "quarantine_aggregate_output_run")
		for token in (
			"reference_doctype",
			"reference_name",
			'"execution_attempt": attempt',
			'"flow_run": run_name',
			'str(row.get("status") or "") != "Draft"',
			"reviewed_by",
			"reviewed_at",
			"tabFlow Session Message",
			"tool_call_id = null",
			'"Flow Run"',
			"QUARANTINED_OUTPUT",
			"OUTPUT_SAFETY_ERROR_PREFIX",
			"frappe.db.commit()",
		):
			self.assertIn(token, quarantine)
		self.assertNotIn("report_drafts_before_attempt", quarantine)
		self.assertNotIn('"creation": ["<"', quarantine)
		self.assertNotIn("preexisting_drafts", quarantine)
		receipt = _function_source(QUARANTINE, "aggregate_output_quarantine_receipt")
		self.assertIn("tabFlow Session Message", receipt)
		self.assertIn("tool_call_id", receipt)
		self.assertIn("_QUARANTINE_RECEIPT.fullmatch", receipt)

	def test_incident_is_inside_the_attempt_savepoint_before_commit(self) -> None:
		finalize = _function_source(ORCHESTRATOR, "_finalize_attempt")
		self.assertIn("append_aggregate_output_incident(", finalize)
		self.assertLess(
			finalize.index("append_aggregate_output_incident("),
			finalize.index("frappe.db.commit()"),
		)
		incident = _function_source(QUARANTINE, "append_aggregate_output_incident")
		self.assertIn('"incident_type": "Patient Data Exposure"', incident)
		self.assertIn('"status": "Contained"', incident)
		self.assertIn('"record_hash"', incident)
		self.assertIn("hmac.compare_digest", incident)

	def test_every_report_draft_is_bound_to_attempt_and_flow_run(self) -> None:
		for function_name in ("ione_create_analysis_draft", "ione_create_report_draft"):
			with self.subTest(function=function_name):
				create = _function_source(TOOLS, function_name)
				self.assertIn('"execution_attempt": execution_attempt', create)
				self.assertIn('"flow_run": flow_run', create)
		provenance = _function_source(TOOLS, "_report_draft_execution_provenance")
		self.assertIn('"execution_attempt"', provenance)
		self.assertIn('"status": "Running"', provenance)
		self.assertIn("_prompt_task_scope", provenance)
