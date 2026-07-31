from __future__ import annotations

from typing import Any

import frappe

from ione_qms.services.medical_record_review import (
	MAX_ARCHIVE_ACK_BYTES,
	MAX_ARCHIVE_PULL_BYTES,
	acknowledge_archive_decision,
	claim_coder_assignment,
	claim_expert_assignment,
	create_review_batch,
	operate_sampling_policy,
	override_archive_decision,
	receive_completeness_watermark,
	reconcile_archive_decisions,
	retrieve_archive_decisions,
	review_sampling_policy,
	submit_coder_review,
	submit_expert_review,
)


@frappe.whitelist(methods=["POST"])
def review_medical_record_sampling_policy(
	policy: str,
	decision: str,
	review_comment: str,
) -> dict[str, str]:
	return review_sampling_policy(policy, decision, review_comment)


@frappe.whitelist(methods=["POST"])
def operate_medical_record_sampling_policy(
	policy: str,
	action: str,
	operation_comment: str,
) -> dict[str, str]:
	return operate_sampling_policy(policy, action, operation_comment)


@frappe.whitelist(methods=["POST"])
def create_medical_record_review_batch(
	policy: str,
	period_start: str,
	period_end: str,
	completeness_watermark: str,
	parent_batch: str | None = None,
) -> dict[str, Any]:
	return create_review_batch(
		policy,
		period_start,
		period_end,
		completeness_watermark,
		parent_batch=parent_batch,
	)


@frappe.whitelist(methods=["POST"])
def claim_medical_record_coder_assignment(assignment: str) -> dict[str, str]:
	return claim_coder_assignment(assignment)


@frappe.whitelist(methods=["POST"])
def claim_medical_record_expert_assignment(assignment: str) -> dict[str, str]:
	return claim_expert_assignment(assignment)


@frappe.whitelist(methods=["POST"])
def submit_medical_record_coder_review(
	assignment: str,
	outcome: str,
	comment: str,
	finding: str | None = None,
	evidence_references: list[str] | None = None,
) -> dict[str, str]:
	return submit_coder_review(
		assignment,
		outcome,
		comment,
		finding=finding,
		evidence_references=evidence_references,
	)


@frappe.whitelist(methods=["POST"])
def submit_medical_record_expert_review(
	assignment: str,
	outcome: str,
	comment: str,
	finding: str | None = None,
	evidence_references: list[str] | None = None,
) -> dict[str, str]:
	return submit_expert_review(
		assignment,
		outcome,
		comment,
		finding=finding,
		evidence_references=evidence_references,
	)


@frappe.whitelist(methods=["POST"])
def override_medical_record_archive_decision(
	archive_decision: str,
	target_decision: str,
	reason_code: str,
	comment: str,
) -> dict[str, str]:
	return override_archive_decision(
		archive_decision,
		target_decision,
		reason_code,
		comment,
	)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_medical_record_completeness_watermark(
	endpoint: str | None = None,
) -> dict[str, str]:
	endpoint_name = _endpoint_name(endpoint)
	request = getattr(frappe.local, "request", None)
	if request is None:
		frappe.throw("Medical-record completeness watermark requires an HTTP request body.")
	if not bool(getattr(request, "is_json", False)):
		frappe.local.response["http_status_code"] = 415
		frappe.throw("Medical-record completeness watermark requires application/json.")
	content_length = getattr(request, "content_length", None)
	if content_length is not None:
		if type(content_length) is not int or content_length < 0:
			frappe.throw("Medical-record completeness watermark Content-Length is invalid.")
		if content_length > MAX_ARCHIVE_PULL_BYTES:
			frappe.local.response["http_status_code"] = 413
			frappe.throw("Medical-record completeness watermark exceeds the 16 KiB ingress limit.")
	raw_body = request.get_data(cache=True, as_text=False)
	if type(raw_body) is not bytes:
		frappe.throw("Medical-record completeness watermark body is not an exact byte sequence.")
	if content_length is not None and len(raw_body) != content_length:
		frappe.throw("Medical-record completeness watermark body does not match Content-Length.")
	return receive_completeness_watermark(endpoint_name, raw_body)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_medical_record_archive_acknowledgement(
	endpoint: str | None = None,
) -> dict[str, str]:
	endpoint_name = _endpoint_name(endpoint)
	request = getattr(frappe.local, "request", None)
	if request is None:
		frappe.throw("Archive acknowledgement requires an HTTP request body.")
	if not bool(getattr(request, "is_json", False)):
		frappe.local.response["http_status_code"] = 415
		frappe.throw("Archive acknowledgement requires application/json.")
	content_length = getattr(request, "content_length", None)
	if content_length is not None:
		if type(content_length) is not int or content_length < 0:
			frappe.throw("Archive acknowledgement Content-Length is invalid.")
		if content_length > MAX_ARCHIVE_ACK_BYTES:
			frappe.local.response["http_status_code"] = 413
			frappe.throw("Archive acknowledgement exceeds the 64 KiB ingress limit.")
	raw_body = request.get_data(cache=True, as_text=False)
	if type(raw_body) is not bytes:
		frappe.throw("Archive acknowledgement body is not an exact byte sequence.")
	if content_length is not None and len(raw_body) != content_length:
		frappe.throw("Archive acknowledgement body does not match Content-Length.")
	return acknowledge_archive_decision(endpoint_name, raw_body)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def pull_medical_record_archive_decisions(
	endpoint: str | None = None,
) -> dict[str, Any]:
	endpoint_name = _endpoint_name(endpoint)
	request = getattr(frappe.local, "request", None)
	if request is None:
		frappe.throw("Archive-decision retrieval requires an HTTP request body.")
	if not bool(getattr(request, "is_json", False)):
		frappe.local.response["http_status_code"] = 415
		frappe.throw("Archive-decision retrieval requires application/json.")
	content_length = getattr(request, "content_length", None)
	if content_length is not None:
		if type(content_length) is not int or content_length < 0:
			frappe.throw("Archive-decision retrieval Content-Length is invalid.")
		if content_length > MAX_ARCHIVE_PULL_BYTES:
			frappe.local.response["http_status_code"] = 413
			frappe.throw("Archive-decision retrieval exceeds the 16 KiB ingress limit.")
	raw_body = request.get_data(cache=True, as_text=False)
	if type(raw_body) is not bytes:
		frappe.throw("Archive-decision retrieval body is not an exact byte sequence.")
	if content_length is not None and len(raw_body) != content_length:
		frappe.throw("Archive-decision retrieval body does not match Content-Length.")
	return retrieve_archive_decisions(endpoint_name, raw_body)


@frappe.whitelist(methods=["POST"])
def reconcile_medical_record_archive_decisions(
	endpoint: str,
	request_id: str,
	hospital: str,
	campus: str | None = None,
	department: str | None = None,
	ward: str | None = None,
	limit: int = 50,
) -> dict[str, Any]:
	return reconcile_archive_decisions(
		endpoint,
		request_id,
		hospital,
		campus=campus,
		department=department,
		ward=ward,
		limit=limit,
	)


def _endpoint_name(argument: str | None) -> str:
	argument_name = str(argument or "")
	header_name = str(frappe.get_request_header("X-IONE-Endpoint") or "")
	if argument_name != argument_name.strip() or header_name != header_name.strip():
		frappe.throw("Archive acknowledgement endpoint is invalid.")
	if argument_name and header_name and argument_name != header_name:
		frappe.throw("Archive acknowledgement endpoint selectors do not match.")
	name = argument_name or header_name
	if not name:
		frappe.throw("Archive acknowledgement endpoint is required.")
	return name
