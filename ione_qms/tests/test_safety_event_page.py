from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from unittest import TestCase

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PAGE_ROOT = PACKAGE_ROOT / "ione_clinical_quality" / "page" / "ione_safety_event_report"
PAGE_JSON = PAGE_ROOT / "ione_safety_event_report.json"
PAGE_JS = PAGE_ROOT / "ione_safety_event_report.js"
QUALITY_API = PACKAGE_ROOT / "api" / "quality.py"


def _api_function(name: str = "report_medical_safety_event") -> ast.FunctionDef:
	tree = ast.parse(QUALITY_API.read_text(encoding="utf-8"))
	return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)


class TestSafetyEventPageMetadata(TestCase):
	def test_page_is_standard_and_lives_in_clinical_quality(self) -> None:
		metadata = json.loads(PAGE_JSON.read_text(encoding="utf-8"))
		self.assertEqual(metadata["doctype"], "Page")
		self.assertEqual(metadata["page_name"], "ione-safety-event-report")
		self.assertEqual(metadata["name"], "ione-safety-event-report")
		self.assertEqual(metadata["module"], "IONE Clinical Quality")
		self.assertEqual(metadata["standard"], "Yes")

	def test_page_roles_exactly_match_backend_reporting_roles(self) -> None:
		metadata = json.loads(PAGE_JSON.read_text(encoding="utf-8"))
		page_roles = {row["role"] for row in metadata["roles"]}
		report_function = _api_function()
		require_role_call = next(
			call
			for call in ast.walk(report_function)
			if isinstance(call, ast.Call)
			and isinstance(call.func, ast.Name)
			and call.func.id == "require_role"
		)
		api_roles = {
			arg.value
			for arg in require_role_call.args
			if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
		}
		self.assertSetEqual(page_roles, api_roles)
		self.assertTrue(
			page_roles.isdisjoint(
				{
					"Guest",
					"Administrator",
					"System Manager",
					"IONE Agent Administrator",
					"IONE Agent Reviewer",
					"IONE Agent Service",
				}
			)
		)


class TestSafetyEventPageClientContract(TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		cls.source = PAGE_JS.read_text(encoding="utf-8")

	def test_client_calls_existing_governed_api_with_exact_parameters(self) -> None:
		self.assertIn(
			'"ione_qms.api.quality.report_safety_event"',
			self.source,
		)
		payload_match = re.search(
			r"build_payload\(values\)\s*\{\s*return\s*\{(?P<body>.*?)^\s*\};",
			self.source,
			flags=re.DOTALL | re.MULTILINE,
		)
		self.assertIsNotNone(payload_match)
		payload_keys = set(
			re.findall(
				r"^\s*([a-z][a-z0-9_]*)\s*:",
				payload_match.group("body"),
				flags=re.MULTILINE,
			)
		)
		api_parameters = {argument.arg for argument in _api_function().args.args}
		self.assertSetEqual(payload_keys, api_parameters)

	def test_public_page_endpoint_is_a_same_signature_security_delegate(self) -> None:
		governed_function = _api_function()
		page_endpoint = _api_function("report_safety_event")
		governed_parameters = [argument.arg for argument in governed_function.args.args]
		endpoint_parameters = [argument.arg for argument in page_endpoint.args.args]
		self.assertListEqual(endpoint_parameters, governed_parameters)

		delegate_call = next(
			call
			for call in ast.walk(page_endpoint)
			if isinstance(call, ast.Call)
			and isinstance(call.func, ast.Name)
			and call.func.id == "report_medical_safety_event"
		)
		self.assertSetEqual(
			{keyword.arg for keyword in delegate_call.keywords},
			set(governed_parameters),
		)
		self.assertTrue(
			any(
				isinstance(decorator, ast.Call)
				and isinstance(decorator.func, ast.Attribute)
				and decorator.func.attr == "whitelist"
				for decorator in page_endpoint.decorator_list
			)
		)

	def test_required_reporting_fields_and_modes_are_present(self) -> None:
		for fieldname in (
			"reporting_mode",
			"near_miss",
			"event_type",
			"severity",
			"event_time",
			"department",
			"ward",
			"narrative",
			"immediate_action",
		):
			with self.subTest(fieldname=fieldname):
				self.assertIn(f'fieldname: "{fieldname}"', self.source)
		self.assertIn('"实名上报"', self.source)
		self.assertIn('"匿名上报"', self.source)
		for severity in ("Low", "Medium", "High", "Critical"):
			self.assertIn(f'"{severity}"', self.source)

	def test_submission_is_confirmed_and_success_clears_form(self) -> None:
		confirm_position = self.source.index("await this.confirm_submission(values)")
		call_position = self.source.index("await frappe.xcall")
		clear_position = self.source.index("await this.clear_form()")
		success_position = self.source.index('title: __("上报成功")')
		self.assertLess(confirm_position, call_position)
		self.assertLess(call_position, clear_position)
		self.assertLess(clear_position, success_position)

	def test_sensitive_values_are_not_written_to_client_persistence_or_logs(self) -> None:
		for prohibited_token in (
			"localStorage",
			"sessionStorage",
			"URLSearchParams",
			"history.pushState",
			"history.replaceState",
			"window.location",
			"document.location",
			"console.",
			"frappe.set_route",
			"error.message",
			"frappe.session.user",
		):
			with self.subTest(prohibited_token=prohibited_token):
				self.assertNotIn(prohibited_token, self.source)

	def test_result_only_exposes_opaque_event_reference(self) -> None:
		self.assertIn("result.safety_event || result.event", self.source)
		self.assertNotIn("result.reported_by", self.source)
		self.assertNotIn("result.owner", self.source)
		self.assertNotIn("result.responsible_staff", self.source)
		self.assertNotIn("result.narrative", self.source)
