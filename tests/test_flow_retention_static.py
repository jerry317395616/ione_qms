from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECURITY = ROOT / "ione_qms" / "tasks" / "security.py"
INSTALL = ROOT / "ione_qms" / "setup" / "install.py"
HOOKS = ROOT / "ione_qms" / "hooks.py"
FLOW_SESSION_OVERRIDE = ROOT / "ione_qms" / "overrides" / "flow_session.py"


def source_function(path: Path, name: str) -> str:
	source = path.read_text(encoding="utf-8")
	for node in ast.parse(source).body:
		if isinstance(node, ast.FunctionDef) and node.name == name:
			return ast.get_source_segment(source, node) or ""
	raise AssertionError(name)


class TestFlowRetentionSourceContract(unittest.TestCase):
	def test_empty_or_null_output_is_still_selected_for_transcript_redaction(self) -> None:
		source = source_function(SECURITY, "_aged_unredacted_flow_runs")
		self.assertIn("coalesce(", source)
		self.assertNotIn('["not in", ["", _REDACTED]]', source)

	def test_flow_log_deletion_has_a_post_redaction_safety_window(self) -> None:
		guard = source_function(INSTALL, "ensure_flow_log_retention")
		self.assertIn('"input_retention_days"', guard)
		self.assertIn('"output_retention_days"', guard)
		self.assertIn('"Flow Session"', guard)
		self.assertIn("safety_window_days", guard)
		for hook in ("after_install", "after_migrate"):
			self.assertIn("ensure_flow_log_retention()", source_function(INSTALL, hook))

	def test_generic_flow_cleanup_is_extended_with_a_governed_deletion_hold(self) -> None:
		hooks = HOOKS.read_text(encoding="utf-8")
		self.assertIn('"Flow Session"', hooks)
		self.assertIn("IONEGovernedFlowSessionMixin", hooks)
		cleanup = FLOW_SESSION_OVERRIDE.read_text(encoding="utf-8")
		self.assertIn("governed_flow_session_retention_verified", cleanup)
		self.assertIn("_session_is_governed", cleanup)


if __name__ == "__main__":
	unittest.main()
