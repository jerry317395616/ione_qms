from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from ione_qms.ai import tools


class _Meta:
	def __init__(self, fields: set[str] | None = None) -> None:
		self.fields = fields or set()

	def has_field(self, fieldname: str) -> bool:
		return fieldname in self.fields


class _Doc(dict):
	def __init__(
		self,
		doctype: str,
		name: str,
		*,
		meta_fields: set[str] | None = None,
		permission: bool = True,
		**values,
	) -> None:
		super().__init__(values)
		self.doctype = doctype
		self.name = name
		self.meta = _Meta(meta_fields)
		self.permission = permission

	def __getattr__(self, fieldname: str):
		try:
			return self[fieldname]
		except KeyError as exc:
			raise AttributeError(fieldname) from exc

	def has_permission(self, *_args, **_kwargs) -> bool:
		return self.permission


def _task(**overrides) -> _Doc:
	values = {
		"task_type": "Rectification",
		"policy": "POLICY-1",
		"requested_by": "requester@example.test",
		"status": "Running",
		"record_count": 1,
		"requires_human_review": 1,
		"hospital": "HOSP-1",
		"campus": "CAMPUS-1",
		"department": "DEPT-1",
		"ward": "WARD-1",
		"patient": "PATIENT-SECRET",
		"encounter": "ENCOUNTER-SECRET",
		"responsible_staff": "STAFF-1",
		"indicator_result": None,
		"finding": "FINDING-1",
	}
	values.update(overrides)
	return _Doc("IONE AI Analysis Task", "TASK-1", **values)


def _finding(**overrides) -> _Doc:
	values = {
		"status": "Rectifying",
		"severity": "High",
		"hospital": "HOSP-1",
		"campus": "CAMPUS-1",
		"department": "DEPT-1",
		"ward": "WARD-1",
		"patient": "PATIENT-SECRET",
		"encounter": "ENCOUNTER-SECRET",
		"responsible_staff": "STAFF-1",
		"detected_at": "2026-07-01 10:00:00",
		"due_date": "2026-07-08",
		"rule": "RULE-1",
		"rule_version": "RULE-VERSION-1",
		"standard": "STANDARD-1",
		"standard_clause": "CLAUSE-1",
		"evidence_hash": "a" * 64,
		"modified": "2026-07-02 10:00:00",
		"title": "PATIENT-SECRET must never be returned",
		"description": "RAW CLINICAL NARRATIVE",
	}
	values.update(overrides)
	return _Doc("IONE QC Finding", "FINDING-1", **values)


class TestRectificationAgentTool(TestCase):
	def test_rectification_task_revalidates_requester_scope_and_finding_access(self) -> None:
		task = _task()
		finding = _finding()
		with (
			patch.object(tools.frappe.db, "get_value", return_value=1),
			patch.object(
				tools.frappe,
				"get_roles",
				return_value=["IONE Department Director"],
			),
			patch.object(tools, "require_scope_read") as require_scope,
			patch.object(tools.frappe, "get_doc", return_value=finding),
			patch.object(tools, "_require_requester_read") as require_record,
		):
			actual = tools._validated_rectification_finding(task)

		self.assertIs(actual, finding)
		require_scope.assert_called_once_with(
			hospital="HOSP-1",
			campus="CAMPUS-1",
			department="DEPT-1",
			ward="WARD-1",
			user="requester@example.test",
			target_doctype="IONE QC Finding",
		)
		require_record.assert_called_once_with(task, finding)

	def test_wrong_task_contract_or_unconfirmed_finding_fails_closed(self) -> None:
		with self.assertRaises(frappe.PermissionError):
			tools._validated_rectification_finding(_task(task_type="Indicator Analysis"))

		with (
			patch.object(tools.frappe.db, "get_value", return_value=1),
			patch.object(tools.frappe, "get_roles", return_value=["IONE QC Reviewer"]),
			patch.object(tools, "require_scope_read"),
			patch.object(tools.frappe, "get_doc", return_value=_finding(status="Appealed")),
			patch.object(tools, "_require_requester_read"),
			self.assertRaises(frappe.ValidationError),
		):
			tools._validated_rectification_finding(_task())

	def test_disabled_or_deauthorized_requester_fails_before_finding_read(self) -> None:
		with (
			patch.object(tools.frappe.db, "get_value", return_value=0),
			patch.object(tools.frappe, "get_roles", return_value=["IONE QC Reviewer"]),
			patch.object(tools.frappe, "get_doc") as get_doc,
			self.assertRaises(frappe.PermissionError),
		):
			tools._validated_rectification_finding(_task())
		get_doc.assert_not_called()

	def test_related_records_are_bounded_and_scope_rechecked(self) -> None:
		task = _task()
		finding = _finding()
		overflow = [f"EVIDENCE-{index}" for index in range(tools.MAX_RECTIFICATION_EVIDENCE_ROWS + 1)]
		with (
			patch.object(tools.frappe, "get_all", return_value=overflow),
			patch.object(tools.frappe, "get_doc") as get_doc,
			self.assertRaises(frappe.ValidationError),
		):
			tools._bounded_related_documents(
				task,
				finding,
				"IONE QC Finding Evidence",
				tools.MAX_RECTIFICATION_EVIDENCE_ROWS,
			)
		get_doc.assert_not_called()

		cross_scope = _Doc(
			"IONE QC Rectification",
			"RECT-1",
			meta_fields={"hospital", "campus", "department", "ward"},
			finding=finding.name,
			hospital="HOSP-1",
			campus="CAMPUS-1",
			department="OTHER-DEPARTMENT",
			ward="WARD-1",
		)
		with (
			patch.object(tools.frappe, "get_all", return_value=[cross_scope.name]),
			patch.object(tools.frappe, "get_doc", return_value=cross_scope),
			patch.object(tools, "_require_requester_read"),
			self.assertRaises(frappe.PermissionError),
		):
			tools._bounded_related_documents(
				task,
				finding,
				"IONE QC Rectification",
				tools.MAX_RECTIFICATION_ROWS,
			)

	def test_tool_returns_only_structured_context_with_receipt_per_source(self) -> None:
		task = _task()
		finding = _finding()
		evidence = _Doc(
			"IONE QC Finding Evidence",
			"EVIDENCE-1",
			finding=finding.name,
			evidence_type="Rule Evidence",
			matched=1,
			captured_at="2026-07-01 10:00:00",
			recorded_at="2026-07-01 10:01:00",
			rule_version="RULE-VERSION-1",
			standard_clause="CLAUSE-1",
			evidence_hash="b" * 64,
			evidence_text="RAW CLINICAL NARRATIVE",
			actual_json='{"patient":"PATIENT-SECRET"}',
			modified="2026-07-01 10:01:00",
		)
		rectification = _Doc(
			"IONE QC Rectification",
			"RECT-1",
			meta_fields={"hospital", "campus", "department", "ward"},
			finding=finding.name,
			status="In Progress",
			due_date="2026-07-08",
			submitted_at="2026-07-02 10:00:00",
			department_approved_at=None,
			functional_reviewed_at=None,
			actions=[
				{
					"status": "In Progress",
					"action_description": "SECRET ACTION NARRATIVE",
				}
			],
			active_key=finding.name,
			plan="SECRET RECTIFICATION PLAN",
			modified="2026-07-02 10:00:00",
		)
		verification = _Doc(
			"IONE QC Verification",
			"VERIFY-1",
			meta_fields={"hospital", "campus", "department", "ward"},
			finding=finding.name,
			rectification=rectification.name,
			status="Submitted",
			verification_result="",
			verified_at=None,
			verification_comment="SECRET VERIFICATION COMMENT",
			active_key=finding.name,
			modified="2026-07-03 10:00:00",
		)
		pdca = _Doc(
			"IONE PDCA Project",
			"PDCA-1",
			meta_fields={"hospital", "campus", "department", "ward"},
			finding=finding.name,
			status="Active",
			indicator="INDICATOR-1",
			start_date="2026-07-01",
			end_date="2026-08-01",
			baseline_value=1.0,
			target_value=0.5,
			actual_value=0.8,
			root_causes=[
				{
					"confirmed": 1,
					"cause": "SECRET ROOT CAUSE",
					"evidence": "SECRET ROOT EVIDENCE",
				}
			],
			improvement_measures=[
				{
					"status": "In Progress",
					"measure": "SECRET IMPROVEMENT MEASURE",
					"effectiveness": "SECRET EFFECTIVENESS",
				}
			],
			project_code="PDCA-SECRET",
			modified="2026-07-04 10:00:00",
		)
		related = {
			"IONE QC Finding Evidence": [evidence],
			"IONE QC Rectification": [rectification],
			"IONE QC Verification": [verification],
			"IONE PDCA Project": [pdca],
		}
		receipt_calls = []

		def record_access(**kwargs):
			receipt_calls.append(kwargs)
			return f"IONE AI Data Access Log:ACCESS-{len(receipt_calls)}"

		with (
			patch.object(tools, "_assert_authenticated"),
			patch.object(tools, "_get_active_task", return_value=task),
			patch.object(
				tools,
				"_active_policy_for_tool",
				return_value=SimpleNamespace(name=task.policy),
			),
			patch.object(tools, "_validated_rectification_finding", return_value=finding),
			patch.object(
				tools,
				"_bounded_related_documents",
				side_effect=lambda _task, _finding, doctype, _limit: related[doctype],
			),
			patch.object(tools, "record_tool_access", side_effect=record_access),
		):
			result = tools.ione_get_rectification_context.func(task=task.name)

		self.assertEqual(len(receipt_calls), 5)
		self.assertEqual(
			{call["source_doctype"] for call in receipt_calls},
			{
				"IONE QC Finding",
				"IONE QC Finding Evidence",
				"IONE QC Rectification",
				"IONE QC Verification",
				"IONE PDCA Project",
			},
		)
		self.assertTrue(all(call["patient"] == "PATIENT-SECRET" for call in receipt_calls))
		self.assertEqual(result["rectification"]["status_counts"], {"In Progress": 1})
		self.assertEqual(result["verification"]["status_counts"], {"Submitted": 1})
		self.assertEqual(result["pdca"]["status_counts"], {"Active": 1})
		self.assertEqual(
			result["historical_cases"],
			{
				"available": False,
				"reason_code": "NO_APPROVED_DEIDENTIFIED_RECTIFICATION_CASE_MODEL",
				"records": [],
			},
		)
		self.assertTrue(result["output_contract"]["draft_only"])
		serialized = json.dumps(result, ensure_ascii=False, default=str)
		for forbidden in (
			"PATIENT-SECRET",
			"ENCOUNTER-SECRET",
			"RAW CLINICAL NARRATIVE",
			"SECRET ACTION NARRATIVE",
			"SECRET RECTIFICATION PLAN",
			"SECRET VERIFICATION COMMENT",
			"SECRET ROOT CAUSE",
			"SECRET ROOT EVIDENCE",
			"SECRET IMPROVEMENT MEASURE",
			"SECRET EFFECTIVENESS",
		):
			with self.subTest(forbidden=forbidden):
				self.assertNotIn(forbidden, serialized)
