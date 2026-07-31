from __future__ import annotations

import ast
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]


class TestReleaseMigrationGateStatic(TestCase):
	def test_ci_asserts_verified_completion_after_worker(self) -> None:
		workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
		worker = "ione_qms.tasks.migrations.run_post_migrate_backfills"
		assertion = "ione_qms.services.migration_state.assert_migration_complete"
		self.assertIn("set -euo pipefail", workflow)
		self.assertIn(worker, workflow)
		self.assertIn(assertion, workflow)
		self.assertLess(workflow.index(worker), workflow.index(assertion))

	def test_fingerprint_inputs_are_explicit_and_runtime_derived(self) -> None:
		source = (ROOT / "ione_qms" / "services" / "migration_state.py").read_text(encoding="utf-8")
		self.assertIn('root.rglob("*.py")', source)
		self.assertIn('root.rglob("*.json")', source)
		self.assertIn('"doctype" in path.relative_to(root).parts', source)
		self.assertIn("MIGRATION_PLAN", source)
		self.assertIn("FULL_FINGERPRINT = compute_full_fingerprint()", source)
		self.assertIn('SCHEMA_REVISION = f"ione-qms-schema-{FULL_FINGERPRINT[:24]}"', source)
		self.assertIn('MIGRATION_KEY = f"ione-qms:{FULL_FINGERPRINT}"', source)

	def test_install_preflight_covers_workflows_and_shared_mutations(self) -> None:
		install = (ROOT / "ione_qms" / "setup" / "install.py").read_text(encoding="utf-8")
		workflows = (ROOT / "ione_qms" / "setup" / "workflows.py").read_text(encoding="utf-8")
		for call in (
			"validate_workflow_install_preflight()",
			"_validate_flow_log_settings()",
			"_validate_reserved_flow_triggers()",
		):
			self.assertIn(call, install)
		self.assertIn("for spec in WORKFLOW_SPECS", workflows)
		self.assertIn("will not claim or overwrite an unknown Workflow", workflows)
		self.assertIn("_validate_existing_workflow_state(state)", workflows)
		self.assertIn("_validate_existing_workflow_action(action)", workflows)
		self.assertNotIn('startswith("IONE ")', install)

	def test_every_non_migration_scheduler_target_has_a_receipt_gate(self) -> None:
		hooks_source = (ROOT / "ione_qms" / "hooks.py").read_text(encoding="utf-8")
		hooks_tree = ast.parse(hooks_source)
		assignment = next(
			node
			for node in hooks_tree.body
			if isinstance(node, ast.Assign)
			and any(
				isinstance(target, ast.Name) and target.id == "scheduler_events" for target in node.targets
			)
		)
		scheduler_events = ast.literal_eval(assignment.value)
		targets = {
			target
			for entries in scheduler_events["cron"].values()
			for target in entries
			if target != "ione_qms.tasks.migrations.run_post_migrate_backfills"
		}
		self.assertTrue(targets)
		for target in sorted(targets):
			with self.subTest(target=target):
				module_name, function_name = target.rsplit(".", 1)
				path = ROOT.joinpath(*module_name.split(".")).with_suffix(".py")
				tree = ast.parse(path.read_text(encoding="utf-8"))
				function = next(
					node
					for node in tree.body
					if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
					and node.name == function_name
				)
				calls = {
					node.func.id
					for node in ast.walk(function)
					if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
				}
				self.assertIn("require_post_migrate_runtime_ready", calls)
