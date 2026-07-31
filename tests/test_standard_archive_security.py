from __future__ import annotations

import ipaddress
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from ione_qms.services import standard_archive
from ione_qms.services.standard_archive import (
	StandardArchiveError,
	validate_standard_archive_content,
)


def _docx_bytes(*, external_relationship: bool = False, unsafe_member: str = "") -> bytes:
	target_mode = ' TargetMode="External"' if external_relationship else ""
	buffer = BytesIO()
	with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
		archive.writestr(
			"[Content_Types].xml",
			(
				'<?xml version="1.0"?>'
				'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
				'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
				'relationships+xml"/>'
				'<Override PartName="/word/document.xml" '
				'ContentType="application/vnd.openxmlformats-officedocument.'
				'wordprocessingml.document.main+xml"/>'
				"</Types>"
			),
		)
		archive.writestr(
			"_rels/.rels",
			(
				'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
				f'<Relationship Id="rId1" Type="officeDocument" Target="word/document.xml"'
				f"{target_mode}/>"
				"</Relationships>"
			),
		)
		archive.writestr(
			"word/document.xml",
			'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
		)
		if unsafe_member:
			archive.writestr(unsafe_member, b"unsafe")
	return buffer.getvalue()


class TestStandardArchiveContentSecurity(unittest.TestCase):
	def test_accepts_only_signature_bound_safe_types(self) -> None:
		pdf = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF"
		self.assertEqual(
			validate_standard_archive_content(
				pdf,
				declared_media_type="application/pdf; charset=binary",
				final_url="https://authority.example/standard.pdf",
				max_bytes=1_024,
			),
			("application/pdf", "pdf"),
		)
		docx = _docx_bytes()
		self.assertEqual(
			validate_standard_archive_content(
				docx,
				declared_media_type=(
					"application/vnd.openxmlformats-officedocument.wordprocessingml.document"
				),
				final_url="https://authority.example/download",
				max_bytes=100_000,
			),
			(
				"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
				"docx",
			),
		)
		self.assertEqual(
			validate_standard_archive_content(
				"医疗质量标准\n仅测试".encode(),
				declared_media_type="text/plain",
				final_url="https://authority.example/standard.txt",
				max_bytes=1_024,
			),
			("text/plain", "txt"),
		)

	def test_rejects_active_or_executable_content_and_mismatched_extensions(self) -> None:
		for content, media_type, url in (
			(b"<html><script>alert(1)</script></html>", "text/html", "https://a.example/a.html"),
			(b"<svg onload='alert(1)'/>", "image/svg+xml", "https://a.example/a.svg"),
			(b"MZ\x90\x00binary", "application/octet-stream", "https://a.example/a.exe"),
			(
				b"%PDF-1.7\n%%EOF",
				"application/pdf",
				"https://a.example/authority.exe",
			),
		):
			with self.subTest(url=url), self.assertRaises(StandardArchiveError):
				validate_standard_archive_content(
					content,
					declared_media_type=media_type,
					final_url=url,
					max_bytes=1_024,
				)

	def test_rejects_docx_macros_embeddings_and_external_relationships(self) -> None:
		for content in (
			_docx_bytes(unsafe_member="word/vbaProject.bin"),
			_docx_bytes(unsafe_member="word/embeddings/object1.bin"),
			_docx_bytes(external_relationship=True),
		):
			with self.assertRaises(StandardArchiveError):
				validate_standard_archive_content(
					content,
					declared_media_type=(
						"application/vnd.openxmlformats-officedocument.wordprocessingml.document"
					),
					final_url="https://a.example/authority.docx",
					max_bytes=100_000,
				)


class TestStandardArchiveNetworkSecurity(unittest.TestCase):
	def test_dns_answer_set_must_be_entirely_global(self) -> None:
		answers = [
			(2, 1, 6, "", ("93.184.216.34", 443)),
			(2, 1, 6, "", ("127.0.0.1", 443)),
		]
		with (
			patch.object(standard_archive.socket, "getaddrinfo", return_value=answers),
			self.assertRaises(StandardArchiveError),
		):
			standard_archive._validated_authority_url(
				"https://authority.example/a.pdf",
				{"authority.example"},
			)

	def test_pinned_tls_connection_uses_verified_ip_and_original_sni(self) -> None:
		class _RawSocket:
			def getpeername(self):
				return ("93.184.216.34", 443)

			def close(self):
				return None

		class _TLSSocket:
			def __init__(self) -> None:
				self.timeout = None

			def settimeout(self, value):
				self.timeout = value

		raw_socket = _RawSocket()
		tls_socket = _TLSSocket()
		connection = standard_archive._PinnedHTTPSConnection(
			"authority.example",
			ipaddress.ip_address("93.184.216.34"),
			connect_timeout=5,
			read_timeout=30,
		)
		with (
			patch.object(standard_archive.socket, "create_connection", return_value=raw_socket) as connect,
			patch.object(
				connection._context,
				"wrap_socket",
				return_value=tls_socket,
			) as wrap_socket,
		):
			connection.connect()
		connect.assert_called_once_with(
			("93.184.216.34", 443),
			timeout=5,
			source_address=None,
		)
		wrap_socket.assert_called_once_with(raw_socket, server_hostname="authority.example")
		self.assertEqual(tls_socket.timeout, 30)

	def test_url_contract_rejects_credentials_ip_literals_and_non_https(self) -> None:
		for url in (
			"http://authority.example/a.pdf",
			"https://user:secret@authority.example/a.pdf",
			"https://127.0.0.1/a.pdf",
			"https://authority.example./a.pdf",
			"https://authority.example/a.pdf#fragment",
		):
			with self.subTest(url=url), self.assertRaises(StandardArchiveError):
				standard_archive._validated_authority_url(url, {"authority.example"})


class TestStandardArchiveResponseContract(unittest.TestCase):
	def test_app_adds_no_sniff_without_modifying_frappe(self) -> None:
		root = Path(__file__).resolve().parents[1]
		hooks = (root / "ione_qms" / "hooks.py").read_text(encoding="utf-8")
		headers = (root / "ione_qms" / "overrides" / "security_headers.py").read_text(encoding="utf-8")
		self.assertIn("ione_qms.overrides.security_headers.apply_security_headers", hooks)
		self.assertIn('"X-Content-Type-Options"] = "nosniff"', headers)


if __name__ == "__main__":
	unittest.main()
