from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe.utils import now_datetime

MAX_EVIDENCE_SNAPSHOT_BYTES = 256 * 1024
EVIDENCE_SNAPSHOT_REDACTED = "[REDACTED BY IONE INPUT RETENTION]"


def record_tool_access(
	*,
	tool_slug: str,
	task: str | None,
	policy: str | None,
	purpose: str,
	accessed_fields: list[str],
	result: str,
	hospital: str | None = None,
	campus: str | None = None,
	department: str | None = None,
	ward: str | None = None,
	patient: str | None = None,
	encounter: str | None = None,
	request: dict[str, Any] | None = None,
	source_doctype: str | None = None,
	source_name: str | None = None,
	source_version: str | None = None,
	source_record_hash: str | None = None,
	snapshot: dict[str, Any] | list[Any] | None = None,
) -> str:
	request_hash = hashlib.sha256(_canonical_json(request or {}).encode()).hexdigest()
	if bool(source_doctype) != bool(source_name):
		frappe.throw("AI evidence receipts require both source_doctype and source_name")
	snapshot_json = _canonical_json(snapshot) if snapshot is not None else ""
	if len(snapshot_json.encode()) > MAX_EVIDENCE_SNAPSHOT_BYTES:
		frappe.throw("AI evidence snapshot exceeds the governed 256 KiB limit")
	content_hash = hashlib.sha256(snapshot_json.encode()).hexdigest() if snapshot_json else None
	doc = frappe.get_doc(
		{
			"doctype": "IONE AI Data Access Log",
			"task": task,
			"policy": policy,
			"user": frappe.session.user,
			"tool_slug": tool_slug,
			"hospital": hospital,
			"campus": campus,
			"department": department,
			"ward": ward,
			"patient": patient,
			"encounter": encounter,
			"purpose": purpose[:500],
			"accessed_fields": json.dumps(sorted(set(accessed_fields)), ensure_ascii=False),
			"accessed_at": now_datetime(),
			"result": result,
			"request_hash": request_hash,
			"source_doctype": source_doctype,
			"source_name": source_name,
			"source_version": str(source_version or "")[:200],
			"source_record_hash": _normalized_hash(source_record_hash),
			"content_hash": content_hash,
			"snapshot_json": snapshot_json or None,
		}
	)
	doc.insert(ignore_permissions=True)
	return f"{doc.doctype}:{doc.name}"


def _canonical_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)


def _normalized_hash(value: Any) -> str | None:
	text = str(value or "").strip().lower()
	if len(text) == 64 and all(character in "0123456789abcdef" for character in text):
		return text
	return hashlib.sha256(str(value).encode()).hexdigest() if value not in (None, "") else None
