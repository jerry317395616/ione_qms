from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
HOOKS = PACKAGE_ROOT / "hooks.py"
API = PACKAGE_ROOT / "api" / "improvement.py"
CLIENT = PACKAGE_ROOT / "public" / "js" / "qc_finding.js"
FILE_OVERRIDE = PACKAGE_ROOT / "overrides" / "file.py"
APPEAL_SCHEMA = (
	PACKAGE_ROOT / "ione_improvement" / "doctype" / "ione_qc_finding_appeal" / "ione_qc_finding_appeal.json"
)
EVIDENCE_SCHEMA = (
	PACKAGE_ROOT
	/ "ione_improvement"
	/ "doctype"
	/ "ione_qc_finding_appeal_evidence"
	/ "ione_qc_finding_appeal_evidence.json"
)


class TestFindingAppealDeskContract(TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		cls.client = CLIENT.read_text(encoding="utf-8")
		cls.api_tree = ast.parse(API.read_text(encoding="utf-8"))

	def test_finding_form_loads_the_standard_client_controller(self) -> None:
		tree = ast.parse(HOOKS.read_text(encoding="utf-8"))
		hooks = ast.literal_eval(
			next(
				node.value
				for node in tree.body
				if isinstance(node, ast.Assign)
				and any(isinstance(target, ast.Name) and target.id == "doctype_js" for target in node.targets)
			)
		)
		self.assertEqual(hooks["IONE QC Finding"], "public/js/qc_finding.js")
		doc_events = ast.literal_eval(
			next(
				node.value
				for node in tree.body
				if isinstance(node, ast.Assign)
				and any(isinstance(target, ast.Name) and target.id == "doc_events" for target in node.targets)
			)
		)
		self.assertEqual(
			doc_events["File"]["validate"],
			"ione_qms.services.finding_appeals.validate_finding_appeal_evidence_file",
		)
		extend_doctype_class = ast.literal_eval(
			next(
				node.value
				for node in tree.body
				if isinstance(node, ast.Assign)
				and any(
					isinstance(target, ast.Name) and target.id == "extend_doctype_class"
					for target in node.targets
				)
			)
		)
		self.assertEqual(
			extend_doctype_class["File"],
			["ione_qms.overrides.file.IONEQMSFileMixin"],
		)
		self.assertFalse(
			any(
				isinstance(node, ast.Assign)
				and any(
					isinstance(target, ast.Name) and target.id == "override_doctype_class"
					for target in node.targets
				)
				for node in tree.body
			)
		)
		has_permission = ast.literal_eval(
			next(
				node.value
				for node in tree.body
				if isinstance(node, ast.Assign)
				and any(
					isinstance(target, ast.Name) and target.id == "has_permission" for target in node.targets
				)
			)
		)
		self.assertEqual(
			has_permission["File"],
			"ione_qms.overrides.file.qms_file_permission",
		)
		self.assertIn("IONE QC Finding Appeal Evidence", doc_events)

	def test_file_extension_guards_before_effective_controller_side_effects(self) -> None:
		tree = ast.parse(FILE_OVERRIDE.read_text(encoding="utf-8"))
		class_node = next(node for node in tree.body if isinstance(node, ast.ClassDef))
		self.assertEqual(class_node.name, "IONEQMSFileMixin")
		self.assertEqual(class_node.bases, [])
		self.assertFalse(any(isinstance(node, ast.Try) for node in tree.body))
		imported_modules = {
			node.module for node in tree.body if isinstance(node, ast.ImportFrom) and node.module
		}
		self.assertNotIn("frappe.core.doctype.file.file", imported_modules)
		self.assertFalse(any(module == "drive" or module.startswith("drive.") for module in imported_modules))
		methods = {node.name: node for node in class_node.body if isinstance(node, ast.FunctionDef)}
		for method_name, guard_name in (
			("validate", "validate_finding_appeal_evidence_file"),
			("optimize_file", "prevent_finding_appeal_evidence_deletion"),
			("on_trash", "prevent_finding_appeal_evidence_deletion"),
		):
			with self.subTest(method=method_name):
				method = methods[method_name]
				calls = [node for node in ast.walk(method) if isinstance(node, ast.Call)]
				guard_call = next(
					call for call in calls if isinstance(call.func, ast.Name) and call.func.id == guard_name
				)
				super_call = next(
					call
					for call in calls
					if isinstance(call.func, ast.Attribute)
					and isinstance(call.func.value, ast.Call)
					and isinstance(call.func.value.func, ast.Name)
					and call.func.value.func.id == "super"
				)
				self.assertLess(guard_call.lineno, super_call.lineno)
		download_method = methods["is_downloadable"]
		download_calls = [call for call in ast.walk(download_method) if isinstance(call, ast.Call)]
		self.assertTrue(
			any(
				isinstance(call.func, ast.Name) and call.func.id == "qms_file_permission"
				for call in download_calls
			)
		)

	def test_file_extension_is_cooperative_with_drive_or_frappe_controller(self) -> None:
		calls: list[str] = []
		namespace = {
			"frappe": SimpleNamespace(
				session=SimpleNamespace(user="reviewer@example.test"),
				whitelist=lambda: lambda function: function,
			),
			"validate_protected_identity_file": lambda _doc: calls.append("qms.identity.validate"),
			"validate_finding_appeal_evidence_file": lambda _doc: calls.append("qms.appeal.validate"),
			"validate_standard_source_file": lambda _doc: calls.append("qms.standard.validate"),
			"validate_production_evidence_manifest_file": lambda _doc: calls.append(
				"qms.production.validate"
			),
			"prevent_finding_appeal_evidence_deletion": lambda _doc: calls.append("qms.appeal.protect"),
			"prevent_standard_source_file_deletion": lambda _doc: calls.append("qms.standard.protect"),
			"prevent_production_evidence_manifest_deletion": lambda _doc: calls.append(
				"qms.production.protect"
			),
			"qms_file_permission": (
				lambda _doc, _ptype, *, user: calls.append(f"qms.permission:{user}") or True
			),
		}
		tree = ast.parse(FILE_OVERRIDE.read_text(encoding="utf-8"))
		class_node = next(node for node in tree.body if isinstance(node, ast.ClassDef))
		exec(  # noqa: S102 - execute only the repository-owned parsed mixin in isolated test globals
			compile(
				ast.fix_missing_locations(ast.Module(body=[class_node], type_ignores=[])),
				str(FILE_OVERRIDE),
				"exec",
			),
			namespace,
		)
		mixin = namespace["IONEQMSFileMixin"]

		class FrappeFile:
			def validate(self):
				calls.append("frappe.validate")
				return "frappe-validated"

			def optimize_file(self):
				calls.append("frappe.optimize")
				return "frappe-optimized"

			def on_trash(self):
				calls.append("frappe.trash")
				return "frappe-trashed"

			def is_downloadable(self):
				calls.append("frappe.download")
				return True

		class DriveFile(FrappeFile):
			is_site_file = False

			def validate(self):
				calls.append("drive.validate")
				if self.is_site_file:
					return super().validate()
				return "drive-validated"

		frappe_only = type("ExtendedFrappeFile", (mixin, FrappeFile), {})
		self.assertEqual(frappe_only().validate(), "frappe-validated")
		self.assertEqual(
			calls,
			[
				"qms.identity.validate",
				"qms.appeal.validate",
				"qms.standard.validate",
				"qms.production.validate",
				"frappe.validate",
			],
		)

		calls.clear()
		drive_backed = type("ExtendedDriveFile", (mixin, DriveFile), {})
		self.assertEqual(drive_backed().validate(), "drive-validated")
		self.assertEqual(
			calls,
			[
				"qms.identity.validate",
				"qms.appeal.validate",
				"qms.standard.validate",
				"qms.production.validate",
				"drive.validate",
			],
		)

		calls.clear()
		drive_site_file = drive_backed()
		drive_site_file.is_site_file = True
		self.assertEqual(drive_site_file.validate(), "frappe-validated")
		self.assertEqual(
			calls,
			[
				"qms.identity.validate",
				"qms.appeal.validate",
				"qms.standard.validate",
				"qms.production.validate",
				"drive.validate",
				"frappe.validate",
			],
		)

		calls.clear()
		self.assertEqual(drive_backed().optimize_file(), "frappe-optimized")
		self.assertEqual(
			calls,
			[
				"qms.identity.validate",
				"qms.appeal.protect",
				"qms.standard.protect",
				"qms.production.protect",
				"frappe.optimize",
			],
		)

		calls.clear()
		self.assertEqual(drive_backed().on_trash(), "frappe-trashed")
		self.assertEqual(
			calls,
			[
				"qms.identity.validate",
				"qms.appeal.protect",
				"qms.standard.protect",
				"qms.production.protect",
				"frappe.trash",
			],
		)

		calls.clear()
		self.assertTrue(drive_backed().is_downloadable())
		self.assertEqual(
			calls,
			["qms.permission:reviewer@example.test", "frappe.download"],
		)

		namespace["qms_file_permission"] = lambda _doc, _ptype, *, user: (
			calls.append(f"qms.denied:{user}") or False
		)
		calls.clear()
		self.assertFalse(drive_backed().is_downloadable())
		self.assertEqual(calls, ["qms.denied:reviewer@example.test"])

	def test_bench_effective_file_controller_has_the_required_mro(self) -> None:
		try:
			import frappe
			from frappe.model.base_document import get_controller
		except ModuleNotFoundError:
			self.skipTest("Frappe-dependent controller assertion runs in Bench CI")

		controller_paths = [
			f"{controller.__module__}.{controller.__name__}" for controller in get_controller("File").mro()
		]
		qms_mixin = "ione_qms.overrides.file.IONEQMSFileMixin"
		core_file = "frappe.core.doctype.file.file.File"
		self.assertIn(qms_mixin, controller_paths)
		self.assertIn(core_file, controller_paths)
		self.assertLess(controller_paths.index(qms_mixin), controller_paths.index(core_file))

		overrides = frappe.get_hooks("override_doctype_class", {}).get("File", [])
		extensions = frappe.get_hooks("extend_doctype_class", {}).get("File", [])
		self.assertNotIn("ione_qms.overrides.file.IONEQMSFileMixin", overrides)
		self.assertIn(qms_mixin, extensions)
		if "drive" in frappe.get_installed_apps():
			drive_file = "drive.overrides.file.File"
			self.assertIn(drive_file, controller_paths)
			self.assertLess(controller_paths.index(qms_mixin), controller_paths.index(drive_file))
			self.assertLess(controller_paths.index(drive_file), controller_paths.index(core_file))
			self.assertIn(drive_file, overrides)
			for method in (
				"share",
				"unshare",
				"move",
				"rename",
				"permanent_delete",
				"after_insert",
				"after_delete",
				"on_rollback",
			):
				self.assertTrue(hasattr(get_controller("File"), method), method)
		else:
			self.assertFalse(any(path.startswith("drive.") for path in controller_paths))

	def test_all_three_actions_call_post_only_governed_endpoints(self) -> None:
		expected = {
			"submit_finding_appeal",
			"discard_unbound_finding_appeal_evidence",
			"review_finding_appeal",
			"withdraw_finding_appeal",
		}
		for function_name in expected:
			with self.subTest(function_name=function_name):
				self.assertIn(f'"ione_qms.api.improvement.{function_name}"', self.client)
				function = next(
					node
					for node in self.api_tree.body
					if isinstance(node, ast.FunctionDef) and node.name == function_name
				)
				whitelist = next(
					decorator
					for decorator in function.decorator_list
					if isinstance(decorator, ast.Call)
					and isinstance(decorator.func, ast.Attribute)
					and decorator.func.attr == "whitelist"
				)
				methods = next(keyword.value for keyword in whitelist.keywords if keyword.arg == "methods")
				self.assertEqual(ast.literal_eval(methods), ["POST"])
		self.assertIn('type: "POST"', self.client)

	def test_buttons_are_status_and_role_scoped_with_self_review_hidden(self) -> None:
		for role in (
			"IONE Physician",
			"IONE Department Director",
			"IONE Department QC Officer",
			"IONE QC Reviewer",
			"IONE Medical Affairs",
		):
			self.assertIn(f'"{role}"', self.client)
		self.assertIn('frm.doc.status === "Confirmed"', self.client)
		self.assertIn('frm.doc.status !== "Appealed"', self.client)
		self.assertIn('frappe.session.user === "Administrator"', self.client)
		self.assertIn("appeal.submitted_by === current_user", self.client)
		self.assertIn("appeal.submitted_by !== current_user", self.client)
		for label in ("Submit Appeal", "Approve Appeal", "Reject Appeal", "Withdraw Appeal"):
			self.assertIn(f'__("{label}")', self.client)

	def test_attachment_control_forces_private_upload_and_server_remains_authoritative(self) -> None:
		self.assertIn('fieldtype: "Attach"', self.client)
		self.assertIn("make_attachments_public: false", self.client)
		self.assertIn("allow_toggle_private: false", self.client)
		self.assertIn("allow_web_link: false", self.client)
		self.assertIn("allow_google_drive: false", self.client)
		self.assertIn("allow_toggle_optimize: false", self.client)
		self.assertIn("disable_file_browser: true", self.client)
		self.assertIn('attachment.startsWith("/private/files/")', self.client)
		self.assertIn("evidence_file: secure_upload?.name", self.client)
		self.assertIn("evidence_content_hash: secure_upload?.content_hash", self.client)
		self.assertIn("discard_unbound_evidence(secure_upload)", self.client)
		self.assertIn("20 * 1024 * 1024", self.client)

	def test_standard_workflow_actions_are_suppressed_and_active_lookup_is_exact(self) -> None:
		self.assertIn("GOVERNED_WORKFLOW_ACTIONS", self.client)
		self.assertIn("MutationObserver", self.client)
		self.assertIn("before_workflow_action", self.client)
		self.assertIn("{ active_key: finding }", self.client)
		self.assertIn("response.message?.name", self.client)

	def test_evidence_isolated_from_appeal_attachments_and_version_docinfo(self) -> None:
		appeal = json.loads(APPEAL_SCHEMA.read_text(encoding="utf-8"))
		evidence = json.loads(EVIDENCE_SCHEMA.read_text(encoding="utf-8"))
		appeal_fields = {field["fieldname"]: field for field in appeal["fields"]}
		self.assertNotIn("evidence_attachment", appeal_fields)
		self.assertEqual(appeal_fields["evidence_record"]["fieldtype"], "Link")
		self.assertEqual(
			appeal_fields["evidence_record"]["options"],
			"IONE QC Finding Appeal Evidence",
		)
		self.assertEqual(appeal.get("track_changes"), 0)
		self.assertFalse(
			any(field.get("fieldtype") in {"Attach", "Attach Image"} for field in appeal["fields"])
		)
		self.assertEqual(evidence.get("track_changes"), 0)
		readers = {permission["role"] for permission in evidence["permissions"] if permission.get("read")}
		self.assertNotIn("IONE QMS Auditor", readers)
		evidence_file = next(field for field in evidence["fields"] if field["fieldname"] == "evidence_file")
		self.assertEqual(evidence_file["fieldtype"], "Link")
		self.assertEqual(evidence_file["options"], "File")
		self.assertEqual(evidence_file.get("unique"), 1)
		content_hash = next(field for field in evidence["fields"] if field["fieldname"] == "content_hash")
		self.assertEqual(content_hash.get("unique"), 1)
		self.assertEqual(content_hash.get("length"), 64)
