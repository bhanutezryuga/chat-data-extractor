"""SSRF guard for outbound fetches.

Blocks requests whose host resolves to a private/loopback/link-local/reserved IP
(e.g. 127.0.0.1, 169.254.169.254 cloud-metadata, 10/8, 192.168/16) and any non-HTTP(S)
scheme. A redirect handler re-validates every hop so a public URL can't bounce inward.
"""
import ipaddress
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse


def is_safe_public_url(url):
    try:
        p = urlparse(url or "")
    except Exception:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80),
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
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False
    return True


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_safe_public_url(newurl):
            raise urllib.error.HTTPError(newurl, code, "blocked redirect to non-public address",
                                         headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_opener():
    """An opener that re-validates redirect targets."""
    return urllib.request.build_opener(_SafeRedirect())
