# Job Agent

Local, self-hosted WhatsApp job-posting matcher. A Node.js WhatsApp client
watches source groups, a Python FastAPI service asks a local Ollama model
whether each posting matches your CV, and matches are forwarded to a target
WhatsApp group.

## Prerequisites (you must supply these — they are not generated)

1. **Ollama running on the host machine** with the model pulled:
   ```bash
   ollama pull llama3.1
   ```
2. **Your CV as a PDF** at `python_service/my_cv.pdf`.
3. **Real WhatsApp group IDs** — edit `SOURCE_GROUP_IDS` and `TARGET_GROUP_ID`
   at the top of `node_service/whatsapp-agent.js`. Group JIDs look like
   `120363012345678901@g.us`; the easiest way to find them is to log the
   `message.from` value once the bot is running against your real groups.

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
