from __future__ import annotations

from decimal import Decimal
from unittest import TestCase

from ione_qms.indicator_engine import safe_rate, validate_formula_schema
from ione_qms.indicator_engine.calculator import (
	_governed_source_rows,
	_indicator_detail_filter_sql,
	indicator_result_detail_governance_sql,
)


class TestIndicatorEngine(TestCase):
	def test_safe_rate_is_decimal_and_uses_half_up_rounding(self) -> None:
		self.assertEqual(safe_rate(1, 6, precision=2), Decimal("16.67"))

	def test_zero_denominator_is_no_data(self) -> None:
		self.assertIsNone(safe_rate(0, 0))

	def test_non_finite_values_are_rejected(self) -> None:
		with self.assertRaisesRegex(ValueError, "finite"):
			safe_rate(float("nan"), 10)

	def test_formula_accepts_only_reviewed_datasets_and_fields(self) -> None:
		formula = validate_formula_schema(
			{
				"measure": "Rate",
				"numerator": {
					"dataset": "quality_findings",
					"date_field": "detected_at",
					"filters": {"severity": ["in", ["High", "Critical"]]},
				},
				"denominator": {
					"dataset": "encounters",
					"date_field": "admission_time",
				},
				"multiplier": 100,
				"precision": 2,
				"dimensions": ["department"],
			}
		)
		self.assertEqual(formula["dimensions"], ["department"])

	def test_formula_rejects_arbitrary_dataset_or_filter(self) -> None:
		with self.assertRaisesRegex(ValueError, "not registered"):
			validate_formula_schema(
				{
					"measure": "Count",
					"numerator": {"dataset": "tabUser", "date_field": "creation"},
				}
			)
		with self.assertRaisesRegex(ValueError, "not reviewed"):
			validate_formula_schema(
				{
					"measure": "Count",
					"numerator": {
						"dataset": "quality_findings",
						"date_field": "detected_at",
						"filters": {"patient": "secret"},
					},
				}
			)

	def test_indicator_detail_governance_sql_requires_current_and_denies_disposed_rows(self) -> None:
		condition = indicator_result_detail_governance_sql("source")
		self.assertIn("governed_pointer.current_result = source.indicator_result", condition)
		self.assertIn("governed_quarantine.target_name = source.name", condition)
		self.assertIn("governed_disposition.status = 'Approved'", condition)
		self.assertIn("not exists", condition)
		with self.assertRaisesRegex(ValueError, "unsupported table reference"):
			indicator_result_detail_governance_sql("source; drop table tabUser")

	def test_indicator_detail_sql_filters_keep_values_parameterized(self) -> None:
		attack = "x' or 1=1 --"
		clauses, params = _indicator_detail_filter_sql(
			{
				"creation": ["between", ["2026-07-01", "2026-07-31"]],
				"department": attack,
				"ward": ["in", ["WARD-1", None]],
			}
		)
		sql = " and ".join(clauses)
		self.assertNotIn(attack, sql)
		self.assertIn(attack, params)
		self.assertIn("source.`ward` is null", sql)
		self.assertEqual(params[:2], ["2026-07-01", "2026-07-31"])

	def test_indicator_detail_calculator_page_uses_governance_predicate(self) -> None:
		class _DB:
			def __init__(self) -> None:
				self.query = ""
				self.params = ()

			def exists(self, _doctype, _name) -> bool:
				return True

			def sql(self, query, params, as_dict=False):
				self.query = query
				self.params = params
				self.as_dict = as_dict
				return []

		class _Frappe:
			db = _DB()

		_governed_source_rows(
			_Frappe,
			"IONE Indicator Result Detail",
			filters=[
				["IONE Indicator Result Detail", "creation", ">", "2026-07-01"],
				["IONE Indicator Result Detail", "creation", "<", "2026-08-01"],
			],
			fields=["name", "indicator_result"],
			order_by="creation asc, name asc",
			limit=25,
		)
		self.assertIn("governed_pointer.current_result = source.indicator_result", _Frappe.db.query)
		self.assertIn("governed_disposition.status = 'Approved'", _Frappe.db.query)
		self.assertEqual(_Frappe.db.params, ("2026-07-01", "2026-08-01", 25))
		self.assertTrue(_Frappe.db.as_dict)
