"""Reminders: PATCH validation, due check, queue to WhatsApp, /state drain, one-shot sending."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import main
import outputs


def _iso(delta):
    return (datetime.now(timezone.utc) + timedelta(hours=delta)).isoformat()


def _setup(monkeypatch, tmp_path, outs="whatsapp"):
    monkeypatch.setattr(main, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OUTPUTS", outs)
    monkeypatch.setenv("TARGET_GROUP_ID", "target@g.us")
    main.pending_whatsapp.clear()
    assert outputs.record_if_new(str(tmp_path), {"company": "Acme", "score": 88, "summary": "Python", "text": "Dev at Acme https://x.io/1"})
    return outputs.read_matches(str(tmp_path))[0]["id"]


def test_patch_remind_at_validates_and_clears(monkeypatch, tmp_path):
    jid = _setup(monkeypatch, tmp_path)
    with TestClient(main.app) as c:
        assert c.patch(f"/api/jobs/{jid}", json={"remind_at": "2030-01-01T10:00:00"}).status_code == 422  # naive
        assert c.patch(f"/api/jobs/{jid}", json={"remind_at": "soon"}).status_code == 422
        assert c.patch(f"/api/jobs/{jid}", json={"remind_at": "2030-01-01T10:00:00Z"}).status_code == 200
        assert c.get("/api/jobs").json()[0]["remind_at"] == "2030-01-01T10:00:00Z"
        assert c.patch(f"/api/jobs/{jid}", json={"remind_at": ""}).status_code == 200
        assert "remind_at" not in c.get("/api/jobs").json()[0]


def test_only_due_reminders_are_sent_once(monkeypatch, tmp_path):
    jid = _setup(monkeypatch, tmp_path)
    sent = []
    monkeypatch.setattr(outputs, "send_reminder", sent.append)
    outputs.update_job(str(tmp_path), jid, {"remind_at": _iso(1)})
    main.check_reminders()
    assert sent == [] and main.pending_whatsapp == []  # not yet due
    outputs.update_job(str(tmp_path), jid, {"remind_at": _iso(-1), "note": "call Dana"})
    main.check_reminders()
    assert len(sent) == 1 and "Acme" in sent[0] and "call Dana" in sent[0]
    assert main.pending_whatsapp == [("target@g.us", sent[0])]
    assert "remind_at" not in outputs.read_matches(str(tmp_path))[0]  # cleared
    main.check_reminders()
    assert len(sent) == 1 and len(main.pending_whatsapp) == 1  # never re-sent


def test_whatsapp_queue_needs_output_and_target(monkeypatch, tmp_path):
    jid = _setup(monkeypatch, tmp_path, outs="file")
    monkeypatch.setattr(outputs, "send_reminder", lambda t: None)
    outputs.update_job(str(tmp_path), jid, {"remind_at": _iso(-1)})
    main.check_reminders()
    assert main.pending_whatsapp == []
    jid2 = outputs.read_matches(str(tmp_path))[0]["id"]
    monkeypatch.setenv("OUTPUTS", "whatsapp")
    monkeypatch.delenv("TARGET_GROUP_ID")
    outputs.update_job(str(tmp_path), jid2, {"remind_at": _iso(-1)})
    main.check_reminders()
    assert main.pending_whatsapp == []


def test_state_drains_only_when_ready(monkeypatch, tmp_path):
    main.pending_whatsapp[:] = [("t@g.us", "hi")]
    with TestClient(main.app) as c:
        assert c.post("/state", json={"status": "qr"}).json()["messages"] == []
        assert main.pending_whatsapp == [("t@g.us", "hi")]
        assert c.post("/state", json={"status": "ready"}).json()["messages"] == [{"to": "t@g.us", "text": "hi"}]
        assert c.post("/state", json={"status": "ready"}).json()["messages"] == []


def test_send_reminder_uses_helpers_and_one_failure_does_not_block(monkeypatch):
    monkeypatch.setenv("OUTPUTS", "telegram,email")
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SMTP_HOST", "EMAIL_FROM", "EMAIL_TO"):
        monkeypatch.setenv(k, "x")
    calls = []

    def boom(text):
        calls.append(("telegram", text))
        raise RuntimeError("down")
    monkeypatch.setattr(outputs, "_telegram_send", boom)
    monkeypatch.setattr(outputs, "_email_send", lambda subject, text: calls.append(("email", text)))
    outputs.send_reminder("⏰ hello")
    assert calls == [("telegram", "⏰ hello"), ("email", "⏰ hello")]
