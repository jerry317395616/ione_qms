from __future__ import annotations

import http.client
import ipaddress
import posixpath
import socket
import ssl
import zipfile
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import unquote, urljoin, urlparse

SAFE_STANDARD_MEDIA_TYPES = frozenset(
	{
		"application/pdf",
		"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
		"text/plain",
	}
)
_BINARY_SAFE_TRANSPORT_MEDIA_TYPE = "application/octet-stream"
_MAX_AUTHORITY_URL_LENGTH = 4_096
_MAX_RESOLVED_ADDRESSES = 16
_MAX_REDIRECTS = 3
_MAX_DOCX_MEMBERS = 5_000
_MAX_DOCX_EXPANDED_BYTES = 250 * 1024 * 1024
_MAX_DOCX_COMPRESSION_RATIO = 100
_SAFE_TEXT_CONTROLS = frozenset({"\t", "\n", "\r"})
_ACTIVE_TEXT_MARKERS = (
	"<!doctype",
	"<?xml",
	"<embed",
	"<html",
	"<iframe",
	"<object",
	"<script",
	"<svg",
)


class StandardArchiveError(ValueError):
	"""Fail-closed validation error for a controlled Standard authority archive."""


@dataclass(frozen=True, slots=True)
class StandardArchivePayload:
	content: bytes
	media_type: str
	extension: str
	final_url: str


def download_standard_authority(
	source_url: str,
	*,
	allowed_hosts: set[str],
	max_bytes: int,
	connect_timeout: float = 5,
	read_timeout: float = 30,
) -> StandardArchivePayload:
	"""Download a safe authority artifact without a DNS-validation/connection gap.

	Every HTTPS connection is made directly to an address from the exact DNS
	answer set that passed the public-address check. TLS SNI and certificate
	verification still use the allowlisted authority hostname. Redirects repeat
	the complete validation and pinned-connect sequence.
	"""
	if max_bytes <= 0:
		raise StandardArchiveError("Standard archive byte limit must be positive")
	normalized_hosts = {_normalize_allowed_host(host) for host in allowed_hosts}
	if not normalized_hosts:
		raise StandardArchiveError("Standard archive authority allowlist is empty")
	current_url = str(source_url or "").strip()
	for redirect_count in range(_MAX_REDIRECTS + 1):
		parsed, addresses = _validated_authority_url(current_url, normalized_hosts)
		connection, response = _open_pinned_response(
			parsed,
			addresses,
			connect_timeout=connect_timeout,
			read_timeout=read_timeout,
		)
		try:
			if response.status in {301, 302, 303, 307, 308}:
				if redirect_count >= _MAX_REDIRECTS:
					raise StandardArchiveError("Standard authority URL exceeded the governed redirect limit")
				location = str(response.getheader("Location") or "").strip()
				if not location:
					raise StandardArchiveError("Standard authority redirect has no Location")
				current_url = urljoin(current_url, location)
				continue
			if response.status != 200:
				raise StandardArchiveError(f"Standard authority archive returned HTTP {int(response.status)}")
			if str(response.getheader("Content-Encoding") or "").strip().lower() not in {"", "identity"}:
				raise StandardArchiveError(
					"Standard authority archive must not use a transformed Content-Encoding"
				)
			content_length = _validated_content_length(response.getheader("Content-Length"), max_bytes)
			content = _read_bounded_response(response, max_bytes=max_bytes)
			if content_length is not None and content_length != len(content):
				raise StandardArchiveError(
					"Standard authority Content-Length does not match the received archive"
				)
			media_type, extension = validate_standard_archive_content(
				content,
				declared_media_type=str(response.getheader("Content-Type") or ""),
				final_url=current_url,
				max_bytes=max_bytes,
			)
			return StandardArchivePayload(
				content=content,
				media_type=media_type,
				extension=extension,
				final_url=current_url,
			)
		finally:
			response.close()
			connection.close()
	raise StandardArchiveError("Standard authority URL exceeded the governed redirect limit")


def validate_standard_archive_content(
	content: bytes,
	*,
	declared_media_type: str,
	final_url: str,
	max_bytes: int,
) -> tuple[str, str]:
	if not isinstance(content, bytes) or not content:
		raise StandardArchiveError("Standard authority URL returned no archival bytes")
	if len(content) > max_bytes:
		raise StandardArchiveError("Standard authority archive exceeds the governed size limit")
	declared = str(declared_media_type or "").partition(";")[0].strip().lower()
	if declared not in SAFE_STANDARD_MEDIA_TYPES | {_BINARY_SAFE_TRANSPORT_MEDIA_TYPE}:
		raise StandardArchiveError("Standard authority returned an unapproved Content-Type")

	if content.startswith(b"%PDF-"):
		if b"%%EOF" not in content[-4_096:]:
			raise StandardArchiveError("Standard authority PDF is incomplete or malformed")
		media_type, extension = "application/pdf", "pdf"
	elif content.startswith(b"PK\x03\x04"):
		_validate_docx_archive(content, max_bytes=max_bytes)
		media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
		extension = "docx"
	else:
		_validate_plain_text(content)
		media_type, extension = "text/plain", "txt"

	if declared == _BINARY_SAFE_TRANSPORT_MEDIA_TYPE and extension not in {"pdf", "docx"}:
		raise StandardArchiveError(
			"Generic binary Content-Type is allowed only for a verified PDF or DOCX archive"
		)
	if declared not in {media_type, _BINARY_SAFE_TRANSPORT_MEDIA_TYPE}:
		raise StandardArchiveError("Standard authority Content-Type does not match its file signature")
	_validate_source_extension(final_url, extension)
	return media_type, extension


def _normalize_allowed_host(host: str) -> str:
	raw = str(host or "").strip().lower()
	if not raw or raw.endswith(".") or any(character.isspace() for character in raw):
		raise StandardArchiveError("Standard archive allowlist contains an invalid hostname")
	try:
		normalized = raw.encode("idna").decode("ascii")
	except UnicodeError as exc:
		raise StandardArchiveError("Standard archive allowlist contains an invalid hostname") from exc
	try:
		ipaddress.ip_address(normalized)
	except ValueError:
		return normalized
	raise StandardArchiveError("Standard archive allowlist must contain DNS hostnames, not IP literals")


def _validated_authority_url(
	url: str,
	allowed_hosts: set[str],
) -> tuple[object, tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]]:
	if (
		not url
		or len(url) > _MAX_AUTHORITY_URL_LENGTH
		or "\\" in url
		or any(ord(character) < 32 or ord(character) == 127 for character in url)
	):
		raise StandardArchiveError("Standard archive URL is malformed or exceeds its size limit")
	try:
		parsed = urlparse(url)
		port = parsed.port
	except ValueError as exc:
		raise StandardArchiveError("Standard archive URL has an invalid authority") from exc
	raw_host = str(parsed.hostname or "")
	if raw_host.endswith("."):
		raise StandardArchiveError("Standard archive URL must use a canonical authority hostname")
	try:
		host = raw_host.encode("idna").decode("ascii").lower()
	except UnicodeError as exc:
		raise StandardArchiveError("Standard archive URL has an invalid authority hostname") from exc
	if (
		parsed.scheme != "https"
		or not host
		or host not in allowed_hosts
		or parsed.username
		or parsed.password
		or parsed.fragment
		or port not in (None, 443)
	):
		raise StandardArchiveError(
			"Standard archive URL is not an allowlisted credential-free HTTPS authority"
		)
	try:
		ipaddress.ip_address(host)
	except ValueError:
		pass
	else:
		raise StandardArchiveError("Standard archive authority must be a DNS hostname")
	addresses = _resolve_public_addresses(host)
	return parsed, addresses


def _resolve_public_addresses(
	host: str,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
	try:
		answers = socket.getaddrinfo(
			host,
			443,
			family=socket.AF_UNSPEC,
			type=socket.SOCK_STREAM,
			proto=socket.IPPROTO_TCP,
		)
	except OSError as exc:
		raise StandardArchiveError("Standard archive authority DNS could not be verified") from exc
	addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
	for answer in answers:
		try:
			address = _normalized_ip_address(str(answer[4][0]))
		except (IndexError, ValueError) as exc:
			raise StandardArchiveError("Standard archive authority DNS returned an invalid address") from exc
		if not address.is_global:
			raise StandardArchiveError(
				"Standard archive authority must resolve only to public global addresses"
			)
		addresses.add(address)
	if not addresses:
		raise StandardArchiveError("Standard archive authority DNS returned no usable address")
	if len(addresses) > _MAX_RESOLVED_ADDRESSES:
		raise StandardArchiveError("Standard archive authority DNS returned too many addresses")
	return tuple(sorted(addresses, key=lambda address: (address.version, int(address))))


def _normalized_ip_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
	address = ipaddress.ip_address(value.partition("%")[0])
	if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
		return address.ipv4_mapped
	return address


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
	def __init__(
		self,
		authority_host: str,
		pinned_address: ipaddress.IPv4Address | ipaddress.IPv6Address,
		*,
		connect_timeout: float,
		read_timeout: float,
	) -> None:
		context = ssl.create_default_context()
		context.check_hostname = True
		context.verify_mode = ssl.CERT_REQUIRED
		context.minimum_version = ssl.TLSVersion.TLSv1_2
		super().__init__(authority_host, port=443, timeout=connect_timeout, context=context)
		self._pinned_address = pinned_address
		self._read_timeout = read_timeout

	def connect(self) -> None:
		if self._tunnel_host:
			raise StandardArchiveError("Standard archive connections cannot use a proxy tunnel")
		raw_socket = socket.create_connection(
			(str(self._pinned_address), 443),
			timeout=self.timeout,
			source_address=self.source_address,
		)
		try:
			peer = _normalized_ip_address(str(raw_socket.getpeername()[0]))
			if peer != self._pinned_address:
				raise StandardArchiveError(
					"Standard archive TLS peer does not match its verified DNS address"
				)
			self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
			self.sock.settimeout(self._read_timeout)
		except Exception:
			raw_socket.close()
			raise


def _open_pinned_response(
	parsed,
	addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...],
	*,
	connect_timeout: float,
	read_timeout: float,
) -> tuple[_PinnedHTTPSConnection, http.client.HTTPResponse]:
	host = str(parsed.hostname or "").encode("idna").decode("ascii").lower()
	path = parsed.path or "/"
	if parsed.query:
		path = f"{path}?{parsed.query}"
	last_error: Exception | None = None
	for address in addresses:
		connection = _PinnedHTTPSConnection(
			host,
			address,
			connect_timeout=connect_timeout,
			read_timeout=read_timeout,
		)
		try:
			connection.request(
				"GET",
				path,
				headers={
					"Accept": (
						"application/pdf,"
						"application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
						"text/plain"
					),
					"Accept-Encoding": "identity",
					"Connection": "close",
					"User-Agent": "IONE-QMS-Standard-Archive/1",
				},
			)
			return connection, connection.getresponse()
		except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
			last_error = exc
			connection.close()
	if last_error is not None:
		raise StandardArchiveError("Standard authority URL could not be archived") from last_error
	raise StandardArchiveError("Standard authority URL has no verified public address")


def _validated_content_length(value: str | None, max_bytes: int) -> int | None:
	if value in {None, ""}:
		return None
	try:
		length = int(str(value))
	except ValueError as exc:
		raise StandardArchiveError("Standard authority returned an invalid Content-Length") from exc
	if length < 1:
		raise StandardArchiveError("Standard authority returned an empty Content-Length")
	if length > max_bytes:
		raise StandardArchiveError("Standard authority archive exceeds the governed size limit")
	return length


def _read_bounded_response(response: http.client.HTTPResponse, *, max_bytes: int) -> bytes:
	chunks: list[bytes] = []
	total = 0
	while True:
		chunk = response.read(min(64 * 1024, max_bytes - total + 1))
		if not chunk:
			break
		total += len(chunk)
		if total > max_bytes:
			raise StandardArchiveError("Standard authority archive exceeds the governed size limit")
		chunks.append(chunk)
	content = b"".join(chunks)
	if not content:
		raise StandardArchiveError("Standard authority URL returned no archival bytes")
	return content


def _validate_source_extension(final_url: str, expected_extension: str) -> None:
	path = unquote(urlparse(final_url).path)
	base_name = posixpath.basename(path).strip().lower()
	if "." not in base_name:
		return
	extension = base_name.rsplit(".", 1)[-1]
	if extension != expected_extension:
		raise StandardArchiveError(
			"Standard authority URL extension does not match the verified archive type"
		)


def _validate_plain_text(content: bytes) -> None:
	if b"\x00" in content:
		raise StandardArchiveError("Standard authority text archive contains binary data")
	try:
		text = content.decode("utf-8-sig")
	except UnicodeDecodeError as exc:
		raise StandardArchiveError("Standard authority text archive must be valid UTF-8") from exc
	if any(ord(character) < 32 and character not in _SAFE_TEXT_CONTROLS for character in text):
		raise StandardArchiveError("Standard authority text archive contains unsafe control characters")
	lowered = text.casefold()
	if any(marker in lowered for marker in _ACTIVE_TEXT_MARKERS):
		raise StandardArchiveError("Standard authority text archive contains active markup")


def _validate_docx_archive(content: bytes, *, max_bytes: int) -> None:
	try:
		with zipfile.ZipFile(BytesIO(content)) as archive:
			members = archive.infolist()
			if not members or len(members) > _MAX_DOCX_MEMBERS:
				raise StandardArchiveError("Standard authority DOCX has an invalid member count")
			names: set[str] = set()
			expanded_bytes = 0
			for member in members:
				name = member.filename.replace("\\", "/")
				lowered = name.casefold()
				parts = tuple(part for part in name.split("/") if part)
				if (
					not name
					or name.startswith("/")
					or ".." in parts
					or member.flag_bits & 0x1
					or lowered.endswith(".bin")
					or lowered.startswith(("word/activex/", "word/embeddings/", "customui/"))
				):
					raise StandardArchiveError("Standard authority DOCX contains an unsafe member")
				expanded_bytes += int(member.file_size)
				if expanded_bytes > min(_MAX_DOCX_EXPANDED_BYTES, max_bytes * 10):
					raise StandardArchiveError("Standard authority DOCX expands beyond its safety limit")
				if member.file_size and member.compress_size == 0:
					raise StandardArchiveError(
						"Standard authority DOCX contains an invalid compressed member"
					)
				if (
					member.compress_size
					and member.file_size > member.compress_size * _MAX_DOCX_COMPRESSION_RATIO
				):
					raise StandardArchiveError("Standard authority DOCX exceeds its compression-ratio limit")
				names.add(name)
			required = {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}
			if not required.issubset(names):
				raise StandardArchiveError("Standard authority ZIP is not a valid DOCX document")
			for member in members:
				if member.filename.casefold().endswith(".rels"):
					relationships = archive.read(member)
					if b'targetmode="external"' in relationships.lower():
						raise StandardArchiveError(
							"Standard authority DOCX contains an external relationship"
						)
	except (zipfile.BadZipFile, OSError) as exc:
		raise StandardArchiveError("Standard authority DOCX is malformed") from exc
