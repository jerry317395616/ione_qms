from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import frappe

from ione_qms.services.surgery_governance import (
	check_preoperative_release,
	complete_preoperative_checklist,
	create_mdt_record,
	request_emergency_exception,
	retire_surgery_governance_record,
	review_emergency_exception,
	review_staff_qualification,
	review_surgery_authorization,
	review_surgery_procedure_policy,
)


@frappe.whitelist(methods=["GET"])
def check_surgery_preoperative_release(surgery_qc: str) -> dict[str, Any]:
	return check_preoperative_release(surgery_qc)


@frappe.whitelist(methods=["POST"])
def review_staff_qualification_record(
	qualification: str,
	decision: str,
	review_comment: str,
) -> dict[str, str]:
	return review_staff_qualification(qualification, decision, review_comment)


@frappe.whitelist(methods=["POST"])
def review_surgery_authorization_record(
	authorization: str,
	decision: str,
	review_comment: str,
) -> dict[str, str]:
	return review_surgery_authorization(authorization, decision, review_comment)


@frappe.whitelist(methods=["POST"])
def review_surgery_procedure_policy_record(
	policy: str,
	decision: str,
	review_comment: str,
) -> dict[str, str]:
	return review_surgery_procedure_policy(policy, decision, review_comment)


@frappe.whitelist(methods=["POST"])
def retire_surgery_governance_definition(
	doctype: str,
	name: str,
	retirement_reason: str,
) -> dict[str, str]:
	return retire_surgery_governance_record(doctype, name, retirement_reason)


@frappe.whitelist(methods=["POST"])
def record_surgery_mdt(
	surgery_qc: str,
	meeting_at: str,
	completed_at: str,
	chair: str,
	conclusion: str,
	conclusion_summary: str,
	participants: Sequence[Mapping[str, Any]] | str,
	evidence_reference: str,
) -> dict[str, str]:
	return create_mdt_record(
		surgery_qc,
		meeting_at,
		completed_at,
		chair,
		conclusion,
		conclusion_summary,
		participants,
		evidence_reference,
	)


@frappe.whitelist(methods=["POST"])
def record_surgery_safety_checklist(
	surgery_qc: str,
	completed_at: str,
	items: Sequence[Mapping[str, Any]] | str,
	evidence_reference: str,
) -> dict[str, str]:
	return complete_preoperative_checklist(
		surgery_qc,
		completed_at,
		items,
		evidence_reference,
	)


@frappe.whitelist(methods=["POST"])
def request_surgery_emergency_exception(
	surgery_qc: str,
	requested_scopes: Sequence[str] | str,
	reason_code: str,
	reason: str,
	valid_from: str,
	valid_to: str,
) -> dict[str, str]:
	return request_emergency_exception(
		surgery_qc,
		requested_scopes,
		reason_code,
		reason,
		valid_from,
		valid_to,
	)


@frappe.whitelist(methods=["POST"])
def review_surgery_emergency_exception(
	exception: str,
	decision: str,
	review_comment: str,
) -> dict[str, str]:
	return review_emergency_exception(exception, decision, review_comment)
