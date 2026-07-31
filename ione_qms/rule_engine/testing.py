from __future__ import annotations

import hashlib
import hmac
import json
from functools import lru_cache
from typing import Any

import frappe
from frappe.utils import now_datetime

from ione_qms.ai.release import current_app_commit_sha
from ione_qms.rule_engine.executor import execute_rule_version
from ione_qms.rule_engine.models import RuleResult

MAX_TEST_INPUT_CHARS = 1024 * 1024
MAX_RULE_TEST_CASES = 1_000
RULE_TEST_EXECUTOR_CONTRACT = "IONE_RULE_EVALUATOR_V2"
RULE_TEST_SAMPLE_TYPES = frozenset(
	{
		"Positive",
		"Negative",
		"Exclusion",
		"Boundary",
		"Missing Data",
		"Real World",
		"Regression",
	}
)
RULE_TEST_MUTABLE_STATES = frozenset({"Draft", "Expert Review", "Test"})
RULE_TEST_RUN_ROLES = frozenset(
	{
		"IONE QC Administrator",
		"IONE QC Reviewer",
		"IONE Medical Affairs",
	}
)
_SAMPLE_EXPECTED_RESULTS = {
	"Positive": "Failed",
	"Negative": "Passed",
	"Exclusion": "Excluded",
	"Missing Data": "Insufficient Data",
}
_DEFINITION_FIELDS = (
	"rule_version",
	"case_name",
	"sample_type",
	"input_json",
	"expected_result",
)
_PROJECTION_FIELDS = (
	"actual_result",
	"test_status",
	"result_json",
	"last_run_at",
	"latest_execution_receipt",
)


def run_rule_test_case(test_case: str, *, save: bool = True) -> dict[str, Any]:
	"""Execute one pinned test and, when requested, append an immutable receipt."""
	initial = frappe.get_doc("IONE QC Rule Test Case", test_case)
	initial.check_permission("read")
	if save:
		initial.check_permission("write")
	with frappe.db.advisory_lock(f"ione-qms:rule-test:{initial.name}", timeout=10):
		initial_rule_version = str(initial.get("rule_version") or "")
		_lock_rule_versions({initial_rule_version})
		_lock_test_case(initial.name)
		doc = frappe.get_doc("IONE QC Rule Test Case", initial.name)
		if str(doc.get("rule_version") or "") != initial_rule_version:
			frappe.throw("Rule test lineage changed while acquiring its execution lock; retry the test")
		rule_version = frappe.get_doc("IONE QC Rule Version", doc.rule_version)
		if str(rule_version.get("status") or "") != "Test":
			frappe.throw("Governed rule tests may only execute while the rule version is in Test")
		if str(rule_version.get("lineage_status") or "") != "Verified":
			frappe.throw("Rule authority lineage must be Verified before governed tests execute")
		rule_checksum = str(rule_version.get("checksum") or "")
		if len(rule_checksum) != 64:
			frappe.throw("Rule version checksum is required before governed tests execute")
		context = _json_object(doc.get("input_json"), "input_json")
		expected = str(doc.get("expected_result") or "").strip()
		sample_type = str(doc.get("sample_type") or "").strip()
		definition_hash = rule_test_definition_hash(
			rule_version=doc.rule_version,
			case_name=doc.get("case_name"),
			sample_type=sample_type,
			input_json=context,
			expected_result=expected,
		)
		if not hmac.compare_digest(str(doc.get("definition_hash") or ""), definition_hash):
			frappe.throw("Rule test definition hash is stale; save the test definition before execution")
		evaluation = execute_rule_version(rule_version, context)
		actual = evaluation.result.value
		passed = expected == actual
		evaluation_payload = evaluation.as_dict()
		output_json = _canonical_json(evaluation_payload)
		result = {
			"test_case": doc.name,
			"rule_version": rule_version.name,
			"rule_checksum": rule_checksum,
			"definition_hash": definition_hash,
			"sample_type": sample_type,
			"expected_result": expected,
			"actual_result": actual,
			"test_status": "Passed" if passed else "Failed",
			"evaluation": evaluation_payload,
			"execution_receipt": None,
		}
		if not save:
			return result
		actor = _require_test_actor()
		executed_at = now_datetime()
		executor_version = _executor_version()
		receipt_values = {
			"doctype": "IONE QC Rule Test Execution",
			"receipt_key": _hash_payload(
				{
					"test_case": doc.name,
					"rule_version": rule_version.name,
					"rule_checksum": rule_checksum,
					"definition_hash": definition_hash,
					"output_hash": _hash_payload(evaluation_payload),
					"executor_version": executor_version,
					"executed_by": actor,
					"executed_at": executed_at,
					"nonce": frappe.generate_hash(length=32),
				}
			),
			"test_case": doc.name,
			"rule_version": rule_version.name,
			"rule_checksum": rule_checksum,
			"sample_type": sample_type,
			"definition_hash": definition_hash,
			"input_hash": _hash_payload(context),
			"expected_result": expected,
			"expected_hash": _hash_payload({"expected_result": expected}),
			"actual_result": actual,
			"output_hash": _hash_payload(evaluation_payload),
			"executor_version": executor_version,
			"lifecycle_state": "Test",
			"passed": int(passed),
			"executed_by": actor,
			"executed_at": executed_at,
		}
		receipt = frappe.get_doc(receipt_values)
		receipt.insert(ignore_permissions=True)
		frappe.db.set_value(
			"IONE QC Rule Test Case",
			doc.name,
			{
				"actual_result": actual,
				"test_status": result["test_status"],
				"last_run_at": executed_at,
				"result_json": output_json,
				"latest_execution_receipt": receipt.name,
			},
			update_modified=False,
		)
		result["execution_receipt"] = receipt.name
		return result


def run_rule_version_tests(rule_version: str) -> dict[str, Any]:
	version = frappe.get_doc("IONE QC Rule Version", rule_version)
	version.check_permission("read")
	names = frappe.get_list(
		"IONE QC Rule Test Case",
		filters={"rule_version": version.name},
		pluck="name",
		order_by="name asc",
		limit_page_length=MAX_RULE_TEST_CASES + 1,
	)
	if len(names) > MAX_RULE_TEST_CASES:
		frappe.throw(f"A rule version may have at most {MAX_RULE_TEST_CASES} governed test cases per run")
	results = [run_rule_test_case(name) for name in names]
	return {
		"rule_version": version.name,
		"total": len(results),
		"passed": sum(item["test_status"] == "Passed" for item in results),
		"failed": sum(item["test_status"] != "Passed" for item in results),
		"results": results,
	}


def validate_rule_test_case(doc, method: str | None = None) -> None:
	"""Canonicalize definitions, freeze them after Test, and invalidate stale results."""
	del method
	context = _json_object(doc.get("input_json"), "input_json")
	doc.input_json = _canonical_json(context)
	expected = str(doc.get("expected_result") or "").strip()
	if expected not in {item.value for item in RuleResult}:
		frappe.throw("Rule test expected_result is invalid")
	sample_type = str(doc.get("sample_type") or "").strip()
	if sample_type not in RULE_TEST_SAMPLE_TYPES:
		frappe.throw("Rule test sample_type must be one of: " + ", ".join(sorted(RULE_TEST_SAMPLE_TYPES)))
	previous = doc.get_doc_before_save()
	version_names = {
		str(source.get("rule_version") or "")
		for source in (previous, doc)
		if source is not None and source.get("rule_version")
	}
	_lock_rule_versions(version_names)
	for version_name in sorted(version_names):
		status = str(frappe.db.get_value("IONE QC Rule Version", version_name, "status") or "Draft")
		if status not in RULE_TEST_MUTABLE_STATES:
			frappe.throw(
				"Rule test cases are frozen after the rule version leaves Test; "
				f"{version_name} is in {status}"
			)
	definition_hash = rule_test_definition_hash(
		rule_version=doc.get("rule_version"),
		case_name=doc.get("case_name"),
		sample_type=sample_type,
		input_json=context,
		expected_result=expected,
	)
	definition_changed = previous is None or not hmac.compare_digest(
		str(previous.get("definition_hash") or ""),
		definition_hash,
	)
	doc.set("definition_hash", definition_hash)
	if previous is not None and not definition_changed:
		changed_projection = [
			fieldname for fieldname in _PROJECTION_FIELDS if previous.get(fieldname) != doc.get(fieldname)
		]
		if changed_projection:
			frappe.throw(
				"Rule test execution fields are server-managed. Changed: " + ", ".join(changed_projection)
			)
	if definition_changed:
		_reset_test_projection(doc)


def prevent_rule_test_case_delete(doc, method: str | None = None) -> None:
	"""Permit cleanup only before the governed version leaves Test."""
	del method
	version_name = str(doc.get("rule_version") or "")
	_lock_rule_versions({version_name})
	status = str(frappe.db.get_value("IONE QC Rule Version", version_name, "status") or "Draft")
	if status not in RULE_TEST_MUTABLE_STATES:
		frappe.throw("Rule test cases cannot be deleted after the rule version leaves Test")


def require_current_rule_test_receipts(rule_version) -> None:
	"""Fail closed unless every required sample has an exact current passing receipt."""
	for doctype in ("IONE QC Rule Test Case", "IONE QC Rule Test Execution"):
		if not frappe.db.exists("DocType", doctype):
			frappe.throw(f"{doctype} is unavailable")
	rule_checksum = str(rule_version.get("checksum") or "")
	if not _is_sha256(rule_checksum):
		frappe.throw("A current rule checksum is required for the publication test gate")
	current_executor_version = _executor_version()
	rows = frappe.get_all(
		"IONE QC Rule Test Case",
		filters={"rule_version": rule_version.name},
		fields=[
			"name",
			"case_name",
			"sample_type",
			"input_json",
			"expected_result",
			"definition_hash",
			"test_status",
			"latest_execution_receipt",
		],
		order_by="name asc",
		limit_page_length=MAX_RULE_TEST_CASES + 1,
	)
	if not rows:
		frappe.throw("A rule version cannot leave Test without governed test cases")
	if len(rows) > MAX_RULE_TEST_CASES:
		frappe.throw(f"A rule version may have at most {MAX_RULE_TEST_CASES} governed test cases")
	categories = {str(row.get("sample_type") or "") for row in rows}
	missing_categories = sorted(RULE_TEST_SAMPLE_TYPES.difference(categories))
	if missing_categories:
		frappe.throw("Rule test coverage is missing required sample types: " + ", ".join(missing_categories))
	for row in rows:
		required_expected = _SAMPLE_EXPECTED_RESULTS.get(str(row.get("sample_type") or ""))
		if required_expected and str(row.get("expected_result") or "") != required_expected:
			frappe.throw(f"{row.get('sample_type')} sample {row.name} must expect {required_expected}")
		if str(row.get("expected_result") or "") == RuleResult.ERROR.value:
			frappe.throw(f"Rule test case {row.name} cannot treat an execution Error as passing")
		expected_definition_hash = rule_test_definition_hash(
			rule_version=rule_version.name,
			case_name=row.get("case_name"),
			sample_type=row.get("sample_type"),
			input_json=_json_object(row.get("input_json"), "input_json"),
			expected_result=row.get("expected_result"),
		)
		if not hmac.compare_digest(
			str(row.get("definition_hash") or ""),
			expected_definition_hash,
		):
			frappe.throw(f"Rule test case {row.name} has a stale definition hash")
		receipt_name = str(row.get("latest_execution_receipt") or "")
		if not receipt_name or str(row.get("test_status") or "") != "Passed":
			frappe.throw(f"Rule test case {row.name} has no current passing execution receipt")
		receipt = frappe.db.get_value(
			"IONE QC Rule Test Execution",
			receipt_name,
			[
				"test_case",
				"rule_version",
				"rule_checksum",
				"sample_type",
				"definition_hash",
				"input_hash",
				"expected_result",
				"expected_hash",
				"actual_result",
				"output_hash",
				"executor_version",
				"lifecycle_state",
				"passed",
				"record_hash",
			],
			as_dict=True,
		)
		expected_pairs = {
			"test_case": row.name,
			"rule_version": rule_version.name,
			"rule_checksum": rule_checksum,
			"sample_type": str(row.get("sample_type") or ""),
			"definition_hash": expected_definition_hash,
			"input_hash": _hash_payload(_json_object(row.get("input_json"), "input_json")),
			"expected_result": str(row.get("expected_result") or ""),
			"expected_hash": _hash_payload({"expected_result": str(row.get("expected_result") or "")}),
			"actual_result": str(row.get("expected_result") or ""),
			"lifecycle_state": "Test",
		}
		if not receipt or any(
			str(receipt.get(fieldname) or "") != str(expected)
			for fieldname, expected in expected_pairs.items()
		):
			frappe.throw(f"Rule test case {row.name} receipt is not bound to its current definition")
		if (
			not int(receipt.get("passed") or 0)
			or not _is_sha256(str(receipt.get("output_hash") or ""))
			or str(receipt.get("executor_version") or "") != current_executor_version
			or not _is_sha256(str(receipt.get("record_hash") or ""))
		):
			frappe.throw(f"Rule test case {row.name} receipt failed integrity checks")
		if not _receipt_record_hash_is_valid(
			receipt_name,
			str(receipt.get("record_hash") or ""),
		):
			frappe.throw(f"Rule test case {row.name} receipt record hash is invalid")


def rule_test_definition_hash(
	*,
	rule_version: Any,
	case_name: Any,
	sample_type: Any,
	input_json: Any,
	expected_result: Any,
) -> str:
	return _hash_payload(
		{
			"contract_version": 1,
			"rule_version": str(rule_version or ""),
			"case_name": str(case_name or "").strip(),
			"sample_type": str(sample_type or "").strip(),
			"input": input_json,
			"expected_result": str(expected_result or "").strip(),
		}
	)


def _reset_test_projection(doc) -> None:
	doc.set("actual_result", None)
	doc.set("test_status", "Not Run")
	doc.set("result_json", None)
	doc.set("last_run_at", None)
	doc.set("latest_execution_receipt", None)


def _require_test_actor() -> str:
	user = str(frappe.session.user or "")
	if not user or user in {"Guest", "Administrator"}:
		frappe.throw(
			"Governed rule tests require a named accountable definition user",
			frappe.PermissionError,
		)
	roles = set(frappe.get_roles(user))
	if not roles.intersection(RULE_TEST_RUN_ROLES) or "IONE Agent Service" in roles:
		frappe.throw("Current user cannot attest governed rule tests", frappe.PermissionError)
	return user


@lru_cache(maxsize=1)
def _executor_version() -> str:
	return f"{RULE_TEST_EXECUTOR_CONTRACT}:{current_app_commit_sha()}"


def _lock_test_case(name: str) -> None:
	frappe.db.sql(
		"select name from `tabIONE QC Rule Test Case` where name = %s for update",
		(name,),
	)


def _lock_rule_versions(names: set[str]) -> None:
	for name in sorted(names - {""}):
		frappe.db.sql(
			"select name from `tabIONE QC Rule Version` where name = %s for update",
			(name,),
		)


def _json_object(value: Any, fieldname: str) -> dict[str, Any]:
	if isinstance(value, str) and len(value) > MAX_TEST_INPUT_CHARS:
		raise frappe.ValidationError(f"{fieldname} exceeds the 1 MiB limit")
	try:
		parsed = json.loads(value) if isinstance(value, str) else value
	except ValueError as exc:
		raise frappe.ValidationError(f"{fieldname} must be valid JSON") from exc
	if not isinstance(parsed, dict):
		raise frappe.ValidationError(f"{fieldname} must be a JSON object")
	return parsed


def _canonical_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)


def _hash_payload(value: Any) -> str:
	return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _is_sha256(value: str) -> bool:
	return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _receipt_record_hash_is_valid(receipt_name: str, stored_hash: str) -> bool:
	from ione_qms.services.immutability import canonical_record_hash

	receipt = frappe.get_doc("IONE QC Rule Test Execution", receipt_name)
	computed = canonical_record_hash(receipt)
	return _is_sha256(stored_hash) and hmac.compare_digest(stored_hash, computed)
