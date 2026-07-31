from __future__ import annotations

import ast
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
TOOLS_PATH = ROOT / "ione_qms" / "ai" / "tools.py"
REGISTRY_PATH = ROOT / "ione_qms" / "ai" / "tool_registry.py"
INSTALL_PATH = ROOT / "ione_qms" / "setup" / "install.py"


def _function_source(source: str, function_name: str) -> str:
	tree = ast.parse(source)
	function = next(
		node
		for node in tree.body
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
	)
	return ast.get_source_segment(source, function) or ""


class TestRectificationAgentToolStaticContract(TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		cls.tools = TOOLS_PATH.read_text(encoding="utf-8")
		cls.registry = REGISTRY_PATH.read_text(encoding="utf-8")
		cls.install = INSTALL_PATH.read_text(encoding="utf-8")

	def test_tool_is_exact_finding_bound_and_revalidates_human_authority(self) -> None:
		validator = _function_source(self.tools, "_validated_rectification_finding")
		for required in (
			'"Rectification"',
			'"finding"',
			'"record_count"',
			'"requires_human_review"',
			'"requested_by"',
			'"User"',
			'"enabled"',
			"RECTIFICATION_REQUESTER_ROLES",
			"require_scope_read",
			"_require_requester_read",
			"_assert_scope_exact",
			"RECTIFICATION_ELIGIBLE_FINDING_STATUSES",
		):
			with self.subTest(required=required):
				self.assertIn(required, validator)

	def test_tool_is_bounded_structured_and_receipts_every_returned_source(self) -> None:
		tool = _function_source(self.tools, "ione_get_rectification_context")
		receipt = _function_source(self.tools, "_receipted_rectification_snapshot")
		for required in (
			"MAX_RECTIFICATION_EVIDENCE_ROWS",
			"MAX_RECTIFICATION_ROWS",
			"MAX_VERIFICATION_ROWS",
			"MAX_PDCA_ROWS",
			"MAX_RECTIFICATION_CONTEXT_BYTES",
			"_bounded_related_documents",
			"_receipted_rectification_snapshot",
			'"patient_identifiers_returned": False',
			'"clinical_free_text_returned": False',
			'"draft_only": True',
		):
			with self.subTest(required=required):
				self.assertIn(required, tool)
		self.assertIn("record_tool_access", receipt)
		self.assertIn('tool_slug="ione_get_rectification_context"', receipt)
		self.assertIn("source_doctype=source_doc.doctype", receipt)
		self.assertIn("source_name=source_doc.name", receipt)
		self.assertIn("snapshot=snapshot", receipt)

	def test_no_clinical_narrative_or_patient_identity_fields_are_returned(self) -> None:
		builders = "\n".join(
			_function_source(self.tools, name)
			for name in (
				"_build_confirmed_finding_snapshot",
				"_build_finding_evidence_snapshot",
				"_build_rectification_snapshot",
				"_build_verification_snapshot",
				"_build_pdca_snapshot",
			)
		)
		for forbidden in (
			'"title"',
			'"description"',
			'"patient"',
			'"encounter"',
			'"evidence_text"',
			'"evidence_json"',
			'"actual_json"',
			'"expected_json"',
			'"plan"',
			'"verification_comment"',
			'"cause"',
			'"measure"',
			'"effectiveness"',
		):
			with self.subTest(forbidden=forbidden):
				self.assertNotIn(forbidden, builders)

	def test_history_fails_closed_until_an_approved_deidentified_model_exists(self) -> None:
		tool = _function_source(self.tools, "ione_get_rectification_context")
		self.assertIn('"available": False', tool)
		self.assertIn('"NO_APPROVED_DEIDENTIFIED_RECTIFICATION_CASE_MODEL"', tool)
		self.assertIn('"records": []', tool)

	def test_registry_and_default_rectification_policy_are_narrowly_wired(self) -> None:
		registry_start = self.registry.index('"ione_get_rectification_context"')
		registry_end = self.registry.index("\n\t),", registry_start)
		registry_spec = self.registry[registry_start:registry_end]
		self.assertIn('"ione_qms.ai.tools.ione_get_rectification_context"', registry_spec)
		self.assertIn("\n\t\tFalse,", registry_spec)

		blueprint_start = self.install.index('"title": "IONE Rectification Agent"')
		blueprint_end = self.install.index('\n\t{\n\t\t"title": "IONE Quality Report Agent"', blueprint_start)
		blueprint = self.install[blueprint_start:blueprint_end]
		self.assertIn('"ione_get_rectification_context"', blueprint)
		self.assertIn('"ione_create_analysis_draft"', blueprint)
		self.assertIn('"ione_search_quality_standard"', blueprint)
		self.assertNotIn('"ione_get_encounter_context"', blueprint)
		self.assertNotIn('"ione_create_candidate_finding"', blueprint)
		self.assertIn("不得推断或编造历史案例", blueprint)
