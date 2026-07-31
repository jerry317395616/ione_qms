from __future__ import annotations

from contextlib import nullcontext
from unittest import TestCase
from unittest.mock import patch

from ione_qms.services import surgery_governance as surgery


class _Doc(dict):
	def __getattr__(self, key):
		try:
			return self[key]
		except KeyError as exc:
			raise AttributeError(key) from exc

	def __setattr__(self, key, value):
		self[key] = value


class TestSurgeryGovernancePureContracts(TestCase):
	def test_checksum_is_canonical_and_binds_source_snapshot(self) -> None:
		first = {
			"source_record_id_hash": "a" * 64,
			"record_snapshot_hash": "b" * 64,
			"policy": "POLICY-1",
		}
		second = {
			"policy": "POLICY-1",
			"record_snapshot_hash": "b" * 64,
			"source_record_id_hash": "a" * 64,
		}
		self.assertEqual(surgery._checksum(first), surgery._checksum(second))
		self.assertNotEqual(
			surgery._checksum(first),
			surgery._checksum({**first, "record_snapshot_hash": "c" * 64}),
		)

	def test_participant_manifest_is_exact_deduplicable_and_order_independent(self) -> None:
		rows = surgery._participant_rows(
			[
				{
					"medical_staff": "STAFF-2",
					"participant_role": "Surgeon",
					"confirmed_at": "2026-07-30 09:02:00",
					"evidence_reference": "MEETING-1/P2",
				},
				{
					"medical_staff": "STAFF-1",
					"participant_role": "Anesthesiologist",
					"confirmed_at": "2026-07-30 09:01:00",
					"evidence_reference": "MEETING-1/P1",
				},
			]
		)
		self.assertEqual(
			[(row["participant_role"], row["medical_staff"]) for row in rows],
			[("Anesthesiologist", "STAFF-1"), ("Surgeon", "STAFF-2")],
		)
		with (
			patch.object(surgery.frappe, "throw", side_effect=RuntimeError("exact fields")),
			self.assertRaisesRegex(RuntimeError, "exact fields"),
		):
			surgery._participant_rows(
				[
					{
						"medical_staff": "STAFF-1",
						"participant_role": "Surgeon",
						"confirmed_at": "2026-07-30 09:00:00",
						"evidence_reference": "MEETING-1",
						"unexpected": "not allowed",
					}
				]
			)

	def test_checklist_results_are_canonical_and_reject_unknown_results(self) -> None:
		results = surgery._check_results(
			[
				{"item_code": "TIMEOUT", "result": "Pass"},
				{"item_code": "SITE", "result": "Pass"},
			]
		)
		self.assertEqual(list(results), ["SITE", "TIMEOUT"])
		with (
			patch.object(surgery.frappe, "throw", side_effect=RuntimeError("unsupported")),
			self.assertRaisesRegex(RuntimeError, "unsupported"),
		):
			surgery._check_results([{"item_code": "TIMEOUT", "result": "Assumed"}])

	def test_structured_occurrence_validates_care_path_and_outcome_timestamps(self) -> None:
		values = {
			"surgery_time": "2026-07-30 10:00:00",
			"source_finalized_at": "2026-07-30 14:00:00",
			"preanesthesia_status": "Completed",
			"preanesthesia_completed_at": "2026-07-30 09:00:00",
			"intraoperative_monitoring_status": "Completed",
			"monitoring_started_at": "2026-07-30 09:50:00",
			"monitoring_completed_at": "2026-07-30 11:00:00",
			"recovery_status": "Pending",
			"postoperative_followup_status": "Pending",
			"cancellation_status": "Not Cancelled",
			"unplanned_surgery_status": "No",
			"unplanned_return_status": "Not Observed",
			"complication_status": "No",
			"mortality_status": "No",
		}
		surgery._validate_structured_surgery_values(values, "Occurred")
		self.assertEqual(values["unplanned_surgery"], 0)
		self.assertEqual(values["complication"], 0)
		with (
			patch.object(surgery.frappe, "throw", side_effect=RuntimeError("source anchor")),
			self.assertRaisesRegex(RuntimeError, "source anchor"),
		):
			surgery._validate_structured_surgery_values(
				{**values, "mortality_status": "Yes", "mortality_at": "2026-07-30 15:00:00"},
				"Occurred",
			)

	def test_structured_occurred_fact_retains_missing_care_stages_as_reason_codes(self) -> None:
		values = {
			"surgery_time": "2026-07-30 10:00:00",
			"source_finalized_at": "2026-07-30 14:00:00",
		}
		reasons = surgery._validate_structured_surgery_values(
			values,
			"Occurred",
			capture_factual_noncompliance=True,
		)
		self.assertEqual(values["preanesthesia_status"], "Pending")
		self.assertEqual(values["intraoperative_monitoring_status"], "Pending")
		self.assertEqual(values["recovery_status"], "Pending")
		self.assertEqual(values["postoperative_followup_status"], "Pending")
		self.assertEqual(values["unplanned_return_status"], "Not Observed")
		self.assertIn("PREANESTHESIA_STATUS_MISSING", reasons)
		self.assertIn("INTRAOPERATIVE_MONITORING_STATUS_MISSING", reasons)
		self.assertIn("UNPLANNED_RETURN_STATUS_MISSING", reasons)

	def test_occurred_fact_is_materializable_when_authorization_mdt_and_checklist_are_missing(
		self,
	) -> None:
		values = {
			"source_record_id_hash": "a" * 64,
			"record_snapshot_hash": "b" * 64,
			"procedure_code": "PROC-4",
			"surgery_level": "Level IV",
			"surgery_time": "2026-07-30 10:00:00",
			"surgery_phase": "Occurred",
			"surgeon": "STAFF-1",
		}
		policy = _Doc(
			name="POLICY-4",
			checksum="p" * 64,
			require_mdt=1,
			required_care_stages_json="[]",
		)
		missing = surgery.frappe.ValidationError("missing evidence")
		with (
			patch.object(surgery, "_approved_policy_for_values", return_value=policy),
			patch.object(surgery, "_approved_exception", return_value=None),
			patch.object(surgery, "_approved_authorization_for_values", side_effect=missing),
			patch.object(surgery, "_completed_mdt", side_effect=missing),
			patch.object(surgery, "_completed_checklist", side_effect=missing),
		):
			evaluation = surgery._evaluate_occurrence(values, _Doc())
		self.assertEqual(evaluation["governance_state"], "Noncompliant")
		self.assertEqual(evaluation["status"], "Failed")
		self.assertEqual(evaluation["compliant"], 0)
		reasons = surgery._governance_reason_codes(evaluation["governance_reason_codes_json"])
		self.assertIn("SURGEON_AUTHORIZATION_MISSING_AMBIGUOUS_OR_INVALID", reasons)
		self.assertIn("REQUIRED_MDT_MISSING_AMBIGUOUS_OR_INVALID", reasons)
		self.assertIn("SAFETY_CHECKLIST_MISSING_AMBIGUOUS_OR_INVALID", reasons)
		self.assertEqual(len(evaluation["governance_checksum"]), 64)

	def test_blocked_occurrence_without_policy_has_a_stable_round_trip_checksum(self) -> None:
		surgery_time = surgery.get_datetime("2026-07-30 10:00:00")
		reason_codes_json = '["PROCEDURE_POLICY_MISSING_AMBIGUOUS_OR_INVALID"]'
		values = {
			"source_record_id_hash": "a" * 64,
			"record_snapshot_hash": "b" * 64,
			"surgery_time": surgery_time,
		}
		created = surgery._surgery_evaluation_values_snapshot(
			values=values,
			policy=None,
			authorization=None,
			qualification=None,
			mdt=None,
			checklist=None,
			exception=None,
			governance_state="Blocked",
			compliant=0,
			reason_codes_json=reason_codes_json,
		)
		projection = _Doc(
			**values,
			procedure_policy="",
			surgery_authorization="",
			staff_qualification="",
			mdt_record="",
			safety_checklist="",
			emergency_exception="",
			governance_state="Blocked",
			compliant=0,
			governance_reason_codes_json=reason_codes_json,
		)
		with patch.object(surgery.frappe.db, "get_value") as get_value:
			validated = surgery._surgery_evaluation_snapshot(projection)
		self.assertEqual(validated, created)
		self.assertIsNone(validated["policy"])
		self.assertEqual(surgery._checksum(validated), surgery._checksum(created))
		get_value.assert_not_called()

	def test_approved_emergency_exception_does_not_infer_factual_compliance(self) -> None:
		values = {
			"source_record_id_hash": "a" * 64,
			"record_snapshot_hash": "b" * 64,
			"procedure_code": "PROC-1",
			"surgery_level": "Level I",
			"surgery_time": "2026-07-30 10:00:00",
			"surgery_phase": "Occurred",
			"surgeon": "STAFF-1",
		}
		policy = _Doc(
			name="POLICY-1",
			checksum="p" * 64,
			require_mdt=0,
			required_care_stages_json="[]",
		)
		authorization = _Doc(name="AUTH-1", checksum="a" * 64, qualification="QUAL-1")
		qualification = _Doc(name="QUAL-1", checksum="q" * 64)
		checklist = _Doc(name="CHECK-1", checksum="c" * 64)
		exception = _Doc(
			name="EXCEPTION-1",
			checksum="e" * 64,
			requested_scopes_json='["Authorization"]',
		)
		with (
			patch.object(surgery, "_approved_policy_for_values", return_value=policy),
			patch.object(surgery, "_approved_exception", return_value=exception),
			patch.object(surgery, "_approved_authorization_for_values", return_value=authorization),
			patch.object(
				surgery,
				"_validated_qualification_for_occurrence",
				return_value=qualification,
			),
			patch.object(surgery, "_completed_checklist", return_value=checklist),
		):
			evaluation = surgery._evaluate_occurrence(values, _Doc())
		self.assertEqual(evaluation["governance_state"], "Noncompliant")
		self.assertEqual(evaluation["compliant"], 0)
		reasons = surgery._governance_reason_codes(evaluation["governance_reason_codes_json"])
		self.assertIn("EMERGENCY_EXCEPTION_USED", reasons)
		self.assertIn("EMERGENCY_EXCEPTION_AUTHORIZATION", reasons)

	def test_preoperative_release_still_fails_closed_without_authorization(self) -> None:
		values = {
			"source_record_id_hash": "a" * 64,
			"procedure_code": "PROC-1",
			"surgery_level": "Level I",
			"surgery_time": "2026-07-30 10:00:00",
			"surgeon": "STAFF-1",
		}
		policy = _Doc(
			name="POLICY-1",
			checksum="p" * 64,
			require_mdt=0,
			required_care_stages_json="[]",
		)
		with (
			patch.object(surgery, "_approved_policy_for_values", return_value=policy),
			patch.object(surgery, "_approved_exception", return_value=None),
			patch.object(
				surgery,
				"_approved_authorization_for_values",
				side_effect=surgery.frappe.ValidationError("blocked"),
			),
			self.assertRaises(surgery.frappe.ValidationError),
		):
			surgery._evaluate_preoperative_release(values, _Doc())

	def test_preoperative_release_evaluates_exception_at_the_locked_decision_time(self) -> None:
		decision_at = surgery.get_datetime("2026-07-30 09:59:00")
		values = {
			"source_record_id_hash": "a" * 64,
			"procedure_code": "PROC-1",
			"surgery_level": "Level I",
			"surgery_time": "2026-07-30 10:00:00",
			"surgeon": "STAFF-1",
		}
		policy = _Doc(
			name="POLICY-1",
			checksum="p" * 64,
			require_mdt=0,
			required_care_stages_json="[]",
		)
		exception = _Doc(
			name="EXCEPTION-1",
			requested_scopes_json='["Authorization","Safety Checklist"]',
		)
		with (
			patch.object(surgery, "_approved_policy_for_values", return_value=policy),
			patch.object(surgery, "_validate_occurrence_care_path"),
			patch.object(
				surgery,
				"_approved_exception",
				return_value=exception,
			) as approved_exception,
		):
			evaluation = surgery._evaluate_preoperative_release(
				values,
				_Doc(),
				decision_at=decision_at,
			)
		self.assertEqual(evaluation["release_state"], "Released with Emergency Exception")
		approved_exception.assert_called_once_with("a" * 64, policy, decision_at)

	def test_finalization_reuses_frozen_occurrence_evidence_after_definition_retirement(
		self,
	) -> None:
		prior = _Doc(
			name="SURGERY-QC-1",
			event="EVENT-OCCURRED-1",
			surgery_phase="Occurred",
			governance_state="Passed",
			governance_checksum="valid",
			governance_reason_codes_json="[]",
			procedure_policy="POLICY-1",
			surgery_authorization="AUTH-1",
			staff_qualification="QUAL-1",
			mdt_record="",
			safety_checklist="CHECK-1",
			emergency_exception="",
		)
		linked = {
			("IONE Surgery Procedure Policy", "POLICY-1"): _Doc(
				name="POLICY-1",
				status="Retired",
				checksum="p" * 64,
			),
			("IONE Surgery Authorization", "AUTH-1"): _Doc(
				name="AUTH-1",
				status="Retired",
				checksum="a" * 64,
			),
			("IONE Staff Qualification", "QUAL-1"): _Doc(
				name="QUAL-1",
				status="Retired",
				checksum="q" * 64,
			),
			("IONE Surgery Safety Checklist", "CHECK-1"): _Doc(
				name="CHECK-1",
				checksum="c" * 64,
			),
		}

		def get_doc(doctype, name):
			if doctype == "IONE Surgery QC":
				return prior
			return linked[(doctype, name)]

		with (
			patch.object(surgery.frappe, "get_all", return_value=["SURGERY-QC-1"]),
			patch.object(surgery.frappe, "get_doc", side_effect=get_doc),
			patch.object(surgery, "_surgery_evaluation_snapshot", return_value={"frozen": True}),
			patch.object(surgery, "_checksum", return_value="valid"),
		):
			evidence = surgery._frozen_occurrence_evidence(
				{
					"source_system": "EMR",
					"source_record_type": "Surgery",
					"source_record_id_hash": "a" * 64,
				}
			)
		self.assertEqual(evidence["procedure_policy"].status, "Retired")
		self.assertEqual(evidence["surgery_authorization"].status, "Retired")

	def test_retirement_after_occurrence_does_not_erase_historical_approval(self) -> None:
		retired_later = _Doc(
			status="Retired",
			approved_at="2026-07-01 09:00:00",
			retired_at="2026-08-01 09:00:00",
		)
		self.assertTrue(
			surgery._governed_definition_status_contains(
				retired_later,
				"2026-07-30 10:00:00",
				allow_historical_retired=True,
			)
		)
		self.assertFalse(
			surgery._governed_definition_status_contains(
				retired_later,
				"2026-08-02 10:00:00",
				allow_historical_retired=True,
			)
		)
		self.assertFalse(
			surgery._governed_definition_status_contains(
				_Doc(
					status="Approved",
					approved_at="2026-07-31 09:00:00",
					retired_at=None,
				),
				"2026-07-30 10:00:00",
				allow_historical_retired=True,
			)
		)

	def test_exception_scope_is_explicit_and_cannot_expand(self) -> None:
		self.assertEqual(
			surgery._exception_scope_list(
				'["Safety Checklist","Authorization"]',
				"requested_scopes",
			),
			["Authorization", "Safety Checklist"],
		)
		with (
			patch.object(surgery.frappe, "throw", side_effect=RuntimeError("scope")),
			self.assertRaisesRegex(RuntimeError, "scope"),
		):
			surgery._exception_scope_list('["All Governance"]', "requested_scopes")

	def test_exception_request_cannot_extend_beyond_scheduled_surgery(self) -> None:
		scheduled = _Doc(
			name="SURGERY-QC-1",
			surgery_phase="Scheduled",
			surgery_time=surgery.get_datetime("2026-07-30 10:00:00"),
			source_record_id_hash="s" * 64,
		)
		policy = _Doc(
			name="POLICY-1",
			allow_emergency_exception=1,
			allowed_exception_scopes_json='["Authorization"]',
			maximum_exception_validity_hours=8,
		)
		with (
			patch.object(surgery, "_require_post"),
			patch.object(surgery, "_require_named_actor", return_value="requester@example.invalid"),
			patch.object(surgery, "_locked_surgery", return_value=scheduled),
			patch.object(surgery, "_require_scope_write"),
			patch.object(surgery, "_approved_policy_for_surgery", return_value=policy),
			patch.object(
				surgery,
				"now_datetime",
				return_value=surgery.get_datetime("2026-07-30 08:00:00"),
			),
			patch.object(
				surgery.frappe,
				"throw",
				side_effect=RuntimeError("validity cannot extend beyond surgery"),
			),
			self.assertRaisesRegex(RuntimeError, "cannot extend"),
		):
			surgery.request_emergency_exception(
				scheduled.name,
				["Authorization"],
				"EMERGENCY",
				"Documented emergency",
				"2026-07-30 08:00:00",
				"2026-07-30 10:01:00",
			)

	def test_post_occurrence_emergency_exception_review_is_rejected_under_surgery_lock(
		self,
	) -> None:
		exception = _Doc(
			name="EXCEPTION-1",
			surgery_qc="SURGERY-QC-1",
			surgery_source_id_hash="s" * 64,
			procedure_policy="POLICY-1",
		)
		scheduled = _Doc(
			name="SURGERY-QC-1",
			surgery_phase="Scheduled",
			surgery_time=surgery.get_datetime("2026-07-30 10:00:00"),
			source_record_id_hash="s" * 64,
			procedure_policy="POLICY-1",
		)
		with (
			patch.object(surgery, "_require_post"),
			patch.object(surgery, "_require_named_actor", return_value="reviewer@example.invalid"),
			patch.object(
				surgery,
				"now_datetime",
				return_value=surgery.get_datetime("2026-07-30 10:00:01"),
			),
			patch.object(
				surgery.frappe.db,
				"advisory_lock",
				side_effect=lambda *args, **kwargs: nullcontext(),
			),
			patch.object(
				surgery.frappe,
				"get_doc",
				side_effect=[exception, scheduled],
			) as get_doc,
			patch.object(
				surgery.frappe,
				"throw",
				side_effect=RuntimeError("after surgery occurrence"),
			),
			self.assertRaisesRegex(RuntimeError, "after surgery"),
		):
			surgery.review_emergency_exception(
				exception.name,
				"Approve",
				"Late approval must be rejected",
			)
		self.assertEqual(
			get_doc.call_args_list[1].kwargs,
			{"for_update": True},
		)

	def test_expired_approved_exception_preserves_and_chains_approval_receipt(self) -> None:
		doc = _Doc(
			exception_key="e" * 64,
			surgery_qc="SURGERY-QC-1",
			surgery_source_id_hash="s" * 64,
			procedure_policy="POLICY-1",
			hospital="HOSP-1",
			campus="CAMPUS-1",
			department="DEPT-1",
			requested_scopes_json='["Authorization"]',
			reason_code="EMERGENCY",
			reason="Documented emergency exception",
			requested_by="requester@example.invalid",
			requested_at=surgery.get_datetime("2026-07-30 08:00:00"),
			valid_from=surgery.get_datetime("2026-07-30 08:30:00"),
			valid_to=surgery.get_datetime("2026-07-30 10:30:00"),
			status="Approved",
			reviewed_by="reviewer@example.invalid",
			reviewed_at=surgery.get_datetime("2026-07-30 08:15:00"),
			review_comment="Approved for the bounded interval",
		)
		doc.checksum = surgery._checksum(surgery._exception_snapshot(doc))
		approval_checksum = doc.checksum
		doc.status = "Expired"
		doc.expired_from_status = "Approved"
		doc.expired_at = surgery.get_datetime("2026-07-30 10:31:00")
		doc.expiration_checksum = surgery._checksum(surgery._exception_expiration_snapshot(doc))

		surgery._validate_expired_exception(doc)
		self.assertEqual(doc.checksum, approval_checksum)
		self.assertTrue(surgery._approved_exception_checksum_is_valid(doc))
		doc.expiration_checksum = "x" * 64
		with (
			patch.object(surgery.frappe, "throw", side_effect=RuntimeError("tampered")),
			self.assertRaisesRegex(RuntimeError, "tampered"),
		):
			surgery._validate_expired_exception(doc)

	def test_historical_occurrence_can_use_an_expired_previously_approved_exception(self) -> None:
		doc = _Doc(
			name="EXCEPTION-1",
			status="Expired",
			expired_from_status="Approved",
			reviewed_at=surgery.get_datetime("2026-07-30 09:00:00"),
		)
		with (
			patch.object(surgery.frappe, "get_all", return_value=["EXCEPTION-1"]),
			patch.object(surgery.frappe, "get_doc", return_value=doc),
			patch.object(surgery, "_validate_expired_exception") as validate_expiry,
			patch.object(surgery, "_approved_exception_checksum_is_valid", return_value=True),
		):
			result = surgery._approved_exception(
				"s" * 64,
				_Doc(name="POLICY-1"),
				surgery.get_datetime("2026-07-30 10:00:00"),
			)
		self.assertIs(result, doc)
		validate_expiry.assert_called_once_with(doc)

	def test_delayed_occurrence_rejects_exception_approved_after_occurrence(self) -> None:
		occurred_at = surgery.get_datetime("2026-07-30 10:00:00")
		for status, expired_from_status in (
			("Approved", None),
			("Expired", "Approved"),
		):
			with self.subTest(status=status):
				doc = _Doc(
					name=f"EXCEPTION-{status}",
					status=status,
					expired_from_status=expired_from_status,
					reviewed_at=surgery.get_datetime("2026-07-30 10:00:01"),
				)
				with (
					patch.object(surgery.frappe, "get_all", return_value=[doc.name]),
					patch.object(surgery.frappe, "get_doc", return_value=doc),
					patch.object(surgery, "_validate_expired_exception") as validate_expiry,
					patch.object(
						surgery,
						"_approved_exception_checksum_is_valid",
						return_value=True,
					) as validate_approval,
				):
					result = surgery._approved_exception(
						"s" * 64,
						_Doc(name="POLICY-1"),
						occurred_at,
					)
				self.assertIsNone(result)
				validate_expiry.assert_not_called()
				validate_approval.assert_not_called()

	def test_pending_expiration_receipt_binds_the_complete_request(self) -> None:
		doc = _Doc(
			exception_key="e" * 64,
			surgery_qc="SURGERY-QC-1",
			surgery_source_id_hash="s" * 64,
			procedure_policy="POLICY-1",
			hospital="HOSP-1",
			campus="CAMPUS-1",
			department="DEPT-1",
			requested_scopes_json='["Authorization"]',
			reason_code="EMERGENCY",
			reason="Documented emergency exception",
			requested_by="requester@example.invalid",
			requested_at=surgery.get_datetime("2026-07-30 08:00:00"),
			valid_from=surgery.get_datetime("2026-07-30 08:30:00"),
			valid_to=surgery.get_datetime("2026-07-30 10:00:00"),
			status="Expired",
			expired_from_status="Pending",
			expired_at=surgery.get_datetime("2026-07-30 10:01:00"),
		)
		doc.expiration_checksum = surgery._checksum(surgery._exception_expiration_snapshot(doc))
		surgery._validate_expired_exception(doc)

		for fieldname, changed_value in (
			("procedure_policy", "POLICY-2"),
			("requested_scopes_json", '["MDT"]'),
			("reason_code", "OTHER"),
			("reason", "Changed reason"),
			("requested_by", "other@example.invalid"),
			("requested_at", surgery.get_datetime("2026-07-30 08:01:00")),
			("valid_from", surgery.get_datetime("2026-07-30 08:31:00")),
		):
			with self.subTest(fieldname=fieldname):
				tampered = _Doc(**doc)
				tampered[fieldname] = changed_value
				with (
					patch.object(
						surgery.frappe,
						"throw",
						side_effect=RuntimeError("tampered request"),
					),
					self.assertRaisesRegex(RuntimeError, "tampered"),
				):
					surgery._validate_expired_exception(tampered)

	def test_definition_slot_is_shared_across_versions_but_not_procedures(self) -> None:
		first = _Doc(
			doctype="IONE Surgery Procedure Policy",
			hospital="HOSP-1",
			campus="CAMPUS-1",
			department="DEPT-1",
			procedure_code="PROC-1",
			policy_version="1",
		)
		second = _Doc({**first, "policy_version": "2"})
		other = _Doc({**first, "procedure_code": "PROC-2"})
		self.assertEqual(
			surgery._definition_slot_key(first),
			surgery._definition_slot_key(second),
		)
		self.assertNotEqual(
			surgery._definition_slot_key(first),
			surgery._definition_slot_key(other),
		)

	def test_projection_progression_updates_one_source_anchor_without_regression(self) -> None:
		before = _Doc(
			event="EVENT-1",
			source_version="1",
			source_event_time="2026-07-30 09:00:00",
			source_finalized_at=None,
			record_snapshot_hash="a" * 64,
			surgery_phase="Scheduled",
		)
		occurred = _Doc(
			event="EVENT-2",
			source_version="2",
			source_event_time="2026-07-30 12:00:00",
			source_finalized_at="2026-07-30 12:00:00",
			record_snapshot_hash="b" * 64,
			surgery_phase="Occurred",
		)
		surgery._validate_surgery_projection_progression(occurred, before)
		with (
			patch.object(surgery.frappe, "throw", side_effect=RuntimeError("regression")),
			self.assertRaisesRegex(RuntimeError, "regression"),
		):
			surgery._validate_surgery_projection_progression(
				_Doc(
					**{
						**occurred,
						"event": "EVENT-3",
						"source_version": "3",
						"source_event_time": "2026-07-30 13:00:00",
						"record_snapshot_hash": "c" * 64,
						"surgery_phase": "Scheduled",
					}
				),
				occurred,
			)
