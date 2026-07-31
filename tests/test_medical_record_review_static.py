from __future__ import annotations

import ast
import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "ione_qms" / "services" / "medical_record_review.py"
PROJECTIONS = ROOT / "ione_qms" / "services" / "projections.py"
API = ROOT / "ione_qms" / "api" / "medical_record.py"
HOOKS = ROOT / "ione_qms" / "hooks.py"
CONSTANTS = ROOT / "ione_qms" / "constants.py"
PERMISSIONS = ROOT / "ione_qms" / "permissions.py"
SCOPE_HIERARCHY = ROOT / "ione_qms" / "services" / "scope_hierarchy.py"
DATA_EXPORT = ROOT / "ione_qms" / "services" / "data_export.py"
GENERATOR = ROOT / "tools" / "generate_doctypes.py"
CLINICAL_QUALITY_MODULE = ROOT / "ione_qms" / "ione_clinical_quality"
CLINICAL_QUALITY_WORKSPACE = (
	CLINICAL_QUALITY_MODULE / "workspace" / "ione_clinical_quality" / "ione_clinical_quality.json"
)

MEDICAL_RECORD_REVIEW_DOCTYPES = {
	"IONE Medical Record Sampling Policy": "ione_medical_record_sampling_policy",
	"IONE Medical Record Sampling Policy Operation": ("ione_medical_record_sampling_policy_operation"),
	"IONE Medical Record Review Batch": "ione_medical_record_review_batch",
	"IONE Medical Record Review Assignment": "ione_medical_record_review_assignment",
	"IONE Medical Record Review Decision": "ione_medical_record_review_decision",
	"IONE Medical Record Archive Decision": "ione_medical_record_archive_decision",
	"IONE Medical Record Archive Acknowledgement": ("ione_medical_record_archive_acknowledgement"),
	"IONE Medical Record Archive Delivery": "ione_medical_record_archive_delivery",
}
SYSTEM_MEDICAL_RECORD_REVIEW_DOCTYPES = frozenset(MEDICAL_RECORD_REVIEW_DOCTYPES) - {
	"IONE Medical Record Sampling Policy"
}
MEDICAL_RECORD_REVIEW_ACTOR_ROLES = {
	"IONE QMS Medical Record Coder",
	"IONE Medical Record Expert Reviewer",
}
MUTATING_API_FUNCTIONS = {
	"review_medical_record_sampling_policy",
	"operate_medical_record_sampling_policy",
	"create_medical_record_review_batch",
	"claim_medical_record_coder_assignment",
	"claim_medical_record_expert_assignment",
	"submit_medical_record_coder_review",
	"submit_medical_record_expert_review",
	"override_medical_record_archive_decision",
}
ACK_API_FUNCTION = "receive_medical_record_archive_acknowledgement"
WATERMARK_API_FUNCTION = "receive_medical_record_completeness_watermark"
PULL_API_FUNCTION = "pull_medical_record_archive_decisions"
RECONCILE_API_FUNCTION = "reconcile_medical_record_archive_decisions"


def _function_source(source: str, name: str) -> str:
	tree = ast.parse(source)
	node = next(
		item
		for item in tree.body
		if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
	)
	return ast.get_source_segment(source, node) or ""


def _assignment_node(source: str, name: str) -> ast.expr:
	tree = ast.parse(source)
	for node in tree.body:
		if isinstance(node, ast.Assign) and any(
			isinstance(target, ast.Name) and target.id == name for target in node.targets
		):
			return node.value
		if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
			if node.target.id == name and node.value is not None:
				return node.value
	raise AssertionError(f"assignment {name!r} was not found")


def _literal_assignment(source: str, name: str) -> object:
	return ast.literal_eval(_assignment_node(source, name))


def _collection_assignment(source: str, name: str) -> frozenset[str]:
	value = _assignment_node(source, name)
	if (
		isinstance(value, ast.Call)
		and isinstance(value.func, ast.Name)
		and value.func.id == "frozenset"
		and len(value.args) == 1
		and not value.keywords
	):
		value = value.args[0]
	collection = ast.literal_eval(value)
	return frozenset(str(item) for item in collection)


def _doctype_document(slug: str) -> dict[str, Any]:
	path = CLINICAL_QUALITY_MODULE / "doctype" / slug / f"{slug}.json"
	return json.loads(path.read_text(encoding="utf-8"))


def _whitelist_options(source: str, function_name: str) -> dict[str, object]:
	tree = ast.parse(source)
	function = next(
		node
		for node in tree.body
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
	)
	decorators = [
		decorator
		for decorator in function.decorator_list
		if isinstance(decorator, ast.Call)
		and isinstance(decorator.func, ast.Attribute)
		and decorator.func.attr == "whitelist"
	]
	if len(decorators) != 1:
		raise AssertionError(f"{function_name} must have exactly one whitelist decorator")
	return {
		keyword.arg: ast.literal_eval(keyword.value)
		for keyword in decorators[0].keywords
		if keyword.arg is not None
	}


def _load_pure_functions(source: str, names: tuple[str, ...]) -> dict[str, object]:
	tree = ast.parse(source)
	nodes = [item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name in names]

	class _Frappe:
		class ValidationError(RuntimeError):
			pass

		@staticmethod
		def throw(message, *_args):
			raise RuntimeError(message)

	namespace: dict[str, object] = {
		"hashlib": hashlib,
		"hmac": hmac,
		"json": json,
		"Sequence": Sequence,
		"Mapping": Mapping,
		"Any": Any,
		"date": date,
		"timedelta": timedelta,
		"get_datetime": lambda value: (
			datetime.combine(value, time.min)
			if isinstance(value, date) and not isinstance(value, datetime)
			else datetime.fromisoformat(str(value))
		),
		"_site_naive_datetime": lambda value, **_kwargs: (
			value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
		),
		"_is_sha256": lambda value: (
			isinstance(value, str)
			and len(value) == 64
			and all(character in "0123456789abcdef" for character in value)
		),
		"ROUNDING_MODES": frozenset({"Ceiling", "Floor", "Half Up"}),
		"UNDERSIZED_ACTIONS": frozenset({"Sample All", "Fail"}),
		"frappe": _Frappe(),
	}
	exec(  # noqa: S102 - executes only selected AST from this checked-in source file
		compile(ast.Module(body=nodes, type_ignores=[]), str(SERVICE), "exec"),
		namespace,
	)
	return namespace


class TestMedicalRecordReviewStaticContract(TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		cls.source = SERVICE.read_text(encoding="utf-8")

	def test_service_declares_the_complete_audit_object_contract(self) -> None:
		for doctype in (
			"IONE Medical Record Sampling Policy",
			"IONE Medical Record Sampling Policy Operation",
			"IONE Medical Record Review Batch",
			"IONE Medical Record Review Assignment",
			"IONE Medical Record Review Decision",
			"IONE Medical Record Archive Decision",
			"IONE Medical Record Archive Acknowledgement",
			"IONE Medical Record Archive Delivery",
		):
			self.assertIn(f'"{doctype}"', self.source)
		for source_anchor in (
			"source_finalized_at",
			"source_record_id_hash",
			"record_snapshot_hash",
		):
			self.assertIn(f'"{source_anchor}"', self.source)

	def test_policy_is_explicit_disabled_and_independently_governed(self) -> None:
		prepare = _function_source(self.source, "prepare_sampling_policy")
		review = _function_source(self.source, "review_sampling_policy")
		operate = _function_source(self.source, "operate_sampling_policy")
		validate = _function_source(self.source, "_validate_policy_definition")
		self.assertIn('doc.status = "Draft"', prepare)
		self.assertIn("doc.enabled = 0", prepare)
		self.assertIn("authors cannot review their own policy", review)
		self.assertIn("doc.enabled = 0", review)
		self.assertIn('"Activate"', operate)
		self.assertIn("_sampling_secret(doc)", operate)
		for required in (
			"sample_rate_basis_points",
			"minimum_sample_size",
			"maximum_sample_size",
			"expert_sample_rate_basis_points",
			"expert_minimum_sample_size",
			"expert_maximum_sample_size",
			"rounding_mode",
			"undersized_population_action",
			"expert_non_pass_policy",
			"archive_gate_enabled",
			"coder_allow_outcomes_json",
			"expert_allow_outcomes_json",
			"sampling_key_id",
			"sampling_key_fingerprint",
		):
			self.assertIn(required, self.source)
		self.assertIn("must explicitly enable its archive gate", validate)
		self.assertNotIn('default="', validate)

	def test_population_is_permission_filtered_bounded_and_frozen_before_sampling(self) -> None:
		eligible = _function_source(self.source, "_eligible_population")
		create = _function_source(self.source, "create_review_batch")
		population_record = _function_source(self.source, "_population_record")
		create_assignment = _function_source(self.source, "_create_assignment")
		locked_publication = _function_source(
			self.source,
			"_create_assignment_under_release_lock",
		)
		self.assertIn("frappe.get_list(", eligible)
		self.assertNotIn("frappe.get_all(", eligible)
		self.assertNotIn("frappe.db.sql(", eligible)
		self.assertIn("MAX_POPULATION_RECORDS + 1", eligible)
		self.assertIn("exceeds the hard limit", eligible)
		self.assertIn("population_manifest_json", create)
		self.assertIn("population_hash", create)
		self.assertIn("selection_manifest_json", create)
		self.assertIn("expert_selection_manifest_json", create)
		self.assertLess(create.index("population_manifest_json ="), create.index("_ranked_population("))
		self.assertIn("frappe.db.savepoint", create)
		self.assertIn("frappe.db.rollback", create)
		self.assertIn("frappe.db.commit", create)
		self.assertIn('"record_status"', population_record)
		self.assertIn('"patient"', population_record)
		self.assertIn('"encounter"', population_record)
		self.assertIn('"responsible_staff"', population_record)
		self.assertIn('"<"', eligible)
		self.assertIn("end + timedelta(days=1)", eligible)
		self.assertIn("for item in sorted(ranked", create)
		self.assertIn('coder_required=item["token"] in selected_tokens', create)
		self.assertIn("locked_item = _population_record(current)", create_assignment)
		self.assertIn("_create_assignment_under_release_lock(", create_assignment)
		self.assertIn('row = locked_item["row"]', locked_publication)
		anchor = _function_source(self.source, "validate_medical_record_review_anchor")
		archive_transition = _function_source(
			self.source,
			"_is_governed_archive_projection_transition",
		)
		self.assertIn("for_update=True", anchor)
		self.assertIn('changed != {"record_status"}', archive_transition)
		self.assertIn('!= "Final"', archive_transition)
		self.assertIn('!= "Archived"', archive_transition)
		archive_ready = _function_source(self.source, "_all_archive_gates_applied")
		self.assertIn('!= "Allow"', archive_ready)
		self.assertIn('!= "Applied"', archive_ready)

	def test_source_watermark_reconciles_the_entire_current_source_identity_population(self) -> None:
		namespace = _load_pure_functions(
			self.source,
			(
				"_source_population_manifest",
				"_assert_source_population_watermark",
				"_as_mapping",
				"_canonical_json",
				"_sha256",
			),
		)
		manifest = namespace["_source_population_manifest"]
		assert_watermark = namespace["_assert_source_population_watermark"]
		row = {
			"name": "LOCAL-1",
			"patient": "PATIENT-1",
			"record_status": "Final",
			"source_record_type": "MedicalRecord",
			"source_record_id_hash": "a" * 64,
			"record_snapshot_hash": "b" * 64,
			"source_finalized_at": "2026-06-15 12:00:00",
		}
		late = {
			**row,
			"name": "LOCAL-2",
			"patient": "PATIENT-2",
			"source_record_id_hash": "c" * 64,
			"record_snapshot_hash": "d" * 64,
			"source_finalized_at": "2026-06-30 23:59:59",
		}
		period = {
			"period_start": date(2026, 6, 1),
			"period_end": date(2026, 6, 30),
		}
		reconciled = manifest([row, late], **period)
		self.assertEqual(reconciled["source_record_count"], 2)
		self.assertEqual(
			reconciled,
			manifest(
				[{**late, "name": "OTHER-2"}, {**row, "patient": "OTHER-1"}],
				**period,
			),
		)
		self.assertNotEqual(
			reconciled["source_manifest_hash"],
			manifest(
				[{**row, "source_finalized_at": "2026-06-15 12:00:01"}, late],
				**period,
			)["source_manifest_hash"],
		)
		self.assertEqual(
			assert_watermark(
				{
					"source_record_count": 2,
					"source_manifest_hash": reconciled["source_manifest_hash"],
					"source_asserted_at": "2026-07-01 00:00:00",
				},
				[row, late],
				**period,
			),
			reconciled,
		)
		empty = manifest([], **period)
		for case, watermark, current in (
			(
				"early_empty",
				{
					"source_record_count": 0,
					"source_manifest_hash": empty["source_manifest_hash"],
					"source_asserted_at": "2026-07-01 00:00:00",
				},
				[row],
			),
			(
				"dropped_record",
				{
					"source_record_count": 2,
					"source_manifest_hash": reconciled["source_manifest_hash"],
					"source_asserted_at": "2026-07-01 00:00:00",
				},
				[row],
			),
			(
				"late_supplemental_mismatch",
				{
					"source_record_count": 1,
					"source_manifest_hash": manifest([row], **period)["source_manifest_hash"],
					"source_asserted_at": "2026-07-01 00:00:00",
				},
				[row, late],
			),
		):
			with (
				self.subTest(case=case),
				self.assertRaisesRegex(
					RuntimeError,
					"entire current eligible population",
				),
			):
				assert_watermark(watermark, current, **period)
		with self.assertRaisesRegex(RuntimeError, "duplicate identities"):
			manifest([row, dict(row)], **period)
		with self.assertRaisesRegex(RuntimeError, "predates"):
			assert_watermark(
				{
					"source_record_count": 2,
					"source_manifest_hash": reconciled["source_manifest_hash"],
					"source_asserted_at": "2026-06-30 23:00:00",
				},
				[row, late],
				**period,
			)

	def test_selection_is_keyed_deterministic_and_has_no_clinical_default(self) -> None:
		rank = _function_source(self.source, "_ranked_population")
		secret = _function_source(self.source, "_sampling_secret")
		self.assertIn("hmac.new(secret", rank)
		self.assertIn('domain="primary"', self.source)
		self.assertIn('domain="expert"', self.source)
		self.assertIn("ione_medical_record_sampling_secrets", secret)
		self.assertIn("shorter than 32 bytes", secret)
		self.assertIn("fingerprint does not match", secret)
		self.assertNotIn("random.", self.source)

		namespace = _load_pure_functions(self.source, ("_sample_size", "_ranked_population"))
		sample_size = namespace["_sample_size"]
		self.assertEqual(
			sample_size(
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
			sample_size(
				101,
				rate_basis_points=1_000,
				minimum=1,
				maximum=100,
				rounding_mode="Floor",
				undersized_action="Sample All",
			),
			10,
		)
		population = [{"token": character * 64, "row": {"name": character}} for character in "abc"]
		rank_population = namespace["_ranked_population"]
		first = rank_population(
			population,
			secret=b"x" * 32,
			domain="primary",
			batch_key="b" * 64,
		)
		second = rank_population(
			list(reversed(population)),
			secret=b"x" * 32,
			domain="primary",
			batch_key="b" * 64,
		)
		self.assertEqual([item["token"] for item in first], [item["token"] for item in second])

	def test_coder_expert_decisions_are_independent_append_only_and_evidence_bound(self) -> None:
		claim_expert = _function_source(self.source, "claim_expert_assignment")
		submit = _function_source(self.source, "_submit_review")
		review_validator = _function_source(self.source, "validate_review_decision")
		deletion = _function_source(self.source, "prevent_medical_record_review_deletion")
		self.assertIn('"IONE QMS Medical Record Coder"', self.source)
		self.assertIn('"IONE Medical Record Expert Reviewer"', self.source)
		self.assertIn("must be independent named users", self.source)
		self.assertIn('other_actor_field="assigned_coder"', claim_expert)
		self.assertIn("non-pass medical-record decision requires", submit)
		self.assertIn("_new_review_decision(", submit)
		self.assertIn("_issue_policy_archive_decision(", submit)
		self.assertIn("_require_creation_capability(", review_validator)
		self.assertIn("are immutable", self.source)
		self.assertIn("retained audit records", deletion)
		self.assertNotIn("frappe.db.set_value", self.source)

	def test_archive_gate_uses_frozen_outcome_mapping_and_override_is_a_new_record(self) -> None:
		issue = _function_source(self.source, "_issue_policy_archive_decision")
		override = _function_source(self.source, "override_archive_decision")
		new_archive = _function_source(self.source, "_new_archive_decision")
		self.assertIn("coder_allow_outcomes_json", issue)
		self.assertIn("expert_allow_outcomes_json", issue)
		self.assertIn('decision = "Allow" if outcome in allowed else "Hold"', issue)
		self.assertIn('"deterministic_gate_status"', issue)
		self.assertIn("cannot convert it to Allow", issue)
		self.assertIn("no clinical inference was performed", issue)
		self.assertIn("requires an independent actor across all record governance", override)
		self.assertIn("supersedes=previous.name", override)
		self.assertIn("_new_archive_decision(", override)
		self.assertIn('"decision_type": decision_type', new_archive)
		self.assertNotIn("UPDATE ", self.source)
		self.assertNotIn("DELETE ", self.source)

	def test_source_ack_is_exact_signed_replay_safe_and_never_uses_raw_source_id(self) -> None:
		ack = _function_source(self.source, "acknowledge_archive_decision")
		payload = _function_source(self.source, "_archive_ack_payload")
		self.assertLess(ack.index("verify_request("), ack.index("_archive_ack_payload("))
		self.assertIn("force_signature=True", ack)
		self.assertIn("assert_integration_endpoint_runtime", ack)
		self.assertIn("request_hash", ack)
		self.assertIn("replayed with different content", ack)
		self.assertIn("latest_archive_decision", ack)
		self.assertIn("source_record_id_hash", payload)
		self.assertIn("_canonical_json(payload) != text", payload)
		self.assertIn("set(payload) != required", payload)
		self.assertIn("fields must all be JSON strings", payload)
		self.assertIn("require_timezone=True", payload)
		self.assertNotIn('"source_record_id"', self.source)

	def test_projection_writes_use_two_exact_capabilities_and_archive_rechecks_receipts(self) -> None:
		source = PROJECTIONS.read_text(encoding="utf-8")
		materialize = _function_source(source, "materialize_projection")
		validate = _function_source(source, "validate_projection_identity")
		apply_archive = _function_source(source, "apply_medical_record_archive_projection")
		self.assertIn("_PROJECTION_MATERIALIZER_CAPABILITY", materialize)
		self.assertIn("is _PROJECTION_MATERIALIZER_CAPABILITY", validate)
		self.assertIn("is _MEDICAL_RECORD_ARCHIVE_APPLICATION_CAPABILITY", validate)
		self.assertIn("distinct archive-application service", validate)
		self.assertIn("assert_medical_record_archive_ready(doc)", apply_archive)
		self.assertIn('doc.record_status = "Archived"', apply_archive)
		self.assertNotIn("frappe.db.set_value", apply_archive)

	def test_full_population_gets_dispositions_and_rules_are_exact_snapshot_bound(self) -> None:
		create = _function_source(self.source, "create_review_batch")
		locked_publication = _function_source(
			self.source,
			"_create_assignment_under_release_lock",
		)
		rule_gate = _function_source(self.source, "_mandatory_rule_gate")
		initial = _function_source(self.source, "_initial_archive_disposition")
		active_policy = _function_source(self.source, "_validate_active_policy")
		self.assertIn("for item in sorted(ranked", create)
		self.assertIn('coder_required=item["token"] in selected_tokens', create)
		self.assertIn("_mandatory_rule_gate(policy, row)", locked_publication)
		self.assertIn("_new_archive_decision(", locked_publication)
		self.assertIn('decision_type="Deterministic Gate"', locked_publication)
		self.assertIn('"status": "Coder Review" if coder_required else "Closed"', locked_publication)
		self.assertIn('"rule_version": item["rule_version"]', rule_gate)
		self.assertIn('"context_hash": snapshot_hash', rule_gate)
		self.assertIn("snapshot_hash", rule_gate)
		for reason in (
			"MANDATORY_RULE_EXECUTION_MISSING",
			"MANDATORY_RULE_SNAPSHOT_MISMATCH",
			"MANDATORY_RULE_FAILED",
			"MANDATORY_RULE_INSUFFICIENT_DATA",
			"MANDATORY_RULE_EXECUTION_ERROR",
		):
			self.assertIn(reason, rule_gate)
		self.assertIn("auto_allow_deterministic_pass", initial)
		self.assertIn('return "Hold"', initial)
		self.assertIn("require_activation_ready=False", active_policy)
		self.assertNotIn("require_activation_ready=True", active_policy)

	def test_signed_pull_is_bounded_idempotent_scope_bound_and_operator_independent(self) -> None:
		pull = _function_source(self.source, "retrieve_archive_decisions")
		deliver = _function_source(self.source, "_deliver_archive_decisions")
		outstanding = _function_source(self.source, "_outstanding_archive_decisions")
		operator = _function_source(self.source, "reconcile_archive_decisions")
		self.assertLess(pull.index("verify_request("), pull.index("_archive_pull_payload("))
		self.assertIn("force_signature=True", pull)
		self.assertIn("MAX_ARCHIVE_PULL_BYTES", pull)
		self.assertIn("delivery_key", deliver)
		self.assertIn("replayed with different authority or content", deliver)
		self.assertIn("_authorized_archive_delivery_scope(", deliver)
		self.assertIn("response_hash", deliver)
		self.assertIn("MAX_ARCHIVE_PULL_SCAN + 1", outstanding)
		self.assertIn('"archive_ack_status"', outstanding)
		self.assertIn('"archive_ack_status", "is", "not set"', outstanding)
		self.assertIn('"archive_ack_status", "in", ["Rejected", "Failed"]', outstanding)
		self.assertIn(
			'"source_record_id_hash"',
			_function_source(self.source, "_effective_archive_envelope"),
		)
		self.assertNotIn('"source_record_id"', outstanding)
		self.assertIn("_named_role_user(ARCHIVE_RECONCILIATION_ROLES)", operator)
		self.assertIn('require_role("IONE Integration Operator"', operator)

	def test_release_publication_is_record_locked_enveloped_and_currentness_checked(self) -> None:
		projection_source = PROJECTIONS.read_text(encoding="utf-8")
		release_lock = _function_source(projection_source, "medical_record_release_lock")
		for invariant in (
			"ione-qms:medical-record-release:",
			"_MEDICAL_RECORD_RELEASE_LOCK_CAPABILITY",
			"for_update=True",
		):
			self.assertIn(invariant, release_lock)

		create_assignment = _function_source(self.source, "_create_assignment")
		locked_publication = _function_source(
			self.source,
			"_create_assignment_under_release_lock",
		)
		submit = _function_source(self.source, "_submit_review")
		override = _function_source(self.source, "override_archive_decision")
		acknowledge = _function_source(self.source, "acknowledge_archive_decision")
		outstanding = _function_source(self.source, "_outstanding_archive_decisions")
		for operation in (create_assignment, submit, override, acknowledge, outstanding):
			self.assertIn("_medical_record_archive_lock(", operation)
		create_tree = ast.parse(create_assignment)
		release_scopes = [node for node in ast.walk(create_tree) if isinstance(node, ast.With)]
		self.assertTrue(
			any(
				any(
					isinstance(call.func, ast.Name)
					and call.func.id == "_create_assignment_under_release_lock"
					for call in ast.walk(release_scope)
					if isinstance(call, ast.Call)
				)
				for release_scope in release_scopes
			)
		)
		self.assertIn("assert_medical_record_release_lock", locked_publication)
		self.assertIn("_mandatory_rule_gate(", locked_publication)
		self.assertIn("_new_archive_decision(", locked_publication)
		self.assertIn("_update_assignment(", locked_publication)

		envelope = _function_source(self.source, "_effective_archive_envelope")
		self.assertIn("all_closed_allow", envelope)
		self.assertIn('str(assignment.get("status") or "") == "Closed"', envelope)
		self.assertIn('str(decision.get("decision") or "") == "Allow"', envelope)
		self.assertIn('"effective_decision": "Allow" if all_closed_allow else "Hold"', envelope)
		self.assertIn('"_publishable": all_closed', envelope)
		self.assertIn("if all_closed", envelope)
		self.assertIn('allowed_pending_delivery != "*"', envelope)
		self.assertIn('"status": "Closed"', outstanding)
		self.assertIn('envelope["_publishable"]', outstanding)

		deliver = _function_source(self.source, "_deliver_archive_decisions")
		delivery_replay = _function_source(
			self.source,
			"_assert_signed_delivery_replay_pending",
		)
		self.assertIn('"pending_archive_delivery": delivery.name', deliver)
		self.assertIn('"pending_archive_envelope_hash": envelope["envelope_hash"]', deliver)
		self.assertIn("_assert_delivery_receipt_exact(existing, response)", deliver)
		self.assertIn("_assert_signed_delivery_replay_pending(existing, stored_response)", deliver)
		self.assertIn('str(item.get("ack_required") or "") != "1"', delivery_replay)
		self.assertIn("pending_archive_delivery", delivery_replay)
		self.assertIn("pending_archive_envelope_hash", delivery_replay)
		self.assertNotIn("for_update=True", deliver)

		replay = _function_source(self.source, "_assert_archive_ack_replay_current")
		self.assertIn("latest_archive_decision", replay)
		self.assertIn("archive_acknowledgement", replay)
		self.assertIn("pending_archive_delivery", replay)
		self.assertIn("_assert_delivery_contains_pending_decision(", replay)
		self.assertIn("_assert_archive_ack_replay_current(", acknowledge)

	def test_completeness_watermark_and_supplemental_chain_are_fail_closed(self) -> None:
		create = _function_source(self.source, "create_review_batch")
		batch_key = _function_source(self.source, "_batch_key")
		chain = _function_source(self.source, "_validate_batch_chain_lineage")
		coverage = _function_source(self.source, "_validate_batch_assignment_coverage")
		watermark = _function_source(self.source, "_validated_completeness_watermark")
		current_watermark = _function_source(
			self.source,
			"_locked_current_completeness_watermark",
		)
		receive_watermark = _function_source(
			self.source,
			"receive_completeness_watermark",
		)
		watermark_progress = _function_source(
			self.source,
			"_validate_watermark_progress",
		)
		manifest = _function_source(self.source, "_source_population_manifest")
		self.assertIn("end >= getdate(now_datetime())", create)
		self.assertIn("_locked_current_completeness_watermark(", create)
		self.assertIn("_validated_completeness_watermark(", current_watermark)
		self.assertIn("_latest_completeness_watermark(", current_watermark)
		self.assertIn("current latest completeness watermark", current_watermark)
		self.assertIn("_completeness_stream_lock_key(", receive_watermark)
		self.assertIn("_latest_completeness_watermark(", receive_watermark)
		self.assertIn('"source_sequence"', receive_watermark)
		self.assertIn("_assert_watermark_cutoff_time(", receive_watermark)
		self.assertIn('"source_sequence"', watermark_progress)
		self.assertIn("source_asserted_at", watermark_progress)
		self.assertIn("_assert_source_population_watermark(", create)
		self.assertIn("_validate_batch_chain_lineage(", create)
		self.assertIn("Supplemental batch", create)
		self.assertIn("uncovered late Final records", create)
		for frozen_identity in (
			"parent_batch",
			"source_completeness_hash",
			"source_completeness_watermark",
			"source_complete_through",
			"source_manifest_hash",
			"source_record_count",
			"source_system",
			"supplement_sequence",
		):
			self.assertIn(f'"{frozen_identity}"', batch_key)
		self.assertIn("_validate_existing_batch(", chain)
		self.assertIn("_validate_watermark_progress(", chain)
		self.assertIn("population_token", coverage)
		self.assertIn("sample_count", coverage)
		self.assertIn("expert_sample_count", coverage)
		self.assertIn("population_hash", coverage)
		self.assertIn("does not cover the requested period end", watermark)
		self.assertIn('"source_finalized_at"', manifest)
		self.assertIn('"eligible_record_status": "Final"', manifest)
		self.assertIn('"population_date_field": "source_finalized_at"', manifest)
		self.assertIn('"population_token"', GENERATOR.read_text(encoding="utf-8"))

	def test_conflicting_execution_receipts_and_override_chain_fail_closed(self) -> None:
		rule_gate = _function_source(self.source, "_mandatory_rule_gate")
		manifest_validator = _function_source(self.source, "_validate_execution_manifest")
		chain = _function_source(self.source, "_archive_supersession_chain")
		exclusions = _function_source(self.source, "_record_override_excluded_actors")
		policy_chain = _function_source(self.source, "_policy_operation_chain")
		policy_operation = _function_source(self.source, "operate_sampling_policy")
		override = _function_source(self.source, "override_archive_decision")
		self.assertIn("MAX_MANDATORY_EXECUTION_RECEIPTS_PER_RULE + 1", rule_gate)
		self.assertIn("len(exact_receipts) != 1", rule_gate)
		self.assertIn("len(results) != 1", rule_gate)
		self.assertIn("MANDATORY_RULE_EXECUTION_CONFLICT", rule_gate)
		self.assertIn("MAX_MANDATORY_EXECUTION_RECEIPTS_PER_RULE + 1", manifest_validator)
		self.assertIn("MAX_ARCHIVE_SUPERSESSION_DEPTH", chain)
		self.assertIn("contains a cycle", chain)
		self.assertIn("crosses assignments", chain)
		for excluded_actor in (
			"assigned_coder",
			"assigned_expert",
			"triggered_by",
			"reviewed_by",
			"created_by",
			"requested_by",
			"operated_by",
		):
			self.assertIn(excluded_actor, exclusions)
		self.assertIn("_locked_record_assignments(", override)
		self.assertIn("_policy_operation_locks(", override)
		self.assertIn("record_assignments", override)
		self.assertIn("MAX_POLICY_OPERATION_DEPTH", policy_chain)
		self.assertIn("previous_operation", policy_chain)
		self.assertIn("contains a cycle", policy_chain)
		self.assertIn("for assignment in assignments", exclusions)
		self.assertIn("_policy_operation_chain(policy)", exclusions)
		self.assertIn("for operation in", exclusions)
		self.assertIn("validated_policies", exclusions)
		self.assertIn("ione_medical_record_policy_operation_creation", policy_operation)
		self.assertIn("latest_operation", policy_operation)
		self.assertIn("pending_archive_delivery", override)
		self.assertIn('"archive_ack_status"', override)
		self.assertIn("An applied archive decision cannot be overridden", override)
		contract = (ROOT / "docs" / "medical-record-prearchive-contract.md").read_text(encoding="utf-8")
		self.assertIn("external, independently", contract)
		self.assertIn("dual-review gate", contract)
		self.assertIn("`Held` to `Allow`", contract)

	def test_named_actor_and_scope_guards_fail_closed(self) -> None:
		named = _function_source(self.source, "_named_user")
		access = _function_source(self.source, "_require_assignment_access")
		policy_validator = _function_source(self.source, "validate_sampling_policy")
		policy_review = _function_source(self.source, "review_sampling_policy")
		policy_operation = _function_source(self.source, "operate_sampling_policy")
		self.assertIn('{"", "Guest", "Administrator"}', named)
		self.assertIn('"IONE Agent Service"', named)
		self.assertIn("user is disabled", named)
		self.assertIn("assignment.check_permission", access)
		self.assertIn("require_scope_read", access)
		self.assertIn("_named_role_user(POLICY_AUTHOR_ROLES)", policy_validator)
		self.assertIn("require_scope_read", policy_validator)
		self.assertIn("require_scope_read", policy_review)
		self.assertIn("require_scope_read", policy_operation)

	def test_generated_doctypes_are_governed_and_system_artifacts_are_read_only(self) -> None:
		documents = {
			doctype: _doctype_document(slug) for doctype, slug in MEDICAL_RECORD_REVIEW_DOCTYPES.items()
		}
		for doctype, document in documents.items():
			with self.subTest(doctype=doctype):
				self.assertEqual(document["doctype"], "DocType")
				self.assertEqual(document["name"], doctype)
				self.assertEqual(document["module"], "IONE Clinical Quality")
				self.assertEqual(int(document.get("allow_import", 0) or 0), 0)
				self.assertEqual(int(document.get("allow_rename", 0) or 0), 0)
				self.assertEqual(int(document.get("track_changes", 0) or 0), 1)

				permissions = document["permissions"]
				expected_read_roles = (
					{
						"IONE Integration Administrator",
						"IONE Integration Operator",
						"IONE QMS Auditor",
					}
					if doctype == "IONE Medical Record Archive Delivery"
					else MEDICAL_RECORD_REVIEW_ACTOR_ROLES
				)
				for role in expected_read_roles:
					role_permissions = [
						permission for permission in permissions if permission.get("role") == role
					]
					self.assertTrue(role_permissions, f"{role} is missing read access to {doctype}")
					for permission in role_permissions:
						self.assertEqual(int(permission.get("read", 0) or 0), 1)
						for operation in ("create", "write", "delete", "export", "print"):
							self.assertEqual(
								int(permission.get(operation, 0) or 0),
								0,
								f"{role} unexpectedly has {operation} on {doctype}",
							)

		policy_permissions = documents["IONE Medical Record Sampling Policy"]["permissions"]
		for operation in ("create", "write"):
			self.assertEqual(
				{
					permission["role"]
					for permission in policy_permissions
					if int(permission.get(operation, 0) or 0)
				},
				{"IONE QC Reviewer", "IONE Medical Affairs"},
			)
		for permission in policy_permissions:
			for operation in ("delete", "export", "print"):
				self.assertEqual(int(permission.get(operation, 0) or 0), 0)

		key_fields = {
			"IONE Medical Record Sampling Policy Operation": "operation_key",
			"IONE Medical Record Review Batch": "batch_key",
			"IONE Medical Record Review Assignment": "assignment_key",
			"IONE Medical Record Review Decision": "decision_key",
			"IONE Medical Record Archive Decision": "archive_key",
			"IONE Medical Record Archive Acknowledgement": "ack_key",
			"IONE Medical Record Archive Delivery": "delivery_key",
		}
		for doctype in SYSTEM_MEDICAL_RECORD_REVIEW_DOCTYPES:
			document = documents[doctype]
			with self.subTest(system_doctype=doctype):
				for permission in document["permissions"]:
					for operation in ("create", "write", "delete", "export", "print"):
						self.assertEqual(
							int(permission.get(operation, 0) or 0),
							0,
							f"{doctype} exposes {operation} to {permission['role']}",
						)
				fields = {field["fieldname"]: field for field in document["fields"]}
				key = fields[key_fields[doctype]]
				self.assertEqual(key["fieldtype"], "Data")
				self.assertEqual(int(key.get("reqd", 0) or 0), 1)
				self.assertEqual(int(key.get("unique", 0) or 0), 1)
				self.assertEqual(int(key.get("read_only", 0) or 0), 1)
				self.assertEqual(int(key.get("length", 0) or 0), 64)

	def test_medical_record_qc_has_five_immutable_source_anchors(self) -> None:
		document = _doctype_document("ione_medical_record_qc")
		fields = {field["fieldname"]: field for field in document["fields"]}
		expectations = {
			"source_system": {
				"fieldtype": "Link",
				"options": "IONE Source System",
				"read_only": 1,
			},
			"source_record_type": {
				"fieldtype": "Data",
				"read_only": 1,
				"length": 200,
			},
			"source_record_id_hash": {
				"fieldtype": "Data",
				"read_only": 1,
				"no_copy": 1,
				"length": 64,
			},
			"source_finalized_at": {
				"fieldtype": "Datetime",
				"read_only": 1,
			},
			"record_snapshot_hash": {
				"fieldtype": "Data",
				"read_only": 1,
				"no_copy": 1,
				"length": 64,
			},
		}
		for fieldname, expected in expectations.items():
			with self.subTest(fieldname=fieldname):
				self.assertIn(fieldname, fields)
				for property_name, value in expected.items():
					self.assertEqual(fields[fieldname].get(property_name), value)
		self.assertNotIn("source_record_id", fields)

		for role in MEDICAL_RECORD_REVIEW_ACTOR_ROLES:
			role_permissions = [
				permission for permission in document["permissions"] if permission.get("role") == role
			]
			self.assertTrue(role_permissions)
			self.assertTrue(all(int(permission.get("read", 0) or 0) == 1 for permission in role_permissions))
			self.assertTrue(
				all(
					int(permission.get(operation, 0) or 0) == 0
					for permission in role_permissions
					for operation in ("create", "write", "delete", "export", "print")
				)
			)

		for permission in document["permissions"]:
			for operation in ("create", "write", "delete"):
				self.assertEqual(
					int(permission.get(operation, 0) or 0),
					0,
					f"{permission['role']} unexpectedly has {operation} on medical-record QC",
				)
		for field in document["fields"]:
			self.assertEqual(
				int(field.get("read_only", 0) or 0),
				1,
				f"{field['fieldname']} is manually writable on medical-record QC",
			)

	def test_hooks_wire_scope_permissions_validators_and_deletion_guards(self) -> None:
		source = HOOKS.read_text(encoding="utf-8")
		doctype_js = _literal_assignment(source, "doctype_js")
		queries = _literal_assignment(source, "permission_query_conditions")
		document_permissions = _literal_assignment(source, "has_permission")
		events = _literal_assignment(source, "doc_events")
		for doctype in (
			"IONE Medical Record Sampling Policy",
			"IONE Medical Record Review Assignment",
			"IONE Medical Record Archive Decision",
		):
			self.assertEqual(doctype_js[doctype], "public/js/medical_record_review.js")
		expected_queries = {
			"IONE Medical Record Sampling Policy": (
				"ione_qms.permissions.medical_record_sampling_policy_query"
			),
			"IONE Medical Record Sampling Policy Operation": (
				"ione_qms.permissions.medical_record_sampling_policy_operation_query"
			),
			"IONE Medical Record Review Batch": ("ione_qms.permissions.medical_record_review_batch_query"),
			"IONE Medical Record Review Assignment": (
				"ione_qms.permissions.medical_record_review_assignment_query"
			),
			"IONE Medical Record Review Decision": (
				"ione_qms.permissions.medical_record_review_decision_query"
			),
			"IONE Medical Record Archive Decision": (
				"ione_qms.permissions.medical_record_archive_decision_query"
			),
			"IONE Medical Record Archive Acknowledgement": (
				"ione_qms.permissions.medical_record_archive_acknowledgement_query"
			),
			"IONE Medical Record Archive Delivery": (
				"ione_qms.permissions.medical_record_archive_delivery_query"
			),
		}
		expected_permissions = {
			doctype: query.replace("_query", "_permission") for doctype, query in expected_queries.items()
		}
		for doctype in MEDICAL_RECORD_REVIEW_DOCTYPES:
			with self.subTest(doctype=doctype):
				self.assertEqual(queries[doctype], expected_queries[doctype])
				self.assertEqual(document_permissions[doctype], expected_permissions[doctype])

		anchor_events = events["IONE Medical Record QC"]
		self.assertIn(
			"ione_qms.services.medical_record_review.validate_medical_record_review_anchor",
			anchor_events["validate"],
		)
		for event in ("on_cancel", "on_trash"):
			self.assertEqual(
				anchor_events[event],
				"ione_qms.services.medical_record_review.prevent_sampled_medical_record_deletion",
			)

		policy_events = events["IONE Medical Record Sampling Policy"]
		self.assertEqual(
			policy_events["before_insert"],
			"ione_qms.services.medical_record_review.prepare_sampling_policy",
		)
		self.assertEqual(
			policy_events["validate"],
			"ione_qms.services.medical_record_review.validate_sampling_policy",
		)
		validators = {
			"IONE Medical Record Sampling Policy Operation": ("validate_sampling_policy_operation"),
			"IONE Medical Record Review Batch": "validate_review_batch",
			"IONE Medical Record Review Assignment": "validate_review_assignment",
			"IONE Medical Record Review Decision": "validate_review_decision",
			"IONE Medical Record Archive Decision": "validate_archive_decision",
			"IONE Medical Record Archive Acknowledgement": ("validate_archive_acknowledgement"),
			"IONE Medical Record Archive Delivery": "validate_archive_delivery",
		}
		for doctype, validator in validators.items():
			self.assertEqual(
				events[doctype]["validate"],
				f"ione_qms.services.medical_record_review.{validator}",
			)
		for doctype in MEDICAL_RECORD_REVIEW_DOCTYPES:
			for event in ("on_cancel", "on_trash"):
				self.assertEqual(
					events[doctype][event],
					"ione_qms.services.medical_record_review.prevent_medical_record_review_deletion",
				)

	def test_api_mutations_are_post_only_and_ack_uses_exact_request_bytes(self) -> None:
		source = API.read_text(encoding="utf-8")
		for function_name in MUTATING_API_FUNCTIONS:
			with self.subTest(function_name=function_name):
				options = _whitelist_options(source, function_name)
				self.assertEqual(options.get("methods"), ["POST"])
				self.assertFalse(bool(options.get("allow_guest", False)))

		ack_options = _whitelist_options(source, ACK_API_FUNCTION)
		self.assertEqual(ack_options.get("methods"), ["POST"])
		self.assertIs(ack_options.get("allow_guest"), True)
		watermark_options = _whitelist_options(source, WATERMARK_API_FUNCTION)
		self.assertEqual(watermark_options.get("methods"), ["POST"])
		self.assertIs(watermark_options.get("allow_guest"), True)
		pull_options = _whitelist_options(source, PULL_API_FUNCTION)
		self.assertEqual(pull_options.get("methods"), ["POST"])
		self.assertIs(pull_options.get("allow_guest"), True)
		reconcile_options = _whitelist_options(source, RECONCILE_API_FUNCTION)
		self.assertEqual(reconcile_options.get("methods"), ["POST"])
		self.assertFalse(bool(reconcile_options.get("allow_guest", False)))

		tree = ast.parse(source)
		whitelisted_functions = {
			node.name
			for node in tree.body
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
			and any(
				isinstance(decorator, ast.Call)
				and isinstance(decorator.func, ast.Attribute)
				and decorator.func.attr == "whitelist"
				for decorator in node.decorator_list
			)
		}
		self.assertEqual(
			whitelisted_functions,
			MUTATING_API_FUNCTIONS
			| {
				ACK_API_FUNCTION,
				WATERMARK_API_FUNCTION,
				PULL_API_FUNCTION,
				RECONCILE_API_FUNCTION,
			},
		)

		ack = _function_source(source, ACK_API_FUNCTION)
		self.assertIn('getattr(request, "is_json", False)', ack)
		self.assertIn("MAX_ARCHIVE_ACK_BYTES", ack)
		self.assertIn("request.get_data(cache=True, as_text=False)", ack)
		self.assertIn("type(raw_body) is not bytes", ack)
		self.assertIn("len(raw_body) != content_length", ack)
		self.assertIn("acknowledge_archive_decision(endpoint_name, raw_body)", ack)
		self.assertNotIn("get_json(", ack)
		self.assertNotIn(".decode(", ack)
		pull = _function_source(source, PULL_API_FUNCTION)
		self.assertIn("MAX_ARCHIVE_PULL_BYTES", pull)
		self.assertIn("request.get_data(cache=True, as_text=False)", pull)
		self.assertIn("retrieve_archive_decisions(endpoint_name, raw_body)", pull)
		watermark = _function_source(source, WATERMARK_API_FUNCTION)
		self.assertIn("MAX_ARCHIVE_PULL_BYTES", watermark)
		self.assertIn("request.get_data(cache=True, as_text=False)", watermark)
		self.assertIn("receive_completeness_watermark(endpoint_name, raw_body)", watermark)

	def test_roles_scope_native_export_workspace_and_actor_read_narrowing_are_wired(
		self,
	) -> None:
		constants = CONSTANTS.read_text(encoding="utf-8")
		app_roles = _collection_assignment(constants, "APP_ROLES")
		department_roles = _collection_assignment(constants, "DEPARTMENT_SCOPED_ROLES")
		global_read_roles = _collection_assignment(constants, "GLOBAL_CLINICAL_READ_ROLES")
		global_write_roles = _collection_assignment(constants, "GLOBAL_CLINICAL_WRITE_ROLES")
		self.assertLessEqual(MEDICAL_RECORD_REVIEW_ACTOR_ROLES, app_roles)
		self.assertLessEqual(MEDICAL_RECORD_REVIEW_ACTOR_ROLES, department_roles)
		self.assertTrue(MEDICAL_RECORD_REVIEW_ACTOR_ROLES.isdisjoint(global_read_roles))
		self.assertTrue(MEDICAL_RECORD_REVIEW_ACTOR_ROLES.isdisjoint(global_write_roles))

		scope_source = SCOPE_HIERARCHY.read_text(encoding="utf-8")
		scoped_doctypes = _collection_assignment(scope_source, "CLINICAL_SCOPE_DOCTYPES")
		self.assertLessEqual(
			set(MEDICAL_RECORD_REVIEW_DOCTYPES) - {"IONE Medical Record Archive Acknowledgement"},
			scoped_doctypes,
		)
		self.assertNotIn("IONE Medical Record Archive Acknowledgement", scoped_doctypes)

		export_source = DATA_EXPORT.read_text(encoding="utf-8")
		for assignment in (
			"EXPORTABLE_DOCTYPES",
			"SENSITIVE_EXPORT_DOCTYPES",
			"GOVERNED_NATIVE_EXPORT_DOCTYPES",
		):
			with self.subTest(export_assignment=assignment):
				self.assertLessEqual(
					set(MEDICAL_RECORD_REVIEW_DOCTYPES),
					_collection_assignment(export_source, assignment),
				)
		generator_source = GENERATOR.read_text(encoding="utf-8")
		self.assertLessEqual(
			set(MEDICAL_RECORD_REVIEW_DOCTYPES),
			_collection_assignment(generator_source, "SENSITIVE_EXPORT_DOCTYPES"),
		)

		workspace = json.loads(CLINICAL_QUALITY_WORKSPACE.read_text(encoding="utf-8"))
		workspace_roles = {
			role["role"] for role in workspace["roles"] if isinstance(role, dict) and role.get("role")
		}
		workspace_links = {
			link["link_to"]
			for link in workspace["sidebar_items"]
			if isinstance(link, dict) and link.get("link_to")
		}
		self.assertLessEqual(MEDICAL_RECORD_REVIEW_ACTOR_ROLES, workspace_roles)
		self.assertLessEqual(set(MEDICAL_RECORD_REVIEW_DOCTYPES), workspace_links)

		permission_source = PERMISSIONS.read_text(encoding="utf-8")
		assignment_query = _function_source(
			permission_source,
			"medical_record_review_assignment_query",
		)
		actor_condition = _function_source(
			permission_source,
			"_medical_record_assignment_actor_condition",
		)
		assignment_permission = _function_source(
			permission_source,
			"medical_record_review_assignment_permission",
		)
		decision_query = _function_source(
			permission_source,
			"medical_record_review_decision_query",
		)
		decision_permission = _function_source(
			permission_source,
			"medical_record_review_decision_permission",
		)
		linked_query = _function_source(
			permission_source,
			"_medical_record_linked_assignment_query",
		)
		linked_permission = _function_source(
			permission_source,
			"_medical_record_linked_assignment_permission",
		)
		self.assertIn("_clinical_condition(", assignment_query)
		self.assertIn("_medical_record_assignment_actor_condition(", assignment_query)
		self.assertIn("_combine_read_conditions(base, actor)", assignment_query)
		for expected in (
			"IONE QMS Medical Record Coder",
			"IONE Medical Record Expert Reviewer",
			"assigned_coder",
			"assigned_expert",
			"status = 'Coder Review'",
			"status = 'Expert Review'",
		):
			self.assertIn(expected, actor_condition)
		self.assertIn("_medical_record_review_scope_permission(", assignment_permission)
		self.assertIn("assigned_coder", assignment_permission)
		self.assertIn("assigned_expert", assignment_permission)
		self.assertIn("_clinical_condition(", decision_query)
		self.assertIn("_combine_read_conditions(", decision_query)
		self.assertIn("reviewed_by", decision_query)
		self.assertIn("_medical_record_review_scope_permission(", decision_permission)
		self.assertIn("reviewed_by", decision_permission)
		self.assertIn("medical_record_review_assignment_query(", linked_query)
		self.assertIn("medical_record_review_assignment_permission(", linked_permission)
