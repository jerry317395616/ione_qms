from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import unquote

import frappe

from ione_qms.privacy_registry import (
	IDENTITY_BEARING_DOCTYPES,
	PHI_SIDECAR_DOCTYPES,
	PROTECTED_FIELDS_BY_DOCTYPE,
)

PROTECTED_IDENTITY_FIELDS_BY_DOCTYPE = PROTECTED_FIELDS_BY_DOCTYPE
PHI_METADATA_DOCTYPES = IDENTITY_BEARING_DOCTYPES
PHI_QUERY_ARTIFACT_DOCTYPES = frozenset(
	{
		"Assignment Rule",
		"Auto Email Report",
		"Auto Repeat",
		"Client Script",
		"Dashboard Chart",
		"Document Naming Rule",
		"List Filter",
		"Notification",
		"Number Card",
		"Prepared Report",
		"Print Format",
		"Report",
		"Server Script",
		"Web Form",
		"Webhook",
		"Workflow",
	}
)
RELEASE_CONTROLLED_PROTECTED_REPORTS = {
	"IONE Rule Quality": {
		"module": "IONE Quality Analytics",
		"ref_doctype": "IONE QC Execution",
		"report_type": "Script Report",
	}
}
PHI_METADATA_OVERRIDE_DOCTYPES = frozenset(
	{
		"Custom DocPerm",
		"Custom Field",
		"Property Setter",
	}
)
PHI_UNSTRUCTURED_SIDECAR_DOCTYPES = frozenset(
	{"Comment", "Communication", "Communication Link", "ToDo", "Version"}
)
_SIDECAR_TARGET_FIELDS_BY_DOCTYPE = {
	"Comment": ("reference_doctype",),
	"Communication": ("reference_doctype",),
	"Communication Link": ("link_doctype",),
	"ToDo": ("reference_type",),
	"Version": ("ref_doctype",),
}
GOVERNED_CONFIGURATION_WRITE_DOCTYPES = frozenset(
	{
		"IONE Integration Mapping",
		"IONE PHI Disclosure Policy",
	}
)
GOVERNED_PHI_COMMANDS = frozenset(
	{
		"ione_qms.api.integration.receive_event",
		"ione_qms.api.integration.receive_hl7_v2",
		"ione_qms.api.privacy.approve_phi_disclosure_policy",
		"ione_qms.api.privacy.read_phi_identity",
		"ione_qms.api.privacy.retire_phi_disclosure_policy",
		"ione_qms.api.privacy.submit_phi_disclosure_policy",
	}
)

_MAX_INSPECTION_DEPTH = 12
_MAX_ARTIFACT_INSPECTION_DEPTH = 64
_MAX_INSPECTION_NODES = 20_000
_MAX_JSON_STRING_LENGTH = 2 * 1024 * 1024
_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_ROOT_DOCTYPE_KEYS = frozenset(
	{
		"doc_type",
		"doctype",
		"dt",
		"document_type",
		"for_doctype",
		"reference_doctype",
		"ref_doctype",
		"webhook_doctype",
		"workflow_document_type",
	}
)
_ENCODED_QUERY_ARGUMENT_KEYS = frozenset(
	{
		"fields",
		"filters",
		"group_by",
		"or_filters",
		"order_by",
	}
)
_PHI_FIELD_PATTERN = re.compile(
	r"(?<![a-z0-9_])(?:"
	+ "|".join(
		re.escape(fieldname)
		for fieldname in sorted(
			{
				fieldname
				for fieldnames in PROTECTED_IDENTITY_FIELDS_BY_DOCTYPE.values()
				for fieldname in fieldnames
			},
			key=len,
			reverse=True,
		)
	)
	+ r")(?![a-z0-9_])",
	flags=re.IGNORECASE,
)
_DOTTED_FIELD_PATTERN = re.compile(
	r"(?<![a-z0-9_])"
	r"([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)"
	r"(?![a-z0-9_])",
	flags=re.IGNORECASE,
)


def prevent_unsafe_phi_query_request() -> None:
	"""Deny generic HTTP query/filter or report paths that name a protected identity field.

	Frappe removes fields that fail field-level read permission from SELECT lists,
	but its generic database query builder accepts those fields in filters. This
	request-boundary guard closes that blind inference path without changing
	Frappe. IONE endpoints remain responsible for their narrower governed input
	contracts; notably, signed clinical ingestion and the audited disclosure API
	must legitimately carry identity field names.
	"""

	request = getattr(frappe.local, "request", None)
	if request is None or str(getattr(request, "method", "")).upper() == "OPTIONS":
		return
	path = str(getattr(request, "path", "") or "")
	form_dict = getattr(frappe, "form_dict", None) or {}
	command = _request_command(path, form_dict)
	is_restricted_administrator = _is_restricted_technical_administrator()
	if is_restricted_administrator and command.startswith("ione_qms."):
		_deny_technical_administrator()
	if command in GOVERNED_PHI_COMMANDS:
		return
	if command in {"upload_file", "frappe.utils.file_manager.upload_file"}:
		if is_restricted_administrator:
			_deny_technical_administrator()
		return
	_assert_bounded_generic_request(request, path, form_dict)
	_assert_parseable_query_arguments(form_dict)
	request_value = {"path": unquote(path), "arguments": form_dict}
	root_doctypes = _request_root_doctypes(path, command, form_dict, request_value)
	if _is_direct_file_download(path, command) and set(root_doctypes).intersection(PHI_METADATA_DOCTYPES):
		frappe.throw(
			"Direct downloads of Files associated with identity-bearing QMS records are denied. "
			"Use a purpose-built governed evidence workflow.",
			frappe.PermissionError,
		)
	if _technical_administrator_targets_ione(
		path=path,
		command=command,
		form_dict=form_dict,
		root_doctypes=root_doctypes,
	):
		_deny_technical_administrator()
	if not (path.startswith("/api/") or form_dict.get("cmd")):
		return
	if _is_governed_configuration_write(request, path, command, form_dict):
		return
	if _contains_phi_field_reference(request_value, root_doctypes=root_doctypes):
		frappe.throw(
			"Protected patient identity fields cannot be used through generic query, "
			"filter, report, search, or resource APIs. Use the governed PHI disclosure API.",
			frappe.PermissionError,
		)


def validate_phi_query_artifact(doc, method: str | None = None) -> None:
	"""Prevent persistence of a query artifact that can become an indirect PHI oracle."""

	del method
	if str(getattr(doc, "doctype", "") or "") not in PHI_QUERY_ARTIFACT_DOCTYPES:
		return
	value = doc.as_dict()
	roots = _artifact_root_doctypes(doc, value)
	if set(roots).intersection(PHI_METADATA_DOCTYPES) and not (_is_release_controlled_protected_report(doc)):
		frappe.throw(
			f"{doc.doctype} cannot target an identity-bearing IONE QMS DocType.",
			frappe.PermissionError,
		)
	if _contains_phi_field_reference(
		value,
		root_doctypes=roots,
		max_depth=_MAX_ARTIFACT_INSPECTION_DEPTH,
	):
		frappe.throw(
			f"{doc.doctype} cannot reference protected patient identity fields.",
			frappe.PermissionError,
		)


def prevent_phi_metadata_override(doc, method: str | None = None) -> None:
	"""Freeze protected DocType metadata and DocPerms against runtime customization."""

	del method
	doctype = str(getattr(doc, "doctype", "") or "")
	if doctype not in PHI_METADATA_OVERRIDE_DOCTYPES:
		return
	target = _metadata_target(doc)
	if target in PHI_METADATA_DOCTYPES:
		frappe.throw(
			f"{target} identity metadata and permissions are release-controlled and cannot "
			"be changed with runtime customization.",
			frappe.PermissionError,
		)


def prevent_ione_phi_sidecar_persistence(doc, method: str | None = None) -> None:
	"""Deny ungoverned timeline/mail/assignment/version copies of IONE data."""
	del method
	doctype = str(getattr(doc, "doctype", "") or "")
	if doctype not in PHI_UNSTRUCTURED_SIDECAR_DOCTYPES:
		return
	if _sidecar_target_doctypes(doc).intersection(PHI_METADATA_DOCTYPES):
		frappe.throw(
			f"{doctype} cannot persist unstructured copies of IONE QMS business data. "
			"Use the governed workflow fields and evidence services.",
			frappe.PermissionError,
		)


def assert_no_legacy_phi_query_bypasses() -> None:
	"""Fail migration if legacy customization or saved query artifacts bypass PHI controls."""

	conf = getattr(frappe, "conf", None) or getattr(getattr(frappe, "local", None), "conf", None)
	if conf and (
		_truthy_config(conf.get("server_script_enabled")) or _truthy_config(conf.get("allow_server_script"))
	):
		frappe.throw(
			"Server Script execution must be disabled on an IONE QMS site because it bypasses "
			"the governed patient-identity boundary."
		)

	for doctype, target_field in (
		("Custom DocPerm", "parent"),
		("Custom Field", "dt"),
		("Property Setter", "doc_type"),
	):
		if not frappe.db.exists("DocType", doctype):
			continue
		row = frappe.db.get_value(
			doctype,
			{target_field: ["in", sorted(PHI_METADATA_DOCTYPES)]},
			"name",
		)
		if row:
			frappe.throw(
				f"Legacy {doctype} '{row}' targets protected patient identity metadata. "
				"Remove the override under approved change control before migration."
			)

	if frappe.db.exists("DocType", "File"):
		protected_file = frappe.db.get_value(
			"File",
			{"attached_to_doctype": ["in", sorted(PHI_METADATA_DOCTYPES)]},
			["name", "attached_to_doctype"],
			as_dict=True,
		)
		if protected_file:
			frappe.throw(
				f"Legacy File '{protected_file.name}' is attached directly to protected "
				f"{protected_file.attached_to_doctype} data. Move it into an approved governed "
				"evidence workflow before migration."
			)

	for doctype in sorted(PHI_QUERY_ARTIFACT_DOCTYPES):
		if not frappe.db.exists("DocType", doctype):
			continue
		for name in _bounded_artifact_names(doctype):
			doc = frappe.get_doc(doctype, name)
			value = doc.as_dict()
			roots = _artifact_root_doctypes(doc, value)
			if set(roots).intersection(PHI_METADATA_DOCTYPES) and not (
				_is_release_controlled_protected_report(doc)
			):
				frappe.throw(
					f"Legacy {doctype} '{name}' targets an identity-bearing IONE QMS "
					"DocType. Retire it under approved change control before migration."
				)
			if _contains_phi_field_reference(
				value,
				root_doctypes=roots,
				max_depth=_MAX_ARTIFACT_INSPECTION_DEPTH,
			):
				frappe.throw(
					f"Legacy {doctype} '{name}' references a protected patient identity "
					"field. Retire it under approved change control before migration."
				)
	for doctype in sorted(PHI_UNSTRUCTURED_SIDECAR_DOCTYPES):
		if not frappe.db.exists("DocType", doctype):
			continue
		target_field = _SIDECAR_TARGET_FIELDS_BY_DOCTYPE[doctype][0]
		row = frappe.db.sql(
			f"select name from `tab{doctype}` "  # noqa: S608
			f"where {target_field} in ({', '.join(frappe.db.escape(item) for item in sorted(PHI_METADATA_DOCTYPES))}) "
			"order by name asc limit 1"
		)
		if row:
			frappe.throw(
				f"Legacy {doctype} content references IONE QMS business data. "
				"Remove it under approved change control before migration."
			)


def _sidecar_target_doctypes(doc) -> frozenset[str]:
	doctype = str(getattr(doc, "doctype", "") or "")
	target_fields = _SIDECAR_TARGET_FIELDS_BY_DOCTYPE.get(doctype, ())
	targets = {
		str(doc.get(fieldname) or "").strip()
		for fieldname in target_fields
		if str(doc.get(fieldname) or "").strip()
	}
	if doctype == "Communication":
		for link in doc.get("links") or ():
			target = str(link.get("link_doctype") or "").strip()
			if target:
				targets.add(target)
	return frozenset(targets)


def _bounded_artifact_names(doctype: str) -> tuple[str, ...]:
	limit = 10_000
	names = tuple(
		str(name)
		for name in frappe.get_all(
			doctype,
			pluck="name",
			order_by="name asc",
			limit_page_length=limit + 1,
		)
	)
	if len(names) > limit:
		frappe.throw(f"{doctype} legacy PHI query audit exceeded its safe bounded cardinality.")
	return names


def _request_command(path: str, form_dict: Mapping[str, Any]) -> str:
	for prefix in ("/api/method/", "/api/v2/method/"):
		if path.startswith(prefix):
			return unquote(path[len(prefix) :]).strip("/")
	return str(form_dict.get("cmd") or "").strip()


def _request_root_doctypes(
	path: str,
	command: str,
	form_dict: Mapping[str, Any],
	request_value: Mapping[str, Any],
) -> frozenset[str]:
	roots = set(_root_doctypes(request_value))
	decoded_path = unquote(path)
	for prefix in ("/api/resource/", "/api/v2/document/"):
		if decoded_path.startswith(prefix):
			target = decoded_path[len(prefix) :].strip("/").split("/", 1)[0]
			if target:
				roots.add(target)
	for key in ("report", "report_name"):
		report = str(form_dict.get(key) or "").strip()
		if report:
			ref_doctype = _linked_report_doctype(report)
			if ref_doctype:
				roots.add(ref_doctype)
	roots.update(_file_attachment_doctypes(decoded_path, command, form_dict, roots))
	return frozenset(roots)


def _technical_administrator_targets_ione(
	*,
	path: str,
	command: str,
	form_dict: Mapping[str, Any],
	root_doctypes: frozenset[str],
) -> bool:
	if not _is_restricted_technical_administrator():
		return False
	if command.startswith("ione_qms."):
		return True
	if set(root_doctypes).intersection(PHI_SIDECAR_DOCTYPES):
		return True
	if any(str(doctype).startswith("IONE ") for doctype in root_doctypes):
		return True
	decoded_path = unquote(path)
	if any(f"/{doctype}/" in f"{decoded_path}/" for doctype in PHI_METADATA_DOCTYPES):
		return True
	return _contains_explicit_ione_doctype(form_dict)


def _is_restricted_technical_administrator() -> bool:
	session = getattr(frappe, "session", None)
	if str(getattr(session, "user", "") or "") != "Administrator":
		return False
	conf = getattr(frappe, "conf", None) or getattr(getattr(frappe, "local", None), "conf", None) or {}
	return not _truthy_config(conf.get("ione_qms_allow_technical_administrator"))


def _contains_explicit_ione_doctype(value: Any, depth: int = 0) -> bool:
	if depth > _MAX_INSPECTION_DEPTH:
		return True
	if isinstance(value, Mapping):
		for key, item in value.items():
			if (
				str(key).strip().lower() in _ROOT_DOCTYPE_KEYS
				and isinstance(item, str)
				and item.strip().startswith("IONE ")
			):
				return True
			if _contains_explicit_ione_doctype(item, depth + 1):
				return True
		return False
	if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
		return any(_contains_explicit_ione_doctype(item, depth + 1) for item in value)
	if isinstance(value, str):
		stripped = value.strip()
		parsed = _parse_embedded_json(stripped)
		if parsed is not None:
			return _contains_explicit_ione_doctype(parsed, depth + 1)
	return False


def _file_attachment_doctypes(
	path: str,
	command: str,
	form_dict: Mapping[str, Any],
	root_doctypes: set[str],
) -> set[str]:
	targets: set[str] = set()
	file_urls: set[str] = set()
	if path.startswith(("/files/", "/private/files/")):
		file_urls.add(path)
	for key in ("file_url", "fileurl"):
		value = str(form_dict.get(key) or "").strip()
		if value.startswith(("/files/", "/private/files/")):
			file_urls.add(unquote(value))
	for file_url in file_urls:
		targets.update(_bounded_file_parent_doctypes({"file_url": file_url}))

	file_names: set[str] = set()
	for prefix in ("/api/resource/File/", "/api/v2/document/File/"):
		if path.startswith(prefix):
			name = path[len(prefix) :].strip("/").split("/", 1)[0]
			if name:
				file_names.add(unquote(name))
	if "File" in root_doctypes and command in {
		"frappe.client.get",
		"frappe.client.get_value",
		"frappe.utils.file_manager.download_file",
	}:
		for key in ("name", "fid", "file_name"):
			name = str(form_dict.get(key) or "").strip()
			if name:
				file_names.add(name)
	for name in file_names:
		targets.update(_bounded_file_parent_doctypes({"name": name}))
	return targets


def _bounded_file_parent_doctypes(filters: Mapping[str, Any]) -> set[str]:
	rows = frappe.get_all(
		"File",
		filters=dict(filters),
		fields=["name", "attached_to_doctype"],
		order_by="name asc",
		limit_page_length=1_001,
	)
	if len(rows) > 1_000:
		frappe.throw("File attachment inspection exceeded its safe bounded cardinality.")
	return {
		str(row.get("attached_to_doctype") or "") for row in rows if str(row.get("attached_to_doctype") or "")
	}


def _is_direct_file_download(path: str, command: str) -> bool:
	decoded = unquote(path)
	return decoded.startswith(("/files/", "/private/files/")) or command in {
		"frappe.utils.file_manager.download_file",
		"frappe.utils.print_format.download_pdf",
	}


def _linked_report_doctype(report: str) -> str:
	if not report or not frappe.db.exists("DocType", "Report"):
		return ""
	return str(frappe.db.get_value("Report", report, "ref_doctype") or "")


def _artifact_root_doctypes(doc, value: Mapping[str, Any]) -> frozenset[str]:
	roots = set(_root_doctypes(value))
	report = str(
		getattr(doc, "report", "")
		or getattr(doc, "report_name", "")
		or value.get("report")
		or value.get("report_name")
		or ""
	).strip()
	if report:
		ref_doctype = _linked_report_doctype(report)
		if ref_doctype:
			roots.add(ref_doctype)
	return frozenset(roots)


def _is_release_controlled_protected_report(doc) -> bool:
	if str(getattr(doc, "doctype", "") or "") != "Report":
		return False
	name = str(getattr(doc, "name", "") or doc.get("report_name") or "").strip()
	expected = RELEASE_CONTROLLED_PROTECTED_REPORTS.get(name)
	if not expected:
		return False
	if any(
		str(doc.get(fieldname) or "").strip() != expected_value
		for fieldname, expected_value in expected.items()
	):
		return False
	if str(doc.get("report_name") or name).strip() != name:
		return False
	if str(doc.get("is_standard") or "") != "Yes":
		return False
	if int(doc.get("prepared_report") or 0) or not int(doc.get("disable_prepared_report_automation") or 0):
		return False
	return not any(
		str(doc.get(fieldname) or "").strip() for fieldname in ("query", "report_script", "javascript")
	)


def _is_governed_configuration_write(
	request,
	path: str,
	command: str,
	form_dict: Mapping[str, Any],
) -> bool:
	if str(getattr(request, "method", "") or "").upper() not in {"POST", "PUT", "PATCH"}:
		return False
	write_commands = {
		"",
		"frappe.client.insert",
		"frappe.client.save",
		"frappe.desk.form.save.savedocs",
	}
	if command not in write_commands:
		return False
	root = _submitted_root_doctype(path, form_dict)
	return root in GOVERNED_CONFIGURATION_WRITE_DOCTYPES


def _submitted_root_doctype(path: str, form_dict: Mapping[str, Any]) -> str:
	for prefix in ("/api/resource/", "/api/v2/document/"):
		if path.startswith(prefix):
			return unquote(path[len(prefix) :]).strip("/").split("/", 1)[0]
	value: Any = form_dict.get("doc")
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except TypeError, ValueError:
			return ""
	if isinstance(value, Mapping):
		return str(value.get("doctype") or "")
	return str(form_dict.get("doctype") or "")


def _metadata_target(doc) -> str:
	doctype = str(getattr(doc, "doctype", "") or "")
	fieldname = {
		"Custom DocPerm": "parent",
		"Custom Field": "dt",
		"Property Setter": "doc_type",
	}.get(doctype, "")
	return str(doc.get(fieldname) or "") if fieldname else ""


def _root_doctypes(value: Any) -> frozenset[str]:
	roots: set[str] = set()

	def inspect(candidate: Any, depth: int) -> None:
		if depth > _MAX_INSPECTION_DEPTH:
			return
		if isinstance(candidate, Mapping):
			for key, item in candidate.items():
				if str(key).strip().lower() in _ROOT_DOCTYPE_KEYS and isinstance(item, str):
					roots.add(item.strip())
				inspect(item, depth + 1)
			return
		if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes, bytearray)):
			for item in candidate:
				inspect(item, depth + 1)
			return
		if isinstance(candidate, str):
			stripped = candidate.strip()
			parsed = _parse_embedded_json(stripped)
			if parsed is not None:
				inspect(parsed, depth + 1)

	inspect(value, 0)
	return frozenset(roots)


def _contains_phi_field_reference(
	value: Any,
	*,
	root_doctypes: frozenset[str] = frozenset(),
	max_depth: int = _MAX_INSPECTION_DEPTH,
) -> bool:
	nodes = 0
	texts: list[str] = []

	def inspect(candidate: Any, depth: int) -> None:
		nonlocal nodes
		nodes += 1
		if nodes > _MAX_INSPECTION_NODES or depth > max_depth:
			frappe.throw("PHI query inspection exceeded its safe bounded input contract.")
		if isinstance(candidate, Mapping):
			for key, item in candidate.items():
				inspect(key, depth + 1)
				inspect(item, depth + 1)
			return
		if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes, bytearray)):
			for item in candidate:
				inspect(item, depth + 1)
			return
		if isinstance(candidate, (bytes, bytearray)):
			text = bytes(candidate).decode("utf-8", errors="replace")
		else:
			text = str(candidate or "")
		texts.append(text)
		stripped = text.strip()
		parsed = _parse_embedded_json(stripped)
		if parsed is not None and parsed is not candidate:
			inspect(parsed, depth + 1)

	inspect(value, 0)
	field_texts = [text for text in texts if _PHI_FIELD_PATTERN.search(text)]
	if not field_texts:
		return False
	if set(root_doctypes).intersection(PHI_METADATA_DOCTYPES):
		return True
	target_names = tuple(name.lower() for name in PHI_METADATA_DOCTYPES)
	if any(target in text.lower() for text in texts for target in target_names):
		return True
	return any(
		_dotted_path_targets_phi(path, root_doctypes)
		for text in field_texts
		for path in _DOTTED_FIELD_PATTERN.findall(text)
		if _PHI_FIELD_PATTERN.search(path.rsplit(".", 1)[-1])
	)


def _dotted_path_targets_phi(path: str, root_doctypes: frozenset[str]) -> bool:
	segments = path.lower().split(".")
	if len(segments) < 2:
		return False
	for root in root_doctypes:
		current = str(root or "")
		for segment in segments[:-1]:
			if not current or not frappe.db.exists("DocType", current):
				break
			meta = frappe.get_meta(current)
			field = meta.get_field(segment)
			if not field or str(field.fieldtype or "") != "Link":
				break
			current = str(field.options or "")
		else:
			if current in PHI_METADATA_DOCTYPES:
				return True
	return False


def _parse_embedded_json(value: str, *, reject_malformed: bool = False) -> Any | None:
	if not value or value[0] not in '[{"' or value[-1] not in ']}"':
		return None
	if len(value.encode("utf-8", errors="replace")) > _MAX_JSON_STRING_LENGTH:
		_raise_inspection_limit("encoded query value")
	try:
		return json.loads(value)
	except TypeError, ValueError:
		if reject_malformed:
			# A JSON-looking generic query argument that cannot be inspected is
			# not passed downstream to Frappe for a second, permissive parse.
			_raise_inspection_limit("malformed encoded query value")
	return None


def _assert_parseable_query_arguments(value: Any, depth: int = 0) -> None:
	"""Fail closed only for fields Frappe interprets as encoded query structures."""
	if depth > _MAX_INSPECTION_DEPTH:
		_raise_inspection_limit("query argument structure")
	if isinstance(value, Mapping):
		for key, item in value.items():
			if str(key).strip().lower() in _ENCODED_QUERY_ARGUMENT_KEYS and isinstance(item, str):
				_parse_embedded_json(item.strip(), reject_malformed=True)
			_assert_parseable_query_arguments(item, depth + 1)
		return
	if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
		for item in value:
			_assert_parseable_query_arguments(item, depth + 1)


def _assert_bounded_generic_request(
	request,
	path: str,
	form_dict: Mapping[str, Any],
) -> None:
	content_length = getattr(request, "content_length", None)
	try:
		length = int(content_length) if content_length not in (None, "") else 0
	except TypeError, ValueError:
		_raise_inspection_limit("invalid request length")
		return
	if length < 0 or length > _MAX_REQUEST_BYTES:
		_raise_inspection_limit("request body")
	nodes = 0
	total_bytes = len(path.encode("utf-8", errors="replace"))

	def inspect(value: Any, depth: int) -> None:
		nonlocal nodes, total_bytes
		nodes += 1
		if nodes > _MAX_INSPECTION_NODES or depth > _MAX_INSPECTION_DEPTH:
			_raise_inspection_limit("request structure")
		if isinstance(value, Mapping):
			for key, item in value.items():
				inspect(key, depth + 1)
				inspect(item, depth + 1)
			return
		if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
			for item in value:
				inspect(item, depth + 1)
			return
		if isinstance(value, (bytes, bytearray)):
			total_bytes += len(value)
		else:
			total_bytes += len(str(value or "").encode("utf-8", errors="replace"))
		if total_bytes > _MAX_REQUEST_BYTES:
			_raise_inspection_limit("request arguments")

	inspect(form_dict, 0)


def _truthy_config(value: Any) -> bool:
	if isinstance(value, bool):
		return value
	if isinstance(value, (int, float)):
		return value != 0
	return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _deny_technical_administrator() -> None:
	frappe.throw(
		"Technical Administrator sessions cannot read or operate on IONE QMS business data. "
		"Use a named, least-privilege accountable user.",
		frappe.PermissionError,
	)


def _raise_inspection_limit(label: str) -> None:
	frappe.throw(
		f"Generic PHI query inspection rejected an unsafe {label}.",
		frappe.PermissionError,
	)
