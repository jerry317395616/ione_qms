from __future__ import annotations

import ast
import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECEIPTS = ROOT / "ione_qms" / "services" / "rule_receipts.py"
VALIDATION = ROOT / "ione_qms" / "rule_engine" / "validation.py"


def _load_receipt_module():
	fake_frappe = types.ModuleType("frappe")
	fake_frappe.ValidationError = type("ValidationError", (Exception,), {})
	previous = sys.modules.get("frappe")
	sys.modules["frappe"] = fake_frappe
	try:
		spec = importlib.util.spec_from_file_location("ione_rule_receipt_contract", RECEIPTS)
		assert spec and spec.loader
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		return module
	finally:
		if previous is None:
			sys.modules.pop("frappe", None)
		else:
			sys.modules["frappe"] = previous


def _function(source: str, name: str) -> str:
	node = next(
		item for item in ast.parse(source).body if isinstance(item, ast.FunctionDef) and item.name == name
	)
	return ast.get_source_segment(source, node) or ""


class TestRuleReceiptAttackRegressions(unittest.TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		cls.receipts = _load_receipt_module()

	def test_every_signed_business_and_chain_field_rejects_tampering(self) -> None:
		key_material = "rule-receipt-test-key-material-at-least-32-bytes"
		for doctype, signed_fields in self.receipts._SIGNED_FIELDS.items():
			values = {fieldname: f"value:{fieldname}" for fieldname in signed_fields}
			values["receipt_chain_sequence"] = 2
			values["previous_receipt_hmac"] = "a" * 64
			baseline = self.receipts.compute_receipt_hmac(doctype, values, key_material)
			for fieldname in signed_fields:
				tampered = dict(values)
				tampered[fieldname] = (
					int(values[fieldname]) + 1
					if isinstance(values[fieldname], int)
					else f"tampered:{values[fieldname]}"
				)
				with self.subTest(doctype=doctype, fieldname=fieldname):
					self.assertNotEqual(
						baseline,
						self.receipts.compute_receipt_hmac(doctype, tampered, key_material),
					)

	def test_source_snapshot_outcome_and_predecessor_are_cryptographically_bound(self) -> None:
		signed = self.receipts._SIGNED_FIELDS
		self.assertIn("outcome_manifest_json", signed["IONE QC Rule Validation Run"])
		self.assertIn("executor_version", signed["IONE QC Rule Validation Run"])
		self.assertIn("context_hash", signed["IONE QC Rule Shadow Execution"])
		self.assertIn("evaluation_hash", signed["IONE QC Rule Shadow Execution"])
		for fields in signed.values():
			self.assertIn("previous_receipt", fields)
			self.assertIn("previous_receipt_hmac", fields)
			self.assertIn("signature_version", fields)
			self.assertIn("hmac_key_id", fields)

	def test_runtime_verifier_rejects_hmac_tamper_gap_break_population_and_wrong_head(self) -> None:
		doctype = "IONE QC Rule Shadow Execution"
		rule_version = "RULE-VERSION-ATTACK-TEST"
		rule_checksum = "b" * 64
		key_id = "test-rule-receipt-v1"
		key_material = "rule-receipt-test-key-material-at-least-32-bytes"
		scope = self.receipts.receipt_chain_scope(doctype, rule_version, rule_checksum)

		def row(sequence: int, previous=None) -> dict:
			values = {fieldname: f"value:{fieldname}" for fieldname in self.receipts._SIGNED_FIELDS[doctype]}
			values.update(
				{
					"name": f"RECEIPT-{sequence}",
					"rule_version": rule_version,
					"rule_checksum": rule_checksum,
					"receipt_chain_scope": scope,
					"receipt_chain_sequence": sequence,
					"receipt_chain_key": self.receipts._receipt_chain_key(scope, sequence),
					"previous_receipt": previous["name"] if previous else None,
					"previous_receipt_hmac": previous["receipt_hmac"] if previous else None,
					"signature_version": self.receipts.RULE_RECEIPT_SIGNATURE_VERSION,
					"hmac_key_id": key_id,
				}
			)
			values["receipt_hmac"] = self.receipts.compute_receipt_hmac(
				doctype,
				values,
				key_material,
			)
			return values

		first = row(1)
		second = row(2, first)
		rows = [first, second]

		def reject(message, *_args, **_kwargs):
			raise self.receipts.frappe.ValidationError(message)

		self.receipts.frappe.conf = {
			f"{self.receipts.RULE_RECEIPT_HMAC_CONFIG_PREFIX}_key_id": key_id,
			f"{self.receipts.RULE_RECEIPT_HMAC_CONFIG_PREFIX}_key": key_material,
		}
		self.receipts.frappe.throw = reject
		self.receipts.frappe.get_all = lambda *_args, **_kwargs: rows
		self.receipts.verify_receipt_chain(
			doctype,
			rule_version=rule_version,
			rule_checksum=rule_checksum,
			expected_names={"RECEIPT-1", "RECEIPT-2"},
			required_head="RECEIPT-2",
		)

		tampered = [dict(first), dict(second)]
		tampered[1]["result"] = "tampered"
		rows = tampered
		with self.assertRaisesRegex(self.receipts.frappe.ValidationError, "HMAC is invalid"):
			self.receipts.verify_receipt_chain(
				doctype,
				rule_version=rule_version,
				rule_checksum=rule_checksum,
			)

		gapped = [dict(first), row(3, second)]
		rows = gapped
		with self.assertRaisesRegex(self.receipts.frappe.ValidationError, "sequence gap"):
			self.receipts.verify_receipt_chain(
				doctype,
				rule_version=rule_version,
				rule_checksum=rule_checksum,
			)

		broken_second = dict(second)
		broken_second["previous_receipt"] = "ATTACKER-RECEIPT"
		broken_second["receipt_hmac"] = self.receipts.compute_receipt_hmac(
			doctype,
			broken_second,
			key_material,
		)
		rows = [first, broken_second]
		with self.assertRaisesRegex(self.receipts.frappe.ValidationError, "predecessor chain is broken"):
			self.receipts.verify_receipt_chain(
				doctype,
				rule_version=rule_version,
				rule_checksum=rule_checksum,
			)

		rows = [first, second]
		with self.assertRaisesRegex(self.receipts.frappe.ValidationError, "governed receipt population"):
			self.receipts.verify_receipt_chain(
				doctype,
				rule_version=rule_version,
				rule_checksum=rule_checksum,
				expected_names={"RECEIPT-1"},
			)
		with self.assertRaisesRegex(self.receipts.frappe.ValidationError, "current signed chain head"):
			self.receipts.verify_receipt_chain(
				doctype,
				rule_version=rule_version,
				rule_checksum=rule_checksum,
				required_head="RECEIPT-1",
			)

	def test_chain_gap_break_population_mismatch_and_wrong_head_fail_closed(self) -> None:
		source = RECEIPTS.read_text(encoding="utf-8")
		verifier = _function(source, "verify_receipt_chain")
		for required in (
			"receipt chain contains a sequence gap",
			"receipt predecessor chain is broken",
			"actual_names !=",
			"selected receipt is not the current signed chain head",
			"verify_signed_receipt",
			"hmac.compare_digest",
		):
			self.assertIn(required, verifier)

	def test_receipts_use_a_dedicated_versioned_keyring_without_generic_key_fallback(self) -> None:
		source = RECEIPTS.read_text(encoding="utf-8")
		self.assertIn("IONE-RULE-RECEIPT-HMAC-SHA256-V1", source)
		self.assertIn("ione_rule_receipt_hmac", source)
		self.assertIn("active_hmac_key", source)
		self.assertIn("verification_hmac_key", source)
		self.assertNotIn('frappe.conf.get("encryption_key")', source)
		self.assertIn("at least 32 bytes", source)

	def test_replay_and_shadow_recompute_current_semantics_instead_of_trusting_hashes(self) -> None:
		source = VALIDATION.read_text(encoding="utf-8")
		replay = _function(source, "_verify_validation_run_receipt")
		shadow = _function(source, "_verify_shadow_execution_receipt")
		for required in (
			"_validation_events",
			"source_semantic_hash",
			"_build_context",
			"_execute_shadow_rule_version",
			"_rule_executor_version",
		):
			self.assertIn(required, replay)
		for required in (
			"frappe.get_doc",
			"_build_context",
			"context_hash",
			"_execute_shadow_rule_version",
			"evaluation_hash",
		):
			self.assertIn(required, shadow)

	def test_false_negative_gate_contains_no_sampling_or_unreviewed_non_hit_escape(self) -> None:
		source = VALIDATION.read_text(encoding="utf-8")
		gate = _function(source, "require_online_shadow_observation")
		self.assertNotIn("_deterministic_feedback_sample", source)
		self.assertIn("RuleResult.PASSED.value", gate)
		self.assertIn("RuleResult.EXCLUDED.value", gate)
		self.assertIn("required_names.difference(feedback_by_execution)", gate)
		self.assertIn("reviewed_count != event_count", gate)
		self.assertIn("including every non-hit", gate)


if __name__ == "__main__":
	unittest.main()
