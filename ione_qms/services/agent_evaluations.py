from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import frappe
from frappe.utils import now_datetime

from ione_qms.ai.evaluation_contract import (
	EVALUATION_CATEGORIES,
	EVALUATION_CONTRACT_VERSION,
	EVALUATION_SYSTEM_PROMPT,
	SECURITY_ZERO_TOLERANCE_METRICS,
	EvaluationContractError,
	canonical_json,
	compute_governed_metrics,
	evaluate_thresholds,
	sha256_json,
	validate_case_contract,
	validate_thresholds,
)

MAX_EVALUATION_CASES = 100
THRESHOLD_POLICY_PARENT_KEY = "GLOBAL"
THRESHOLD_REVIEW_ROLES = frozenset({"IONE Agent Reviewer", "IONE QC Reviewer", "IONE Medical Affairs"})
_EVALUATION_BINDING_FIELDS = (
	"release_checksum",
	"test_suite_hash",
	"model_configuration_hash",
	"prompt_hash",
	"instructions_hash",
	"tool_schema_hash",
	"policy_checksum",
	"knowledge_base_manifest_hash",
	"threshold_policy",
	"threshold_policy_hash",
	"evaluator_code_hash",
)
_EVALUATION_RESULT_FIELDS = frozenset(
	{
		"evaluation_key",
		"agent",
		"policy",
		"agent_release",
		"flow_model",
		*_EVALUATION_BINDING_FIELDS,
		"category_matrix_hash",
		"result_hash",
		"receipt_hash",
		"status",
		"metrics_json",
		"sample_count",
		"passed_count",
		"failed_count",
		"evaluated_by",
		"evaluated_at",
		"requested_by",
	}
)
_EVALUATION_REVIEW_FIELDS = frozenset(
	{
		"review_status",
		"reviewed_by",
		"reviewed_at",
		"review_comment",
	}
)
_TEST_RESULT_FIELDS = frozenset({"status", "result_json", "last_run_at"})
_TEST_DEFINITION_FIELDS = (
	"flow_agent",
	"policy",
	"evaluation_category",
	"input_json",
	"expected_criteria_json",
)
_THRESHOLD_PROTECTED_FIELDS = (
	"threshold_policy_key",
	"version",
	"categories_json",
	"thresholds_json",
	"checksum",
	"approved_by",
	"approved_at",
	"approval_signature",
)
_LEGACY_POSITIVE_CRITERIA = ("must_contain", "expected_tool_names")
_LEGACY_SAFETY_CRITERIA = ("must_not_contain", "forbidden_tool_names")


def validate_agent_evaluation(doc, method: str | None = None) -> None:
	del method
	worker = bool(getattr(frappe.flags, "ione_evaluation_worker", False))
	reviewer = bool(getattr(frappe.flags, "ione_evaluation_review", False))
	previous = doc.get_doc_before_save()
	if previous is None:
		if not worker:
			frappe.throw(
				"Agent Evaluations may only be created by the governed evaluation worker.",
				frappe.PermissionError,
			)
		_validate_evaluation_result(doc)
		return
	changed_result = {
		fieldname for fieldname in _EVALUATION_RESULT_FIELDS if doc.get(fieldname) != previous.get(fieldname)
	}
	if changed_result:
		frappe.throw("Agent Evaluation result artifacts are immutable.")
	changed_review = {
		fieldname for fieldname in _EVALUATION_REVIEW_FIELDS if doc.get(fieldname) != previous.get(fieldname)
	}
	if changed_review and not reviewer:
		frappe.throw(
			"Agent Evaluation review fields may only change through the governed review API.",
			frappe.PermissionError,
		)
	if reviewer:
		_validate_review(doc, previous)


def validate_agent_test_case(doc, method: str | None = None) -> None:
	del method
	if not doc.get("policy") or not doc.get("flow_agent"):
		frappe.throw("Agent Test Case requires both policy and Flow Agent")
	if frappe.db.get_value("IONE Agent Policy", doc.get("policy"), "flow_agent") != doc.get("flow_agent"):
		frappe.throw("Agent Test Case policy does not govern its Flow Agent")
	input_value = _json_object(doc.get("input_json"), "input_json", required=True)
	criteria = _json_object(
		doc.get("expected_criteria_json"),
		"expected_criteria_json",
		required=True,
	)
	try:
		validate_case_contract(str(doc.get("evaluation_category") or ""), input_value, criteria)
	except EvaluationContractError as exc:
		frappe.throw(str(exc))
	_validate_criteria_tool_names(doc, criteria)
	doc.input_json = canonical_json(input_value)
	doc.expected_criteria_json = canonical_json(criteria)
	previous = doc.get_doc_before_save()
	if previous is None:
		if not getattr(frappe.flags, "ione_evaluation_worker", False):
			doc.status = "Not Run"
			doc.result_json = None
			doc.last_run_at = None
		return
	definition_changed = any(
		doc.get(fieldname) != previous.get(fieldname) for fieldname in _TEST_DEFINITION_FIELDS
	)
	if definition_changed and not getattr(frappe.flags, "ione_evaluation_worker", False):
		doc.status = "Not Run"
		doc.result_json = None
		doc.last_run_at = None
		return
	if any(doc.get(fieldname) != previous.get(fieldname) for fieldname in _TEST_RESULT_FIELDS):
		if not getattr(frappe.flags, "ione_evaluation_worker", False):
			frappe.throw(
				"Agent Test Case results may only be written by the evaluation worker.",
				frappe.PermissionError,
			)


def validate_evaluation_threshold_policy(doc, method: str | None = None) -> None:
	"""Version, independently approve, sign, and freeze the global evaluation thresholds."""
	del method
	status = str(doc.get("status") or "Draft")
	previous = doc.get_doc_before_save()
	if status not in {"Draft", "Approved", "Retired"}:
		frappe.throw("Evaluation Threshold Policy status is invalid")
	if not str(doc.get("version") or "").strip():
		frappe.throw("Evaluation Threshold Policy requires a version")
	categories = _json_array(doc.get("categories_json"), "categories_json")
	if len(categories) != len(EVALUATION_CATEGORIES) or set(categories) != set(EVALUATION_CATEGORIES):
		frappe.throw("Evaluation Threshold Policy must contain every governed category exactly once")
	thresholds = _json_object(doc.get("thresholds_json"), "thresholds_json", required=True)
	try:
		thresholds = validate_thresholds(thresholds)
	except EvaluationContractError as exc:
		frappe.throw(str(exc))
	doc.categories_json = canonical_json(sorted(categories))
	doc.thresholds_json = canonical_json(thresholds)
	expected_checksum = evaluation_threshold_policy_checksum(doc)

	if previous is None:
		if status != "Draft":
			frappe.throw("New Evaluation Threshold Policies must start in Draft")
		doc.active_parent_key = None
		doc.checksum = expected_checksum
		doc.approved_by = None
		doc.approved_at = None
		doc.approval_signature = None
		return

	previous_status = str(previous.get("status") or "Draft")
	if previous_status == "Draft" and status == "Draft":
		if "IONE Agent Administrator" not in frappe.get_roles(frappe.session.user):
			frappe.throw(
				"Only an IONE Agent Administrator may edit Draft evaluation thresholds",
				frappe.PermissionError,
			)
		doc.checksum = expected_checksum
		doc.active_parent_key = None
		return
	if previous_status == "Draft" and status == "Approved":
		for fieldname in (
			"threshold_policy_key",
			"version",
			"categories_json",
			"thresholds_json",
		):
			if previous.get(fieldname) != doc.get(fieldname):
				frappe.throw(
					"Threshold content must be saved as Draft before a different reviewer approves it"
				)
		if not hmac.compare_digest(
			str(previous.get("checksum") or ""),
			sha256_json(_threshold_policy_payload(previous)),
		):
			frappe.throw("Draft Evaluation Threshold Policy checksum is invalid")
		_assert_independent_threshold_reviewer(doc, previous)
		if frappe.db.exists(
			"IONE Agent Evaluation Threshold Policy",
			{"status": "Approved", "name": ["!=", doc.name]},
		):
			frappe.throw("Retire the current Approved Evaluation Threshold Policy first")
		doc.checksum = expected_checksum
		doc.approved_by = frappe.session.user
		doc.approved_at = now_datetime()
		doc.active_parent_key = THRESHOLD_POLICY_PARENT_KEY
		doc.approval_signature = _threshold_approval_signature(doc)
		return
	if previous_status == "Approved" and status == "Retired":
		_assert_threshold_reviewer()
		_assert_threshold_policy_integrity(previous)
		for fieldname in _THRESHOLD_PROTECTED_FIELDS:
			if previous.get(fieldname) != doc.get(fieldname):
				frappe.throw("Approved Evaluation Threshold Policy content is immutable")
		doc.active_parent_key = None
		return
	if previous_status == status and previous_status in {"Approved", "Retired"}:
		for fieldname in (*_THRESHOLD_PROTECTED_FIELDS, "status"):
			if previous.get(fieldname) != doc.get(fieldname):
				frappe.throw("Approved Evaluation Threshold Policy content is immutable")
		_assert_threshold_policy_integrity(doc, require_approved=previous_status == "Approved")
		return
	frappe.throw(f"Invalid Evaluation Threshold Policy transition: {previous_status} -> {status}")


def current_test_suite_hash(policy: str, agent_release: str) -> str:
	rows = frappe.get_all(
		"IONE Agent Test Case",
		filters={"policy": policy},
		fields=[
			"name",
			"flow_agent",
			"evaluation_category",
			"input_json",
			"expected_criteria_json",
		],
		order_by="name asc",
		limit_page_length=1_001,
	)
	return test_suite_hash_from_rows(rows, policy=policy, agent_release=agent_release)


def locked_test_suite(policy: str, flow_agent: str) -> list[Any]:
	names = frappe.get_all(
		"IONE Agent Test Case",
		filters={"policy": policy, "flow_agent": flow_agent},
		pluck="name",
		order_by="name asc",
		limit_page_length=MAX_EVALUATION_CASES + 1,
	)
	if not names:
		frappe.throw("Agent Evaluation requires a governed synthetic test suite")
	if len(names) > MAX_EVALUATION_CASES:
		frappe.throw(f"Agent Evaluation is limited to {MAX_EVALUATION_CASES} test cases")
	rows = [frappe.get_doc("IONE Agent Test Case", name, for_update=True) for name in names]
	validate_test_suite_coverage(rows)
	return rows


def test_suite_hash_from_rows(rows: list[Any], *, policy: str, agent_release: str) -> str:
	if not rows:
		frappe.throw("Agent Evaluation requires at least one synthetic Agent Test Case")
	if len(rows) > MAX_EVALUATION_CASES:
		frappe.throw(f"Agent Evaluation is limited to {MAX_EVALUATION_CASES} test cases")
	validate_test_suite_coverage(rows)
	payload = {
		"contract_version": EVALUATION_CONTRACT_VERSION,
		"policy": policy,
		"agent_release": agent_release,
		"cases": [
			{
				"name": _value(row, "name"),
				"flow_agent": _value(row, "flow_agent"),
				"evaluation_category": _value(row, "evaluation_category"),
				"input": _json_object(_value(row, "input_json"), "input_json", required=True),
				"criteria": _json_object(
					_value(row, "expected_criteria_json"),
					"expected_criteria_json",
					required=True,
				),
			}
			for row in rows
		],
	}
	return sha256_json(payload)


def validate_test_suite_coverage(rows: list[Any]) -> None:
	"""Require every typed category, expected-behavior assertion, and fail-closed assertion."""
	# Keep the old pure-unit helper behavior while legacy tests are migrated. Persisted
	# test cases cannot use this path because ``evaluation_category`` is required and
	# ``validate_agent_test_case`` accepts only the typed contract.
	if rows and all(not _value(row, "evaluation_category") for row in rows):
		_validate_legacy_suite_coverage(rows)
		return
	categories: list[str] = []
	for row in rows:
		category = str(_value(row, "evaluation_category") or "")
		input_value = _json_object(_value(row, "input_json"), "input_json", required=True)
		criteria = _json_object(
			_value(row, "expected_criteria_json"),
			"expected_criteria_json",
			required=True,
		)
		try:
			validate_case_contract(category, input_value, criteria)
		except EvaluationContractError as exc:
			frappe.throw(str(exc))
		categories.append(category)
	missing = sorted(set(EVALUATION_CATEGORIES).difference(categories))
	if missing:
		frappe.throw("Agent Evaluation suite is missing governed categories: " + ", ".join(missing))


def current_approved_threshold_policy(*, for_update: bool = False):
	if not frappe.db.exists("DocType", "IONE Agent Evaluation Threshold Policy"):
		frappe.throw("Approved Agent Evaluation Threshold Policy is required")
	names = frappe.get_all(
		"IONE Agent Evaluation Threshold Policy",
		filters={"status": "Approved"},
		pluck="name",
		order_by="approved_at desc, name desc",
		limit_page_length=2,
	)
	if len(names) != 1:
		frappe.throw("Exactly one Approved Agent Evaluation Threshold Policy is required")
	doc = frappe.get_doc(
		"IONE Agent Evaluation Threshold Policy",
		names[0],
		for_update=for_update,
	)
	_assert_threshold_policy_integrity(doc)
	return doc


def current_evaluation_binding(policy, release, *, threshold_policy=None) -> dict[str, str]:
	from ione_qms.ai.release import build_release_configuration

	agent = frappe.get_doc("Flow Agent", policy.flow_agent)
	model = frappe.get_doc("Flow Model", agent.model)
	configuration = build_release_configuration(policy, agent, model)
	require_confirmation_lifecycle_evaluation(configuration)
	configuration_json = canonical_json(configuration)
	if (
		str(release.get("configuration_json") or "") != configuration_json
		or str(release.get("checksum") or "") != hashlib.sha256(configuration_json.encode()).hexdigest()
	):
		frappe.throw("Agent Release configuration changed before or after evaluation")
	threshold_policy = threshold_policy or current_approved_threshold_policy()
	test_suite_hash = current_test_suite_hash(policy.name, release.name)
	return {
		"release_checksum": str(release.checksum),
		"test_suite_hash": test_suite_hash,
		"model_configuration_hash": sha256_json(
			{"model": configuration["model"], "provider": configuration["provider"]}
		),
		"prompt_hash": sha256_json(
			{
				"system_prompt": EVALUATION_SYSTEM_PROMPT,
				"test_suite_hash": test_suite_hash,
			}
		),
		"instructions_hash": hashlib.sha256(
			(f"{configuration['agent']['instructions'] or ''}\n\n{EVALUATION_SYSTEM_PROMPT}").encode()
		).hexdigest(),
		"tool_schema_hash": str(configuration["tool_schema_fingerprint"]),
		"policy_checksum": sha256_json(configuration["policy"]),
		"knowledge_base_manifest_hash": str(configuration["knowledge_base_manifest_hash"]),
		"threshold_policy": str(threshold_policy.name),
		"threshold_policy_hash": str(threshold_policy.checksum),
		"evaluator_code_hash": evaluator_code_hash(),
	}


def require_confirmation_lifecycle_evaluation(configuration: dict[str, Any]) -> None:
	"""Fail closed until a signed cross-transaction pause/review/resume harness exists."""
	tools = configuration.get("tools") if isinstance(configuration, dict) else None
	if not isinstance(tools, list):
		frappe.throw("Agent Release tool manifest is unavailable")
	confirmation_tools = sorted(
		{
			str(tool.get("slug") or "")
			for tool in tools
			if isinstance(tool, dict) and int(tool.get("requires_confirmation") or 0)
		}
		- {""}
	)
	if confirmation_tools:
		frappe.throw(
			"Agent Release is blocked because confirmation-tool production evaluation "
			"does not yet implement a signed, per-tool, cross-transaction pause -> "
			"independent named reviewer approve/reject -> resume receipt. Blocked tools: "
			+ ", ".join(confirmation_tools),
			frappe.PermissionError,
		)


def evaluator_code_hash() -> str:
	root = Path(__file__).resolve().parents[1]
	relative_paths = (
		"ai/evaluation.py",
		"ai/evaluation_contract.py",
		"ai/evidence.py",
		"ai/flow_gateway.py",
		"ai/governance.py",
		"ai/orchestrator.py",
		"ai/release.py",
		"ai/tool_registry.py",
		"ai/tools.py",
		"services/agent_evaluations.py",
		"services/ai_tasks.py",
	)
	digest = hashlib.sha256()
	for relative_path in relative_paths:
		path = root / relative_path
		if not path.is_file():
			frappe.throw(f"Governed evaluator source is missing: {relative_path}")
		digest.update(relative_path.encode())
		digest.update(b"\0")
		digest.update(path.read_bytes())
		digest.update(b"\0")
	return digest.hexdigest()


def evaluation_receipt_hash(values: dict[str, Any]) -> str:
	payload = {
		"contract_version": EVALUATION_CONTRACT_VERSION,
		"agent": values.get("agent"),
		"policy": values.get("policy"),
		"agent_release": values.get("agent_release"),
		"flow_model": values.get("flow_model"),
		**{fieldname: values.get(fieldname) for fieldname in _EVALUATION_BINDING_FIELDS},
		"category_matrix_hash": values.get("category_matrix_hash"),
		"result_hash": values.get("result_hash"),
		"status": values.get("status"),
		"sample_count": int(values.get("sample_count") or 0),
		"passed_count": int(values.get("passed_count") or 0),
		"failed_count": int(values.get("failed_count") or 0),
		"evaluated_by": values.get("evaluated_by"),
		"evaluated_at": values.get("evaluated_at"),
		"requested_by": values.get("requested_by"),
	}
	return sha256_json(payload)


def validate_evaluation_receipt_current(doc, policy=None, release=None) -> None:
	from ione_qms.ai.release import assert_approved_agent_release

	policy = policy or frappe.get_doc("IONE Agent Policy", doc.policy)
	release = release or assert_approved_agent_release(policy)
	if str(doc.get("agent_release") or "") != str(release.name):
		frappe.throw("Agent Evaluation no longer references the current Approved release")
	binding = current_evaluation_binding(policy, release)
	for fieldname, expected in binding.items():
		if not hmac.compare_digest(str(doc.get(fieldname) or ""), str(expected or "")):
			frappe.throw(f"Agent Evaluation receipt is stale: {fieldname}")
	if doc.get("status") != "Passed":
		frappe.throw("Production authorization requires a Passed Agent Evaluation")
	metrics = _json_object(doc.get("metrics_json"), "metrics_json", required=True)
	_assert_complete_metrics(metrics)
	_assert_evaluation_counts(doc, metrics)
	matrix_hash = sha256_json(metrics["category_confusion_matrix"])
	if not hmac.compare_digest(str(doc.get("category_matrix_hash") or ""), matrix_hash):
		frappe.throw("Agent Evaluation category matrix hash is invalid")
	result_hash = hashlib.sha256(canonical_json(metrics).encode()).hexdigest()
	if not hmac.compare_digest(str(doc.get("result_hash") or ""), result_hash):
		frappe.throw("Agent Evaluation result hash is invalid")
	threshold_policy = frappe.get_doc(
		"IONE Agent Evaluation Threshold Policy",
		doc.threshold_policy,
	)
	_assert_threshold_policy_integrity(threshold_policy)
	thresholds = _json_object(
		threshold_policy.thresholds_json,
		"thresholds_json",
		required=True,
	)
	checks = evaluate_thresholds(metrics, thresholds)
	if not all(checks.values()):
		frappe.throw("Agent Evaluation no longer satisfies every governed threshold")
	expected_receipt_hash = evaluation_receipt_hash(doc)
	if not hmac.compare_digest(str(doc.get("receipt_hash") or ""), expected_receipt_hash):
		frappe.throw("Agent Evaluation receipt hash is invalid")


def has_passing_agent_evaluation(policy, release=None) -> bool:
	"""Authorize production only from the current complete, approved governed receipt."""
	if not frappe.db.exists("DocType", "IONE Agent Evaluation"):
		return False
	if release is None:
		from ione_qms.ai.release import assert_approved_agent_release

		release = assert_approved_agent_release(policy)
	binding = current_evaluation_binding(policy, release)
	rows = frappe.get_all(
		"IONE Agent Evaluation",
		filters={
			"policy": policy.name,
			"agent": policy.flow_agent,
			"agent_release": release.name,
			**binding,
			"receipt_hash": ["is", "set"],
		},
		fields=["name", "status", "review_status"],
		order_by="evaluated_at desc, creation desc, name desc",
		limit_page_length=2,
	)
	if not rows:
		return False
	if rows[0].status != "Passed" or rows[0].review_status != "Approved":
		return False
	doc = frappe.get_doc("IONE Agent Evaluation", rows[0].name)
	validate_evaluation_receipt_current(doc, policy, release)
	return True


def _validate_evaluation_result(doc) -> None:
	if doc.get("status") not in {"Passed", "Failed"}:
		frappe.throw("Evaluation worker status must be Passed or Failed")
	sample_count = int(doc.get("sample_count") or 0)
	passed_count = int(doc.get("passed_count") or 0)
	failed_count = int(doc.get("failed_count") or 0)
	if sample_count < len(EVALUATION_CATEGORIES) or passed_count < 0 or failed_count < 0:
		frappe.throw("Agent Evaluation counts are invalid")
	if passed_count + failed_count != sample_count:
		frappe.throw("Agent Evaluation passed and failed counts must equal sample_count")
	if doc.get("status") == "Passed" and failed_count:
		frappe.throw("A Passed Agent Evaluation cannot contain failed cases")
	for fieldname in (
		"evaluation_key",
		"agent",
		"policy",
		"agent_release",
		"flow_model",
		*_EVALUATION_BINDING_FIELDS,
		"category_matrix_hash",
		"result_hash",
		"receipt_hash",
		"metrics_json",
		"evaluated_by",
		"evaluated_at",
		"requested_by",
	):
		if not doc.get(fieldname):
			frappe.throw(f"Agent Evaluation requires {fieldname}")
	release = frappe.get_doc("IONE Agent Release", doc.get("agent_release"))
	if release.get("status") != "Approved":
		frappe.throw("Agent Evaluation requires an Approved Agent Release")
	if str(release.get("checksum") or "") != str(doc.get("release_checksum") or ""):
		frappe.throw("Agent Evaluation release checksum does not match its Agent Release")
	metrics = _json_object(doc.get("metrics_json"), "metrics_json", required=True)
	_assert_complete_metrics(metrics)
	_assert_evaluation_counts(doc, metrics)
	threshold_policy = frappe.get_doc(
		"IONE Agent Evaluation Threshold Policy",
		doc.threshold_policy,
	)
	_assert_threshold_policy_integrity(threshold_policy)
	if not hmac.compare_digest(
		str(doc.get("threshold_policy_hash") or ""),
		str(threshold_policy.get("checksum") or ""),
	):
		frappe.throw("Agent Evaluation threshold policy hash is invalid")
	thresholds = _json_object(
		threshold_policy.thresholds_json,
		"thresholds_json",
		required=True,
	)
	threshold_checks = evaluate_thresholds(metrics, thresholds)
	if metrics.get("threshold_checks") != threshold_checks:
		frappe.throw("Agent Evaluation threshold checks do not match governed metrics")
	if not isinstance(metrics.get("threshold_passed"), bool) or metrics.get("threshold_passed") != all(
		threshold_checks.values()
	):
		frappe.throw("Agent Evaluation aggregate threshold result is invalid")
	if doc.get("status") == "Failed" and not failed_count and metrics.get("threshold_passed") is not False:
		frappe.throw("A Failed Agent Evaluation requires a failed case or aggregate threshold")
	if doc.get("status") == "Passed" and metrics.get("threshold_passed") is not True:
		frappe.throw("A Passed Agent Evaluation must satisfy every aggregate threshold")
	doc.metrics_json = canonical_json(metrics)
	if str(doc.get("result_hash") or "") != hashlib.sha256(doc.metrics_json.encode()).hexdigest():
		frappe.throw("Agent Evaluation result_hash does not match metrics_json")
	if str(doc.get("category_matrix_hash") or "") != sha256_json(metrics["category_confusion_matrix"]):
		frappe.throw("Agent Evaluation category_matrix_hash does not match metrics_json")
	if str(doc.get("receipt_hash") or "") != evaluation_receipt_hash(doc):
		frappe.throw("Agent Evaluation receipt_hash is invalid")
	doc.review_status = "Pending Review"
	doc.reviewed_by = None
	doc.reviewed_at = None
	doc.review_comment = None


def _assert_complete_metrics(metrics: dict[str, Any]) -> None:
	if int(metrics.get("contract_version") or 0) != EVALUATION_CONTRACT_VERSION:
		frappe.throw("Agent Evaluation metrics contract version is stale")
	counts = metrics.get("category_counts")
	matrix = metrics.get("category_confusion_matrix")
	if not isinstance(counts, dict) or set(counts) != set(EVALUATION_CATEGORIES):
		frappe.throw("Agent Evaluation category counts are incomplete")
	if any(int(counts.get(category) or 0) < 1 for category in EVALUATION_CATEGORIES):
		frappe.throw("Agent Evaluation must contain every governed category")
	if not isinstance(matrix, dict) or set(matrix) != set(EVALUATION_CATEGORIES):
		frappe.throw("Agent Evaluation category confusion matrix is incomplete")
	for category in EVALUATION_CATEGORIES:
		row = matrix.get(category)
		if not isinstance(row, dict) or set(row) != {"tp", "tn", "fp", "fn"}:
			frappe.throw("Agent Evaluation category confusion matrix is invalid")
		if any(int(value or 0) < 0 for value in row.values()):
			frappe.throw("Agent Evaluation confusion counts cannot be negative")
	for metric in SECURITY_ZERO_TOLERANCE_METRICS:
		if int(metrics.get(metric) or 0) != 0 and metrics.get("threshold_passed"):
			frappe.throw("A passing Agent Evaluation contains a zero-tolerance safety violation")
	case_results = metrics.get("case_results")
	if not isinstance(case_results, list):
		frappe.throw("Agent Evaluation metrics require sanitized case results")
	for result in case_results:
		if not isinstance(result, dict):
			frappe.throw("Agent Evaluation case results must be JSON objects")
		if result.get("passed") is True:
			prompt_hash = str(result.get("runtime_prompt_hash") or "")
			if len(prompt_hash) != 64 or any(
				character not in "0123456789abcdef" for character in prompt_hash
			):
				frappe.throw("Passing Agent Evaluation cases require an exact runtime prompt hash")
	try:
		recomputed = compute_governed_metrics(case_results)
	except EvaluationContractError as exc:
		frappe.throw(str(exc))
	for fieldname, expected in recomputed.items():
		if metrics.get(fieldname) != expected:
			frappe.throw(f"Agent Evaluation governed metric is inconsistent: {fieldname}")


def _assert_evaluation_counts(doc, metrics: dict[str, Any]) -> None:
	case_results = metrics["case_results"]
	sample_count = len(case_results)
	passed_count = sum(result.get("passed") is True for result in case_results)
	if (
		int(doc.get("sample_count") or 0) != sample_count
		or int(metrics.get("sample_count") or 0) != sample_count
		or int(doc.get("passed_count") or 0) != passed_count
		or int(doc.get("failed_count") or 0) != sample_count - passed_count
	):
		frappe.throw("Agent Evaluation document counts do not match its bound case results")


def _validate_review(doc, previous) -> None:
	if previous.get("review_status") != "Pending Review":
		frappe.throw("Agent Evaluation has already been reviewed")
	if doc.get("review_status") not in {"Approved", "Rejected"}:
		frappe.throw("Agent Evaluation review_status must be Approved or Rejected")
	if not doc.get("reviewed_by") or not doc.get("reviewed_at"):
		frappe.throw("Agent Evaluation review requires reviewer identity and timestamp")
	if str(doc.get("reviewed_by")) == str(doc.get("evaluated_by")):
		frappe.throw("Agent Evaluation cannot be self-approved")
	if str(doc.get("reviewed_by")) == str(doc.get("requested_by")):
		frappe.throw("The Agent Evaluation requester cannot approve the same result")
	if len(str(doc.get("review_comment") or "").strip()) < 5:
		frappe.throw("Agent Evaluation review requires a comment of at least five characters")
	if doc.get("review_status") == "Approved" and doc.get("status") != "Passed":
		frappe.throw("A failed Agent Evaluation cannot be approved")


def _validate_criteria_tool_names(doc, criteria: dict[str, Any]) -> None:
	from ione_qms.ai.tool_registry import IONE_FLOW_TOOL_BY_SLUG

	agent_tools = {
		str(row.tool) for row in frappe.get_doc("Flow Agent", doc.get("flow_agent")).get("tools") or []
	}
	for spec in (*criteria["allowed_tool_calls"], *criteria["expected_denials"]):
		name = str(spec["name"])
		if name not in agent_tools or name not in IONE_FLOW_TOOL_BY_SLUG:
			frappe.throw(f"Expected governed tool is not in the Agent and IONE registry: {name}")
	forbidden = set(criteria["forbidden_tools"])
	if not forbidden:
		return
	if forbidden.intersection(agent_tools):
		frappe.throw("forbidden_tools must not be configured on the governed Flow Agent")


def _assert_independent_threshold_reviewer(doc, previous) -> None:
	_assert_threshold_reviewer()
	user = str(frappe.session.user or "")
	if user in {
		str(doc.get("owner") or ""),
		str(previous.get("modified_by") or ""),
	}:
		frappe.throw("Evaluation Threshold Policy authors or last editors cannot approve their own policy")


def _assert_threshold_reviewer() -> None:
	user = str(frappe.session.user or "")
	if user in {"", "Guest", "Administrator"}:
		frappe.throw(
			"Evaluation Threshold Policy approval requires a named independent reviewer",
			frappe.PermissionError,
		)
	if not THRESHOLD_REVIEW_ROLES.intersection(frappe.get_roles(user)):
		frappe.throw(
			"Current user cannot approve Evaluation Threshold Policies",
			frappe.PermissionError,
		)


def _threshold_policy_payload(doc) -> dict[str, Any]:
	return {
		"contract_version": EVALUATION_CONTRACT_VERSION,
		"threshold_policy_key": doc.get("threshold_policy_key"),
		"author": doc.get("owner"),
		"version": str(doc.get("version") or "").strip(),
		"categories": _json_array(doc.get("categories_json"), "categories_json"),
		"thresholds": _json_object(doc.get("thresholds_json"), "thresholds_json", required=True),
	}


def evaluation_threshold_policy_checksum(doc) -> str:
	"""Return the canonical checksum, including the accountable policy author."""
	return sha256_json(_threshold_policy_payload(doc))


def _threshold_approval_signature(doc) -> str:
	signing_key = str(frappe.conf.get("encryption_key") or "")
	if len(signing_key) < 16:
		frappe.throw("Site encryption_key is required to sign Agent Evaluation Threshold Policies")
	payload = canonical_json(
		{
			"signature_version": "HMAC-SHA256-v1",
			"doctype": "IONE Agent Evaluation Threshold Policy",
			"threshold_policy_key": doc.get("threshold_policy_key"),
			"checksum": doc.get("checksum"),
			"approved_by": doc.get("approved_by"),
			"approved_at": doc.get("approved_at"),
		}
	)
	return hmac.new(
		signing_key.encode("utf-8"),
		payload.encode("utf-8"),
		hashlib.sha256,
	).hexdigest()


def _assert_threshold_policy_integrity(doc, *, require_approved: bool = True) -> None:
	if require_approved and str(doc.get("status") or "") != "Approved":
		frappe.throw("Agent Evaluation Threshold Policy is not Approved")
	if require_approved and str(doc.get("active_parent_key") or "") != THRESHOLD_POLICY_PARENT_KEY:
		frappe.throw("Agent Evaluation Threshold Policy singleton binding is invalid")
	if not require_approved and doc.get("active_parent_key"):
		frappe.throw("Retired Agent Evaluation Threshold Policy cannot remain active")
	expected_checksum = evaluation_threshold_policy_checksum(doc)
	if not hmac.compare_digest(str(doc.get("checksum") or ""), expected_checksum):
		frappe.throw("Agent Evaluation Threshold Policy checksum is invalid")
	if not doc.get("approved_by") or not doc.get("approved_at"):
		frappe.throw("Agent Evaluation Threshold Policy approval identity is incomplete")
	if not hmac.compare_digest(
		str(doc.get("approval_signature") or ""),
		_threshold_approval_signature(doc),
	):
		frappe.throw("Agent Evaluation Threshold Policy approval signature is invalid")


def _validate_legacy_suite_coverage(rows: list[Any]) -> None:
	has_positive = False
	has_safety = False
	for row in rows:
		criteria = _json_object(
			_value(row, "expected_criteria_json"),
			"expected_criteria_json",
			required=True,
		)
		_validate_criteria(criteria)
		has_positive = has_positive or any(criteria.get(fieldname) for fieldname in _LEGACY_POSITIVE_CRITERIA)
		has_safety = has_safety or any(criteria.get(fieldname) for fieldname in _LEGACY_SAFETY_CRITERIA)
	if not has_positive or not has_safety:
		frappe.throw(
			"Agent Evaluation suites require at least one expected-behavior assertion "
			"(must_contain or expected_tool_names) and one fail-closed assertion "
			"(must_not_contain or forbidden_tool_names)."
		)


def _validate_criteria(criteria: dict[str, Any]) -> None:
	"""Legacy pure-test helper; persisted cases use the typed v3 contract above."""
	allowed = {
		"must_contain",
		"must_not_contain",
		"expected_tool_names",
		"forbidden_tool_names",
		"max_output_chars",
	}
	unexpected = set(criteria).difference(allowed)
	if unexpected:
		frappe.throw("Unsupported Agent Test criteria: " + ", ".join(sorted(unexpected)))
	for fieldname in (
		"must_contain",
		"must_not_contain",
		"expected_tool_names",
		"forbidden_tool_names",
	):
		value = criteria.get(fieldname, [])
		if not isinstance(value, list) or any(
			not isinstance(item, str) or not item.strip() for item in value
		):
			frappe.throw(f"{fieldname} must be a list of non-empty strings")
	if not any(
		criteria.get(fieldname) for fieldname in (*_LEGACY_POSITIVE_CRITERIA, *_LEGACY_SAFETY_CRITERIA)
	):
		frappe.throw(
			"Agent Test Case criteria must contain at least one non-empty semantic "
			"assertion; max_output_chars alone is not an acceptance result."
		)
	if "max_output_chars" in criteria:
		limit = int(criteria["max_output_chars"])
		if limit < 1 or limit > 100_000:
			frappe.throw("max_output_chars must be between 1 and 100,000")


def _json_object(value: Any, fieldname: str, *, required: bool) -> dict[str, Any]:
	if value in (None, ""):
		if required:
			frappe.throw(f"{fieldname} is required")
		return {}
	try:
		parsed = json.loads(value) if isinstance(value, str) else value
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError(f"{fieldname} must be valid JSON") from exc
	if not isinstance(parsed, dict):
		frappe.throw(f"{fieldname} must be a JSON object")
	return parsed


def _json_array(value: Any, fieldname: str) -> list[Any]:
	try:
		parsed = json.loads(value) if isinstance(value, str) else value
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError(f"{fieldname} must be valid JSON") from exc
	if not isinstance(parsed, list):
		frappe.throw(f"{fieldname} must be a JSON array")
	return parsed


def _value(row: Any, fieldname: str) -> Any:
	if fieldname == "name" and hasattr(row, "name"):
		return row.name
	if hasattr(row, "get"):
		return row.get(fieldname)
	return getattr(row, fieldname, None)
