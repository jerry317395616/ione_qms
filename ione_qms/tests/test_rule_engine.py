from __future__ import annotations

from unittest import TestCase

from ione_qms.rule_engine.evaluator import (
	evaluate_rule_definition,
	validate_rule_definition_schema,
)
from ione_qms.rule_engine.models import RuleResult
from ione_qms.rule_engine.operators import compare


class TestDeterministicRuleEngine(TestCase):
	def test_matching_condition_is_failed_finding_candidate(self) -> None:
		result = evaluate_rule_definition(
			{
				"all": [
					{"field": "patient.age", "operator": ">=", "value": 18},
					{"field": "vte.assessed", "operator": "eq", "value": False},
				]
			},
			{"patient": {"age": "64"}, "vte": {"assessed": False}},
		)
		self.assertEqual(result.result, RuleResult.FAILED)
		self.assertEqual(len(result.evidence), 2)

	def test_non_matching_condition_passes(self) -> None:
		result = evaluate_rule_definition(
			{"field": "vte.assessed", "operator": "eq", "value": False},
			{"vte": {"assessed": True}},
		)
		self.assertEqual(result.result, RuleResult.PASSED)

	def test_missing_data_fails_closed_without_finding(self) -> None:
		result = evaluate_rule_definition(
			{"field": "vte.assessed", "operator": "eq", "value": False},
			{"vte": {}},
		)
		self.assertEqual(result.result, RuleResult.INSUFFICIENT_DATA)
		self.assertEqual(result.missing_fields, ["vte.assessed"])

	def test_approved_exclusion_wins(self) -> None:
		result = evaluate_rule_definition(
			{"field": "vte.assessed", "operator": "eq", "value": False},
			{"vte": {"assessed": False}, "encounter": {"palliative": True}},
			{"field": "encounter.palliative", "operator": "eq", "value": True},
		)
		self.assertEqual(result.result, RuleResult.EXCLUDED)

	def test_unknown_exclusion_fails_closed_without_finding(self) -> None:
		result = evaluate_rule_definition(
			{"field": "vte.assessed", "operator": "eq", "value": False},
			{"vte": {"assessed": False}, "encounter": {}},
			{"field": "encounter.palliative", "operator": "eq", "value": True},
		)
		self.assertEqual(result.result, RuleResult.INSUFFICIENT_DATA)
		self.assertEqual(result.missing_fields, ["encounter.palliative"])

	def test_irrelevant_missing_any_branch_does_not_hide_a_match(self) -> None:
		result = evaluate_rule_definition(
			{
				"any": [
					{"field": "record.optional", "operator": "eq", "value": True},
					{"field": "record.confirmed", "operator": "eq", "value": True},
				]
			},
			{"record": {"confirmed": True}},
		)
		self.assertEqual(result.result, RuleResult.FAILED)

	def test_false_all_branch_resolves_even_when_another_branch_is_missing(self) -> None:
		result = evaluate_rule_definition(
			{
				"all": [
					{"field": "record.confirmed", "operator": "eq", "value": True},
					{"field": "record.optional", "operator": "eq", "value": True},
				]
			},
			{"record": {"confirmed": False}},
		)
		self.assertEqual(result.result, RuleResult.PASSED)

	def test_unsafe_field_path_is_rejected(self) -> None:
		result = evaluate_rule_definition(
			{"field": "patient.__class__", "operator": "eq", "value": "x"},
			{"patient": {}},
		)
		self.assertEqual(result.result, RuleResult.ERROR)
		self.assertEqual(result.error, "ValueError: deterministic evaluation failed")
		self.assertNotIn("__class__", result.error or "")

	def test_excessive_depth_is_rejected(self) -> None:
		node: dict = {"field": "value", "operator": "eq", "value": 1}
		for _ in range(20):
			node = {"not": node}
		result = evaluate_rule_definition(node, {"value": 1})
		self.assertEqual(result.result, RuleResult.ERROR)
		self.assertEqual(result.error, "ValueError: deterministic evaluation failed")

	def test_numeric_and_datetime_comparison_are_deterministic(self) -> None:
		self.assertTrue(compare(">=", "10.00", 10))
		self.assertTrue(compare("<", "2026-07-01 08:00:00", "2026-07-01 09:00:00"))

	def test_nan_is_rejected(self) -> None:
		with self.assertRaisesRegex(ValueError, "NaN"):
			compare(">", float("nan"), 1)

	def test_schema_validator_rejects_unknown_operator_even_without_source_data(self) -> None:
		with self.assertRaisesRegex(ValueError, "Unsupported rule operator"):
			validate_rule_definition_schema(
				{"field": "vte.assessed", "operator": "eval", "value": "malicious"}
			)

	def test_schema_validator_rejects_extra_leaf_fields(self) -> None:
		with self.assertRaisesRegex(ValueError, "Unexpected rule fields"):
			validate_rule_definition_schema(
				{
					"field": "vte.assessed",
					"operator": "eq",
					"value": True,
					"python": "delete_everything()",
				}
			)
