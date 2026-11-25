import json
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

import main


def test_analyze_endpoint_match(monkeypatch):
    with TestClient(main.app) as test_client:
        monkeypatch.setattr(main, "CV_TEXT", "Python, FastAPI, 5 years experience")
        mock_client = MagicMock()
        mock_client.chat.return_value = {
            "message": {
                "content": json.dumps(
                    {
                        "is_match": True,
                        "reason": "התאמה טובה",
                        "summary": "מפתח/ת Python",
                    }
                )
            }
        }
        monkeypatch.setattr(main, "ollama_client", mock_client)

        response = test_client.post("/analyze", json={"text": "Python developer wanted"})

    assert response.status_code == 200
    assert response.json()["is_match"] is True


def test_analyze_endpoint_failure_returns_no_match(monkeypatch):
    with TestClient(main.app) as test_client:
        monkeypatch.setattr(main, "CV_TEXT", "Python experience")
        mock_client = MagicMock()
        mock_client.chat.side_effect = RuntimeError("boom")
        monkeypatch.setattr(main, "ollama_client", mock_client)

        response = test_client.post("/analyze", json={"text": "Python developer wanted"})

    assert response.status_code == 200
    assert response.json() == {"is_match": False}


def test_startup_missing_cv_sets_empty_cv_text(monkeypatch):
    monkeypatch.setattr(main, "CV_PATH", "/no/such/file.pdf")
    with TestClient(main.app):
        pass
    assert main.CV_TEXT == ""


def test_analyze_endpoint_fetches_linked_job_description(monkeypatch):
    with TestClient(main.app) as test_client:
        monkeypatch.setattr(main, "CV_TEXT", "Python experience")
        monkeypatch.setattr(
            main, "fetch_url_text", lambda url, timeout=10: "תיאור משרה מלא מהקישור"
        )
        mock_client = MagicMock()
        mock_client.chat.return_value = {
            "message": {
                "content": json.dumps(
                    {"is_match": True, "reason": "ok", "summary": "ok"}
                )
            }
        }
        monkeypatch.setattr(main, "ollama_client", mock_client)

        response = test_client.post(
            "/analyze",
            json={"text": "דרוש מפתח Python https://example.com/jobs/123"},
        )

    assert response.status_code == 200
    _, kwargs = mock_client.chat.call_args
    sent_content = kwargs["messages"][1]["content"]
    assert "תיאור משרה מלא מהקישור" in sent_content
