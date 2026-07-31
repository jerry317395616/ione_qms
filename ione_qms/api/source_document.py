from __future__ import annotations

from typing import Any

import frappe

from ione_qms.services.source_document_locator import (
	approve_source_document_locator,
	locate_source_document,
	operate_source_document_locator,
)


@frappe.whitelist(methods=["POST"])
def approve_locator(locator: str, review_comment: str) -> dict[str, str]:
	return approve_source_document_locator(locator, review_comment)


@frappe.whitelist(methods=["POST"])
def operate_locator(
	locator: str,
	action: str,
	operation_comment: str,
) -> dict[str, str]:
	return operate_source_document_locator(locator, action, operation_comment)


@frappe.whitelist(methods=["POST"])
def locate_finding_evidence(finding: str, evidence: str) -> dict[str, Any]:
	return locate_source_document(finding, evidence)
