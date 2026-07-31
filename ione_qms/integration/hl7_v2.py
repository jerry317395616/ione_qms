from __future__ import annotations

import json
import re
import unicodedata
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_HL7_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_HL7_MESSAGES_PER_BATCH = 500
MAX_HL7_SEGMENTS_PER_MESSAGE = 4096
MAX_HL7_FIELDS_PER_SEGMENT = 250
MAX_HL7_COMPONENTS_PER_FIELD = 64
MAX_HL7_OUTPUT_SCALARS = 50_000
HL7_V2_MEDIA_TYPES = frozenset({"application/hl7-v2", "application/hl7-v2+er7"})

_CONTROL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")
_MESSAGE_COMPONENT = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,19}$")
_SEGMENT_ID = re.compile(r"^[A-Z0-9]{3}$")
_SEQUENCE_NUMBER = re.compile(r"^[0-9]{1,20}$")
_VERSION = re.compile(r"^2\.[0-9](?:\.[0-9])?$")
_TIMESTAMP = re.compile(
	r"^(?P<digits>[0-9]{14})(?P<fraction>\.[0-9]{1,6})?"
	r"(?P<offset>[+-](?:(?:0[0-9]|1[0-3])[0-5][0-9]|1400))?$"
)
_ALLOWED_ENCODINGS = frozenset({"utf-8", "gb18030"})


class HL7V2ContractError(ValueError):
	"""A message-local HL7 v2 transport or envelope contract failure."""


def normalize_hl7_v2_batch(
	raw_payload: bytes,
	*,
	source_timezone: str,
	encoding: str = "utf-8",
) -> list[dict[str, object]]:
	"""Normalize one bounded HL7 v2 batch into IONE clinical-event envelopes.

	The parser handles an exact MLLP frame sequence or an unframed text batch.
	It deliberately does not infer patient, encounter, department, or clinical
	code semantics. Hospital-reviewed Integration Mapping versions consume the
	stable ``payload.hl7`` field paths produced here.
	"""
	if type(raw_payload) is not bytes:
		raise TypeError("HL7 v2 payload must be bytes")
	if not raw_payload:
		raise HL7V2ContractError("HL7 v2 payload is empty")
	if len(raw_payload) > MAX_HL7_PAYLOAD_BYTES:
		raise HL7V2ContractError("HL7 v2 payload exceeds the 2 MiB ingress limit")
	timezone = _source_timezone(source_timezone)
	if not isinstance(encoding, str) or encoding != encoding.strip():
		raise HL7V2ContractError("HL7 v2 encoding must be explicitly utf-8 or gb18030")
	codec = encoding
	if codec not in _ALLOWED_ENCODINGS:
		raise HL7V2ContractError("HL7 v2 encoding must be explicitly utf-8 or gb18030")
	frames = _mllp_frames(raw_payload)
	messages: list[list[str]] = []
	for frame in frames:
		text = _decode_frame(frame, codec)
		messages.extend(_split_messages(text))
		if len(messages) > MAX_HL7_MESSAGES_PER_BATCH:
			raise HL7V2ContractError("HL7 v2 batch exceeds 500 messages")
	if not messages:
		raise HL7V2ContractError("HL7 v2 batch contains no message")
	return [_normalize_message(segments, timezone) for segments in messages]


def normalize_hl7_v2_transport_options(value: object) -> dict[str, str]:
	"""Validate the exact non-secret options accepted by signed HL7 ingress."""
	if isinstance(value, str):
		try:
			value = json.loads(value, object_pairs_hook=_unique_json_object)
		except ValueError as exc:
			raise HL7V2ContractError("HL7 v2 connector_options must be valid JSON") from exc
	if not isinstance(value, dict) or set(value) != {"source_timezone", "encoding"}:
		raise HL7V2ContractError("HL7 v2 connector_options must contain exactly source_timezone and encoding")
	timezone = value.get("source_timezone")
	_source_timezone(timezone)
	encoding = value.get("encoding")
	if not isinstance(encoding, str) or encoding != encoding.strip():
		raise HL7V2ContractError("HL7 v2 encoding must be explicitly utf-8 or gb18030")
	if encoding not in _ALLOWED_ENCODINGS:
		raise HL7V2ContractError("HL7 v2 encoding must be explicitly utf-8 or gb18030")
	return {"source_timezone": timezone, "encoding": encoding}


def normalize_hl7_v2_content_type(value: object, *, encoding: str) -> str:
	"""Validate the exact media type and charset used to decode signed bytes."""
	if not isinstance(value, str) or not value or len(value) > 200 or value != value.strip():
		raise HL7V2ContractError("HL7 v2 Content-Type is invalid")
	if encoding not in _ALLOWED_ENCODINGS:
		raise HL7V2ContractError("HL7 v2 encoding must be explicitly utf-8 or gb18030")
	parts = [part.strip() for part in value.split(";")]
	media_type = parts[0].casefold()
	if media_type not in HL7_V2_MEDIA_TYPES:
		raise HL7V2ContractError("HL7 v2 Content-Type media type is not supported")
	parameters: dict[str, str] = {}
	for raw_parameter in parts[1:]:
		if not raw_parameter or "=" not in raw_parameter:
			raise HL7V2ContractError("HL7 v2 Content-Type parameter is invalid")
		key, parameter_value = (item.strip() for item in raw_parameter.split("=", 1))
		key = key.casefold()
		if key != "charset" or key in parameters:
			raise HL7V2ContractError("HL7 v2 Content-Type accepts exactly one charset parameter")
		if len(parameter_value) >= 2 and parameter_value[0] == parameter_value[-1] == '"':
			parameter_value = parameter_value[1:-1]
		if not parameter_value or not re.fullmatch(r"[A-Za-z0-9._-]+", parameter_value):
			raise HL7V2ContractError("HL7 v2 Content-Type charset is invalid")
		parameters[key] = parameter_value.casefold()
	if parameters.get("charset") != encoding:
		raise HL7V2ContractError("HL7 v2 Content-Type must declare the endpoint's configured charset")
	return media_type


def normalize_hl7_v2_batch_size(value: object) -> int:
	"""Return the exact bounded message count accepted by one HTTP request."""
	if type(value) is not int or not 1 <= value <= MAX_HL7_MESSAGES_PER_BATCH:
		raise HL7V2ContractError(
			f"HL7 v2 endpoint batch_size must be an integer from 1 through {MAX_HL7_MESSAGES_PER_BATCH}"
		)
	return value


def _mllp_frames(raw_payload: bytes) -> list[bytes]:
	has_start = b"\x0b" in raw_payload
	has_end = b"\x1c" in raw_payload
	if not has_start and not has_end:
		return [raw_payload]
	if not has_start or not has_end:
		raise HL7V2ContractError("HL7 v2 MLLP framing is incomplete")
	frames: list[bytes] = []
	cursor = 0
	size = len(raw_payload)
	while cursor < size:
		while cursor < size and raw_payload[cursor : cursor + 1] in {b"\r", b"\n"}:
			cursor += 1
		if cursor == size:
			break
		if raw_payload[cursor : cursor + 1] != b"\x0b":
			raise HL7V2ContractError("HL7 v2 MLLP batch contains data outside a frame")
		end = raw_payload.find(b"\x1c\r", cursor + 1)
		if end < 0:
			raise HL7V2ContractError("HL7 v2 MLLP frame has no canonical terminator")
		frame = raw_payload[cursor + 1 : end]
		if not frame:
			raise HL7V2ContractError("HL7 v2 MLLP frame is empty")
		if b"\x0b" in frame or b"\x1c" in frame:
			raise HL7V2ContractError("HL7 v2 MLLP frame contains nested framing bytes")
		frames.append(frame)
		if len(frames) > MAX_HL7_MESSAGES_PER_BATCH:
			raise HL7V2ContractError("HL7 v2 MLLP batch exceeds 500 frames")
		cursor = end + 2
	return frames


def _decode_frame(frame: bytes, encoding: str) -> str:
	try:
		text = frame.decode(encoding, errors="strict")
	except UnicodeDecodeError as exc:
		raise HL7V2ContractError("HL7 v2 frame does not match its configured encoding") from exc
	if any(unicodedata.category(character) == "Cc" and character not in {"\r", "\n"} for character in text):
		raise HL7V2ContractError("HL7 v2 frame contains a forbidden control character")
	return text


def _split_messages(text: str) -> list[list[str]]:
	normalized = text.replace("\r\n", "\r").replace("\n", "\r")
	segments = normalized.split("\r")
	while segments and not segments[-1]:
		segments.pop()
	if any(not segment for segment in segments):
		raise HL7V2ContractError("HL7 v2 message contains an empty segment")
	messages: list[list[str]] = []
	current: list[str] = []
	for segment in segments:
		if segment.startswith("MSH"):
			if current:
				messages.append(current)
			current = [segment]
		elif not current:
			raise HL7V2ContractError("HL7 v2 message must begin with MSH")
		else:
			current.append(segment)
		if len(current) > MAX_HL7_SEGMENTS_PER_MESSAGE:
			raise HL7V2ContractError("HL7 v2 message exceeds 4,096 segments")
	if current:
		messages.append(current)
	return messages


def _normalize_message(
	segments: list[str],
	source_timezone: ZoneInfo,
) -> dict[str, object]:
	if not segments or not segments[0].startswith("MSH"):
		raise HL7V2ContractError("HL7 v2 message must begin with MSH")
	msh = segments[0]
	if len(msh) < 8:
		raise HL7V2ContractError("HL7 v2 MSH segment is incomplete")
	field_separator = msh[3]
	if (
		not field_separator.isprintable()
		or field_separator.isspace()
		or field_separator.isalnum()
		or field_separator in {"\\", "\x0b", "\x1c"}
	):
		raise HL7V2ContractError("HL7 v2 MSH-1 field separator is invalid")
	msh_parts = msh.split(field_separator)
	if len(msh_parts) < 12 or len(msh_parts) > MAX_HL7_FIELDS_PER_SEGMENT:
		raise HL7V2ContractError("HL7 v2 MSH segment has an invalid field count")
	encoding_characters = msh_parts[1]
	if (
		len(encoding_characters) not in {4, 5}
		or len(set(encoding_characters)) != len(encoding_characters)
		or field_separator in encoding_characters
		or any(not character.isprintable() or character.isalnum() for character in encoding_characters)
	):
		raise HL7V2ContractError("HL7 v2 MSH-2 encoding characters are invalid")
	component_separator = encoding_characters[0]
	message_code, trigger_event, message_structure = _message_type(
		_field(msh_parts, 9),
		component_separator,
	)
	control_id = _canonical_identifier(_field(msh_parts, 10), "MSH-10 message control ID")
	version = _hl7_version(_field(msh_parts, 12))
	sequence = _field(msh_parts, 13)
	if sequence and not _SEQUENCE_NUMBER.fullmatch(sequence):
		raise HL7V2ContractError("HL7 v2 MSH-13 sequence number is invalid")
	event_timestamp = _field(msh_parts, 7) or _event_timestamp(segments, field_separator)
	event_time = _hl7_timestamp(event_timestamp, source_timezone)

	segment_payload: dict[str, object] = {}
	segment_order: list[str] = []
	segment_counts: dict[str, int] = {}
	scalar_count = 0
	for line in segments:
		segment_id = line[:3]
		if not _SEGMENT_ID.fullmatch(segment_id):
			raise HL7V2ContractError("HL7 v2 segment identifier is invalid")
		if len(line) < 4 or line[3] != field_separator:
			raise HL7V2ContractError("HL7 v2 segment does not use the MSH field separator")
		parts = line.split(field_separator)
		if len(parts) > MAX_HL7_FIELDS_PER_SEGMENT + 1:
			raise HL7V2ContractError("HL7 v2 segment exceeds 250 fields")
		segment_counts[segment_id] = segment_counts.get(segment_id, 0) + 1
		instance = (
			segment_id if segment_counts[segment_id] == 1 else f"{segment_id}_{segment_counts[segment_id]}"
		)
		values, added_scalars = _segment_values(
			segment_id,
			parts,
			field_separator,
			component_separator,
		)
		scalar_count += added_scalars
		if scalar_count > MAX_HL7_OUTPUT_SCALARS:
			raise HL7V2ContractError("HL7 v2 normalized output exceeds 50,000 scalar fields")
		segment_payload[instance] = values
		segment_order.append(instance)

	hl7_payload: dict[str, object] = {
		"version": version,
		"message_code": message_code,
		"trigger_event": trigger_event,
		"message_structure": message_structure,
		"segment_order": segment_order,
		**segment_payload,
	}
	envelope: dict[str, object] = {
		"event_type": ".".join(part for part in ("HL7", message_code, trigger_event) if part),
		"event_time": event_time.isoformat(),
		"source_record_type": ".".join(part for part in ("HL7v2", message_code, trigger_event) if part),
		"source_record_id": control_id,
		"source_version": sequence or "0",
		"payload": {"hl7": hl7_payload},
	}
	encoded = json.dumps(
		envelope,
		ensure_ascii=False,
		sort_keys=True,
		separators=(",", ":"),
	).encode()
	if len(encoded) > MAX_HL7_PAYLOAD_BYTES:
		raise HL7V2ContractError("HL7 v2 normalized event exceeds the 2 MiB ingress limit")
	return envelope


def _segment_values(
	segment_id: str,
	parts: list[str],
	field_separator: str,
	component_separator: str,
) -> tuple[dict[str, str], int]:
	if segment_id == "MSH":
		fields = [field_separator, *parts[1:]]
	else:
		fields = parts[1:]
	output: dict[str, str] = {}
	scalars = 0
	for index, value in enumerate(fields, start=1):
		output[f"field_{index}"] = value
		scalars += 1
		components = value.split(component_separator)
		if len(components) > MAX_HL7_COMPONENTS_PER_FIELD:
			raise HL7V2ContractError("HL7 v2 field exceeds 64 components")
		if len(components) > 1:
			for component_index, component in enumerate(components, start=1):
				output[f"field_{index}_component_{component_index}"] = component
				scalars += 1
	return output, scalars


def _field(msh_parts: list[str], field_number: int) -> str:
	index = field_number - 1
	return msh_parts[index] if index < len(msh_parts) else ""


def _event_timestamp(segments: list[str], field_separator: str) -> str:
	for line in segments[1:]:
		if line.startswith(f"EVN{field_separator}"):
			parts = line.split(field_separator)
			return parts[2] if len(parts) > 2 else ""
	return ""


def _message_type(value: str, component_separator: str) -> tuple[str, str, str]:
	components = value.split(component_separator)
	if not components or not _MESSAGE_COMPONENT.fullmatch(components[0]):
		raise HL7V2ContractError("HL7 v2 MSH-9 message code is invalid")
	if len(components) > 3:
		raise HL7V2ContractError("HL7 v2 MSH-9 contains too many components")
	trigger = components[1] if len(components) > 1 else ""
	structure = components[2] if len(components) > 2 else ""
	for label, component in (("trigger event", trigger), ("message structure", structure)):
		if component and not _MESSAGE_COMPONENT.fullmatch(component):
			raise HL7V2ContractError(f"HL7 v2 MSH-9 {label} is invalid")
	return components[0], trigger, structure


def _canonical_identifier(value: str, label: str) -> str:
	if not _CONTROL_ID.fullmatch(value):
		raise HL7V2ContractError(f"HL7 v2 {label} is invalid")
	return value


def _hl7_version(value: str) -> str:
	if not _VERSION.fullmatch(value):
		raise HL7V2ContractError("HL7 v2 MSH-12 version is invalid")
	parts = tuple(int(part) for part in value.split("."))
	if parts < (2, 3) or parts > (2, 9, 1):
		raise HL7V2ContractError("HL7 v2 MSH-12 version is outside the supported 2.3-2.9.1 range")
	return value


def _hl7_timestamp(value: str, source_timezone: ZoneInfo) -> datetime:
	match = _TIMESTAMP.fullmatch(value)
	if not match:
		raise HL7V2ContractError(
			"HL7 v2 event timestamp must contain YYYYMMDDHHMMSS and an optional UTC offset"
		)
	fraction = match.group("fraction") or ""
	try:
		parsed = datetime.strptime(
			match.group("digits") + fraction,
			"%Y%m%d%H%M%S" + (".%f" if fraction else ""),
		)
	except ValueError as exc:
		raise HL7V2ContractError("HL7 v2 event timestamp is not a valid calendar time") from exc
	offset = match.group("offset")
	if offset:
		try:
			return datetime.strptime(
				match.group("digits") + fraction + offset,
				"%Y%m%d%H%M%S" + (".%f" if fraction else "") + "%z",
			)
		except ValueError as exc:
			raise HL7V2ContractError("HL7 v2 event timestamp UTC offset is invalid") from exc
	candidates = []
	for fold in (0, 1):
		candidate = parsed.replace(tzinfo=source_timezone, fold=fold)
		round_trip = candidate.astimezone(UTC).astimezone(source_timezone)
		if round_trip.replace(tzinfo=None) == parsed:
			candidates.append(candidate)
	if not candidates:
		raise HL7V2ContractError(
			"HL7 v2 local event timestamp does not exist in source_timezone; include a UTC offset"
		)
	if len({candidate.utcoffset() for candidate in candidates}) > 1:
		raise HL7V2ContractError(
			"HL7 v2 local event timestamp is ambiguous in source_timezone; include a UTC offset"
		)
	return candidates[0]


def _source_timezone(value: object) -> ZoneInfo:
	if not isinstance(value, str) or value != value.strip():
		raise HL7V2ContractError("HL7 v2 source_timezone is required")
	name = value
	if not name or len(name) > 100:
		raise HL7V2ContractError("HL7 v2 source_timezone is required")
	try:
		return ZoneInfo(name)
	except (ValueError, ZoneInfoNotFoundError) as exc:
		raise HL7V2ContractError("HL7 v2 source_timezone is not a valid IANA timezone") from exc


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
	value: dict[str, object] = {}
	for key, item in pairs:
		if key in value:
			raise ValueError("Duplicate HL7 v2 connector option")
		value[key] = item
	return value
