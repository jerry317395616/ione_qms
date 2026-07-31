from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BATCH = ROOT / "ione_qms" / "services" / "batch_reliability.py"
HOOKS = ROOT / "ione_qms" / "hooks.py"
INDICATORS = ROOT / "ione_qms" / "tasks" / "indicators.py"
ANALYTICS = ROOT / "ione_qms" / "tasks" / "analytics.py"
GENERATOR = ROOT / "tools" / "generate_doctypes.py"


def function_source(path: Path, name: str) -> str:
	source = path.read_text(encoding="utf-8")
	tree = ast.parse(source)
	for node in tree.body:
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
			return ast.get_source_segment(source, node) or ""
	raise AssertionError(f"{name} was not found in {path}")


class TestDurableBatchSourceContract(unittest.TestCase):
	def test_all_durable_batch_records_are_generated_and_immutable(self) -> None:
		source = GENERATOR.read_text(encoding="utf-8")
		for doctype in (
			"IONE Batch Run",
			"IONE Batch Work Item",
			"IONE Batch Recovery Receipt",
			"IONE Batch Source Epoch",
		):
			self.assertIn(f'"{doctype}": schema(', source)
		self.assertIn(f'"{doctype}"', source[source.index("SENSITIVE_EXPORT_DOCTYPES") :])
		self.assertIn('autoname="field:run_key"', source)
		self.assertIn('autoname="field:item_key"', source)
		self.assertIn('autoname="field:receipt_key"', source)
		self.assertIn('autoname="field:source_doctype"', source)

	def test_source_write_barrier_runs_before_mutation_without_global_hook(self) -> None:
		hooks = HOOKS.read_text(encoding="utf-8")
		self.assertNotIn('"*": {', hooks)
		self.assertIn('for _epoch_event in ("before_save", "on_trash")', hooks)
		self.assertIn("bump_indicator_source_epoch", hooks)
		barrier = function_source(BATCH, "bump_indicator_source_epoch")
		self.assertIn("advance_source_epoch", barrier)
		advance = function_source(BATCH, "advance_source_epoch")
		self.assertIn("for update", advance.lower())
		self.assertIn("source_epoch", advance)
		lock = function_source(BATCH, "lock_source_epochs")
		self.assertIn("order", lock.lower())
		self.assertIn("for update", lock.lower())
		complete = function_source(BATCH, "complete_run")
		self.assertIn('advance_source_epoch("IONE Indicator Result")', complete)

	def test_retry_recovery_uses_real_frappe_timeout_and_append_only_receipt(self) -> None:
		source = BATCH.read_text(encoding="utf-8")
		self.assertIn("from frappe.exceptions import QueryTimeoutError", source)
		self.assertNotIn("frappe.LockTimeoutError", source)
		recover_due = function_source(BATCH, "recover_due_batch_runs")
		self.assertIn("except QueryTimeoutError", recover_due)
		recover = function_source(BATCH, "recover_dead_letter")
		self.assertIn(
			'@frappe.whitelist(methods=["POST"])\ndef recover_dead_letter',
			source,
		)
		self.assertIn('"Guest", "Administrator"', recover)
		self.assertIn('"doctype": RECOVERY_DOCTYPE', recover)
		self.assertIn("manual_recovery_count", recover)
		self.assertNotIn('"recovery_generation": recovery_sequence', recover)

	def test_indicator_run_has_three_pass_snapshot_and_multi_period_reconciliation(self) -> None:
		source = INDICATORS.read_text(encoding="utf-8")
		self.assertIn('"Materializing"', source)
		self.assertIn('"Verifying"', source)
		self.assertIn('"Final Verifying"', source)
		self.assertIn("lock_source_epochs(", function_source(INDICATORS, "_verify_dimension_page"))
		reconcile = function_source(INDICATORS, "reconcile_indicator_backlog")
		self.assertIn("_due_periods(", reconcile)
		self.assertIn("current_period_cursor", reconcile)
		due_periods = function_source(INDICATORS, "_due_periods")
		self.assertIn("lookback_days", due_periods)
		self.assertIn("for offset in range", due_periods)
		work = function_source(INDICATORS, "_execute_indicator_work_page")
		self.assertIn("savepoint: str | None = None", work)
		self.assertIn("if savepoint:", work)

	def test_analytics_consumes_only_latest_completed_indicator_batch(self) -> None:
		source = ANALYTICS.read_text(encoding="utf-8")
		self.assertIn("batch.status = 'Completed'", source)
		self.assertIn("newer.recovery_generation > batch.recovery_generation", source)
		process = function_source(ANALYTICS, "process_analytics_batch_run")
		self.assertIn("lock_source_epochs", process)
		self.assertIn("_supersede_analytics_run", process)
		cleanup = function_source(ANALYTICS, "_clean_analytics_fact_page")
		self.assertIn("_analytics_fact_cleanup_rows", cleanup)
		self.assertIn("frappe.delete_doc", cleanup)
		self.assertIn("batches.complete_run", cleanup)
		supersede = function_source(ANALYTICS, "_supersede_analytics_run")
		self.assertIn("ANALYTICS_SOURCE_DRIFT", supersede)


if __name__ == "__main__":
	unittest.main()
