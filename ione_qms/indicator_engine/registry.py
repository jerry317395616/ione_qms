from __future__ import annotations

from collections.abc import Callable

from ione_qms.indicator_engine.calculator import BaseIndicatorCalculator, RecordAggregateCalculator

_INDICATORS: dict[str, type[BaseIndicatorCalculator]] = {
	RecordAggregateCalculator.code: RecordAggregateCalculator,
}


def register_indicator(
	code: str,
) -> Callable[[type[BaseIndicatorCalculator]], type[BaseIndicatorCalculator]]:
	normalized = _normalize(code)

	def decorator(
		calculator: type[BaseIndicatorCalculator],
	) -> type[BaseIndicatorCalculator]:
		if normalized in _INDICATORS:
			raise ValueError(f"Duplicate indicator calculator registration: {normalized}")
		if not issubclass(calculator, BaseIndicatorCalculator):
			raise TypeError("Registered indicator calculators must inherit BaseIndicatorCalculator.")
		calculator.code = normalized
		_INDICATORS[normalized] = calculator
		return calculator

	return decorator


def get_indicator(code: str) -> type[BaseIndicatorCalculator] | None:
	return _INDICATORS.get(_normalize(code, required=False))


def registered_indicators() -> tuple[str, ...]:
	return tuple(sorted(_INDICATORS))


def _normalize(code: str, *, required: bool = True) -> str:
	normalized = str(code or "").strip().upper()
	if required and not normalized:
		raise ValueError("Indicator calculator code is required.")
	return normalized
