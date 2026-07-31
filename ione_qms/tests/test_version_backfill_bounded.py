from __future__ import annotations

import importlib.util
import sys
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import TestCase


class _Row(dict):
	def __getattr__(self, fieldname: str):
		return self[fieldname]


class _Cache:
	def __init__(self) -> None:
		self.values: dict[str, object] = {}

	def get_value(self, key: str):
		return self.values.get(key)

	def set_value(self, key: str, value, **_kwargs) -> None:
		self.values[key] = value

	def delete_value(self, key: str) -> None:
		self.values.pop(key, None)


class _Database:
	def __init__(self) -> None:
		self.writes: list[tuple] = []
		self.sql_calls: list[tuple[str, dict]] = []
		self.active_candidates = [
			_Row(name="VER-A1", parent_name="PARENT-DUP"),
			_Row(name="VER-A2", parent_name="PARENT-DUP"),
			_Row(name="VER-Z1", parent_name="PARENT-VALID"),
		]

	def exists(self, doctype: str, name: str) -> bool:
		return (doctype == "DocType" and name == "IONE QC Standard Version") or (
			doctype == "IONE QC Standard" and name in {"PARENT-DUP", "PARENT-VALID"}
		)

	def set_value(self, *args, **kwargs) -> None:
		self.writes.append((*args, kwargs))

	def sql(self, query: str, values: dict, *, as_dict: bool):
		assert as_dict
		self.sql_calls.append((query, values))
		if "v.active_parent_key IS NULL" not in query:
			return []
		cursor = str(values["cursor"])
		limit = int(values["limit"])
		return [row for row in self.active_candidates if row.name > cursor][:limit]


def _module(name: str, **attributes) -> ModuleType:
	module = ModuleType(name)
	for fieldname, value in attributes.items():
		setattr(module, fieldname, value)
	return module


@contextmanager
def _load_versions():
	database = _Database()
	cache = _Cache()
	frappe = _module(
		"frappe",
		db=database,
		cache=cache,
		clear_cache=lambda **_kwargs: None,
		get_meta=lambda _doctype: SimpleNamespace(
			has_field=lambda fieldname: fieldname == "active_parent_key"
		),
		get_all=lambda _doctype, *, filters, **_kwargs: (
			["VER-A1", "VER-A2"] if filters.get("standard") == "PARENT-DUP" else ["VER-Z1"]
		),
		log_error=lambda **_kwargs: None,
		DuplicateEntryError=type("DuplicateEntryError", (Exception,), {}),
		UniqueValidationError=type("UniqueValidationError", (Exception,), {}),
	)
	stubs = {
		"frappe": frappe,
		"frappe.utils": _module(
			"frappe.utils",
			getdate=lambda value: value if isinstance(value, date) else date.fromisoformat(str(value)),
			now_datetime=datetime.now,
		),
		"ione_qms.ai.release": _module(
			"ione_qms.ai.release",
			build_release_configuration=lambda *_args, **_kwargs: {},
			canonical_json=lambda value: str(value),
			current_app_commit_sha=lambda: "",
		),
		"ione_qms.rule_engine.evaluator": _module(
			"ione_qms.rule_engine.evaluator",
			validate_rule_definition_schema=lambda _value: None,
		),
		"ione_qms.rule_engine.registry": _module(
			"ione_qms.rule_engine.registry",
			get_rule=lambda _key: None,
		),
		"ione_qms.services.indicators": _module(
			"ione_qms.services.indicators",
			validate_indicator_effective_boundaries=lambda _doc: None,
		),
	}
	original = {name: sys.modules.get(name) for name in stubs}
	try:
		sys.modules.update(stubs)
		path = Path(__file__).resolve().parents[1] / "services" / "versions.py"
		spec = importlib.util.spec_from_file_location("_ione_versions_bounded_test", path)
		assert spec and spec.loader
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
	finally:
		for name, previous in original.items():
			if previous is None:
				sys.modules.pop(name, None)
			else:
				sys.modules[name] = previous
	yield module, database, cache


class TestBoundedVersionBackfill(TestCase):
	def test_duplicate_page_is_reported_and_cursor_allows_later_repair(self) -> None:
		with _load_versions() as (versions, database, cache):
			first = versions.backfill_active_version_keys(batch_size=2)
			self.assertEqual(first["blocked"], 2)
			self.assertEqual(database.writes, [])
			self.assertEqual(
				cache.values["ione_qms:version_backfill:IONE QC Standard Version:active-key"],
				"VER-A2",
			)

			second = versions.backfill_active_version_keys(batch_size=2)
			self.assertEqual(second["updated"], 1)
			self.assertIn(
				(
					"IONE QC Standard Version",
					"VER-Z1",
					"active_parent_key",
					"PARENT-VALID",
					{"update_modified": False},
				),
				database.writes,
			)

	def test_every_selector_query_has_a_database_limit(self) -> None:
		with _load_versions() as (versions, database, _cache):
			versions.backfill_active_version_keys(batch_size=7)
			self.assertTrue(database.sql_calls)
			for query, values in database.sql_calls:
				self.assertIn("LIMIT %(limit)s", query)
				self.assertLessEqual(values["limit"], 7)
				self.assertGreater(values["limit"], 0)
