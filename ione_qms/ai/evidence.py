from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import frappe
from frappe.utils import now_datetime

from ione_qms.ai.audit import EVIDENCE_SNAPSHOT_REDACTED

MAX_EVIDENCE_REFERENCES = 100
RECEIPT_DOCTYPE = "IONE AI Data Access Log"

_SCOPE_FIELDS = ("hospital", "campus", "department", "ward", "patient", "encounter")
_GOVERNANCE_SOURCE_TYPES = frozenset(
	{
		"IONE QC Standard",
		"IONE QC Standard Version",
		"IONE QC Standard Clause",
		"IONE QC Rule",
		"IONE QC Rule Version",
		"IONE QC Indicator",
		"IONE QC Indicator Version",
	}
)
_CLINICAL_SOURCE_TYPES = frozenset(
	{
		"IONE Clinical Quality Event",
		"IONE Encounter Index",
		"IONE QC Execution",
		"IONE QC Finding",
		"IONE QC Finding Evidence",
		"IONE Indicator Result",
		"IONE Indicator Result Detail",
		"IONE QC Rectification",
		"IONE QC Verification",
		"IONE PDCA Project",
		"IONE Quality Report Snapshot",
	}
)
_ALLOWED_SOURCE_TYPES = _GOVERNANCE_SOURCE_TYPES | _CLINICAL_SOURCE_TYPES


@dataclass(frozen=True)
class ValidatedEvidenceReference:
	reference: str
	receipt_name: str
	receipt_hash: str
	source_doctype: str
	source_name: str
	source_version: str
	content_hash: str
	source_record_hash: str

	@property
	def is_clinical(self) -> bool:
		return self.source_doctype in _CLINICAL_SOURCE_TYPES


def validate_evidence_references(
	task: Any,
	references: Iterable[str] | str,
	*,
	max_references: int = MAX_EVIDENCE_REFERENCES,
	require_clinical: bool = False,
) -> list[ValidatedEvidenceReference]:
	"""Validate citations as immutable access receipts created by this exact AI task."""
	task_doc = frappe.get_doc("IONE AI Analysis Task", task) if isinstance(task, str) else task
	normalized = _normalize_references(references, max_references=max_references)
	validated = [_validate_receipt(task_doc, reference) for reference in normalized]
	if require_clinical and not any(reference.is_clinical for reference in validated):
		frappe.throw("Candidate findings require at least one task-bound clinical evidence receipt")
	return validated


def parse_stored_references(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
	if isinstance(value, (list, tuple)):
		return [str(item) for item in value]
	try:
		decoded = json.loads(str(value or ""))
	except (TypeError, ValueError) as exc:
		raise frappe.ValidationError("Stored AI evidence references are not valid JSON") from exc
	if not isinstance(decoded, list):
		frappe.throw("Stored AI evidence references must be a JSON list")
	return [str(item) for item in decoded]


def persist_finding_source_evidence(finding: Any, candidate: Any) -> None:
	"""Link immutable, task-bound access receipts to an accepted formal finding."""
	task = frappe.get_doc("IONE AI Analysis Task", candidate.task)
	references = validate_evidence_references(
		task,
		parse_stored_references(candidate.get("evidence_references")),
		require_clinical=True,
	)
	recorded_at = now_datetime()
	for source in references:
		if frappe.db.exists(
			"IONE QC Finding Evidence",
			{
				"finding": finding.name,
				"evidence_type": "Source Snapshot",
				"source_reference": source.reference,
				"source_version": source.receipt_hash,
			},
		):
			continue
		pointer = {
			"receipt": source.reference,
			"receipt_hash": source.receipt_hash,
			"content_hash": source.content_hash,
			"original_source": {
				"doctype": source.source_doctype,
				"name": source.source_name,
				"version": source.source_version,
				"record_hash": source.source_record_hash,
			},
			"ai_task": task.name,
			"ai_candidate": candidate.name,
		}
		doc = frappe.get_doc(
			{
				"doctype": "IONE QC Finding Evidence",
				"finding": finding.name,
				"execution": (source.source_name if source.source_doctype == "IONE QC Execution" else None),
				"evidence_type": "Source Snapshot",
				"source_system": "IONE Governed AI",
				"source_record_type": RECEIPT_DOCTYPE,
				"source_record_id": source.receipt_name,
				"source_version": source.receipt_hash,
				"source_reference": source.reference,
				"evidence_json": json.dumps(
					pointer,
					ensure_ascii=False,
					sort_keys=True,
					separators=(",", ":"),
				),
				"context_summary": "Immutable task-bound AI access receipt accepted by a named reviewer.",
				"captured_at": recorded_at,
				"recorded_at": recorded_at,
			}
		)
		doc.insert(ignore_permissions=True)


def _normalize_references(
	references: Iterable[str] | str,
	*,
	max_references: int,
) -> list[str]:
	if isinstance(references, str):
		references = parse_stored_references(references)
	if not isinstance(references, (list, tuple)):
		frappe.throw("Evidence references must be a list")
	if not references:
		frappe.throw("At least one traceable evidence receipt is required")
	if len(references) > max_references:
		frappe.throw(f"No more than {max_references} evidence references are allowed")
	output: list[str] = []
	seen: set[str] = set()
	for raw_reference in references:
		reference = str(raw_reference or "").strip()
		if not reference or len(reference) > 350 or any(ord(character) < 32 for character in reference):
			frappe.throw("Evidence references must be bounded printable text")
		doctype, separator, name = reference.partition(":")
		if not separator or doctype != RECEIPT_DOCTYPE or not name or len(name) > 200:
			frappe.throw(f"Evidence references must use a canonical '{RECEIPT_DOCTYPE}:name' receipt")
		canonical = f"{RECEIPT_DOCTYPE}:{name}"
		if canonical not in seen:
			seen.add(canonical)
			output.append(canonical)
	return output


def _validate_receipt(task: Any, reference: str) -> ValidatedEvidenceReference:
	_receipt_doctype, receipt_name = reference.split(":", 1)
	fields = [
		"task",
		"policy",
		"result",
		"hospital",
		"campus",
		"department",
		"ward",
		"patient",
		"encounter",
		"source_doctype",
		"source_name",
		"source_version",
		"source_record_hash",
		"content_hash",
		"snapshot_json",
		"record_hash",
	]
	receipt = frappe.db.get_value(RECEIPT_DOCTYPE, receipt_name, fields, as_dict=True)
	if not receipt:
		frappe.throw(f"AI evidence receipt does not exist: {reference}")
	if receipt.get("task") != task.name or receipt.get("policy") != task.get("policy"):
		frappe.throw(
			"AI evidence receipt belongs to a different governed task",
			frappe.PermissionError,
		)
	if receipt.get("result") != "Success":
		frappe.throw("Only successful AI data-access receipts may be cited")
	source_doctype = str(receipt.get("source_doctype") or "")
	source_name = str(receipt.get("source_name") or "")
	if source_doctype not in _ALLOWED_SOURCE_TYPES or not source_name:
		frappe.throw("AI evidence receipt has no approved source identity")
	snapshot_json = str(receipt.get("snapshot_json") or "")
	content_hash = str(receipt.get("content_hash") or "")
	if not snapshot_json or not content_hash or not receipt.get("record_hash"):
		frappe.throw("AI evidence receipt is missing immutable snapshot integrity fields")
	if not _is_sha256(content_hash) or not _is_sha256(str(receipt.get("record_hash") or "")):
		frappe.throw("AI evidence receipt has malformed immutable integrity fields")
	if (
		snapshot_json != EVIDENCE_SNAPSHOT_REDACTED
		and hashlib.sha256(snapshot_json.encode()).hexdigest() != content_hash
	):
		frappe.throw("AI evidence receipt snapshot failed its content-hash check")
	if source_doctype not in _GOVERNANCE_SOURCE_TYPES:
		_validate_receipt_scope(task, receipt)
	return ValidatedEvidenceReference(
		reference=reference,
		receipt_name=receipt_name,
		receipt_hash=str(receipt.record_hash),
		source_doctype=source_doctype,
		source_name=source_name,
		source_version=str(receipt.get("source_version") or ""),
		content_hash=content_hash,
		source_record_hash=str(receipt.get("source_record_hash") or ""),
	)


def _validate_receipt_scope(task: Any, receipt: Any) -> None:
	for fieldname in _SCOPE_FIELDS:
		expected = str(task.get(fieldname) or "")
		if not expected:
			continue
		actual = str(receipt.get(fieldname) or "")
		if actual != expected:
			frappe.throw(
				f"AI evidence receipt is outside the task {fieldname} scope",
				frappe.PermissionError,
			)


def _is_sha256(value: str) -> bool:
	return len(value) == 64 and all(character in "0123456789abcdef" for character in value.lower())
