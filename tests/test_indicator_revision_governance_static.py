from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ione_qms.indicator_engine.calculator import (  # noqa: E402
	BaseIndicatorCalculator,
	governed_query_contract_hash,
	validate_formula_schema,
)
from ione_qms.indicator_engine.dimensions import (  # noqa: E402
	canonical_dimension,
	dimension_registry_contract,
	normalize_dimension_values,
	registered_dimensions,
)


class TestIndicatorDimensionRegistry(unittest.TestCase):
	def test_nine_canonical_dimensions_and_legacy_alias_are_governed(self) -> None:
		self.assertEqual(
			set(registered_dimensions()),
			{
				"hospital",
				"campus",
				"department",
				"ward",
				"medical_group",
				"physician",
				"disease",
				"surgery",
				"drg",
			},
		)
		self.assertEqual(canonical_dimension("medical_staff"), "physician")
		self.assertTrue(
			all(
				specification["authorization_scope"]
				for specification in dimension_registry_contract()["dimensions"].values()
			)
		)
		self.assertEqual(
			normalize_dimension_values({"medical_staff": "STAFF-1"}),
			{"physician": "STAFF-1"},
		)
		with self.assertRaisesRegex(ValueError, "Conflicting"):
			normalize_dimension_values({"medical_staff": "STAFF-1", "physician": "STAFF-2"})

	def test_query_contract_is_alias_stable_and_mapping_sensitive(self) -> None:
		legacy = {
			"measure": "Count",
			"numerator": {"dataset": "surgery_qc", "date_field": "surgery_time"},
			"dimensions": ["medical_staff"],
		}
		canonical = {**legacy, "dimensions": ["physician"]}
		self.assertEqual(
			governed_query_contract_hash(legacy, ["medical_staff"]),
			governed_query_contract_hash(canonical, ["physician"]),
		)
		self.assertNotEqual(
			governed_query_contract_hash(canonical, ["physician"]),
			governed_query_contract_hash(
				{**canonical, "numerator": {"dataset": "encounters"}},
				["physician"],
			),
		)

	def test_formula_and_calculator_capability_fail_closed(self) -> None:
		formula = validate_formula_schema(
			{
				"measure": "Count",
				"numerator": {"dataset": "surgery_qc"},
				"dimensions": [
					"hospital",
					"campus",
					"department",
					"ward",
					"medical_group",
					"physician",
					"disease",
					"surgery",
					"drg",
				],
			}
		)
		self.assertEqual(len(formula["dimensions"]), 9)
		with self.assertRaisesRegex(ValueError, "does not support"):
			BaseIndicatorCalculator.validate_dimension_capability({}, ("hospital",))
		with self.assertRaisesRegex(ValueError, "not governed"):
			validate_formula_schema(
				{
					"measure": "Count",
					"numerator": {"dataset": "encounters"},
					"dimensions": ["arbitrary_sql_dimension"],
				}
			)


class TestIndicatorRevisionStatic(unittest.TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		cls.service = (ROOT / "ione_qms/services/indicators.py").read_text(encoding="utf-8")
		cls.versions = (ROOT / "ione_qms/services/versions.py").read_text(encoding="utf-8")
		cls.generator = (ROOT / "tools/generate_doctypes.py").read_text(encoding="utf-8")
		cls.hooks = (ROOT / "ione_qms/hooks.py").read_text(encoding="utf-8")
		cls.permissions = (ROOT / "ione_qms/permissions.py").read_text(encoding="utf-8")
		cls.calculator = (ROOT / "ione_qms/indicator_engine/calculator.py").read_text(encoding="utf-8")
		cls.data_export = (ROOT / "ione_qms/services/data_export.py").read_text(encoding="utf-8")
		cls.pdca = (ROOT / "ione_qms/services/pdca_structure.py").read_text(encoding="utf-8")
		cls.migrations = (ROOT / "ione_qms/tasks/migrations.py").read_text(encoding="utf-8")
		cls.integration_mapping = (ROOT / "ione_qms/integration/mapping.py").read_text(encoding="utf-8")

	def test_service_uses_receipt_idempotency_and_locked_atomic_pointer(self) -> None:
		self.assertIn('"input_receipt_hash": input_receipt_hash', self.service)
		self.assertIn('{"input_receipt_hash": input_receipt_hash}', self.service)
		self.assertIn("ione-qms:indicator-series:", self.service)
		self.assertIn("for update", self.service.lower())
		self.assertIn("_switch_result_pointer(", self.service)
		self.assertIn("result_revision", self.service)
		self.assertIn("series_revision_key", self.service)
		self.assertNotIn("def _upsert_result(", self.service)

	def test_receipt_has_every_required_reproduction_anchor(self) -> None:
		for token in (
			"indicator_version_checksum",
			"dimension_hash",
			"source_manifest_hash",
			"source_manifest_count",
			"mapping_record",
			"mapping_version",
			"mapping_checksum",
			"query_contract_hash",
			"calculator_version",
			"calculator_code_hash",
		):
			self.assertIn(f'"{token}"', self.service)
		self.assertIn("verify_indicator_result_reproducibility", self.service)
		self.assertIn("result_checksum", self.service)
		self.assertIn("detail_checksum", self.service)

	def test_publishing_uses_same_registry_and_freezes_execution_contract(self) -> None:
		for token in (
			"normalize_dimension_names",
			"validate_dimension_capability",
			"governed_query_contract_hash",
			"calculator_code_hash",
			"mapping_checksum_snapshot",
			"source_system_snapshot",
		):
			self.assertIn(token, self.versions)
		self.assertIn("Published indicator versions require a governed source_mapping", self.versions)
		self.assertIn("does not resolve governed dimensions", self.versions)

	def test_schema_hooks_permissions_and_migration_are_fail_closed(self) -> None:
		for doctype in (
			"IONE Indicator Calculation",
			"IONE Indicator Result",
			"IONE Indicator Result Detail",
			"IONE Indicator Result Pointer",
		):
			self.assertIn(f'"{doctype}"', self.generator)
			self.assertIn(f'"{doctype}"', self.hooks)
		for fieldname in (
			"input_receipt_hash",
			"result_revision",
			"series_revision_key",
			"result_checksum",
			"pointer_checksum",
			"lineage_status",
		):
			self.assertIn(f'"{fieldname}"', self.generator)
		self.assertIn("pointer.current_result", self.permissions)
		self.assertIn("quarantine_legacy_indicator_receipts", self.migrations)
		self.assertIn("LEGACY_INPUT_RECEIPT_UNPROVABLE", self.service)
		for target in ("medical_group", "medical_staff", "disease", "surgery", "drg"):
			self.assertIn(f'"{target}"', self.integration_mapping)

	def test_result_detail_consumers_share_current_and_disposition_fail_closed_contract(self) -> None:
		for token in (
			"Indicator Result Pointer",
			"Indicator Quarantine Receipt",
			"Indicator Quarantine Disposition",
			"not exists",
			"status = 'Approved'",
		):
			self.assertIn(token, self.calculator)
		self.assertIn("indicator_result_detail_governance_sql", self.permissions)
		self.assertIn("indicator_result_detail_is_governed_readable", self.permissions)
		self.assertIn("indicator_result_detail_governance_schema_ready", self.data_export)
		self.assertIn("_governed_source_page(", self.calculator)

	def test_pdca_uses_one_globally_ordered_multi_result_lock(self) -> None:
		self.assertIn("current_indicator_results_lock(source_names)", self.pdca)
		self.assertIn("for series_key in sorted(name_by_series)", self.service)
		self.assertIn("source_snapshots=source_snapshots", self.pdca)

	def test_python_sources_parse_without_importing_frappe(self) -> None:
		for path in (
			ROOT / "ione_qms/services/indicators.py",
			ROOT / "ione_qms/indicator_engine/calculator.py",
			ROOT / "ione_qms/indicator_engine/dimensions.py",
			ROOT / "ione_qms/tasks/indicators.py",
		):
			ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


if __name__ == "__main__":
	unittest.main()
