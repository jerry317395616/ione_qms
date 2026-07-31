from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from ione_qms.ai.governance import (
	QWEN_FLOW_MODEL,
	QWEN_MODEL_ID,
	_validate_policy_scope,
	_validate_qwen_model_configuration,
	_validate_scope_hierarchy,
	is_private_model_url,
)
from ione_qms.constants import AGENT_FORBIDDEN_TOOL_SLUGS
from ione_qms.setup.install import AGENT_BLUEPRINTS


class _FakeDoc(dict):
	def __init__(self, *, name: str, credential: str | None = None, **values) -> None:
		super().__init__(values)
		self.name = name
		self.meta = SimpleNamespace(has_field=lambda fieldname: fieldname in self)
		self._password = credential

	def get_password(self, fieldname: str, *, raise_exception: bool = False) -> str | None:
		del fieldname, raise_exception
		return self._password


class TestAIGovernance(TestCase):
	@patch("ione_qms.ai.governance._allowed_model_hosts", return_value=frozenset({"10.144.133.1"}))
	def test_private_allowlisted_model_endpoint_is_accepted(self, _allowed) -> None:
		self.assertTrue(is_private_model_url("http://10.144.133.1:1234/v1"))

	@patch("ione_qms.ai.governance._allowed_model_hosts", return_value=frozenset({"10.144.133.1"}))
	def test_credentials_or_query_in_model_url_are_rejected(self, _allowed) -> None:
		self.assertFalse(is_private_model_url("http://u:p@10.144.133.1:1234/v1"))
		self.assertFalse(is_private_model_url("http://10.144.133.1:1234/v1?token=x"))

	@patch("ione_qms.ai.governance._allowed_model_hosts", return_value=frozenset())
	def test_private_but_unapproved_endpoint_is_rejected(self, _allowed) -> None:
		self.assertFalse(is_private_model_url("http://10.144.133.99:1234/v1"))

	@patch(
		"ione_qms.ai.governance._allowed_model_hosts",
		return_value=frozenset({"models.internal"}),
	)
	def test_allowlisted_internal_hostname_is_accepted(self, _allowed) -> None:
		self.assertTrue(is_private_model_url("https://models.internal/v1"))

	@patch(
		"ione_qms.ai.governance._allowed_model_hosts",
		return_value=frozenset({"models.example.com"}),
	)
	def test_public_host_is_rejected_even_if_allowlisted(self, _allowed) -> None:
		self.assertFalse(is_private_model_url("https://models.example.com/v1"))

	@patch(
		"ione_qms.ai.governance._allowed_model_hosts",
		return_value=frozenset({"169.254.169.254", "0.0.0.0"}),  # noqa: S104
	)
	def test_link_local_and_unspecified_endpoints_are_rejected(self, _allowed) -> None:
		self.assertFalse(is_private_model_url("http://169.254.169.254/v1"))
		self.assertFalse(is_private_model_url("http://0.0.0.0:1234/v1"))

	@patch(
		"ione_qms.ai.governance._allowed_model_hosts",
		return_value=frozenset({"10.144.133.1"}),
	)
	def test_reviewed_qwen_configuration_is_exact_and_authenticated(self, _allowed) -> None:
		model = _FakeDoc(
			name=QWEN_FLOW_MODEL,
			credential="x",
			model_id=QWEN_MODEL_ID,
			base_url="http://10.144.133.1:1234/v1",
			api_key="set",
		)
		_validate_qwen_model_configuration(model)
		model["model_id"] = "openai/qwen3.6-35b-a3b-fp16"
		with (
			patch(
				"ione_qms.ai.governance.frappe.throw",
				side_effect=RuntimeError("invalid model"),
			),
			self.assertRaisesRegex(RuntimeError, "invalid model"),
		):
			_validate_qwen_model_configuration(model)

	def test_unreviewed_flow_model_is_rejected(self) -> None:
		model = _FakeDoc(
			name="Other Model",
			credential="x",
			model_id=QWEN_MODEL_ID,
			base_url="http://127.0.0.1:1234/v1",
			api_key="set",
		)
		with (
			patch(
				"ione_qms.ai.governance.frappe.throw",
				side_effect=RuntimeError("unreviewed model"),
			),
			self.assertRaisesRegex(RuntimeError, "unreviewed model"),
		):
			_validate_qwen_model_configuration(model)

	def test_patient_scope_requires_nonempty_exact_policy_allowlists(self) -> None:
		policy = {
			"agent_category": "Medical Record QC",
			"contains_patient_data": 1,
			"allowed_hospitals": [],
			"allowed_departments": [],
		}
		task = {
			"task_type": "Medical Record QC",
			"patient": "PAT-1",
			"hospital": "HOSP-1",
			"department": "DEPT-1",
		}
		with (
			patch(
				"ione_qms.ai.governance.frappe.throw",
				side_effect=RuntimeError("outside policy"),
			),
			patch("ione_qms.ai.governance._validate_scope_hierarchy"),
			self.assertRaisesRegex(RuntimeError, "outside policy"),
		):
			_validate_policy_scope(policy, task)

	def test_policy_scope_requires_exact_hospital_and_department_match(self) -> None:
		policy = {
			"agent_category": "Medical Record QC",
			"contains_patient_data": 1,
			"allowed_hospitals": [{"hospital": "HOSP-1"}],
			"allowed_departments": [{"department": "DEPT-1"}],
		}
		task = {
			"task_type": "Medical Record QC",
			"patient": "PAT-1",
			"hospital": "HOSP-1",
			"campus": "CAMPUS-1",
			"department": "DEPT-1",
			"ward": "WARD-1",
		}
		with patch("ione_qms.ai.governance._validate_scope_hierarchy") as hierarchy:
			_validate_policy_scope(policy, task)
		hierarchy.assert_called_once_with(
			hospital="HOSP-1",
			campus="CAMPUS-1",
			department="DEPT-1",
			ward="WARD-1",
		)

	def test_non_policy_agent_cannot_run_without_explicit_hospital_scope(self) -> None:
		policy = {
			"agent_category": "Quality Report",
			"contains_patient_data": 0,
			"allowed_hospitals": [{"hospital": "HOSP-1"}],
			"allowed_departments": [],
		}
		task = {"task_type": "Quality Report"}
		with (
			patch(
				"ione_qms.ai.governance.frappe.throw",
				side_effect=RuntimeError("hospital required"),
			),
			self.assertRaisesRegex(RuntimeError, "hospital required"),
		):
			_validate_policy_scope(policy, task)

	def test_task_type_must_match_active_policy_category(self) -> None:
		policy = {
			"agent_category": "Quality Report",
			"contains_patient_data": 0,
			"allowed_hospitals": [{"hospital": "HOSP-1"}],
			"allowed_departments": [],
		}
		task = {"task_type": "Indicator Analysis", "hospital": "HOSP-1"}
		with (
			patch(
				"ione_qms.ai.governance.frappe.throw",
				side_effect=RuntimeError("category mismatch"),
			),
			self.assertRaisesRegex(RuntimeError, "category mismatch"),
		):
			_validate_policy_scope(policy, task)

	def test_department_hierarchy_mismatch_is_rejected(self) -> None:
		scope_docs = {
			("IONE Hospital", "HOSP-1"): {"name": "HOSP-1"},
			("IONE Hospital Campus", "CAMPUS-1"): {
				"name": "CAMPUS-1",
				"hospital": "HOSP-1",
			},
			("IONE Medical Department", "DEPT-1"): {
				"name": "DEPT-1",
				"hospital": "HOSP-1",
				"campus": "CAMPUS-2",
			},
		}
		with (
			patch(
				"ione_qms.ai.governance._active_scope_doc",
				side_effect=lambda doctype, name: scope_docs[(doctype, name)],
			),
			patch(
				"ione_qms.ai.governance.frappe.throw",
				side_effect=RuntimeError("hierarchy mismatch"),
			),
			self.assertRaisesRegex(RuntimeError, "hierarchy mismatch"),
		):
			_validate_scope_hierarchy(
				hospital="HOSP-1",
				campus="CAMPUS-1",
				department="DEPT-1",
				ward="",
			)

	def test_required_agent_blueprints_are_complete_and_fail_closed(self) -> None:
		expected = {
			"IONE Quality Policy Agent",
			"IONE Medical Record QC Agent",
			"IONE Indicator Analysis Agent",
			"IONE Rectification Agent",
			"IONE Quality Report Agent",
		}
		self.assertEqual({str(item["title"]) for item in AGENT_BLUEPRINTS}, expected)
		self.assertEqual(len({str(item["policy_code"]) for item in AGENT_BLUEPRINTS}), len(expected))
		self.assertEqual(len({str(item["service_user"]) for item in AGENT_BLUEPRINTS}), len(expected))
		for item in AGENT_BLUEPRINTS:
			with self.subTest(agent=item["title"]):
				self.assertTrue(str(item["service_user"]).endswith("@ione-qms.local"))
				self.assertFalse(set(item["tools"]).intersection(AGENT_FORBIDDEN_TOOL_SLUGS))
				self.assertGreater(int(item["maximum_records_per_run"]), 0)
				if item["contains_patient_data"]:
					self.assertLessEqual(int(item["maximum_records_per_run"]), 20)
