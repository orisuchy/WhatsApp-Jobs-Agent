"""Distance from home to a job: place names -> coordinates via OpenStreetMap Nominatim (cached on disk), then
straight-line (great-circle) distance. Only place names are sent, never the posting or the profile."""
import json
import math
import os
from threading import Lock

import requests

from logger import setup_logger

logger = setup_logger("job-agent.geo")

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "job-agent (self-hosted, open source)"  # Nominatim's policy asks for an identifying User-Agent
_lock = Lock()


def _cache_path(data_dir: str) -> str:
    return os.path.join(data_dir, "geocache.json")


def _load(data_dir: str) -> dict:
    try:
        with open(_cache_path(data_dir), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def geocode(place: str, data_dir: str) -> dict | None:
    """{'lat', 'lon', 'name'} for a place name, or None if unknown. Misses are cached too; network errors are not."""
    key = " ".join(place.lower().split())
    if not key or key == "unknown":
        return None
    with _lock:
        cache = _load(data_dir)
        if key in cache:
            return cache[key]
        try:
            r = requests.get(NOMINATIM, params={"q": place, "format": "json", "limit": 1},
                             headers={"User-Agent": USER_AGENT}, timeout=10)
            r.raise_for_status()
            hits = r.json()
        except Exception as e:
            logger.warning("Geocoding %r failed: %s", place, e)
            return None
        hit = {"lat": float(hits[0]["lat"]), "lon": float(hits[0]["lon"]), "name": hits[0].get("display_name", place)} if hits else None
        cache[key] = hit
        os.makedirs(data_dir, exist_ok=True)
        tmp = _cache_path(data_dir) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, _cache_path(data_dir))
        return hit


def km_between(a: dict, b: dict) -> float:
    """Great-circle distance in km (haversine)."""
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dp, dl = p2 - p1, math.radians(b["lon"] - a["lon"])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


TOO_FAR_CAP = 30  # same ceiling as the prompt's hard rejects


def apply_distance(result: dict, env, data_dir: str) -> dict:
    """Add distance_km to the result and cap the score when an on-site/hybrid job is beyond MAX_DISTANCE_KM.
    Remote jobs, unknown places and an unset home are never penalised."""
    home_name, max_km = (env.get("HOME_LOCATION") or "").strip(), (env.get("MAX_DISTANCE_KM") or "").strip()
    if not home_name or not result.get("location"):
        return result
    home, job = geocode(home_name, data_dir), geocode(result["location"], data_dir)
    if not home or not job:
        return result
    km = round(km_between(home, job))
    result = {**result, "distance_km": km}
    if max_km and result.get("work_mode") != "remote" and km > float(max_km) and result.get("odds", 0) > TOO_FAR_CAP:
        logger.info("Too far: %s is %d km from home (max %s km); score %s -> %d", result["location"], km, max_km, result["odds"], TOO_FAR_CAP)
        result["odds"] = TOO_FAR_CAP
    return result
