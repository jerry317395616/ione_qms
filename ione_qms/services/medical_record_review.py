from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import get_datetime, get_system_timezone, getdate, now_datetime

from ione_qms.integration.signing import verify_request
from ione_qms.permissions import require_role, require_scope_read
from ione_qms.services.integration_config import assert_integration_endpoint_runtime
from ione_qms.services.integration_scope import authorize_endpoint_scope

SAMPLING_POLICY_DOCTYPE = "IONE Medical Record Sampling Policy"
POLICY_OPERATION_DOCTYPE = "IONE Medical Record Sampling Policy Operation"
REVIEW_BATCH_DOCTYPE = "IONE Medical Record Review Batch"
REVIEW_ASSIGNMENT_DOCTYPE = "IONE Medical Record Review Assignment"
REVIEW_DECISION_DOCTYPE = "IONE Medical Record Review Decision"
ARCHIVE_DECISION_DOCTYPE = "IONE Medical Record Archive Decision"
ARCHIVE_ACK_DOCTYPE = "IONE Medical Record Archive Acknowledgement"
ARCHIVE_DELIVERY_DOCTYPE = "IONE Medical Record Archive Delivery"
COMPLETENESS_WATERMARK_DOCTYPE = "IONE Medical Record Completeness Watermark"
MEDICAL_RECORD_QC_DOCTYPE = "IONE Medical Record QC"

MAX_POPULATION_RECORDS = 50_000
MAX_POPULATION_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_EVIDENCE_REFERENCES = 50
MAX_ARCHIVE_ACK_BYTES = 64 * 1024
MAX_ARCHIVE_PULL_BYTES = 16 * 1024
MAX_ARCHIVE_PULL_LIMIT = 100
MAX_ARCHIVE_PULL_SCAN = 5_000
MAX_MANDATORY_EXECUTION_RECEIPTS_PER_RULE = 20
MAX_BATCH_CHAIN_LENGTH = 100
MAX_ARCHIVE_SUPERSESSION_DEPTH = 100
MAX_POLICY_OPERATION_DEPTH = 200
MAX_COMMENT_LENGTH = 5_000

POLICY_AUTHOR_ROLES = frozenset({"IONE QC Reviewer", "IONE Medical Affairs"})
POLICY_REVIEWER_ROLES = frozenset({"IONE QC Reviewer", "IONE Medical Affairs"})
POLICY_OPERATOR_ROLES = frozenset({"IONE Medical Affairs"})
CODER_ROLES = frozenset({"IONE Medical Record Coder"})
EXPERT_ROLES = frozenset({"IONE Medical Record Expert Reviewer"})
ARCHIVE_OVERRIDE_ROLES = frozenset({"IONE Medical Affairs"})
ARCHIVE_RECONCILIATION_ROLES = frozenset({"IONE Integration Operator"})

REVIEW_OUTCOMES = frozenset({"Pass", "Defect", "Needs Correction", "Unable to Determine"})
ARCHIVE_DECISIONS = frozenset({"Allow", "Hold"})
ACK_STATUSES = frozenset({"Applied", "Rejected", "Failed"})
ROUNDING_MODES = frozenset({"Ceiling", "Floor", "Half Up"})
UNDERSIZED_ACTIONS = frozenset({"Sample All", "Fail"})
EXPERT_NON_PASS_POLICIES = frozenset({"Always Expert Review", "Sample Only"})
POLICY_STATUSES = frozenset({"Draft", "Approved", "Rejected", "Suspended", "Retired"})

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_.:-]{1,63}$")
_SAFE_ACK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{15,127}$")
_EVIDENCE_REFERENCE_RE = re.compile(r"^IONE QC Finding Evidence:[A-Za-z0-9._:-]{1,140}$")

_SCOPE_FIELDS = ("hospital", "campus", "department", "ward")
_POLICY_SEMANTIC_FIELDS = (
	"policy_code",
	"policy_version",
	"source_system",
	"hospital",
	"campus",
	"department",
	"ward",
	"effective_from",
	"effective_to",
	"eligible_record_status",
	"population_date_field",
	"sample_rate_basis_points",
	"minimum_sample_size",
	"maximum_sample_size",
	"expert_sample_rate_basis_points",
	"expert_minimum_sample_size",
	"expert_maximum_sample_size",
	"rounding_mode",
	"undersized_population_action",
	"expert_non_pass_policy",
	"archive_gate_enabled",
	"mandatory_rule_manifest_json",
	"mandatory_rule_manifest_hash",
	"auto_allow_deterministic_pass",
	"coder_allow_outcomes_json",
	"expert_allow_outcomes_json",
	"sampling_key_id",
	"sampling_key_fingerprint",
)
_POLICY_GOVERNANCE_FIELDS = frozenset(
	{
		"status",
		"enabled",
		"requested_by",
		"requested_at",
		"reviewed_by",
		"reviewed_at",
		"review_comment",
		"approval_checksum",
		"operated_by",
		"operated_at",
		"operation_comment",
		"latest_operation",
	}
)
_POLICY_OPERATION_FIELDS = frozenset(
	{
		"operation_key",
		"operation_checksum",
		"policy",
		"policy_checksum",
		"source_system",
		"hospital",
		"campus",
		"department",
		"ward",
		"action",
		"from_status",
		"from_enabled",
		"to_status",
		"to_enabled",
		"operation_comment",
		"operated_by",
		"operated_at",
		"previous_operation",
	}
)
_BATCH_IMMUTABLE_FIELDS = frozenset(
	{
		"batch_key",
		"batch_kind",
		"parent_batch",
		"supplement_sequence",
		"policy",
		"policy_checksum",
		"period_start",
		"period_end",
		"hospital",
		"campus",
		"department",
		"ward",
		"population_count",
		"population_manifest_json",
		"population_hash",
		"sample_count",
		"selection_manifest_json",
		"selection_hash",
		"expert_sample_count",
		"expert_selection_manifest_json",
		"expert_selection_hash",
		"sampling_key_id",
		"sampling_key_fingerprint",
		"source_system",
		"source_completeness_watermark",
		"source_complete_through",
		"source_completeness_hash",
		"created_by",
		"created_at",
	}
)
_BATCH_RUNTIME_FIELDS = frozenset({"status", "completed_at"})
_ASSIGNMENT_IMMUTABLE_FIELDS = frozenset(
	{
		"assignment_key",
		"batch",
		"policy",
		"medical_record_qc",
		"clinical_event",
		"population_token",
		"record_snapshot_hash",
		"sampling_score_hash",
		"coder_required",
		"expert_required",
		"mandatory_rule_manifest_hash",
		"mandatory_execution_manifest_json",
		"mandatory_execution_manifest_hash",
		"deterministic_gate_status",
		"deterministic_gate_reason_code",
		"hospital",
		"campus",
		"department",
		"ward",
		"patient",
		"encounter",
		"responsible_staff",
		"source_system",
		"source_record_type",
		"source_record_id_hash",
	}
)
_ASSIGNMENT_RUNTIME_FIELDS = frozenset(
	{
		"status",
		"assigned_coder",
		"coder_claimed_at",
		"coder_decision",
		"coder_outcome",
		"coder_reviewed_at",
		"assigned_expert",
		"expert_claimed_at",
		"expert_decision",
		"expert_outcome",
		"expert_reviewed_at",
		"latest_archive_decision",
		"archive_acknowledgement",
		"archive_ack_status",
		"pending_archive_delivery",
		"pending_archive_envelope_hash",
	}
)
_ASSIGNMENT_ARCHIVE_RUNTIME_FIELDS = frozenset(
	{
		"latest_archive_decision",
		"archive_acknowledgement",
		"archive_ack_status",
		"pending_archive_delivery",
		"pending_archive_envelope_hash",
	}
)
_ASSIGNMENT_DECISION_RUNTIME_FIELDS = frozenset(
	{
		"latest_archive_decision",
		"archive_acknowledgement",
		"archive_ack_status",
		"pending_archive_delivery",
		"pending_archive_envelope_hash",
	}
)
_ASSIGNMENT_ACK_RUNTIME_FIELDS = frozenset(
	{
		"archive_acknowledgement",
		"archive_ack_status",
		"pending_archive_delivery",
		"pending_archive_envelope_hash",
	}
)
_ASSIGNMENT_DELIVERY_RUNTIME_FIELDS = frozenset({"pending_archive_delivery", "pending_archive_envelope_hash"})
_MEDICAL_RECORD_REVIEW_ANCHOR_FIELDS = frozenset(
	{
		"record_key",
		"record_status",
		"event",
		"source_system",
		"source_record_type",
		"source_record_id_hash",
		"source_finalized_at",
		"record_snapshot_hash",
		"hospital",
		"campus",
		"department",
		"ward",
		"patient",
		"encounter",
		"responsible_staff",
	}
)
_ARCHIVE_DELIVERY_FIELDS = frozenset(
	{
		"delivery_key",
		"request_id",
		"request_hash",
		"endpoint",
		"source_system",
		"hospital",
		"campus",
		"department",
		"ward",
		"delivery_mode",
		"decision_count",
		"decision_manifest_json",
		"envelope_count",
		"envelope_manifest_json",
		"response_json",
		"response_hash",
		"delivery_status",
		"delivered_by",
		"delivered_at",
	}
)
_COMPLETENESS_WATERMARK_FIELDS = frozenset(
	{
		"watermark_key",
		"watermark_checksum",
		"source_watermark_id",
		"endpoint",
		"source_system",
		"hospital",
		"campus",
		"department",
		"ward",
		"complete_through",
		"period_start",
		"period_end",
		"eligible_record_status",
		"population_date_field",
		"source_sequence",
		"source_record_count",
		"source_manifest_hash",
		"request_hash",
		"source_asserted_at",
		"received_at",
	}
)

_POLICY_TRANSITION_CAPABILITY = object()
_POLICY_OPERATION_CREATION_CAPABILITY = object()
_BATCH_CREATION_CAPABILITY = object()
_BATCH_RUNTIME_CAPABILITY = object()
_ASSIGNMENT_CREATION_CAPABILITY = object()
_ASSIGNMENT_RUNTIME_CAPABILITY = object()
_REVIEW_DECISION_CREATION_CAPABILITY = object()
_ARCHIVE_DECISION_CREATION_CAPABILITY = object()
_ARCHIVE_ACK_CREATION_CAPABILITY = object()
_ARCHIVE_DELIVERY_CREATION_CAPABILITY = object()
_COMPLETENESS_WATERMARK_CREATION_CAPABILITY = object()


def prepare_sampling_policy(doc, method: str | None = None) -> None:
	"""Force a new hospital sampling policy to begin as an inert draft."""
	del method
	if not doc.is_new():
		return
	user = _named_role_user(POLICY_AUTHOR_ROLES)
	doc.requested_by = user
	doc.requested_at = now_datetime()
	doc.status = "Draft"
	doc.enabled = 0
	for fieldname in _POLICY_GOVERNANCE_FIELDS - {
		"requested_by",
		"requested_at",
		"status",
		"enabled",
	}:
		doc.set(fieldname, None)


def validate_sampling_policy(doc, method: str | None = None) -> None:
	"""Validate explicit sampling semantics and block direct governance changes."""
	del method
	_normalize_mandatory_rule_manifest(doc)
	_validate_policy_definition(doc, require_activation_ready=False)
	user = _named_role_user(POLICY_AUTHOR_ROLES)
	require_scope_read(**_scope(doc), user=user)
	previous = doc.get_doc_before_save()
	if previous is None:
		if str(doc.get("status") or "") != "Draft" or int(doc.get("enabled") or 0):
			frappe.throw("New medical-record sampling policies must be disabled Draft records.")
		if str(doc.get("requested_by") or "") != user:
			frappe.throw(
				"Sampling-policy requester must match the named creating user.",
				frappe.PermissionError,
			)
		if any(
			doc.get(fieldname) not in (None, "", 0, "0")
			for fieldname in _POLICY_GOVERNANCE_FIELDS - {"requested_by", "requested_at", "status", "enabled"}
		):
			frappe.throw("New sampling policies cannot contain review or operation state.")
		return

	transition_allowed = (
		getattr(frappe.flags, "ione_medical_record_policy_transition", None) is _POLICY_TRANSITION_CAPABILITY
	)
	if str(doc.get("requested_by") or "") != str(previous.get("requested_by") or ""):
		frappe.throw("Sampling-policy requester is immutable.")
	semantic_changes = _changed_fields(doc, previous, frozenset(_POLICY_SEMANTIC_FIELDS))
	governance_changes = _changed_fields(doc, previous, _POLICY_GOVERNANCE_FIELDS)
	if str(previous.get("status") or "") != "Draft" and semantic_changes:
		frappe.throw("Reviewed sampling-policy semantics are immutable; create a new version.")
	if transition_allowed and semantic_changes:
		frappe.throw("A governed policy transition cannot alter sampling semantics.")
	if governance_changes and not transition_allowed:
		frappe.throw("Sampling-policy state may only change through governed service methods.")
	status = str(doc.get("status") or "")
	if status not in POLICY_STATUSES:
		frappe.throw("Medical-record sampling policy status is invalid.")
	if status in {"Approved", "Suspended", "Retired"}:
		_assert_policy_checksum(doc)
		if not doc.get("reviewed_by") or not doc.get("reviewed_at"):
			frappe.throw("Reviewed sampling policies require independent review provenance.")
	if status == "Rejected":
		if not doc.get("reviewed_by") or not doc.get("reviewed_at"):
			frappe.throw("Rejected sampling policies require independent review provenance.")
		if doc.get("approval_checksum"):
			frappe.throw("Rejected sampling policies cannot retain an approval checksum.")
	latest_operation = str(doc.get("latest_operation") or "")
	if latest_operation:
		receipt = frappe.get_doc(POLICY_OPERATION_DOCTYPE, latest_operation)
		_assert_policy_operation_receipt(receipt, policy=str(doc.name))
		if (
			str(receipt.get("to_status") or "") != status
			or int(receipt.get("to_enabled") or 0) != int(doc.get("enabled") or 0)
			or str(receipt.get("operated_by") or "") != str(doc.get("operated_by") or "")
			or _site_naive_datetime(
				receipt.get("operated_at"),
				label="Sampling-policy receipt operated_at",
			)
			!= _site_naive_datetime(
				doc.get("operated_at"),
				label="Sampling-policy operated_at",
			)
			or str(receipt.get("operation_comment") or "") != str(doc.get("operation_comment") or "")
		):
			frappe.throw("Sampling-policy current state does not match its latest operation receipt.")
	elif (
		any(doc.get(fieldname) for fieldname in ("operated_by", "operated_at", "operation_comment"))
		or status in {"Suspended", "Retired"}
		or int(doc.get("enabled") or 0)
	):
		frappe.throw("Sampling-policy operated state is missing its append-only operation history.")
	if int(doc.get("enabled") or 0) and status != "Approved":
		frappe.throw("Only an Approved sampling policy may be enabled.")


def validate_sampling_policy_operation(doc, method: str | None = None) -> None:
	"""Accept only a service-created, immutable, contiguous policy transition receipt."""
	del method
	_require_creation_capability(
		doc,
		flag="ione_medical_record_policy_operation_creation",
		capability=_POLICY_OPERATION_CREATION_CAPABILITY,
		label="Sampling-policy operation receipts",
	)
	for fieldname in _POLICY_OPERATION_FIELDS - {
		"campus",
		"department",
		"ward",
		"previous_operation",
		"from_enabled",
		"to_enabled",
	}:
		if doc.get(fieldname) in (None, ""):
			frappe.throw(f"Sampling-policy operation receipt requires {fieldname}.")
	if type(doc.get("from_enabled")) is not int or type(doc.get("to_enabled")) is not int:
		frappe.throw("Sampling-policy operation receipt enabled states must be exact integers.")
	_validate_scope_shape(doc)
	_assert_policy_operation_receipt(doc, policy=str(doc.get("policy") or ""))
	policy = frappe.get_doc(SAMPLING_POLICY_DOCTYPE, doc.policy)
	if (
		str(policy.get("source_system") or "") != str(doc.get("source_system") or "")
		or any(
			str(policy.get(fieldname) or "") != str(doc.get(fieldname) or "") for fieldname in _SCOPE_FIELDS
		)
		or not hmac.compare_digest(
			str(policy.get("approval_checksum") or ""),
			str(doc.get("policy_checksum") or ""),
		)
		or str(policy.get("status") or "") != str(doc.get("from_status") or "")
		or int(policy.get("enabled") or 0) != int(doc.get("from_enabled") or 0)
		or str(policy.get("latest_operation") or "") != str(doc.get("previous_operation") or "")
	):
		frappe.throw("Sampling-policy operation receipt does not extend the locked policy state.")
	operated_at = _site_naive_datetime(
		doc.get("operated_at"),
		label="Sampling-policy operation operated_at",
	)
	if operated_at < _site_naive_datetime(
		policy.get("reviewed_at"),
		label="Sampling-policy reviewed_at",
	) or operated_at > _site_naive_datetime(
		now_datetime(),
		label="Sampling-policy operation validation time",
	) + timedelta(minutes=5):
		frappe.throw("Sampling-policy operation receipt time is outside its governance interval.")
	previous_name = str(doc.get("previous_operation") or "")
	if previous_name:
		previous = frappe.get_doc(POLICY_OPERATION_DOCTYPE, previous_name)
		_assert_policy_operation_receipt(previous, policy=str(policy.name))
		if (
			str(previous.get("to_status") or "") != str(doc.get("from_status") or "")
			or int(previous.get("to_enabled") or 0) != int(doc.get("from_enabled") or 0)
			or operated_at
			<= _site_naive_datetime(
				previous.get("operated_at"),
				label="Previous sampling-policy operation operated_at",
			)
		):
			frappe.throw("Sampling-policy operation receipt does not continue its predecessor.")
	elif (
		str(doc.get("from_status") or ""),
		int(doc.get("from_enabled") or 0),
	) != ("Approved", 0):
		frappe.throw("Sampling-policy operation receipt has no valid approved root state.")


def _policy_operation_values(receipt) -> dict[str, Any]:
	return {
		"policy": str(receipt.get("policy") or ""),
		"policy_checksum": str(receipt.get("policy_checksum") or ""),
		"source_system": str(receipt.get("source_system") or ""),
		**{fieldname: str(receipt.get(fieldname) or "") for fieldname in _SCOPE_FIELDS},
		"action": str(receipt.get("action") or ""),
		"from_status": str(receipt.get("from_status") or ""),
		"from_enabled": int(receipt.get("from_enabled") or 0),
		"to_status": str(receipt.get("to_status") or ""),
		"to_enabled": int(receipt.get("to_enabled") or 0),
		"operation_comment": str(receipt.get("operation_comment") or ""),
		"operated_by": str(receipt.get("operated_by") or ""),
		"operated_at": _site_naive_datetime(
			receipt.get("operated_at"),
			label="Sampling-policy operation operated_at",
		),
		"previous_operation": str(receipt.get("previous_operation") or "") or None,
	}


def _assert_policy_operation_receipt(receipt, *, policy: str) -> None:
	values = _policy_operation_values(receipt)
	if not policy or values["policy"] != policy:
		frappe.throw("Sampling-policy operation receipt crosses policies.")
	for fieldname in ("policy", "source_system", "hospital", "operated_by"):
		if not values[fieldname]:
			frappe.throw(f"Sampling-policy operation receipt requires {fieldname}.")
	if not _is_sha256(values["policy_checksum"]):
		frappe.throw("Sampling-policy operation receipt policy checksum is invalid.")
	comment = _bounded_comment(values["operation_comment"], minimum=20)
	if comment != values["operation_comment"]:
		frappe.throw("Sampling-policy operation receipt comment is not canonical.")
	targets = {
		("Approved", 0, "Activate"): ("Approved", 1),
		("Approved", 1, "Suspend"): ("Suspended", 0),
		("Suspended", 0, "Activate"): ("Approved", 1),
		("Approved", 0, "Retire"): ("Retired", 0),
		("Approved", 1, "Retire"): ("Retired", 0),
		("Suspended", 0, "Retire"): ("Retired", 0),
	}
	if targets.get((values["from_status"], values["from_enabled"], values["action"])) != (
		values["to_status"],
		values["to_enabled"],
	):
		frappe.throw("Sampling-policy operation receipt contains an invalid state transition.")
	operation_key = str(receipt.get("operation_key") or "")
	operation_checksum = str(receipt.get("operation_checksum") or "")
	if (
		not _is_sha256(operation_key)
		or not _is_sha256(operation_checksum)
		or not hmac.compare_digest(operation_key, _sha256(_canonical_json(values)))
		or not hmac.compare_digest(
			operation_checksum,
			_sha256(_canonical_json({"operation_key": operation_key, **values})),
		)
	):
		frappe.throw("Sampling-policy operation receipt checksum does not match its content.")


def review_sampling_policy(
	policy: str,
	decision: str,
	review_comment: str,
) -> dict[str, str]:
	"""Approve or reject one immutable hospital-authored sampling-policy version."""
	user = _named_role_user(POLICY_REVIEWER_ROLES)
	if decision not in {"Approve", "Reject"}:
		frappe.throw("Sampling-policy decision must be Approve or Reject.")
	comment = _bounded_comment(review_comment, minimum=20)
	with frappe.db.advisory_lock(f"ione-qms:record-policy-review:{policy}", timeout=10):
		doc = frappe.get_doc(SAMPLING_POLICY_DOCTYPE, policy, for_update=True)
		doc.check_permission("read")
		require_scope_read(**_scope(doc), user=user)
		if str(doc.get("status") or "") != "Draft":
			frappe.throw("Sampling policy has already been reviewed.")
		if str(doc.get("requested_by") or "") == user:
			frappe.throw("Sampling-policy authors cannot review their own policy.")
		if decision == "Approve":
			_validate_policy_definition(doc, require_activation_ready=True)
			doc.status = "Approved"
			doc.enabled = 0
			doc.approval_checksum = _policy_checksum(doc)
		else:
			doc.status = "Rejected"
			doc.enabled = 0
			doc.approval_checksum = None
		doc.reviewed_by = user
		doc.reviewed_at = now_datetime()
		doc.review_comment = comment
		with _policy_transition():
			doc.save(ignore_permissions=True)
		frappe.db.commit()
	return {"policy": doc.name, "status": str(doc.status), "enabled": str(int(doc.enabled or 0))}


def operate_sampling_policy(
	policy: str,
	action: str,
	operation_comment: str,
) -> dict[str, str]:
	"""Activate, suspend, or retire an independently approved policy."""
	user = _named_role_user(POLICY_OPERATOR_ROLES)
	if action not in {"Activate", "Suspend", "Retire"}:
		frappe.throw("Sampling-policy action must be Activate, Suspend, or Retire.")
	comment = _bounded_comment(operation_comment, minimum=20)
	with frappe.db.advisory_lock(f"ione-qms:record-policy-operation:{policy}", timeout=10):
		doc = frappe.get_doc(SAMPLING_POLICY_DOCTYPE, policy, for_update=True)
		doc.check_permission("read")
		require_scope_read(**_scope(doc), user=user)
		current = str(doc.get("status") or "")
		enabled = int(doc.get("enabled") or 0)
		targets = {
			("Approved", 0, "Activate"): ("Approved", 1),
			("Approved", 1, "Suspend"): ("Suspended", 0),
			("Suspended", 0, "Activate"): ("Approved", 1),
			("Approved", 0, "Retire"): ("Retired", 0),
			("Approved", 1, "Retire"): ("Retired", 0),
			("Suspended", 0, "Retire"): ("Retired", 0),
		}
		target = targets.get((current, enabled, action))
		if target is None:
			frappe.throw(f"Sampling policy cannot {action.lower()} from its current state.")
		if action == "Activate":
			_validate_policy_definition(doc, require_activation_ready=True)
			_assert_policy_checksum(doc)
			_sampling_secret(doc)
		operated_at = _site_naive_datetime(
			now_datetime(),
			label="Sampling-policy operation time",
		)
		previous_operation = str(doc.get("latest_operation") or "")
		if previous_operation:
			previous_receipt = frappe.get_doc(
				POLICY_OPERATION_DOCTYPE,
				previous_operation,
				for_update=True,
			)
			_assert_policy_operation_receipt(previous_receipt, policy=str(doc.name))
			if (
				str(previous_receipt.get("to_status") or "") != current
				or int(previous_receipt.get("to_enabled") or 0) != enabled
			):
				frappe.throw("Sampling-policy operation history does not reach the current state.")
		elif (current, enabled) != ("Approved", 0):
			frappe.throw("Sampling-policy operation history is missing its governed root.")
		receipt_values = {
			"policy": str(doc.name),
			"policy_checksum": str(doc.get("approval_checksum") or ""),
			"source_system": str(doc.get("source_system") or ""),
			**{fieldname: str(doc.get(fieldname) or "") for fieldname in _SCOPE_FIELDS},
			"action": action,
			"from_status": current,
			"from_enabled": enabled,
			"to_status": target[0],
			"to_enabled": target[1],
			"operation_comment": comment,
			"operated_by": user,
			"operated_at": operated_at,
			"previous_operation": previous_operation or None,
		}
		operation_key = _sha256(_canonical_json(receipt_values))
		receipt = frappe.get_doc(
			{
				"doctype": POLICY_OPERATION_DOCTYPE,
				"operation_key": operation_key,
				**receipt_values,
				"operation_checksum": _sha256(
					_canonical_json(
						{
							"operation_key": operation_key,
							**receipt_values,
						}
					)
				),
			}
		)
		with _creation_capability(
			"ione_medical_record_policy_operation_creation",
			_POLICY_OPERATION_CREATION_CAPABILITY,
		):
			receipt.insert(ignore_permissions=True)
		doc.status, doc.enabled = target
		doc.operated_by = user
		doc.operated_at = operated_at
		doc.operation_comment = comment
		doc.latest_operation = receipt.name
		with _policy_transition():
			doc.save(ignore_permissions=True)
		frappe.db.commit()
	return {
		"policy": doc.name,
		"operation": receipt.name,
		"status": str(doc.status),
		"enabled": str(int(doc.enabled or 0)),
	}


def receive_completeness_watermark(endpoint: str, raw_body: bytes) -> dict[str, str]:
	"""Persist one exact signed source assertion; never infer source completeness."""
	endpoint_name = str(endpoint or "").strip()
	if not endpoint_name:
		frappe.throw("Medical-record completeness watermark endpoint is required.")
	if not isinstance(raw_body, bytes):
		raise TypeError("Medical-record completeness watermark body must be exact bytes.")
	if not raw_body or len(raw_body) > MAX_ARCHIVE_PULL_BYTES:
		frappe.throw(
			f"Medical-record completeness watermark body must contain 1-{MAX_ARCHIVE_PULL_BYTES} bytes.",
			frappe.ValidationError,
		)
	endpoint_doc = frappe.get_cached_doc("IONE Integration Endpoint", endpoint_name)
	_validate_archive_pull_endpoint(endpoint_doc)
	verify_request(endpoint_doc, raw_body, force_signature=True)
	payload = _completeness_watermark_payload(raw_body)
	source = frappe.get_cached_doc("IONE Source System", endpoint_doc.source_system)
	scope = authorize_endpoint_scope(
		endpoint_doc,
		source,
		{
			**{fieldname: payload[fieldname] for fieldname in _SCOPE_FIELDS},
			"source_system": source.name,
		},
	)
	request_hash = _sha256_bytes(raw_body)
	watermark_key = _sha256(f"{endpoint_doc.name}|{payload['source_watermark_id']}")
	source_asserted_at = _site_naive_datetime(
		payload["source_asserted_at"],
		label="Medical-record completeness source_asserted_at",
		require_timezone=True,
	).replace(microsecond=0)
	received_at = _site_naive_datetime(
		now_datetime(),
		label="Medical-record completeness received_at",
	).replace(microsecond=0)
	if source_asserted_at > received_at + timedelta(minutes=5):
		frappe.throw("Medical-record completeness assertion time is in the future.")
	_assert_watermark_cutoff_time(
		source_asserted_at,
		getdate(payload["complete_through"]),
	)

	stream_lock = _completeness_stream_lock_key(source.name, scope)
	with frappe.db.advisory_lock(stream_lock, timeout=30):
		with frappe.db.advisory_lock(
			f"ione-qms:record-completeness-watermark:{watermark_key}",
			timeout=10,
		):
			existing_name = frappe.db.get_value(
				COMPLETENESS_WATERMARK_DOCTYPE,
				{"watermark_key": watermark_key},
				"name",
				for_update=True,
			)
			if existing_name:
				existing = frappe.get_doc(COMPLETENESS_WATERMARK_DOCTYPE, existing_name)
				if not hmac.compare_digest(str(existing.get("request_hash") or ""), request_hash):
					frappe.throw("Completeness watermark ID was replayed with different content.")
				_assert_watermark_checksum(existing)
				return {
					"watermark": str(existing.name),
					"complete_through": str(existing.complete_through),
					"source_sequence": str(int(existing.source_sequence)),
					"replay": "1",
				}
			latest = _latest_completeness_watermark(
				str(source.name),
				scope,
				for_update=True,
			)
			if latest and (
				int(payload["source_sequence"]) <= int(latest.get("source_sequence") or 0)
				or source_asserted_at
				<= _site_naive_datetime(
					latest.get("source_asserted_at"),
					label="Latest completeness source_asserted_at",
				)
				or getdate(payload["complete_through"]) < getdate(latest.get("complete_through"))
			):
				frappe.throw("Completeness watermark must advance sequence, assertion time, and cutoff.")
			doc = frappe.get_doc(
				{
					"doctype": COMPLETENESS_WATERMARK_DOCTYPE,
					"watermark_key": watermark_key,
					"source_watermark_id": payload["source_watermark_id"],
					"endpoint": endpoint_doc.name,
					"source_system": source.name,
					**{fieldname: scope.get(fieldname) for fieldname in _SCOPE_FIELDS},
					"complete_through": payload["complete_through"],
					"period_start": payload["period_start"],
					"period_end": payload["period_end"],
					"eligible_record_status": payload["eligible_record_status"],
					"population_date_field": payload["population_date_field"],
					"source_sequence": payload["source_sequence"],
					"source_record_count": payload["source_record_count"],
					"source_manifest_hash": payload["source_manifest_hash"],
					"request_hash": request_hash,
					"source_asserted_at": source_asserted_at,
					"received_at": received_at,
				}
			)
			doc.watermark_checksum = _watermark_checksum(doc)
			with _creation_capability(
				"ione_medical_record_completeness_watermark_creation",
				_COMPLETENESS_WATERMARK_CREATION_CAPABILITY,
			):
				doc.insert(ignore_permissions=True)
			frappe.db.commit()
	return {
		"watermark": str(doc.name),
		"complete_through": str(doc.complete_through),
		"source_sequence": str(int(doc.source_sequence)),
		"replay": "0",
	}


def create_review_batch(
	policy: str,
	period_start: str | date,
	period_end: str | date,
	completeness_watermark: str,
	*,
	parent_batch: str | None = None,
) -> dict[str, Any]:
	"""Freeze one permission-scoped population and create deterministic review assignments."""
	user = _named_role_user(POLICY_AUTHOR_ROLES)
	start, end = _closed_period(period_start, period_end)
	if end >= getdate(now_datetime()):
		frappe.throw("Medical-record review batches require a fully closed prior period.")
	policy_doc = frappe.get_doc(SAMPLING_POLICY_DOCTYPE, policy, for_update=True)
	policy_doc.check_permission("read")
	_validate_active_policy(policy_doc, start, end)
	scope = _scope(policy_doc)
	require_scope_read(**scope, user=user)
	chain_lock = _sha256(f"{policy_doc.name}|{start.isoformat()}|{end.isoformat()}")
	with (
		_locked_current_completeness_watermark(
			policy_doc,
			completeness_watermark,
			period_start=start,
			period_end=end,
		) as watermark,
		frappe.db.advisory_lock(
			f"ione-qms:record-review-chain:{chain_lock}",
			timeout=30,
		),
	):
		chain = _review_batch_chain(policy_doc.name, start, end)
		_validate_batch_chain_lineage(chain, policy_doc, start, end)
		parent = _validated_batch_parent(chain, parent_batch)
		batch_kind = "Supplemental" if parent else "Initial"
		supplement_sequence = int(parent.get("supplement_sequence") or 0) + 1 if parent else 0
		batch_key = _batch_key(
			policy_doc,
			start,
			end,
			watermark=watermark,
			parent_batch=str(parent.name) if parent else None,
			supplement_sequence=supplement_sequence,
		)
		existing_name = frappe.db.get_value(
			REVIEW_BATCH_DOCTYPE,
			{"batch_key": batch_key},
			"name",
			for_update=True,
		)
		if existing_name:
			existing = frappe.get_doc(REVIEW_BATCH_DOCTYPE, existing_name)
			_validate_existing_batch(
				existing,
				policy_doc,
				start,
				end,
				batch_key,
				watermark=watermark,
				parent_batch=str(parent.name) if parent else None,
				supplement_sequence=supplement_sequence,
			)
			_validate_batch_chain_coverage(
				chain,
				[_population_record(row) for row in _eligible_population(policy_doc, start, end)],
			)
			return _batch_result(existing, created=False)
		if chain and (not parent or str(parent.name) != str(chain[-1].name)):
			frappe.throw("A supplemental medical-record batch must extend the current chain tip.")
		if not chain and parent:
			frappe.throw("An initial medical-record batch cannot identify a parent batch.")
		if parent:
			_validate_watermark_progress(parent, watermark)

		rows = _eligible_population(policy_doc, start, end)
		_assert_source_population_watermark(
			watermark,
			rows,
			period_start=start,
			period_end=end,
		)
		secret = _sampling_secret(policy_doc)
		current_population = [_population_record(row) for row in rows]
		covered_tokens = _batch_chain_population_tokens(chain)
		population = [item for item in current_population if item["token"] not in covered_tokens]
		if parent and not population:
			frappe.throw("Supplemental batch creation found no uncovered late Final records.")
		if not parent and covered_tokens:
			frappe.throw("Initial batch creation encountered an existing population coverage chain.")
		population.sort(key=lambda item: item["token"])
		population_manifest_json = _canonical_json([item["token"] for item in population])
		if len(population_manifest_json.encode()) > MAX_POPULATION_MANIFEST_BYTES:
			frappe.throw(
				f"Frozen medical-record population manifest exceeds {MAX_POPULATION_MANIFEST_BYTES} bytes.",
				frappe.ValidationError,
			)
		population_hash = _sha256(population_manifest_json)

		sample_count = _sample_size(
			len(population),
			rate_basis_points=_required_int(policy_doc, "sample_rate_basis_points"),
			minimum=_required_int(policy_doc, "minimum_sample_size"),
			maximum=_required_int(policy_doc, "maximum_sample_size"),
			rounding_mode=str(policy_doc.get("rounding_mode") or ""),
			undersized_action=str(policy_doc.get("undersized_population_action") or ""),
		)
		ranked = _ranked_population(
			population,
			secret=secret,
			domain="primary",
			batch_key=batch_key,
		)
		selected = ranked[:sample_count]
		selected_tokens = {item["token"] for item in selected}

		expert_count = _sample_size(
			len(selected),
			rate_basis_points=_required_int(policy_doc, "expert_sample_rate_basis_points"),
			minimum=_required_int(policy_doc, "expert_minimum_sample_size"),
			maximum=_required_int(policy_doc, "expert_maximum_sample_size"),
			rounding_mode=str(policy_doc.get("rounding_mode") or ""),
			undersized_action=str(policy_doc.get("undersized_population_action") or ""),
		)
		expert_ranked = _ranked_population(
			selected,
			secret=secret,
			domain="expert",
			batch_key=batch_key,
		)
		expert_tokens = {item["token"] for item in expert_ranked[:expert_count]}

		selection_manifest_json = _canonical_json(sorted(item["token"] for item in selected))
		expert_manifest_json = _canonical_json(sorted(expert_tokens))
		savepoint = f"ione_record_batch_{batch_key[:16]}"
		frappe.db.savepoint(savepoint)
		try:
			batch = frappe.get_doc(
				{
					"doctype": REVIEW_BATCH_DOCTYPE,
					"batch_key": batch_key,
					"batch_kind": batch_kind,
					"parent_batch": parent.name if parent else None,
					"supplement_sequence": supplement_sequence,
					"policy": policy_doc.name,
					"policy_checksum": policy_doc.approval_checksum,
					"period_start": start,
					"period_end": end,
					"source_system": policy_doc.source_system,
					"source_completeness_watermark": watermark.name,
					"source_complete_through": watermark.complete_through,
					"source_completeness_hash": watermark.watermark_checksum,
					**scope,
					"population_count": len(population),
					"population_manifest_json": population_manifest_json,
					"population_hash": population_hash,
					"sample_count": len(selected),
					"selection_manifest_json": selection_manifest_json,
					"selection_hash": _sha256(selection_manifest_json),
					"expert_sample_count": len(expert_tokens),
					"expert_selection_manifest_json": expert_manifest_json,
					"expert_selection_hash": _sha256(expert_manifest_json),
					"sampling_key_id": policy_doc.sampling_key_id,
					"sampling_key_fingerprint": policy_doc.sampling_key_fingerprint,
					"status": "In Review" if selected else "Completed",
					"completed_at": None if selected else now_datetime(),
					"created_by": user,
					"created_at": now_datetime(),
				}
			)
			with _creation_capability(
				"ione_medical_record_batch_creation",
				_BATCH_CREATION_CAPABILITY,
			):
				batch.insert(ignore_permissions=True)
			# Lock shared source projections in one global order so concurrent
			# batches from different policies cannot form an AB/BA row-lock cycle.
			for item in sorted(ranked, key=lambda candidate: str(candidate["row"].get("name") or "")):
				_create_assignment(
					batch,
					policy_doc,
					item,
					coder_required=item["token"] in selected_tokens,
					expert_required=item["token"] in expert_tokens,
				)
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			raise
		frappe.db.release_savepoint(savepoint)
		frappe.db.commit()
	return _batch_result(batch, created=True)


def claim_coder_assignment(assignment: str) -> dict[str, str]:
	"""Atomically claim one sampled record for a named medical-record coder."""
	user = _named_role_user(CODER_ROLES)
	return _claim_assignment(
		assignment,
		user=user,
		required_status="Coder Review",
		actor_field="assigned_coder",
		claimed_at_field="coder_claimed_at",
		other_actor_field=None,
	)


def claim_expert_assignment(assignment: str) -> dict[str, str]:
	"""Atomically claim an expert review while enforcing reviewer independence."""
	user = _named_role_user(EXPERT_ROLES)
	return _claim_assignment(
		assignment,
		user=user,
		required_status="Expert Review",
		actor_field="assigned_expert",
		claimed_at_field="expert_claimed_at",
		other_actor_field="assigned_coder",
	)


def submit_coder_review(
	assignment: str,
	outcome: str,
	comment: str,
	*,
	finding: str | None = None,
	evidence_references: Sequence[str] | None = None,
) -> dict[str, str]:
	"""Append one coder decision and route the assignment using its frozen policy."""
	user = _named_role_user(CODER_ROLES)
	return _submit_review(
		assignment,
		stage="Coder",
		user=user,
		outcome=outcome,
		comment=comment,
		finding=finding,
		evidence_references=evidence_references,
	)


def submit_expert_review(
	assignment: str,
	outcome: str,
	comment: str,
	*,
	finding: str | None = None,
	evidence_references: Sequence[str] | None = None,
) -> dict[str, str]:
	"""Append one independent expert decision and close the sampled assignment."""
	user = _named_role_user(EXPERT_ROLES)
	return _submit_review(
		assignment,
		stage="Expert",
		user=user,
		outcome=outcome,
		comment=comment,
		finding=finding,
		evidence_references=evidence_references,
	)


def override_archive_decision(
	archive_decision: str,
	target_decision: str,
	reason_code: str,
	comment: str,
) -> dict[str, str]:
	"""Create an independent, append-only override; never rewrite the original decision."""
	user = _named_role_user(ARCHIVE_OVERRIDE_ROLES)
	target = str(target_decision or "").strip()
	if target not in ARCHIVE_DECISIONS:
		frappe.throw("Archive override target must be Allow or Hold.")
	code = _safe_code(reason_code, label="Archive override reason code")
	rationale = _bounded_comment(comment, minimum=30)
	seed = frappe.db.get_value(
		ARCHIVE_DECISION_DOCTYPE,
		archive_decision,
		["assignment"],
		as_dict=True,
	)
	if not seed:
		frappe.throw("Archive override target does not exist.")
	record_name = frappe.db.get_value(
		REVIEW_ASSIGNMENT_DOCTYPE,
		seed["assignment"],
		"medical_record_qc",
	)
	if not record_name:
		frappe.throw("Archive override target has no medical-record assignment.")
	with _medical_record_archive_lock(str(record_name)):
		with frappe.db.advisory_lock(
			f"ione-qms:record-archive-override:{archive_decision}",
			timeout=10,
		):
			previous = frappe.get_doc(
				ARCHIVE_DECISION_DOCTYPE,
				archive_decision,
				for_update=True,
			)
			previous.check_permission("read")
			record_assignments = _locked_record_assignments(str(record_name))
			assignments_by_name = {str(candidate.name): candidate for candidate in record_assignments}
			assignment = assignments_by_name.get(str(previous.get("assignment") or ""))
			if assignment is None:
				frappe.throw("Archive override target left its record assignment set.")
			if str(assignment.get("medical_record_qc") or "") != str(record_name):
				frappe.throw("Archive override assignment changed during locking.")
			_require_assignment_access(assignment, user)
			override_key = _sha256(
				_canonical_json(
					{
						"supersedes": previous.name,
						"target": target,
						"reason_code": code,
						"comment": rationale,
						"actor": user,
					}
				)
			)
			existing = frappe.db.get_value(
				ARCHIVE_DECISION_DOCTYPE,
				{"archive_key": override_key},
				"name",
				for_update=True,
			)
			latest = str(assignment.get("latest_archive_decision") or "")
			if latest != previous.name:
				if existing and latest == str(existing):
					doc = frappe.get_doc(ARCHIVE_DECISION_DOCTYPE, existing)
					return {
						"assignment": assignment.name,
						"archive_decision": doc.name,
						"decision": str(doc.decision),
						"decision_type": str(doc.decision_type),
					}
				frappe.throw("Only the latest archive decision may be overridden.")
			if assignment.get("pending_archive_delivery"):
				frappe.throw(
					"A delivered archive decision cannot be superseded before a Rejected or Failed ACK."
				)
			if str(assignment.get("archive_ack_status") or "") == "Applied":
				frappe.throw("An applied archive decision cannot be overridden in place.")
			if str(previous.get("decision") or "") == target:
				frappe.throw("Archive override must change the current decision.")
			policy_names = sorted(
				{
					str(candidate.get("policy") or "")
					for candidate in record_assignments
					if candidate.get("policy")
				}
			)
			if not policy_names:
				frappe.throw("Archive override record has no governed sampling policy.")
			with _policy_operation_locks(policy_names):
				policies = {
					policy_name: frappe.get_doc(
						SAMPLING_POLICY_DOCTYPE,
						policy_name,
						for_update=True,
					)
					for policy_name in policy_names
				}
				excluded_actors = _record_override_excluded_actors(
					record_assignments,
					policies,
				)
				if user in excluded_actors:
					frappe.throw(
						"Archive override requires an independent actor across all record governance."
					)
				if existing:
					doc = frappe.get_doc(ARCHIVE_DECISION_DOCTYPE, existing)
				else:
					doc = _new_archive_decision(
						assignment,
						review_decision=previous.review_decision,
						decision=target,
						decision_type="Override",
						reason_code=code,
						rationale=rationale,
						triggered_by=user,
						supersedes=previous.name,
						archive_key=override_key,
					)
				_update_assignment(
					assignment,
					{
						"latest_archive_decision": doc.name,
						"archive_acknowledgement": None,
						"archive_ack_status": None,
						"pending_archive_delivery": None,
						"pending_archive_envelope_hash": None,
					},
				)
				frappe.db.commit()
	return {
		"assignment": assignment.name,
		"archive_decision": doc.name,
		"decision": str(doc.decision),
		"decision_type": str(doc.decision_type),
	}


def _archive_supersession_chain(latest) -> list[Any]:
	assignment = str(latest.get("assignment") or "")
	if not assignment:
		frappe.throw("Archive decision chain has no assignment.")
	chain: list[Any] = []
	seen: set[str] = set()
	current = latest
	for _depth in range(MAX_ARCHIVE_SUPERSESSION_DEPTH):
		name = str(current.get("name") or "")
		if not name or name in seen:
			frappe.throw("Archive decision supersession chain contains a cycle.")
		if str(current.get("assignment") or "") != assignment:
			frappe.throw("Archive decision supersession chain crosses assignments.")
		seen.add(name)
		chain.append(current)
		prior_name = str(current.get("supersedes") or "")
		if not prior_name:
			if str(current.get("decision_type") or "") != "Deterministic Gate":
				frappe.throw("Archive decision supersession chain has no deterministic root.")
			return chain
		current = frappe.get_doc(
			ARCHIVE_DECISION_DOCTYPE,
			prior_name,
			for_update=True,
		)
	frappe.throw("Archive decision supersession chain exceeds its governed depth.")


def _record_override_excluded_actors(
	assignments: Sequence[Any],
	policies: Mapping[str, Any],
) -> set[str]:
	"""Collect every attributable actor across every assignment on one record."""
	if not assignments:
		frappe.throw("Archive override record has no governed assignments.")
	record_names = {str(assignment.get("medical_record_qc") or "") for assignment in assignments}
	if len(record_names) != 1 or not next(iter(record_names)):
		frappe.throw("Archive override assignment set crosses medical records.")
	excluded: set[str] = set()
	validated_policies: set[str] = set()
	for assignment in assignments:
		for fieldname in ("assigned_coder", "assigned_expert"):
			if assignment.get(fieldname):
				excluded.add(str(assignment.get(fieldname)))
		for stage, decision_field, actor_field, outcome_field, reviewed_at_field in (
			("Coder", "coder_decision", "assigned_coder", "coder_outcome", "coder_reviewed_at"),
			("Expert", "expert_decision", "assigned_expert", "expert_outcome", "expert_reviewed_at"),
		):
			decision_name = str(assignment.get(decision_field) or "")
			has_runtime_result = bool(assignment.get(outcome_field) or assignment.get(reviewed_at_field))
			if not decision_name:
				if has_runtime_result:
					frappe.throw("Archive override record has an incomplete review-decision link.")
				continue
			review = frappe.get_doc(REVIEW_DECISION_DOCTYPE, decision_name)
			reviewer = str(review.get("reviewed_by") or "")
			if (
				str(review.get("assignment") or "") != str(assignment.name)
				or str(review.get("stage") or "") != stage
				or not reviewer
				or not assignment.get(actor_field)
				or reviewer != str(assignment.get(actor_field) or "")
			):
				frappe.throw("Archive override record has inconsistent review actor lineage.")
			excluded.add(reviewer)

		latest_name = str(assignment.get("latest_archive_decision") or "")
		if not latest_name:
			frappe.throw("Archive override record has an assignment without a decision chain.")
		latest = frappe.get_doc(
			ARCHIVE_DECISION_DOCTYPE,
			latest_name,
			for_update=True,
		)
		if str(latest.get("assignment") or "") != str(assignment.name):
			frappe.throw("Archive override record latest decision crosses assignments.")
		for archive_decision in _archive_supersession_chain(latest):
			decision_actor = str(archive_decision.get("triggered_by") or "")
			if not decision_actor:
				frappe.throw("Archive decision chain has no attributable actor.")
			excluded.add(decision_actor)
			review_name = str(archive_decision.get("review_decision") or "")
			if review_name:
				review = frappe.get_doc(REVIEW_DECISION_DOCTYPE, review_name)
				reviewer = str(review.get("reviewed_by") or "")
				if str(review.get("assignment") or "") != str(assignment.name) or not reviewer:
					frappe.throw("Archive decision chain has a missing review actor.")
				excluded.add(reviewer)

		batch = frappe.get_doc(REVIEW_BATCH_DOCTYPE, assignment.batch)
		batch_creator = str(batch.get("created_by") or "")
		if not batch_creator or str(batch.get("policy") or "") != str(assignment.get("policy") or ""):
			frappe.throw("Archive override record has incomplete batch governance lineage.")
		excluded.add(batch_creator)

		policy_name = str(assignment.get("policy") or "")
		policy = policies.get(policy_name)
		if policy is None:
			frappe.throw("Archive override record has a missing locked policy.")
		if policy_name in validated_policies:
			continue
		for fieldname in ("requested_by", "reviewed_by"):
			actor = str(policy.get(fieldname) or "")
			if not actor:
				frappe.throw("Archive override record has incomplete policy governance actors.")
			excluded.add(actor)
		for operation in _policy_operation_chain(policy):
			actor = str(operation.get("operated_by") or "")
			if not actor:
				frappe.throw("Sampling-policy operation chain has no attributable actor.")
			excluded.add(actor)
		validated_policies.add(policy_name)
	return excluded


def _policy_operation_chain(policy) -> list[Any]:
	latest_name = str(policy.get("latest_operation") or "")
	if not latest_name:
		frappe.throw("Sampling policy is missing its append-only operation history.")
	chain: list[Any] = []
	seen: set[str] = set()
	expected_status = str(policy.get("status") or "")
	expected_enabled = int(policy.get("enabled") or 0)
	newer_operated_at = None
	current_name = latest_name
	for _depth in range(MAX_POLICY_OPERATION_DEPTH):
		if not current_name or current_name in seen:
			frappe.throw("Sampling-policy operation chain contains a cycle.")
		operation = frappe.get_doc(POLICY_OPERATION_DOCTYPE, current_name)
		if (
			str(operation.get("name") or "") != current_name
			or str(operation.get("operation_key") or "") != current_name
		):
			frappe.throw("Sampling-policy operation chain has an invalid receipt identity.")
		_assert_policy_operation_receipt(operation, policy=str(policy.name))
		operated_at = _site_naive_datetime(
			operation.get("operated_at"),
			label="Sampling-policy operation-chain operated_at",
		)
		if (
			str(operation.get("to_status") or "") != expected_status
			or int(operation.get("to_enabled") or 0) != expected_enabled
			or (newer_operated_at is not None and operated_at >= newer_operated_at)
			or not hmac.compare_digest(
				str(operation.get("policy_checksum") or ""),
				str(policy.get("approval_checksum") or ""),
			)
		):
			frappe.throw("Sampling-policy operation chain is not contiguous with policy state.")
		seen.add(current_name)
		chain.append(operation)
		newer_operated_at = operated_at
		expected_status = str(operation.get("from_status") or "")
		expected_enabled = int(operation.get("from_enabled") or 0)
		current_name = str(operation.get("previous_operation") or "")
		if not current_name:
			if (expected_status, expected_enabled) != ("Approved", 0):
				frappe.throw("Sampling-policy operation chain has no approved disabled root.")
			latest = chain[0]
			if (
				str(latest.get("operated_by") or "") != str(policy.get("operated_by") or "")
				or _site_naive_datetime(
					latest.get("operated_at"),
					label="Sampling-policy latest receipt operated_at",
				)
				!= _site_naive_datetime(
					policy.get("operated_at"),
					label="Sampling-policy operated_at",
				)
				or str(latest.get("operation_comment") or "") != str(policy.get("operation_comment") or "")
			):
				frappe.throw("Sampling policy does not mirror its latest operation receipt.")
			return chain
	frappe.throw("Sampling-policy operation chain exceeds its governed depth.")


@contextmanager
def _policy_operation_locks(policy_names: Sequence[str]) -> Iterator[None]:
	with ExitStack() as stack:
		for policy_name in sorted(set(policy_names)):
			stack.enter_context(
				frappe.db.advisory_lock(
					f"ione-qms:record-policy-operation:{policy_name}",
					timeout=10,
				)
			)
		yield


def acknowledge_archive_decision(
	endpoint: str,
	raw_body: bytes,
) -> dict[str, str]:
	"""Verify an exact signed source-system acknowledgement and append its receipt."""
	endpoint_name = str(endpoint or "").strip()
	if not endpoint_name:
		frappe.throw("Archive acknowledgement endpoint is required.")
	if not isinstance(raw_body, bytes):
		raise TypeError("Archive acknowledgement body must be exact bytes.")
	if not raw_body or len(raw_body) > MAX_ARCHIVE_ACK_BYTES:
		frappe.throw(
			f"Archive acknowledgement body must contain 1-{MAX_ARCHIVE_ACK_BYTES} bytes.",
			frappe.ValidationError,
		)
	endpoint_doc = frappe.get_cached_doc("IONE Integration Endpoint", endpoint_name)
	if (
		endpoint_doc.get("direction") != "Inbound"
		or endpoint_doc.get("authentication_type") != "HMAC"
		or not int(endpoint_doc.get("require_signature") or 0)
	):
		frappe.throw(
			"Archive acknowledgements require an enabled signed inbound endpoint.",
			frappe.PermissionError,
		)
	assert_integration_endpoint_runtime(endpoint_doc)
	verify_request(endpoint_doc, raw_body, force_signature=True)
	payload = _archive_ack_payload(raw_body)
	ack_key = _sha256(f"{endpoint_name}|{payload['source_ack_id']}")
	request_hash = _sha256_bytes(raw_body)
	seed = frappe.db.get_value(
		ARCHIVE_DECISION_DOCTYPE,
		payload["archive_decision"],
		["assignment"],
		as_dict=True,
	)
	if not seed:
		frappe.throw("Archive acknowledgement references an unknown decision.")
	record_name = frappe.db.get_value(
		REVIEW_ASSIGNMENT_DOCTYPE,
		seed["assignment"],
		"medical_record_qc",
	)
	if not record_name:
		frappe.throw("Archive acknowledgement decision has no medical-record assignment.")

	with _medical_record_archive_lock(str(record_name)):
		with frappe.db.advisory_lock(f"ione-qms:record-archive-ack:{ack_key}", timeout=10):
			existing_name = frappe.db.get_value(
				ARCHIVE_ACK_DOCTYPE,
				{"ack_key": ack_key},
				"name",
				for_update=True,
			)
			if existing_name:
				existing = frappe.get_doc(ARCHIVE_ACK_DOCTYPE, existing_name)
				if not hmac.compare_digest(str(existing.request_hash or ""), request_hash):
					frappe.throw("Archive acknowledgement ID was replayed with different content.")
				_assert_archive_ack_replay_current(
					existing,
					endpoint_doc,
					payload,
					record_name=str(record_name),
				)
				return _archive_ack_result(existing, replay=True)

			decision = frappe.get_doc(
				ARCHIVE_DECISION_DOCTYPE,
				payload["archive_decision"],
				for_update=True,
			)
			assignment = frappe.get_doc(
				REVIEW_ASSIGNMENT_DOCTYPE,
				decision.assignment,
				for_update=True,
			)
			if str(assignment.get("medical_record_qc") or "") != str(record_name):
				frappe.throw("Archive acknowledgement assignment changed during locking.")
			if str(assignment.get("latest_archive_decision") or "") != decision.name:
				frappe.throw("A superseded archive decision cannot receive a source acknowledgement.")
			if str(assignment.get("pending_archive_delivery") or "") != payload["delivery"]:
				frappe.throw("Archive acknowledgement does not match the assignment pending delivery.")
			if not hmac.compare_digest(
				str(assignment.get("pending_archive_envelope_hash") or ""),
				payload["envelope_hash"],
			):
				frappe.throw("Archive acknowledgement does not match the pending record envelope.")
			delivery = frappe.get_doc(
				ARCHIVE_DELIVERY_DOCTYPE,
				payload["delivery"],
				for_update=True,
			)
			_assert_delivery_contains_pending_decision(
				delivery,
				assignment,
				decision,
				envelope_hash=payload["envelope_hash"],
			)
			if assignment.get("archive_acknowledgement"):
				previous_ack = frappe.get_doc(
					ARCHIVE_ACK_DOCTYPE,
					assignment.archive_acknowledgement,
					for_update=True,
				)
				if str(previous_ack.get("ack_status") or "") == "Applied":
					frappe.throw("The latest archive decision already has an applied acknowledgement.")
			if str(endpoint_doc.get("source_system") or "") != str(decision.get("source_system") or ""):
				frappe.throw(
					"Archive acknowledgement endpoint does not belong to the decision source.",
					frappe.PermissionError,
				)
			if str(delivery.get("endpoint") or "") != str(endpoint_doc.name):
				frappe.throw("Archive acknowledgement endpoint does not match its delivery.")
			source_doc = frappe.get_cached_doc("IONE Source System", endpoint_doc.source_system)
			authorize_endpoint_scope(
				endpoint_doc,
				source_doc,
				{
					**_scope(decision),
					"source_system": decision.source_system,
				},
			)
			if not hmac.compare_digest(
				str(payload["decision_checksum"]),
				str(decision.get("decision_checksum") or ""),
			):
				frappe.throw("Archive acknowledgement decision checksum does not match.")
			if not hmac.compare_digest(
				str(payload["source_record_id_hash"]),
				str(decision.get("source_record_id_hash") or ""),
			):
				frappe.throw("Archive acknowledgement source identity does not match.")
			source_ack_at = _site_naive_datetime(
				payload["source_ack_at"],
				label="Archive acknowledgement source_ack_at",
				require_timezone=True,
			)
			decided_at = _site_naive_datetime(
				decision.get("decided_at"),
				label="Archive decision decided_at",
			)
			received_at = _site_naive_datetime(
				now_datetime(),
				label="Archive acknowledgement received_at",
			)
			if not decided_at or source_ack_at < decided_at:
				frappe.throw("Archive acknowledgement predates its decision.")
			if source_ack_at > received_at + timedelta(minutes=5):
				frappe.throw("Archive acknowledgement source time is in the future.")

			ack = frappe.get_doc(
				{
					"doctype": ARCHIVE_ACK_DOCTYPE,
					"ack_key": ack_key,
					"archive_decision": decision.name,
					"assignment": assignment.name,
					"delivery": delivery.name,
					"envelope_hash": payload["envelope_hash"],
					"endpoint": endpoint_doc.name,
					"source_system": endpoint_doc.source_system,
					"source_ack_id": payload["source_ack_id"],
					"source_ack_at": source_ack_at,
					"ack_status": payload["ack_status"],
					"message_code": payload["message_code"],
					"request_hash": request_hash,
					"decision_checksum": decision.decision_checksum,
					"source_record_id_hash": decision.source_record_id_hash,
					"received_at": received_at,
				}
			)
			with _creation_capability(
				"ione_medical_record_archive_ack_creation",
				_ARCHIVE_ACK_CREATION_CAPABILITY,
			):
				ack.insert(ignore_permissions=True)
			_update_assignment(
				assignment,
				{
					"archive_acknowledgement": ack.name,
					"archive_ack_status": ack.ack_status,
					"pending_archive_delivery": None,
					"pending_archive_envelope_hash": None,
				},
			)
			_apply_archive_projection_if_ready(assignment, decision, ack)
			frappe.db.commit()
	return _archive_ack_result(ack, replay=False)


def _apply_archive_projection_if_ready(assignment, decision, acknowledgement) -> None:
	if (
		str(decision.get("decision") or "") != "Allow"
		or str(acknowledgement.get("ack_status") or "") != "Applied"
	):
		return
	projection = frappe.get_doc(
		MEDICAL_RECORD_QC_DOCTYPE,
		assignment.medical_record_qc,
		for_update=True,
	)
	if str(projection.get("record_status") or "") == "Archived":
		return
	if str(projection.get("record_status") or "") != "Final":
		frappe.throw("Archive acknowledgement targets a non-Final medical-record projection.")
	if not _all_archive_gates_applied(projection):
		return
	from ione_qms.services.projections import apply_medical_record_archive_projection

	apply_medical_record_archive_projection(projection.name)


def retrieve_archive_decisions(endpoint: str, raw_body: bytes) -> dict[str, Any]:
	"""Return a bounded at-least-once decision batch to one signed source endpoint."""
	endpoint_name = str(endpoint or "").strip()
	if not endpoint_name:
		frappe.throw("Archive-decision retrieval endpoint is required.")
	if not isinstance(raw_body, bytes):
		raise TypeError("Archive-decision retrieval body must be exact bytes.")
	if not raw_body or len(raw_body) > MAX_ARCHIVE_PULL_BYTES:
		frappe.throw(
			f"Archive-decision retrieval body must contain 1-{MAX_ARCHIVE_PULL_BYTES} bytes.",
			frappe.ValidationError,
		)
	endpoint_doc = frappe.get_cached_doc("IONE Integration Endpoint", endpoint_name)
	_validate_archive_pull_endpoint(endpoint_doc)
	verify_request(endpoint_doc, raw_body, force_signature=True)
	payload = _archive_pull_payload(raw_body)
	return _deliver_archive_decisions(
		endpoint_doc,
		payload,
		request_hash=_sha256_bytes(raw_body),
		delivery_mode="Signed Pull",
		delivered_by=None,
	)


def reconcile_archive_decisions(
	endpoint: str,
	request_id: str,
	hospital: str,
	*,
	campus: str | None = None,
	department: str | None = None,
	ward: str | None = None,
	limit: int = 50,
) -> dict[str, Any]:
	"""Let an independent named Integration Operator inspect the exact pull queue."""
	user = _named_role_user(ARCHIVE_RECONCILIATION_ROLES)
	require_role("IONE Integration Operator", user=user)
	endpoint_doc = frappe.get_cached_doc("IONE Integration Endpoint", str(endpoint or "").strip())
	endpoint_doc.check_permission("read")
	_validate_archive_pull_endpoint(endpoint_doc)
	payload = _validated_archive_pull_payload(
		{
			"request_id": request_id,
			"hospital": hospital,
			"campus": campus or "",
			"department": department or "",
			"ward": ward or "",
			"limit": limit,
		}
	)
	raw_body = _canonical_json(payload).encode()
	return _deliver_archive_decisions(
		endpoint_doc,
		payload,
		request_hash=_sha256_bytes(raw_body),
		delivery_mode="Operator Reconciliation",
		delivered_by=user,
	)


def _validate_archive_pull_endpoint(endpoint) -> None:
	if (
		str(endpoint.get("direction") or "") != "Inbound"
		or str(endpoint.get("authentication_type") or "") != "HMAC"
		or not int(endpoint.get("require_signature") or 0)
	):
		frappe.throw(
			"Archive-decision pull requires an enabled signed inbound integration endpoint.",
			frappe.PermissionError,
		)
	assert_integration_endpoint_runtime(endpoint)


def _archive_pull_payload(raw_body: bytes) -> dict[str, Any]:
	try:
		text = raw_body.decode("utf-8", errors="strict")
		payload = json.loads(text, object_pairs_hook=_unique_json_object)
	except (UnicodeDecodeError, ValueError) as exc:
		raise frappe.ValidationError(
			"Archive-decision retrieval must be one canonical UTF-8 JSON object."
		) from exc
	validated = _validated_archive_pull_payload(payload)
	if _canonical_json(validated) != text:
		frappe.throw("Archive-decision retrieval JSON must use canonical serialization.")
	return validated


def _validated_archive_pull_payload(payload: Any) -> dict[str, Any]:
	required = {"request_id", "hospital", "campus", "department", "ward", "limit"}
	if not isinstance(payload, Mapping) or set(payload) != required:
		frappe.throw("Archive-decision retrieval has an invalid exact field contract.")
	for fieldname in required - {"limit"}:
		if not isinstance(payload.get(fieldname), str):
			frappe.throw("Archive-decision retrieval scope fields must be strings.")
		value = str(payload.get(fieldname) or "")
		if value != value.strip() or len(value) > 140:
			frappe.throw("Archive-decision retrieval contains a non-canonical scope value.")
	request_id = str(payload.get("request_id") or "")
	if not _SAFE_REQUEST_ID_RE.fullmatch(request_id):
		frappe.throw("Archive-decision retrieval request_id is invalid.")
	if not str(payload.get("hospital") or ""):
		frappe.throw("Archive-decision retrieval requires an exact hospital.")
	limit = payload.get("limit")
	if type(limit) is not int or not 1 <= limit <= MAX_ARCHIVE_PULL_LIMIT:
		frappe.throw(f"Archive-decision retrieval limit must be 1-{MAX_ARCHIVE_PULL_LIMIT}.")
	return {
		"campus": str(payload.get("campus") or ""),
		"department": str(payload.get("department") or ""),
		"hospital": str(payload.get("hospital") or ""),
		"limit": limit,
		"request_id": request_id,
		"ward": str(payload.get("ward") or ""),
	}


def _deliver_archive_decisions(
	endpoint,
	payload: Mapping[str, Any],
	*,
	request_hash: str,
	delivery_mode: str,
	delivered_by: str | None,
) -> dict[str, Any]:
	delivery_key = _sha256(f"{endpoint.name}|{payload['request_id']}")
	with frappe.db.advisory_lock(f"ione-qms:record-archive-delivery:{delivery_key}", timeout=10):
		existing_name = frappe.db.get_value(
			ARCHIVE_DELIVERY_DOCTYPE,
			{"delivery_key": delivery_key},
			"name",
		)
		if existing_name:
			existing = frappe.get_doc(ARCHIVE_DELIVERY_DOCTYPE, existing_name)
			if (
				not hmac.compare_digest(str(existing.get("request_hash") or ""), request_hash)
				or str(existing.get("delivery_mode") or "") != delivery_mode
				or str(existing.get("delivered_by") or "") != str(delivered_by or "")
			):
				frappe.throw("Archive-decision request ID was replayed with different authority or content.")
			stored_response = _decoded_delivery_response(existing)
			_assert_delivery_receipt_exact(existing, stored_response)
			if delivery_mode == "Signed Pull":
				_assert_signed_delivery_replay_pending(existing, stored_response)
			source = frappe.get_cached_doc("IONE Source System", endpoint.source_system)
			scope = _authorized_archive_delivery_scope(endpoint, source, payload)
			envelopes = _outstanding_archive_decisions(
				endpoint,
				source,
				scope,
				limit=int(payload["limit"]),
				allowed_pending_delivery=(str(existing.name) if delivery_mode == "Signed Pull" else "*"),
			)
			response = _archive_delivery_response(
				endpoint,
				payload,
				scope,
				envelopes,
				delivery_mode=delivery_mode,
				generated_at=str(stored_response.get("generated_at") or ""),
			)
			_assert_delivery_receipt_exact(existing, response)
			return stored_response

		source = frappe.get_cached_doc("IONE Source System", endpoint.source_system)
		scope = _authorized_archive_delivery_scope(endpoint, source, payload)
		envelopes = _outstanding_archive_decisions(
			endpoint,
			source,
			scope,
			limit=int(payload["limit"]),
			allowed_pending_delivery=None if delivery_mode == "Signed Pull" else "*",
		)
		delivered_at = _site_naive_datetime(
			now_datetime(),
			label="Archive delivery delivered_at",
		).replace(microsecond=0)
		response = _archive_delivery_response(
			endpoint,
			payload,
			scope,
			envelopes,
			delivery_mode=delivery_mode,
			generated_at=str(delivered_at),
		)
		response_json = _canonical_json(response)
		public_envelopes = response["envelopes"]
		decision_manifest_json = _canonical_json(
			sorted(
				str(item["archive_decision"])
				for envelope in public_envelopes
				for item in envelope["constituent_decisions"]
			)
		)
		envelope_manifest_json = _canonical_json(
			sorted(str(envelope["envelope_hash"]) for envelope in public_envelopes)
		)
		delivery = frappe.get_doc(
			{
				"doctype": ARCHIVE_DELIVERY_DOCTYPE,
				"delivery_key": delivery_key,
				"request_id": payload["request_id"],
				"request_hash": request_hash,
				"endpoint": endpoint.name,
				"source_system": endpoint.source_system,
				**{fieldname: scope.get(fieldname) for fieldname in _SCOPE_FIELDS},
				"delivery_mode": delivery_mode,
				"decision_count": response["decision_count"],
				"decision_manifest_json": decision_manifest_json,
				"envelope_count": response["envelope_count"],
				"envelope_manifest_json": envelope_manifest_json,
				"response_json": response_json,
				"response_hash": _sha256(response_json),
				"delivery_status": response["delivery_status"],
				"delivered_by": delivered_by,
				"delivered_at": delivered_at,
			}
		)
		with _creation_capability(
			"ione_medical_record_archive_delivery_creation",
			_ARCHIVE_DELIVERY_CREATION_CAPABILITY,
		):
			delivery.insert(ignore_permissions=True)
		if delivery_mode == "Signed Pull":
			for envelope in envelopes:
				for assignment in envelope["_pending_assignments"]:
					_update_assignment(
						assignment,
						{
							"pending_archive_delivery": delivery.name,
							"pending_archive_envelope_hash": envelope["envelope_hash"],
						},
					)
		frappe.db.commit()
	return response


def _assert_signed_delivery_replay_pending(
	delivery,
	response: Mapping[str, Any],
) -> None:
	"""Reject replay once any originally pending constituent has moved on."""
	pending_by_record: dict[str, list[tuple[str, str, Mapping[str, Any], Any]]] = {}
	for envelope in response["envelopes"]:
		envelope_hash = str(envelope["envelope_hash"])
		for item in envelope["constituent_decisions"]:
			if str(item.get("ack_required") or "") != "1":
				continue
			decision = frappe.get_doc(
				ARCHIVE_DECISION_DOCTYPE,
				str(item.get("archive_decision") or ""),
			)
			assignment_name = str(decision.get("assignment") or "")
			if not assignment_name:
				frappe.throw("Stored archive delivery has a decision without an assignment.")
			record_name = str(
				frappe.db.get_value(
					REVIEW_ASSIGNMENT_DOCTYPE,
					assignment_name,
					"medical_record_qc",
				)
				or ""
			)
			if not record_name:
				frappe.throw("Stored archive delivery has an assignment without a record.")
			pending_by_record.setdefault(record_name, []).append(
				(assignment_name, envelope_hash, item, decision)
			)

	for record_name in sorted(pending_by_record):
		with _medical_record_archive_lock(record_name):
			assignments = {
				str(assignment.name): assignment for assignment in _locked_record_assignments(record_name)
			}
			for assignment_name, envelope_hash, item, decision in pending_by_record[record_name]:
				assignment = assignments.get(assignment_name)
				if assignment is None:
					frappe.throw("Stored archive delivery assignment left its record lineage.")
				if (
					str(decision.get("assignment") or "") != assignment_name
					or str(assignment.get("latest_archive_decision") or "") != str(decision.name)
					or str(assignment.get("pending_archive_delivery") or "") != str(delivery.name)
					or not hmac.compare_digest(
						str(assignment.get("pending_archive_envelope_hash") or ""),
						envelope_hash,
					)
					or not hmac.compare_digest(
						str(item.get("decision_checksum") or ""),
						str(decision.get("decision_checksum") or ""),
					)
				):
					frappe.throw("Stored signed archive delivery is stale; retrieve a new delivery request.")


def _outstanding_archive_decisions(
	endpoint,
	source,
	scope: Mapping[str, str],
	*,
	limit: int,
	allowed_pending_delivery: str | None,
) -> list[dict[str, Any]]:
	filters: dict[str, Any] = {
		"source_system": source.name,
		"latest_archive_decision": ["is", "set"],
		"hospital": scope["hospital"],
		"status": "Closed",
	}
	for fieldname in ("campus", "department", "ward"):
		if scope.get(fieldname):
			filters[fieldname] = scope[fieldname]
	assignments = frappe.get_all(
		REVIEW_ASSIGNMENT_DOCTYPE,
		filters=filters,
		or_filters=[
			["archive_ack_status", "is", "not set"],
			["archive_ack_status", "in", ["Rejected", "Failed"]],
		],
		fields=[
			"name",
			"medical_record_qc",
			"latest_archive_decision",
			"archive_ack_status",
			"hospital",
			"campus",
			"department",
			"ward",
			"source_system",
		],
		order_by="name asc",
		limit_page_length=MAX_ARCHIVE_PULL_SCAN + 1,
	)
	if len(assignments) > MAX_ARCHIVE_PULL_SCAN:
		frappe.throw(
			"Archive-decision reconciliation scope exceeds the bounded scan; narrow the scope.",
			frappe.ValidationError,
		)
	record_names: set[str] = set()
	for assignment in assignments:
		if str(assignment.get("archive_ack_status") or "") == "Applied":
			continue
		authorize_endpoint_scope(
			endpoint,
			source,
			{
				**_scope(assignment),
				"source_system": assignment.get("source_system"),
			},
		)
		record_name = str(assignment.get("medical_record_qc") or "")
		if not record_name:
			frappe.throw("Outstanding archive decision has no medical-record projection.")
		record_names.add(record_name)
	envelopes: list[dict[str, Any]] = []
	for record_name in sorted(record_names):
		if len(envelopes) >= limit:
			break
		with _medical_record_archive_lock(record_name) as projection:
			locked_assignments = _locked_record_assignments(record_name)
			envelope = _effective_archive_envelope(
				projection,
				locked_assignments,
				endpoint=endpoint,
				source=source,
				allowed_pending_delivery=allowed_pending_delivery,
			)
			if envelope["_publishable"] and envelope["_pending_assignments"]:
				envelopes.append(envelope)
	return envelopes


def _authorized_archive_delivery_scope(endpoint, source, payload: Mapping[str, Any]) -> dict[str, str]:
	return authorize_endpoint_scope(
		endpoint,
		source,
		{
			"hospital": payload["hospital"],
			"campus": payload["campus"],
			"department": payload["department"],
			"ward": payload["ward"],
			"source_system": endpoint.source_system,
		},
	)


def _archive_delivery_response(
	endpoint,
	payload: Mapping[str, Any],
	scope: Mapping[str, str],
	envelopes: Sequence[Mapping[str, Any]],
	*,
	delivery_mode: str,
	generated_at: str,
) -> dict[str, Any]:
	public = [
		{key: value for key, value in envelope.items() if not str(key).startswith("_")}
		for envelope in envelopes
	]
	return {
		"contract_version": 2,
		"decision_count": sum(len(item["constituent_decisions"]) for item in public),
		"delivery_status": "Delivered" if delivery_mode == "Signed Pull" else "Inspected",
		"endpoint": str(endpoint.name),
		"envelope_count": len(public),
		"envelopes": public,
		"generated_at": generated_at,
		"request_id": str(payload["request_id"]),
		"scope": {fieldname: str(scope.get(fieldname) or "") for fieldname in _SCOPE_FIELDS},
		"source_system": str(endpoint.source_system),
	}


def _locked_record_assignments(record_name: str) -> list[Any]:
	names = frappe.get_all(
		REVIEW_ASSIGNMENT_DOCTYPE,
		filters={"medical_record_qc": record_name},
		pluck="name",
		order_by="name asc",
		limit_page_length=MAX_POPULATION_RECORDS + 1,
	)
	if not names or len(names) > MAX_POPULATION_RECORDS:
		frappe.throw("Medical-record release requires a bounded non-empty assignment set.")
	return [frappe.get_doc(REVIEW_ASSIGNMENT_DOCTYPE, name, for_update=True) for name in names]


def _effective_archive_envelope(
	projection,
	assignments: Sequence[Any],
	*,
	endpoint,
	source,
	allowed_pending_delivery: str | None,
) -> dict[str, Any]:
	constituents: list[dict[str, str]] = []
	pending_assignments: list[Any] = []
	all_closed = all(str(assignment.get("status") or "") == "Closed" for assignment in assignments)
	all_closed_allow = all_closed
	for assignment in assignments:
		if str(assignment.get("medical_record_qc") or "") != str(projection.name) or str(
			assignment.get("source_system") or ""
		) != str(source.name):
			frappe.throw("Record-level archive envelope assignment lineage is inconsistent.")
		authorize_endpoint_scope(
			endpoint,
			source,
			{**_scope(assignment), "source_system": assignment.source_system},
		)
		decision_name = str(assignment.get("latest_archive_decision") or "")
		if not decision_name:
			frappe.throw("Record-level archive envelope has an assignment without a disposition.")
		decision = frappe.get_doc(
			ARCHIVE_DECISION_DOCTYPE,
			decision_name,
			for_update=True,
		)
		if (
			str(decision.get("assignment") or "") != str(assignment.name)
			or str(decision.get("record_snapshot_hash") or "")
			!= str(projection.get("record_snapshot_hash") or "")
			or str(decision.get("source_record_id_hash") or "")
			!= str(projection.get("source_record_id_hash") or "")
		):
			frappe.throw("Record-level archive envelope decision lineage is inconsistent.")
		applied = _assignment_has_current_applied_ack(assignment, decision)
		if not applied:
			pending_delivery = str(assignment.get("pending_archive_delivery") or "")
			if (
				pending_delivery
				and allowed_pending_delivery != "*"
				and pending_delivery != str(allowed_pending_delivery or "")
			):
				frappe.throw("Archive decision already has a pending delivery; replay its exact request ID.")
			if all_closed:
				pending_assignments.append(assignment)
		all_closed_allow = all_closed_allow and (
			str(assignment.get("status") or "") == "Closed" and str(decision.get("decision") or "") == "Allow"
		)
		constituents.append(
			{
				"ack_required": "0" if applied else "1",
				"ack_status": str(assignment.get("archive_ack_status") or "Pending"),
				"archive_decision": str(decision.name),
				"decided_at": str(decision.get("decided_at") or ""),
				"decision": str(decision.get("decision") or ""),
				"decision_checksum": str(decision.get("decision_checksum") or ""),
				"decision_type": str(decision.get("decision_type") or ""),
				"reason_code": str(decision.get("reason_code") or ""),
			}
		)
	constituents.sort(key=lambda item: item["archive_decision"])
	semantic = {
		"constituent_decisions": constituents,
		"effective_decision": "Allow" if all_closed_allow else "Hold",
		"reason_code": "RECORD_GATES_ALLOW" if all_closed_allow else "RECORD_GATES_HOLD",
		"record_snapshot_hash": str(projection.get("record_snapshot_hash") or ""),
		"scope": {fieldname: str(projection.get(fieldname) or "") for fieldname in _SCOPE_FIELDS},
		"source_record_id_hash": str(projection.get("source_record_id_hash") or ""),
		"source_record_type": str(projection.get("source_record_type") or ""),
	}
	for fieldname in ("record_snapshot_hash", "source_record_id_hash"):
		if not _is_sha256(semantic[fieldname]):
			frappe.throw("Record-level archive envelope source hashes are invalid.")
	envelope_hash = _sha256(_canonical_json(semantic))
	return {
		**semantic,
		"envelope_hash": envelope_hash,
		"_pending_assignments": pending_assignments,
		"_publishable": all_closed,
	}


def _assignment_has_current_applied_ack(assignment, decision) -> bool:
	if str(assignment.get("archive_ack_status") or "") != "Applied":
		return False
	if not assignment.get("archive_acknowledgement"):
		frappe.throw("Applied archive state has no immutable acknowledgement receipt.")
	ack = frappe.get_doc(
		ARCHIVE_ACK_DOCTYPE,
		assignment.archive_acknowledgement,
	)
	if (
		str(ack.get("archive_decision") or "") != str(decision.name)
		or str(ack.get("assignment") or "") != str(assignment.name)
		or str(ack.get("ack_status") or "") != "Applied"
		or not hmac.compare_digest(
			str(ack.get("decision_checksum") or ""),
			str(decision.get("decision_checksum") or ""),
		)
		or not hmac.compare_digest(
			str(ack.get("source_record_id_hash") or ""),
			str(decision.get("source_record_id_hash") or ""),
		)
		or not ack.get("delivery")
		or not _is_sha256(str(ack.get("envelope_hash") or ""))
	):
		frappe.throw("Applied archive acknowledgement no longer matches the current decision.")
	delivery = frappe.get_doc(ARCHIVE_DELIVERY_DOCTYPE, ack.delivery)
	_assert_delivery_contains_pending_decision(
		delivery,
		assignment,
		decision,
		envelope_hash=str(ack.envelope_hash),
		require_pending=False,
	)
	return True


def validate_review_batch(doc, method: str | None = None) -> None:
	del method
	_validate_governed_artifact(
		doc,
		create_flag="ione_medical_record_batch_creation",
		create_capability=_BATCH_CREATION_CAPABILITY,
		immutable_fields=_BATCH_IMMUTABLE_FIELDS,
		runtime_fields=_BATCH_RUNTIME_FIELDS,
		runtime_flag="ione_medical_record_batch_runtime",
		runtime_capability=_BATCH_RUNTIME_CAPABILITY,
	)
	if not _is_sha256(str(doc.get("batch_key") or "")):
		frappe.throw("Medical-record review batch key is invalid.")
	for fieldname in (
		"policy_checksum",
		"population_hash",
		"selection_hash",
		"expert_selection_hash",
		"sampling_key_fingerprint",
		"source_completeness_hash",
	):
		if not _is_sha256(str(doc.get(fieldname) or "")):
			frappe.throw(f"Medical-record review batch {fieldname} is invalid.")
	status = str(doc.get("status") or "")
	if status not in {"In Review", "Completed"}:
		frappe.throw("Medical-record review batch status is invalid.")
	if (status == "Completed") != bool(doc.get("completed_at")):
		frappe.throw("Only a completed medical-record review batch may have completed_at.")
	previous = doc.get_doc_before_save()
	if (
		previous is not None
		and str(previous.get("status") or "") == "Completed"
		and _changed_fields(doc, previous, _BATCH_RUNTIME_FIELDS)
	):
		frappe.throw("A completed medical-record review batch is terminal.")
	batch_kind = str(doc.get("batch_kind") or "")
	sequence = int(doc.get("supplement_sequence") or 0)
	if batch_kind == "Initial":
		if doc.get("parent_batch") or sequence != 0:
			frappe.throw("An Initial medical-record review batch cannot have a parent.")
	elif batch_kind == "Supplemental":
		if not doc.get("parent_batch") or sequence < 1:
			frappe.throw("A Supplemental medical-record review batch requires an ordered parent.")
	else:
		frappe.throw("Medical-record review batch kind is invalid.")
	for fieldname in (
		"source_system",
		"source_completeness_watermark",
		"source_complete_through",
	):
		if not doc.get(fieldname):
			frappe.throw(f"Medical-record review batch requires {fieldname}.")
	policy = frappe.get_doc(SAMPLING_POLICY_DOCTYPE, doc.policy)
	watermark = frappe.get_doc(
		COMPLETENESS_WATERMARK_DOCTYPE,
		doc.source_completeness_watermark,
	)
	_assert_policy_checksum(policy)
	_assert_watermark_checksum(watermark)
	if (
		not hmac.compare_digest(
			str(doc.get("policy_checksum") or ""),
			str(policy.get("approval_checksum") or ""),
		)
		or str(doc.get("source_system") or "") != str(policy.get("source_system") or "")
		or str(watermark.get("source_system") or "") != str(doc.get("source_system") or "")
		or any(
			str(watermark.get(fieldname) or "") != str(doc.get(fieldname) or "")
			for fieldname in _SCOPE_FIELDS
		)
		or getdate(doc.get("source_complete_through")) != getdate(watermark.get("complete_through"))
		or getdate(doc.get("source_complete_through")) < getdate(doc.get("period_end"))
		or not hmac.compare_digest(
			str(doc.get("source_completeness_hash") or ""),
			str(watermark.get("watermark_checksum") or ""),
		)
	):
		frappe.throw("Medical-record review batch frozen source lineage is inconsistent.")
	parent = None
	if batch_kind == "Supplemental":
		parent = frappe.get_doc(REVIEW_BATCH_DOCTYPE, doc.parent_batch)
		if (
			str(parent.get("policy") or "") != str(doc.get("policy") or "")
			or getdate(parent.get("period_start")) != getdate(doc.get("period_start"))
			or getdate(parent.get("period_end")) != getdate(doc.get("period_end"))
			or int(parent.get("supplement_sequence") or 0) + 1 != sequence
		):
			frappe.throw("Supplemental medical-record review batch parent lineage is inconsistent.")
		_validate_watermark_progress(parent, watermark)
	expected_batch_key = _batch_key(
		policy,
		getdate(doc.period_start),
		getdate(doc.period_end),
		watermark=watermark,
		parent_batch=str(parent.name) if parent else None,
		supplement_sequence=sequence,
	)
	if not hmac.compare_digest(str(doc.get("batch_key") or ""), expected_batch_key):
		frappe.throw("Medical-record review batch key does not match its frozen source lineage.")
	for fieldname in (
		"population_manifest_json",
		"selection_manifest_json",
		"expert_selection_manifest_json",
	):
		_validate_hash_manifest(doc.get(fieldname), label=fieldname)
	population = _decoded_hash_manifest(doc.get("population_manifest_json"))
	selection = _decoded_hash_manifest(doc.get("selection_manifest_json"))
	expert_selection = _decoded_hash_manifest(doc.get("expert_selection_manifest_json"))
	if int(doc.get("population_count") or 0) != len(population):
		frappe.throw("Medical-record review population count does not match its manifest.")
	if int(doc.get("sample_count") or 0) != len(selection):
		frappe.throw("Medical-record review sample count does not match its manifest.")
	if int(doc.get("expert_sample_count") or 0) != len(expert_selection):
		frappe.throw("Medical-record expert sample count does not match its manifest.")
	if any(
		int(doc.get(fieldname) or 0) < 0
		for fieldname in ("population_count", "sample_count", "expert_sample_count")
	):
		frappe.throw("Medical-record review batch counts cannot be negative.")
	if not set(selection).issubset(population) or not set(expert_selection).issubset(selection):
		frappe.throw("Medical-record review sample manifests do not form valid subsets.")
	for fieldname, manifest_field in (
		("population_hash", "population_manifest_json"),
		("selection_hash", "selection_manifest_json"),
		("expert_selection_hash", "expert_selection_manifest_json"),
	):
		if not hmac.compare_digest(
			str(doc.get(fieldname) or ""),
			_sha256(str(doc.get(manifest_field) or "")),
		):
			frappe.throw(f"Medical-record review batch {fieldname} does not match its manifest.")


def validate_review_assignment(doc, method: str | None = None) -> None:
	del method
	_validate_governed_artifact(
		doc,
		create_flag="ione_medical_record_assignment_creation",
		create_capability=_ASSIGNMENT_CREATION_CAPABILITY,
		immutable_fields=_ASSIGNMENT_IMMUTABLE_FIELDS,
		runtime_fields=_ASSIGNMENT_RUNTIME_FIELDS,
		runtime_flag="ione_medical_record_assignment_runtime",
		runtime_capability=_ASSIGNMENT_RUNTIME_CAPABILITY,
	)
	if not _is_sha256(str(doc.get("assignment_key") or "")):
		frappe.throw("Medical-record review assignment key is invalid.")
	for fieldname in (
		"population_token",
		"record_snapshot_hash",
		"sampling_score_hash",
		"source_record_id_hash",
		"mandatory_rule_manifest_hash",
		"mandatory_execution_manifest_hash",
	):
		if not _is_sha256(str(doc.get(fieldname) or "")):
			frappe.throw(f"Medical-record review assignment {fieldname} is invalid.")
	for fieldname in ("coder_required", "expert_required"):
		if type(doc.get(fieldname)) is not int or int(doc.get(fieldname)) not in {0, 1}:
			frappe.throw(f"Medical-record review assignment {fieldname} must be an explicit Check value.")
	if int(doc.get("expert_required") or 0) and not int(doc.get("coder_required") or 0):
		frappe.throw("Expert sampling must be a subset of the coder cohort.")
	if str(doc.get("status") or "") not in {"Coder Review", "Expert Review", "Closed"}:
		frappe.throw("Medical-record review assignment status is invalid.")
	if not int(doc.get("coder_required") or 0) and str(doc.get("status") or "") != "Closed":
		frappe.throw("A non-coder population disposition must be Closed.")
	if str(doc.get("deterministic_gate_status") or "") not in {"Passed", "Held"}:
		frappe.throw("Medical-record review deterministic gate status is invalid.")
	_safe_code(
		doc.get("deterministic_gate_reason_code"),
		label="Medical-record deterministic gate reason code",
	)
	_validate_execution_manifest(
		doc.get("mandatory_execution_manifest_json"),
		expected_hash=str(doc.get("mandatory_execution_manifest_hash") or ""),
	)
	for fieldname in (
		"batch",
		"policy",
		"medical_record_qc",
		"hospital",
		"source_system",
		"source_record_type",
	):
		if not doc.get(fieldname):
			frappe.throw(f"Medical-record review assignment requires {fieldname}.")
	if doc.get("assigned_coder") and str(doc.get("assigned_coder")) == str(doc.get("assigned_expert") or ""):
		frappe.throw("Coder and expert reviewers must be independent named users.")
	previous = doc.get_doc_before_save()
	if (
		previous is not None
		and str(previous.get("status") or "") == "Closed"
		and str(doc.get("status") or "") != "Closed"
	):
		frappe.throw("A closed medical-record review assignment cannot be reopened.")
	if doc.get("archive_ack_status") and not doc.get("archive_acknowledgement"):
		frappe.throw("Archive acknowledgement status requires its immutable receipt.")
	if doc.get("archive_acknowledgement") and str(doc.get("archive_ack_status") or "") not in ACK_STATUSES:
		frappe.throw("Archive acknowledgement receipt requires a valid status.")
	pending_delivery = bool(doc.get("pending_archive_delivery"))
	pending_hash = str(doc.get("pending_archive_envelope_hash") or "")
	if pending_delivery != bool(pending_hash):
		frappe.throw("Pending archive delivery requires its exact envelope hash linkage.")
	if pending_hash and not _is_sha256(pending_hash):
		frappe.throw("Pending archive envelope hash is invalid.")
	if pending_delivery and str(doc.get("archive_ack_status") or "") == "Applied":
		frappe.throw("An Applied archive decision cannot remain pending delivery.")


def validate_review_decision(doc, method: str | None = None) -> None:
	del method
	_require_creation_capability(
		doc,
		flag="ione_medical_record_review_decision_creation",
		capability=_REVIEW_DECISION_CREATION_CAPABILITY,
		label="Medical-record review decisions",
	)
	if str(doc.get("stage") or "") not in {"Coder", "Expert"}:
		frappe.throw("Medical-record review decision stage is invalid.")
	if str(doc.get("outcome") or "") not in REVIEW_OUTCOMES:
		frappe.throw("Medical-record review decision outcome is invalid.")
	for fieldname in ("decision_key", "decision_checksum", "record_snapshot_hash"):
		if not _is_sha256(str(doc.get(fieldname) or "")):
			frappe.throw(f"Medical-record review decision {fieldname} is invalid.")
	for fieldname in (
		"assignment",
		"batch",
		"policy",
		"medical_record_qc",
		"reviewed_by",
		"reviewed_at",
		"hospital",
	):
		if not doc.get(fieldname):
			frappe.throw(f"Medical-record review decision requires {fieldname}.")
	comment = _bounded_comment(doc.get("comment"), minimum=10)
	if comment != str(doc.get("comment") or ""):
		frappe.throw("Medical-record review decision comment is not canonical.")
	try:
		decoded_references = json.loads(str(doc.get("evidence_references_json") or ""))
	except ValueError as exc:
		raise frappe.ValidationError(
			"Medical-record review decision evidence references must be canonical JSON."
		) from exc
	references = _evidence_references(decoded_references)
	if _canonical_json(list(references)) != str(doc.get("evidence_references_json") or ""):
		frappe.throw("Medical-record review decision evidence references are not canonical.")
	if str(doc.get("outcome") or "") != "Pass" and not doc.get("finding") and not references:
		frappe.throw("A non-pass medical-record review decision requires governed evidence.")
	semantic = {
		"decision_key": doc.get("decision_key"),
		"assignment": doc.get("assignment"),
		"batch": doc.get("batch"),
		"policy": doc.get("policy"),
		"medical_record_qc": doc.get("medical_record_qc"),
		"stage": doc.get("stage"),
		"outcome": doc.get("outcome"),
		"comment": doc.get("comment"),
		"finding": doc.get("finding"),
		"evidence_references_json": doc.get("evidence_references_json"),
		"reviewed_by": doc.get("reviewed_by"),
		"reviewed_at": doc.get("reviewed_at"),
		"record_snapshot_hash": doc.get("record_snapshot_hash"),
	}
	if not hmac.compare_digest(
		str(doc.get("decision_checksum") or ""),
		_sha256(_canonical_json(semantic)),
	):
		frappe.throw("Medical-record review decision checksum does not match its content.")


def validate_archive_decision(doc, method: str | None = None) -> None:
	del method
	_require_creation_capability(
		doc,
		flag="ione_medical_record_archive_decision_creation",
		capability=_ARCHIVE_DECISION_CREATION_CAPABILITY,
		label="Medical-record archive decisions",
	)
	if str(doc.get("decision") or "") not in ARCHIVE_DECISIONS:
		frappe.throw("Medical-record archive decision is invalid.")
	decision_type = str(doc.get("decision_type") or "")
	if decision_type not in {"Deterministic Gate", "Review Outcome", "Override"}:
		frappe.throw("Medical-record archive decision type is invalid.")
	for fieldname in (
		"archive_key",
		"decision_checksum",
		"policy_checksum",
		"record_snapshot_hash",
		"source_record_id_hash",
	):
		if not _is_sha256(str(doc.get(fieldname) or "")):
			frappe.throw(f"Medical-record archive decision {fieldname} is invalid.")
	for fieldname in (
		"assignment",
		"batch",
		"policy",
		"medical_record_qc",
		"triggered_by",
		"decided_at",
		"hospital",
		"source_system",
		"source_record_type",
	):
		if not doc.get(fieldname):
			frappe.throw(f"Medical-record archive decision requires {fieldname}.")
	_safe_code(doc.get("reason_code"), label="Medical-record archive decision reason code")
	rationale = _bounded_comment(doc.get("rationale"), minimum=10)
	if rationale != str(doc.get("rationale") or ""):
		frappe.throw("Medical-record archive decision rationale is not canonical.")
	if decision_type == "Deterministic Gate" and (doc.get("review_decision") or doc.get("supersedes")):
		frappe.throw("A deterministic gate decision cannot claim review or supersession.")
	if decision_type == "Review Outcome" and (not doc.get("review_decision") or not doc.get("supersedes")):
		frappe.throw("A review-outcome decision must replace the deterministic disposition.")
	if decision_type == "Override" and not doc.get("supersedes"):
		frappe.throw("An override archive decision must identify the superseded decision.")
	if doc.get("supersedes"):
		prior = frappe.get_doc(ARCHIVE_DECISION_DOCTYPE, doc.supersedes, for_update=True)
		if str(prior.get("assignment") or "") != str(doc.get("assignment") or ""):
			frappe.throw("Archive decision supersession cannot cross assignments.")
		_archive_supersession_chain(prior)
	semantic = {
		"archive_key": doc.get("archive_key"),
		"assignment": doc.get("assignment"),
		"batch": doc.get("batch"),
		"policy": doc.get("policy"),
		"policy_checksum": doc.get("policy_checksum"),
		"medical_record_qc": doc.get("medical_record_qc"),
		"review_decision": doc.get("review_decision"),
		"decision": doc.get("decision"),
		"decision_type": doc.get("decision_type"),
		"reason_code": doc.get("reason_code"),
		"rationale": doc.get("rationale"),
		"triggered_by": doc.get("triggered_by"),
		"decided_at": doc.get("decided_at"),
		"supersedes": doc.get("supersedes"),
		"record_snapshot_hash": doc.get("record_snapshot_hash"),
		"source_system": doc.get("source_system"),
		"source_record_type": doc.get("source_record_type"),
		"source_record_id_hash": doc.get("source_record_id_hash"),
	}
	if not hmac.compare_digest(
		str(doc.get("decision_checksum") or ""),
		_sha256(_canonical_json(semantic)),
	):
		frappe.throw("Medical-record archive decision checksum does not match its content.")


def validate_archive_acknowledgement(doc, method: str | None = None) -> None:
	del method
	_require_creation_capability(
		doc,
		flag="ione_medical_record_archive_ack_creation",
		capability=_ARCHIVE_ACK_CREATION_CAPABILITY,
		label="Medical-record archive acknowledgements",
	)
	if str(doc.get("ack_status") or "") not in ACK_STATUSES:
		frappe.throw("Medical-record archive acknowledgement status is invalid.")
	for fieldname in (
		"archive_decision",
		"assignment",
		"delivery",
		"endpoint",
		"source_system",
		"source_ack_at",
		"received_at",
	):
		if not doc.get(fieldname):
			frappe.throw(f"Medical-record archive acknowledgement requires {fieldname}.")
	for fieldname in (
		"ack_key",
		"archive_decision",
		"delivery",
		"request_hash",
		"decision_checksum",
		"envelope_hash",
		"source_record_id_hash",
	):
		if not _is_sha256(str(doc.get(fieldname) or "")):
			frappe.throw(f"Medical-record archive acknowledgement {fieldname} is invalid.")
	if not _SAFE_ACK_ID_RE.fullmatch(str(doc.get("source_ack_id") or "")):
		frappe.throw("Medical-record archive acknowledgement source_ack_id is invalid.")
	_safe_code(
		doc.get("message_code"),
		label="Medical-record archive acknowledgement message code",
	)
	expected_ack_key = _sha256(f"{doc.get('endpoint') or ''!s}|{doc.get('source_ack_id') or ''!s}")
	if not hmac.compare_digest(str(doc.get("ack_key") or ""), expected_ack_key):
		frappe.throw("Medical-record archive acknowledgement key does not match its source identity.")
	source_ack_at = _site_naive_datetime(
		doc.get("source_ack_at"),
		label="Medical-record archive acknowledgement source_ack_at",
	)
	received_at = _site_naive_datetime(
		doc.get("received_at"),
		label="Medical-record archive acknowledgement received_at",
	)
	decision = frappe.get_doc(ARCHIVE_DECISION_DOCTYPE, doc.archive_decision)
	assignment = frappe.get_doc(REVIEW_ASSIGNMENT_DOCTYPE, decision.assignment)
	delivery = frappe.get_doc(ARCHIVE_DELIVERY_DOCTYPE, doc.delivery)
	decided_at = _site_naive_datetime(
		decision.get("decided_at"),
		label="Medical-record archive decision decided_at",
	)
	if source_ack_at < decided_at or source_ack_at > received_at + timedelta(minutes=5):
		frappe.throw("Medical-record archive acknowledgement timestamps are inconsistent.")
	if (
		str(decision.get("assignment") or "") != str(assignment.name)
		or str(doc.get("assignment") or "") != str(assignment.name)
		or str(assignment.get("latest_archive_decision") or "") != str(decision.name)
		or str(assignment.get("pending_archive_delivery") or "") != str(delivery.name)
		or not hmac.compare_digest(
			str(assignment.get("pending_archive_envelope_hash") or ""),
			str(doc.get("envelope_hash") or ""),
		)
		or str(delivery.get("endpoint") or "") != str(doc.get("endpoint") or "")
		or str(delivery.get("source_system") or "") != str(doc.get("source_system") or "")
		or str(decision.get("source_system") or "") != str(doc.get("source_system") or "")
		or not hmac.compare_digest(
			str(decision.get("decision_checksum") or ""),
			str(doc.get("decision_checksum") or ""),
		)
		or not hmac.compare_digest(
			str(decision.get("source_record_id_hash") or ""),
			str(doc.get("source_record_id_hash") or ""),
		)
	):
		frappe.throw("Medical-record archive acknowledgement lineage is inconsistent.")
	_assert_delivery_contains_pending_decision(
		delivery,
		assignment,
		decision,
		envelope_hash=str(doc.get("envelope_hash") or ""),
	)


def validate_medical_record_completeness_watermark(doc, method: str | None = None) -> None:
	del method
	_require_creation_capability(
		doc,
		flag="ione_medical_record_completeness_watermark_creation",
		capability=_COMPLETENESS_WATERMARK_CREATION_CAPABILITY,
		label="Medical-record completeness watermarks",
	)
	for fieldname in _COMPLETENESS_WATERMARK_FIELDS:
		if fieldname in {"campus", "department", "ward"}:
			continue
		if doc.get(fieldname) in (None, ""):
			frappe.throw(f"Medical-record completeness watermark requires {fieldname}.")
	for fieldname in (
		"watermark_key",
		"watermark_checksum",
		"source_manifest_hash",
		"request_hash",
	):
		if not _is_sha256(str(doc.get(fieldname) or "")):
			frappe.throw(f"Medical-record completeness watermark {fieldname} is invalid.")
	if not _SAFE_REQUEST_ID_RE.fullmatch(str(doc.get("source_watermark_id") or "")):
		frappe.throw("Medical-record completeness source_watermark_id is invalid.")
	if type(doc.get("source_record_count")) is not int or int(doc.get("source_record_count")) < 0:
		frappe.throw("Medical-record completeness source_record_count must be a non-negative integer.")
	if (
		type(doc.get("source_sequence")) is not int
		or not 1 <= int(doc.get("source_sequence") or 0) <= 9_223_372_036_854_775_807
	):
		frappe.throw("Medical-record completeness source_sequence must be a positive 64-bit integer.")
	_validate_scope_shape(doc)
	complete_through = getdate(doc.get("complete_through"))
	period_start = getdate(doc.get("period_start"))
	period_end = getdate(doc.get("period_end"))
	if (
		complete_through > getdate(now_datetime())
		or period_start > period_end
		or period_end > complete_through
	):
		frappe.throw("Medical-record completeness period is invalid or outside its cutoff.")
	if (
		str(doc.get("eligible_record_status") or "") != "Final"
		or str(doc.get("population_date_field") or "") != "source_finalized_at"
	):
		frappe.throw("Medical-record completeness eligibility predicate is invalid.")
	expected_key = _sha256(f"{doc.get('endpoint') or ''!s}|{doc.get('source_watermark_id') or ''!s}")
	if not hmac.compare_digest(str(doc.get("watermark_key") or ""), expected_key):
		frappe.throw("Medical-record completeness key does not match its source identity.")
	source_asserted_at = _site_naive_datetime(
		doc.get("source_asserted_at"),
		label="Medical-record completeness source_asserted_at",
	)
	received_at = _site_naive_datetime(
		doc.get("received_at"),
		label="Medical-record completeness received_at",
	)
	if source_asserted_at > received_at + timedelta(minutes=5):
		frappe.throw("Medical-record completeness assertion time is in the future.")
	_assert_watermark_cutoff_time(source_asserted_at, complete_through)
	_assert_watermark_checksum(doc)


def validate_archive_delivery(doc, method: str | None = None) -> None:
	del method
	_require_creation_capability(
		doc,
		flag="ione_medical_record_archive_delivery_creation",
		capability=_ARCHIVE_DELIVERY_CREATION_CAPABILITY,
		label="Medical-record archive deliveries",
	)
	if str(doc.get("delivery_mode") or "") not in {
		"Signed Pull",
		"Operator Reconciliation",
	}:
		frappe.throw("Medical-record archive delivery mode is invalid.")
	expected_status = "Delivered" if str(doc.get("delivery_mode") or "") == "Signed Pull" else "Inspected"
	if str(doc.get("delivery_status") or "") != expected_status:
		frappe.throw("Medical-record archive delivery status does not match its authority.")
	for fieldname in ("delivery_key", "request_hash", "response_hash"):
		if not _is_sha256(str(doc.get(fieldname) or "")):
			frappe.throw(f"Medical-record archive delivery {fieldname} is invalid.")
	if not _SAFE_REQUEST_ID_RE.fullmatch(str(doc.get("request_id") or "")):
		frappe.throw("Medical-record archive delivery request_id is invalid.")
	for fieldname in ("endpoint", "source_system", "hospital", "delivered_at"):
		if not doc.get(fieldname):
			frappe.throw(f"Medical-record archive delivery requires {fieldname}.")
	if str(doc.get("delivery_mode") or "") == "Operator Reconciliation":
		if not doc.get("delivered_by"):
			frappe.throw("Operator reconciliation requires a named operator.")
	elif doc.get("delivered_by"):
		frappe.throw("Signed source delivery cannot claim a human operator.")
	_validate_hash_manifest(doc.get("decision_manifest_json"), label="decision_manifest_json")
	decisions = _decoded_hash_manifest(doc.get("decision_manifest_json"))
	if type(doc.get("decision_count")) is not int or int(doc.get("decision_count")) != len(decisions):
		frappe.throw("Medical-record archive delivery decision count does not match its manifest.")
	_validate_hash_manifest(doc.get("envelope_manifest_json"), label="envelope_manifest_json")
	envelopes = _decoded_hash_manifest(doc.get("envelope_manifest_json"))
	if type(doc.get("envelope_count")) is not int or int(doc.get("envelope_count")) != len(envelopes):
		frappe.throw("Medical-record archive delivery envelope count does not match its manifest.")
	response = _decoded_delivery_response(doc)
	_assert_delivery_receipt_exact(doc, response)


def _decoded_delivery_response(delivery) -> dict[str, Any]:
	response_json = str(delivery.get("response_json") or "")
	try:
		response = json.loads(response_json, object_pairs_hook=_unique_json_object)
	except ValueError as exc:
		raise frappe.ValidationError("Archive delivery response must be canonical JSON.") from exc
	if not isinstance(response, dict) or _canonical_json(response) != response_json:
		frappe.throw("Archive delivery response is not one canonical JSON object.")
	return response


def _assert_delivery_receipt_exact(delivery, response: Mapping[str, Any]) -> None:
	expected_fields = {
		"contract_version",
		"decision_count",
		"delivery_status",
		"endpoint",
		"envelope_count",
		"envelopes",
		"generated_at",
		"request_id",
		"scope",
		"source_system",
	}
	if not isinstance(response, Mapping) or set(response) != expected_fields:
		frappe.throw("Archive delivery response has an invalid exact field contract.")
	expected_delivery_key = _sha256(
		f"{delivery.get('endpoint') or ''!s}|{delivery.get('request_id') or ''!s}"
	)
	if not hmac.compare_digest(
		str(delivery.get("delivery_key") or ""),
		expected_delivery_key,
	):
		frappe.throw("Archive delivery key does not match its endpoint request identity.")
	response_json = _canonical_json(response)
	if response_json != str(delivery.get("response_json") or ""):
		frappe.throw("Archive delivery replay no longer matches its canonical stored response.")
	if not hmac.compare_digest(
		str(delivery.get("response_hash") or ""),
		_sha256(response_json),
	):
		frappe.throw("Archive delivery response hash does not match.")
	if (
		response.get("contract_version") != 2
		or str(response.get("endpoint") or "") != str(delivery.get("endpoint") or "")
		or str(response.get("source_system") or "") != str(delivery.get("source_system") or "")
		or str(response.get("request_id") or "") != str(delivery.get("request_id") or "")
		or str(response.get("delivery_status") or "") != str(delivery.get("delivery_status") or "")
	):
		frappe.throw("Archive delivery response authority does not match its receipt.")
	scope = response.get("scope")
	if not isinstance(scope, Mapping) or set(scope) != set(_SCOPE_FIELDS):
		frappe.throw("Archive delivery response scope is invalid.")
	if any(
		str(scope.get(fieldname) or "") != str(delivery.get(fieldname) or "") for fieldname in _SCOPE_FIELDS
	):
		frappe.throw("Archive delivery response scope does not match its receipt.")
	generated_at = _site_naive_datetime(
		response.get("generated_at"),
		label="Archive delivery generated_at",
	)
	delivered_at = _site_naive_datetime(
		delivery.get("delivered_at"),
		label="Archive delivery delivered_at",
	)
	if generated_at != delivered_at:
		frappe.throw("Archive delivery response time does not match its receipt.")
	envelopes = response.get("envelopes")
	if not isinstance(envelopes, list) or len(envelopes) > MAX_ARCHIVE_PULL_LIMIT:
		frappe.throw("Archive delivery envelopes must be a bounded list.")
	decision_names: list[str] = []
	envelope_hashes: list[str] = []
	for envelope in envelopes:
		if not isinstance(envelope, Mapping) or set(envelope) != {
			"constituent_decisions",
			"effective_decision",
			"envelope_hash",
			"reason_code",
			"record_snapshot_hash",
			"scope",
			"source_record_id_hash",
			"source_record_type",
		}:
			frappe.throw("Archive delivery contains an invalid record-level envelope.")
		envelope_hash = str(envelope.get("envelope_hash") or "")
		if not _is_sha256(envelope_hash):
			frappe.throw("Archive delivery envelope hash is invalid.")
		semantic = {key: value for key, value in envelope.items() if key != "envelope_hash"}
		if not hmac.compare_digest(envelope_hash, _sha256(_canonical_json(semantic))):
			frappe.throw("Archive delivery envelope hash does not match its content.")
		if str(envelope.get("effective_decision") or "") not in ARCHIVE_DECISIONS:
			frappe.throw("Archive delivery envelope effective decision is invalid.")
		_safe_code(envelope.get("reason_code"), label="Archive delivery envelope reason code")
		for fieldname in ("record_snapshot_hash", "source_record_id_hash"):
			if not _is_sha256(str(envelope.get(fieldname) or "")):
				frappe.throw(f"Archive delivery envelope {fieldname} is invalid.")
		envelope_scope = envelope.get("scope")
		if not isinstance(envelope_scope, Mapping) or set(envelope_scope) != set(_SCOPE_FIELDS):
			frappe.throw("Archive delivery envelope scope is invalid.")
		_validate_scope_shape(envelope_scope)
		if any(
			scope.get(fieldname)
			and str(envelope_scope.get(fieldname) or "") != str(scope.get(fieldname) or "")
			for fieldname in _SCOPE_FIELDS
		):
			frappe.throw("Archive delivery envelope is outside its receipt scope.")
		source_record_type = str(envelope.get("source_record_type") or "")
		if (
			not source_record_type
			or len(source_record_type) > 200
			or source_record_type != source_record_type.strip()
		):
			frappe.throw("Archive delivery envelope source record type is invalid.")
		constituents = envelope.get("constituent_decisions")
		if (
			not isinstance(constituents, list)
			or not constituents
			or len(constituents) > MAX_POPULATION_RECORDS
		):
			frappe.throw("Archive delivery envelope requires constituent decisions.")
		for item in constituents:
			if not isinstance(item, Mapping) or set(item) != {
				"ack_required",
				"ack_status",
				"archive_decision",
				"decided_at",
				"decision",
				"decision_checksum",
				"decision_type",
				"reason_code",
			}:
				frappe.throw("Archive delivery constituent decision is invalid.")
			ack_required = str(item.get("ack_required") or "")
			ack_status = str(item.get("ack_status") or "")
			if (
				ack_required not in {"0", "1"}
				or ack_status not in {"Pending", *ACK_STATUSES}
				or (ack_required == "0") != (ack_status == "Applied")
				or str(item.get("decision") or "") not in ARCHIVE_DECISIONS
				or not _is_sha256(str(item.get("archive_decision") or ""))
				or not _is_sha256(str(item.get("decision_checksum") or ""))
			):
				frappe.throw("Archive delivery constituent decision lineage is invalid.")
			_site_naive_datetime(
				item.get("decided_at"),
				label="Archive delivery constituent decided_at",
			)
			_safe_code(item.get("reason_code"), label="Archive delivery constituent reason code")
			decision_names.append(str(item["archive_decision"]))
		effective = str(envelope.get("effective_decision") or "")
		expected_reason = "RECORD_GATES_ALLOW" if effective == "Allow" else "RECORD_GATES_HOLD"
		if str(envelope.get("reason_code") or "") != expected_reason or (
			effective == "Allow" and any(str(item.get("decision") or "") != "Allow" for item in constituents)
		):
			frappe.throw("Archive delivery envelope effective disposition is inconsistent.")
		envelope_hashes.append(envelope_hash)
	if len(decision_names) != len(set(decision_names)) or len(envelope_hashes) != len(set(envelope_hashes)):
		frappe.throw("Archive delivery manifests cannot contain duplicate identities.")
	if (
		type(response.get("decision_count")) is not int
		or int(response["decision_count"]) != len(decision_names)
		or type(response.get("envelope_count")) is not int
		or int(response["envelope_count"]) != len(envelope_hashes)
		or int(delivery.get("decision_count") or 0) != len(decision_names)
		or int(delivery.get("envelope_count") or 0) != len(envelope_hashes)
		or _canonical_json(sorted(decision_names)) != str(delivery.get("decision_manifest_json") or "")
		or _canonical_json(sorted(envelope_hashes)) != str(delivery.get("envelope_manifest_json") or "")
	):
		frappe.throw("Archive delivery response manifests do not match its receipt.")


def _assert_delivery_contains_pending_decision(
	delivery,
	assignment,
	decision,
	*,
	envelope_hash: str,
	require_pending: bool = True,
) -> None:
	response = _decoded_delivery_response(delivery)
	_assert_delivery_receipt_exact(delivery, response)
	for envelope in response["envelopes"]:
		if str(envelope.get("envelope_hash") or "") != envelope_hash:
			continue
		if not hmac.compare_digest(
			str(envelope.get("source_record_id_hash") or ""),
			str(decision.get("source_record_id_hash") or ""),
		):
			frappe.throw("Pending archive envelope source identity changed.")
		for item in envelope["constituent_decisions"]:
			if str(item.get("archive_decision") or "") != str(decision.name):
				continue
			if (
				str(item.get("ack_required") or "") != "1"
				or not hmac.compare_digest(
					str(item.get("decision_checksum") or ""),
					str(decision.get("decision_checksum") or ""),
				)
				or (
					require_pending
					and str(assignment.get("pending_archive_delivery") or "") != str(delivery.name)
				)
			):
				frappe.throw("Pending archive decision does not match its delivery receipt.")
			return
	frappe.throw("Archive acknowledgement decision is absent from its pending envelope.")


def validate_medical_record_review_anchor(doc, method: str | None = None) -> None:
	"""Require immutable source identity before a Final record may enter sampling."""
	del method
	if str(doc.get("record_status") or "") == "Final":
		for fieldname in (
			"source_system",
			"source_record_type",
			"source_record_id_hash",
			"source_finalized_at",
			"record_snapshot_hash",
			"hospital",
		):
			if doc.get(fieldname) in (None, ""):
				frappe.throw(f"A Final medical-record QC projection requires {fieldname}.")
		for fieldname in ("source_record_id_hash", "record_snapshot_hash"):
			if not _is_sha256(str(doc.get(fieldname) or "")):
				frappe.throw(f"Final medical-record QC {fieldname} is invalid.")
	previous = doc.get_doc_before_save()
	if previous is None or not doc.get("name"):
		return
	# Serialize projection changes against batch creation, which locks this same
	# source row before inserting an assignment.
	persisted = frappe.get_doc(MEDICAL_RECORD_QC_DOCTYPE, doc.name, for_update=True)
	changed = _changed_fields(doc, persisted, _MEDICAL_RECORD_REVIEW_ANCHOR_FIELDS)
	if not frappe.db.exists(
		REVIEW_ASSIGNMENT_DOCTYPE,
		{"medical_record_qc": doc.name},
	):
		if (
			changed == {"record_status"}
			and str(persisted.get("record_status") or "") == "Final"
			and str(doc.get("record_status") or "") == "Archived"
		):
			frappe.throw("A medical record without a durable disposition cannot be archived.")
		return
	if changed and not _is_governed_archive_projection_transition(doc, persisted, changed):
		frappe.throw("A sampled medical-record QC source anchor is immutable.")


def prevent_medical_record_review_deletion(doc, method: str | None = None) -> None:
	del doc, method
	frappe.throw(
		"Medical-record sampling, review, archive decisions, and receipts are retained audit records."
	)


def prevent_sampled_medical_record_deletion(doc, method: str | None = None) -> None:
	"""Retain a source QC projection after it has entered a frozen review batch."""
	del method
	if doc.get("name") and frappe.db.exists(
		REVIEW_ASSIGNMENT_DOCTYPE,
		{"medical_record_qc": doc.name},
	):
		frappe.throw("A sampled medical-record QC source projection is a retained audit record.")


def _is_governed_archive_projection_transition(
	doc,
	persisted,
	changed: set[str],
) -> bool:
	"""Allow only Final -> Archived after every applicable gate is durably applied."""
	if (
		changed != {"record_status"}
		or str(persisted.get("record_status") or "") != "Final"
		or str(doc.get("record_status") or "") != "Archived"
	):
		return False
	return _all_archive_gates_applied(persisted)


def assert_medical_record_archive_ready(doc) -> None:
	"""Fail closed unless every durable disposition is Allow with an Applied ACK."""
	if str(doc.get("record_status") or "") != "Final" or not _all_archive_gates_applied(doc):
		frappe.throw("Medical-record projection is not ready for archive application.")


def _all_archive_gates_applied(doc) -> bool:
	assignments = frappe.get_all(
		REVIEW_ASSIGNMENT_DOCTYPE,
		filters={"medical_record_qc": doc.name},
		fields=[
			"name",
			"status",
			"latest_archive_decision",
			"archive_acknowledgement",
			"archive_ack_status",
			"pending_archive_delivery",
			"pending_archive_envelope_hash",
			"record_snapshot_hash",
			"source_system",
			"source_record_id_hash",
		],
		order_by="name asc",
		limit_page_length=MAX_POPULATION_RECORDS + 1,
	)
	if not assignments or len(assignments) > MAX_POPULATION_RECORDS:
		return False
	for assignment in assignments:
		if (
			str(assignment.get("status") or "") != "Closed"
			or str(assignment.get("archive_ack_status") or "") != "Applied"
			or assignment.get("pending_archive_delivery")
			or assignment.get("pending_archive_envelope_hash")
			or not assignment.get("latest_archive_decision")
			or not assignment.get("archive_acknowledgement")
		):
			return False
		decision = frappe.db.get_value(
			ARCHIVE_DECISION_DOCTYPE,
			assignment["latest_archive_decision"],
			[
				"name",
				"assignment",
				"decision",
				"decision_checksum",
				"record_snapshot_hash",
				"source_system",
				"source_record_id_hash",
			],
			as_dict=True,
		)
		ack = frappe.db.get_value(
			ARCHIVE_ACK_DOCTYPE,
			assignment["archive_acknowledgement"],
			[
				"archive_decision",
				"assignment",
				"ack_status",
				"delivery",
				"endpoint",
				"source_system",
				"decision_checksum",
				"envelope_hash",
				"source_record_id_hash",
			],
			as_dict=True,
		)
		if (
			not decision
			or not ack
			or any(
				not _is_sha256(str(value or ""))
				for value in (
					assignment.get("record_snapshot_hash"),
					assignment.get("source_record_id_hash"),
					decision.get("decision_checksum"),
					decision.get("record_snapshot_hash"),
					decision.get("source_record_id_hash"),
					ack.get("decision_checksum"),
					ack.get("envelope_hash"),
					ack.get("source_record_id_hash"),
				)
			)
			or str(decision.get("assignment") or "") != str(assignment["name"])
			or str(decision.get("decision") or "") != "Allow"
			or str(decision.get("source_system") or "") != str(assignment.get("source_system") or "")
			or str(ack.get("archive_decision") or "") != str(decision["name"])
			or str(ack.get("assignment") or "") != str(assignment["name"])
			or str(ack.get("ack_status") or "") != "Applied"
			or not ack.get("delivery")
			or not hmac.compare_digest(
				str(decision.get("decision_checksum") or ""),
				str(ack.get("decision_checksum") or ""),
			)
			or not hmac.compare_digest(
				str(assignment.get("record_snapshot_hash") or ""),
				str(decision.get("record_snapshot_hash") or ""),
			)
			or not hmac.compare_digest(
				str(doc.get("record_snapshot_hash") or ""),
				str(assignment.get("record_snapshot_hash") or ""),
			)
			or not hmac.compare_digest(
				str(assignment.get("source_record_id_hash") or ""),
				str(decision.get("source_record_id_hash") or ""),
			)
			or not hmac.compare_digest(
				str(decision.get("source_record_id_hash") or ""),
				str(ack.get("source_record_id_hash") or ""),
			)
		):
			return False
		try:
			delivery = frappe.get_doc(ARCHIVE_DELIVERY_DOCTYPE, ack.get("delivery"))
			if (
				str(ack.get("endpoint") or "") != str(delivery.get("endpoint") or "")
				or str(ack.get("source_system") or "") != str(delivery.get("source_system") or "")
				or str(ack.get("source_system") or "") != str(decision.get("source_system") or "")
			):
				return False
			_assert_delivery_contains_pending_decision(
				delivery,
				assignment,
				decision,
				envelope_hash=str(ack.get("envelope_hash") or ""),
				require_pending=False,
			)
		except (
			frappe.DoesNotExistError,
			frappe.PermissionError,
			frappe.ValidationError,
		):
			return False
	return True


def _claim_assignment(
	assignment_name: str,
	*,
	user: str,
	required_status: str,
	actor_field: str,
	claimed_at_field: str,
	other_actor_field: str | None,
) -> dict[str, str]:
	with frappe.db.advisory_lock(f"ione-qms:record-review-claim:{assignment_name}", timeout=10):
		assignment = frappe.get_doc(REVIEW_ASSIGNMENT_DOCTYPE, assignment_name, for_update=True)
		_require_assignment_access(assignment, user)
		if str(assignment.get("status") or "") != required_status:
			frappe.throw(f"Medical-record assignment is not in {required_status}.")
		if other_actor_field and str(assignment.get(other_actor_field) or "") == user:
			frappe.throw("Coder and expert reviewers must be independent named users.")
		current = str(assignment.get(actor_field) or "")
		if current and current != user:
			frappe.throw("Medical-record review assignment is already claimed.")
		if not current:
			_update_assignment(
				assignment,
				{actor_field: user, claimed_at_field: now_datetime()},
			)
			frappe.db.commit()
	return {
		"assignment": assignment.name,
		"status": str(assignment.status),
		"reviewer": user,
	}


def _submit_review(
	assignment_name: str,
	*,
	stage: str,
	user: str,
	outcome: str,
	comment: str,
	finding: str | None,
	evidence_references: Sequence[str] | None,
) -> dict[str, str]:
	normalized_outcome = str(outcome or "").strip()
	if normalized_outcome not in REVIEW_OUTCOMES:
		frappe.throw("Medical-record review outcome is invalid.")
	rationale = _bounded_comment(comment, minimum=10)
	references = _evidence_references(evidence_references)
	finding_name = str(finding or "").strip() or None
	if normalized_outcome != "Pass" and not finding_name and not references:
		frappe.throw("A non-pass medical-record decision requires a finding or governed evidence.")
	record_name = frappe.db.get_value(
		REVIEW_ASSIGNMENT_DOCTYPE,
		assignment_name,
		"medical_record_qc",
	)
	if not record_name:
		frappe.throw("Medical-record review assignment has no source projection.")
	with (
		_medical_record_archive_lock(str(record_name)),
		frappe.db.advisory_lock(f"ione-qms:record-review-submit:{assignment_name}", timeout=10),
	):
		assignment = frappe.get_doc(REVIEW_ASSIGNMENT_DOCTYPE, assignment_name, for_update=True)
		if str(assignment.get("medical_record_qc") or "") != str(record_name):
			frappe.throw("Medical-record review assignment changed during record locking.")
		_require_assignment_access(assignment, user)
		required_status = "Coder Review" if stage == "Coder" else "Expert Review"
		actor_field = "assigned_coder" if stage == "Coder" else "assigned_expert"
		other_actor_field = "assigned_expert" if stage == "Coder" else "assigned_coder"
		if str(assignment.get(actor_field) or "") != user:
			frappe.throw(f"The {stage.lower()} assignment must be claimed by the submitting user.")
		if stage == "Expert" and str(assignment.get(other_actor_field) or "") == user:
			frappe.throw("Coder and expert reviewers must be independent named users.")

		decision_key = _review_decision_key(
			assignment,
			stage=stage,
			user=user,
			outcome=normalized_outcome,
			comment=rationale,
			finding=finding_name,
			evidence_references=references,
		)
		existing_name = frappe.db.get_value(
			REVIEW_DECISION_DOCTYPE,
			{"decision_key": decision_key},
			"name",
			for_update=True,
		)
		if existing_name:
			existing = frappe.get_doc(REVIEW_DECISION_DOCTYPE, existing_name)
			return {
				"assignment": assignment.name,
				"decision": existing.name,
				"stage": str(existing.stage),
				"outcome": str(existing.outcome),
				"status": str(assignment.status),
				"archive_decision": str(assignment.get("latest_archive_decision") or ""),
			}
		if assignment.get("pending_archive_delivery"):
			frappe.throw(
				"A delivered archive disposition must receive Rejected or Failed before review state changes."
			)
		if str(assignment.get("archive_ack_status") or "") == "Applied":
			frappe.throw("An applied archive disposition is terminal and cannot be superseded by review.")
		if str(assignment.get("status") or "") != required_status:
			frappe.throw(f"Medical-record assignment is not in {required_status}.")
		if finding_name:
			_validate_finding_reference(finding_name, assignment, user)
		_validate_evidence_links(references, assignment, user)

		decision = _new_review_decision(
			assignment,
			stage=stage,
			user=user,
			outcome=normalized_outcome,
			comment=rationale,
			finding=finding_name,
			evidence_references=references,
			decision_key=decision_key,
		)
		policy = frappe.get_doc(SAMPLING_POLICY_DOCTYPE, assignment.policy)
		_assert_frozen_policy(assignment, policy)
		if stage == "Coder":
			requires_expert = bool(int(assignment.get("expert_required") or 0)) or (
				normalized_outcome != "Pass"
				and str(policy.get("expert_non_pass_policy") or "") == "Always Expert Review"
			)
			values = {
				"coder_decision": decision.name,
				"coder_outcome": normalized_outcome,
				"coder_reviewed_at": decision.reviewed_at,
				"status": "Expert Review" if requires_expert else "Closed",
			}
		else:
			values = {
				"expert_decision": decision.name,
				"expert_outcome": normalized_outcome,
				"expert_reviewed_at": decision.reviewed_at,
				"status": "Closed",
			}
		_update_assignment(assignment, values)
		archive = None
		if str(assignment.status) == "Closed":
			archive = _issue_policy_archive_decision(
				assignment,
				policy,
				review_decision=decision,
				stage=stage,
				outcome=normalized_outcome,
				triggered_by=user,
			)
			_update_batch_completion(assignment.batch)
		frappe.db.commit()
	return {
		"assignment": assignment.name,
		"decision": decision.name,
		"stage": stage,
		"outcome": normalized_outcome,
		"status": str(assignment.status),
		"archive_decision": archive.name if archive else "",
	}


def _new_review_decision(
	assignment,
	*,
	stage: str,
	user: str,
	outcome: str,
	comment: str,
	finding: str | None,
	evidence_references: Sequence[str],
	decision_key: str,
):
	reviewed_at = now_datetime()
	semantic = {
		"decision_key": decision_key,
		"assignment": assignment.name,
		"batch": assignment.batch,
		"policy": assignment.policy,
		"medical_record_qc": assignment.medical_record_qc,
		"stage": stage,
		"outcome": outcome,
		"comment": comment,
		"finding": finding,
		"evidence_references_json": _canonical_json(list(evidence_references)),
		"reviewed_by": user,
		"reviewed_at": reviewed_at,
		"record_snapshot_hash": assignment.record_snapshot_hash,
	}
	doc = frappe.get_doc(
		{
			"doctype": REVIEW_DECISION_DOCTYPE,
			**semantic,
			**{fieldname: assignment.get(fieldname) for fieldname in _SCOPE_FIELDS},
			"decision_checksum": _sha256(_canonical_json(semantic)),
		}
	)
	with _creation_capability(
		"ione_medical_record_review_decision_creation",
		_REVIEW_DECISION_CREATION_CAPABILITY,
	):
		doc.insert(ignore_permissions=True)
	return doc


def _issue_policy_archive_decision(
	assignment,
	policy,
	*,
	review_decision,
	stage: str,
	outcome: str,
	triggered_by: str,
):
	if not int(policy.get("archive_gate_enabled") or 0):
		frappe.throw("The frozen review policy does not enable an archive gate.")
	if assignment.get("pending_archive_delivery"):
		frappe.throw("A pending delivered archive disposition cannot be superseded.")
	if str(assignment.get("archive_ack_status") or "") == "Applied":
		frappe.throw("An applied archive disposition is terminal and cannot be superseded.")
	allowed_field = "coder_allow_outcomes_json" if stage == "Coder" else "expert_allow_outcomes_json"
	allowed = _outcome_set(policy.get(allowed_field), label=allowed_field)
	gate_passed = str(assignment.get("deterministic_gate_status") or "") == "Passed"
	if not gate_passed:
		decision = "Hold"
		reason_code = _safe_code(
			assignment.get("deterministic_gate_reason_code") or "MANDATORY_RULE_EXECUTION_ERROR",
			label="Medical-record deterministic gate reason code",
		)
		rationale = (
			f"The frozen mandatory deterministic gate remains Held ({reason_code}); "
			f"the {stage} outcome {outcome} cannot convert it to Allow."
		)
	else:
		decision = "Allow" if outcome in allowed else "Hold"
		reason_code = (
			"APPROVED_POLICY_ALLOW_OUTCOME" if decision == "Allow" else "APPROVED_POLICY_HOLD_OUTCOME"
		)
		rationale = (
			f"Frozen policy outcome mapping for {stage} outcome {outcome}; "
			"no clinical inference was performed."
		)
	supersedes = str(assignment.get("latest_archive_decision") or "") or None
	if not supersedes:
		frappe.throw("Review outcome cannot replace a missing deterministic disposition.")
	archive_key = _sha256(
		_canonical_json(
			{
				"assignment": assignment.name,
				"review_decision": review_decision.name,
				"policy_checksum": policy.approval_checksum,
				"decision": decision,
				"reason_code": reason_code,
				"supersedes": supersedes,
			}
		)
	)
	existing = frappe.db.get_value(
		ARCHIVE_DECISION_DOCTYPE,
		{"archive_key": archive_key},
		"name",
		for_update=True,
	)
	if existing:
		doc = frappe.get_doc(ARCHIVE_DECISION_DOCTYPE, existing)
	else:
		doc = _new_archive_decision(
			assignment,
			review_decision=review_decision.name,
			decision=decision,
			decision_type="Review Outcome",
			reason_code=reason_code,
			rationale=rationale,
			triggered_by=triggered_by,
			supersedes=supersedes,
			archive_key=archive_key,
		)
	_update_assignment(
		assignment,
		{
			"latest_archive_decision": doc.name,
			"archive_acknowledgement": None,
			"archive_ack_status": None,
			"pending_archive_delivery": None,
			"pending_archive_envelope_hash": None,
		},
	)
	return doc


def _new_archive_decision(
	assignment,
	*,
	review_decision: str | None,
	decision: str,
	decision_type: str,
	reason_code: str,
	rationale: str,
	triggered_by: str,
	supersedes: str | None,
	archive_key: str,
):
	policy = frappe.get_doc(SAMPLING_POLICY_DOCTYPE, assignment.policy)
	_assert_frozen_policy(assignment, policy)
	semantic = {
		"archive_key": archive_key,
		"assignment": assignment.name,
		"batch": assignment.batch,
		"policy": policy.name,
		"policy_checksum": policy.approval_checksum,
		"medical_record_qc": assignment.medical_record_qc,
		"review_decision": review_decision,
		"decision": decision,
		"decision_type": decision_type,
		"reason_code": reason_code,
		"rationale": rationale,
		"triggered_by": triggered_by,
		"decided_at": now_datetime(),
		"supersedes": supersedes,
		"record_snapshot_hash": assignment.record_snapshot_hash,
		"source_system": assignment.source_system,
		"source_record_type": assignment.source_record_type,
		"source_record_id_hash": assignment.source_record_id_hash,
	}
	doc = frappe.get_doc(
		{
			"doctype": ARCHIVE_DECISION_DOCTYPE,
			**semantic,
			**{fieldname: assignment.get(fieldname) for fieldname in _SCOPE_FIELDS},
			"decision_checksum": _sha256(_canonical_json(semantic)),
		}
	)
	with _creation_capability(
		"ione_medical_record_archive_decision_creation",
		_ARCHIVE_DECISION_CREATION_CAPABILITY,
	):
		doc.insert(ignore_permissions=True)
	return doc


def _create_assignment(
	batch,
	policy,
	item: Mapping[str, Any],
	*,
	coder_required: bool,
	expert_required: bool,
) -> None:
	row = item["row"]
	record_name = str(row.get("name") or "")
	with _medical_record_archive_lock(record_name) as current:
		current.check_permission("read")
		if frappe.db.exists(
			REVIEW_ASSIGNMENT_DOCTYPE,
			{
				"medical_record_qc": record_name,
				"pending_archive_delivery": ["is", "set"],
			},
		):
			frappe.throw("A medical record with a pending delivered envelope cannot acquire a new gate.")
		locked_item = _population_record(current)
		if not hmac.compare_digest(
			str(locked_item["token"]),
			str(item["token"]),
		):
			frappe.throw("Selected medical-record source anchor changed during batch creation.")
		_create_assignment_under_release_lock(
			batch,
			policy,
			item,
			locked_item,
			coder_required=coder_required,
			expert_required=expert_required,
		)


def _create_assignment_under_release_lock(
	batch,
	policy,
	item: Mapping[str, Any],
	locked_item: Mapping[str, Any],
	*,
	coder_required: bool,
	expert_required: bool,
) -> None:
	row = locked_item["row"]
	from ione_qms.services.projections import assert_medical_record_release_lock

	assert_medical_record_release_lock(str(row.get("name") or ""))
	gate = _mandatory_rule_gate(policy, row)
	gate_decision, gate_reason = _initial_archive_disposition(
		policy,
		gate,
		coder_required=coder_required,
	)
	assignment_key = _sha256(f"{batch.batch_key}|{item['token']}")
	doc = frappe.get_doc(
		{
			"doctype": REVIEW_ASSIGNMENT_DOCTYPE,
			"assignment_key": assignment_key,
			"batch": batch.name,
			"policy": policy.name,
			"medical_record_qc": row.get("name"),
			"clinical_event": row.get("event"),
			"population_token": item["token"],
			"record_snapshot_hash": row.get("record_snapshot_hash"),
			"sampling_score_hash": item["score"],
			"coder_required": int(coder_required),
			"expert_required": int(expert_required),
			"mandatory_rule_manifest_hash": policy.mandatory_rule_manifest_hash,
			"mandatory_execution_manifest_json": gate["execution_manifest_json"],
			"mandatory_execution_manifest_hash": gate["execution_manifest_hash"],
			"deterministic_gate_status": gate["status"],
			"deterministic_gate_reason_code": gate["reason_code"],
			**{
				fieldname: row.get(fieldname)
				for fieldname in (
					"hospital",
					"campus",
					"department",
					"ward",
					"patient",
					"encounter",
					"responsible_staff",
					"source_system",
					"source_record_type",
					"source_record_id_hash",
				)
			},
			"status": "Coder Review" if coder_required else "Closed",
		}
	)
	with _creation_capability(
		"ione_medical_record_assignment_creation",
		_ASSIGNMENT_CREATION_CAPABILITY,
	):
		doc.insert(ignore_permissions=True)
	archive_key = _sha256(
		_canonical_json(
			{
				"assignment": doc.name,
				"policy_checksum": policy.approval_checksum,
				"mandatory_execution_manifest_hash": gate["execution_manifest_hash"],
				"decision": gate_decision,
				"reason_code": gate_reason,
			}
		)
	)
	archive = _new_archive_decision(
		doc,
		review_decision=None,
		decision=gate_decision,
		decision_type="Deterministic Gate",
		reason_code=gate_reason,
		rationale=_initial_disposition_rationale(gate_reason),
		triggered_by=batch.created_by,
		supersedes=None,
		archive_key=archive_key,
	)
	_update_assignment(
		doc,
		{
			"latest_archive_decision": archive.name,
			"archive_acknowledgement": None,
			"archive_ack_status": None,
			"pending_archive_delivery": None,
			"pending_archive_envelope_hash": None,
		},
	)


def _normalize_mandatory_rule_manifest(doc) -> None:
	manifest = _mandatory_rule_manifest(doc.get("mandatory_rule_manifest_json"))
	manifest_json = _canonical_json(list(manifest))
	if manifest_json != str(doc.get("mandatory_rule_manifest_json") or ""):
		frappe.throw("Mandatory-rule manifest must use exact canonical JSON serialization.")
	expected_hash = _sha256(manifest_json)
	status = str(doc.get("status") or "Draft")
	stored = str(doc.get("mandatory_rule_manifest_hash") or "")
	if status == "Draft":
		doc.mandatory_rule_manifest_hash = expected_hash
	elif not _is_sha256(stored) or not hmac.compare_digest(stored, expected_hash):
		frappe.throw("Approved mandatory-rule manifest hash is missing or stale.")


def _mandatory_rule_manifest(value: Any) -> tuple[dict[str, str], ...]:
	try:
		decoded = json.loads(str(value or ""), object_pairs_hook=_unique_json_object)
	except ValueError as exc:
		raise frappe.ValidationError("Mandatory-rule manifest must be canonical JSON.") from exc
	if not isinstance(decoded, list) or not decoded or len(decoded) > 100:
		frappe.throw("Mandatory-rule manifest must contain 1-100 exact rule versions.")
	manifest: list[dict[str, str]] = []
	for item in decoded:
		if (
			not isinstance(item, dict)
			or set(item) != {"rule", "rule_checksum", "rule_version"}
			or any(not isinstance(item.get(fieldname), str) for fieldname in item)
		):
			frappe.throw(
				"Each mandatory-rule entry requires exact string fields "
				"rule, rule_version, and rule_checksum."
			)
		rule = str(item["rule"]).strip()
		version = str(item["rule_version"]).strip()
		checksum = str(item["rule_checksum"]).strip()
		if (
			not rule
			or len(rule) > 140
			or not version
			or len(version) > 140
			or not _is_sha256(checksum)
			or rule != item["rule"]
			or version != item["rule_version"]
			or checksum != item["rule_checksum"]
		):
			frappe.throw("Mandatory-rule manifest contains an invalid exact rule identity.")
		manifest.append(
			{
				"rule": rule,
				"rule_checksum": checksum,
				"rule_version": version,
			}
		)
	ordered = sorted(manifest, key=lambda item: (item["rule"], item["rule_version"]))
	identities = {(item["rule"], item["rule_version"]) for item in ordered}
	if len(identities) != len(ordered) or ordered != manifest:
		frappe.throw("Mandatory-rule manifest must be sorted and contain unique exact versions.")
	if _canonical_json(ordered) != str(value or ""):
		frappe.throw("Mandatory-rule manifest is not canonical.")
	return tuple(ordered)


def _mandatory_rule_definition_reason(
	manifest: Sequence[Mapping[str, str]],
	*,
	activation: bool,
) -> str | None:
	allowed_statuses = {"Published"} if activation else {"Published", "Retired"}
	for item in manifest:
		row = frappe.db.get_value(
			"IONE QC Rule Version",
			item["rule_version"],
			["rule", "rule_type_snapshot", "status", "shadow_mode", "checksum"],
			as_dict=True,
		)
		if (
			not row
			or str(row.get("rule") or "") != item["rule"]
			or str(row.get("rule_type_snapshot") or "") != "Deterministic"
			or str(row.get("status") or "") not in allowed_statuses
			or int(row.get("shadow_mode") or 0)
			or not hmac.compare_digest(
				str(row.get("checksum") or ""),
				item["rule_checksum"],
			)
		):
			return "MANDATORY_RULE_MANIFEST_DRIFT"
	return None


def _mandatory_rule_gate(policy, row: Mapping[str, Any]) -> dict[str, str]:
	manifest = _mandatory_rule_manifest(policy.get("mandatory_rule_manifest_json"))
	manifest_hash = _sha256(_canonical_json(list(manifest)))
	if not hmac.compare_digest(
		manifest_hash,
		str(policy.get("mandatory_rule_manifest_hash") or ""),
	):
		reason = "MANDATORY_RULE_MANIFEST_DRIFT"
		return _rule_gate_result([], reason)
	definition_reason = _mandatory_rule_definition_reason(manifest, activation=False)
	if definition_reason:
		return _rule_gate_result([], definition_reason)
	event = str(row.get("event") or "").strip()
	if not event:
		return _rule_gate_result([], "MANDATORY_RULE_EVENT_MISSING")
	snapshot_hash = str(row.get("record_snapshot_hash") or "")
	execution_manifest: list[dict[str, str]] = []
	failure_reason: str | None = None
	reason_priority = {
		"MANDATORY_RULE_EXECUTION_MISSING": 1,
		"MANDATORY_RULE_SNAPSHOT_MISMATCH": 2,
		"MANDATORY_RULE_EXCLUDED": 3,
		"MANDATORY_RULE_FAILED": 4,
		"MANDATORY_RULE_INSUFFICIENT_DATA": 5,
		"MANDATORY_RULE_EXECUTION_ERROR": 6,
		"MANDATORY_RULE_EXECUTION_CONFLICT": 7,
	}
	for item in manifest:
		execution_filters = {
			"event": event,
			"rule": item["rule"],
			"rule_version": item["rule_version"],
		}
		executions = frappe.get_all(
			"IONE QC Execution",
			filters={
				**execution_filters,
				"context_hash": snapshot_hash,
			},
			fields=["name", "context_hash", "result", "completed_at"],
			order_by="name asc",
			limit_page_length=MAX_MANDATORY_EXECUTION_RECEIPTS_PER_RULE + 1,
		)
		exact_receipts = [
			execution
			for execution in executions
			if _is_sha256(str(execution.get("context_hash") or ""))
			and hmac.compare_digest(
				str(execution.get("context_hash") or ""),
				snapshot_hash,
			)
		]
		if not exact_receipts:
			execution_manifest.append(
				{
					"completed_at": "",
					"context_hash": "",
					"execution": "",
					"result": "",
					"rule": item["rule"],
					"rule_checksum": item["rule_checksum"],
					"rule_version": item["rule_version"],
				}
			)
			reason = (
				"MANDATORY_RULE_SNAPSHOT_MISMATCH"
				if frappe.db.exists("IONE QC Execution", execution_filters)
				else "MANDATORY_RULE_EXECUTION_MISSING"
			)
		else:
			for receipt in exact_receipts:
				execution_manifest.append(
					{
						"completed_at": str(receipt.get("completed_at") or ""),
						"context_hash": str(receipt.get("context_hash") or ""),
						"execution": str(receipt.get("name") or ""),
						"result": str(receipt.get("result") or ""),
						"rule": item["rule"],
						"rule_checksum": item["rule_checksum"],
						"rule_version": item["rule_version"],
					}
				)
			results = {str(receipt.get("result") or "") for receipt in exact_receipts}
			if (
				len(executions) > MAX_MANDATORY_EXECUTION_RECEIPTS_PER_RULE
				or len(exact_receipts) != 1
				or len(results) != 1
			):
				reason = "MANDATORY_RULE_EXECUTION_CONFLICT"
			else:
				result = next(iter(results))
				reason = {
					"Passed": None,
					"Failed": "MANDATORY_RULE_FAILED",
					"Excluded": "MANDATORY_RULE_EXCLUDED",
					"Insufficient Data": "MANDATORY_RULE_INSUFFICIENT_DATA",
					"Error": "MANDATORY_RULE_EXECUTION_ERROR",
				}.get(result, "MANDATORY_RULE_EXECUTION_ERROR")
				if any(not receipt.get("completed_at") for receipt in exact_receipts):
					reason = "MANDATORY_RULE_EXECUTION_ERROR"
		if reason and (failure_reason is None or reason_priority[reason] > reason_priority[failure_reason]):
			failure_reason = reason
	execution_manifest.sort(
		key=lambda receipt: (
			receipt["rule"],
			receipt["rule_version"],
			receipt["execution"],
		)
	)
	return _rule_gate_result(
		execution_manifest,
		failure_reason or "MANDATORY_RULES_PASSED",
	)


def _rule_gate_result(
	execution_manifest: Sequence[Mapping[str, str]],
	reason_code: str,
) -> dict[str, str]:
	manifest_json = _canonical_json(list(execution_manifest))
	return {
		"execution_manifest_json": manifest_json,
		"execution_manifest_hash": _sha256(manifest_json),
		"reason_code": reason_code,
		"status": "Passed" if reason_code == "MANDATORY_RULES_PASSED" else "Held",
	}


def _initial_archive_disposition(
	policy,
	gate: Mapping[str, str],
	*,
	coder_required: bool,
) -> tuple[str, str]:
	if str(gate.get("status") or "") != "Passed":
		return "Hold", str(gate.get("reason_code") or "MANDATORY_RULE_EXECUTION_ERROR")
	if coder_required:
		return "Hold", "CODER_REVIEW_REQUIRED"
	if int(policy.get("auto_allow_deterministic_pass") or 0):
		return "Allow", "MANDATORY_RULES_PASSED"
	return "Hold", "AUTO_ALLOW_DISABLED"


def _initial_disposition_rationale(reason_code: str) -> str:
	rationales = {
		"MANDATORY_RULES_PASSED": (
			"Every exact approved mandatory deterministic rule version passed for the frozen record "
			"snapshot, and the policy explicitly permits automatic Allow."
		),
		"CODER_REVIEW_REQUIRED": (
			"The deterministic gate passed, but this record belongs to the frozen coder cohort."
		),
		"AUTO_ALLOW_DISABLED": (
			"The deterministic gate passed, but the approved policy does not permit automatic Allow."
		),
		"MANDATORY_RULE_MANIFEST_DRIFT": (
			"The approved mandatory-rule manifest no longer matches its immutable definitions."
		),
		"MANDATORY_RULE_EVENT_MISSING": (
			"The record has no exact clinical event for mandatory deterministic-rule evidence."
		),
		"MANDATORY_RULE_EXECUTION_MISSING": (
			"At least one exact approved mandatory deterministic rule has no execution receipt."
		),
		"MANDATORY_RULE_SNAPSHOT_MISMATCH": (
			"Mandatory deterministic-rule receipts do not bind to the frozen record snapshot."
		),
		"MANDATORY_RULE_EXCLUDED": (
			"At least one mandatory deterministic rule was excluded; exclusion is not a pass."
		),
		"MANDATORY_RULE_FAILED": (
			"At least one exact approved mandatory deterministic rule produced Failed."
		),
		"MANDATORY_RULE_INSUFFICIENT_DATA": (
			"At least one mandatory deterministic rule reported insufficient data."
		),
		"MANDATORY_RULE_EXECUTION_ERROR": (
			"At least one mandatory deterministic rule is incomplete or ended in Error."
		),
		"MANDATORY_RULE_EXECUTION_CONFLICT": (
			"Multiple, conflicting, or over-bound execution receipts exist for the same exact "
			"rule, version, event, and record snapshot."
		),
	}
	return rationales.get(
		reason_code,
		"The deterministic pre-archive gate could not establish a safe Allow decision.",
	)


def _eligible_population(policy, start: date, end: date) -> list[Any]:
	date_field = str(policy.get("population_date_field") or "")
	if date_field != "source_finalized_at":
		frappe.throw("Medical-record sampling requires the reviewed source_finalized_at field.")
	filters: list[list[Any]] = [
		["record_status", "=", policy.eligible_record_status],
		["source_system", "=", policy.source_system],
		[date_field, ">=", get_datetime(start)],
		[date_field, "<", get_datetime(end + timedelta(days=1))],
		*[[fieldname, "=", value] for fieldname, value in _scope(policy).items() if value],
	]
	fields = [
		"name",
		"event",
		"record_key",
		"record_status",
		"record_snapshot_hash",
		"source_system",
		"source_record_type",
		"source_record_id_hash",
		"source_finalized_at",
		"hospital",
		"campus",
		"department",
		"ward",
		"patient",
		"encounter",
		"responsible_staff",
	]
	rows = list(
		frappe.get_list(
			MEDICAL_RECORD_QC_DOCTYPE,
			filters=filters,
			fields=fields,
			order_by="name asc",
			limit_page_length=MAX_POPULATION_RECORDS + 1,
		)
	)
	if len(rows) > MAX_POPULATION_RECORDS:
		frappe.throw(
			f"Eligible medical-record population exceeds the hard limit of "
			f"{MAX_POPULATION_RECORDS}; narrow the approved scope or period.",
			frappe.ValidationError,
		)
	return rows


def _source_population_manifest(
	rows: Sequence[Any],
	*,
	period_start: date,
	period_end: date,
) -> dict[str, Any]:
	"""Bind source identity, finalization time, and the exact eligible-period predicate."""
	leaves: list[str] = []
	source_identities: set[tuple[str, str]] = set()
	period_floor = get_datetime(period_start)
	period_ceiling = get_datetime(period_end + timedelta(days=1))
	for raw_row in rows:
		row = _as_mapping(raw_row)
		source_record_type = str(row.get("source_record_type") or "")
		source_record_id_hash = str(row.get("source_record_id_hash") or "")
		record_snapshot_hash = str(row.get("record_snapshot_hash") or "")
		source_finalized_at = _site_naive_datetime(
			row.get("source_finalized_at"),
			label="Eligible medical-record source_finalized_at",
		)
		if (
			not source_record_type
			or len(source_record_type) > 200
			or source_record_type != source_record_type.strip()
			or not _is_sha256(source_record_id_hash)
			or not _is_sha256(record_snapshot_hash)
			or str(row.get("record_status") or "") != "Final"
			or source_finalized_at < period_floor
			or source_finalized_at >= period_ceiling
		):
			frappe.throw("Eligible medical record has invalid source reconciliation identity.")
		source_identity = (source_record_type, source_record_id_hash)
		if source_identity in source_identities:
			frappe.throw("Eligible medical-record source reconciliation contains duplicate identities.")
		source_identities.add(source_identity)
		leaves.append(
			_sha256(
				_canonical_json(
					{
						"record_snapshot_hash": record_snapshot_hash,
						"source_record_id_hash": source_record_id_hash,
						"source_record_type": source_record_type,
						"source_finalized_at": source_finalized_at.isoformat(timespec="microseconds"),
					}
				)
			)
		)
	if len(leaves) != len(set(leaves)):
		frappe.throw("Eligible medical-record source reconciliation contains duplicate leaves.")
	leaves.sort()
	manifest_json = _canonical_json(
		{
			"eligible_record_status": "Final",
			"period_end": period_end.isoformat(),
			"period_start": period_start.isoformat(),
			"population_date_field": "source_finalized_at",
			"source_identity_leaves": leaves,
		}
	)
	return {
		"source_identity_manifest_json": manifest_json,
		"source_manifest_hash": _sha256(manifest_json),
		"source_record_count": len(leaves),
	}


def _assert_source_population_watermark(
	watermark,
	rows: Sequence[Any],
	*,
	period_start: date,
	period_end: date,
) -> dict[str, Any]:
	reconciliation = _source_population_manifest(
		rows,
		period_start=period_start,
		period_end=period_end,
	)
	source_asserted_at = _site_naive_datetime(
		watermark.get("source_asserted_at"),
		label="Completeness watermark source_asserted_at",
	)
	if rows:
		max_finalized_at = max(
			_site_naive_datetime(
				_as_mapping(row).get("source_finalized_at"),
				label="Eligible medical-record source_finalized_at",
			)
			for row in rows
		)
		if source_asserted_at < max_finalized_at:
			frappe.throw("Completeness watermark predates a current eligible source finalization.")
	if (
		type(watermark.get("source_record_count")) is not int
		or int(watermark.get("source_record_count")) != reconciliation["source_record_count"]
		or not _is_sha256(str(watermark.get("source_manifest_hash") or ""))
		or not hmac.compare_digest(
			str(watermark.get("source_manifest_hash") or ""),
			str(reconciliation["source_manifest_hash"]),
		)
	):
		frappe.throw(
			"Signed source completeness manifest does not match the entire current eligible population."
		)
	return reconciliation


def _population_record(row: Any) -> dict[str, Any]:
	values = _as_mapping(row)
	for fieldname in (
		"name",
		"record_key",
		"record_status",
		"record_snapshot_hash",
		"source_system",
		"source_record_type",
		"source_record_id_hash",
		"source_finalized_at",
		"hospital",
	):
		if values.get(fieldname) in (None, ""):
			frappe.throw(f"Eligible medical record is missing required {fieldname}.")
	if str(values.get("record_status") or "") != "Final":
		frappe.throw("Eligible medical record is no longer Final.")
	for fieldname in ("record_snapshot_hash", "source_record_id_hash"):
		if not _is_sha256(str(values.get(fieldname) or "")):
			frappe.throw(f"Eligible medical record has invalid {fieldname}.")
	token_payload = {
		"name": str(values["name"]),
		"event": str(values.get("event") or ""),
		"record_key": str(values["record_key"]),
		"record_status": str(values["record_status"]),
		"record_snapshot_hash": str(values["record_snapshot_hash"]),
		"source_system": str(values["source_system"]),
		"source_record_type": str(values["source_record_type"]),
		"source_record_id_hash": str(values["source_record_id_hash"]),
		"source_finalized_at": str(values["source_finalized_at"]),
		"scope": {fieldname: values.get(fieldname) for fieldname in _SCOPE_FIELDS},
		"patient": values.get("patient"),
		"encounter": values.get("encounter"),
		"responsible_staff": values.get("responsible_staff"),
	}
	return {"token": _sha256(_canonical_json(token_payload)), "row": values}


def _ranked_population(
	population: Sequence[Mapping[str, Any]],
	*,
	secret: bytes,
	domain: str,
	batch_key: str,
) -> list[dict[str, Any]]:
	ranked: list[dict[str, Any]] = []
	for raw_item in population:
		item = dict(raw_item)
		message = f"{domain}|{batch_key}|{item['token']}".encode()
		item["score"] = hmac.new(secret, message, hashlib.sha256).hexdigest()
		ranked.append(item)
	return sorted(ranked, key=lambda item: (item["score"], item["token"]))


def _sample_size(
	population_count: int,
	*,
	rate_basis_points: int,
	minimum: int,
	maximum: int,
	rounding_mode: str,
	undersized_action: str,
) -> int:
	if population_count < 0:
		raise ValueError("Population count cannot be negative.")
	if not 1 <= rate_basis_points <= 10_000:
		raise ValueError("Sampling rate must be 1-10,000 basis points.")
	if minimum < 1 or maximum < minimum:
		raise ValueError("Sampling minimum/maximum are invalid.")
	if rounding_mode not in ROUNDING_MODES:
		raise ValueError("Sampling rounding mode is invalid.")
	if undersized_action not in UNDERSIZED_ACTIONS:
		raise ValueError("Undersized-population action is invalid.")
	if population_count == 0:
		return 0
	numerator = population_count * rate_basis_points
	if rounding_mode == "Ceiling":
		target = (numerator + 9_999) // 10_000
	elif rounding_mode == "Floor":
		target = numerator // 10_000
	else:
		target = (numerator + 5_000) // 10_000
	target = min(max(target, minimum), maximum)
	if target > population_count:
		if undersized_action == "Fail":
			frappe.throw(
				"Eligible population cannot satisfy the approved minimum sample size.",
				frappe.ValidationError,
			)
		return population_count
	return target


def _validate_policy_definition(doc, *, require_activation_ready: bool) -> None:
	for fieldname in (
		"policy_code",
		"policy_version",
		"source_system",
		"hospital",
		"effective_from",
		"eligible_record_status",
		"population_date_field",
		"rounding_mode",
		"undersized_population_action",
		"expert_non_pass_policy",
		"mandatory_rule_manifest_json",
		"mandatory_rule_manifest_hash",
		"sampling_key_id",
		"sampling_key_fingerprint",
	):
		if doc.get(fieldname) in (None, ""):
			frappe.throw(f"Medical-record sampling policy requires {fieldname}.")
	if str(doc.get("eligible_record_status") or "") != "Final":
		frappe.throw("Pre-archive medical-record sampling accepts only records explicitly marked Final.")
	if str(doc.get("population_date_field") or "") != "source_finalized_at":
		frappe.throw("Sampling policy population_date_field must be source_finalized_at.")
	if str(doc.get("rounding_mode") or "") not in ROUNDING_MODES:
		frappe.throw("Sampling policy rounding_mode is invalid.")
	if str(doc.get("undersized_population_action") or "") not in UNDERSIZED_ACTIONS:
		frappe.throw("Sampling policy undersized_population_action is invalid.")
	if str(doc.get("expert_non_pass_policy") or "") not in EXPERT_NON_PASS_POLICIES:
		frappe.throw("Sampling policy expert_non_pass_policy is invalid.")
	if type(doc.get("archive_gate_enabled")) is not int or int(doc.get("archive_gate_enabled")) not in {
		0,
		1,
	}:
		frappe.throw("Sampling policy archive_gate_enabled must be an explicit Check value.")
	if type(doc.get("auto_allow_deterministic_pass")) is not int or int(
		doc.get("auto_allow_deterministic_pass")
	) not in {0, 1}:
		frappe.throw("Sampling policy auto_allow_deterministic_pass must be an explicit Check value.")
	manifest = _mandatory_rule_manifest(doc.get("mandatory_rule_manifest_json"))
	manifest_hash = _sha256(_canonical_json(list(manifest)))
	if not hmac.compare_digest(
		manifest_hash,
		str(doc.get("mandatory_rule_manifest_hash") or ""),
	):
		frappe.throw("Sampling policy mandatory-rule manifest hash does not match.")
	for prefix in ("", "expert_"):
		rate = _required_int(doc, f"{prefix}sample_rate_basis_points")
		minimum = _required_int(doc, f"{prefix}minimum_sample_size")
		maximum = _required_int(doc, f"{prefix}maximum_sample_size")
		if not 1 <= rate <= 10_000:
			frappe.throw(f"Sampling policy {prefix}sample_rate_basis_points must be 1-10,000.")
		if minimum < 1 or maximum < minimum or maximum > MAX_POPULATION_RECORDS:
			frappe.throw(f"Sampling policy {prefix}sample size bounds are invalid.")
	start = getdate(doc.get("effective_from"))
	end = getdate(doc.get("effective_to")) if doc.get("effective_to") else None
	if end and end < start:
		frappe.throw("Sampling-policy effective_to cannot precede effective_from.")
	_validate_scope_shape(doc)
	if require_activation_ready:
		if not int(doc.get("archive_gate_enabled") or 0):
			frappe.throw("An approved production sampling policy must explicitly enable its archive gate.")
		if _mandatory_rule_definition_reason(manifest, activation=True):
			frappe.throw("Every mandatory rule must be an exact non-shadow Published deterministic version.")
		_outcome_set(doc.get("coder_allow_outcomes_json"), label="coder_allow_outcomes_json")
		_outcome_set(doc.get("expert_allow_outcomes_json"), label="expert_allow_outcomes_json")


def _validate_active_policy(policy, start: date, end: date) -> None:
	# Publication is required at approval/activation time. Once an active policy
	# is frozen, later rule retirement or checksum drift must not make the full
	# population disappear: each record is still assigned and receives a
	# deterministic Hold from _mandatory_rule_gate.
	_validate_policy_definition(policy, require_activation_ready=False)
	if str(policy.get("status") or "") != "Approved" or not int(policy.get("enabled") or 0):
		frappe.throw("Medical-record sampling policy is not active.")
	if not int(policy.get("archive_gate_enabled") or 0):
		frappe.throw("An active sampling policy must retain its approved archive gate.")
	_outcome_set(policy.get("coder_allow_outcomes_json"), label="coder_allow_outcomes_json")
	_outcome_set(policy.get("expert_allow_outcomes_json"), label="expert_allow_outcomes_json")
	_assert_policy_checksum(policy)
	effective_from = getdate(policy.effective_from)
	effective_to = getdate(policy.effective_to) if policy.get("effective_to") else None
	if start < effective_from or (effective_to and end > effective_to):
		frappe.throw("Review period is outside the approved policy effective dates.")


def _assert_frozen_policy(assignment, policy) -> None:
	batch_checksum = frappe.db.get_value(
		REVIEW_BATCH_DOCTYPE,
		assignment.batch,
		"policy_checksum",
	)
	if not _is_sha256(str(batch_checksum or "")):
		frappe.throw("Review batch has no valid frozen policy checksum.")
	if not hmac.compare_digest(str(batch_checksum), str(policy.get("approval_checksum") or "")):
		frappe.throw("Review assignment policy no longer matches its frozen batch.")
	_assert_policy_checksum(policy)


def _sampling_secret(policy) -> bytes:
	key_id = str(policy.get("sampling_key_id") or "").strip()
	if not key_id or len(key_id) > 128:
		frappe.throw("Sampling policy requires a bounded sampling_key_id.")
	expected_fingerprint = str(policy.get("sampling_key_fingerprint") or "").strip()
	if not _is_sha256(expected_fingerprint):
		frappe.throw("Sampling policy requires a SHA-256 sampling_key_fingerprint.")
	secrets = frappe.conf.get("ione_medical_record_sampling_secrets")
	if not isinstance(secrets, Mapping):
		frappe.throw("Medical-record sampling secrets are not configured.")
	secret = secrets.get(key_id)
	if not isinstance(secret, str) or len(secret.encode()) < 32:
		frappe.throw("Sampling policy key is unavailable or shorter than 32 bytes.")
	secret_bytes = secret.encode()
	if not hmac.compare_digest(expected_fingerprint, _sha256_bytes(secret_bytes)):
		frappe.throw("Sampling policy key fingerprint does not match the configured secret.")
	return secret_bytes


def _policy_checksum(doc) -> str:
	return _sha256(_canonical_json({fieldname: doc.get(fieldname) for fieldname in _POLICY_SEMANTIC_FIELDS}))


def _assert_policy_checksum(doc) -> None:
	stored = str(doc.get("approval_checksum") or "")
	actual = _policy_checksum(doc)
	if not _is_sha256(stored) or not hmac.compare_digest(stored, actual):
		frappe.throw("Sampling-policy approval checksum is missing or stale.")


def _batch_key(
	policy,
	start: date,
	end: date,
	*,
	watermark,
	parent_batch: str | None,
	supplement_sequence: int,
) -> str:
	return _sha256(
		_canonical_json(
			{
				"parent_batch": parent_batch or "",
				"policy": policy.name,
				"policy_checksum": policy.approval_checksum,
				"period_start": start.isoformat(),
				"period_end": end.isoformat(),
				"scope": _scope(policy),
				"source_completeness_hash": watermark.watermark_checksum,
				"source_completeness_watermark": watermark.name,
				"source_complete_through": str(watermark.complete_through),
				"source_manifest_hash": watermark.source_manifest_hash,
				"source_record_count": int(watermark.source_record_count),
				"source_system": policy.source_system,
				"supplement_sequence": supplement_sequence,
			}
		)
	)


def _validate_existing_batch(
	existing,
	policy,
	start: date,
	end: date,
	batch_key: str,
	*,
	watermark,
	parent_batch: str | None,
	supplement_sequence: int,
) -> None:
	if not hmac.compare_digest(str(existing.batch_key or ""), batch_key):
		frappe.throw("Existing medical-record review batch key does not match.")
	if str(existing.policy or "") != str(policy.name):
		frappe.throw("Existing medical-record review batch belongs to another policy.")
	if not hmac.compare_digest(
		str(existing.policy_checksum or ""),
		str(policy.approval_checksum or ""),
	):
		frappe.throw("Existing medical-record review batch policy checksum does not match.")
	if getdate(existing.period_start) != start or getdate(existing.period_end) != end:
		frappe.throw("Existing medical-record review batch period does not match.")
	if (
		str(existing.get("parent_batch") or "") != str(parent_batch or "")
		or str(existing.get("batch_kind") or "") != ("Supplemental" if parent_batch else "Initial")
		or int(existing.get("supplement_sequence") or 0) != supplement_sequence
		or str(existing.get("source_system") or "") != str(policy.source_system)
		or str(existing.get("source_completeness_watermark") or "") != str(watermark.name)
		or not hmac.compare_digest(
			str(existing.get("source_completeness_hash") or ""),
			str(watermark.watermark_checksum),
		)
		or getdate(existing.get("source_complete_through")) != getdate(watermark.complete_through)
	):
		frappe.throw("Existing medical-record review batch completeness lineage does not match.")
	_validate_batch_assignment_coverage(existing)


def _review_batch_chain(policy: str, start: date, end: date) -> list[Any]:
	names = frappe.get_all(
		REVIEW_BATCH_DOCTYPE,
		filters={
			"policy": policy,
			"period_start": start,
			"period_end": end,
		},
		pluck="name",
		order_by="supplement_sequence asc, name asc",
		limit_page_length=MAX_BATCH_CHAIN_LENGTH + 1,
	)
	if len(names) > MAX_BATCH_CHAIN_LENGTH:
		frappe.throw("Medical-record supplemental batch chain exceeds its governed bound.")
	chain = [frappe.get_doc(REVIEW_BATCH_DOCTYPE, name, for_update=True) for name in names]
	for index, batch in enumerate(chain):
		if int(batch.get("supplement_sequence") or 0) != index:
			frappe.throw("Medical-record supplemental batch chain sequence is incomplete.")
		if index == 0:
			if str(batch.get("batch_kind") or "") != "Initial" or batch.get("parent_batch"):
				frappe.throw("Medical-record batch chain has no valid Initial root.")
		elif str(batch.get("batch_kind") or "") != "Supplemental" or str(
			batch.get("parent_batch") or ""
		) != str(chain[index - 1].name):
			frappe.throw("Medical-record supplemental batch chain is branched or broken.")
	return chain


def _validated_batch_parent(chain: Sequence[Any], parent_batch: str | None):
	name = str(parent_batch or "").strip()
	if not name:
		return None
	for batch in chain:
		if str(batch.name) == name:
			return batch
	frappe.throw("Supplemental medical-record batch parent is outside the exact policy period chain.")


def _validate_batch_chain_lineage(
	chain: Sequence[Any],
	policy,
	start: date,
	end: date,
) -> None:
	for sequence, batch in enumerate(chain):
		parent = str(chain[sequence - 1].name) if sequence else None
		watermark = _validated_completeness_watermark(
			policy,
			str(batch.get("source_completeness_watermark") or ""),
			period_start=start,
			period_end=end,
		)
		expected_key = _batch_key(
			policy,
			start,
			end,
			watermark=watermark,
			parent_batch=parent,
			supplement_sequence=sequence,
		)
		_validate_existing_batch(
			batch,
			policy,
			start,
			end,
			expected_key,
			watermark=watermark,
			parent_batch=parent,
			supplement_sequence=sequence,
		)
		if sequence:
			_validate_watermark_progress(chain[sequence - 1], watermark)


def _batch_chain_population_tokens(chain: Sequence[Any]) -> set[str]:
	covered: set[str] = set()
	for batch in chain:
		tokens = _decoded_hash_manifest(batch.get("population_manifest_json"))
		if covered.intersection(tokens):
			frappe.throw("Medical-record supplemental chain contains duplicate population coverage.")
		covered.update(tokens)
	return covered


def _validate_batch_chain_coverage(
	chain: Sequence[Any],
	current_population: Sequence[Mapping[str, Any]],
) -> None:
	for batch in chain:
		_validate_batch_assignment_coverage(batch)
	covered = _batch_chain_population_tokens(chain)
	current_tokens = {str(item["token"]) for item in current_population}
	if len(current_tokens) != len(current_population):
		frappe.throw("Current medical-record population contains duplicate frozen identities.")
	uncovered = sorted(current_tokens.difference(covered))
	if uncovered:
		frappe.throw(
			"Late Final medical records are outside the frozen chain; create a linked "
			"Supplemental batch with a newer signed completeness watermark."
		)


def _validate_batch_assignment_coverage(batch) -> None:
	for fieldname in (
		"population_manifest_json",
		"selection_manifest_json",
		"expert_selection_manifest_json",
	):
		_validate_hash_manifest(batch.get(fieldname), label=fieldname)
	rows = frappe.get_all(
		REVIEW_ASSIGNMENT_DOCTYPE,
		filters={"batch": batch.name},
		fields=["population_token", "coder_required", "expert_required"],
		order_by="population_token asc",
		limit_page_length=MAX_POPULATION_RECORDS + 1,
	)
	if len(rows) > MAX_POPULATION_RECORDS:
		frappe.throw("Medical-record batch assignment coverage exceeds its governed bound.")
	population = sorted(str(row.get("population_token") or "") for row in rows)
	coder = sorted(
		str(row.get("population_token") or "") for row in rows if int(row.get("coder_required") or 0)
	)
	expert = sorted(
		str(row.get("population_token") or "") for row in rows if int(row.get("expert_required") or 0)
	)
	if (
		any(not _is_sha256(token) for token in population)
		or population != _decoded_hash_manifest(batch.get("population_manifest_json"))
		or coder != _decoded_hash_manifest(batch.get("selection_manifest_json"))
		or expert != _decoded_hash_manifest(batch.get("expert_selection_manifest_json"))
		or len(population) != int(batch.get("population_count") or 0)
		or len(coder) != int(batch.get("sample_count") or 0)
		or len(expert) != int(batch.get("expert_sample_count") or 0)
		or not hmac.compare_digest(
			str(batch.get("population_hash") or ""),
			_sha256(str(batch.get("population_manifest_json") or "")),
		)
		or not hmac.compare_digest(
			str(batch.get("selection_hash") or ""),
			_sha256(str(batch.get("selection_manifest_json") or "")),
		)
		or not hmac.compare_digest(
			str(batch.get("expert_selection_hash") or ""),
			_sha256(str(batch.get("expert_selection_manifest_json") or "")),
		)
	):
		frappe.throw("Medical-record batch population manifest and durable assignments are not one-to-one.")


def _validated_completeness_watermark(
	policy,
	watermark_name: str,
	*,
	period_start: date,
	period_end: date,
):
	name = str(watermark_name or "").strip()
	if not name:
		frappe.throw("Review batch creation requires an explicit signed completeness watermark.")
	watermark = frappe.get_doc(COMPLETENESS_WATERMARK_DOCTYPE, name)
	_assert_watermark_checksum(watermark)
	if str(watermark.get("source_system") or "") != str(policy.get("source_system") or ""):
		frappe.throw("Completeness watermark belongs to another source system.")
	if any(
		str(watermark.get(fieldname) or "") != str(policy.get(fieldname) or "") for fieldname in _SCOPE_FIELDS
	):
		frappe.throw("Completeness watermark scope must exactly equal the sampling policy scope.")
	if (
		getdate(watermark.get("period_start")) != period_start
		or getdate(watermark.get("period_end")) != period_end
		or str(watermark.get("eligible_record_status") or "")
		!= str(policy.get("eligible_record_status") or "")
		or str(watermark.get("population_date_field") or "") != str(policy.get("population_date_field") or "")
	):
		frappe.throw("Completeness watermark must sign the exact batch period and eligibility predicate.")
	if getdate(watermark.get("complete_through")) < period_end:
		frappe.throw("Source completeness watermark does not cover the requested period end.")
	_assert_watermark_cutoff_time(
		_site_naive_datetime(
			watermark.get("source_asserted_at"),
			label="Completeness watermark source_asserted_at",
		),
		getdate(watermark.get("complete_through")),
	)
	return watermark


@contextmanager
def _locked_current_completeness_watermark(
	policy,
	watermark_name: str,
	*,
	period_start: date,
	period_end: date,
) -> Iterator[Any]:
	stream_lock = _completeness_stream_lock_key(
		str(policy.get("source_system") or ""),
		_scope(policy),
	)
	with frappe.db.advisory_lock(stream_lock, timeout=30):
		watermark = _validated_completeness_watermark(
			policy,
			watermark_name,
			period_start=period_start,
			period_end=period_end,
		)
		latest = _latest_completeness_watermark(
			str(policy.get("source_system") or ""),
			_scope(policy),
			for_update=True,
		)
		if latest is None or str(latest.name) != str(watermark.name):
			frappe.throw("Review batch creation requires the current latest completeness watermark.")
		yield watermark


def _completeness_stream_lock_key(
	source_system: str,
	scope: Mapping[str, Any],
) -> str:
	if not source_system:
		frappe.throw("Completeness watermark stream requires a source system.")
	identity = {
		"source_system": str(source_system),
		"scope": {fieldname: str(scope.get(fieldname) or "") for fieldname in _SCOPE_FIELDS},
	}
	return f"ione-qms:record-completeness-stream:{_sha256(_canonical_json(identity))}"


def _latest_completeness_watermark(
	source_system: str,
	scope: Mapping[str, Any],
	*,
	for_update: bool,
):
	filters: dict[str, Any] = {"source_system": source_system}
	for fieldname in _SCOPE_FIELDS:
		value = str(scope.get(fieldname) or "")
		filters[fieldname] = value if value else ["is", "not set"]
	rows = frappe.get_all(
		COMPLETENESS_WATERMARK_DOCTYPE,
		filters=filters,
		fields=["name", "source_sequence"],
		order_by="source_sequence desc, source_asserted_at desc, name desc",
		limit_page_length=2,
	)
	if not rows:
		return None
	if len(rows) > 1 and int(rows[0].get("source_sequence") or 0) == int(rows[1].get("source_sequence") or 0):
		frappe.throw("Completeness watermark stream has a duplicate source sequence.")
	latest = frappe.get_doc(
		COMPLETENESS_WATERMARK_DOCTYPE,
		rows[0].name,
		for_update=for_update,
	)
	_assert_watermark_checksum(latest)
	if str(latest.get("source_system") or "") != source_system or any(
		str(latest.get(fieldname) or "") != str(scope.get(fieldname) or "") for fieldname in _SCOPE_FIELDS
	):
		frappe.throw("Completeness watermark latest lookup crossed its exact source scope.")
	return latest


def _validate_watermark_progress(parent_batch, watermark) -> None:
	previous = frappe.get_doc(
		COMPLETENESS_WATERMARK_DOCTYPE,
		parent_batch.source_completeness_watermark,
	)
	_assert_watermark_checksum(previous)
	if (
		str(previous.get("source_system") or "") != str(watermark.get("source_system") or "")
		or any(
			str(previous.get(fieldname) or "") != str(watermark.get(fieldname) or "")
			for fieldname in _SCOPE_FIELDS
		)
		or getdate(watermark.get("complete_through")) < getdate(previous.get("complete_through"))
		or int(watermark.get("source_sequence") or 0) <= int(previous.get("source_sequence") or 0)
		or not hmac.compare_digest(
			str(parent_batch.get("source_completeness_hash") or ""),
			str(previous.get("watermark_checksum") or ""),
		)
	):
		frappe.throw("Supplemental batch completeness watermark does not extend its parent lineage.")
	if hmac.compare_digest(
		str(previous.get("watermark_checksum") or ""),
		str(watermark.get("watermark_checksum") or ""),
	):
		frappe.throw("A Supplemental batch requires a new signed completeness assertion.")
	if get_datetime(watermark.get("source_asserted_at")) <= get_datetime(previous.get("source_asserted_at")):
		frappe.throw("Supplemental completeness assertion must be newer than its parent.")


def _batch_result(batch, *, created: bool) -> dict[str, Any]:
	return {
		"batch": batch.name,
		"created": created,
		"status": str(batch.status),
		"population_count": int(batch.population_count or 0),
		"sample_count": int(batch.sample_count or 0),
		"expert_sample_count": int(batch.expert_sample_count or 0),
		"population_hash": str(batch.population_hash),
		"selection_hash": str(batch.selection_hash),
	}


def _update_batch_completion(batch_name: str) -> None:
	with frappe.db.advisory_lock(f"ione-qms:record-review-batch-complete:{batch_name}", timeout=10):
		open_count = frappe.db.count(
			REVIEW_ASSIGNMENT_DOCTYPE,
			{"batch": batch_name, "status": ["!=", "Closed"]},
		)
		if open_count:
			return
		batch = frappe.get_doc(REVIEW_BATCH_DOCTYPE, batch_name, for_update=True)
		if str(batch.get("status") or "") == "Completed":
			return
		batch.status = "Completed"
		batch.completed_at = now_datetime()
		with _creation_capability(
			"ione_medical_record_batch_runtime",
			_BATCH_RUNTIME_CAPABILITY,
		):
			batch.save(ignore_permissions=True)


def _update_assignment(assignment, values: Mapping[str, Any]) -> None:
	fields = set(values)
	if not fields.issubset(_ASSIGNMENT_RUNTIME_FIELDS):
		raise ValueError("Attempted to update non-runtime medical-record assignment fields.")
	current_status = str(assignment.get("status") or "")
	allowed = bool(
		(fields == _ASSIGNMENT_DECISION_RUNTIME_FIELDS and values.get("latest_archive_decision"))
		or (
			fields == _ASSIGNMENT_ACK_RUNTIME_FIELDS
			and values.get("archive_acknowledgement")
			and str(values.get("archive_ack_status") or "") in ACK_STATUSES
		)
		or (
			fields == _ASSIGNMENT_DELIVERY_RUNTIME_FIELDS
			and values.get("pending_archive_delivery")
			and _is_sha256(str(values.get("pending_archive_envelope_hash") or ""))
		)
	)
	if not allowed and current_status == "Coder Review":
		allowed = fields == {"assigned_coder", "coder_claimed_at"} or (
			fields
			== {
				"status",
				"coder_decision",
				"coder_outcome",
				"coder_reviewed_at",
			}
			and str(values.get("status") or "") in {"Expert Review", "Closed"}
		)
	elif not allowed and current_status == "Expert Review":
		allowed = fields == {"assigned_expert", "expert_claimed_at"} or (
			fields
			== {
				"status",
				"expert_decision",
				"expert_outcome",
				"expert_reviewed_at",
			}
			and str(values.get("status") or "") == "Closed"
		)
	if not allowed:
		raise ValueError(
			f"Unsupported medical-record assignment transition from {current_status or 'empty state'}."
		)
	assignment.update(dict(values))
	with _creation_capability(
		"ione_medical_record_assignment_runtime",
		_ASSIGNMENT_RUNTIME_CAPABILITY,
	):
		assignment.save(ignore_permissions=True)


def _review_decision_key(
	assignment,
	*,
	stage: str,
	user: str,
	outcome: str,
	comment: str,
	finding: str | None,
	evidence_references: Sequence[str],
) -> str:
	return _sha256(
		_canonical_json(
			{
				"assignment": assignment.name,
				"record_snapshot_hash": assignment.record_snapshot_hash,
				"stage": stage,
				"reviewer": user,
				"outcome": outcome,
				"comment": comment,
				"finding": finding,
				"evidence_references": list(evidence_references),
			}
		)
	)


def _evidence_references(values: Sequence[str] | None) -> tuple[str, ...]:
	if values is None:
		return ()
	if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
		frappe.throw("Medical-record review evidence references must be a list.")
	if len(values) > MAX_EVIDENCE_REFERENCES:
		frappe.throw(f"No more than {MAX_EVIDENCE_REFERENCES} review evidence references are allowed.")
	normalized: list[str] = []
	for value in values:
		reference = str(value or "").strip()
		if not _EVIDENCE_REFERENCE_RE.fullmatch(reference):
			frappe.throw("Medical-record review evidence reference is invalid.")
		normalized.append(reference)
	if len(set(normalized)) != len(normalized):
		frappe.throw("Medical-record review evidence references cannot contain duplicates.")
	return tuple(normalized)


def _validate_finding_reference(finding: str, assignment, user: str) -> None:
	doc = frappe.get_doc("IONE QC Finding", finding)
	doc.check_permission("read")
	if any(
		assignment.get(fieldname) and str(doc.get(fieldname) or "") != str(assignment.get(fieldname) or "")
		for fieldname in (*_SCOPE_FIELDS, "patient", "encounter")
	):
		frappe.throw("Medical-record review finding is outside the sampled record scope.")
	require_scope_read(**_scope(assignment), user=user)


def _validate_evidence_links(
	references: Sequence[str],
	assignment,
	user: str,
) -> None:
	for reference in references:
		name = reference.split(":", 1)[1]
		doc = frappe.get_doc("IONE QC Finding Evidence", name)
		doc.check_permission("read")
		finding = frappe.get_doc("IONE QC Finding", doc.finding)
		finding.check_permission("read")
		if any(
			assignment.get(fieldname)
			and str(finding.get(fieldname) or "") != str(assignment.get(fieldname) or "")
			for fieldname in (*_SCOPE_FIELDS, "patient", "encounter")
		):
			frappe.throw("Medical-record review evidence is outside the sampled record scope.")
	require_scope_read(**_scope(assignment), user=user)


def _require_assignment_access(assignment, user: str) -> None:
	assignment.check_permission("read")
	require_scope_read(**_scope(assignment), user=user)


def _completeness_watermark_payload(raw_body: bytes) -> dict[str, Any]:
	try:
		text = raw_body.decode("utf-8", errors="strict")
		payload = json.loads(text, object_pairs_hook=_unique_json_object)
	except (UnicodeDecodeError, ValueError) as exc:
		raise frappe.ValidationError(
			"Medical-record completeness watermark must be one canonical UTF-8 JSON object."
		) from exc
	required = {
		"campus",
		"complete_through",
		"department",
		"eligible_record_status",
		"hospital",
		"period_end",
		"period_start",
		"population_date_field",
		"source_asserted_at",
		"source_manifest_hash",
		"source_record_count",
		"source_sequence",
		"source_watermark_id",
		"ward",
	}
	if not isinstance(payload, dict) or set(payload) != required:
		frappe.throw("Medical-record completeness watermark has an invalid exact field contract.")
	for fieldname in required - {"source_record_count", "source_sequence"}:
		if not isinstance(payload.get(fieldname), str):
			frappe.throw("Medical-record completeness watermark text fields must be strings.")
		value = str(payload.get(fieldname) or "")
		if value != value.strip() or len(value) > 140:
			frappe.throw("Medical-record completeness watermark contains a non-canonical value.")
	if not str(payload.get("hospital") or ""):
		frappe.throw("Medical-record completeness watermark requires an exact hospital.")
	if not _SAFE_REQUEST_ID_RE.fullmatch(str(payload.get("source_watermark_id") or "")):
		frappe.throw("Medical-record completeness source_watermark_id is invalid.")
	if not _is_sha256(str(payload.get("source_manifest_hash") or "")):
		frappe.throw("Medical-record completeness source_manifest_hash is invalid.")
	if type(payload.get("source_record_count")) is not int or int(payload["source_record_count"]) < 0:
		frappe.throw("Medical-record completeness source_record_count must be a non-negative integer.")
	if (
		type(payload.get("source_sequence")) is not int
		or not 1 <= int(payload["source_sequence"]) <= 9_223_372_036_854_775_807
	):
		frappe.throw("Medical-record completeness source_sequence must be a positive 64-bit integer.")
	try:
		complete_through = getdate(payload["complete_through"])
		period_start = getdate(payload["period_start"])
		period_end = getdate(payload["period_end"])
	except Exception as exc:
		raise frappe.ValidationError("Medical-record completeness period or cutoff is invalid.") from exc
	if (
		not complete_through
		or complete_through.isoformat() != payload["complete_through"]
		or not period_start
		or period_start.isoformat() != payload["period_start"]
		or not period_end
		or period_end.isoformat() != payload["period_end"]
		or period_start > period_end
		or period_end > complete_through
		or complete_through > getdate(now_datetime())
	):
		frappe.throw(
			"Medical-record completeness period and cutoff must be exact covered non-future ISO dates."
		)
	if (
		str(payload.get("eligible_record_status") or "") != "Final"
		or str(payload.get("population_date_field") or "") != "source_finalized_at"
	):
		frappe.throw("Medical-record completeness predicate must be Final on source_finalized_at.")
	source_asserted_at = _site_naive_datetime(
		payload["source_asserted_at"],
		label="Medical-record completeness source_asserted_at",
		require_timezone=True,
	)
	_assert_watermark_cutoff_time(source_asserted_at, complete_through)
	validated = {
		"campus": str(payload["campus"]),
		"complete_through": complete_through.isoformat(),
		"department": str(payload["department"]),
		"eligible_record_status": "Final",
		"hospital": str(payload["hospital"]),
		"period_end": period_end.isoformat(),
		"period_start": period_start.isoformat(),
		"population_date_field": "source_finalized_at",
		"source_asserted_at": str(payload["source_asserted_at"]),
		"source_manifest_hash": str(payload["source_manifest_hash"]),
		"source_record_count": int(payload["source_record_count"]),
		"source_sequence": int(payload["source_sequence"]),
		"source_watermark_id": str(payload["source_watermark_id"]),
		"ward": str(payload["ward"]),
	}
	if _canonical_json(validated) != text:
		frappe.throw("Medical-record completeness watermark JSON must use canonical serialization.")
	return validated


def _assert_watermark_cutoff_time(source_asserted_at, complete_through: date) -> None:
	"""A complete-through day is closed only at the next site-local midnight."""
	complete_boundary = get_datetime(complete_through + timedelta(days=1))
	if source_asserted_at < complete_boundary:
		frappe.throw(
			"Medical-record completeness source_asserted_at must be at or after "
			"the end of complete_through in the site timezone."
		)


def _watermark_checksum(doc) -> str:
	semantic = {
		"endpoint": doc.get("endpoint"),
		"received_at": doc.get("received_at"),
		"scope": _scope(doc),
		"period_start": doc.get("period_start"),
		"period_end": doc.get("period_end"),
		"eligible_record_status": doc.get("eligible_record_status"),
		"population_date_field": doc.get("population_date_field"),
		"source_asserted_at": doc.get("source_asserted_at"),
		"source_manifest_hash": doc.get("source_manifest_hash"),
		"source_record_count": doc.get("source_record_count"),
		"source_sequence": doc.get("source_sequence"),
		"source_system": doc.get("source_system"),
		"source_watermark_id": doc.get("source_watermark_id"),
		"complete_through": doc.get("complete_through"),
		"request_hash": doc.get("request_hash"),
		"watermark_key": doc.get("watermark_key"),
	}
	return _sha256(_canonical_json(semantic))


def _assert_watermark_checksum(doc) -> None:
	stored = str(doc.get("watermark_checksum") or "")
	if not _is_sha256(stored) or not hmac.compare_digest(stored, _watermark_checksum(doc)):
		frappe.throw("Medical-record completeness watermark checksum is missing or stale.")


def _archive_ack_payload(raw_body: bytes) -> dict[str, str]:
	try:
		text = raw_body.decode("utf-8", errors="strict")
		payload = json.loads(text, object_pairs_hook=_unique_json_object)
	except (UnicodeDecodeError, ValueError) as exc:
		raise frappe.ValidationError(
			"Archive acknowledgement must be one canonical UTF-8 JSON object."
		) from exc
	required = {
		"archive_decision",
		"decision_checksum",
		"delivery",
		"envelope_hash",
		"source_record_id_hash",
		"source_ack_id",
		"source_ack_at",
		"ack_status",
		"message_code",
	}
	if not isinstance(payload, dict) or set(payload) != required:
		frappe.throw("Archive acknowledgement has an invalid exact field contract.")
	if any(not isinstance(payload.get(fieldname), str) for fieldname in required):
		frappe.throw("Archive acknowledgement fields must all be JSON strings.")
	if _canonical_json(payload) != text:
		frappe.throw("Archive acknowledgement JSON must use canonical serialization.")
	for fieldname in ("decision_checksum", "envelope_hash", "source_record_id_hash"):
		value = str(payload.get(fieldname) or "")
		if not _is_sha256(value):
			frappe.throw(f"Archive acknowledgement {fieldname} is invalid.")
	if not _SAFE_ACK_ID_RE.fullmatch(str(payload.get("source_ack_id") or "")):
		frappe.throw("Archive acknowledgement source_ack_id is invalid.")
	if str(payload.get("ack_status") or "") not in ACK_STATUSES:
		frappe.throw("Archive acknowledgement ack_status is invalid.")
	_safe_code(payload.get("message_code"), label="Archive acknowledgement message code")
	_site_naive_datetime(
		payload.get("source_ack_at"),
		label="Archive acknowledgement source_ack_at",
		require_timezone=True,
	)
	archive_decision = str(payload.get("archive_decision") or "").strip()
	if not _is_sha256(archive_decision):
		frappe.throw("Archive acknowledgement archive_decision is invalid.")
	delivery = str(payload.get("delivery") or "").strip()
	if not _is_sha256(delivery):
		frappe.throw("Archive acknowledgement delivery is invalid.")
	return {key: str(value) for key, value in payload.items()}


def _archive_ack_result(ack, *, replay: bool) -> dict[str, str]:
	return {
		"acknowledgement": ack.name,
		"archive_decision": str(ack.archive_decision),
		"delivery": str(ack.delivery),
		"envelope_hash": str(ack.envelope_hash),
		"ack_status": str(ack.ack_status),
		"replay": str(int(replay)),
	}


def _assert_archive_ack_replay_current(
	ack,
	endpoint,
	payload: Mapping[str, str],
	*,
	record_name: str,
) -> None:
	"""Reject an exact old request once its decision or receipt is no longer current."""
	for payload_field, receipt_field in (
		("archive_decision", "archive_decision"),
		("decision_checksum", "decision_checksum"),
		("delivery", "delivery"),
		("envelope_hash", "envelope_hash"),
		("source_record_id_hash", "source_record_id_hash"),
		("source_ack_id", "source_ack_id"),
		("ack_status", "ack_status"),
		("message_code", "message_code"),
	):
		if str(payload.get(payload_field) or "") != str(ack.get(receipt_field) or ""):
			frappe.throw("Archive acknowledgement replay no longer matches its immutable receipt.")
	payload_source_at = _site_naive_datetime(
		payload.get("source_ack_at"),
		label="Archive acknowledgement replay source_ack_at",
		require_timezone=True,
	)
	receipt_source_at = _site_naive_datetime(
		ack.get("source_ack_at"),
		label="Archive acknowledgement receipt source_ack_at",
	)
	if payload_source_at != receipt_source_at:
		frappe.throw("Archive acknowledgement replay source time no longer matches its receipt.")
	if str(ack.get("endpoint") or "") != str(endpoint.name) or str(ack.get("source_system") or "") != str(
		endpoint.source_system
	):
		frappe.throw("Archive acknowledgement replay authority no longer matches its receipt.")

	decision = frappe.get_doc(
		ARCHIVE_DECISION_DOCTYPE,
		ack.archive_decision,
		for_update=True,
	)
	assignment = frappe.get_doc(
		REVIEW_ASSIGNMENT_DOCTYPE,
		decision.assignment,
		for_update=True,
	)
	if (
		str(assignment.get("medical_record_qc") or "") != record_name
		or str(decision.get("assignment") or "") != str(assignment.name)
		or str(ack.get("assignment") or "") != str(assignment.name)
		or str(assignment.get("latest_archive_decision") or "") != str(decision.name)
		or str(assignment.get("archive_acknowledgement") or "") != str(ack.name)
		or str(assignment.get("archive_ack_status") or "") != str(ack.get("ack_status") or "")
		or assignment.get("pending_archive_delivery")
		or assignment.get("pending_archive_envelope_hash")
	):
		frappe.throw("Archive acknowledgement replay is stale for the current assignment.")
	if not hmac.compare_digest(
		str(decision.get("decision_checksum") or ""),
		str(ack.get("decision_checksum") or ""),
	) or not hmac.compare_digest(
		str(decision.get("source_record_id_hash") or ""),
		str(ack.get("source_record_id_hash") or ""),
	):
		frappe.throw("Archive acknowledgement replay decision lineage no longer matches.")
	delivery = frappe.get_doc(
		ARCHIVE_DELIVERY_DOCTYPE,
		ack.delivery,
	)
	if str(delivery.get("endpoint") or "") != str(endpoint.name) or str(
		delivery.get("source_system") or ""
	) != str(endpoint.source_system):
		frappe.throw("Archive acknowledgement replay delivery authority no longer matches.")
	_assert_delivery_contains_pending_decision(
		delivery,
		assignment,
		decision,
		envelope_hash=str(ack.get("envelope_hash") or ""),
		require_pending=False,
	)
	source = frappe.get_cached_doc("IONE Source System", endpoint.source_system)
	authorize_endpoint_scope(
		endpoint,
		source,
		{
			**_scope(decision),
			"source_system": decision.source_system,
		},
	)


def _site_naive_datetime(
	value: Any,
	*,
	label: str,
	require_timezone: bool = False,
):
	"""Normalize an explicit source timestamp to the site's naive storage timezone."""
	try:
		parsed = get_datetime(value)
	except Exception as exc:
		raise frappe.ValidationError(f"{label} is invalid.") from exc
	if not parsed:
		frappe.throw(f"{label} is invalid.")
	if require_timezone and parsed.tzinfo is None:
		frappe.throw(f"{label} must include an explicit timezone offset.")
	if parsed.tzinfo is not None:
		try:
			parsed = parsed.astimezone(ZoneInfo(get_system_timezone())).replace(tzinfo=None)
		except Exception as exc:
			raise frappe.ValidationError(f"{label} cannot be normalized to the site timezone.") from exc
	return parsed


def _outcome_set(value: Any, *, label: str) -> frozenset[str]:
	try:
		decoded = json.loads(str(value or ""), object_pairs_hook=_unique_json_object)
	except ValueError as exc:
		raise frappe.ValidationError(f"Sampling policy {label} must be canonical JSON.") from exc
	if (
		not isinstance(decoded, list)
		or not decoded
		or len(decoded) > len(REVIEW_OUTCOMES)
		or any(not isinstance(item, str) or item not in REVIEW_OUTCOMES for item in decoded)
		or len(set(decoded)) != len(decoded)
		or _canonical_json(decoded) != str(value)
	):
		frappe.throw(f"Sampling policy {label} must be a non-empty canonical outcome list.")
	return frozenset(decoded)


def _validate_scope_shape(doc) -> None:
	hospital = str(doc.get("hospital") or "").strip()
	campus = str(doc.get("campus") or "").strip()
	department = str(doc.get("department") or "").strip()
	ward = str(doc.get("ward") or "").strip()
	if not hospital:
		frappe.throw("Medical-record sampling requires a hospital scope.")
	if ward and not department:
		frappe.throw("A ward-scoped sampling policy requires a department.")
	if department and not campus:
		frappe.throw("A department-scoped sampling policy requires a campus.")


def _scope(doc) -> dict[str, str | None]:
	return {fieldname: doc.get(fieldname) or None for fieldname in _SCOPE_FIELDS}


def _closed_period(start: str | date, end: str | date) -> tuple[date, date]:
	try:
		period_start = getdate(start)
		period_end = getdate(end)
	except Exception as exc:
		raise frappe.ValidationError("Medical-record review period is invalid.") from exc
	if not period_start or not period_end or period_start > period_end:
		frappe.throw("Medical-record review period must be a non-empty closed date range.")
	if (period_end - period_start).days + 1 > 366:
		frappe.throw("Medical-record review period cannot exceed 366 inclusive days.")
	return period_start, period_end


def _required_int(doc, fieldname: str) -> int:
	value = doc.get(fieldname)
	if type(value) is not int:
		frappe.throw(f"Sampling policy {fieldname} must be an explicit integer.")
	return value


def _named_user() -> str:
	user = str(frappe.session.user or "").strip()
	if user in {"", "Guest", "Administrator"}:
		frappe.throw("Medical-record review governance requires a named user.", frappe.PermissionError)
	if "IONE Agent Service" in set(frappe.get_roles(user)):
		frappe.throw("Agent service users cannot govern medical-record review.", frappe.PermissionError)
	if not frappe.db.get_value("User", user, "enabled"):
		frappe.throw("Medical-record review governance user is disabled.", frappe.PermissionError)
	return user


def _named_role_user(allowed_roles: frozenset[str]) -> str:
	user = _named_user()
	if not set(frappe.get_roles(user)).intersection(allowed_roles):
		frappe.throw("User has no eligible medical-record review role.", frappe.PermissionError)
	return user


def _bounded_comment(value: Any, *, minimum: int) -> str:
	comment = str(value or "").strip()
	if len(comment) < minimum or len(comment) > MAX_COMMENT_LENGTH:
		frappe.throw(f"Review comment must contain {minimum}-{MAX_COMMENT_LENGTH} characters.")
	if any(ord(character) < 32 and character not in "\n\r\t" for character in comment):
		frappe.throw("Review comment contains forbidden control characters.")
	return comment


def _safe_code(value: Any, *, label: str) -> str:
	code = str(value or "").strip()
	if not _SAFE_CODE_RE.fullmatch(code):
		frappe.throw(f"{label} is invalid.")
	return code


def _validate_hash_manifest(value: Any, *, label: str) -> None:
	raw = str(value or "")
	try:
		decoded = json.loads(raw)
	except ValueError as exc:
		raise frappe.ValidationError(f"{label} must be canonical JSON.") from exc
	if (
		not isinstance(decoded, list)
		or any(not isinstance(item, str) or not _is_sha256(item) for item in decoded)
		or len(set(decoded)) != len(decoded)
		or decoded != sorted(decoded)
		or _canonical_json(decoded) != raw
	):
		frappe.throw(f"{label} must contain one sorted canonical list of unique SHA-256 hashes.")


def _decoded_hash_manifest(value: Any) -> list[str]:
	decoded = json.loads(str(value or ""))
	if not isinstance(decoded, list):
		raise ValueError("Hash manifest must be a list.")
	return decoded


def _validate_execution_manifest(value: Any, *, expected_hash: str) -> None:
	raw = str(value or "")
	try:
		decoded = json.loads(raw, object_pairs_hook=_unique_json_object)
	except ValueError as exc:
		raise frappe.ValidationError("Mandatory execution manifest must be canonical JSON.") from exc
	required = {
		"completed_at",
		"context_hash",
		"execution",
		"result",
		"rule",
		"rule_checksum",
		"rule_version",
	}
	if not isinstance(decoded, list) or len(decoded) > 100 * (MAX_MANDATORY_EXECUTION_RECEIPTS_PER_RULE + 1):
		frappe.throw("Mandatory execution manifest must be one bounded list.")
	for item in decoded:
		if (
			not isinstance(item, dict)
			or set(item) != required
			or any(not isinstance(item.get(fieldname), str) for fieldname in required)
			or not _is_sha256(str(item.get("rule_checksum") or ""))
			or not str(item.get("rule") or "")
			or len(str(item.get("rule") or "")) > 140
			or str(item.get("rule") or "") != str(item.get("rule") or "").strip()
			or not str(item.get("rule_version") or "")
			or len(str(item.get("rule_version") or "")) > 140
			or str(item.get("rule_version") or "") != str(item.get("rule_version") or "").strip()
			or (item.get("context_hash") and not _is_sha256(str(item.get("context_hash") or "")))
			or str(item.get("result") or "")
			not in {"", "Passed", "Failed", "Excluded", "Insufficient Data", "Error"}
			or (
				bool(item.get("execution"))
				!= bool(item.get("completed_at") and item.get("context_hash") and item.get("result"))
			)
			or (
				not item.get("execution")
				and any(item.get(fieldname) for fieldname in ("completed_at", "context_hash", "result"))
			)
			or len(str(item.get("execution") or "")) > 140
			or str(item.get("execution") or "") != str(item.get("execution") or "").strip()
		):
			frappe.throw("Mandatory execution manifest contains an invalid receipt.")
		if item.get("completed_at"):
			_site_naive_datetime(
				item["completed_at"],
				label="Mandatory execution receipt completed_at",
			)
	receipt_keys = [
		(str(item["rule"]), str(item["rule_version"]), str(item["execution"])) for item in decoded
	]
	if len(receipt_keys) != len(set(receipt_keys)):
		frappe.throw("Mandatory execution manifest contains duplicate receipts.")
	if decoded != sorted(
		decoded,
		key=lambda item: (item["rule"], item["rule_version"], item["execution"]),
	):
		frappe.throw("Mandatory execution manifest must be sorted by exact rule and receipt identity.")
	if _canonical_json(decoded) != raw:
		frappe.throw("Mandatory execution manifest is not canonical.")
	if not _is_sha256(expected_hash) or not hmac.compare_digest(expected_hash, _sha256(raw)):
		frappe.throw("Mandatory execution manifest hash does not match.")


def _validate_governed_artifact(
	doc,
	*,
	create_flag: str,
	create_capability: object,
	immutable_fields: frozenset[str],
	runtime_fields: frozenset[str],
	runtime_flag: str,
	runtime_capability: object,
) -> None:
	previous = doc.get_doc_before_save()
	if previous is None:
		if getattr(frappe.flags, create_flag, None) is not create_capability:
			frappe.throw(
				f"{doc.doctype} may only be created by its governed service.", frappe.PermissionError
			)
		return
	changed_immutable = _changed_fields(doc, previous, immutable_fields)
	if changed_immutable:
		frappe.throw(f"{doc.doctype} frozen fields are immutable.")
	changed_runtime = _changed_fields(doc, previous, runtime_fields)
	if changed_runtime and getattr(frappe.flags, runtime_flag, None) is not runtime_capability:
		frappe.throw(f"{doc.doctype} runtime state is service-managed.")
	meta = getattr(doc, "meta", None)
	meta_fields = {
		str(field.fieldname)
		for field in (getattr(meta, "fields", None) or [])
		if getattr(field, "fieldname", None)
	}
	unexpected = _changed_fields(
		doc,
		previous,
		frozenset(meta_fields) - runtime_fields - immutable_fields,
	)
	if unexpected:
		frappe.throw(f"{doc.doctype} contains an ungoverned mutable field.")


def _require_creation_capability(
	doc,
	*,
	flag: str,
	capability: object,
	label: str,
) -> None:
	if not doc.is_new():
		frappe.throw(f"{label} are immutable.")
	if getattr(frappe.flags, flag, None) is not capability:
		frappe.throw(f"{label} may only be created by the governed service.", frappe.PermissionError)


def _changed_fields(doc, previous, fieldnames: frozenset[str]) -> set[str]:
	return {
		fieldname
		for fieldname in fieldnames
		if _comparable(doc.get(fieldname)) != _comparable(previous.get(fieldname))
	}


def _comparable(value: Any) -> str:
	if value in (None, ""):
		return ""
	return str(value)


def _as_mapping(value: Any) -> dict[str, Any]:
	if isinstance(value, Mapping):
		return dict(value)
	if hasattr(value, "as_dict"):
		return dict(value.as_dict())
	raise TypeError("Medical-record population row is not a mapping.")


def _canonical_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)


def _sha256(value: str) -> str:
	return hashlib.sha256(value.encode()).hexdigest()


def _sha256_bytes(value: bytes) -> str:
	return hashlib.sha256(value).hexdigest()


def _is_sha256(value: str) -> bool:
	return bool(_SHA256_RE.fullmatch(value))


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
	output: dict[str, Any] = {}
	for key, value in pairs:
		if key in output:
			raise ValueError("Duplicate JSON object key")
		output[key] = value
	return output


@contextmanager
def _medical_record_archive_lock(record_name: str) -> Iterator[Any]:
	"""Use one record-first lock order for batch, publication, override, and ACK."""
	from ione_qms.services.projections import medical_record_release_lock

	with medical_record_release_lock(record_name) as projection:
		yield projection


@contextmanager
def _policy_transition() -> Iterator[None]:
	with _creation_capability(
		"ione_medical_record_policy_transition",
		_POLICY_TRANSITION_CAPABILITY,
	):
		yield


@contextmanager
def _creation_capability(flag: str, capability: object) -> Iterator[None]:
	previous = getattr(frappe.flags, flag, None)
	setattr(frappe.flags, flag, capability)
	try:
		yield
	finally:
		setattr(frappe.flags, flag, previous)
