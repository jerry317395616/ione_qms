from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ione_qms.indicator_engine.dimensions import (  # noqa: E402
	canonical_dimension,
	dimension_spec,
	normalize_dimension_values,
)
from ione_qms.integration.mapping import (  # noqa: E402
	ALLOWED_TARGET_FIELDS,
	ENCOUNTER_MASTER_DATA_FIELDS,
	MASTER_DATA_CONSTANT_FIELDS,
)

SOURCE_DIMENSION_DOCTYPES = (
	"ione_qms/ione_clinical_quality/doctype/ione_clinical_quality_event/ione_clinical_quality_event.json",
	"ione_qms/ione_foundation/doctype/ione_encounter_index/ione_encounter_index.json",
	"ione_qms/ione_indicators/doctype/ione_indicator_result_detail/ione_indicator_result_detail.json",
	"ione_qms/ione_clinical_quality/doctype/ione_medical_record_qc/ione_medical_record_qc.json",
	"ione_qms/ione_clinical_quality/doctype/ione_qc_finding/ione_qc_finding.json",
	"ione_qms/ione_clinical_quality/doctype/ione_medical_safety_event/ione_medical_safety_event.json",
	"ione_qms/ione_clinical_quality/doctype/ione_surgery_qc/ione_surgery_qc.json",
)

PROJECTION_DOCTYPES = (
	"ione_qms/ione_indicators/doctype/ione_indicator_result/ione_indicator_result.json",
	"ione_qms/ione_indicators/doctype/ione_indicator_result_pointer/ione_indicator_result_pointer.json",
	"ione_qms/ione_quality_analytics/doctype/ione_daily_quality_fact/ione_daily_quality_fact.json",
	"ione_qms/ione_quality_analytics/doctype/ione_monthly_quality_fact/ione_monthly_quality_fact.json",
)


def _doctype(path: str) -> dict:
	return json.loads((ROOT / path).read_text(encoding="utf-8"))


class TestDIPGroupingSupport(unittest.TestCase):
	def test_dip_is_a_distinct_governed_dimension(self) -> None:
		self.assertEqual(canonical_dimension("dip"), "dip")
		self.assertEqual(canonical_dimension("dip_code"), "dip")
		self.assertEqual(normalize_dimension_values({"dip_code": "DIP-A01"}), {"dip": "DIP-A01"})
		specification = dimension_spec("dip")
		self.assertEqual(specification.label, "DIP")
		self.assertEqual(specification.aliases, ("dip_code",))
		for dataset in (
			"clinical_events",
			"encounters",
			"indicator_details",
			"medical_record_qc",
			"quality_findings",
			"safety_events",
			"surgery_qc",
		):
			self.assertEqual(specification.fields_for_dataset(dataset), ("dip", "dip_code"))

	def test_integration_mapping_accepts_dip_for_encounter_lineage(self) -> None:
		for contract in (
			ALLOWED_TARGET_FIELDS,
			ENCOUNTER_MASTER_DATA_FIELDS,
			MASTER_DATA_CONSTANT_FIELDS,
		):
			self.assertIn("dip", contract)

	def test_source_and_projection_doctypes_store_dip_separately_from_drg(self) -> None:
		for relative_path in (*SOURCE_DIMENSION_DOCTYPES, *PROJECTION_DOCTYPES):
			with self.subTest(path=relative_path):
				definition = _doctype(relative_path)
				field_order = definition.get("field_order") or []
				fields = {field.get("fieldname"): field for field in definition.get("fields") or []}
				self.assertIn("drg", field_order)
				self.assertIn("dip", field_order)
				self.assertIn("dip", fields)
				self.assertEqual(fields["dip"].get("fieldtype"), "Data")
				self.assertEqual(
					fields["dip"].get("label"),
					"DIP" if relative_path in PROJECTION_DOCTYPES else "DIP Source Dimension",
				)

	def test_dip_is_propagated_through_execution_and_analytics_sources(self) -> None:
		for relative_path in (
			"ione_qms/integration/service.py",
			"ione_qms/integration/master_data.py",
			"ione_qms/rule_engine/executor.py",
			"ione_qms/services/projections.py",
			"ione_qms/services/indicators.py",
			"ione_qms/tasks/analytics.py",
		):
			with self.subTest(path=relative_path):
				source = (ROOT / relative_path).read_text(encoding="utf-8")
				ast.parse(source, filename=relative_path)
				self.assertIn('"dip"', source)


if __name__ == "__main__":
	unittest.main()
