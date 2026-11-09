import socket
import urllib.error

import pytest

from capture import netguard


def fake_dns(monkeypatch, addr):
    family = socket.AF_INET6 if ":" in addr else socket.AF_INET
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [(family, socket.SOCK_STREAM, 6, "", (addr, 443))],
    )


@pytest.mark.parametrize("addr", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254",
                                  "100.64.0.1", "100.100.100.100", "::1", "::ffff:127.0.0.1",
                                  "fd7a:115c:a1e0::1",
                                  "64:ff9b::a00:5", "64:ff9b::7f00:1", "fec0::1",
                                  "::127.0.0.1"])
def test_blocks_non_public(monkeypatch, addr):
    fake_dns(monkeypatch, addr)
    assert netguard.is_safe_public_url("https://evil.example/") is False


@pytest.mark.parametrize("addr", ["93.184.216.34", "64:ff9b::5db8:d822"])
def test_allows_public(monkeypatch, addr):
    fake_dns(monkeypatch, addr)
    assert netguard.is_safe_public_url("https://example.com/") is True


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x", "javascript:alert(1)"])
def test_rejects_non_http_schemes(url):
    assert netguard.is_safe_public_url(url) is False


def test_redirect_to_private_is_blocked(monkeypatch):
    fake_dns(monkeypatch, "10.0.0.5")
    handler = next(h for h in netguard.safe_opener().handlers
                   if isinstance(h, netguard._SafeRedirect))
    with pytest.raises(netguard.BlockedRedirect, match="blocked redirect"):
        handler.redirect_request(None, None, 302, "Found", {}, "http://internal.example/")
