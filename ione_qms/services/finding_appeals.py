from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager

import frappe
from frappe.model.workflow import apply_workflow
from frappe.utils import now_datetime

from ione_qms.permissions import finding_permission

APPEAL_DOCTYPE = "IONE QC Finding Appeal"
APPEAL_EVIDENCE_DOCTYPE = "IONE QC Finding Appeal Evidence"
APPEAL_SUBMITTER_ROLES = frozenset(
	{
		"IONE Physician",
		"IONE Department Director",
		"IONE Department QC Officer",
	}
)
APPEAL_REVIEWER_ROLES = frozenset({"IONE QC Reviewer", "IONE Medical Affairs"})
APPEAL_READ_ROLES = APPEAL_SUBMITTER_ROLES | APPEAL_REVIEWER_ROLES | frozenset({"IONE QMS Auditor"})
APPEAL_TYPES = frozenset(
	{
		"Factual Dispute",
		"Rule Applicability",
		"Clinical Exception",
		"Evidence Dispute",
		"Other",
	}
)

APPEAL_FINDING_ACTION = "Appeal Finding"
WITHDRAW_APPEAL_ACTION = "Withdraw Finding Appeal"
APPROVE_APPEAL_ACTION = "Approve Finding Appeal"
REJECT_APPEAL_ACTION = "Reject Finding Appeal"

_APPEAL_PROVENANCE_FIELDS = frozenset(
	{
		"finding",
		"appeal_type",
		"reason",
		"evidence_summary",
		"evidence_record",
		"evidence_file",
		"evidence_content_hash",
		"hospital",
		"campus",
		"department",
		"ward",
		"patient",
		"encounter",
		"responsible_staff",
		"submission_hash",
		"submitted_by",
		"submitted_at",
	}
)
_PROTECTED_FILE_FIELDS = frozenset(
	{
		"attached_to_doctype",
		"attached_to_name",
		"attached_to_field",
		"content_hash",
		"file_name",
		"file_size",
		"file_type",
		"file_url",
		"folder",
		"is_attachments_folder",
		"is_folder",
		"is_home_folder",
		"is_private",
		"old_parent",
		"thumbnail_url",
		"uploaded_to_dropbox",
		"uploaded_to_google_drive",
	}
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_EVIDENCE_FILE_SIZE = 20 * 1024 * 1024
_APPEAL_STATE_FIELDS = frozenset(
	{
		"active_key",
		"status",
		"reviewed_by",
		"reviewed_at",
		"review_comment",
		"withdrawn_by",
		"withdrawn_at",
		"withdraw_comment",
	}
)
_FINDING_SCOPE_FIELDS = (
	"hospital",
	"campus",
	"department",
	"ward",
	"patient",
	"encounter",
	"responsible_staff",
)
_FINDING_APPEAL_TRANSITIONS = {
	("Confirmed", "Appealed"): frozenset({"Submitted"}),
	("Appealed", "Confirmed"): frozenset({"Withdrawn"}),
	("Appealed", "Appeal Approved"): frozenset({"Approved"}),
	("Appealed", "Appeal Rejected"): frozenset({"Rejected"}),
}


def submit_finding_appeal(
	finding: str,
	appeal_type: str,
	reason: str,
	evidence_summary: str | None = None,
	evidence_file: str | None = None,
	evidence_content_hash: str | None = None,
) -> dict[str, str]:
	"""Create one structured appeal and atomically move its Finding to Appealed."""
	user = _require_named_actor(APPEAL_SUBMITTER_ROLES)
	normalized = _normalized_submission(
		appeal_type=appeal_type,
		reason=reason,
		evidence_summary=evidence_summary,
		evidence_file=evidence_file,
		evidence_content_hash=evidence_content_hash,
	)
	submission_hash = _submission_hash(
		finding=finding,
		submitted_by=user,
		**normalized,
	)

	with _finding_appeal_lock(finding):
		with _atomic_appeal_change(_appeal_savepoint("submit", finding)):
			finding_doc = frappe.get_doc("IONE QC Finding", finding, for_update=True)
			_require_finding_access(finding_doc, user)
			existing_name = _scalar_name(
				frappe.db.get_value(
					APPEAL_DOCTYPE,
					{"active_key": finding_doc.name},
					"name",
					for_update=True,
				)
			)
			if existing_name:
				existing = frappe.get_doc(APPEAL_DOCTYPE, existing_name, for_update=True)
				if (
					existing.status == "Submitted"
					and existing.active_key == finding_doc.name
					and existing.finding == finding_doc.name
					and existing.submitted_by == user
					and existing.submission_hash == submission_hash
					and finding_doc.status == "Appealed"
				):
					return {
						"appeal": existing.name,
						"finding": finding_doc.name,
						"status": existing.status,
					}
				frappe.throw("This quality finding already has an active appeal")
			if finding_doc.status != "Confirmed":
				frappe.throw("Only a Confirmed quality finding may be appealed")

			evidence = _create_appeal_evidence(
				finding_doc,
				submitted_by=user,
				submission_hash=submission_hash,
				evidence_file=normalized["evidence_file"],
				evidence_content_hash=normalized["evidence_content_hash"],
			)
			appeal = frappe.get_doc(
				{
					"doctype": APPEAL_DOCTYPE,
					"finding": finding_doc.name,
					"active_key": finding_doc.name,
					**normalized,
					"evidence_record": evidence.name if evidence else None,
					**{fieldname: finding_doc.get(fieldname) for fieldname in _FINDING_SCOPE_FIELDS},
					"submission_hash": submission_hash,
					"status": "Submitted",
					"submitted_by": user,
					"submitted_at": now_datetime(),
				}
			)
			with _appeal_document_capability(
				"ione_finding_appeal_create",
				{
					"finding": finding_doc.name,
					"submitted_by": user,
					"submission_hash": submission_hash,
					"evidence_record": evidence.name if evidence else "",
				},
			):
				appeal.insert(ignore_permissions=True)
			finding_doc = _apply_finding_appeal_transition(
				finding_doc,
				appeal,
				action=APPEAL_FINDING_ACTION,
				target_status="Appealed",
			)
	return {"appeal": appeal.name, "finding": finding_doc.name, "status": appeal.status}


def discard_unbound_finding_appeal_evidence(
	evidence_file: str,
	evidence_content_hash: str,
) -> dict[str, str]:
	"""Delete a failed-dialog upload only while it is still exact, owned, and unbound."""
	user = _require_named_actor(APPEAL_SUBMITTER_ROLES)
	file_name = str(evidence_file or "").strip()
	content_hash = str(evidence_content_hash or "").strip().lower()
	_validate_submission_values(
		appeal_type="Other",
		reason="Discard an unused appeal evidence upload.",
		evidence_summary="",
		evidence_file=file_name,
		evidence_content_hash=content_hash,
	)
	if not frappe.db.exists("File", file_name):
		return {"evidence_file": file_name, "status": "Missing"}
	_get_evidence_file_row(
		file_name,
		content_hash,
		submitted_by=user,
		require_unattached=True,
		for_update=True,
	)
	if frappe.db.exists(APPEAL_EVIDENCE_DOCTYPE, {"evidence_file": file_name}):
		frappe.throw("Bound finding appeal evidence cannot be discarded", frappe.PermissionError)
	frappe.delete_doc("File", file_name, ignore_permissions=True)
	return {"evidence_file": file_name, "status": "Discarded"}


def review_finding_appeal(
	appeal: str,
	decision: str,
	review_comment: str,
) -> dict[str, str]:
	"""Approve or reject one Submitted appeal with reviewer segregation."""
	user = _require_named_actor(APPEAL_REVIEWER_ROLES)
	target_status, finding_status, workflow_action = _decision_contract(decision)
	comment = str(review_comment or "").strip()
	if len(comment) < 5:
		frappe.throw("Finding appeal review requires a comment of at least five characters")
	if len(comment) > 2_000:
		frappe.throw("Finding appeal review comment cannot exceed 2,000 characters")

	preview = frappe.get_doc(APPEAL_DOCTYPE, appeal)
	preview.check_permission("read")
	finding_name = str(preview.get("finding") or "")
	if not finding_name:
		frappe.throw("Finding appeal has no linked quality finding")

	with _finding_appeal_lock(finding_name):
		with _atomic_appeal_change(_appeal_savepoint("review", finding_name)):
			finding_doc = frappe.get_doc("IONE QC Finding", finding_name, for_update=True)
			appeal_doc = frappe.get_doc(APPEAL_DOCTYPE, appeal, for_update=True)
			if str(appeal_doc.get("finding") or "") != finding_doc.name:
				frappe.throw("Finding appeal changed its linked quality finding")
			_require_finding_access(finding_doc, user)
			appeal_doc.check_permission("read")
			if user == appeal_doc.submitted_by:
				frappe.throw(
					"A finding appeal submitter cannot review the same appeal",
					frappe.PermissionError,
				)
			if appeal_doc.status == target_status:
				if (
					appeal_doc.reviewed_by == user
					and str(appeal_doc.review_comment or "") == comment
					and finding_doc.status == finding_status
				):
					return {
						"appeal": appeal_doc.name,
						"finding": finding_doc.name,
						"status": appeal_doc.status,
						"finding_status": finding_doc.status,
					}
				frappe.throw("Finding appeal final review is different or out of sync with its finding")
			if str(appeal_doc.get("active_key") or "") != finding_doc.name:
				frappe.throw("Submitted finding appeal active key is inconsistent with its finding")
			if appeal_doc.status != "Submitted" or finding_doc.status != "Appealed":
				frappe.throw("Only a Submitted appeal for an Appealed finding may be reviewed")

			with _appeal_document_capability(
				"ione_finding_appeal_review",
				{
					"appeal": appeal_doc.name,
					"finding": finding_doc.name,
					"reviewed_by": user,
					"target_status": target_status,
				},
			):
				appeal_doc.status = target_status
				appeal_doc.active_key = None
				appeal_doc.reviewed_by = user
				appeal_doc.reviewed_at = now_datetime()
				appeal_doc.review_comment = comment
				appeal_doc.save(ignore_permissions=True)
			finding_doc = _apply_finding_appeal_transition(
				finding_doc,
				appeal_doc,
				action=workflow_action,
				target_status=finding_status,
			)
	return {
		"appeal": appeal_doc.name,
		"finding": finding_doc.name,
		"status": appeal_doc.status,
		"finding_status": finding_doc.status,
	}


def withdraw_finding_appeal(
	appeal: str,
	withdraw_comment: str,
) -> dict[str, str]:
	"""Let the original named submitter withdraw an undecided appeal atomically."""
	user = _require_named_actor(APPEAL_SUBMITTER_ROLES)
	comment = str(withdraw_comment or "").strip()
	if len(comment) < 5:
		frappe.throw("Finding appeal withdrawal requires a comment of at least five characters")
	if len(comment) > 2_000:
		frappe.throw("Finding appeal withdrawal comment cannot exceed 2,000 characters")

	preview = frappe.get_doc(APPEAL_DOCTYPE, appeal)
	preview.check_permission("read")
	finding_name = str(preview.get("finding") or "")
	if not finding_name:
		frappe.throw("Finding appeal has no linked quality finding")

	with _finding_appeal_lock(finding_name):
		with _atomic_appeal_change(_appeal_savepoint("withdraw", finding_name)):
			finding_doc = frappe.get_doc("IONE QC Finding", finding_name, for_update=True)
			appeal_doc = frappe.get_doc(APPEAL_DOCTYPE, appeal, for_update=True)
			if str(appeal_doc.get("finding") or "") != finding_doc.name:
				frappe.throw("Finding appeal changed its linked quality finding")
			_require_finding_access(finding_doc, user)
			appeal_doc.check_permission("read")
			if user != appeal_doc.submitted_by:
				frappe.throw(
					"Only the original finding appeal submitter may withdraw it",
					frappe.PermissionError,
				)
			if appeal_doc.status == "Withdrawn":
				if (
					appeal_doc.withdrawn_by == user
					and str(appeal_doc.withdraw_comment or "") == comment
					and finding_doc.status == "Confirmed"
				):
					return {
						"appeal": appeal_doc.name,
						"finding": finding_doc.name,
						"status": appeal_doc.status,
						"finding_status": finding_doc.status,
					}
				frappe.throw("Finding appeal withdrawal is different or out of sync with its finding")
			if str(appeal_doc.get("active_key") or "") != finding_doc.name:
				frappe.throw("Submitted finding appeal active key is inconsistent with its finding")
			if appeal_doc.status != "Submitted" or finding_doc.status != "Appealed":
				frappe.throw("Only an undecided Submitted appeal for an Appealed finding may be withdrawn")

			with _appeal_document_capability(
				"ione_finding_appeal_withdraw",
				{
					"appeal": appeal_doc.name,
					"finding": finding_doc.name,
					"withdrawn_by": user,
					"target_status": "Withdrawn",
				},
			):
				appeal_doc.status = "Withdrawn"
				appeal_doc.active_key = None
				appeal_doc.withdrawn_by = user
				appeal_doc.withdrawn_at = now_datetime()
				appeal_doc.withdraw_comment = comment
				appeal_doc.save(ignore_permissions=True)
			finding_doc = _apply_finding_appeal_transition(
				finding_doc,
				appeal_doc,
				action=WITHDRAW_APPEAL_ACTION,
				target_status="Confirmed",
			)
	return {
		"appeal": appeal_doc.name,
		"finding": finding_doc.name,
		"status": appeal_doc.status,
		"finding_status": finding_doc.status,
	}


def validate_finding_appeal(doc, method: str | None = None) -> None:
	"""Reject generic create/edit paths and enforce immutable appeal provenance."""
	del method
	previous = doc.get_doc_before_save()
	if previous is None:
		_validate_new_appeal(doc)
		return

	changed_provenance = {
		fieldname for fieldname in _APPEAL_PROVENANCE_FIELDS if doc.get(fieldname) != previous.get(fieldname)
	}
	if changed_provenance:
		frappe.throw(
			"Finding appeal provenance is immutable: " + ", ".join(sorted(changed_provenance)),
			frappe.PermissionError,
		)
	if previous.get("status") in {"Approved", "Rejected", "Withdrawn"}:
		changed_state = {
			fieldname for fieldname in _APPEAL_STATE_FIELDS if doc.get(fieldname) != previous.get(fieldname)
		}
		if changed_state:
			frappe.throw("Final finding appeal records are immutable", frappe.PermissionError)
		return

	transition = (str(previous.get("status") or ""), str(doc.get("status") or ""))
	if transition[0] == transition[1]:
		changed_state = {
			fieldname for fieldname in _APPEAL_STATE_FIELDS if doc.get(fieldname) != previous.get(fieldname)
		}
		if changed_state:
			frappe.throw("Finding appeal state may only change through the governed API")
		return
	if transition in {("Submitted", "Approved"), ("Submitted", "Rejected")}:
		_validate_review_transition(doc, transition[1])
		return
	if transition == ("Submitted", "Withdrawn"):
		_validate_withdrawal_transition(doc)
		return
	frappe.throw(f"Invalid finding appeal transition: {transition[0]} -> {transition[1]}")


def _validate_review_transition(doc, target_status: str) -> None:
	context = getattr(frappe.flags, "ione_finding_appeal_review", None)
	if not isinstance(context, dict) or any(
		(
			str(context.get("appeal") or "") != str(doc.name or ""),
			str(context.get("finding") or "") != str(doc.get("finding") or ""),
			str(context.get("reviewed_by") or "") != str(doc.get("reviewed_by") or ""),
			str(context.get("target_status") or "") != target_status,
			str(frappe.session.user or "") != str(doc.get("reviewed_by") or ""),
		)
	):
		frappe.throw("Finding appeal review must use the governed review API", frappe.PermissionError)
	if (
		not doc.get("reviewed_by")
		or not doc.get("reviewed_at")
		or len(str(doc.get("review_comment") or "").strip()) < 5
	):
		frappe.throw("Final finding appeal review requires reviewer, time, and comment")
	if doc.get("reviewed_by") == doc.get("submitted_by"):
		frappe.throw("A finding appeal submitter cannot review the same appeal", frappe.PermissionError)
	if doc.get("active_key"):
		frappe.throw("Final finding appeal records cannot retain an active key")
	if any(doc.get(fieldname) for fieldname in ("withdrawn_by", "withdrawn_at", "withdraw_comment")):
		frappe.throw("Approved or rejected appeals cannot contain withdrawal audit fields")


def _validate_withdrawal_transition(doc) -> None:
	context = getattr(frappe.flags, "ione_finding_appeal_withdraw", None)
	if not isinstance(context, dict) or any(
		(
			str(context.get("appeal") or "") != str(doc.name or ""),
			str(context.get("finding") or "") != str(doc.get("finding") or ""),
			str(context.get("withdrawn_by") or "") != str(doc.get("withdrawn_by") or ""),
			str(context.get("target_status") or "") != "Withdrawn",
			str(frappe.session.user or "") != str(doc.get("withdrawn_by") or ""),
		)
	):
		frappe.throw("Finding appeal withdrawal must use the governed API", frappe.PermissionError)
	if (
		not doc.get("withdrawn_by")
		or doc.get("withdrawn_by") != doc.get("submitted_by")
		or not doc.get("withdrawn_at")
		or len(str(doc.get("withdraw_comment") or "").strip()) < 5
	):
		frappe.throw("Finding appeal withdrawal requires the original submitter, time, and comment")
	if any(doc.get(fieldname) for fieldname in ("reviewed_by", "reviewed_at", "review_comment")):
		frappe.throw("Withdrawn appeals cannot contain review audit fields")
	if doc.get("active_key"):
		frappe.throw("Withdrawn finding appeal records cannot retain an active key")


def validate_finding_appeal_transition(doc, method: str | None = None) -> None:
	"""Require an exact persisted Appeal for every appeal-related Finding edge."""
	del method
	previous = doc.get_doc_before_save()
	if previous is None:
		return
	transition = (str(previous.get("status") or ""), str(doc.get("status") or ""))
	expected_appeal_statuses = _FINDING_APPEAL_TRANSITIONS.get(transition)
	if not expected_appeal_statuses:
		return
	context = getattr(frappe.flags, "ione_finding_appeal_sync", None)
	if not isinstance(context, dict) or any(
		(
			str(context.get("finding") or "") != str(doc.name or ""),
			str(context.get("target_status") or "") != transition[1],
			str(context.get("source_status") or "") != transition[0],
		)
	):
		frappe.throw(
			"Appeal-related Finding transitions require a structured Finding Appeal record",
			frappe.PermissionError,
		)
	appeal_name = str(context.get("appeal") or "")
	appeal = frappe.db.get_value(
		APPEAL_DOCTYPE,
		appeal_name,
		["finding", "status"],
		as_dict=True,
	)
	if (
		not appeal
		or str(appeal.get("finding") or "") != str(doc.name or "")
		or str(appeal.get("status") or "") not in expected_appeal_statuses
	):
		frappe.throw("Finding appeal transition provenance is missing or inconsistent")


def prevent_finding_appeal_deletion(doc, method: str | None = None) -> None:
	del doc, method
	frappe.throw("Finding appeal audit records cannot be deleted", frappe.PermissionError)


def validate_finding_appeal_evidence(doc, method: str | None = None) -> None:
	"""Allow only the governed one-time creation of immutable evidence metadata."""
	del method
	previous = doc.get_doc_before_save()
	if previous is not None:
		frappe.throw("Finding appeal evidence records are immutable", frappe.PermissionError)

	context = getattr(frappe.flags, "ione_finding_appeal_evidence_create", None)
	if not isinstance(context, dict) or any(
		(
			str(context.get("finding") or "") != str(doc.get("finding") or ""),
			str(context.get("evidence_file") or "") != str(doc.get("evidence_file") or ""),
			str(context.get("content_hash") or "") != str(doc.get("content_hash") or ""),
			str(context.get("submission_hash") or "") != str(doc.get("appeal_submission_hash") or ""),
			str(context.get("submitted_by") or "") != str(doc.get("submitted_by") or ""),
			str(frappe.session.user or "") != str(doc.get("submitted_by") or ""),
		)
	):
		frappe.throw(
			"Finding appeal evidence may only be created by the governed submission API",
			frappe.PermissionError,
		)
	if not doc.get("submitted_at"):
		frappe.throw("Finding appeal evidence requires accountable submission time")
	if not _SHA256_PATTERN.fullmatch(str(doc.get("content_hash") or "")):
		frappe.throw("Finding appeal evidence requires an exact SHA-256 content hash")
	if not _SHA256_PATTERN.fullmatch(str(doc.get("appeal_submission_hash") or "")):
		frappe.throw("Finding appeal evidence requires an exact appeal submission hash")
	row = _get_evidence_file_row(
		str(doc.get("evidence_file") or ""),
		str(doc.get("content_hash") or ""),
		submitted_by=str(doc.get("submitted_by") or ""),
		require_unattached=True,
		for_update=True,
	)
	if str(doc.get("file_name") or "") != str(row.get("file_name") or "") or int(
		doc.get("file_size") or 0
	) != int(row.get("file_size") or 0):
		frappe.throw("Finding appeal evidence file metadata is inconsistent")


def validate_finding_appeal_evidence_file(doc, method: str | None = None) -> None:
	"""Prevent mutation or hash-linked collateral changes to governed evidence Files."""
	del method
	previous = doc.get_doc_before_save()
	file_names = {
		str(value)
		for value in (
			doc.get("name"),
			previous.get("name") if previous is not None else None,
		)
		if value
	}
	content_hashes = {
		str(value)
		for value in (
			doc.get("content_hash"),
			previous.get("content_hash") if previous is not None else None,
		)
		if value
	}
	file_urls = {
		str(value)
		for value in (
			doc.get("file_url"),
			previous.get("file_url") if previous is not None else None,
		)
		if value
	}
	if not _references_protected_evidence(file_names, content_hashes, file_urls):
		return

	context = getattr(frappe.flags, "ione_finding_appeal_evidence_file_bind", None)
	if _valid_evidence_file_binding_context(doc, previous, context):
		return
	if previous is None:
		frappe.throw(
			"A duplicate of retained finding appeal evidence cannot be created",
			frappe.PermissionError,
		)
	changed = {
		fieldname for fieldname in _PROTECTED_FILE_FIELDS if doc.get(fieldname) != previous.get(fieldname)
	}
	if changed:
		frappe.throw(
			"Retained finding appeal evidence File is immutable: " + ", ".join(sorted(changed)),
			frappe.PermissionError,
		)


def prevent_finding_appeal_evidence_deletion(doc, method: str | None = None) -> None:
	"""Keep evidence records, exact Files, and hash-sharing Files from deletion."""
	del method
	if str(doc.get("doctype") or "") == APPEAL_EVIDENCE_DOCTYPE:
		frappe.throw(
			"Finding appeal evidence audit records cannot be deleted",
			frappe.PermissionError,
		)
	file_names = {str(doc.get("name") or "")} - {""}
	content_hashes = {str(doc.get("content_hash") or "")} - {""}
	file_urls = {str(doc.get("file_url") or "")} - {""}
	if _references_protected_evidence(file_names, content_hashes, file_urls):
		frappe.throw(
			"This File or its content hash is retained by a finding appeal evidence record",
			frappe.PermissionError,
		)


def _validate_new_appeal(doc) -> None:
	context = getattr(frappe.flags, "ione_finding_appeal_create", None)
	if not isinstance(context, dict) or any(
		(
			str(context.get("finding") or "") != str(doc.get("finding") or ""),
			str(context.get("submitted_by") or "") != str(doc.get("submitted_by") or ""),
			str(context.get("submission_hash") or "") != str(doc.get("submission_hash") or ""),
			str(context.get("evidence_record") or "") != str(doc.get("evidence_record") or ""),
			str(frappe.session.user or "") != str(doc.get("submitted_by") or ""),
		)
	):
		frappe.throw("Finding appeals may only be created by the governed submission API")
	if doc.get("status") != "Submitted" or doc.get("active_key") != doc.get("finding"):
		frappe.throw("New finding appeals must start Submitted with the exact active finding key")
	if not doc.get("submitted_at"):
		frappe.throw("New finding appeals require accountable submitter time")
	if any(
		doc.get(fieldname)
		for fieldname in (
			"reviewed_by",
			"reviewed_at",
			"review_comment",
			"withdrawn_by",
			"withdrawn_at",
			"withdraw_comment",
		)
	):
		frappe.throw("New finding appeals cannot contain review or withdrawal audit values")
	_validate_submission_values(
		appeal_type=str(doc.get("appeal_type") or ""),
		reason=str(doc.get("reason") or ""),
		evidence_summary=str(doc.get("evidence_summary") or ""),
		evidence_file=str(doc.get("evidence_file") or ""),
		evidence_content_hash=str(doc.get("evidence_content_hash") or ""),
	)
	finding = frappe.db.get_value(
		"IONE QC Finding",
		doc.get("finding"),
		["status", *_FINDING_SCOPE_FIELDS],
		as_dict=True,
	)
	if not finding or finding.get("status") != "Confirmed":
		frappe.throw("New finding appeals require a Confirmed quality finding")
	for fieldname in _FINDING_SCOPE_FIELDS:
		if str(doc.get(fieldname) or "") != str(finding.get(fieldname) or ""):
			frappe.throw(f"Finding appeal {fieldname} must match its quality finding")
	_validate_bound_appeal_evidence(doc)
	expected_hash = _submission_hash(
		finding=str(doc.get("finding") or ""),
		submitted_by=str(doc.get("submitted_by") or ""),
		appeal_type=str(doc.get("appeal_type") or ""),
		reason=str(doc.get("reason") or ""),
		evidence_summary=str(doc.get("evidence_summary") or ""),
		evidence_file=str(doc.get("evidence_file") or ""),
		evidence_content_hash=str(doc.get("evidence_content_hash") or ""),
	)
	if str(doc.get("submission_hash") or "") != expected_hash:
		frappe.throw("Finding appeal submission hash does not match its immutable content")


def _apply_finding_appeal_transition(finding, appeal, *, action: str, target_status: str):
	source_status = str(finding.status or "")
	with _appeal_document_capability(
		"ione_finding_appeal_sync",
		{
			"appeal": appeal.name,
			"finding": finding.name,
			"source_status": source_status,
			"target_status": target_status,
		},
	):
		updated = apply_workflow(finding, action)
	if not updated or str(updated.get("status") or "") != target_status:
		frappe.throw("Finding workflow did not reach the appeal state required by the appeal record")
	return updated


def _require_named_actor(allowed_roles: frozenset[str]) -> str:
	user = str(frappe.session.user or "")
	roles = set(frappe.get_roles(user)) if user else set()
	if (
		user in {"", "Guest", "Administrator"}
		or "IONE Agent Service" in roles
		or not roles.intersection(allowed_roles)
	):
		frappe.throw(
			"Finding appeal action requires a named authorized clinical actor", frappe.PermissionError
		)
	return user


def _require_finding_access(finding, user: str) -> None:
	finding.check_permission("read")
	if not finding_permission(finding, "read", user=user):
		frappe.throw("Current user cannot access this quality finding", frappe.PermissionError)


def _normalized_submission(
	*,
	appeal_type: str,
	reason: str,
	evidence_summary: str | None,
	evidence_file: str | None,
	evidence_content_hash: str | None,
) -> dict[str, str]:
	values = {
		"appeal_type": str(appeal_type or "").strip(),
		"reason": str(reason or "").strip(),
		"evidence_summary": str(evidence_summary or "").strip(),
		"evidence_file": str(evidence_file or "").strip(),
		"evidence_content_hash": str(evidence_content_hash or "").strip().lower(),
	}
	_validate_submission_values(**values)
	return values


def _validate_submission_values(
	*,
	appeal_type: str,
	reason: str,
	evidence_summary: str,
	evidence_file: str,
	evidence_content_hash: str,
) -> None:
	if appeal_type not in APPEAL_TYPES:
		frappe.throw("Finding appeal type is not supported")
	if len(reason) < 10:
		frappe.throw("Finding appeal reason must contain at least 10 characters")
	if len(reason) > 20_000 or len(evidence_summary) > 20_000:
		frappe.throw("Finding appeal reason and evidence summary cannot exceed 20,000 characters")
	if bool(evidence_file) != bool(evidence_content_hash):
		frappe.throw("Finding appeal evidence requires both exact File name and content hash")
	if len(evidence_file) > 140:
		frappe.throw("Finding appeal evidence File name is too long")
	if evidence_content_hash and not _SHA256_PATTERN.fullmatch(evidence_content_hash):
		frappe.throw("Finding appeal evidence content hash must be an exact SHA-256 digest")


def _create_appeal_evidence(
	finding,
	*,
	submitted_by: str,
	submission_hash: str,
	evidence_file: str,
	evidence_content_hash: str,
):
	if not evidence_file:
		return None
	row = _get_evidence_file_row(
		evidence_file,
		evidence_content_hash,
		submitted_by=submitted_by,
		require_unattached=True,
		for_update=True,
	)
	evidence = frappe.get_doc(
		{
			"doctype": APPEAL_EVIDENCE_DOCTYPE,
			"finding": finding.name,
			"evidence_file": evidence_file,
			"content_hash": evidence_content_hash,
			"file_name": row.get("file_name"),
			"file_size": row.get("file_size"),
			"appeal_submission_hash": submission_hash,
			"submitted_by": submitted_by,
			"submitted_at": now_datetime(),
		}
	)
	with _appeal_document_capability(
		"ione_finding_appeal_evidence_create",
		{
			"finding": finding.name,
			"evidence_file": evidence_file,
			"content_hash": evidence_content_hash,
			"submission_hash": submission_hash,
			"submitted_by": submitted_by,
		},
	):
		evidence.insert(ignore_permissions=True)
	_validate_evidence_hash_siblings(evidence_file, evidence_content_hash)

	file_doc = frappe.get_doc("File", evidence_file, for_update=True)
	with _appeal_document_capability(
		"ione_finding_appeal_evidence_file_bind",
		{
			"evidence": evidence.name,
			"evidence_file": evidence_file,
			"content_hash": evidence_content_hash,
			"submitted_by": submitted_by,
		},
	):
		file_doc.attached_to_doctype = APPEAL_EVIDENCE_DOCTYPE
		file_doc.attached_to_name = evidence.name
		file_doc.attached_to_field = None
		file_doc.save(ignore_permissions=True)
	return evidence


def _get_evidence_file_row(
	evidence_file: str,
	evidence_content_hash: str,
	*,
	submitted_by: str,
	require_unattached: bool,
	for_update: bool,
):
	row = frappe.db.get_value(
		"File",
		evidence_file,
		[
			"name",
			"owner",
			"is_private",
			"file_name",
			"file_size",
			"file_url",
			"content_hash",
			"attached_to_doctype",
			"attached_to_name",
			"attached_to_field",
		],
		as_dict=True,
		for_update=for_update,
	)
	if not row:
		frappe.throw("Finding appeal evidence File does not exist")
	file_doc = frappe.get_doc("File", evidence_file)
	file_doc.check_permission("read")
	if str(row.get("owner") or "") != submitted_by:
		frappe.throw(
			"Finding appeal evidence must be uploaded by the named submitter",
			frappe.PermissionError,
		)
	if not int(row.get("is_private") or 0) or not str(row.get("file_url") or "").startswith(
		"/private/files/"
	):
		frappe.throw("Finding appeal evidence must be a private local File")
	if str(row.get("content_hash") or "") != evidence_content_hash:
		frappe.throw("Finding appeal evidence File content hash does not match")
	file_size = int(row.get("file_size") or 0)
	if file_size <= 0 or file_size > _MAX_EVIDENCE_FILE_SIZE:
		frappe.throw("Finding appeal evidence File must be non-empty and no larger than 20 MB")
	if require_unattached and any(
		row.get(fieldname) for fieldname in ("attached_to_doctype", "attached_to_name", "attached_to_field")
	):
		frappe.throw("Finding appeal evidence File must be a new unbound upload")
	return row


def _validate_evidence_hash_siblings(evidence_file: str, content_hash: str) -> None:
	rows = frappe.get_all(
		"File",
		filters={"content_hash": content_hash},
		fields=["name", "is_private", "file_url"],
		limit_page_length=10_000,
	)
	names = {str(row.get("name") or "") for row in rows if row.get("name")}
	if names != {evidence_file}:
		frappe.throw("Finding appeal evidence content hash must belong to exactly one File record")
	file_url = str(rows[0].get("file_url") or "")
	if any(
		not int(row.get("is_private") or 0)
		or not str(row.get("file_url") or "").startswith("/private/files/")
		for row in rows
	):
		frappe.throw("Finding appeal evidence content already has a public or remote File copy")
	url_names = {
		str(value)
		for value in frappe.get_all(
			"File",
			filters={"file_url": file_url},
			pluck="name",
			limit_page_length=10_000,
		)
		if value
	}
	if url_names != {evidence_file}:
		frappe.throw("Finding appeal evidence File URL must not be shared by another File record")


def _validate_bound_appeal_evidence(doc) -> None:
	evidence_record = str(doc.get("evidence_record") or "")
	evidence_file = str(doc.get("evidence_file") or "")
	content_hash = str(doc.get("evidence_content_hash") or "")
	if not evidence_record:
		if evidence_file or content_hash:
			frappe.throw("Finding appeal evidence reference is incomplete")
		return
	if not evidence_file or not content_hash:
		frappe.throw("Finding appeal evidence reference is incomplete")
	evidence = frappe.db.get_value(
		APPEAL_EVIDENCE_DOCTYPE,
		evidence_record,
		[
			"finding",
			"evidence_file",
			"content_hash",
			"appeal_submission_hash",
			"submitted_by",
		],
		as_dict=True,
	)
	if (
		not evidence
		or str(evidence.get("finding") or "") != str(doc.get("finding") or "")
		or str(evidence.get("evidence_file") or "") != evidence_file
		or str(evidence.get("content_hash") or "") != content_hash
		or str(evidence.get("appeal_submission_hash") or "") != str(doc.get("submission_hash") or "")
		or str(evidence.get("submitted_by") or "") != str(doc.get("submitted_by") or "")
	):
		frappe.throw("Finding appeal evidence provenance is inconsistent")
	file_row = frappe.db.get_value(
		"File",
		evidence_file,
		[
			"content_hash",
			"is_private",
			"file_url",
			"attached_to_doctype",
			"attached_to_name",
			"attached_to_field",
		],
		as_dict=True,
	)
	if (
		not file_row
		or str(file_row.get("content_hash") or "") != content_hash
		or not int(file_row.get("is_private") or 0)
		or not str(file_row.get("file_url") or "").startswith("/private/files/")
		or str(file_row.get("attached_to_doctype") or "") != APPEAL_EVIDENCE_DOCTYPE
		or str(file_row.get("attached_to_name") or "") != evidence_record
		or file_row.get("attached_to_field")
	):
		frappe.throw("Finding appeal evidence File binding is inconsistent")


def _references_protected_evidence(
	file_names: set[str],
	content_hashes: set[str],
	file_urls: set[str] | None = None,
) -> bool:
	if not _appeal_evidence_doctype_available():
		return False
	for file_name in file_names:
		if frappe.db.get_value(
			APPEAL_EVIDENCE_DOCTYPE,
			{"evidence_file": file_name},
			"name",
			for_update=True,
		):
			return True
	for content_hash in content_hashes:
		if frappe.db.get_value(
			APPEAL_EVIDENCE_DOCTYPE,
			{"content_hash": content_hash},
			"name",
			for_update=True,
		):
			return True
	if file_urls:
		linked_file_names = {
			str(value)
			for value in frappe.get_all(
				"File",
				filters={"file_url": ["in", sorted(file_urls)]},
				pluck="name",
				limit_page_length=10_000,
			)
			if value
		}
		for file_name in linked_file_names:
			if frappe.db.get_value(
				APPEAL_EVIDENCE_DOCTYPE,
				{"evidence_file": file_name},
				"name",
				for_update=True,
			):
				return True
	return False


def _appeal_evidence_doctype_available() -> bool:
	if any(
		getattr(frappe.flags, flag_name, False) for flag_name in ("in_install", "in_migrate", "in_uninstall")
	):
		return False
	try:
		return bool(frappe.db.exists("DocType", APPEAL_EVIDENCE_DOCTYPE))
	except Exception:
		return False


def _valid_evidence_file_binding_context(doc, previous, context) -> bool:
	if not isinstance(context, dict) or previous is None:
		return False
	evidence_name = str(context.get("evidence") or "")
	evidence_file = str(context.get("evidence_file") or "")
	content_hash = str(context.get("content_hash") or "")
	submitted_by = str(context.get("submitted_by") or "")
	if any(
		(
			not evidence_name,
			evidence_file != str(doc.get("name") or ""),
			content_hash != str(doc.get("content_hash") or ""),
			submitted_by != str(doc.get("owner") or ""),
			submitted_by != str(frappe.session.user or ""),
			str(doc.get("attached_to_doctype") or "") != APPEAL_EVIDENCE_DOCTYPE,
			str(doc.get("attached_to_name") or "") != evidence_name,
			bool(doc.get("attached_to_field")),
			bool(previous.get("attached_to_doctype")),
			bool(previous.get("attached_to_name")),
			bool(previous.get("attached_to_field")),
		)
	):
		return False
	changed = {
		fieldname for fieldname in _PROTECTED_FILE_FIELDS if doc.get(fieldname) != previous.get(fieldname)
	}
	if changed - {"attached_to_doctype", "attached_to_name"}:
		return False
	evidence = frappe.db.get_value(
		APPEAL_EVIDENCE_DOCTYPE,
		evidence_name,
		["evidence_file", "content_hash", "submitted_by"],
		as_dict=True,
	)
	return bool(
		evidence
		and str(evidence.get("evidence_file") or "") == evidence_file
		and str(evidence.get("content_hash") or "") == content_hash
		and str(evidence.get("submitted_by") or "") == submitted_by
	)


def _submission_hash(
	*,
	finding: str,
	submitted_by: str,
	appeal_type: str,
	reason: str,
	evidence_summary: str,
	evidence_file: str,
	evidence_content_hash: str,
) -> str:
	payload = {
		"appeal_type": appeal_type,
		"evidence_content_hash": evidence_content_hash,
		"evidence_file": evidence_file,
		"evidence_summary": evidence_summary,
		"finding": finding,
		"reason": reason,
		"submitted_by": submitted_by,
	}
	serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
	return hashlib.sha256(serialized.encode()).hexdigest()


def _decision_contract(decision: str) -> tuple[str, str, str]:
	normalized = str(decision or "").strip()
	if normalized == "Approve":
		return "Approved", "Appeal Approved", APPROVE_APPEAL_ACTION
	if normalized == "Reject":
		return "Rejected", "Appeal Rejected", REJECT_APPEAL_ACTION
	frappe.throw("Finding appeal decision must be Approve or Reject")


def _scalar_name(value) -> str:
	if isinstance(value, dict):
		value = value.get("name")
	return str(value or "")


def _finding_appeal_lock(finding: str):
	return frappe.db.advisory_lock(f"ione-qms:finding-appeal:{finding}", timeout=15)


def _appeal_savepoint(operation: str, finding: str) -> str:
	suffix = hashlib.sha256(str(finding).encode()).hexdigest()[:16]
	return f"ione_finding_appeal_{operation}_{suffix}"


@contextmanager
def _atomic_appeal_change(savepoint: str):
	frappe.db.savepoint(savepoint)
	try:
		yield
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	else:
		frappe.db.release_savepoint(savepoint)


@contextmanager
def _appeal_document_capability(flag_name: str, value: dict[str, str]):
	missing = object()
	previous = frappe.flags.get(flag_name, missing)
	frappe.flags[flag_name] = value
	try:
		yield
	finally:
		if previous is missing:
			frappe.flags.pop(flag_name, None)
		else:
			frappe.flags[flag_name] = previous
