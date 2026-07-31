from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]


class TestIntegrationPackageImport(TestCase):
	def test_package_import_does_not_require_frappe_or_eagerly_import_service(self) -> None:
		probe = (
			"import sys; "
			"import ione_qms.integration as package; "
			"assert package.__all__ == ['receive_clinical_event']; "
			"assert 'ione_qms.integration.service' not in sys.modules"
		)
		result = subprocess.run(  # noqa: S603 - fixed interpreter and static probe
			[sys.executable, "-c", probe],
			cwd=ROOT,
			capture_output=True,
			text=True,
			check=False,
		)
		self.assertEqual(result.returncode, 0, result.stderr)
