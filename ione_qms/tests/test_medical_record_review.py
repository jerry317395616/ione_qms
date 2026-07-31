from __future__ import annotations

import json
from contextlib import contextmanager
from unittest import TestCase
from unittest.mock import patch

from ione_qms.services import medical_record_review as review


class _Doc(dict):
	def __getattr__(self, key):
		try:
			return self[key]
		except KeyError as exc:
			raise AttributeError(key) from exc

	def __setattr__(self, key, value):
		self[key] = value

	def check_permission(self, *_args, **_kwargs):
		return None

	def save(self, **_kwargs):
		self["saved"] = True
		return self

	def insert(self, **_kwargs):
		self["inserted"] = True
		self.setdefault("name", f"NEW-{self.get('doctype')}")
		return self

	def is_new(self):
		return bool(self.get("_is_new", True))

	def get_doc_before_save(self):
		return self.get("_previous")


@contextmanager
def _lock():
	yield


def _assignment(**overrides) -> _Doc:
	values = {
		"doctype": review.REVIEW_ASSIGNMENT_DOCTYPE,
		"name": "ASSIGNMENT-1",
		"assignment_key": "a" * 64,
		"batch": "BATCH-1",
		"policy": "POLICY-1",
		"medical_record_qc": "MRQC-1",
		"clinical_event": "EVENT-1",
		"population_token": "0" * 64,
		"record_snapshot_hash": "b" * 64,
		"sampling_score_hash": "c" * 64,
		"coder_required": 1,
		"expert_required": 0,
		"mandatory_rule_manifest_hash": "1" * 64,
		"mandatory_execution_manifest_json": "[]",
		"mandatory_execution_manifest_hash": review._sha256("[]"),
		"deterministic_gate_status": "Passed",
		"deterministic_gate_reason_code": "MANDATORY_RULES_PASSED",
		"hospital": "HOSPITAL-1",
		"campus": "CAMPUS-1",
		"department": "DEPARTMENT-1",
		"ward": None,
		"patient": "PATIENT-1",
		"encounter": "ENCOUNTER-1",
		"responsible_staff": "STAFF-1",
		"source_system": "SOURCE-1",
		"source_record_type": "MedicalRecord",
		"source_record_id_hash": "d" * 64,
		"status": "Coder Review",
		"assigned_coder": "coder@example.test",
		"assigned_expert": None,
		"latest_archive_decision": None,
		"archive_acknowledgement": None,
		"archive_ack_status": None,
		"pending_archive_delivery": None,
		"pending_archive_envelope_hash": None,
	}
	values.update(overrides)
	return _Doc(values)


class TestMedicalRecordSampling(TestCase):
	def test_sampling_math_requires_explicit_rounding_and_undersized_action(self) -> None:
		self.assertEqual(
			review._sample_size(
				101,
				rate_basis_points=1_000,
				minimum=1,
				maximum=100,
				rounding_mode="Ceiling",
				undersized_action="Sample All",
			),
			11,
		)
		self.assertEqual(
			review._sample_size(
				101,
				rate_basis_points=1_000,
				minimum=1,
				maximum=100,
				rounding_mode="Floor",
				undersized_action="Sample All",
			),
			10,
		)
		self.assertEqual(
			review._sample_size(
				2,
				rate_basis_points=10_000,
				minimum=5,
				maximum=10,
				rounding_mode="Half Up",
				undersized_action="Sample All",
			),
			2,
		)
		with (
			patch.object(review.frappe, "throw", side_effect=RuntimeError("undersized")),
			self.assertRaisesRegex(RuntimeError, "undersized"),
		):
			review._sample_size(
				2,
				rate_basis_points=10_000,
				minimum=5,
				maximum=10,
				rounding_mode="Half Up",
				undersized_action="Fail",
			)

	def test_population_token_and_keyed_ranking_are_order_independent(self) -> None:
		rows = [
			{
				"name": f"MRQC-{index}",
				"record_key": f"RK-{index}",
				"record_status": "Final",
				"record_snapshot_hash": f"{index:x}" * 64,
				"source_system": "SOURCE-1",
				"source_record_type": "MedicalRecord",
				"source_record_id_hash": f"{index + 4:x}" * 64,
				"source_finalized_at": "2026-07-01 09:00:00",
				"hospital": "HOSPITAL-1",
				"campus": "CAMPUS-1",
				"department": "DEPARTMENT-1",
				"ward": None,
				"patient": f"PATIENT-{index}",
				"encounter": f"ENCOUNTER-{index}",
				"responsible_staff": None,
			}
			for index in (1, 2, 3)
		]
		population = [review._population_record(row) for row in rows]
		first = review._ranked_population(
			population,
			secret=b"x" * 32,
			domain="primary",
			batch_key="f" * 64,
		)
		second = review._ranked_population(
			list(reversed(population)),
			secret=b"x" * 32,
			domain="primary",
			batch_key="f" * 64,
		)
		self.assertEqual([item["token"] for item in first], [item["token"] for item in second])
		self.assertTrue(all(len(item["score"]) == 64 for item in first))

	def test_population_token_binds_final_state_and_clinical_links(self) -> None:
		row = {
			"name": "MRQC-1",
			"record_key": "RK-1",
			"record_status": "Final",
			"record_snapshot_hash": "a" * 64,
			"source_system": "SOURCE-1",
			"source_record_type": "MedicalRecord",
			"source_record_id_hash": "b" * 64,
			"source_finalized_at": "2026-07-01 09:00:00",
			"hospital": "HOSPITAL-1",
			"campus": "CAMPUS-1",
			"department": "DEPARTMENT-1",
			"ward": None,
			"patient": "PATIENT-1",
			"encounter": "ENCOUNTER-1",
			"responsible_staff": "STAFF-1",
		}
		baseline = review._population_record(row)
		changed = review._population_record({**row, "patient": "PATIENT-2"})
		self.assertNotEqual(baseline["token"], changed["token"])
		with (
			patch.object(review.frappe, "throw", side_effect=RuntimeError("no longer Final")),
			self.assertRaisesRegex(RuntimeError, "no longer Final"),
		):
			review._population_record({**row, "record_status": "Archived"})

	def test_population_date_filter_is_half_open_after_the_inclusive_end_day(self) -> None:
		policy = _Doc(
			eligible_record_status="Final",
			population_date_field="source_finalized_at",
			source_system="SOURCE-1",
			hospital="HOSPITAL-1",
			campus=None,
			department=None,
			ward=None,
		)
		with patch.object(review.frappe, "get_list", return_value=[]) as get_list:
			review._eligible_population(
				policy,
				review.getdate("2026-07-01"),
				review.getdate("2026-07-31"),
			)
		filters = get_list.call_args.kwargs["filters"]
		self.assertIn(
			["source_finalized_at", ">=", review.get_datetime("2026-07-01")],
			filters,
		)
		self.assertIn(
			["source_finalized_at", "<", review.get_datetime("2026-08-01")],
			filters,
		)

	def test_source_ack_time_is_normalized_to_site_timezone_and_requires_an_offset(self) -> None:
		with patch.object(review, "get_system_timezone", return_value="Asia/Shanghai"):
			value = review._site_naive_datetime(
				"2026-07-30T03:30:00Z",
				label="source_ack_at",
				require_timezone=True,
			)
		self.assertEqual(str(value), "2026-07-30 11:30:00")
		with (
			patch.object(review.frappe, "throw", side_effect=RuntimeError("explicit timezone")),
			self.assertRaisesRegex(RuntimeError, "explicit timezone"),
		):
			review._site_naive_datetime(
				"2026-07-30 11:30:00",
				label="source_ack_at",
				require_timezone=True,
			)

	def test_final_sample_anchor_cannot_change_after_assignment(self) -> None:
		previous = _Doc(
			{
				"record_key": "RK-1",
				"record_status": "Final",
				"source_system": "SOURCE-1",
				"source_record_type": "MedicalRecord",
				"source_record_id_hash": "a" * 64,
				"source_finalized_at": "2026-07-01 09:00:00",
				"record_snapshot_hash": "b" * 64,
				"hospital": "HOSPITAL-1",
				"campus": "CAMPUS-1",
				"department": "DEPARTMENT-1",
				"patient": "PATIENT-1",
				"encounter": "ENCOUNTER-1",
			}
		)
		current = _Doc(previous)
		current.update(
			{
				"name": "MRQC-1",
				"_previous": previous,
				"record_snapshot_hash": "c" * 64,
			}
		)
		with (
			patch.object(review.frappe.db, "exists", return_value=True),
			patch.object(review.frappe, "get_doc", return_value=previous),
			patch.object(review.frappe, "throw", side_effect=RuntimeError("immutable anchor")),
			self.assertRaisesRegex(RuntimeError, "immutable anchor"),
		):
			review.validate_medical_record_review_anchor(current)

	def test_final_projection_can_archive_only_after_every_gate_is_applied(self) -> None:
		previous = _Doc(
			{
				"name": "MRQC-1",
				"record_key": "RK-1",
				"record_status": "Final",
				"source_system": "SOURCE-1",
				"source_record_type": "MedicalRecord",
				"source_record_id_hash": "a" * 64,
				"source_finalized_at": "2026-07-01 09:00:00",
				"record_snapshot_hash": "b" * 64,
				"hospital": "HOSPITAL-1",
				"campus": "CAMPUS-1",
				"department": "DEPARTMENT-1",
				"patient": "PATIENT-1",
				"encounter": "ENCOUNTER-1",
			}
		)
		current = _Doc(previous)
		current.update(
			{
				"_previous": previous,
				"record_status": "Archived",
			}
		)
		with (
			patch.object(review.frappe.db, "exists", return_value=True),
			patch.object(review.frappe, "get_doc", return_value=previous),
			patch.object(review, "_is_governed_archive_projection_transition", return_value=True),
		):
			review.validate_medical_record_review_anchor(current)

	def test_sampled_source_projection_cannot_be_deleted(self) -> None:
		with (
			patch.object(review.frappe.db, "exists", return_value=True),
			patch.object(review.frappe, "throw", side_effect=RuntimeError("retained audit record")),
			self.assertRaisesRegex(RuntimeError, "retained audit record"),
		):
			review.prevent_sampled_medical_record_deletion(_Doc(name="MRQC-1"))

	def test_mandatory_rule_manifest_is_exact_sorted_and_hash_bound(self) -> None:
		manifest = [
			{
				"rule": "RULE-1",
				"rule_checksum": "a" * 64,
				"rule_version": "RULE-1-v1",
			}
		]
		policy = _Doc(
			status="Draft",
			mandatory_rule_manifest_json=review._canonical_json(manifest),
		)
		review._normalize_mandatory_rule_manifest(policy)
		self.assertEqual(
			policy.mandatory_rule_manifest_hash,
			review._sha256(policy.mandatory_rule_manifest_json),
		)
		with (
			patch.object(review.frappe, "throw", side_effect=RuntimeError("sorted")),
			self.assertRaisesRegex(RuntimeError, "sorted"),
		):
			review._mandatory_rule_manifest(
				review._canonical_json(
					[
						{**manifest[0], "rule": "RULE-2", "rule_version": "RULE-2-v1"},
						manifest[0],
					]
				)
			)

	def test_mandatory_gate_allows_only_exact_pass_for_snapshot(self) -> None:
		manifest = [
			{
				"rule": "RULE-1",
				"rule_checksum": "a" * 64,
				"rule_version": "RULE-1-v1",
			}
		]
		manifest_json = review._canonical_json(manifest)
		policy = _Doc(
			mandatory_rule_manifest_json=manifest_json,
			mandatory_rule_manifest_hash=review._sha256(manifest_json),
		)
		version = _Doc(
			rule="RULE-1",
			rule_type_snapshot="Deterministic",
			status="Published",
			shadow_mode=0,
			checksum="a" * 64,
		)
		execution = _Doc(
			name="EXECUTION-1",
			context_hash="b" * 64,
			result="Passed",
			completed_at="2026-07-30 12:00:00",
		)
		with (
			patch.object(review.frappe.db, "get_value", return_value=version),
			patch.object(review.frappe, "get_all", return_value=[execution]),
		):
			gate = review._mandatory_rule_gate(
				policy,
				{"event": "EVENT-1", "record_snapshot_hash": "b" * 64},
			)
		self.assertEqual(gate["status"], "Passed")
		self.assertEqual(gate["reason_code"], "MANDATORY_RULES_PASSED")

		with (
			patch.object(review.frappe.db, "get_value", return_value=version),
			patch.object(review.frappe.db, "exists", return_value=True),
			patch.object(
				review.frappe,
				"get_all",
				return_value=[_Doc(execution, context_hash="c" * 64)],
			),
		):
			held = review._mandatory_rule_gate(
				policy,
				{"event": "EVENT-1", "record_snapshot_hash": "b" * 64},
			)
		self.assertEqual(held["status"], "Held")
		self.assertEqual(held["reason_code"], "MANDATORY_RULE_SNAPSHOT_MISMATCH")

	def test_mandatory_gate_holds_and_freezes_conflicting_exact_receipts(self) -> None:
		manifest = [
			{
				"rule": "RULE-1",
				"rule_checksum": "a" * 64,
				"rule_version": "RULE-1-v1",
			}
		]
		manifest_json = review._canonical_json(manifest)
		policy = _Doc(
			mandatory_rule_manifest_json=manifest_json,
			mandatory_rule_manifest_hash=review._sha256(manifest_json),
		)
		version = _Doc(
			rule="RULE-1",
			rule_type_snapshot="Deterministic",
			status="Published",
			shadow_mode=0,
			checksum="a" * 64,
		)
		for results in (("Passed", "Failed"), ("Passed", "Passed")):
			with self.subTest(results=results):
				receipts = [
					_Doc(
						name=f"EXECUTION-{index}",
						context_hash="b" * 64,
						result=result,
						completed_at=f"2026-07-30 12:00:0{index}",
					)
					for index, result in enumerate(results, start=1)
				]
				with (
					patch.object(review.frappe.db, "get_value", return_value=version),
					patch.object(review.frappe, "get_all", return_value=receipts),
				):
					gate = review._mandatory_rule_gate(
						policy,
						{"event": "EVENT-1", "record_snapshot_hash": "b" * 64},
					)
				self.assertEqual(gate["status"], "Held")
				self.assertEqual(gate["reason_code"], "MANDATORY_RULE_EXECUTION_CONFLICT")
				frozen = json.loads(gate["execution_manifest_json"])
				self.assertEqual(
					[item["execution"] for item in frozen],
					["EXECUTION-1", "EXECUTION-2"],
				)

	def test_initial_disposition_requires_explicit_auto_allow_and_preserves_coder_cohort(self) -> None:
		gate = {"status": "Passed", "reason_code": "MANDATORY_RULES_PASSED"}
		self.assertEqual(
			review._initial_archive_disposition(
				_Doc(auto_allow_deterministic_pass=1),
				gate,
				coder_required=False,
			),
			("Allow", "MANDATORY_RULES_PASSED"),
		)
		self.assertEqual(
			review._initial_archive_disposition(
				_Doc(auto_allow_deterministic_pass=0),
				gate,
				coder_required=False,
			),
			("Hold", "AUTO_ALLOW_DISABLED"),
		)
		self.assertEqual(
			review._initial_archive_disposition(
				_Doc(auto_allow_deterministic_pass=1),
				gate,
				coder_required=True,
			),
			("Hold", "CODER_REVIEW_REQUIRED"),
		)

	def test_assignmentless_record_is_never_archive_ready(self) -> None:
		with patch.object(review.frappe, "get_all", return_value=[]):
			self.assertFalse(
				review._all_archive_gates_applied(_Doc(name="MRQC-1", record_snapshot_hash="a" * 64))
			)

	def test_review_outcome_cannot_bypass_held_mandatory_gate(self) -> None:
		assignment = _assignment(
			status="Closed",
			deterministic_gate_status="Held",
			deterministic_gate_reason_code="MANDATORY_RULE_EXECUTION_MISSING",
			latest_archive_decision="ARCHIVE-GATE-1",
		)
		policy = _Doc(
			archive_gate_enabled=1,
			coder_allow_outcomes_json=review._canonical_json(["Pass"]),
			expert_allow_outcomes_json=review._canonical_json(["Pass"]),
			approval_checksum="e" * 64,
		)
		review_decision = _Doc(name="REVIEW-1")
		created = _Doc(name="ARCHIVE-REVIEW-1", decision="Hold")

		with (
			patch.object(review.frappe.db, "get_value", return_value=None),
			patch.object(review, "_new_archive_decision", return_value=created) as new_archive,
			patch.object(review, "_update_assignment"),
		):
			result = review._issue_policy_archive_decision(
				assignment,
				policy,
				review_decision=review_decision,
				stage="Coder",
				outcome="Pass",
				triggered_by="coder@example.test",
			)

		self.assertIs(result, created)
		self.assertEqual(new_archive.call_args.kwargs["decision"], "Hold")
		self.assertEqual(
			new_archive.call_args.kwargs["reason_code"],
			"MANDATORY_RULE_EXECUTION_MISSING",
		)


class TestMedicalRecordReviewDecisions(TestCase):
	def test_closed_assignment_rejects_review_state_mutation(self) -> None:
		assignment = _assignment(status="Closed")
		with self.assertRaisesRegex(ValueError, "Unsupported medical-record assignment transition"):
			review._update_assignment(
				assignment,
				{
					"coder_decision": "DECISION-2",
					"coder_outcome": "Pass",
					"coder_reviewed_at": "2026-07-30 12:00:00",
					"status": "Closed",
				},
			)

	def test_coder_sample_is_routed_to_independent_expert_without_archive_decision(self) -> None:
		assignment = _assignment(expert_required=1)
		decision = _Doc(
			name="DECISION-CODER-1",
			stage="Coder",
			outcome="Pass",
			reviewed_at="2026-07-30 10:00:00",
		)
		policy = _Doc(
			{
				"name": "POLICY-1",
				"expert_non_pass_policy": "Sample Only",
				"approval_checksum": "e" * 64,
			}
		)

		def update_assignment(doc, values):
			doc.update(values)

		def get_doc(doctype, name, **_kwargs):
			if doctype == review.REVIEW_ASSIGNMENT_DOCTYPE:
				return assignment
			if doctype == review.SAMPLING_POLICY_DOCTYPE:
				return policy
			raise AssertionError((doctype, name))

		def get_value(doctype, name, fieldname=None, **_kwargs):
			if (
				doctype == review.REVIEW_ASSIGNMENT_DOCTYPE
				and name == assignment.name
				and fieldname == "medical_record_qc"
			):
				return assignment.medical_record_qc
			return None

		with (
			patch.object(review, "_named_role_user", return_value="coder@example.test"),
			patch.object(review, "_require_assignment_access"),
			patch.object(review, "_medical_record_archive_lock", return_value=_lock()),
			patch.object(review.frappe.db, "advisory_lock", return_value=_lock()),
			patch.object(review.frappe, "get_doc", side_effect=get_doc),
			patch.object(review.frappe.db, "get_value", side_effect=get_value),
			patch.object(review, "_new_review_decision", return_value=decision),
			patch.object(review, "_assert_frozen_policy"),
			patch.object(review, "_update_assignment", side_effect=update_assignment),
			patch.object(review, "_issue_policy_archive_decision") as archive,
			patch.object(review.frappe.db, "commit"),
		):
			result = review.submit_coder_review(
				assignment.name,
				"Pass",
				"Coding review found no defect.",
			)

		self.assertEqual(result["status"], "Expert Review")
		self.assertEqual(assignment.coder_decision, decision.name)
		archive.assert_not_called()

	def test_expert_must_differ_from_coder(self) -> None:
		assignment = _assignment(
			status="Expert Review",
			assigned_coder="same@example.test",
			assigned_expert="same@example.test",
		)
		with (
			patch.object(review, "_named_role_user", return_value="same@example.test"),
			patch.object(review, "_require_assignment_access"),
			patch.object(review, "_medical_record_archive_lock", return_value=_lock()),
			patch.object(review.frappe.db, "advisory_lock", return_value=_lock()),
			patch.object(
				review.frappe.db,
				"get_value",
				return_value=assignment.medical_record_qc,
			),
			patch.object(review.frappe, "get_doc", return_value=assignment),
			patch.object(review.frappe, "throw", side_effect=RuntimeError("independent")),
			self.assertRaisesRegex(RuntimeError, "independent"),
		):
			review.submit_expert_review(
				assignment.name,
				"Pass",
				"Expert review independently confirmed the record.",
			)

	def test_expert_decision_closes_assignment_and_issues_policy_gate(self) -> None:
		assignment = _assignment(
			status="Expert Review",
			assigned_coder="coder@example.test",
			assigned_expert="expert@example.test",
		)
		decision = _Doc(
			name="DECISION-EXPERT-1",
			stage="Expert",
			outcome="Defect",
			reviewed_at="2026-07-30 11:00:00",
		)
		archive = _Doc(name="ARCHIVE-1", decision="Hold")
		policy = _Doc(
			{
				"name": "POLICY-1",
				"expert_non_pass_policy": "Always Expert Review",
				"approval_checksum": "e" * 64,
			}
		)

		def update_assignment(doc, values):
			doc.update(values)

		def get_doc(doctype, name, **_kwargs):
			if doctype == review.REVIEW_ASSIGNMENT_DOCTYPE:
				return assignment
			if doctype == review.SAMPLING_POLICY_DOCTYPE:
				return policy
			raise AssertionError((doctype, name))

		def get_value(doctype, name, fieldname=None, **_kwargs):
			if (
				doctype == review.REVIEW_ASSIGNMENT_DOCTYPE
				and name == assignment.name
				and fieldname == "medical_record_qc"
			):
				return assignment.medical_record_qc
			return None

		with (
			patch.object(review, "_named_role_user", return_value="expert@example.test"),
			patch.object(review, "_require_assignment_access"),
			patch.object(review, "_medical_record_archive_lock", return_value=_lock()),
			patch.object(review.frappe.db, "advisory_lock", return_value=_lock()),
			patch.object(review.frappe, "get_doc", side_effect=get_doc),
			patch.object(review.frappe.db, "get_value", side_effect=get_value),
			patch.object(review, "_new_review_decision", return_value=decision),
			patch.object(review, "_assert_frozen_policy"),
			patch.object(review, "_update_assignment", side_effect=update_assignment),
			patch.object(review, "_issue_policy_archive_decision", return_value=archive) as issue,
			patch.object(review, "_update_batch_completion") as complete,
			patch.object(review.frappe.db, "commit"),
		):
			result = review.submit_expert_review(
				assignment.name,
				"Defect",
				"Expert review confirmed a coding discrepancy.",
				finding="FINDING-1",
			)

		self.assertEqual(result["status"], "Closed")
		self.assertEqual(result["archive_decision"], archive.name)
		issue.assert_called_once()
		complete.assert_called_once_with(assignment.batch)


class TestMedicalRecordArchiveGate(TestCase):
	def test_record_envelope_allows_only_when_every_current_gate_is_closed_allow(self) -> None:
		projection = _Doc(
			name="MRQC-1",
			record_snapshot_hash="b" * 64,
			source_record_id_hash="d" * 64,
			source_record_type="MedicalRecord",
			hospital="HOSPITAL-1",
			campus="CAMPUS-1",
			department="DEPARTMENT-1",
			ward=None,
		)
		assignments = [
			_assignment(
				name=f"ASSIGNMENT-{index}",
				status="Closed",
				latest_archive_decision=f"{index}" * 64,
			)
			for index in (1, 2)
		]
		decisions = {
			assignment.latest_archive_decision: _Doc(
				name=assignment.latest_archive_decision,
				assignment=assignment.name,
				decision="Allow",
				decision_checksum=f"{index + 2}" * 64,
				decision_type="Deterministic Gate",
				reason_code="MANDATORY_RULES_PASSED",
				decided_at="2026-07-30 12:00:00",
				record_snapshot_hash=projection.record_snapshot_hash,
				source_record_id_hash=projection.source_record_id_hash,
			)
			for index, assignment in enumerate(assignments)
		}

		def get_doc(doctype, name, **_kwargs):
			if doctype == review.ARCHIVE_DECISION_DOCTYPE:
				return decisions[name]
			raise AssertionError((doctype, name))

		with (
			patch.object(review, "authorize_endpoint_scope"),
			patch.object(review.frappe, "get_doc", side_effect=get_doc),
			patch.object(review, "_assignment_has_current_applied_ack", return_value=False),
		):
			allowed = review._effective_archive_envelope(
				projection,
				assignments,
				endpoint=_Doc(name="ENDPOINT-1"),
				source=_Doc(name="SOURCE-1"),
				allowed_pending_delivery=None,
			)
			self.assertEqual(allowed["effective_decision"], "Allow")
			self.assertTrue(allowed["_publishable"])
			self.assertEqual(len(allowed["_pending_assignments"]), 2)
			assignments[1].status = "Coder Review"
			held = review._effective_archive_envelope(
				projection,
				assignments,
				endpoint=_Doc(name="ENDPOINT-1"),
				source=_Doc(name="SOURCE-1"),
				allowed_pending_delivery=None,
			)
			self.assertEqual(held["effective_decision"], "Hold")
			self.assertFalse(held["_publishable"])
			self.assertEqual(held["_pending_assignments"], [])
			assignments[1].status = "Closed"
			decisions[assignments[1].latest_archive_decision].decision = "Hold"
			closed_hold = review._effective_archive_envelope(
				projection,
				assignments,
				endpoint=_Doc(name="ENDPOINT-1"),
				source=_Doc(name="SOURCE-1"),
				allowed_pending_delivery=None,
			)
		self.assertEqual(closed_hold["effective_decision"], "Hold")
		self.assertTrue(closed_hold["_publishable"])
		self.assertEqual(len(closed_hold["_pending_assignments"]), 2)

	def test_override_appends_a_superseding_decision(self) -> None:
		previous = _Doc(
			name="ARCHIVE-HOLD-1",
			assignment="ASSIGNMENT-1",
			review_decision="DECISION-1",
			decision="Hold",
			triggered_by="expert@example.test",
		)
		assignment = _assignment(
			status="Closed",
			assigned_coder="coder@example.test",
			assigned_expert="expert@example.test",
			latest_archive_decision=previous.name,
		)
		policy = _Doc(name=assignment.policy)
		override = _Doc(name="ARCHIVE-ALLOW-2", decision="Allow", decision_type="Override")

		def get_doc(doctype, name, **_kwargs):
			if doctype == review.ARCHIVE_DECISION_DOCTYPE:
				return previous
			if doctype == review.REVIEW_ASSIGNMENT_DOCTYPE:
				return assignment
			if doctype == review.SAMPLING_POLICY_DOCTYPE:
				return policy
			raise AssertionError((doctype, name))

		def get_value(doctype, name, fieldname=None, *, as_dict=False, **_kwargs):
			if doctype == review.ARCHIVE_DECISION_DOCTYPE and name == previous.name:
				return _Doc(assignment=assignment.name) if as_dict else assignment.name
			if (
				doctype == review.REVIEW_ASSIGNMENT_DOCTYPE
				and name == assignment.name
				and fieldname == "medical_record_qc"
			):
				return assignment.medical_record_qc
			return None

		with (
			patch.object(review, "_named_role_user", return_value="medical-affairs@example.test"),
			patch.object(review, "_medical_record_archive_lock", return_value=_lock()),
			patch.object(review.frappe.db, "advisory_lock", return_value=_lock()),
			patch.object(review.frappe, "get_doc", side_effect=get_doc),
			patch.object(review, "_require_assignment_access"),
			patch.object(review.frappe.db, "get_value", side_effect=get_value),
			patch.object(review, "_locked_record_assignments", return_value=[assignment]),
			patch.object(review, "_policy_operation_locks", return_value=_lock()),
			patch.object(review, "_record_override_excluded_actors", return_value=set()),
			patch.object(review, "_new_archive_decision", return_value=override) as create,
			patch.object(review, "_update_assignment") as update,
			patch.object(review.frappe.db, "commit"),
		):
			result = review.override_archive_decision(
				previous.name,
				"Allow",
				"CLINICAL_OVERRIDE",
				"Independent medical-affairs review approved this documented exception.",
			)

		self.assertEqual(result["archive_decision"], override.name)
		self.assertEqual(create.call_args.kwargs["supersedes"], previous.name)
		self.assertEqual(create.call_args.kwargs["decision_type"], "Override")
		update.assert_called_once()

	def test_record_override_actor_union_keeps_u_v_u_and_cross_policy_history(self) -> None:
		first = _assignment(
			name="ASSIGNMENT-1",
			policy="POLICY-1",
			batch="BATCH-1",
			medical_record_qc="MRQC-1",
			assigned_coder=None,
			latest_archive_decision="ARCHIVE-1",
		)
		second = _assignment(
			name="ASSIGNMENT-2",
			policy="POLICY-2",
			batch="BATCH-2",
			medical_record_qc="MRQC-1",
			assigned_coder=None,
			latest_archive_decision="ARCHIVE-2",
		)
		archive_decisions = {
			"ARCHIVE-1": _Doc(
				name="ARCHIVE-1",
				assignment=first.name,
				triggered_by="gate-1@example.test",
			),
			"ARCHIVE-2": _Doc(
				name="ARCHIVE-2",
				assignment=second.name,
				triggered_by="gate-2@example.test",
			),
		}
		batches = {
			"BATCH-1": _Doc(
				name="BATCH-1",
				policy=first.policy,
				created_by="batch-1@example.test",
			),
			"BATCH-2": _Doc(
				name="BATCH-2",
				policy=second.policy,
				created_by="batch-2@example.test",
			),
		}
		policies = {
			"POLICY-1": _Doc(
				name="POLICY-1",
				requested_by="author-1@example.test",
				reviewed_by="reviewer-1@example.test",
			),
			"POLICY-2": _Doc(
				name="POLICY-2",
				requested_by="author-2@example.test",
				reviewed_by="reviewer-2@example.test",
			),
		}
		operations = {
			"POLICY-1": [
				_Doc(operated_by="u@example.test"),
				_Doc(operated_by="v@example.test"),
				_Doc(operated_by="u@example.test"),
			],
			"POLICY-2": [_Doc(operated_by="cross-policy@example.test")],
		}

		def get_doc(doctype, name, **_kwargs):
			if doctype == review.ARCHIVE_DECISION_DOCTYPE:
				return archive_decisions[name]
			if doctype == review.REVIEW_BATCH_DOCTYPE:
				return batches[name]
			raise AssertionError((doctype, name))

		with (
			patch.object(review.frappe, "get_doc", side_effect=get_doc),
			patch.object(
				review,
				"_archive_supersession_chain",
				side_effect=lambda latest: [latest],
			),
			patch.object(
				review,
				"_policy_operation_chain",
				side_effect=lambda policy: operations[policy.name],
			),
		):
			excluded = review._record_override_excluded_actors(
				[first, second],
				policies,
			)

		self.assertIn("u@example.test", excluded)
		self.assertIn("v@example.test", excluded)
		self.assertIn("cross-policy@example.test", excluded)

	def test_exact_ack_replay_rejects_a_receipt_that_is_no_longer_current(self) -> None:
		endpoint = _Doc(name="ENDPOINT-1", source_system="SOURCE-1")
		decision = _Doc(
			name="a" * 64,
			assignment="ASSIGNMENT-1",
			decision_checksum="b" * 64,
			source_record_id_hash="c" * 64,
			source_system="SOURCE-1",
		)
		assignment = _assignment(
			name=decision.assignment,
			latest_archive_decision="d" * 64,
			archive_acknowledgement="OTHER-ACK",
			archive_ack_status="Applied",
		)
		ack = _Doc(
			name="ACK-1",
			archive_decision=decision.name,
			assignment=assignment.name,
			delivery="e" * 64,
			envelope_hash="f" * 64,
			endpoint=endpoint.name,
			source_system=endpoint.source_system,
			source_ack_id="ACK-1",
			source_ack_at="2026-07-30 12:00:00",
			ack_status="Applied",
			message_code="ARCHIVE_APPLIED",
			decision_checksum=decision.decision_checksum,
			source_record_id_hash=decision.source_record_id_hash,
		)
		payload = {
			"archive_decision": ack.archive_decision,
			"decision_checksum": ack.decision_checksum,
			"delivery": ack.delivery,
			"envelope_hash": ack.envelope_hash,
			"source_record_id_hash": ack.source_record_id_hash,
			"source_ack_id": ack.source_ack_id,
			"source_ack_at": "2026-07-30T12:00:00+08:00",
			"ack_status": ack.ack_status,
			"message_code": ack.message_code,
		}

		def get_doc(doctype, _name, **_kwargs):
			if doctype == review.ARCHIVE_DECISION_DOCTYPE:
				return decision
			if doctype == review.REVIEW_ASSIGNMENT_DOCTYPE:
				return assignment
			raise AssertionError(doctype)

		with (
			patch.object(review, "get_system_timezone", return_value="Asia/Shanghai"),
			patch.object(review.frappe, "get_doc", side_effect=get_doc),
			patch.object(review.frappe, "throw", side_effect=RuntimeError("stale")),
			self.assertRaisesRegex(RuntimeError, "stale"),
		):
			review._assert_archive_ack_replay_current(
				ack,
				endpoint,
				payload,
				record_name=assignment.medical_record_qc,
			)

	def test_signed_ack_verification_precedes_parse_and_creates_append_only_receipt(self) -> None:
		events: list[str] = []
		endpoint = _Doc(
			name="ENDPOINT-1",
			enabled=1,
			direction="Inbound",
			authentication_type="HMAC",
			require_signature=1,
			source_system="SOURCE-1",
		)
		decision = _Doc(
			name="ARCHIVE-1",
			assignment="ASSIGNMENT-1",
			decision_checksum="a" * 64,
			source_record_id_hash="b" * 64,
			source_system="SOURCE-1",
			decided_at="2026-07-30 11:00:00",
		)
		delivery = _Doc(
			name="e" * 64,
			endpoint=endpoint.name,
			source_system=endpoint.source_system,
		)
		assignment = _assignment(
			status="Closed",
			latest_archive_decision=decision.name,
			pending_archive_delivery=delivery.name,
			pending_archive_envelope_hash="f" * 64,
		)
		payload = {
			"archive_decision": decision.name,
			"decision_checksum": decision.decision_checksum,
			"delivery": delivery.name,
			"envelope_hash": assignment.pending_archive_envelope_hash,
			"source_record_id_hash": decision.source_record_id_hash,
			"source_ack_id": "ACK-1",
			"source_ack_at": "2026-07-30T11:30:00+08:00",
			"ack_status": "Applied",
			"message_code": "ARCHIVE_APPLIED",
		}
		created: list[_Doc] = []

		def get_doc(first, second=None, **_kwargs):
			if isinstance(first, dict):
				doc = _Doc(first)
				doc.name = "ACK-RECORD-1"
				created.append(doc)
				return doc
			if first == review.ARCHIVE_DECISION_DOCTYPE:
				return decision
			if first == review.REVIEW_ASSIGNMENT_DOCTYPE:
				return assignment
			if first == review.ARCHIVE_DELIVERY_DOCTYPE:
				return delivery
			raise AssertionError((first, second))

		def get_value(doctype, name, fieldname=None, *, as_dict=False, **_kwargs):
			if doctype == review.ARCHIVE_DECISION_DOCTYPE and name == decision.name:
				return _Doc(assignment=assignment.name) if as_dict else assignment.name
			if (
				doctype == review.REVIEW_ASSIGNMENT_DOCTYPE
				and name == assignment.name
				and fieldname == "medical_record_qc"
			):
				return assignment.medical_record_qc
			return None

		def runtime(_endpoint):
			events.append("runtime")

		def verify(_endpoint, _body, **_kwargs):
			events.append("verify")

		def parse(_body):
			events.append("parse")
			return payload

		with (
			patch.object(review.frappe, "get_cached_doc", return_value=endpoint),
			patch.object(review, "assert_integration_endpoint_runtime", side_effect=runtime),
			patch.object(review, "verify_request", side_effect=verify),
			patch.object(review, "_archive_ack_payload", side_effect=parse),
			patch.object(review, "now_datetime", return_value=review.get_datetime("2026-07-30 11:31:00")),
			patch.object(review, "_medical_record_archive_lock", return_value=_lock()),
			patch.object(review.frappe.db, "advisory_lock", return_value=_lock()),
			patch.object(review.frappe.db, "get_value", side_effect=get_value),
			patch.object(review.frappe, "get_doc", side_effect=get_doc),
			patch.object(review, "_assert_delivery_contains_pending_decision"),
			patch.object(review, "authorize_endpoint_scope", return_value={"hospital": "HOSPITAL-1"}),
			patch.object(review, "_update_assignment") as update,
			patch.object(review, "_apply_archive_projection_if_ready") as apply_projection,
			patch.object(review.frappe.db, "commit"),
		):
			result = review.acknowledge_archive_decision(
				endpoint.name,
				b'{"signed":"exact bytes"}',
			)

		self.assertEqual(events, ["runtime", "verify", "parse"])
		self.assertEqual(result["ack_status"], "Applied")
		self.assertTrue(created[0].inserted)
		self.assertEqual(created[0].source_record_id_hash, decision.source_record_id_hash)
		update.assert_called_once()
		apply_projection.assert_called_once_with(assignment, decision, created[0])

	def test_conflicting_ack_replay_is_rejected_without_second_receipt(self) -> None:
		endpoint = _Doc(
			name="ENDPOINT-1",
			enabled=1,
			direction="Inbound",
			authentication_type="HMAC",
			require_signature=1,
			source_system="SOURCE-1",
		)
		existing = _Doc(
			name="ACK-RECORD-1",
			request_hash="f" * 64,
			archive_decision="ARCHIVE-1",
			ack_status="Applied",
		)
		payload = {
			"archive_decision": "ARCHIVE-1",
			"decision_checksum": "a" * 64,
			"delivery": "e" * 64,
			"envelope_hash": "f" * 64,
			"source_record_id_hash": "b" * 64,
			"source_ack_id": "ACK-1",
			"source_ack_at": "2026-07-30T11:30:00+08:00",
			"ack_status": "Applied",
			"message_code": "ARCHIVE_APPLIED",
		}

		def get_value(doctype, _name, fieldname=None, *, as_dict=False, **_kwargs):
			if doctype == review.ARCHIVE_DECISION_DOCTYPE:
				return _Doc(assignment="ASSIGNMENT-1") if as_dict else "ASSIGNMENT-1"
			if doctype == review.REVIEW_ASSIGNMENT_DOCTYPE and fieldname == "medical_record_qc":
				return "MRQC-1"
			if doctype == review.ARCHIVE_ACK_DOCTYPE:
				return existing.name
			return None

		with (
			patch.object(review.frappe, "get_cached_doc", return_value=endpoint),
			patch.object(review, "assert_integration_endpoint_runtime"),
			patch.object(review, "verify_request"),
			patch.object(review, "_archive_ack_payload", return_value=payload),
			patch.object(review, "_medical_record_archive_lock", return_value=_lock()),
			patch.object(review.frappe.db, "advisory_lock", return_value=_lock()),
			patch.object(review.frappe.db, "get_value", side_effect=get_value),
			patch.object(review.frappe, "get_doc", return_value=existing),
			patch.object(review.frappe, "throw", side_effect=RuntimeError("conflicting replay")),
			self.assertRaisesRegex(RuntimeError, "conflicting replay"),
		):
			review.acknowledge_archive_decision(endpoint.name, b'{"different":"body"}')

	def test_signed_pull_verifies_before_parse_and_delegates_exact_request_hash(self) -> None:
		events: list[str] = []
		endpoint = _Doc(
			name="ENDPOINT-1",
			enabled=1,
			direction="Inbound",
			authentication_type="HMAC",
			require_signature=1,
			source_system="SOURCE-1",
		)
		raw = (
			b'{"campus":"","department":"","hospital":"HOSPITAL-1","limit":10,'
			b'"request_id":"REQUEST-00000001","ward":""}'
		)
		payload = json.loads(raw)

		def verify(*_args, **_kwargs):
			events.append("verify")

		def parse(_body):
			events.append("parse")
			return payload

		with (
			patch.object(review.frappe, "get_cached_doc", return_value=endpoint),
			patch.object(review, "_validate_archive_pull_endpoint"),
			patch.object(review, "verify_request", side_effect=verify),
			patch.object(review, "_archive_pull_payload", side_effect=parse),
			patch.object(
				review,
				"_deliver_archive_decisions",
				return_value={"decision_count": 0},
			) as deliver,
		):
			result = review.retrieve_archive_decisions(endpoint.name, raw)

		self.assertEqual(events, ["verify", "parse"])
		self.assertEqual(result["decision_count"], 0)
		self.assertEqual(
			deliver.call_args.kwargs["request_hash"],
			review._sha256_bytes(raw),
		)

	def test_delivery_request_replay_returns_exact_stored_response(self) -> None:
		request_hash = "a" * 64
		scope = {
			"hospital": "HOSPITAL-1",
			"campus": "",
			"department": "",
			"ward": "",
		}
		response = {
			"contract_version": 2,
			"decision_count": 0,
			"delivery_status": "Delivered",
			"endpoint": "ENDPOINT-1",
			"envelope_count": 0,
			"envelopes": [],
			"generated_at": "2026-07-30 12:00:00",
			"request_id": "REQUEST-00000001",
			"scope": scope,
			"source_system": "SOURCE-1",
		}
		existing = _Doc(
			name="DELIVERY-1",
			request_hash=request_hash,
			delivery_mode="Signed Pull",
			delivered_by=None,
			response_json=review._canonical_json(response),
		)
		endpoint = _Doc(name="ENDPOINT-1", source_system="SOURCE-1")
		payload = {
			"request_id": "REQUEST-00000001",
			"hospital": "HOSPITAL-1",
			"campus": "",
			"department": "",
			"ward": "",
			"limit": 10,
		}
		with (
			patch.object(review.frappe.db, "advisory_lock", return_value=_lock()),
			patch.object(review.frappe.db, "get_value", return_value=existing.name),
			patch.object(review.frappe, "get_doc", return_value=existing),
			patch.object(review.frappe, "get_cached_doc", return_value=_Doc(name="SOURCE-1")),
			patch.object(review, "_authorized_archive_delivery_scope", return_value=scope),
			patch.object(review, "_outstanding_archive_decisions", return_value=[]),
			patch.object(review, "_assert_delivery_receipt_exact") as assert_exact,
		):
			replayed = review._deliver_archive_decisions(
				endpoint,
				payload,
				request_hash=request_hash,
				delivery_mode="Signed Pull",
				delivered_by=None,
			)
		self.assertEqual(replayed, response)
		self.assertEqual(assert_exact.call_count, 2)

		with (
			patch.object(review.frappe.db, "advisory_lock", return_value=_lock()),
			patch.object(review.frappe.db, "get_value", return_value=existing.name),
			patch.object(review.frappe, "get_doc", return_value=existing),
			patch.object(review.frappe, "throw", side_effect=RuntimeError("different")),
			self.assertRaisesRegex(RuntimeError, "different"),
		):
			review._deliver_archive_decisions(
				endpoint,
				payload,
				request_hash="b" * 64,
				delivery_mode="Signed Pull",
				delivered_by=None,
			)

	def test_signed_delivery_replay_rejects_cleared_pending_linkage(self) -> None:
		decision = _Doc(
			name="a" * 64,
			assignment="ASSIGNMENT-1",
			decision_checksum="b" * 64,
		)
		assignment = _assignment(
			name=decision.assignment,
			latest_archive_decision=decision.name,
			pending_archive_delivery=None,
			pending_archive_envelope_hash=None,
		)
		delivery = _Doc(name="d" * 64)
		response = {
			"envelopes": [
				{
					"envelope_hash": "e" * 64,
					"constituent_decisions": [
						{
							"ack_required": "1",
							"archive_decision": decision.name,
							"decision_checksum": decision.decision_checksum,
						}
					],
				}
			]
		}
		with (
			patch.object(review.frappe, "get_doc", return_value=decision),
			patch.object(
				review.frappe.db,
				"get_value",
				return_value=assignment.medical_record_qc,
			),
			patch.object(review, "_medical_record_archive_lock", return_value=_lock()),
			patch.object(review, "_locked_record_assignments", return_value=[assignment]),
			patch.object(review.frappe, "throw", side_effect=RuntimeError("stale")),
			self.assertRaisesRegex(RuntimeError, "stale"),
		):
			review._assert_signed_delivery_replay_pending(delivery, response)
