from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from ione_qms.rule_engine.models import RuleEvaluation

_RULE_REGISTRY: dict[str, type[BaseQCRule]] = {}


class BaseQCRule(ABC):
	code: str

	@abstractmethod
	def evaluate(self, context: dict[str, Any]) -> RuleEvaluation:
		"""Evaluate a trusted, code-reviewed clinical rule."""


def register_rule(code: str) -> Callable[[type[BaseQCRule]], type[BaseQCRule]]:
	normalized = code.strip().upper()
	if not normalized:
		raise ValueError("Rule code is required")

	def decorator(rule_class: type[BaseQCRule]) -> type[BaseQCRule]:
		if normalized in _RULE_REGISTRY:
			raise ValueError(f"Duplicate rule registration: {normalized}")
		if not issubclass(rule_class, BaseQCRule):
			raise TypeError("Registered rules must inherit BaseQCRule")
		rule_class.code = normalized
		_RULE_REGISTRY[normalized] = rule_class
		return rule_class

	return decorator


def get_rule(code: str) -> type[BaseQCRule] | None:
	return _RULE_REGISTRY.get(code.strip().upper())


def registered_rules() -> tuple[str, ...]:
	return tuple(sorted(_RULE_REGISTRY))
