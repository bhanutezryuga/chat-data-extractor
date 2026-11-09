import pytest
from fastapi.testclient import TestClient

from capture.auth import LOGIN_HEADER
from capture.config import Settings
from capture.web import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(
        db_path=str(tmp_path / "t.db"),
        allowed_logins=frozenset({"me@example.com"}),
        dev_no_auth=False,
    )


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings), headers={LOGIN_HEADER: "me@example.com"})
