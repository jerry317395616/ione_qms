from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VEX_IDS = {
	"PYSEC-2026-388",
	"PYSEC-2026-2601",
	"PYSEC-2026-2598",
	"PYSEC-2026-2600",
	"PYSEC-2026-3479",
	"PYSEC-2026-3476",
	"PYSEC-2026-2860",
	"PYSEC-2026-2270",
	"PYSEC-2026-237",
	"PYSEC-2026-2104",
	"PYSEC-2026-2105",
	"PYSEC-2026-2106",
	"PYSEC-2026-2107",
	"PYSEC-2026-2108",
	"PYSEC-2026-2109",
	"PYSEC-2026-2110",
	"PYSEC-2026-2111",
	"PYSEC-2026-2112",
	"PYSEC-2026-2113",
}


class TestSupplyChainPolicyStatic(unittest.TestCase):
	def test_safe_direct_dependency_versions_are_exact(self) -> None:
		pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
		for dependency in ("litellm==1.83.7",):
			self.assertIn(f'"{dependency}"', pyproject)
		self.assertNotIn('"aiohttp==', pyproject)
		self.assertNotIn('"python-dotenv==', pyproject)

	def test_vex_file_contains_only_the_reviewed_residual_ids(self) -> None:
		vex = (ROOT / "security" / "pip-audit-vex.txt").read_text(encoding="utf-8")
		ids = {
			line.strip() for line in vex.splitlines() if line.strip() and not line.lstrip().startswith("#")
		}
		self.assertEqual(ids, EXPECTED_VEX_IDS)
		self.assertTrue(all(re.fullmatch(r"PYSEC-\d{4}-\d+", item) for item in ids))

	def test_vex_rationale_and_fail_closed_ci_contract_are_present(self) -> None:
		rationale = (ROOT / "security" / "dependency-vex.md").read_text(encoding="utf-8")
		workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
		for advisory in EXPECTED_VEX_IDS:
			self.assertIn(f"`{advisory}`", rationale)
		self.assertIn("security/pip-audit-vex.txt", workflow)
		self.assertIn("disable-local-file-access", workflow)
		self.assertIn(r"litellm\.proxy", workflow)


if __name__ == "__main__":
	unittest.main()
