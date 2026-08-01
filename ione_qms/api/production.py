from __future__ import annotations

from typing import Any

import frappe

from ione_qms.services.production_readiness import (
	activate_production_mode as _activate_production_mode,
)
from ione_qms.services.production_readiness import (
	approve_production_readiness_assessment as _approve_assessment,
)
from ione_qms.services.production_readiness import (
	deactivate_production_mode as _deactivate_production_mode,
)
from ione_qms.services.production_readiness import (
	reject_production_readiness_assessment as _reject_assessment,
)
from ione_qms.services.production_readiness import (
	revoke_production_readiness_assessment as _revoke_assessment,
)
from ione_qms.services.production_readiness import (
	submit_production_readiness_assessment as _submit_assessment,
)


@frappe.whitelist(methods=["POST"])
def submit_assessment(assessment: str) -> dict[str, str]:
	return _submit_assessment(assessment)


@frappe.whitelist(methods=["POST"])
def approve_assessment(assessment: str, review_comment: str) -> dict[str, str]:
	return _approve_assessment(assessment, review_comment)


@frappe.whitelist(methods=["POST"])
def reject_assessment(assessment: str, review_comment: str) -> dict[str, str]:
	return _reject_assessment(assessment, review_comment)


@frappe.whitelist(methods=["POST"])
def activate_production_mode(
	assessment: str,
	reason: str,
	enable_realtime_rules: bool | int = False,
	enable_ai: bool | int = False,
) -> dict[str, Any]:
	return _activate_production_mode(
		assessment,
		reason,
		enable_realtime_rules=enable_realtime_rules,
		enable_ai=enable_ai,
	)


@frappe.whitelist(methods=["POST"])
def deactivate_production_mode(reason: str) -> dict[str, Any]:
	return _deactivate_production_mode(reason)


@frappe.whitelist(methods=["POST"])
def revoke_assessment(assessment: str, reason: str) -> dict[str, str]:
	return _revoke_assessment(assessment, reason)
