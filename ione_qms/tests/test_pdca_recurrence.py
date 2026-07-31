from __future__ import annotations

from contextlib import contextmanager
from unittest import TestCase
from unittest.mock import patch

import frappe

from ione_qms.services import finding_recurrence, indicators, pdca_structure


class _Record(dict):
	def __getattr__(self, name):
		try:
			return self[name]
		except KeyError as exc:
			raise AttributeError(name) from exc

	def __setattr__(self, name, value) -> None:
		self[name] = value


class TestPDCARecurrenceDeterminism(TestCase):
	def test_policy_checksum_binds_explicit_hospital_parameters(self) -> None:
		base = _Record(
			policy_code="REC-001",
			policy_name="Governed recurrence",
			hospital="HOSP-1",
			campus=None,
			department=None,
			ward=None,
			aggregation_level="Hospital",
			grouping_mode=finding_recurrence.GROUPING_MODE,
			threshold=3,
			window_days=30,
			effective_from="2026-01-01",
			effective_to=None,
		)
		first = finding_recurrence.recurrence_policy_checksum(base)
		base.threshold = 4
		self.assertNotEqual(first, finding_recurrence.recurrence_policy_checksum(base))

	def test_idempotent_recurrence_replay_returns_the_frozen_projects(self) -> None:
		snapshot = [
			{
				"evaluation": "EVALUATION-1",
				"evaluation_key": "a" * 64,
				"group_key": "b" * 64,
				"rule": "RULE-1",
				"occurrence_count": 3,
				"threshold": 3,
				"outcome": "Triggered",
				"finding_set_hash": "c" * 64,
				"project": "PDCA-1",
			}
		]
		doc = _Record(
			name="RUN-1",
			run_key="d" * 64,
			group_count=1,
			finding_count=3,
			triggered_group_count=1,
			project_count=1,
			snapshot_json=finding_recurrence._canonical_json(snapshot),
			snapshot_hash=finding_recurrence._hash_payload(snapshot),
		)
		with patch.object(finding_recurrence.frappe, "get_doc", return_value=doc):
			result = finding_recurrence._existing_run_response(doc.name)
		self.assertEqual(result["projects"], ["PDCA-1"])
		self.assertTrue(result["idempotent"])

		doc.project_count = 0
		with (
			patch.object(finding_recurrence.frappe, "get_doc", return_value=doc),
			self.assertRaises(frappe.ValidationError),
		):
			finding_recurrence._existing_run_response(doc.name)

	def test_five_why_parent_must_be_at_the_immediately_preceding_level(self) -> None:
		doc = _Record(
			root_causes=[
				_Record(
					cause_code="WHY-1",
					analysis_method="Five Why",
					why_level=1,
					parent_cause_code=None,
					category=None,
					cause="First governed cause",
					evidence="Receipt A",
					confirmed=1,
				),
				_Record(
					cause_code="WHY-2",
					analysis_method="Five Why",
					why_level=2,
					parent_cause_code="WHY-1",
					category=None,
					cause="Second governed cause",
					evidence="Receipt B",
					confirmed=1,
				),
			],
			status="Approved",
		)
		pdca_structure._validate_root_causes(doc, None)
		self.assertEqual(len(doc.root_causes[0].evidence_hash), 64)

	def test_pareto_receipt_uses_deterministic_rank_and_cumulative_percentage(self) -> None:
		doc = _Record(
			pareto_items=[
				_Record(
					cause_code="A",
					category="Process",
					cause="Cause A",
					occurrence_count=3,
					rank=1,
					cumulative_count=3,
					cumulative_percent=75,
				),
				_Record(
					cause_code="B",
					category="People",
					cause="Cause B",
					occurrence_count=1,
					rank=2,
					cumulative_count=4,
					cumulative_percent=100,
				),
			],
			status="Approved",
		)
		pdca_structure._validate_pareto(doc, None)

	def test_effect_hash_binds_sources_period_values_scope_and_lineage(self) -> None:
		project = _Record(
			name="PDCA-1",
			hospital="HOSP-1",
			campus="CAMP-1",
			department="DEPT-1",
			ward=None,
		)
		measurement = _Record(
			measurement_code="M-1",
			indicator="IND-1",
			baseline_source="RESULT-BASE",
			actual_source="RESULT-ACTUAL",
			baseline_period_start="2026-01-01",
			baseline_period_end="2026-01-31",
			measurement_period_start="2026-02-01",
			measurement_period_end="2026-02-28",
			baseline_value=10,
			target_value=8,
			actual_value=7,
		)

		def result(name):
			is_baseline = name == "RESULT-BASE"
			return _Record(
				name=name,
				result_key=f"KEY-{name}",
				indicator="IND-1",
				indicator_version="IND-V1",
				period_start="2026-01-01" if is_baseline else "2026-02-01",
				period_end="2026-01-31" if is_baseline else "2026-02-28",
				indicator_value=10 if is_baseline else 7,
				status="Calculated",
				hospital="HOSP-1",
				campus="CAMP-1",
				department="DEPT-1",
				ward=None,
				calculator_key="CALC-1",
				calculator_version="1",
				lineage_json='{"source":"governed"}',
			)

		with patch.object(pdca_structure, "_indicator_result_snapshot", side_effect=result):
			source_hash, measurement_hash = pdca_structure._validated_effect_hashes(
				project,
				measurement,
			)
		self.assertEqual(len(source_hash), 64)
		self.assertEqual(len(measurement_hash), 64)

	def test_verified_effect_receipt_no_longer_reopens_current_indicator_pointers(self) -> None:
		project = _Record(
			name="PDCA-1",
			hospital="HOSP-1",
			campus="CAMP-1",
			department="DEPT-1",
			ward=None,
		)
		measurement = _Record(
			measurement_code="M-1",
			indicator="IND-1",
			baseline_source="RESULT-BASE-R1",
			actual_source="RESULT-ACTUAL-R1",
			baseline_period_start="2026-01-01",
			baseline_period_end="2026-01-31",
			measurement_period_start="2026-02-01",
			measurement_period_end="2026-02-28",
			baseline_value=10,
			target_value=8,
			actual_value=7,
			source_snapshot_hash="a" * 64,
			verified_by="reviewer@example.com",
			verified_at="2026-03-01 09:00:00",
			verification_comment="Independent governed verification",
		)
		binding = pdca_structure._validated_effect_binding(measurement)
		measurement.measurement_hash = pdca_structure._effect_measurement_hash(
			binding,
			measurement.source_snapshot_hash,
		)
		with patch.object(
			pdca_structure,
			"_indicator_result_snapshot",
			side_effect=AssertionError("historical evidence attempted a current-pointer read"),
		):
			pdca_structure._validate_complete_effect_row(
				project,
				measurement,
				require_verified=True,
				resolve_current_sources=False,
			)
		measurement.actual_value = 6
		with self.assertRaises(frappe.ValidationError):
			pdca_structure._validate_complete_effect_row(
				project,
				measurement,
				require_verified=True,
				resolve_current_sources=False,
			)

	def test_multi_result_lock_acquires_series_in_global_order(self) -> None:
		candidates = {
			"RESULT-A": _Record(name="RESULT-A", series_key="b" * 64),
			"RESULT-B": _Record(name="RESULT-B", series_key="a" * 64),
		}
		acquired: list[str] = []

		@contextmanager
		def lock_series(series_key: str):
			acquired.append(series_key)
			name = "RESULT-B" if series_key == "a" * 64 else "RESULT-A"
			yield _Record(current_result=name), candidates[name]

		with (
			patch.object(indicators.frappe, "get_doc", side_effect=lambda _doctype, name: candidates[name]),
			patch.object(indicators, "current_indicator_series_lock", side_effect=lock_series),
			indicators.current_indicator_results_lock(["RESULT-A", "RESULT-B"]) as locked,
		):
			self.assertEqual(set(locked), {"RESULT-A", "RESULT-B"})

		self.assertEqual(acquired, ["a" * 64, "b" * 64])

	def test_effect_hash_keeps_business_baseline_actual_order_with_prelocked_sources(self) -> None:
		project = _Record(
			name="PDCA-1",
			hospital="HOSP-1",
			campus="CAMP-1",
			department="DEPT-1",
			ward=None,
		)
		measurement = _Record(
			measurement_code="M-1",
			indicator="IND-1",
			baseline_source="RESULT-Z",
			actual_source="RESULT-A",
			baseline_period_start="2026-01-01",
			baseline_period_end="2026-01-31",
			measurement_period_start="2026-02-01",
			measurement_period_end="2026-02-28",
			baseline_value=10,
			target_value=8,
			actual_value=7,
		)
		baseline = {
			"name": "RESULT-Z",
			"indicator": "IND-1",
			"period_start": "2026-01-01",
			"period_end": "2026-01-31",
			"indicator_value": "10",
			"hospital": "HOSP-1",
			"campus": "CAMP-1",
			"department": "DEPT-1",
			"ward": "",
		}
		actual = {
			"name": "RESULT-A",
			"indicator": "IND-1",
			"period_start": "2026-02-01",
			"period_end": "2026-02-28",
			"indicator_value": "7",
			"hospital": "HOSP-1",
			"campus": "CAMP-1",
			"department": "DEPT-1",
			"ward": "",
		}
		source_hash, _measurement_hash = pdca_structure._validated_effect_hashes(
			project,
			measurement,
			source_snapshots={"RESULT-A": actual, "RESULT-Z": baseline},
		)
		self.assertEqual(
			source_hash,
			pdca_structure._hash_payload({"baseline": baseline, "actual": actual}),
		)
