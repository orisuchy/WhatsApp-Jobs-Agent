"""Telegram and email destinations, with fake transports (no network)."""
import json
import logging
from unittest.mock import MagicMock

import pytest
import requests
from fastapi.testclient import TestClient

import main
import outputs

TOKEN = "123456:SECRET-TOKEN"
GOOD = {"odds": 80, "reason": "good fit", "company": "Acme", "summary": "Python dev"}


def _job(text="Python dev at Acme https://x.io/j/1", score=80):
    return {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "group": "G1",
        "score": score,
        "company": "Acme",
        "summary": "Python dev",
        "reason": "good fit",
        "text": text,
        "url": outputs.first_url(text),
    }


@pytest.fixture
def telegram_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-10042")


@pytest.fixture
def posts(monkeypatch):
    """Fake requests.post; records calls. Set `posts.fail` to an exception to raise it."""
    calls = []

    class Recorder:
        fail = None

        def __call__(self, url, json=None, timeout=None):
            calls.append({"url": url, "json": json, "timeout": timeout})
            if self.fail:
                raise self.fail
            return MagicMock(status_code=200)

    rec = Recorder()
    monkeypatch.setattr(outputs.requests, "post", rec)
    rec.calls = calls
    return rec


@pytest.fixture
def smtp(monkeypatch):
    """Fake smtplib.SMTP; each instance records what was called on it."""
    sessions = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.args = (host, port, timeout)
            self.calls = []
            sessions.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context=None):
            self.calls.append(("starttls", context is not None))

        def login(self, user, password):
            self.calls.append(("login", user, password))

        def send_message(self, msg):
            self.calls.append(("send", msg))

    monkeypatch.setattr(outputs.smtplib, "SMTP", FakeSMTP)
    return sessions


@pytest.fixture
def email_env(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "me@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-pass")
    monkeypatch.setenv("EMAIL_FROM", "me@example.com")
    monkeypatch.setenv("EMAIL_TO", "you@example.com")


# --- telegram ----------------------------------------------------------------


def test_telegram_posts_plain_text_to_bot_api(telegram_env, posts):
    outputs.telegram_output(_job())

    (call,) = posts.calls
    assert call["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert call["json"]["chat_id"] == "-10042"
    assert "Acme" in call["json"]["text"] and "https://x.io/j/1" in call["json"]["text"]
    assert "parse_mode" not in call["json"]  # posting text may contain * or _
    assert "*" not in call["json"]["text"]
    assert call["timeout"]


def test_telegram_splits_message_over_4096_chars(telegram_env, posts):
    outputs.telegram_output(_job(text="x" * 10000))

    assert len(posts.calls) == 3
    assert all(len(c["json"]["text"]) <= 4096 for c in posts.calls)
    assert "x" * 10000 in "".join(c["json"]["text"] for c in posts.calls)


def test_telegram_error_raises_without_leaking_token(telegram_env, posts):
    posts.fail = requests.ConnectionError(f"failed to reach https://api.telegram.org/bot{TOKEN}/sendMessage")

    with pytest.raises(RuntimeError) as err:
        outputs.telegram_output(_job())
    assert TOKEN not in str(err.value)
    assert err.value.__suppress_context__  # no chained traceback carrying the URL


def test_telegram_language_follows_output_language(telegram_env, posts, monkeypatch):
    monkeypatch.setenv("OUTPUT_LANGUAGE", "Hebrew")
    outputs.telegram_output(_job())
    assert "התאמה" in posts.calls[0]["json"]["text"]


# --- email -------------------------------------------------------------------


def test_email_sends_with_starttls_and_login(email_env, smtp):
    outputs.email_output(_job())

    (session,) = smtp
    assert session.args == ("smtp.example.com", 587, outputs.NETWORK_TIMEOUT_S)
    assert ("starttls", True) in session.calls
    assert ("login", "me@example.com", "app-pass") in session.calls
    (msg,) = [c[1] for c in session.calls if c[0] == "send"]
    assert msg["From"] == "me@example.com" and msg["To"] == "you@example.com"
    assert "80%" in msg["Subject"] and "Acme" in msg["Subject"]
    body = msg.get_content()
    assert "Acme" in body and "https://x.io/j/1" in body


def test_email_body_is_utf8_hebrew(email_env, smtp, monkeypatch):
    monkeypatch.setenv("OUTPUT_LANGUAGE", "Hebrew")
    outputs.email_output(_job(text="דרוש מפתח Python"))
    (msg,) = [c[1] for c in smtp[0].calls if c[0] == "send"]
    assert "דרוש מפתח Python" in msg.get_content()


def test_email_starttls_can_be_turned_off(email_env, smtp, monkeypatch):
    monkeypatch.setenv("SMTP_STARTTLS", "0")
    outputs.email_output(_job())
    assert not any(c[0] == "starttls" for c in smtp[0].calls)


def test_email_skips_login_without_user(email_env, smtp, monkeypatch):
    monkeypatch.delenv("SMTP_USER")
    outputs.email_output(_job())
    assert not any(c[0] == "login" for c in smtp[0].calls)


# --- config, startup warning, dispatch ----------------------------------------


def test_missing_config_is_skipped_and_warned_at_startup(monkeypatch, posts, caplog):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    monkeypatch.setenv("OUTPUTS", "whatsapp,telegram")

    with caplog.at_level(logging.WARNING, logger="job-agent.outputs"):
        outputs.warn_missing_config()
    assert "telegram" in caplog.text and "TELEGRAM_BOT_TOKEN" in caplog.text

    outputs.dispatch(_job())
    assert posts.calls == []


def test_startup_hook_warns_about_missing_config(monkeypatch, caplog):
    monkeypatch.setenv("OUTPUTS", "email")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    with caplog.at_level(logging.WARNING, logger="job-agent.outputs"), TestClient(main.app):
        pass
    assert "SMTP_HOST" in caplog.text


def test_failing_telegram_does_not_stop_email_through_analyze(
    tmp_path, monkeypatch, telegram_env, email_env, posts, smtp
):
    monkeypatch.setenv("OUTPUTS", "whatsapp,telegram,email")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
    posts.fail = requests.ConnectionError("telegram down")
    client = MagicMock()
    client.complete.return_value = json.dumps(GOOD)
    monkeypatch.setattr(main, "llm_client", client)

    with TestClient(main.app) as c:  # startup (lifespan) reloads the profile, so patch after it
        monkeypatch.setattr(main, "CV_TEXT", "Python")
        body = c.post("/analyze", json={"text": "Python dev at Acme", "group": "G1"}).json()

    assert body["forward"] is True and "whatsapp_text" in body
    assert any(call[0] == "send" for call in smtp[0].calls)


def test_failing_email_does_not_stop_telegram_through_analyze(
    tmp_path, monkeypatch, telegram_env, email_env, posts, smtp
):
    monkeypatch.setenv("OUTPUTS", "whatsapp,telegram,email")
    monkeypatch.setattr(outputs.smtplib, "SMTP", MagicMock(side_effect=OSError("smtp down")))
    client = MagicMock()
    client.complete.return_value = json.dumps(GOOD)
    monkeypatch.setattr(main, "llm_client", client)

    with TestClient(main.app) as c:  # startup (lifespan) reloads the profile, so patch after it
        monkeypatch.setattr(main, "CV_TEXT", "Python")
        body = c.post("/analyze", json={"text": "Python dev at Acme", "group": "G1"}).json()

    assert body["forward"] is True
    assert len(posts.calls) == 1
