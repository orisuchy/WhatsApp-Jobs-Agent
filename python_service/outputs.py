"""Matched-jobs store, dedup, message formatting and output destinations."""
import csv
import hashlib
import io
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

def key_file() -> str:
    """GOOGLE_SERVICE_ACCOUNT_FILE without quotes; a path that doesn't exist here (e.g. a host path pasted
    into the UI) falls back to the same file name in DATA_DIR."""
    key = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE") or "").strip().strip("'\"")
    in_data = os.path.join(os.environ.get("DATA_DIR") or "", os.path.basename(key))
    return in_data if key and not os.path.isfile(key) and os.path.isfile(in_data) else key

def missing_config(name: str) -> list[str]:
    missing = [v for v in REQUIRED_ENV.get(name, ()) if not os.environ.get(v)]
    key = key_file()
    if name == "sheets" and key and not os.path.isfile(key):
        missing.append(f"GOOGLE_SERVICE_ACCOUNT_FILE (no such file: {key}; put the key in the data folder)")
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


STATE_FILE = "job_state.json"  # the user's own data per job (note, hidden), keyed by job id


def _state(data_dir: str) -> dict:
    try:
        with open(os.path.join(data_dir, STATE_FILE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write(path: str, text: str) -> None:
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(path + ".tmp", path)


def read_matches(data_dir: str) -> list[dict]:
    """Matched jobs, newest first, each with its id (= dedup key) and the user's note/hidden flag."""
    path = os.path.join(data_dir, "matches.jsonl")
    if not os.path.exists(path):
        return []
    with _lock, open(path, encoding="utf-8") as f:
        # ponytail: whole file per poll; paginate/sqlite if it grows large
        lines = [line for line in reversed(f.read().splitlines()) if line.strip()]
        state = _state(data_dir)
    jobs = []
    for line in lines:
        try:
            job = json.loads(line)
        except ValueError:
            logger.warning("Skipping corrupt line in matches.jsonl")
            continue
        key = job_key(job.get("text", ""))
        job = {**job, "id": key, **state.get(key, {})}
        if "hidden" not in job and not job.get("starred") and not job.get("status") and _older_than_auto_hide(job):
            job.update(hidden=True, auto_hidden=True)  # computed, not stored: changing the setting re-shows them
        jobs.append(job)
    return jobs


def _older_than_auto_hide(job: dict) -> bool:
    days = (os.environ.get("AUTO_HIDE_DAYS") or "").strip()
    try:
        when = datetime.fromisoformat(job.get("timestamp", ""))
        return days.isdigit() and int(days) > 0 and (datetime.now(timezone.utc) - when).days >= int(days)
    except (ValueError, TypeError):  # bad timestamp (missing, naive, non-string) or odd env value: just don't hide
        return False


CSV_HEADER = ["date", "score", "company", "summary", "reason", "group", "URL", "status", "starred", "hidden",
              "note", "reminder", "original text"]
STATUS_LABELS = {"interested": "Interested", "applied": "Applied", "interview": "Interview", "offer": "Offer",
                 "rejected": "Rejected"}


def _csv_safe(value):
    """A cell Excel/Sheets would run as a formula (posting text can start with '=') gets a leading quote."""
    return "'" + value if isinstance(value, str) and value[:1] in ("=", "+", "-", "@") else value


def matches_csv(data_dir: str) -> str:
    """All matched jobs with the user's fields, as CSV text with a UTF-8 BOM (Excel reads Hebrew then)."""
    # ponytail: whole store in memory per download; stream rows if the store grows large
    out = io.StringIO()
    out.write("\ufeff")  # BOM as an escape: editors can silently strip a literal one
    w = csv.writer(out)
    w.writerow(CSV_HEADER)
    for j in read_matches(data_dir):
        w.writerow([_csv_safe(v) for v in (
            j.get("timestamp", ""), j.get("score", ""), j.get("company", ""), j.get("summary", ""),
            j.get("reason", ""), j.get("group", ""), j.get("url") or "", STATUS_LABELS.get(j.get("status"), ""),
            "yes" if j.get("starred") else "", "yes" if j.get("hidden") else "", j.get("note", ""),
            j.get("remind_at", ""), j.get("text", ""))])
    return out.getvalue()


def update_job(data_dir: str, job_id: str, changes: dict) -> bool:
    """Set the user's fields on a job (note, hidden, starred, status). None/"" clears a field; hidden=False is
    kept, so an unhidden job can be told apart from one never touched. False if no such job."""
    if not any(j["id"] == job_id for j in read_matches(data_dir)):
        return False
    with _lock:
        state = _state(data_dir)
        entry = {k: v for k, v in {**state.get(job_id, {}), **changes}.items() if v or (k == "hidden" and v is False)}
        if entry:
            state[job_id] = entry
        else:
            state.pop(job_id, None)
        _write(os.path.join(data_dir, STATE_FILE), json.dumps(state, ensure_ascii=False))
    return True


def due_reminders(data_dir: str) -> list[dict]:
    """Jobs whose reminder time has come."""
    now = datetime.now(timezone.utc)
    due = []
    for job in read_matches(data_dir):
        try:
            if job.get("remind_at") and datetime.fromisoformat(job["remind_at"]) <= now:
                due.append(job)
        except (ValueError, TypeError):
            pass  # unparseable or naive time: never fires
    return due

def clear_reminder(data_dir: str, job_id: str, due: str) -> bool:
    """Clear remind_at only if it is still the value that was due; a reminder set meanwhile is kept."""
    with _lock:
        state = _state(data_dir)
        entry = state.get(job_id, {})
        if entry.get("remind_at") != due:
            return False
        entry.pop("remind_at")
        if entry:
            state[job_id] = entry
        else:
            state.pop(job_id)
        _write(os.path.join(data_dir, STATE_FILE), json.dumps(state, ensure_ascii=False))
    return True

def reminder_text(job: dict) -> str:
    lines = [f"⏰ Reminder: {job.get('company') or 'Unknown'} ({job.get('score')}%)", job.get("summary") or ""]
    if job.get("note"):
        lines.append(f"Your note: {job['note']}")
    if job.get("url"):
        lines.append(job["url"])
    return "\n".join(line for line in lines if line)

def send_reminder(text: str) -> None:
    """To the enabled Telegram/email destinations; one failing never blocks the other. WhatsApp is sent via Node."""
    for name in enabled_outputs():
        try:
            if name == "telegram":
                _telegram_send(text)
            elif name == "email":
                _email_send("Job Agent reminder", text)
        except Exception:
            logger.exception("Reminder to %r failed", name)

def remove_job(data_dir: str, job_id: str, allow_again: bool) -> bool:
    """Delete a job from the store. allow_again also forgets its dedup key, so a repost is analysed and
    can match again; otherwise reposts stay ignored. False if no such job."""
    path = os.path.join(data_dir, "matches.jsonl")
    with _lock:
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            return False

        def is_it(line):
            try:
                return job_key(json.loads(line).get("text", "")) == job_id
            except ValueError:
                return False
        keep = [line for line in lines if line.strip() and not is_it(line)]
        if len(keep) == len([line for line in lines if line.strip()]):
            return False
        _write(path, "".join(line + "\n" for line in keep))
        state = _state(data_dir)
        if state.pop(job_id, None) is not None:
            _write(os.path.join(data_dir, STATE_FILE), json.dumps(state, ensure_ascii=False))
        seen = os.path.join(data_dir, "seen_jobs.txt")
        if allow_again and os.path.exists(seen):
            with open(seen, encoding="utf-8") as f:
                keys = [k for k in f.read().split() if k != job_id]
            _write(seen, "".join(k + "\n" for k in keys))
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


def telegram_output(job: dict) -> None:
    _telegram_send(format_whatsapp(job, _language(), bold=""))  # no parse_mode: posting text may hold * or _

def _telegram_send(text: str) -> None:
    token, chat_id = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
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
    _email_send(f"New job match {job['score']}%: {job['company']}", format_whatsapp(job, _language(), bold=""))

def _email_send(subject: str, text: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = os.environ["EMAIL_TO"]
    msg.set_content(text)
    port = int(os.environ.get("SMTP_PORT") or 587)
    with smtplib.SMTP(os.environ["SMTP_HOST"], port, timeout=NETWORK_TIMEOUT_S) as smtp:
        if _smtp_starttls_on():
            smtp.starttls(context=ssl.create_default_context())
        if user := os.environ.get("SMTP_USER"):
            smtp.login(user, os.environ.get("SMTP_PASSWORD", ""))
        smtp.send_message(msg)

def _open_worksheet(sheet_id: str | None = None, tab: str | None = None):
    import gspread  # lazy: only needed when `sheets` is enabled

    sheet = gspread.service_account(filename=key_file()).open_by_key(sheet_id or os.environ["GOOGLE_SHEET_ID"])
    tab = os.environ.get("GOOGLE_SHEET_TAB") if tab is None else tab
    return sheet.worksheet(tab) if tab else sheet.sheet1


def check_service_account_key(data: bytes) -> str:
    """Returns the key's client_email; ValueError with a user-facing message if it isn't a service-account key."""
    try:
        key = json.loads(data)
    except ValueError:
        raise ValueError("That file is not JSON. Download the key as JSON from Google Cloud.")
    if not isinstance(key, dict) or key.get("type") != "service_account" or not key.get("client_email") or not key.get("private_key"):
        raise ValueError("That is not a Google service-account key (it needs type service_account, client_email and private_key).")
    return key["client_email"]


def sheet_key_email() -> str | None:
    try:
        with open(key_file(), "rb") as f:
            return check_service_account_key(f.read())
    except (OSError, ValueError):
        return None


def test_sheet(sheet_id: str, tab: str) -> str:
    """Opens the sheet read-only; returns the tab title or raises ValueError saying what to fix."""
    import gspread

    email = sheet_key_email()
    if not email:
        raise ValueError("Upload the service-account key file first.")
    if not sheet_id:
        raise ValueError("Enter the sheet id.")
    try:
        return _open_worksheet(sheet_id, tab).title
    except gspread.exceptions.SpreadsheetNotFound:
        raise ValueError(f"Sheet not found. Check the sheet id, and share the sheet with {email} as Editor.")
    except gspread.exceptions.WorksheetNotFound:
        raise ValueError(f"The sheet has no tab named {tab!r}. Fix the tab name or leave it empty.")
    except gspread.exceptions.APIError as e:
        msg = str(e)
        if "has not been used" in msg or "disabled" in msg:
            raise ValueError("The Google Sheets API is not enabled in your Google Cloud project. Enable it and try again.")
        if "PERMISSION_DENIED" in msg or "403" in msg:
            raise ValueError(f"No access. Share the sheet with {email} as Editor.")
        raise ValueError("Google said: " + msg[:300])
    except Exception as e:  # bad/expired key, no network, ...
        raise ValueError(f"Could not open the sheet: {e}"[:300])


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
