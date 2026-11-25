import json
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import main

GOOD = {"odds": "80%", "reason": "התאמה טובה", "company": "Acme", "summary": "מפתח/ת Python"}


def _mock_llm(monkeypatch, content=None, error=None):
    client = MagicMock()
    if error:
        client.complete.side_effect = error
    else:
        client.complete.return_value = json.dumps(content or GOOD)
    monkeypatch.setattr(main, "llm_client", client)
    return client


def _analyze(c, text="Python developer wanted"):
    return c.post("/analyze", json={"text": text})


def test_analyze_returns_result_synchronously(monkeypatch):
    with TestClient(main.app) as c:
        monkeypatch.setattr(main, "CV_TEXT", "Python")
        _mock_llm(monkeypatch)
        body = _analyze(c).json()

    assert body == GOOD


def test_batch_endpoints_are_gone():
    with TestClient(main.app) as c:
        assert c.post("/analyze/flush").status_code == 404


def test_concurrent_requests_are_serialised_and_get_own_result(monkeypatch):
    active = {"now": 0, "max": 0}

    def complete(messages, **kwargs):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        time.sleep(0.05)
        active["now"] -= 1
        text = messages[1]["content"]
        company = "A" if "posting-A" in text else "B"
        return json.dumps({**GOOD, "company": company})

    client = MagicMock()
    client.complete.side_effect = complete
    with TestClient(main.app) as c:
        monkeypatch.setattr(main, "CV_TEXT", "Python")
        monkeypatch.setattr(main, "llm_client", client)
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda t: _analyze(c, t).json()["company"], ["posting-A", "posting-B"] * 2))

    assert results == ["A", "B", "A", "B"]
    assert active["max"] == 1


def test_failure_yields_zero_odds(monkeypatch):
    with TestClient(main.app) as c:
        monkeypatch.setattr(main, "CV_TEXT", "Python")
        _mock_llm(monkeypatch, error=RuntimeError("boom"))
        response = _analyze(c)

    assert response.status_code == 200
    assert response.json() == {"odds": 0}


def test_startup_missing_cv_sets_empty_cv_text(monkeypatch):
    monkeypatch.setattr(main, "CV_PATH", "/no/such/file.pdf")
    monkeypatch.setattr(main.os.path, "exists", lambda p: False)
    with TestClient(main.app):
        pass
    assert main.CV_TEXT == ""


def test_analyze_includes_fetched_job_description(monkeypatch):
    with TestClient(main.app) as c:
        monkeypatch.setattr(main, "CV_TEXT", "Python")
        monkeypatch.setattr(
            main, "fetch_url_text", lambda url, timeout=30: ("תיאור משרה מלא מהקישור", None)
        )
        client = _mock_llm(monkeypatch)
        _analyze(c, "דרוש מפתח Python https://example.com/jobs/123")

    sent = client.complete.call_args[0][0][1]["content"]
    assert "תיאור משרה מלא מהקישור" in sent
