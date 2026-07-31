from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from contextlib import contextmanager
from datetime import date
from typing import Any

import frappe
from frappe.utils import add_days, getdate, now_datetime, nowdate

from ione_qms.permissions import require_role
from ione_qms.services.runtime_settings import require_post_migrate_runtime_ready

POLICY_DOCTYPE = "IONE Finding Recurrence Policy"
RUN_DOCTYPE = "IONE Finding Recurrence Run"
EVALUATION_DOCTYPE = "IONE Finding Recurrence Evaluation"
PDCA_DOCTYPE = "IONE PDCA Project"

GROUPING_MODE = "Rule and Approved Organizational Scope"
RECURRENCE_PROBLEM_PLACEHOLDER = (
	"A governed finding group met the hospital-approved recurrence threshold. "
	"Human review must define the clinical problem before approval."
)
RECURRENCE_GOAL_PLACEHOLDER = "Pending human definition and approval of a measurable improvement goal."
POLICY_STATUSES = frozenset({"Draft", "Approved", "Suspended", "Retired", "Rejected"})
TERMINAL_POLICY_STATUSES = frozenset({"Retired", "Rejected"})
ELIGIBLE_FINDING_STATUSES = frozenset(
	{
		"Confirmed",
		"Appeal Rejected",
		"Pending Rectification",
		"Rectifying",
		"Pending Department Approval",
		"Pending Functional Review",
		"Rework",
		"Closed",
	}
)

MAX_ACTIVE_POLICIES = 100
MAX_FINDINGS_PER_POLICY_RUN = 5_000
MAX_GROUPS_PER_POLICY_RUN = 500
MAX_SNAPSHOT_BYTES = 1_048_576
MAX_POLICY_WINDOW_DAYS = 3_660
MAX_POLICY_THRESHOLD = 1_000
MAX_COMMENT_LENGTH = 2_000
CONFIRMATION_QUERY_CHUNK = 500

_SCOPE_FIELDS = ("hospital", "campus", "department", "ward")
_AGGREGATION_FIELDS = {
	"Hospital": ("hospital",),
	"Campus": ("hospital", "campus"),
	"Department": ("hospital", "campus", "department"),
	"Ward": ("hospital", "campus", "department", "ward"),
}
_REQUIRED_LEVEL_FIELD = {
	"Hospital": "hospital",
	"Campus": "campus",
	"Department": "department",
	"Ward": "ward",
}
_POLICY_DEFINITION_FIELDS = (
	"policy_code",
	"policy_name",
	"hospital",
	"campus",
	"department",
	"ward",
	"aggregation_level",
	"grouping_mode",
	"threshold",
	"window_days",
	"effective_from",
	"effective_to",
)
_POLICY_AUDIT_FIELDS = (
	"requested_by",
	"requested_at",
	"request_comment",
	"request_checksum",
	"approved_by",
	"approved_at",
	"review_comment",
	"policy_checksum",
	"suspended_by",
	"suspended_at",
	"suspension_comment",
	"retired_by",
	"retired_at",
	"retirement_comment",
	"rejected_by",
	"rejected_at",
	"rejection_comment",
)
_POLICY_CODE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{1,63}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_POLICY_ACTION_FLAG = "ione_finding_recurrence_policy_action"
_AUDIT_CREATE_FLAG = "ione_finding_recurrence_audit_create"
_PDCA_CREATE_FLAG = "ione_finding_recurrence_pdca_create"


def prepare_recurrence_policy(doc, method: str | None = None) -> None:
	"""Force every new policy to remain an inert Draft until dual control approves it."""
	del method
	if not doc.is_new():
		return
	doc.status = "Draft"
	doc.enabled = 0
	for fieldname in _POLICY_AUDIT_FIELDS:
		doc.set(fieldname, None)


def validate_recurrence_policy(doc, method: str | None = None) -> None:
	"""Validate an explicit hospital policy and reject every ungoverned state mutation."""
	del method
	_normalize_policy_definition(doc)
	previous = doc.get_doc_before_save()
	status = str(doc.get("status") or "Draft")
	if status not in POLICY_STATUSES:
		frappe.throw("Finding recurrence policy status is invalid")
	doc.status = status
	doc.enabled = 1 if status == "Approved" else 0

	if previous is None:
		_require_named_policy_actor()
		if status != "Draft":
			frappe.throw("New finding recurrence policies must start in Draft status")
		if any(doc.get(fieldname) for fieldname in _POLICY_AUDIT_FIELDS):
			frappe.throw("New finding recurrence policies cannot contain approval audit fields")
		return

	_validate_policy_mutation_context(doc, previous)
	_validate_policy_audit_contract(doc)
	if status in {"Approved", "Suspended"}:
		expected = recurrence_policy_checksum(doc)
		if str(doc.get("policy_checksum") or "") != expected:
			frappe.throw("Approved finding recurrence policy checksum no longer matches its definition")
		_assert_no_overlapping_policy(doc)


def prevent_recurrence_record_deletion(doc, method: str | None = None) -> None:
	del doc, method
	frappe.throw(
		"Finding recurrence governance and evaluation records cannot be deleted", frappe.PermissionError
	)


def request_recurrence_policy_approval(policy: str, comment: str) -> dict[str, str]:
	_require_named_policy_actor()
	request_comment = _bounded_comment(comment, "Policy approval request comment")
	with frappe.db.advisory_lock(_policy_lock(policy), timeout=15):
		doc = frappe.get_doc(POLICY_DOCTYPE, policy, for_update=True)
		doc.check_permission("write")
		if doc.status != "Draft" or doc.get("requested_at"):
			frappe.throw("Only an unrequested Draft recurrence policy may be submitted")
		checksum = recurrence_policy_checksum(doc)
		with _controlled_policy_action(doc.name, "request"):
			doc.requested_by = frappe.session.user
			doc.requested_at = now_datetime()
			doc.request_comment = request_comment
			doc.request_checksum = checksum
			doc.save()
	return {"policy": doc.name, "status": doc.status, "request_checksum": checksum}


def review_recurrence_policy(
	policy: str,
	decision: str,
	review_comment: str,
) -> dict[str, str]:
	_require_named_policy_actor()
	if decision not in {"Approve", "Reject"}:
		frappe.throw("Policy review decision must be Approve or Reject")
	comment = _bounded_comment(review_comment, "Policy review comment")
	with frappe.db.advisory_lock(_policy_lock(policy), timeout=15):
		doc = frappe.get_doc(POLICY_DOCTYPE, policy, for_update=True)
		doc.check_permission("write")
		if doc.status != "Draft" or not doc.get("requested_at") or not doc.get("requested_by"):
			frappe.throw("Only a requested Draft recurrence policy may be reviewed")
		if doc.requested_by == frappe.session.user:
			frappe.throw("Recurrence policy requester and reviewer must be different named users")
		checksum = recurrence_policy_checksum(doc)
		if str(doc.get("request_checksum") or "") != checksum:
			frappe.throw("Recurrence policy definition changed after approval was requested")
		action = "approve" if decision == "Approve" else "reject"
		with _controlled_policy_action(doc.name, action):
			if decision == "Approve":
				doc.status = "Approved"
				doc.enabled = 1
				doc.approved_by = frappe.session.user
				doc.approved_at = now_datetime()
				doc.review_comment = comment
				doc.policy_checksum = checksum
			else:
				doc.status = "Rejected"
				doc.enabled = 0
				doc.rejected_by = frappe.session.user
				doc.rejected_at = now_datetime()
				doc.rejection_comment = comment
			doc.save()
	return {
		"policy": doc.name,
		"status": doc.status,
		"policy_checksum": str(doc.get("policy_checksum") or ""),
	}


def operate_recurrence_policy(
	policy: str,
	action: str,
	operation_comment: str,
) -> dict[str, str]:
	_require_named_policy_actor()
	if action not in {"Suspend", "Resume", "Retire"}:
		frappe.throw("Policy operation must be Suspend, Resume, or Retire")
	comment = _bounded_comment(operation_comment, "Policy operation comment")
	with frappe.db.advisory_lock(_policy_lock(policy), timeout=15):
		doc = frappe.get_doc(POLICY_DOCTYPE, policy, for_update=True)
		doc.check_permission("write")
		allowed = {
			("Approved", "Suspend"): ("Suspended", "suspend"),
			("Suspended", "Resume"): ("Approved", "resume"),
			("Approved", "Retire"): ("Retired", "retire"),
			("Suspended", "Retire"): ("Retired", "retire"),
		}
		target = allowed.get((str(doc.status), action))
		if not target:
			frappe.throw(f"Policy action {action} is not allowed from {doc.status}")
		target_status, context_action = target
		with _controlled_policy_action(doc.name, context_action):
			doc.status = target_status
			doc.enabled = 1 if target_status == "Approved" else 0
			if action == "Suspend":
				doc.suspended_by = frappe.session.user
				doc.suspended_at = now_datetime()
				doc.suspension_comment = comment
			elif action == "Retire":
				doc.retired_by = frappe.session.user
				doc.retired_at = now_datetime()
				doc.retirement_comment = comment
			doc.save()
	return {"policy": doc.name, "status": doc.status}


def dispatch_finding_recurrence_scans() -> dict[str, int]:
	"""Queue a bounded daily scan for every explicitly enabled Approved policy."""
	require_post_migrate_runtime_ready("dispatch finding recurrence scans")
	rows = frappe.get_all(
		POLICY_DOCTYPE,
		filters={"status": "Approved", "enabled": 1},
		fields=["name"],
		order_by="name asc",
		limit_page_length=MAX_ACTIVE_POLICIES + 1,
	)
	if len(rows) > MAX_ACTIVE_POLICIES:
		frappe.throw("Active finding recurrence policy count exceeds the scheduler hard limit")
	as_of = str(nowdate())
	for row in rows:
		policy = str(row["name"])
		frappe.enqueue(
			"ione_qms.services.finding_recurrence.run_finding_recurrence_scan",
			policy=policy,
			as_of=as_of,
			trigger_source="Scheduled",
			queue="long",
			enqueue_after_commit=True,
			job_name=f"ione-recurrence-{policy}-{as_of}",
		)
	return {"queued": len(rows)}


def run_finding_recurrence_scan(
	policy: str,
	as_of: str | date | None = None,
	trigger_source: str = "Manual",
) -> dict[str, Any]:
	"""Evaluate one approved policy without selecting patient identity or clinical narrative."""
	require_post_migrate_runtime_ready("run a finding recurrence scan")
	if trigger_source not in {"Manual", "Scheduled"}:
		frappe.throw("Finding recurrence trigger source is invalid")
	if trigger_source == "Manual":
		_require_named_policy_actor()
	evaluation_date = getdate(as_of or nowdate())
	if evaluation_date > getdate(nowdate()):
		frappe.throw("Finding recurrence scans cannot evaluate a future date")

	with frappe.db.advisory_lock(_policy_lock(policy), timeout=30):
		policy_doc = frappe.get_doc(POLICY_DOCTYPE, policy, for_update=True)
		if trigger_source == "Manual":
			policy_doc.check_permission("read")
		_assert_policy_is_runnable(policy_doc, evaluation_date)
		run_key = _hash_payload(
			{
				"policy": policy_doc.name,
				"policy_checksum": policy_doc.policy_checksum,
				"as_of": str(evaluation_date),
			}
		)
		existing = frappe.db.get_value(RUN_DOCTYPE, {"run_key": run_key}, "name")
		if existing:
			return _existing_run_response(str(existing))

		window_start = add_days(evaluation_date, -(int(policy_doc.window_days) - 1))
		findings = _load_bounded_eligible_findings(policy_doc, window_start, evaluation_date)
		confirmation_hashes = _load_human_confirmation_hashes(findings)
		groups = _group_findings(policy_doc, findings, confirmation_hashes)
		if len(groups) > MAX_GROUPS_PER_POLICY_RUN:
			frappe.throw("Finding recurrence group count exceeds the hard safety limit")

		savepoint = f"ione_recurrence_{run_key[:16]}"
		frappe.db.savepoint(savepoint)
		try:
			result = _persist_recurrence_run(
				policy_doc=policy_doc,
				run_key=run_key,
				evaluation_date=evaluation_date,
				window_start=window_start,
				trigger_source=trigger_source,
				groups=groups,
			)
		except frappe.DuplicateEntryError:
			frappe.db.rollback(save_point=savepoint)
			existing = frappe.db.get_value(RUN_DOCTYPE, {"run_key": run_key}, "name")
			if existing:
				return _existing_run_response(str(existing))
			raise
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			raise
		else:
			frappe.db.release_savepoint(savepoint)
			return result


def validate_recurrence_run(doc, method: str | None = None) -> None:
	del method
	_validate_controlled_append_only(doc, record_type="run", identity_field="run_key")
	snapshot = _exact_json_list(doc.get("snapshot_json"), "Recurrence run snapshot")
	_validate_recurrence_run_snapshot(doc, snapshot)


def validate_recurrence_evaluation(doc, method: str | None = None) -> None:
	del method
	_validate_controlled_append_only(doc, record_type="evaluation", identity_field="evaluation_key")
	links = _exact_json_list(doc.get("finding_links_json"), "Finding recurrence links")
	if len(links) != int(doc.get("occurrence_count") or 0):
		frappe.throw("Recurrence evaluation occurrence count does not match finding links")
	_validate_finding_links(links)
	finding_set_hash = _hash_payload(links)
	if finding_set_hash != str(doc.get("finding_set_hash") or ""):
		frappe.throw("Recurrence evaluation finding set hash is invalid")
	expected_outcome = (
		"Triggered"
		if int(doc.get("occurrence_count") or 0) >= int(doc.get("threshold") or 0)
		else "Below Threshold"
	)
	if str(doc.get("outcome") or "") != expected_outcome:
		frappe.throw("Recurrence evaluation outcome does not match the approved threshold")
	expected_hash = _evaluation_hash(doc, links)
	if str(doc.get("evaluation_hash") or "") != expected_hash:
		frappe.throw("Recurrence evaluation hash is invalid")
	_assert_serialized_size(links, "Recurrence evaluation finding links")


def recurrence_policy_checksum(doc) -> str:
	return _hash_payload(
		{fieldname: _canonical_value(doc.get(fieldname)) for fieldname in _POLICY_DEFINITION_FIELDS}
	)


def is_controlled_recurrence_pdca_proposal(doc) -> bool:
	"""Return True only for the narrow system-created Proposed recurrence project."""
	context = getattr(frappe.flags, _PDCA_CREATE_FLAG, None)
	if not isinstance(context, dict):
		return False
	evaluation_name = str(doc.get("recurrence_evaluation") or "")
	if str(context.get("evaluation") or "") != evaluation_name or str(context.get("case_key") or "") != str(
		doc.get("recurrence_case_key") or ""
	):
		return False
	evaluation = frappe.db.get_value(
		EVALUATION_DOCTYPE,
		evaluation_name,
		[
			"policy",
			"policy_checksum",
			"rule",
			"group_key",
			"finding_set_hash",
			"evaluation_hash",
			"outcome",
			"hospital",
			"campus",
			"department",
			"ward",
		],
		as_dict=True,
	)
	if not evaluation or evaluation.get("outcome") != "Triggered":
		return False
	expected_case_key = _case_key(str(evaluation.group_key), str(evaluation.finding_set_hash))
	expected = {
		"project_type": "Special Recurrence",
		"status": "Proposed",
		"recurrence_policy": evaluation.policy,
		"recurrence_policy_checksum": evaluation.policy_checksum,
		"recurrence_rule": evaluation.rule,
		"recurrence_group_key": evaluation.group_key,
		"recurrence_case_key": expected_case_key,
		"recurrence_snapshot_hash": evaluation.evaluation_hash,
		"hospital": evaluation.hospital,
		"campus": evaluation.campus,
		"department": evaluation.department,
		"ward": evaluation.ward,
	}
	return all(_same_optional(doc.get(key), value) for key, value in expected.items()) and not any(
		doc.get(fieldname) for fieldname in ("finding", "indicator", "patient", "encounter")
	)


def _persist_recurrence_run(
	*,
	policy_doc,
	run_key: str,
	evaluation_date: date,
	window_start: date,
	trigger_source: str,
	groups: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
	snapshot: list[dict[str, Any]] = []
	project_names: list[str] = []
	evaluations = 0
	for group_key, rows in sorted(groups.items()):
		evaluation, links = _create_group_evaluation(
			policy_doc=policy_doc,
			run_key=run_key,
			evaluation_date=evaluation_date,
			window_start=window_start,
			group_key=group_key,
			rows=rows,
			trigger_source=trigger_source,
		)
		evaluations += 1
		project = None
		if evaluation.outcome == "Triggered":
			project = _get_or_create_recurrence_project(evaluation)
			project_names.append(project)
		snapshot.append(
			{
				"evaluation": evaluation.name,
				"evaluation_key": evaluation.evaluation_key,
				"group_key": group_key,
				"rule": evaluation.rule,
				"occurrence_count": evaluation.occurrence_count,
				"threshold": evaluation.threshold,
				"outcome": evaluation.outcome,
				"finding_set_hash": _hash_payload(links),
				"project": project,
			}
		)
	_assert_serialized_size(snapshot, "Recurrence run snapshot")
	values = {
		"doctype": RUN_DOCTYPE,
		"run_key": run_key,
		"policy": policy_doc.name,
		"policy_checksum": policy_doc.policy_checksum,
		"as_of_date": evaluation_date,
		"window_start": window_start,
		"window_end": evaluation_date,
		**_scope_values(policy_doc),
		"trigger_source": trigger_source,
		"evaluated_by": frappe.session.user,
		"evaluated_at": now_datetime(),
		"group_count": len(snapshot),
		"finding_count": sum(int(row["occurrence_count"]) for row in snapshot),
		"triggered_group_count": sum(row["outcome"] == "Triggered" for row in snapshot),
		"project_count": len(set(project_names)),
		"snapshot_json": _canonical_json(snapshot),
		"snapshot_hash": _hash_payload(snapshot),
	}
	with _controlled_audit_create("run", run_key):
		run_doc = frappe.get_doc(values)
		run_doc.insert(ignore_permissions=True)
	return {
		"run": run_doc.name,
		"run_key": run_key,
		"evaluations": evaluations,
		"projects": sorted(set(project_names)),
		"finding_count": values["finding_count"],
	}


def _create_group_evaluation(
	*,
	policy_doc,
	run_key: str,
	evaluation_date: date,
	window_start: date,
	group_key: str,
	rows: list[dict[str, Any]],
	trigger_source: str,
):
	links = [
		{"finding": row["name"], "finding_hash": row["finding_hash"]}
		for row in sorted(rows, key=lambda value: (value["detected_at"], value["name"]))
	]
	_validate_finding_links(links)
	_assert_serialized_size(links, "Recurrence evaluation finding links")
	rule = str(rows[0]["rule"])
	evaluation_key = _hash_payload(
		{
			"run_key": run_key,
			"group_key": group_key,
			"finding_set_hash": _hash_payload(links),
		}
	)
	values = {
		"doctype": EVALUATION_DOCTYPE,
		"evaluation_key": evaluation_key,
		"run_key": run_key,
		"policy": policy_doc.name,
		"policy_checksum": policy_doc.policy_checksum,
		"as_of_date": evaluation_date,
		"window_start": window_start,
		"window_end": evaluation_date,
		**_scope_values(policy_doc),
		"grouping_mode": GROUPING_MODE,
		"group_key": group_key,
		"rule": rule,
		"threshold": int(policy_doc.threshold),
		"occurrence_count": len(rows),
		"outcome": "Triggered" if len(rows) >= int(policy_doc.threshold) else "Below Threshold",
		"finding_links_json": _canonical_json(links),
		"finding_set_hash": _hash_payload(links),
		"trigger_source": trigger_source,
		"evaluated_by": frappe.session.user,
		"evaluated_at": now_datetime(),
	}
	values["evaluation_hash"] = _evaluation_hash(values, links)
	with _controlled_audit_create("evaluation", evaluation_key):
		evaluation = frappe.get_doc(values)
		evaluation.insert(ignore_permissions=True)
	return evaluation, links


def _get_or_create_recurrence_project(evaluation) -> str:
	active = frappe.db.get_value(
		PDCA_DOCTYPE,
		{"active_recurrence_key": evaluation.group_key},
		"name",
	)
	if active:
		return str(active)
	case_key = _case_key(evaluation.group_key, evaluation.finding_set_hash)
	existing = frappe.db.get_value(
		PDCA_DOCTYPE,
		{"recurrence_case_key": case_key},
		"name",
	)
	if existing:
		return str(existing)
	values = {
		"doctype": PDCA_DOCTYPE,
		"project_code": f"PDCA-R-{case_key[:24].upper()}",
		"project_name": f"Special recurrence PDCA {case_key[:12].upper()}",
		"project_type": "Special Recurrence",
		"status": "Proposed",
		"problem_statement": RECURRENCE_PROBLEM_PLACEHOLDER,
		"goal": RECURRENCE_GOAL_PLACEHOLDER,
		**_scope_values(evaluation),
		"recurrence_rule": evaluation.rule,
		"recurrence_policy": evaluation.policy,
		"recurrence_evaluation": evaluation.name,
		"recurrence_policy_checksum": evaluation.policy_checksum,
		"recurrence_group_key": evaluation.group_key,
		"recurrence_case_key": case_key,
		"active_recurrence_key": evaluation.group_key,
		"recurrence_snapshot_hash": evaluation.evaluation_hash,
	}
	with _controlled_pdca_create(evaluation.name, case_key):
		try:
			project = frappe.get_doc(values)
			project.insert(ignore_permissions=True)
		except frappe.DuplicateEntryError:
			existing = frappe.db.get_value(
				PDCA_DOCTYPE,
				{"active_recurrence_key": evaluation.group_key},
				"name",
			) or frappe.db.get_value(
				PDCA_DOCTYPE,
				{"recurrence_case_key": case_key},
				"name",
			)
			if existing:
				return str(existing)
			raise
	return str(project.name)


def _load_bounded_eligible_findings(policy_doc, window_start: date, window_end: date):
	filters: list[list[Any]] = [
		["status", "in", sorted(ELIGIBLE_FINDING_STATUSES)],
		["detected_at", ">=", f"{window_start} 00:00:00"],
		["detected_at", "<", f"{add_days(window_end, 1)} 00:00:00"],
		["hospital", "=", policy_doc.hospital],
	]
	for fieldname in _AGGREGATION_FIELDS[str(policy_doc.aggregation_level)][1:]:
		value = policy_doc.get(fieldname)
		if value:
			filters.append([fieldname, "=", value])
	rows = frappe.get_all(
		"IONE QC Finding",
		filters=filters,
		fields=[
			"name",
			"rule",
			"hospital",
			"campus",
			"department",
			"ward",
			"detected_at",
			"deduplication_key",
			"evidence_hash",
		],
		order_by="detected_at asc, name asc",
		limit_page_length=MAX_FINDINGS_PER_POLICY_RUN + 1,
	)
	if len(rows) > MAX_FINDINGS_PER_POLICY_RUN:
		frappe.throw("Eligible finding count exceeds the recurrence scan hard limit")
	for row in rows:
		if not row.get("rule"):
			frappe.throw("A recurrence policy cannot evaluate an eligible finding without a governed rule")
		if not _row_matches_approved_scope(row, policy_doc):
			frappe.throw("An eligible finding escaped the approved recurrence policy scope")
	return rows


def _load_human_confirmation_hashes(findings) -> dict[str, str]:
	names = [str(row["name"]) for row in findings]
	confirmation_hashes: dict[str, str] = {}
	for offset in range(0, len(names), CONFIRMATION_QUERY_CHUNK):
		chunk = names[offset : offset + CONFIRMATION_QUERY_CHUNK]
		rows = frappe.get_all(
			"IONE QC Finding Evidence",
			filters={
				"finding": ["in", chunk],
				"evidence_type": "Workflow Transition",
				"field_path": "status",
				"evidence_json": ["like", '%"to_status":"Confirmed"%'],
			},
			fields=["finding", "evidence_json", "evidence_hash"],
			order_by="finding asc, creation asc",
			limit_page_length=len(chunk) + 1,
		)
		if len(rows) > len(chunk):
			frappe.throw("Human confirmation evidence is ambiguous or exceeds its hard limit")
		for row in rows:
			payload = _exact_json_object(row.get("evidence_json"), "Finding confirmation evidence")
			if (
				set(payload)
				!= {
					"finding",
					"from_status",
					"to_status",
					"reviewed_by",
					"reviewed_at",
					"rule_version",
					"standard_clause",
				}
				or payload.get("finding") != row.get("finding")
				or payload.get("from_status") != "Pending QC Review"
				or payload.get("to_status") != "Confirmed"
				or not payload.get("reviewed_by")
				or payload.get("reviewed_by") == "Administrator"
				or not payload.get("reviewed_at")
			):
				frappe.throw("Finding confirmation evidence is not an exact named-human approval receipt")
			name = str(row["finding"])
			if name in confirmation_hashes:
				frappe.throw("A finding has more than one confirmation receipt")
			evidence_hash = str(row.get("evidence_hash") or "")
			if not _HEX64.fullmatch(evidence_hash):
				frappe.throw("Finding confirmation evidence hash is missing or invalid")
			confirmation_hashes[name] = evidence_hash
	missing = sorted(set(names).difference(confirmation_hashes))
	if missing:
		frappe.throw("Eligible findings are missing immutable named-human confirmation evidence")
	return confirmation_hashes


def _group_findings(policy_doc, findings, confirmation_hashes: dict[str, str]):
	groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
	for source in findings:
		scope = {
			fieldname: str(source.get(fieldname) or "")
			for fieldname in _AGGREGATION_FIELDS[str(policy_doc.aggregation_level)]
		}
		group_key = _hash_payload(
			{
				"grouping_mode": GROUPING_MODE,
				"rule": str(source["rule"]),
				"scope": scope,
			}
		)
		row = dict(source)
		row["finding_hash"] = _hash_payload(
			{
				"finding": str(source["name"]),
				"rule": str(source["rule"]),
				"scope": scope,
				"detected_at": str(source["detected_at"]),
				"deduplication_key_hash": _hash_text(str(source.get("deduplication_key") or "")),
				"finding_evidence_hash": str(source.get("evidence_hash") or ""),
				"confirmation_evidence_hash": confirmation_hashes[str(source["name"])],
			}
		)
		groups[group_key].append(row)
	return dict(groups)


def _validate_policy_mutation_context(doc, previous) -> None:
	previous_status = str(previous.get("status") or "")
	if previous_status in TERMINAL_POLICY_STATUSES:
		frappe.throw("Terminal finding recurrence policies are immutable")

	definition_changed = any(
		_canonical_value(doc.get(fieldname)) != _canonical_value(previous.get(fieldname))
		for fieldname in _POLICY_DEFINITION_FIELDS
	)
	if definition_changed:
		_require_named_policy_actor()
	if definition_changed and (previous_status != "Draft" or previous.get("requested_at")):
		frappe.throw("Requested or reviewed recurrence policy definitions are immutable")

	changed_governance = {
		fieldname
		for fieldname in ("status", "enabled", *_POLICY_AUDIT_FIELDS)
		if _canonical_value(doc.get(fieldname)) != _canonical_value(previous.get(fieldname))
	}
	if not changed_governance:
		return
	context = getattr(frappe.flags, _POLICY_ACTION_FLAG, None)
	if not isinstance(context, dict) or str(context.get("policy") or "") != str(doc.name or ""):
		frappe.throw("Recurrence policy governance fields may only change through the POST-only API")
	action = str(context.get("action") or "")
	allowed_fields = {
		"request": {"requested_by", "requested_at", "request_comment", "request_checksum"},
		"approve": {"status", "enabled", "approved_by", "approved_at", "review_comment", "policy_checksum"},
		"reject": {"status", "enabled", "rejected_by", "rejected_at", "rejection_comment"},
		"suspend": {"status", "enabled", "suspended_by", "suspended_at", "suspension_comment"},
		"resume": {"status", "enabled"},
		"retire": {"status", "enabled", "retired_by", "retired_at", "retirement_comment"},
	}.get(action, set())
	if not changed_governance.issubset(allowed_fields):
		frappe.throw("Recurrence policy operation attempted to change unauthorized governance fields")


def _validate_policy_audit_contract(doc) -> None:
	status = str(doc.status)
	checksum = recurrence_policy_checksum(doc)
	if doc.get("requested_at"):
		if (
			not doc.get("requested_by")
			or not doc.get("request_comment")
			or str(doc.get("request_checksum") or "") != checksum
		):
			frappe.throw("Requested recurrence policy audit receipt is incomplete")
	if status in {"Approved", "Suspended", "Retired"}:
		if (
			not doc.get("approved_by")
			or not doc.get("approved_at")
			or not doc.get("review_comment")
			or str(doc.get("policy_checksum") or "") != checksum
			or doc.get("approved_by") == doc.get("requested_by")
		):
			frappe.throw("Approved recurrence policy requires a separated immutable approval receipt")
	if status == "Suspended" and (
		not doc.get("suspended_by") or not doc.get("suspended_at") or not doc.get("suspension_comment")
	):
		frappe.throw("Suspended recurrence policy requires a complete operation receipt")
	if status == "Retired" and (
		not doc.get("retired_by") or not doc.get("retired_at") or not doc.get("retirement_comment")
	):
		frappe.throw("Retired recurrence policy requires a complete operation receipt")
	if status == "Rejected" and (
		not doc.get("rejected_by")
		or not doc.get("rejected_at")
		or not doc.get("rejection_comment")
		or doc.get("rejected_by") == doc.get("requested_by")
	):
		frappe.throw("Rejected recurrence policy requires a separated review receipt")


def _normalize_policy_definition(doc) -> None:
	code = str(doc.get("policy_code") or "")
	if code != code.strip() or not _POLICY_CODE.fullmatch(code):
		frappe.throw("Policy code must be 2-64 uppercase letters, digits, dots, underscores, or hyphens")
	name = str(doc.get("policy_name") or "").strip()
	if not name or len(name) > 140:
		frappe.throw("Policy name is required and cannot exceed 140 characters")
	doc.policy_name = name
	if str(doc.get("grouping_mode") or "") != GROUPING_MODE:
		frappe.throw("Recurrence grouping must be Rule and Approved Organizational Scope")
	level = str(doc.get("aggregation_level") or "")
	if level not in _AGGREGATION_FIELDS:
		frappe.throw("Recurrence policy aggregation level is invalid")
	if not doc.get("hospital") or not doc.get(_REQUIRED_LEVEL_FIELD[level]):
		frappe.throw("Recurrence policy requires the complete selected organizational scope")
	level_fields = _AGGREGATION_FIELDS[level]
	for fieldname in _SCOPE_FIELDS:
		if fieldname not in level_fields and doc.get(fieldname):
			frappe.throw(f"{fieldname} must be empty below the selected aggregation level")
	doc.threshold = _bounded_exact_int(doc.get("threshold"), "Recurrence threshold", 2, MAX_POLICY_THRESHOLD)
	doc.window_days = _bounded_exact_int(
		doc.get("window_days"),
		"Recurrence window days",
		1,
		MAX_POLICY_WINDOW_DAYS,
	)
	if not doc.get("effective_from"):
		frappe.throw("Recurrence policy effective_from is required")
	start = getdate(doc.effective_from)
	end = getdate(doc.effective_to) if doc.get("effective_to") else None
	if end and end < start:
		frappe.throw("Recurrence policy effective_to cannot precede effective_from")
	doc.effective_from = start
	doc.effective_to = end


def _assert_no_overlapping_policy(doc) -> None:
	rows = frappe.get_all(
		POLICY_DOCTYPE,
		filters={
			"hospital": doc.hospital,
			"aggregation_level": doc.aggregation_level,
			"status": ["in", ["Approved", "Suspended"]],
			"name": ["!=", doc.name],
		},
		fields=["name", "campus", "department", "ward", "effective_from", "effective_to"],
		order_by="name asc",
		limit_page_length=101,
	)
	if len(rows) > 100:
		frappe.throw("Comparable recurrence policies exceed the overlap-check hard limit")
	current_scope = tuple(str(doc.get(fieldname) or "") for fieldname in _SCOPE_FIELDS)
	current_start = getdate(doc.effective_from)
	current_end = getdate(doc.effective_to) if doc.get("effective_to") else date.max
	for row in rows:
		row_scope = tuple(str(row.get(fieldname) or "") for fieldname in _SCOPE_FIELDS)
		if row_scope != current_scope:
			continue
		row_start = getdate(row["effective_from"])
		row_end = getdate(row["effective_to"]) if row.get("effective_to") else date.max
		if current_start <= row_end and row_start <= current_end:
			frappe.throw(f"Recurrence policy effective period overlaps governed policy {row['name']}")


def _assert_policy_is_runnable(doc, evaluation_date: date) -> None:
	if doc.status != "Approved" or int(doc.enabled or 0) != 1:
		frappe.throw("Only an enabled Approved recurrence policy may be evaluated")
	_normalize_policy_definition(doc)
	if recurrence_policy_checksum(doc) != str(doc.policy_checksum or ""):
		frappe.throw("Recurrence policy checksum drifted after approval")
	if evaluation_date < getdate(doc.effective_from) or (
		doc.effective_to and evaluation_date > getdate(doc.effective_to)
	):
		frappe.throw("Recurrence policy is not effective on the requested evaluation date")


def _validate_controlled_append_only(doc, *, record_type: str, identity_field: str) -> None:
	if doc.get_doc_before_save() is not None:
		frappe.throw("Finding recurrence evaluation records are immutable", frappe.PermissionError)
	identity = str(doc.get(identity_field) or "")
	context = getattr(frappe.flags, _AUDIT_CREATE_FLAG, None)
	if (
		not isinstance(context, dict)
		or context.get("record_type") != record_type
		or str(context.get("identity") or "") != identity
	):
		frappe.throw("Finding recurrence audit records may only be created by the governed evaluator")
	if not _HEX64.fullmatch(identity):
		frappe.throw("Finding recurrence audit identity is invalid")


def _evaluation_hash(doc, links: list[dict[str, str]]) -> str:
	getter = doc.get if hasattr(doc, "get") else doc.__getitem__
	return _hash_payload(
		{
			"evaluation_key": getter("evaluation_key"),
			"run_key": getter("run_key"),
			"policy": getter("policy"),
			"policy_checksum": getter("policy_checksum"),
			"as_of_date": _canonical_value(getter("as_of_date")),
			"window_start": _canonical_value(getter("window_start")),
			"window_end": _canonical_value(getter("window_end")),
			"scope": {fieldname: _canonical_value(getter(fieldname)) for fieldname in _SCOPE_FIELDS},
			"grouping_mode": getter("grouping_mode"),
			"group_key": getter("group_key"),
			"rule": getter("rule"),
			"threshold": int(getter("threshold") or 0),
			"occurrence_count": int(getter("occurrence_count") or 0),
			"outcome": getter("outcome"),
			"finding_links": links,
			"finding_set_hash": getter("finding_set_hash"),
		}
	)


def _validate_finding_links(links: list[Any]) -> None:
	if len(links) > MAX_FINDINGS_PER_POLICY_RUN:
		frappe.throw("Recurrence evaluation finding links exceed the hard limit")
	seen: set[str] = set()
	for item in links:
		if (
			type(item) is not dict
			or set(item) != {"finding", "finding_hash"}
			or not str(item.get("finding") or "")
			or len(str(item["finding"])) > 140
			or not _HEX64.fullmatch(str(item.get("finding_hash") or ""))
		):
			frappe.throw("Recurrence evaluation contains an invalid finding link")
		name = str(item["finding"])
		if name in seen:
			frappe.throw("Recurrence evaluation contains a duplicate finding link")
		seen.add(name)


def _row_matches_approved_scope(row, policy_doc) -> bool:
	for fieldname in _AGGREGATION_FIELDS[str(policy_doc.aggregation_level)]:
		if str(row.get(fieldname) or "") != str(policy_doc.get(fieldname) or ""):
			return False
	return True


def _existing_run_response(name: str) -> dict[str, Any]:
	doc = frappe.get_doc(RUN_DOCTYPE, name)
	snapshot = _exact_json_list(doc.get("snapshot_json"), "Recurrence run snapshot")
	_validate_recurrence_run_snapshot(doc, snapshot)
	projects = sorted(
		{str(item["project"]) for item in snapshot if isinstance(item, dict) and item.get("project")}
	)
	return {
		"run": doc.name,
		"run_key": doc.run_key,
		"evaluations": int(doc.group_count or 0),
		"projects": projects,
		"finding_count": int(doc.finding_count or 0),
		"idempotent": True,
	}


def _validate_recurrence_run_snapshot(doc, snapshot: list[Any]) -> None:
	"""Revalidate the immutable response payload before initial or replay use."""
	if len(snapshot) != int(doc.get("group_count") or 0):
		frappe.throw("Recurrence run group count does not match its snapshot")
	if len(snapshot) > MAX_GROUPS_PER_POLICY_RUN:
		frappe.throw("Recurrence run group count exceeds the hard safety limit")
	expected_fields = {
		"evaluation",
		"evaluation_key",
		"group_key",
		"rule",
		"occurrence_count",
		"threshold",
		"outcome",
		"finding_set_hash",
		"project",
	}
	evaluations: set[str] = set()
	evaluation_keys: set[str] = set()
	group_keys: set[str] = set()
	projects: set[str] = set()
	finding_count = 0
	triggered_count = 0
	for item in snapshot:
		if not isinstance(item, dict) or set(item) != expected_fields:
			frappe.throw("Recurrence run snapshot row contract is invalid")
		evaluation = str(item.get("evaluation") or "")
		evaluation_key = str(item.get("evaluation_key") or "")
		group_key = str(item.get("group_key") or "")
		rule = str(item.get("rule") or "")
		finding_set_hash = str(item.get("finding_set_hash") or "")
		try:
			occurrence_count = int(item.get("occurrence_count") or 0)
			threshold = int(item.get("threshold") or 0)
		except (TypeError, ValueError) as exc:
			raise frappe.ValidationError("Recurrence run snapshot counts are invalid") from exc
		outcome = str(item.get("outcome") or "")
		project = str(item.get("project") or "")
		if (
			not evaluation
			or len(evaluation) > 140
			or not _HEX64.fullmatch(evaluation_key)
			or not _HEX64.fullmatch(group_key)
			or not rule
			or len(rule) > 140
			or not _HEX64.fullmatch(finding_set_hash)
			or occurrence_count < 1
			or threshold < 2
			or outcome
			not in {
				"Triggered",
				"Below Threshold",
			}
			or (outcome == "Triggered") != (occurrence_count >= threshold)
			or (outcome == "Triggered") != bool(project)
			or len(project) > 140
		):
			frappe.throw("Recurrence run snapshot row semantics are invalid")
		if evaluation in evaluations or evaluation_key in evaluation_keys or group_key in group_keys:
			frappe.throw("Recurrence run snapshot contains duplicate group provenance")
		evaluations.add(evaluation)
		evaluation_keys.add(evaluation_key)
		group_keys.add(group_key)
		finding_count += occurrence_count
		if outcome == "Triggered":
			triggered_count += 1
			projects.add(project)
	if finding_count != int(doc.get("finding_count") or 0):
		frappe.throw("Recurrence run finding count does not match its snapshot")
	if triggered_count != int(doc.get("triggered_group_count") or 0):
		frappe.throw("Recurrence run triggered-group count does not match its snapshot")
	if len(projects) != int(doc.get("project_count") or 0):
		frappe.throw("Recurrence run project count does not match its snapshot")
	if _hash_payload(snapshot) != str(doc.get("snapshot_hash") or ""):
		frappe.throw("Recurrence run snapshot hash is invalid")
	_assert_serialized_size(snapshot, "Recurrence run snapshot")


def _case_key(group_key: str, finding_set_hash: str) -> str:
	return _hash_payload({"group_key": group_key, "finding_set_hash": finding_set_hash})


def _policy_lock(policy: str) -> str:
	name = str(policy or "").strip()
	if not name or len(name) > 140:
		frappe.throw("Finding recurrence policy identifier is invalid")
	return f"ione-qms:finding-recurrence-policy:{_hash_text(name)[:24]}"


def _scope_values(source) -> dict[str, Any]:
	return {fieldname: source.get(fieldname) for fieldname in _SCOPE_FIELDS}


def _require_named_policy_actor() -> None:
	if frappe.session.user in {"", "Guest", "Administrator"}:
		frappe.throw(
			"Finding recurrence governance requires a named accountable business user",
			frappe.PermissionError,
		)
	require_role("IONE QC Reviewer", "IONE Medical Affairs")
	if "IONE Agent Service" in set(frappe.get_roles(frappe.session.user)):
		frappe.throw("AI service users cannot govern finding recurrence policies", frappe.PermissionError)


def _bounded_comment(value: str, label: str) -> str:
	comment = str(value or "").strip()
	if not comment or len(comment) > MAX_COMMENT_LENGTH:
		frappe.throw(f"{label} is required and cannot exceed {MAX_COMMENT_LENGTH} characters")
	return comment


def _bounded_exact_int(value: Any, label: str, minimum: int, maximum: int) -> int:
	if type(value) is int:
		number = value
	elif type(value) is str and value and value == value.strip() and value.isascii() and value.isdigit():
		number = int(value)
	else:
		frappe.throw(f"{label} must be an exact integer")
	if number < minimum or number > maximum:
		frappe.throw(f"{label} must be between {minimum} and {maximum}")
	return number


def _exact_json_list(value: Any, label: str) -> list[Any]:
	parsed = _parse_json(value, label)
	if type(parsed) is not list:
		frappe.throw(f"{label} must be a JSON array")
	if _canonical_json(parsed) != str(value):
		frappe.throw(f"{label} must use canonical JSON encoding")
	return parsed


def _exact_json_object(value: Any, label: str) -> dict[str, Any]:
	parsed = _parse_json(value, label)
	if type(parsed) is not dict:
		frappe.throw(f"{label} must be a JSON object")
	if _canonical_json(parsed) != str(value):
		frappe.throw(f"{label} must use canonical JSON encoding")
	return parsed


def _parse_json(value: Any, label: str) -> Any:
	if type(value) is not str:
		frappe.throw(f"{label} must be stored as JSON text")
	try:
		return json.loads(value)
	except TypeError, ValueError:
		frappe.throw(f"{label} is not valid JSON")


def _assert_serialized_size(value: Any, label: str) -> None:
	if len(_canonical_json(value).encode("utf-8")) > MAX_SNAPSHOT_BYTES:
		frappe.throw(f"{label} exceeds the {MAX_SNAPSHOT_BYTES}-byte safety limit")


def _canonical_value(value: Any) -> Any:
	if isinstance(value, (date,)):
		return str(value)
	if value in ("", None):
		return None
	return value


def _canonical_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)


def _hash_payload(value: Any) -> str:
	return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _hash_text(value: str) -> str:
	return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _same_optional(left: Any, right: Any) -> bool:
	return _canonical_value(left) == _canonical_value(right)


@contextmanager
def _controlled_policy_action(policy: str, action: str):
	previous = getattr(frappe.flags, _POLICY_ACTION_FLAG, None)
	setattr(frappe.flags, _POLICY_ACTION_FLAG, {"policy": policy, "action": action})
	try:
		yield
	finally:
		setattr(frappe.flags, _POLICY_ACTION_FLAG, previous)


@contextmanager
def _controlled_audit_create(record_type: str, identity: str):
	previous = getattr(frappe.flags, _AUDIT_CREATE_FLAG, None)
	setattr(
		frappe.flags,
		_AUDIT_CREATE_FLAG,
		{"record_type": record_type, "identity": identity},
	)
	try:
		yield
	finally:
		setattr(frappe.flags, _AUDIT_CREATE_FLAG, previous)


@contextmanager
def _controlled_pdca_create(evaluation: str, case_key: str):
	previous = getattr(frappe.flags, _PDCA_CREATE_FLAG, None)
	setattr(
		frappe.flags,
		_PDCA_CREATE_FLAG,
		{"evaluation": evaluation, "case_key": case_key},
	)
	try:
		yield
	finally:
		setattr(frappe.flags, _PDCA_CREATE_FLAG, previous)
