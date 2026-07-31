from __future__ import annotations

import hashlib
from typing import Any

import frappe
from frappe.model.workflow import apply_workflow
from frappe.share import add_docshare
from frappe.share import remove as remove_docshare
from frappe.utils import now_datetime

from ione_qms.ai.approvals import (
	queue_resume_for_run,
	review_tool_approval,
)
from ione_qms.ai.evaluation import queue_agent_evaluation
from ione_qms.ai.evidence import (
	parse_stored_references,
	persist_finding_source_evidence,
	validate_evidence_references,
)
from ione_qms.ai.governance import validate_policy_runtime
from ione_qms.ai.privacy import AIPrivacyViolation, deidentify_ai_value
from ione_qms.ai.privacy_runtime import task_known_identifiers
from ione_qms.ai.readiness import check_qwen_readiness
from ione_qms.ai.release import assert_approved_agent_release
from ione_qms.permissions import require_role, require_scope_read
from ione_qms.services.agent_evaluations import (
	current_approved_threshold_policy,
	locked_test_suite,
	test_suite_hash_from_rows,
	validate_evaluation_receipt_current,
)
from ione_qms.services.ai_report_schedules import (
	authorize_report_recovery,
	change_report_schedule_state,
	review_report_schedule,
)
from ione_qms.services.ai_tasks import controlled_analysis_task_creation
from ione_qms.services.findings import _is_human_accepted_ai_candidate
from ione_qms.services.indicators import current_indicator_result_lock
from ione_qms.services.runtime_settings import require_ai_runtime_enabled
from ione_qms.setup.workflows import FINDING_REVIEW_ACTION


@frappe.whitelist(methods=["GET"])
def get_model_readiness(model_name: str = "I-ONE Qwen 35B") -> dict:
	require_role("IONE Agent Administrator", "IONE Auditor")
	return check_qwen_readiness(model_name)


@frappe.whitelist(methods=["POST"])
def start_agent_evaluation(agent_release: str) -> dict[str, str]:
	if frappe.session.user == "Administrator":
		frappe.throw(
			"Administrator cannot request a governed Agent Evaluation; use a named user.",
			frappe.PermissionError,
		)
	require_role(
		"IONE Agent Administrator",
		"IONE Agent Reviewer",
		"IONE QC Reviewer",
		"IONE Medical Affairs",
	)
	require_ai_runtime_enabled()
	return queue_agent_evaluation(agent_release, frappe.session.user)


@frappe.whitelist(methods=["POST"])
def review_agent_evaluation(
	evaluation: str,
	decision: str,
	review_comment: str,
) -> dict[str, str]:
	if frappe.session.user == "Administrator":
		frappe.throw(
			"Administrator cannot review a governed Agent Evaluation; use a named reviewer.",
			frappe.PermissionError,
		)
	require_role("IONE Agent Reviewer", "IONE QC Reviewer", "IONE Medical Affairs")
	if decision not in {"Approve", "Reject"}:
		frappe.throw("Decision must be Approve or Reject")
	comment = str(review_comment or "").strip()
	if len(comment) < 5:
		frappe.throw("Agent Evaluation review requires a comment of at least five characters")
	with frappe.db.advisory_lock(f"ione-qms:agent-evaluation-review:{evaluation}", timeout=10):
		doc = frappe.get_doc("IONE Agent Evaluation", evaluation, for_update=True)
		doc.check_permission("read")
		if doc.get("review_status") != "Pending Review":
			frappe.throw("Agent Evaluation has already been reviewed")
		if frappe.session.user in {doc.get("evaluated_by"), doc.get("requested_by")}:
			frappe.throw("Agent Evaluation requester or worker cannot approve the same result")
		if decision == "Approve":
			with frappe.db.advisory_lock(
				f"ione-qms:agent-evaluation-binding:{doc.agent_release}",
				timeout=30,
			):
				_validate_current_evaluation_for_approval(doc)
				_save_evaluation_review(doc, decision, comment)
				frappe.db.commit()
		else:
			_save_evaluation_review(doc, decision, comment)
			frappe.db.commit()
	return {"evaluation": doc.name, "review_status": doc.review_status}


def _validate_current_evaluation_for_approval(doc) -> None:
	release = frappe.get_doc("IONE Agent Release", doc.agent_release, for_update=True)
	policy = frappe.get_doc("IONE Agent Policy", doc.policy, for_update=True)
	agent = frappe.get_doc("Flow Agent", release.flow_agent, for_update=True)
	frappe.get_doc("Flow Model", release.flow_model, for_update=True)
	current_approved_threshold_policy(for_update=True)
	suite = locked_test_suite(policy.name, agent.name)
	if doc.status != "Passed":
		frappe.throw("Only a Passed Agent Evaluation can be approved")
	approved_release = assert_approved_agent_release(policy, agent)
	if approved_release.name != release.name or release.status != "Approved":
		frappe.throw("Agent Evaluation no longer references the current Approved release")
	if test_suite_hash_from_rows(
		suite,
		policy=policy.name,
		agent_release=release.name,
	) != doc.get("test_suite_hash"):
		frappe.throw("Agent Test Case definitions changed after this evaluation")
	validate_evaluation_receipt_current(doc, policy, release)


def _save_evaluation_review(doc, decision: str, comment: str) -> None:
	flag = getattr(frappe.flags, "ione_evaluation_review", False)
	try:
		frappe.flags.ione_evaluation_review = True
		doc.review_status = "Approved" if decision == "Approve" else "Rejected"
		doc.reviewed_by = frappe.session.user
		doc.reviewed_at = now_datetime()
		doc.review_comment = comment[:2_000]
		doc.save(ignore_permissions=True)
	finally:
		frappe.flags.ione_evaluation_review = flag


@frappe.whitelist(methods=["POST"])
def review_agent_tool_call(
	approval: str,
	decision: str,
	review_comment: str,
) -> dict[str, Any]:
	return review_tool_approval(approval, decision, review_comment)


@frappe.whitelist(methods=["POST"])
def resume_agent_tool_calls(flow_run: str) -> dict[str, Any]:
	return queue_resume_for_run(flow_run)


@frappe.whitelist(methods=["POST"])
def review_quality_report_schedule(
	schedule: str,
	decision: str,
	review_comment: str,
) -> dict[str, str]:
	return review_report_schedule(schedule, decision, review_comment)


@frappe.whitelist(methods=["POST"])
def operate_quality_report_schedule(
	schedule: str,
	action: str,
	operation_comment: str,
) -> dict[str, str]:
	return change_report_schedule_state(schedule, action, operation_comment)


@frappe.whitelist(methods=["POST"])
def authorize_quality_report_recovery(
	schedule: str,
	prior_task: str,
	reason_code: str,
	reason: str,
) -> dict[str, str | int]:
	return authorize_report_recovery(schedule, prior_task, reason_code, reason)


@frappe.whitelist(methods=["POST"])
def create_analysis_task(
	task_type: str,
	policy: str,
	input_summary: str,
	hospital: str | None = None,
	campus: str | None = None,
	department: str | None = None,
	ward: str | None = None,
	patient: str | None = None,
	encounter: str | None = None,
	indicator_result: str | None = None,
	finding: str | None = None,
	record_count: int = 1,
) -> dict[str, str]:
	require_ai_runtime_enabled()
	if frappe.session.user == "Administrator":
		frappe.throw(
			"Administrator cannot originate governed clinical AI tasks; use a named accountable user.",
			frappe.PermissionError,
		)
	require_role(
		"IONE Agent Reviewer",
		"IONE QC Reviewer",
		"IONE Medical Affairs",
		"IONE Department Director",
		"IONE Department QC Officer",
	)
	summary = str(input_summary or "").strip()
	if not summary:
		frappe.throw("AI task input_summary is required")
	if len(summary) > 20000:
		frappe.throw("AI task input_summary exceeds the 20,000-character limit")
	record_count = max(int(record_count or 1), 1)
	scope = _resolve_task_scope(
		hospital=hospital,
		campus=campus,
		department=department,
		ward=ward,
		patient=patient,
		encounter=encounter,
		indicator_result=indicator_result,
		finding=finding,
	)
	try:
		summary = str(
			deidentify_ai_value(
				summary,
				known_identifiers=task_known_identifiers(frappe._dict(scope)),
			)
		)
	except AIPrivacyViolation as exc:
		frappe.throw(
			f"AI task input failed the patient-identity safety gate. code={exc.code}",
			frappe.ValidationError,
		)
	if task_type != "Policy Governance" or any(
		scope.get(fieldname) for fieldname in ("hospital", "campus", "department")
	):
		require_scope_read(
			hospital=scope["hospital"],
			campus=scope["campus"],
			department=scope["department"],
			ward=scope["ward"],
		)
	policy_doc = frappe.get_doc("IONE Agent Policy", policy)
	policy_doc.check_permission("read")
	if policy_doc.agent_category != task_type:
		frappe.throw("Task type does not match the selected Agent Policy")
	if record_count > int(policy_doc.maximum_records_per_run or 1):
		frappe.throw("Task exceeds policy maximum_records_per_run")
	if (scope["patient"] or scope["encounter"]) and not int(policy_doc.contains_patient_data or 0):
		frappe.throw("Selected policy does not permit patient data")
	validate_policy_runtime(policy_doc, {**scope, "record_count": record_count})
	doc = frappe.get_doc(
		{
			"doctype": "IONE AI Analysis Task",
			"task_type": task_type,
			"policy": policy,
			"requested_by": frappe.session.user,
			"requested_at": now_datetime(),
			**scope,
			"record_count": record_count,
			"input_summary": summary,
			"input_hash": hashlib.sha256(summary.encode()).hexdigest(),
			"data_classification": (
				"Sensitive Medical" if scope["patient"] or scope["encounter"] else "Hospital Internal"
			),
			"requires_human_review": int(policy_doc.requires_human_review or 0),
			"status": "Pending",
		}
	)
	# Frappe's technical Administrator bypasses ordinary DocType permissions, so
	# the document hook also requires this exact in-process creation capability.
	with controlled_analysis_task_creation(doc):
		doc.insert(ignore_permissions=True)
	return {"task": doc.name, "status": doc.status}


@frappe.whitelist(methods=["POST"])
def review_candidate_finding(
	candidate: str,
	decision: str,
	review_comment: str,
) -> dict[str, str | None]:
	if frappe.session.user == "Administrator":
		frappe.throw(
			"Administrator cannot review AI findings; use a named accountable reviewer.",
			frappe.PermissionError,
		)
	require_role("IONE Agent Reviewer", "IONE QC Reviewer", "IONE Medical Affairs")
	if decision not in {"Accept", "Reject"}:
		frappe.throw("Decision must be Accept or Reject")
	if len(str(review_comment or "").strip()) < 5:
		frappe.throw("A review comment of at least five characters is required")
	formal_finding = None
	with frappe.db.advisory_lock(f"ione-qms:ai-candidate-review:{candidate}", timeout=5):
		doc = frappe.get_doc("IONE AI Candidate Finding", candidate, for_update=True)
		doc.check_permission("read")
		if doc.status != "Pending Review":
			frappe.throw("Candidate finding has already been reviewed")
		if frappe.db.get_value("IONE AI Analysis Task", doc.task, "requested_by") == frappe.session.user:
			frappe.throw("AI task requesters cannot review their own candidate findings")
		if decision == "Accept":
			validate_evidence_references(
				doc.task,
				parse_stored_references(doc.get("evidence_references")),
				require_clinical=True,
			)
		savepoint = "ione_ai_candidate_review"
		frappe.db.savepoint(savepoint)
		previous_capability = getattr(frappe.flags, "ione_ai_artifact_review", None)
		try:
			formal_finding = _accepted_candidate_finding(doc) if decision == "Accept" else None
			doc.status = "Accepted" if decision == "Accept" else "Rejected"
			doc.reviewed_by = frappe.session.user
			doc.reviewed_at = now_datetime()
			doc.review_comment = review_comment[:2000]
			doc.formal_finding = formal_finding
			frappe.flags.ione_ai_artifact_review = f"{doc.doctype}:{doc.name}"
			doc.save(ignore_permissions=True)
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			raise
		finally:
			frappe.flags.ione_ai_artifact_review = previous_capability
		frappe.db.release_savepoint(savepoint)
		frappe.db.commit()
	return {"candidate": doc.name, "status": doc.status, "formal_finding": formal_finding}


def _accepted_candidate_finding(candidate) -> str:
	deduplication_key = hashlib.sha256(f"ai-candidate|{candidate.name}".encode()).hexdigest()
	formal_finding = frappe.db.get_value(
		"IONE QC Finding",
		{"deduplication_key": deduplication_key},
		"name",
	)
	if formal_finding:
		existing = frappe.get_doc("IONE QC Finding", formal_finding)
		if existing.get("status") != "Candidate" or not _is_human_accepted_ai_candidate(existing):
			frappe.throw(
				"An existing finding uses the candidate deduplication key without its exact "
				"pending provenance."
			)
		persist_finding_source_evidence(existing, candidate)
		return _apply_candidate_review_workflow(existing).name
	finding = frappe.get_doc(
		{
			"doctype": "IONE QC Finding",
			"deduplication_key": deduplication_key,
			"status": "Candidate",
			"hospital": candidate.get("hospital"),
			"campus": candidate.get("campus"),
			"department": candidate.get("department"),
			"ward": candidate.get("ward"),
			"patient": candidate.get("patient"),
			"encounter": candidate.get("encounter"),
			"severity": candidate.get("severity"),
			"title": candidate.get("title"),
			"description": candidate.get("rationale"),
			"detected_at": now_datetime(),
			"ai_origin": 1,
			"ai_task": candidate.get("task"),
			"ai_candidate": candidate.name,
		}
	)
	finding.insert(ignore_permissions=True)
	persist_finding_source_evidence(finding, candidate)
	return _apply_candidate_review_workflow(finding).name


def _apply_candidate_review_workflow(finding):
	"""Apply the reviewed action through Frappe without persisting broad write access."""
	user = frappe.session.user
	share_filters = {
		"user": user,
		"share_doctype": finding.doctype,
		"share_name": finding.name,
	}
	existing_share = frappe.db.get_value(
		"DocShare",
		share_filters,
		["name", "read", "share", "modified", "modified_by"],
		as_dict=True,
	)
	previous_reviewer = getattr(frappe.flags, "ione_candidate_reviewer", None)
	previous_finding = getattr(frappe.flags, "ione_candidate_review_finding", None)
	frappe.flags.ione_candidate_reviewer = user
	frappe.flags.ione_candidate_review_finding = finding.name
	try:
		add_docshare(
			finding.doctype,
			finding.name,
			user=user,
			read=1,
			write=1,
			notify=0,
			flags={"ignore_share_permission": True},
		)
		return apply_workflow(finding, FINDING_REVIEW_ACTION)
	finally:
		try:
			current_share = frappe.db.get_value("DocShare", share_filters, "name")
			if current_share:
				if existing_share:
					# Restore a pre-existing read/share grant while always revoking
					# temporary decision rights from the candidate reviewer.
					frappe.db.set_value(
						"DocShare",
						existing_share.name,
						{
							"read": int(existing_share.read or 0),
							"write": 0,
							"submit": 0,
							"share": int(existing_share.share or 0),
							"modified": existing_share.modified,
							"modified_by": existing_share.modified_by,
						},
						update_modified=False,
					)
				else:
					remove_docshare(
						finding.doctype,
						finding.name,
						user,
						flags={"ignore_permissions": True},
					)
		finally:
			frappe.flags.ione_candidate_reviewer = previous_reviewer
			frappe.flags.ione_candidate_review_finding = previous_finding


@frappe.whitelist(methods=["POST"])
def review_report_draft(
	draft: str,
	decision: str,
	review_comment: str,
) -> dict[str, str]:
	if frappe.session.user == "Administrator":
		frappe.throw(
			"Administrator cannot review AI reports; use a named accountable reviewer.",
			frappe.PermissionError,
		)
	require_role("IONE Agent Reviewer", "IONE QC Reviewer", "IONE Medical Affairs")
	if decision not in {"Approve", "Reject", "Request Revision"}:
		frappe.throw("Invalid report review decision")
	if len(str(review_comment or "").strip()) < 5:
		frappe.throw("A review comment of at least five characters is required")
	status = {
		"Approve": "Approved",
		"Reject": "Rejected",
		"Request Revision": "Revision Requested",
	}[decision]
	with frappe.db.advisory_lock(f"ione-qms:ai-report-review:{draft}", timeout=5):
		doc = frappe.get_doc("IONE AI Report Draft", draft, for_update=True)
		doc.check_permission("read")
		if doc.status not in {"Draft", "Pending Review"}:
			frappe.throw("Report draft has already been finalized")
		task_control = frappe.db.get_value(
			"IONE AI Analysis Task",
			doc.task,
			["requested_by", "origin", "report_reviewer"],
			as_dict=True,
		)
		if not task_control:
			frappe.throw("Report draft task no longer exists")
		if task_control.get("requested_by") == frappe.session.user:
			frappe.throw("AI task requesters cannot review their own report drafts")
		if str(task_control.get("origin") or "") == "Scheduled" and str(
			task_control.get("report_reviewer") or ""
		) != str(frappe.session.user or ""):
			frappe.throw(
				"Scheduled quality reports may only be reviewed by the independently approved reviewer.",
				frappe.PermissionError,
			)
		if decision == "Approve":
			validate_evidence_references(
				doc.task,
				parse_stored_references(doc.get("source_references")),
			)
		savepoint = "ione_ai_report_review"
		frappe.db.savepoint(savepoint)
		doc.status = status
		doc.reviewed_by = frappe.session.user
		doc.reviewed_at = now_datetime()
		doc.review_comment = review_comment[:2000]
		previous_capability = getattr(frappe.flags, "ione_ai_artifact_review", None)
		try:
			frappe.flags.ione_ai_artifact_review = f"{doc.doctype}:{doc.name}"
			doc.save(ignore_permissions=True)
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			raise
		finally:
			frappe.flags.ione_ai_artifact_review = previous_capability
		frappe.db.release_savepoint(savepoint)
		frappe.db.commit()
	return {"draft": doc.name, "status": doc.status}


def _resolve_task_scope(
	*,
	hospital: str | None,
	campus: str | None,
	department: str | None,
	ward: str | None,
	patient: str | None,
	encounter: str | None,
	indicator_result: str | None,
	finding: str | None,
) -> dict[str, Any]:
	scope: dict[str, Any] = {
		"hospital": hospital,
		"campus": campus,
		"department": department,
		"ward": ward,
		"patient": patient,
		"encounter": encounter,
		"indicator_result": indicator_result,
		"finding": finding,
	}
	for doctype, name in (
		("IONE Patient Index", patient),
		("IONE Encounter Index", encounter),
		("IONE Indicator Result", indicator_result),
		("IONE QC Finding", finding),
	):
		if not name:
			continue
		if doctype == "IONE Indicator Result":
			with current_indicator_result_lock(name) as (_pointer, doc):
				doc.check_permission("read")
				_merge_scope(scope, doc)
		else:
			doc = frappe.get_doc(doctype, name)
			doc.check_permission("read")
			_merge_scope(scope, doc)
	_complete_scope_hierarchy(scope)
	return scope


def _merge_scope(scope: dict[str, Any], doc) -> None:
	mappings = {
		"hospital": doc.get("hospital"),
		"campus": doc.get("campus"),
		"department": doc.get("department"),
		"ward": doc.get("ward"),
		"patient": doc.get("patient"),
		"encounter": doc.get("encounter"),
	}
	if doc.doctype == "IONE Patient Index":
		mappings["patient"] = doc.name
		mappings["hospital"] = doc.get("tenant_hospital")
	if doc.doctype == "IONE Encounter Index":
		mappings["encounter"] = doc.name
	for fieldname, value in mappings.items():
		if not value:
			continue
		if scope.get(fieldname) and scope[fieldname] != value:
			frappe.throw(f"AI task {fieldname} conflicts with the referenced record scope")
		scope[fieldname] = value


def _complete_scope_hierarchy(scope: dict[str, Any]) -> None:
	department = scope.get("department")
	if department:
		values = frappe.db.get_value(
			"IONE Medical Department",
			department,
			["hospital", "campus"],
			as_dict=True,
		)
		if not values:
			frappe.throw("AI task references an unknown department")
		for fieldname in ("hospital", "campus"):
			value = values.get(fieldname)
			if not value:
				continue
			if scope.get(fieldname) and scope[fieldname] != value:
				frappe.throw(f"AI task {fieldname} conflicts with its department")
			scope[fieldname] = value
	campus = scope.get("campus")
	if campus:
		campus_hospital = frappe.db.get_value("IONE Hospital Campus", campus, "hospital")
		if not campus_hospital:
			frappe.throw("AI task references an unknown campus")
		if scope.get("hospital") and scope["hospital"] != campus_hospital:
			frappe.throw("AI task hospital conflicts with its campus")
		scope["hospital"] = campus_hospital
	ward = scope.get("ward")
	if ward:
		values = frappe.db.get_value(
			"IONE Ward",
			ward,
			["hospital", "campus", "department"],
			as_dict=True,
		)
		if not values:
			frappe.throw("AI task references an unknown ward")
		for fieldname in ("hospital", "campus", "department"):
			value = values.get(fieldname)
			if not value:
				continue
			if scope.get(fieldname) and scope[fieldname] != value:
				frappe.throw(f"AI task {fieldname} conflicts with its ward")
			scope[fieldname] = value
