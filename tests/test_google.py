"""Connecting Google Drive: finding the OAuth client file, and the connect endpoint."""

import json
import os
import time
from urllib.parse import parse_qs, urlparse

import pytest

from autocutlib.cloud.gdrive import find_client_file

CLIENT = {"installed": {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "s3cret",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": ["http://localhost"]}}


def write(path, data, age=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    t = time.time() - age
    os.utime(path, (t, t))
    return path


def test_find_client_file_newest_valid(tmp_path):
    dl = tmp_path / "Downloads"
    write(dl / "client_secret_old.apps.googleusercontent.com.json", CLIENT, age=100)
    newer = {"installed": {**CLIENT["installed"], "client_id": "456-new.apps.googleusercontent.com"}}
    write(dl / "client_secret_new.apps.googleusercontent.com.json", newer, age=10)
    write(dl / "client_secret_broken.json", "{not json", age=0)          # newest, but invalid
    write(dl / "client_secret_other.json", {"web_not": 1}, age=0)       # not an OAuth client
    write(dl / "notes.json", CLIENT, age=0)                               # not named like one
    path, cfg = find_client_file([tmp_path / "nope", dl])
    assert path.name.startswith("client_secret_new") and cfg == newer


def test_find_client_file_renamed_by_browser(tmp_path):
    write(tmp_path / "client_secret_123.apps.googleusercontent.com (1).json", CLIENT)
    assert find_client_file([tmp_path])[1] == CLIENT


def test_find_client_file_none(tmp_path):
    assert find_client_file([tmp_path]) == (None, None)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AUTOCUT_STORE", raising=False)
    from autocutlib.web import server
    monkeypatch.setattr(server, "SRC_ROOT", tmp_path / "app" / "src")   # don't pick up the repo folder
    app = server.create_app(tmp_path / "ws", port=8765)
    c = app.test_client()
    c.environ_base.update(HTTP_HOST="127.0.0.1:8765", HTTP_X_AUTOCUT="1")
    return c, tmp_path


def test_connect_without_client_file_asks_for_it(client):
    c, _ = client
    r = c.post("/api/google/connect")
    assert r.status_code == 200 and r.get_json() == {"need_client": True}


def test_connect_finds_downloaded_client_file(client):
    c, tmp = client
    write(tmp / "home" / "Downloads" / "client_secret_123-abc.apps.googleusercontent.com.json", CLIENT)
    r = c.post("/api/google/connect").get_json()
    url = urlparse(r["url"])
    q = parse_qs(url.query)
    assert url.netloc == "accounts.google.com"
    assert q["client_id"] == ["123-abc.apps.googleusercontent.com"]
    assert q["redirect_uri"] == ["http://127.0.0.1:8765/oauth/callback"]
    assert q["scope"] == ["https://www.googleapis.com/auth/drive.file"]
    assert c.get("/api/google").get_json()["client"] is True                 # remembered


def test_client_upload_then_connect(client):
    c, _ = client
    assert c.post("/api/google/client", json={"json": "{}"}).status_code == 400
    assert c.post("/api/google/client", json={"json": json.dumps(CLIENT)}).status_code == 200
    assert "url" in c.post("/api/google/connect").get_json()


def test_missing_google_libraries_explained(client, monkeypatch):
    import sys
    c, _ = client
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", None)
    c.post("/api/google/client", json={"json": json.dumps(CLIENT)})
    r = c.post("/api/google/connect")
    assert r.status_code == 400 and "pip install" in r.get_json()["error"]


def test_unexpected_errors_are_json(client, monkeypatch):
    from autocutlib.web import server
    c, _ = client

    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(server, "list_projects", boom)
    monkeypatch.setenv("AUTOCUT_STORE", "local:" + str(_ / "drive"))
    r = c.get("/api/projects")
    assert r.status_code == 500 and "RuntimeError: disk on fire" in r.get_json()["error"]
