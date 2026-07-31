from __future__ import annotations

import hashlib
import uuid

import frappe
from frappe.tests import IntegrationTestCase

from ione_qms.services import batch_reliability as batches


class TestDurableBatchReliability(IntegrationTestCase):
	def setUp(self) -> None:
		super().setUp()
		frappe.set_user("Administrator")
		batches.ensure_source_epoch_rows()
		self.subject = f"test-{uuid.uuid4().hex}"

	def tearDown(self) -> None:
		frappe.set_user("Administrator")
		super().tearDown()

	def test_run_and_work_identity_are_idempotent_and_payload_bound(self) -> None:
		run = batches.ensure_run(
			batch_type="Analytics",
			subject_key=self.subject,
			period_start="2026-07-01",
			period_end="2026-07-31",
			task_path="ione_qms.tasks.analytics.process_analytics_batch_run",
			task_args={},
			source_doctype="IONE QC Finding",
			source_index=1,
		)
		duplicate = batches.ensure_run(
			batch_type="Analytics",
			subject_key=self.subject,
			period_start="2026-07-01",
			period_end="2026-07-31",
			task_path="ione_qms.tasks.analytics.process_analytics_batch_run",
			task_args={},
			source_doctype="IONE QC Finding",
			source_index=1,
		)
		self.assertEqual(run.name, duplicate.name)
		self.assertEqual(len(run.name), 64)

		item = batches.ensure_work_item(
			run.name,
			payload={"fact_key": "a" * 64},
			sequence=1,
		)
		duplicate_item = batches.ensure_work_item(
			run.name,
			payload={"fact_key": "a" * 64},
			sequence=99,
		)
		self.assertEqual(item.name, duplicate_item.name)
		self.assertEqual(len(item.name), 64)

		item.payload_json = '{"fact_key":"tampered"}'
		with self.assertRaisesRegex(ValueError, "payload hash verification failed"):
			item.save(ignore_permissions=True)

	def test_indicator_completion_advances_analytics_visibility_epoch(self) -> None:
		run = batches.ensure_run(
			batch_type="Indicator",
			subject_key=self.subject,
			period_start="2026-07-01",
			period_end="2026-07-31",
			task_path="ione_qms.tasks.indicators.process_indicator_batch_run",
			task_args={},
		)
		activated = batches.activate_run(run.name)
		self.assertIsNotNone(activated)
		_activated_run, lease_token = activated
		before = batches.source_epoch("IONE Indicator Result")

		batches.complete_run(run.name, lease_token)

		completed = frappe.get_doc(batches.RUN_DOCTYPE, run.name)
		self.assertEqual(completed.status, "Completed")
		self.assertEqual(completed.phase, "Complete")
		self.assertEqual(
			batches.source_epoch("IONE Indicator Result"),
			before + 1,
		)
		self.assertEqual(completed.work_receipt_count, 0)
		self.assertEqual(completed.work_receipt_hash, batches.EMPTY_MANIFEST_HASH)

	def test_recovery_receipt_is_hash_bound_and_append_only(self) -> None:
		requested_at = frappe.utils.now_datetime()
		reason = "Approved incident recovery after worker investigation."
		reason_hash = hashlib.sha256(reason.encode("utf-8")).hexdigest()
		values = {
			"batch_run": "b" * 64,
			"recovery_sequence": 1,
			"reason_hash": reason_hash,
			"requested_at": requested_at,
			"requested_by": "Administrator",
			"version": "ione-batch-recovery-receipt-v1",
		}
		receipt = frappe.get_doc(
			{
				"doctype": batches.RECOVERY_DOCTYPE,
				"receipt_key": batches.digest(values),
				"batch_run": values["batch_run"],
				"recovery_sequence": 1,
				"prior_status": "Dead Letter",
				"reason": reason,
				"reason_hash": reason_hash,
				"requested_by": "Administrator",
				"requested_at": requested_at,
			}
		)
		# Link validation deliberately fails before this synthetic receipt could
		# be inserted without a real run; the controller integrity check is
		# exercised directly and remains independent of generic permissions.
		batches.validate_batch_recovery_receipt(receipt)
		receipt.reason = f"{reason} tampered"
		with self.assertRaisesRegex(ValueError, "reason hash verification failed"):
			batches.validate_batch_recovery_receipt(receipt)
