from __future__ import annotations

import ast
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from ione_qms.overrides.native_export_guard import (
	MAX_EXPORT_TARGET_DEPTH,
	MAX_EXPORT_TARGET_NODES,
	MAX_EXPORT_TARGET_STRING_BYTES,
	ExportTargetInspectionError,
	collect_export_target_strings,
)

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "tools" / "generate_doctypes.py"
RUNTIME_EXPORT = ROOT / "ione_qms" / "services" / "data_export.py"
OVERRIDE = ROOT / "ione_qms" / "overrides" / "data_export.py"
HOOKS = ROOT / "ione_qms" / "hooks.py"


def _set_assignment(path: Path, name: str) -> frozenset[str]:
	tree = ast.parse(path.read_text(encoding="utf-8"))
	assignments: dict[str, ast.expr] = {}
	for node in tree.body:
		if isinstance(node, ast.Assign):
			for target in node.targets:
				if isinstance(target, ast.Name):
					assignments[target.id] = node.value
		elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
			assignments[node.target.id] = node.value

	def evaluate(node: ast.expr) -> set[str]:
		if isinstance(node, ast.Name):
			return evaluate(assignments[node.id])
		if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
			return evaluate(node.left) | evaluate(node.right)
		if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
			if node.func.id not in {"frozenset", "set"} or len(node.args) != 1:
				raise AssertionError(f"Unsupported set call in {path}: {ast.dump(node)}")
			return evaluate(node.args[0])
		if isinstance(node, ast.Set | ast.List | ast.Tuple):
			return {str(ast.literal_eval(item)) for item in node.elts}
		raise AssertionError(f"Unsupported set expression in {path}: {ast.dump(node)}")

	return frozenset(evaluate(assignments[name]))


class TestNativeExportGovernanceStatic(unittest.TestCase):
	def test_generator_and_runtime_governed_sets_are_identical(self) -> None:
		self.assertEqual(
			_set_assignment(GENERATOR, "GOVERNED_NATIVE_EXPORT_DOCTYPES"),
			_set_assignment(RUNTIME_EXPORT, "GOVERNED_NATIVE_EXPORT_DOCTYPES"),
		)

	def test_every_governed_schema_disables_native_export(self) -> None:
		governed = _set_assignment(RUNTIME_EXPORT, "GOVERNED_NATIVE_EXPORT_DOCTYPES")
		schemas: dict[str, dict] = {}
		for path in (ROOT / "ione_qms").glob("ione_*/doctype/*/*.json"):
			payload = json.loads(path.read_text(encoding="utf-8"))
			if payload.get("doctype") == "DocType":
				schemas[str(payload["name"])] = payload
		self.assertFalse(governed.difference(schemas))
		for doctype in sorted(governed):
			with self.subTest(doctype=doctype):
				self.assertFalse(
					[
						permission.get("role")
						for permission in schemas[doctype].get("permissions", [])
						if permission.get("export")
					]
				)

	def test_controlled_export_targets_are_a_subset_of_native_governance(self) -> None:
		self.assertTrue(
			_set_assignment(RUNTIME_EXPORT, "EXPORTABLE_DOCTYPES").issubset(
				_set_assignment(RUNTIME_EXPORT, "GOVERNED_NATIVE_EXPORT_DOCTYPES")
			)
		)

	def test_exact_frappe_export_method_is_overridden_by_full_guard(self) -> None:
		hooks = HOOKS.read_text(encoding="utf-8")
		override = OVERRIDE.read_text(encoding="utf-8")
		self.assertIn(
			'"frappe.core.doctype.data_export.exporter.export_data"',
			hooks,
		)
		self.assertIn("GOVERNED_NATIVE_EXPORT_DOCTYPES", override)
		self.assertNotIn("intersection(EXPORTABLE_DOCTYPES)", override)


class TestNativeExportTargetInspection(unittest.TestCase):
	def test_collects_direct_json_and_recursively_nested_targets(self) -> None:
		values = collect_export_target_strings(
			"Sales Invoice",
			'["User", {"payload": ["IONE QC Finding"]}]',
			({"parent_doctype": '"IONE Medical Staff"'},),
		)
		self.assertTrue(
			{
				"Sales Invoice",
				"User",
				"IONE QC Finding",
				"IONE Medical Staff",
			}.issubset(values)
		)

	def test_malformed_suspected_qms_json_fails_closed(self) -> None:
		for value in (
			'["IONE QC Finding"',
			'{"doctype":"IONE Medical Staff"',
			'["\\u0049\\u004f\\u004e\\u0045 QC Finding"',
		):
			with self.subTest(value=value), self.assertRaises(ExportTargetInspectionError):
				collect_export_target_strings(value)

	def test_non_qms_malformed_json_is_left_for_upstream_validation(self) -> None:
		self.assertEqual(collect_export_target_strings('["Sales Invoice"'), frozenset({'["Sales Invoice"'}))

	def test_depth_node_string_and_cycle_limits_fail_closed(self) -> None:
		deep: object = "Sales Invoice"
		for _ in range(MAX_EXPORT_TARGET_DEPTH + 1):
			deep = [deep]
		with self.assertRaises(ExportTargetInspectionError):
			collect_export_target_strings(deep)

		with self.assertRaises(ExportTargetInspectionError):
			collect_export_target_strings(["x"] * MAX_EXPORT_TARGET_NODES)
		with self.assertRaises(ExportTargetInspectionError):
			collect_export_target_strings("x" * (MAX_EXPORT_TARGET_STRING_BYTES + 1))

		cyclic: list[object] = []
		cyclic.append(cyclic)
		with self.assertRaises(ExportTargetInspectionError):
			collect_export_target_strings(cyclic)

	def test_depth_boundary_is_accepted(self) -> None:
		value: object = "IONE QC Finding"
		for _ in range(MAX_EXPORT_TARGET_DEPTH):
			value = [value]
		self.assertIn("IONE QC Finding", collect_export_target_strings(value))


class TestNativeExportOverrideWithoutFrappe(unittest.TestCase):
	def _load_override(self):
		governed = _set_assignment(RUNTIME_EXPORT, "GOVERNED_NATIVE_EXPORT_DOCTYPES")
		calls: list[dict] = []

		class FakePermissionError(Exception):
			pass

		frappe = types.ModuleType("frappe")
		frappe.__path__ = []
		frappe.PermissionError = FakePermissionError
		frappe.session = types.SimpleNamespace(user="Administrator")
		frappe.whitelist = lambda: lambda function: function

		def throw(message, exception):
			raise exception(message)

		frappe.throw = throw
		exporter = types.ModuleType("frappe.core.doctype.data_export.exporter")

		def upstream_export_data(**kwargs):
			calls.append(kwargs)
			return "UPSTREAM"

		exporter.export_data = upstream_export_data
		service = types.ModuleType("ione_qms.services.data_export")
		service.GOVERNED_NATIVE_EXPORT_DOCTYPES = governed
		modules = {
			"frappe": frappe,
			"frappe.core": types.ModuleType("frappe.core"),
			"frappe.core.doctype": types.ModuleType("frappe.core.doctype"),
			"frappe.core.doctype.data_export": types.ModuleType("frappe.core.doctype.data_export"),
			"frappe.core.doctype.data_export.exporter": exporter,
			"ione_qms.services.data_export": service,
		}
		spec = importlib.util.spec_from_file_location(
			"ione_qms_native_export_override_under_test",
			OVERRIDE,
		)
		self.assertIsNotNone(spec)
		self.assertIsNotNone(spec.loader)
		module = importlib.util.module_from_spec(spec)
		with patch.dict(sys.modules, modules):
			spec.loader.exec_module(module)
		return module, FakePermissionError, calls

	def test_administrator_is_denied_for_direct_and_nested_governed_targets(self) -> None:
		module, permission_error, calls = self._load_override()
		for doctype, parent in (
			("IONE QC Finding", None),
			([{"nested": ["IONE Medical Staff"]}], None),
			("Sales Invoice", '{"nested":"IONE Agent Release"}'),
		):
			with self.subTest(doctype=doctype, parent=parent), self.assertRaises(permission_error):
				module.export_data(doctype=doctype, parent_doctype=parent)
		self.assertEqual(calls, [])

	def test_non_governed_target_delegates_with_exact_api_arguments(self) -> None:
		module, _permission_error, calls = self._load_override()
		result = module.export_data(
			doctype="Sales Invoice",
			parent_doctype=None,
			all_doctypes="false",
			with_data="true",
			select_columns='{"Sales Invoice":["name"]}',
			file_type="Excel",
			template="false",
			filters='[["docstatus","=",1]]',
			export_without_column_meta="true",
		)
		self.assertEqual(result, "UPSTREAM")
		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0]["doctype"], "Sales Invoice")
		self.assertEqual(calls[0]["file_type"], "Excel")
		self.assertEqual(calls[0]["export_without_column_meta"], "true")
