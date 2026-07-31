from __future__ import annotations

from typing import Any

import frappe

from ione_qms.services.finding_recurrence import (
	operate_recurrence_policy,
	request_recurrence_policy_approval,
	review_recurrence_policy,
	run_finding_recurrence_scan,
)
from ione_qms.services.pdca_structure import (
	record_pdca_standardization,
	verify_pdca_effect_measurements,
)


@frappe.whitelist(methods=["POST"])
def request_finding_recurrence_policy_approval(
	policy: str,
	comment: str,
) -> dict[str, str]:
	return request_recurrence_policy_approval(policy, comment)


@frappe.whitelist(methods=["POST"])
def review_finding_recurrence_policy(
	policy: str,
	decision: str,
	review_comment: str,
) -> dict[str, str]:
	return review_recurrence_policy(policy, decision, review_comment)


@frappe.whitelist(methods=["POST"])
def operate_finding_recurrence_policy(
	policy: str,
	action: str,
	operation_comment: str,
) -> dict[str, str]:
	return operate_recurrence_policy(policy, action, operation_comment)


@frappe.whitelist(methods=["POST"])
def run_finding_recurrence_policy_scan(
	policy: str,
	as_of: str | None = None,
) -> dict[str, Any]:
	return run_finding_recurrence_scan(policy, as_of=as_of, trigger_source="Manual")


@frappe.whitelist(methods=["POST"])
def verify_pdca_measurements(
	project: str,
	measurement_codes: list[str] | str,
	verification_comment: str,
) -> dict[str, Any]:
	return verify_pdca_effect_measurements(project, measurement_codes, verification_comment)


@frappe.whitelist(methods=["POST"])
def record_pdca_standardization_decision(
	project: str,
	decision: str,
	conclusion: str,
	evidence_reference: str,
	evidence_hash: str,
) -> dict[str, str]:
	return record_pdca_standardization(
		project,
		decision,
		conclusion,
		evidence_reference,
		evidence_hash,
	)
