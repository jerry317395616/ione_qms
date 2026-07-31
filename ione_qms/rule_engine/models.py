from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RuleResult(StrEnum):
	PASSED = "Passed"
	FAILED = "Failed"
	EXCLUDED = "Excluded"
	INSUFFICIENT_DATA = "Insufficient Data"
	ERROR = "Error"


@dataclass(frozen=True)
class EvidenceItem:
	field: str
	operator: str
	expected: Any
	actual: Any
	matched: bool

	def as_dict(self) -> dict[str, Any]:
		return {
			"field": self.field,
			"operator": self.operator,
			"expected": self.expected,
			"actual": self.actual,
			"matched": self.matched,
		}


@dataclass
class RuleEvaluation:
	result: RuleResult
	reason: str
	evidence: list[EvidenceItem] = field(default_factory=list)
	missing_fields: list[str] = field(default_factory=list)
	error: str | None = None

	def as_dict(self) -> dict[str, Any]:
		return {
			"result": self.result.value,
			"reason": self.reason,
			"evidence": [item.as_dict() for item in self.evidence],
			"missing_fields": sorted(set(self.missing_fields)),
			"error": self.error,
		}
