from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from ione_qms.services import source_document_locator as locator


class _Document(dict):
	def __getattr__(self, name):
		try:
			return self[name]
		except KeyError as exc:
			raise AttributeError(name) from exc

	def get(self, key, default=None):
		return super().get(key, default)


class TestSourceDocumentLocator(unittest.TestCase):
	def test_static_paths_reject_templates_queries_percent_and_traversal(self) -> None:
		for invalid in (
			"/record/{id}/",
			"/record/?next=/",
			"/record/%2e%2e/",
			"/record/../",
			"record/",
			"/record",
		):
			with self.subTest(invalid=invalid):
				self.assertFalse(locator._valid_prefix(invalid))
		for invalid in ("/view?next=x", "/../view", ".pdf#x", "//view", "/view/"):
			with self.subTest(invalid=invalid):
				self.assertFalse(locator._valid_suffix(invalid))
		self.assertTrue(locator._valid_prefix("/emr/document/"))
		self.assertTrue(locator._valid_suffix("/view"))
		self.assertTrue(locator._valid_suffix(".pdf"))

	def test_record_hash_binds_source_type_and_exact_identifier(self) -> None:
		first = locator._record_id_hash("EMR", "DOCUMENT", "A/B ?#")
		self.assertEqual(len(first), 64)
		self.assertNotEqual(first, locator._record_id_hash("EMR2", "DOCUMENT", "A/B ?#"))
		self.assertNotEqual(first, locator._record_id_hash("EMR", "NOTE", "A/B ?#"))
		self.assertNotEqual(first, locator._record_id_hash("EMR", "DOCUMENT", "A/B ?# "))

	def test_live_link_percent_encodes_identifier_as_one_path_segment(self) -> None:
		doc = _Document(
			{
				"name": "LOC-1",
				"status": "Approved",
				"enabled": 1,
				"hospital": "H-1",
				"campus": "",
				"department": "",
				"ward": "",
				"path_prefix": "/emr/document/",
				"path_suffix": "/view",
				"approval_checksum": "checksum",
			}
		)
		source = SimpleNamespace(name="SOURCE-1")
		endpoint = SimpleNamespace(name="ENDPOINT-1")
		with (
			patch.object(locator, "_approval_checksum", return_value="checksum"),
			patch.object(
				locator,
				"_validate_locator_endpoint_contract",
				return_value=(source, endpoint, "https://emr.example.test", "emr.example.test"),
			),
			patch.object(locator, "authorize_endpoint_scope", return_value={"hospital": "H-1"}),
		):
			url, host = locator._live_deep_link(
				doc,
				{"hospital": "H-1", "campus": "", "department": "", "ward": ""},
				"A/B ?#中文",
			)
		self.assertEqual(host, "emr.example.test")
		self.assertEqual(
			url,
			"https://emr.example.test/emr/document/A%2FB%20%3F%23%E4%B8%AD%E6%96%87/view",
		)
		self.assertNotIn("?", url)
		self.assertNotIn("#", url)

	def test_host_allowlist_rejects_wildcards_urls_ports_and_duplicates(self) -> None:
		for invalid in (
			'["*.example.test"]',
			'["https://emr.example.test"]',
			'["emr.example.test:443"]',
			'["emr.example.test", "EMR.EXAMPLE.TEST"]',
		):
			with (
				self.subTest(invalid=invalid),
				self.assertRaises((frappe.ValidationError, frappe.PermissionError)),
			):
				locator._exact_allowed_hosts(invalid)
		self.assertEqual(
			locator._exact_allowed_hosts('["emr.example.test"]'),
			frozenset({"emr.example.test"}),
		)


if __name__ == "__main__":
	unittest.main()
