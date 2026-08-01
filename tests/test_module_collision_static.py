from __future__ import annotations

import json
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "ione_qms"
INTEGRATION_ROOT = APP_ROOT / "ione_quality_integration"
OLD_MODULE = "IONE Integration"
NEW_MODULE = "IONE Quality Integration"


class TestModuleCollisionStatic(TestCase):
	def test_integration_module_has_unique_route_and_package(self) -> None:
		modules = (APP_ROOT / "modules.txt").read_text(encoding="utf-8").splitlines()
		self.assertIn(NEW_MODULE, modules)
		self.assertNotIn(OLD_MODULE, modules)
		self.assertTrue(INTEGRATION_ROOT.is_dir())
		self.assertFalse((APP_ROOT / "ione_integration").exists())

	def test_all_qms_integration_doctypes_use_unique_module(self) -> None:
		metadata = sorted((INTEGRATION_ROOT / "doctype").glob("*/*.json"))
		self.assertEqual(len(metadata), 10)
		for path in metadata:
			self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["module"], NEW_MODULE)

	def test_pre_model_sync_patch_releases_foreign_module(self) -> None:
		patches = (APP_ROOT / "patches.txt").read_text(encoding="utf-8")
		self.assertIn("[pre_model_sync]", patches)
		self.assertIn("ione_qms.patches.rename_integration_module", patches)
		self.assertNotIn("ione_qms.patches.rename_integration_module.execute", patches)
		source = (APP_ROOT / "patches" / "rename_integration_module.py").read_text(encoding="utf-8")
		self.assertIn('COLLISION_APP = "ione_medical_insurance"', source)
		self.assertIn("QMS_DOCTYPES = (", source)
		self.assertIn("frappe.local.module_app[scrub(OLD_MODULE)] = COLLISION_APP", source)
