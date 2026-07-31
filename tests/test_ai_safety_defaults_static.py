from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEM_SETTINGS = (
	ROOT
	/ "ione_qms"
	/ "ione_administration"
	/ "doctype"
	/ "ione_system_settings"
	/ "ione_system_settings.json"
)
AI_SETTINGS = (
	ROOT / "ione_qms" / "ione_administration" / "doctype" / "ione_ai_settings" / "ione_ai_settings.json"
)
GOVERNANCE = ROOT / "ione_qms" / "ai" / "governance.py"
HOOKS = ROOT / "ione_qms" / "hooks.py"
AI_TASKS = ROOT / "ione_qms" / "services" / "ai_tasks.py"
AGENT_EVALUATIONS = ROOT / "ione_qms" / "services" / "agent_evaluations.py"
PRIVACY_RUNTIME = ROOT / "ione_qms" / "ai" / "privacy_runtime.py"


def source_function(path: Path, name: str) -> str:
	source = path.read_text(encoding="utf-8")
	for node in ast.parse(source).body:
		if isinstance(node, ast.FunctionDef) and node.name == name:
			return ast.get_source_segment(source, node) or ""
	raise AssertionError(name)


class TestAISafetyDefaults(unittest.TestCase):
	def test_execution_switches_default_off(self) -> None:
		defaults = {}
		for path in (SYSTEM_SETTINGS, AI_SETTINGS):
			payload = json.loads(path.read_text(encoding="utf-8"))
			defaults.update(
				{
					(payload["name"], field["fieldname"]): str(field.get("default") or "0")
					for field in payload["fields"]
				}
			)
		for key in (
			("IONE System Settings", "enable_realtime_rules"),
			("IONE System Settings", "enable_ai"),
			("IONE System Settings", "production_mode"),
			("IONE AI Settings", "enabled"),
		):
			self.assertEqual(defaults[key], "0", key)

	def test_production_runtime_rechecks_current_approved_evaluation(self) -> None:
		source = source_function(GOVERNANCE, "validate_policy_runtime")
		self.assertIn("production_mode_enabled()", source)
		self.assertIn("has_passing_agent_evaluation(policy, release)", source)

	def test_ai_task_generic_crud_is_guarded_even_for_administrator(self) -> None:
		hooks = HOOKS.read_text(encoding="utf-8")
		self.assertIn("ione_qms.services.ai_tasks.validate_analysis_task", hooks)
		self.assertIn('"on_trash": "ione_qms.services.immutability.prevent_delete"', hooks)
		validator = source_function(AI_TASKS, "validate_analysis_task")
		self.assertIn("_validate_controlled_insert", validator)
		self.assertIn("immutable", validator)

	def test_evaluation_criteria_cannot_be_vacuous(self) -> None:
		validator = source_function(AGENT_EVALUATIONS, "_validate_criteria")
		self.assertIn("semantic", validator)
		coverage = source_function(AGENT_EVALUATIONS, "validate_test_suite_coverage")
		self.assertIn("expected-behavior assertion", coverage)
		self.assertIn("fail-closed assertion", coverage)

	def test_model_output_dlp_loads_raw_and_pseudonymous_scope_identifiers(self) -> None:
		loader = source_function(PRIVACY_RUNTIME, "task_known_identifiers")
		for fieldname in (
			"patient",
			"encounter",
			"patient_name",
			"source_patient_id",
			"date_of_birth",
			"encounter_no",
			"source_encounter_id",
		):
			self.assertIn(fieldname, loader)


if __name__ == "__main__":
	unittest.main()
