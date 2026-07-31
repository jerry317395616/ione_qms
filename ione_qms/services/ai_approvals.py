from __future__ import annotations

import hashlib
import hmac

import frappe

APPROVAL_CONTENT_REDACTED = "[REDACTED BY IONE INPUT RETENTION]"
_FINAL_TASK_STATUSES = frozenset({"Completed", "Failed", "Rejected", "Cancelled", "Reviewed"})
_PROVENANCE_FIELDS = frozenset(
	{
		"approval_key",
		"task",
		"policy",
		"flow_session",
		"flow_run",
		"run_iteration",
		"question_key",
		"tool_slug",
		"arguments_hash",
		"arguments_json",
		"question_hash",
		"question_prompt_hash",
		"question_prompt",
		"hospital",
		"campus",
		"department",
		"ward",
		"requested_at",
		"expires_at",
	}
)
_REVIEW_FIELDS = frozenset(
	{
		"status",
		"decision",
		"reviewed_by",
		"reviewed_at",
		"review_comment",
		"consumed_at",
		"resume_status",
	}
)


def validate_ai_tool_approval(doc, method: str | None = None) -> None:
	del method
	previous = doc.get_doc_before_save()
	create_flag = bool(getattr(frappe.flags, "ione_ai_approval_create", False))
	review_flag = bool(getattr(frappe.flags, "ione_ai_approval_review", False))
	resume_flag = bool(getattr(frappe.flags, "ione_ai_approval_resume", False))
	expire_flag = bool(getattr(frappe.flags, "ione_ai_approval_expire", False))
	if previous is None:
		if not create_flag:
			frappe.throw(
				"AI Tool Approvals may only be created by the governed orchestrator.",
				frappe.PermissionError,
			)
		if doc.get("status") != "Pending" or doc.get("decision"):
			frappe.throw("New AI Tool Approvals must start Pending without a decision")
		for fieldname in _PROVENANCE_FIELDS:
			if fieldname not in {"hospital", "campus", "department", "ward"} and not doc.get(fieldname):
				frappe.throw(f"AI Tool Approval requires {fieldname}")
		_validate_proposed_content_hashes(doc)
		return
	changed_provenance = {
		fieldname for fieldname in _PROVENANCE_FIELDS if doc.get(fieldname) != previous.get(fieldname)
	}
	changed_review = {
		fieldname for fieldname in _REVIEW_FIELDS if doc.get(fieldname) != previous.get(fieldname)
	}
	expected_retention_capability = f"{doc.doctype}:{doc.name}"
	if getattr(frappe.flags, "ione_ai_approval_retention", None) == expected_retention_capability:
		_validate_retention_transition(doc, previous, changed_provenance, changed_review)
		return
	if changed_provenance:
		frappe.throw("AI Tool Approval provenance and proposed arguments are immutable")
	if not changed_review:
		return
	if review_flag:
		if previous.get("status") != "Pending" or doc.get("status") != "Reviewed":
			frappe.throw("AI Tool Approval review must transition Pending to Reviewed")
		if doc.get("decision") not in {"Approved", "Rejected"}:
			frappe.throw("AI Tool Approval decision must be Approved or Rejected")
		if not doc.get("reviewed_by") or not doc.get("reviewed_at"):
			frappe.throw("AI Tool Approval review requires reviewer identity and time")
		if len(str(doc.get("review_comment") or "").strip()) < 5:
			frappe.throw("AI Tool Approval review requires a comment of at least five characters")
		return
	if resume_flag:
		if previous.get("status") != "Reviewed" or doc.get("status") != "Consumed":
			frappe.throw("AI Tool Approval consumption must transition Reviewed to Consumed")
		if doc.get("decision") != previous.get("decision") or not doc.get("consumed_at"):
			frappe.throw("AI Tool Approval consumption cannot change its reviewed decision")
		return
	if expire_flag:
		if previous.get("status") != "Pending" or doc.get("status") != "Expired":
			frappe.throw("Only a Pending AI Tool Approval may expire")
		if doc.get("decision"):
			frappe.throw("Expired AI Tool Approvals cannot carry a decision")
		return
	frappe.throw(
		"AI Tool Approval state may only change through governed review, resume, or expiry.",
		frappe.PermissionError,
	)


def _validate_retention_transition(doc, previous, changed_provenance, changed_review) -> None:
	allowed_fields = {"arguments_json", "question_prompt"}
	if changed_review or not changed_provenance or not changed_provenance.issubset(allowed_fields):
		frappe.throw("AI Tool Approval retention may only redact proposed content")
	previous_markers = {
		fieldname for fieldname in allowed_fields if previous.get(fieldname) == APPROVAL_CONTENT_REDACTED
	}
	if previous_markers and previous_markers != allowed_fields:
		frappe.throw("AI Tool Approval has a partial or unauthorized retention marker")
	if previous.get("status") not in {"Consumed", "Expired"} or doc.get("status") != previous.get("status"):
		frappe.throw("Only terminal AI Tool Approvals may be redacted")
	task_status = frappe.db.get_value("IONE AI Analysis Task", doc.get("task"), "status")
	if task_status not in _FINAL_TASK_STATUSES:
		frappe.throw("AI Tool Approval content cannot be redacted before its task is terminal")
	for fieldname, hash_field in (
		("arguments_json", "arguments_hash"),
		("question_prompt", "question_prompt_hash"),
	):
		if doc.get(fieldname) != APPROVAL_CONTENT_REDACTED:
			frappe.throw("AI Tool Approval retention must use the canonical redaction marker")
		if fieldname not in changed_provenance:
			continue
		original = str(previous.get(fieldname) or "")
		expected_hash = str(previous.get(hash_field) or "").lower()
		actual_hash = hashlib.sha256(original.encode()).hexdigest()
		if len(expected_hash) != 64 or not hmac.compare_digest(actual_hash, expected_hash):
			frappe.throw(f"AI Tool Approval {fieldname} failed its retention hash check")


def _validate_proposed_content_hashes(doc) -> None:
	for fieldname, hash_field in (
		("arguments_json", "arguments_hash"),
		("question_prompt", "question_prompt_hash"),
	):
		content = str(doc.get(fieldname) or "")
		expected_hash = str(doc.get(hash_field) or "").lower()
		actual_hash = hashlib.sha256(content.encode()).hexdigest()
		if (
			content == APPROVAL_CONTENT_REDACTED
			or len(expected_hash) != 64
			or not hmac.compare_digest(actual_hash, expected_hash)
		):
			frappe.throw(f"AI Tool Approval {fieldname} does not match its supplied hash")
