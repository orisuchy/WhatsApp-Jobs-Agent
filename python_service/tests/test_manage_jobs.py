"""Managing matched jobs: notes, hide, remove (with or without letting a repost match again)."""
import json

from fastapi.testclient import TestClient

import main
import outputs


def _setup(monkeypatch, tmp_path, texts=("Python dev at Acme", "Java dev at Beta")):
    monkeypatch.setattr(main, "DATA_DIR", str(tmp_path))
    for i, t in enumerate(texts):
        assert outputs.record_if_new(str(tmp_path), {"company": f"C{i}", "text": t})


def test_note_and_hide_are_stored_and_returned(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    with TestClient(main.app) as c:
        jid = c.get("/api/jobs").json()[0]["id"]
        etag = c.get("/api/jobs").headers["etag"]
        assert c.patch(f"/api/jobs/{jid}", json={"note": "Call Dana on Sunday", "hidden": True}).status_code == 200
        job = next(j for j in c.get("/api/jobs").json() if j["id"] == jid)
        assert job["note"] == "Call Dana on Sunday" and job["hidden"] is True
        assert c.get("/api/jobs", headers={"If-None-Match": etag}).status_code == 200  # a note change is new data
        c.patch(f"/api/jobs/{jid}", json={"note": "", "hidden": None})
        job = next(j for j in c.get("/api/jobs").json() if j["id"] == jid)
        assert "note" not in job and "hidden" not in job
        assert json.loads((tmp_path / outputs.STATE_FILE).read_text()) == {}


def test_unknown_or_malformed_id_and_long_note_are_rejected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    with TestClient(main.app) as c:
        jid = c.get("/api/jobs").json()[0]["id"]
        assert c.patch("/api/jobs/" + "0" * 64, json={"note": "x"}).status_code == 404
        assert c.patch("/api/jobs/../etc", json={"note": "x"}).status_code in (404, 422)
        assert c.delete("/api/jobs/" + "0" * 64).status_code == 404
        assert c.patch(f"/api/jobs/{jid}", json={"note": "x" * 5001}).status_code == 422


def test_remove_keeps_reposts_ignored_by_default(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    with TestClient(main.app) as c:
        first = c.get("/api/jobs").json()[0]
        c.patch(f"/api/jobs/{first['id']}", json={"note": "n"})
        assert c.delete(f"/api/jobs/{first['id']}").status_code == 200
        assert [j["id"] for j in c.get("/api/jobs").json()] != [first["id"]]
        assert len(c.get("/api/jobs").json()) == 1
        assert first["id"] not in json.loads((tmp_path / outputs.STATE_FILE).read_text())
    assert not outputs.record_if_new(str(tmp_path), {"text": first["text"]})  # repost still ignored


def test_remove_with_allow_again_lets_a_repost_match(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    with TestClient(main.app) as c:
        first = c.get("/api/jobs").json()[0]
        assert c.delete(f"/api/jobs/{first['id']}?allow_again=true").status_code == 200
    assert outputs.record_if_new(str(tmp_path), {"text": first["text"]})  # repost is new again
    assert not outputs.record_if_new(str(tmp_path), {"text": "Java dev at Beta"})  # others untouched


def test_star_and_status_set_clear_and_change_etag(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    with TestClient(main.app) as c:
        jid = c.get("/api/jobs").json()[0]["id"]
        etag = c.get("/api/jobs").headers["etag"]
        assert c.patch(f"/api/jobs/{jid}", json={"starred": True, "status": "applied"}).status_code == 200
        job = next(j for j in c.get("/api/jobs").json() if j["id"] == jid)
        assert job["starred"] is True and job["status"] == "applied"
        assert c.get("/api/jobs", headers={"If-None-Match": etag}).status_code == 200
        c.patch(f"/api/jobs/{jid}", json={"status": None, "starred": False})
        job = next(j for j in c.get("/api/jobs").json() if j["id"] == jid)
        assert "status" not in job and "starred" not in job
        c.patch(f"/api/jobs/{jid}", json={"status": "offer"})
        c.patch(f"/api/jobs/{jid}", json={"status": ""})
        assert json.loads((tmp_path / outputs.STATE_FILE).read_text()) == {}


def test_unhide_is_remembered_and_invalid_status_rejected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    with TestClient(main.app) as c:
        jid = c.get("/api/jobs").json()[0]["id"]
        assert c.patch(f"/api/jobs/{jid}", json={"status": "maybe"}).status_code == 422
        c.patch(f"/api/jobs/{jid}", json={"hidden": False})
        assert json.loads((tmp_path / outputs.STATE_FILE).read_text())[jid] == {"hidden": False}
