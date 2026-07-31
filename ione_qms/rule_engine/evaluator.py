from __future__ import annotations

import re
from typing import Any

from ione_qms.rule_engine.models import EvidenceItem, RuleEvaluation, RuleResult
from ione_qms.rule_engine.operators import MISSING, OPERATORS, compare

FIELD_PATH_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*$")
MAX_CONDITION_DEPTH = 12
MAX_CONDITION_NODES = 500


def evaluate_rule_definition(
	condition: dict[str, Any] | list[Any],
	context: dict[str, Any],
	exclusions: dict[str, Any] | list[Any] | None = None,
) -> RuleEvaluation:
	try:
		_validate_tree(condition)
		if exclusions:
			_validate_tree(exclusions)
			exclusion_evidence: list[EvidenceItem] = []
			exclusion_missing: list[str] = []
			exclusion_match = _evaluate_node(
				exclusions,
				context,
				exclusion_evidence,
				exclusion_missing,
			)
			if exclusion_match is True:
				return RuleEvaluation(
					result=RuleResult.EXCLUDED,
					reason="The encounter matched an approved exclusion.",
					evidence=exclusion_evidence,
					missing_fields=exclusion_missing,
				)
			if exclusion_match is None:
				return RuleEvaluation(
					result=RuleResult.INSUFFICIENT_DATA,
					reason=("Required exclusion data is missing; no clinical finding was produced."),
					evidence=exclusion_evidence,
					missing_fields=exclusion_missing,
				)

		evidence: list[EvidenceItem] = []
		missing_fields: list[str] = []
		matched = _evaluate_node(condition, context, evidence, missing_fields)
		if matched is None:
			return RuleEvaluation(
				result=RuleResult.INSUFFICIENT_DATA,
				reason="Required source data is missing; no clinical finding was produced.",
				evidence=evidence,
				missing_fields=missing_fields,
			)
		return RuleEvaluation(
			result=RuleResult.FAILED if matched else RuleResult.PASSED,
			reason=("The rule condition matched." if matched else "The rule condition did not match."),
			evidence=evidence,
		)
	except Exception as exc:
		return RuleEvaluation(
			result=RuleResult.ERROR,
			reason="The deterministic rule could not be evaluated.",
			error=f"{type(exc).__name__}: deterministic evaluation failed",
		)


def validate_rule_definition_schema(node: dict[str, Any] | list[Any]) -> None:
	_validate_tree(node)

	def validate(value: Any) -> None:
		if isinstance(value, list):
			if not value:
				raise ValueError("Rule arrays cannot be empty")
			for item in value:
				validate(item)
			return
		if not isinstance(value, dict):
			raise ValueError("A rule node must be an object or array")
		group_keys = [key for key in ("all", "any", "none", "not") if key in value]
		if group_keys:
			if len(group_keys) != 1 or any(key in value for key in ("field", "operator", "value")):
				raise ValueError("A group node must contain exactly one logical operator")
			key = group_keys[0]
			children = value[key]
			if key == "not":
				validate(children)
				return
			if not isinstance(children, list) or not children:
				raise ValueError(f"Logical group '{key}' requires a non-empty array")
			for child in children:
				validate(child)
			return
		field_path = str(value.get("field") or "")
		operator = str(value.get("operator") or "")
		if not FIELD_PATH_PATTERN.fullmatch(field_path) or "__" in field_path:
			raise ValueError(f"Unsafe field path: {field_path!r}")
		if operator not in OPERATORS:
			raise ValueError(f"Unsupported rule operator: {operator}")
		unexpected = set(value).difference({"field", "operator", "value"})
		if unexpected:
			raise ValueError("Unexpected rule fields: " + ", ".join(sorted(unexpected)))

	validate(node)


def _evaluate_node(
	node: dict[str, Any] | list[Any],
	context: dict[str, Any],
	evidence: list[EvidenceItem],
	missing_fields: list[str],
) -> bool | None:
	if isinstance(node, list):
		return _all_state([_evaluate_node(item, context, evidence, missing_fields) for item in node])
	if not isinstance(node, dict):
		raise ValueError("A rule node must be an object or array")

	group_keys = [key for key in ("all", "any", "none", "not") if key in node]
	if group_keys:
		if len(group_keys) != 1 or any(key in node for key in ("field", "operator")):
			raise ValueError("A group node must contain exactly one logical operator")
		key = group_keys[0]
		children = node[key]
		if key == "not":
			return _not_state(_evaluate_node(children, context, evidence, missing_fields))
		if not isinstance(children, list) or not children:
			raise ValueError(f"Logical group '{key}' requires a non-empty array")
		results = [_evaluate_node(child, context, evidence, missing_fields) for child in children]
		if key == "all":
			return _all_state(results)
		if key == "any":
			return _any_state(results)
		return _not_state(_any_state(results))

	field_path = str(node.get("field") or "")
	operator = str(node.get("operator") or "")
	if not FIELD_PATH_PATTERN.fullmatch(field_path) or "__" in field_path:
		raise ValueError(f"Unsafe field path: {field_path!r}")
	if not operator:
		raise ValueError("A leaf rule node requires an operator")

	actual = _resolve_field(context, field_path)
	if actual is MISSING:
		if field_path not in missing_fields:
			missing_fields.append(field_path)
		return None
	expected = node.get("value")
	matched = compare(operator, actual, expected)
	evidence.append(
		EvidenceItem(
			field=field_path,
			operator=operator,
			expected=expected,
			actual=actual,
			matched=matched,
		)
	)
	return matched


def _all_state(values: list[bool | None]) -> bool | None:
	if any(value is False for value in values):
		return False
	if any(value is None for value in values):
		return None
	return True


def _any_state(values: list[bool | None]) -> bool | None:
	if any(value is True for value in values):
		return True
	if any(value is None for value in values):
		return None
	return False


def _not_state(value: bool | None) -> bool | None:
	return None if value is None else not value


def _resolve_field(context: dict[str, Any], field_path: str) -> Any:
	current: Any = context
	for part in field_path.split("."):
		if not isinstance(current, dict) or part not in current:
			return MISSING
		current = current[part]
	return current


def _validate_tree(node: dict[str, Any] | list[Any]) -> None:
	counter = [0]

	def walk(value: Any, depth: int) -> None:
		counter[0] += 1
		if counter[0] > MAX_CONDITION_NODES:
			raise ValueError("Rule condition exceeds the maximum node count")
		if depth > MAX_CONDITION_DEPTH:
			raise ValueError("Rule condition exceeds the maximum nesting depth")
		if isinstance(value, list):
			for item in value:
				walk(item, depth + 1)
		elif isinstance(value, dict):
			for child in value.values():
				if isinstance(child, (list, dict)):
					walk(child, depth + 1)

	walk(node, 0)
