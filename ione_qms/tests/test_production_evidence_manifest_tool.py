from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ione_qms.production_contracts import (
	EVIDENCE_REGISTER_FORMAT,
	MANIFEST_FORMAT,
	REQUIRED_GATE_CODES,
)
from ione_qms.release_tools.production_evidence import (
	EvidenceRegisterError,
	generate,
	main,
)

ROOT = Path(__file__).resolve().parents[2]


class TestProductionEvidenceManifestTool(unittest.TestCase):
	def setUp(self) -> None:
		self.temporary = tempfile.TemporaryDirectory()
		self.root = Path(self.temporary.name)
		self.register_path = self.root / "register.json"
		artifacts = []
		for index, gate_code in enumerate(REQUIRED_GATE_CODES):
			artifact_path = self.root / f"evidence-{index}.bin"
			artifact_path.write_bytes(f"signed evidence {gate_code}".encode())
			artifacts.append(
				{
					"id": f"EVIDENCE-{index:02d}",
					"path": artifact_path.name,
					"gate_codes": [gate_code],
					"owner": f"signatory-{index}@hospital.example",
					"signed_at": "2026-08-01T08:00:00+08:00",
					"expires_at": "2026-10-01T00:00:00+08:00",
				}
			)
		self.register = {
			"format": EVIDENCE_REGISTER_FORMAT,
			"site": "manager.example.internal",
			"release_record": "REL-2026-0001",
			"schema_revision": "schema-1",
			"migration_completion_receipt": "MIGRATION-2026-0001",
			"generated_at": "2026-08-01T09:00:00+08:00",
			"assessment_valid_until": "2026-08-31T23:59:59+08:00",
			"artifacts": artifacts,
		}
		self._write_register()

	def tearDown(self) -> None:
		self.temporary.cleanup()

	def _write_register(self) -> None:
		self.register_path.write_text(json.dumps(self.register), encoding="utf-8")

	def test_generates_exact_manifest_and_gate_rows_without_paths(self) -> None:
		manifest, rows = generate(self.register_path)
		self.assertEqual(manifest["format"], MANIFEST_FORMAT)
		self.assertEqual([row["gate_code"] for row in rows], list(REQUIRED_GATE_CODES))
		self.assertEqual(len(manifest["artifacts"]), len(REQUIRED_GATE_CODES))
		first_artifact = manifest["artifacts"][0]
		expected = hashlib.sha256((self.root / "evidence-0.bin").read_bytes()).hexdigest()
		self.assertEqual(first_artifact["sha256"], expected)
		serialized = json.dumps({"manifest": manifest, "rows": rows})
		self.assertNotIn(str(self.root), serialized)
		self.assertNotIn("path", first_artifact)

	def test_rejects_missing_or_duplicate_gate_coverage(self) -> None:
		self.register["artifacts"].pop()
		self.register["artifacts"][0]["gate_codes"].append(REQUIRED_GATE_CODES[1])
		self._write_register()
		with self.assertRaisesRegex(EvidenceRegisterError, "missing=.*oncall_observability"):
			generate(self.register_path)

	def test_rejects_technical_signatory_and_short_expiry(self) -> None:
		self.register["artifacts"][0]["owner"] = "Administrator"
		self._write_register()
		with self.assertRaisesRegex(EvidenceRegisterError, "named signatory"):
			generate(self.register_path)

		self.register["artifacts"][0]["owner"] = "security-owner@hospital.example"
		self.register["artifacts"][0]["expires_at"] = "2026-08-02T00:00:00+08:00"
		self._write_register()
		with self.assertRaisesRegex(EvidenceRegisterError, "expires before"):
			generate(self.register_path)

	def test_rejects_validity_beyond_server_maximum(self) -> None:
		self.register["assessment_valid_until"] = "2026-12-01T00:00:00+08:00"
		for artifact in self.register["artifacts"]:
			artifact["expires_at"] = "2026-12-31T00:00:00+08:00"
		self._write_register()
		with self.assertRaisesRegex(EvidenceRegisterError, "cannot exceed 90 days"):
			generate(self.register_path)

	def test_documented_register_has_exact_gate_contract(self) -> None:
		example = json.loads(
			(ROOT / "docs" / "production-evidence-register.example.json").read_text(encoding="utf-8")
		)
		self.assertEqual(example["format"], EVIDENCE_REGISTER_FORMAT)
		self.assertEqual(
			[code for artifact in example["artifacts"] for code in artifact["gate_codes"]],
			list(REQUIRED_GATE_CODES),
		)

	def test_cli_writes_atomically_and_does_not_overwrite_without_force(self) -> None:
		manifest_path = self.root / "manifest.json"
		rows_path = self.root / "rows.json"
		self.assertEqual(
			main(
				[
					str(self.register_path),
					"--manifest-output",
					str(manifest_path),
					"--gate-rows-output",
					str(rows_path),
				]
			),
			0,
		)
		self.assertEqual(len(json.loads(rows_path.read_text(encoding="utf-8"))), 14)
		with self.assertRaises(SystemExit):
			main(
				[
					str(self.register_path),
					"--manifest-output",
					str(manifest_path),
					"--gate-rows-output",
					str(rows_path),
				]
			)


if __name__ == "__main__":
	unittest.main()
