from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from ione_qms.api import ai as ai_api
from ione_qms.api import integration as integration_api


@contextmanager
def _tracked_lock(events: list[str]):
	events.append("lock_enter")
	try:
		yield
	finally:
		events.append("lock_exit")


def _document(**values):
	doc = MagicMock()
	for key, value in values.items():
		setattr(doc, key, value)
	doc.get.side_effect = lambda key, default=None: values.get(key, default)
	return doc


class TestReviewDecisionConcurrency(TestCase):
	def test_conflicting_second_decisions_lock_and_reject_terminal_rows(self) -> None:
		cases = (
			(
				ai_api,
				ai_api.review_agent_evaluation,
				("EVAL-1", "Approve", "reviewed evaluation"),
				"IONE Agent Evaluation",
				_document(name="EVAL-1", review_status="Rejected"),
				"save",
			),
			(
				ai_api,
				ai_api.review_candidate_finding,
				("CANDIDATE-1", "Accept", "reviewed candidate"),
				"IONE AI Candidate Finding",
				_document(name="CANDIDATE-1", status="Rejected"),
				"save",
			),
			(
				ai_api,
				ai_api.review_report_draft,
				("DRAFT-1", "Reject", "reviewed report"),
				"IONE AI Report Draft",
				_document(name="DRAFT-1", status="Approved"),
				"save",
			),
			(
				integration_api,
				integration_api.review_data_quality_issue,
				("ISSUE-1", "Accept", "reviewed data quality issue"),
				"IONE Data Quality Issue",
				_document(name="ISSUE-1", status="Resolved"),
				"db_set",
			),
		)
		for module, action, args, doctype, doc, mutation in cases:
			with self.subTest(action=action.__name__):
				events: list[str] = []

				def get_doc(
					*call_args,
					_events=events,
					_doctype=doctype,
					_name=args[0],
					_doc=doc,
					**call_kwargs,
				):
					self.assertEqual(_events, ["lock_enter"])
					self.assertEqual(call_args, (_doctype, _name))
					self.assertEqual(call_kwargs, {"for_update": True})
					return _doc

				with (
					patch.object(module, "require_role"),
					patch.object(
						module.frappe,
						"session",
						SimpleNamespace(user="reviewer@example.test"),
					),
					patch.object(
						module.frappe.db,
						"advisory_lock",
						return_value=_tracked_lock(events),
					),
					patch.object(module.frappe, "get_doc", side_effect=get_doc),
					patch.object(
						module.frappe,
						"throw",
						side_effect=RuntimeError("conflicting decision"),
					),
					patch.object(module.frappe.db, "commit") as commit,
				):
					with self.assertRaisesRegex(RuntimeError, "conflicting decision"):
						action(*args)

				self.assertEqual(events, ["lock_enter", "lock_exit"])
				getattr(doc, mutation).assert_not_called()
				commit.assert_not_called()

	def test_agent_evaluation_commits_before_releasing_review_lock(self) -> None:
		events: list[str] = []
		doc = _document(
			name="EVAL-1",
			review_status="Pending Review",
			evaluated_by="worker@example.test",
			requested_by="requester@example.test",
			agent_release="RELEASE-1",
		)
		doc.save.side_effect = lambda **_kwargs: events.append("save")
		with (
			patch.object(ai_api, "require_role"),
			patch.object(
				ai_api.frappe,
				"session",
				SimpleNamespace(user="reviewer@example.test"),
			),
			patch.object(ai_api.frappe, "flags", SimpleNamespace()),
			patch.object(
				ai_api.frappe.db,
				"advisory_lock",
				return_value=_tracked_lock(events),
			),
			patch.object(ai_api.frappe, "get_doc", return_value=doc) as get_doc,
			patch.object(ai_api, "now_datetime", return_value="2026-07-30 12:00:00"),
			patch.object(
				ai_api.frappe.db,
				"commit",
				side_effect=lambda: events.append("commit"),
			),
		):
			result = ai_api.review_agent_evaluation(
				doc.name,
				"Reject",
				"reviewed evaluation",
			)

		get_doc.assert_any_call("IONE Agent Evaluation", doc.name, for_update=True)
		self.assertEqual(events, ["lock_enter", "save", "commit", "lock_exit"])
		self.assertEqual(result["review_status"], "Rejected")

	def test_candidate_finding_commits_before_releasing_review_lock(self) -> None:
		events: list[str] = []
		doc = _document(
			doctype="IONE AI Candidate Finding",
			name="CANDIDATE-1",
			status="Pending Review",
			task="TASK-1",
		)
		doc.save.side_effect = lambda **_kwargs: events.append("save")
		with (
			patch.object(ai_api, "require_role"),
			patch.object(
				ai_api.frappe,
				"session",
				SimpleNamespace(user="reviewer@example.test"),
			),
			patch.object(ai_api.frappe, "flags", SimpleNamespace()),
			patch.object(
				ai_api.frappe.db,
				"advisory_lock",
				return_value=_tracked_lock(events),
			),
			patch.object(ai_api.frappe, "get_doc", return_value=doc) as get_doc,
			patch.object(
				ai_api.frappe.db,
				"get_value",
				return_value="requester@example.test",
			),
			patch.object(ai_api.frappe.db, "savepoint"),
			patch.object(ai_api.frappe.db, "release_savepoint"),
			patch.object(ai_api, "now_datetime", return_value="2026-07-30 12:00:00"),
			patch.object(
				ai_api.frappe.db,
				"commit",
				side_effect=lambda: events.append("commit"),
			),
		):
			result = ai_api.review_candidate_finding(
				doc.name,
				"Reject",
				"reviewed candidate",
			)

		get_doc.assert_called_once_with("IONE AI Candidate Finding", doc.name, for_update=True)
		self.assertEqual(events, ["lock_enter", "save", "commit", "lock_exit"])
		self.assertEqual(result["status"], "Rejected")

	def test_report_draft_commits_before_releasing_review_lock(self) -> None:
		events: list[str] = []
		doc = _document(
			doctype="IONE AI Report Draft",
			name="DRAFT-1",
			status="Pending Review",
			task="TASK-1",
		)
		doc.save.side_effect = lambda **_kwargs: events.append("save")
		with (
			patch.object(ai_api, "require_role"),
			patch.object(
				ai_api.frappe,
				"session",
				SimpleNamespace(user="reviewer@example.test"),
			),
			patch.object(ai_api.frappe, "flags", SimpleNamespace()),
			patch.object(
				ai_api.frappe.db,
				"advisory_lock",
				return_value=_tracked_lock(events),
			),
			patch.object(ai_api.frappe, "get_doc", return_value=doc) as get_doc,
			patch.object(
				ai_api.frappe.db,
				"get_value",
				return_value={
					"requested_by": "requester@example.test",
					"origin": "Manual",
					"report_reviewer": None,
				},
			),
			patch.object(ai_api.frappe.db, "savepoint"),
			patch.object(ai_api.frappe.db, "release_savepoint"),
			patch.object(ai_api, "now_datetime", return_value="2026-07-30 12:00:00"),
			patch.object(
				ai_api.frappe.db,
				"commit",
				side_effect=lambda: events.append("commit"),
			),
		):
			result = ai_api.review_report_draft(
				doc.name,
				"Reject",
				"reviewed report",
			)

		get_doc.assert_called_once_with("IONE AI Report Draft", doc.name, for_update=True)
		self.assertEqual(events, ["lock_enter", "save", "commit", "lock_exit"])
		self.assertEqual(result["status"], "Rejected")

	def test_data_quality_issue_commits_before_releasing_review_lock(self) -> None:
		events: list[str] = []
		doc = _document(name="ISSUE-1", status="Open")
		doc.db_set.side_effect = lambda *_args, **_kwargs: events.append("db_set")
		with (
			patch.object(integration_api, "require_role"),
			patch.object(
				integration_api.frappe,
				"session",
				SimpleNamespace(user="reviewer@example.test"),
			),
			patch.object(
				integration_api.frappe.db,
				"advisory_lock",
				return_value=_tracked_lock(events),
			),
			patch.object(integration_api.frappe, "get_doc", return_value=doc) as get_doc,
			patch.object(
				integration_api.frappe.db,
				"commit",
				side_effect=lambda: events.append("commit"),
			),
		):
			result = integration_api.review_data_quality_issue(
				doc.name,
				"Investigate",
				"reviewed data quality issue",
			)

		get_doc.assert_called_once_with("IONE Data Quality Issue", doc.name, for_update=True)
		self.assertEqual(events, ["lock_enter", "db_set", "commit", "lock_exit"])
		self.assertEqual(result["status"], "Investigating")
