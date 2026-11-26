"""Effective config = process env (.env) overridden by data/settings.json, which the web UI writes.

Overrides are applied onto os.environ so every module that reads env sees them without a restart.
"""
import json
import os
import re

from logger import setup_logger

logger = setup_logger("job-agent.settings")

KEYS = (
    "SOURCE_GROUP_IDS", "TARGET_GROUP_ID", "MATCH_THRESHOLD", "OUTPUT_LANGUAGE", "OUTPUTS",
    "LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY", "LLM_NUM_CTX",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO",
    "GOOGLE_SERVICE_ACCOUNT_FILE", "GOOGLE_SHEET_ID", "GOOGLE_SHEET_TAB", "GOOGLE_SHEETS",
    "SCORE_WEIGHT_ROLE", "SCORE_WEIGHT_SKILLS", "SCORE_WEIGHT_DOMAIN", "SCORE_WEIGHT_FIT", "HARD_REJECTS", "SCORING_NOTES",
    "HOME_LOCATION", "MAX_DISTANCE_KM", "AUTO_HIDE_DAYS",
)
WEIGHTS = ("SCORE_WEIGHT_ROLE", "SCORE_WEIGHT_SKILLS", "SCORE_WEIGHT_DOMAIN", "SCORE_WEIGHT_FIT")
REJECTS = ("role_family", "seniority", "mandatory")
SECRETS = ("LLM_API_KEY", "TELEGRAM_BOT_TOKEN", "SMTP_PASSWORD")
PROVIDERS = ("ollama", "openai")
OUTPUT_NAMES = ("whatsapp", "file", "telegram", "email", "sheets")

_orig: dict[str, str | None] = {}  # env value each overridden key had before the first override


def _path(data_dir: str) -> str:
    return os.path.join(data_dir, "settings.json")


def load(data_dir: str) -> dict[str, str]:
    try:
        with open(_path(data_dir), encoding="utf-8") as f:
            data = json.load(f)
        return {k: str(v) for k, v in data.items() if k in KEYS}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, AttributeError):
        logger.exception("Unreadable settings file moved aside to settings.json.corrupt")
        try:
            os.replace(_path(data_dir), _path(data_dir) + ".corrupt")  # keep it: a later save must not destroy it
        except OSError:
            pass
        return {}


def apply(data_dir: str) -> None:
    """Put the saved overrides into os.environ (restoring the .env value for keys no longer overridden)."""
    overrides = load(data_dir)
    for k in KEYS:
        if k in overrides:
            _orig.setdefault(k, os.environ.get(k))
            os.environ[k] = overrides[k]
        elif k in _orig:
            old = _orig.pop(k)
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


def validate(patch: dict) -> dict[str, str]:
    """Normalise a PUT body to {KEY: str}; raises ValueError with a user-facing message."""
    out = {}
    for k, v in patch.items():
        if k not in KEYS:
            raise ValueError(f"Unknown setting {k!r}")
        if isinstance(v, list):
            v = ",".join(str(x).strip() for x in v if str(x).strip())
        out[k] = "" if v is None else str(v).strip()
    if out.get("MATCH_THRESHOLD"):  # empty = fall back to the default
        try:
            ok = 0 <= int(out["MATCH_THRESHOLD"]) <= 100
        except ValueError:
            ok = False
        if not ok:
            raise ValueError("MATCH_THRESHOLD must be an integer 0-100")
    if "LLM_PROVIDER" in out:
        out["LLM_PROVIDER"] = out["LLM_PROVIDER"].lower()
        if out["LLM_PROVIDER"] not in PROVIDERS:
            raise ValueError(f"LLM_PROVIDER must be one of {', '.join(PROVIDERS)}")
    if "OUTPUTS" in out:
        out["OUTPUTS"] = out["OUTPUTS"].lower()
        names = [n.strip() for n in out["OUTPUTS"].split(",") if n.strip()]
        bad = [n for n in names if n not in OUTPUT_NAMES]
        if bad:
            raise ValueError(f"Unknown output(s): {', '.join(bad)}")
        out["OUTPUTS"] = ",".join(names)
    if out.get("LLM_NUM_CTX") and not (out["LLM_NUM_CTX"].isdigit() and int(out["LLM_NUM_CTX"]) >= 512):
        raise ValueError("LLM_NUM_CTX must be a number of tokens (512 or more)")
    for k in WEIGHTS:
        if out.get(k) and not (out[k].isdigit() and int(out[k]) <= 100):
            raise ValueError(f"{k} must be a whole number from 0 to 100")
    if out.get("HARD_REJECTS"):
        out["HARD_REJECTS"] = out["HARD_REJECTS"].lower()
        bad = [r for r in out["HARD_REJECTS"].split(",") if r.strip() and r.strip() not in (*REJECTS, "none")]
        if bad:
            raise ValueError(f"Unknown hard reject(s): {', '.join(bad)}")
    if out.get("MAX_DISTANCE_KM"):
        try:
            ok = float(out["MAX_DISTANCE_KM"]) > 0
        except ValueError:
            ok = False
        if not ok:
            raise ValueError("MAX_DISTANCE_KM must be a positive number of kilometres")
    if out.get("AUTO_HIDE_DAYS") and not (out["AUTO_HIDE_DAYS"].isdigit() and int(out["AUTO_HIDE_DAYS"]) > 0):
        raise ValueError("AUTO_HIDE_DAYS must be a whole number of days (1 or more)")
    if out.get("GOOGLE_SHEETS"):
        out["GOOGLE_SHEETS"] = _sheets(out["GOOGLE_SHEETS"])
    if "SMTP_PORT" in out and out["SMTP_PORT"] and not out["SMTP_PORT"].isdigit():
        raise ValueError("SMTP_PORT must be a number")
    return out


def save(data_dir: str, patch: dict) -> None:
    """Validate and merge into the settings file (empty secret = keep current). Callers then reload_settings()."""
    clean = validate(patch)
    current = load(data_dir)
    for k, v in clean.items():
        if k in SECRETS and not v:
            continue
        current[k] = v
    os.makedirs(data_dir, exist_ok=True)
    tmp = _path(data_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _path(data_dir))


def public() -> dict:
    """Effective settings for the UI: secrets are reported only as set/unset."""
    values = {k: os.environ.get(k, "") for k in KEYS if k not in SECRETS}
    return {"values": values, "secrets_set": {k: bool(os.environ.get(k)) for k in SECRETS}}


MAX_SHEETS = 20

def sheet_id(v: str) -> str:
    """The id from a pasted sheet link (…/d/<id>/edit…), or the value itself."""
    m = re.search(r"/d/([A-Za-z0-9_-]+)", v)
    return m.group(1) if m else v.strip()

def _sheets(raw: str) -> str:
    """GOOGLE_SHEETS: JSON list of {"id", "tab"}; links become ids, empty rows are dropped."""
    try:
        items = json.loads(raw)
        rows = [{"id": sheet_id(str(i.get("id") or "")), "tab": str(i.get("tab") or "").strip()} for i in items]
    except (ValueError, TypeError, AttributeError):
        raise ValueError("GOOGLE_SHEETS must be a list of sheets")
    rows = [r for r in rows if r["id"]]
    if len(rows) > MAX_SHEETS:
        raise ValueError(f"At most {MAX_SHEETS} sheets")
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":")) if rows else ""
