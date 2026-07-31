from __future__ import annotations

import math
from collections.abc import Callable, Collection
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

MISSING = object()


def is_empty(value: Any) -> bool:
	return value is None or value == "" or value == [] or value == {} or value == ()


def compare(operator: str, actual: Any, expected: Any = None) -> bool:
	try:
		return OPERATORS[operator](actual, expected)
	except KeyError as exc:
		raise ValueError(f"Unsupported rule operator: {operator}") from exc


def _eq(actual: Any, expected: Any) -> bool:
	return actual == expected


def _ne(actual: Any, expected: Any) -> bool:
	return actual != expected


def _ordered(actual: Any, expected: Any, operation: Callable[[Any, Any], bool]) -> bool:
	left, right = _coerce_pair(actual, expected)
	return operation(left, right)


def _in(actual: Any, expected: Any) -> bool:
	if isinstance(expected, Collection) and not isinstance(expected, (str, bytes, dict)):
		return actual in expected
	raise ValueError("Operator 'in' requires an array value")


def _not_in(actual: Any, expected: Any) -> bool:
	return not _in(actual, expected)


def _contains(actual: Any, expected: Any) -> bool:
	if isinstance(actual, (str, bytes, list, tuple, set, frozenset, dict)):
		return expected in actual
	raise ValueError("Operator 'contains' requires text, an array, or an object")


def _not_contains(actual: Any, expected: Any) -> bool:
	return not _contains(actual, expected)


def _between(actual: Any, expected: Any) -> bool:
	if not isinstance(expected, (list, tuple)) or len(expected) != 2:
		raise ValueError("Operator 'between' requires a two-item array")
	value, lower = _coerce_pair(actual, expected[0])
	value_again, upper = _coerce_pair(actual, expected[1])
	return lower <= value and value_again <= upper


def _coerce_pair(left: Any, right: Any) -> tuple[Any, Any]:
	if isinstance(left, (datetime, date)) or isinstance(right, (datetime, date)):
		return _datetime(left), _datetime(right)
	if _looks_numeric(left) and _looks_numeric(right):
		return _decimal(left), _decimal(right)
	return left, right


def _datetime(value: Any) -> datetime:
	if isinstance(value, datetime):
		return value
	if isinstance(value, date):
		return datetime.combine(value, time.min)
	if isinstance(value, str):
		normalized = value.strip()
		if normalized.endswith("Z"):
			normalized = normalized[:-1] + "+00:00"
		try:
			return datetime.fromisoformat(normalized)
		except ValueError as exc:
			raise ValueError(f"Invalid datetime value: {value!r}") from exc
	raise ValueError(f"Invalid datetime value: {value!r}")


def _looks_numeric(value: Any) -> bool:
	if isinstance(value, bool) or value is None:
		return False
	if isinstance(value, (int, float, Decimal)):
		return True
	if isinstance(value, str):
		try:
			Decimal(value.strip())
			return True
		except InvalidOperation:
			return False
	return False


def _decimal(value: Any) -> Decimal:
	if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
		raise ValueError("NaN and infinity are not valid clinical rule values")
	try:
		return Decimal(str(value))
	except InvalidOperation as exc:
		raise ValueError(f"Invalid numeric value: {value!r}") from exc


OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
	"=": _eq,
	"==": _eq,
	"eq": _eq,
	"!=": _ne,
	"ne": _ne,
	">": lambda actual, expected: _ordered(actual, expected, lambda left, right: left > right),
	">=": lambda actual, expected: _ordered(actual, expected, lambda left, right: left >= right),
	"<": lambda actual, expected: _ordered(actual, expected, lambda left, right: left < right),
	"<=": lambda actual, expected: _ordered(actual, expected, lambda left, right: left <= right),
	"in": _in,
	"not_in": _not_in,
	"contains": _contains,
	"not_contains": _not_contains,
	"between": _between,
	"is_empty": lambda actual, _expected: is_empty(actual),
	"not_empty": lambda actual, _expected: not is_empty(actual),
}
