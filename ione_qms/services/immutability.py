from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import frappe
from frappe.utils import now_datetime


def validate_append_only(doc, method: str | None = None) -> None:
	if doc.is_new() or _maintenance_context():
		if doc.is_new():
			_set_integrity_fields(doc)
		return
	frappe.throw(f"{doc.doctype} records are append-only")


def prevent_delete(doc, method: str | None = None) -> None:
	if _maintenance_context():
		return
	frappe.throw(f"{doc.doctype} records cannot be cancelled or deleted")


def validate_reviewed_ai_artifact(doc, method: str | None = None) -> None:
	previous = doc.get_doc_before_save()
	if not previous:
		return
	expected_capability = f"{doc.doctype}:{doc.name}"
	if getattr(frappe.flags, "ione_ai_artifact_review", None) != expected_capability:
		frappe.throw("AI artifacts may only be reviewed through the governed review API")
	allowed_fields = {
		"status",
		"reviewed_by",
		"reviewed_at",
		"review_comment",
		"formal_finding",
	}
	changed = {
		field.fieldname
		for field in doc.meta.fields
		if not field.fieldtype.endswith("Break")
		and field.fieldname
		not in {
			"modified",
			"modified_by",
			"_comments",
			"_assign",
			"_liked_by",
			"_user_tags",
		}
		and doc.get(field.fieldname) != previous.get(field.fieldname)
	}
	if not changed or not changed.issubset(allowed_fields):
		frappe.throw("AI artifact content is immutable after model creation")
	transitions = {
		"IONE AI Candidate Finding": {
			"Pending Review": {"Accepted", "Rejected"},
		},
		"IONE AI Report Draft": {
			"Draft": {"Approved", "Rejected", "Revision Requested"},
			"Pending Review": {"Approved", "Rejected", "Revision Requested"},
		},
	}
	previous_status = str(previous.get("status") or "")
	current_status = str(doc.get("status") or "")
	if current_status not in transitions.get(doc.doctype, {}).get(previous_status, set()):
		frappe.throw(f"Invalid governed AI artifact transition: {previous_status} -> {current_status}")
	if frappe.session.user in {"", "Guest", "Administrator"}:
		frappe.throw("AI artifact review requires a named accountable reviewer", frappe.PermissionError)
	if doc.get("reviewed_by") != frappe.session.user or not doc.get("reviewed_at"):
		frappe.throw("AI artifact review provenance does not match the active reviewer")
	if len(str(doc.get("review_comment") or "").strip()) < 5:
		frappe.throw("AI artifact review requires a comment of at least five characters")
	if doc.doctype == "IONE AI Candidate Finding":
		if current_status == "Accepted" and not doc.get("formal_finding"):
			frappe.throw("Accepted AI candidates require a governed formal finding")
		if current_status == "Rejected" and doc.get("formal_finding"):
			frappe.throw("Rejected AI candidates cannot reference a formal finding")


def _set_integrity_fields(doc) -> None:
	if doc.meta.has_field("recorded_at") and not doc.get("recorded_at"):
		doc.recorded_at = now_datetime()
	hash_field = next(
		(fieldname for fieldname in ("evidence_hash", "record_hash") if doc.meta.has_field(fieldname)),
		None,
	)
	if not hash_field:
		return
	computed = _document_hash(doc)
	provided = str(doc.get(hash_field) or "")
	if provided and not hmac.compare_digest(provided, computed):
		frappe.throw(f"Supplied {hash_field} does not match the canonical record content")
	doc.set(hash_field, computed)


def _document_hash(doc) -> str:
	return canonical_record_hash_values(doc.doctype, doc)


def canonical_record_hash_values(doctype: str, values: Any) -> str:
	"""Recompute an append-only receipt hash from a complete row mapping.

	Release gates use this variant so every bounded receipt can be verified
	row-by-row without trusting the stored ``record_hash`` or loading one Document
	per row.
	"""
	excluded = {
		"name",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"idx",
		"docstatus",
		"evidence_hash",
		"record_hash",
	}
	payload: dict[str, Any] = {
		field.fieldname: values.get(field.fieldname)
		for field in frappe.get_meta(doctype).fields
		if field.fieldname not in excluded
	}
	return hashlib.sha256(
		json.dumps(
			payload,
			ensure_ascii=False,
			sort_keys=True,
			separators=(",", ":"),
			default=str,
		).encode()
	).hexdigest()


def canonical_record_hash(doc) -> str:
	"""Return the canonical hash used by append-only audit records."""
	return _document_hash(doc)


def _maintenance_context() -> bool:
	return bool(getattr(frappe.flags, "in_uninstall", False))
