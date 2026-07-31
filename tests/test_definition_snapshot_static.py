from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
VERSIONS_PATH = ROOT / "ione_qms" / "services" / "versions.py"


def _schema(*parts: str) -> dict:
	return json.loads((ROOT / "ione_qms").joinpath(*parts).read_text(encoding="utf-8"))


class TestDefinitionSnapshotSourceContract(TestCase):
	def test_all_governed_lineage_links_are_permanently_locked(self) -> None:
		expected = {
			"IONE QC Standard Version": {"standard": "IONE QC Standard"},
			"IONE QC Rule Version": {"rule": "IONE QC Rule"},
			"IONE QC Indicator Version": {"indicator": "IONE QC Indicator"},
			"IONE Agent Release": {
				"policy": "IONE Agent Policy",
				"flow_agent": "Flow Agent",
				"flow_model": "Flow Model",
			},
		}
		source = VERSIONS_PATH.read_text(encoding="utf-8")
		tree = ast.parse(source)
		mapping = next(
			ast.literal_eval(node.value)
			for node in tree.body
			if isinstance(node, ast.AnnAssign)
			and isinstance(node.target, ast.Name)
			and node.target.id == "GOVERNED_LINEAGE_LINKS"
		)
		self.assertEqual(mapping, expected)

		for function_name in (
			"validate_standard_version",
			"validate_rule_version",
			"validate_indicator_version",
			"validate_agent_release",
		):
			function = next(
				node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function_name
			)
			self.assertTrue(
				any(
					isinstance(node, ast.Call)
					and isinstance(node.func, ast.Name)
					and node.func.id == "_lock_and_validate_lineage_links"
					for node in ast.walk(function)
				),
				f"{function_name} must enforce immutable lineage",
			)

		guard = next(
			node
			for node in tree.body
			if isinstance(node, ast.FunctionDef) and node.name == "_lock_and_validate_lineage_links"
		)
		guard_source = ast.get_source_segment(source, guard) or ""
		self.assertIn("for source in (previous, doc)", guard_source)
		self.assertIn("sorted(lock_targets)", guard_source)
		self.assertIn("lineage links are immutable after insert", guard_source)

	def test_governed_lineage_links_are_read_only_after_insert_in_the_desk(self) -> None:
		schemas = {
			(
				"ione_quality_standards",
				"doctype",
				"ione_qc_standard_version",
				"ione_qc_standard_version.json",
			): ("standard",),
			(
				"ione_quality_standards",
				"doctype",
				"ione_qc_rule_version",
				"ione_qc_rule_version.json",
			): ("rule",),
			(
				"ione_indicators",
				"doctype",
				"ione_qc_indicator_version",
				"ione_qc_indicator_version.json",
			): ("indicator",),
			(
				"ione_flow_ai",
				"doctype",
				"ione_agent_release",
				"ione_agent_release.json",
			): ("policy", "flow_agent", "flow_model"),
		}
		for path, fieldnames in schemas.items():
			with self.subTest(schema=path[-1]):
				fields = {field["fieldname"]: field for field in _schema(*path)["fields"]}
				for fieldname in fieldnames:
					self.assertEqual(
						fields[fieldname].get("read_only_depends_on"),
						"eval:!doc.__islocal",
					)

	def test_rule_version_snapshot_schema_is_generated_and_governed(self) -> None:
		payload = _schema(
			"ione_quality_standards",
			"doctype",
			"ione_qc_rule_version",
			"ione_qc_rule_version.json",
		)
		fields = {field["fieldname"]: field for field in payload["fields"]}
		required = {
			"rule_code_snapshot",
			"rule_name_snapshot",
			"rule_type_snapshot",
			"risk_level_snapshot",
		}
		for fieldname in required:
			self.assertEqual(fields[fieldname].get("reqd"), 1)
		self.assertEqual(fields["rule_code_snapshot"].get("read_only"), 1)
		for fieldname in {
			"rule_name_snapshot",
			"rule_type_snapshot",
			"standard_snapshot",
			"standard_clause_snapshot",
			"category_snapshot",
			"risk_level_snapshot",
			"owner_department_snapshot",
			"description_snapshot",
		}:
			self.assertEqual(
				fields[fieldname].get("read_only_depends_on"),
				'eval:doc.status!="Draft"',
			)

	def test_runtime_does_not_read_mutable_rule_parent_semantics(self) -> None:
		source = (ROOT / "ione_qms" / "rule_engine" / "executor.py").read_text(encoding="utf-8")
		self.assertNotIn('get_cached_doc("IONE QC Rule"', source)
		self.assertNotIn('get_value("IONE QC Rule"', source)
		self.assertNotIn("inner join `tabIONE QC Rule`", source)
		for fieldname in (
			"rule_type_snapshot",
			"rule_code_snapshot",
			"rule_name_snapshot",
			"standard_clause_snapshot",
			"risk_level_snapshot",
		):
			self.assertIn(fieldname, source)

	def test_rule_and_clause_governance_hooks_are_declared(self) -> None:
		tree = ast.parse((ROOT / "ione_qms" / "hooks.py").read_text(encoding="utf-8"))
		doc_events = next(
			ast.literal_eval(node.value)
			for node in tree.body
			if isinstance(node, (ast.Assign, ast.AnnAssign))
			and (
				(
					isinstance(node, ast.Assign)
					and any(
						isinstance(target, ast.Name) and target.id == "doc_events" for target in node.targets
					)
				)
				or (
					isinstance(node, ast.AnnAssign)
					and isinstance(node.target, ast.Name)
					and node.target.id == "doc_events"
				)
			)
		)
		self.assertEqual(
			doc_events["IONE QC Rule"]["validate"],
			"ione_qms.services.versions.validate_rule",
		)
		self.assertEqual(
			doc_events["IONE QC Standard Clause"]["validate"],
			"ione_qms.services.versions.validate_standard_clause",
		)

	def test_standard_clause_status_is_parent_managed(self) -> None:
		payload = _schema(
			"ione_quality_standards",
			"doctype",
			"ione_qc_standard_clause",
			"ione_qc_standard_clause.json",
		)
		status = next(field for field in payload["fields"] if field["fieldname"] == "status")
		self.assertEqual(status.get("read_only"), 1)
		source = (ROOT / "ione_qms" / "services" / "versions.py").read_text(encoding="utf-8")
		self.assertIn('content["standard_clauses"] = _standard_clause_hashes(doc.name)', source)
		self.assertIn('"content_hash": hashlib.sha256(encoded).hexdigest()', source)
