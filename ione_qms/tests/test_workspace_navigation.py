from __future__ import annotations

import importlib.util
import json
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "ione_qms"
NAVIGATION_PATH = PACKAGE / "setup" / "navigation.py"

EXPECTED_NAVIGATION = (
	("IONE Analytics", "IONE Quality Analytics", "质量总览", 81.0),
	("IONE Clinical Quality", "IONE Clinical Quality", "临床质控", 82.0),
	("IONE Improvement", "IONE Improvement", "持续改进", 83.0),
	("IONE Indicators", "IONE Indicators", "指标管理", 84.0),
	("IONE Quality Standards", "IONE Quality Standards", "标准规则", 85.0),
	("IONE Flow AI", "IONE Flow AI", "智能质控", 86.0),
	("IONE Integration", "IONE Quality Integration", "数据集成", 87.0),
	("IONE Foundation", "IONE Foundation", "基础资料", 88.0),
	("IONE Administration", "IONE Administration", "系统管理", 89.0),
)
EXPECTED_PAGE_TITLES = {
	"ione-quality-command-center": "医疗质量驾驶舱",
	"ione-quality-action-workbench": "质量行动工作台",
}

FORBIDDEN_DISPLAY_TEXT = re.compile(r"ione|i-one|qms|ai|flow|mdt|phi", re.IGNORECASE)
CHINESE_TEXT = re.compile(r"[\u3400-\u9fff]")


def _workspace_payloads() -> list[dict]:
	payloads = []
	for path in PACKAGE.glob("*/workspace/*/*.json"):
		payload = json.loads(path.read_text(encoding="utf-8"))
		if payload.get("doctype") == "Workspace" and payload.get("app") == "ione_qms":
			payloads.append(payload)
	return sorted(payloads, key=lambda payload: float(payload["sequence_id"]))


class _Workspace:
	def __init__(self, name: str) -> None:
		self.name = name
		self.values: dict = {}
		self.sidebar_items: list[dict] = []
		self.save_count = 0

	def update(self, values: dict) -> None:
		self.values.update(values)

	def set(self, fieldname: str, values: list) -> None:
		assert fieldname == "sidebar_items"
		self.sidebar_items = list(values)

	def append(self, fieldname: str, value: dict) -> None:
		assert fieldname == "sidebar_items"
		self.sidebar_items.append(dict(value))

	def save(self, *, ignore_permissions: bool) -> None:
		assert ignore_permissions
		self.save_count += 1


class _Database:
	def __init__(self) -> None:
		self.page_titles = {
			"ione-quality-command-center": "IONE Quality Command Center",
			"ione-quality-action-workbench": "IONE Quality Action Workbench",
		}
		self.set_value_calls: list[tuple[str, str, str, str, bool]] = []

	def exists(self, _doctype: str, _name: str) -> bool:
		return True

	def get_value(self, doctype: str, name: str, fieldname: str) -> str:
		assert doctype == "Page"
		assert fieldname == "title"
		return self.page_titles[name]

	def set_value(
		self,
		doctype: str,
		name: str,
		fieldname: str,
		value: str,
		*,
		update_modified: bool,
	) -> None:
		assert doctype == "Page"
		assert fieldname == "title"
		self.page_titles[name] = value
		self.set_value_calls.append((doctype, name, fieldname, value, update_modified))


@contextmanager
def _load_navigation():
	workspaces = {name: _Workspace(name) for name, *_rest in EXPECTED_NAVIGATION}
	clear_cache_calls: list[bool] = []
	database = _Database()
	frappe = ModuleType("frappe")
	frappe.db = database
	frappe.flags = SimpleNamespace(in_migrate=False)
	frappe.get_app_path = lambda _app: str(PACKAGE)
	frappe.get_doc = lambda _doctype, name: workspaces[name]
	frappe.clear_cache = lambda: clear_cache_calls.append(True)
	previous = sys.modules.get("frappe")
	try:
		sys.modules["frappe"] = frappe
		spec = importlib.util.spec_from_file_location("_ione_qms_navigation_test", NAVIGATION_PATH)
		assert spec and spec.loader
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		yield module, workspaces, clear_cache_calls, database
	finally:
		if previous is None:
			sys.modules.pop("frappe", None)
		else:
			sys.modules["frappe"] = previous


class TestWorkspaceNavigation(TestCase):
	def test_navigation_keeps_internal_names_and_uses_chinese_display_text(self) -> None:
		payloads = _workspace_payloads()
		self.assertEqual(
			[
				(payload["name"], payload["module"], payload["label"], payload["sequence_id"])
				for payload in payloads
			],
			list(EXPECTED_NAVIGATION),
		)

		section_count = 0
		link_count = 0
		link_type_counts = {"DocType": 0, "Page": 0, "Report": 0}
		for payload in payloads:
			for value in (payload["label"], payload["title"]):
				self.assertRegex(value, CHINESE_TEXT)
				self.assertNotRegex(value, FORBIDDEN_DISPLAY_TEXT)
			for item in payload["sidebar_items"]:
				self.assertRegex(item["label"], CHINESE_TEXT)
				self.assertNotRegex(item["label"], FORBIDDEN_DISPLAY_TEXT)
				if item["type"] == "Section Break":
					section_count += 1
					continue
				self.assertEqual(item["type"], "Link")
				self.assertTrue(item["link_to"])
				link_count += 1
				link_type_counts[item["link_type"]] += 1

		self.assertEqual(section_count, 29)
		self.assertEqual(link_count, 114)
		self.assertEqual(link_type_counts, {"DocType": 101, "Page": 4, "Report": 9})

	def test_after_migrate_reconciliation_is_idempotent(self) -> None:
		with _load_navigation() as (navigation, workspaces, clear_cache_calls, database):
			first = navigation.ensure_workspace_navigation()
			second = navigation.ensure_workspace_navigation()

		self.assertEqual(first, second)
		self.assertEqual(first["sections"], 29)
		self.assertEqual(first["links"], 114)
		self.assertEqual(first["page_titles"], EXPECTED_PAGE_TITLES)
		self.assertEqual(clear_cache_calls, [True, True])
		self.assertEqual(database.page_titles, EXPECTED_PAGE_TITLES)
		self.assertEqual(
			database.set_value_calls,
			[("Page", page_name, "title", title, False) for page_name, title in EXPECTED_PAGE_TITLES.items()],
		)
		for name, _module, label, sequence_id in EXPECTED_NAVIGATION:
			workspace = workspaces[name]
			self.assertEqual(workspace.save_count, 2)
			self.assertEqual(workspace.values["label"], label)
			self.assertEqual(workspace.values["title"], label)
			self.assertEqual(workspace.values["sequence_id"], sequence_id)
			self.assertTrue(workspace.sidebar_items)

	def test_page_title_contract_is_validated_before_navigation_is_written(self) -> None:
		with _load_navigation() as (navigation, workspaces, clear_cache_calls, database):
			navigation.EXPECTED_PAGE_TITLES["ione-quality-command-center"] = "IONE 驾驶舱"
			with self.assertRaisesRegex(RuntimeError, "forbidden English name"):
				navigation.ensure_workspace_navigation()

		self.assertFalse(navigation.frappe.flags.in_migrate)
		self.assertFalse(clear_cache_calls)
		self.assertFalse(database.set_value_calls)
		self.assertTrue(all(workspace.save_count == 0 for workspace in workspaces.values()))

	def test_application_entry_and_landing_page_are_chinese(self) -> None:
		hooks = (PACKAGE / "hooks.py").read_text(encoding="utf-8")
		desktop = (PACKAGE / "config" / "desktop.py").read_text(encoding="utf-8")
		page = json.loads(
			(
				PACKAGE
				/ "ione_quality_analytics"
				/ "page"
				/ "ione_quality_command_center"
				/ "ione_quality_command_center.json"
			).read_text(encoding="utf-8")
		)
		action_workbench = json.loads(
			(
				PACKAGE
				/ "ione_quality_analytics"
				/ "page"
				/ "ione_quality_action_workbench"
				/ "ione_quality_action_workbench.json"
			).read_text(encoding="utf-8")
		)
		self.assertIn('app_title = "医疗质量管理"', hooks)
		self.assertIn('_("医疗质量管理")', desktop)
		self.assertEqual(page["title"], "医疗质量驾驶舱")
		self.assertEqual(action_workbench["title"], "质量行动工作台")
		self.assertEqual(page["name"], "ione-quality-command-center")
		self.assertEqual(page["page_name"], "ione-quality-command-center")
		self.assertEqual(action_workbench["name"], "ione-quality-action-workbench")
		self.assertEqual(action_workbench["page_name"], "ione-quality-action-workbench")
