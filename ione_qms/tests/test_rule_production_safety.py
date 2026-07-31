from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from ione_qms.hooks import doc_events
from ione_qms.rule_engine import executor, validation
from ione_qms.rule_engine.models import EvidenceItem, RuleEvaluation, RuleResult
from ione_qms.tasks.quality import _pending_events


def _raise_runtime(message: str, *args, **kwargs) -> None:
	del args, kwargs
	raise RuntimeError(message)


class TestRuleProductionSafety(TestCase):
	def _evaluate(self, evaluation: RuleEvaluation):
		event = MagicMock()
		event.name = "EVT-1"
		event.doctype = "IONE Clinical Quality Event"
		event.event_type = "EncounterUpdated"
		event.event_time = "2026-07-01 10:00:00"
		event.get.side_effect = {
			"event_type": "EncounterUpdated",
			"department": None,
		}.get
		version = frappe._dict(
			name="RULE-1-v1",
			rule="RULE-1",
			action="Create Finding",
			shadow_mode=0,
		)
		patches = (
			patch.object(executor.frappe, "get_doc", return_value=event),
			patch.object(executor, "_build_context", return_value={}),
			patch.object(executor, "_get_published_rules", return_value=[version]),
			patch.object(executor, "execute_rule_version", return_value=evaluation),
			patch.object(executor, "_save_execution", return_value="EXEC-1"),
			patch.object(executor, "_create_candidate_finding"),
			patch.object(executor, "_upsert_rule_data_quality_issue"),
			patch.object(executor, "_notify_rule_error"),
			patch.object(executor.frappe.db, "set_value"),
			patch.object(executor, "_get_shadow_rules", return_value=[]),
		)
		return event, patches

	def test_rule_version_selection_uses_clinical_event_date(self) -> None:
		with patch.object(executor.frappe.db, "sql", return_value=[]) as sql:
			executor._get_published_rules(
				"EncounterUpdated",
				None,
				effective_date="2026-06-30",
			)
		query = sql.call_args.args[0]
		values = sql.call_args.kwargs["values"]
		self.assertNotIn("current_date", query.lower())
		self.assertNotIn("tabIONE QC Rule` r", query)
		self.assertIn("rv.status in ('Published', 'Retired')", query)
		self.assertIn("rv.rule_code_snapshot", query)
		self.assertEqual(values["effective_date"], "2026-06-30")

	def test_rule_execution_uses_only_version_pinned_semantics(self) -> None:
		version = frappe._dict(
			rule_type_snapshot="Deterministic",
			condition_json='{"field":"record.confirmed","operator":"eq","value":true}',
			exclusion_json=None,
		)
		with patch.object(executor.frappe.db, "get_value") as get_value:
			result = executor.execute_rule_version(version, {"record": {"confirmed": True}})
		self.assertEqual(result.result, RuleResult.FAILED)
		get_value.assert_not_called()

	def test_legacy_rule_without_semantic_snapshot_fails_closed(self) -> None:
		version = frappe._dict(
			rule_type_snapshot=None,
			condition_json='{"field":"record.confirmed","operator":"eq","value":true}',
			exclusion_json=None,
		)
		with patch.object(executor.frappe.db, "get_value") as get_value:
			result = executor.execute_rule_version(version, {"record": {"confirmed": True}})
		self.assertEqual(result.result, RuleResult.ERROR)
		self.assertIn("snapshot", result.reason.lower())
		get_value.assert_not_called()

	def test_error_execution_marks_event_error_and_is_alerted(self) -> None:
		evaluation = RuleEvaluation(
			result=RuleResult.ERROR,
			reason="Evaluation failed",
			error="RuntimeError",
		)
		event, patches = self._evaluate(evaluation)
		with (
			patches[0],
			patches[1],
			patches[2],
			patches[3],
			patches[4],
			patches[5] as create_finding,
			patches[6] as quality_issue,
			patches[7] as notify,
			patches[8] as set_value,
			patches[9],
		):
			results = executor.evaluate_event("EVT-1")
		self.assertEqual(results[0]["result"], "Error")
		create_finding.assert_not_called()
		quality_issue.assert_called_once()
		notify.assert_called_once()
		self.assertEqual(set_value.call_args.args[0:2], (event.doctype, event.name))
		self.assertEqual(set_value.call_args.args[2]["processing_status"], "Error")

	def test_insufficient_data_creates_issue_but_never_finding(self) -> None:
		evaluation = RuleEvaluation(
			result=RuleResult.INSUFFICIENT_DATA,
			reason="Missing required field",
			missing_fields=["diagnosis.code"],
		)
		_event, patches = self._evaluate(evaluation)
		with (
			patches[0],
			patches[1],
			patches[2],
			patches[3],
			patches[4],
			patches[5] as create_finding,
			patches[6] as quality_issue,
			patches[7],
			patches[8] as set_value,
			patches[9],
		):
			results = executor.evaluate_event("EVT-1")
		self.assertEqual(results[0]["result"], "Insufficient Data")
		create_finding.assert_not_called()
		quality_issue.assert_called_once()
		self.assertEqual(set_value.call_args.args[2]["processing_status"], "Completed")

	def test_internal_queue_entry_does_not_inherit_guest_read_permissions(self) -> None:
		event = MagicMock()
		with (
			patch.object(executor.frappe, "get_doc", return_value=event),
			patch.object(executor, "_evaluate_event", return_value=[]) as evaluate,
		):
			executor.evaluate_event_job("EVT-1")
		event.check_permission.assert_not_called()
		evaluate.assert_called_once()

	def test_rule_evidence_captures_source_version_clause_and_value_summary(self) -> None:
		finding = SimpleNamespace(name="FIND-1")
		event = MagicMock()
		event.name = "EVT-1"
		event.get.side_effect = {
			"source_system": "HIS",
			"source_record_type": "Encounter",
			"source_record_id": "SRC-1",
			"source_version": "7",
			"payload_reference": "MSG-1",
			"event_time": "2026-07-01 10:00:00",
		}.get
		version = frappe._dict(
			name="RULE-1-v1",
			rule="RULE-1",
			checksum="a" * 64,
			standard_clause_snapshot="CLAUSE-1",
		)
		evaluation = RuleEvaluation(
			result=RuleResult.FAILED,
			reason="Threshold matched",
			evidence=[
				EvidenceItem(
					field="risk.score",
					operator="gt",
					expected=3,
					actual=5,
					matched=True,
				)
			],
		)
		evidence_doc = MagicMock()
		evidence_doc.evidence_hash = None
		with (
			patch.object(executor.frappe.db, "exists", side_effect=[True, False]),
			patch.object(executor.frappe, "get_cached_doc") as get_cached_doc,
			patch.object(executor.frappe, "get_doc", return_value=evidence_doc) as get_doc,
			patch.object(executor, "_document_content_hash", return_value="b" * 64),
		):
			executor._persist_finding_evidence(
				finding,
				event,
				version,
				"EXEC-1",
				evaluation,
			)
		values = get_doc.call_args.args[0]
		payload = json.loads(values["evidence_json"])
		self.assertEqual(values["execution"], "EXEC-1")
		self.assertEqual(values["rule_version"], "RULE-1-v1")
		self.assertEqual(values["standard_clause"], "CLAUSE-1")
		self.assertEqual(payload["source"]["version"], "7")
		self.assertEqual(payload["actual_summary"], 5)
		self.assertEqual(evidence_doc.evidence_hash, "b" * 64)
		get_cached_doc.assert_not_called()
		evidence_doc.insert.assert_called_once_with(ignore_permissions=True)

	def test_error_events_remain_in_the_retry_scan(self) -> None:
		with (
			patch("ione_qms.tasks.quality._has_field", return_value=True),
			patch("ione_qms.tasks.quality.frappe.get_all", return_value=[]) as get_all,
		):
			_pending_events(None, 10)
		self.assertEqual(
			get_all.call_args.kwargs["filters"]["processing_status"],
			["in", ["Pending", "Error"]],
		)

	def test_current_checksum_requires_real_replay_artifact(self) -> None:
		version = frappe._dict(name="RULE-1-v1", checksum="a" * 64)
		with (
			patch.object(validation.frappe.db, "exists", return_value=True),
			patch.object(validation.frappe.db, "get_value", return_value=None),
			patch.object(validation.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "completed validation artifact"),
		):
			validation.require_validation_artifact(version, "Historical Replay")

	def test_validation_artifact_is_append_only(self) -> None:
		self.assertEqual(
			doc_events["IONE QC Rule Validation Run"]["validate"],
			"ione_qms.services.immutability.validate_append_only",
		)
