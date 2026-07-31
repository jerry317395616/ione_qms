from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECURITY = ROOT / "ione_qms" / "tasks" / "security.py"
EVIDENCE = ROOT / "ione_qms" / "ai" / "evidence.py"
APPROVAL_RUNTIME = ROOT / "ione_qms" / "ai" / "approvals.py"
APPROVAL_GUARD = ROOT / "ione_qms" / "services" / "ai_approvals.py"
FLOW_ARTIFACTS = ROOT / "ione_qms" / "ai" / "flow_artifacts.py"
APPROVAL_SCHEMA = (
	ROOT / "ione_qms" / "ione_flow_ai" / "doctype" / "ione_ai_tool_approval" / "ione_ai_tool_approval.json"
)


def source_function(path: Path, name: str) -> str:
	source = path.read_text(encoding="utf-8")
	for node in ast.parse(source).body:
		if isinstance(node, ast.FunctionDef) and node.name == name:
			return ast.get_source_segment(source, node) or ""
	raise AssertionError(name)


class TestAIRetentionSourceContract(unittest.TestCase):
	def test_access_snapshot_retains_hash_receipt_after_verified_redaction(self) -> None:
		redact = source_function(SECURITY, "_redact_access_log_snapshot")
		for required in (
			"content_hash",
			"canonical_record_hash",
			"EVIDENCE_SNAPSHOT_REDACTED",
			"for_update=True",
			"update_modified=False",
		):
			self.assertIn(required, redact)
		validate = source_function(EVIDENCE, "_validate_receipt")
		self.assertIn("EVIDENCE_SNAPSHOT_REDACTED", validate)
		self.assertIn("hashlib.sha256(snapshot_json.encode())", validate)

	def test_approval_prompt_has_an_independent_hash_without_legacy_mandatory_breakage(self) -> None:
		schema = json.loads(APPROVAL_SCHEMA.read_text(encoding="utf-8"))
		prompt_hash = next(
			field for field in schema["fields"] if field["fieldname"] == "question_prompt_hash"
		)
		self.assertEqual(prompt_hash.get("read_only"), 1)
		self.assertEqual(prompt_hash.get("length"), 64)
		self.assertFalse(prompt_hash.get("reqd"))
		create = source_function(APPROVAL_RUNTIME, "create_tool_approvals")
		self.assertIn('"question_prompt_hash"', create)
		self.assertIn("hashlib.sha256(question_prompt.encode())", create)
		selection = source_function(SECURITY, "_aged_terminal_approvals")
		self.assertIn("length(coalesce(approval.question_prompt_hash, '')) = 64", selection)

	def test_approval_redaction_is_terminal_hash_checked_and_capability_scoped(self) -> None:
		validate = source_function(APPROVAL_GUARD, "validate_ai_tool_approval")
		transition = source_function(APPROVAL_GUARD, "_validate_retention_transition")
		self.assertIn("ione_ai_approval_retention", validate)
		self.assertIn("_validate_proposed_content_hashes(doc)", validate)
		for required in (
			'{"Consumed", "Expired"}',
			"_FINAL_TASK_STATUSES",
			"hashlib.sha256(original.encode())",
			"APPROVAL_CONTENT_REDACTED",
		):
			self.assertIn(required, transition)
		self.assertIn("partial or unauthorized retention marker", transition)

	def test_governed_flow_attachments_fail_closed_without_partial_file_cleanup(self) -> None:
		hashes = source_function(FLOW_ARTIFACTS, "flow_run_artifact_hashes")
		self.assertIn("flow_run_attachments(flow_run)", hashes)
		self.assertIn("if attachments:", hashes)
		self.assertIn("frappe.throw", hashes)
		security_source = SECURITY.read_text(encoding="utf-8")
		self.assertNotIn("attachment_store.delete", security_source)

	def test_flow_redaction_waits_for_terminal_governed_task(self) -> None:
		redact = source_function(SECURITY, "_redact_flow_runs")
		self.assertIn('if not _task_is_final(run.get("reference_name"))', redact)


if __name__ == "__main__":
	unittest.main()
