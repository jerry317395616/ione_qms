from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from typing import Any

import frappe
from frappe.utils import get_datetime, now_datetime

from ione_qms.rule_engine.executor import _build_context, _execute_shadow_rule_version
from ione_qms.rule_engine.models import RuleResult
from ione_qms.services.rule_receipts import (
	MAX_RULE_RECEIPT_CHAIN_ROWS,
	prepare_signed_receipt,
	verify_receipt_chain,
	verify_signed_receipt,
)

MAX_VALIDATION_EVENTS = 5_000
MAX_SHADOW_OBSERVATION_EXECUTIONS = 100_000
_RUN_TYPES = frozenset({"Historical Replay"})
_RUN_ROLES = frozenset({"IONE QC Reviewer", "IONE Medical Affairs"})
_ALLOWED_VERSION_STATES = {
	"Historical Replay": frozenset({"Historical Replay", "Shadow Run", "Approval", "Published"}),
}
_SHADOW_ASSESSMENTS = frozenset(
	{
		"Confirmed Finding",
		"Correct Pass",
		"Correct Exclusion",
		"False Positive",
		"False Negative",
		"Missing Data",
		"Rule Error",
		"Needs Review",
	}
)
_GOLD_LABELS = frozenset({"Finding", "No Finding", "Excluded", "Insufficient Data", "Needs Review"})
_ASSESSMENT_BY_RESULT_AND_GOLD = {
	"Passed": {
		"Finding": "False Negative",
		"No Finding": "Correct Pass",
		"Insufficient Data": "Missing Data",
		"Needs Review": "Needs Review",
	},
	"Failed": {
		"Finding": "Confirmed Finding",
		"No Finding": "False Positive",
		"Excluded": "False Positive",
		"Insufficient Data": "Missing Data",
		"Needs Review": "Needs Review",
	},
	"Excluded": {
		"Finding": "False Negative",
		"No Finding": "Correct Exclusion",
		"Excluded": "Correct Exclusion",
		"Insufficient Data": "Missing Data",
		"Needs Review": "Needs Review",
	},
	"Insufficient Data": {
		"Insufficient Data": "Missing Data",
		"Needs Review": "Needs Review",
	},
	"Error": {
		"Finding": "Rule Error",
		"No Finding": "Rule Error",
		"Excluded": "Rule Error",
		"Insufficient Data": "Rule Error",
		"Needs Review": "Needs Review",
	},
}


def queue_rule_validation(
	rule_version: str,
	run_type: str,
	window_start: str,
	window_end: str,
	sample_limit: int = 1_000,
) -> dict[str, str]:
	"""Queue a bounded historical replay without producing clinical findings."""
	version = frappe.get_doc("IONE QC Rule Version", rule_version)
	version.check_permission("write")
	_require_validation_actor(frappe.session.user)
	start, end, limit = _validate_request(version, run_type, window_start, window_end, sample_limit)
	request_key = _hash_payload(
		{
			"rule_version": version.name,
			"rule_checksum": version.checksum,
			"run_type": run_type,
			"window_start": start,
			"window_end": end,
			"sample_limit": limit,
			"requested_by": frappe.session.user,
		}
	)
	job_id = f"ione-qms:rule-validation:{request_key[:32]}"
	frappe.enqueue(
		"ione_qms.rule_engine.validation.run_rule_validation",
		queue="long",
		enqueue_after_commit=True,
		job_id=job_id,
		deduplicate=True,
		rule_version=version.name,
		run_type=run_type,
		window_start=str(start),
		window_end=str(end),
		sample_limit=limit,
		requested_by=frappe.session.user,
	)
	return {"job_id": job_id, "request_key": request_key}


def run_rule_validation(
	*,
	rule_version: str,
	run_type: str,
	window_start: str,
	window_end: str,
	sample_limit: int = 1_000,
	requested_by: str,
) -> dict[str, Any]:
	"""Execute a deterministic historical replay and persist one immutable artifact."""
	_require_validation_actor(requested_by)
	version = frappe.get_doc("IONE QC Rule Version", rule_version)
	start, end, limit = _validate_request(version, run_type, window_start, window_end, sample_limit)
	started_at = now_datetime()
	event_names = _validation_events(version, start, end, limit)
	executor_version = _rule_executor_version()
	counts = {result.value: 0 for result in RuleResult}
	outcome_digests: list[dict[str, str]] = []

	for event_name in event_names:
		event = frappe.get_doc("IONE Clinical Quality Event", event_name)
		context = _build_context(event)
		source_semantic_hash = _hash_payload(context)
		savepoint = f"ione_replay_{_hash_payload({'event': event_name})[:16]}"
		frappe.db.savepoint(savepoint)
		try:
			evaluation = _execute_shadow_rule_version(version, context)
		finally:
			frappe.db.rollback(save_point=savepoint)
			frappe.db.release_savepoint(savepoint)
		counts[evaluation.result.value] += 1
		outcome_digests.append(
			{
				"event": event_name,
				"event_hash": _hash_payload(
					{
						"event": event_name,
						"source_semantic_hash": source_semantic_hash,
					}
				),
				"source_semantic_hash": source_semantic_hash,
				"result": evaluation.result.value,
				"evaluation_hash": _hash_payload(evaluation.as_dict()),
			}
		)

	sample_hash = _hash_payload(
		[
			{
				"event": item["event"],
				"event_hash": item["event_hash"],
				"source_semantic_hash": item["source_semantic_hash"],
			}
			for item in outcome_digests
		]
	)
	result_hash = _hash_payload(outcome_digests)
	completed_at = now_datetime()
	status = "Completed" if event_names and counts[RuleResult.ERROR.value] == 0 else "Failed"
	summary = {
		"contract_version": 3,
		"rule_version": version.name,
		"rule_checksum": version.checksum,
		"executor_version": executor_version,
		"run_type": run_type,
		"window_start": str(start),
		"window_end": str(end),
		"sample_limit": limit,
		"event_count": len(event_names),
		"counts": counts,
		"sample_hash": sample_hash,
		"result_hash": result_hash,
	}
	run_key = _hash_payload(summary)
	values = {
		"doctype": "IONE QC Rule Validation Run",
		"run_key": run_key,
		"rule_version": version.name,
		"rule_checksum": version.checksum,
		"run_type": run_type,
		"status": status,
		"window_start": start,
		"window_end": end,
		"event_count": len(event_names),
		"passed_count": counts[RuleResult.PASSED.value],
		"failed_count": counts[RuleResult.FAILED.value],
		"excluded_count": counts[RuleResult.EXCLUDED.value],
		"insufficient_data_count": counts[RuleResult.INSUFFICIENT_DATA.value],
		"error_count": counts[RuleResult.ERROR.value],
		"sample_hash": sample_hash,
		"result_hash": result_hash,
		"outcome_manifest_json": _canonical_json(outcome_digests),
		"summary_json": _canonical_json(summary),
		"executor_version": executor_version,
		"requested_by": requested_by,
		"started_at": started_at,
		"completed_at": completed_at,
	}
	existing = frappe.db.get_value(
		"IONE QC Rule Validation Run",
		{"run_key": run_key},
		"name",
	)
	if existing:
		existing_rows = _append_only_receipt_rows(
			"IONE QC Rule Validation Run",
			{"name": existing},
			limit=1,
			order_by="name asc",
		)
		if not existing_rows:
			frappe.throw("Existing Rule validation receipt is unavailable")
		_verify_validation_run_receipt(
			existing_rows[0],
			version,
			run_type,
			require_completed=False,
		)
		return {"name": existing, "status": status, **summary}
	frappe.db.sql(
		"select name from `tabIONE QC Rule Version` where name = %s for update",
		(version.name,),
	)
	current_version = frappe.get_doc("IONE QC Rule Version", version.name)
	_validate_request(current_version, run_type, window_start, window_end, sample_limit)
	if (
		str(current_version.get("checksum") or "") != str(version.get("checksum") or "")
		or _rule_executor_version() != executor_version
	):
		frappe.throw("Rule or executor changed while the historical replay was running")
	prepare_signed_receipt("IONE QC Rule Validation Run", values)
	try:
		record = frappe.get_doc(values)
		record.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		existing = frappe.db.get_value(
			"IONE QC Rule Validation Run",
			{"run_key": run_key},
			"name",
		)
		if not existing:
			raise
		existing_rows = _append_only_receipt_rows(
			"IONE QC Rule Validation Run",
			{"name": existing},
			limit=1,
			order_by="name asc",
		)
		if not existing_rows:
			frappe.throw("Concurrent Rule validation receipt is unavailable")
		_verify_validation_run_receipt(
			existing_rows[0],
			current_version,
			run_type,
			require_completed=False,
		)
		return {"name": existing, "status": status, **summary}
	return {"name": record.name, "status": status, **summary}


def require_validation_artifact(rule_version, run_type: str) -> None:
	"""Fail closed unless the current immutable rule checksum has a real successful run."""
	if run_type == "Shadow Run":
		require_online_shadow_observation(rule_version)
		return
	if run_type not in _RUN_TYPES:
		frappe.throw("Unsupported rule validation run type")
	if not frappe.db.exists("DocType", "IONE QC Rule Validation Run"):
		frappe.throw("Rule validation-run DocType is unavailable")
	if not rule_version.get("checksum"):
		frappe.throw("Rule version checksum is required before validation")
	rows = _append_only_receipt_rows(
		"IONE QC Rule Validation Run",
		{
			"rule_version": rule_version.name,
			"rule_checksum": rule_version.checksum,
			"run_type": run_type,
		},
		limit=2,
		order_by="receipt_chain_sequence desc, name desc",
	)
	if not rows:
		frappe.throw(
			f"{run_type} requires a completed validation artifact for the current rule checksum "
			"with at least one event and zero evaluation errors."
		)
	if (
		str(rows[0].get("status") or "") != "Completed"
		or int(rows[0].get("event_count") or 0) < 1
		or int(rows[0].get("error_count") or 0) != 0
	):
		frappe.throw(f"The current signed {run_type} receipt is not a successful validation run")
	_verify_validation_run_receipt(rows[0], rule_version, run_type)


def require_online_shadow_observation(rule_version) -> dict[str, Any]:
	"""Require a completed online observation window and accountable feedback."""
	if str(rule_version.get("rule_type_snapshot") or "") != "Deterministic":
		frappe.throw("Online Shadow Run accepts only the built-in deterministic expression DSL")
	for doctype in ("IONE QC Rule Shadow Execution", "IONE QC Rule Shadow Feedback"):
		if not frappe.db.exists("DocType", doctype):
			frappe.throw(f"{doctype} is unavailable")
	rule_checksum = str(rule_version.get("checksum") or "")
	if not _is_sha256(rule_checksum):
		frappe.throw("Rule checksum is required for the online shadow gate")
	(
		start,
		end,
		minimum_events,
		minimum_feedback,
		maximum_false_positive_rate,
		maximum_false_negative_rate,
		minimum_feedback_coverage_rate,
		minimum_passed_feedback,
		minimum_excluded_feedback,
	) = validate_shadow_observation_contract(rule_version)
	if now_datetime() < end:
		frappe.throw("Online Shadow Run observation window has not completed")
	expected_executor_version = _rule_executor_version()
	execution_rows = _append_only_receipt_rows(
		"IONE QC Rule Shadow Execution",
		{
			"rule_version": rule_version.name,
			"rule_checksum": rule_checksum,
			"executor_version": expected_executor_version,
			"observed_at": ["between", [start, end]],
		},
		limit=MAX_SHADOW_OBSERVATION_EXECUTIONS + 1,
		order_by="name asc",
	)
	event_count = len(execution_rows)
	if event_count > MAX_SHADOW_OBSERVATION_EXECUTIONS:
		frappe.throw("Online Shadow Run exceeds the governed observation bound; split and review the window")
	if event_count < minimum_events:
		frappe.throw(f"Online Shadow Run requires at least {minimum_events} events; observed {event_count}")
	execution_names = {str(row.get("name") or "") for row in execution_rows}
	verify_receipt_chain(
		"IONE QC Rule Shadow Execution",
		rule_version=str(rule_version.name),
		rule_checksum=rule_checksum,
		expected_names=execution_names,
		max_rows=MAX_SHADOW_OBSERVATION_EXECUTIONS,
	)
	executions_by_name: dict[str, Any] = {}
	result_rows: dict[str, list[Any]] = {result.value: [] for result in RuleResult}
	for row in execution_rows:
		_verify_shadow_execution_receipt(row, rule_version, expected_executor_version, start, end)
		name = str(row.get("name") or "")
		if not name or name in executions_by_name:
			frappe.throw("Online Shadow Run contains duplicate or unnamed execution receipts")
		executions_by_name[name] = row
		result = str(row.get("result") or "")
		if result not in result_rows:
			frappe.throw("Online Shadow Run contains an invalid result")
		result_rows[result].append(row)
	if result_rows[RuleResult.ERROR.value]:
		frappe.throw("Online Shadow Run contains rule execution errors")
	if not result_rows[RuleResult.FAILED.value]:
		frappe.throw("Online Shadow Run must observe at least one rule-triggering Failed result")
	if result_rows[RuleResult.INSUFFICIENT_DATA.value]:
		frappe.throw(
			"Online Shadow Run contains Insufficient Data results; resolve the data contract before release"
		)
	feedback_rows = _append_only_receipt_rows(
		"IONE QC Rule Shadow Feedback",
		{
			"rule_version": rule_version.name,
			"rule_checksum": rule_checksum,
		},
		limit=MAX_SHADOW_OBSERVATION_EXECUTIONS + 1,
		order_by="name asc",
	)
	if len(feedback_rows) > MAX_SHADOW_OBSERVATION_EXECUTIONS:
		frappe.throw("Online Shadow Run feedback exceeds the governed observation bound")
	verify_receipt_chain(
		"IONE QC Rule Shadow Feedback",
		rule_version=str(rule_version.name),
		rule_checksum=rule_checksum,
		expected_names={str(row.get("name") or "") for row in feedback_rows},
		max_rows=MAX_SHADOW_OBSERVATION_EXECUTIONS,
	)
	feedback_by_execution: dict[str, Any] = {}
	counts = {assessment: 0 for assessment in _SHADOW_ASSESSMENTS}
	for row in feedback_rows:
		execution_name = str(row.get("shadow_execution") or "")
		execution = executions_by_name.get(execution_name)
		if execution is None:
			frappe.throw("Online Shadow Run feedback references an execution outside its frozen population")
		_verify_shadow_feedback_receipt(row, execution)
		if execution_name in feedback_by_execution:
			frappe.throw("Online Shadow Run contains duplicate feedback for one execution")
		feedback_by_execution[execution_name] = row
		counts[str(row.get("assessment") or "")] += 1
	reviewed_count = len(feedback_by_execution)
	if reviewed_count < minimum_feedback:
		frappe.throw(
			f"Online Shadow Run requires at least {minimum_feedback} reviewed executions; "
			f"observed {reviewed_count}"
		)
	coverage_rate = reviewed_count * 100.0 / event_count
	if coverage_rate < minimum_feedback_coverage_rate:
		frappe.throw(
			"Online Shadow Run feedback coverage "
			f"{coverage_rate:.2f}% is below {minimum_feedback_coverage_rate:.2f}%"
		)

	required_names = {
		str(row.get("name") or "")
		for result in (RuleResult.FAILED.value, RuleResult.PASSED.value, RuleResult.EXCLUDED.value)
		for row in result_rows[result]
	}
	for result, minimum in (
		(RuleResult.PASSED.value, minimum_passed_feedback),
		(RuleResult.EXCLUDED.value, minimum_excluded_feedback),
	):
		stratum = result_rows[result]
		if len(stratum) < minimum:
			frappe.throw(
				f"Online Shadow Run {result} stratum requires at least {minimum} executions; "
				f"observed {len(stratum)}"
			)
	missing_feedback = sorted(required_names.difference(feedback_by_execution))
	if missing_feedback:
		frappe.throw(
			"Online Shadow Run is missing full-population independent gold feedback "
			f"({len(missing_feedback)} executions, including every non-hit)"
		)
	if reviewed_count != event_count:
		frappe.throw("Online Shadow Run requires exactly one independent gold label for every execution")
	blocking = {
		assessment: counts[assessment]
		for assessment in ("Missing Data", "Rule Error", "Needs Review")
		if counts[assessment]
	}
	if blocking:
		frappe.throw(
			"Online Shadow Run has unresolved safety feedback: "
			+ ", ".join(f"{key}={value}" for key, value in sorted(blocking.items()))
		)
	finding_reviews = counts["Confirmed Finding"] + counts["False Positive"]
	if finding_reviews < 1:
		frappe.throw("Online Shadow Run requires at least one reviewed rule-triggering Failed result")
	false_positive_rate = (counts["False Positive"] * 100.0 / finding_reviews) if finding_reviews else 0.0
	if false_positive_rate > maximum_false_positive_rate:
		frappe.throw(
			"Online Shadow Run false-positive rate "
			f"{false_positive_rate:.2f}% exceeds {maximum_false_positive_rate:.2f}%"
		)
	gold_positive_reviews = counts["Confirmed Finding"] + counts["False Negative"]
	if gold_positive_reviews < 1:
		frappe.throw("Online Shadow Run requires at least one independent gold-label Finding")
	false_negative_rate = counts["False Negative"] * 100.0 / gold_positive_reviews
	if false_negative_rate > maximum_false_negative_rate:
		frappe.throw(
			"Online Shadow Run false-negative rate "
			f"{false_negative_rate:.2f}% exceeds {maximum_false_negative_rate:.2f}%"
		)
	return {
		"event_count": event_count,
		"feedback_count": reviewed_count,
		"feedback_coverage_rate": coverage_rate,
		"false_positive_rate": false_positive_rate,
		"false_negative_rate": false_negative_rate,
		"feedback_counts": counts,
		"window_start": start,
		"window_end": end,
	}


def validate_shadow_observation_contract(
	rule_version,
) -> tuple[datetime, datetime, int, int, float, float, float, int, int]:
	start_value = rule_version.get("shadow_observation_start")
	end_value = rule_version.get("shadow_observation_end")
	if not start_value or not end_value:
		frappe.throw("Shadow Run requires an explicit online observation start and end")
	start = get_datetime(start_value)
	end = get_datetime(end_value)
	if end <= start:
		frappe.throw("Shadow observation end must be later than its start")
	minimum_events = int(rule_version.get("shadow_min_events") or 0)
	minimum_feedback = int(rule_version.get("shadow_min_feedback") or 0)
	maximum_false_positive_rate = float(
		rule_version.get("shadow_max_false_positive_rate")
		if rule_version.get("shadow_max_false_positive_rate") not in (None, "")
		else -1
	)
	maximum_false_negative_rate = float(
		rule_version.get("shadow_max_false_negative_rate")
		if rule_version.get("shadow_max_false_negative_rate") not in (None, "")
		else -1
	)
	minimum_feedback_coverage_rate = float(
		rule_version.get("shadow_min_feedback_coverage_rate")
		if rule_version.get("shadow_min_feedback_coverage_rate") not in (None, "")
		else -1
	)
	minimum_passed_feedback = int(rule_version.get("shadow_min_passed_feedback") or 0)
	minimum_excluded_feedback = int(rule_version.get("shadow_min_excluded_feedback") or 0)
	if minimum_events < 1 or minimum_events > MAX_SHADOW_OBSERVATION_EXECUTIONS:
		frappe.throw(f"shadow_min_events must be between 1 and {MAX_SHADOW_OBSERVATION_EXECUTIONS}")
	if minimum_feedback < 1 or minimum_feedback > minimum_events:
		frappe.throw("shadow_min_feedback must be positive and cannot exceed shadow_min_events")
	if maximum_false_positive_rate < 0 or maximum_false_positive_rate > 100:
		frappe.throw("shadow_max_false_positive_rate must be between 0 and 100")
	if maximum_false_negative_rate < 0 or maximum_false_negative_rate > 100:
		frappe.throw("shadow_max_false_negative_rate must be between 0 and 100")
	if minimum_feedback_coverage_rate != 100:
		frappe.throw(
			"shadow_min_feedback_coverage_rate must be 100; false-negative governance "
			"requires full-population independent gold feedback"
		)
	for fieldname, value in (
		("shadow_min_passed_feedback", minimum_passed_feedback),
		("shadow_min_excluded_feedback", minimum_excluded_feedback),
	):
		if value < 1 or value > minimum_events:
			frappe.throw(f"{fieldname} must be positive and cannot exceed shadow_min_events")
	return (
		start,
		end,
		minimum_events,
		minimum_feedback,
		maximum_false_positive_rate,
		maximum_false_negative_rate,
		minimum_feedback_coverage_rate,
		minimum_passed_feedback,
		minimum_excluded_feedback,
	)


def validate_shadow_feedback(doc, method: str | None = None) -> None:
	"""Derive and lock one accountable feedback record for one shadow execution."""
	del method
	if doc.get_doc_before_save() is not None:
		return
	user = str(frappe.session.user or "")
	if not user or user in {"Guest", "Administrator"}:
		frappe.throw(
			"Shadow feedback requires a named accountable reviewer",
			frappe.PermissionError,
		)
	roles = set(frappe.get_roles(user))
	if not roles.intersection(_RUN_ROLES) or "IONE Agent Service" in roles:
		frappe.throw("Current user cannot attest shadow feedback", frappe.PermissionError)
	execution_name = str(doc.get("shadow_execution") or "")
	if not execution_name:
		frappe.throw("shadow_execution is required")
	preliminary_rule_version = str(
		frappe.db.get_value(
			"IONE QC Rule Shadow Execution",
			execution_name,
			"rule_version",
		)
		or ""
	)
	if not preliminary_rule_version:
		frappe.throw("Shadow execution receipt is unavailable")
	frappe.db.sql(
		"select name from `tabIONE QC Rule Version` where name = %s for update",
		(preliminary_rule_version,),
	)
	frappe.db.sql(
		"select name from `tabIONE QC Rule Shadow Execution` where name = %s for update",
		(execution_name,),
	)
	execution_rows = _append_only_receipt_rows(
		"IONE QC Rule Shadow Execution",
		{"name": execution_name},
		limit=1,
		order_by="name asc",
	)
	execution = execution_rows[0] if execution_rows else None
	if not execution:
		frappe.throw("Shadow execution receipt is unavailable or failed integrity validation")
	if str(execution.rule_version or "") != preliminary_rule_version:
		frappe.throw("Shadow execution lineage changed during feedback validation")
	version = frappe.get_doc("IONE QC Rule Version", execution.rule_version)
	if str(version.get("status") or "") != "Shadow Run":
		frappe.throw("Shadow feedback can only be recorded while the version is in Shadow Run")
	if str(version.get("lineage_status") or "") != "Verified":
		frappe.throw("Shadow feedback requires a lineage-verified rule version")
	if str(version.get("checksum") or "") != str(execution.rule_checksum or ""):
		frappe.throw("Shadow execution does not belong to the current rule checksum")
	if str(execution.get("executor_version") or "") != _rule_executor_version():
		frappe.throw("Shadow execution was produced by a stale evaluator commit")
	if user in {
		str(version.get("owner") or ""),
		str(version.get("approved_by") or ""),
	}:
		frappe.throw(
			"Shadow gold feedback must be independent from the rule-version author or approver",
			frappe.PermissionError,
		)
	start, end, *_limits = validate_shadow_observation_contract(version)
	_verify_shadow_execution_receipt(
		execution,
		version,
		_rule_executor_version(),
		start,
		end,
	)
	observed_at = get_datetime(execution.observed_at)
	if observed_at < start or observed_at > end:
		frappe.throw("Shadow execution falls outside the governed observation window")
	gold_label = str(doc.get("gold_label") or "").strip()
	if gold_label not in _GOLD_LABELS:
		frappe.throw("Shadow feedback requires an independent governed gold_label")
	assessment = _ASSESSMENT_BY_RESULT_AND_GOLD.get(
		str(execution.result or ""),
		{},
	).get(gold_label)
	if not assessment:
		frappe.throw(f"Gold label {gold_label} is invalid for {execution.result} shadow result")
	comment = str(doc.get("comment") or "").strip()
	if len(comment) < 5:
		frappe.throw("Shadow feedback requires a comment of at least five characters")
	reviewed_at = now_datetime()
	doc.set("assessment", assessment)
	doc.set("gold_label", gold_label)
	doc.set("comment", comment)
	doc.set("execution_feedback_key", execution_name)
	doc.set("rule_version", execution.rule_version)
	doc.set("rule_checksum", execution.rule_checksum)
	for fieldname in ("hospital", "campus", "department", "ward"):
		doc.set(fieldname, execution.get(fieldname))
	doc.set("reviewed_by", user)
	doc.set("reviewed_at", reviewed_at)
	doc.set(
		"feedback_key",
		_hash_payload(
			{
				"contract_version": 2,
				"shadow_execution": execution_name,
				"rule_version": execution.rule_version,
				"rule_checksum": execution.rule_checksum,
				"gold_label": gold_label,
				"assessment": assessment,
				"comment": comment,
				"reviewed_by": user,
				"reviewed_at": reviewed_at,
			}
		),
	)
	prepare_signed_receipt("IONE QC Rule Shadow Feedback", doc)


def _append_only_receipt_rows(
	doctype: str,
	filters: dict[str, Any],
	*,
	limit: int,
	order_by: str,
) -> list[Any]:
	meta = frappe.get_meta(doctype)
	fields = list(dict.fromkeys(meta.get_valid_columns()))
	if "name" not in fields:
		fields.insert(0, "name")
	return frappe.get_all(
		doctype,
		filters=filters,
		fields=fields,
		order_by=order_by,
		limit_page_length=limit,
	)


def _verify_canonical_receipt(doctype: str, row: Any) -> None:
	from ione_qms.services.immutability import canonical_record_hash_values

	stored = str(row.get("record_hash") or "")
	computed = canonical_record_hash_values(doctype, row)
	if not _is_sha256(stored) or not hmac.compare_digest(stored, computed):
		frappe.throw(f"{doctype} canonical receipt hash is invalid")


def _verify_validation_run_receipt(
	row: Any,
	rule_version,
	run_type: str,
	*,
	require_completed: bool = True,
) -> None:
	doctype = "IONE QC Rule Validation Run"
	_verify_canonical_receipt(doctype, row)
	verify_signed_receipt(doctype, row)
	try:
		summary = json.loads(str(row.get("summary_json") or ""))
		outcomes = json.loads(str(row.get("outcome_manifest_json") or ""))
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError("Rule validation receipt JSON is invalid") from exc
	summary_keys = {
		"contract_version",
		"rule_version",
		"rule_checksum",
		"executor_version",
		"run_type",
		"window_start",
		"window_end",
		"sample_limit",
		"event_count",
		"counts",
		"sample_hash",
		"result_hash",
	}
	if not isinstance(summary, dict) or set(summary) != summary_keys:
		frappe.throw("Rule validation summary contract is incomplete")
	if int(summary.get("contract_version") or 0) != 3:
		frappe.throw("Legacy Rule validation receipts are not release-authoritative")
	if not isinstance(outcomes, list):
		frappe.throw("Rule validation outcome manifest is missing")
	if len(outcomes) > MAX_VALIDATION_EVENTS:
		frappe.throw("Rule validation outcome manifest exceeds its governed bound")
	valid_results = {result.value for result in RuleResult}
	event_names: list[str] = []
	for item in outcomes:
		if (
			not isinstance(item, dict)
			or set(item)
			!= {
				"event",
				"event_hash",
				"source_semantic_hash",
				"result",
				"evaluation_hash",
			}
			or not str(item.get("event") or "")
			or not _is_sha256(str(item.get("event_hash") or ""))
			or not _is_sha256(str(item.get("source_semantic_hash") or ""))
			or not _is_sha256(str(item.get("evaluation_hash") or ""))
			or str(item.get("result") or "") not in valid_results
		):
			frappe.throw("Rule validation outcome manifest contains an invalid row")
		event_names.append(str(item["event"]))
	if len(event_names) != len(set(event_names)):
		frappe.throw("Rule validation outcome manifest contains duplicate source events")
	executor_version = _rule_executor_version()
	if (
		str(summary.get("executor_version") or "") != executor_version
		or str(row.get("executor_version") or "") != executor_version
	):
		frappe.throw("Rule validation receipt was produced by a stale executor")
	window_start = get_datetime(row.get("window_start"))
	window_end = get_datetime(row.get("window_end"))
	sample_limit = int(summary.get("sample_limit") or 0)
	if sample_limit < 1 or sample_limit > MAX_VALIDATION_EVENTS:
		frappe.throw("Rule validation sample limit is invalid")
	expected_events = _validation_events(rule_version, window_start, window_end, sample_limit)
	if event_names != expected_events:
		frappe.throw("Rule validation receipt does not match the current governed source population")
	counts = {result.value: 0 for result in RuleResult}
	for index, item in enumerate(outcomes):
		event = frappe.get_doc("IONE Clinical Quality Event", event_names[index])
		context = _build_context(event)
		source_semantic_hash = _hash_payload(context)
		expected_event_hash = _hash_payload(
			{
				"event": event_names[index],
				"source_semantic_hash": source_semantic_hash,
			}
		)
		if not hmac.compare_digest(
			str(item.get("source_semantic_hash") or ""),
			source_semantic_hash,
		) or not hmac.compare_digest(str(item.get("event_hash") or ""), expected_event_hash):
			frappe.throw("Rule validation source semantic snapshot changed after replay")
		savepoint = (
			f"ione_replay_verify_{index}_{_hash_payload({'receipt': str(row.get('name') or '')})[:12]}"
		)
		frappe.db.savepoint(savepoint)
		try:
			evaluation = _execute_shadow_rule_version(rule_version, context)
		finally:
			frappe.db.rollback(save_point=savepoint)
			frappe.db.release_savepoint(savepoint)
		evaluation_hash = _hash_payload(evaluation.as_dict())
		if str(item.get("result") or "") != evaluation.result.value or not hmac.compare_digest(
			str(item.get("evaluation_hash") or ""),
			evaluation_hash,
		):
			frappe.throw("Rule validation outcome does not reproduce under the current executor")
		counts[evaluation.result.value] += 1
	sample_hash = _hash_payload(
		[
			{
				"event": str(item["event"]),
				"event_hash": str(item["event_hash"]),
				"source_semantic_hash": str(item["source_semantic_hash"]),
			}
			for item in outcomes
		]
	)
	result_hash = _hash_payload(outcomes)
	expected_summary = {
		"contract_version": 3,
		"rule_version": str(rule_version.name),
		"rule_checksum": str(rule_version.checksum),
		"executor_version": executor_version,
		"run_type": run_type,
		"window_start": str(window_start),
		"window_end": str(window_end),
		"sample_limit": sample_limit,
		"event_count": len(outcomes),
		"counts": counts,
		"sample_hash": sample_hash,
		"result_hash": result_hash,
	}
	if (
		summary != expected_summary
		or str(row.get("summary_json") or "") != _canonical_json(expected_summary)
		or str(row.get("outcome_manifest_json") or "") != _canonical_json(outcomes)
	):
		frappe.throw("Rule validation summary does not match its row-level outcomes")
	row_counts = {
		RuleResult.PASSED.value: int(row.get("passed_count") or 0),
		RuleResult.FAILED.value: int(row.get("failed_count") or 0),
		RuleResult.EXCLUDED.value: int(row.get("excluded_count") or 0),
		RuleResult.INSUFFICIENT_DATA.value: int(row.get("insufficient_data_count") or 0),
		RuleResult.ERROR.value: int(row.get("error_count") or 0),
	}
	expected_status = "Completed" if event_names and counts[RuleResult.ERROR.value] == 0 else "Failed"
	if (
		int(row.get("event_count") or 0) != len(outcomes)
		or row_counts != counts
		or str(row.get("sample_hash") or "") != sample_hash
		or str(row.get("result_hash") or "") != result_hash
		or str(row.get("run_key") or "") != _hash_payload(expected_summary)
		or str(row.get("status") or "") != expected_status
	):
		frappe.throw("Rule validation receipt keys or counters are inconsistent")
	if require_completed and (
		expected_status != "Completed" or not event_names or counts[RuleResult.ERROR.value] != 0
	):
		frappe.throw("Rule validation receipt is not a successful release-authoritative replay")
	verify_receipt_chain(
		doctype,
		rule_version=str(rule_version.name),
		rule_checksum=str(rule_version.checksum),
		required_head=str(row.get("name") or ""),
		max_rows=MAX_RULE_RECEIPT_CHAIN_ROWS,
	)


def _verify_shadow_execution_receipt(
	row: Any,
	rule_version,
	expected_executor_version: str,
	window_start: datetime,
	window_end: datetime,
) -> None:
	doctype = "IONE QC Rule Shadow Execution"
	_verify_canonical_receipt(doctype, row)
	verify_signed_receipt(doctype, row)
	for fieldname in ("event_hash", "context_hash", "evaluation_hash"):
		if not _is_sha256(str(row.get(fieldname) or "")):
			frappe.throw(f"Shadow execution {fieldname} is invalid")
	if (
		str(row.get("rule_version") or "") != str(rule_version.name)
		or str(row.get("rule_checksum") or "") != str(rule_version.get("checksum") or "")
		or str(row.get("rule") or "") != str(rule_version.get("rule") or "")
		or str(row.get("executor_version") or "") != expected_executor_version
		or str(row.get("result") or "") not in {result.value for result in RuleResult}
	):
		frappe.throw("Shadow execution receipt is not bound to the current rule")
	observed_at = get_datetime(row.get("observed_at"))
	completed_at = get_datetime(row.get("completed_at"))
	if observed_at < window_start or observed_at > window_end or completed_at < observed_at:
		frappe.throw("Shadow execution receipt timestamps are invalid")
	if int(row.get("duration_ms") or 0) < 0:
		frappe.throw("Shadow execution duration is invalid")
	try:
		event = frappe.get_doc("IONE Clinical Quality Event", str(row.get("event") or ""))
	except frappe.DoesNotExistError:
		frappe.throw("Shadow execution source event is unavailable")
	event_hash = _hash_payload(
		{
			"event": event.name,
			"idempotency_key": event.get("idempotency_key"),
			"source_system": event.get("source_system"),
			"source_record_type": event.get("source_record_type"),
			"source_version": event.get("source_version"),
			"mapping_checksum": event.get("mapping_checksum"),
		}
	)
	if not hmac.compare_digest(str(row.get("event_hash") or ""), event_hash):
		frappe.throw("Shadow execution event hash is invalid")
	context = _build_context(event)
	context_hash = _hash_payload(context)
	if not hmac.compare_digest(str(row.get("context_hash") or ""), context_hash):
		frappe.throw("Shadow execution source semantic snapshot changed after observation")
	savepoint = (
		"ione_shadow_verify_"
		+ _hash_payload(
			{
				"receipt": str(row.get("name") or ""),
				"event": str(event.name),
			}
		)[:20]
	)
	frappe.db.savepoint(savepoint)
	try:
		evaluation = _execute_shadow_rule_version(rule_version, context)
	finally:
		frappe.db.rollback(save_point=savepoint)
		frappe.db.release_savepoint(savepoint)
	evaluation_hash = _hash_payload(evaluation.as_dict())
	if evaluation.result.value != str(row.get("result") or "") or not hmac.compare_digest(
		str(row.get("evaluation_hash") or ""),
		evaluation_hash,
	):
		frappe.throw("Shadow execution outcome does not reproduce under the current executor")
	execution_key = _hash_payload(
		{
			"contract_version": 1,
			"event_hash": event_hash,
			"rule_version": str(row.get("rule_version") or ""),
			"rule_checksum": str(row.get("rule_checksum") or ""),
			"context_hash": str(row.get("context_hash") or ""),
			"evaluation_hash": str(row.get("evaluation_hash") or ""),
			"executor_version": str(row.get("executor_version") or ""),
		}
	)
	if not hmac.compare_digest(str(row.get("execution_key") or ""), execution_key):
		frappe.throw("Shadow execution key is invalid")


def _verify_shadow_feedback_receipt(row: Any, execution: Any) -> None:
	doctype = "IONE QC Rule Shadow Feedback"
	_verify_canonical_receipt(doctype, row)
	verify_signed_receipt(doctype, row)
	gold_label = str(row.get("gold_label") or "")
	assessment = _ASSESSMENT_BY_RESULT_AND_GOLD.get(
		str(execution.get("result") or ""),
		{},
	).get(gold_label)
	if (
		gold_label not in _GOLD_LABELS
		or not assessment
		or str(row.get("assessment") or "") != assessment
		or str(row.get("shadow_execution") or "") != str(execution.get("name") or "")
		or str(row.get("execution_feedback_key") or "") != str(execution.get("name") or "")
		or str(row.get("rule_version") or "") != str(execution.get("rule_version") or "")
		or str(row.get("rule_checksum") or "") != str(execution.get("rule_checksum") or "")
		or not str(row.get("reviewed_by") or "")
		or str(row.get("reviewed_by") or "") in {"Guest", "Administrator"}
		or not row.get("reviewed_at")
		or len(str(row.get("comment") or "").strip()) < 5
	):
		frappe.throw("Shadow feedback receipt is not independently or canonically bound")
	for fieldname in ("hospital", "campus", "department", "ward"):
		if str(row.get(fieldname) or "") != str(execution.get(fieldname) or ""):
			frappe.throw("Shadow feedback scope does not match its execution")
	expected_key = _hash_payload(
		{
			"contract_version": 2,
			"shadow_execution": str(execution.get("name") or ""),
			"rule_version": str(execution.get("rule_version") or ""),
			"rule_checksum": str(execution.get("rule_checksum") or ""),
			"gold_label": gold_label,
			"assessment": assessment,
			"comment": str(row.get("comment") or "").strip(),
			"reviewed_by": str(row.get("reviewed_by") or ""),
			"reviewed_at": row.get("reviewed_at"),
		}
	)
	if not hmac.compare_digest(str(row.get("feedback_key") or ""), expected_key):
		frappe.throw("Shadow feedback key is invalid")


def _validation_events(version, start: datetime, end: datetime, limit: int) -> list[str]:
	filters: dict[str, Any] = {
		"event_time": ["between", [start, end]],
	}
	trigger_event = str(version.get("trigger_event") or "")
	if trigger_event and trigger_event != "*":
		filters["event_type"] = trigger_event
	return frappe.get_all(
		"IONE Clinical Quality Event",
		filters=filters,
		pluck="name",
		order_by="event_time asc, name asc",
		limit_page_length=limit,
	)


def _validate_request(
	version,
	run_type: str,
	window_start: str,
	window_end: str,
	sample_limit: int,
) -> tuple[datetime, datetime, int]:
	if run_type not in _RUN_TYPES:
		frappe.throw("run_type must be Historical Replay; Shadow Run is evaluated online from live events")
	if str(version.get("rule_type_snapshot") or "") != "Deterministic":
		frappe.throw(
			"Historical Replay accepts only the built-in deterministic expression DSL; "
			"Python plug-ins fail closed before enqueue or execution"
		)
	if version.get("status") not in _ALLOWED_VERSION_STATES[run_type]:
		frappe.throw(f"{run_type} cannot run while the rule version is in {version.get('status')} status")
	if str(version.get("lineage_status") or "") != "Verified":
		frappe.throw("Rule authority lineage must be Verified before governed validation")
	if not version.get("checksum"):
		frappe.throw("Rule version checksum is required before validation")
	start = get_datetime(window_start)
	end = get_datetime(window_end)
	if end < start:
		frappe.throw("window_end cannot be earlier than window_start")
	limit = int(sample_limit or 0)
	if limit < 1 or limit > MAX_VALIDATION_EVENTS:
		frappe.throw(f"sample_limit must be between 1 and {MAX_VALIDATION_EVENTS}")
	return start, end, limit


def _require_validation_actor(user: str) -> None:
	if user in {"", "Guest", "Administrator", None}:
		frappe.throw(
			"A named clinical validation requester is required; Guest and Administrator "
			"cannot attest a rule-validation run.",
			frappe.PermissionError,
		)
	roles = set(frappe.get_roles(user))
	if not roles.intersection(_RUN_ROLES):
		frappe.throw(
			"Historical replay and shadow validation require an IONE QC Reviewer "
			"or IONE Medical Affairs role.",
			frappe.PermissionError,
		)


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


def _rule_executor_version() -> str:
	from ione_qms.rule_engine.executor import _rule_executor_version as executor_version

	return executor_version()


def _is_sha256(value: str) -> bool:
	return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
