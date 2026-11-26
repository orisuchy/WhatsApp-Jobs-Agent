# Job Agent

Local, self-hosted WhatsApp job-posting matcher. It watches the WhatsApp groups you choose,
asks an LLM whether each posting fits your CV and the job you want, and sends the matches to
WhatsApp, a file, Telegram, email or Google Sheets. It runs on your machine; the LLM can be
local (Ollama) or a hosted API.

## How it works

```
WhatsApp source groups
        │  (whatsapp-web.js, node_service)
        ▼
  keyword pre-filter ──► POST /analyze ──► python_service (FastAPI)
                                              │ fetches linked posting, asks the LLM
                                              ▼
                                 score ≥ MATCH_THRESHOLD? ── dedup ──► outputs:
                                 WhatsApp · file · Telegram · email · Google Sheets
```

The web UI (`python_service`, port 8000) shows status and matches and edits settings.

## Quick start

Requires Docker with Compose, and [Ollama](https://ollama.com) on the host (or a hosted LLM,
see [LLM providers](#llm-providers)).

1. `ollama pull gemma4:e2b`
2. `cp .env.example .env`
3. Add your profile: `mkdir -p data/profile`, then see [Profile](#profile).
4. List your WhatsApp groups (name + JID; scan the QR code on first run):
   ```bash
   docker compose run --rm -e LIST_GROUPS=1 node-whatsapp
   ```
   Paste the JIDs into `.env`: `SOURCE_GROUP_IDS` (comma-separated), `TARGET_GROUP_ID`, and
   optionally `MATCH_THRESHOLD` (default 70) and `OUTPUTS`. You can also pick groups in the web UI.
5. `docker compose up --build`

On first run `node-whatsapp` prints a QR code in its logs (also shown in the web UI): scan it
via WhatsApp → Linked Devices. The session persists in the `whatsapp_session` Docker volume.

### Data paths

`./data` on the host is mounted at `/data` in the containers, and `.env` is read inside the
containers. So in `.env` use container paths (`/data/service-account.json`); on the host the
same file is `./data/service-account.json`. Only use `./data/...` in `.env` if you run the
Python service directly on the host.

## Profile

Put these in `data/profile/` (gitignored). You need at least one:

- `cv.pdf` — your CV (your background). Cleaned and anonymised by the LLM once, cached in
  `cv_clean_text.txt`; the cache refreshes automatically when `cv.pdf` changes.
- `job_description.md` (or `.txt`) — free text describing the job you want.

CV only, description only, or both all work. With neither, the service logs an error and every
posting is a no-match. Change the folder with `DATA_DIR`. Both can also be edited in the web UI.

## Outputs

Every match at or above `MATCH_THRESHOLD` is recorded once in `data/matches.jsonl`;
repeats of the same job (same first URL, or same text ignoring case/whitespace) across
groups are dropped. Pick destinations with `OUTPUTS` (comma-separated, default `whatsapp`;
unknown names are logged and ignored). One destination failing doesn't affect the others.

- `whatsapp` — forwards to `TARGET_GROUP_ID`, in `OUTPUT_LANGUAGE` (Hebrew or English labels).
- `file` — appends `matches.jsonl` and a readable `matches.md` to `OUTPUT_DIR`
  (default `data/outputs`).
- `telegram` — posts each match to a Telegram chat via the Bot API (see [Telegram](#telegram)).
- `email` — sends each match as an email over SMTP with STARTTLS (see [Email](#email)).
- `sheets` — appends one row per match to a Google Sheet (see [Google Sheets](#google-sheets)).

A destination whose settings are missing is logged as a warning at startup and skipped.

### Sync the file output to Google Drive

Install [Drive for Desktop](https://www.google.com/drive/download/), pick a folder inside
the synced Drive, and bind-mount it as `OUTPUT_DIR` in `docker-compose.yml`:

```yaml
  python-backend:
    environment:
      DATA_DIR: /data
      OUTPUT_DIR: /outputs
    volumes:
      - ./data:/data
      - "/Users/you/Google Drive/My Drive/job-matches:/outputs"
```

And set `OUTPUTS=whatsapp,file` in `.env`.

### Telegram

1. In Telegram, open **@BotFather**, send `/newbot`, and follow the prompts. Copy the token it
   gives you into `TELEGRAM_BOT_TOKEN`.
2. Create a group (or use a private chat) and add your bot to it. Send any message in it.
3. Find the chat id: open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and look
   for `"chat":{"id": ...}` — groups have negative ids, e.g. `-1001234567890`. Put it in
   `TELEGRAM_CHAT_ID`.
4. Set `OUTPUTS=whatsapp,telegram` (or just `telegram`) in `.env`.

Messages are plain text and split into 4096-character parts when longer.

### Email

Any SMTP server works. Gmail example:

1. Turn on 2-Step Verification on your Google account, then create an **app password**
   at https://myaccount.google.com/apppasswords.
2. In `.env`:
   ```
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USER=you@gmail.com
   SMTP_PASSWORD=<the 16-character app password>
   EMAIL_FROM=you@gmail.com
   EMAIL_TO=you@gmail.com
   ```
3. Set `OUTPUTS=whatsapp,email` in `.env`.

STARTTLS is on by default (`SMTP_STARTTLS=1`). Leave `SMTP_USER` empty for a relay that needs no login.

### Google Sheets

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project and enable
   the **Google Sheets API** (APIs & Services → Library).
2. IAM & Admin → Service Accounts → create one, then Keys → Add key → JSON. Save the download as
   `data/service-account.json` (`data/` is git-ignored; never commit it).
3. Open your Google Sheet and **Share** it (Editor) with the service account's email
   (`...@<project>.iam.gserviceaccount.com`).
4. In `.env` (use the container path, see [Data paths](#data-paths)):
   ```
   GOOGLE_SERVICE_ACCOUNT_FILE=/data/service-account.json
   GOOGLE_SHEET_ID=<the id from the sheet URL: /d/<id>/edit>
   GOOGLE_SHEET_TAB=Matches   # optional, default: first tab
   ```
5. Set `OUTPUTS=whatsapp,sheets` in `.env`.

A header row (date, score, company, summary, reason, group, URL, original text) is written if
the sheet is empty.

## LLM providers

Set in `.env`. `LLM_PROVIDER=ollama` (default) talks to Ollama natively;
`LLM_PROVIDER=openai` uses any OpenAI-compatible endpoint. **Tested:** Ollama. **Should work** (OpenAI-compatible, untested): everything else in the table.

| Provider | `LLM_BASE_URL` | `LLM_MODEL` example |
|---|---|---|
| Ollama (default, tested) | `http://host.docker.internal:11434` (default) | `gemma4:e2b` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.0-flash` |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` |
| OpenRouter | `https://openrouter.ai/api/v1` | `meta-llama/llama-3.3-70b-instruct` |
| LM Studio | `http://host.docker.internal:1234/v1` | the loaded model |
| Anthropic | `https://api.anthropic.com/v1/` | a Claude model id |

### Scoring preferences and location

The default scoring weighs role & seniority, hard skills and domain experience 30% each and overall fit 10%, and automatically rejects (score ≤ 30) postings for a different role family, a seniority gap of 2+ levels, or a missing mandatory requirement. Change any of it in the web UI (Settings → Scoring preferences) or with the `SCORE_WEIGHT_*`, `HARD_REJECTS` and `SCORING_NOTES` keys in `.env`. `SCORING_NOTES` takes plain-language preferences ("no recruitment agencies", "prefer startups").

Set `HOME_LOCATION` and `MAX_DISTANCE_KM` to reject on-site and hybrid jobs that are too far (straight-line distance). The model extracts each job's location and work mode; the distance is computed in code. Remote jobs and jobs with no clear location are never rejected for distance. Place names — your home and the job's city, never the posting or your CV — are looked up on [OpenStreetMap Nominatim](https://nominatim.org/) and cached in `data/geocache.json`.

**Context-window check:** the web UI (and a startup log line) estimates how many tokens one analysis needs (prompts + your CV/job description + room for the posting and reply) and warns when that doesn't fit the model's context window, with the fix (for Ollama usually: raise `LLM_NUM_CTX`). The window is read from Ollama, or from the provider's `/models` list when it reports one; otherwise the check is skipped.

Also: `LLM_API_KEY` (hosted providers), `LLM_NUM_CTX` (Ollama only, default 16384),
`LLM_TIMEOUT_S` (default 170), `LLM_THINK` (Ollama only, default false: hidden "thinking" is disabled so thinking models answer fast; set `true` to re-enable). `OLLAMA_URL` / `OLLAMA_MODEL` still work as fallbacks.
A malformed model reply is retried once, then treated as no-match. Note: with a hosted
provider your CV and postings are sent to it (the raw CV text goes once for the cleaning/anonymising step).

## Web UI (local-only)

Open <http://localhost:8000> for WhatsApp status, the QR code to scan (when not connected),
and your matched jobs, newest first. The UI has no authentication, so compose publishes the
port on `127.0.0.1` only: it is **local-only**; don't expose it to a network. The QR code is
rendered in your browser with `qrcodejs` loaded from cdnjs.

### Using the web UI

The same page edits your configuration, no container rebuild or restart needed:

- **Settings:** pick source groups (listed once WhatsApp is connected) and the target group, set the
  threshold (0–100), output language, LLM provider/model/base URL/key, outputs and their settings.
  Saved values go to `data/settings.json` (gitignored, owner-readable) and override `.env`;
  to go back to a `.env` value, edit or delete that file. Node re-reads the group settings every ~30 s.
- **Secrets** (LLM API key, Telegram bot token, SMTP password) are never sent back to the browser;
  the form only shows whether each is set. Leave a secret field blank to keep the current value.
- **Profile:** upload your CV PDF and/or edit the job description; the profile reloads immediately
  (a changed CV is re-cleaned by the LLM, which can take a moment).
- The Google service-account key is still a file in `data/`; the form only takes its path.

## Running the tests

Python:
```bash
cd python_service
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

Node (skip the Chromium download — tests don't need a real browser):
```bash
cd node_service
PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true npm install
npm test
```

## Privacy

Everything stays on your machine except what you send out:

- **LLM:** with Ollama (local) nothing leaves your machine. With a hosted provider (OpenAI,
  Gemini, Groq, OpenRouter, Anthropic) every posting, any page fetched from a link in it, your job
  description, and your CV (raw text once for cleaning/anonymising, then the anonymised version) are sent to
  whichever provider you configure. Messages without job keywords never reach the LLM.
- **Outputs:** matches (posting text, score, group name) go to whichever of Telegram, SMTP
  server, Google Sheets, or your WhatsApp target group you enable.
- **Web UI:** no authentication, bound to `127.0.0.1`; loads `qrcodejs` from cdnjs in your browser.
- Secrets live in `.env` / `data/settings.json` (both gitignored) and are never sent back to the browser.

## Troubleshooting

- **No QR code / can't link:** watch `docker compose logs -f node-whatsapp`, or open the web UI.
  If the session is stale, `docker compose down -v` removes the `whatsapp_session` volume (you re-scan).
- **WhatsApp disconnected / auth failed:** the WhatsApp container restarts itself after a disconnect, and the web UI has a **Reconnect WhatsApp** button. Both rely on Docker's `restart: unless-stopped`; without Docker, restart `node whatsapp-agent.js` yourself.
- **Chromium "profile in use" / SingletonLock error:** the container deletes stale locks on start, so
  `docker compose restart node-whatsapp` is usually enough; if it persists, remove the `whatsapp_session` volume.
- **"Failed to initialize client: Cannot read properties of null (reading 'Socket')" (or similar):** a
  WhatsApp Web update broke whatsapp-web.js. (whatsapp-web.js is installed from upstream commit `38eb45b2`, which fixes the
  November 2026 break; switch back to the npm release once one includes it.) Rebuild to pick up a newer library (`docker compose up --build`);
  if that doesn't help, pin a known-good build with `WWEBJS_WEB_VERSION=<version>` in `.env` (pick one from
  [wa-version](https://github.com/wppconnect-team/wa-version/tree/main/html), file name without `.html`) and
  `docker compose up -d node-whatsapp`.
- **Ollama unreachable:** make sure `ollama serve` is running on the host and the model is pulled.
  Inside Docker the URL is `http://host.docker.internal:11434`; on Linux Ollama must listen on
  an address the container can reach (`OLLAMA_HOST=0.0.0.0`).
- **Nothing is analysed:** a keyword pre-filter in `node_service/whatsapp-agent.js` (`JOB_KEYWORDS`, Hebrew and English hiring phrases) drops messages that look unrelated to jobs.
- **Slow local model / "LLM timed out" in the logs:** raise `LLM_TIMEOUT_S` *and* `ANALYZE_TIMEOUT_MS`
  together (the Node value, in ms, must be the larger one), or use a smaller model.
- **Every posting is a no-match:** check the python-backend logs for a missing profile or LLM errors.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Licensed under the [MIT License](LICENSE).
