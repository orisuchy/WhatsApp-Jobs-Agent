import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import main

GOOD = {"odds": "80%", "reason": "התאמה טובה", "company": "Acme", "summary": "מפתח/ת Python"}


@pytest.fixture(autouse=True)
def _empty_queue():
    main.job_queue.clear()
    yield
    main.job_queue.clear()


def _mock_ollama(monkeypatch, content=None, error=None):
    client = MagicMock()
    if error:
        client.chat.side_effect = error
    else:
        client.chat.return_value = {"message": {"content": json.dumps(content or GOOD)}}
    monkeypatch.setattr(main, "ollama_client", client)
    return client


def test_analyze_queues_until_batch_is_full(monkeypatch):
    with TestClient(main.app) as c:
        monkeypatch.setattr(main, "CV_TEXT", "Python")
        client = _mock_ollama(monkeypatch)
        for i in range(1, main.BATCH_SIZE):
            body = c.post("/analyze", json={"text": f"job {i}"}).json()
            assert body == {"queued": True, "queue_size": i, "batch_size": main.BATCH_SIZE}
        response = c.post("/analyze", json={"text": "last"})

    body = response.json()
    assert body["count"] == main.BATCH_SIZE
    assert body["batch_results"][0]["odds"] == "80%"
    assert client.chat.call_count == main.BATCH_SIZE


def test_flush_processes_queued_posting(monkeypatch):
    with TestClient(main.app) as c:
        monkeypatch.setattr(main, "CV_TEXT", "Python")
        _mock_ollama(monkeypatch)
        c.post("/analyze", json={"text": "Python developer wanted"})
        response = c.post("/analyze/flush")

    body = response.json()
    assert body["count"] == 1
    assert body["batch_results"][0]["odds"] == "80%"
    assert body["batch_results"][0]["reason"] == "התאמה טובה"
    assert body["batch_results"][0]["company"] == "Acme"
    assert body["batch_results"][0]["summary"] == "מפתח/ת Python"


def test_flush_empty_queue():
    with TestClient(main.app) as c:
        assert c.post("/analyze/flush").json() == {"processed": 0, "message": "queue was empty"}


def test_failure_yields_zero_odds(monkeypatch):
    with TestClient(main.app) as c:
        monkeypatch.setattr(main, "CV_TEXT", "Python")
        _mock_ollama(monkeypatch, error=RuntimeError("boom"))
        c.post("/analyze", json={"text": "Python developer wanted"})
        response = c.post("/analyze/flush")

    assert response.status_code == 200
    assert response.json()["batch_results"] == [{"odds": 0}]


def test_startup_missing_cv_sets_empty_cv_text(monkeypatch):
    monkeypatch.setattr(main, "CV_PATH", "/no/such/file.pdf")
    monkeypatch.setattr(main.os.path, "exists", lambda p: False)
    with TestClient(main.app):
        pass
    assert main.CV_TEXT == ""


def test_flush_includes_fetched_job_description(monkeypatch):
    with TestClient(main.app) as c:
        monkeypatch.setattr(main, "CV_TEXT", "Python")
        monkeypatch.setattr(
            main, "fetch_url_text", lambda url, timeout=30: ("תיאור משרה מלא מהקישור", None)
        )
        client = _mock_ollama(monkeypatch)
        c.post("/analyze", json={"text": "דרוש מפתח Python https://example.com/jobs/123"})
        c.post("/analyze/flush")

    sent = client.chat.call_args[1]["messages"][1]["content"]
    assert "תיאור משרה מלא מהקישור" in sent
