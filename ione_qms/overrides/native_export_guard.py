from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

MAX_EXPORT_TARGET_DEPTH = 12
MAX_EXPORT_TARGET_NODES = 512
MAX_EXPORT_TARGET_STRING_BYTES = 65_536


class ExportTargetInspectionError(ValueError):
	"""The native-export target could not be inspected within fail-closed bounds."""


def collect_export_target_strings(*values: Any) -> frozenset[str]:
	"""Recursively collect possible DocType names from Frappe export arguments."""
	result: set[str] = set()
	state = {"nodes": 0}
	active_containers: set[int] = set()
	for value in values:
		_collect(
			value,
			result=result,
			state=state,
			active_containers=active_containers,
			depth=0,
		)
	return frozenset(result)


def _collect(
	value: Any,
	*,
	result: set[str],
	state: dict[str, int],
	active_containers: set[int],
	depth: int,
) -> None:
	state["nodes"] += 1
	if state["nodes"] > MAX_EXPORT_TARGET_NODES:
		raise ExportTargetInspectionError("Native export target exceeds the inspection node limit.")
	if depth > MAX_EXPORT_TARGET_DEPTH:
		raise ExportTargetInspectionError("Native export target exceeds the inspection depth limit.")
	if value is None or isinstance(value, bool | int | float):
		return
	if isinstance(value, str):
		_collect_string(
			value,
			result=result,
			state=state,
			active_containers=active_containers,
			depth=depth,
		)
		return
	if isinstance(value, Mapping):
		_collect_container(
			value,
			items=(item for pair in value.items() for item in pair),
			result=result,
			state=state,
			active_containers=active_containers,
			depth=depth,
		)
		return
	if isinstance(value, list | tuple):
		_collect_container(
			value,
			items=iter(value),
			result=result,
			state=state,
			active_containers=active_containers,
			depth=depth,
		)
		return
	text = str(value)
	if _looks_like_qms(text):
		raise ExportTargetInspectionError("Unrecognized QMS native export target shape.")


def _collect_string(
	value: str,
	*,
	result: set[str],
	state: dict[str, int],
	active_containers: set[int],
	depth: int,
) -> None:
	text = value.strip()
	if not text:
		return
	if len(text.encode("utf-8")) > MAX_EXPORT_TARGET_STRING_BYTES:
		raise ExportTargetInspectionError("Native export target exceeds the inspection string limit.")
	if text[0] not in {'"', "[", "{"}:
		result.add(text)
		return
	try:
		decoded = json.loads(text)
	except (json.JSONDecodeError, RecursionError) as exc:
		if _looks_like_qms(text):
			raise ExportTargetInspectionError(
				"Malformed JSON may contain a governed QMS native export target."
			) from exc
		result.add(text)
		return
	_collect(
		decoded,
		result=result,
		state=state,
		active_containers=active_containers,
		depth=depth + 1,
	)


def _collect_container(
	container: Any,
	*,
	items,
	result: set[str],
	state: dict[str, int],
	active_containers: set[int],
	depth: int,
) -> None:
	identity = id(container)
	if identity in active_containers:
		raise ExportTargetInspectionError("Native export target contains a recursive container.")
	active_containers.add(identity)
	try:
		for item in items:
			_collect(
				item,
				result=result,
				state=state,
				active_containers=active_containers,
				depth=depth + 1,
			)
	finally:
		active_containers.remove(identity)


def _looks_like_qms(value: str) -> bool:
	normalized = value.casefold()
	for encoded, decoded in (
		("\\u0049", "i"),
		("\\u0069", "i"),
		("\\u004f", "o"),
		("\\u006f", "o"),
		("\\u004e", "n"),
		("\\u006e", "n"),
		("\\u0045", "e"),
		("\\u0065", "e"),
	):
		normalized = normalized.replace(encoded, decoded)
	return "ione" in normalized
