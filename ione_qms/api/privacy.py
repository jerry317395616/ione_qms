from __future__ import annotations

from typing import Any

import frappe

from ione_qms.services.phi_access import (
	approve_phi_disclosure_policy as _approve_phi_disclosure_policy,
)
from ione_qms.services.phi_access import (
	disclose_phi_identity,
)
from ione_qms.services.phi_access import (
	retire_phi_disclosure_policy as _retire_phi_disclosure_policy,
)
from ione_qms.services.phi_access import (
	submit_phi_disclosure_policy as _submit_phi_disclosure_policy,
)


@frappe.whitelist(methods=["POST"])
def read_phi_identity(
	reference_doctype: str,
	reference_name: str,
	fields: list[str] | str,
	purpose_code: str,
	request_id: str,
) -> dict[str, Any]:
	return disclose_phi_identity(
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		fields=fields,
		purpose_code=purpose_code,
		request_id=request_id,
	)


@frappe.whitelist(methods=["POST"])
def submit_phi_disclosure_policy(policy: str) -> dict[str, str]:
	return _submit_phi_disclosure_policy(policy)


@frappe.whitelist(methods=["POST"])
def approve_phi_disclosure_policy(policy: str, review_comment: str) -> dict[str, str]:
	return _approve_phi_disclosure_policy(policy, review_comment)


@frappe.whitelist(methods=["POST"])
def retire_phi_disclosure_policy(policy: str, retirement_reason: str) -> dict[str, str]:
	return _retire_phi_disclosure_policy(policy, retirement_reason)
