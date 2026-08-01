from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from ione_qms.indicator_engine import validate_formula_schema
from ione_qms.rule_engine.evaluator import validate_rule_definition_schema
from ione_qms.setup.blueprint import (
	BLUEPRINT_VALIDATOR_NAMES,
	CORE_RULE_TEMPLATES,
	NATIONAL_GOALS,
	_event_rate_formula,
	_reserved_blueprint_collisions,
	blueprint_checksum,
	seed_quality_blueprint,
)


class _BlueprintDatabase:
	def __init__(self, existing: set[tuple[str, str]]) -> None:
		self.existing = existing

	def exists(self, doctype: str, value) -> bool:
		if doctype == "DocType":
			return value == "IONE QC Standard"
		fieldname, code = next(iter(value.items()))
		return (doctype, f"{fieldname}:{code}") in self.existing


def _fake_frappe(existing: set[tuple[str, str]]):
	frappe = ModuleType("frappe")
	frappe.db = _BlueprintDatabase(existing)
	frappe.logger = lambda _name: SimpleNamespace(warning=lambda *_args: None)
	return frappe


class TestQualityBlueprint(TestCase):
	def test_every_seeded_doctype_has_an_explicit_install_time_validator(self) -> None:
		self.assertEqual(
			set(BLUEPRINT_VALIDATOR_NAMES),
			{
				"IONE QC Standard",
				"IONE QC Standard Version",
				"IONE QC Standard Clause",
				"IONE QC Indicator",
				"IONE QC Indicator Version",
				"IONE QC Rule",
				"IONE QC Rule Version",
			},
		)

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

	def test_existing_reserved_record_skips_whole_blueprint_without_writes(self) -> None:
		frappe = _fake_frappe({("IONE QC Indicator", "indicator_code:NIT-2026-III")})
		with (
			patch.dict(sys.modules, {"frappe": frappe}),
			patch("ione_qms.setup.blueprint._insert_reserved_blueprint") as insert,
		):
			result = seed_quality_blueprint()
		self.assertEqual(result["skipped_existing"], 1)
		self.assertEqual(result["standards"], 0)
		insert.assert_not_called()

	def test_reserved_namespace_scan_covers_standard_indicators_and_rules(self) -> None:
		existing = {
			("IONE QC Standard", "standard_code:NHC-QSA-2026"),
			("IONE QC Indicator", "indicator_code:NIT-2026-X"),
			("IONE QC Rule", "rule_code:IONE-QCR-VTE-001"),
		}
		with patch.dict(sys.modules, {"frappe": _fake_frappe(existing)}):
			collisions = _reserved_blueprint_collisions()
		self.assertEqual(len(collisions), 3)
		self.assertIn("IONE QC Standard:NHC-QSA-2026", collisions)
