# Contributing

## Setup

```bash
git clone https://github.com/orisuchy/WhatsApp-Jobs-Agent.git job-agent && cd job-agent
cp .env.example .env
```

Python (3.11):
```bash
cd python_service
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

Node (20):
```bash
cd node_service
PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true npm install
npm test
```

## Pull requests

- Keep PRs small and focused; describe what and why.
- Add or update tests for behaviour changes. Both suites must pass (CI runs them).
- No personal data in commits: `.env`, `data/`, CVs, WhatsApp JIDs and phone numbers stay local.
- New config keys go in `.env.example` and the README.
