from __future__ import annotations

import base64
import binascii
import hashlib
import inspect
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from ione_qms.indicator_engine.dimensions import (
	dataset_dimension_field,
	dimension_registry_contract,
	dimension_spec,
	normalize_dimension_names,
	validate_dataset_capability,
)

_ALLOWED_DATASETS: dict[str, tuple[str, tuple[str, ...]]] = {
	"clinical_events": (
		"IONE Clinical Quality Event",
		(
			"event_time",
			"hospital",
			"campus",
			"department",
			"ward",
			"medical_group",
			"medical_staff",
			"responsible_staff",
			"disease",
			"disease_code",
			"diagnosis_code",
			"surgery",
			"procedure_code",
			"drg",
			"drg_code",
			"dip",
			"dip_code",
			"event_type",
			"processing_status",
		),
	),
	"encounters": (
		"IONE Encounter Index",
		(
			"admission_time",
			"discharge_time",
			"hospital",
			"campus",
			"department",
			"ward",
			"medical_group",
			"medical_staff",
			"responsible_staff",
			"disease",
			"disease_code",
			"diagnosis_code",
			"surgery",
			"procedure_code",
			"drg",
			"drg_code",
			"dip",
			"dip_code",
			"encounter_type",
			"status",
		),
	),
	"indicator_details": (
		"IONE Indicator Result Detail",
		(
			"creation",
			"hospital",
			"campus",
			"department",
			"ward",
			"medical_group",
			"medical_staff",
			"responsible_staff",
			"disease",
			"disease_code",
			"diagnosis_code",
			"surgery",
			"procedure_code",
			"drg",
			"drg_code",
			"dip",
			"dip_code",
			"reason_code",
		),
	),
	"medical_record_qc": (
		"IONE Medical Record QC",
		(
			"creation",
			"hospital",
			"campus",
			"department",
			"ward",
			"medical_group",
			"medical_staff",
			"responsible_staff",
			"disease",
			"disease_code",
			"diagnosis_code",
			"surgery",
			"procedure_code",
			"drg",
			"drg_code",
			"dip",
			"dip_code",
			"status",
		),
	),
	"quality_findings": (
		"IONE QC Finding",
		(
			"detected_at",
			"hospital",
			"campus",
			"department",
			"ward",
			"medical_group",
			"medical_staff",
			"responsible_staff",
			"disease",
			"disease_code",
			"diagnosis_code",
			"surgery",
			"procedure_code",
			"drg",
			"drg_code",
			"dip",
			"dip_code",
			"severity",
			"status",
			"ai_origin",
		),
	),
	"safety_events": (
		"IONE Medical Safety Event",
		(
			"event_time",
			"hospital",
			"campus",
			"department",
			"ward",
			"medical_group",
			"medical_staff",
			"responsible_staff",
			"disease",
			"disease_code",
			"diagnosis_code",
			"surgery",
			"procedure_code",
			"drg",
			"drg_code",
			"dip",
			"dip_code",
			"severity",
			"status",
		),
	),
	"surgery_qc": (
		"IONE Surgery QC",
		(
			"surgery_time",
			"hospital",
			"campus",
			"department",
			"ward",
			"medical_group",
			"medical_staff",
			"responsible_staff",
			"surgeon",
			"disease",
			"disease_code",
			"diagnosis_code",
			"surgery",
			"procedure_code",
			"drg",
			"drg_code",
			"dip",
			"dip_code",
			"status",
		),
	),
}

_ALLOWED_FILTER_OPERATORS = frozenset(
	{"=", "!=", ">", ">=", "<", "<=", "in", "not in", "between", "is", "like"}
)
_FORMULA_KEYS = frozenset(
	{
		"measure",
		"multiplier",
		"precision",
		"numerator",
		"denominator",
		"dimensions",
		"rolling_window_days",
	}
)
_DATASET_SPEC_KEYS = frozenset({"dataset", "filters", "date_field"})
_MANIFEST_PAGE_SIZE = 1_000
_MAX_MANIFEST_RECORDS = 1_000_000
_INDICATOR_DETAIL_GOVERNANCE_DOCTYPES = (
	"IONE Indicator Result Detail",
	"IONE Indicator Result Pointer",
	"IONE Indicator Quarantine Receipt",
	"IONE Indicator Quarantine Disposition",
)
_SOURCE_LINEAGE_FIELDS = (
	"source_system",
	"mapping_record",
	"mapping_version",
	"mapping_checksum",
)
_STANDARD_FIELD_SIGNATURES = {
	"name": {"fieldname": "name", "fieldtype": "Data", "options": None, "length": 140},
	"creation": {
		"fieldname": "creation",
		"fieldtype": "Datetime",
		"options": None,
		"length": None,
	},
	"modified": {
		"fieldname": "modified",
		"fieldtype": "Datetime",
		"options": None,
		"length": None,
	},
}
_SUPPORTED_AUTHORIZATION_SCOPE_FIELDS = frozenset(
	{
		"hospital",
		"campus",
		"department",
		"ward",
		"medical_staff",
		"responsible_staff",
	}
)
_AUTHORIZATION_SCOPE_DIMENSION_ALIASES = {
	"medical_staff": "physician",
	"responsible_staff": "physician",
}


@dataclass(frozen=True)
class IndicatorContext:
	indicator_code: str
	indicator_name: str
	indicator_version: str
	period_start: date
	period_end: date
	dimensions: dict[str, str] = field(default_factory=dict)
	configured_dimensions: tuple[str, ...] = ()
	parameters: dict[str, Any] = field(default_factory=dict)
	calculator_version: str = "1"
	calculator_code_hash: str = ""
	mapping_record: str = ""
	mapping_version: str = ""
	mapping_checksum: str = ""
	source_system: str = ""
	query_contract_hash: str = ""
	physical_query_contract_hash: str = ""
	physical_query_contract: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IndicatorDetail:
	dimension_type: str | None = None
	dimension_value: str | None = None
	numerator: Decimal = Decimal(0)
	denominator: Decimal = Decimal(0)
	value: Decimal | None = None
	reason_code: str | None = None
	source_reference_hash: str | None = None
	lineage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IndicatorResult:
	numerator: Decimal
	denominator: Decimal
	value: Decimal | None
	lineage: dict[str, Any]
	details: tuple[IndicatorDetail, ...] = ()

	def __post_init__(self) -> None:
		if not self.numerator.is_finite() or not self.denominator.is_finite():
			raise ValueError("Indicator numerator and denominator must be finite.")
		if self.denominator < 0:
			raise ValueError("Indicator denominator cannot be negative.")
		if self.value is not None and not self.value.is_finite():
			raise ValueError("Indicator value must be finite when present.")


@dataclass(frozen=True)
class DimensionSnapshotPage:
	"""One bounded page from a signed, repeatable dimension-manifest scan.

	``record_hashes`` fingerprints every source row (including ``modified`` and
	the governed dimension values), not only the de-duplicated combinations.  A
	caller can therefore materialize a first pass, repeat the exact signed
	high-water scan, and publish work only after both hash chains match.
	"""

	dimensions: tuple[dict[str, str], ...]
	record_hashes: tuple[str, ...]
	has_more: bool
	next_cursor: str | None


class BaseIndicatorCalculator(ABC):
	code = "ABSTRACT"
	version = "1"
	supports_governed_dimensions = False

	@classmethod
	def validate_dimension_capability(
		cls,
		formula: dict[str, Any],
		dimensions: tuple[str, ...],
	) -> None:
		del formula
		if dimensions and not cls.supports_governed_dimensions:
			raise ValueError(f"Indicator calculator {cls.code} does not support governed dimensions.")

	@abstractmethod
	def calculate(self, context: IndicatorContext) -> IndicatorResult:
		"""Calculate one governed indicator without mutating source data."""


class RecordAggregateCalculator(BaseIndicatorCalculator):
	"""Calculate counts or rates from a small, reviewed set of local QMS datasets.

	Formula JSON has a measure, multiplier, and numerator/denominator dataset
	specifications. Dataset aliases and filter fields are hard-coded. This deliberately
	does not accept SQL, Python expressions, source-system credentials, or writes.
	"""

	code = "IONE_RECORD_AGGREGATE"
	version = "1"
	supports_governed_dimensions = True

	@classmethod
	def validate_dimension_capability(
		cls,
		formula: dict[str, Any],
		dimensions: tuple[str, ...],
	) -> None:
		validated = validate_formula_schema(formula, expected_dimensions=dimensions)
		measure = str(validated.get("measure") or "Rate").strip().lower()
		specs = [_mapping(validated.get("numerator"), "numerator")]
		if measure != "count":
			specs.append(_mapping(validated.get("denominator"), "denominator"))
		for spec in specs:
			validate_dataset_capability(str(spec.get("dataset") or "").strip().lower(), dimensions)

	def calculate(self, context: IndicatorContext) -> IndicatorResult:
		formula = validate_formula_schema(
			_formula(context.parameters),
			expected_dimensions=context.configured_dimensions,
		)
		measure = str(formula.get("measure") or "Rate").strip().lower()
		numerator_spec = _mapping(formula.get("numerator"), "numerator")
		numerator_count, numerator_source = _count_records(
			numerator_spec,
			context,
			role="numerator",
		)
		numerator = Decimal(numerator_count)
		if measure == "count":
			return IndicatorResult(
				numerator=numerator,
				denominator=Decimal(1),
				value=numerator,
				lineage=_lineage(context, formula, {"numerator": numerator_source}),
			)
		if measure not in {"rate", "proportion", "ratio"}:
			raise ValueError(f"Unsupported governed indicator measure: {measure}")
		denominator_spec = _mapping(formula.get("denominator"), "denominator")
		denominator_count, denominator_source = _count_records(
			denominator_spec,
			context,
			role="denominator",
		)
		denominator = Decimal(denominator_count)
		multiplier = _decimal(formula.get("multiplier", 100), "multiplier")
		precision = min(max(int(formula.get("precision", 2)), 0), 8)
		return IndicatorResult(
			numerator=numerator,
			denominator=denominator,
			value=safe_rate(numerator, denominator, multiplier=multiplier, precision=precision),
			lineage=_lineage(
				context,
				formula,
				{
					"denominator": denominator_source,
					"numerator": numerator_source,
				},
			),
		)


def validate_formula_schema(
	formula_json: dict[str, Any] | str,
	*,
	expected_dimensions: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
	"""Validate a generic aggregate formula without querying Frappe or a source system."""
	try:
		formula = json.loads(formula_json) if isinstance(formula_json, str) else formula_json
	except ValueError as exc:
		raise ValueError("Indicator formula must be valid JSON.") from exc
	if not isinstance(formula, dict):
		raise ValueError("Indicator formula must be a JSON object.")
	unknown = set(formula) - _FORMULA_KEYS
	if unknown:
		raise ValueError(f"Unsupported indicator formula keys: {', '.join(sorted(unknown))}.")
	measure = str(formula.get("measure") or "Rate").strip().lower()
	if measure not in {"count", "rate", "proportion", "ratio"}:
		raise ValueError(f"Unsupported governed indicator measure: {measure}")
	_validate_dataset_spec(formula.get("numerator"), "numerator")
	if measure != "count":
		_validate_dataset_spec(formula.get("denominator"), "denominator")
	elif formula.get("denominator") not in (None, ""):
		_validate_dataset_spec(formula.get("denominator"), "denominator")
	multiplier = _decimal(formula.get("multiplier", 100), "multiplier")
	if multiplier < 0:
		raise ValueError("Indicator multiplier cannot be negative.")
	try:
		precision = int(formula.get("precision", 2))
	except (TypeError, ValueError) as exc:
		raise ValueError("Indicator precision must be an integer from 0 to 8.") from exc
	if isinstance(formula.get("precision"), float) and not float(formula["precision"]).is_integer():
		raise ValueError("Indicator precision must be an integer from 0 to 8.")
	if precision not in range(0, 9):
		raise ValueError("Indicator precision must be an integer from 0 to 8.")
	dimensions = normalize_dimension_names(formula.get("dimensions") or [])
	if expected_dimensions is not None:
		expected = normalize_dimension_names(list(expected_dimensions))
		if dimensions != expected:
			raise ValueError(
				"Indicator formula dimensions must exactly match the governed version dimensions."
			)
	measure_specs = [_mapping(formula.get("numerator"), "numerator")]
	if measure != "count":
		measure_specs.append(_mapping(formula.get("denominator"), "denominator"))
	for spec in measure_specs:
		validate_dataset_capability(
			str(spec.get("dataset") or "").strip().lower(),
			dimensions,
		)
	if formula.get("rolling_window_days") not in (None, ""):
		try:
			window_days = int(formula["rolling_window_days"])
		except (TypeError, ValueError) as exc:
			raise ValueError("rolling_window_days must be an integer from 1 to 366.") from exc
		if window_days not in range(1, 367):
			raise ValueError("rolling_window_days must be an integer from 1 to 366.")
	normalized_formula = dict(formula)
	normalized_formula["dimensions"] = list(dimensions)
	return normalized_formula


def safe_rate(
	numerator: Decimal | int | float | str,
	denominator: Decimal | int | float | str,
	*,
	multiplier: Decimal | int | float | str = 100,
	precision: int = 2,
) -> Decimal | None:
	"""Return a rounded rate, or ``None`` when the denominator is zero."""
	numerator_value = _decimal(numerator, "numerator")
	denominator_value = _decimal(denominator, "denominator")
	multiplier_value = _decimal(multiplier, "multiplier")
	if denominator_value < 0:
		raise ValueError("Indicator denominator cannot be negative.")
	if denominator_value == 0:
		return None
	precision = min(max(int(precision), 0), 8)
	quantum = Decimal(1).scaleb(-precision)
	return (numerator_value / denominator_value * multiplier_value).quantize(
		quantum,
		rounding=ROUND_HALF_UP,
	)


def _validate_dataset_spec(value: Any, label: str) -> None:
	spec = _mapping(value, label)
	unknown = set(spec) - _DATASET_SPEC_KEYS
	if unknown:
		raise ValueError(f"Unsupported {label} keys: {', '.join(sorted(unknown))}.")
	alias = str(spec.get("dataset") or "").strip().lower()
	dataset = _ALLOWED_DATASETS.get(alias)
	if not dataset:
		raise ValueError(f"Indicator dataset is not registered: {alias}")
	_, reviewed_fields = dataset
	reviewed = frozenset(reviewed_fields)
	date_field = str(spec.get("date_field") or "").strip()
	if date_field and date_field not in reviewed:
		raise ValueError(f"Indicator date field is not reviewed: {date_field}")
	_validate_filter_shape(spec.get("filters"), reviewed)


def _validate_filter_shape(raw: Any, allowed_fields: frozenset[str]) -> None:
	if raw in (None, ""):
		return
	if not isinstance(raw, dict):
		raise ValueError("Indicator dataset filters must be a JSON object.")
	if len(raw) > 50:
		raise ValueError("Indicator dataset filters cannot contain more than 50 fields.")
	for fieldname, condition in raw.items():
		if fieldname not in allowed_fields:
			raise ValueError(f"Indicator filter field is not reviewed: {fieldname}")
		if fieldname == "name":
			raise ValueError("Indicator filters cannot bind the immutable pagination identity field name.")
		if isinstance(condition, list):
			if len(condition) != 2:
				raise ValueError(f"Invalid indicator filter for {fieldname}.")
			operator = str(condition[0]).strip().lower()
			if operator not in _ALLOWED_FILTER_OPERATORS:
				raise ValueError(f"Indicator filter operator is not allowed: {operator}")
			operand = condition[1]
			if operator in {"in", "not in"} and not isinstance(operand, list | tuple):
				raise ValueError(f"Indicator {operator} filter for {fieldname} requires an array.")
			if operator in {"in", "not in"} and (
				not operand or len(operand) > 1_000 or any(not _is_json_scalar(item) for item in operand)
			):
				raise ValueError(
					f"Indicator {operator} filter for {fieldname} requires 1-1,000 scalar values."
				)
			if operator == "between" and (not isinstance(operand, list | tuple) or len(operand) != 2):
				raise ValueError(f"Indicator between filter for {fieldname} requires two values.")
			if operator == "between" and any(not _is_json_scalar(item) for item in operand):
				raise ValueError(f"Indicator between filter for {fieldname} requires scalar values.")
			if operator == "is" and str(operand).strip().lower() not in {"set", "not set"}:
				raise ValueError(f"Indicator is filter for {fieldname} must be set or not set.")
			if operator == "like" and (not isinstance(operand, str) or len(operand) > 200):
				raise ValueError(f"Indicator like filter for {fieldname} requires a short string.")
			if operator not in {"in", "not in", "between"} and not _is_json_scalar(operand):
				raise ValueError(f"Indicator filter for {fieldname} requires a scalar value.")
		elif isinstance(condition, dict | tuple | set):
			raise ValueError(f"Indicator equality filter for {fieldname} must be a JSON scalar.")
		elif not _is_json_scalar(condition):
			raise ValueError(f"Indicator equality filter for {fieldname} must be a JSON scalar.")


def _is_json_scalar(value: Any) -> bool:
	if value is None or isinstance(value, str | bool | int):
		return True
	return isinstance(value, float) and value not in {float("inf"), float("-inf")} and value == value


def resolved_physical_query_contract(
	formula_json: dict[str, Any] | str,
	dimensions: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
	"""Resolve and fingerprint the exact database fields used by a published formula.

	This is deliberately a publication/runtime operation rather than a pure registry
	hash. A declared adapter is insufficient: every selected date, filter, dimension,
	and four-part integration-lineage field must exist in the installed schema.
	"""
	import frappe

	formula = validate_formula_schema(formula_json, expected_dimensions=dimensions)
	configured = normalize_dimension_names(list(dimensions or ()))
	_validate_runtime_dimension_scopes(configured)
	roles: dict[str, dict[str, Any]] = {}
	for role, spec in _formula_source_roles(formula):
		alias = str(spec.get("dataset") or "").strip().lower()
		doctype, reviewed_fields = _ALLOWED_DATASETS[alias]
		meta = frappe.get_meta(doctype)
		available_fields = {
			fieldname
			for fieldname in reviewed_fields
			if fieldname in _STANDARD_FIELD_SIGNATURES or meta.has_field(fieldname)
		}
		available_fields.update(_STANDARD_FIELD_SIGNATURES)
		missing_lineage = [fieldname for fieldname in _SOURCE_LINEAGE_FIELDS if not meta.has_field(fieldname)]
		if missing_lineage:
			raise ValueError(
				f"Dataset {alias} cannot prove complete source lineage; missing "
				+ ", ".join(missing_lineage)
				+ "."
			)
		available_fields.update(_SOURCE_LINEAGE_FIELDS)
		raw_filters = _adapt_legacy_dimension_filters(spec.get("filters"), available_fields)
		filters = _validated_filters(raw_filters, frozenset(available_fields))
		date_field = _resolved_date_field(alias, spec, available_fields)
		dimension_fields = {
			dimension: dataset_dimension_field(alias, dimension, available_fields) for dimension in configured
		}
		enumerator_fields: dict[str, str] = {}
		for dimension in configured:
			for scoped_dimension in dimension_spec(dimension).enumerator_scope:
				if scoped_dimension not in dimension_registry_contract()["dimensions"]:
					continue
				enumerator_fields.setdefault(
					scoped_dimension,
					dataset_dimension_field(alias, scoped_dimension, available_fields),
				)
		query_fields = {
			"name",
			"modified",
			date_field,
			*filters,
			*dimension_fields.values(),
			*enumerator_fields.values(),
			*_SOURCE_LINEAGE_FIELDS,
		}
		roles[role] = {
			"dataset": alias,
			"source_doctype": doctype,
			"date_field": date_field,
			"filters": filters,
			"dimension_fields": dict(sorted(dimension_fields.items())),
			"enumerator_fields": dict(sorted(enumerator_fields.items())),
			"authorization_scopes": {
				dimension: list(dimension_spec(dimension).authorization_scope) for dimension in configured
			},
			"lineage_fields": {fieldname: fieldname for fieldname in _SOURCE_LINEAGE_FIELDS},
			"schema_fields": {
				fieldname: _field_signature(meta, fieldname) for fieldname in sorted(query_fields)
			},
		}
	schema_payload = {role: contract["schema_fields"] for role, contract in sorted(roles.items())}
	return {
		"contract_version": "ione-indicator-physical-query-v1",
		"dimensions": list(configured),
		"roles": roles,
		"schema_signature_hash": hashlib.sha256(_canonical_json(schema_payload).encode("utf-8")).hexdigest(),
	}


def physical_query_contract_hash(contract: dict[str, Any]) -> str:
	"""Hash a resolved physical contract after enforcing its canonical shape."""
	if not isinstance(contract, dict) or contract.get("contract_version") != (
		"ione-indicator-physical-query-v1"
	):
		raise ValueError("Indicator physical query contract is invalid.")
	roles = contract.get("roles")
	if not isinstance(roles, dict) or not roles:
		raise ValueError("Indicator physical query contract requires source roles.")
	return hashlib.sha256(_canonical_json(contract).encode("utf-8")).hexdigest()


def _validate_runtime_dimension_scopes(dimensions: tuple[str, ...]) -> None:
	"""Fail publication when registry scope declarations have no runtime consumer."""
	configured = set(dimensions)
	for dimension in dimensions:
		specification = dimension_spec(dimension)
		if not specification.enumerator_scope:
			raise ValueError(f"Indicator dimension {dimension} lacks an enumerator scope.")
		if not specification.authorization_scope:
			raise ValueError(f"Indicator dimension {dimension} lacks an authorization scope.")
		unsupported = set(specification.authorization_scope) - _SUPPORTED_AUTHORIZATION_SCOPE_FIELDS
		if unsupported:
			raise ValueError(
				f"Indicator dimension {dimension} has unsupported authorization scope fields: "
				+ ", ".join(sorted(unsupported))
				+ "."
			)
		required_enumerator = set(specification.enumerator_scope)
		required_authorization = {
			_AUTHORIZATION_SCOPE_DIMENSION_ALIASES.get(fieldname, fieldname)
			for fieldname in specification.authorization_scope
		}
		missing = (required_enumerator | required_authorization) - configured
		if missing:
			raise ValueError(
				f"Indicator dimension {dimension} requires configured runtime scope dimensions: "
				+ ", ".join(sorted(missing))
				+ "."
			)


def _formula_source_roles(formula: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
	roles = [("numerator", _mapping(formula.get("numerator"), "numerator"))]
	if str(formula.get("measure") or "Rate").strip().lower() != "count":
		denominator = _mapping(formula.get("denominator"), "denominator")
		if _canonical_json(denominator) != _canonical_json(roles[0][1]):
			roles.append(("denominator", denominator))
	return roles


def _resolved_date_field(
	alias: str,
	spec: dict[str, Any],
	available_fields: set[str],
) -> str:
	date_field = str(spec.get("date_field") or "").strip()
	if not date_field:
		date_field = next(
			(
				candidate
				for candidate in (
					"event_time",
					"admission_time",
					"discharge_time",
					"surgery_time",
					"detected_at",
					"creation",
				)
				if candidate in available_fields
			),
			"",
		)
	if not date_field or date_field not in available_fields:
		raise ValueError(f"Dataset {alias} has no reviewed date field.")
	return date_field


def _field_signature(meta, fieldname: str) -> dict[str, Any]:
	if fieldname in _STANDARD_FIELD_SIGNATURES:
		return dict(_STANDARD_FIELD_SIGNATURES[fieldname])
	field = meta.get_field(fieldname)
	if field is None:
		raise ValueError(f"Indicator physical schema field is unavailable: {fieldname}.")
	return {
		"fieldname": fieldname,
		"fieldtype": str(getattr(field, "fieldtype", "") or ""),
		"options": getattr(field, "options", None),
		"length": getattr(field, "length", None),
		"precision": getattr(field, "precision", None),
		"reqd": int(getattr(field, "reqd", 0) or 0),
	}


def _count_records(
	spec: dict[str, Any],
	context: IndicatorContext,
	*,
	role: str,
) -> tuple[int, dict[str, Any]]:
	import frappe

	alias = str(spec.get("dataset") or "").strip().lower()
	physical = _physical_role_contract(context, role, spec)
	if str(physical.get("dataset") or "") != alias:
		raise ValueError("Indicator physical query role does not match its formula dataset.")
	doctype = str(physical.get("source_doctype") or "")
	date_field = str(physical.get("date_field") or "")
	filters = dict(physical.get("filters") or {})
	dimension_fields = dict(physical.get("dimension_fields") or {})
	lineage_fields = dict(physical.get("lineage_fields") or {})
	if set(lineage_fields) != set(_SOURCE_LINEAGE_FIELDS):
		raise ValueError("Indicator physical query contract lacks complete lineage bindings.")
	filters[date_field] = [
		"between",
		[
			datetime.combine(context.period_start, time.min),
			datetime.combine(context.period_end, time.max),
		],
	]
	for fieldname, value in context.dimensions.items():
		dataset_field = dimension_fields.get(fieldname)
		if not dataset_field:
			raise ValueError(f"Indicator physical query lacks dimension field {fieldname}.")
		if value:
			filters[dataset_field] = value
	lineage_values = _context_lineage_values(context)
	for fieldname, value in lineage_values.items():
		physical_field = lineage_fields.get(fieldname)
		if not physical_field:
			raise ValueError(f"Indicator physical query lacks lineage field {fieldname}.")
		filters[physical_field] = value
	manifest_fields = {
		"name",
		"modified",
		date_field,
		*dimension_fields.values(),
		*(physical.get("filters") or {}),
		*lineage_fields.values(),
	}
	rows_seen = 0
	record_cursor = ""
	leaf_hasher = hashlib.sha256()
	while True:
		page_filters = dict(filters)
		if record_cursor:
			page_filters["name"] = [">", record_cursor]
		rows = _governed_source_page(
			frappe,
			doctype,
			filters=page_filters,
			fields=sorted(manifest_fields),
			limit=min(
				_MANIFEST_PAGE_SIZE,
				_MAX_MANIFEST_RECORDS - rows_seen + 1,
			),
		)
		if not rows:
			break
		for row in rows:
			if rows_seen >= _MAX_MANIFEST_RECORDS:
				raise ValueError(f"Indicator source manifest exceeds {_MAX_MANIFEST_RECORDS} records.")
			leaf = {fieldname: row.get(fieldname) for fieldname in sorted(manifest_fields)}
			leaf_hash = hashlib.sha256(_canonical_json(leaf).encode("utf-8")).hexdigest()
			leaf_hasher.update(leaf_hash.encode("ascii"))
			leaf_hasher.update(b"\n")
			rows_seen += 1
			record_cursor = str(row.get("name") or "")
			if not record_cursor:
				raise ValueError("Indicator source manifest row lacks an immutable record identity.")
		if len(rows) < _MANIFEST_PAGE_SIZE:
			break
	source_receipt = {
		"dataset": alias,
		"source_doctype": doctype,
		"source_manifest_count": rows_seen,
		"source_manifest_hash": leaf_hasher.hexdigest(),
		**lineage_values,
	}
	return rows_seen, source_receipt


def indicator_result_detail_governance_schema_ready(frappe_module) -> bool:
	"""Return whether every table needed to fail-close governed detail reads exists."""
	try:
		return all(
			frappe_module.db.exists("DocType", doctype) for doctype in _INDICATOR_DETAIL_GOVERNANCE_DOCTYPES
		)
	except Exception:
		return False


def indicator_result_detail_governance_sql(table_reference: str) -> str:
	"""Build the mandatory current-pointer/exclusion predicate for a detail table."""
	if table_reference not in {"source", "`tabIONE Indicator Result Detail`"}:
		raise ValueError("Indicator detail governance SQL requested an unsupported table reference.")
	return (
		"exists (select 1 from `tabIONE Indicator Result Pointer` governed_pointer "  # noqa: S608
		f"where governed_pointer.current_result = {table_reference}.indicator_result) "
		"and (not exists ("
		"select 1 from `tabIONE Batch Work Item` governed_work "
		f"where governed_work.result_reference = {table_reference}.indicator_result"
		") or exists ("
		"select 1 from `tabIONE Batch Work Item` governed_work "
		"inner join `tabIONE Batch Run` governed_batch "
		"on governed_batch.name = governed_work.batch_run "
		f"where governed_work.result_reference = {table_reference}.indicator_result "
		"and governed_work.status = 'Succeeded' "
		"and governed_batch.batch_type = 'Indicator' "
		"and governed_batch.status = 'Completed'"
		")) "
		"and not exists ("
		"select 1 from `tabIONE Indicator Quarantine Receipt` governed_quarantine "
		"inner join `tabIONE Indicator Quarantine Disposition` governed_disposition "
		"on governed_disposition.quarantine_receipt = governed_quarantine.name "
		"where governed_quarantine.target_doctype = 'IONE Indicator Result Detail' "
		f"and governed_quarantine.target_name = {table_reference}.name "
		"and governed_disposition.status = 'Approved'"
		")"
	)


def _governed_source_page(
	frappe,
	doctype: str,
	*,
	filters: dict[str, Any],
	fields: list[str],
	limit: int,
) -> list[Any]:
	return _governed_source_rows(
		frappe,
		doctype,
		filters=filters,
		fields=fields,
		order_by="name asc",
		limit=limit,
	)


def _governed_source_rows(
	frappe,
	doctype: str,
	*,
	filters: dict[str, Any] | list[list[Any]],
	fields: list[str],
	order_by: str,
	limit: int,
) -> list[Any]:
	if doctype != "IONE Indicator Result Detail":
		return frappe.get_all(
			doctype,
			filters=filters,
			fields=fields,
			order_by=order_by,
			limit_page_length=limit,
		)
	if not indicator_result_detail_governance_schema_ready(frappe):
		raise ValueError("Indicator detail governance schema is unavailable; calculation is denied.")
	clauses, params = _indicator_detail_filter_sql(filters)
	clauses.append(indicator_result_detail_governance_sql("source"))
	selection = ", ".join(f"source.`{_sql_fieldname(fieldname)}`" for fieldname in fields)
	order = {
		"name asc": "source.name asc",
		"creation asc, name asc": "source.creation asc, source.name asc",
		"creation desc, name desc": "source.creation desc, source.name desc",
	}.get(str(order_by or "").strip().lower())
	if order is None:
		raise ValueError("Indicator detail governance SQL requested an unsupported source order.")
	query = (
		f"select {selection} from `tabIONE Indicator Result Detail` source "  # noqa: S608
		f"where {' and '.join(clauses)} order by {order} limit %s"
	)
	return frappe.db.sql(query, (*params, int(limit)), as_dict=True)


def _indicator_detail_filter_sql(
	filters: dict[str, Any] | list[list[Any]],
) -> tuple[list[str], list[Any]]:
	clauses: list[str] = []
	params: list[Any] = []
	for fieldname, operator, operand in _indicator_detail_filter_conditions(filters):
		field = f"source.`{_sql_fieldname(fieldname)}`"
		if operator not in _ALLOWED_FILTER_OPERATORS:
			raise ValueError(f"Unsupported governed indicator-detail filter operator: {operator}")
		if operator == "is":
			mode = str(operand).strip().lower()
			if mode == "set":
				clauses.append(f"({field} is not null and {field} != '')")
			elif mode == "not set":
				clauses.append(f"({field} is null or {field} = '')")
			else:
				raise ValueError("Governed indicator-detail is filter must be set or not set.")
			continue
		if operator in {"in", "not in"}:
			values = list(operand) if isinstance(operand, list | tuple) else []
			if not values:
				raise ValueError(f"Governed indicator-detail {operator} filter requires values.")
			nonnull = [value for value in values if value is not None]
			has_null = len(nonnull) != len(values)
			parts: list[str] = []
			if nonnull:
				placeholders = ", ".join(["%s"] * len(nonnull))
				parts.append(f"{field} {operator} ({placeholders})")
				params.extend(nonnull)
			if has_null:
				parts.append(f"{field} is {'not ' if operator == 'not in' else ''}null")
			joiner = " and " if operator == "not in" else " or "
			clauses.append(f"({joiner.join(parts)})")
			continue
		if operator == "between":
			if not isinstance(operand, list | tuple) or len(operand) != 2:
				raise ValueError("Governed indicator-detail between filter requires two values.")
			clauses.append(f"{field} between %s and %s")
			params.extend(operand)
			continue
		if operand is None and operator in {"=", "!="}:
			clauses.append(f"{field} is {'not ' if operator == '!=' else ''}null")
			continue
		clauses.append(f"{field} {operator} %s")
		params.append(operand)
	return clauses, params


def _indicator_detail_filter_conditions(
	filters: dict[str, Any] | list[list[Any]],
) -> list[tuple[str, str, Any]]:
	conditions: list[tuple[str, str, Any]] = []
	if isinstance(filters, dict):
		for fieldname, condition in filters.items():
			if isinstance(condition, list):
				if len(condition) != 2:
					raise ValueError(f"Invalid governed indicator-detail filter for {fieldname}.")
				conditions.append(
					(
						str(fieldname),
						str(condition[0]).strip().lower(),
						condition[1],
					)
				)
			else:
				conditions.append((str(fieldname), "=", condition))
		return conditions
	if not isinstance(filters, list):
		raise ValueError("Governed indicator-detail filters must be a mapping or filter list.")
	for condition in filters:
		if not isinstance(condition, list) or len(condition) != 4:
			raise ValueError("Governed indicator-detail filter-list condition is invalid.")
		conditions.append(
			(
				str(condition[1]),
				str(condition[2]).strip().lower(),
				condition[3],
			)
		)
	return conditions


def _sql_fieldname(fieldname: str) -> str:
	value = str(fieldname or "")
	if not value or not value.replace("_", "").isalnum():
		raise ValueError("Governed indicator-detail SQL field is invalid.")
	return value


def _physical_role_contract(
	context: IndicatorContext,
	role: str,
	spec: dict[str, Any],
) -> dict[str, Any]:
	contract = context.physical_query_contract
	if not contract:
		contract = resolved_physical_query_contract(
			_formula(context.parameters),
			context.configured_dimensions,
		)
	if context.physical_query_contract_hash:
		observed = physical_query_contract_hash(contract)
		if not hmac_compare(observed, context.physical_query_contract_hash):
			raise ValueError("Indicator physical query contract hash drifted.")
	roles = contract.get("roles") if isinstance(contract, dict) else None
	physical = roles.get(role) if isinstance(roles, dict) else None
	if not isinstance(physical, dict):
		# Identical numerator/denominator specifications intentionally share one
		# physical role; calculation can safely reuse the numerator contract.
		if role == "denominator" and isinstance(roles, dict):
			physical = roles.get("numerator")
	if not isinstance(physical, dict):
		raise ValueError(f"Indicator physical query contract lacks role {role}.")
	expected_alias = str(spec.get("dataset") or "").strip().lower()
	if str(physical.get("dataset") or "") != expected_alias:
		raise ValueError("Indicator physical query contract role conflicts with formula.")
	return physical


def _context_lineage_values(context: IndicatorContext) -> dict[str, str]:
	values = {
		"source_system": str(context.source_system or "").strip(),
		"mapping_record": str(context.mapping_record or "").strip(),
		"mapping_version": str(context.mapping_version or "").strip(),
		"mapping_checksum": str(context.mapping_checksum or "").strip().lower(),
	}
	if any(not value for value in values.values()):
		raise ValueError("Indicator source query requires complete source/mapping lineage.")
	if len(values["mapping_checksum"]) != 64 or any(
		character not in "0123456789abcdef" for character in values["mapping_checksum"]
	):
		raise ValueError("Indicator source query mapping_checksum must be a SHA-256 digest.")
	return values


def hmac_compare(left: str, right: str) -> bool:
	"""Local constant-time text comparison without adding a module-level Frappe dependency."""
	import hmac

	return hmac.compare_digest(str(left or ""), str(right or ""))


def _validated_filters(raw: Any, allowed_fields: frozenset[str]) -> dict[str, Any]:
	_validate_filter_shape(raw, allowed_fields)
	if raw in (None, ""):
		return {}
	filters: dict[str, Any] = {}
	for fieldname, condition in raw.items():
		filters[fieldname] = condition
	return filters


def _lineage(
	context: IndicatorContext,
	formula: dict[str, Any],
	sources: dict[str, dict[str, Any]],
) -> dict[str, Any]:
	return {
		"calculator": RecordAggregateCalculator.code,
		"calculator_version": RecordAggregateCalculator.version,
		"calculator_code_hash": context.calculator_code_hash,
		"indicator_code": context.indicator_code,
		"indicator_version": context.indicator_version,
		"mapping_record": context.mapping_record,
		"mapping_version": context.mapping_version,
		"mapping_checksum": context.mapping_checksum,
		"source_system": context.source_system,
		"query_contract_hash": context.query_contract_hash,
		"physical_query_contract_hash": context.physical_query_contract_hash,
		"period_start": context.period_start.isoformat(),
		"period_end": context.period_end.isoformat(),
		"dimensions": dict(sorted(context.dimensions.items())),
		"formula": formula,
		"sources": sources,
	}


def calculator_code_hash(calculator: type[BaseIndicatorCalculator]) -> str:
	"""Hash the reviewed calculator module and governed dimension dependency."""
	try:
		calculator_module = inspect.getmodule(calculator)
		dimension_module = inspect.getmodule(dataset_dimension_field)
		if calculator_module is None or dimension_module is None:
			raise OSError("calculator source module is unavailable")
		source = inspect.getsource(calculator_module)
		dimension_source = inspect.getsource(dimension_module)
	except (OSError, TypeError) as exc:
		raise ValueError(f"Cannot fingerprint indicator calculator {calculator!r}.") from exc
	payload = {
		"code": str(getattr(calculator, "code", "") or "").strip().upper(),
		"module": calculator.__module__,
		"module_source": source.replace("\r\n", "\n").replace("\r", "\n"),
		"dimension_module_source": dimension_source.replace("\r\n", "\n").replace("\r", "\n"),
		"qualname": calculator.__qualname__,
		"version": str(getattr(calculator, "version", "") or ""),
	}
	return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def governed_query_contract_hash(
	formula_json: dict[str, Any] | str,
	dimensions: tuple[str, ...] | list[str] | None = None,
) -> str:
	"""Hash the normalized query plus every reviewed physical dimension mapping."""
	formula = validate_formula_schema(
		formula_json,
		expected_dimensions=dimensions,
	)
	payload = {
		"contract_version": "ione-indicator-query-v1",
		"dimension_registry": dimension_registry_contract(),
		"dataset_registry": governed_dataset_contract(),
		"formula": formula,
	}
	return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def governed_dataset_contract() -> dict[str, Any]:
	"""Return the non-secret physical source/query allowlist frozen at publication."""
	return {
		"allowed_filter_operators": sorted(_ALLOWED_FILTER_OPERATORS),
		"datasets": {
			alias: {
				"doctype": doctype,
				"reviewed_fields": list(reviewed_fields),
			}
			for alias, (doctype, reviewed_fields) in sorted(_ALLOWED_DATASETS.items())
		},
	}


def start_dimension_snapshot(
	formula_json: dict[str, Any] | str,
	dimensions: tuple[str, ...] | list[str],
	period_start: date,
	period_end: date,
	*,
	lineage: dict[str, str] | None = None,
	physical_query_contract: dict[str, Any] | str | None = None,
) -> str:
	"""Create a signed start cursor for a repeatable, bounded manifest scan.

	The cursor freezes the per-role ``(creation, name)`` upper bounds.  It does
	not by itself claim that mutable row values are frozen; callers must complete
	two passes with :func:`read_dimension_snapshot_page` and compare the complete
	row-hash chains before releasing materialized dimension work.
	"""
	import frappe

	(
		_contract,
		contract_hash,
		cursor_scope_hash,
		roles,
		_source_lineage,
	) = _dimension_snapshot_contract(
		formula_json,
		dimensions,
		period_start,
		period_end,
		lineage=lineage,
		physical_query_contract=physical_query_contract,
	)
	high_watermarks = _dimension_source_high_watermarks(
		frappe,
		roles=roles,
		contract=_contract,
		period_start=period_start,
		period_end=period_end,
		lineage=_source_lineage,
	)
	return _encode_dimension_cursor(
		roles[0][0],
		"",
		"",
		contract_hash,
		cursor_scope_hash,
		high_watermarks,
	)


def source_snapshot_watermark(
	formula_json: dict[str, Any] | str,
	dimensions: tuple[str, ...] | list[str],
	period_start: date,
	period_end: date,
	*,
	lineage: dict[str, str] | None = None,
	physical_query_contract: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
	"""Return a bounded mutation watermark for daily completed-run freshness sweeps."""
	import frappe

	configured = normalize_dimension_names(list(dimensions))
	_validate_runtime_dimension_scopes(configured)
	formula = validate_formula_schema(formula_json, expected_dimensions=configured)
	contract = _coerce_physical_contract(physical_query_contract)
	if not contract:
		contract = resolved_physical_query_contract(formula, configured)
	contract_hash = physical_query_contract_hash(contract)
	source_lineage = _enumerator_lineage_values(lineage)
	role_watermarks: dict[str, dict[str, Any]] = {}
	for role, _spec in _formula_source_roles(formula):
		role_contract = contract.get("roles", {}).get(role)
		if not isinstance(role_contract, dict):
			raise ValueError(f"Indicator physical query contract lacks watermark role {role}.")
		doctype = str(role_contract.get("source_doctype") or "")
		filters = _enumerator_filters(
			role_contract,
			period_start=period_start,
			period_end=period_end,
			lineage=source_lineage,
		)
		count = int(frappe.db.count(doctype, filters=filters))
		identity_rows = _governed_source_rows(
			frappe,
			doctype,
			filters=filters,
			fields=["name", "creation"],
			order_by="creation desc, name desc",
			limit=1,
		)
		modified_rows = _governed_source_rows(
			frappe,
			doctype,
			filters=filters,
			fields=["name", "modified"],
			order_by="modified desc, name desc",
			limit=1,
		)
		role_watermarks[role] = {
			"count": count,
			"high_creation": str(identity_rows[0].get("creation") or "") if identity_rows else "",
			"high_name": str(identity_rows[0].get("name") or "") if identity_rows else "",
			"max_modified": str(modified_rows[0].get("modified") or "") if modified_rows else "",
			"modified_name": str(modified_rows[0].get("name") or "") if modified_rows else "",
			"source_doctype": doctype,
		}
	return {
		"contract_hash": contract_hash,
		"period_end": period_end.isoformat(),
		"period_start": period_start.isoformat(),
		"roles": dict(sorted(role_watermarks.items())),
		"version": "ione-indicator-source-watermark-v1",
	}


def read_dimension_snapshot_page(
	formula_json: dict[str, Any] | str,
	dimensions: tuple[str, ...] | list[str],
	period_start: date,
	period_end: date,
	*,
	record_cursor: str,
	limit: int,
	lineage: dict[str, str] | None = None,
	physical_query_contract: dict[str, Any] | str | None = None,
) -> DimensionSnapshotPage:
	"""Read one source-row page from a previously signed snapshot cursor.

	The page is deliberately based on source rows rather than ``DISTINCT``
	dimensions.  This makes deletion, filter/date changes, and in-place dimension
	mutations observable during the mandatory verification pass.  The returned
	dimension combinations can be de-duplicated into persistent work items by a
	deterministic unique key.
	"""
	import frappe

	if not record_cursor:
		raise ValueError("Indicator dimension snapshot reads require a signed start cursor.")
	(
		contract,
		contract_hash,
		cursor_scope_hash,
		roles,
		source_lineage,
	) = _dimension_snapshot_contract(
		formula_json,
		dimensions,
		period_start,
		period_end,
		lineage=lineage,
		physical_query_contract=physical_query_contract,
	)
	configured = normalize_dimension_names(list(dimensions))
	(
		cursor_role,
		cursor_creation,
		cursor_name,
		high_watermarks,
	) = _decode_dimension_cursor(
		record_cursor,
		roles=roles,
		contract_hash=contract_hash,
		cursor_scope_hash=cursor_scope_hash,
	)
	if cursor_role is None:
		raise ValueError("Indicator dimension snapshot cursor lacks a source role.")
	page_limit = min(max(int(limit or 1), 1), 5_000)
	role_names = [role for role, _spec in roles]
	start_index = role_names.index(cursor_role)
	manifest_hashes: list[str] = []
	combinations: list[dict[str, str]] = []
	last_creation = cursor_creation or ""
	last_name = cursor_name or ""
	for role_index in range(start_index, len(roles)):
		role, spec = roles[role_index]
		role_contract = contract.get("roles", {}).get(role)
		if not isinstance(role_contract, dict):
			raise ValueError(f"Indicator physical query contract lacks snapshot role {role}.")
		alias = str(spec.get("dataset") or "").strip().lower()
		if str(role_contract.get("dataset") or "") != alias:
			raise ValueError("Indicator snapshot role conflicts with its frozen physical contract.")
		doctype = str(role_contract.get("source_doctype") or "")
		mappings = dict(role_contract.get("dimension_fields") or {})
		if set(mappings) != set(configured):
			raise ValueError("Indicator snapshot dimension mapping is incomplete.")
		filters = _enumerator_filters(
			role_contract,
			period_start=period_start,
			period_end=period_end,
			lineage=source_lineage,
		)
		lineage_fields = dict(role_contract.get("lineage_fields") or {})
		source_fields = sorted(
			{
				*mappings.values(),
				*(role_contract.get("enumerator_fields") or {}).values(),
				*lineage_fields.values(),
			}
		)
		remaining = page_limit - len(manifest_hashes)
		role_cursor_creation = cursor_creation if role_index == start_index else None
		role_cursor_name = cursor_name if role_index == start_index else None
		rows = _frozen_enumerator_page(
			frappe,
			doctype,
			base_filters=filters,
			fields=["name", "creation", "modified", *source_fields],
			cursor_creation=role_cursor_creation,
			cursor_name=role_cursor_name,
			high_watermark=high_watermarks[role],
			limit=remaining + 1,
		)
		for row in rows[:remaining]:
			last_name = str(row.get("name") or "")
			last_creation = str(row.get("creation") or "")
			if not last_name or not last_creation:
				raise ValueError("Indicator dimension snapshot row identity is incomplete.")
			values = {
				dimension: str(row.get(source_field) or "").strip()
				for dimension, source_field in mappings.items()
			}
			if any(not value for value in values.values()):
				raise ValueError(f"{doctype} {last_name} lacks a configured governed dimension source value.")
			canonical_values = dict(sorted(values.items()))
			combinations.append(canonical_values)
			manifest_hashes.append(
				hashlib.sha256(
					_canonical_json(
						{
							"creation": last_creation,
							"doctype": doctype,
							"modified": str(row.get("modified") or ""),
							"name": last_name,
							"role": role,
							"source_values": {
								fieldname: str(row.get(fieldname) or "") for fieldname in source_fields
							},
							"values": canonical_values,
							"version": "ione-dimension-source-row-v1",
						}
					).encode("utf-8")
				).hexdigest()
			)
		if len(rows) > remaining:
			return DimensionSnapshotPage(
				dimensions=tuple(combinations),
				record_hashes=tuple(manifest_hashes),
				has_more=True,
				next_cursor=_encode_dimension_cursor(
					role,
					last_creation,
					last_name,
					contract_hash,
					cursor_scope_hash,
					high_watermarks,
				),
			)
		cursor_creation = None
		cursor_name = None
		if role_index + 1 < len(roles):
			if len(manifest_hashes) >= page_limit:
				return DimensionSnapshotPage(
					dimensions=tuple(combinations),
					record_hashes=tuple(manifest_hashes),
					has_more=True,
					next_cursor=_encode_dimension_cursor(
						roles[role_index + 1][0],
						"",
						"",
						contract_hash,
						cursor_scope_hash,
						high_watermarks,
					),
				)
			last_creation = ""
			last_name = ""
	return DimensionSnapshotPage(
		dimensions=tuple(combinations),
		record_hashes=tuple(manifest_hashes),
		has_more=False,
		next_cursor=None,
	)


def _dimension_snapshot_contract(
	formula_json: dict[str, Any] | str,
	dimensions: tuple[str, ...] | list[str],
	period_start: date,
	period_end: date,
	*,
	lineage: dict[str, str] | None,
	physical_query_contract: dict[str, Any] | str | None,
) -> tuple[
	dict[str, Any],
	str,
	str,
	list[tuple[str, dict[str, Any]]],
	dict[str, str],
]:
	configured = normalize_dimension_names(list(dimensions))
	if not configured:
		raise ValueError("Indicator dimension snapshot requires configured dimensions.")
	_validate_runtime_dimension_scopes(configured)
	formula = validate_formula_schema(formula_json, expected_dimensions=configured)
	contract = _coerce_physical_contract(physical_query_contract)
	if not contract:
		contract = resolved_physical_query_contract(formula, configured)
	contract_hash = physical_query_contract_hash(contract)
	source_lineage = _enumerator_lineage_values(lineage)
	cursor_scope_hash = hashlib.sha256(
		_canonical_json(
			{
				"contract": contract_hash,
				"lineage": source_lineage,
				"period_end": period_end.isoformat(),
				"period_start": period_start.isoformat(),
				"snapshot_protocol": "ione-dimension-double-scan-v1",
			}
		).encode("utf-8")
	).hexdigest()
	return (
		contract,
		contract_hash,
		cursor_scope_hash,
		_formula_source_roles(formula),
		source_lineage,
	)


def enumerate_dimension_combinations(
	formula_json: dict[str, Any] | str,
	dimensions: tuple[str, ...] | list[str],
	period_start: date,
	period_end: date,
	*,
	record_cursor: str | None,
	limit: int,
	lineage: dict[str, str] | None = None,
	physical_query_contract: dict[str, Any] | str | None = None,
) -> tuple[list[dict[str, str]], bool, str | None]:
	"""Enumerate the numerator/denominator union with a stable opaque cursor."""
	import frappe

	configured = normalize_dimension_names(list(dimensions))
	if not configured:
		return ([{}], False, None)
	_validate_runtime_dimension_scopes(configured)
	formula = validate_formula_schema(formula_json, expected_dimensions=configured)
	contract = _coerce_physical_contract(physical_query_contract)
	if not contract:
		contract = resolved_physical_query_contract(formula, configured)
	contract_hash = physical_query_contract_hash(contract)
	source_lineage = _enumerator_lineage_values(lineage)
	cursor_scope_hash = hashlib.sha256(
		_canonical_json(
			{
				"contract": contract_hash,
				"lineage": source_lineage,
				"period_end": period_end.isoformat(),
				"period_start": period_start.isoformat(),
			}
		).encode("utf-8")
	).hexdigest()
	roles = _formula_source_roles(formula)
	(
		cursor_role,
		cursor_creation,
		cursor_name,
		high_watermarks,
	) = _decode_dimension_cursor(
		record_cursor,
		roles=roles,
		contract_hash=contract_hash,
		cursor_scope_hash=cursor_scope_hash,
	)
	if not record_cursor:
		high_watermarks = _dimension_source_high_watermarks(
			frappe,
			roles=roles,
			contract=contract,
			period_start=period_start,
			period_end=period_end,
			lineage=source_lineage,
		)
	page_limit = min(max(int(limit or 1), 1), 1_000)
	work: list[dict[str, str]] = []
	seen: set[str] = set()
	role_names = [role for role, _spec in roles]
	start_index = role_names.index(cursor_role) if cursor_role else 0
	for role_index in range(start_index, len(roles)):
		role, spec = roles[role_index]
		role_contract = contract.get("roles", {}).get(role)
		if not isinstance(role_contract, dict):
			raise ValueError(f"Indicator physical query contract lacks enumerator role {role}.")
		alias = str(spec.get("dataset") or "").strip().lower()
		if str(role_contract.get("dataset") or "") != alias:
			raise ValueError("Indicator enumerator role conflicts with its frozen physical contract.")
		doctype = str(role_contract.get("source_doctype") or "")
		mappings = dict(role_contract.get("dimension_fields") or {})
		if set(mappings) != set(configured):
			raise ValueError("Indicator enumerator dimension mapping is incomplete.")
		filters = _enumerator_filters(
			role_contract,
			period_start=period_start,
			period_end=period_end,
			lineage=source_lineage,
		)
		lineage_fields = dict(role_contract.get("lineage_fields") or {})
		role_cursor_creation = cursor_creation if role_index == start_index else None
		role_cursor_name = cursor_name if role_index == start_index else None
		high_watermark = high_watermarks.get(role)
		if not isinstance(high_watermark, dict):
			raise ValueError("Indicator dimension cursor lacks a frozen source high-water mark.")
		rows = _frozen_enumerator_page(
			frappe,
			doctype,
			base_filters=filters,
			fields=[
				"name",
				"creation",
				*sorted(
					{
						*mappings.values(),
						*(role_contract.get("enumerator_fields") or {}).values(),
						*lineage_fields.values(),
					}
				),
			],
			cursor_creation=role_cursor_creation,
			cursor_name=role_cursor_name,
			high_watermark=high_watermark,
			limit=page_limit + 1,
		)
		processed = 0
		last_name: str | None = None
		for row in rows:
			if len(work) >= page_limit:
				break
			last_name = str(row.get("name") or "")
			last_creation = str(row.get("creation") or "")
			processed += 1
			values = {
				dimension: str(row.get(source_field) or "").strip()
				for dimension, source_field in mappings.items()
			}
			if any(not value for value in values.values()):
				raise ValueError(f"{doctype} {last_name} lacks a configured governed dimension source value.")
			key = _canonical_json(values)
			if key not in seen and not _dimension_combination_previously_enumerated(
				frappe,
				roles=roles,
				role_index=role_index,
				contract=contract,
				current_name=last_name,
				current_creation=last_creation,
				values=values,
				period_start=period_start,
				period_end=period_end,
				lineage=source_lineage,
				check_current_role_before=bool(
					role_index == start_index and (cursor_creation or cursor_name)
				),
				high_watermarks=high_watermarks,
			):
				seen.add(key)
				work.append(dict(sorted(values.items())))
		role_has_more = processed < len(rows) or len(rows) > page_limit
		if role_has_more:
			return (
				work,
				True,
				_encode_dimension_cursor(
					role,
					last_creation or role_cursor_creation or "",
					last_name or cursor_name or "",
					contract_hash,
					cursor_scope_hash,
					high_watermarks,
				),
			)
		cursor_creation = None
		cursor_name = None
		if role_index + 1 < len(roles):
			return (
				work,
				True,
				_encode_dimension_cursor(
					roles[role_index + 1][0],
					"",
					"",
					contract_hash,
					cursor_scope_hash,
					high_watermarks,
				),
			)
	return work, False, None


def _dimension_combination_previously_enumerated(
	frappe,
	*,
	roles: list[tuple[str, dict[str, Any]]],
	role_index: int,
	contract: dict[str, Any],
	current_name: str,
	current_creation: str,
	values: dict[str, str],
	period_start: date,
	period_end: date,
	lineage: dict[str, str],
	check_current_role_before: bool,
	high_watermarks: dict[str, dict[str, str]],
) -> bool:
	"""Prove that a candidate is the first occurrence in the ordered source union."""
	indices = list(range(role_index))
	if check_current_role_before:
		indices.append(role_index)
	for prior_index in indices:
		prior_role, _prior_spec = roles[prior_index]
		prior_contract = contract.get("roles", {}).get(prior_role)
		if not isinstance(prior_contract, dict):
			raise ValueError(f"Indicator physical query contract lacks enumerator role {prior_role}.")
		filters = _enumerator_filters(
			prior_contract,
			period_start=period_start,
			period_end=period_end,
			lineage=lineage,
		)
		mappings = dict(prior_contract.get("dimension_fields") or {})
		if set(mappings) != set(values):
			raise ValueError("Indicator enumerator union has incompatible dimension mappings.")
		for dimension, value in values.items():
			filters[mappings[dimension]] = value
		upper_bound = (
			{"creation": current_creation, "name": current_name, "inclusive": False}
			if prior_index == role_index
			else {
				**high_watermarks.get(prior_role, {}),
				"inclusive": True,
			}
		)
		if _frozen_enumerator_exists(
			frappe,
			str(prior_contract.get("source_doctype") or ""),
			base_filters=filters,
			upper_bound=upper_bound,
		):
			return True
	return False


def _dimension_source_high_watermarks(
	frappe,
	*,
	roles: list[tuple[str, dict[str, Any]]],
	contract: dict[str, Any],
	period_start: date,
	period_end: date,
	lineage: dict[str, str],
) -> dict[str, dict[str, str]]:
	"""Freeze each source union at its current `(creation, name)` maximum."""
	high_watermarks: dict[str, dict[str, str]] = {}
	for role, _spec in roles:
		role_contract = contract.get("roles", {}).get(role)
		if not isinstance(role_contract, dict):
			raise ValueError(f"Indicator physical query contract lacks enumerator role {role}.")
		doctype = str(role_contract.get("source_doctype") or "")
		filters = _enumerator_filters(
			role_contract,
			period_start=period_start,
			period_end=period_end,
			lineage=lineage,
		)
		rows = _governed_source_rows(
			frappe,
			doctype,
			filters=filters,
			fields=["name", "creation"],
			order_by="creation desc, name desc",
			limit=1,
		)
		if rows:
			creation = str(rows[0].get("creation") or "")
			name = str(rows[0].get("name") or "")
			if not creation or not name:
				raise ValueError("Indicator dimension source high-water mark is incomplete.")
		else:
			creation = ""
			name = ""
		high_watermarks[role] = {"creation": creation, "name": name}
	return high_watermarks


def _frozen_enumerator_page(
	frappe,
	doctype: str,
	*,
	base_filters: dict[str, Any],
	fields: list[str],
	cursor_creation: str | None,
	cursor_name: str | None,
	high_watermark: dict[str, str],
	limit: int,
) -> list[Any]:
	"""Read a stable lexicographic page without relying on random/hash names."""
	high_creation = str(high_watermark.get("creation") or "")
	high_name = str(high_watermark.get("name") or "")
	if not high_creation and not high_name:
		return []
	if not high_creation or not high_name:
		raise ValueError("Indicator dimension source high-water mark is invalid.")
	if bool(cursor_creation) != bool(cursor_name):
		raise ValueError("Indicator dimension cursor position is incomplete.")
	if cursor_creation and (str(cursor_creation), str(cursor_name)) > (high_creation, high_name):
		raise ValueError("Indicator dimension cursor advanced beyond its frozen source high-water mark.")
	target = min(max(int(limit or 1), 1), 1_001)
	rows: list[Any] = []

	def append_segment(extra_filters: list[list[Any]], order_by: str) -> None:
		remaining = target - len(rows)
		if remaining <= 0:
			return
		filters = _enumerator_filter_list(doctype, base_filters)
		filters.extend(extra_filters)
		rows.extend(
			_governed_source_rows(
				frappe,
				doctype,
				filters=filters,
				fields=fields,
				order_by=order_by,
				limit=remaining,
			)
		)

	if cursor_creation:
		same_creation_filters = [
			[doctype, "creation", "=", cursor_creation],
			[doctype, "name", ">", cursor_name],
		]
		if str(cursor_creation) == high_creation:
			same_creation_filters.append([doctype, "name", "<=", high_name])
		append_segment(same_creation_filters, "name asc")
		if str(cursor_creation) == high_creation:
			return rows
		append_segment(
			[
				[doctype, "creation", ">", cursor_creation],
				[doctype, "creation", "<", high_creation],
			],
			"creation asc, name asc",
		)
	else:
		append_segment(
			[[doctype, "creation", "<", high_creation]],
			"creation asc, name asc",
		)
	append_segment(
		[
			[doctype, "creation", "=", high_creation],
			[doctype, "name", "<=", high_name],
		],
		"name asc",
	)
	return rows


def _frozen_enumerator_exists(
	frappe,
	doctype: str,
	*,
	base_filters: dict[str, Any],
	upper_bound: dict[str, Any],
) -> bool:
	creation = str(upper_bound.get("creation") or "")
	name = str(upper_bound.get("name") or "")
	inclusive = bool(upper_bound.get("inclusive"))
	if not creation or not name:
		return False
	base = _enumerator_filter_list(doctype, base_filters)
	if _governed_source_rows(
		frappe,
		doctype,
		filters=[*base, [doctype, "creation", "<", creation]],
		fields=["name"],
		order_by="creation asc, name asc",
		limit=1,
	):
		return True
	operator = "<=" if inclusive else "<"
	return bool(
		_governed_source_rows(
			frappe,
			doctype,
			filters=[
				*base,
				[doctype, "creation", "=", creation],
				[doctype, "name", operator, name],
			],
			fields=["name"],
			order_by="name asc",
			limit=1,
		)
	)


def _enumerator_filter_list(doctype: str, filters: dict[str, Any]) -> list[list[Any]]:
	items: list[list[Any]] = []
	for fieldname, condition in filters.items():
		if isinstance(condition, (list, tuple)) and len(condition) == 2:
			items.append([doctype, fieldname, condition[0], condition[1]])
		else:
			items.append([doctype, fieldname, "=", condition])
	return items


def _enumerator_filters(
	role_contract: dict[str, Any],
	*,
	period_start: date,
	period_end: date,
	lineage: dict[str, str],
) -> dict[str, Any]:
	date_field = str(role_contract.get("date_field") or "")
	filters = dict(role_contract.get("filters") or {})
	filters[date_field] = [
		"between",
		[
			datetime.combine(period_start, time.min),
			datetime.combine(period_end, time.max),
		],
	]
	lineage_fields = dict(role_contract.get("lineage_fields") or {})
	if set(lineage_fields) != set(_SOURCE_LINEAGE_FIELDS):
		raise ValueError("Indicator enumerator lacks complete lineage fields.")
	for fieldname, value in lineage.items():
		filters[lineage_fields[fieldname]] = value
	return filters


def _coerce_physical_contract(value: dict[str, Any] | str | None) -> dict[str, Any]:
	if value in (None, ""):
		return {}
	try:
		parsed = json.loads(value) if isinstance(value, str) else value
	except ValueError as exc:
		raise ValueError("Indicator physical query contract must be valid JSON.") from exc
	if not isinstance(parsed, dict):
		raise ValueError("Indicator physical query contract must be a JSON object.")
	return parsed


def _enumerator_lineage_values(value: dict[str, str] | None) -> dict[str, str]:
	if not isinstance(value, dict):
		raise ValueError("Indicator dimension enumeration requires frozen source lineage.")
	values = {fieldname: str(value.get(fieldname) or "").strip() for fieldname in _SOURCE_LINEAGE_FIELDS}
	values["mapping_checksum"] = values["mapping_checksum"].lower()
	if any(not item for item in values.values()):
		raise ValueError("Indicator dimension enumeration requires complete source lineage.")
	if len(values["mapping_checksum"]) != 64 or any(
		character not in "0123456789abcdef" for character in values["mapping_checksum"]
	):
		raise ValueError("Indicator dimension enumeration mapping checksum is invalid.")
	return values


def _encode_dimension_cursor(
	role: str,
	creation: str,
	name: str,
	contract_hash: str,
	cursor_scope_hash: str,
	high_watermarks: dict[str, dict[str, str]],
) -> str:
	unsigned = {
		"contract": contract_hash,
		"high_watermarks": high_watermarks,
		"position": {
			"creation": str(creation or ""),
			"name": str(name or ""),
		},
		"role": role,
		"scope": cursor_scope_hash,
		"version": 2,
	}
	payload = _canonical_json(
		{
			**unsigned,
			"signature": _dimension_cursor_signature(unsigned),
		}
	).encode("utf-8")
	return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_dimension_cursor(
	value: str | None,
	*,
	roles: list[tuple[str, dict[str, Any]]],
	contract_hash: str,
	cursor_scope_hash: str,
) -> tuple[
	str | None,
	str | None,
	str | None,
	dict[str, dict[str, str]],
]:
	if not value:
		return None, None, None, {}
	if not isinstance(value, str) or len(value) > 4_096 or not value.isascii():
		raise ValueError("Indicator dimension cursor is invalid.")
	try:
		padding = "=" * (-len(value) % 4)
		payload = json.loads(base64.urlsafe_b64decode(value + padding).decode("utf-8"))
	except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
		raise ValueError("Indicator dimension cursor is invalid.") from exc
	role_names = {role for role, _spec in roles}
	if not isinstance(payload, dict) or set(payload) != {
		"contract",
		"high_watermarks",
		"position",
		"role",
		"scope",
		"signature",
		"version",
	}:
		raise ValueError("Indicator dimension cursor does not match the published query contract.")
	unsigned = {key: payload[key] for key in payload if key != "signature"}
	if (
		payload.get("version") != 2
		or payload.get("role") not in role_names
		or not hmac_compare(str(payload.get("contract") or ""), contract_hash)
		or not hmac_compare(str(payload.get("scope") or ""), cursor_scope_hash)
		or not hmac_compare(
			str(payload.get("signature") or ""),
			_dimension_cursor_signature(unsigned),
		)
	):
		raise ValueError("Indicator dimension cursor does not match the published query contract.")
	position = payload.get("position")
	high_watermarks = payload.get("high_watermarks")
	if (
		not isinstance(position, dict)
		or set(position) != {"creation", "name"}
		or not isinstance(high_watermarks, dict)
		or set(high_watermarks) != role_names
	):
		raise ValueError("Indicator dimension cursor lacks its frozen source contract.")
	normalized_watermarks: dict[str, dict[str, str]] = {}
	for role in sorted(role_names):
		watermark = high_watermarks.get(role)
		if not isinstance(watermark, dict) or set(watermark) != {"creation", "name"}:
			raise ValueError("Indicator dimension cursor high-water mark is invalid.")
		high_creation = _validated_cursor_creation(watermark.get("creation"), allow_empty=True)
		high_name = _validated_cursor_name(watermark.get("name"), allow_empty=True)
		if bool(high_creation) != bool(high_name):
			raise ValueError("Indicator dimension cursor high-water mark is incomplete.")
		normalized_watermarks[role] = {
			"creation": high_creation,
			"name": high_name,
		}
	creation = _validated_cursor_creation(position.get("creation"), allow_empty=True)
	name = _validated_cursor_name(position.get("name"), allow_empty=True)
	if bool(creation) != bool(name):
		raise ValueError("Indicator dimension cursor record position is incomplete.")
	role_watermark = normalized_watermarks[str(payload["role"])]
	if creation and (creation, name) > (
		role_watermark["creation"],
		role_watermark["name"],
	):
		raise ValueError("Indicator dimension cursor position exceeds its frozen source.")
	return str(payload["role"]), creation or None, name or None, normalized_watermarks


def _dimension_cursor_signature(payload: dict[str, Any]) -> str:
	import hmac

	import frappe

	signing_key = str((getattr(frappe, "conf", {}) or {}).get("encryption_key") or "")
	if not signing_key:
		raise ValueError("Site encryption_key is required for indicator dimension cursors.")
	return hmac.new(
		signing_key.encode("utf-8"),
		_canonical_json(payload).encode("utf-8"),
		hashlib.sha256,
	).hexdigest()


def _validated_cursor_creation(value: Any, *, allow_empty: bool) -> str:
	creation = str(value or "")
	if not creation and allow_empty:
		return ""
	try:
		datetime.fromisoformat(creation.replace("Z", "+00:00"))
	except ValueError as exc:
		raise ValueError("Indicator dimension cursor creation watermark is invalid.") from exc
	if len(creation) > 40:
		raise ValueError("Indicator dimension cursor creation watermark is invalid.")
	return creation


def _validated_cursor_name(value: Any, *, allow_empty: bool) -> str:
	name = str(value or "")
	if not name and allow_empty:
		return ""
	if len(name) > 140 or any(ord(character) < 32 for character in name):
		raise ValueError("Indicator dimension cursor record identity is invalid.")
	return name


def _adapt_legacy_dimension_filters(raw: Any, available_fields: set[str]) -> Any:
	"""Use the same source-field compatibility adapters in every query path."""
	if not isinstance(raw, dict):
		return raw
	adapted = dict(raw)
	for legacy, canonical in (
		("medical_staff", "responsible_staff"),
		("diagnosis_code", "disease"),
		("disease_code", "disease"),
		("procedure_code", "surgery"),
		("drg_code", "drg"),
		("dip_code", "dip"),
	):
		if legacy not in adapted or legacy in available_fields or canonical not in available_fields:
			continue
		if canonical in adapted:
			raise ValueError(f"Indicator filters cannot specify both {legacy} and {canonical}.")
		adapted[canonical] = adapted.pop(legacy)
	return adapted


def _formula(parameters: dict[str, Any]) -> dict[str, Any]:
	raw = parameters.get("formula") or parameters.get("formula_json") or parameters
	if isinstance(raw, str):
		raw = json.loads(raw)
	return _mapping(raw, "formula")


def _mapping(value: Any, label: str) -> dict[str, Any]:
	if not isinstance(value, dict):
		raise ValueError(f"Indicator {label} must be a JSON object.")
	return value


def _decimal(value: Decimal | int | float | str | Any, label: str) -> Decimal:
	try:
		converted = Decimal(str(value))
	except (InvalidOperation, TypeError, ValueError) as exc:
		raise ValueError(f"Invalid indicator {label}: {value!r}") from exc
	if not converted.is_finite():
		raise ValueError(f"Indicator {label} must be finite.")
	return converted


def _canonical_json(value: Any) -> str:
	return json.dumps(
		value,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
		default=str,
	)
