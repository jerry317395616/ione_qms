#!/usr/bin/env python3
"""Loopback-only, allowlisted CONNECT proxy for the server's GitHub API relay.

The server already has a root-owned listener on 127.0.0.1:443 that forwards
``CONNECT api.github.com:443`` requests to 127.0.0.1:17890.  This process
provides only that missing endpoint.  It is intentionally not a general HTTP
proxy and never handles application credentials or decrypted TLS.
"""

from __future__ import annotations

import ipaddress
import select
import socket
import socketserver
import threading
import time

import dns.exception
import dns.resolver

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 17890
ALLOWED_HOST = "api.github.com"
ALLOWED_PORT = 443
MAX_HEADER_BYTES = 8_192
HEADER_TIMEOUT_SECONDS = 10
CONNECT_TIMEOUT_SECONDS = 10
IDLE_TIMEOUT_SECONDS = 300
BUFFER_BYTES = 64 * 1024
DNS_SERVERS = ("1.1.1.1", "8.8.8.8")
DNS_CACHE_SECONDS = 600
# Last-resort bootstrap values observed from two independent GitHub API
# anycast regions. TLS still authenticates api.github.com, and successful TCP
# DNS refresh replaces this tuple before it expires.
BOOTSTRAP_ADDRESSES = ("20.205.243.168", "20.27.177.116")
_address_cache: tuple[str, ...] = ()
_address_cache_expires_at = 0.0
_address_cache_lock = threading.Lock()


class AllowlistedConnectHandler(socketserver.BaseRequestHandler):
	"""Relay one allowlisted CONNECT tunnel without inspecting TLS."""

	def handle(self) -> None:
		client = self.request
		client.settimeout(HEADER_TIMEOUT_SECONDS)
		header = _read_header(client)
		if not _is_allowed_connect(header):
			_send_status(client, 403, "Forbidden")
			return

		try:
			upstream = _connect_allowed_upstream()
		except OSError:
			_send_status(client, 502, "Bad Gateway")
			return

		with upstream:
			client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
			_relay(client, upstream)


class ThreadedConnectServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
	allow_reuse_address = True
	daemon_threads = True
	request_queue_size = 64


def _read_header(client: socket.socket) -> bytes:
	data = bytearray()
	while b"\r\n\r\n" not in data:
		chunk = client.recv(min(1024, MAX_HEADER_BYTES - len(data)))
		if not chunk:
			break
		data.extend(chunk)
		if len(data) >= MAX_HEADER_BYTES:
			break
	return bytes(data)


def _is_allowed_connect(header: bytes) -> bool:
	if not header.endswith(b"\r\n\r\n") or len(header) > MAX_HEADER_BYTES:
		return False
	try:
		request_line = header.split(b"\r\n", 1)[0].decode("ascii", errors="strict")
		method, target, version = request_line.split(" ")
	except UnicodeDecodeError:
		return False
	except ValueError:
		return False
	return (
		method == "CONNECT"
		and target.lower() == f"{ALLOWED_HOST}:{ALLOWED_PORT}"
		and version in {"HTTP/1.0", "HTTP/1.1"}
	)


def _public_ipv4_addresses() -> tuple[str, ...]:
	global _address_cache, _address_cache_expires_at

	now = time.monotonic()
	if _address_cache and now < _address_cache_expires_at:
		return _address_cache
	with _address_cache_lock:
		now = time.monotonic()
		if _address_cache and now < _address_cache_expires_at:
			return _address_cache
		resolver = dns.resolver.Resolver(configure=False)
		resolver.nameservers = list(DNS_SERVERS)
		resolver.timeout = 3
		resolver.lifetime = 8
		try:
			# UDP/53 is intermittently dropped on this host; TCP DNS is reliable
			# and bypasses the stale hosts entry without DoH bootstrap recursion.
			answers = resolver.resolve(ALLOWED_HOST, "A", search=False, tcp=True)
			addresses = tuple(
				address for answer in answers if ipaddress.ip_address(address := str(answer)).is_global
			)
		except dns.exception.DNSException:
			addresses = _address_cache or BOOTSTRAP_ADDRESSES
		if not addresses:
			raise OSError("Allowlisted GitHub API DNS resolution failed")
		_address_cache = addresses
		_address_cache_expires_at = now + DNS_CACHE_SECONDS
		return addresses


def _connect_allowed_upstream() -> socket.socket:
	last_error: OSError | None = None
	for address in _public_ipv4_addresses():
		try:
			return socket.create_connection(
				(address, ALLOWED_PORT),
				timeout=CONNECT_TIMEOUT_SECONDS,
			)
		except OSError as exc:
			last_error = exc
	if last_error:
		raise last_error
	raise OSError("No public GitHub API address was returned by the allowlisted resolvers")


def _relay(client: socket.socket, upstream: socket.socket) -> None:
	# Keep the sockets blocking for sendall(): TLS records can exceed the
	# instantaneous kernel buffer and a non-blocking sendall otherwise turns
	# normal backpressure into a truncated handshake. select() bounds reads.
	client.settimeout(CONNECT_TIMEOUT_SECONDS)
	upstream.settimeout(CONNECT_TIMEOUT_SECONDS)
	peers = {client: upstream, upstream: client}
	last_activity = time.monotonic()
	while time.monotonic() - last_activity < IDLE_TIMEOUT_SECONDS:
		readable, _writable, exceptional = select.select(
			list(peers),
			[],
			list(peers),
			5,
		)
		if exceptional:
			return
		for source in readable:
			chunk = source.recv(BUFFER_BYTES)
			if not chunk:
				return
			peers[source].sendall(chunk)
			last_activity = time.monotonic()


def _send_status(client: socket.socket, code: int, reason: str) -> None:
	try:
		client.sendall(f"HTTP/1.1 {code} {reason}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".encode())
	except OSError:
		return


def main() -> None:
	with ThreadedConnectServer(
		(LISTEN_HOST, LISTEN_PORT),
		AllowlistedConnectHandler,
	) as server:
		server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
	main()
