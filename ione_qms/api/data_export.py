from __future__ import annotations

import json
from typing import Any

import frappe

from ione_qms.services.data_export import (
	approve_export_request,
	reject_export_request,
)


@frappe.whitelist(methods=["POST"])
def request_data_export(
	reference_doctype: str,
	filters: dict[str, Any] | str,
	fields: list[str] | str,
	reason: str,
	classification: str = "Sensitive Medical",
) -> dict[str, Any]:
	doc = frappe.get_doc(
		{
			"doctype": "IONE Data Export Request",
			"reference_doctype": reference_doctype,
			"filters_json": _json_value(filters),
			"fields_json": _json_value(fields),
			"reason": reason,
			"classification": classification,
		}
	)
	doc.insert()
	return {
		"request": doc.name,
		"request_id": doc.request_id,
		"status": doc.status,
		"requested_at": doc.requested_at,
		"scope_mode": doc.scope_mode,
		"hospital": doc.hospital,
		"campus": doc.campus,
		"department": doc.department,
	}


@frappe.whitelist(methods=["POST"])
def approve_data_export(request_name: str, comment: str) -> dict[str, Any]:
	return approve_export_request(request_name, comment)


@frappe.whitelist(methods=["POST"])
def reject_data_export(request_name: str, comment: str) -> dict[str, Any]:
	return reject_export_request(request_name, comment)


def _json_value(value: Any) -> str:
	if isinstance(value, str):
		return value
	return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
