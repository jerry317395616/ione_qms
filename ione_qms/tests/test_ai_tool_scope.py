from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from ione_qms.ai import tools


class _Doc(SimpleNamespace):
	def get(self, fieldname: str, default=None):
		return getattr(self, fieldname, default)

	def insert(self, *, ignore_permissions: bool = False):
		self.inserted_ignore_permissions = ignore_permissions
		return self

	def has_permission(self, *_args, **_kwargs) -> bool:
		return True


def _task(**overrides) -> _Doc:
	values = {
		"name": "TASK-1",
		"policy": "POLICY-1",
		"requested_by": "reviewer@example.com",
		"status": "Running",
		"hospital": "HOSP-1",
		"campus": "CAMPUS-1",
		"department": "DEPT-1",
		"ward": "WARD-1",
		"patient": "PATIENT-1",
		"encounter": "ENCOUNTER-1",
		"responsible_staff": "STAFF-1",
		"indicator_result": "RESULT-1",
		"finding": "FINDING-1",
	}
	values.update(overrides)
	return _Doc(**values)


def _matching_scope() -> dict[str, str]:
	return {
		"hospital": "HOSP-1",
		"campus": "CAMPUS-1",
		"department": "DEPT-1",
		"ward": "WARD-1",
		"patient": "PATIENT-1",
		"encounter": "ENCOUNTER-1",
		"responsible_staff": "STAFF-1",
		"indicator_result": "RESULT-1",
		"finding": "FINDING-1",
	}


class TestAIToolScope(TestCase):
	def test_every_applicable_non_empty_scope_requires_an_exact_value(self) -> None:
		task = _task()
		tools._assert_scope_exact(task, source_label="test record", **_matching_scope())

		for fieldname in tools.TASK_SCOPE_FIELDS:
			for invalid in (None, f"OTHER-{fieldname}"):
				with self.subTest(fieldname=fieldname, invalid=invalid):
					scope = _matching_scope()
					scope[fieldname] = invalid
					with self.assertRaises(frappe.PermissionError):
						tools._assert_scope_exact(
							task,
							source_label="test record",
							**scope,
						)

	def test_scope_query_filters_push_every_mapped_task_scope(self) -> None:
		field_map = {
			"hospital": "hospital",
			"campus": "campus",
			"department": "department",
			"ward": "ward",
			"patient": "patient_index",
			"encounter": "encounter_index",
			"responsible_staff": "responsible_staff",
			"indicator_result": "indicator_result",
		}
		self.assertEqual(
			tools._scope_query_filters(_task(), field_map),
			{
				"hospital": "HOSP-1",
				"campus": "CAMPUS-1",
				"department": "DEPT-1",
				"ward": "WARD-1",
				"patient_index": "PATIENT-1",
				"encounter_index": "ENCOUNTER-1",
				"responsible_staff": "STAFF-1",
				"indicator_result": "RESULT-1",
			},
		)

	def test_task_anchor_with_missing_or_different_scope_is_rejected(self) -> None:
		task = _task()
		result = _Doc(
			name="RESULT-1",
			hospital="HOSP-1",
			campus="CAMPUS-1",
			department="DEPT-1",
			ward=None,
		)
		with (
			patch.object(tools.frappe, "get_doc", return_value=result),
			patch.object(tools, "_require_requester_read"),
			self.assertRaises(frappe.PermissionError),
		):
			tools._validate_task_anchor_scopes(task)

	def test_indicator_trend_pushes_full_scope_and_rechecks_returned_rows(self) -> None:
		task = _task(finding=None)
		result_row = frappe._dict(
			name="RESULT-1",
			indicator_version="VERSION-1",
			period_start="2026-01-01",
			period_end="2026-01-31",
			hospital="HOSP-1",
			campus="CAMPUS-1",
			department="DEPT-1",
			ward="OTHER-WARD",
			numerator=1,
			denominator=2,
			indicator_value=0.5,
			target_value=0.9,
			status="Below Target",
			computed_at="2026-02-01",
			result_key="RESULT-KEY",
			dimension_hash="DIMENSION-HASH",
			calculator_version="1",
			lineage_json="{}",
			modified="2026-02-01",
		)
		queries: list[tuple[str, dict]] = []

		def get_all(doctype, *, filters=None, **_kwargs):
			queries.append((doctype, filters or {}))
			if doctype == "IONE QC Indicator Version":
				return ["VERSION-1"]
			if doctype == "IONE Indicator Result Detail":
				return ["RESULT-1"]
			if doctype == "IONE Indicator Result":
				return [result_row]
			raise AssertionError(f"Unexpected query: {doctype}")

		with (
			patch.object(tools, "_assert_authenticated"),
			patch.object(tools, "_get_active_task", return_value=task),
			patch.object(tools, "require_scope_read"),
			patch.object(tools.frappe.db, "get_value", return_value="INDICATOR-1"),
			patch.object(tools.frappe, "get_all", side_effect=get_all),
			patch.object(tools, "record_tool_access") as record_access,
			self.assertRaises(frappe.PermissionError),
		):
			tools.ione_get_indicator_trend.func(
				indicator_code="IND-1",
				task=task.name,
				department=None,
				periods=6,
			)

		detail_filters = next(
			filters for doctype, filters in queries if doctype == "IONE Indicator Result Detail"
		)
		self.assertEqual(
			detail_filters,
			{
				"hospital": "HOSP-1",
				"campus": "CAMPUS-1",
				"department": "DEPT-1",
				"ward": "WARD-1",
				"patient": "PATIENT-1",
				"encounter": "ENCOUNTER-1",
				"responsible_staff": "STAFF-1",
				"indicator_result": "RESULT-1",
			},
		)
		result_filters = next(filters for doctype, filters in queries if doctype == "IONE Indicator Result")
		for fieldname in ("hospital", "campus", "department", "ward"):
			self.assertEqual(result_filters[fieldname], task.get(fieldname))
		self.assertEqual(result_filters["name"], ["in", ["RESULT-1"]])
		record_access.assert_not_called()

	def test_contributing_case_with_cross_scope_row_is_rejected(self) -> None:
		task = _task(finding=None)
		result = _Doc(
			name="RESULT-1",
			hospital="HOSP-1",
			campus="CAMPUS-1",
			department="DEPT-1",
			ward="WARD-1",
		)
		case = frappe._dict(
			name="DETAIL-1",
			hospital="HOSP-1",
			campus="OTHER-CAMPUS",
			department="DEPT-1",
			ward="WARD-1",
			patient="PATIENT-1",
			encounter="ENCOUNTER-1",
			responsible_staff="STAFF-1",
			contribution_value=1,
			reason_code="CASE",
			source_reference_hash="SOURCE-HASH",
			detail_key="DETAIL-KEY",
			lineage_json="{}",
			modified="2026-02-01",
		)
		with (
			patch.object(tools, "_assert_authenticated"),
			patch.object(tools, "_get_active_task", return_value=task),
			patch.object(tools.frappe, "get_doc", return_value=result),
			patch.object(tools, "_require_requester_read"),
			patch.object(tools.frappe, "get_all", return_value=[case]) as get_all,
			patch.object(tools, "record_tool_access") as record_access,
			self.assertRaises(frappe.PermissionError),
		):
			tools.ione_get_contributing_cases.func(
				task=task.name,
				indicator_result=None,
				limit=10,
			)

		self.assertEqual(
			get_all.call_args.kwargs["filters"],
			{
				"indicator_result": "RESULT-1",
				"hospital": "HOSP-1",
				"campus": "CAMPUS-1",
				"department": "DEPT-1",
				"ward": "WARD-1",
				"patient": "PATIENT-1",
				"encounter": "ENCOUNTER-1",
				"responsible_staff": "STAFF-1",
			},
		)
		record_access.assert_not_called()

	def test_candidate_and_report_copy_all_supported_task_scope_fields(self) -> None:
		task = _task(
			task_type="Quality Report",
			origin="Scheduled",
			requires_human_review=1,
			report_schedule="SCHEDULE-1",
			report_snapshot="SNAPSHOT-1",
			report_reviewer="report-reviewer@example.com",
			patient=None,
			encounter=None,
			responsible_staff=None,
			indicator_result=None,
			finding=None,
		)
		snapshot_hash = "a" * 64
		snapshot = _Doc(
			name="SNAPSHOT-1",
			hospital=task.hospital,
			campus=task.campus,
			department=task.department,
			ward=task.ward,
			data_hash=snapshot_hash,
		)
		reference = SimpleNamespace(
			reference="IONE AI Data Access Log:ACCESS-1",
			source_doctype="IONE Quality Report Snapshot",
			source_name=snapshot.name,
			source_record_hash=snapshot_hash,
			content_hash=snapshot_hash,
		)
		created: list[_Doc] = []

		def get_doc(value, *args, **kwargs):
			if not isinstance(value, dict):
				raise AssertionError((value, args, kwargs))
			doc = _Doc(**value)
			created.append(doc)
			return doc

		with (
			patch.object(tools, "_assert_authenticated"),
			patch.object(tools, "_assert_writable_task", return_value=task),
			patch.object(tools, "_active_policy_for_tool"),
			patch.object(
				tools,
				"validated_quality_report_snapshot_for_task",
				return_value=(snapshot, {}),
			),
			patch.object(tools, "_require_requester_read"),
			patch.object(tools, "validate_evidence_references", return_value=[reference]),
			patch.object(
				tools,
				"_report_draft_execution_provenance",
				return_value=(2, "FLOW-RUN-2"),
			),
			patch.object(tools.frappe, "get_doc", side_effect=get_doc),
			patch.object(
				tools.frappe.db,
				"get_value",
				side_effect=["Department Monthly", None],
			),
			patch.object(tools.frappe.db, "advisory_lock", return_value=nullcontext()),
		):
			tools.ione_create_candidate_finding.func(
				task=task.name,
				title="Candidate",
				rationale="Evidence",
				severity="Medium",
				evidence_references=[],
			)
			tools.ione_create_report_draft.func(
				task=task.name,
				report_type="Department Monthly",
				content="Draft",
				source_references=[reference.reference],
			)

		candidate, report = created
		for fieldname in ("hospital", "campus", "department", "ward", "patient", "encounter"):
			self.assertEqual(candidate.get(fieldname), task.get(fieldname))
		for fieldname in ("hospital", "campus", "department", "ward"):
			self.assertEqual(report.get(fieldname), task.get(fieldname))
		self.assertEqual(report.execution_attempt, 2)
		self.assertEqual(report.flow_run, "FLOW-RUN-2")
