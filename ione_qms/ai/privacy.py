from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from ione_qms.ai.output_safety import ai_content_privacy_violation

MEDICAL_RECORD_AI_PROFILE_VERSION = "ione-qms-medical-record-ai-profile-v1"
MEDICAL_RECORD_AI_SECTION_PATHS = frozenset(
	{
		"allergies",
		"care_timeline",
		"clinical_quality_flags",
		"diagnoses",
		"diagnosis_codes",
		"discharge_disposition",
		"infection_control",
		"laboratory_results",
		"lab_results",
		"length_of_stay",
		"medication_classes",
		"medications",
		"nursing_quality",
		"operative_quality",
		"pharmacy_quality",
		"procedure_codes",
		"procedures",
		"record_completeness",
		"treatment_timeline",
		"vital_signs",
	}
)

_MAX_DEPTH = 12
_MAX_NODES = 20_000
_MAX_COLLECTION_ITEMS = 1_000
_FORBIDDEN_KEY_PATTERN = re.compile(
	r"(?:^|_)(?:"
	r"address|birth|contact|date_of_birth|dob|email|encounter_id|encounter_no|"
	r"encounter_reference|id_card|identification|identity|medical_record_no|mobile|"
	r"mrn|name|patient|patient_id|patient_name|patient_reference|phone|source_id|"
	r"source_patient_id|source_encounter_id|source_record_id|telephone"
	r")(?:$|_)",
	re.IGNORECASE,
)


class AIPrivacyViolation(ValueError):
	def __init__(self, code: str):
		super().__init__(code)
		self.code = code


def deidentify_ai_value(value: Any, *, known_identifiers: tuple[str, ...]) -> Any:
	"""Recursively remove identity keys and reject residual direct identifiers."""
	nodes = 0
	known = tuple(
		sorted(
			{
				unicodedata.normalize("NFKC", str(item or "")).strip()
				for item in known_identifiers
				if len(unicodedata.normalize("NFKC", str(item or "")).strip()) >= 2
			},
			key=len,
			reverse=True,
		)
	)

	def inspect(candidate: Any, depth: int) -> Any:
		nonlocal nodes
		nodes += 1
		if nodes > _MAX_NODES or depth > _MAX_DEPTH:
			raise AIPrivacyViolation("AI_INPUT_STRUCTURE_LIMIT")
		if isinstance(candidate, Mapping):
			if len(candidate) > _MAX_COLLECTION_ITEMS:
				raise AIPrivacyViolation("AI_INPUT_COLLECTION_LIMIT")
			output: dict[str, Any] = {}
			for raw_key, item in candidate.items():
				key = unicodedata.normalize("NFKC", str(raw_key or "")).strip()
				normalized_key = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
				if not key or len(key) > 128 or _FORBIDDEN_KEY_PATTERN.search(normalized_key):
					continue
				output[key] = inspect(item, depth + 1)
			return output
		if isinstance(candidate, Sequence) and not isinstance(
			candidate,
			(str, bytes, bytearray),
		):
			if len(candidate) > _MAX_COLLECTION_ITEMS:
				raise AIPrivacyViolation("AI_INPUT_COLLECTION_LIMIT")
			return [inspect(item, depth + 1) for item in candidate]
		if isinstance(candidate, (bytes, bytearray)):
			raise AIPrivacyViolation("AI_INPUT_BINARY_VALUE")
		if candidate is None or isinstance(candidate, (bool, int, float)):
			return candidate
		text = unicodedata.normalize("NFKC", str(candidate))
		for identifier in known:
			text = re.sub(re.escape(identifier), "[REDACTED]", text, flags=re.IGNORECASE)
		violation = ai_content_privacy_violation(
			text,
			known_identifiers=known,
		)
		if violation:
			raise AIPrivacyViolation(f"AI_INPUT_{violation}")
		return text

	return inspect(value, 0)


def deidentified_content_hash(value: Any) -> str:
	return hashlib.sha256(
		json.dumps(
			value,
			ensure_ascii=False,
			sort_keys=True,
			separators=(",", ":"),
			default=str,
		).encode()
	).hexdigest()


def assert_model_output_privacy(
	*texts: Any,
	known_identifiers: tuple[str, ...],
) -> None:
	for value in texts:
		violation = ai_content_privacy_violation(
			str(value or ""),
			known_identifiers=known_identifiers,
		)
		if violation:
			raise AIPrivacyViolation(f"AI_OUTPUT_{violation}")
