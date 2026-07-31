from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

import frappe
from frappe.utils import get_datetime, getdate, now_datetime

from ione_qms.permissions import get_access_context, require_scope_read
from ione_qms.services.runtime_settings import require_post_migrate_runtime_ready

SURGERY_LEVELS = frozenset({"Level I", "Level II", "Level III", "Level IV"})
POLICY_STATUSES = frozenset({"Draft", "Approved", "Retired"})
GOVERNED_STATUSES = frozenset({"Draft", "Approved", "Retired"})
EXCEPTION_STATUSES = frozenset({"Pending", "Approved", "Rejected", "Expired"})
SURGERY_PHASES = frozenset({"Scheduled", "Occurred", "Postoperative Finalized", "Cancelled"})
EXCEPTION_SCOPES = frozenset({"Authorization", "MDT", "Safety Checklist"})
MDT_CONCLUSIONS = frozenset({"Proceed", "Proceed with Conditions", "Do Not Proceed"})
CARE_STAGES = frozenset(
	{
		"Pre-anesthesia Assessment",
		"Intraoperative Monitoring",
		"Post-anesthesia Recovery",
		"Postoperative Follow-up",
	}
)
CARE_STAGE_STATUSES = frozenset({"Not Applicable", "Pending", "Completed", "Failed"})
OUTCOME_STATUSES = frozenset({"Not Observed", "No", "Yes"})
FACTUAL_GOVERNANCE_STATES = frozenset({"Passed", "Noncompliant", "Blocked"})

_CARE_STAGE_FIELDS = {
	"Pre-anesthesia Assessment": "preanesthesia_status",
	"Intraoperative Monitoring": "intraoperative_monitoring_status",
	"Post-anesthesia Recovery": "recovery_status",
	"Postoperative Follow-up": "postoperative_followup_status",
}
_CARE_STAGE_REASON_TOKENS = {
	"Pre-anesthesia Assessment": "PREANESTHESIA",
	"Intraoperative Monitoring": "INTRAOPERATIVE_MONITORING",
	"Post-anesthesia Recovery": "POSTANESTHESIA_RECOVERY",
	"Postoperative Follow-up": "POSTOPERATIVE_FOLLOWUP",
}

APPROVER_ROLES = frozenset({"IONE QC Reviewer", "IONE Medical Affairs"})
AUTHOR_ROLES = frozenset(
	{
		"IONE QC Administrator",
		"IONE QC Reviewer",
		"IONE Medical Affairs",
		"IONE Department Director",
		"IONE Department QC Officer",
	}
)
CLINICAL_RECORDER_ROLES = frozenset(
	{
		"IONE Medical Affairs",
		"IONE Department Director",
		"IONE Department QC Officer",
	}
)
PREOPERATIVE_RELEASE_ROLES = CLINICAL_RECORDER_ROLES | APPROVER_ROLES

MAX_JSON_BYTES = 64 * 1024
MAX_PARTICIPANTS = 100
MAX_CHECK_ITEMS = 100
MAX_EXCEPTION_EXPIRY_BATCH = 500

_QUALIFICATION_IMMUTABLE = (
	"qualification_key",
	"medical_staff",
	"hospital",
	"campus",
	"department",
	"qualification_type",
	"qualification_code",
	"issuing_authority",
	"issued_on",
	"effective_from",
	"effective_to",
	"evidence_file",
)
_AUTHORIZATION_IMMUTABLE = (
	"authorization_key",
	"medical_staff",
	"procedure_code",
	"procedure_name",
	"surgery_level",
	"authorization_scope",
	"hospital",
	"campus",
	"department",
	"qualification",
	"effective_from",
	"effective_to",
	"authorization_basis",
)
_POLICY_IMMUTABLE = (
	"policy_code",
	"policy_version",
	"hospital",
	"campus",
	"department",
	"procedure_code",
	"procedure_name",
	"surgery_level",
	"effective_from",
	"effective_to",
	"require_mdt",
	"minimum_mdt_participants",
	"required_mdt_roles_json",
	"required_check_items_json",
	"required_care_stages_json",
	"postoperative_followup_due_hours",
	"allow_emergency_exception",
	"allowed_exception_scopes_json",
	"maximum_exception_validity_hours",
)
_APPROVAL_AUDIT_FIELDS = ("approved_by", "approved_at", "approval_comment", "checksum")
_RETIREMENT_AUDIT_FIELDS = ("retired_by", "retired_at", "retirement_reason")


def validate_staff_qualification(doc, method: str | None = None) -> None:
	del method
	_validate_status(doc.status, GOVERNED_STATUSES, "qualification")
	_validate_period(doc.effective_from, doc.effective_to)
	_require_complete_organization(doc)
	_validate_staff_scope(doc)
	_require_named_governance_actor_on_create(doc)
	_validate_governed_transition(doc, immutable_fields=_QUALIFICATION_IMMUTABLE)
	if doc.status == "Approved":
		_validate_approval_audit(doc)
		_reject_overlapping_qualification(doc)
		expected = _checksum(_qualification_snapshot(doc))
		if str(doc.checksum or "") != expected:
			frappe.throw("Approved staff qualification checksum does not match its frozen definition.")
	elif doc.is_new() or str(doc.status or "") == "Draft":
		doc.checksum = ""


def validate_surgery_authorization(doc, method: str | None = None) -> None:
	del method
	_validate_status(doc.status, GOVERNED_STATUSES, "authorization")
	_validate_surgery_level(doc.surgery_level)
	_validate_period(doc.effective_from, doc.effective_to)
	_require_complete_organization(doc)
	_validate_staff_scope(doc)
	_require_named_governance_actor_on_create(doc)
	_validate_governed_transition(doc, immutable_fields=_AUTHORIZATION_IMMUTABLE)
	if str(doc.authorization_scope or "") != "Primary Surgeon":
		frappe.throw("Only explicitly governed Primary Surgeon authorizations are supported.")
	_validate_authorization_qualification(doc)
	if doc.status == "Approved":
		_validate_approval_audit(doc)
		_reject_overlapping_authorization(doc)
		expected = _checksum(_authorization_snapshot(doc))
		if str(doc.checksum or "") != expected:
			frappe.throw("Approved surgery authorization checksum does not match its frozen definition.")
	elif doc.is_new() or str(doc.status or "") == "Draft":
		doc.checksum = ""


def validate_surgery_procedure_policy(doc, method: str | None = None) -> None:
	del method
	_validate_status(doc.status, POLICY_STATUSES, "surgery procedure policy")
	_validate_surgery_level(doc.surgery_level)
	_validate_period(doc.effective_from, doc.effective_to)
	_require_complete_organization(doc)
	_require_named_governance_actor_on_create(doc)
	_validate_governed_transition(doc, immutable_fields=_POLICY_IMMUTABLE)

	required_roles = _string_list(doc.required_mdt_roles_json, "required_mdt_roles_json")
	required_items = _string_list(doc.required_check_items_json, "required_check_items_json")
	required_care_stages = _care_stage_list(
		doc.required_care_stages_json,
		"required_care_stages_json",
	)
	if not required_items:
		frappe.throw("An approved surgery policy must define at least one pre-operative check item.")
	if len(required_items) > MAX_CHECK_ITEMS:
		frappe.throw("A surgery policy cannot require more than 100 pre-operative check items.")
	doc.required_mdt_roles_json = _canonical_json(required_roles)
	doc.required_check_items_json = _canonical_json(required_items)
	doc.required_care_stages_json = _canonical_json(required_care_stages)
	if not required_care_stages:
		frappe.throw("An approved surgery policy must explicitly configure required care stages.")
	if (
		"Postoperative Follow-up" in required_care_stages
		and int(doc.postoperative_followup_due_hours or 0) < 1
	):
		frappe.throw("Postoperative follow-up requires an explicit positive completion window in hours.")
	if "Postoperative Follow-up" not in required_care_stages and int(
		doc.postoperative_followup_due_hours or 0
	):
		frappe.throw("Postoperative follow-up window must be empty when that care stage is not required.")

	if str(doc.surgery_level or "") == "Level IV":
		if not int(doc.require_mdt or 0):
			frappe.throw("Level IV surgery policy must explicitly require pre-operative MDT.")
		if int(doc.minimum_mdt_participants or 0) < 1:
			frappe.throw(
				"Level IV surgery policy must explicitly configure a positive MDT participant minimum."
			)
		if not required_roles:
			frappe.throw("Level IV surgery policy must explicitly define required MDT participant roles.")
	elif int(doc.require_mdt or 0):
		if int(doc.minimum_mdt_participants or 0) < 1 or not required_roles:
			frappe.throw("An MDT requirement needs an explicit participant minimum and required roles.")
	else:
		if int(doc.minimum_mdt_participants or 0) or required_roles:
			frappe.throw("MDT thresholds cannot be configured when require_mdt is disabled.")

	allowed_scopes = _exception_scope_list(
		doc.allowed_exception_scopes_json,
		"allowed_exception_scopes_json",
	)
	doc.allowed_exception_scopes_json = _canonical_json(allowed_scopes)
	if int(doc.allow_emergency_exception or 0):
		if not allowed_scopes:
			frappe.throw("Emergency exceptions require an explicit non-empty allowed scope list.")
		if int(doc.maximum_exception_validity_hours or 0) < 1:
			frappe.throw("Emergency exceptions require an explicit positive validity limit.")
	else:
		if allowed_scopes or int(doc.maximum_exception_validity_hours or 0):
			frappe.throw("Emergency exception configuration must be empty when exceptions are disabled.")

	if doc.status == "Approved":
		_validate_approval_audit(doc)
		_reject_overlapping_policy(doc)
		expected = _checksum(_policy_snapshot(doc))
		if str(doc.checksum or "") != expected:
			frappe.throw("Approved surgery policy checksum does not match its frozen definition.")
	elif doc.is_new() or str(doc.status or "") == "Draft":
		doc.checksum = ""


def review_staff_qualification(
	qualification: str,
	decision: str,
	review_comment: str,
) -> dict[str, str]:
	return _review_governed_definition(
		"IONE Staff Qualification",
		qualification,
		decision,
		review_comment,
		_validate_qualification_before_approval,
		_qualification_snapshot,
	)


def review_surgery_authorization(
	authorization: str,
	decision: str,
	review_comment: str,
) -> dict[str, str]:
	return _review_governed_definition(
		"IONE Surgery Authorization",
		authorization,
		decision,
		review_comment,
		_validate_authorization_before_approval,
		_authorization_snapshot,
	)


def review_surgery_procedure_policy(
	policy: str,
	decision: str,
	review_comment: str,
) -> dict[str, str]:
	return _review_governed_definition(
		"IONE Surgery Procedure Policy",
		policy,
		decision,
		review_comment,
		_validate_policy_before_approval,
		_policy_snapshot,
	)


def retire_surgery_governance_record(
	doctype: str,
	name: str,
	retirement_reason: str,
) -> dict[str, str]:
	if doctype not in {
		"IONE Staff Qualification",
		"IONE Surgery Authorization",
		"IONE Surgery Procedure Policy",
	}:
		frappe.throw("Unsupported surgery governance definition type.")
	_require_post()
	actor = _require_named_actor(APPROVER_ROLES)
	reason = _bounded_text(retirement_reason, "retirement_reason", 2_000)
	with frappe.db.advisory_lock(f"ione-qms:surgery-governance:{doctype}:{name}", timeout=5):
		doc = frappe.get_doc(doctype, name)
		_require_scope_write(doc, actor)
		if str(doc.status or "") != "Approved":
			frappe.throw("Only an Approved surgery governance definition may be retired.")
		doc.status = "Retired"
		doc.retired_by = actor
		doc.retired_at = now_datetime()
		doc.retirement_reason = reason
		doc.flags.ione_surgery_governance_transition = True
		doc.save(ignore_permissions=True)
	return {"name": str(doc.name), "status": str(doc.status)}


def create_mdt_record(
	surgery_qc: str,
	meeting_at: str,
	completed_at: str,
	chair: str,
	conclusion: str,
	conclusion_summary: str,
	participants: Sequence[Mapping[str, Any]] | str,
	evidence_reference: str,
) -> dict[str, str]:
	_require_post()
	actor = _require_named_actor(CLINICAL_RECORDER_ROLES)
	conclusion = str(conclusion or "").strip()
	if conclusion not in MDT_CONCLUSIONS:
		frappe.throw("Unsupported MDT conclusion.")
	participant_rows = _participant_rows(participants)
	if not participant_rows:
		frappe.throw("An MDT record requires explicit participants.")
	if len(participant_rows) > MAX_PARTICIPANTS:
		frappe.throw("An MDT record cannot contain more than 100 participants.")

	surgery = _locked_surgery(surgery_qc)
	_require_scope_write(surgery, actor)
	_require_surgery_pending(surgery)
	policy = _approved_policy_for_surgery(surgery)
	if not int(policy.require_mdt or 0):
		frappe.throw("The approved surgery policy does not require MDT.")
	meeting_time = get_datetime(meeting_at)
	completion_time = get_datetime(completed_at)
	surgery_time = get_datetime(surgery.surgery_time)
	if completion_time < meeting_time or completion_time >= surgery_time:
		frappe.throw("MDT must be completed after it starts and before the surgery occurrence.")
	if str(conclusion) == "Do Not Proceed":
		frappe.throw("An MDT conclusion of Do Not Proceed cannot satisfy surgery prerequisites.")
	_validate_staff_identity_in_scope(chair, surgery)
	_validate_participants_against_policy(participant_rows, policy, completion_time)
	reference = _bounded_text(evidence_reference, "evidence_reference", 2_000)

	key = _checksum(
		{
			"surgery_source_id_hash": surgery.source_record_id_hash,
			"policy": policy.name,
			"meeting_at": meeting_time,
			"completed_at": completion_time,
			"chair": chair,
			"participants": participant_rows,
		}
	)
	with frappe.db.advisory_lock(
		f"ione-qms:surgery-governance-source:{surgery.source_record_id_hash}",
		timeout=5,
	):
		if _active_record_exists("IONE Surgery MDT Record", surgery.source_record_id_hash):
			frappe.throw("A completed MDT record already exists for this surgery source identity.")
		doc = frappe.get_doc(
			{
				"doctype": "IONE Surgery MDT Record",
				"mdt_key": key,
				"surgery_qc": surgery.name,
				"surgery_source_id_hash": surgery.source_record_id_hash,
				"procedure_policy": policy.name,
				**_scope_values(surgery),
				"meeting_at": meeting_time,
				"completed_at": completion_time,
				"chair": chair,
				"conclusion": conclusion,
				"conclusion_summary": _bounded_text(
					conclusion_summary,
					"conclusion_summary",
					10_000,
				),
				"evidence_reference": reference,
				"participant_count": len(participant_rows),
				"recorded_by": actor,
				"recorded_at": now_datetime(),
				"status": "Completed",
			}
		)
		doc.checksum = _checksum(_mdt_snapshot(doc, participant_rows))
		doc.flags.ione_surgery_governance_materializer = True
		doc.insert(ignore_permissions=True)
		for participant in participant_rows:
			participant_doc = frappe.get_doc(
				{
					"doctype": "IONE Surgery MDT Participant",
					"participant_key": _checksum(
						{
							"mdt": doc.name,
							"medical_staff": participant["medical_staff"],
							"participant_role": participant["participant_role"],
						}
					),
					"mdt_record": doc.name,
					"surgery_qc": surgery.name,
					"surgery_source_id_hash": surgery.source_record_id_hash,
					**_scope_values(surgery),
					"medical_staff": participant["medical_staff"],
					"participant_role": participant["participant_role"],
					"attended": 1,
					"confirmed_at": get_datetime(participant["confirmed_at"]),
					"evidence_reference": participant["evidence_reference"],
					"recorded_by": actor,
					"recorded_at": doc.recorded_at,
				}
			)
			participant_doc.checksum = _checksum(_participant_snapshot(participant_doc))
			participant_doc.flags.ione_surgery_governance_materializer = True
			participant_doc.insert(ignore_permissions=True)
	return {"name": str(doc.name), "status": str(doc.status), "checksum": str(doc.checksum)}


def complete_preoperative_checklist(
	surgery_qc: str,
	completed_at: str,
	items: Sequence[Mapping[str, Any]] | str,
	evidence_reference: str,
) -> dict[str, str]:
	_require_post()
	actor = _require_named_actor(CLINICAL_RECORDER_ROLES)
	surgery = _locked_surgery(surgery_qc)
	_require_scope_write(surgery, actor)
	_require_surgery_pending(surgery)
	policy = _approved_policy_for_surgery(surgery)
	completion_time = get_datetime(completed_at)
	if completion_time >= get_datetime(surgery.surgery_time):
		frappe.throw("The pre-operative safety checklist must be completed before surgery.")
	results = _check_results(items)
	required = _string_list(policy.required_check_items_json, "required_check_items_json")
	if set(results) != set(required):
		frappe.throw("Checklist results must exactly cover the approved policy check items.")
	if any(results[item] != "Pass" for item in required):
		frappe.throw("Every required pre-operative safety check must explicitly pass.")
	reference = _bounded_text(evidence_reference, "evidence_reference", 2_000)
	key = _checksum(
		{
			"surgery_source_id_hash": surgery.source_record_id_hash,
			"policy": policy.name,
			"completed_at": completion_time,
			"results": results,
		}
	)
	with frappe.db.advisory_lock(
		f"ione-qms:surgery-governance-source:{surgery.source_record_id_hash}",
		timeout=5,
	):
		if _active_record_exists("IONE Surgery Safety Checklist", surgery.source_record_id_hash):
			frappe.throw("A completed safety checklist already exists for this surgery source identity.")
		doc = frappe.get_doc(
			{
				"doctype": "IONE Surgery Safety Checklist",
				"checklist_key": key,
				"surgery_qc": surgery.name,
				"surgery_source_id_hash": surgery.source_record_id_hash,
				"procedure_policy": policy.name,
				**_scope_values(surgery),
				"required_items_json": _canonical_json(required),
				"results_json": _canonical_json(results),
				"completed_by": actor,
				"completed_at": completion_time,
				"evidence_reference": reference,
				"status": "Completed",
			}
		)
		doc.checksum = _checksum(_checklist_snapshot(doc))
		doc.flags.ione_surgery_governance_materializer = True
		doc.insert(ignore_permissions=True)
	return {"name": str(doc.name), "status": str(doc.status), "checksum": str(doc.checksum)}


def request_emergency_exception(
	surgery_qc: str,
	requested_scopes: Sequence[str] | str,
	reason_code: str,
	reason: str,
	valid_from: str,
	valid_to: str,
) -> dict[str, str]:
	_require_post()
	actor = _require_named_actor(CLINICAL_RECORDER_ROLES)
	surgery = _locked_surgery(surgery_qc)
	_require_scope_write(surgery, actor)
	_require_surgery_pending(surgery)
	policy = _approved_policy_for_surgery(surgery)
	if not int(policy.allow_emergency_exception or 0):
		frappe.throw("The approved surgery policy does not allow emergency exceptions.")
	scopes = _exception_scope_list(requested_scopes, "requested_scopes")
	allowed = set(
		_exception_scope_list(
			policy.allowed_exception_scopes_json,
			"allowed_exception_scopes_json",
		)
	)
	if not scopes or not set(scopes).issubset(allowed):
		frappe.throw("Requested exception scopes exceed the approved policy.")
	start = get_datetime(valid_from)
	end = get_datetime(valid_to)
	if end <= start:
		frappe.throw("Emergency exception valid_to must follow valid_from.")
	max_hours = int(policy.maximum_exception_validity_hours or 0)
	if end - start > timedelta(hours=max_hours):
		frappe.throw("Emergency exception validity exceeds the approved policy limit.")
	surgery_time = get_datetime(surgery.surgery_time)
	if end <= now_datetime() or start >= surgery_time:
		frappe.throw("Emergency exception validity must cover a future pre-operative interval.")
	if end > surgery_time:
		frappe.throw("Emergency exception validity cannot extend beyond the scheduled surgery time.")
	source_hash = str(surgery.source_record_id_hash or "")
	with frappe.db.advisory_lock(
		f"ione-qms:surgery-governance-source:{source_hash}",
		timeout=5,
	):
		surgery = frappe.get_doc("IONE Surgery QC", surgery_qc, for_update=True)
		requested_at = now_datetime()
		if str(surgery.source_record_id_hash or "") != source_hash or str(
			surgery.procedure_policy or ""
		) != str(policy.name or ""):
			frappe.throw("Emergency exception no longer matches the locked scheduled surgery.")
		_require_surgery_pending(surgery, at=requested_at)
		if end <= requested_at:
			frappe.throw("Emergency exception validity elapsed before the request was locked.")
		if frappe.db.exists(
			"IONE Surgery Emergency Exception",
			{
				"surgery_source_id_hash": surgery.source_record_id_hash,
				"status": ["in", ["Pending", "Approved"]],
			},
		):
			frappe.throw("This surgery already has a pending or approved emergency exception.")
		key = _checksum(
			{
				"surgery_source_id_hash": surgery.source_record_id_hash,
				"policy": policy.name,
				"requested_scopes": scopes,
				"requested_by": actor,
				"requested_at": requested_at,
			}
		)
		doc = frappe.get_doc(
			{
				"doctype": "IONE Surgery Emergency Exception",
				"exception_key": key,
				"surgery_qc": surgery.name,
				"surgery_source_id_hash": surgery.source_record_id_hash,
				"procedure_policy": policy.name,
				**_scope_values(surgery),
				"requested_scopes_json": _canonical_json(scopes),
				"reason_code": _bounded_text(reason_code, "reason_code", 100),
				"reason": _bounded_text(reason, "reason", 10_000),
				"requested_by": actor,
				"requested_at": requested_at,
				"valid_from": start,
				"valid_to": end,
				"status": "Pending",
			}
		)
		doc.flags.ione_surgery_governance_materializer = True
		doc.insert(ignore_permissions=True)
	return {"name": str(doc.name), "status": str(doc.status)}


def review_emergency_exception(
	exception: str,
	decision: str,
	review_comment: str,
) -> dict[str, str]:
	_require_post()
	actor = _require_named_actor(APPROVER_ROLES)
	if decision not in {"Approve", "Reject"}:
		frappe.throw("Emergency exception decision must be Approve or Reject.")
	comment = _bounded_text(review_comment, "review_comment", 2_000)
	with frappe.db.advisory_lock(f"ione-qms:surgery-exception-review:{exception}", timeout=5):
		doc = frappe.get_doc("IONE Surgery Emergency Exception", exception, for_update=True)
		with frappe.db.advisory_lock(
			f"ione-qms:surgery-governance-source:{doc.surgery_source_id_hash}",
			timeout=5,
		):
			surgery = frappe.get_doc("IONE Surgery QC", doc.surgery_qc, for_update=True)
			reviewed_at = now_datetime()
			if str(surgery.source_record_id_hash or "") != str(doc.surgery_source_id_hash or "") or str(
				surgery.procedure_policy or ""
			) != str(doc.procedure_policy or ""):
				frappe.throw("Emergency exception no longer matches the locked scheduled surgery.")
			_require_surgery_pending(surgery, at=reviewed_at)
			_require_scope_write(doc, actor)
			if str(doc.status or "") != "Pending":
				frappe.throw("Only a Pending emergency exception may be reviewed.")
			if str(doc.requested_by or "") == actor:
				frappe.throw("An emergency exception requester cannot approve or reject their own request.")
			policy = frappe.get_doc("IONE Surgery Procedure Policy", doc.procedure_policy)
			_validate_live_policy(policy, get_datetime(doc.valid_from))
			if not int(policy.allow_emergency_exception or 0):
				frappe.throw("The governing policy no longer permits emergency exceptions.")
			scopes = _exception_scope_list(doc.requested_scopes_json, "requested_scopes_json")
			allowed = set(
				_exception_scope_list(
					policy.allowed_exception_scopes_json,
					"allowed_exception_scopes_json",
				)
			)
			if not set(scopes).issubset(allowed):
				frappe.throw("Emergency exception scope is not allowed by the live approved policy.")
			if get_datetime(doc.valid_to) <= reviewed_at:
				frappe.throw("An expired emergency exception cannot be approved.")
			doc.status = "Approved" if decision == "Approve" else "Rejected"
			doc.reviewed_by = actor
			doc.reviewed_at = reviewed_at
			doc.review_comment = comment
			if decision == "Approve":
				doc.checksum = _checksum(_exception_snapshot(doc))
			doc.flags.ione_surgery_governance_transition = True
			doc.save(ignore_permissions=True)
	return {"name": str(doc.name), "status": str(doc.status), "checksum": str(doc.checksum or "")}


def check_preoperative_release(surgery_qc: str) -> dict[str, Any]:
	"""Evaluate a scheduled surgery without mutating its factual QC projection.

	This endpoint is a decision boundary for an external theatre/HIS workflow. The
	hospital must call it immediately before release and must treat every exception
	or non-success response as a block.
	"""
	actor = _require_named_actor(PREOPERATIVE_RELEASE_ROLES)
	surgery = frappe.get_doc("IONE Surgery QC", surgery_qc)
	_require_scope_write(surgery, actor)
	source_hash = str(surgery.source_record_id_hash or "")
	with frappe.db.advisory_lock(
		f"ione-qms:surgery-governance-source:{source_hash}",
		timeout=5,
	):
		surgery = frappe.get_doc("IONE Surgery QC", surgery_qc, for_update=True)
		checked_at = now_datetime()
		if str(surgery.source_record_id_hash or "") != source_hash:
			frappe.throw("Surgery source identity changed during pre-operative release evaluation.")
		_require_surgery_pending(surgery, at=checked_at)
		event = frappe.get_doc("IONE Clinical Quality Event", surgery.event)
		evaluation = _evaluate_preoperative_release(
			surgery.as_dict(),
			event,
			decision_at=checked_at,
		)

	evidence = {
		fieldname: (
			{
				"name": str(evaluation[fieldname].name),
				"checksum": str(evaluation[fieldname].get("checksum") or ""),
			}
			if evaluation.get(fieldname)
			else None
		)
		for fieldname in (
			"procedure_policy",
			"surgery_authorization",
			"staff_qualification",
			"mdt_record",
			"safety_checklist",
			"emergency_exception",
		)
	}
	receipt = {
		"surgery_qc": str(surgery.name),
		"source_record_id_hash": source_hash,
		"surgery_time": surgery.surgery_time,
		"checked_at": checked_at,
		"release_state": str(evaluation["release_state"]),
		"evidence": evidence,
	}
	return {
		**receipt,
		"release_allowed": 1,
		"release_checksum": _checksum(receipt),
	}


def expire_surgery_emergency_exceptions(limit: int = MAX_EXCEPTION_EXPIRY_BATCH) -> dict[str, Any]:
	"""Durably close elapsed Pending/Approved exception windows without erasing approval evidence."""
	require_post_migrate_runtime_ready("expire surgery emergency exceptions")
	batch_limit = min(max(int(limit or 1), 1), MAX_EXCEPTION_EXPIRY_BATCH)
	cutoff = now_datetime()
	names = frappe.get_all(
		"IONE Surgery Emergency Exception",
		filters={
			"status": ["in", ["Pending", "Approved"]],
			"valid_to": ["<=", cutoff],
		},
		pluck="name",
		order_by="valid_to asc, name asc",
		limit_page_length=batch_limit,
	)
	expired = 0
	failed = 0
	for name in names:
		failure_code = None
		try:
			with frappe.db.advisory_lock(f"ione-qms:surgery-exception-review:{name}", timeout=5):
				doc = frappe.get_doc("IONE Surgery Emergency Exception", name, for_update=True)
				prior_status = str(doc.status or "")
				if prior_status not in {"Pending", "Approved"} or get_datetime(doc.valid_to) > cutoff:
					continue
				with frappe.db.advisory_lock(
					f"ione-qms:surgery-governance-source:{doc.surgery_source_id_hash}",
					timeout=5,
				):
					doc.status = "Expired"
					doc.expired_from_status = prior_status
					doc.expired_at = cutoff
					doc.expiration_checksum = _checksum(_exception_expiration_snapshot(doc))
					doc.flags.ione_surgery_governance_transition = True
					doc.flags.ione_surgery_governance_expiry = True
					doc.save(ignore_permissions=True)
			frappe.db.commit()
			expired += 1
		except Exception as exc:
			# Leave the active exception context before logging so traceback
			# locals can never copy clinical governance data into Error Log.
			failure_code = _surgery_expiry_failure_code(exc)
			frappe.db.rollback()
			failed += 1
		if not failure_code:
			continue
		frappe.log_error(
			title="IONE surgery emergency exception expiry failed",
			message=(
				f"code={failure_code}: one bounded emergency exception expiry failed. "
				"Clinical details were not copied into this log."
			),
			reference_doctype="IONE Surgery Emergency Exception",
			reference_name=str(name),
		)
	return {
		"cutoff": cutoff,
		"scanned": len(names),
		"expired": expired,
		"failed": failed,
		"has_more": len(names) == batch_limit,
	}


def _surgery_expiry_failure_code(exc: Exception) -> str:
	if isinstance(exc, frappe.PermissionError):
		return "PERMISSION_DENIED"
	if isinstance(exc, (frappe.ValidationError, ValueError, TypeError)):
		return "VALIDATION_FAILED"
	if exc.__class__.__name__ in {"QueryTimeoutError", "LockTimeoutError"}:
		return "LOCK_TIMEOUT"
	return "UNEXPECTED_FAILURE"


def validate_surgery_mdt_record(doc, method: str | None = None) -> None:
	del method
	_validate_materialized_append_only(doc, "MDT record")
	if str(doc.status or "") != "Completed":
		frappe.throw("Surgery MDT records are immutable Completed records.")
	if get_datetime(doc.completed_at) < get_datetime(doc.meeting_at):
		frappe.throw("MDT completed_at cannot precede meeting_at.")
	if str(doc.conclusion or "") not in MDT_CONCLUSIONS:
		frappe.throw("Unsupported MDT conclusion.")
	if str(doc.checksum or "") != _checksum(
		_mdt_snapshot(doc, _stored_mdt_participant_rows(doc.name) if not doc.is_new() else None)
	):
		if not doc.is_new():
			frappe.throw("Surgery MDT checksum does not match its immutable record.")


def validate_surgery_mdt_participant(doc, method: str | None = None) -> None:
	del method
	_validate_materialized_append_only(doc, "MDT participant")
	if not int(doc.attended or 0):
		frappe.throw("Only explicitly attended MDT participants may be recorded.")
	if str(doc.checksum or "") != _checksum(_participant_snapshot(doc)):
		frappe.throw("MDT participant checksum does not match its immutable record.")


def validate_surgery_safety_checklist(doc, method: str | None = None) -> None:
	del method
	_validate_materialized_append_only(doc, "safety checklist")
	if str(doc.status or "") != "Completed":
		frappe.throw("Surgery safety checklists are immutable Completed records.")
	if str(doc.checksum or "") != _checksum(_checklist_snapshot(doc)):
		frappe.throw("Surgery safety checklist checksum does not match its immutable record.")


def validate_surgery_emergency_exception(doc, method: str | None = None) -> None:
	del method
	_validate_status(doc.status, EXCEPTION_STATUSES, "emergency exception")
	if doc.is_new() and not bool(getattr(doc.flags, "ione_surgery_governance_materializer", False)):
		frappe.throw(
			"Emergency exceptions may only be created through the governed POST API.",
			frappe.PermissionError,
		)
	if doc.is_new():
		if str(doc.status or "") != "Pending":
			frappe.throw("New emergency exception requests must start Pending.")
		if any(
			doc.get(fieldname)
			for fieldname in (
				"reviewed_by",
				"reviewed_at",
				"review_comment",
				"checksum",
				"expired_from_status",
				"expired_at",
				"expiration_checksum",
			)
		):
			frappe.throw("New emergency exception requests cannot contain review or expiry evidence.")
	if not doc.is_new():
		before = doc.get_doc_before_save()
		if before:
			_changed = _changed_fields(
				doc,
				before,
				(
					"exception_key",
					"surgery_qc",
					"surgery_source_id_hash",
					"procedure_policy",
					"hospital",
					"campus",
					"department",
					"requested_scopes_json",
					"reason_code",
					"reason",
					"requested_by",
					"requested_at",
					"valid_from",
					"valid_to",
				),
			)
			if _changed:
				frappe.throw("Emergency exception request fields are immutable.")
			if not bool(getattr(doc.flags, "ione_surgery_governance_transition", False)):
				if _changed_fields(
					doc,
					before,
					(
						"status",
						"reviewed_by",
						"reviewed_at",
						"review_comment",
						"checksum",
						"expired_from_status",
						"expired_at",
						"expiration_checksum",
					),
				):
					frappe.throw("Emergency exception review must use the governed POST API.")
			if (
				str(doc.status or "") == "Expired"
				and str(before.status or "") != "Expired"
				and not bool(getattr(doc.flags, "ione_surgery_governance_expiry", False))
			):
				frappe.throw("Emergency exception expiry must use the scheduled governed transition.")
	status = str(doc.status or "")
	expiry_values = (
		doc.get("expired_from_status"),
		doc.get("expired_at"),
		doc.get("expiration_checksum"),
	)
	if status in {"Pending", "Approved", "Rejected"} and any(expiry_values):
		frappe.throw("Only Expired emergency exceptions may contain expiry evidence.")
	if status == "Pending":
		if any(
			doc.get(fieldname) for fieldname in ("reviewed_by", "reviewed_at", "review_comment", "checksum")
		):
			frappe.throw("Pending emergency exceptions cannot contain review evidence.")
	elif status in {"Approved", "Rejected"}:
		if not doc.reviewed_by or not doc.reviewed_at or not doc.review_comment:
			frappe.throw("Reviewed emergency exceptions require accountable review evidence.")
		if status == "Approved" and str(doc.checksum or "") != _checksum(_exception_snapshot(doc)):
			frappe.throw("Emergency exception checksum does not match its approved record.")
		if status == "Rejected" and doc.checksum:
			frappe.throw("Rejected emergency exceptions cannot contain an approval checksum.")
	elif status == "Expired":
		_validate_expired_exception(doc)


def prevent_surgery_governance_deletion(doc, method: str | None = None) -> None:
	del doc, method
	frappe.throw("Surgery governance records are retained as immutable audit evidence.")


def prepare_surgery_projection(values: Mapping[str, Any]) -> dict[str, Any]:
	"""Bind a surgery projection to immutable source identity and evaluate its governed facts.

	Pre-operative release remains a separate, fail-closed decision. Once the source
	system says a surgery occurred or was finalized, this projection must retain the
	fact even when a prerequisite is absent or invalid.
	"""
	if not isinstance(values, Mapping):
		raise TypeError("Surgery projection values must be a mapping.")
	result = dict(values)
	event_name = str(result.get("event") or "").strip()
	if not event_name:
		frappe.throw("Surgery projection requires a normalized clinical event.")
	event = frappe.db.get_value(
		"IONE Clinical Quality Event",
		event_name,
		[
			"name",
			"event_type",
			"event_time",
			"source_system",
			"source_record_type",
			"source_record_id",
			"source_version",
			"hospital",
			"campus",
			"department",
		],
		as_dict=True,
	)
	if not event:
		frappe.throw("Surgery projection references an unknown clinical event.")
	phase = str(result.get("surgery_phase") or "").strip()
	if phase not in SURGERY_PHASES:
		frappe.throw("Surgery projection requires an explicit supported surgery_phase.")
	if phase in {"Occurred", "Postoperative Finalized", "Cancelled"} and not result.get(
		"source_finalized_at"
	):
		frappe.throw("Finalized surgery projections require an explicit source_finalized_at.")
	source_identity_hash = _checksum(
		{
			"source_system": event.source_system,
			"source_record_type": event.source_record_type,
			"source_record_id": event.source_record_id,
		}
	)
	result["source_system"] = event.source_system
	result["source_record_type"] = event.source_record_type
	result["source_record_id_hash"] = source_identity_hash
	result["source_version"] = str(event.source_version or "")
	result["source_event_time"] = event.event_time
	result["record_snapshot_hash"] = _checksum(
		{
			"source_system": event.source_system,
			"source_record_type": event.source_record_type,
			"source_record_id_hash": source_identity_hash,
			"source_version": event.source_version,
			"source_finalized_at": result.get("source_finalized_at"),
			"event_type": event.event_type,
			"event_time": event.event_time,
			"surgery_no": result.get("surgery_no"),
			"surgery_type": result.get("surgery_type"),
			"procedure_code": result.get("procedure_code"),
			"surgery_level": result.get("surgery_level"),
			"surgery_time": result.get("surgery_time"),
			"surgeon": result.get("surgeon"),
			"anesthesia_type": result.get("anesthesia_type"),
			"risk_level": result.get("risk_level"),
			"qc_status": result.get("status"),
			"details_json_hash": _checksum(result.get("details_json") or ""),
			"surgery_phase": phase,
			"preanesthesia_status": result.get("preanesthesia_status"),
			"preanesthesia_completed_at": result.get("preanesthesia_completed_at"),
			"intraoperative_monitoring_status": result.get("intraoperative_monitoring_status"),
			"monitoring_started_at": result.get("monitoring_started_at"),
			"monitoring_completed_at": result.get("monitoring_completed_at"),
			"recovery_status": result.get("recovery_status"),
			"recovery_completed_at": result.get("recovery_completed_at"),
			"postoperative_followup_status": result.get("postoperative_followup_status"),
			"postoperative_followup_completed_at": result.get("postoperative_followup_completed_at"),
			"cancellation_status": result.get("cancellation_status"),
			"cancellation_reason_code": result.get("cancellation_reason_code"),
			"cancelled_at": result.get("cancelled_at"),
			"unplanned_surgery_status": result.get("unplanned_surgery_status"),
			"unplanned_surgery_reason_code": result.get("unplanned_surgery_reason_code"),
			"unplanned_return_status": result.get("unplanned_return_status"),
			"unplanned_return_at": result.get("unplanned_return_at"),
			"complication_status": result.get("complication_status"),
			"complication_code": result.get("complication_code"),
			"complication_at": result.get("complication_at"),
			"mortality_status": result.get("mortality_status"),
			"mortality_at": result.get("mortality_at"),
			"hospital": event.hospital,
			"campus": event.campus,
			"department": event.department,
		}
	)
	structured_reason_codes = _validate_structured_surgery_values(
		result,
		phase,
		capture_factual_noncompliance=phase in {"Occurred", "Postoperative Finalized"},
	)
	if phase == "Scheduled":
		policy = _approved_policy_for_values(result, event)
		result["procedure_policy"] = policy.name
		result["governance_state"] = "Pending Prerequisites"
		result["governance_checked_at"] = now_datetime()
		result["governance_reason_codes_json"] = "[]"
	elif phase in {"Occurred", "Postoperative Finalized"}:
		with frappe.db.advisory_lock(
			f"ione-qms:surgery-governance-source:{source_identity_hash}",
			timeout=5,
		):
			evaluation = _evaluate_occurrence(
				result,
				event,
				structured_reason_codes=structured_reason_codes,
			)
			result.update(evaluation)
	elif phase == "Cancelled":
		result["governance_state"] = "Cancelled"
		result["governance_checked_at"] = now_datetime()
		result["governance_reason_codes_json"] = "[]"
	return result


def validate_surgery_qc(doc, method: str | None = None) -> None:
	del method
	if not bool(getattr(doc.flags, "ione_projection_materializer", False)):
		frappe.throw("Surgery QC may only be changed by the controlled projection materializer.")
	required = (
		"event",
		"source_system",
		"source_record_type",
		"source_record_id_hash",
		"source_version",
		"source_event_time",
		"record_snapshot_hash",
		"procedure_code",
		"surgery_level",
		"surgery_time",
		"surgeon",
		"hospital",
		"campus",
		"department",
		"surgery_phase",
		"governance_state",
		"governance_checked_at",
		"governance_reason_codes_json",
	)
	if any(not doc.get(fieldname) for fieldname in required):
		frappe.throw("Surgery QC is missing governed source, scope, or evaluation fields.")
	_validate_surgery_level(doc.surgery_level)
	if str(doc.surgery_phase or "") == "Scheduled" and not doc.procedure_policy:
		frappe.throw("Scheduled surgery requires an approved procedure policy reference.")
	if str(doc.surgery_phase or "") in {"Occurred", "Postoperative Finalized"}:
		governance_state = str(doc.governance_state or "")
		if governance_state not in FACTUAL_GOVERNANCE_STATES:
			frappe.throw("Occurred surgery requires an explicit factual governance outcome.")
		reason_codes = _governance_reason_codes(doc.governance_reason_codes_json)
		if governance_state == "Passed":
			if not int(doc.compliant or 0) or reason_codes:
				frappe.throw("Passed surgery governance cannot contain noncompliance evidence.")
		elif int(doc.compliant or 0) or not reason_codes:
			frappe.throw("Noncompliant or Blocked surgery governance requires stable reason codes.")
		expected_status = "Passed" if governance_state == "Passed" else "Failed"
		if str(doc.status or "") != expected_status:
			frappe.throw("Surgery QC status must match its factual governance outcome.")
		if not doc.source_finalized_at or not doc.governance_checksum:
			frappe.throw("Occurred surgery requires source finalization and evaluation checksum evidence.")
		expected = _checksum(_surgery_evaluation_snapshot(doc))
		if str(doc.governance_checksum or "") != expected:
			frappe.throw("Surgery governance evaluation checksum is invalid.")
	if not doc.is_new():
		before = doc.get_doc_before_save()
		if before and _changed_fields(
			doc,
			before,
			(
				"source_system",
				"source_record_type",
				"source_record_id_hash",
				"procedure_code",
				"surgery_level",
				"surgery_time",
				"surgeon",
				"hospital",
				"campus",
				"department",
				"ward",
				"patient",
				"encounter",
				"responsible_staff",
			),
		):
			frappe.throw("Surgery source identity, clinical identity, and scope are immutable.")
		if before:
			_validate_surgery_projection_progression(doc, before)


def _validate_surgery_projection_progression(doc, before) -> None:
	before_phase = str(before.surgery_phase or "")
	after_phase = str(doc.surgery_phase or "")
	allowed = {
		"Scheduled": {"Scheduled", "Occurred", "Postoperative Finalized", "Cancelled"},
		"Occurred": {"Occurred", "Postoperative Finalized"},
		"Postoperative Finalized": {"Postoperative Finalized"},
		"Cancelled": {"Cancelled"},
	}
	if after_phase not in allowed.get(before_phase, set()):
		frappe.throw("Surgery projection phase cannot regress or cross a terminal state.")

	same_event = str(doc.event or "") == str(before.event or "")
	if same_event:
		if str(doc.record_snapshot_hash or "") != str(before.record_snapshot_hash or ""):
			frappe.throw("One immutable source event cannot produce different surgery snapshots.")
		if str(doc.source_version or "") != str(before.source_version or ""):
			frappe.throw("One immutable source event cannot change source_version.")
		return

	if get_datetime(doc.source_event_time) <= get_datetime(before.source_event_time):
		frappe.throw("New surgery source event versions must advance source_event_time.")
	if str(doc.source_version or "") == str(before.source_version or ""):
		frappe.throw("New surgery source events require a distinct source_version.")
	if str(doc.record_snapshot_hash or "") == str(before.record_snapshot_hash or ""):
		frappe.throw("New surgery source events require a distinct canonical snapshot.")
	if (
		before.source_finalized_at
		and doc.source_finalized_at
		and get_datetime(doc.source_finalized_at) < get_datetime(before.source_finalized_at)
	):
		frappe.throw("Surgery source_finalized_at cannot regress.")


def _evaluate_preoperative_release(
	values: Mapping[str, Any],
	event,
	*,
	decision_at=None,
) -> dict[str, Any]:
	"""Fail closed before surgery; an exception permits release but never proves compliance."""
	policy = _approved_policy_for_values(values, event)
	occurred_at = get_datetime(values.get("surgery_time"))
	_validate_occurrence_care_path(values, policy, occurred_at)
	source_hash = str(values.get("source_record_id_hash") or "")
	exception = _approved_exception(
		source_hash,
		policy,
		get_datetime(decision_at) if decision_at is not None else occurred_at,
	)
	exception_scopes = (
		set(_exception_scope_list(exception.requested_scopes_json, "requested_scopes_json"))
		if exception
		else set()
	)

	authorization = None
	qualification = None
	if "Authorization" not in exception_scopes:
		authorization = _approved_authorization_for_values(values, event, occurred_at)
		qualification = frappe.get_doc("IONE Staff Qualification", authorization.qualification)
		_validate_live_qualification(qualification, occurred_at, str(values.get("surgeon") or ""))
	mdt = None
	if int(policy.require_mdt or 0) and "MDT" not in exception_scopes:
		mdt = _completed_mdt(source_hash, policy, occurred_at)
	checklist = None
	if "Safety Checklist" not in exception_scopes:
		checklist = _completed_checklist(source_hash, policy, occurred_at)
	return {
		"procedure_policy": policy,
		"surgery_authorization": authorization,
		"staff_qualification": qualification,
		"mdt_record": mdt,
		"safety_checklist": checklist,
		"emergency_exception": exception,
		"release_state": "Released with Emergency Exception" if exception else "Released",
	}


def _evaluate_occurrence(
	values: Mapping[str, Any],
	event,
	*,
	structured_reason_codes: Sequence[str] = (),
) -> dict[str, Any]:
	"""Capture an occurred/finalized fact and classify it without suppressing the projection."""
	occurred_at = get_datetime(values.get("surgery_time"))
	source_hash = str(values.get("source_record_id_hash") or "")
	reason_codes = set(structured_reason_codes)
	blocked = False

	authorization = None
	qualification = None
	mdt = None
	checklist = None
	exception = None
	frozen_evidence = None
	frozen_candidate, frozen_invalid = _capture_governance_evidence(
		lambda: _frozen_occurrence_evidence(values),
		"FROZEN_OCCURRENCE_EVIDENCE_INVALID",
	)
	if frozen_invalid:
		reason_codes.add(frozen_invalid)
		blocked = True
	if frozen_candidate and (
		str(values.get("surgery_phase") or "") == "Postoperative Finalized"
		or str(frozen_candidate["event"] or "") == str(values.get("event") or "")
	):
		frozen_evidence = frozen_candidate

	if frozen_evidence:
		policy = frozen_evidence["procedure_policy"]
		authorization = frozen_evidence["surgery_authorization"]
		qualification = frozen_evidence["staff_qualification"]
		mdt = frozen_evidence["mdt_record"]
		checklist = frozen_evidence["safety_checklist"]
		exception = frozen_evidence["emergency_exception"]
		reason_codes.update(frozen_evidence["reason_codes"])
		blocked = blocked or frozen_evidence["governance_state"] == "Blocked"
		if policy:
			reason_codes.update(_finalized_care_path_reason_codes(values, policy))
	else:
		policy, policy_invalid = _capture_governance_evidence(
			lambda: _approved_policy_for_values(
				values,
				event,
				allow_historical_retired=True,
			),
			"PROCEDURE_POLICY_MISSING_AMBIGUOUS_OR_INVALID",
		)
		if policy_invalid:
			reason_codes.add(policy_invalid)
			blocked = True

	if policy and not frozen_evidence:
		reason_codes.update(_occurrence_care_path_reason_codes(values, policy, occurred_at))
		if str(values.get("surgery_phase") or "") == "Postoperative Finalized":
			reason_codes.update(_finalized_care_path_reason_codes(values, policy))

		exception, exception_invalid = _capture_governance_evidence(
			lambda: _approved_exception(source_hash, policy, occurred_at),
			"EMERGENCY_EXCEPTION_EVIDENCE_INVALID_OR_AMBIGUOUS",
		)
		if exception_invalid:
			reason_codes.add(exception_invalid)
			blocked = True
		if exception:
			scopes = _exception_scope_list(
				exception.requested_scopes_json,
				"requested_scopes_json",
			)
			reason_codes.add("EMERGENCY_EXCEPTION_USED")
			reason_codes.update(f"EMERGENCY_EXCEPTION_{_reason_token(scope)}" for scope in scopes)

		authorization, authorization_invalid = _capture_governance_evidence(
			lambda: _approved_authorization_for_values(
				values,
				event,
				occurred_at,
				allow_historical_retired=True,
			),
			"SURGEON_AUTHORIZATION_MISSING_AMBIGUOUS_OR_INVALID",
		)
		if authorization_invalid:
			reason_codes.add(authorization_invalid)
		if authorization:
			qualification, qualification_invalid = _capture_governance_evidence(
				lambda: _validated_qualification_for_occurrence(
					authorization,
					occurred_at,
					str(values.get("surgeon") or ""),
					allow_historical_retired=True,
				),
				"STAFF_QUALIFICATION_MISSING_OR_INVALID",
			)
			if qualification_invalid:
				reason_codes.add(qualification_invalid)

		if int(policy.require_mdt or 0):
			mdt, mdt_invalid = _capture_governance_evidence(
				lambda: _completed_mdt(source_hash, policy, occurred_at),
				"REQUIRED_MDT_MISSING_AMBIGUOUS_OR_INVALID",
			)
			if mdt_invalid:
				reason_codes.add(mdt_invalid)
		checklist, checklist_invalid = _capture_governance_evidence(
			lambda: _completed_checklist(source_hash, policy, occurred_at),
			"SAFETY_CHECKLIST_MISSING_AMBIGUOUS_OR_INVALID",
		)
		if checklist_invalid:
			reason_codes.add(checklist_invalid)

	ordered_reason_codes = sorted(reason_codes)
	governance_state = "Blocked" if blocked else ("Noncompliant" if ordered_reason_codes else "Passed")
	compliant = int(governance_state == "Passed")
	reason_codes_json = _canonical_json(ordered_reason_codes)
	evaluation = {
		"procedure_policy": policy.name if policy else "",
		"surgery_authorization": authorization.name if authorization else "",
		"staff_qualification": qualification.name if qualification else "",
		"mdt_record": mdt.name if mdt else "",
		"safety_checklist": checklist.name if checklist else "",
		"emergency_exception": exception.name if exception else "",
		"governance_state": governance_state,
		"governance_checked_at": now_datetime(),
		"governance_reason_codes_json": reason_codes_json,
		"compliant": compliant,
		"status": "Passed" if compliant else "Failed",
	}
	evaluation["governance_checksum"] = _checksum(
		_surgery_evaluation_values_snapshot(
			values=values,
			policy=policy,
			authorization=authorization,
			qualification=qualification,
			mdt=mdt,
			checklist=checklist,
			exception=exception,
			governance_state=governance_state,
			compliant=compliant,
			reason_codes_json=reason_codes_json,
		)
	)
	return evaluation


def _capture_governance_evidence(factory, reason_code: str):
	try:
		return factory(), None
	except frappe.ValidationError:
		return None, reason_code


def _validated_qualification_for_occurrence(
	authorization,
	occurred_at,
	surgeon: str,
	*,
	allow_historical_retired: bool = False,
):
	qualification = frappe.get_doc("IONE Staff Qualification", authorization.qualification)
	_validate_live_qualification(
		qualification,
		occurred_at,
		surgeon,
		allow_historical_retired=allow_historical_retired,
	)
	return qualification


def _frozen_occurrence_evidence(values: Mapping[str, Any]) -> dict[str, Any] | None:
	"""Reuse the occurrence-time evidence even if governed definitions were later retired."""
	names = frappe.get_all(
		"IONE Surgery QC",
		filters={
			"source_system": str(values.get("source_system") or ""),
			"source_record_type": str(values.get("source_record_type") or ""),
			"source_record_id_hash": str(values.get("source_record_id_hash") or ""),
		},
		pluck="name",
		limit_page_length=2,
	)
	if len(names) > 1:
		frappe.throw("Surgery source identity resolves to ambiguous prior projections.")
	if not names:
		return None
	prior = frappe.get_doc("IONE Surgery QC", names[0])
	if str(prior.surgery_phase or "") not in {"Occurred", "Postoperative Finalized"}:
		return None
	if str(prior.governance_state or "") not in FACTUAL_GOVERNANCE_STATES:
		frappe.throw("Prior surgery occurrence has no valid factual governance outcome.")
	if str(prior.governance_checksum or "") != _checksum(_surgery_evaluation_snapshot(prior)):
		frappe.throw("Prior surgery occurrence frozen evidence checksum is invalid.")

	def linked(doctype: str, fieldname: str):
		name = str(prior.get(fieldname) or "")
		return frappe.get_doc(doctype, name) if name else None

	return {
		"event": str(prior.event or ""),
		"procedure_policy": linked("IONE Surgery Procedure Policy", "procedure_policy"),
		"surgery_authorization": linked("IONE Surgery Authorization", "surgery_authorization"),
		"staff_qualification": linked("IONE Staff Qualification", "staff_qualification"),
		"mdt_record": linked("IONE Surgery MDT Record", "mdt_record"),
		"safety_checklist": linked("IONE Surgery Safety Checklist", "safety_checklist"),
		"emergency_exception": linked("IONE Surgery Emergency Exception", "emergency_exception"),
		"governance_state": str(prior.governance_state),
		"reason_codes": _governance_reason_codes(prior.governance_reason_codes_json),
	}


def _validate_structured_surgery_values(
	values: dict[str, Any],
	phase: str,
	*,
	capture_factual_noncompliance: bool = False,
) -> list[str]:
	reason_codes: set[str] = set()

	def noncompliance_or_throw(reason_code: str, message: str) -> None:
		if capture_factual_noncompliance:
			reason_codes.add(reason_code)
			return
		frappe.throw(message)

	surgery_time = get_datetime(values.get("surgery_time"))
	if phase == "Scheduled":
		return []
	if phase == "Cancelled":
		if str(values.get("cancellation_status") or "") != "Confirmed":
			frappe.throw("Cancelled surgery requires cancellation_status Confirmed.")
		_bounded_text(values.get("cancellation_reason_code"), "cancellation_reason_code", 100)
		if not values.get("cancelled_at") or get_datetime(values["cancelled_at"]) > get_datetime(
			values["source_finalized_at"]
		):
			frappe.throw("Cancelled surgery requires a source-anchored cancellation timestamp.")
		return []
	if not values.get("cancellation_status"):
		values["cancellation_status"] = "Not Cancelled"
		reason_codes.add("CANCELLATION_STATUS_MISSING")
	if str(values.get("cancellation_status") or "") == "Confirmed":
		noncompliance_or_throw(
			"OCCURRED_WITH_CONFIRMED_CANCELLATION",
			"Occurred surgery cannot also be recorded as cancelled.",
		)
	finalized_at = get_datetime(values["source_finalized_at"])
	if finalized_at < surgery_time:
		noncompliance_or_throw(
			"SOURCE_FINALIZED_BEFORE_OCCURRENCE",
			"source_finalized_at cannot precede surgery occurrence.",
		)

	stage_fields = {
		"preanesthesia_status": "preanesthesia_completed_at",
		"intraoperative_monitoring_status": "monitoring_completed_at",
		"recovery_status": "recovery_completed_at",
		"postoperative_followup_status": "postoperative_followup_completed_at",
	}
	for status_field, completed_field in stage_fields.items():
		status = str(values.get(status_field) or "")
		if not status and capture_factual_noncompliance:
			values[status_field] = "Pending"
			status = "Pending"
			reason_codes.add(f"{_reason_token(status_field)}_MISSING")
		if status not in CARE_STAGE_STATUSES:
			frappe.throw(f"{status_field} must be an explicit structured care-stage status.")
		if status == "Completed" and not values.get(completed_field):
			noncompliance_or_throw(
				f"{_reason_token(completed_field)}_MISSING",
				f"{completed_field} is required when {status_field} is Completed.",
			)
		if status != "Completed" and values.get(completed_field):
			noncompliance_or_throw(
				f"{_reason_token(status_field)}_TIMESTAMP_STATUS_CONFLICT",
				f"{completed_field} is only allowed for a Completed care stage.",
			)

	if (
		values.get("preanesthesia_completed_at")
		and get_datetime(values["preanesthesia_completed_at"]) >= surgery_time
	):
		noncompliance_or_throw(
			"PREANESTHESIA_NOT_COMPLETED_BEFORE_SURGERY",
			"Pre-anesthesia assessment must be completed before surgery.",
		)
	if values.get("monitoring_started_at") and get_datetime(values["monitoring_started_at"]) > surgery_time:
		noncompliance_or_throw(
			"INTRAOPERATIVE_MONITORING_STARTED_AFTER_OCCURRENCE",
			"Intraoperative monitoring must start no later than surgery occurrence.",
		)
	if values.get("monitoring_completed_at"):
		if not values.get("monitoring_started_at"):
			noncompliance_or_throw(
				"INTRAOPERATIVE_MONITORING_START_MISSING",
				"Completed intraoperative monitoring requires monitoring_started_at.",
			)
		elif get_datetime(values["monitoring_completed_at"]) < get_datetime(values["monitoring_started_at"]):
			noncompliance_or_throw(
				"INTRAOPERATIVE_MONITORING_COMPLETED_BEFORE_START",
				"Intraoperative monitoring completion cannot precede its start.",
			)
	for fieldname in ("recovery_completed_at", "postoperative_followup_completed_at"):
		if values.get(fieldname) and get_datetime(values[fieldname]) < surgery_time:
			noncompliance_or_throw(
				f"{_reason_token(fieldname)}_BEFORE_OCCURRENCE",
				f"{fieldname} cannot precede surgery occurrence.",
			)
	for fieldname in (
		"preanesthesia_completed_at",
		"monitoring_started_at",
		"monitoring_completed_at",
		"recovery_completed_at",
		"postoperative_followup_completed_at",
		"unplanned_return_at",
		"complication_at",
		"mortality_at",
	):
		if values.get(fieldname) and get_datetime(values[fieldname]) > finalized_at:
			noncompliance_or_throw(
				f"{_reason_token(fieldname)}_AFTER_SOURCE_FINALIZED",
				f"{fieldname} cannot follow source_finalized_at.",
			)

	for status_field, at_field, code_field in (
		("unplanned_return_status", "unplanned_return_at", None),
		("complication_status", "complication_at", "complication_code"),
		("mortality_status", "mortality_at", None),
	):
		status = str(values.get(status_field) or "")
		if not status and capture_factual_noncompliance:
			values[status_field] = "Not Observed"
			status = "Not Observed"
			reason_codes.add(f"{_reason_token(status_field)}_MISSING")
		if status not in OUTCOME_STATUSES:
			frappe.throw(f"{status_field} must be an explicit structured outcome status.")
		if status == "Yes":
			if not values.get(at_field):
				noncompliance_or_throw(
					f"{_reason_token(at_field)}_MISSING",
					f"{at_field} is required when {status_field} is Yes.",
				)
			if code_field:
				if values.get(code_field):
					_bounded_text(values.get(code_field), code_field, 100)
				else:
					noncompliance_or_throw(
						f"{_reason_token(code_field)}_MISSING",
						f"{code_field} is required when {status_field} is Yes.",
					)
		elif values.get(at_field) or (code_field and values.get(code_field)):
			noncompliance_or_throw(
				f"{_reason_token(status_field)}_EVIDENCE_CONFLICT",
				f"{status_field} evidence fields are only allowed when the status is Yes.",
			)
	unplanned = str(values.get("unplanned_surgery_status") or "")
	if not unplanned and capture_factual_noncompliance:
		values["unplanned_surgery_status"] = "Not Observed"
		unplanned = "Not Observed"
		reason_codes.add("UNPLANNED_SURGERY_STATUS_MISSING")
	if unplanned not in OUTCOME_STATUSES:
		frappe.throw("unplanned_surgery_status must be an explicit structured outcome status.")
	if unplanned == "Yes":
		if values.get("unplanned_surgery_reason_code"):
			_bounded_text(
				values.get("unplanned_surgery_reason_code"),
				"unplanned_surgery_reason_code",
				100,
			)
		else:
			noncompliance_or_throw(
				"UNPLANNED_SURGERY_REASON_CODE_MISSING",
				"unplanned_surgery_reason_code is required when unplanned_surgery_status is Yes.",
			)
	elif values.get("unplanned_surgery_reason_code"):
		noncompliance_or_throw(
			"UNPLANNED_SURGERY_REASON_STATUS_CONFLICT",
			"unplanned_surgery_reason_code is only allowed when unplanned_surgery_status is Yes.",
		)
	values["unplanned_surgery"] = int(unplanned == "Yes")
	values["unplanned_return"] = int(str(values.get("unplanned_return_status")) == "Yes")
	values["complication"] = int(str(values.get("complication_status")) == "Yes")
	values["mortality"] = int(str(values.get("mortality_status")) == "Yes")
	return sorted(reason_codes)


def _validate_occurrence_care_path(values: Mapping[str, Any], policy, occurred_at) -> None:
	required = set(_care_stage_list(policy.required_care_stages_json, "required_care_stages_json"))
	preoperative = {
		stage: _CARE_STAGE_FIELDS[stage]
		for stage in ("Pre-anesthesia Assessment", "Intraoperative Monitoring")
	}
	for stage, fieldname in preoperative.items():
		if stage in required and str(values.get(fieldname) or "") != "Completed":
			frappe.throw(f"Surgery is blocked: required care stage {stage} is not Completed.")
	if (
		"Intraoperative Monitoring" in required
		and get_datetime(values.get("monitoring_completed_at")) < occurred_at
	):
		# ``surgery_time`` is the source occurrence marker. Monitoring completion may
		# be equal or later, but it must never precede its source-defined operation.
		frappe.throw("Intraoperative monitoring completion precedes surgery occurrence.")


def _occurrence_care_path_reason_codes(
	values: Mapping[str, Any],
	policy,
	occurred_at,
) -> list[str]:
	required = set(_care_stage_list(policy.required_care_stages_json, "required_care_stages_json"))
	reasons = []
	for stage in ("Pre-anesthesia Assessment", "Intraoperative Monitoring"):
		if stage in required and str(values.get(_CARE_STAGE_FIELDS[stage]) or "") != "Completed":
			reasons.append(f"REQUIRED_{_CARE_STAGE_REASON_TOKENS[stage]}_NOT_COMPLETED")
	if (
		"Intraoperative Monitoring" in required
		and values.get("monitoring_completed_at")
		and get_datetime(values.get("monitoring_completed_at")) < occurred_at
	):
		reasons.append("INTRAOPERATIVE_MONITORING_COMPLETED_BEFORE_OCCURRENCE")
	return reasons


def _validate_finalized_care_path(values: Mapping[str, Any], policy) -> None:
	required = set(_care_stage_list(policy.required_care_stages_json, "required_care_stages_json"))
	for stage in required:
		if str(values.get(_CARE_STAGE_FIELDS[stage]) or "") != "Completed":
			frappe.throw(f"Postoperative finalization requires Completed care stage: {stage}.")
	if "Postoperative Follow-up" in required:
		deadline = get_datetime(values.get("surgery_time")) + timedelta(
			hours=int(policy.postoperative_followup_due_hours or 0)
		)
		if get_datetime(values.get("postoperative_followup_completed_at")) > deadline:
			frappe.throw("Postoperative follow-up completion exceeds the approved policy window.")


def _finalized_care_path_reason_codes(values: Mapping[str, Any], policy) -> list[str]:
	required = set(_care_stage_list(policy.required_care_stages_json, "required_care_stages_json"))
	reasons = []
	for stage in required:
		if str(values.get(_CARE_STAGE_FIELDS[stage]) or "") != "Completed":
			reasons.append(f"REQUIRED_{_CARE_STAGE_REASON_TOKENS[stage]}_NOT_COMPLETED_AT_FINALIZATION")
	if "Postoperative Follow-up" in required and values.get("postoperative_followup_completed_at"):
		deadline = get_datetime(values.get("surgery_time")) + timedelta(
			hours=int(policy.postoperative_followup_due_hours or 0)
		)
		if get_datetime(values.get("postoperative_followup_completed_at")) > deadline:
			reasons.append("POSTOPERATIVE_FOLLOWUP_EXCEEDED_POLICY_WINDOW")
	return reasons


def _review_governed_definition(
	doctype: str,
	name: str,
	decision: str,
	comment: str,
	validate_before_approval,
	snapshot,
) -> dict[str, str]:
	_require_post()
	actor = _require_named_actor(APPROVER_ROLES)
	if decision not in {"Approve", "Reject"}:
		frappe.throw("Decision must be Approve or Reject.")
	review_comment = _bounded_text(comment, "review_comment", 2_000)
	with frappe.db.advisory_lock(f"ione-qms:surgery-definition:{doctype}:{name}", timeout=5):
		doc = frappe.get_doc(doctype, name)
		slot_key = _definition_slot_key(doc)
		with frappe.db.advisory_lock(
			f"ione-qms:surgery-definition-slot:{slot_key}",
			timeout=5,
		):
			_require_scope_write(doc, actor)
			if str(doc.status or "") != "Draft":
				frappe.throw("Only a Draft surgery governance definition may be reviewed.")
			if str(doc.owner or "") == actor:
				frappe.throw("A definition author cannot approve or reject their own definition.")
			if decision == "Approve":
				validate_before_approval(doc)
				doc.status = "Approved"
				doc.approved_by = actor
				doc.approved_at = now_datetime()
				doc.approval_comment = review_comment
				doc.checksum = _checksum(snapshot(doc))
			else:
				doc.status = "Retired"
				doc.retired_by = actor
				doc.retired_at = now_datetime()
				doc.retirement_reason = review_comment
			doc.flags.ione_surgery_governance_transition = True
			doc.save(ignore_permissions=True)
	return {"name": str(doc.name), "status": str(doc.status), "checksum": str(doc.checksum or "")}


def _validate_qualification_before_approval(doc) -> None:
	_validate_period(doc.effective_from, doc.effective_to)
	_require_complete_organization(doc)
	_validate_staff_scope(doc)
	_reject_overlapping_qualification(doc)


def _validate_authorization_before_approval(doc) -> None:
	_validate_surgery_level(doc.surgery_level)
	_validate_period(doc.effective_from, doc.effective_to)
	_require_complete_organization(doc)
	_validate_staff_scope(doc)
	_validate_authorization_qualification(doc)
	_reject_overlapping_authorization(doc)


def _validate_policy_before_approval(doc) -> None:
	_validate_surgery_level(doc.surgery_level)
	_validate_period(doc.effective_from, doc.effective_to)
	_require_complete_organization(doc)
	_reject_overlapping_policy(doc)


def _validate_governed_transition(doc, *, immutable_fields: Sequence[str]) -> None:
	if doc.is_new():
		if str(doc.status or "Draft") != "Draft":
			frappe.throw("New surgery governance definitions must start in Draft.")
		if any(doc.get(fieldname) for fieldname in (*_APPROVAL_AUDIT_FIELDS, *_RETIREMENT_AUDIT_FIELDS)):
			frappe.throw("Draft governance definitions cannot assert approval or retirement audit fields.")
		return
	before = doc.get_doc_before_save()
	if not before:
		return
	before_status = str(before.status or "")
	after_status = str(doc.status or "")
	if before_status == "Draft":
		if after_status != "Draft" and not bool(
			getattr(doc.flags, "ione_surgery_governance_transition", False)
		):
			frappe.throw("Surgery governance approval must use the governed POST API.")
		if after_status == "Draft" and _changed_fields(
			doc,
			before,
			(*_APPROVAL_AUDIT_FIELDS, *_RETIREMENT_AUDIT_FIELDS),
		):
			frappe.throw("Draft governance definitions cannot modify managed audit fields.")
	elif before_status == "Approved":
		if _changed_fields(doc, before, (*immutable_fields, *_APPROVAL_AUDIT_FIELDS)):
			frappe.throw("Approved surgery governance definition fields are immutable.")
		if after_status != "Approved" and not (
			after_status == "Retired"
			and bool(getattr(doc.flags, "ione_surgery_governance_transition", False))
		):
			frappe.throw("Approved definitions may only transition to Retired through the POST API.")
		if after_status == "Approved" and _changed_fields(doc, before, _RETIREMENT_AUDIT_FIELDS):
			frappe.throw("Approved definitions cannot assert retirement audit fields.")
	elif before_status == "Retired":
		if _changed_fields(
			doc,
			before,
			(
				*immutable_fields,
				"status",
				*_APPROVAL_AUDIT_FIELDS,
				*_RETIREMENT_AUDIT_FIELDS,
			),
		):
			frappe.throw("Retired surgery governance definitions are immutable.")


def _validate_authorization_qualification(doc) -> None:
	if not doc.qualification:
		frappe.throw("Surgery authorization requires an approved staff qualification.")
	qualification = frappe.get_doc("IONE Staff Qualification", doc.qualification)
	if str(qualification.medical_staff or "") != str(doc.medical_staff or ""):
		frappe.throw("Authorization qualification belongs to a different medical staff record.")
	_validate_live_qualification(
		qualification,
		get_datetime(doc.effective_from),
		str(doc.medical_staff or ""),
	)
	if doc.effective_to and qualification.effective_to:
		if getdate(doc.effective_to) > getdate(qualification.effective_to):
			frappe.throw("Authorization validity cannot exceed the linked qualification.")
	if getdate(doc.effective_from) < getdate(qualification.effective_from):
		frappe.throw("Authorization validity cannot start before the linked qualification.")


def _validate_staff_scope(doc) -> None:
	if not doc.medical_staff:
		frappe.throw("medical_staff is required.")
	staff = frappe.db.get_value(
		"IONE Medical Staff",
		doc.medical_staff,
		["hospital", "campus", "department"],
		as_dict=True,
	)
	if not staff:
		frappe.throw("Surgery governance definition references an unknown medical staff record.")
	for fieldname in ("hospital", "campus", "department"):
		if str(doc.get(fieldname) or "") != str(staff.get(fieldname) or ""):
			frappe.throw(f"Medical staff {fieldname} does not match the governance definition scope.")


def _validate_staff_identity_in_scope(medical_staff: str, scope) -> None:
	staff_name = _bounded_text(medical_staff, "medical_staff", 140)
	staff = frappe.db.get_value(
		"IONE Medical Staff",
		staff_name,
		["hospital", "campus", "department", "practice_status"],
		as_dict=True,
	)
	if not staff or str(staff.practice_status or "") != "Active":
		frappe.throw("Surgery governance evidence requires an active medical staff identity.")
	for fieldname in ("hospital", "campus", "department"):
		if str(staff.get(fieldname) or "") != str(scope.get(fieldname) or ""):
			frappe.throw("Surgery governance participant scope does not match the surgery.")


def _validate_live_qualification(
	qualification,
	occurred_at,
	medical_staff: str,
	*,
	allow_historical_retired: bool = False,
) -> None:
	_validate_governed_definition_status_at(
		qualification,
		occurred_at,
		"surgery authorization qualification",
		allow_historical_retired=allow_historical_retired,
	)
	if str(qualification.medical_staff or "") != medical_staff:
		frappe.throw("Surgery qualification does not belong to the operating surgeon.")
	_validate_staff_identity_in_scope(medical_staff, qualification)
	_validate_date_contains(
		qualification.effective_from,
		qualification.effective_to,
		occurred_at,
		"staff qualification",
	)
	if str(qualification.checksum or "") != _checksum(_qualification_snapshot(qualification)):
		frappe.throw("Staff qualification checksum is invalid.")


def _approved_policy_for_surgery(surgery):
	return _approved_policy_for_values(
		{
			"procedure_code": surgery.procedure_code,
			"surgery_level": surgery.surgery_level,
			"surgery_time": surgery.surgery_time,
		},
		surgery,
	)


def _approved_policy_for_values(
	values: Mapping[str, Any],
	scope,
	*,
	allow_historical_retired: bool = False,
):
	procedure_code = _bounded_text(values.get("procedure_code"), "procedure_code", 100)
	level = str(values.get("surgery_level") or "").strip()
	_validate_surgery_level(level)
	at = get_datetime(values.get("surgery_time"))
	names = frappe.get_all(
		"IONE Surgery Procedure Policy",
		filters=[
			[
				"status",
				"in",
				["Approved", "Retired"] if allow_historical_retired else ["Approved"],
			],
			["hospital", "=", str(scope.hospital or "")],
			["campus", "=", str(scope.campus or "")],
			["department", "=", str(scope.department or "")],
			["procedure_code", "=", procedure_code],
			["effective_from", "<=", getdate(at)],
		],
		pluck="name",
		limit_page_length=100,
	)
	live_names = []
	for name in names:
		candidate = frappe.get_doc("IONE Surgery Procedure Policy", name)
		if candidate.effective_to and getdate(candidate.effective_to) < getdate(at):
			continue
		if _governed_definition_status_contains(
			candidate,
			at,
			allow_historical_retired=allow_historical_retired,
		):
			live_names.append(name)
	if len(live_names) != 1:
		frappe.throw("Surgery is blocked: exactly one approved hospital procedure-level policy is required.")
	policy = frappe.get_doc("IONE Surgery Procedure Policy", live_names[0])
	if str(policy.surgery_level or "") != level:
		frappe.throw("Surgery level does not match the approved hospital procedure policy.")
	_validate_policy_at(policy, at, allow_historical_retired=allow_historical_retired)
	return policy


def _validate_live_policy(policy, at) -> None:
	_validate_policy_at(policy, at, allow_historical_retired=False)


def _validate_policy_at(policy, at, *, allow_historical_retired: bool) -> None:
	_validate_governed_definition_status_at(
		policy,
		at,
		"surgery procedure policy",
		allow_historical_retired=allow_historical_retired,
	)
	_validate_date_contains(policy.effective_from, policy.effective_to, at, "surgery policy")
	if str(policy.checksum or "") != _checksum(_policy_snapshot(policy)):
		frappe.throw("Surgery procedure policy checksum is invalid.")


def _validate_governed_definition_status_at(
	doc,
	at,
	label: str,
	*,
	allow_historical_retired: bool,
) -> None:
	status = str(doc.status or "")
	if status not in {"Approved", "Retired"}:
		frappe.throw(f"{label} was not approved.")
	if status == "Retired" and not allow_historical_retired:
		frappe.throw(f"{label} is Retired.")
	if not _governed_definition_status_contains(
		doc,
		at,
		allow_historical_retired=allow_historical_retired,
	):
		frappe.throw(f"{label} was not approved at the surgery occurrence time.")


def _governed_definition_status_contains(
	doc,
	at,
	*,
	allow_historical_retired: bool,
) -> bool:
	if not doc.approved_at or get_datetime(doc.approved_at) > get_datetime(at):
		return False
	status = str(doc.status or "")
	if status == "Approved":
		return True
	return bool(
		status == "Retired"
		and allow_historical_retired
		and doc.retired_at
		and get_datetime(doc.retired_at) > get_datetime(at)
	)


def _approved_authorization_for_values(
	values: Mapping[str, Any],
	scope,
	at,
	*,
	allow_historical_retired: bool = False,
):
	names = frappe.get_all(
		"IONE Surgery Authorization",
		filters=[
			[
				"status",
				"in",
				["Approved", "Retired"] if allow_historical_retired else ["Approved"],
			],
			["medical_staff", "=", str(values.get("surgeon") or "")],
			["procedure_code", "=", str(values.get("procedure_code") or "")],
			["surgery_level", "=", str(values.get("surgery_level") or "")],
			["authorization_scope", "=", "Primary Surgeon"],
			["hospital", "=", str(scope.hospital or "")],
			["campus", "=", str(scope.campus or "")],
			["department", "=", str(scope.department or "")],
			["effective_from", "<=", getdate(at)],
		],
		pluck="name",
		limit_page_length=10,
	)
	live = []
	for name in names:
		doc = frappe.get_doc("IONE Surgery Authorization", name)
		if doc.effective_to and getdate(doc.effective_to) < getdate(at):
			continue
		if not _governed_definition_status_contains(
			doc,
			at,
			allow_historical_retired=allow_historical_retired,
		):
			continue
		if str(doc.checksum or "") != _checksum(_authorization_snapshot(doc)):
			frappe.throw("Surgery authorization checksum is invalid.")
		live.append(doc)
	if len(live) != 1:
		frappe.throw("Surgery is blocked: exactly one live approved surgeon authorization is required.")
	return live[0]


def _approved_exception(source_hash: str, policy, at):
	common_filters = [
		["surgery_source_id_hash", "=", source_hash],
		["procedure_policy", "=", policy.name],
		["valid_from", "<=", at],
		["valid_to", ">=", at],
		["reviewed_at", "<=", at],
	]
	names = [
		*frappe.get_all(
			"IONE Surgery Emergency Exception",
			filters=[*common_filters, ["status", "=", "Approved"]],
			pluck="name",
			limit_page_length=2,
		),
		*frappe.get_all(
			"IONE Surgery Emergency Exception",
			filters=[
				*common_filters,
				["status", "=", "Expired"],
				["expired_from_status", "=", "Approved"],
			],
			pluck="name",
			limit_page_length=2,
		),
	]
	names = sorted(set(names))
	eligible = []
	for name in names:
		doc = frappe.get_doc("IONE Surgery Emergency Exception", name)
		if not doc.reviewed_at or get_datetime(doc.reviewed_at) > get_datetime(at):
			continue
		if str(doc.status or "") == "Expired":
			if str(doc.expired_from_status or "") != "Approved":
				continue
			_validate_expired_exception(doc)
		if not _approved_exception_checksum_is_valid(doc):
			frappe.throw("Emergency exception checksum is invalid.")
		eligible.append(doc)
	if len(eligible) > 1:
		frappe.throw("Surgery is blocked by ambiguous emergency exception records.")
	return eligible[0] if eligible else None


def _completed_mdt(source_hash: str, policy, at):
	names = frappe.get_all(
		"IONE Surgery MDT Record",
		filters={
			"surgery_source_id_hash": source_hash,
			"procedure_policy": policy.name,
			"status": "Completed",
		},
		pluck="name",
		limit_page_length=2,
	)
	if len(names) != 1:
		frappe.throw("Level IV surgery is blocked: exactly one completed pre-operative MDT is required.")
	doc = frappe.get_doc("IONE Surgery MDT Record", names[0])
	if get_datetime(doc.completed_at) >= at or str(doc.conclusion or "") == "Do Not Proceed":
		frappe.throw("Level IV surgery MDT is late or does not authorize proceeding.")
	rows = _stored_mdt_participant_rows(doc.name)
	_validate_participants_against_policy(rows, policy, get_datetime(doc.completed_at))
	if str(doc.checksum or "") != _checksum(_mdt_snapshot(doc, rows)):
		frappe.throw("Surgery MDT checksum is invalid.")
	return doc


def _completed_checklist(source_hash: str, policy, at):
	names = frappe.get_all(
		"IONE Surgery Safety Checklist",
		filters={
			"surgery_source_id_hash": source_hash,
			"procedure_policy": policy.name,
			"status": "Completed",
		},
		pluck="name",
		limit_page_length=2,
	)
	if len(names) != 1:
		frappe.throw("Surgery is blocked: exactly one completed pre-operative safety checklist is required.")
	doc = frappe.get_doc("IONE Surgery Safety Checklist", names[0])
	if get_datetime(doc.completed_at) >= at:
		frappe.throw("Surgery safety checklist was not completed before occurrence.")
	required = _string_list(policy.required_check_items_json, "required_check_items_json")
	results = _check_results(doc.results_json)
	if set(results) != set(required) or any(value != "Pass" for value in results.values()):
		frappe.throw("Surgery safety checklist does not exactly satisfy the approved policy.")
	if str(doc.checksum or "") != _checksum(_checklist_snapshot(doc)):
		frappe.throw("Surgery safety checklist checksum is invalid.")
	return doc


def _reject_overlapping_qualification(doc) -> None:
	filters = [
		["name", "!=", str(doc.name or "")],
		["status", "=", "Approved"],
		["medical_staff", "=", str(doc.medical_staff or "")],
		["qualification_type", "=", str(doc.qualification_type or "")],
		["qualification_code", "=", str(doc.qualification_code or "")],
		["effective_from", "<=", doc.effective_to or "9999-12-31"],
	]
	for name in frappe.get_all(
		"IONE Staff Qualification",
		filters=filters,
		pluck="name",
		limit_page_length=100,
	):
		other_end = frappe.db.get_value("IONE Staff Qualification", name, "effective_to")
		if not other_end or getdate(other_end) >= getdate(doc.effective_from):
			frappe.throw("Approved staff qualification validity periods cannot overlap.")


def _reject_overlapping_authorization(doc) -> None:
	filters = [
		["name", "!=", str(doc.name or "")],
		["status", "=", "Approved"],
		["medical_staff", "=", str(doc.medical_staff or "")],
		["procedure_code", "=", str(doc.procedure_code or "")],
		["surgery_level", "=", str(doc.surgery_level or "")],
		["authorization_scope", "=", str(doc.authorization_scope or "")],
		["hospital", "=", str(doc.hospital or "")],
		["campus", "=", str(doc.campus or "")],
		["department", "=", str(doc.department or "")],
		["effective_from", "<=", doc.effective_to or "9999-12-31"],
	]
	for name in frappe.get_all(
		"IONE Surgery Authorization",
		filters=filters,
		pluck="name",
		limit_page_length=100,
	):
		other_end = frappe.db.get_value("IONE Surgery Authorization", name, "effective_to")
		if not other_end or getdate(other_end) >= getdate(doc.effective_from):
			frappe.throw("Approved surgery authorization validity periods cannot overlap.")


def _reject_overlapping_policy(doc) -> None:
	filters = [
		["name", "!=", str(doc.name or "")],
		["status", "=", "Approved"],
		["hospital", "=", str(doc.hospital or "")],
		["campus", "=", str(doc.campus or "")],
		["department", "=", str(doc.department or "")],
		["procedure_code", "=", str(doc.procedure_code or "")],
		["effective_from", "<=", doc.effective_to or "9999-12-31"],
	]
	for name in frappe.get_all(
		"IONE Surgery Procedure Policy",
		filters=filters,
		pluck="name",
		limit_page_length=100,
	):
		other_end = frappe.db.get_value("IONE Surgery Procedure Policy", name, "effective_to")
		if not other_end or getdate(other_end) >= getdate(doc.effective_from):
			frappe.throw("Approved procedure-level policy validity periods cannot overlap.")


def _validate_participants_against_policy(rows, policy, completion_time) -> None:
	if len(rows) < int(policy.minimum_mdt_participants or 0):
		frappe.throw("MDT participant count is below the explicit approved policy minimum.")
	roles = {str(row["participant_role"]) for row in rows}
	required_roles = set(_string_list(policy.required_mdt_roles_json, "required_mdt_roles_json"))
	if not required_roles.issubset(roles):
		frappe.throw("MDT participants do not cover every role required by the approved policy.")
	identities = set()
	for row in rows:
		identity = (str(row["medical_staff"]), str(row["participant_role"]))
		if identity in identities:
			frappe.throw("Duplicate MDT participant identity and role are not allowed.")
		identities.add(identity)
		_validate_staff_identity_in_scope(str(row["medical_staff"]), policy)
		if get_datetime(row["confirmed_at"]) > completion_time:
			frappe.throw("MDT participant confirmation cannot occur after MDT completion.")


def _participant_rows(value) -> list[dict[str, str]]:
	data = _json_value(value, "participants")
	if not isinstance(data, list):
		frappe.throw("participants must be a JSON array.")
	rows: list[dict[str, str]] = []
	for item in data:
		if not isinstance(item, Mapping):
			frappe.throw("Each MDT participant must be an object.")
		if set(item) != {
			"medical_staff",
			"participant_role",
			"confirmed_at",
			"evidence_reference",
		}:
			frappe.throw("Each MDT participant must contain the exact approved participant fields.")
		rows.append(
			{
				"medical_staff": _bounded_text(item["medical_staff"], "medical_staff", 140),
				"participant_role": _bounded_text(
					item["participant_role"],
					"participant_role",
					200,
				),
				"confirmed_at": str(get_datetime(item["confirmed_at"])),
				"evidence_reference": _bounded_text(
					item["evidence_reference"],
					"evidence_reference",
					2_000,
				),
			}
		)
	return sorted(
		rows,
		key=lambda row: (row["participant_role"], row["medical_staff"]),
	)


def _stored_mdt_participant_rows(mdt_name: str) -> list[dict[str, str]]:
	return [
		{
			"medical_staff": str(row.medical_staff),
			"participant_role": str(row.participant_role),
			"confirmed_at": str(get_datetime(row.confirmed_at)),
			"evidence_reference": str(row.evidence_reference or ""),
		}
		for row in frappe.get_all(
			"IONE Surgery MDT Participant",
			filters={"mdt_record": mdt_name},
			fields=[
				"medical_staff",
				"participant_role",
				"confirmed_at",
				"evidence_reference",
			],
			order_by="participant_role asc, medical_staff asc",
			limit_page_length=MAX_PARTICIPANTS + 1,
		)
	]


def _check_results(value) -> dict[str, str]:
	data = _json_value(value, "items")
	if isinstance(data, list):
		results: dict[str, str] = {}
		for item in data:
			if not isinstance(item, Mapping) or set(item) != {"item_code", "result"}:
				frappe.throw("Each checklist item must contain exactly item_code and result.")
			code = _bounded_text(item["item_code"], "item_code", 200)
			result = str(item["result"] or "").strip()
			if result not in {"Pass", "Fail", "Not Performed"}:
				frappe.throw("Unsupported checklist result.")
			if code in results:
				frappe.throw("Duplicate checklist item code.")
			results[code] = result
	elif isinstance(data, Mapping):
		results = {}
		for key, value in data.items():
			code = _bounded_text(key, "item_code", 200)
			result = str(value or "").strip()
			if result not in {"Pass", "Fail", "Not Performed"}:
				frappe.throw("Unsupported checklist result.")
			results[code] = result
	else:
		frappe.throw("items must be a JSON array or object.")
	if not results or len(results) > MAX_CHECK_ITEMS:
		frappe.throw("Checklist must contain between 1 and 100 unique items.")
	return dict(sorted(results.items()))


def _string_list(value, fieldname: str) -> list[str]:
	data = _json_value(value, fieldname)
	if data in (None, ""):
		return []
	if not isinstance(data, list):
		frappe.throw(f"{fieldname} must be a JSON array.")
	items = []
	for item in data:
		text = _bounded_text(item, fieldname, 200)
		if text in items:
			frappe.throw(f"{fieldname} cannot contain duplicates.")
		items.append(text)
	return sorted(items)


def _exception_scope_list(value, fieldname: str) -> list[str]:
	items = _string_list(value, fieldname)
	if any(item not in EXCEPTION_SCOPES for item in items):
		frappe.throw(f"{fieldname} contains an unsupported emergency exception scope.")
	return items


def _care_stage_list(value, fieldname: str) -> list[str]:
	items = _string_list(value, fieldname)
	if any(item not in CARE_STAGES for item in items):
		frappe.throw(f"{fieldname} contains an unsupported surgery care stage.")
	return items


def _governance_reason_codes(value) -> list[str]:
	items = _string_list(value, "governance_reason_codes_json")
	if len(items) > 100:
		frappe.throw("governance_reason_codes_json cannot contain more than 100 reason codes.")
	for item in items:
		if item != _reason_token(item) or len(item) > 100:
			frappe.throw("Surgery governance reason codes must be stable uppercase tokens.")
	return items


def _json_value(value, fieldname: str):
	if isinstance(value, str):
		if len(value.encode("utf-8")) > MAX_JSON_BYTES:
			frappe.throw(f"{fieldname} exceeds the 64 KiB limit.")
		try:
			return json.loads(value)
		except TypeError, ValueError:
			frappe.throw(f"{fieldname} must contain valid JSON.")
	return value


def _qualification_snapshot(doc) -> dict[str, Any]:
	return {fieldname: doc.get(fieldname) for fieldname in _QUALIFICATION_IMMUTABLE}


def _authorization_snapshot(doc) -> dict[str, Any]:
	return {fieldname: doc.get(fieldname) for fieldname in _AUTHORIZATION_IMMUTABLE}


def _policy_snapshot(doc) -> dict[str, Any]:
	return {fieldname: doc.get(fieldname) for fieldname in _POLICY_IMMUTABLE}


def _mdt_snapshot(doc, participants=None) -> dict[str, Any]:
	return {
		"mdt_key": doc.mdt_key,
		"surgery_qc": doc.surgery_qc,
		"surgery_source_id_hash": doc.surgery_source_id_hash,
		"procedure_policy": doc.procedure_policy,
		**_scope_values(doc),
		"meeting_at": doc.meeting_at,
		"completed_at": doc.completed_at,
		"chair": doc.chair,
		"conclusion": doc.conclusion,
		"conclusion_summary": doc.conclusion_summary,
		"evidence_reference": doc.evidence_reference,
		"participant_count": doc.participant_count,
		"participants": participants,
		"recorded_by": doc.recorded_by,
		"recorded_at": doc.recorded_at,
		"status": doc.status,
	}


def _participant_snapshot(doc) -> dict[str, Any]:
	return {
		"participant_key": doc.participant_key,
		"mdt_record": doc.mdt_record,
		"surgery_qc": doc.surgery_qc,
		"surgery_source_id_hash": doc.surgery_source_id_hash,
		**_scope_values(doc),
		"medical_staff": doc.medical_staff,
		"participant_role": doc.participant_role,
		"attended": int(doc.attended or 0),
		"confirmed_at": doc.confirmed_at,
		"evidence_reference": doc.evidence_reference,
		"recorded_by": doc.recorded_by,
		"recorded_at": doc.recorded_at,
	}


def _checklist_snapshot(doc) -> dict[str, Any]:
	return {
		"checklist_key": doc.checklist_key,
		"surgery_qc": doc.surgery_qc,
		"surgery_source_id_hash": doc.surgery_source_id_hash,
		"procedure_policy": doc.procedure_policy,
		**_scope_values(doc),
		"required_items_json": _canonical_json(_string_list(doc.required_items_json, "required_items_json")),
		"results_json": _canonical_json(_check_results(doc.results_json)),
		"completed_by": doc.completed_by,
		"completed_at": doc.completed_at,
		"evidence_reference": doc.evidence_reference,
		"status": doc.status,
	}


def _exception_request_snapshot(doc) -> dict[str, Any]:
	return {
		"surgery_qc": doc.surgery_qc,
		"surgery_source_id_hash": doc.surgery_source_id_hash,
		"procedure_policy": doc.procedure_policy,
		**_scope_values(doc),
		"requested_scopes_json": _canonical_json(
			_exception_scope_list(doc.requested_scopes_json, "requested_scopes_json")
		),
		"reason_code": doc.reason_code,
		"reason": doc.reason,
		"requested_by": doc.requested_by,
		"requested_at": doc.requested_at,
		"valid_from": doc.valid_from,
		"valid_to": doc.valid_to,
	}


def _exception_snapshot(doc, *, status_override: str | None = None) -> dict[str, Any]:
	return {
		"exception_key": doc.exception_key,
		**_exception_request_snapshot(doc),
		"status": status_override if status_override is not None else doc.status,
		"reviewed_by": doc.reviewed_by,
		"reviewed_at": doc.reviewed_at,
		"review_comment": doc.review_comment,
	}


def _approved_exception_checksum_is_valid(doc) -> bool:
	status = str(doc.status or "")
	if status == "Approved":
		expected = _exception_snapshot(doc)
	elif status == "Expired" and str(doc.expired_from_status or "") == "Approved":
		expected = _exception_snapshot(doc, status_override="Approved")
	else:
		return False
	return bool(doc.checksum) and str(doc.checksum) == _checksum(expected)


def _exception_expiration_snapshot(doc) -> dict[str, Any]:
	expired_from_status = str(doc.expired_from_status or "")
	return {
		"exception_key": str(doc.exception_key or ""),
		"request": _exception_request_snapshot(doc),
		"status": "Expired",
		"expired_from_status": expired_from_status,
		"expired_at": doc.expired_at,
		"approval_checksum": (str(doc.checksum or "") if expired_from_status == "Approved" else None),
	}


def _validate_expired_exception(doc) -> None:
	expired_from_status = str(doc.expired_from_status or "")
	if expired_from_status not in {"Pending", "Approved"}:
		frappe.throw("Expired emergency exceptions require their exact prior status.")
	if not doc.expired_at or get_datetime(doc.expired_at) < get_datetime(doc.valid_to):
		frappe.throw("Emergency exception expiry must occur at or after valid_to.")
	if expired_from_status == "Approved":
		if not doc.reviewed_by or not doc.reviewed_at or not doc.review_comment:
			frappe.throw("Expired approved exceptions require accountable approval evidence.")
		if not _approved_exception_checksum_is_valid(doc):
			frappe.throw("Expired emergency exception approval checksum is invalid.")
	else:
		if any(
			doc.get(fieldname) for fieldname in ("reviewed_by", "reviewed_at", "review_comment", "checksum")
		):
			frappe.throw("An exception expired from Pending cannot contain approval evidence.")
	if not doc.expiration_checksum or str(doc.expiration_checksum) != _checksum(
		_exception_expiration_snapshot(doc)
	):
		frappe.throw("Emergency exception expiration checksum is invalid.")


def _surgery_evaluation_snapshot(doc) -> dict[str, Any]:
	policy = str(doc.procedure_policy or "")
	authorization = str(doc.surgery_authorization or "")
	qualification = str(doc.staff_qualification or "")
	mdt = str(doc.mdt_record or "")
	checklist = str(doc.safety_checklist or "")
	exception = str(doc.emergency_exception or "")
	return {
		"source_record_id_hash": doc.source_record_id_hash,
		"record_snapshot_hash": doc.record_snapshot_hash,
		"surgery_time": doc.surgery_time,
		"policy": policy or None,
		"policy_checksum": (
			frappe.db.get_value(
				"IONE Surgery Procedure Policy",
				policy,
				"checksum",
			)
			if policy
			else None
		),
		"authorization": authorization or None,
		"authorization_checksum": (
			frappe.db.get_value("IONE Surgery Authorization", authorization, "checksum")
			if authorization
			else None
		),
		"qualification": qualification or None,
		"qualification_checksum": (
			frappe.db.get_value("IONE Staff Qualification", qualification, "checksum")
			if qualification
			else None
		),
		"mdt": mdt or None,
		"mdt_checksum": (frappe.db.get_value("IONE Surgery MDT Record", mdt, "checksum") if mdt else None),
		"checklist": checklist or None,
		"checklist_checksum": (
			frappe.db.get_value("IONE Surgery Safety Checklist", checklist, "checksum") if checklist else None
		),
		"exception": exception or None,
		"exception_checksum": (
			frappe.db.get_value("IONE Surgery Emergency Exception", exception, "checksum")
			if exception
			else None
		),
		"governance_state": str(doc.governance_state or ""),
		"compliant": int(doc.compliant or 0),
		"governance_reason_codes_json": _canonical_json(
			_governance_reason_codes(doc.governance_reason_codes_json)
		),
	}


def _surgery_evaluation_values_snapshot(
	*,
	values: Mapping[str, Any],
	policy,
	authorization,
	qualification,
	mdt,
	checklist,
	exception,
	governance_state: str,
	compliant: int,
	reason_codes_json: str,
) -> dict[str, Any]:
	return {
		"source_record_id_hash": values.get("source_record_id_hash"),
		"record_snapshot_hash": values.get("record_snapshot_hash"),
		"surgery_time": get_datetime(values.get("surgery_time")),
		"policy": policy.name if policy else None,
		"policy_checksum": policy.checksum if policy else None,
		"authorization": authorization.name if authorization else None,
		"authorization_checksum": authorization.checksum if authorization else None,
		"qualification": qualification.name if qualification else None,
		"qualification_checksum": qualification.checksum if qualification else None,
		"mdt": mdt.name if mdt else None,
		"mdt_checksum": mdt.checksum if mdt else None,
		"checklist": checklist.name if checklist else None,
		"checklist_checksum": checklist.checksum if checklist else None,
		"exception": exception.name if exception else None,
		"exception_checksum": exception.checksum if exception else None,
		"governance_state": governance_state,
		"compliant": int(compliant),
		"governance_reason_codes_json": _canonical_json(_governance_reason_codes(reason_codes_json)),
	}


def _scope_values(doc) -> dict[str, str]:
	return {
		"hospital": str(doc.get("hospital") or ""),
		"campus": str(doc.get("campus") or ""),
		"department": str(doc.get("department") or ""),
	}


def _definition_slot_key(doc) -> str:
	common = {"doctype": doc.doctype, **_scope_values(doc)}
	if doc.doctype == "IONE Staff Qualification":
		common.update(
			{
				"medical_staff": doc.medical_staff,
				"qualification_type": doc.qualification_type,
				"qualification_code": doc.qualification_code,
			}
		)
	elif doc.doctype == "IONE Surgery Authorization":
		common.update(
			{
				"medical_staff": doc.medical_staff,
				"procedure_code": doc.procedure_code,
				"surgery_level": doc.surgery_level,
				"authorization_scope": doc.authorization_scope,
			}
		)
	elif doc.doctype == "IONE Surgery Procedure Policy":
		common["procedure_code"] = doc.procedure_code
	else:
		frappe.throw("Unsupported surgery governance definition type.")
	return _checksum(common)


def _require_complete_organization(doc) -> None:
	if not doc.hospital or not doc.campus or not doc.department:
		frappe.throw("Hospital, campus, and department are all required.")


def _validate_period(start, end) -> None:
	if not start:
		frappe.throw("effective_from is required.")
	if end and getdate(end) < getdate(start):
		frappe.throw("effective_to cannot precede effective_from.")


def _validate_date_contains(start, end, at, label: str) -> None:
	value = getdate(at)
	if value < getdate(start) or (end and value > getdate(end)):
		frappe.throw(f"Surgery occurrence is outside the approved {label} validity period.")


def _validate_surgery_level(value) -> None:
	if str(value or "").strip() not in SURGERY_LEVELS:
		frappe.throw("Surgery level must be an explicitly configured Level I, II, III, or IV.")


def _validate_status(value, allowed, label: str) -> None:
	if str(value or "") not in allowed:
		frappe.throw(f"Unsupported {label} status.")


def _validate_approval_audit(doc) -> None:
	if not doc.approved_by or not doc.approved_at or not doc.approval_comment:
		frappe.throw("Approved governance definitions require approver, time, and comment.")


def _require_named_governance_actor_on_create(doc) -> None:
	if not doc.is_new():
		return
	actor = str(frappe.session.user or "")
	if actor in {"", "Guest", "Administrator"}:
		frappe.throw(
			"A named accountable business user must create surgery governance definitions.",
			frappe.PermissionError,
		)
	if not set(frappe.get_roles(actor)).intersection(AUTHOR_ROLES):
		frappe.throw("Current user cannot author surgery governance definitions.", frappe.PermissionError)


def _require_named_actor(roles) -> str:
	actor = str(frappe.session.user or "")
	if actor in {"", "Guest", "Administrator"}:
		frappe.throw(
			"A named accountable business user is required.",
			frappe.PermissionError,
		)
	if not set(frappe.get_roles(actor)).intersection(roles):
		frappe.throw("Current user lacks the required surgery governance role.", frappe.PermissionError)
	return actor


def _require_scope_write(doc, actor: str) -> None:
	context = get_access_context(actor)
	if context.has_global_clinical_write:
		return
	require_scope_read(
		hospital=str(doc.get("hospital") or ""),
		campus=str(doc.get("campus") or ""),
		department=str(doc.get("department") or ""),
		user=actor,
		target_doctype=str(doc.doctype),
	)


def _locked_surgery(name: str):
	with frappe.db.advisory_lock(f"ione-qms:surgery-anchor:{name}", timeout=5):
		return frappe.get_doc("IONE Surgery QC", name)


def _require_surgery_pending(surgery, *, at=None) -> None:
	checked_at = get_datetime(at) if at is not None else now_datetime()
	if str(surgery.surgery_phase or "") != "Scheduled":
		frappe.throw("Pre-operative governance evidence may only be recorded for Scheduled surgery.")
	if get_datetime(surgery.surgery_time) <= checked_at:
		frappe.throw("Pre-operative governance evidence cannot be created after surgery occurrence.")


def _active_record_exists(doctype: str, source_hash: str) -> bool:
	return bool(
		frappe.db.exists(
			doctype,
			{
				"surgery_source_id_hash": source_hash,
				"status": "Completed",
			},
		)
	)


def _validate_materialized_append_only(doc, label: str) -> None:
	if doc.is_new():
		if not bool(getattr(doc.flags, "ione_surgery_governance_materializer", False)):
			frappe.throw(
				f"{label} may only be created through the governed POST API.",
				frappe.PermissionError,
			)
		return
	before = doc.get_doc_before_save()
	if before:
		changed = [
			field.fieldname
			for field in doc.meta.fields
			if doc.get(field.fieldname) != before.get(field.fieldname)
		]
		if changed:
			frappe.throw(f"{label} is immutable after creation.")


def _require_post() -> None:
	request = getattr(frappe.local, "request", None)
	if request is not None and str(getattr(request, "method", "")).upper() != "POST":
		frappe.throw("This governed operation requires HTTP POST.", frappe.PermissionError)


def _bounded_text(value, fieldname: str, maximum: int) -> str:
	if not isinstance(value, str):
		frappe.throw(f"{fieldname} must be text.")
	text = value.strip()
	if not text or len(text) > maximum:
		frappe.throw(f"{fieldname} must contain between 1 and {maximum} characters.")
	return text


def _changed_fields(doc, before, fields: Sequence[str]) -> tuple[str, ...]:
	return tuple(fieldname for fieldname in fields if doc.get(fieldname) != before.get(fieldname))


def _canonical_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)


def _reason_token(value: Any) -> str:
	text = str(value or "").strip().upper()
	return "_".join(
		filter(None, "".join(character if character.isalnum() else "_" for character in text).split("_"))
	)


def _checksum(value: Any) -> str:
	return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
