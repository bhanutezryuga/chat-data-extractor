"""SSRF guard for outbound fetches (port of app/netguard.py).

Allows only http(s) URLs whose every resolved address is globally routable. That rejects
private, loopback, link-local, CGNAT/tailnet (100.64.0.0/10) and other special ranges.
A redirect handler re-validates every hop so a public URL can't bounce inward.
"""
import ipaddress
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse


def is_safe_public_url(url: str) -> bool:
    try:
        p = urlparse(url or "")
        port = p.port
    except Exception:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        infos = socket.getaddrinfo(p.hostname, port or (443 if p.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except Exception:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except Exception:
            return False
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        if not addr.is_global or addr.is_multicast:
            return False
    return True


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_safe_public_url(newurl):
            raise urllib.error.HTTPError(newurl, code, "blocked redirect to non-public address",
                                         headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_opener() -> urllib.request.OpenerDirector:
    """An opener that re-validates redirect targets."""
    return urllib.request.build_opener(_SafeRedirect())
