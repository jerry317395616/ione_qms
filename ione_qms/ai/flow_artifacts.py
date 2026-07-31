from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe

SANITIZED_FLOW_ERROR = "GovernedExecutionFailed"


def flow_run_artifact_hashes(flow_run: str) -> dict[str, str]:
	"""Hash every Flow storage surface that can contain governed task payloads."""
	attachments = flow_run_attachments(flow_run)
	if attachments:
		frappe.throw("Governed IONE Flow Runs do not permit session attachments")
	return {
		"input_messages_hash": canonical_hash(flow_run_input_messages(flow_run)),
		"output_messages_hash": canonical_hash(flow_run_output_messages(flow_run)),
		"tool_trace_hash": canonical_hash(flow_run_tool_trace(flow_run)),
		"attachment_text_hash": canonical_hash(attachments),
	}


def flow_run_input_messages(flow_run: str) -> list[dict[str, Any]]:
	return [
		{
			"idx": row.get("idx"),
			"role": row.get("role"),
			"content": row.get("content"),
			"run": row.get("run"),
		}
		for row in _flow_run_message_rows(flow_run)
		if row.get("role") in {"user", "tool"}
	]


def flow_run_output_messages(flow_run: str) -> list[dict[str, Any]]:
	return [
		{
			"idx": row.get("idx"),
			"role": row.get("role"),
			"content": row.get("content"),
			"run": row.get("run"),
		}
		for row in _flow_run_message_rows(flow_run)
		if row.get("role") == "assistant"
	]


def _flow_run_message_rows(flow_run: str) -> list[Any]:
	if not _doctype_exists("Flow Session Message"):
		return []
	return frappe.get_all(
		"Flow Session Message",
		filters={"run": flow_run},
		fields=["idx", "role", "content", "tool_call_id", "tool_calls", "run"],
		order_by="idx asc, name asc",
		limit_page_length=10_000,
	)


def flow_run_attachments(flow_run: str) -> list[dict[str, Any]]:
	if not _doctype_exists("Flow Session Attachment"):
		return []
	rows = frappe.get_all(
		"Flow Session Attachment",
		filters={"run": flow_run},
		fields=["idx", "file", "file_name", "file_size", "run", "mode", "extracted_text"],
		order_by="idx asc, name asc",
		limit_page_length=1_000,
	)
	return [
		{
			"idx": row.get("idx"),
			"file": row.get("file"),
			"file_name": row.get("file_name"),
			"file_size": row.get("file_size"),
			"run": row.get("run"),
			"mode": row.get("mode"),
			"extracted_text": row.get("extracted_text"),
		}
		for row in rows
	]


def flow_run_tool_trace(flow_run: str) -> dict[str, Any]:
	if not _doctype_exists("Flow Run"):
		return {"tool_calls": None, "questions": None, "error": None}
	values = frappe.db.get_value(
		"Flow Run",
		flow_run,
		["tool_calls", "questions", "error"],
		as_dict=True,
	)
	message_trace = [
		{
			"idx": row.get("idx"),
			"role": row.get("role"),
			"tool_call_id": row.get("tool_call_id"),
			"tool_calls": row.get("tool_calls"),
			"run": row.get("run"),
		}
		for row in _flow_run_message_rows(flow_run)
		if row.get("tool_call_id") or row.get("tool_calls")
	]
	return {
		"tool_calls": values.get("tool_calls") if values else None,
		"questions": values.get("questions") if values else None,
		"error": values.get("error") if values else None,
		"message_trace": message_trace,
	}


def sanitize_flow_run_error(flow_run: str, failure_type: str | None = None) -> None:
	"""Remove provider/tool exception text immediately while retaining a bounded category."""
	if not flow_run or not _doctype_exists("Flow Run"):
		return
	category = str(failure_type or "Error")
	safe_category = "".join(character for character in category if character.isalnum())[:80] or "Error"
	frappe.db.set_value(
		"Flow Run",
		flow_run,
		"error",
		f"{SANITIZED_FLOW_ERROR}:{safe_category}",
		update_modified=False,
	)


def canonical_hash(value: Any) -> str:
	return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def canonical_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)


def _doctype_exists(doctype: str) -> bool:
	try:
		return bool(frappe.db.exists("DocType", doctype))
	except Exception:
		return False
