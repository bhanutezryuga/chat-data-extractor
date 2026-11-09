import io
import urllib.error
from email.message import Message

import pytest

from capture import netguard, titlefetch
from capture.titlefetch import Blocked, fetch_title

URL = "https://example.com/post"


class FakeResp(io.BytesIO):
    def __init__(self, body, charset=None):
        super().__init__(body)
        self.headers = Message()
        if charset:
            self.headers["Content-Type"] = f"text/html; charset={charset}"
        self.read_sizes = []

    def read(self, n=-1):
        self.read_sizes.append(n)
        return super().read(n)


class FakeOpener:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def open(self, req, timeout=None):
        self.calls.append(req)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def opener(monkeypatch):
    holder = {}

    def install(result, safe=True):
        o = FakeOpener(result)
        holder["o"] = o
        monkeypatch.setattr(netguard, "safe_opener", lambda: o)
        monkeypatch.setattr(netguard, "is_safe_public_url", lambda u: safe)
        return o

    return install


def http_error(code):
    return urllib.error.HTTPError(URL, code, "x", Message(), None)


def test_returns_og_title(opener):
    opener(FakeResp(b'<meta property="og:title" content="Hello">'))
    assert fetch_title(URL) == "Hello"


def test_404_is_none(opener):
    opener(http_error(404))
    assert fetch_title(URL) is None


def test_500_raises(opener):
    opener(http_error(500))
    with pytest.raises(urllib.error.HTTPError):
        fetch_title(URL)


def test_blocked_redirect_becomes_blocked(opener):
    opener(netguard.BlockedRedirect("http://10.0.0.1/", 302, "whatever", Message(), None))
    with pytest.raises(Blocked):
        fetch_title(URL)


def test_unsafe_url_never_opened(opener):
    o = opener(FakeResp(b""), safe=False)
    with pytest.raises(Blocked):
        fetch_title(URL)
    assert o.calls == []


def test_urlerror_propagates(opener):
    opener(urllib.error.URLError("down"))
    with pytest.raises(urllib.error.URLError):
        fetch_title(URL)


def test_charset_honoured(opener):
    opener(FakeResp("<title>Caf\xe9</title>".encode("iso-8859-1"), charset="iso-8859-1"))
    assert fetch_title(URL) == "Café"


def test_bogus_charset_falls_back_to_utf8(opener):
    opener(FakeResp("<title>Café</title>".encode("utf-8"), charset="nope-123"))
    assert fetch_title(URL) == "Café"


def test_reads_at_most_max_bytes_and_sends_user_agent(opener):
    resp = FakeResp(b"<title>x</title>")
    o = opener(resp)
    fetch_title(URL)
    assert resp.read_sizes == [titlefetch.MAX_BYTES]
    assert o.calls[0].get_header("User-agent") == "Mozilla/5.0 (compatible; CaptureBot/1.0)"
