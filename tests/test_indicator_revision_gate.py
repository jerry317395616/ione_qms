from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ione_qms.indicator_engine.calculator import enumerate_dimension_combinations  # noqa: E402
from ione_qms.indicator_engine.publication import (  # noqa: E402
	freeze_indicator_publication_contract,
	verify_indicator_publication_contract,
)


class _Source(dict):
	def get(self, key, default=None):
		return super().get(key, default)


class TestIndicatorPublicationReceipt(TestCase):
	def test_retirement_does_not_change_publication_checksum(self) -> None:
		version = _Source(
			status="Published",
			indicator="IND-1",
			version="1.0.0",
			calculator_key="IONE_RECORD_AGGREGATE",
			calculator_version_snapshot="1",
			calculator_code_hash="a" * 64,
			query_contract_hash="b" * 64,
			physical_query_contract_hash="c" * 64,
			schema_signature_hash="d" * 64,
			effective_from="2026-01-01",
			effective_to=None,
		)
		contract_json, checksum = freeze_indicator_publication_contract(version)
		version.update(
			publication_contract_json=contract_json,
			publication_checksum=checksum,
			checksum=checksum,
		)
		verify_indicator_publication_contract(version)
		version.update(status="Retired", effective_to="2026-12-31")
		verify_indicator_publication_contract(version)
		self.assertEqual(version["checksum"], checksum)
		version["calculator_version_snapshot"] = "2"
		with self.assertRaisesRegex(ValueError, "drifted"):
			verify_indicator_publication_contract(version)


class TestRateDimensionEnumeration(TestCase):
	def test_denominator_only_dimension_is_enumerated_with_opaque_cursor(self) -> None:
		formula = {
			"measure": "Rate",
			"numerator": {"dataset": "quality_findings", "date_field": "detected_at"},
			"denominator": {"dataset": "encounters", "date_field": "admission_time"},
			"dimensions": ["hospital", "campus", "department"],
		}
		lineage = {
			"source_system": "SOURCE-1",
			"mapping_record": "MAP-1",
			"mapping_version": "1",
			"mapping_checksum": "e" * 64,
		}
		physical_contract = {
			"contract_version": "ione-indicator-physical-query-v1",
			"dimensions": ["campus", "department", "hospital"],
			"schema_signature_hash": "f" * 64,
			"roles": {
				"numerator": {
					"dataset": "quality_findings",
					"source_doctype": "IONE QC Finding",
					"date_field": "detected_at",
					"filters": {},
					"dimension_fields": {
						"campus": "campus",
						"department": "department",
						"hospital": "hospital",
					},
					"enumerator_fields": {
						"campus": "campus",
						"department": "department",
						"hospital": "hospital",
					},
					"lineage_fields": {key: key for key in lineage},
				},
				"denominator": {
					"dataset": "encounters",
					"source_doctype": "IONE Encounter Index",
					"date_field": "admission_time",
					"filters": {},
					"dimension_fields": {
						"campus": "campus",
						"department": "department",
						"hospital": "hospital",
					},
					"enumerator_fields": {
						"campus": "campus",
						"department": "department",
						"hospital": "hospital",
					},
					"lineage_fields": {key: key for key in lineage},
				},
			},
		}
		rows = {
			"IONE QC Finding": [
				{
					"name": "F-1",
					"creation": "2026-01-01 00:00:01",
					"detected_at": "2026-01-10 00:00:00",
					"hospital": "H-1",
					"campus": "C-1",
					"department": "DEPT-A",
					**lineage,
				},
				{
					"name": "F-2",
					"creation": "2026-01-01 00:00:02",
					"detected_at": "2026-01-11 00:00:00",
					"hospital": "H-1",
					"campus": "C-1",
					"department": "DEPT-A",
					**lineage,
				},
			],
			"IONE Encounter Index": [
				{
					"name": "E-1",
					"creation": "2026-01-01 00:00:03",
					"admission_time": "2026-01-12 00:00:00",
					"hospital": "H-1",
					"campus": "C-1",
					"department": "DEPT-B",
					**lineage,
				},
				{
					"name": "E-2",
					"creation": "2026-01-01 00:00:04",
					"admission_time": "2026-01-13 00:00:00",
					"hospital": "H-1",
					"campus": "C-1",
					"department": "DEPT-A",
					**lineage,
				},
			],
		}

		def get_all(
			doctype,
			*,
			filters,
			fields=None,
			pluck=None,
			order_by=None,
			limit_page_length=None,
			**_kwargs,
		):
			output = list(rows[doctype])
			conditions = (
				[
					(
						fieldname,
						condition[0],
						condition[1],
					)
					if isinstance(condition, (list, tuple)) and len(condition) == 2
					else (fieldname, "=", condition)
					for fieldname, condition in filters.items()
				]
				if isinstance(filters, dict)
				else [(item[-3], item[-2], item[-1]) for item in filters]
			)
			for fieldname, operator, boundary in conditions:
				if operator == "between":
					lower, upper = (str(item) for item in boundary)
					output = [row for row in output if lower <= str(row.get(fieldname) or "") <= upper]
				elif operator == "=":
					output = [row for row in output if str(row.get(fieldname) or "") == str(boundary)]
				elif operator == ">":
					output = [row for row in output if str(row.get(fieldname) or "") > str(boundary)]
				elif operator == ">=":
					output = [row for row in output if str(row.get(fieldname) or "") >= str(boundary)]
				elif operator == "<":
					output = [row for row in output if str(row.get(fieldname) or "") < str(boundary)]
				elif operator == "<=":
					output = [row for row in output if str(row.get(fieldname) or "") <= str(boundary)]
			for ordering in reversed((order_by or "name asc").split(",")):
				parts = ordering.strip().split()
				fieldname = parts[0]
				reverse = len(parts) > 1 and parts[1].lower() == "desc"
				output.sort(key=lambda row: str(row.get(fieldname) or ""), reverse=reverse)
			if limit_page_length:
				output = output[:limit_page_length]
			if pluck:
				return [row[pluck] for row in output]
			return [
				{fieldname: row.get(fieldname) for fieldname in fields} if fields else dict(row)
				for row in output
			]

		fake_frappe = SimpleNamespace(
			conf={"encryption_key": "test-indicator-dimension-key-0123456789"},
			get_all=get_all,
		)
		with patch.dict(sys.modules, {"frappe": fake_frappe}):
			first, has_more, cursor = enumerate_dimension_combinations(
				formula,
				["hospital", "campus", "department"],
				date(2026, 1, 1),
				date(2026, 1, 31),
				record_cursor=None,
				limit=10,
				lineage=lineage,
				physical_query_contract=physical_contract,
			)
			self.assertEqual(
				first,
				[{"campus": "C-1", "department": "DEPT-A", "hospital": "H-1"}],
			)
			self.assertTrue(has_more)
			self.assertNotIn("E-1", cursor or "")
			with self.assertRaisesRegex(ValueError, "does not match"):
				enumerate_dimension_combinations(
					formula,
					["hospital", "campus", "department"],
					date(2026, 2, 1),
					date(2026, 2, 28),
					record_cursor=cursor,
					limit=10,
					lineage=lineage,
					physical_query_contract=physical_contract,
				)
			second, has_more, cursor = enumerate_dimension_combinations(
				formula,
				["hospital", "campus", "department"],
				date(2026, 1, 1),
				date(2026, 1, 31),
				record_cursor=cursor,
				limit=10,
				lineage=lineage,
				physical_query_contract=physical_contract,
			)
		self.assertEqual(
			second,
			[{"campus": "C-1", "department": "DEPT-B", "hospital": "H-1"}],
		)
		self.assertFalse(has_more)
		self.assertIsNone(cursor)

		# A late row with a lower random/hash name is outside the signed high-water mark.
		with patch.dict(sys.modules, {"frappe": fake_frappe}):
			collected: list[dict[str, str]] = []
			page, has_more, frozen_cursor = enumerate_dimension_combinations(
				formula,
				["hospital", "campus", "department"],
				date(2026, 1, 1),
				date(2026, 1, 31),
				record_cursor=None,
				limit=1,
				lineage=lineage,
				physical_query_contract=physical_contract,
			)
			collected.extend(page)
			self.assertTrue(has_more)
			self.assertIsNotNone(frozen_cursor)
			tampered = (
				frozen_cursor[: len(frozen_cursor) // 2]
				+ ("A" if frozen_cursor[len(frozen_cursor) // 2] != "A" else "B")
				+ frozen_cursor[len(frozen_cursor) // 2 + 1 :]
			)
			with self.assertRaisesRegex(ValueError, "published query contract|invalid"):
				enumerate_dimension_combinations(
					formula,
					["hospital", "campus", "department"],
					date(2026, 1, 1),
					date(2026, 1, 31),
					record_cursor=tampered,
					limit=1,
					lineage=lineage,
					physical_query_contract=physical_contract,
				)
			rows["IONE QC Finding"].append(
				{
					"name": "A-LATE-RANDOM-HASH",
					"creation": "2026-01-01 00:00:10",
					"detected_at": "2026-01-14 00:00:00",
					"hospital": "H-1",
					"campus": "C-1",
					"department": "DEPT-C",
					**lineage,
				}
			)
			for _index in range(10):
				if not has_more:
					break
				page, has_more, frozen_cursor = enumerate_dimension_combinations(
					formula,
					["hospital", "campus", "department"],
					date(2026, 1, 1),
					date(2026, 1, 31),
					record_cursor=frozen_cursor,
					limit=1,
					lineage=lineage,
					physical_query_contract=physical_contract,
				)
				collected.extend(page)
			self.assertFalse(has_more)
			self.assertNotIn(
				{"campus": "C-1", "department": "DEPT-C", "hospital": "H-1"},
				collected,
			)
			fresh, fresh_has_more, fresh_cursor = enumerate_dimension_combinations(
				formula,
				["hospital", "campus", "department"],
				date(2026, 1, 1),
				date(2026, 1, 31),
				record_cursor=None,
				limit=10,
				lineage=lineage,
				physical_query_contract=physical_contract,
			)
			while fresh_has_more:
				page, fresh_has_more, fresh_cursor = enumerate_dimension_combinations(
					formula,
					["hospital", "campus", "department"],
					date(2026, 1, 1),
					date(2026, 1, 31),
					record_cursor=fresh_cursor,
					limit=10,
					lineage=lineage,
					physical_query_contract=physical_contract,
				)
				fresh.extend(page)
			self.assertIn(
				{"campus": "C-1", "department": "DEPT-C", "hospital": "H-1"},
				fresh,
			)


class TestRevisionGateStatic(TestCase):
	def test_revision_publication_consumers_and_disposition_are_wired(self) -> None:
		service = (ROOT / "ione_qms/services/indicators.py").read_text(encoding="utf-8")
		versions = (ROOT / "ione_qms/services/versions.py").read_text(encoding="utf-8")
		generator = (ROOT / "tools/generate_doctypes.py").read_text(encoding="utf-8")
		ai_tools = (ROOT / "ione_qms/ai/tools.py").read_text(encoding="utf-8")
		pdca = (ROOT / "ione_qms/services/pdca_structure.py").read_text(encoding="utf-8")
		reports = (ROOT / "ione_qms/services/analytics_reports.py").read_text(encoding="utf-8")
		analytics = (ROOT / "ione_qms/tasks/analytics.py").read_text(encoding="utf-8")
		indicator_tasks = (ROOT / "ione_qms/tasks/indicators.py").read_text(encoding="utf-8")
		indicator_api = (ROOT / "ione_qms/api/indicator.py").read_text(encoding="utf-8")
		dashboard = (ROOT / "ione_qms/api/dashboard.py").read_text(encoding="utf-8")
		for token in (
			'"result_key": revision_key',
			"_calculation_revision_key",
			"current_input_receipt_hash",
			"physical_query_contract_hash",
			"verify_indicator_publication_contract",
			"_project_current_indicator_result",
			"any immutable historical revision",
			"_INDICATOR_AUDIT_DOCTYPES",
			"request_indicator_quarantine_disposition",
			"review_indicator_quarantine_disposition",
			"verify_indicator_quarantine_disposition",
			"requester and reviewer must be different",
		):
			self.assertIn(token, service)
		self.assertIn("freeze_indicator_publication_contract", versions)
		for token in (
			"physical_query_contract_json",
			"schema_signature_hash",
			"publication_checksum",
		):
			self.assertIn(token, versions)
			self.assertIn(token, generator)
		self.assertIn("current_indicator_result_lock", ai_tools)
		self.assertIn("indicator_artifact_is_governed_excluded", ai_tools)
		self.assertIn("verify_indicator_result_detail_integrity", ai_tools)
		self.assertIn("current_indicator_result_lock", pdca)
		self.assertIn("_retarget_indicator_filters", reports)
		self.assertIn("with current_indicator_result_lock(result_name)", reports)
		self.assertIn("lock_source_epochs", analytics)
		self.assertIn("batch.status = 'Completed'", analytics)
		self.assertIn("newer.recovery_generation > batch.recovery_generation", analytics)
		self.assertIn("result.series_key > %s", analytics)
		self.assertNotIn("and result.name > %s", analytics)
		self.assertIn("continuation budget was exhausted", indicator_tasks)
		self.assertIn("Dimension manifest page omitted its signed continuation", indicator_tasks)
		self.assertIn("Dimension verification page omitted its signed continuation", indicator_tasks)
		self.assertIn("with current_indicator_result_lock(str(row.indicator_result))", dashboard)
		for doctype in (
			"IONE Indicator Quarantine Receipt",
			"IONE Indicator Quarantine Disposition",
		):
			self.assertIn(doctype, generator)
		for doctype in (
			"IONE Indicator Historical Audit Authorization",
			"IONE Indicator Historical Audit Access Receipt",
		):
			self.assertIn(doctype, generator)
		historical_audit = (ROOT / "ione_qms/services/indicator_audit.py").read_text(encoding="utf-8")
		self.assertIn("requester and reviewer must be different named users", historical_audit)
		self.assertIn("MAX_HISTORICAL_AUDIT_SOURCE_RECORDS", historical_audit)
		self.assertIn("_append_access_receipt", historical_audit)
		self.assertIn("_authorized_historical_indicator_reproduction", historical_audit)
		self.assertIn('active_hmac_key("ione_phi_hmac")', historical_audit)
		self.assertIn('verification_hmac_key("ione_phi_hmac"', historical_audit)
		self.assertIn("require_scope_read(", historical_audit)
		self.assertIn("request_historical_indicator_verification", indicator_api)
		self.assertIn("review_historical_indicator_verification", indicator_api)
		self.assertIn('@frappe.whitelist(methods=["POST"])\ndef verify_indicator_result', indicator_api)
		self.assertNotIn("historical.check_permission", indicator_api)
		self.assertIn("_verify_series_revision_continuity", service)
		self.assertIn("exactly contiguous from 1 through N", service)
		self.assertIn("indicator_artifact_is_governed_excluded", service)
		self.assertIn("_lock_result_pointer_for_repair", service)
		for occurrence in ('field(\n\t\t\t\t\t\t"Input Receipt Hash",\n\t\t\t\t\t\t"input_receipt_hash"',):
			start = generator.index(occurrence)
			self.assertNotIn("unique=1", generator[start : start + 180])
