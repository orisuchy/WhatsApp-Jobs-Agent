import json
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import main

GOOD = {"odds": 80, "reason": "התאמה טובה", "company": "Acme", "summary": "מפתח/ת Python"}


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


# --- candidate profile -------------------------------------------------------

FIXTURE_CV = os.path.join(os.path.dirname(__file__), "fixtures", "sample_cv.pdf")


def _profile(tmp_path, cv=False, jd=None, jd_name="job_description.md"):
    d = tmp_path / "profile"
    d.mkdir(exist_ok=True)
    if cv:
        shutil.copy(FIXTURE_CV, d / "cv.pdf")
    if jd is not None:
        (d / jd_name).write_text(jd, encoding="utf-8")
    return d


def _sent(client):
    return client.complete.call_args[0][0][1]["content"]


def test_missing_profile_returns_no_match_without_calling_llm(tmp_path, monkeypatch):
    client = _mock_llm(monkeypatch)
    with TestClient(main.app) as c:
        response = _analyze(c)
    assert response.json() == {"odds": 0}
    client.complete.assert_not_called()


def test_cv_only_mode(tmp_path, monkeypatch):
    _profile(tmp_path, cv=True)
    client = _mock_llm(monkeypatch)
    monkeypatch.setattr(main, "clean_cv", lambda raw, c: "CLEANED CV")
    with TestClient(main.app) as c:
        assert _analyze(c).json() == GOOD
    sent = _sent(client)
    assert "<candidate_cv>" in sent and "CLEANED CV" in sent
    assert "<desired_job_description>" not in sent
    assert "Profile mode: cv" in sent


def test_jd_only_mode_txt(tmp_path, monkeypatch):
    _profile(tmp_path, jd="Senior Rust backend", jd_name="job_description.txt")
    client = _mock_llm(monkeypatch)
    with TestClient(main.app) as c:
        assert _analyze(c).json() == GOOD
    sent = _sent(client)
    assert "<desired_job_description>" in sent and "Senior Rust backend" in sent
    assert "<candidate_cv>" not in sent
    assert "Profile mode: jd" in sent


def test_both_mode(tmp_path, monkeypatch):
    _profile(tmp_path, cv=True, jd="Remote only")
    client = _mock_llm(monkeypatch)
    monkeypatch.setattr(main, "clean_cv", lambda raw, c: "CLEANED CV")
    with TestClient(main.app) as c:
        _analyze(c)
    sent = _sent(client)
    assert "CLEANED CV" in sent and "Remote only" in sent
    assert "Profile mode: both" in sent


def test_cv_cache_reused_and_invalidated_on_change(tmp_path, monkeypatch):
    d = _profile(tmp_path, cv=True)
    calls = []
    monkeypatch.setattr(main, "clean_cv", lambda raw, c: calls.append(raw) or f"clean{len(calls)}")

    assert main.load_cv() == "clean1"
    assert main.load_cv() == "clean1"  # cache hit
    assert len(calls) == 1

    # a different CV file -> re-clean
    (d / "cv.pdf").write_bytes((d / "cv.pdf").read_bytes() + b"\n%changed")
    assert main.load_cv() == "clean2"
    assert len(calls) == 2


def test_failed_cleaning_falls_back_to_raw_and_is_not_cached(tmp_path, monkeypatch):
    d = _profile(tmp_path, cv=True)

    def boom(raw, c):
        raise RuntimeError("llm down")

    monkeypatch.setattr(main, "clean_cv", boom)
    assert "Python" in main.load_cv()
    assert not (d / "cv_clean_text.txt").exists()
