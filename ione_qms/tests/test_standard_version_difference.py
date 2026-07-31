from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from ione_qms.services import versions


def _raise_runtime(message: str, *args, **kwargs) -> None:
	del args, kwargs
	raise RuntimeError(message)


def _text_state(value: str | None) -> dict:
	text = value or ""
	return {
		"is_null": value is None,
		"characters": len(text),
		"sha256": hashlib.sha256(text.encode()).hexdigest(),
		"preview": value,
		"truncated": False,
	}


def _version(
	name: str,
	*,
	standard: str = "STD-1",
	effective_from: str = "2026-01-01",
	scope: str = "{}",
) -> dict:
	return {
		"name": name,
		"standard": standard,
		"version": name.rsplit("-", 1)[-1],
		"approval_status": "Approved",
		"effective_from": effective_from,
		"effective_to": None,
		"scope_json": _text_state(scope),
		"checksum": "a" * 64,
		"approved_by": "reviewer@example.test",
		"approved_at": "2026-01-01T08:00:00",
	}


def _clause(
	code: str,
	name: str,
	*,
	heading: str = "Heading",
	content: str = "Policy content",
	keywords: str | None = None,
) -> dict:
	fields = {
		"heading": _text_state(heading),
		"content": _text_state(content),
		"keywords": _text_state(keywords),
	}
	record = {
		"name": name,
		"clause_key": f"KEY:{name}",
		"clause_code": code,
		"status": "Published",
		"fields": fields,
	}
	record["record_checksum"] = versions._standard_diff_hash(
		{
			"clause_key": record["clause_key"],
			"clause_code": code,
			"status": record["status"],
			"fields": {
				fieldname: versions._standard_diff_text_digest_state(value)
				for fieldname, value in fields.items()
			},
		}
	)
	return record


def _rule_row(
	*,
	record: str,
	rule: str,
	clause: str | None,
	code: str,
	name: str,
	version: str | None = "1",
	status: str = "Published",
) -> dict:
	return {
		"relation_record": record,
		"rule": rule,
		"rule_version": version,
		"status": status,
		"checksum": "b" * 64 if version else None,
		"clause_reference": clause,
		"rule_code_chars": len(code),
		"rule_code_preview": code,
		"rule_name_chars": len(name),
		"rule_name_preview": name,
	}


class TestStandardVersionDifference(TestCase):
	def test_only_named_business_users_with_all_read_permissions_are_allowed(self) -> None:
		denied = (
			("Guest", ["Guest"]),
			("Administrator", ["System Manager", "IONE QC Reviewer"]),
			("agent@example.test", ["IONE Agent Service", "IONE QC Reviewer"]),
			("other@example.test", ["System Manager"]),
		)
		for user, roles in denied:
			with (
				self.subTest(user=user),
				patch.object(versions.frappe, "session", SimpleNamespace(user=user)),
				patch.object(versions.frappe, "get_roles", return_value=roles),
				patch.object(versions.frappe, "has_permission", return_value=True),
				patch.object(versions.frappe, "throw", side_effect=_raise_runtime),
				self.assertRaises(RuntimeError),
			):
				versions.require_standard_version_diff_access()

		with (
			patch.object(
				versions.frappe,
				"session",
				SimpleNamespace(user="reviewer@example.test"),
			),
			patch.object(
				versions.frappe,
				"get_roles",
				return_value=["IONE QC Reviewer"],
			),
			patch.object(versions.frappe, "has_permission", return_value=True) as permission,
		):
			versions.require_standard_version_diff_access()
		self.assertEqual(permission.call_count, len(versions.STANDARD_VERSION_DIFF_DOCTYPES))

		with (
			patch.object(
				versions.frappe,
				"session",
				SimpleNamespace(user="reviewer@example.test"),
			),
			patch.object(
				versions.frappe,
				"get_roles",
				return_value=["IONE QC Reviewer"],
			),
			patch.object(
				versions.frappe,
				"has_permission",
				side_effect=[True, True, False, True],
			),
			patch.object(versions.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "Read permission is required"),
		):
			versions.require_standard_version_diff_access()

	def test_clause_differences_are_deterministic_and_code_renames_are_not_inferred(self) -> None:
		baseline = {
			"A": _clause("A", "OLD-A", content="old"),
			"B": _clause("B", "OLD-B"),
		}
		target = {
			"A": _clause("A", "NEW-A", content="new"),
			"C": _clause("C", "NEW-C"),
		}
		changes, counts = versions._build_standard_clause_changes(baseline, target)
		self.assertEqual(
			[(item["clause_code"], item["change_type"]) for item in changes],
			[("A", "Modified"), ("B", "Removed"), ("C", "Added")],
		)
		self.assertEqual(
			counts,
			{"added": 1, "removed": 1, "modified": 1, "unchanged": 0, "total_changes": 3},
		)
		self.assertEqual(
			[item["fieldname"] for item in changes[0]["field_changes"]],
			["content"],
		)
		self.assertEqual(len(changes[0]["change_checksum"]), 64)

		renamed, renamed_counts = versions._build_standard_clause_changes(
			{"OLD": _clause("OLD", "OLD-CLAUSE")},
			{"NEW": _clause("NEW", "NEW-CLAUSE")},
		)
		self.assertEqual(
			[(item["clause_code"], item["change_type"]) for item in renamed],
			[("NEW", "Added"), ("OLD", "Removed")],
		)
		self.assertEqual(renamed_counts["modified"], 0)

	def test_rule_impact_uses_only_exact_standard_and_clause_lineage(self) -> None:
		version_rows = [
			_rule_row(
				record="RV-1",
				rule="RULE-1",
				clause="OLD-A",
				code="R-1",
				name="Rule One",
			),
			_rule_row(
				record="RV-2",
				rule="RULE-2",
				clause="UNRELATED",
				code="R-2",
				name="Rule Two",
			),
		]
		parent_rows = [
			_rule_row(
				record="RULE-3",
				rule="RULE-3",
				clause="NEW-A",
				code="R-3",
				name="Rule Three",
				version=None,
				status="Draft",
			)
		]
		queries: list[tuple[str, dict]] = []

		def sql(query, params, as_dict=False):
			self.assertTrue(as_dict)
			queries.append((query, params))
			return version_rows if "Rule Version" in query else parent_rows

		with patch.object(versions.frappe.db, "sql", side_effect=sql):
			relations = versions._load_affected_standard_rule_relations(
				"STD-1' OR 1=1 --",
				{
					"OLD-A": ("A", "Modified"),
					"NEW-A": ("A", "Modified"),
				},
				(),
			)
		self.assertEqual([item["rule"] for item in relations], ["RULE-1", "RULE-3"])
		self.assertEqual(
			[item["impact_basis"]["clause"]["clause_code"] for item in relations],
			["A", "A"],
		)
		for query, params in queries:
			self.assertNotIn("STD-1' OR 1=1 --", query)
			self.assertEqual(params["standard"], "STD-1' OR 1=1 --")

		with patch.object(versions.frappe.db, "sql", side_effect=[version_rows, parent_rows]):
			standard_wide = versions._load_affected_standard_rule_relations(
				"STD-1",
				{},
				("scope_json",),
			)
		self.assertEqual({item["rule"] for item in standard_wide}, {"RULE-1", "RULE-2", "RULE-3"})
		self.assertTrue(
			all(item["impact_basis"]["standard_fields"] == ["scope_json"] for item in standard_wide)
		)

	def test_comparison_is_paginated_and_checksum_fences_later_pages(self) -> None:
		baseline = _version("STD-1-1", effective_from="2026-01-01")
		target = _version("STD-1-2", effective_from="2026-02-01")
		baseline_clauses = {
			"A": _clause("A", "OLD-A", content="old"),
			"B": _clause("B", "OLD-B"),
		}
		target_clauses = {
			"A": _clause("A", "NEW-A", content="new"),
			"C": _clause("C", "NEW-C"),
		}
		relation = {
			"source": "Rule Version Snapshot",
			"relation_record": "RV-1",
			"rule": "RULE-1",
			"rule_version": "1",
			"status": "Published",
			"checksum": "b" * 64,
			"rule_code": "R-1",
			"rule_name": "Rule One",
			"clause_reference": "OLD-A",
			"impact_basis": {
				"standard_fields": ["effective_from"],
				"clause": {"clause_code": "A", "change_type": "Modified"},
			},
			"relation_checksum": "c" * 64,
		}

		def clauses(name: str):
			return baseline_clauses if name == "STD-1-1" else target_clauses

		with (
			patch.object(versions, "require_standard_version_diff_access"),
			patch.object(
				versions,
				"_load_standard_diff_versions",
				return_value={"STD-1-1": baseline, "STD-1-2": target},
			),
			patch.object(versions, "_load_standard_diff_clauses", side_effect=clauses),
			patch.object(
				versions,
				"_load_affected_standard_rule_relations",
				return_value=[relation],
			),
		):
			first = versions.compare_standard_versions(
				"STD-1-1",
				"STD-1-2",
				change_page_length=1,
			)
			second = versions.compare_standard_versions(
				"STD-1-1",
				"STD-1-2",
				change_start=1,
				change_page_length=1,
				expected_comparison_checksum=first["comparison_checksum"],
			)
			self.assertEqual(first["comparison_checksum"], second["comparison_checksum"])
			self.assertTrue(first["clause_changes"]["has_more"])
			self.assertEqual(first["clause_changes"]["next_start"], 1)
			self.assertEqual(second["clause_changes"]["start"], 1)
			self.assertEqual(first["affected_rule_count"], 1)
			self.assertFalse(first["clinical_data_read"])

			with (
				patch.object(versions.frappe, "throw", side_effect=_raise_runtime),
				self.assertRaisesRegex(RuntimeError, "snapshot changed"),
			):
				versions.compare_standard_versions(
					"STD-1-1",
					"STD-1-2",
					expected_comparison_checksum="0" * 64,
				)

	def test_cross_standard_and_unbounded_inputs_fail_closed(self) -> None:
		with (
			patch.object(versions, "require_standard_version_diff_access"),
			patch.object(
				versions,
				"_load_standard_diff_versions",
				return_value={
					"STD-1-1": _version("STD-1-1", standard="STD-1"),
					"STD-2-1": _version("STD-2-1", standard="STD-2"),
				},
			),
			patch.object(versions.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "same governed standard"),
		):
			versions.compare_standard_versions("STD-1-1", "STD-2-1")

		for fieldname, value, minimum, maximum in (
			("start", -1, 0, 10),
			("length", 11, 1, 10),
			("length", True, 1, 10),
			("length", "1.0", 1, 10),
		):
			with (
				self.subTest(fieldname=fieldname, value=value),
				patch.object(versions.frappe, "throw", side_effect=_raise_runtime),
				self.assertRaises(RuntimeError),
			):
				versions._bounded_standard_diff_integer(
					value,
					fieldname,
					minimum=minimum,
					maximum=maximum,
				)

	def test_clause_loader_uses_parameterized_bounded_sql(self) -> None:
		with (
			patch.object(versions, "STANDARD_DIFF_MAX_CLAUSES_PER_VERSION", 1),
			patch.object(versions.frappe.db, "sql", return_value=[{}, {}]) as sql,
			patch.object(versions.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "clause comparison limit"),
		):
			versions._load_standard_diff_clauses("VERSION' OR 1=1 --")
		query, params = sql.call_args.args[:2]
		self.assertNotIn("VERSION' OR 1=1 --", query)
		self.assertEqual(params["standard_version"], "VERSION' OR 1=1 --")
		self.assertEqual(params["row_limit"], 2)
