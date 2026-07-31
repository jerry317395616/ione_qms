from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from ione_qms.services import versions


def _raise_runtime(message: str, *args, **kwargs) -> None:
	del args, kwargs
	raise RuntimeError(message)


class _Document:
	def __init__(
		self,
		*,
		doctype: str,
		name: str,
		values: dict,
		previous: dict | None = None,
		fields: tuple[tuple[str, str], ...] = (),
	):
		self.doctype = doctype
		self.name = name
		self._values = values
		self._previous = previous
		self.meta = SimpleNamespace(
			fields=[
				SimpleNamespace(fieldname=fieldname, fieldtype=fieldtype) for fieldname, fieldtype in fields
			],
			has_field=lambda fieldname: fieldname in {item[0] for item in fields},
		)

	def get(self, fieldname: str):
		return self._values.get(fieldname)

	def set(self, fieldname: str, value) -> None:
		self._values[fieldname] = value

	def get_doc_before_save(self):
		return self._previous


class _Row(dict):
	def __getattr__(self, fieldname: str):
		return self[fieldname]


class TestDefinitionVersionSnapshots(TestCase):
	def test_all_governed_lineage_links_lock_old_and_new_before_rejection(self) -> None:
		cases = (
			(
				"IONE QC Standard Version",
				{"standard": "STD-NEW"},
				{"standard": "STD-OLD"},
			),
			(
				"IONE QC Rule Version",
				{"rule": "RULE-NEW"},
				{"rule": "RULE-OLD"},
			),
			(
				"IONE QC Indicator Version",
				{"indicator": "IND-NEW"},
				{"indicator": "IND-OLD"},
			),
			(
				"IONE Agent Release",
				{
					"policy": "POLICY-NEW",
					"flow_agent": "AGENT-NEW",
					"flow_model": "MODEL-NEW",
				},
				{
					"policy": "POLICY-OLD",
					"flow_agent": "AGENT-OLD",
					"flow_model": "MODEL-OLD",
				},
			),
		)
		for doctype, values, previous in cases:
			with self.subTest(doctype=doctype):
				doc = _Document(
					doctype=doctype,
					name=f"{doctype}-1",
					values=values,
					previous=previous,
				)
				locked: list[tuple[str, str]] = []
				with (
					patch.object(
						versions,
						"_lock_lineage_parent",
						side_effect=lambda parent_doctype, name, target=locked: target.append(
							(parent_doctype, name)
						),
					),
					patch.object(versions.frappe, "throw", side_effect=_raise_runtime),
					self.assertRaisesRegex(RuntimeError, "lineage links are immutable after insert"),
				):
					versions._lock_and_validate_lineage_links(doc)
				expected = sorted(
					{
						(parent_doctype, str(source[fieldname]))
						for fieldname, parent_doctype in versions.GOVERNED_LINEAGE_LINKS[doctype].items()
						for source in (previous, values)
					}
				)
				self.assertEqual(locked, expected)

	def test_governed_lineage_insert_locks_current_parents_without_rejection(self) -> None:
		doc = _Document(
			doctype="IONE Agent Release",
			name="RELEASE-1",
			values={
				"policy": "POLICY-1",
				"flow_agent": "AGENT-1",
				"flow_model": "MODEL-1",
			},
		)
		with patch.object(versions, "_lock_lineage_parent") as lock_parent:
			versions._lock_and_validate_lineage_links(doc)
		self.assertEqual(
			[call.args for call in lock_parent.call_args_list],
			[
				("Flow Agent", "AGENT-1"),
				("Flow Model", "MODEL-1"),
				("IONE Agent Policy", "POLICY-1"),
			],
		)

	def test_unchanged_governed_lineage_locks_each_parent_once(self) -> None:
		values = {
			"policy": "POLICY-1",
			"flow_agent": "AGENT-1",
			"flow_model": "MODEL-1",
		}
		doc = _Document(
			doctype="IONE Agent Release",
			name="RELEASE-1",
			values=dict(values),
			previous=dict(values),
		)
		with patch.object(versions, "_lock_lineage_parent") as lock_parent:
			versions._lock_and_validate_lineage_links(doc)
		self.assertEqual(lock_parent.call_count, 3)

	def test_generated_rule_version_snapshots_lock_when_draft_ends(self) -> None:
		root = Path(__file__).resolve().parents[1]
		payload = json.loads(
			(
				root
				/ "ione_quality_standards"
				/ "doctype"
				/ "ione_qc_rule_version"
				/ "ione_qc_rule_version.json"
			).read_text(encoding="utf-8")
		)
		fields = {field["fieldname"]: field for field in payload["fields"]}
		self.assertEqual(fields["rule_code_snapshot"].get("read_only"), 1)
		for fieldname in set(versions.RULE_VERSION_SNAPSHOT_FIELDS) - {"rule_code_snapshot"}:
			with self.subTest(fieldname=fieldname):
				self.assertEqual(
					fields[fieldname].get("read_only_depends_on"),
					'eval:doc.status!="Draft"',
				)
		for fieldname in versions.REQUIRED_RULE_SNAPSHOT_FIELDS:
			self.assertEqual(fields[fieldname].get("reqd"), 1)

	def test_parent_rule_and_clause_statuses_are_server_managed(self) -> None:
		root = Path(__file__).resolve().parents[1]
		for relative_path in (
			(
				"ione_quality_standards",
				"doctype",
				"ione_qc_rule",
				"ione_qc_rule.json",
			),
			(
				"ione_quality_standards",
				"doctype",
				"ione_qc_standard_clause",
				"ione_qc_standard_clause.json",
			),
		):
			payload = json.loads(root.joinpath(*relative_path).read_text(encoding="utf-8"))
			status = next(field for field in payload["fields"] if field["fieldname"] == "status")
			self.assertEqual(status.get("read_only"), 1)

	def test_rule_snapshot_is_materialized_before_review(self) -> None:
		doc = _Document(
			doctype="IONE QC Rule Version",
			name="RULE-1-v1",
			values={"status": "Draft", "plugin_key": None},
		)
		rule = _Row(
			rule_code="RULE-1",
			rule_name="Reviewed Rule",
			rule_type="Python Plugin",
			standard="STD-1",
			standard_clause="CLAUSE-1",
			category="Medication",
			risk_level="High",
			owner_department="MED",
			description="Reviewed definition",
		)
		versions._materialize_rule_snapshot(doc, rule)
		self.assertEqual(doc.get("rule_code_snapshot"), "RULE-1")
		self.assertEqual(doc.get("standard_clause_snapshot"), "CLAUSE-1")
		self.assertEqual(doc.get("risk_level_snapshot"), "High")
		self.assertEqual(doc.get("plugin_key"), "RULE-1")

	def test_url_only_standard_template_remains_pending_archive_while_draft(self) -> None:
		doc = _Document(
			doctype="IONE QC Standard Version",
			name="STD-1-v1",
			values={"approval_status": "Draft"},
		)
		standard = _Row(
			name="STD-1",
			standard_code="STD-1",
			standard_name="Draft Template",
			standard_category="Template",
			source_type="National Policy",
			responsible_department="Quality",
			issue_date="2026-01-01",
			source_url="https://authority.example.test/standard.pdf",
			source_file=None,
			description="Not reviewable until archived",
		)
		versions._materialize_standard_snapshot(doc, standard)
		self.assertEqual(doc.get("lineage_status"), "Pending Archive")
		self.assertEqual(doc.get("source_file_hash"), "")
		self.assertEqual(doc.get("standard_code_snapshot"), "STD-1")

	def test_url_only_standard_cannot_leave_draft_without_archived_bytes(self) -> None:
		doc = _Document(
			doctype="IONE QC Standard Version",
			name="STD-1-v1",
			values={"approval_status": "Under Review"},
			previous={"approval_status": "Draft"},
		)
		standard = _Row(
			name="STD-1",
			standard_code="STD-1",
			standard_name="Draft Template",
			standard_category="Template",
			source_type="National Policy",
			responsible_department="Quality",
			issue_date="2026-01-01",
			source_url="https://authority.example.test/standard.pdf",
			source_file=None,
			description="Not reviewable until archived",
		)
		with (
			patch.object(versions.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "cannot enter verified review"),
		):
			versions._materialize_standard_snapshot(doc, standard)

	def test_draft_version_can_change_snapshotted_semantics(self) -> None:
		previous = {
			"status": "Draft",
			"rule_code_snapshot": "RULE-1",
			"rule_name_snapshot": "Original",
			"rule_type_snapshot": "Deterministic",
			"risk_level_snapshot": "Medium",
		}
		doc = _Document(
			doctype="IONE QC Rule Version",
			name="RULE-1-v2",
			values={
				**previous,
				"rule_name_snapshot": "New reviewed name",
				"risk_level_snapshot": "Critical",
			},
			previous=previous,
		)
		versions._materialize_rule_snapshot(
			doc,
			_Row(
				rule_code="RULE-1",
				rule_name="Parent identity",
				rule_type="Deterministic",
				risk_level="Medium",
			),
		)
		self.assertEqual(doc.get("rule_name_snapshot"), "New reviewed name")
		self.assertEqual(doc.get("risk_level_snapshot"), "Critical")

	def test_legacy_draft_materializes_complete_snapshot_once(self) -> None:
		doc = _Document(
			doctype="IONE QC Rule Version",
			name="RULE-1-v1",
			values={"status": "Draft"},
			previous={"status": "Draft"},
		)
		versions._materialize_rule_snapshot(
			doc,
			_Row(
				rule_code="RULE-1",
				rule_name="Rule",
				rule_type="Deterministic",
				standard="STD-1",
				standard_clause="CLAUSE-1",
				category="Safety",
				risk_level="High",
				owner_department="MED",
				description="Legacy draft",
			),
		)
		self.assertEqual(doc.get("standard_snapshot"), "STD-1")
		self.assertEqual(doc.get("standard_clause_snapshot"), "CLAUSE-1")
		self.assertEqual(doc.get("description_snapshot"), "Legacy draft")

	def test_rule_snapshot_cannot_change_after_review_starts(self) -> None:
		previous = {
			"status": "Expert Review",
			**{fieldname: f"original-{fieldname}" for fieldname in versions.RULE_VERSION_SNAPSHOT_FIELDS},
		}
		values = dict(previous)
		values["rule_name_snapshot"] = "changed"
		doc = _Document(
			doctype="IONE QC Rule Version",
			name="RULE-1-v1",
			values=values,
			previous=previous,
		)
		with (
			patch.object(versions.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "semantic snapshots are immutable"),
		):
			versions._materialize_rule_snapshot(doc, _Row())

	def test_reviewed_rule_parent_definition_cannot_be_changed(self) -> None:
		previous = {
			"status": "Active",
			"rule_name": "Original",
		}
		doc = _Document(
			doctype="IONE QC Rule",
			name="RULE-1",
			values={"status": "Active", "rule_name": "Changed"},
			previous=previous,
		)
		with (
			patch.object(versions.frappe.db, "sql"),
			patch.object(versions.frappe.db, "exists", return_value=True),
			patch.object(versions.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "Reviewed rule identity is immutable"),
		):
			versions.validate_rule(doc)

	def test_clause_content_is_immutable_after_parent_approval(self) -> None:
		doc = _Document(
			doctype="IONE QC Standard Clause",
			name="CLAUSE-1",
			values={
				"standard_version": "STD-1-v1",
				"clause_key": "STD-1:1",
				"content": "Changed",
				"status": "Published",
			},
			previous={
				"standard_version": "STD-1-v1",
				"clause_key": "STD-1:1",
				"content": "Original",
				"status": "Published",
			},
		)
		with (
			patch.object(versions.frappe.db, "sql"),
			patch.object(versions.frappe.db, "get_value", return_value="Approved"),
			patch.object(versions.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "standard clauses are immutable"),
		):
			versions.validate_standard_clause(doc)

	def test_clause_hash_changes_when_governed_text_changes(self) -> None:
		base = _Row(
			name="CLAUSE-1",
			clause_key="STD-1:1",
			standard_version="STD-1-v1",
			clause_code="1",
			heading="Safety",
			content="Original",
			keywords="safety",
		)
		changed = _Row(base)
		changed["content"] = "Changed"
		with patch.object(versions.frappe, "get_all", side_effect=[[base], [changed]]):
			first = versions._standard_clause_hashes("STD-1-v1")
			second = versions._standard_clause_hashes("STD-1-v1")
		self.assertNotEqual(first[0]["content_hash"], second[0]["content_hash"])

	def test_standard_version_checksum_content_includes_ordered_clause_hashes(self) -> None:
		doc = _Document(
			doctype="IONE QC Standard Version",
			name="STD-1-v1",
			values={"standard": "STD-1", "version": "1"},
			fields=(("standard", "Link"), ("version", "Data")),
		)
		clause_hashes = [
			{
				"name": "CLAUSE-1",
				"clause_key": "STD-1:1",
				"content_hash": "a" * 64,
			}
		]
		with patch.object(versions, "_standard_clause_hashes", return_value=clause_hashes):
			content = versions._standard_version_content(doc)
		self.assertEqual(content["standard_clauses"], clause_hashes)

	def test_definition_lifecycle_locks_are_transactional(self) -> None:
		with patch.object(versions.frappe.db, "sql") as sql:
			versions._lock_rule_parent("RULE-1")
			versions._lock_lineage_parent("Flow Agent", "AGENT-1")
			versions._lock_standard_version("STD-1-v1")
		self.assertEqual(sql.call_count, 3)
		for call in sql.call_args_list:
			self.assertIn("for update", call.args[0].lower())
