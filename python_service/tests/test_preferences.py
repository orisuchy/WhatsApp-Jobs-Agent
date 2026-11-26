"""Scoring preferences (weights, hard rejects, notes) and the home-distance limit."""
import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import geo
import main
import settings
from prompts import evaluation_prompt, scoring_prefs

PLACES = {"modiin": {"lat": 31.898, "lon": 35.010, "name": "Modiin"},
          "tel aviv": {"lat": 32.085, "lon": 34.782, "name": "Tel Aviv"},
          "haifa": {"lat": 32.794, "lon": 34.990, "name": "Haifa"}}


def test_default_prompt_keeps_original_weights_and_rejects():
    p = evaluation_prompt("cv", "English")
    assert "(30%)" in p and "(10%)" in p and "Hard rejects" in p and "candidate_preferences" not in p


def test_weights_are_normalised_and_zero_weights_dropped():
    p = evaluation_prompt("cv", "English", scoring_prefs({"SCORE_WEIGHT_ROLE": "50", "SCORE_WEIGHT_SKILLS": "50",
                                                          "SCORE_WEIGHT_DOMAIN": "0", "SCORE_WEIGHT_FIT": "0"}))
    assert p.count("(50%)") == 2 and "Domain experience" not in p and "Overall fit" not in p


def test_all_zero_weights_fall_back_to_defaults():
    assert scoring_prefs({k: "0" for k in settings.WEIGHTS})["weights"] == {"role": 30, "skills": 30, "domain": 30, "fit": 10}


def test_hard_rejects_can_be_chosen_or_turned_off():
    only = evaluation_prompt("cv", "English", scoring_prefs({"HARD_REJECTS": "seniority"}))
    assert "seniority gap" in only and "different role family than" not in only
    assert "Hard rejects" not in evaluation_prompt("cv", "English", scoring_prefs({"HARD_REJECTS": "none"}))


def test_notes_are_included_as_preferences():
    p = evaluation_prompt("jd", "English", scoring_prefs({"SCORING_NOTES": "No agencies"}))
    assert "<candidate_preferences>\nNo agencies\n</candidate_preferences>" in p


@pytest.mark.parametrize("patch", [{"SCORE_WEIGHT_FIT": "101"}, {"SCORE_WEIGHT_ROLE": "x"}, {"HARD_REJECTS": "salary"}, {"MAX_DISTANCE_KM": "-5"}])
def test_invalid_preference_settings_rejected(patch):
    with pytest.raises(ValueError):
        settings.validate(patch)


@pytest.fixture
def places(monkeypatch):
    monkeypatch.setattr(geo, "geocode", lambda place, data_dir: PLACES.get(place.lower()))


ENV = {"HOME_LOCATION": "Modiin", "MAX_DISTANCE_KM": "40"}


def test_onsite_job_too_far_is_capped(places):
    r = geo.apply_distance({"odds": 85, "location": "Haifa", "work_mode": "onsite"}, ENV, "")
    assert r["odds"] == geo.TOO_FAR_CAP and r["distance_km"] > 40


def test_close_job_keeps_score(places):
    r = geo.apply_distance({"odds": 85, "location": "Tel Aviv", "work_mode": "hybrid"}, ENV, "")
    assert r["odds"] == 85 and 20 < r["distance_km"] < 35


def test_remote_or_unknown_place_never_penalised(places):
    assert geo.apply_distance({"odds": 85, "location": "Haifa", "work_mode": "remote"}, ENV, "")["odds"] == 85
    assert geo.apply_distance({"odds": 85, "location": "Atlantis", "work_mode": "onsite"}, ENV, "") == {"odds": 85, "location": "Atlantis", "work_mode": "onsite"}
    assert "distance_km" not in geo.apply_distance({"odds": 85, "location": "Haifa"}, {}, "")


def test_geocode_caches_hits_and_misses(monkeypatch, tmp_path):
    import importlib
    real = importlib.reload(geo)  # undo the autouse stub for this module only
    calls = []

    def fake_get(url, params, headers, timeout):
        calls.append(params["q"])
        resp = MagicMock()
        resp.json.return_value = [{"lat": "31.9", "lon": "35.0", "display_name": "Modiin, Israel"}] if params["q"] == "Modiin" else []
        return resp
    monkeypatch.setattr(real.requests, "get", fake_get)
    assert real.geocode("Modiin", str(tmp_path))["name"] == "Modiin, Israel"
    assert real.geocode("  modiin ", str(tmp_path))["lat"] == 31.9
    assert real.geocode("Nowhere", str(tmp_path)) is None and real.geocode("Nowhere", str(tmp_path)) is None
    assert calls == ["Modiin", "Nowhere"]
    assert "USER_AGENT" not in json.dumps(json.load(open(tmp_path / "geocache.json")))


def test_analyze_applies_distance_and_stores_location(monkeypatch, places):
    monkeypatch.setitem(main.os.environ, "HOME_LOCATION", "Modiin")
    monkeypatch.setitem(main.os.environ, "MAX_DISTANCE_KM", "40")
    with TestClient(main.app) as c:
        monkeypatch.setattr(main, "CV_TEXT", "Python")
        client = MagicMock()
        client.complete.return_value = json.dumps({"odds": 90, "reason": "r", "company": "Acme", "summary": "s",
                                                   "location": "Tel Aviv", "work_mode": "Hybrid"})
        monkeypatch.setattr(main, "llm_client", client)
        body = c.post("/analyze", json={"text": "Python dev in Tel Aviv"}).json()
        assert body["forward"] is True and "📍 Tel Aviv · hybrid · " in body["whatsapp_text"]
        job = c.get("/api/jobs").json()[0]
        assert job["location"] == "Tel Aviv" and job["work_mode"] == "hybrid" and job["distance_km"] > 0

        client.complete.return_value = json.dumps({"odds": 90, "reason": "r", "company": "Far", "summary": "s",
                                                   "location": "Haifa", "work_mode": "onsite"})
        assert c.post("/analyze", json={"text": "Python dev in Haifa"}).json()["forward"] is False
