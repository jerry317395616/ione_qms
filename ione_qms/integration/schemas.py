from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from frappe.utils import get_datetime, get_system_timezone

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")
EVENT_TYPE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,99}$")
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class IncomingClinicalEvent:
	event_type: str
	event_time: datetime
	source_record_type: str
	source_record_id: str
	source_version: str
	payload: dict[str, Any]
	patient_reference: str | None = None
	encounter_reference: str | None = None
	department_reference: str | None = None

	@classmethod
	def from_payload(cls, payload: dict[str, Any]) -> IncomingClinicalEvent:
		if not isinstance(payload, dict):
			raise ValueError("Clinical event payload must be a JSON object")
		event_type = _identifier(payload.get("event_type"), EVENT_TYPE_PATTERN, "event_type")
		source_record_type = _identifier(
			payload.get("source_record_type"),
			IDENTIFIER_PATTERN,
			"source_record_type",
		)
		source_record_id = _identifier(
			payload.get("source_record_id"),
			IDENTIFIER_PATTERN,
			"source_record_id",
		)
		source_version = _identifier(
			payload.get("source_version") or "0",
			IDENTIFIER_PATTERN,
			"source_version",
		)
		if not payload.get("event_time"):
			raise ValueError("event_time is required")
		event_time = get_datetime(payload.get("event_time"))
		if not event_time:
			raise ValueError("event_time is required")
		if event_time.tzinfo is not None:
			event_time = event_time.astimezone(ZoneInfo(get_system_timezone())).replace(tzinfo=None)
		domain_payload = payload.get("payload")
		if not isinstance(domain_payload, dict):
			raise ValueError("payload must be a JSON object")
		return cls(
			event_type=event_type,
			event_time=event_time,
			source_record_type=source_record_type,
			source_record_id=source_record_id,
			source_version=source_version,
			payload=domain_payload,
			patient_reference=_optional_identifier(payload.get("patient_reference")),
			encounter_reference=_optional_identifier(payload.get("encounter_reference")),
			department_reference=_optional_identifier(payload.get("department_reference")),
		)


def validate_payload_size(raw_payload: bytes) -> None:
	if len(raw_payload) > MAX_PAYLOAD_BYTES:
		raise ValueError(f"Payload exceeds the {MAX_PAYLOAD_BYTES}-byte limit")


def _identifier(value: Any, pattern: re.Pattern[str], fieldname: str) -> str:
	normalized = str(value or "").strip()
	if not pattern.fullmatch(normalized):
		raise ValueError(f"Invalid {fieldname}")
	return normalized


def _optional_identifier(value: Any) -> str | None:
	if value in (None, ""):
		return None
	return _identifier(value, IDENTIFIER_PATTERN, "reference")
