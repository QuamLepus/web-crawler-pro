from __future__ import annotations

import http.server
import socket
import threading
from typing import ClassVar

import pytest

from src.utils import public_http


class _Handler(http.server.BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, str]]] = []
    redirect_to: ClassVar[str] = ""
    oversized: ClassVar[bool] = False

    def do_GET(self):
        self.requests.append((self.path, self.headers.get("Host", "")))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", self.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.oversized:
            self.send_response(200)
            self.send_header("Content-Length", str(public_http.MAX_RESPONSE_BYTES + 1))
            self.end_headers()
            return
        body = b"<title>Public</title> pricing"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def local_http(monkeypatch):
    _Handler.requests = []
    _Handler.redirect_to = ""
    _Handler.oversized = False
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    real_getaddrinfo = socket.getaddrinfo
    real_create_connection = socket.create_connection

    def resolve(host, target_port, *args, **kwargs):
        if host == "example.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", target_port))]
        if host == "internal.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", target_port))]
        return real_getaddrinfo(host, target_port, *args, **kwargs)

    def connect(address, timeout=None, source_address=None):
        host, _target_port = address
        if host == "93.184.216.34":
            return real_create_connection(("127.0.0.1", port), timeout, source_address)
        return real_create_connection(address, timeout, source_address)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "create_connection", connect)
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_fetch_pins_validated_public_address_and_preserves_host(local_http):
    text, status = public_http.fetch_public_html(
        f"http://example.test:{local_http}/pricing?q=annual")

    assert status == 200
    assert "Public" in text
    assert _Handler.requests == [("/pricing?q=annual", f"example.test:{local_http}")]


def test_redirect_to_private_address_is_rejected_before_connection(local_http):
    _Handler.redirect_to = f"http://internal.test:{local_http}/admin"

    with pytest.raises(public_http.UnsafePublicURL, match="non-public"):
        public_http.fetch_public_html(f"http://example.test:{local_http}/redirect")

    assert len(_Handler.requests) == 1
    assert _Handler.requests[0][0] == "/redirect"


def test_mixed_public_and_private_dns_answers_are_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 80)),
    ])
    monkeypatch.setattr(socket, "create_connection",
                        lambda *_a, **_k: pytest.fail("must reject before connecting"))

    with pytest.raises(public_http.UnsafePublicURL, match="non-public"):
        public_http.fetch_public_html("http://mixed.example.test/")


@pytest.mark.parametrize("url", ["ftp://example.test/file", "http://user:pw@example.test/",
                                  "http://example.test:0/", "http://localhost/"])
def test_invalid_or_local_urls_are_rejected_before_connect(url):
    with pytest.raises(public_http.UnsafePublicURL):
        public_http.fetch_public_html(url)


def test_private_literal_is_rejected(local_http):
    with pytest.raises(public_http.UnsafePublicURL, match="non-public"):
        public_http.fetch_public_html("http://127.0.0.1/admin")
    assert _Handler.requests == []


def test_oversized_response_is_rejected(local_http):
    _Handler.oversized = True
    with pytest.raises(public_http.UnsafePublicURL, match="size limit"):
        public_http.fetch_public_html(f"http://example.test:{local_http}/large")
