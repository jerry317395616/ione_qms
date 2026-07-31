from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from ione_qms import permissions
from ione_qms.services import finding_appeals


class _Flags(dict):
	def __getattr__(self, key: str):
		return self.get(key)


class _Document:
	def __init__(self, **values) -> None:
		self.__dict__.update(values)
		self.checked_permissions: list[str] = []

	def get(self, key: str, default=None):
		return getattr(self, key, default)

	def check_permission(self, permission_type: str) -> None:
		self.checked_permissions.append(permission_type)

	def insert(self, *, ignore_permissions: bool = False) -> None:
		self.insert_ignore_permissions = ignore_permissions
		self.insert_capability = finding_appeals.frappe.flags.get("ione_finding_appeal_create")
		self.evidence_create_capability = finding_appeals.frappe.flags.get(
			"ione_finding_appeal_evidence_create"
		)
		self.name = getattr(self, "name", "APPEAL-0001")

	def save(self, *, ignore_permissions: bool = False) -> None:
		self.save_ignore_permissions = ignore_permissions
		self.save_capability = finding_appeals.frappe.flags.get("ione_finding_appeal_review")
		self.withdraw_capability = finding_appeals.frappe.flags.get("ione_finding_appeal_withdraw")
		self.evidence_file_binding_capability = finding_appeals.frappe.flags.get(
			"ione_finding_appeal_evidence_file_bind"
		)

	def get_doc_before_save(self):
		return getattr(self, "previous", None)


def _finding(status: str = "Confirmed") -> _Document:
	return _Document(
		doctype="IONE QC Finding",
		name="FINDING-0001",
		status=status,
		hospital="HOSPITAL-1",
		campus="CAMPUS-1",
		department="DEPARTMENT-1",
		ward="WARD-1",
		patient="PATIENT-1",
		encounter="ENCOUNTER-1",
		responsible_staff="STAFF-1",
	)


class TestFindingAppealService(TestCase):
	def test_submission_serializes_finding_and_creates_exact_scoped_audit_record(self) -> None:
		now = datetime(2026, 7, 30, 12, 0, 0)
		finding = _finding()
		created: list[_Document] = []
		get_doc_calls: list[tuple] = []
		sync_contexts: list[dict[str, str] | None] = []

		def get_doc(*args, **kwargs):
			get_doc_calls.append((*args, kwargs))
			if len(args) == 1 and isinstance(args[0], dict):
				doc = _Document(**args[0])
				created.append(doc)
				return doc
			if args[:2] == ("IONE QC Finding", finding.name):
				return finding
			raise AssertionError((args, kwargs))

		def apply_workflow(doc, action):
			sync_contexts.append(finding_appeals.frappe.flags.get("ione_finding_appeal_sync"))
			self.assertEqual(action, finding_appeals.APPEAL_FINDING_ACTION)
			doc.status = "Appealed"
			return doc

		flags = _Flags()
		with (
			patch.object(finding_appeals.frappe, "session", SimpleNamespace(user="doctor@example.test")),
			patch.object(finding_appeals.frappe, "flags", flags),
			patch.object(
				finding_appeals.frappe,
				"get_roles",
				return_value=["IONE Physician"],
			),
			patch.object(finding_appeals, "_finding_appeal_lock", return_value=nullcontext()) as lock,
			patch.object(finding_appeals.frappe, "get_doc", side_effect=get_doc),
			patch.object(finding_appeals.frappe.db, "get_value", return_value=None) as get_value,
			patch.object(finding_appeals.frappe.db, "savepoint") as savepoint,
			patch.object(finding_appeals.frappe.db, "rollback") as rollback,
			patch.object(finding_appeals.frappe.db, "release_savepoint") as release,
			patch.object(finding_appeals, "finding_permission", return_value=True),
			patch.object(finding_appeals, "apply_workflow", side_effect=apply_workflow),
			patch.object(finding_appeals, "now_datetime", return_value=now),
		):
			result = finding_appeals.submit_finding_appeal(
				finding.name,
				"Clinical Exception",
				"Patient-specific contraindication was documented.",
				"See the private clinical evidence.",
			)

		lock.assert_called_once_with(finding.name)
		self.assertIn(("IONE QC Finding", finding.name, {"for_update": True}), get_doc_calls)
		get_value.assert_called_once_with(
			finding_appeals.APPEAL_DOCTYPE,
			{"active_key": finding.name},
			"name",
			for_update=True,
		)
		appeal = created[0]
		self.assertTrue(appeal.insert_ignore_permissions)
		self.assertEqual(appeal.insert_capability["finding"], finding.name)
		self.assertEqual(appeal.active_key, finding.name)
		self.assertEqual(appeal.status, "Submitted")
		self.assertEqual(appeal.submitted_by, "doctor@example.test")
		self.assertEqual(appeal.submitted_at, now)
		for fieldname in finding_appeals._FINDING_SCOPE_FIELDS:
			self.assertEqual(appeal.get(fieldname), finding.get(fieldname))
		self.assertEqual(sync_contexts[0]["appeal"], appeal.name)
		self.assertEqual(sync_contexts[0]["source_status"], "Confirmed")
		self.assertEqual(sync_contexts[0]["target_status"], "Appealed")
		self.assertEqual(result, {"appeal": appeal.name, "finding": finding.name, "status": "Submitted"})
		savepoint.assert_called_once()
		release.assert_called_once_with(savepoint.call_args.args[0])
		rollback.assert_not_called()
		self.assertEqual(flags, {})

	def test_submission_retry_returns_same_locked_active_appeal(self) -> None:
		finding = _finding("Appealed")
		existing = _Document(
			name="APPEAL-0001",
			finding=finding.name,
			active_key=finding.name,
			status="Submitted",
			submitted_by="doctor@example.test",
			submission_hash="HASH",
		)

		def get_doc(*args, **kwargs):
			if args[:2] == ("IONE QC Finding", finding.name):
				return finding
			if args[:2] == (finding_appeals.APPEAL_DOCTYPE, existing.name):
				return existing
			raise AssertionError((args, kwargs))

		with (
			patch.object(finding_appeals.frappe, "session", SimpleNamespace(user=existing.submitted_by)),
			patch.object(finding_appeals.frappe, "flags", _Flags()),
			patch.object(finding_appeals.frappe, "get_roles", return_value=["IONE Physician"]),
			patch.object(finding_appeals, "_submission_hash", return_value="HASH"),
			patch.object(finding_appeals, "_finding_appeal_lock", return_value=nullcontext()),
			patch.object(finding_appeals.frappe, "get_doc", side_effect=get_doc),
			patch.object(finding_appeals.frappe.db, "get_value", return_value=existing.name),
			patch.object(finding_appeals.frappe.db, "savepoint"),
			patch.object(finding_appeals.frappe.db, "release_savepoint"),
			patch.object(finding_appeals, "finding_permission", return_value=True),
			patch.object(finding_appeals, "apply_workflow") as apply_workflow,
		):
			result = finding_appeals.submit_finding_appeal(
				finding.name,
				"Factual Dispute",
				"This retry has identical content.",
			)

		self.assertEqual(result["appeal"], existing.name)
		apply_workflow.assert_not_called()

	def test_review_locks_both_rows_and_synchronizes_final_states(self) -> None:
		now = datetime(2026, 7, 30, 13, 0, 0)
		finding = _finding("Appealed")
		preview = _Document(name="APPEAL-0001", finding=finding.name)
		appeal = _Document(
			name=preview.name,
			finding=finding.name,
			active_key=finding.name,
			status="Submitted",
			submitted_by="doctor@example.test",
		)
		get_doc_calls: list[tuple] = []
		sync_contexts: list[dict[str, str] | None] = []

		def get_doc(*args, **kwargs):
			get_doc_calls.append((*args, kwargs))
			if args[:2] == (finding_appeals.APPEAL_DOCTYPE, appeal.name) and not kwargs:
				return preview
			if args[:2] == ("IONE QC Finding", finding.name):
				return finding
			if args[:2] == (finding_appeals.APPEAL_DOCTYPE, appeal.name):
				return appeal
			raise AssertionError((args, kwargs))

		def apply_workflow(doc, action):
			sync_contexts.append(finding_appeals.frappe.flags.get("ione_finding_appeal_sync"))
			self.assertEqual(action, finding_appeals.APPROVE_APPEAL_ACTION)
			doc.status = "Appeal Approved"
			return doc

		flags = _Flags()
		with (
			patch.object(finding_appeals.frappe, "session", SimpleNamespace(user="reviewer@example.test")),
			patch.object(finding_appeals.frappe, "flags", flags),
			patch.object(finding_appeals.frappe, "get_roles", return_value=["IONE QC Reviewer"]),
			patch.object(finding_appeals, "_finding_appeal_lock", return_value=nullcontext()) as lock,
			patch.object(finding_appeals.frappe, "get_doc", side_effect=get_doc),
			patch.object(finding_appeals.frappe.db, "savepoint"),
			patch.object(finding_appeals.frappe.db, "rollback") as rollback,
			patch.object(finding_appeals.frappe.db, "release_savepoint"),
			patch.object(finding_appeals, "finding_permission", return_value=True),
			patch.object(finding_appeals, "apply_workflow", side_effect=apply_workflow),
			patch.object(finding_appeals, "now_datetime", return_value=now),
		):
			result = finding_appeals.review_finding_appeal(
				appeal.name,
				"Approve",
				"Clinical exception is supported.",
			)

		lock.assert_called_once_with(finding.name)
		self.assertIn(("IONE QC Finding", finding.name, {"for_update": True}), get_doc_calls)
		self.assertIn(
			(finding_appeals.APPEAL_DOCTYPE, appeal.name, {"for_update": True}),
			get_doc_calls,
		)
		self.assertTrue(appeal.save_ignore_permissions)
		self.assertEqual(appeal.save_capability["appeal"], appeal.name)
		self.assertEqual(appeal.status, "Approved")
		self.assertIsNone(appeal.active_key)
		self.assertEqual(appeal.reviewed_by, "reviewer@example.test")
		self.assertEqual(appeal.reviewed_at, now)
		self.assertEqual(sync_contexts[0]["target_status"], "Appeal Approved")
		self.assertEqual(result["finding_status"], "Appeal Approved")
		rollback.assert_not_called()
		self.assertEqual(flags, {})

	def test_review_workflow_failure_rolls_back_appeal_change(self) -> None:
		finding = _finding("Appealed")
		preview = _Document(name="APPEAL-0001", finding=finding.name)
		appeal = _Document(
			name=preview.name,
			finding=finding.name,
			active_key=finding.name,
			status="Submitted",
			submitted_by="doctor@example.test",
		)

		def get_doc(*args, **kwargs):
			if args[:2] == (finding_appeals.APPEAL_DOCTYPE, appeal.name) and not kwargs:
				return preview
			if args[:2] == ("IONE QC Finding", finding.name):
				return finding
			return appeal

		with (
			patch.object(finding_appeals.frappe, "session", SimpleNamespace(user="reviewer@example.test")),
			patch.object(finding_appeals.frappe, "flags", _Flags()),
			patch.object(finding_appeals.frappe, "get_roles", return_value=["IONE Medical Affairs"]),
			patch.object(finding_appeals, "_finding_appeal_lock", return_value=nullcontext()),
			patch.object(finding_appeals.frappe, "get_doc", side_effect=get_doc),
			patch.object(finding_appeals.frappe.db, "savepoint") as savepoint,
			patch.object(finding_appeals.frappe.db, "rollback") as rollback,
			patch.object(finding_appeals.frappe.db, "release_savepoint") as release,
			patch.object(finding_appeals, "finding_permission", return_value=True),
			patch.object(finding_appeals, "apply_workflow", side_effect=RuntimeError("workflow failed")),
			patch.object(finding_appeals, "now_datetime", return_value=datetime(2026, 7, 30, 13, 0)),
			self.assertRaisesRegex(RuntimeError, "workflow failed"),
		):
			finding_appeals.review_finding_appeal(
				appeal.name,
				"Reject",
				"Evidence does not support the appeal.",
			)

		rollback.assert_called_once_with(save_point=savepoint.call_args.args[0])
		release.assert_not_called()

	def test_original_submitter_can_withdraw_and_restore_confirmed_finding(self) -> None:
		now = datetime(2026, 7, 30, 14, 0, 0)
		finding = _finding("Appealed")
		appeal = _Document(
			name="APPEAL-0001",
			finding=finding.name,
			active_key=finding.name,
			status="Submitted",
			submitted_by="doctor@example.test",
		)

		def get_doc(*args, **kwargs):
			if args[:2] == ("IONE QC Finding", finding.name):
				return finding
			return appeal

		def apply_workflow(doc, action):
			self.assertEqual(action, finding_appeals.WITHDRAW_APPEAL_ACTION)
			doc.status = "Confirmed"
			return doc

		with (
			patch.object(finding_appeals.frappe, "session", SimpleNamespace(user=appeal.submitted_by)),
			patch.object(finding_appeals.frappe, "flags", _Flags()),
			patch.object(finding_appeals.frappe, "get_roles", return_value=["IONE Physician"]),
			patch.object(finding_appeals, "_finding_appeal_lock", return_value=nullcontext()),
			patch.object(finding_appeals.frappe, "get_doc", side_effect=get_doc),
			patch.object(finding_appeals.frappe.db, "savepoint"),
			patch.object(finding_appeals.frappe.db, "rollback") as rollback,
			patch.object(finding_appeals.frappe.db, "release_savepoint"),
			patch.object(finding_appeals, "finding_permission", return_value=True),
			patch.object(finding_appeals, "apply_workflow", side_effect=apply_workflow),
			patch.object(finding_appeals, "now_datetime", return_value=now),
		):
			result = finding_appeals.withdraw_finding_appeal(
				appeal.name,
				"Submitted in error; withdrawing before review.",
			)

		self.assertEqual(appeal.status, "Withdrawn")
		self.assertIsNone(appeal.active_key)
		self.assertEqual(appeal.withdrawn_by, appeal.submitted_by)
		self.assertEqual(appeal.withdrawn_at, now)
		self.assertEqual(appeal.withdraw_capability["appeal"], appeal.name)
		self.assertEqual(result["finding_status"], "Confirmed")
		rollback.assert_not_called()

	def test_submitter_cannot_review_the_same_appeal(self) -> None:
		finding = _finding("Appealed")
		appeal = _Document(
			name="APPEAL-0001",
			finding=finding.name,
			active_key=finding.name,
			status="Submitted",
			submitted_by="dual-role@example.test",
		)

		def get_doc(*args, **kwargs):
			if args[:2] == ("IONE QC Finding", finding.name):
				return finding
			return appeal

		with (
			patch.object(
				finding_appeals.frappe,
				"session",
				SimpleNamespace(user=appeal.submitted_by),
			),
			patch.object(finding_appeals.frappe, "flags", _Flags()),
			patch.object(finding_appeals.frappe, "get_roles", return_value=["IONE QC Reviewer"]),
			patch.object(finding_appeals, "_finding_appeal_lock", return_value=nullcontext()),
			patch.object(finding_appeals.frappe, "get_doc", side_effect=get_doc),
			patch.object(finding_appeals.frappe.db, "savepoint"),
			patch.object(finding_appeals.frappe.db, "rollback"),
			patch.object(finding_appeals, "finding_permission", return_value=True),
			patch.object(
				finding_appeals.frappe,
				"throw",
				side_effect=RuntimeError("self review"),
			),
			patch.object(finding_appeals, "apply_workflow") as apply_workflow,
			self.assertRaisesRegex(RuntimeError, "self review"),
		):
			finding_appeals.review_finding_appeal(
				appeal.name,
				"Approve",
				"This comment is sufficiently long.",
			)
		apply_workflow.assert_not_called()

	def test_review_rejects_submitted_appeal_with_mismatched_active_key(self) -> None:
		finding = _finding("Appealed")
		appeal = _Document(
			name="APPEAL-0001",
			finding=finding.name,
			active_key="FINDING-OTHER",
			status="Submitted",
			submitted_by="doctor@example.test",
		)

		def get_doc(*args, **kwargs):
			if args[:2] == ("IONE QC Finding", finding.name):
				return finding
			return appeal

		with (
			patch.object(
				finding_appeals.frappe,
				"session",
				SimpleNamespace(user="reviewer@example.test"),
			),
			patch.object(finding_appeals.frappe, "flags", _Flags()),
			patch.object(finding_appeals.frappe, "get_roles", return_value=["IONE QC Reviewer"]),
			patch.object(finding_appeals, "_finding_appeal_lock", return_value=nullcontext()),
			patch.object(finding_appeals.frappe, "get_doc", side_effect=get_doc),
			patch.object(finding_appeals.frappe.db, "savepoint"),
			patch.object(finding_appeals.frappe.db, "rollback"),
			patch.object(finding_appeals, "finding_permission", return_value=True),
			patch.object(
				finding_appeals.frappe,
				"throw",
				side_effect=RuntimeError("active key mismatch"),
			),
			patch.object(finding_appeals, "apply_workflow") as apply_workflow,
			self.assertRaisesRegex(RuntimeError, "active key mismatch"),
		):
			finding_appeals.review_finding_appeal(
				appeal.name,
				"Approve",
				"This comment is sufficiently long.",
			)
		apply_workflow.assert_not_called()

	def test_evidence_validation_locks_exact_private_local_file_and_checks_owner(self) -> None:
		content_hash = "a" * 64
		file_doc = _Document(name="FILE-0001")
		row = {
			"name": file_doc.name,
			"owner": "doctor@example.test",
			"is_private": 1,
			"file_name": "evidence.pdf",
			"file_size": 1_024,
			"file_url": "/private/files/evidence.pdf",
			"content_hash": content_hash,
			"attached_to_doctype": None,
			"attached_to_name": None,
			"attached_to_field": None,
		}
		with (
			patch.object(
				finding_appeals.frappe.db,
				"get_value",
				return_value=row,
			) as get_value,
			patch.object(finding_appeals.frappe, "get_doc", return_value=file_doc),
		):
			result = finding_appeals._get_evidence_file_row(
				file_doc.name,
				content_hash,
				submitted_by=row["owner"],
				require_unattached=True,
				for_update=True,
			)
		self.assertEqual(result, row)
		get_value.assert_called_once_with(
			"File",
			file_doc.name,
			[
				"name",
				"owner",
				"is_private",
				"file_name",
				"file_size",
				"file_url",
				"content_hash",
				"attached_to_doctype",
				"attached_to_name",
				"attached_to_field",
			],
			as_dict=True,
			for_update=True,
		)
		self.assertEqual(file_doc.checked_permissions, ["read"])

	def test_drive_team_storage_key_cannot_be_submitted_as_appeal_evidence(self) -> None:
		content_hash = "a" * 64
		file_doc = _Document(name="DRIVE-FILE-0001")
		row = {
			"name": file_doc.name,
			"owner": "doctor@example.test",
			"is_private": 1,
			"file_name": "team-evidence.pdf",
			"file_size": 1_024,
			"file_url": "teams/TEAM-1/files/team-evidence.pdf",
			"content_hash": content_hash,
			"attached_to_doctype": None,
			"attached_to_name": None,
			"attached_to_field": None,
		}
		with (
			patch.object(finding_appeals.frappe.db, "get_value", return_value=row),
			patch.object(finding_appeals.frappe, "get_doc", return_value=file_doc),
			self.assertRaisesRegex(finding_appeals.frappe.ValidationError, "private local File"),
		):
			finding_appeals._get_evidence_file_row(
				file_doc.name,
				content_hash,
				submitted_by=row["owner"],
				require_unattached=True,
				for_update=True,
			)

	def test_evidence_creation_binds_exact_file_to_isolated_record_under_capabilities(self) -> None:
		user = "doctor@example.test"
		content_hash = "a" * 64
		submission_hash = "b" * 64
		finding = _finding()
		evidence_file = _Document(
			doctype="File",
			name="FILE-0001",
			owner=user,
			content_hash=content_hash,
			attached_to_doctype=None,
			attached_to_name=None,
			attached_to_field=None,
		)
		created: list[_Document] = []

		def get_doc(*args, **kwargs):
			if len(args) == 1 and isinstance(args[0], dict):
				doc = _Document(**args[0])
				doc.name = "EVIDENCE-0001"
				created.append(doc)
				return doc
			if args[:2] == ("File", evidence_file.name):
				return evidence_file
			raise AssertionError((args, kwargs))

		flags = _Flags()
		with (
			patch.object(finding_appeals.frappe, "session", SimpleNamespace(user=user)),
			patch.object(finding_appeals.frappe, "flags", flags),
			patch.object(finding_appeals.frappe, "get_doc", side_effect=get_doc),
			patch.object(
				finding_appeals,
				"_get_evidence_file_row",
				return_value={"file_name": "evidence.pdf", "file_size": 1_024},
			),
			patch.object(finding_appeals, "_validate_evidence_hash_siblings"),
			patch.object(finding_appeals, "now_datetime", return_value=datetime(2026, 7, 30, 12, 0)),
		):
			evidence = finding_appeals._create_appeal_evidence(
				finding,
				submitted_by=user,
				submission_hash=submission_hash,
				evidence_file=evidence_file.name,
				evidence_content_hash=content_hash,
			)

		self.assertIs(evidence, created[0])
		self.assertTrue(evidence.insert_ignore_permissions)
		self.assertEqual(evidence.evidence_create_capability["finding"], finding.name)
		self.assertEqual(
			evidence.evidence_create_capability["submission_hash"],
			submission_hash,
		)
		self.assertTrue(evidence_file.save_ignore_permissions)
		self.assertEqual(
			evidence_file.attached_to_doctype,
			finding_appeals.APPEAL_EVIDENCE_DOCTYPE,
		)
		self.assertEqual(evidence_file.attached_to_name, evidence.name)
		self.assertIsNone(evidence_file.attached_to_field)
		self.assertEqual(
			evidence_file.evidence_file_binding_capability["content_hash"],
			content_hash,
		)
		self.assertEqual(flags, {})

	def test_named_actor_policy_blocks_special_and_agent_service_accounts(self) -> None:
		cases = (
			("Guest", ["IONE Physician"]),
			("Administrator", ["IONE Medical Affairs"]),
			("agent@example.test", ["IONE Agent Service", "IONE QC Reviewer"]),
			("user@example.test", ["System Manager"]),
		)
		for user, roles in cases:
			with (
				self.subTest(user=user),
				patch.object(finding_appeals.frappe, "session", SimpleNamespace(user=user)),
				patch.object(finding_appeals.frappe, "get_roles", return_value=roles),
				patch.object(
					finding_appeals.frappe,
					"throw",
					side_effect=RuntimeError("named actor required"),
				),
				self.assertRaisesRegex(RuntimeError, "named actor required"),
			):
				finding_appeals._require_named_actor(finding_appeals.APPEAL_REVIEWER_ROLES)


class TestFindingAppealGuardrails(TestCase):
	def test_public_or_remote_hash_sibling_blocks_evidence_binding(self) -> None:
		content_hash = "a" * 64
		with (
			patch.object(
				finding_appeals.frappe,
				"get_all",
				return_value=[
					{
						"name": "FILE-0001",
						"is_private": 1,
						"file_url": "/private/files/evidence.pdf",
					},
					{
						"name": "FILE-PUBLIC",
						"is_private": 0,
						"file_url": "/files/evidence.pdf",
					},
				],
			),
			patch.object(
				finding_appeals.frappe,
				"throw",
				side_effect=RuntimeError("public sibling"),
			),
			self.assertRaisesRegex(RuntimeError, "public sibling"),
		):
			finding_appeals._validate_evidence_hash_siblings("FILE-0001", content_hash)

	def test_protected_file_private_or_hash_mutation_is_rejected(self) -> None:
		content_hash = "a" * 64
		doc = _Document(
			doctype="File",
			name="FILE-0001",
			owner="doctor@example.test",
			is_private=0,
			file_url="/files/evidence.pdf",
			content_hash=content_hash,
			file_name="evidence.pdf",
			file_size=1_024,
			attached_to_doctype=finding_appeals.APPEAL_EVIDENCE_DOCTYPE,
			attached_to_name="EVIDENCE-0001",
			attached_to_field=None,
		)
		doc.previous = {
			"name": doc.name,
			"owner": doc.owner,
			"is_private": 1,
			"file_url": "/private/files/evidence.pdf",
			"content_hash": content_hash,
			"file_name": doc.file_name,
			"file_size": doc.file_size,
			"attached_to_doctype": doc.attached_to_doctype,
			"attached_to_name": doc.attached_to_name,
			"attached_to_field": None,
		}
		with (
			patch.object(finding_appeals.frappe, "flags", _Flags()),
			patch.object(finding_appeals, "_appeal_evidence_doctype_available", return_value=True),
			patch.object(finding_appeals.frappe.db, "get_value", return_value="EVIDENCE-0001"),
			patch.object(
				finding_appeals.frappe,
				"throw",
				side_effect=RuntimeError("immutable evidence File"),
			),
			self.assertRaisesRegex(RuntimeError, "immutable evidence File"),
		):
			finding_appeals.validate_finding_appeal_evidence_file(doc)

	def test_hash_sharing_file_is_guarded_even_when_not_directly_attached(self) -> None:
		content_hash = "b" * 64
		doc = _Document(
			doctype="File",
			name="FILE-SIBLING",
			content_hash="c" * 64,
			is_private=1,
		)
		doc.previous = {
			"name": doc.name,
			"content_hash": content_hash,
			"is_private": 1,
		}

		def get_value(_doctype, filters, _fieldname, **_kwargs):
			return "EVIDENCE-0001" if filters.get("content_hash") == content_hash else None

		with (
			patch.object(finding_appeals.frappe, "flags", _Flags()),
			patch.object(finding_appeals, "_appeal_evidence_doctype_available", return_value=True),
			patch.object(finding_appeals.frappe.db, "get_value", side_effect=get_value),
			patch.object(
				finding_appeals.frappe,
				"throw",
				side_effect=RuntimeError("hash-linked evidence"),
			),
			self.assertRaisesRegex(RuntimeError, "hash-linked evidence"),
		):
			finding_appeals.validate_finding_appeal_evidence_file(doc)

	def test_shared_protected_url_blocks_a_new_file_even_with_a_different_hash(self) -> None:
		doc = _Document(
			doctype="File",
			name="FILE-NEW",
			content_hash="c" * 64,
			is_private=1,
			file_url="/private/files/evidence.pdf",
		)
		with (
			patch.object(finding_appeals.frappe, "flags", _Flags()),
			patch.object(finding_appeals, "_appeal_evidence_doctype_available", return_value=True),
			patch.object(
				finding_appeals.frappe.db,
				"get_value",
				side_effect=[None, None, "EVIDENCE-0001"],
			),
			patch.object(
				finding_appeals.frappe,
				"get_all",
				return_value=["FILE-PROTECTED"],
			),
			patch.object(
				finding_appeals.frappe,
				"throw",
				side_effect=RuntimeError("protected URL"),
			),
			self.assertRaisesRegex(RuntimeError, "protected URL"),
		):
			finding_appeals.validate_finding_appeal_evidence_file(doc)

	def test_exact_governed_file_binding_is_the_only_attachment_change_allowed(self) -> None:
		content_hash = "d" * 64
		user = "doctor@example.test"
		evidence_name = "EVIDENCE-0001"
		doc = _Document(
			doctype="File",
			name="FILE-0001",
			owner=user,
			content_hash=content_hash,
			is_private=1,
			file_url="/private/files/evidence.pdf",
			file_name="evidence.pdf",
			file_size=1_024,
			attached_to_doctype=finding_appeals.APPEAL_EVIDENCE_DOCTYPE,
			attached_to_name=evidence_name,
			attached_to_field=None,
		)
		doc.previous = {
			"name": doc.name,
			"owner": user,
			"content_hash": content_hash,
			"is_private": 1,
			"file_url": doc.file_url,
			"file_name": doc.file_name,
			"file_size": doc.file_size,
			"attached_to_doctype": None,
			"attached_to_name": None,
			"attached_to_field": None,
		}
		flags = _Flags(
			ione_finding_appeal_evidence_file_bind={
				"evidence": evidence_name,
				"evidence_file": doc.name,
				"content_hash": content_hash,
				"submitted_by": user,
			}
		)
		with (
			patch.object(finding_appeals.frappe, "session", SimpleNamespace(user=user)),
			patch.object(finding_appeals.frappe, "flags", flags),
			patch.object(finding_appeals, "_appeal_evidence_doctype_available", return_value=True),
			patch.object(
				finding_appeals.frappe.db,
				"get_value",
				side_effect=[
					evidence_name,
					{
						"evidence_file": doc.name,
						"content_hash": content_hash,
						"submitted_by": user,
					},
				],
			),
		):
			finding_appeals.validate_finding_appeal_evidence_file(doc)

	def test_direct_finding_transition_is_rejected_without_exact_persisted_appeal(self) -> None:
		doc = _Document(name="FINDING-0001", status="Appealed")
		doc.previous = {"status": "Confirmed"}
		with (
			patch.object(finding_appeals.frappe, "flags", _Flags()),
			patch.object(
				finding_appeals.frappe,
				"throw",
				side_effect=RuntimeError("structured appeal required"),
			),
			self.assertRaisesRegex(RuntimeError, "structured appeal required"),
		):
			finding_appeals.validate_finding_appeal_transition(doc)

	def test_exact_capability_requires_matching_persisted_appeal(self) -> None:
		doc = _Document(name="FINDING-0001", status="Appealed")
		doc.previous = {"status": "Confirmed"}
		flags = _Flags(
			ione_finding_appeal_sync={
				"appeal": "APPEAL-0001",
				"finding": doc.name,
				"source_status": "Confirmed",
				"target_status": "Appealed",
			}
		)
		with (
			patch.object(finding_appeals.frappe, "flags", flags),
			patch.object(
				finding_appeals.frappe.db,
				"get_value",
				return_value={"finding": doc.name, "status": "Submitted"},
			) as get_value,
		):
			finding_appeals.validate_finding_appeal_transition(doc)
		get_value.assert_called_once_with(
			finding_appeals.APPEAL_DOCTYPE,
			"APPEAL-0001",
			["finding", "status"],
			as_dict=True,
		)

	def test_withdrawal_validator_requires_exact_original_submitter_capability(self) -> None:
		doc = _Document(
			name="APPEAL-0001",
			finding="FINDING-0001",
			active_key=None,
			status="Withdrawn",
			submitted_by="doctor@example.test",
			withdrawn_by="doctor@example.test",
			withdrawn_at=datetime(2026, 7, 30, 14, 0),
			withdraw_comment="Submitted in error.",
		)
		doc.previous = {
			"finding": doc.finding,
			"status": "Submitted",
			"submitted_by": doc.submitted_by,
			"active_key": doc.finding,
		}
		flags = _Flags(
			ione_finding_appeal_withdraw={
				"appeal": doc.name,
				"finding": doc.finding,
				"withdrawn_by": doc.withdrawn_by,
				"target_status": "Withdrawn",
			}
		)
		with (
			patch.object(
				finding_appeals.frappe,
				"session",
				SimpleNamespace(user=doc.submitted_by),
			),
			patch.object(finding_appeals.frappe, "flags", flags),
		):
			finding_appeals.validate_finding_appeal(doc)

	def test_direct_create_and_final_record_edit_are_rejected(self) -> None:
		new_doc = _Document(finding="FINDING-0001", submitted_by="doctor@example.test")
		with (
			patch.object(finding_appeals.frappe, "flags", _Flags()),
			patch.object(
				finding_appeals.frappe,
				"throw",
				side_effect=RuntimeError("governed API"),
			),
			self.assertRaisesRegex(RuntimeError, "governed API"),
		):
			finding_appeals.validate_finding_appeal(new_doc)

		final_doc = _Document(
			finding="FINDING-0001",
			status="Approved",
			review_comment="Changed after final review",
		)
		final_doc.previous = {
			"finding": "FINDING-0001",
			"status": "Approved",
			"review_comment": "Original final review",
		}
		with (
			patch.object(
				finding_appeals.frappe,
				"throw",
				side_effect=RuntimeError("immutable"),
			),
			self.assertRaisesRegex(RuntimeError, "immutable"),
		):
			finding_appeals.validate_finding_appeal(final_doc)

	def test_direct_evidence_creation_and_persisted_evidence_change_are_rejected(self) -> None:
		new_doc = _Document(
			doctype=finding_appeals.APPEAL_EVIDENCE_DOCTYPE,
			finding="FINDING-0001",
			evidence_file="FILE-0001",
		)
		with (
			patch.object(finding_appeals.frappe, "flags", _Flags()),
			patch.object(
				finding_appeals.frappe,
				"throw",
				side_effect=RuntimeError("governed evidence API"),
			),
			self.assertRaisesRegex(RuntimeError, "governed evidence API"),
		):
			finding_appeals.validate_finding_appeal_evidence(new_doc)

		persisted = _Document(
			doctype=finding_appeals.APPEAL_EVIDENCE_DOCTYPE,
			finding="FINDING-0001",
			evidence_file="FILE-CHANGED",
		)
		persisted.previous = {
			"finding": persisted.finding,
			"evidence_file": "FILE-ORIGINAL",
		}
		with (
			patch.object(
				finding_appeals.frappe,
				"throw",
				side_effect=RuntimeError("immutable evidence"),
			),
			self.assertRaisesRegex(RuntimeError, "immutable evidence"),
		):
			finding_appeals.validate_finding_appeal_evidence(persisted)

	def test_referenced_appeal_evidence_file_cannot_be_deleted(self) -> None:
		file_doc = _Document(
			doctype="File",
			name="FILE-0001",
			content_hash="a" * 64,
		)
		with (
			patch.object(finding_appeals, "_appeal_evidence_doctype_available", return_value=True),
			patch.object(
				finding_appeals.frappe.db,
				"get_value",
				side_effect=["EVIDENCE-0001"],
			) as get_value,
			patch.object(
				finding_appeals.frappe,
				"throw",
				side_effect=RuntimeError("retained evidence"),
			),
			self.assertRaisesRegex(RuntimeError, "retained evidence"),
		):
			finding_appeals.prevent_finding_appeal_evidence_deletion(file_doc)
		get_value.assert_called_once_with(
			finding_appeals.APPEAL_EVIDENCE_DOCTYPE,
			{"evidence_file": file_doc.name},
			"name",
			for_update=True,
		)

		with (
			patch.object(finding_appeals, "_appeal_evidence_doctype_available", return_value=True),
			patch.object(finding_appeals.frappe.db, "get_value", return_value=None),
		):
			finding_appeals.prevent_finding_appeal_evidence_deletion(
				_Document(doctype="File", name="FILE-0002", content_hash="b" * 64)
			)


class TestFindingAppealPermissions(TestCase):
	def test_query_uses_the_linked_finding_scope_and_blocks_special_accounts(self) -> None:
		reviewer = permissions.AccessContext(
			user="reviewer@example.test",
			roles=frozenset({"IONE QC Reviewer"}),
			hospitals=frozenset(),
			campuses=frozenset(),
			departments=frozenset({"DEPARTMENT-1"}),
			wards=frozenset(),
			staff_records=frozenset(),
		)
		with (
			patch.object(permissions, "get_access_context", return_value=reviewer),
			patch.object(
				permissions,
				"qc_finding_query",
				return_value="`tabIONE QC Finding`.department = 'DEPARTMENT-1'",
			),
		):
			condition = permissions.finding_appeal_query(reviewer.user)
		self.assertIn(
			"`tabIONE QC Finding`.name = `tabIONE QC Finding Appeal`.finding",
			condition,
		)
		self.assertIn("`tabIONE QC Finding`.department = 'DEPARTMENT-1'", condition)

		for user, roles in (
			("Administrator", frozenset({"System Manager"})),
			("agent@example.test", frozenset({"IONE Agent Service", "IONE QC Reviewer"})),
		):
			context = SimpleNamespace(user=user, roles=roles)
			with (
				self.subTest(user=user),
				patch.object(permissions, "get_access_context", return_value=context),
			):
				self.assertEqual(permissions.finding_appeal_query(user), "1=0")

	def test_has_permission_is_read_only_and_delegates_to_linked_finding(self) -> None:
		context = permissions.AccessContext(
			user="reviewer@example.test",
			roles=frozenset({"IONE QC Reviewer"}),
			hospitals=frozenset(),
			campuses=frozenset(),
			departments=frozenset({"DEPARTMENT-1"}),
			wards=frozenset(),
			staff_records=frozenset(),
		)
		appeal = _Document(finding="FINDING-0001")
		finding = _finding()
		with (
			patch.object(permissions, "get_access_context", return_value=context),
			patch.object(permissions.frappe, "get_doc", return_value=finding) as get_doc,
			patch.object(permissions, "finding_permission", return_value=True) as finding_permission,
		):
			self.assertTrue(permissions.finding_appeal_permission(appeal, "read", user=context.user))
			self.assertFalse(permissions.finding_appeal_permission(appeal, "write", user=context.user))
		get_doc.assert_called_once_with("IONE QC Finding", appeal.finding)
		finding_permission.assert_called_once_with(finding, "read", user=context.user)

	def test_evidence_permissions_exclude_auditor_and_follow_finding_scope(self) -> None:
		evidence = _Document(finding="FINDING-0001")
		auditor = permissions.AccessContext(
			user="auditor@example.test",
			roles=frozenset({"IONE Auditor"}),
			hospitals=frozenset(),
			campuses=frozenset(),
			departments=frozenset(),
			wards=frozenset(),
			staff_records=frozenset(),
		)
		with patch.object(permissions, "get_access_context", return_value=auditor):
			self.assertEqual(permissions.finding_appeal_evidence_query(auditor.user), "1=0")
			self.assertFalse(
				permissions.finding_appeal_evidence_permission(
					evidence,
					"read",
					user=auditor.user,
				)
			)

		reviewer = permissions.AccessContext(
			user="reviewer@example.test",
			roles=frozenset({"IONE QC Reviewer"}),
			hospitals=frozenset(),
			campuses=frozenset(),
			departments=frozenset({"DEPARTMENT-1"}),
			wards=frozenset(),
			staff_records=frozenset(),
		)
		finding = _finding()
		with (
			patch.object(permissions, "get_access_context", return_value=reviewer),
			patch.object(
				permissions,
				"qc_finding_query",
				return_value="`tabIONE QC Finding`.department = 'DEPARTMENT-1'",
			),
			patch.object(permissions.frappe, "get_doc", return_value=finding),
			patch.object(permissions, "finding_permission", return_value=True),
		):
			condition = permissions.finding_appeal_evidence_query(reviewer.user)
			self.assertIn(
				"`tabIONE QC Finding`.name = `tabIONE QC Finding Appeal Evidence`.finding",
				condition,
			)
			self.assertTrue(
				permissions.finding_appeal_evidence_permission(
					evidence,
					"read",
					user=reviewer.user,
				)
			)

	def test_exact_evidence_file_cannot_be_shared_and_reads_follow_evidence_permission(self) -> None:
		file_doc = _Document(name="FILE-0001", content_hash="a" * 64)
		evidence = _Document(name="EVIDENCE-0001", finding="FINDING-0001")
		with (
			patch.object(permissions, "_doctype_exists", return_value=True),
			patch.object(
				permissions.frappe.db,
				"get_value",
				side_effect=[None, evidence.name, None, evidence.name],
			),
			patch.object(permissions.frappe, "get_doc", return_value=evidence),
			patch.object(
				permissions,
				"finding_appeal_evidence_permission",
				return_value=True,
			) as evidence_permission,
		):
			self.assertFalse(
				permissions.finding_appeal_evidence_file_permission(
					file_doc,
					"share",
					user="doctor@example.test",
				)
			)
			self.assertTrue(
				permissions.finding_appeal_evidence_file_permission(
					file_doc,
					"read",
					user="reviewer@example.test",
				)
			)
		evidence_permission.assert_called_once_with(
			evidence,
			"read",
			user="reviewer@example.test",
		)

	def test_hash_linked_file_read_is_denied_when_evidence_scope_denies_it(self) -> None:
		file_doc = _Document(name="FILE-SIBLING", content_hash="a" * 64)
		evidence = _Document(name="EVIDENCE-0001", finding="FINDING-0001")
		with (
			patch.object(permissions, "_doctype_exists", return_value=True),
			patch.object(
				permissions.frappe.db,
				"get_value",
				side_effect=[None, evidence.name],
			),
			patch.object(permissions.frappe, "get_doc", return_value=evidence),
			patch.object(
				permissions,
				"finding_appeal_evidence_permission",
				return_value=False,
			),
		):
			self.assertFalse(
				permissions.finding_appeal_evidence_file_permission(
					file_doc,
					"read",
					user="auditor@example.test",
				)
			)
