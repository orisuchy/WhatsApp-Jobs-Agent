# Job Agent

Local, self-hosted WhatsApp job-posting matcher. A Node.js WhatsApp client
watches source groups, a Python FastAPI service asks an LLM (local Ollama by default)
whether each posting matches your CV, and matches are forwarded to a target
WhatsApp group.

## Prerequisites (you must supply these — they are not generated)

1. **Ollama running on the host machine** with the model pulled:
   ```bash
   ollama pull gemma4:e2b
   ```
2. **Your profile** — a CV and/or a job description, see [Your profile](#your-profile).
3. **Your WhatsApp groups** — see [Configure your groups](#configure-your-groups).

## Your profile

Put these in `data/profile/` (gitignored, mounted into the Python service; create it with
`mkdir -p data/profile`). You need at least one:

- `cv.pdf` — your CV (your background). Cleaned and anonymised by the LLM once, cached in
  `cv_clean_text.txt`; the cache refreshes automatically when `cv.pdf` changes.
- `job_description.md` (or `.txt`) — free text describing the job you want.

CV only, description only, or both (CV = background, description = what you're looking
for) all work. With neither, the service logs an error and every posting is a no-match.
Change the folder with `DATA_DIR`.

## Configure your groups

1. `cp .env.example .env`
2. List the groups you belong to (prints name + JID, then exits):
   ```bash
   LIST_GROUPS=1 docker compose run --rm node-whatsapp
   ```
   Or locally: `cd node_service && LIST_GROUPS=1 npm start`. Scan the QR code on first run.
3. Paste the JIDs into `.env`: `SOURCE_GROUP_IDS` (comma-separated), `TARGET_GROUP_ID`,
   and optionally `MATCH_THRESHOLD` (default 70) and `OUTPUTS`.

## Where matches go

Every match at or above `MATCH_THRESHOLD` is recorded once in `data/matches.jsonl`;
repeats of the same job (same first URL, or same text ignoring case/whitespace) across
groups are dropped. Pick destinations with `OUTPUTS` (comma-separated, default `whatsapp`;
unknown names are logged and ignored). One destination failing doesn't affect the others.

- `whatsapp` — forwards to `TARGET_GROUP_ID`, in `OUTPUT_LANGUAGE` (Hebrew or English labels).
- `file` — appends `matches.jsonl` and a readable `matches.md` to `OUTPUT_DIR`
  (default `data/outputs`).

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

## Choosing an LLM provider

Set in `.env`. `LLM_PROVIDER=ollama` (default) talks to Ollama natively;
`LLM_PROVIDER=openai` uses any OpenAI-compatible endpoint. Only Ollama is tested.

| Provider | `LLM_BASE_URL` | `LLM_MODEL` example |
|---|---|---|
| Ollama (default, tested) | `http://host.docker.internal:11434` (default) | `gemma4:e2b` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.0-flash` |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` |
| OpenRouter | `https://openrouter.ai/api/v1` | `meta-llama/llama-3.3-70b-instruct` |
| LM Studio | `http://host.docker.internal:1234/v1` | the loaded model |
| Anthropic | `https://api.anthropic.com/v1/` | a Claude model id |

Also: `LLM_API_KEY` (hosted providers), `LLM_NUM_CTX` (Ollama only, default 16384),
`LLM_TIMEOUT_S` (default 170). `OLLAMA_URL` / `OLLAMA_MODEL` still work as fallbacks.
A malformed model reply is retried once, then treated as no-match. Note: with a hosted
provider your CV text (anonymised by the cleaning step) and postings are sent to it.

## Running

```bash
docker compose up --build
```

On first run, `node-whatsapp` prints a QR code in the container logs — scan
it with WhatsApp's "Linked Devices" to authenticate. The session persists in
the `whatsapp_session` Docker volume, so you won't need to re-scan on
restart unless the volume is removed.

## Running the tests locally (outside Docker)

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
