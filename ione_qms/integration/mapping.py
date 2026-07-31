from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any

FIELD_PATH_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_MAPPING_SNAPSHOT_BYTES = 16 * 1024
MAX_MAPPING_VALUE_LENGTH = 256
ALLOWED_TARGET_FIELDS = frozenset(
	{
		"patient_index",
		"encounter_index",
		"hospital",
		"campus",
		"department",
		"ward",
		"responsible_staff",
		"medical_staff",
		"medical_group",
		"disease",
		"surgery",
		"drg",
	}
)
PATIENT_MASTER_DATA_FIELDS = frozenset(
	{
		"source_patient_id",
		"patient_name",
		"gender",
		"date_of_birth",
		"identification_number",
		"phone",
		"status",
	}
)
ENCOUNTER_MASTER_DATA_FIELDS = frozenset(
	{
		"source_encounter_id",
		"encounter_no",
		"patient_source_id",
		"hospital",
		"campus",
		"department",
		"ward",
		"encounter_type",
		"status",
		"admission_time",
		"discharge_time",
		"responsible_staff",
		"medical_staff",
		"medical_group",
		"disease",
		"surgery",
		"drg",
	}
)
MASTER_DATA_CONSTANT_FIELDS = frozenset(
	{
		"gender",
		"status",
		"hospital",
		"campus",
		"department",
		"ward",
		"encounter_type",
		"responsible_staff",
		"medical_staff",
		"medical_group",
		"disease",
		"surgery",
		"drg",
	}
)


@dataclass(frozen=True)
class MappingDefinition:
	record: str
	code: str
	version: str
	checksum: str
	snapshot: str
	values: dict[str, Any]
	master_data: dict[str, Any]

	def lineage_values(self) -> dict[str, str]:
		return {
			"mapping_record": self.record,
			"mapping_code": self.code,
			"mapping_version": self.version,
			"mapping_checksum": self.checksum,
			"mapping_snapshot": self.snapshot,
		}


def apply_mapping(payload: dict[str, Any], mapping: dict[str, Any] | None) -> dict[str, Any]:
	normalized = normalize_mapping(mapping)
	result: dict[str, Any] = {}
	for target, source in normalized.items():
		if isinstance(source, dict):
			result[target] = source["constant"]
			continue
		value = _resolve(payload, source)
		if value not in (None, ""):
			result[target] = value
	return result


def normalize_mapping(mapping: Any) -> dict[str, Any]:
	if mapping in (None, ""):
		return {}
	if isinstance(mapping, str):
		try:
			mapping = json.loads(mapping)
		except ValueError as exc:
			raise ValueError("Integration mapping must be valid JSON") from exc
	if not isinstance(mapping, dict):
		raise ValueError("Integration mapping must be a JSON object")
	if len(mapping) > len(ALLOWED_TARGET_FIELDS):
		raise ValueError("Integration mapping contains too many target fields")
	normalized: dict[str, Any] = {}
	for target, source in mapping.items():
		if not isinstance(target, str) or target not in ALLOWED_TARGET_FIELDS:
			raise ValueError("Unsupported integration target field")
		if isinstance(source, dict):
			if set(source) != {"constant"}:
				raise ValueError(f"Mapping constant for {target} must contain only 'constant'")
			constant = source["constant"]
			if (
				not isinstance(constant, str)
				or not constant
				or constant != constant.strip()
				or len(constant) > MAX_MAPPING_VALUE_LENGTH
				or any(ord(character) < 32 for character in constant)
			):
				raise ValueError(f"Mapping constant for {target} must be a bounded canonical string")
			normalized[target] = {"constant": constant}
			continue
		if (
			not isinstance(source, str)
			or len(source) > MAX_MAPPING_VALUE_LENGTH
			or not FIELD_PATH_PATTERN.fullmatch(source)
			or "__" in source
		):
			raise ValueError(f"Unsafe source field path for {target}")
		normalized[target] = source
	return normalized


def normalize_master_data_configuration(value: Any) -> dict[str, Any]:
	if value in (None, ""):
		return {}
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError as exc:
			raise ValueError("Master-data mapping must be valid JSON") from exc
	if not isinstance(value, dict):
		raise ValueError("Master-data mapping must be a JSON object")
	if not value:
		return {}
	if set(value) != {"enabled", "entity", "source_version_strategy", "fields"}:
		raise ValueError("Master-data mapping must use the exact governed schema")
	enabled = value["enabled"]
	if type(enabled) not in {bool, int} or int(enabled) != 1:
		raise ValueError("Governed master-data mapping must be explicitly enabled")
	entity = str(value["entity"] or "")
	if entity not in {"Patient", "Encounter"}:
		raise ValueError("Master-data entity must be Patient or Encounter")
	strategy = str(value["source_version_strategy"] or "")
	if strategy not in {"integer", "datetime"}:
		raise ValueError("Master-data source version strategy must be integer or datetime")
	fields = value["fields"]
	allowed = PATIENT_MASTER_DATA_FIELDS if entity == "Patient" else ENCOUNTER_MASTER_DATA_FIELDS
	if not isinstance(fields, dict) or not fields or len(fields) > len(allowed):
		raise ValueError("Master-data fields must be a non-empty bounded JSON object")
	normalized_fields: dict[str, Any] = {}
	for target, source in fields.items():
		if not isinstance(target, str) or target not in allowed:
			raise ValueError("Unsupported master-data target field")
		if isinstance(source, dict):
			if set(source) != {"constant"} or target not in MASTER_DATA_CONSTANT_FIELDS:
				raise ValueError("Master-data constant is not allowed for this target")
			constant = source["constant"]
			if (
				not isinstance(constant, str)
				or not constant
				or constant != constant.strip()
				or len(constant) > MAX_MAPPING_VALUE_LENGTH
				or any(ord(character) < 32 for character in constant)
			):
				raise ValueError("Master-data constant must be a bounded canonical string")
			normalized_fields[target] = {"constant": constant}
			continue
		if (
			not isinstance(source, str)
			or len(source) > MAX_MAPPING_VALUE_LENGTH
			or not FIELD_PATH_PATTERN.fullmatch(source)
			or "__" in source
		):
			raise ValueError("Unsafe master-data source field path")
		normalized_fields[target] = source
	return {
		"enabled": 1,
		"entity": entity,
		"fields": normalized_fields,
		"source_version_strategy": strategy,
	}


def canonicalize_mapping(
	mapping: Any,
	master_data: Any = None,
) -> tuple[dict[str, Any], dict[str, Any], str, str]:
	normalized = normalize_mapping(mapping)
	normalized_master_data = normalize_master_data_configuration(master_data)
	snapshot = json.dumps(
		{
			"master_data": normalized_master_data,
			"scope": normalized,
		},
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
	)
	if len(snapshot.encode()) > MAX_MAPPING_SNAPSHOT_BYTES:
		raise ValueError("Canonical integration mapping exceeds the storage limit")
	checksum = hashlib.sha256(snapshot.encode()).hexdigest()
	return normalized, normalized_master_data, snapshot, checksum


def mapping_definition(
	*,
	record: Any,
	code: Any,
	version: Any,
	mapping: Any,
	master_data: Any = None,
	stored_checksum: Any = None,
	stored_snapshot: Any = None,
) -> MappingDefinition:
	record_raw = str(record or "")
	code_raw = str(code or "")
	version_raw = str(version or "")
	record_value = record_raw.strip()
	code_value = code_raw.strip()
	version_value = version_raw.strip()
	if not record_value or record_raw != record_value:
		raise ValueError("Integration mapping record identity is required")
	if (
		not code_value
		or code_raw != code_value
		or len(code_value) > 140
		or any(ord(character) < 32 for character in code_value)
	):
		raise ValueError("Integration mapping code must contain 1-140 characters")
	if version_raw != version_value or not VERSION_PATTERN.fullmatch(version_value):
		raise ValueError("Integration mapping version must be a canonical 1-64 character identifier")
	values, normalized_master_data, snapshot, checksum = canonicalize_mapping(mapping, master_data)
	if stored_checksum not in (None, ""):
		stored = str(stored_checksum)
		if not CHECKSUM_PATTERN.fullmatch(stored) or not hmac.compare_digest(stored, checksum):
			raise ValueError("Integration mapping checksum does not match its canonical snapshot")
	if stored_snapshot not in (None, "") and (
		not isinstance(stored_snapshot, str) or not hmac.compare_digest(stored_snapshot, snapshot)
	):
		raise ValueError("Stored integration mapping snapshot is not canonical")
	return MappingDefinition(
		record=record_value,
		code=code_value,
		version=version_value,
		checksum=checksum,
		snapshot=snapshot,
		values=values,
		master_data=normalized_master_data,
	)


def frozen_mapping_definition(
	*,
	record: Any,
	code: Any,
	version: Any,
	checksum: Any,
	snapshot: Any,
) -> MappingDefinition:
	if not isinstance(snapshot, str) or not snapshot:
		raise ValueError("Frozen integration mapping snapshot is required")
	if len(snapshot.encode()) > MAX_MAPPING_SNAPSHOT_BYTES:
		raise ValueError("Frozen integration mapping snapshot exceeds the storage limit")
	try:
		combined = json.loads(snapshot)
	except ValueError as exc:
		raise ValueError("Frozen integration mapping snapshot must be valid JSON") from exc
	if not isinstance(combined, dict) or set(combined) != {"master_data", "scope"}:
		raise ValueError("Frozen integration mapping snapshot has an invalid schema")
	definition = mapping_definition(
		record=record,
		code=code,
		version=version,
		mapping=combined["scope"],
		master_data=combined["master_data"],
		stored_checksum=checksum,
	)
	if not hmac.compare_digest(snapshot, definition.snapshot):
		raise ValueError("Frozen integration mapping snapshot is not canonical")
	return definition


def _resolve(payload: dict[str, Any], path: str) -> Any:
	current: Any = payload
	for part in path.split("."):
		if not isinstance(current, dict) or part not in current:
			return None
		current = current[part]
	return current
