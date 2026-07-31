from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import patch


def _module(name: str, **attributes) -> ModuleType:
	module = ModuleType(name)
	for fieldname, value in attributes.items():
		setattr(module, fieldname, value)
	return module


def _indicator_manifest_binding() -> dict:
	return {
		"artifact_manifest_format": "ione-indicator-artifact-manifest-v1",
		"artifact_manifest_count": 4,
		"artifact_manifest_hash": "a" * 64,
		"artifact_high_watermark": "2026-07-30 11:59:59",
	}


def _completion_summary_json() -> str:
	return json.dumps(_completion_summary(), sort_keys=True, separators=(",", ":"))


def _completion_summary(binding: dict | None = None) -> dict:
	return {
		"artifact_bindings": {
			"indicator_artifacts": binding or _indicator_manifest_binding(),
		},
		"batches": 1,
		"last_batch": {},
		"totals": {},
	}


def _completion_receipt_key(migration_state) -> str:
	return migration_state._completion_receipt_key(json.loads(_completion_summary_json()))


class _Database:
	def __init__(self) -> None:
		self.commits = 0
		self.rollbacks = 0

	@contextmanager
	def advisory_lock(self, _key: str, *, timeout: int):
		assert timeout == 0
		yield

	def commit(self) -> None:
		self.commits += 1

	def rollback(self, **_kwargs) -> None:
		self.rollbacks += 1


class _MigrationState:
	def __init__(self, *, already_complete: bool = False) -> None:
		self.already_complete = already_complete
		self.batches: list[dict] = []
		self.blocked = 0
		self.failed: list[str] = []
		self.pending_sequence = 6
		self.completed = 0
		self.running = 0

	def initialize(self) -> bool:
		return True

	def is_complete(self) -> bool:
		return self.already_complete

	def mark_running(self) -> None:
		self.running += 1

	def record_batch(self, summary: dict) -> int:
		self.batches.append(summary)
		return len(self.batches)

	def mark_blocked(self) -> None:
		self.blocked += 1

	def mark_failed(self, code: str) -> None:
		self.failed.append(code)

	def mark_pending(self) -> int:
		self.pending_sequence += 1
		return self.pending_sequence

	def complete(self) -> str:
		self.completed += 1
		return "ione-qms:test-schema"


@contextmanager
def _load_migrations(*, already_complete: bool = False):
	database = _Database()
	state = _MigrationState(already_complete=already_complete)
	enqueued: list[dict] = []
	frappe = _module(
		"frappe",
		db=database,
		enqueue=lambda *args, **kwargs: enqueued.append({"args": args, **kwargs}),
		cache=SimpleNamespace(make_key=lambda value: value, set=lambda **_kwargs: True),
		log_error=lambda **_kwargs: None,
		get_all=lambda *_args, **_kwargs: [],
		get_meta=lambda _doctype: SimpleNamespace(has_field=lambda _fieldname: True),
		DuplicateEntryError=type("DuplicateEntryError", (Exception,), {}),
		UniqueValidationError=type("UniqueValidationError", (Exception,), {}),
	)
	migration_state = _module(
		"ione_qms.services.migration_state",
		MIGRATION_KEY="ione-qms:test-schema",
		SCHEMA_REVISION="test-schema",
		complete_migration=state.complete,
		current_migration_completion_receipt=lambda: "ione-qms:test-schema:epoch",
		initialize_migration_state=state.initialize,
		mark_migration_blocked=state.mark_blocked,
		mark_migration_failed=state.mark_failed,
		mark_migration_pending=state.mark_pending,
		mark_migration_running=state.mark_running,
		migration_is_complete=state.is_complete,
		record_migration_batch=state.record_batch,
	)
	stubs = {
		"frappe": frappe,
		"ione_qms.services.identity_keys": _module(
			"ione_qms.services.identity_keys",
			backfill_site_identity_keys=lambda **_kwargs: {
				"updated": 0,
				"blocked": 0,
				"has_more": False,
			},
		),
		"ione_qms.services.indicators": _module(
			"ione_qms.services.indicators",
			quarantine_legacy_indicator_receipts=lambda **_kwargs: {
				"quarantined": 0,
				"blocked": 0,
				"has_more": False,
				**_indicator_manifest_binding(),
			},
		),
		"ione_qms.services.migration_state": migration_state,
		"ione_qms.services.scope_hierarchy": _module(
			"ione_qms.services.scope_hierarchy",
			audit_scope_integrity=lambda **_kwargs: {
				"audited": 0,
				"quarantined": 0,
				"has_more": False,
			},
		),
		"ione_qms.services.versions": _module(
			"ione_qms.services.versions",
			backfill_active_version_keys=lambda **_kwargs: {
				"updated": 0,
				"blocked": 0,
				"has_more": False,
			},
			backfill_definition_lineage=lambda **_kwargs: {
				"updated": 0,
				"blocked": 0,
				"has_more": False,
			},
		),
		"ione_qms.setup.install": _module(
			"ione_qms.setup.install",
			backfill_finding_due_dates=lambda _limit: 0,
		),
	}
	original = {name: sys.modules.get(name) for name in stubs}
	try:
		sys.modules.update(stubs)
		path = Path(__file__).resolve().parents[1] / "tasks" / "migrations.py"
		spec = importlib.util.spec_from_file_location("_ione_migration_gate_test", path)
		assert spec and spec.loader
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		module._backfill_active_key = lambda *_args, **_kwargs: {
			"updated": 0,
			"blocked": 0,
			"has_more": False,
		}
		yield module, database, state, enqueued
	finally:
		for name, previous in original.items():
			if previous is None:
				sys.modules.pop(name, None)
			else:
				sys.modules[name] = previous


class TestMigrationCompletionGate(TestCase):
	def test_multiple_batches_converge_before_receipt(self) -> None:
		with _load_migrations() as (migrations, database, state, enqueued):
			calls = 0

			def tenant_boundary(_limit: int):
				nonlocal calls
				calls += 1
				return {
					"namespaced": 1 if calls == 1 else 0,
					"blocked": 0,
					"has_more": calls == 1,
				}

			migrations.enforce_integration_tenant_fail_closed = tenant_boundary
			result = migrations.run_post_migrate_backfills(batch_size=25, max_passes=4)

			self.assertEqual(result["status"], "Completed")
			self.assertEqual(result["passes"], 2)
			self.assertEqual(len(state.batches), 2)
			self.assertEqual(state.completed, 1)
			self.assertEqual(state.blocked, 0)
			self.assertEqual(enqueued, [])
			self.assertEqual(database.commits, 3)

	def test_unfinished_work_enqueues_revision_scoped_continuation(self) -> None:
		with _load_migrations() as (migrations, database, state, enqueued):
			migrations.enforce_integration_tenant_fail_closed = lambda _limit: {
				"namespaced": 1,
				"blocked": 0,
				"has_more": True,
			}
			result = migrations.run_post_migrate_backfills(batch_size=33, max_passes=2)

			self.assertEqual(result["status"], "Pending")
			self.assertEqual(result["continuation_sequence"], 7)
			self.assertEqual(state.completed, 0)
			self.assertEqual(len(enqueued), 1)
			self.assertEqual(
				enqueued[0]["job_id"],
				"ione-qms:post-migrate:test-schema:7",
			)
			self.assertTrue(enqueued[0]["enqueue_after_commit"])
			self.assertEqual(enqueued[0]["batch_size"], 33)
			self.assertEqual(database.commits, 4)

	def test_blocked_rows_prevent_receipt_and_continuation(self) -> None:
		with _load_migrations() as (migrations, database, state, enqueued):
			migrations.enforce_integration_tenant_fail_closed = lambda _limit: {
				"namespaced": 0,
				"blocked": 1,
				"has_more": False,
			}
			result = migrations.run_post_migrate_backfills(max_passes=5)

			self.assertEqual(result["status"], "Blocked")
			self.assertEqual(state.blocked, 1)
			self.assertEqual(state.completed, 0)
			self.assertEqual(enqueued, [])
			self.assertEqual(database.commits, 2)

	def test_verified_existing_receipt_short_circuits_repairs(self) -> None:
		with _load_migrations(already_complete=True) as (
			migrations,
			database,
			state,
			enqueued,
		):
			migrations.enforce_integration_tenant_fail_closed = lambda _limit: self.fail(
				"completed migration must not rerun finite repair"
			)
			result = migrations.run_post_migrate_backfills()

			self.assertEqual(result["status"], "Completed")
			self.assertEqual(state.running, 0)
			self.assertEqual(state.batches, [])
			self.assertEqual(enqueued, [])
			self.assertEqual(database.commits, 1)


class _Receipt(dict):
	def __init__(self, **values) -> None:
		super().__init__(values)
		self.expected_hash = values.get("expected_hash")


@contextmanager
def _load_migration_state():
	frappe = _module("frappe")
	frappe_utils = _module("frappe.utils", now_datetime=lambda: "2026-07-30 12:00:00")
	immutability = _module(
		"ione_qms.services.immutability",
		canonical_record_hash=lambda doc: doc.expected_hash,
		validate_append_only=lambda _doc: None,
	)
	stubs = {
		"frappe": frappe,
		"frappe.utils": frappe_utils,
		"ione_qms.services.immutability": immutability,
	}
	original = {name: sys.modules.get(name) for name in stubs}
	try:
		sys.modules.update(stubs)
		path = Path(__file__).resolve().parents[1] / "services" / "migration_state.py"
		spec = importlib.util.spec_from_file_location("_ione_migration_state_test", path)
		assert spec and spec.loader
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		yield module
	finally:
		for name, previous in original.items():
			if previous is None:
				sys.modules.pop(name, None)
			else:
				sys.modules[name] = previous


@contextmanager
def _load_indicator_manifest(rows_by_doctype: dict[str, list[dict]]):
	state_values: dict[str, object] = {"_get_all_calls": []}

	def filtered_rows(rows: list[dict], filters) -> list[dict]:
		if not filters:
			return list(rows)
		if isinstance(filters, dict):
			conditions = []
			for fieldname, condition in filters.items():
				if isinstance(condition, (list, tuple)) and len(condition) == 2:
					conditions.append([fieldname, condition[0], condition[1]])
				else:
					conditions.append([fieldname, "=", condition])
		else:
			conditions = filters

		def matches(row: dict, condition) -> bool:
			fieldname, operator, expected = condition[-3:]
			value = row.get(fieldname)
			is_missing = value is None or value == ""
			if operator == "is":
				return is_missing if expected == "not set" else not is_missing
			if operator == "=":
				return value == expected
			if is_missing:
				return False
			if operator == "<=":
				return str(value) <= str(expected)
			if operator == ">":
				return str(value) > str(expected)
			raise AssertionError(f"Unsupported test filter operator: {operator}")

		return [row for row in rows if all(matches(row, condition) for condition in conditions)]

	class Database:
		@staticmethod
		def get_single_value(_doctype, fieldname):
			return state_values.get(fieldname)

		@staticmethod
		def set_single_value(_doctype, fieldname, value):
			state_values[fieldname] = value

		@staticmethod
		def count(doctype, filters=None):
			return len(filtered_rows(rows_by_doctype.get(doctype, []), filters))

	frappe = _module(
		"frappe",
		db=Database(),
		DoesNotExistError=type("DoesNotExistError", (Exception,), {}),
		ValidationError=type("ValidationError", (Exception,), {}),
	)

	def get_all(
		doctype,
		*,
		fields=None,
		filters=None,
		pluck=None,
		limit_start=0,
		limit_page_length=0,
		**_kwargs,
	):
		state_values["_get_all_calls"].append(
			{
				"doctype": doctype,
				"limit_page_length": limit_page_length,
				"limit_start": limit_start,
			}
		)
		rows = sorted(
			rows_by_doctype.get(doctype, []),
			key=lambda row: (str(row.get("creation") or ""), str(row.get("name") or "")),
		)
		rows = filtered_rows(rows, filters)
		end = limit_start + limit_page_length if limit_page_length else None
		rows = rows[limit_start:end]
		if pluck:
			return [row.get(pluck) for row in rows]
		return [{fieldname: row.get(fieldname) for fieldname in (fields or ["name"])} for row in rows]

	class Doc(dict):
		def __init__(self, doctype: str, values: dict):
			super().__init__(values)
			self.doctype = doctype
			self.name = str(values.get("name") or "")

	def get_doc(doctype, name):
		for row in rows_by_doctype.get(doctype, []):
			if str(row.get("name") or "") == str(name):
				return Doc(doctype, row)
		raise frappe.DoesNotExistError(name)

	frappe.get_all = get_all
	frappe.get_doc = get_doc
	frappe_utils = _module(
		"frappe.utils",
		add_days=lambda *args: args[0] if args else None,
		get_first_day=lambda value: value,
		get_last_day=lambda value: value,
		getdate=lambda value: value,
		now_datetime=lambda: "2026-07-30 12:00:00",
	)
	indicator_engine = _module(
		"ione_qms.indicator_engine",
		IndicatorContext=type("IndicatorContext", (), {}),
		IndicatorResult=type("IndicatorResult", (), {}),
		calculator_code_hash=lambda *_args, **_kwargs: "",
		canonical_dimension=lambda value: value,
		get_indicator=lambda *_args, **_kwargs: None,
		governed_query_contract_hash=lambda *_args, **_kwargs: "",
		normalize_dimension_names=lambda value: value,
		normalize_dimension_values=lambda value: value,
		physical_query_contract_hash=lambda *_args, **_kwargs: "",
		resolved_physical_query_contract=lambda *_args, **_kwargs: {},
		verify_indicator_publication_contract=lambda *_args, **_kwargs: None,
	)
	stubs = {
		"frappe": frappe,
		"frappe.utils": frappe_utils,
		"ione_qms.indicator_engine": indicator_engine,
	}
	original = {name: sys.modules.get(name) for name in stubs}
	try:
		sys.modules.update(stubs)
		path = Path(__file__).resolve().parents[1] / "services" / "indicators.py"
		spec = importlib.util.spec_from_file_location("_ione_indicator_manifest_test", path)
		assert spec and spec.loader
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		yield module, state_values
	finally:
		for name, previous in original.items():
			if previous is None:
				sys.modules.pop(name, None)
			else:
				sys.modules[name] = previous


class TestIndicatorArtifactManifest(TestCase):
	def _rows(self) -> dict[str, list[dict]]:
		return {
			"IONE Indicator Calculation": [
				{
					"name": "CALC-B",
					"creation": "2026-07-29 09:00:00",
					"receipt_checksum": "b" * 64,
				},
			],
			"IONE Indicator Result": [
				{
					"name": "RESULT-C",
					"creation": "2026-07-29 09:01:00",
					"result_checksum": "c" * 64,
				},
			],
			"IONE Indicator Result Detail": [],
			"IONE Indicator Result Pointer": [],
		}

	def test_lower_sorting_restore_changes_snapshot_and_cannot_hide_behind_cursor(self) -> None:
		rows = self._rows()
		with _load_indicator_manifest(rows) as (indicators, state):
			binding = indicators._indicator_artifact_manifest("2026-07-30 11:59:59")
			population, _has_more, _offset = indicators._indicator_audit_page(
				"2026-07-30 11:59:59",
				0,
				10,
			)
			cursor = indicators._indicator_verification_cursor_value(binding, 1)
			state["indicator_verification_cursor"] = cursor
			parsed_binding, offset = indicators._indicator_verification_cursor()
			self.assertEqual(offset, 1)
			self.assertNotIn("CALC-B", cursor)

			rows["IONE Indicator Calculation"].append(
				{
					"name": "CALC-A",
					"creation": "2026-07-28 09:00:00",
					"receipt_checksum": "a" * 64,
				}
			)
			changed = indicators._indicator_artifact_manifest("2026-07-30 11:59:59")
			changed_population, _has_more, _offset = indicators._indicator_audit_page(
				"2026-07-30 11:59:59",
				0,
				10,
			)
			self.assertFalse(indicators._indicator_manifest_bindings_equal(parsed_binding, changed))
			self.assertEqual(changed_population[0], ("IONE Indicator Calculation", "CALC-A"))
			self.assertEqual(len(population) + 1, len(changed_population))

	def test_missing_creation_is_bound_and_audited_exactly_once(self) -> None:
		rows = self._rows()
		rows["IONE Indicator Calculation"].append(
			{
				"name": "CALC-NULL-CREATION",
				"creation": None,
				"receipt_checksum": "0" * 64,
			}
		)
		with _load_indicator_manifest(rows) as (indicators, _state):
			binding = indicators._indicator_artifact_manifest("2026-07-30 11:59:59")
			population, has_more, offset = indicators._indicator_audit_page(
				"2026-07-30 11:59:59",
				0,
				10,
			)

			self.assertEqual(binding["artifact_manifest_count"], 3)
			self.assertEqual(population.count(("IONE Indicator Calculation", "CALC-NULL-CREATION")), 1)
			self.assertEqual(len(population), 3)
			self.assertFalse(has_more)
			self.assertEqual(offset, 3)

	def test_post_watermark_valid_artifacts_are_allowed_but_legacy_artifacts_close_gate(self) -> None:
		rows = self._rows()
		with _load_indicator_manifest(rows) as (indicators, _state):
			binding = indicators._indicator_artifact_manifest("2026-07-30 11:59:59")
			rows["IONE Indicator Result"].append(
				{
					"name": "RESULT-NEW",
					"creation": "2026-07-30 12:01:00",
					"result_checksum": "d" * 64,
				}
			)
			with (
				patch.object(
					indicators,
					"_post_watermark_indicator_artifacts_are_governed",
					return_value=True,
				),
				patch.object(
					indicators,
					"_prefix_mutable_indicator_artifacts_are_governed",
					return_value=True,
				),
				patch.object(
					indicators,
					"_unresolved_indicator_quarantine_count",
					return_value=0,
				),
			):
				self.assertTrue(indicators.indicator_artifact_manifest_matches(binding))
			with (
				patch.object(
					indicators,
					"_post_watermark_indicator_artifacts_are_governed",
					return_value=False,
				),
				patch.object(
					indicators,
					"_prefix_mutable_indicator_artifacts_are_governed",
					return_value=True,
				),
				patch.object(
					indicators,
					"_unresolved_indicator_quarantine_count",
					return_value=0,
				),
			):
				self.assertFalse(indicators.indicator_artifact_manifest_matches(binding))

	def test_payload_tamper_with_unchanged_stored_checksum_changes_manifest(self) -> None:
		rows = self._rows()
		rows["IONE Indicator Calculation"][0]["input_receipt_json"] = '{"source":"before"}'
		with _load_indicator_manifest(rows) as (indicators, _state):
			before = indicators._indicator_artifact_manifest("2026-07-30 11:59:59")
			rows["IONE Indicator Calculation"][0]["input_receipt_json"] = '{"source":"tampered"}'
			after = indicators._indicator_artifact_manifest("2026-07-30 11:59:59")
			self.assertFalse(indicators._indicator_manifest_bindings_equal(before, after))
			self.assertEqual(
				rows["IONE Indicator Calculation"][0]["receipt_checksum"],
				"b" * 64,
			)

	def test_manifest_capture_and_audit_page_keep_database_pages_bounded(self) -> None:
		rows = self._rows()
		rows["IONE Indicator Result"] = [
			{
				"name": f"RESULT-{index:04d}",
				"creation": "2026-07-29 09:01:00",
				"result_checksum": f"{index:064x}"[-64:],
			}
			for index in range(1_201)
		]
		with _load_indicator_manifest(rows) as (indicators, state):
			binding = indicators._indicator_artifact_manifest("2026-07-30 11:59:59")
			page, has_more, offset = indicators._indicator_audit_page(
				"2026-07-30 11:59:59",
				0,
				37,
			)
			self.assertEqual(binding["artifact_manifest_count"], 1_202)
			self.assertEqual(len(page), 37)
			self.assertTrue(has_more)
			self.assertEqual(offset, 37)
			scan_calls = state["_get_all_calls"]
			self.assertGreaterEqual(
				sum(
					1
					for call in scan_calls
					if call["doctype"] == "IONE Indicator Result"
					and call["limit_page_length"] == indicators._INDICATOR_MANIFEST_SCAN_PAGE_SIZE
				),
				3,
			)
			self.assertTrue(
				all(
					not call["limit_page_length"]
					or call["limit_page_length"] <= indicators._INDICATOR_MANIFEST_SCAN_PAGE_SIZE
					for call in scan_calls
				)
			)

	def test_prefix_pointer_is_live_verified_while_its_governed_advance_keeps_manifest_stable(
		self,
	) -> None:
		rows = self._rows()
		rows["IONE Indicator Result Pointer"].append(
			{
				"name": "POINTER-1",
				"creation": "2026-07-29 09:02:00",
				"current_result": "RESULT-C",
				"current_revision": 1,
				"pointer_checksum": "e" * 64,
			}
		)
		with _load_indicator_manifest(rows) as (indicators, _state):
			binding = indicators._indicator_artifact_manifest("2026-07-30 11:59:59")
			rows["IONE Indicator Result Pointer"][0].update(
				{
					"current_result": "RESULT-D",
					"current_revision": 2,
					"pointer_checksum": "f" * 64,
				}
			)
			advanced = indicators._indicator_artifact_manifest("2026-07-30 11:59:59")
			self.assertTrue(indicators._indicator_manifest_bindings_equal(binding, advanced))
			with (
				patch.object(
					indicators,
					"_indicator_artifact_is_governed_or_excluded",
					return_value=True,
				),
				patch.object(
					indicators,
					"_post_watermark_indicator_artifacts_are_governed",
					return_value=True,
				),
				patch.object(
					indicators,
					"_unresolved_indicator_quarantine_count",
					return_value=0,
				),
			):
				self.assertTrue(indicators.indicator_artifact_manifest_matches(binding))
			with (
				patch.object(
					indicators,
					"_indicator_artifact_is_governed_or_excluded",
					return_value=False,
				),
				patch.object(
					indicators,
					"_post_watermark_indicator_artifacts_are_governed",
					return_value=True,
				),
				patch.object(
					indicators,
					"_unresolved_indicator_quarantine_count",
					return_value=0,
				),
			):
				self.assertFalse(indicators.indicator_artifact_manifest_matches(binding))

	def test_unresolved_quarantine_closes_an_exact_manifest_match(self) -> None:
		rows = self._rows()
		with _load_indicator_manifest(rows) as (indicators, _state):
			binding = indicators._indicator_artifact_manifest("2026-07-30 11:59:59")
			with (
				patch.object(
					indicators,
					"_unresolved_indicator_quarantine_count",
					return_value=1,
				),
				patch.object(
					indicators,
					"_prefix_mutable_indicator_artifacts_are_governed",
					return_value=True,
				),
				patch.object(
					indicators,
					"_post_watermark_indicator_artifacts_are_governed",
					return_value=True,
				),
			):
				self.assertFalse(indicators.indicator_artifact_manifest_matches(binding))

	def test_final_audit_can_finish_blocked_for_an_exact_unresolved_mutable_quarantine(
		self,
	) -> None:
		rows = self._rows()
		with _load_indicator_manifest(rows) as (indicators, _state):
			binding = indicators._indicator_artifact_manifest("2026-07-30 11:59:59")
			with (
				patch.object(
					indicators,
					"_unresolved_indicator_quarantine_count",
					return_value=1,
				),
				patch.object(
					indicators,
					"_verify_indicator_artifact",
					side_effect=ValueError("legacy"),
				),
				patch.object(
					indicators,
					"_approved_indicator_disposition_exists",
					return_value=False,
				),
				patch.object(
					indicators,
					"_exact_indicator_quarantine_receipt_exists",
					return_value=True,
				),
			):
				self.assertTrue(
					indicators.indicator_artifact_manifest_matches(
						binding,
						require_resolved=False,
					)
				)
				self.assertFalse(indicators.indicator_artifact_manifest_matches(binding))

	def test_two_connection_late_commit_closes_old_epoch_and_mints_a_new_receipt_identity(
		self,
	) -> None:
		rows = self._rows()
		writer_ready = threading.Event()
		allow_commit = threading.Event()

		def writer_connection() -> None:
			pending = {
				"name": "RESULT-LATE",
				"creation": "2026-07-30 11:58:00",
				"result_checksum": "9" * 64,
				"input_receipt_json": '{"late":true}',
			}
			writer_ready.set()
			self.assertTrue(allow_commit.wait(5))
			rows["IONE Indicator Result"].append(pending)

		with (
			_load_indicator_manifest(rows) as (indicators, _state),
			_load_migration_state() as migration_state,
		):
			writer = threading.Thread(target=writer_connection)
			writer.start()
			self.assertTrue(writer_ready.wait(5))
			first = indicators._indicator_artifact_manifest("2026-07-30 11:59:59")
			first_receipt = migration_state._completion_receipt_key(_completion_summary(first))
			with (
				patch.object(
					indicators,
					"_unresolved_indicator_quarantine_count",
					return_value=0,
				),
				patch.object(
					indicators,
					"_prefix_mutable_indicator_artifacts_are_governed",
					return_value=True,
				),
				patch.object(
					indicators,
					"_post_watermark_indicator_artifacts_are_governed",
					return_value=True,
				),
			):
				self.assertTrue(indicators.indicator_artifact_manifest_matches(first))
				allow_commit.set()
				writer.join(5)
				self.assertFalse(writer.is_alive())
				self.assertFalse(indicators.indicator_artifact_manifest_matches(first))

			second = indicators._indicator_artifact_manifest("2026-07-30 12:05:00")
			second_receipt = migration_state._completion_receipt_key(_completion_summary(second))
			self.assertNotEqual(first_receipt, second_receipt)
			self.assertTrue(first_receipt.startswith(migration_state.MIGRATION_KEY + ":"))
			self.assertTrue(second_receipt.startswith(migration_state.MIGRATION_KEY + ":"))


class TestMigrationReceiptVerification(TestCase):
	def test_assertion_raises_when_state_or_receipt_is_not_ready(self) -> None:
		with _load_migration_state() as migration_state:
			migration_state.migration_is_complete = lambda: False

			def reject(message: str):
				raise RuntimeError(message)

			migration_state.frappe.throw = reject
			with self.assertRaisesRegex(RuntimeError, "cannot accept the CI candidate"):
				migration_state.assert_migration_complete("accept the CI candidate")

	def test_tampered_receipt_hash_is_rejected(self) -> None:
		with _load_migration_state() as migration_state:
			summary = _completion_summary_json()
			receipt_key = _completion_receipt_key(migration_state)
			receipt = _Receipt(
				name=receipt_key,
				migration_key=receipt_key,
				schema_revision=migration_state.SCHEMA_REVISION,
				full_fingerprint=migration_state.FULL_FINGERPRINT,
				app_version=migration_state.__version__,
				completed_at="2026-07-30 12:00:00",
				batch_count=1,
				summary_json=summary,
				record_hash="verified",
				expected_hash="verified",
			)
			self.assertTrue(migration_state.verify_completion_receipt(receipt))
			receipt["record_hash"] = "tampered"
			self.assertFalse(migration_state.verify_completion_receipt(receipt))

	def test_old_fingerprint_and_app_version_are_rejected(self) -> None:
		with _load_migration_state() as migration_state:
			receipt_key = _completion_receipt_key(migration_state)
			base = {
				"name": receipt_key,
				"migration_key": receipt_key,
				"schema_revision": migration_state.SCHEMA_REVISION,
				"full_fingerprint": migration_state.FULL_FINGERPRINT,
				"app_version": migration_state.__version__,
				"completed_at": "2026-07-30 12:00:00",
				"batch_count": 1,
				"summary_json": _completion_summary_json(),
				"record_hash": "verified",
				"expected_hash": "verified",
			}
			old_fingerprint = _Receipt(**{**base, "full_fingerprint": "0" * 64})
			self.assertFalse(migration_state.verify_completion_receipt(old_fingerprint))
			old_version = _Receipt(**{**base, "app_version": "0.0.0-old"})
			self.assertFalse(migration_state.verify_completion_receipt(old_version))

	def test_batch_summary_binds_exact_indicator_manifest_without_summing_its_count(self) -> None:
		with _load_migration_state() as migration_state:
			current = {
				"indicator_receipts": {
					"verified": 4,
					"blocked": 0,
					"has_more": False,
					**_indicator_manifest_binding(),
				}
			}
			sanitized = migration_state._sanitized_summary(current)
			accumulated = migration_state._accumulate_summary({}, sanitized, 1)

			self.assertEqual(
				accumulated["artifact_bindings"]["indicator_artifacts"],
				_indicator_manifest_binding(),
			)
			self.assertEqual(accumulated["totals"]["indicator_receipts"]["verified"], 4)
			self.assertNotIn(
				"artifact_manifest_count",
				accumulated["totals"]["indicator_receipts"],
			)

	def test_receipt_without_indicator_manifest_binding_is_rejected(self) -> None:
		with _load_migration_state() as migration_state:
			receipt = _Receipt(
				name=migration_state.MIGRATION_KEY,
				migration_key=migration_state.MIGRATION_KEY,
				schema_revision=migration_state.SCHEMA_REVISION,
				full_fingerprint=migration_state.FULL_FINGERPRINT,
				app_version=migration_state.__version__,
				completed_at="2026-07-30 12:00:00",
				batch_count=1,
				summary_json='{"batches":1,"last_batch":{},"totals":{}}',
				record_hash="verified",
				expected_hash="verified",
			)
			self.assertFalse(migration_state.verify_completion_receipt(receipt))

	def test_live_manifest_mismatch_closes_an_otherwise_valid_completed_gate(self) -> None:
		with _load_migration_state() as migration_state:
			summary_json = _completion_summary_json()
			receipt_key = _completion_receipt_key(migration_state)
			receipt = _Receipt(
				name=receipt_key,
				migration_key=receipt_key,
				schema_revision=migration_state.SCHEMA_REVISION,
				full_fingerprint=migration_state.FULL_FINGERPRINT,
				app_version=migration_state.__version__,
				completed_at="2026-07-30 12:00:00",
				batch_count=1,
				summary_json=summary_json,
				record_hash="verified",
				expected_hash="verified",
			)
			state_values = {
				"schema_revision": migration_state.SCHEMA_REVISION,
				"full_fingerprint": migration_state.FULL_FINGERPRINT,
				"status": "Completed",
				"completion_receipt": receipt_key,
				"batch_count": 1,
				"last_summary_json": summary_json,
				"completed_at": "2026-07-30 12:00:00",
			}

			class Database:
				@staticmethod
				def exists(_doctype, _name):
					return True

				@staticmethod
				def get_single_value(_doctype, fieldname):
					return state_values.get(fieldname)

			migration_state.frappe.db = Database()
			migration_state.frappe.get_doc = lambda _doctype, _name: receipt
			live = {"matches": True}
			indicator_service = _module(
				"ione_qms.services.indicators",
				indicator_artifact_manifest_matches=lambda _binding: live["matches"],
			)
			with patch.dict(
				sys.modules,
				{"ione_qms.services.indicators": indicator_service},
			):
				self.assertTrue(migration_state.migration_is_complete())
				live["matches"] = False
				self.assertFalse(migration_state.migration_is_complete())

	def test_full_fingerprint_is_canonical_and_covers_plan_and_version(self) -> None:
		with _load_migration_state() as migration_state, tempfile.TemporaryDirectory() as directory:
			root = Path(directory) / "ione_qms"
			doctype = root / "module" / "doctype" / "sample"
			doctype.mkdir(parents=True)
			(root / "feature.py").write_bytes(b"value = 1\r\n")
			(doctype / "sample.json").write_text(
				'{"fields": [{"fieldname": "value"}], "name": "Sample"}',
				encoding="utf-8",
			)
			(root / "ignored.json").write_text('{"ignored": true}', encoding="utf-8")
			pycache = root / "__pycache__"
			pycache.mkdir()
			(pycache / "ignored.py").write_text("ignored = True", encoding="utf-8")

			first = migration_state.compute_full_fingerprint(root)
			(root / "feature.py").write_bytes(b"value = 1\n")
			(doctype / "sample.json").write_text(
				json.dumps(
					{"name": "Sample", "fields": [{"fieldname": "value"}]},
					indent=2,
				),
				encoding="utf-8",
			)
			(root / "ignored.json").write_text('{"ignored": false}', encoding="utf-8")
			self.assertEqual(first, migration_state.compute_full_fingerprint(root))

			(root / "feature.py").write_text("value = 2\n", encoding="utf-8")
			self.assertNotEqual(first, migration_state.compute_full_fingerprint(root))
			self.assertNotEqual(
				first,
				migration_state.compute_full_fingerprint(root, app_version="9.9.9"),
			)
			self.assertNotEqual(
				first,
				migration_state.compute_full_fingerprint(
					root,
					migration_plan=({"id": "changed-plan"},),
				),
			)
