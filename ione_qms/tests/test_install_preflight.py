from __future__ import annotations

import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from ione_qms.constants import APP_ROLES


class _InstallationRejected(Exception):
	pass


class _Database:
	def __init__(self, *, role_collision: bool = True) -> None:
		self.mutations: list[tuple] = []
		self.role_collision = role_collision
		self.existing: set[tuple[str, str]] = set()

	def get_value(self, doctype: str, name: str, fields, *, as_dict: bool):
		assert doctype == "Role"
		assert as_dict
		if not self.role_collision:
			return None
		return {
			"name": name,
			"desk_access": 0,
			"is_custom": 1,
		}

	def set_value(self, *args, **kwargs) -> None:
		self.mutations.append((args, kwargs))

	def exists(self, doctype: str, name: str) -> bool:
		return (doctype, name) in self.existing

	def commit(self) -> None:
		self.mutations.append(("commit",))


def _module(name: str, **attributes) -> ModuleType:
	module = ModuleType(name)
	for fieldname, value in attributes.items():
		setattr(module, fieldname, value)
	return module


@contextmanager
def _load_install(*, role_collision: bool = True, duplicate_crypto_secrets: bool = False):
	database = _Database(role_collision=role_collision)

	def reject(message: str, *_args, **_kwargs):
		raise _InstallationRejected(message)

	frappe = _module(
		"frappe",
		__version__="17.0.0",
		db=database,
		get_installed_apps=lambda: ["frappe", "flow"],
		throw=reject,
		conf={},
		flags=SimpleNamespace(),
		clear_cache=lambda: None,
	)
	stubs = {
		"frappe": frappe,
		"frappe.utils": _module(
			"frappe.utils",
			add_days=lambda value, _days: value,
			getdate=lambda value: value,
			nowdate=lambda: "2026-07-30",
		),
		"ione_qms.services.integration_config": _module(
			"ione_qms.services.integration_config",
			trusted_integration_configuration_maintenance=lambda: None,
		),
		"ione_qms.services.crypto_keys": _module(
			"ione_qms.services.crypto_keys",
			active_hmac_key=lambda prefix: SimpleNamespace(
				key_id="test-v1",
				secret=(b"x" * 32 if duplicate_crypto_secrets else (prefix.encode() * 4)[:32]),
			),
			retained_hmac_keys=lambda _prefix: (),
		),
		"ione_qms.setup.blueprint": _module(
			"ione_qms.setup.blueprint",
			seed_quality_blueprint=lambda: None,
		),
		"ione_qms.setup.workflows": _module(
			"ione_qms.setup.workflows",
			ensure_workflows=lambda: None,
			validate_workflow_install_preflight=lambda: None,
		),
	}
	original = {name: sys.modules.get(name) for name in stubs}
	try:
		sys.modules.update(stubs)
		path = Path(__file__).resolve().parents[1] / "setup" / "install.py"
		spec = importlib.util.spec_from_file_location("_ione_install_preflight_test", path)
		assert spec and spec.loader
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		yield module, database
	finally:
		for name, previous in original.items():
			if previous is None:
				sys.modules.pop(name, None)
			else:
				sys.modules[name] = previous


class TestInstallPreflight(TestCase):
	def test_qms_role_namespace_does_not_claim_manager_external_roles(self) -> None:
		self.assertTrue({"IONE QMS Auditor", "IONE QMS Medical Record Coder"}.issubset(APP_ROLES))
		manager_external_roles = {
			"IONE " + "Auditor",
			"IONE Medical Record " + "Coder",
		}
		self.assertTrue(APP_ROLES.isdisjoint(manager_external_roles))

	def test_reserved_collision_fails_before_any_write(self) -> None:
		with _load_install() as (install, database):
			with self.assertRaisesRegex(
				_InstallationRejected,
				"no IONE schema has been synchronized",
			):
				install.before_install()
			self.assertEqual(database.mutations, [])

	def test_uninstall_is_fail_closed(self) -> None:
		with _load_install() as (install, database):
			with self.assertRaisesRegex(_InstallationRejected, "cannot be uninstalled in place"):
				install.before_uninstall()
			self.assertEqual(database.mutations, [])

	def test_duplicate_dedicated_hmac_secrets_fail_before_any_write(self) -> None:
		with _load_install(
			role_collision=False,
			duplicate_crypto_secrets=True,
		) as (install, database):
			with self.assertRaisesRegex(_InstallationRejected, "independent dedicated secrets"):
				install.before_install()
			self.assertEqual(database.mutations, [])

	def test_fail_closed_uninstall_is_bound_to_the_frappe_hook(self) -> None:
		hooks = (Path(__file__).resolve().parents[1] / "hooks.py").read_text(encoding="utf-8")
		self.assertIn(
			'before_uninstall = "ione_qms.setup.install.before_uninstall"',
			hooks,
		)

	def test_owned_workflow_collision_is_rejected_before_any_write(self) -> None:
		with _load_install(role_collision=False) as (install, database):
			install.validate_workflow_install_preflight = lambda: install.frappe.throw(
				"Reserved Workflow 'IONE QC Finding Workflow' already exists"
			)
			with self.assertRaisesRegex(_InstallationRejected, "Reserved Workflow"):
				install.before_install()
			self.assertEqual(database.mutations, [])

	def test_exact_looking_flow_tool_is_still_unknown_on_fresh_install(self) -> None:
		with _load_install(role_collision=False) as (install, database):
			database.existing.add(("Flow Tool", "ione_search_quality_standard"))
			with self.assertRaisesRegex(_InstallationRejected, "unknown pre-existing reserved name"):
				install.before_install()
			self.assertEqual(database.mutations, [])

	def test_duplicate_shared_flow_retention_rows_are_rejected(self) -> None:
		with _load_install(role_collision=False) as (install, database):
			database.existing.add(("DocType", "Log Settings"))
			install.frappe.get_single = lambda _doctype: {
				"logs_to_clear": [
					{"ref_doctype": "Flow Session", "days": 30},
					{"ref_doctype": "Flow Session", "days": 90},
				]
			}
			with self.assertRaisesRegex(_InstallationRejected, "duplicate shared retention rows"):
				install.before_install()
			self.assertEqual(database.mutations, [])

	def test_existing_reserved_agent_trigger_is_rejected(self) -> None:
		with _load_install(role_collision=False) as (install, database):
			database.existing.add(("DocType", "Flow Trigger"))
			install.frappe.get_all = lambda *_args, **_kwargs: [
				{"name": "UNKNOWN-TRIGGER", "agent": "IONE Quality Policy Agent"}
			]
			with self.assertRaisesRegex(_InstallationRejected, "reserved IONE Flow Agent"):
				install.before_install()
			self.assertEqual(database.mutations, [])

	def test_after_install_replays_every_reconciliation_step(self) -> None:
		with _load_install(role_collision=False) as (install, database):
			step_names = (
				"_validate_cryptographic_configuration",
				"_assert_phi_query_boundary_is_clean",
				"ensure_identity_source_uniqueness",
				"ensure_batch_source_epoch_rows",
				"ensure_roles",
				"ensure_settings",
				"ensure_flow_log_retention",
				"ensure_workflows",
				"ensure_flow_tools",
				"ensure_flow_configuration",
				"ensure_evaluation_threshold_policy",
				"disable_ione_flow_triggers",
				"seed_quality_blueprint",
				"schedule_post_migrate_backfills",
			)
			patchers = [patch.object(install, name) for name in step_names]
			mocks = [patcher.start() for patcher in patchers]
			try:
				install.after_install()
				install.after_install()
			finally:
				for patcher in reversed(patchers):
					patcher.stop()
			for step in mocks:
				self.assertEqual(step.call_count, 2)
			self.assertEqual(database.mutations, [("commit",), ("commit",)])

	def test_after_migrate_reconciles_blueprint_without_committing(self) -> None:
		with _load_install(role_collision=False) as (install, database):
			step_names = (
				"_validate_cryptographic_configuration",
				"_assert_phi_query_boundary_is_clean",
				"ensure_identity_source_uniqueness",
				"ensure_batch_source_epoch_rows",
				"ensure_roles",
				"ensure_settings",
				"ensure_flow_log_retention",
				"ensure_workflows",
				"ensure_flow_tools",
				"ensure_flow_configuration",
				"ensure_evaluation_threshold_policy",
				"disable_ione_flow_triggers",
				"seed_quality_blueprint",
				"schedule_post_migrate_backfills",
			)
			patchers = [patch.object(install, name) for name in step_names]
			mocks = [patcher.start() for patcher in patchers]
			try:
				install.after_migrate()
				install.after_migrate()
			finally:
				for patcher in reversed(patchers):
					patcher.stop()
			for step in mocks:
				self.assertEqual(step.call_count, 2)
			self.assertEqual(database.mutations, [])
