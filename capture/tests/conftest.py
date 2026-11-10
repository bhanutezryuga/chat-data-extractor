import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from capture.auth import LOGIN_HEADER
from capture.config import Settings
from capture.web import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]


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


@pytest.fixture(scope="session", autouse=True)
def _repo_root_cwd():
    """Deploy-file tests use repo-relative paths."""
    old = os.getcwd()
    os.chdir(REPO_ROOT)
    yield
    os.chdir(old)


@pytest.fixture
def store(settings):
    from capture.store import Store

    s = Store(settings.db_path)
    yield s
    s.close()
