from ione_qms.setup.navigation import ensure_workspace_navigation


def execute() -> None:
	"""Reconcile localized navigation during upgrades that require a site migrate."""
	ensure_workspace_navigation()
