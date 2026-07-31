from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from ione_qms.hooks import doc_events
from ione_qms.services import versions


def _raise_runtime(message: str, *args, **kwargs) -> None:
	del args, kwargs
	raise RuntimeError(message)


class _VersionDocument:
	def __init__(
		self,
		*,
		doctype: str,
		name: str,
		values: dict,
		previous: dict | None,
	):
		self.doctype = doctype
		self.name = name
		self._values = values
		self._previous = previous
		self.meta = SimpleNamespace(has_field=lambda fieldname: fieldname == "active_parent_key")

	def get(self, fieldname: str):
		return self._values.get(fieldname)

	def set(self, fieldname: str, value) -> None:
		self._values[fieldname] = value

	def get_doc_before_save(self):
		return self._previous


class _Row(dict):
	def __getattr__(self, fieldname: str):
		return self[fieldname]


class TestVersionParentSynchronization(TestCase):
	def test_active_parent_keys_are_database_unique(self) -> None:
		package_root = Path(__file__).resolve().parents[1]
		paths = (
			package_root
			/ "ione_quality_standards"
			/ "doctype"
			/ "ione_qc_standard_version"
			/ "ione_qc_standard_version.json",
			package_root
			/ "ione_quality_standards"
			/ "doctype"
			/ "ione_qc_rule_version"
			/ "ione_qc_rule_version.json",
			package_root
			/ "ione_indicators"
			/ "doctype"
			/ "ione_qc_indicator_version"
			/ "ione_qc_indicator_version.json",
		)
		for path in paths:
			with self.subTest(path=path):
				payload = json.loads(path.read_text(encoding="utf-8"))
				field = next(item for item in payload["fields"] if item["fieldname"] == "active_parent_key")
				self.assertEqual(field.get("unique"), 1)
				self.assertEqual(field.get("hidden"), 1)
				self.assertEqual(field.get("read_only"), 1)

	def test_version_hooks_include_parent_synchronizers(self) -> None:
		expected = {
			"IONE QC Standard Version": "ione_qms.services.versions.on_standard_version_update",
			"IONE QC Rule Version": "ione_qms.services.versions.on_rule_version_update",
			"IONE QC Indicator Version": "ione_qms.services.versions.on_indicator_version_update",
		}
		for doctype, handler in expected.items():
			with self.subTest(doctype=doctype):
				self.assertEqual(doc_events[doctype]["on_update"], handler)

	def test_rule_parent_and_standard_clause_have_governance_hooks(self) -> None:
		self.assertEqual(
			doc_events["IONE QC Rule"]["validate"],
			"ione_qms.services.versions.validate_rule",
		)
		self.assertEqual(
			doc_events["IONE QC Standard Clause"]["validate"],
			"ione_qms.services.versions.validate_standard_clause",
		)

	def test_standard_approval_marks_parent_published(self) -> None:
		doc = _VersionDocument(
			doctype="IONE QC Standard Version",
			name="STD-1-2",
			values={"standard": "STD-1", "approval_status": "Approved"},
			previous={"approval_status": "Under Review"},
		)
		with (
			patch.object(versions.frappe.db, "get_value", return_value="Under Review"),
			patch.object(versions.frappe.db, "set_value") as set_value,
		):
			versions.on_standard_version_update(doc)
		set_value.assert_any_call("IONE QC Standard", "STD-1", "status", "Published")
		set_value.assert_any_call(
			"IONE QC Standard Clause",
			{"standard_version": "STD-1-2"},
			"status",
			"Published",
			update_modified=False,
		)
		self.assertEqual(set_value.call_count, 2)

	def test_rule_retirement_marks_parent_retired_when_no_active_version_remains(self) -> None:
		doc = _VersionDocument(
			doctype="IONE QC Rule Version",
			name="RULE-1-2",
			values={"rule": "RULE-1", "status": "Retired"},
			previous={"status": "Published"},
		)
		with (
			patch.object(versions.frappe.db, "exists", return_value=False),
			patch.object(versions.frappe.db, "get_value", return_value="Active"),
			patch.object(versions.frappe.db, "set_value") as set_value,
		):
			versions.on_rule_version_update(doc)
		set_value.assert_called_once_with("IONE QC Rule", "RULE-1", "status", "Retired")

	def test_a_parent_cannot_have_two_active_versions(self) -> None:
		doc = _VersionDocument(
			doctype="IONE QC Indicator Version",
			name="IND-1-2",
			values={"indicator": "IND-1", "status": "Published"},
			previous={"status": "Under Review"},
		)
		with (
			patch.object(versions.frappe.db, "exists", return_value=True),
			patch.object(versions.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "already has an active Published version"),
		):
			versions._require_single_active_version(
				doc,
				parent_field="indicator",
				status_field="status",
				active_status="Published",
			)

	def test_active_parent_key_is_set_only_for_an_active_version(self) -> None:
		doc = _VersionDocument(
			doctype="IONE QC Rule Version",
			name="RULE-1-2",
			values={"rule": "RULE-1", "status": "Published"},
			previous={"status": "Approval"},
		)
		versions._set_active_parent_key(
			doc,
			parent_field="rule",
			status_field="status",
			active_status="Published",
		)
		self.assertEqual(doc.get("active_parent_key"), "RULE-1")
		doc.set("status", "Retired")
		versions._set_active_parent_key(
			doc,
			parent_field="rule",
			status_field="status",
			active_status="Published",
		)
		self.assertIsNone(doc.get("active_parent_key"))

	def test_migration_reports_legacy_duplicate_active_versions_without_writing(self) -> None:
		rows = [
			_Row(name="STD-1-1", parent_name="STD-1"),
			_Row(name="STD-1-2", parent_name="STD-1"),
		]

		def page(_doctype, stage, _limit, _fetch):
			return (rows, False) if stage == "active-key" else ([], False)

		with (
			patch.object(
				versions.frappe.db,
				"exists",
				side_effect=lambda doctype, name: (
					(doctype == "DocType" and name == "IONE QC Standard Version")
					or (doctype == "IONE QC Standard" and name == "STD-1")
				),
			),
			patch.object(
				versions.frappe,
				"get_meta",
				return_value=SimpleNamespace(has_field=lambda fieldname: True),
			),
			patch.object(versions.frappe, "clear_cache"),
			patch.object(versions, "_version_backfill_page", side_effect=page),
			patch.object(
				versions,
				"_active_version_names",
				return_value=["STD-1-1", "STD-1-2"],
			),
			patch.object(versions.frappe.cache, "get_value", return_value=None),
			patch.object(versions.frappe.cache, "set_value"),
			patch.object(versions.frappe, "log_error") as log_error,
			patch.object(versions.frappe.db, "set_value") as set_value,
		):
			summary = versions.backfill_active_version_keys(batch_size=10)
		self.assertEqual(summary["blocked"], 2)
		self.assertEqual(
			summary["doctypes"]["IONE QC Standard Version"]["issues"][0]["code"],
			"DUPLICATE_ACTIVE_VERSION",
		)
		set_value.assert_not_called()
		log_error.assert_called_once()

	def test_active_key_selector_excludes_correct_rows_and_is_bounded(self) -> None:
		with patch.object(versions.frappe.db, "sql", return_value=[]) as sql:
			versions._select_active_key_rows(
				"IONE QC Standard Version",
				"standard",
				"approval_status",
				"Approved",
				"STD-0009",
				17,
			)
		query, values = sql.call_args.args
		self.assertIn("v.active_parent_key <> v.`standard`", query)
		self.assertIn("v.name > %(cursor)s", query)
		self.assertIn("LIMIT %(limit)s", query)
		self.assertEqual(values["cursor"], "STD-0009")
		self.assertEqual(values["limit"], 17)
		self.assertTrue(sql.call_args.kwargs["as_dict"])

	def test_version_backfill_uses_one_shared_batch_budget_per_doctype(self) -> None:
		stages: list[str] = []

		def page(_doctype, stage, limit, _fetch):
			stages.append(stage)
			if stage == "inactive-key":
				return [_Row(name=f"STD-{index}") for index in range(limit)], False
			self.fail("A later stage must not run after the shared budget is consumed")

		with (
			patch.object(
				versions.frappe.db,
				"exists",
				side_effect=lambda _doctype, name: name == "IONE QC Standard Version",
			),
			patch.object(
				versions.frappe,
				"get_meta",
				return_value=SimpleNamespace(has_field=lambda fieldname: True),
			),
			patch.object(versions.frappe, "clear_cache"),
			patch.object(versions, "_version_backfill_page", side_effect=page),
			patch.object(versions.frappe.db, "set_value") as set_value,
		):
			summary = versions.backfill_active_version_keys(batch_size=3)
		self.assertEqual(stages, ["inactive-key"])
		self.assertEqual(set_value.call_count, 3)
		self.assertEqual(summary["cleared"], 3)
		self.assertTrue(summary["has_more"])

	def test_blocked_page_cursor_advances_then_wraps_without_unbounded_rescan(self) -> None:
		stored_cursor = ""

		def get_value(_key):
			return stored_cursor

		def set_value(_key, value, **_kwargs):
			nonlocal stored_cursor
			stored_cursor = value

		def delete_value(_key):
			nonlocal stored_cursor
			stored_cursor = ""

		def fetch(cursor, limit):
			rows = [_Row(name="VER-0002"), _Row(name="VER-0003")] if not cursor else []
			return rows[:limit]

		with (
			patch.object(versions.frappe.cache, "get_value", side_effect=get_value),
			patch.object(versions.frappe.cache, "set_value", side_effect=set_value),
			patch.object(versions.frappe.cache, "delete_value", side_effect=delete_value),
		):
			first, first_has_more = versions._version_backfill_page(
				"IONE QC Rule Version", "active-key", 2, fetch
			)
			self.assertEqual([row.name for row in first], ["VER-0002", "VER-0003"])
			self.assertTrue(first_has_more)
			self.assertEqual(stored_cursor, "VER-0003")
			second, second_has_more = versions._version_backfill_page(
				"IONE QC Rule Version", "active-key", 2, fetch
			)
			self.assertEqual(second, [])
			self.assertTrue(second_has_more)
			self.assertEqual(stored_cursor, "")

	def test_author_cannot_edit_rule_content_after_expert_review_starts(self) -> None:
		doc = _VersionDocument(
			doctype="IONE QC Rule Version",
			name="RULE-1-2",
			values={
				"status": "Expert Review",
				"condition_json": '{"field":"changed"}',
			},
			previous={
				"status": "Expert Review",
				"condition_json": '{"field":"original"}',
			},
		)
		doc.meta = SimpleNamespace(
			has_field=lambda fieldname: fieldname == "active_parent_key",
			fields=[SimpleNamespace(fieldname="condition_json", fieldtype="Code")],
		)
		with (
			patch.object(versions.frappe.session, "user", "author@example.test"),
			patch.object(
				versions.frappe,
				"get_roles",
				return_value=["IONE QC Administrator"],
			),
			patch.object(versions.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "cannot edit content"),
		):
			versions._require_same_state_editor(
				doc,
				status_field="status",
				transition=None,
				state_role_map=versions.RULE_VERSION_STATE_EDIT_ROLES,
			)
