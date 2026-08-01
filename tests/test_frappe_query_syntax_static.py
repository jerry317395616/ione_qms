from __future__ import annotations

import ast
import re
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "ione_qms"
DASHBOARD_PATH = APP_ROOT / "api" / "dashboard.py"
FUNCTION_FIELD = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*\(")


class TestFrappeQuerySyntaxStatic(TestCase):
	def test_permissioned_list_fields_use_modern_function_dictionary_syntax(self) -> None:
		violations: list[str] = []
		for path in sorted(APP_ROOT.rglob("*.py")):
			tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
			for node in ast.walk(tree):
				if not isinstance(node, ast.Call):
					continue
				for keyword in node.keywords:
					if keyword.arg != "fields" or not isinstance(keyword.value, ast.List | ast.Tuple):
						continue
					for field in keyword.value.elts:
						if (
							isinstance(field, ast.Constant)
							and isinstance(field.value, str)
							and FUNCTION_FIELD.match(field.value)
						):
							violations.append(f"{path.relative_to(ROOT)}:{field.lineno}:{field.value}")
		self.assertEqual(violations, [])

	def test_daily_aggregation_keeps_permission_query_enforcement_enabled(self) -> None:
		source = DASHBOARD_PATH.read_text(encoding="utf-8")
		tree = ast.parse(source)
		daily = next(
			node
			for node in tree.body
			if isinstance(node, ast.FunctionDef) and node.name == "_permissioned_daily_counts"
		)
		fragment = ast.get_source_segment(source, daily) or ""
		self.assertIn("frappe.qb.get_query", fragment)
		self.assertIn("ignore_permissions=False", fragment)
		self.assertIn("user=frappe.session.user", fragment)
		self.assertNotIn("frappe.db.sql", fragment)
