"""Matched-jobs store, dedup, message formatting and output destinations."""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from threading import Lock

from logger import setup_logger

logger = setup_logger("job-agent.outputs")

_lock = Lock()
URL_RE = re.compile(r"https?://\S+")

LABELS = {
    "hebrew": ("משרה חדשה! {odds}% התאמה:", "קבוצה", "סיכום", "חברה", "סיבת התאמה", "המודעה המקורית"),
    "english": ("New job! {odds}% match:", "Group", "Summary", "Company", "Why it matches", "Original posting"),
}


def first_url(text: str):
    m = URL_RE.search(text)
    return m.group(0).rstrip(".,;:!?)]}>\"'") if m else None


def match_threshold() -> int:
    try:
        return int(os.environ.get("MATCH_THRESHOLD", "70"))
    except ValueError:
        return 70


def enabled_outputs() -> list[str]:
    names = [n.strip().lower() for n in (os.environ.get("OUTPUTS") or "whatsapp").split(",") if n.strip()]
    known = ["whatsapp", *DESTINATIONS]
    for n in names:
        if n not in known:
            logger.error("Unknown OUTPUTS entry %r ignored (known: %s)", n, ", ".join(known))
    return [n for n in names if n in known]


def job_key(text: str) -> str:
    """Hash of the first URL, else of the whitespace/case-normalised text."""
    basis = first_url(text) or " ".join(text.lower().split())
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def format_whatsapp(job: dict, language: str) -> str:
    title, grp, summary, company, reason, original = LABELS["hebrew" if language.strip().lower() in ("hebrew", "עברית") else "english"]
    lines = [title.format(odds=job["score"]), ""]
    if job["group"]:
        lines.append(f"*{grp}:* {job['group']}")
    lines += [
        f"*{summary}:* {job['summary']}",
        f"*{company}:* {job['company']}",
        f"*{reason}:* {job['reason']}",
        "",
        f"--- {original} ---",
        job["text"],
    ]
    return "\n".join(lines)


def record_if_new(data_dir: str, job: dict) -> bool:
    """Atomically dedup + append to the matched-jobs store. False if already seen."""
    key = job_key(job["text"])
    seen_path = os.path.join(data_dir, "seen_jobs.txt")
    with _lock:
        os.makedirs(data_dir, exist_ok=True)
        # ponytail: whole seen file read per match; use sqlite if it ever gets large
        if os.path.exists(seen_path):
            with open(seen_path, encoding="utf-8") as f:
                if key in f.read().split():
                    return False
        with open(os.path.join(data_dir, "matches.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(job, ensure_ascii=False) + "\n")
        with open(seen_path, "a", encoding="utf-8") as f:
            f.write(key + "\n")
    return True


def file_output(job: dict) -> None:
    out = os.environ.get("OUTPUT_DIR") or os.path.join(os.environ.get("DATA_DIR") or "data", "outputs")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "matches.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(job, ensure_ascii=False) + "\n")
    md = (
        f"## {job['company']} — {job['score']}%\n\n"
        f"- **When:** {job['timestamp']}\n- **Group:** {job['group'] or '-'}\n"
        f"- **Summary:** {job['summary']}\n- **Why:** {job['reason']}\n"
        f"- **URL:** {job['url'] or '-'}\n\n> " + job["text"].replace("\n", "\n> ") + "\n\n---\n\n"
    )
    with open(os.path.join(out, "matches.md"), "a", encoding="utf-8") as f:
        f.write(md)


DESTINATIONS = {"file": file_output}  # later tickets add destinations here


def dispatch(job: dict) -> None:
    """Call every enabled non-WhatsApp destination; one failing never affects the others."""
    for name in enabled_outputs():
        fn = DESTINATIONS.get(name)
        if not fn:
            continue
        try:
            fn(job)
        except Exception:
            logger.exception("Output %r failed", name)


def process_match(result: dict, text: str, group: str, data_dir: str, language: str) -> dict:
    """Decide match vs threshold, dedup, store, dispatch. Returns the /analyze response."""
    odds = result.get("odds", 0)
    if odds < match_threshold():
        return {**result, "forward": False}
    job = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "group": group,
        "score": odds,
        "company": result.get("company", "Unknown"),
        "summary": result.get("summary", ""),
        "reason": result.get("reason", ""),
        "text": text,
        "url": first_url(text),
    }
    if not record_if_new(data_dir, job):
        logger.info("Duplicate job dropped (%s)", job["url"] or job["text"][:60])
        return {**result, "forward": False}
    dispatch(job)
    return {**result, "forward": True, "whatsapp_text": format_whatsapp(job, language)}
