"""Build a hash-only production evidence manifest from signed local artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ione_qms.production_contracts import (
	EVIDENCE_REGISTER_FORMAT,
	MANIFEST_FORMAT,
	MAX_PRODUCTION_APPROVAL_DAYS,
	REQUIRED_GATE_CODES,
)

_SAFE_REFERENCE = re.compile(r"^[^\x00-\x1f\x7f]{1,240}$")
_REGISTER_KEYS = {
	"format",
	"site",
	"release_record",
	"schema_revision",
	"migration_completion_receipt",
	"generated_at",
	"assessment_valid_until",
	"artifacts",
}
_ARTIFACT_KEYS = {
	"id",
	"path",
	"gate_codes",
	"owner",
	"signed_at",
	"expires_at",
}


class EvidenceRegisterError(ValueError):
	"""The evidence register cannot produce a governed manifest."""


def _safe_reference(value: Any, label: str) -> str:
	normalized = str(value or "").strip()
	if not _SAFE_REFERENCE.fullmatch(normalized):
		raise EvidenceRegisterError(f"{label} must contain 1-240 printable characters")
	return normalized


def _parse_datetime(value: Any, label: str) -> datetime:
	text = str(value or "").strip()
	if not text:
		raise EvidenceRegisterError(f"{label} is required")
	try:
		parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
	except ValueError as exc:
		raise EvidenceRegisterError(f"{label} must be an ISO-8601 datetime") from exc
	if parsed.tzinfo is None:
		return parsed.replace(tzinfo=UTC)
	return parsed.astimezone(UTC)


def _sha256(path: Path) -> str:
	if not path.is_file():
		raise EvidenceRegisterError(f"evidence artifact is not a regular file: {path}")
	if path.stat().st_size <= 0:
		raise EvidenceRegisterError(f"evidence artifact is empty: {path}")
	digest = hashlib.sha256()
	with path.open("rb") as source:
		for chunk in iter(lambda: source.read(1024 * 1024), b""):
			digest.update(chunk)
	return digest.hexdigest()


def _load_register(register_path: Path) -> dict[str, Any]:
	try:
		register = json.loads(register_path.read_text(encoding="utf-8"))
	except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
		raise EvidenceRegisterError("evidence register must be readable UTF-8 JSON") from exc
	if not isinstance(register, dict) or set(register) != _REGISTER_KEYS:
		raise EvidenceRegisterError("evidence register keys do not match the governed format")
	if register.get("format") != EVIDENCE_REGISTER_FORMAT:
		raise EvidenceRegisterError("evidence register format is not supported")
	return register


def generate(register_path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
	"""Return a private-manifest payload and matching Frappe child-table rows."""

	path = Path(register_path).resolve(strict=True)
	register = _load_register(path)
	generated_at = _parse_datetime(register["generated_at"], "generated_at")
	valid_until = _parse_datetime(register["assessment_valid_until"], "assessment_valid_until")
	if generated_at > datetime.now(UTC):
		raise EvidenceRegisterError("generated_at cannot be in the future")
	if valid_until <= generated_at:
		raise EvidenceRegisterError("assessment_valid_until must be after generated_at")
	if valid_until > generated_at + timedelta(days=MAX_PRODUCTION_APPROVAL_DAYS):
		raise EvidenceRegisterError(f"assessment validity cannot exceed {MAX_PRODUCTION_APPROVAL_DAYS} days")

	input_artifacts = register.get("artifacts")
	if not isinstance(input_artifacts, list) or not (1 <= len(input_artifacts) <= 128):
		raise EvidenceRegisterError("artifacts must contain 1-128 entries")

	manifest_artifacts: list[dict[str, Any]] = []
	rows_by_gate: dict[str, dict[str, Any]] = {}
	artifact_ids: set[str] = set()
	gate_counts: Counter[str] = Counter()
	for index, artifact in enumerate(input_artifacts):
		if not isinstance(artifact, dict) or set(artifact) != _ARTIFACT_KEYS:
			raise EvidenceRegisterError(f"artifact {index} keys do not match the governed format")
		artifact_id = _safe_reference(artifact["id"], f"artifact {index} id")
		owner = _safe_reference(artifact["owner"], f"artifact {artifact_id} owner")
		if owner in {"Administrator", "Guest"}:
			raise EvidenceRegisterError(f"artifact {artifact_id} requires a named signatory")
		if artifact_id in artifact_ids:
			raise EvidenceRegisterError(f"duplicate artifact id: {artifact_id}")
		artifact_ids.add(artifact_id)

		gate_codes = artifact.get("gate_codes")
		if not isinstance(gate_codes, list) or not gate_codes:
			raise EvidenceRegisterError(f"artifact {artifact_id} requires gate_codes")
		if len(set(map(str, gate_codes))) != len(gate_codes):
			raise EvidenceRegisterError(f"artifact {artifact_id} repeats a gate code")
		unknown = sorted(set(map(str, gate_codes)) - set(REQUIRED_GATE_CODES))
		if unknown:
			raise EvidenceRegisterError(f"artifact {artifact_id} has unknown gates: {unknown}")

		signed_at = _parse_datetime(artifact["signed_at"], f"artifact {artifact_id} signed_at")
		expires_at = _parse_datetime(artifact["expires_at"], f"artifact {artifact_id} expires_at")
		if signed_at > generated_at:
			raise EvidenceRegisterError(f"artifact {artifact_id} is signed after generated_at")
		if expires_at < valid_until:
			raise EvidenceRegisterError(f"artifact {artifact_id} expires before assessment_valid_until")

		artifact_path = Path(str(artifact["path"]))
		if not artifact_path.is_absolute():
			artifact_path = path.parent / artifact_path
		digest = _sha256(artifact_path.resolve(strict=True))
		normalized_gates = [str(code) for code in gate_codes]
		manifest_artifacts.append(
			{
				"id": artifact_id,
				"sha256": digest,
				"gate_codes": normalized_gates,
				"owner": owner,
				"signed_at": str(artifact["signed_at"]),
				"expires_at": str(artifact["expires_at"]),
			}
		)
		for gate_code in normalized_gates:
			gate_counts[gate_code] += 1
			rows_by_gate[gate_code] = {
				"gate_code": gate_code,
				"evidence_reference": artifact_id,
				"evidence_sha256": digest,
				"signed_by": owner,
				"signed_at": str(artifact["signed_at"]),
				"expires_at": str(artifact["expires_at"]),
			}

	missing = [code for code in REQUIRED_GATE_CODES if gate_counts[code] == 0]
	duplicated = [code for code in REQUIRED_GATE_CODES if gate_counts[code] > 1]
	if missing or duplicated:
		raise EvidenceRegisterError(
			f"gate coverage must be exact; missing={missing}, duplicated={duplicated}"
		)

	manifest = {
		"format": MANIFEST_FORMAT,
		"site": _safe_reference(register["site"], "site"),
		"release_record": _safe_reference(register["release_record"], "release_record"),
		"schema_revision": _safe_reference(register["schema_revision"], "schema_revision"),
		"migration_completion_receipt": _safe_reference(
			register["migration_completion_receipt"], "migration_completion_receipt"
		),
		"generated_at": str(register["generated_at"]),
		"artifacts": sorted(manifest_artifacts, key=lambda item: item["id"]),
	}
	rows = [rows_by_gate[gate_code] for gate_code in REQUIRED_GATE_CODES]
	return manifest, rows


def _write_json(path: Path, payload: Any, *, force: bool) -> None:
	path = path.resolve()
	path.parent.mkdir(parents=True, exist_ok=True)
	if path.exists() and not force:
		raise EvidenceRegisterError(f"output already exists (use --force): {path}")
	encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
	temporary_name = ""
	try:
		with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as out:
			temporary_name = out.name
			out.write(encoded)
			out.flush()
			os.fsync(out.fileno())
		Path(temporary_name).replace(path)
	finally:
		if temporary_name:
			Path(temporary_name).unlink(missing_ok=True)


def _preflight_outputs(paths: tuple[Path, Path], *, force: bool) -> None:
	resolved = tuple(path.resolve() for path in paths)
	if resolved[0] == resolved[1]:
		raise EvidenceRegisterError("manifest and gate-row outputs must be different files")
	if not force:
		existing = [str(path) for path in resolved if path.exists()]
		if existing:
			raise EvidenceRegisterError(f"output already exists (use --force): {existing}")


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("register", type=Path, help="signed-evidence register JSON")
	parser.add_argument("--manifest-output", type=Path, required=True)
	parser.add_argument("--gate-rows-output", type=Path, required=True)
	parser.add_argument("--force", action="store_true")
	args = parser.parse_args(argv)
	try:
		_preflight_outputs(
			(args.manifest_output, args.gate_rows_output),
			force=args.force,
		)
		manifest, rows = generate(args.register)
		_write_json(args.manifest_output, manifest, force=args.force)
		_write_json(args.gate_rows_output, rows, force=args.force)
	except (EvidenceRegisterError, OSError) as exc:
		parser.error(str(exc))
	manifest_digest = hashlib.sha256(
		(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
	).hexdigest()
	print(f"manifest_sha256={manifest_digest}")
	print(f"gate_rows={len(rows)}")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
