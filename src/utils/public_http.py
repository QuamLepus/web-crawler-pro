"""HTTP fetches pinned to a validated public DNS address.

This is used for customer-configured URL watches. Each connection resolves its
hostname once, rejects the request if *any* answer is non-public, and connects
to one of the validated numeric addresses while retaining the original Host
header and TLS SNI name. Redirect targets go through the same checks.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from email.message import Message
from urllib.parse import urljoin, urlsplit

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5


class UnsafePublicURL(ValueError):
    """The URL is malformed, resolves to a non-public address, or is unsafe."""


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, *, pinned_ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, *, pinned_ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout,
                         context=ssl.create_default_context())
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _target(url: str) -> tuple[str, str, int, str, str]:
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise UnsafePublicURL("invalid URL") from exc
    if (scheme not in {"http", "https"} or not host or parsed.username is not None
            or parsed.password is not None):
        raise UnsafePublicURL("only credential-free HTTP(S) URLs are allowed")
    host = host.rstrip(".").lower()
    if not host or host == "localhost" or host.endswith((".localhost", ".local")):
        raise UnsafePublicURL("local hostnames are not allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise UnsafePublicURL("invalid hostname") from exc
    if port is None:
        port = 443 if scheme == "https" else 80
    if not 1 <= port <= 65535:
        raise UnsafePublicURL("invalid port")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    # Use the normalized DNS name for both HTTP Host and TLS SNI. Preserve
    # brackets around IPv6 literals and include only non-default ports.
    try:
        header_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafePublicURL("invalid hostname") from exc
    if ":" in header_host:
        header_host = f"[{header_host}]"
    default_port = 443 if scheme == "https" else 80
    host_header = header_host if port == default_port else f"{header_host}:{port}"
    return scheme, host, port, path, host_header


def _public_addresses(host: str, port: int) -> list[str]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    else:
        try:
            records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafePublicURL("hostname did not resolve") from exc
        addresses = list(dict.fromkeys(record[4][0] for record in records))
    if not addresses:
        raise UnsafePublicURL("hostname did not resolve")
    for value in addresses:
        try:
            address = ipaddress.ip_address(value.split("%", 1)[0])
        except ValueError as exc:
            raise UnsafePublicURL("hostname resolved to an invalid address") from exc
        if not address.is_global:
            raise UnsafePublicURL("hostname resolves to a non-public address")
    return addresses


def _read_response(url: str, timeout: float, *, max_response_bytes: int,
                   user_agent: str):
    scheme, host, port, path, host_header = _target(url)
    addresses = _public_addresses(host, port)
    connection_type = (_PinnedHTTPSConnection if scheme == "https"
                       else _PinnedHTTPConnection)
    connection = connection_type(host, port, pinned_ip=addresses[0], timeout=timeout)
    try:
        connection.putrequest("GET", path, skip_host=True)
        connection.putheader("Host", host_header)
        connection.putheader("User-Agent", user_agent)
        connection.putheader("Accept", "text/html,application/xhtml+xml,*/*;q=0.5")
        connection.putheader("Accept-Encoding", "identity")
        connection.putheader("Connection", "close")
        connection.endheaders()
        response = connection.getresponse()
        content_length = response.getheader("Content-Length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = None
            if declared_size is not None and declared_size > max_response_bytes:
                raise UnsafePublicURL("response exceeds the configured size limit")
        content_encoding = (response.getheader("Content-Encoding") or "identity").lower()
        if content_encoding not in {"", "identity"}:
            raise UnsafePublicURL("compressed responses are not accepted")
        body = response.read(max_response_bytes + 1)
        if len(body) > max_response_bytes:
            raise UnsafePublicURL("response exceeds the configured size limit")
        content_type = response.getheader("Content-Type", "")
        status = response.status
        location = response.getheader("Location")
        return status, location, content_type, body
    finally:
        connection.close()


def fetch_public_response(
    url: str,
    *,
    timeout: float = 15.0,
    max_redirects: int = MAX_REDIRECTS,
    user_agent: str = "WebCrawlerPro/1.0",
    max_response_bytes: int = MAX_RESPONSE_BYTES,
) -> tuple[str, int, str]:
    """Fetch a public URL, returning decoded text, status, and content type.

    Each hostname is resolved and validated before the socket connects to that
    exact numeric address. Redirects are revalidated, HTTPS redirects may not
    downgrade, and the response is bounded. The connection is direct, without
    ambient HTTP proxy settings that could change DNS resolution.
    """
    if max_response_bytes < 0:
        raise ValueError("max_response_bytes must be non-negative")
    current = url
    for redirect_count in range(max_redirects + 1):
        status, location, content_type, body = _read_response(
            current, timeout, max_response_bytes=max_response_bytes,
            user_agent=user_agent)
        if status in {301, 302, 303, 307, 308} and location:
            if redirect_count >= max_redirects:
                raise UnsafePublicURL("too many redirects")
            next_url = urljoin(current, location)
            next_scheme = urlsplit(next_url).scheme.lower()
            current_scheme = urlsplit(current).scheme.lower()
            if current_scheme == "https" and next_scheme != "https":
                raise UnsafePublicURL("HTTPS redirects may not downgrade to HTTP")
            # Validate URL syntax here; _read_response resolves and checks all
            # DNS answers immediately before opening the next connection.
            _target(next_url)
            current = next_url
            continue
        if content_type:
            metadata = Message()
            metadata["content-type"] = content_type
            charset = metadata.get_content_charset() or "utf-8"
        else:
            charset = "utf-8"
        try:
            text = body.decode(charset, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        return text, status, content_type
    raise UnsafePublicURL("too many redirects")


def fetch_public_html(
    url: str,
    *,
    timeout: float = 15.0,
    max_redirects: int = MAX_REDIRECTS,
    user_agent: str = "WebCrawlerPro-Watch/1.0",
    max_response_bytes: int = MAX_RESPONSE_BYTES,
) -> tuple[str, int]:
    """Compatibility wrapper returning text and status for public HTML fetches."""
    text, status, _content_type = fetch_public_response(
        url, timeout=timeout, max_redirects=max_redirects, user_agent=user_agent,
        max_response_bytes=max_response_bytes)
    return text, status
