from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from ione_qms.integration.projections import materialize_event_projection
from ione_qms.services.projections import (
	_PROJECTIONS,
	_projection_key,
	materialize_data_reconciliation,
	validate_projection_identity,
)


class _ProjectionDoc:
	def __init__(self, *, name: str | None, insert_error: Exception | None = None) -> None:
		self.doctype = "IONE Data Reconciliation"
		self.name = name
		self.flags = SimpleNamespace()
		self.values: dict = {}
		self.insert_error = insert_error
		self.insert_calls = 0
		self.save_calls = 0

	def update(self, values: dict) -> None:
		self.values.update(values)

	def is_new(self) -> bool:
		return self.name is None

	def insert(self) -> None:
		self.insert_calls += 1
		if self.insert_error:
			raise self.insert_error
		self.name = "REC-NEW"

	def save(self) -> None:
		self.save_calls += 1


class TestProjectionMaterializers(TestCase):
	def test_canonical_surgery_key_is_stable_across_source_versions(self) -> None:
		spec = _PROJECTIONS["IONE Surgery QC"]
		first = {
			"_source_identity": {
				"event": "EVT-1",
				"source_system": "HIS",
				"source_record_id": "SURG-1",
				"source_version": "1",
			},
			"surgery_no": "SURG-1",
			"surgery_time": "2026-07-30 08:00:00",
			"hospital": "HOSP-1",
			"encounter": "ENC-1",
		}
		second = {
			**first,
			"surgery_time": datetime(2026, 7, 30, 8),
		}
		self.assertEqual(_projection_key(spec, first), _projection_key(spec, second))
		second["_source_identity"] = {**first["_source_identity"], "source_version": "2"}
		self.assertEqual(_projection_key(spec, first), _projection_key(spec, second))
		second["_source_identity"] = {
			**first["_source_identity"],
			"source_record_id": "SURG-2",
		}
		self.assertNotEqual(_projection_key(spec, first), _projection_key(spec, second))

	def test_direct_projection_creation_is_rejected(self) -> None:
		doc = SimpleNamespace(
			doctype="IONE Data Reconciliation",
			flags=SimpleNamespace(),
			is_new=lambda: True,
		)
		with (
			patch(
				"ione_qms.services.projections.frappe.throw",
				side_effect=RuntimeError("materializer required"),
			),
			self.assertRaisesRegex(RuntimeError, "materializer required"),
		):
			validate_projection_identity(doc)

	def test_truthy_projection_flag_is_not_the_materializer_capability(self) -> None:
		doc = SimpleNamespace(
			doctype="IONE Data Reconciliation",
			flags=SimpleNamespace(ione_projection_materializer=True),
			is_new=lambda: False,
			get_doc_before_save=lambda: SimpleNamespace(get=lambda _field: None),
		)
		with (
			patch(
				"ione_qms.services.projections.frappe.throw",
				side_effect=RuntimeError("controlled projection service"),
			),
			self.assertRaisesRegex(RuntimeError, "controlled projection service"),
		):
			validate_projection_identity(doc)

	def test_archive_application_rechecks_gate_before_local_projection_save(self) -> None:
		from ione_qms.services.projections import apply_medical_record_archive_projection

		doc = MagicMock()
		doc.name = "MRQC-1"
		doc.get.side_effect = lambda field: {"record_status": "Final"}.get(field)
		doc.flags = SimpleNamespace()
		with (
			patch(
				"ione_qms.services.projections.frappe.db.advisory_lock",
				return_value=nullcontext(),
			),
			patch("ione_qms.services.projections.frappe.get_doc", return_value=doc),
			patch("ione_qms.services.medical_record_review.assert_medical_record_archive_ready") as ready,
		):
			result = apply_medical_record_archive_projection(doc.name)
		self.assertEqual(result, doc.name)
		ready.assert_called_once_with(doc)
		self.assertEqual(doc.record_status, "Archived")
		doc.save.assert_called_once()

	def test_reconciliation_materializer_updates_existing_key_idempotently(self) -> None:
		doc = _ProjectionDoc(name="REC-EXISTING")
		meta = SimpleNamespace(
			fields=[
				SimpleNamespace(fieldname=fieldname)
				for fieldname in (
					"reconciliation_key",
					"source_system",
					"endpoint",
					"period_start",
					"period_end",
					"status",
				)
			]
		)
		with (
			patch("ione_qms.services.projections.frappe.get_meta", return_value=meta),
			patch(
				"ione_qms.services.projections.frappe.db.advisory_lock",
				return_value=nullcontext(),
			),
			patch(
				"ione_qms.services.projections.frappe.db.get_value",
				return_value="REC-EXISTING",
			),
			patch("ione_qms.services.projections.frappe.get_doc", return_value=doc),
		):
			name = materialize_data_reconciliation(
				{
					"source_system": "HIS",
					"period_start": "2026-07-01 00:00:00",
					"period_end": "2026-07-31 23:59:59",
					"status": "Matched",
				}
			)
		self.assertEqual(name, "REC-EXISTING")
		self.assertEqual(doc.save_calls, 1)
		self.assertEqual(doc.insert_calls, 0)
		self.assertEqual(len(doc.values["reconciliation_key"]), 64)
		self.assertTrue(doc.flags.ione_projection_materializer)
		self.assertTrue(doc.flags.ignore_permissions)

	def test_unique_key_race_reloads_and_updates_the_winner(self) -> None:
		from ione_qms.services.projections import frappe

		new_doc = _ProjectionDoc(name=None, insert_error=frappe.DuplicateEntryError())
		existing_doc = _ProjectionDoc(name="REC-WINNER")
		meta = SimpleNamespace(
			fields=[
				SimpleNamespace(fieldname=fieldname)
				for fieldname in (
					"reconciliation_key",
					"source_system",
					"period_start",
					"period_end",
				)
			]
		)
		get_doc = MagicMock(side_effect=[new_doc, existing_doc])
		with (
			patch("ione_qms.services.projections.frappe.get_meta", return_value=meta),
			patch(
				"ione_qms.services.projections.frappe.db.advisory_lock",
				return_value=nullcontext(),
			),
			patch(
				"ione_qms.services.projections.frappe.db.get_value",
				side_effect=[None, "REC-WINNER"],
			),
			patch("ione_qms.services.projections.frappe.get_doc", get_doc),
		):
			name = materialize_data_reconciliation(
				{
					"source_system": "HIS",
					"period_start": "2026-07-01",
					"period_end": "2026-07-31",
				}
			)
		self.assertEqual(name, "REC-WINNER")
		self.assertEqual(new_doc.insert_calls, 1)
		self.assertEqual(existing_doc.save_calls, 1)


class TestClinicalEventProjectionDispatch(TestCase):
	@patch(
		"ione_qms.integration.projections.materialize_medical_record_qc",
		return_value="MRQ-1",
	)
	def test_medical_record_namespace_materializes_reviewed_fields(self, materialize) -> None:
		name = materialize_event_projection(
			"EVT-1",
			"MedicalRecord.Updated",
			{
				"record_type": "Admission Note",
				"record_status": "Active",
				"completeness_score": 95,
				"qc_status": "Needs Review",
			},
		)
		self.assertEqual(name, "MRQ-1")
		values = materialize.call_args.args[0]
		self.assertEqual(values["event"], "EVT-1")
		self.assertEqual(values["record_type"], "Admission Note")
		self.assertEqual(values["completeness_score"], 95.0)

	def test_unknown_event_stays_in_event_stream_without_projection(self) -> None:
		self.assertIsNone(
			materialize_event_projection(
				"EVT-2",
				"Medication.Administered",
				{"status": "Completed"},
			)
		)

	def test_explicit_unknown_projection_is_rejected(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			materialize_event_projection(
				"EVT-3",
				"Custom.Event",
				{"projection_type": "Arbitrary Table"},
			)

	def test_surgery_projection_requires_stable_number_and_time(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			materialize_event_projection(
				"EVT-4",
				"Surgery.Scheduled",
				{"surgery_no": "SURG-1"},
			)
