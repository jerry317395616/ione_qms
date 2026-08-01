from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PATCH_PATH = ROOT / "ione_qms" / "patches" / "rename_integration_module.py"


class _Database:
	def __init__(self) -> None:
		self.modules = {
			"IONE Integration": "ione_qms",
			"IONE Analytics": "ione_qms",
		}
		self.doctypes = {
			"IONE Integration Error": "IONE Integration",
			"IONE Improvement Project": "IONE Analytics",
		}

	def exists(self, doctype: str, name: str) -> bool:
		rows = self.modules if doctype == "Module Def" else self.doctypes
		return name in rows

	def get_value(self, doctype: str, name: str, fieldname: str) -> str | None:
		rows = self.modules if doctype == "Module Def" else self.doctypes
		return rows.get(name)

	def set_value(
		self,
		doctype: str,
		name: str,
		fieldname: str,
		value: str,
		*,
		update_modified: bool,
	) -> None:
		rows = self.modules if doctype == "Module Def" else self.doctypes
		rows[name] = value


def _load_patch() -> tuple[types.ModuleType, _Database, types.SimpleNamespace]:
	database = _Database()
	local = types.SimpleNamespace(module_app={"ione_integration": "ione_qms"})
	frappe = types.ModuleType("frappe")
	frappe.ValidationError = type("ValidationError", (Exception,), {})
	frappe.db = database
	frappe.local = local
	frappe.get_installed_apps = lambda: ["frappe", "ione_medical_insurance", "ione_qms"]

	def get_doc(values: dict[str, str]) -> types.SimpleNamespace:
		def insert(*, ignore_permissions: bool) -> None:
			database.modules[values["module_name"]] = values["app_name"]

		return types.SimpleNamespace(insert=insert)

	frappe.get_doc = get_doc

	def throw(message: str, exception: type[Exception]) -> None:
		raise exception(message)

	frappe.throw = throw
	utils = types.ModuleType("frappe.utils")
	utils.scrub = lambda value: "_".join(value.lower().split())
	spec = importlib.util.spec_from_file_location("module_collision_patch", PATCH_PATH)
	assert spec and spec.loader
	module = importlib.util.module_from_spec(spec)
	with patch.dict(sys.modules, {"frappe": frappe, "frappe.utils": utils}):
		spec.loader.exec_module(module)
	return module, database, local


class TestModuleCollisionPatch(TestCase):
	def test_patch_separates_qms_and_medical_insurance_modules_idempotently(self) -> None:
		module, database, local = _load_patch()
		for old_module, _new_module, doctypes in module.MODULE_MIGRATIONS:
			for doctype in doctypes:
				database.doctypes[doctype] = old_module
			for _artifact_doctype, artifact_name in module.QMS_MODULE_ARTIFACTS[old_module]:
				database.doctypes[artifact_name] = old_module

		module.execute()
		module.execute()

		self.assertEqual(database.modules[module.OLD_MODULE], module.COLLISION_APP)
		self.assertEqual(database.modules[module.NEW_MODULE], module.QMS_APP)
		self.assertEqual(database.modules[module.ANALYTICS_OLD_MODULE], module.COLLISION_APP)
		self.assertEqual(database.modules[module.ANALYTICS_NEW_MODULE], module.QMS_APP)
		self.assertEqual(database.doctypes["IONE Integration Error"], module.OLD_MODULE)
		self.assertEqual(database.doctypes["IONE Improvement Project"], module.ANALYTICS_OLD_MODULE)
		for _old_module, new_module, doctypes in module.MODULE_MIGRATIONS:
			for doctype in doctypes:
				self.assertEqual(database.doctypes[doctype], new_module)
			for _artifact_doctype, artifact_name in module.QMS_MODULE_ARTIFACTS[_old_module]:
				self.assertEqual(database.doctypes[artifact_name], new_module)
		self.assertEqual(local.module_app["ione_integration"], module.COLLISION_APP)
		self.assertEqual(local.module_app["ione_quality_integration"], module.QMS_APP)
		self.assertEqual(local.module_app["ione_analytics"], module.COLLISION_APP)
		self.assertEqual(local.module_app["ione_quality_analytics"], module.QMS_APP)
