"""Google Sheets key upload and connection test, via the web UI's endpoints."""
import json
import os
import stat
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

import main
import outputs

KEY = {"type": "service_account", "client_email": "bot@proj.iam.gserviceaccount.com", "private_key": "-----BEGIN..."}


def test_upload_saves_key_selects_it_and_never_returns_it(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")  # restored after the test (upload sets it)
    with TestClient(main.app) as c:
        assert c.get("/api/sheets").json() == {"client_email": None}
        r = c.post("/api/sheets/key", content=json.dumps(KEY))
        assert r.json() == {"client_email": KEY["client_email"]}
        path = tmp_path / "google-key.json"
        assert json.loads(path.read_text()) == KEY and stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"] == str(path)
        assert c.get("/api/sheets").json() == {"client_email": KEY["client_email"]}
        assert "private_key" not in c.get("/api/settings").text


def test_upload_rejects_files_that_are_not_service_account_keys(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DATA_DIR", str(tmp_path))
    with TestClient(main.app) as c:
        for body in (b"not json", json.dumps({"type": "authorized_user"}), b"x" * 70000):
            assert c.post("/api/sheets/key", content=body).status_code == 422
    assert not (tmp_path / "google-key.json").exists()


def test_connection_test_reports_what_to_fix(monkeypatch, tmp_path):
    import gspread

    key = tmp_path / "k.json"
    key.write_text(json.dumps(KEY))
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(key))
    book = MagicMock()
    book.worksheet.return_value.title = "all"
    sa = MagicMock()
    sa.return_value.open_by_key.return_value = book
    monkeypatch.setattr(gspread, "service_account", sa)
    with TestClient(main.app) as c:
        assert c.post("/api/sheets/test", json={"sheet_id": "s1", "tab": "all"}).json() == {"tab": "all"}
        sa.return_value.open_by_key.assert_called_with("s1")
        assert c.post("/api/sheets/test", json={"sheet_id": "", "tab": ""}).status_code == 422
        sa.return_value.open_by_key.side_effect = gspread.exceptions.SpreadsheetNotFound
        r = c.post("/api/sheets/test", json={"sheet_id": "s1", "tab": ""})
        assert r.status_code == 422 and KEY["client_email"] in r.json()["detail"]
