from __future__ import annotations

import ast
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
VERSIONS_PATH = ROOT / "ione_qms" / "services" / "versions.py"
API_PATH = ROOT / "ione_qms" / "api" / "rules.py"


def _assignment(tree: ast.Module, name: str):
	for node in tree.body:
		if isinstance(node, ast.Assign) and any(
			isinstance(target, ast.Name) and target.id == name for target in node.targets
		):
			value = node.value
			if (
				isinstance(value, ast.Call)
				and isinstance(value.func, ast.Name)
				and value.func.id == "frozenset"
			):
				return frozenset(ast.literal_eval(value.args[0]))
			return ast.literal_eval(value)
		if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
			return ast.literal_eval(node.value)
	raise AssertionError(f"Missing assignment: {name}")


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
	return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)


class TestStandardVersionDifferenceStaticContract(TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		cls.versions_source = VERSIONS_PATH.read_text(encoding="utf-8")
		cls.versions_tree = ast.parse(cls.versions_source)
		cls.api_source = API_PATH.read_text(encoding="utf-8")
		cls.api_tree = ast.parse(cls.api_source)

	def test_endpoint_is_read_only_and_delegates_to_governed_service(self) -> None:
		endpoint = _function(self.api_tree, "get_standard_version_difference")
		decorator = endpoint.decorator_list[0]
		self.assertIsInstance(decorator, ast.Call)
		self.assertEqual(ast.unparse(decorator.func), "frappe.whitelist")
		methods = next(
			ast.literal_eval(keyword.value) for keyword in decorator.keywords if keyword.arg == "methods"
		)
		self.assertEqual(methods, ["GET"])
		endpoint_source = ast.get_source_segment(self.api_source, endpoint) or ""
		self.assertIn("return compare_standard_versions(", endpoint_source)
		for parameter in (
			"change_start",
			"change_page_length",
			"rule_start",
			"rule_page_length",
			"relation_start",
			"relation_page_length",
			"expected_comparison_checksum",
		):
			self.assertIn(parameter, endpoint_source)

	def test_named_role_allowlist_and_explicit_service_account_denials_are_closed(self) -> None:
		self.assertEqual(
			_assignment(self.versions_tree, "STANDARD_VERSION_DIFF_ROLES"),
			frozenset(
				{
					"IONE QC Administrator",
					"IONE QC Reviewer",
					"IONE Medical Affairs",
					"IONE QMS Auditor",
				}
			),
		)
		guard = _function(self.versions_tree, "require_standard_version_diff_access")
		guard_source = ast.get_source_segment(self.versions_source, guard) or ""
		self.assertIn('{"Guest", "Administrator"}', guard_source)
		self.assertIn('"IONE Agent Service" in roles', guard_source)
		self.assertIn("roles.intersection(STANDARD_VERSION_DIFF_ROLES)", guard_source)
		self.assertIn('frappe.has_permission(doctype, "read", user=user)', guard_source)
		self.assertIn("frappe.PermissionError", guard_source)

	def test_all_queries_are_static_and_values_are_parameterized(self) -> None:
		for function_name in (
			"_load_standard_diff_versions",
			"_load_standard_diff_clauses",
			"_load_affected_standard_rule_relations",
		):
			function = _function(self.versions_tree, function_name)
			sql_calls = [
				node
				for node in ast.walk(function)
				if isinstance(node, ast.Call) and ast.unparse(node.func) == "frappe.db.sql"
			]
			self.assertTrue(sql_calls, function_name)
			for call in sql_calls:
				self.assertIsInstance(call.args[0], ast.Constant)
				self.assertIsInstance(call.args[1], ast.Dict)
				self.assertTrue(
					any(keyword.arg == "as_dict" for keyword in call.keywords),
					function_name,
				)

	def test_comparison_has_hard_count_page_and_text_limits(self) -> None:
		limits = {
			"STANDARD_DIFF_MAX_CLAUSES_PER_VERSION": 5_000,
			"STANDARD_DIFF_MAX_RULE_RELATIONS": 5_000,
			"STANDARD_DIFF_MAX_AFFECTED_RULES": 2_500,
			"STANDARD_DIFF_MAX_PAGE_LENGTH": 25,
			"STANDARD_DIFF_MAX_PAGE_START": 10_000,
			"STANDARD_DIFF_TEXT_PREVIEW_CHARS": 400,
		}
		for name, expected in limits.items():
			self.assertEqual(_assignment(self.versions_tree, name), expected)
		compare_source = (
			ast.get_source_segment(
				self.versions_source,
				_function(self.versions_tree, "compare_standard_versions"),
			)
			or ""
		)
		self.assertIn("_bounded_standard_diff_integer(", compare_source)
		self.assertIn("_verify_expected_standard_diff_checksum(", compare_source)
		self.assertIn('"clinical_data_read": False', compare_source)
		self.assertIn('"comparison_checksum": comparison_checksum', compare_source)

	def test_clause_identity_and_rule_impact_never_use_text_similarity(self) -> None:
		compare = _function(self.versions_tree, "compare_standard_versions")
		docstring = ast.get_docstring(compare) or ""
		self.assertIn("clause_code", docstring)
		self.assertIn("never infers lineage", docstring)
		change_source = (
			ast.get_source_segment(
				self.versions_source,
				_function(self.versions_tree, "_build_standard_clause_changes"),
			)
			or ""
		)
		self.assertIn("for clause_code in sorted(set(baseline).union(target))", change_source)
		self.assertNotIn("SequenceMatcher", self.versions_source)
		self.assertNotIn("levenshtein", self.versions_source.lower())

	def test_comparison_reads_definition_tables_only(self) -> None:
		start = self.versions_source.index("def compare_standard_versions(")
		end = self.versions_source.index("def backfill_active_version_keys(", start)
		source = self.versions_source[start:end]
		for table in (
			"tabIONE QC Standard Version",
			"tabIONE QC Standard Clause",
			"tabIONE QC Rule",
			"tabIONE QC Rule Version",
		):
			self.assertIn(table, source)
		for forbidden in (
			"tabIONE Patient",
			"tabIONE Encounter",
			"tabIONE Clinical Event",
			"tabIONE QC Finding",
			"tabIONE Integration Message",
			"payload_json",
		):
			self.assertNotIn(forbidden, source)
