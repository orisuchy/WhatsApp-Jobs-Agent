"""Matched-jobs store, dedup, message formatting and output destinations."""
import hashlib
import json
import os
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from threading import Lock

import requests

from logger import setup_logger

logger = setup_logger("job-agent.outputs")

_lock = Lock()
URL_RE = re.compile(r"https?://\S+")

TELEGRAM_MAX_CHARS = 4096
NETWORK_TIMEOUT_S = 15
# Env vars each destination needs; missing ones disable that destination (warned at startup).
REQUIRED_ENV = {
    "telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
    "email": ("SMTP_HOST", "EMAIL_FROM", "EMAIL_TO"),
    "sheets": ("GOOGLE_SERVICE_ACCOUNT_FILE", "GOOGLE_SHEET_ID"),
}
SHEET_HEADER = ["date", "score", "company", "summary", "reason", "group", "URL", "original text"]

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


def requested_outputs() -> list[str]:
    names = [n.strip().lower() for n in (os.environ.get("OUTPUTS") or "whatsapp").split(",") if n.strip()]
    known = ["whatsapp", *DESTINATIONS]
    for n in names:
        if n not in known:
            logger.error("Unknown OUTPUTS entry %r ignored (known: %s)", n, ", ".join(known))
    return [n for n in names if n in known]

def missing_config(name: str) -> list[str]:
    missing = [v for v in REQUIRED_ENV.get(name, ()) if not os.environ.get(v)]
    key = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if name == "sheets" and key and not os.path.isfile(key):
        missing.append(f"GOOGLE_SERVICE_ACCOUNT_FILE (no such file: {key})")
    return missing

def enabled_outputs() -> list[str]:
    return [n for n in requested_outputs() if not missing_config(n)]

def warn_missing_config() -> None:
    """Startup: say which requested destinations are switched off and why."""
    for n in requested_outputs():
        if missing := missing_config(n):
            logger.warning("Output %r disabled, missing env: %s", n, ", ".join(missing))


def job_key(text: str) -> str:
    """Hash of the first URL, else of the whitespace/case-normalised text."""
    basis = first_url(text) or " ".join(text.lower().split())
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def format_whatsapp(job: dict, language: str, bold: str = "*") -> str:
    """Message text; `bold` is the emphasis marker (WhatsApp '*', plain text '')."""
    title, grp, summary, company, reason, original = LABELS["hebrew" if language.strip().lower() in ("hebrew", "עברית") else "english"]
    lines = [title.format(odds=job["score"]), ""]
    if job["group"]:
        lines.append(f"{bold}{grp}:{bold} {job['group']}")
    lines += [
        f"{bold}{summary}:{bold} {job['summary']}",
        f"{bold}{company}:{bold} {job['company']}",
        f"{bold}{reason}:{bold} {job['reason']}",
    ]
    where = [x for x in (job.get("location") if job.get("location") not in (None, "", "Unknown") else None,
                         job.get("work_mode") if job.get("work_mode") not in (None, "unknown") else None,
                         f"{job['distance_km']} km" if job.get("distance_km") is not None else None) if x]
    if where:
        lines.append("📍 " + " · ".join(where))
    lines += [
        "",
        f"--- {original} ---",
        job["text"],
    ]
    return "\n".join(lines)

def _language() -> str:
    return os.environ.get("OUTPUT_LANGUAGE") or "English"

def _smtp_starttls_on() -> bool:
    return os.environ.get("SMTP_STARTTLS", "1").strip().lower() not in ("0", "false", "no", "off")


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


def read_matches(data_dir: str) -> list[dict]:
    """Matched jobs, newest first."""
    path = os.path.join(data_dir, "matches.jsonl")
    if not os.path.exists(path):
        return []
    with _lock, open(path, encoding="utf-8") as f:
        # ponytail: whole file per poll; paginate/sqlite if it grows large
        lines = [line for line in reversed(f.read().splitlines()) if line.strip()]
    jobs = []
    for line in lines:
        try:
            jobs.append(json.loads(line))
        except ValueError:
            logger.warning("Skipping corrupt line in matches.jsonl")
    return jobs

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


def telegram_output(job: dict) -> None:
    token, chat_id = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    text = format_whatsapp(job, _language(), bold="")  # no parse_mode: posting text may hold * or _
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # ponytail: hard cut at the limit; split on newlines if long messages read badly
    for i in range(0, len(text), TELEGRAM_MAX_CHARS):
        try:
            r = requests.post(url, json={"chat_id": chat_id, "text": text[i:i + TELEGRAM_MAX_CHARS]}, timeout=NETWORK_TIMEOUT_S)
            r.raise_for_status()
        except requests.RequestException as e:  # re-raised without the URL, which holds the bot token
            status = getattr(e.response, "status_code", None) or "no response"
            raise RuntimeError(f"Telegram sendMessage failed ({status})") from None

def email_output(job: dict) -> None:
    msg = EmailMessage()
    msg["Subject"] = f"New job match {job['score']}%: {job['company']}"
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = os.environ["EMAIL_TO"]
    msg.set_content(format_whatsapp(job, _language(), bold=""))
    port = int(os.environ.get("SMTP_PORT") or 587)
    with smtplib.SMTP(os.environ["SMTP_HOST"], port, timeout=NETWORK_TIMEOUT_S) as smtp:
        if _smtp_starttls_on():
            smtp.starttls(context=ssl.create_default_context())
        if user := os.environ.get("SMTP_USER"):
            smtp.login(user, os.environ.get("SMTP_PASSWORD", ""))
        smtp.send_message(msg)

def _open_worksheet():
    import gspread  # lazy: only needed when `sheets` is enabled

    sheet = gspread.service_account(filename=os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"]).open_by_key(os.environ["GOOGLE_SHEET_ID"])
    tab = os.environ.get("GOOGLE_SHEET_TAB")
    return sheet.worksheet(tab) if tab else sheet.sheet1


def sheets_output(job: dict) -> None:
    ws = _open_worksheet()
    if not ws.row_values(1):
        ws.append_row(SHEET_HEADER, value_input_option="RAW")
    row = [job["timestamp"], job["score"], job["company"], job["summary"], job["reason"], job["group"], job["url"] or "", job["text"]]
    ws.append_row(row, value_input_option="RAW")  # RAW: posting text starting with "=" must not become a formula


DESTINATIONS = {"file": file_output, "telegram": telegram_output, "email": email_output, "sheets": sheets_output}


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
        "location": result.get("location", "Unknown"),
        "work_mode": result.get("work_mode", "unknown"),
        "distance_km": result.get("distance_km"),
    }
    if not record_if_new(data_dir, job):
        logger.info("Duplicate job dropped (%s)", job["url"] or job["text"][:60])
        return {**result, "forward": False}
    dispatch(job)
    return {**result, "forward": True, "whatsapp_text": format_whatsapp(job, language)}
