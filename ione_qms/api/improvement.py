from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Any

import frappe
from frappe.model.workflow import apply_workflow
from frappe.utils import getdate, nowdate

from ione_qms.permissions import require_department, require_role
from ione_qms.services.finding_appeals import (
	discard_unbound_finding_appeal_evidence as discard_unbound_finding_appeal_evidence_service,
)
from ione_qms.services.finding_appeals import (
	review_finding_appeal as review_finding_appeal_service,
)
from ione_qms.services.finding_appeals import (
	submit_finding_appeal as submit_finding_appeal_service,
)
from ione_qms.services.finding_appeals import (
	withdraw_finding_appeal as withdraw_finding_appeal_service,
)
from ione_qms.services.improvement import active_rectification_name
from ione_qms.setup.workflows import RECTIFICATION_SUBMIT_ACTION


@frappe.whitelist(methods=["POST"])
def submit_finding_appeal(
	finding: str,
	appeal_type: str,
	reason: str,
	evidence_summary: str | None = None,
	evidence_file: str | None = None,
	evidence_content_hash: str | None = None,
) -> dict[str, str]:
	return submit_finding_appeal_service(
		finding=finding,
		appeal_type=appeal_type,
		reason=reason,
		evidence_summary=evidence_summary,
		evidence_file=evidence_file,
		evidence_content_hash=evidence_content_hash,
	)


@frappe.whitelist(methods=["POST"])
def discard_unbound_finding_appeal_evidence(
	evidence_file: str,
	evidence_content_hash: str,
) -> dict[str, str]:
	return discard_unbound_finding_appeal_evidence_service(
		evidence_file=evidence_file,
		evidence_content_hash=evidence_content_hash,
	)


@frappe.whitelist(methods=["POST"])
def review_finding_appeal(
	appeal: str,
	decision: str,
	review_comment: str,
) -> dict[str, str]:
	return review_finding_appeal_service(
		appeal=appeal,
		decision=decision,
		review_comment=review_comment,
	)


@frappe.whitelist(methods=["POST"])
def withdraw_finding_appeal(
	appeal: str,
	withdraw_comment: str,
) -> dict[str, str]:
	return withdraw_finding_appeal_service(
		appeal=appeal,
		withdraw_comment=withdraw_comment,
	)


@frappe.whitelist(methods=["POST"])
def submit_rectification(
	finding: str,
	plan: str,
	due_date: str,
	actions: list[dict[str, Any]] | str | None = None,
) -> dict[str, str]:
	finding_name = str(finding or "").strip()
	if not finding_name:
		frappe.throw("A quality finding is required")
	if getdate(due_date) < getdate(nowdate()):
		frappe.throw("Rectification due date cannot be in the past")
	if len(str(plan).strip()) < 10:
		frappe.throw("Rectification plan must contain at least 10 characters")
	action_rows = frappe.parse_json(actions) if isinstance(actions, str) else actions or []
	if not isinstance(action_rows, list) or len(action_rows) > 100:
		frappe.throw("Rectification actions must be an array with no more than 100 rows")

	# Fail unauthorized requests before taking a database lock, then repeat every
	# stateful check after the parent row is locked.
	finding_doc = frappe.get_doc("IONE QC Finding", finding_name)
	_validate_rectification_request_actor(finding_doc)

	lock_name = f"ione-qms:rectification-create:{finding_name}"
	with frappe.db.advisory_lock(lock_name, timeout=15):
		try:
			with _atomic_rectification_create(_rectification_savepoint(finding_name)):
				locked_finding = frappe.db.get_value(
					"IONE QC Finding",
					finding_name,
					"name",
					for_update=True,
				)
				if not locked_finding:
					frappe.throw(
						"The quality finding no longer exists",
						frappe.DoesNotExistError,
					)
				finding_doc.reload()
				_validate_rectification_request_actor(finding_doc)

				existing = active_rectification_name(finding_doc.name)
				if existing:
					return {"rectification": existing, "finding": finding_doc.name}

				rectification = frappe.get_doc(
					{
						"doctype": "IONE QC Rectification",
						"finding": finding_doc.name,
						"active_key": finding_doc.name,
						"hospital": finding_doc.get("hospital"),
						"campus": finding_doc.get("campus"),
						"department": finding_doc.get("department"),
						"assigned_to": frappe.session.user,
						"plan": plan,
						"due_date": due_date,
						"status": "Draft",
						"actions": action_rows,
					}
				)
				rectification.insert()
				rectification = apply_workflow(rectification, RECTIFICATION_SUBMIT_ACTION)
				return {"rectification": rectification.name, "finding": finding_doc.name}
		except frappe.DuplicateEntryError:
			# The unique active_key is the final concurrency barrier for direct
			# REST inserts and the narrow release-before-request-commit window.
			existing = active_rectification_name(finding_name)
			if existing:
				return {"rectification": existing, "finding": finding_name}
			raise


def _validate_rectification_request_actor(finding_doc) -> None:
	finding_doc.check_permission("read")
	require_department(finding_doc.department)
	require_role(
		"IONE Physician",
		"IONE Department Director",
		"IONE Department QC Officer",
	)
	if finding_doc.status not in {
		"Confirmed",
		"Pending Rectification",
		"Rectifying",
		"Rework",
	}:
		frappe.throw("The finding is not in a rectifiable state")


def _rectification_savepoint(finding: str) -> str:
	suffix = hashlib.sha256(finding.encode()).hexdigest()[:16]
	return f"ione_rectification_create_{suffix}"


@contextmanager
def _atomic_rectification_create(savepoint: str):
	frappe.db.savepoint(savepoint)
	try:
		yield
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	else:
		frappe.db.release_savepoint(savepoint)
