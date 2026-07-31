from __future__ import annotations

from typing import Any

__all__ = ["receive_clinical_event"]


def __getattr__(name: str) -> Any:
	"""Load public integration entry points without creating package import cycles."""
	if name == "receive_clinical_event":
		from ione_qms.integration.service import receive_clinical_event

		return receive_clinical_event
	raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
