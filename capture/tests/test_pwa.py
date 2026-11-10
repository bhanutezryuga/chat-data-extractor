import struct
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "static"


def test_manifest_share_target(client):
    m = client.get("/manifest.webmanifest").json()
    assert m["share_target"] == {
        "action": "/share", "method": "POST", "enctype": "multipart/form-data",
        "params": {"title": "title", "text": "text", "url": "url"},
    }
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert (m["name"], m["short_name"], m["start_url"], m["scope"], m["display"], m["theme_color"]) == \
        ("Capture", "Capture", "/", "/", "standalone", "#1f6feb")


def test_manifest_content_type(client):
    r = client.get("/manifest.webmanifest")
    assert r.headers["content-type"].startswith("application/manifest+json")


def test_sw_served_from_root(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert 'importScripts("/static/outbox.js")' in r.text
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["content-type"].startswith("text/javascript")


def test_manifest_and_sw_need_login(settings):
    from fastapi.testclient import TestClient
    from capture.web import create_app
    c = TestClient(create_app(settings))
    assert c.get("/sw.js").status_code == 403
    assert c.get("/manifest.webmanifest").status_code == 403


def test_home_links_manifest(client):
    html = client.get("/").text
    assert '<link rel="manifest" href="/manifest.webmanifest">' in html
    assert '<meta name="theme-color" content="#1f6feb">' in html
    assert '<script src="/static/app.js" defer></script>' in html


def test_share_with_client_id_replay_is_one_item(client):
    files = {"text": (None, "just a thought"), "client_id": (None, "abc-123")}
    assert client.post("/share", files=files, follow_redirects=False).status_code == 303
    assert client.post("/share", files=files, follow_redirects=False).status_code == 303
    assert len(client.get("/api/items").json()["items"]) == 1


@pytest.mark.parametrize("name,size", [("icon-192.png", 192), ("icon-512.png", 512),
                                       ("icon-maskable-512.png", 512)])
def test_icon_png_header(name, size):
    data = (STATIC / name).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", data[16:24])
    assert (w, h) == (size, size)
