from __future__ import annotations

import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import TestCase


class _ValidationError(Exception):
	pass


def _module(name: str, **attributes) -> ModuleType:
	module = ModuleType(name)
	for fieldname, value in attributes.items():
		setattr(module, fieldname, value)
	return module


@contextmanager
def _load_key_modules(configuration: dict[str, object]):
	frappe = _module(
		"frappe",
		ValidationError=_ValidationError,
		local=SimpleNamespace(conf=configuration),
		conf=configuration,
	)
	module_names = (
		"frappe",
		"ione_qms.services.crypto_keys",
		"ione_qms.services.identity_keys",
	)
	original = {name: sys.modules.get(name) for name in module_names}
	try:
		sys.modules["frappe"] = frappe
		root = Path(__file__).resolve().parents[1]
		crypto_path = root / "services" / "crypto_keys.py"
		crypto_spec = importlib.util.spec_from_file_location(
			"ione_qms.services.crypto_keys",
			crypto_path,
		)
		assert crypto_spec and crypto_spec.loader
		crypto = importlib.util.module_from_spec(crypto_spec)
		sys.modules["ione_qms.services.crypto_keys"] = crypto
		crypto_spec.loader.exec_module(crypto)

		identity_path = root / "services" / "identity_keys.py"
		identity_spec = importlib.util.spec_from_file_location(
			"ione_qms.services.identity_keys",
			identity_path,
		)
		assert identity_spec and identity_spec.loader
		identity = importlib.util.module_from_spec(identity_spec)
		sys.modules["ione_qms.services.identity_keys"] = identity
		identity_spec.loader.exec_module(identity)
		yield crypto, identity
	finally:
		for name, previous in original.items():
			if previous is None:
				sys.modules.pop(name, None)
			else:
				sys.modules[name] = previous


def _configuration() -> dict[str, object]:
	return {
		"ione_identity_hmac_key_id": "identity-v1",
		"ione_identity_hmac_key": "i" * 32,
		"ione_phi_hmac_key_id": "phi-v2",
		"ione_phi_hmac_key": "p" * 32,
		"ione_phi_hmac_verify_keys": {"phi-v1": "o" * 32},
	}


class TestDedicatedCryptographicKeys(TestCase):
	def test_active_key_never_falls_back_to_general_site_encryption_key(self) -> None:
		with _load_key_modules({"encryption_key": "x" * 64}) as (crypto, _identity):
			with self.assertRaisesRegex(_ValidationError, "_key_id"):
				crypto.active_hmac_key("ione_identity_hmac")

	def test_active_and_historical_keys_are_explicit_and_versioned(self) -> None:
		with _load_key_modules(_configuration()) as (crypto, _identity):
			active = crypto.active_hmac_key("ione_phi_hmac")
			historical = crypto.verification_hmac_key("ione_phi_hmac", "phi-v1")
			retained = crypto.retained_hmac_keys("ione_phi_hmac")
			self.assertEqual((active.key_id, active.secret), ("phi-v2", b"p" * 32))
			self.assertEqual((historical.key_id, historical.secret), ("phi-v1", b"o" * 32))
			self.assertEqual(retained, (historical,))

	def test_short_or_malformed_key_material_fails_closed(self) -> None:
		configuration = _configuration()
		configuration["ione_identity_hmac_key"] = "too-short"
		with _load_key_modules(configuration) as (crypto, _identity):
			with self.assertRaisesRegex(_ValidationError, "at least 32 bytes"):
				crypto.active_hmac_key("ione_identity_hmac")

	def test_complete_retained_ring_is_validated_before_production_use(self) -> None:
		configuration = _configuration()
		configuration["ione_phi_hmac_verify_keys"] = {
			"phi-v1": "o" * 32,
			"unsafe id": "z" * 32,
		}
		with _load_key_modules(configuration) as (crypto, _identity):
			with self.assertRaisesRegex(_ValidationError, "_key_id"):
				crypto.retained_hmac_keys("ione_phi_hmac")

	def test_retained_ring_cannot_repeat_active_id_or_secret(self) -> None:
		for ring, message in (
			({"phi-v2": "o" * 32}, "active key ID"),
			({"phi-v1": "p" * 32}, "active key secret"),
			({"phi-v1": "o" * 32, "phi-v0": "o" * 32}, "distinct secret"),
		):
			configuration = _configuration()
			configuration["ione_phi_hmac_verify_keys"] = ring
			with (
				self.subTest(ring=ring),
				_load_key_modules(configuration) as (
					crypto,
					_identity,
				),
				self.assertRaisesRegex(_ValidationError, message),
			):
				crypto.retained_hmac_keys("ione_phi_hmac")

	def test_identity_surrogates_are_deterministic_and_domain_separated(self) -> None:
		with _load_key_modules(_configuration()) as (_crypto, identity):
			patient = identity.site_identity_key("patient", "his-a", "HOSP-1", "12345")
			repeated = identity.site_identity_key("patient", "his-a", "HOSP-1", "12345")
			encounter = identity.site_identity_key("encounter", "his-a", "HOSP-1", "12345")
			other_tenant = identity.site_identity_key("patient", "his-a", "HOSP-2", "12345")
			self.assertEqual(patient, repeated)
			self.assertEqual(len(patient), 64)
			self.assertNotEqual(patient, encounter)
			self.assertNotEqual(patient, other_tenant)
			self.assertNotEqual(
				patient,
				identity.legacy_public_identity_key("his-a", "HOSP-1", "12345"),
			)
			self.assertEqual(
				identity.identity_key_contract(),
				"ione-qms-identity-key-v1:identity-v1",
			)

	def test_unknown_identity_domain_is_rejected(self) -> None:
		with _load_key_modules(_configuration()) as (_crypto, identity):
			with self.assertRaisesRegex(_ValidationError, "kind is not governed"):
				identity.site_identity_key("untyped", "his-a", "HOSP-1", "12345")

	def test_stable_source_fingerprint_is_exact_case_and_length_delimited(self) -> None:
		with _load_key_modules(_configuration()) as (_crypto, identity):
			upper = identity.stable_source_identity_key("patient", "SOURCE-1", "Namespace-A", "HOSP-1", "P-1")
			repeated = identity.stable_source_identity_key(
				"patient", "SOURCE-1", "Namespace-A", "HOSP-1", "P-1"
			)
			lower = identity.stable_source_identity_key("patient", "SOURCE-1", "namespace-a", "HOSP-1", "p-1")
			delimiter_variant = identity.stable_source_identity_key(
				"patient", "SOURCE-1", "a|b", "HOSP-1", "c"
			)
			repartitioned = identity.stable_source_identity_key("patient", "SOURCE-1", "a", "HOSP-1", "b|c")
			self.assertEqual(upper, repeated)
			self.assertEqual(len(upper), 64)
			self.assertNotEqual(upper, lower)
			self.assertNotEqual(delimiter_variant, repartitioned)

	def test_identity_rotation_verifies_retained_and_legacy_keys_before_rekey(self) -> None:
		configuration = _configuration()
		configuration.update(
			{
				"ione_identity_hmac_key_id": "identity-v2",
				"ione_identity_hmac_key": "n" * 32,
				"ione_identity_hmac_verify_keys": {"identity-v1": "i" * 32},
			}
		)
		with _load_key_modules(configuration) as (crypto, identity):
			parts = ("his-a", "HOSP-1", "12345")
			old_key = crypto.verification_hmac_key("ione_identity_hmac", "identity-v1")
			old_value = identity._identity_key_with_key("patient", *parts, key=old_key)
			self.assertTrue(
				identity.verify_stored_identity_key(
					"patient",
					old_value,
					"ione-qms-identity-key-v1:identity-v1",
					*parts,
				)
			)
			self.assertFalse(
				identity.verify_stored_identity_key(
					"patient",
					"0" * 64,
					"ione-qms-identity-key-v1:identity-v1",
					*parts,
				)
			)
			legacy = identity.legacy_public_identity_key(*parts)
			self.assertFalse(identity.verify_stored_identity_key("patient", legacy, "", *parts))
			self.assertTrue(
				identity.verify_stored_identity_key(
					"patient",
					legacy,
					"",
					*parts,
					allow_legacy=True,
				)
			)
			self.assertNotEqual(old_value, identity.site_identity_key("patient", *parts))
			candidates = identity.identity_key_candidates("patient", *parts)
			self.assertEqual(
				[contract for contract, _key in candidates],
				[
					"ione-qms-identity-key-v1:identity-v2",
					"ione-qms-identity-key-v1:identity-v1",
				],
			)
			self.assertEqual(
				identity.acceptable_identity_key_contracts(),
				tuple(contract for contract, _key in candidates),
			)
			self.assertEqual(candidates[1][1], old_value)

	def test_two_phase_identity_rotation_keeps_old_and_new_candidate_sets_identical(self) -> None:
		parts = ("his-a", "HOSP-1", "12345")
		phase_one = _configuration()
		phase_one["ione_identity_hmac_verify_keys"] = {"identity-v2": "n" * 32}
		with _load_key_modules(phase_one) as (_crypto, identity):
			old_worker_candidates = identity.identity_key_candidates("patient", *parts)

		phase_two = _configuration()
		phase_two.update(
			{
				"ione_identity_hmac_key_id": "identity-v2",
				"ione_identity_hmac_key": "n" * 32,
				"ione_identity_hmac_verify_keys": {"identity-v1": "i" * 32},
			}
		)
		with _load_key_modules(phase_two) as (_crypto, identity):
			new_worker_candidates = identity.identity_key_candidates("patient", *parts)

		self.assertEqual(old_worker_candidates[0][0], "ione-qms-identity-key-v1:identity-v1")
		self.assertEqual(new_worker_candidates[0][0], "ione-qms-identity-key-v1:identity-v2")
		self.assertEqual(set(old_worker_candidates), set(new_worker_candidates))


if __name__ == "__main__":
	import unittest

	unittest.main()
