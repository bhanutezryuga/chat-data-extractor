import dataclasses

from fastapi.testclient import TestClient

from capture.config import load_settings
from capture.web import create_app


def test_healthz_needs_no_login(settings):
    c = TestClient(create_app(settings))
    assert c.get("/healthz").json() == {"ok": True}


def test_home_without_header_is_403(settings):
    r = TestClient(create_app(settings)).get("/")
    assert r.status_code == 403 and r.text == "Not allowed"


def test_home_with_wrong_login_is_403(settings):
    r = TestClient(create_app(settings)).get("/", headers={"Tailscale-User-Login": "x@evil.com"})
    assert r.status_code == 403


def test_home_with_allowed_login_case_insensitive(settings):
    r = TestClient(create_app(settings)).get("/", headers={"Tailscale-User-Login": "Me@Example.com"})
    assert r.status_code == 200 and "Capture" in r.text


def test_empty_allow_list_refuses_everyone(settings):
    s = dataclasses.replace(settings, allowed_logins=frozenset())
    r = TestClient(create_app(s)).get("/", headers={"Tailscale-User-Login": "me@example.com"})
    assert r.status_code == 403


def test_dev_no_auth_lets_anyone_in(settings):
    s = dataclasses.replace(settings, allowed_logins=frozenset(), dev_no_auth=True)
    assert TestClient(create_app(s)).get("/").status_code == 200


def test_load_settings_parses_env():
    s = load_settings({"CAPTURE_DB_PATH": "/x.db", "CAPTURE_ALLOWED_LOGINS": " A@b.com, ,c@d.com "})
    assert s.db_path == "/x.db" and s.allowed_logins == {"a@b.com", "c@d.com"} and s.dev_no_auth is False
