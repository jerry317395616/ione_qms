from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from ione_qms.ai import audit, evidence


class _Task(SimpleNamespace):
	def get(self, fieldname: str, default=None):
		return getattr(self, fieldname, default)


class TestAIEvidenceReceipts(TestCase):
	def setUp(self) -> None:
		self.task = _Task(
			name="TASK-1",
			policy="POLICY-1",
			hospital="HOSP-1",
			campus="CAMPUS-1",
			department="DEPT-1",
			ward=None,
			patient=None,
			encounter=None,
		)
		self.snapshot_json = json.dumps(
			{"indicator_value": 0.82, "numerator": 82, "denominator": 100},
			sort_keys=True,
			separators=(",", ":"),
		)
		self.receipt = frappe._dict(
			task="TASK-1",
			policy="POLICY-1",
			result="Success",
			hospital="HOSP-1",
			campus="CAMPUS-1",
			department="DEPT-1",
			ward=None,
			patient=None,
			encounter=None,
			source_doctype="IONE Indicator Result",
			source_name="RESULT-1",
			source_version="2026-07-29 12:00:00",
			source_record_hash="a" * 64,
			content_hash=hashlib.sha256(self.snapshot_json.encode()).hexdigest(),
			snapshot_json=self.snapshot_json,
			record_hash="b" * 64,
		)

	def test_only_same_task_immutable_receipt_is_accepted(self) -> None:
		with patch.object(evidence.frappe.db, "get_value", return_value=self.receipt):
			(result,) = evidence.validate_evidence_references(
				self.task,
				["IONE AI Data Access Log:RECEIPT-1"],
				require_clinical=True,
			)
		self.assertEqual(result.source_name, "RESULT-1")
		self.assertEqual(result.content_hash, self.receipt.content_hash)

	def test_direct_record_pointer_is_rejected(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			evidence.validate_evidence_references(
				self.task,
				["IONE Indicator Result:RESULT-1"],
			)

	def test_receipt_from_another_task_is_rejected(self) -> None:
		receipt = frappe._dict(self.receipt)
		receipt.task = "TASK-2"
		with (
			patch.object(evidence.frappe.db, "get_value", return_value=receipt),
			self.assertRaises(frappe.PermissionError),
		):
			evidence.validate_evidence_references(
				self.task,
				["IONE AI Data Access Log:RECEIPT-1"],
			)

	def test_tampered_snapshot_is_rejected(self) -> None:
		receipt = frappe._dict(self.receipt)
		receipt.snapshot_json = '{"indicator_value":0.99}'
		with (
			patch.object(evidence.frappe.db, "get_value", return_value=receipt),
			self.assertRaises(frappe.ValidationError),
		):
			evidence.validate_evidence_references(
				self.task,
				["IONE AI Data Access Log:RECEIPT-1"],
			)

	def test_retention_redacted_snapshot_keeps_task_bound_receipt_valid(self) -> None:
		receipt = frappe._dict(self.receipt)
		receipt.snapshot_json = audit.EVIDENCE_SNAPSHOT_REDACTED
		with patch.object(evidence.frappe.db, "get_value", return_value=receipt):
			(result,) = evidence.validate_evidence_references(
				self.task,
				["IONE AI Data Access Log:RECEIPT-1"],
				require_clinical=True,
			)
		self.assertEqual(result.receipt_hash, self.receipt.record_hash)
		self.assertEqual(result.content_hash, self.receipt.content_hash)

	def test_retention_marker_does_not_bypass_malformed_integrity_hashes(self) -> None:
		receipt = frappe._dict(self.receipt)
		receipt.snapshot_json = audit.EVIDENCE_SNAPSHOT_REDACTED
		receipt.content_hash = "not-a-sha256"
		with (
			patch.object(evidence.frappe.db, "get_value", return_value=receipt),
			self.assertRaises(frappe.ValidationError),
		):
			evidence.validate_evidence_references(
				self.task,
				["IONE AI Data Access Log:RECEIPT-1"],
			)

	def test_candidate_requires_a_clinical_receipt(self) -> None:
		receipt = frappe._dict(self.receipt)
		receipt.source_doctype = "IONE QC Standard Clause"
		receipt.source_name = "CLAUSE-1"
		with (
			patch.object(evidence.frappe.db, "get_value", return_value=receipt),
			self.assertRaises(frappe.ValidationError),
		):
			evidence.validate_evidence_references(
				self.task,
				["IONE AI Data Access Log:RECEIPT-1"],
				require_clinical=True,
			)
