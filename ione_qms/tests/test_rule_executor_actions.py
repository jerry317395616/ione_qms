from __future__ import annotations

from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from ione_qms.rule_engine.executor import evaluate_event
from ione_qms.rule_engine.models import RuleEvaluation, RuleResult


class TestRuleExecutorActions(TestCase):
	def test_failed_result_honors_reviewed_action(self) -> None:
		for action, expected_finding, expected_alert in (
			("Create Finding", 1, 0),
			("Alert Only", 0, 1),
			("Measure Only", 0, 0),
		):
			with self.subTest(action=action):
				event = MagicMock()
				event.name = "EVT-1"
				event.doctype = "IONE Clinical Quality Event"
				event.event_type = "EncounterUpdated"
				event.get.side_effect = {
					"event_type": "EncounterUpdated",
					"department": None,
				}.get
				version = frappe._dict(
					name=f"RULE-VERSION-{action}",
					rule="RULE-1",
					action=action,
					shadow_mode=0,
				)
				evaluation = RuleEvaluation(
					result=RuleResult.FAILED,
					reason="Matched",
				)
				with (
					patch(
						"ione_qms.rule_engine.executor.frappe.get_doc",
						return_value=event,
					),
					patch(
						"ione_qms.rule_engine.executor._build_context",
						return_value={},
					),
					patch(
						"ione_qms.rule_engine.executor._get_published_rules",
						return_value=[version],
					),
					patch(
						"ione_qms.rule_engine.executor.execute_rule_version",
						return_value=evaluation,
					),
					patch(
						"ione_qms.rule_engine.executor._save_execution",
						return_value="EXEC-1",
					),
					patch("ione_qms.rule_engine.executor.frappe.db.set_value"),
					patch("ione_qms.rule_engine.executor._create_candidate_finding") as create_finding,
					patch("ione_qms.rule_engine.executor._notify_rule_alert") as notify_alert,
				):
					result = evaluate_event("EVT-1")
				self.assertEqual(create_finding.call_count, expected_finding)
				self.assertEqual(notify_alert.call_count, expected_alert)
				self.assertEqual(result[0]["action"], action)
