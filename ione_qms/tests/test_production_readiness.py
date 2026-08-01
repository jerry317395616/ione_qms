from __future__ import annotations

from datetime import datetime
from unittest import TestCase
from unittest.mock import patch

import frappe

from ione_qms.services import production_readiness, runtime_settings


class _Document(dict):
	def __init__(self, doctype: str, values: dict, previous: _Document | None = None) -> None:
		super().__init__(values)
		self.doctype = doctype
		self._previous = previous

	def get_doc_before_save(self):
		return self._previous

	def __getattr__(self, name: str):
		try:
			return self[name]
		except KeyError as exc:
			raise AttributeError(name) from exc

	def __setattr__(self, name: str, value) -> None:
		if name in {"doctype", "_previous"}:
			object.__setattr__(self, name, value)
		else:
			self[name] = value


class TestProductionReadinessUnit(TestCase):
	def test_domain_separated_hmac_signatures_detect_governance_and_event_tamper(self) -> None:
		secret = b"production-gate-test-key-32-bytes-minimum"
		assessment = _Document(
			"IONE Production Readiness Assessment",
			{
				"name": "ASSESSMENT-1",
				"assessment_checksum": "a" * 64,
				"hmac_key_id": "key-1",
				"status": "Approved",
				"reviewed_by": "auditor@example.test",
				"reviewed_at": "2026-08-01 02:00:00",
				"review_comment": "All evidence independently verified.",
				"governance_hmac_key_id": "key-1",
			},
		)
		assessment.name = "ASSESSMENT-1"
		governance_signature = production_readiness._governance_signature(assessment, secret)
		assessment["reviewed_by"] = "forged@example.test"
		self.assertNotEqual(
			governance_signature,
			production_readiness._governance_signature(assessment, secret),
		)
		event = _Document(
			"IONE Production Activation Event",
			{
				"event_key": "b" * 64,
				"action": "Activated",
				"assessment": "ASSESSMENT-1",
				"assessment_checksum": "a" * 64,
				"release_record": "REL-1",
				"release_record_hash": "c" * 64,
				"schema_revision": "schema",
				"full_fingerprint": "d" * 64,
				"migration_completion_receipt": "MIG-1",
				"runtime_commit_manifest_hash": "e" * 64,
				"actor": "operator@example.test",
				"event_at": "2026-08-01 03:00:00",
				"reason": "Approved production change window.",
				"production_mode": 1,
				"realtime_rules_enabled": 0,
				"ai_enabled": 0,
				"previous_event": "",
				"hmac_key_id": "key-1",
			},
		)
		event_signature = production_readiness._event_signature(event, secret)
		event["ai_enabled"] = 1
		self.assertNotEqual(event_signature, production_readiness._event_signature(event, secret))

	def test_assessment_checksum_is_order_independent_and_detects_evidence_tamper(self) -> None:
		base = {
			"release_record": "REL-1",
			"site_name": "manager.test",
			"schema_revision": "schema",
			"full_fingerprint": "a" * 64,
			"migration_completion_receipt": "MIG-1",
			"runtime_commit_manifest_json": '{"frappe":"' + "b" * 40 + '"}',
			"valid_until": "2026-08-30 00:00:00",
			"evidence_manifest_file": "/private/files/evidence.json",
			"evidence_manifest_sha256": "c" * 64,
			"requested_by": "assessor@example.test",
			"requested_at": "2026-08-01 00:00:00",
			"submitted_by": "assessor@example.test",
			"submitted_at": "2026-08-01 01:00:00",
		}
		rows = [
			{
				"gate_code": "release_integrity",
				"evidence_reference": "REL-EVIDENCE",
				"evidence_sha256": "d" * 64,
				"signed_by": "Release Owner",
				"signed_at": "2026-08-01 00:30:00",
				"expires_at": "",
			},
			{
				"gate_code": "security_privacy",
				"evidence_reference": "SEC-EVIDENCE",
				"evidence_sha256": "e" * 64,
				"signed_by": "Security Owner",
				"signed_at": "2026-08-01 00:40:00",
				"expires_at": "",
			},
		]
		first = _Document("IONE Production Readiness Assessment", {**base, "gate_evidence": rows})
		second = _Document(
			"IONE Production Readiness Assessment",
			{**base, "gate_evidence": list(reversed(rows))},
		)
		self.assertEqual(
			production_readiness._assessment_checksum(first),
			production_readiness._assessment_checksum(second),
		)
		second["gate_evidence"][0] = {**second["gate_evidence"][0], "evidence_sha256": "f" * 64}
		self.assertNotEqual(
			production_readiness._assessment_checksum(first),
			production_readiness._assessment_checksum(second),
		)

	def test_manifest_artifact_normalization_rejects_unknown_gate(self) -> None:
		artifact = {
			"id": "SEC-1",
			"sha256": "a" * 64,
			"gate_codes": ["not_a_gate"],
			"owner": "Named Security Owner",
			"signed_at": "2026-08-01 00:00:00",
		}
		with (
			patch.object(production_readiness, "now_datetime", return_value=datetime(2026, 8, 2)),
			patch.object(
				production_readiness.frappe,
				"throw",
				side_effect=frappe.ValidationError("invalid gate"),
			),
			self.assertRaisesRegex(frappe.ValidationError, "invalid gate"),
		):
			production_readiness._normalize_manifest_artifact(artifact)

	def test_direct_production_disable_clears_dependent_switches(self) -> None:
		previous = _Document(
			"IONE System Settings",
			{
				"production_mode": 1,
				"enable_realtime_rules": 1,
				"enable_ai": 1,
				"production_activation_event": "",
			},
		)
		current = _Document(
			"IONE System Settings",
			{
				"production_mode": 0,
				"enable_realtime_rules": 1,
				"enable_ai": 1,
				"production_activation_event": "",
			},
			previous,
		)
		runtime_settings.validate_runtime_enablement(current)
		self.assertEqual(current["enable_realtime_rules"], 0)
		self.assertEqual(current["enable_ai"], 0)

	def test_direct_production_enable_is_rejected_without_governed_capability(self) -> None:
		previous = _Document(
			"IONE System Settings",
			{
				"production_mode": 0,
				"enable_realtime_rules": 0,
				"enable_ai": 0,
				"production_activation_event": "",
			},
		)
		current = _Document(
			"IONE System Settings",
			{
				"production_mode": 1,
				"enable_realtime_rules": 0,
				"enable_ai": 0,
				"production_activation_event": "FORGED-EVENT",
			},
			previous,
		)
		with (
			patch.object(
				runtime_settings.frappe,
				"throw",
				side_effect=frappe.ValidationError("governed API required"),
			),
			self.assertRaisesRegex(frappe.ValidationError, "governed API required"),
		):
			runtime_settings.validate_runtime_enablement(current)
