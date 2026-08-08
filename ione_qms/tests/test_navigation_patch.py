import importlib
import sys
from types import ModuleType
from unittest.mock import Mock, patch


def test_navigation_patch_runs_reconciliation():
	reconcile = Mock()
	navigation = ModuleType("ione_qms.setup.navigation")
	navigation.ensure_workspace_navigation = reconcile
	sys.modules.pop("ione_qms.patches.reconcile_navigation_metadata", None)
	with patch.dict(sys.modules, {"ione_qms.setup.navigation": navigation}):
		migration_patch = importlib.import_module("ione_qms.patches.reconcile_navigation_metadata")
		migration_patch.execute()

	reconcile.assert_called_once_with()
