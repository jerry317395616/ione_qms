from __future__ import annotations

from unittest import TestCase

from ione_qms.indicator_engine import validate_formula_schema
from ione_qms.rule_engine.evaluator import validate_rule_definition_schema
from ione_qms.setup.blueprint import (
	CORE_RULE_TEMPLATES,
	NATIONAL_GOALS,
	_event_rate_formula,
	blueprint_checksum,
)


class TestQualityBlueprint(TestCase):
	def test_all_ten_national_goals_have_unique_executable_definitions(self) -> None:
		self.assertEqual(len(NATIONAL_GOALS), 10)
		self.assertEqual(len({goal["code"] for goal in NATIONAL_GOALS}), 10)
		self.assertEqual(
			{goal["code"] for goal in NATIONAL_GOALS},
			{
				f"NIT-2026-{suffix}"
				for suffix in ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X")
			},
		)
		for goal in NATIONAL_GOALS:
			formula = _event_rate_formula(
				str(goal["numerator_event"]),
				str(goal["denominator_event"]),
			)
			self.assertEqual(validate_formula_schema(formula), formula)

	def test_representative_rules_are_deterministic_and_linked_to_goals(self) -> None:
		self.assertEqual(len(CORE_RULE_TEMPLATES), 10)
		self.assertEqual(len({rule["code"] for rule in CORE_RULE_TEMPLATES}), 10)
		goal_codes = {goal["code"] for goal in NATIONAL_GOALS}
		for rule in CORE_RULE_TEMPLATES:
			self.assertIn(rule["goal"], goal_codes)
			validate_rule_definition_schema(rule["condition"])
			if rule.get("exclusions"):
				validate_rule_definition_schema(rule["exclusions"])
			self.assertIn(
				rule.get("action", "Create Finding"),
				{"Create Finding", "Alert Only", "Measure Only"},
			)

	def test_vte_template_contains_adult_timing_and_fail_closed_exclusion_data(self) -> None:
		vte = next(rule for rule in CORE_RULE_TEMPLATES if rule["code"] == "IONE-QCR-VTE-001")
		leaves = vte["condition"]["all"]
		self.assertIn({"field": "patient_age", "operator": ">=", "value": 18}, leaves)
		self.assertIn({"field": "hours_after_admission", "operator": ">", "value": 24}, leaves)
		self.assertEqual(vte["exclusions"]["all"][0]["field"], "encounter_type")

	def test_blueprint_checksum_is_stable_sha256(self) -> None:
		checksum = blueprint_checksum()
		self.assertEqual(len(checksum), 64)
		self.assertEqual(checksum, blueprint_checksum())
