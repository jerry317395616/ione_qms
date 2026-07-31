from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, call, patch

from ione_qms.api import improvement as improvement_api
from ione_qms.services import improvement


def _raise_runtime(message: str, *args, **kwargs) -> None:
	del args, kwargs
	raise RuntimeError(message)


class _Document:
	def __init__(self, values: dict, previous: dict | None = None):
		self._values = values
		self._previous = previous
		self.name = values.get("name", "")

	def __getattr__(self, fieldname: str):
		try:
			return self._values[fieldname]
		except KeyError as exc:
			raise AttributeError(fieldname) from exc

	def get(self, fieldname: str):
		return self._values.get(fieldname)

	def set(self, fieldname: str, value) -> None:
		self._values[fieldname] = value

	def get_doc_before_save(self):
		return self._previous

	def is_new(self) -> bool:
		return self._previous is None


class TestImprovementWorkflowIntegrity(TestCase):
	def test_rectification_inherits_finding_scope(self) -> None:
		doc = _Document(
			{
				"name": "RECT-1",
				"finding": "FIND-1",
				"hospital": "",
				"campus": "",
				"department": "",
				"ward": "",
			}
		)
		finding = {
			"status": "Confirmed",
			"hospital": "HOSP-1",
			"campus": "CAMP-1",
			"department": "DEPT-1",
			"ward": "WARD-1",
		}

		def get_value(doctype, *args, **kwargs):
			del args, kwargs
			if doctype == "IONE QC Finding":
				return finding
			return None

		with (
			patch.object(improvement.frappe.db, "get_value", side_effect=get_value),
		):
			improvement._validate_rectification_linkage(doc, None)

		self.assertEqual(doc.get("hospital"), "HOSP-1")
		self.assertEqual(doc.get("campus"), "CAMP-1")
		self.assertEqual(doc.get("department"), "DEPT-1")
		self.assertEqual(doc.get("ward"), "WARD-1")

	def test_verification_cannot_point_at_a_different_finding(self) -> None:
		doc = _Document(
			{
				"name": "VER-1",
				"rectification": "RECT-1",
				"finding": "FIND-2",
			}
		)
		rectification = {
			"finding": "FIND-1",
			"status": "Pending Functional Review",
			"hospital": None,
			"campus": None,
			"department": None,
			"ward": None,
		}
		with (
			patch.object(improvement.frappe.db, "get_value", return_value=rectification),
			patch.object(improvement.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "must match"),
		):
			improvement._validate_verification_linkage(doc, None)

	def test_verification_decision_requires_pending_functional_review(self) -> None:
		doc = _Document(
			{
				"name": "VER-1",
				"status": "Submitted",
				"owner": "reviewer-a@example.test",
			},
			previous={"status": "Draft"},
		)
		with (
			patch.object(
				improvement,
				"_validate_verification_linkage",
				return_value="In Progress",
			),
			patch.object(improvement, "_require_transition_roles"),
			patch.object(improvement.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "pending functional review"),
		):
			improvement.validate_verification(doc)

	def test_active_keys_are_forced_by_record_lifecycle(self) -> None:
		rectification = _Document(
			{
				"finding": "FIND-1",
				"status": "Rework",
				"active_key": "attacker-controlled-value",
			}
		)
		improvement._enforce_active_key(
			rectification,
			source_field="finding",
			active_states=improvement._ACTIVE_RECTIFICATION_STATES,
		)
		self.assertEqual(rectification.get("active_key"), "FIND-1")

		rectification.set("status", "Closed")
		improvement._enforce_active_key(
			rectification,
			source_field="finding",
			active_states=improvement._ACTIVE_RECTIFICATION_STATES,
		)
		self.assertIsNone(rectification.get("active_key"))

		verification = _Document(
			{
				"rectification": "RECT-1",
				"status": "Submitted",
				"active_key": "",
			}
		)
		improvement._enforce_active_key(
			verification,
			source_field="rectification",
			active_states=improvement._ACTIVE_VERIFICATION_STATES,
		)
		self.assertEqual(verification.get("active_key"), "RECT-1")
		verification.set("status", "Rejected")
		improvement._enforce_active_key(
			verification,
			source_field="rectification",
			active_states=improvement._ACTIVE_VERIFICATION_STATES,
		)
		self.assertIsNone(verification.get("active_key"))

	def test_document_validators_apply_the_active_key_contract(self) -> None:
		rectification = _Document(
			{
				"finding": "FIND-1",
				"status": "Draft",
				"plan": "A sufficiently detailed corrective plan.",
				"active_key": "incorrect",
			}
		)
		with (
			patch.object(improvement, "_validate_rectification_linkage"),
			patch.object(improvement, "_require_roles"),
		):
			improvement.validate_rectification(rectification)
		self.assertEqual(rectification.get("active_key"), "FIND-1")

		verification = _Document(
			{
				"finding": "FIND-1",
				"rectification": "RECT-1",
				"status": "Draft",
				"active_key": "incorrect",
			}
		)
		with (
			patch.object(
				improvement,
				"_validate_verification_linkage",
				return_value="Pending Functional Review",
			),
			patch.object(improvement, "_require_roles"),
		):
			improvement.validate_verification(verification)
		self.assertEqual(verification.get("active_key"), "RECT-1")

	def test_active_record_lookup_prefers_durable_key_and_has_legacy_fallback(self) -> None:
		with patch.object(
			improvement.frappe.db,
			"get_value",
			side_effect=[None, "RECT-LEGACY", "VER-1"],
		) as get_value:
			self.assertEqual(improvement.active_rectification_name("FIND-1"), "RECT-LEGACY")
			self.assertEqual(improvement.active_verification_name("RECT-1"), "VER-1")

		self.assertEqual(
			get_value.call_args_list,
			[
				call(
					"IONE QC Rectification",
					{"active_key": "FIND-1"},
					"name",
				),
				call(
					"IONE QC Rectification",
					{
						"finding": "FIND-1",
						"status": ["in", sorted(improvement._ACTIVE_RECTIFICATION_STATES)],
					},
					"name",
				),
				call(
					"IONE QC Verification",
					{"active_key": "RECT-1"},
					"name",
				),
			],
		)

	def test_rectification_api_serializes_creation_and_locks_parent_row(self) -> None:
		finding = SimpleNamespace(
			name="FIND-1",
			department="DEPT-1",
			status="Confirmed",
			hospital="HOSP-1",
			campus="CAMP-1",
		)
		finding.checked_permissions = []
		finding.reload_count = 0

		def check_permission(permission_type: str) -> None:
			finding.checked_permissions.append(permission_type)

		def reload() -> None:
			finding.reload_count += 1

		def get(fieldname: str):
			return getattr(finding, fieldname, None)

		finding.check_permission = check_permission
		finding.reload = reload
		finding.get = get
		rectification = MagicMock()
		rectification.name = "RECT-1"

		def get_doc(doctype, name=None):
			if doctype == "IONE QC Finding":
				self.assertEqual(name, "FIND-1")
				return finding
			self.assertIsInstance(doctype, dict)
			self.assertEqual(doctype["active_key"], "FIND-1")
			return rectification

		with (
			patch.object(improvement_api.frappe, "get_doc", side_effect=get_doc),
			patch.object(improvement_api.frappe, "parse_json"),
			patch.object(improvement_api.frappe.db, "advisory_lock", return_value=nullcontext()) as lock,
			patch.object(
				improvement_api.frappe.db,
				"get_value",
				return_value="FIND-1",
			) as get_value,
			patch.object(improvement_api.frappe.db, "savepoint") as savepoint,
			patch.object(improvement_api.frappe.db, "release_savepoint") as release_savepoint,
			patch.object(improvement_api, "active_rectification_name", return_value=None),
			patch.object(improvement_api, "require_department"),
			patch.object(improvement_api, "require_role"),
			patch.object(
				improvement_api,
				"apply_workflow",
				return_value=rectification,
			),
			patch.object(improvement_api, "getdate", return_value=0),
			patch.object(improvement_api, "nowdate", return_value="2026-07-30"),
		):
			result = improvement_api.submit_rectification(
				"FIND-1",
				"Complete a clinically reviewed corrective plan.",
				"2026-08-30",
				[],
			)

		self.assertEqual(result, {"rectification": "RECT-1", "finding": "FIND-1"})
		self.assertEqual(finding.checked_permissions, ["read", "read"])
		self.assertEqual(finding.reload_count, 1)
		lock.assert_called_once_with(
			"ione-qms:rectification-create:FIND-1",
			timeout=15,
		)
		get_value.assert_called_once_with(
			"IONE QC Finding",
			"FIND-1",
			"name",
			for_update=True,
		)
		rectification.insert.assert_called_once_with()
		savepoint.assert_called_once()
		release_savepoint.assert_called_once_with(savepoint.call_args.args[0])

	def test_active_key_metadata_is_hidden_read_only_and_unique(self) -> None:
		app_root = Path(__file__).resolve().parents[1]
		for relative_path in (
			"ione_improvement/doctype/ione_qc_rectification/ione_qc_rectification.json",
			"ione_improvement/doctype/ione_qc_verification/ione_qc_verification.json",
		):
			schema = json.loads((app_root / relative_path).read_text(encoding="utf-8"))
			active_key = next(field for field in schema["fields"] if field.get("fieldname") == "active_key")
			self.assertEqual(active_key.get("fieldtype"), "Data")
			self.assertEqual(active_key.get("hidden"), 1)
			self.assertEqual(active_key.get("read_only"), 1)
			self.assertEqual(active_key.get("unique"), 1)
			self.assertEqual(active_key.get("no_copy"), 1)

	def test_rectification_creation_savepoint_rolls_back_as_a_unit(self) -> None:
		with (
			patch.object(improvement_api.frappe.db, "savepoint") as savepoint,
			patch.object(improvement_api.frappe.db, "rollback") as rollback,
			patch.object(improvement_api.frappe.db, "release_savepoint") as release_savepoint,
			self.assertRaisesRegex(RuntimeError, "workflow failed"),
		):
			with improvement_api._atomic_rectification_create("ione_rectification_test"):
				raise RuntimeError("workflow failed")

		savepoint.assert_called_once_with("ione_rectification_test")
		rollback.assert_called_once_with(save_point="ione_rectification_test")
		release_savepoint.assert_not_called()

	def test_rectification_sync_grants_only_a_scoped_internal_capability(self) -> None:
		doc = _Document(
			{
				"name": "RECT-1",
				"finding": "FIND-1",
				"status": "Pending Department Approval",
			}
		)
		doc.finding = "FIND-1"
		doc.status = "Pending Department Approval"
		finding = SimpleNamespace(name="FIND-1", status="Rectifying")
		captured_contexts: list[dict] = []

		def save() -> None:
			captured_contexts.append(dict(improvement.frappe.flags["ione_rectification_finding_sync"]))

		finding.save = save
		with (
			patch.object(improvement.frappe, "flags", {}),
			patch.object(improvement.frappe, "get_doc", return_value=finding),
		):
			improvement.on_rectification_update(doc)
			self.assertNotIn("ione_rectification_finding_sync", improvement.frappe.flags)

		self.assertEqual(
			captured_contexts,
			[
				{
					"finding": "FIND-1",
					"rectification": "RECT-1",
					"target_status": "Pending Department Approval",
				}
			],
		)

	def test_department_actor_cannot_edit_during_functional_review(self) -> None:
		doc = _Document(
			{
				"name": "RECT-1",
				"status": "Pending Functional Review",
				"plan": "changed rectification plan",
			},
			previous={
				"status": "Pending Functional Review",
				"plan": "original rectification plan",
			},
		)
		doc.meta = SimpleNamespace(fields=[SimpleNamespace(fieldname="plan", fieldtype="Text Editor")])
		with (
			patch.object(
				improvement.frappe,
				"session",
				SimpleNamespace(user="physician@example.test"),
			),
			patch.object(
				improvement.frappe,
				"get_roles",
				return_value=["IONE Physician"],
			),
			patch.object(improvement.frappe, "throw", side_effect=_raise_runtime),
			self.assertRaisesRegex(RuntimeError, "cannot edit content"),
		):
			improvement._require_same_state_editor(
				doc,
				status_field="status",
				transition=None,
				state_role_map=improvement.RECTIFICATION_STATE_EDIT_ROLES,
			)
