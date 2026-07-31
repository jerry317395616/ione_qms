from __future__ import annotations

from typing import Any

import frappe

from ione_qms.services.quality_meetings import (
	cancel_quality_action_item as cancel_quality_action_item_service,
)
from ione_qms.services.quality_meetings import (
	cancel_quality_meeting as cancel_quality_meeting_service,
)
from ione_qms.services.quality_meetings import (
	close_quality_meeting as close_quality_meeting_service,
)
from ione_qms.services.quality_meetings import (
	create_quality_action_item as create_quality_action_item_service,
)
from ione_qms.services.quality_meetings import (
	mark_meeting_minutes_pending as mark_meeting_minutes_pending_service,
)
from ione_qms.services.quality_meetings import (
	quality_action_workbench as quality_action_workbench_service,
)
from ione_qms.services.quality_meetings import (
	record_quality_meeting_held as record_quality_meeting_held_service,
)
from ione_qms.services.quality_meetings import (
	retire_quality_experience_share as retire_quality_experience_share_service,
)
from ione_qms.services.quality_meetings import (
	review_meeting_minute as review_meeting_minute_service,
)
from ione_qms.services.quality_meetings import (
	review_quality_action_item as review_quality_action_item_service,
)
from ione_qms.services.quality_meetings import (
	review_quality_experience_share as review_quality_experience_share_service,
)
from ione_qms.services.quality_meetings import (
	review_quality_meeting as review_quality_meeting_service,
)
from ione_qms.services.quality_meetings import (
	start_quality_action_item as start_quality_action_item_service,
)
from ione_qms.services.quality_meetings import (
	submit_meeting_minute as submit_meeting_minute_service,
)
from ione_qms.services.quality_meetings import (
	submit_quality_action_verification as submit_quality_action_verification_service,
)
from ione_qms.services.quality_meetings import (
	submit_quality_experience_share as submit_quality_experience_share_service,
)
from ione_qms.services.quality_meetings import (
	submit_quality_meeting as submit_quality_meeting_service,
)


@frappe.whitelist(methods=["POST"])
def submit_quality_meeting(meeting: str) -> dict[str, str]:
	return submit_quality_meeting_service(meeting)


@frappe.whitelist(methods=["POST"])
def review_quality_meeting(
	meeting: str,
	decision: str,
	review_comment: str,
) -> dict[str, str]:
	return review_quality_meeting_service(meeting, decision, review_comment)


@frappe.whitelist(methods=["POST"])
def cancel_quality_meeting(meeting: str, reason: str) -> dict[str, str]:
	return cancel_quality_meeting_service(meeting, reason)


@frappe.whitelist(methods=["POST"])
def record_quality_meeting_held(
	meeting: str,
	actual_start: str,
	actual_end: str,
	attendance: list[dict[str, Any]] | str,
	evidence_reference: str,
) -> dict[str, str]:
	return record_quality_meeting_held_service(
		meeting,
		actual_start,
		actual_end,
		attendance,
		evidence_reference,
	)


@frappe.whitelist(methods=["POST"])
def mark_meeting_minutes_pending(meeting: str) -> dict[str, str]:
	return mark_meeting_minutes_pending_service(meeting)


@frappe.whitelist(methods=["POST"])
def submit_meeting_minute(minute: str) -> dict[str, str]:
	return submit_meeting_minute_service(minute)


@frappe.whitelist(methods=["POST"])
def review_meeting_minute(
	minute: str,
	decision: str,
	review_comment: str,
) -> dict[str, Any]:
	return review_meeting_minute_service(minute, decision, review_comment)


@frappe.whitelist(methods=["POST"])
def close_quality_meeting(meeting: str) -> dict[str, str]:
	return close_quality_meeting_service(meeting)


@frappe.whitelist(methods=["POST"])
def create_quality_action_item(
	title: str,
	description: str,
	assigned_to: str,
	verifier: str,
	due_date: str,
	meeting: str | None = None,
	meeting_decision: str | None = None,
	finding: str | None = None,
	pdca_project: str | None = None,
	safety_event: str | None = None,
) -> dict[str, str]:
	return create_quality_action_item_service(
		title,
		description,
		assigned_to,
		verifier,
		due_date,
		meeting=meeting,
		meeting_decision=meeting_decision,
		finding=finding,
		pdca_project=pdca_project,
		safety_event=safety_event,
	)


@frappe.whitelist(methods=["POST"])
def start_quality_action_item(action_item: str) -> dict[str, str]:
	return start_quality_action_item_service(action_item)


@frappe.whitelist(methods=["POST"])
def submit_quality_action_verification(
	action_item: str,
	completion_evidence: str,
	effect_result: str,
	request_key: str,
) -> dict[str, str]:
	return submit_quality_action_verification_service(
		action_item,
		completion_evidence,
		effect_result,
		request_key,
	)


@frappe.whitelist(methods=["POST"])
def review_quality_action_item(
	action_item: str,
	decision: str,
	verification_comment: str,
	request_key: str,
) -> dict[str, str]:
	return review_quality_action_item_service(
		action_item,
		decision,
		verification_comment,
		request_key,
	)


@frappe.whitelist(methods=["POST"])
def cancel_quality_action_item(
	action_item: str,
	reason: str,
	request_key: str,
) -> dict[str, str]:
	return cancel_quality_action_item_service(action_item, reason, request_key)


@frappe.whitelist(methods=["POST"])
def quality_action_workbench(
	status: str | None = None,
	role: str | None = None,
	limit: int = 100,
) -> dict[str, Any]:
	return quality_action_workbench_service(status=status, role=role, limit=limit)


@frappe.whitelist(methods=["POST"])
def submit_quality_experience_share(experience: str) -> dict[str, str]:
	return submit_quality_experience_share_service(experience)


@frappe.whitelist(methods=["POST"])
def review_quality_experience_share(
	experience: str,
	decision: str,
	review_comment: str,
) -> dict[str, str]:
	return review_quality_experience_share_service(experience, decision, review_comment)


@frappe.whitelist(methods=["POST"])
def retire_quality_experience_share(experience: str, reason: str) -> dict[str, str]:
	return retire_quality_experience_share_service(experience, reason)
