from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

REGISTRY_VERSION = "ione-indicator-dimensions-v1"


@dataclass(frozen=True)
class DimensionSpec:
	"""One governed analytical dimension and its source/authorization adapters."""

	key: str
	label: str
	aliases: tuple[str, ...]
	enumerator_scope: tuple[str, ...]
	authorization_scope: tuple[str, ...]
	dataset_fields: dict[str, tuple[str, ...]]

	def fields_for_dataset(self, dataset: str) -> tuple[str, ...]:
		return self.dataset_fields.get(str(dataset or "").strip().lower(), ())


_CLINICAL_DATASETS = (
	"clinical_events",
	"encounters",
	"indicator_details",
	"medical_record_qc",
	"quality_findings",
	"safety_events",
	"surgery_qc",
)


def _all_datasets(*fields: str) -> dict[str, tuple[str, ...]]:
	return {dataset: tuple(fields) for dataset in _CLINICAL_DATASETS}


_DIMENSIONS: dict[str, DimensionSpec] = {
	"hospital": DimensionSpec(
		key="hospital",
		label="Hospital",
		aliases=(),
		enumerator_scope=("hospital",),
		authorization_scope=("hospital",),
		dataset_fields=_all_datasets("hospital"),
	),
	"campus": DimensionSpec(
		key="campus",
		label="Campus",
		aliases=(),
		enumerator_scope=("hospital", "campus"),
		authorization_scope=("hospital", "campus"),
		dataset_fields=_all_datasets("campus"),
	),
	"department": DimensionSpec(
		key="department",
		label="Department",
		aliases=(),
		enumerator_scope=("hospital", "campus", "department"),
		authorization_scope=("hospital", "campus", "department"),
		dataset_fields=_all_datasets("department"),
	),
	"ward": DimensionSpec(
		key="ward",
		label="Ward",
		aliases=(),
		enumerator_scope=("hospital", "campus", "department", "ward"),
		authorization_scope=("hospital", "campus", "department", "ward"),
		dataset_fields=_all_datasets("ward"),
	),
	"medical_group": DimensionSpec(
		key="medical_group",
		label="Medical Group",
		aliases=(),
		enumerator_scope=("hospital", "campus", "department", "medical_group"),
		authorization_scope=("hospital", "campus", "department"),
		dataset_fields=_all_datasets("medical_group"),
	),
	"physician": DimensionSpec(
		key="physician",
		label="Physician",
		aliases=("medical_staff",),
		enumerator_scope=("hospital", "campus", "department", "ward", "physician"),
		authorization_scope=(
			"hospital",
			"campus",
			"department",
			"ward",
			"medical_staff",
			"responsible_staff",
		),
		dataset_fields={
			"clinical_events": ("medical_staff", "responsible_staff"),
			"encounters": ("medical_staff", "responsible_staff"),
			"indicator_details": ("responsible_staff", "medical_staff"),
			"medical_record_qc": ("medical_staff", "responsible_staff"),
			"quality_findings": ("medical_staff", "responsible_staff"),
			"safety_events": ("medical_staff", "responsible_staff"),
			"surgery_qc": ("surgeon", "medical_staff", "responsible_staff"),
		},
	),
	"disease": DimensionSpec(
		key="disease",
		label="Disease",
		aliases=("diagnosis", "diagnosis_code"),
		enumerator_scope=("hospital", "campus", "department", "disease"),
		authorization_scope=("hospital", "campus", "department", "ward"),
		dataset_fields=_all_datasets("disease", "disease_code", "diagnosis_code"),
	),
	"surgery": DimensionSpec(
		key="surgery",
		label="Surgery",
		aliases=("procedure", "procedure_code"),
		enumerator_scope=("hospital", "campus", "department", "surgery"),
		authorization_scope=("hospital", "campus", "department", "ward"),
		dataset_fields={
			**_all_datasets("surgery", "procedure_code"),
			"surgery_qc": ("procedure_code", "surgery_type", "surgery"),
		},
	),
	"drg": DimensionSpec(
		key="drg",
		label="DRG",
		aliases=("drg_code",),
		enumerator_scope=("hospital", "campus", "department", "drg"),
		authorization_scope=("hospital", "campus", "department", "ward"),
		dataset_fields=_all_datasets("drg", "drg_code"),
	),
}

_ALIASES = {alias: key for key, spec in _DIMENSIONS.items() for alias in (key, *spec.aliases)}


def registered_dimensions() -> tuple[str, ...]:
	return tuple(sorted(_DIMENSIONS))


def dimension_spec(value: str) -> DimensionSpec:
	key = canonical_dimension(value)
	return _DIMENSIONS[key]


def canonical_dimension(value: str) -> str:
	key = str(value or "").strip().lower()
	canonical = _ALIASES.get(key)
	if not canonical:
		raise ValueError(f"Indicator dimension is not governed: {key or '<empty>'}.")
	return canonical


def normalize_dimension_names(values: Any) -> tuple[str, ...]:
	"""Normalize configured names and collapse the legacy medical_staff alias."""
	if values in (None, ""):
		return ()
	if isinstance(values, str):
		try:
			values = json.loads(values)
		except ValueError as exc:
			raise ValueError("Indicator dimensions must be valid JSON.") from exc
	if isinstance(values, dict):
		values = list(values)
	if not isinstance(values, list | tuple | set | frozenset):
		raise ValueError("Indicator dimensions must be an array or object.")
	normalized: list[str] = []
	for item in values:
		raw = item.get("fieldname") or item.get("field") or "" if isinstance(item, dict) else item
		canonical = canonical_dimension(str(raw))
		if canonical in normalized:
			raise ValueError(f"Indicator dimension is duplicated after alias migration: {canonical}.")
		normalized.append(canonical)
	return tuple(sorted(normalized))


def normalize_dimension_values(value: dict[str, Any] | str | None) -> dict[str, str]:
	"""Return the only canonical representation used by receipt and series hashes."""
	if value in (None, ""):
		return {}
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError as exc:
			raise ValueError("Indicator dimensions must be valid JSON.") from exc
	if not isinstance(value, dict):
		raise ValueError("Indicator dimensions must be a JSON object.")
	normalized: dict[str, str] = {}
	for raw_key, raw_value in value.items():
		if raw_value in (None, ""):
			continue
		key = canonical_dimension(str(raw_key))
		candidate = str(raw_value).strip()
		if not candidate:
			continue
		if key in normalized and normalized[key] != candidate:
			raise ValueError(f"Conflicting values were supplied for indicator dimension alias {key}.")
		normalized[key] = candidate
	return dict(sorted(normalized.items()))


def dataset_dimension_field(dataset: str, dimension: str, available_fields: set[str]) -> str:
	"""Resolve one reviewed physical mapping or fail closed."""
	spec = dimension_spec(dimension)
	for fieldname in spec.fields_for_dataset(dataset):
		if fieldname in available_fields:
			return fieldname
	raise ValueError(
		f"Indicator dataset {dataset!r} has no reviewed source mapping for dimension {spec.key!r}."
	)


def validate_dataset_capability(dataset: str, dimensions: tuple[str, ...] | list[str]) -> None:
	"""Pure publication guard: every dimension must declare a dataset adapter."""
	for dimension in dimensions:
		spec = dimension_spec(dimension)
		if not spec.fields_for_dataset(dataset):
			raise ValueError(
				f"Indicator dataset {dataset!r} does not declare calculator capability "
				f"for dimension {spec.key!r}."
			)


def dimension_registry_contract() -> dict[str, Any]:
	"""Canonical non-secret registry material included in query contracts."""
	return {
		"version": REGISTRY_VERSION,
		"dimensions": {
			key: {
				"aliases": list(spec.aliases),
				"enumerator_scope": list(spec.enumerator_scope),
				"authorization_scope": list(spec.authorization_scope),
				"dataset_fields": {
					dataset: list(fields) for dataset, fields in sorted(spec.dataset_fields.items())
				},
			}
			for key, spec in sorted(_DIMENSIONS.items())
		},
	}
