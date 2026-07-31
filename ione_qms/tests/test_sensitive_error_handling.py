from __future__ import annotations

import ast
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]


class TestSensitiveErrorHandling(unittest.TestCase):
	def test_error_logs_are_never_created_inside_active_exception_handlers(self) -> None:
		violations: list[str] = []
		for path in APP_ROOT.rglob("*.py"):
			tree = ast.parse(path.read_text(encoding="utf-8"))
			for handler in (node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)):
				for node in ast.walk(handler):
					if (
						isinstance(node, ast.Call)
						and isinstance(node.func, ast.Attribute)
						and node.func.attr == "log_error"
					):
						violations.append(f"{path.relative_to(APP_ROOT)}:{node.lineno}")
		self.assertEqual(
			violations,
			[],
			"Active exception tracebacks can contain clinical payloads: " + ", ".join(violations),
		)

	def test_sensitive_runtime_paths_do_not_persist_exception_text(self) -> None:
		for relative_path in (
			"ai/approvals.py",
			"ai/orchestrator.py",
			"integration/service.py",
			"rule_engine/evaluator.py",
			"rule_engine/executor.py",
			"services/data_export.py",
		):
			source = (APP_ROOT / relative_path).read_text(encoding="utf-8")
			self.assertNotIn("str(exc)", source, relative_path)

	def test_ai_task_does_not_copy_flow_error_payload(self) -> None:
		orchestrator = (APP_ROOT / "ai/orchestrator.py").read_text(encoding="utf-8")
		approvals = (APP_ROOT / "ai/approvals.py").read_text(encoding="utf-8")
		self.assertNotIn("run.error", orchestrator)
		self.assertNotIn("resumed.error", approvals)
		self.assertIn("governed model execution failed", orchestrator)
		self.assertIn("orchestrator._finalize_attempt(", approvals)
		self.assertIn("failure_type = type(exc).__name__", approvals)


if __name__ == "__main__":
	unittest.main()
