from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "ione_qms" / "services" / "source_document_locator.py"
API = ROOT / "ione_qms" / "api" / "source_document.py"
PERMISSIONS = ROOT / "ione_qms" / "permissions.py"
HOOKS = ROOT / "ione_qms" / "hooks.py"
SCOPE = ROOT / "ione_qms" / "services" / "scope_hierarchy.py"
EXPORT = ROOT / "ione_qms" / "services" / "data_export.py"
GENERATOR = ROOT / "tools" / "generate_doctypes.py"
WORKSPACES = ROOT / "tools" / "generate_workspaces.py"
EVIDENCE_JS = ROOT / "ione_qms" / "public" / "js" / "qc_finding_evidence.js"
LOCATOR_JS = ROOT / "ione_qms" / "public" / "js" / "source_document_locator.js"


def _function_source(path: Path, name: str) -> str:
	source = path.read_text(encoding="utf-8")
	tree = ast.parse(source)
	for node in tree.body:
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
			return ast.get_source_segment(source, node) or ""
	raise AssertionError(f"{name} was not found in {path}")


class TestSourceDocumentLocatorStatic(unittest.TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		cls.service = SERVICE.read_text(encoding="utf-8")
		cls.api = API.read_text(encoding="utf-8")
		cls.permissions = PERMISSIONS.read_text(encoding="utf-8")
		cls.hooks = HOOKS.read_text(encoding="utf-8")

	def test_only_post_endpoints_and_both_form_experiences_are_wired(self) -> None:
		self.assertEqual(self.api.count('@frappe.whitelist(methods=["POST"])'), 3)
		self.assertNotIn('methods=["GET"]', self.api)
		self.assertIn('"IONE QC Finding Evidence": "public/js/qc_finding_evidence.js"', self.hooks)
		self.assertIn('"IONE Source Document Locator": "public/js/source_document_locator.js"', self.hooks)
		evidence_js = EVIDENCE_JS.read_text(encoding="utf-8")
		locator_js = LOCATOR_JS.read_text(encoding="utf-8")
		for source in (evidence_js, locator_js):
			self.assertIn('type: "POST"', source)
		self.assertNotIn("GET", source)
		self.assertNotIn("eval(", source)
		self.assertNotIn("innerHTML", source)
		self.assertIn("frappe.call", source)
		self.assertIn("IONE Source Document Locator", locator_js)
		self.assertIn("frm.reload_doc()", locator_js)
		self.assertIn('window.open(result.url, "_blank", "noopener,noreferrer")', evidence_js)

	def test_governed_lifecycle_is_separated_immutable_and_delete_protected(self) -> None:
		approval = _function_source(SERVICE, "approve_source_document_locator")
		operation = _function_source(SERVICE, "operate_source_document_locator")
		lifecycle = _function_source(SERVICE, "_validate_locator_lifecycle")
		self.assertIn("requested_by", approval)
		self.assertIn("cannot approve", approval)
		self.assertIn("_approval_checksum", approval)
		self.assertIn("advisory_lock", approval)
		for transition in ("Suspend", "Resume", "Retire"):
			self.assertIn(transition, operation)
		self.assertIn("_LOCATOR_SEMANTIC_FIELDS", lifecycle)
		self.assertIn("immutable", lifecycle)
		self.assertIn("prevent_source_document_record_deletion", self.hooks)

	def test_deep_link_is_strict_https_path_segment_only_and_never_fetches(self) -> None:
		live = _function_source(SERVICE, "_live_deep_link")
		origin = _function_source(SERVICE, "_https_origin")
		prefix = _function_source(SERVICE, "_valid_prefix")
		suffix = _function_source(SERVICE, "_valid_suffix")
		self.assertIn('quote(raw_record_id, safe=""', live)
		self.assertIn('parsed.scheme != "https"', live)
		self.assertIn("parsed.query", live)
		self.assertIn("parsed.fragment", live)
		self.assertIn("parsed.username", live)
		self.assertIn("parsed.netloc", live)
		self.assertIn('parsed.path not in {"", "/"}', origin)
		for forbidden in ('"?"', '"#"', '"{"', '"}"', '"%"'):
			self.assertIn(forbidden, prefix + suffix)
		for network_client in ("requests.", "httpx.", "urllib.request", "oracledb", "cx_Oracle"):
			self.assertNotIn(network_client, self.service)
		self.assertNotIn("source_record_id =", self.service)

	def test_endpoint_and_source_contract_is_live_rechecked_and_checksum_bound(self) -> None:
		contract = _function_source(SERVICE, "_validate_locator_endpoint_contract")
		checksum = _function_source(SERVICE, "_approval_checksum")
		for expected in (
			'"Read Only"',
			'"tls_verify"',
			'"read_only"',
			'"enabled"',
			"_exact_allowed_hosts",
			"_https_origin",
		):
			self.assertIn(expected, contract)
		self.assertIn("scope_policy_checksum", checksum)
		self.assertIn("source_allowed_hosts", checksum)
		self.assertIn("endpoint_allowed_hosts", checksum)
		self.assertIn("origin", checksum)

	def test_scope_overlap_is_locked_and_resolution_fails_closed(self) -> None:
		overlap = _function_source(SERVICE, "_lock_and_reject_overlapping_locators")
		resolve = _function_source(SERVICE, "locate_source_document")
		self.assertIn("for update", overlap.lower())
		self.assertIn("scope_family_key", overlap)
		self.assertIn("overlaps", overlap)
		self.assertIn("len(locators) != 1", resolve)
		self.assertIn("NO_APPROVED_LOCATOR", resolve)
		self.assertIn("AMBIGUOUS_LOCATOR", resolve)
		self.assertIn("require_scope_read", resolve)

	def test_evidence_authorization_inherits_the_linked_finding(self) -> None:
		query = _function_source(PERMISSIONS, "qc_finding_evidence_query")
		permission = _function_source(PERMISSIONS, "qc_finding_evidence_permission")
		resolve = _function_source(SERVICE, "locate_source_document")
		self.assertIn("QC Finding Evidence`.finding", query)
		self.assertIn("qc_finding_query", query)
		self.assertIn('frappe.get_doc("IONE QC Finding"', permission)
		self.assertIn("finding_permission", permission)
		self.assertIn("unavailable or outside", resolve)
		self.assertNotIn("DoesNotExistError as", resolve)

	def test_access_log_is_hash_only_system_managed_and_non_exportable(self) -> None:
		writer = _function_source(SERVICE, "_write_access_log")
		validator = _function_source(SERVICE, "validate_source_document_access_log")
		self.assertIn('"record_id_hash": record_id_hash', writer)
		self.assertNotIn("raw_record_id", writer)
		self.assertNotIn('"url"', writer)
		self.assertIn("_ACCESS_LOG_TOKEN", validator)
		self.assertIn("_record_id_hash", validator)
		self.assertIn("validate_append_only", validator)
		for source_path in (EXPORT, GENERATOR):
			source = source_path.read_text(encoding="utf-8")
			self.assertIn('"IONE Source Document Access Log"', source)
		controlled_export = _function_source(EXPORT, "normalize_export_spec")
		self.assertNotIn("IONE Source Document Access Log", controlled_export)
		metadata = json.loads(
			(
				ROOT
				/ "ione_qms"
				/ "ione_quality_integration"
				/ "doctype"
				/ "ione_source_document_access_log"
				/ "ione_source_document_access_log.json"
			).read_text(encoding="utf-8")
		)
		for permission_row in metadata["permissions"]:
			self.assertEqual(permission_row.get("create", 0), 0)
			self.assertEqual(permission_row.get("write", 0), 0)
			self.assertEqual(permission_row.get("delete", 0), 0)
			self.assertEqual(permission_row.get("export", 0), 0)
			self.assertEqual(permission_row.get("print", 0), 0)

	def test_metadata_scope_permissions_workspace_and_hooks_are_complete(self) -> None:
		scope = SCOPE.read_text(encoding="utf-8")
		workspace = WORKSPACES.read_text(encoding="utf-8")
		for doctype in ("IONE Source Document Locator", "IONE Source Document Access Log"):
			self.assertIn(f'"{doctype}"', scope)
			self.assertIn(f'"{doctype}"', self.hooks)
			self.assertIn(f'"{doctype}"', workspace)
		self.assertIn("source_document_locator_query", self.hooks)
		self.assertIn("source_document_access_log_query", self.hooks)
		self.assertIn("source_document_locator_permission", self.hooks)
		self.assertIn("source_document_access_log_permission", self.hooks)


if __name__ == "__main__":
	unittest.main()
