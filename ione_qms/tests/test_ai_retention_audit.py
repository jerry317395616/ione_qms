from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from ione_qms.ai.audit import EVIDENCE_SNAPSHOT_REDACTED
from ione_qms.services import ai_approvals
from ione_qms.services.immutability import canonical_record_hash
from ione_qms.tasks import security


class TestAIAuditRetention(TestCase):
	def _access_receipt(self):
		snapshot_json = '{"sections":{"assessment":"clinical body"}}'
		doc = frappe._dict(
			doctype="IONE AI Data Access Log",
			name="ACCESS-1",
			task="TASK-1",
			policy="POLICY-1",
			result="Success",
			source_doctype="IONE Clinical Quality Event",
			source_name="EVENT-1",
			content_hash=hashlib.sha256(snapshot_json.encode()).hexdigest(),
			snapshot_json=snapshot_json,
			record_hash=None,
		)
		doc.meta = SimpleNamespace(
			fields=[
				SimpleNamespace(fieldname=fieldname)
				for fieldname in (
					"task",
					"policy",
					"result",
					"source_doctype",
					"source_name",
					"content_hash",
					"snapshot_json",
					"record_hash",
				)
			]
		)
		doc.record_hash = canonical_record_hash(doc)
		doc.add_comment = Mock()
		return doc

	def test_access_snapshot_is_hash_verified_before_redaction(self) -> None:
		doc = self._access_receipt()
		with (
			patch.object(security.frappe, "get_doc", return_value=doc),
			patch.object(security, "_task_is_final", return_value=True),
			patch.object(security.frappe.db, "set_value") as set_value,
		):
			self.assertTrue(security._redact_access_log_snapshot(doc.name))
		set_value.assert_called_once_with(
			"IONE AI Data Access Log",
			doc.name,
			"snapshot_json",
			EVIDENCE_SNAPSHOT_REDACTED,
			update_modified=False,
		)
		doc.add_comment.assert_called_once()

	def test_access_snapshot_record_hash_mismatch_fails_closed(self) -> None:
		doc = self._access_receipt()
		doc.record_hash = "f" * 64
		with (
			patch.object(security.frappe, "get_doc", return_value=doc),
			patch.object(security, "_task_is_final", return_value=True),
			patch.object(security.frappe.db, "set_value") as set_value,
			self.assertRaises(ValueError),
		):
			security._redact_access_log_snapshot(doc.name)
		set_value.assert_not_called()


class TestAIApprovalRetention(TestCase):
	def _approval_pair(self, *, status: str = "Consumed", prompt_hash: str | None = None):
		arguments_json = '{"finding":"FINDING-1"}'
		question_prompt = "Confirm governed finding creation?"
		values = {
			"doctype": "IONE AI Tool Approval",
			"name": "APPROVAL-1",
			"task": "TASK-1",
			"status": status,
			"arguments_hash": hashlib.sha256(arguments_json.encode()).hexdigest(),
			"arguments_json": arguments_json,
			"question_prompt_hash": (
				prompt_hash
				if prompt_hash is not None
				else hashlib.sha256(question_prompt.encode()).hexdigest()
			),
			"question_prompt": question_prompt,
		}
		previous = frappe._dict(values)
		current = frappe._dict(values)
		current.arguments_json = ai_approvals.APPROVAL_CONTENT_REDACTED
		current.question_prompt = ai_approvals.APPROVAL_CONTENT_REDACTED
		return current, previous

	def test_only_hash_verified_terminal_content_can_be_redacted(self) -> None:
		current, previous = self._approval_pair()
		with patch.object(
			ai_approvals.frappe.db,
			"get_value",
			return_value="Completed",
		):
			ai_approvals._validate_retention_transition(
				current,
				previous,
				{"arguments_json", "question_prompt"},
				set(),
			)

	def test_legacy_approval_without_prompt_hash_fails_closed(self) -> None:
		current, previous = self._approval_pair(prompt_hash="")
		with (
			patch.object(
				ai_approvals.frappe.db,
				"get_value",
				return_value="Completed",
			),
			self.assertRaises(frappe.ValidationError),
		):
			ai_approvals._validate_retention_transition(
				current,
				previous,
				{"arguments_json", "question_prompt"},
				set(),
			)

	def test_pending_or_resumable_approval_cannot_be_redacted(self) -> None:
		current, previous = self._approval_pair(status="Reviewed")
		with self.assertRaises(frappe.ValidationError):
			ai_approvals._validate_retention_transition(
				current,
				previous,
				{"arguments_json", "question_prompt"},
				set(),
			)

	def test_partial_preexisting_retention_marker_fails_closed(self) -> None:
		current, previous = self._approval_pair()
		previous.arguments_json = ai_approvals.APPROVAL_CONTENT_REDACTED
		with self.assertRaises(frappe.ValidationError):
			ai_approvals._validate_retention_transition(
				current,
				previous,
				{"question_prompt"},
				set(),
			)
