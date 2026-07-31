from __future__ import annotations

import hashlib
from contextlib import nullcontext
from datetime import date, datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from ione_qms.ai import orchestrator
from ione_qms.api import ai as ai_api
from ione_qms.services import ai_report_schedules as schedules
from ione_qms.services import ai_tasks


class _Doc(SimpleNamespace):
	def get(self, fieldname: str, default=None):
		return getattr(self, fieldname, default)

	def set(self, fieldname: str, value) -> None:
		setattr(self, fieldname, value)

	def update(self, values: dict) -> None:
		for fieldname, value in values.items():
			setattr(self, fieldname, value)

	def check_permission(self, *_args, **_kwargs) -> None:
		self.permission_checked = True

	def save(self, **kwargs):
		self.save_calls = [*(getattr(self, "save_calls", [])), kwargs]
		return self


def _schedule(**overrides) -> _Doc:
	values = {
		"name": "SCHEDULE-1",
		"schedule_code": "MONTHLY-1",
		"schedule_title": "Monthly quality report",
		"report_type": "Department Monthly",
		"policy": "POLICY-1",
		"requested_by": "requester@example.test",
		"report_reviewer": "report-reviewer@example.test",
		"hospital": "HOSP-1",
		"campus": None,
		"department": "DEPT-1",
		"ward": None,
		"run_day": 5,
		"coverage_start_period": date(2026, 1, 1),
		"required_indicator_codes_json": '["IND-1","IND-2"]',
		"status": "Approved",
		"enabled": 1,
		"approval_checksum": None,
		"last_period_start": None,
		"last_period_end": None,
		"last_result": None,
	}
	values.update(overrides)
	doc = _Doc(**values)
	if doc.approval_checksum is None and doc.status != "Draft":
		doc.approval_checksum = schedules._schedule_checksum(doc)
	return doc


def _snapshot_payload(
	*,
	required_codes: list[str] | None = None,
	observed_codes: list[str] | None = None,
	missing_codes: list[str] | None = None,
	complete: bool = True,
) -> dict:
	required_codes = required_codes if required_codes is not None else ["IND-1"]
	observed_codes = observed_codes if observed_codes is not None else ["IND-1"]
	missing_codes = missing_codes if missing_codes is not None else []
	return {
		"contract_version": "1",
		"period": {
			"start": "2026-01-01",
			"end": "2026-01-31",
			"semantics": "closed calendar month",
		},
		"scope": {
			"hospital": "HOSP-1",
			"campus": None,
			"department": "DEPT-1",
			"ward": None,
		},
		"generated_at": "2026-02-01T06:00:00",
		"indicator_coverage": {
			"required_codes": required_codes,
			"observed_codes": observed_codes,
			"missing_codes": missing_codes,
			"complete": complete,
		},
		"indicator_summary": [
			{
				"indicator_code": "IND-1",
				"indicator_name": "Indicator 1",
				"direction": "Higher is Better",
				"indicator_version": "IND-V1",
				"numerator": 9,
				"denominator": 10,
				"indicator_value": 0.9,
				"target_value": 0.95,
				"target_min": None,
				"target_max": None,
				"status": "Below Target",
				"result_count": 10,
			}
		],
		"finding_summary": [],
		"rectification_summary": [],
		"pdca_current_summary": [],
		"semantic_notes": {
			"findings": "Monthly cohort.",
			"rectifications": "Current state.",
			"pdca": "Current state.",
			"patient_identifiers": "Not included.",
		},
	}


def _snapshot_doc(payload: dict | None = None) -> _Doc:
	payload = payload or _snapshot_payload()
	data_json = schedules._canonical_json(payload)
	return _Doc(
		name="SNAPSHOT-1",
		schedule="SCHEDULE-1",
		snapshot_key="a" * 64,
		period_start=date(2026, 1, 1),
		period_end=date(2026, 1, 31),
		hospital="HOSP-1",
		campus=None,
		department="DEPT-1",
		ward=None,
		generated_for="requester@example.test",
		report_reviewer="report-reviewer@example.test",
		generated_at=datetime(2026, 2, 1, 6, 0, 0),
		source_cutoff=datetime(2026, 2, 1, 6, 0, 0),
		source_group_count=schedules._snapshot_group_count(payload),
		data_json=data_json,
		data_hash=hashlib.sha256(data_json.encode()).hexdigest(),
	)


class TestAIReportScheduleGovernance(TestCase):
	def test_task_provenance_accepts_base_schedule_and_requires_complete_recovery_set(self) -> None:
		task = _Doc(
			task_type="Quality Report",
			origin="Scheduled",
			report_schedule="SCHEDULE-1",
			report_snapshot="SNAPSHOT-1",
			report_period_start=date(2026, 1, 1),
			report_period_end=date(2026, 1, 31),
			report_reviewer="reviewer@example.test",
			dispatch_key="d" * 64,
			schedule_approval_checksum="a" * 64,
			prior_report_task=None,
			report_recovery_authorization=None,
			recovery_sequence=0,
			recovery_authorization_checksum=None,
			requested_by="requester@example.test",
			record_count=1,
			requires_human_review=1,
			data_classification="Hospital Internal",
			patient=None,
			encounter=None,
			responsible_staff=None,
			indicator_result=None,
			finding=None,
		)
		ai_tasks._assert_task_provenance(task)

		task.prior_report_task = "TASK-FAILED"
		with self.assertRaises(frappe.ValidationError):
			ai_tasks._assert_task_provenance(task)

		task.report_recovery_authorization = "AUTH-1"
		task.recovery_sequence = 1
		task.recovery_authorization_checksum = "c" * 64
		ai_tasks._assert_task_provenance(task)

		task.recovery_sequence = 3
		with self.assertRaises(frappe.ValidationError):
			ai_tasks._assert_task_provenance(task)

	def test_recovery_authorization_creates_distinct_task_and_preserves_terminal_task(self) -> None:
		period_start = date(2026, 1, 1)
		period_end = date(2026, 1, 31)
		schedule = _schedule(
			last_period_start=period_start,
			last_period_end=period_end,
			last_result="Failed",
			last_task="TASK-FAILED",
		)
		prior_task = _Doc(
			name="TASK-FAILED",
			status="Failed",
			dispatch_key=schedules._dispatch_key(schedule, period_start, period_end),
			recovery_sequence=0,
		)
		schedule.last_dispatch_key = prior_task.dispatch_key
		snapshot = _Doc(name="SNAPSHOT-1")
		captured: dict[str, _Doc] = {}

		def get_doc(doctype_or_payload, *args, **kwargs):
			if isinstance(doctype_or_payload, dict):
				receipt = _Doc(**doctype_or_payload)
				receipt.name = receipt.authorization_key

				def insert(**insert_kwargs):
					receipt.insert_kwargs = insert_kwargs
					return receipt

				receipt.insert = insert
				captured["receipt"] = receipt
				return receipt
			if doctype_or_payload == "IONE AI Report Schedule":
				self.assertTrue(kwargs.get("for_update"))
				return schedule
			self.fail(f"Unexpected get_doc call: {doctype_or_payload!r}, {args!r}")

		recovery_task = _Doc(name="TASK-RECOVERY-1")
		with (
			patch.object(
				schedules,
				"_named_user",
				return_value=schedule.report_reviewer,
			),
			patch.object(schedules, "_eligible_user", side_effect=lambda user, _roles: user),
			patch.object(schedules, "_validate_schedule_definition"),
			patch.object(schedules, "_assert_approval_checksum"),
			patch.object(schedules.frappe.db, "advisory_lock", return_value=nullcontext()),
			patch.object(schedules.frappe, "get_doc", side_effect=get_doc),
			patch.object(
				schedules,
				"_current_recovery_target",
				return_value=(period_start, period_end, prior_task),
			),
			patch.object(schedules, "_idempotent_recovery_replay", return_value=None),
			patch.object(schedules, "_existing_dispatch_task_state", return_value="failed"),
			patch.object(
				schedules,
				"validated_quality_report_snapshot_for_task",
				return_value=(snapshot, {}),
			),
			patch.object(schedules, "_period_recovery_receipts", return_value=[]),
			patch.object(schedules, "_accountable_user", return_value=nullcontext()),
			patch.object(
				schedules,
				"_create_scheduled_task",
				return_value=recovery_task,
			) as create_task,
			patch.object(schedules, "_enqueue_scheduled_task_after_commit") as enqueue_task,
			patch.object(schedules.frappe.db, "savepoint"),
			patch.object(schedules.frappe.db, "release_savepoint"),
			patch.object(schedules.frappe.db, "commit") as commit,
		):
			result = schedules.authorize_report_recovery(
				schedule.name,
				prior_task.name,
				"MODEL.TIMEOUT",
				"Reviewed provider timeout; authorize one replacement task.",
			)

		receipt = captured["receipt"]
		self.assertEqual(receipt.prior_task, prior_task.name)
		self.assertEqual(receipt.prior_terminal_status, "Failed")
		self.assertEqual(receipt.prior_dispatch_key, prior_task.dispatch_key)
		self.assertEqual(receipt.report_snapshot, snapshot.name)
		self.assertEqual(receipt.recovery_sequence, 1)
		self.assertEqual(receipt.authorized_by, schedule.report_reviewer)
		self.assertNotEqual(receipt.recovery_dispatch_key, prior_task.dispatch_key)
		self.assertEqual(receipt.authorization_checksum, schedules._recovery_checksum(receipt))
		self.assertEqual(receipt.insert_kwargs, {"ignore_permissions": True})
		create_task.assert_called_once()
		self.assertIs(create_task.call_args.args[0], schedule)
		self.assertIs(create_task.call_args.kwargs["prior_task"], prior_task)
		self.assertIs(create_task.call_args.kwargs["recovery_authorization"], receipt)
		self.assertEqual(schedule.last_task, recovery_task.name)
		self.assertEqual(schedule.last_dispatch_key, receipt.recovery_dispatch_key)
		self.assertEqual(schedule.last_period_start, period_start)
		self.assertEqual(schedule.last_period_end, period_end)
		self.assertEqual(schedule.last_result, "Success")
		self.assertEqual(result["status"], "Created")
		self.assertEqual(result["recovery_sequence"], 1)
		self.assertEqual(prior_task.status, "Failed")
		enqueue_task.assert_called_once_with(recovery_task)
		commit.assert_called_once_with()

	def test_recovery_limit_creates_incident_and_keeps_month_failed(self) -> None:
		period_start = date(2026, 1, 1)
		period_end = date(2026, 1, 31)
		schedule = _schedule(
			last_period_start=period_start,
			last_period_end=period_end,
			last_result="Failed",
			last_task="TASK-RECOVERY-2",
		)
		prior_task = _Doc(
			name="TASK-RECOVERY-2",
			status="Rejected",
			dispatch_key="d" * 64,
			recovery_sequence=2,
		)
		schedule.last_dispatch_key = prior_task.dispatch_key
		with (
			patch.object(
				schedules,
				"_named_user",
				return_value=schedule.report_reviewer,
			),
			patch.object(schedules, "_eligible_user", side_effect=lambda user, _roles: user),
			patch.object(schedules, "_validate_schedule_definition"),
			patch.object(schedules, "_assert_approval_checksum"),
			patch.object(schedules.frappe.db, "advisory_lock", return_value=nullcontext()),
			patch.object(schedules.frappe, "get_doc", return_value=schedule),
			patch.object(
				schedules,
				"_current_recovery_target",
				return_value=(period_start, period_end, prior_task),
			),
			patch.object(schedules, "_idempotent_recovery_replay", return_value=None),
			patch.object(schedules, "_existing_dispatch_task_state", return_value="failed"),
			patch.object(
				schedules,
				"validated_quality_report_snapshot_for_task",
				return_value=(_Doc(name="SNAPSHOT-1"), {}),
			),
			patch.object(
				schedules,
				"_period_recovery_receipts",
				return_value=[
					{"name": "AUTH-1", "recovery_sequence": 1},
					{"name": "AUTH-2", "recovery_sequence": 2},
				],
			),
			patch.object(
				schedules,
				"_ensure_recovery_exhaustion_incident",
				return_value="INCIDENT-1",
			) as incident,
			patch.object(schedules.frappe.db, "commit") as commit,
			patch.object(schedules, "_create_scheduled_task") as create_task,
		):
			result = schedules.authorize_report_recovery(
				schedule.name,
				prior_task.name,
				"MODEL.TIMEOUT",
				"Second replacement task is terminal; escalate for investigation.",
			)

		incident.assert_called_once()
		create_task.assert_not_called()
		self.assertEqual(schedule.last_task, prior_task.name)
		self.assertEqual(schedule.last_dispatch_key, prior_task.dispatch_key)
		self.assertEqual(schedule.last_period_start, period_start)
		self.assertEqual(schedule.last_period_end, period_end)
		self.assertEqual(schedule.last_result, "Failed")
		self.assertEqual(schedule.last_error_code, "RECOVERY_LIMIT_EXHAUSTED")
		self.assertEqual(result["status"], "Exhausted")
		self.assertEqual(result["incident"], "INCIDENT-1")
		commit.assert_called_once_with()

	def test_recovery_request_replay_returns_existing_waiting_task(self) -> None:
		period_start = date(2026, 1, 1)
		period_end = date(2026, 1, 31)
		schedule = _schedule()
		prior_task_name = "TASK-TERMINAL-1"
		task = _Doc(
			name="TASK-RECOVERY-1",
			report_recovery_authorization="AUTH-1",
			status="Pending",
		)
		receipt = _Doc(
			name="AUTH-1",
			period_start=period_start,
			period_end=period_end,
			authorized_by=schedule.report_reviewer,
			reason_code="MODEL.TIMEOUT",
			reason="Reviewed provider timeout; authorize one replacement task.",
			recovery_sequence=1,
		)

		def get_doc(doctype, name):
			if doctype == schedules.REPORT_RECOVERY_DOCTYPE and name == receipt.name:
				return receipt
			if doctype == "IONE AI Analysis Task" and name == task.name:
				return task
			self.fail(f"Unexpected get_doc call: {doctype!r}, {name!r}")

		with (
			patch.object(
				schedules.frappe.db,
				"get_value",
				side_effect=[receipt.name, task.name],
			),
			patch.object(schedules.frappe, "get_doc", side_effect=get_doc),
			patch.object(schedules, "_assert_recovery_authorization_integrity") as integrity,
			patch.object(schedules, "validated_quality_report_snapshot_for_task") as validate_task,
			patch.object(schedules, "_enqueue_scheduled_task_after_commit") as enqueue,
		):
			result = schedules._idempotent_recovery_replay(
				schedule,
				prior_task_name,
				authorized_by=schedule.report_reviewer,
				reason_code=receipt.reason_code,
				reason=receipt.reason,
			)

		integrity.assert_called_once_with(
			receipt,
			schedule,
			require_current_runtime=False,
		)
		validate_task.assert_called_once_with(task)
		enqueue.assert_called_once_with(task)
		self.assertEqual(
			result,
			{
				"schedule": schedule.name,
				"authorization": receipt.name,
				"task": task.name,
				"recovery_sequence": 1,
				"status": "Existing",
			},
		)

	def test_after_commit_queue_callback_never_touches_redis_before_registration_or_rethrows(self) -> None:
		task = _Doc(name="TASK-PENDING-1", execution_attempt=0, status="Pending")
		callbacks = []
		with (
			patch.object(
				schedules.frappe.db.after_commit,
				"add",
				side_effect=callbacks.append,
			) as add,
			patch.object(
				orchestrator,
				"_enqueue_task_name",
				side_effect=RuntimeError("redis unavailable"),
			) as enqueue,
			patch.object(
				schedules.frappe,
				"log_error",
				side_effect=RuntimeError("logging unavailable"),
			),
		):
			schedules._enqueue_scheduled_task_after_commit(task)
			enqueue.assert_not_called()
			self.assertEqual(len(callbacks), 1)
			callbacks[0]()

		add.assert_called_once()
		enqueue.assert_called_once_with(
			task.name,
			attempt_hint=1,
			enqueue_after_commit=False,
		)
		self.assertEqual(task.status, "Pending")

	def test_pending_sweep_repair_allows_successful_month_to_advance(self) -> None:
		period_start = date(2026, 1, 1)
		period_end = date(2026, 1, 31)
		schedule = _schedule(
			last_period_start=period_start,
			last_period_end=period_end,
			last_result="Success",
			last_task="TASK-PENDING-1",
			last_dispatch_key="d" * 64,
		)
		task = _Doc(
			name=schedule.last_task,
			execution_attempt=0,
			status="Pending",
			dispatch_key=schedule.last_dispatch_key,
		)
		with (
			patch.object(orchestrator, "ai_runtime_enabled", return_value=True),
			patch.object(
				orchestrator.frappe,
				"get_all",
				return_value=[{"name": task.name, "execution_attempt": 0}],
			),
			patch.object(orchestrator, "_enqueue_task_name") as enqueue,
		):
			self.assertEqual(orchestrator._enqueue_pending_tasks(limit=1), 1)
		enqueue.assert_called_once()

		task.status = "Completed"
		with (
			patch.object(schedules.frappe, "get_doc", return_value=task),
			patch.object(schedules, "_existing_dispatch_task_state", return_value="completed"),
		):
			self.assertEqual(
				schedules._next_due_period(
					schedule,
					date(2026, 2, 1),
					date(2026, 2, 28),
				),
				(date(2026, 2, 1), date(2026, 2, 28)),
			)

	def test_non_configured_reviewer_cannot_authorize_recovery(self) -> None:
		schedule = _schedule()
		with (
			patch.object(
				schedules,
				"_named_user",
				return_value="other-reviewer@example.test",
			),
			patch.object(schedules, "_eligible_user", side_effect=lambda user, _roles: user),
			patch.object(schedules, "_validate_schedule_definition"),
			patch.object(schedules, "_assert_approval_checksum"),
			patch.object(schedules.frappe.db, "advisory_lock", return_value=nullcontext()),
			patch.object(schedules.frappe, "get_doc", return_value=schedule),
			patch.object(schedules.frappe.db, "commit") as commit,
			self.assertRaises(frappe.PermissionError),
		):
			schedules.authorize_report_recovery(
				schedule.name,
				"TASK-TERMINAL-1",
				"MODEL.TIMEOUT",
				"Independent recovery review reason.",
			)

		commit.assert_not_called()

	def test_approval_freezes_first_closed_coverage_month_before_checksum(self) -> None:
		doc = _schedule(
			status="Draft",
			enabled=0,
			coverage_start_period=None,
			approval_checksum=None,
		)
		with (
			patch.object(
				schedules,
				"_named_role_user",
				return_value="schedule-reviewer@example.test",
			),
			patch.object(schedules, "_validate_schedule_definition"),
			patch.object(schedules.frappe.db, "advisory_lock", return_value=nullcontext()),
			patch.object(schedules.frappe, "get_doc", return_value=doc),
			patch.object(
				schedules,
				"now_datetime",
				return_value=datetime(2026, 7, 30, 12, 0, 0),
			),
			patch.object(schedules, "nowdate", return_value="2026-07-30"),
			patch.object(schedules.frappe.db, "commit") as commit,
		):
			result = schedules.review_report_schedule(
				doc.name,
				"Approve",
				"independent approval",
			)

		self.assertEqual(doc.coverage_start_period, date(2026, 6, 1))
		self.assertEqual(doc.approval_checksum, schedules._schedule_checksum(doc))
		self.assertEqual(doc.status, "Approved")
		self.assertEqual(doc.enabled, 1)
		self.assertEqual(result["status"], "Approved")
		self.assertEqual(doc.save_calls, [{"ignore_permissions": True}])
		commit.assert_called_once_with()

	def test_requester_cannot_approve_own_schedule(self) -> None:
		doc = _schedule(
			status="Draft",
			enabled=0,
			coverage_start_period=None,
			approval_checksum=None,
		)
		with (
			patch.object(
				schedules,
				"_named_role_user",
				return_value=doc.requested_by,
			),
			patch.object(schedules.frappe.db, "advisory_lock", return_value=nullcontext()),
			patch.object(schedules.frappe, "get_doc", return_value=doc),
			patch.object(schedules.frappe.db, "commit") as commit,
			self.assertRaises(frappe.ValidationError),
		):
			schedules.review_report_schedule(
				doc.name,
				"Approve",
				"self approval denied",
			)

		self.assertFalse(hasattr(doc, "save_calls"))
		commit.assert_not_called()

	def test_first_run_and_retry_progress_from_frozen_coverage_period(self) -> None:
		latest_start = date(2026, 3, 1)
		latest_end = date(2026, 3, 31)
		doc = _schedule(coverage_start_period=date(2026, 1, 1))
		self.assertEqual(
			schedules._next_due_period(doc, latest_start, latest_end),
			(date(2026, 1, 1), date(2026, 1, 31)),
		)

		doc.last_period_start = date(2026, 1, 1)
		doc.last_period_end = date(2026, 1, 31)
		doc.last_result = "Failed"
		self.assertEqual(
			schedules._next_due_period(doc, latest_start, latest_end),
			(date(2026, 1, 1), date(2026, 1, 31)),
		)

		doc.last_result = "Success"
		doc.last_task = "TASK-1"
		task = _Doc(
			name=doc.last_task,
			dispatch_key=schedules._dispatch_key(
				doc,
				date(2026, 1, 1),
				date(2026, 1, 31),
			),
		)
		doc.last_dispatch_key = task.dispatch_key
		with (
			patch.object(schedules.frappe, "get_doc", return_value=task),
			patch.object(schedules, "_existing_dispatch_task_state", return_value="completed"),
		):
			self.assertEqual(
				schedules._next_due_period(doc, latest_start, latest_end),
				(date(2026, 2, 1), date(2026, 2, 28)),
			)

	def test_more_than_twelve_pending_months_fails_closed(self) -> None:
		allowed = _schedule(coverage_start_period=date(2025, 2, 1))
		self.assertEqual(
			schedules._next_due_period(
				allowed,
				date(2026, 1, 1),
				date(2026, 1, 31),
			),
			(date(2025, 2, 1), date(2025, 2, 28)),
		)
		for latest_start, latest_end in (
			(date(2026, 1, 1), date(2026, 1, 31)),
			(date(2026, 3, 1), date(2026, 3, 31)),
		):
			with self.subTest(latest_start=latest_start):
				doc = _schedule(coverage_start_period=date(2025, 1, 1))
				with self.assertRaises(frappe.ValidationError):
					schedules._next_due_period(
						doc,
						latest_start,
						latest_end,
					)

	def test_dispatcher_isolates_one_bad_schedule_and_continues(self) -> None:
		with (
			patch.object(schedules, "ai_runtime_enabled", return_value=True),
			patch.object(schedules, "nowdate", return_value="2026-07-30"),
			patch.object(
				schedules.frappe,
				"get_all",
				return_value=["SCHEDULE-BAD", "SCHEDULE-GOOD"],
			),
			patch.object(
				schedules,
				"_dispatch_one_schedule",
				side_effect=[
					frappe.ValidationError("bad schedule"),
					"created",
				],
			),
			patch.object(schedules.frappe.db, "rollback") as rollback,
			patch.object(schedules.frappe, "log_error") as log_error,
		):
			result = schedules.dispatch_monthly_quality_reports()

		self.assertEqual(
			result,
			{
				"due": 2,
				"created": 1,
				"existing": 0,
				"failed": 1,
				"disabled": 0,
			},
		)
		rollback.assert_called_once_with()
		log_error.assert_called_once()

	def test_runtime_save_failure_rolls_back_task_before_recording_no_task_failure(self) -> None:
		period_start = date(2026, 6, 1)
		period_end = date(2026, 6, 30)
		dirty_schedule = _schedule(coverage_start_period=period_start)
		fresh_schedule = _schedule(coverage_start_period=period_start)
		task = _Doc(name="TASK-ROLLED-BACK")
		snapshot = _Doc(name="SNAPSHOT-ROLLED-BACK")
		update_calls = 0

		def update_runtime(schedule, **values):
			nonlocal update_calls
			update_calls += 1
			schedule.update(
				{
					"last_period_start": values["period_start"],
					"last_period_end": values["period_end"],
					"last_dispatch_key": values["dispatch_key"],
					"last_task": values["task"],
					"last_result": values["result"],
					"last_error_code": values["error_code"],
				}
			)
			if update_calls == 1:
				raise RuntimeError("injected schedule save failure")

		with (
			patch.object(schedules.frappe.db, "advisory_lock", return_value=nullcontext()),
			patch.object(
				schedules.frappe,
				"get_doc",
				side_effect=[dirty_schedule, fresh_schedule],
			),
			patch.object(schedules, "_validate_schedule_definition"),
			patch.object(schedules, "_assert_approval_checksum"),
			patch.object(schedules, "_current_runtime_task_state", return_value=None),
			patch.object(
				schedules,
				"_next_due_period",
				return_value=(period_start, period_end),
			),
			patch.object(schedules.frappe.db, "get_value", return_value=None),
			patch.object(schedules, "_accountable_user", return_value=nullcontext()),
			patch.object(schedules, "_get_or_create_snapshot", return_value=snapshot),
			patch.object(schedules, "_create_scheduled_task", return_value=task),
			patch.object(schedules, "_update_schedule_runtime", side_effect=update_runtime),
			patch.object(schedules, "_enqueue_scheduled_task_after_commit") as enqueue,
			patch.object(schedules.frappe.db, "savepoint"),
			patch.object(schedules.frappe.db, "rollback") as rollback,
			patch.object(schedules.frappe.db, "commit") as commit,
		):
			result = schedules._dispatch_one_schedule(
				dirty_schedule.name,
				date(2026, 7, 30),
			)

		self.assertEqual(result, "failed")
		self.assertEqual(update_calls, 2)
		rollback.assert_called_once_with()
		enqueue.assert_not_called()
		commit.assert_called_once_with()
		self.assertIsNone(fresh_schedule.last_task)
		self.assertIsNone(fresh_schedule.last_dispatch_key)
		self.assertEqual(fresh_schedule.last_result, "Failed")
		self.assertEqual(fresh_schedule.last_error_code, "UNEXPECTED_FAILURE")

	def test_existing_dispatch_task_state_is_exact_and_never_advances_terminal_or_waiting(self) -> None:
		schedule = _schedule(last_task="TASK-1")
		period_start = date(2026, 1, 1)
		period_end = date(2026, 1, 31)
		task = _Doc(
			name="TASK-1",
			status="Failed",
			report_period_start=period_start,
			report_period_end=period_end,
			dispatch_key=schedules._dispatch_key(
				schedule,
				period_start,
				period_end,
			),
		)
		with patch.object(
			schedules,
			"validated_quality_report_snapshot_for_task",
		):
			self.assertEqual(
				schedules._existing_dispatch_task_state(
					schedule,
					task,
					period_start,
					period_end,
				),
				"failed",
			)
			task.status = "Pending"
			self.assertEqual(
				schedules._existing_dispatch_task_state(
					schedule,
					task,
					period_start,
					period_end,
				),
				"waiting",
			)

			task.status = "Completed"
			with (
				patch.object(schedules.frappe.db, "exists", return_value=False),
				self.assertRaises(frappe.ValidationError),
			):
				schedules._existing_dispatch_task_state(
					schedule,
					task,
					period_start,
					period_end,
				)
			with patch.object(schedules.frappe.db, "exists", return_value=True):
				self.assertEqual(
					schedules._existing_dispatch_task_state(
						schedule,
						task,
						period_start,
						period_end,
					),
					"completed",
				)

	def test_failed_and_waiting_existing_tasks_cannot_be_attributed_to_another_period(self) -> None:
		schedule = _schedule(last_task="TASK-1")
		task_start = date(2026, 1, 1)
		task_end = date(2026, 1, 31)
		task = _Doc(
			name="TASK-1",
			status="Failed",
			report_period_start=task_start,
			report_period_end=task_end,
			dispatch_key=schedules._dispatch_key(
				schedule,
				task_start,
				task_end,
			),
		)
		with patch.object(
			schedules,
			"validated_quality_report_snapshot_for_task",
		):
			for status in ("Failed", "Pending"):
				with self.subTest(status=status), self.assertRaises(frappe.ValidationError):
					task.status = status
					schedules._existing_dispatch_task_state(
						schedule,
						task,
						date(2026, 2, 1),
						date(2026, 2, 28),
					)

	def test_suspended_or_retired_schedule_revokes_snapshot_execution(self) -> None:
		for status in ("Suspended", "Retired"):
			with self.subTest(status=status):
				schedule = _schedule(status=status, enabled=0)
				period_start = date(2026, 1, 1)
				period_end = date(2026, 1, 31)
				task = _Doc(
					name="TASK-1",
					task_type="Quality Report",
					origin="Scheduled",
					report_snapshot="SNAPSHOT-1",
					report_schedule=schedule.name,
					report_period_start=period_start,
					report_period_end=period_end,
					schedule_approval_checksum=schedule.approval_checksum,
					dispatch_key=schedules._dispatch_key(
						schedule,
						period_start,
						period_end,
					),
					requested_by=schedule.requested_by,
					report_reviewer=schedule.report_reviewer,
					hospital=schedule.hospital,
					campus=schedule.campus,
					department=schedule.department,
					ward=schedule.ward,
				)
				loaded: list[str] = []

				def get_doc(doctype, name, *, _loaded=loaded, _schedule=schedule):
					_loaded.append(str(doctype))
					if doctype == "IONE AI Report Schedule":
						return _schedule
					self.fail("Suspended/retired task loaded its report snapshot")

				with (
					patch.object(schedules.frappe, "get_doc", side_effect=get_doc),
					patch.object(schedules, "_eligible_user", side_effect=lambda user, _roles: user),
					patch.object(schedules, "require_scope_read"),
					self.assertRaises(frappe.ValidationError),
				):
					schedules.validated_quality_report_snapshot_for_task(task)

				self.assertEqual(loaded, ["IONE AI Report Schedule"])

	def test_suspended_schedule_fails_before_model_agent_is_called(self) -> None:
		task = _Doc(
			doctype="IONE AI Analysis Task",
			name="TASK-1",
			policy="POLICY-1",
			task_type="Quality Report",
			origin="Scheduled",
			status="Running",
		)
		claim = orchestrator.ExecutionClaim(
			task=task,
			attempt=1,
			token="t" * 43,
			token_hash=orchestrator._hash_text("t" * 43),
			claimed_at=datetime(2026, 2, 1, 6, 0, 0),
			lease_expires_at=datetime(2026, 2, 1, 6, 30, 0),
		)
		policy = _Doc(
			name="POLICY-1",
			flow_agent="AGENT-1",
			service_user="service@example.test",
			allow_auto_approve=0,
		)
		agent = _Doc(name="AGENT-1", run=MagicMock())

		def get_doc(doctype: str, _name: str):
			return policy if doctype == "IONE Agent Policy" else agent

		with (
			patch.object(orchestrator, "require_ai_runtime_enabled"),
			patch.object(orchestrator, "_claim_analysis_task", return_value=claim),
			patch.object(
				orchestrator.frappe,
				"session",
				SimpleNamespace(user="scheduler@example.test"),
			),
			patch.object(orchestrator.frappe, "flags", SimpleNamespace()),
			patch.object(orchestrator.frappe, "get_doc", side_effect=get_doc),
			patch.object(orchestrator, "validate_policy_runtime"),
			patch.object(
				schedules,
				"validated_quality_report_snapshot_for_task",
				side_effect=frappe.ValidationError("schedule suspended"),
			) as live_provenance,
			patch.object(orchestrator, "_build_prompt") as build_prompt,
			patch.object(orchestrator.frappe, "set_user"),
			patch.object(orchestrator, "_finalize_attempt", return_value=True) as finalize,
			patch.object(orchestrator.frappe, "log_error"),
		):
			result = orchestrator.run_analysis_task(task.name)

		live_provenance.assert_called_once_with(task)
		build_prompt.assert_not_called()
		agent.run.assert_not_called()
		self.assertEqual(finalize.call_args.kwargs["status"], "Failed")
		self.assertIsNone(finalize.call_args.kwargs["agent"])
		self.assertIsNone(finalize.call_args.kwargs["run"])
		self.assertEqual(result["status"], "Failed")
		self.assertIsNone(result["flow_run"])

	def test_live_policy_or_agent_tool_drift_is_rejected_before_snapshot_load(self) -> None:
		schedule = _schedule()
		task = _Doc(
			name="TASK-1",
			task_type="Quality Report",
			origin="Scheduled",
			report_snapshot="SNAPSHOT-1",
			report_schedule=schedule.name,
			report_period_start=date(2026, 1, 1),
			report_period_end=date(2026, 1, 31),
		)
		safe_tools = [
			_Doc(tool="ione_get_quality_summary"),
			_Doc(tool="ione_create_report_draft"),
		]
		extra_tool = _Doc(tool="ione_get_encounter_context")
		for policy_tools, agent_tools in (
			([*safe_tools, extra_tool], safe_tools),
			(safe_tools, [*safe_tools, extra_tool]),
		):
			with self.subTest(
				policy_has_extra=len(policy_tools) > len(safe_tools),
				agent_has_extra=len(agent_tools) > len(safe_tools),
			):
				policy = _Doc(
					name="POLICY-1",
					agent_category="Quality Report",
					status="Active",
					allow_scheduled_run=1,
					contains_patient_data=0,
					requires_human_review=1,
					service_user="service@example.test",
					flow_agent="AGENT-1",
					allowed_tools=policy_tools,
				)
				agent = _Doc(name="AGENT-1", tools=agent_tools)
				loaded: list[str] = []

				def get_doc(
					doctype: str,
					_name: str,
					*,
					_loaded=loaded,
					_policy=policy,
					_agent=agent,
				):
					_loaded.append(doctype)
					if doctype == "IONE AI Report Schedule":
						return schedule
					if doctype == "IONE Agent Policy":
						return _policy
					if doctype == "Flow Agent":
						return _agent
					self.fail("Policy drift must fail before the report snapshot is loaded")

				with (
					patch.object(schedules.frappe, "get_doc", side_effect=get_doc),
					patch.object(
						schedules.frappe,
						"get_all",
						return_value=[
							{"indicator_code": "IND-1"},
							{"indicator_code": "IND-2"},
						],
					),
					patch.object(schedules, "_eligible_user", side_effect=lambda user, _roles: user),
					patch.object(schedules, "require_scope_read"),
					self.assertRaises(frappe.ValidationError),
				):
					schedules.validated_quality_report_snapshot_for_task(task)

				self.assertNotIn("IONE Quality Report Snapshot", loaded)

	def test_snapshot_payload_is_canonical_complete_and_rejects_phi_or_hash_drift(self) -> None:
		valid = _snapshot_doc()
		self.assertEqual(
			schedules._validated_snapshot_payload(valid),
			_snapshot_payload(),
		)

		incomplete_payload = _snapshot_payload(
			missing_codes=["IND-2"],
			complete=False,
		)
		incomplete = _snapshot_doc(incomplete_payload)
		with self.assertRaises(frappe.ValidationError):
			schedules._validated_snapshot_payload(incomplete)

		inconsistent_coverage = _snapshot_doc(
			_snapshot_payload(
				required_codes=["IND-1", "IND-2"],
				observed_codes=["IND-1", "IND-2"],
			)
		)
		with self.assertRaises(frappe.ValidationError):
			schedules._validated_snapshot_payload(inconsistent_coverage)

		phi_payload = _snapshot_payload()
		phi_payload["semantic_notes"]["patient_id"] = "PATIENT-1"
		phi = _snapshot_doc(phi_payload)
		with self.assertRaises(frappe.ValidationError):
			schedules._validated_snapshot_payload(phi)

		hash_drift = _snapshot_doc()
		hash_drift.data_hash = "b" * 64
		with self.assertRaises(frappe.ValidationError):
			schedules._validated_snapshot_payload(hash_drift)

	def test_missing_required_indicator_fact_fails_before_snapshot_creation(self) -> None:
		schedule = _schedule(required_indicator_codes_json='["IND-1","IND-2"]')

		def permissioned_rows(doctype: str, **_kwargs):
			if doctype == "IONE Monthly Quality Fact":
				return [
					{
						"name": "FACT-1",
						"indicator": "INDICATOR-1",
						"indicator_version": "IND-V1",
						"numerator": 9,
						"denominator": 10,
						"indicator_value": 0.9,
						"target_value": 0.95,
						"target_min": None,
						"target_max": None,
						"status": "Below Target",
						"result_count": 10,
					}
				]
			if doctype == "IONE QC Indicator":
				return [
					{
						"name": f"INDICATOR-{number}",
						"indicator_code": f"IND-{number}",
						"indicator_name": f"Indicator {number}",
						"direction": "Higher is Better",
					}
					for number in (1, 2)
				]
			return []

		with (
			patch.object(schedules, "require_scope_read"),
			patch.object(
				schedules,
				"_permissioned_rows",
				side_effect=permissioned_rows,
			),
			patch.object(schedules, "_assert_permissioned_count"),
			self.assertRaises(frappe.ValidationError),
		):
			schedules._quality_summary_data(
				schedule,
				date(2026, 1, 1),
				date(2026, 1, 31),
				datetime(2026, 2, 1, 6, 0, 0),
			)

	def test_complete_indicator_coverage_is_sorted_and_deduplicated(self) -> None:
		schedule = _schedule(required_indicator_codes_json='["IND-2","IND-1","IND-2"]')

		def permissioned_rows(doctype: str, **_kwargs):
			if doctype == "IONE Monthly Quality Fact":
				return [
					{
						"name": f"FACT-{number}",
						"indicator": f"INDICATOR-{number}",
						"indicator_version": f"IND-V{number}",
						"numerator": 9,
						"denominator": 10,
						"indicator_value": 0.9,
						"target_value": 0.95,
						"target_min": None,
						"target_max": None,
						"status": "Below Target",
						"result_count": 10,
					}
					for number in (2, 1)
				]
			if doctype == "IONE QC Indicator":
				return [
					{
						"name": f"INDICATOR-{number}",
						"indicator_code": f"IND-{number}",
						"indicator_name": f"Indicator {number}",
						"direction": "Higher is Better",
					}
					for number in (2, 1)
				]
			return []

		with (
			patch.object(schedules, "require_scope_read"),
			patch.object(
				schedules,
				"_permissioned_rows",
				side_effect=permissioned_rows,
			),
			patch.object(schedules, "_assert_permissioned_count"),
		):
			summary = schedules._quality_summary_data(
				schedule,
				date(2026, 1, 1),
				date(2026, 1, 31),
				datetime(2026, 2, 1, 6, 0, 0),
			)

		self.assertEqual(
			summary["indicator_coverage"],
			{
				"required_codes": ["IND-1", "IND-2"],
				"observed_codes": ["IND-1", "IND-2"],
				"missing_codes": [],
				"complete": True,
			},
		)

	def test_only_preapproved_reviewer_can_review_scheduled_draft(self) -> None:
		draft = _Doc(
			doctype="IONE AI Report Draft",
			name="DRAFT-1",
			status="Draft",
			task="TASK-1",
			source_references="[]",
		)
		task_control = {
			"requested_by": "requester@example.test",
			"origin": "Scheduled",
			"report_reviewer": "approved-reviewer@example.test",
		}
		with (
			patch.object(ai_api, "require_role"),
			patch.object(
				ai_api.frappe.session,
				"user",
				"different-reviewer@example.test",
			),
			patch.object(ai_api.frappe.db, "advisory_lock", return_value=nullcontext()),
			patch.object(ai_api.frappe, "get_doc", return_value=draft),
			patch.object(ai_api.frappe.db, "get_value", return_value=task_control),
			patch.object(ai_api.frappe.db, "commit") as commit,
			self.assertRaises(frappe.PermissionError),
		):
			ai_api.review_report_draft(
				draft.name,
				"Reject",
				"independent review",
			)

		self.assertFalse(hasattr(draft, "save_calls"))
		commit.assert_not_called()
