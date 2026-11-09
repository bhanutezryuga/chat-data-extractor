from capture.auth import LOGIN_HEADER

REEL = "Look https://www.instagram.com/reel/C9xYz12AbC/?igsh=x"


def _items(client):
    return client.get("/api/items").json()["items"]


def test_api_capture_201_then_200_on_duplicate(client):
    body = {"text": "https://youtu.be/dQw4w9WgXcQ"}
    first = client.post("/api/capture", json=body)
    assert first.status_code == 201
    assert first.json()["duplicate"] is False
    second = client.post("/api/capture", json=body)
    assert second.status_code == 200
    assert second.json() == {"item_id": first.json()["item_id"], "duplicate": True}


def test_api_capture_422_on_empty(client):
    r = client.post("/api/capture", json={"text": "  "})
    assert r.status_code == 422
    assert "error" in r.json()


def test_api_capture_422_on_bad_type_hint(client):
    r = client.post("/api/capture", json={"text": "hello", "type_hint": "nope"})
    assert r.status_code == 422


def test_share_multipart_from_android(client):
    r = client.post("/share", files={"title": (None, "Reel"), "text": (None, REEL)},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/saved"
    items = _items(client)
    assert len(items) == 1
    assert items[0]["url_norm"] == "https://instagram.com/p/C9xYz12AbC"


def test_share_urlencoded(client):
    r = client.post("/share", data={"title": "Reel", "text": REEL}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/saved"
    assert len(_items(client)) == 1


def test_share_with_only_url_field(client):
    r = client.post("/share", data={"url": "https://youtu.be/dQw4w9WgXcQ"},
                    follow_redirects=False)
    assert r.headers["location"] == "/saved"
    assert _items(client)[0]["url"] == "https://youtu.be/dQw4w9WgXcQ"


def test_share_text_without_link_is_saved(client):
    r = client.post("/share", data={"text": "sourdough 1:5:5"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/saved"
    items = _items(client)
    assert len(items) == 1
    assert items[0]["url"] is None


def test_share_empty_redirects_with_error(client):
    r = client.post("/share", data={"text": "  "}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/?error=empty"
    assert _items(client) == []
    assert "Nothing to save — paste a link or some text." in client.get("/?error=empty").text


def test_share_bad_type_hint_still_saves(client):
    r = client.post("/share", data={"text": "hello", "type_hint": "bogus"},
                    follow_redirects=False)
    assert r.headers["location"] == "/saved"
    assert _items(client)[0]["type_hint"] is None


def test_share_empty_type_hint_means_none(client):
    client.post("/share", data={"text": "hello", "type_hint": ""})
    assert _items(client)[0]["type_hint"] is None


def test_share_return_to_other_goes_to_saved(client):
    r = client.post("/share", data={"text": "hello", "return_to": "https://evil.example"},
                    follow_redirects=False)
    assert r.headers["location"] == "/saved"


def test_paste_box_returns_home(client):
    r = client.post("/share", data={"text": "hello", "return_to": "home", "type_hint": "note"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/?saved=1"
    assert _items(client)[0]["type_hint"] == "note"


def test_saved_page_script(client):
    r = client.get("/saved")
    assert r.status_code == 200
    assert 'setTimeout(() => { window.close(); location.replace("/"); }, 1000)' in r.text
    assert "Saved ✓" in r.text


def test_home_lists_items_and_banner(client):
    client.post("/api/capture", json={"text": "https://youtu.be/dQw4w9WgXcQ"})
    html = client.get("/?saved=1").text
    assert "Saved ✓" in html
    assert "https://youtu.be/dQw4w9WgXcQ" in html
    assert 'id="outbox"' in html
    assert "Capture" in html
    assert "Saved ✓" not in client.get("/").text


def test_home_escapes_html(client):
    client.post("/api/capture", json={"text": "<script>x</script>"})
    html = client.get("/").text
    assert "&lt;script&gt;x&lt;/script&gt;" in html
    assert "<script>x</script>" not in html


def test_api_items_newest_first(client):
    client.post("/api/capture", json={"text": "first note"})
    client.post("/api/capture", json={"text": "second note"})
    texts = [i["share_text"] for i in _items(client)]
    assert texts == ["second note", "first note"]


def test_static_style_served(client):
    r = client.get("/static/style.css")
    assert r.status_code == 200
    assert "prefers-color-scheme: dark" in r.text


def test_share_requires_login(client, settings):
    from fastapi.testclient import TestClient
    from capture.web import create_app
    with TestClient(create_app(settings)) as anon:
        r = anon.post("/share", data={"text": "hello"}, follow_redirects=False)
        assert r.status_code == 403
    assert _items(client) == []
    r = client.post("/share", data={"text": "x"}, headers={LOGIN_HEADER: "other@example.com"})
    assert r.status_code == 403


def test_two_hostless_shares_make_two_items(client):
    client.post("/api/capture", json={"text": "https://… one"})
    client.post("/api/capture", json={"text": "https://… two"})
    assert len(_items(client)) == 2
