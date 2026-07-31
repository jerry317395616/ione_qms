from __future__ import annotations

from typing import Any

import frappe

from ione_qms.indicator_engine import (
	dimension_registry_contract,
	governed_dataset_contract,
)
from ione_qms.services.indicator_audit import (
	reproduce_historical_indicator_result as reproduce_authorized_historical_result,
)
from ione_qms.services.indicator_audit import (
	request_historical_indicator_audit,
	review_historical_indicator_audit,
)
from ione_qms.services.indicators import (
	current_indicator_result_lock,
	request_indicator_quarantine_disposition,
	review_indicator_quarantine_disposition,
	verify_indicator_result_integrity,
)


@frappe.whitelist(methods=["POST"])
def request_legacy_indicator_disposition(
	quarantine_receipt: str,
	action: str,
	request_reason: str,
	replacement_result: str | None = None,
) -> dict[str, Any]:
	return request_indicator_quarantine_disposition(
		quarantine_receipt,
		action,
		request_reason,
		replacement_result,
	)


@frappe.whitelist(methods=["POST"])
def review_legacy_indicator_disposition(
	disposition: str,
	approve: int,
	review_comment: str,
) -> dict[str, Any]:
	return review_indicator_quarantine_disposition(
		disposition,
		approve,
		review_comment,
	)


@frappe.whitelist(methods=["POST"])
def request_historical_indicator_verification(
	result: str,
	purpose: str,
	max_source_records: int = 10_000,
	max_reproductions: int = 1,
	ttl_hours: int = 24,
) -> dict[str, Any]:
	return request_historical_indicator_audit(
		result,
		purpose,
		max_source_records,
		max_reproductions,
		ttl_hours,
	)


@frappe.whitelist(methods=["POST"])
def review_historical_indicator_verification(
	authorization: str,
	approve: int,
	review_comment: str,
) -> dict[str, Any]:
	return review_historical_indicator_audit(authorization, approve, review_comment)


@frappe.whitelist(methods=["GET"])
def get_indicator_trend(
	indicator_code: str,
	department: str | None = None,
	periods: int = 12,
) -> dict[str, Any]:
	periods = min(max(int(periods or 12), 1), 60)
	indicator = frappe.db.get_value(
		"IONE QC Indicator",
		{"indicator_code": indicator_code},
		"name",
	)
	if not indicator:
		frappe.throw("Indicator not found", frappe.DoesNotExistError)
	versions = frappe.get_all(
		"IONE QC Indicator Version",
		filters={"indicator": indicator},
		pluck="name",
		limit_page_length=100,
	)
	pointer_filters: dict[str, Any] = {
		"indicator_version": ["in", versions or ["__none__"]],
	}
	if department:
		pointer_filters["department"] = department
	pointers = frappe.get_list(
		"IONE Indicator Result Pointer",
		filters=pointer_filters,
		fields=["current_result"],
		order_by="period_end desc",
		limit_page_length=periods,
	)
	current_results = [row.current_result for row in pointers if row.get("current_result")]
	if not current_results:
		return {
			"indicator": indicator,
			"indicator_code": indicator_code,
			"results": [],
		}
	results = frappe.get_list(
		"IONE Indicator Result",
		filters={"name": ["in", current_results]},
		fields=[
			"name",
			"indicator_version",
			"period_start",
			"period_end",
			"department",
			"numerator",
			"denominator",
			"indicator_value",
			"target_value",
			"status",
			"computed_at",
			"result_revision",
			"input_receipt_hash",
			"result_checksum",
		],
		order_by="period_end desc",
		limit_page_length=periods,
	)
	verified_results = []
	for row in results:
		with current_indicator_result_lock(row.name) as (_pointer, current):
			verified_results.append(
				{
					fieldname: current.get(fieldname)
					for fieldname in (
						"name",
						"indicator_version",
						"period_start",
						"period_end",
						"department",
						"numerator",
						"denominator",
						"indicator_value",
						"target_value",
						"status",
						"computed_at",
						"result_revision",
						"input_receipt_hash",
						"result_checksum",
					)
				}
			)
	return {
		"indicator": indicator,
		"indicator_code": indicator_code,
		"results": list(reversed(verified_results)),
	}


@frappe.whitelist(methods=["GET"])
def get_indicator_dimension_contract() -> dict[str, Any]:
	"""Return the non-secret governed dimension/source/capability contract."""
	return {
		**dimension_registry_contract(),
		"source_query_contract": governed_dataset_contract(),
	}


@frappe.whitelist(methods=["POST"])
def verify_indicator_result(
	result: str,
	reproduce: int = 0,
	authorization: str | None = None,
) -> dict[str, Any]:
	"""Verify current lineage or consume an approved historical audit authorization."""
	try:
		reproduction_requested = int(reproduce or 0)
	except (TypeError, ValueError) as exc:
		raise ValueError("reproduce must be exactly 0 or 1.") from exc
	if reproduction_requested not in {0, 1}:
		raise ValueError("reproduce must be exactly 0 or 1.")
	if reproduction_requested:
		if not authorization:
			raise frappe.PermissionError(
				"Historical reproduction requires an approved dual-person authorization receipt."
			)
		return reproduce_authorized_historical_result(result, authorization)
	with current_indicator_result_lock(result) as (_pointer, doc):
		doc.check_permission("read")
		receipt = verify_indicator_result_integrity(doc)
	return {
		"verified": True,
		"input_receipt_hash": doc.get("input_receipt_hash"),
		"result_checksum": doc.get("result_checksum"),
		"revision": int(doc.get("result_revision") or 0),
		"source_manifest_hash": receipt.get("source_manifest_hash"),
		"source_manifest_count": receipt.get("source_manifest_count"),
	}
