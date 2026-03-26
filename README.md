# SEC Alert Self-Hosted

Self-hosted SEC alert application with a localhost-first, BYOK posture.

## Current scope

This repository currently implements `Phase 1: Foundation`.

- FastAPI + Jinja2 app shell
- SQLite models and startup validation
- localhost-only runtime guard
- single-process lock to prevent duplicate schedulers
- shared in-memory SEC request broker with queue metrics
- watchlist CRUD
- Slack destination metadata and test-send skeleton

The product is **near-real-time**, not instant. SEC filings usually become visible within a few minutes, but SEC availability can lag longer under load. See the SEC [Webmaster FAQ](https://www.sec.gov/about/webmaster-frequently-asked-questions), [EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces), and [Accessing EDGAR Data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data).

## Security posture

- `APP_HOST` must stay `127.0.0.1` or `localhost`
- `SEC_USER_AGENT` is required
- secrets come from environment variables only
- secrets are not stored in the database
- telemetry is off by default
- CORS is off by default
- this project is single-user and single-process in v1

## Quick start

1. Create the virtual environment:

```powershell
uv venv .venv --python 3.12
```

2. Install dependencies:

```powershell
uv sync --python 3.12
```

3. Copy `.env.example` to `.env` and set `SEC_USER_AGENT`.

4. Run locally with one worker only:

```powershell
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

5. Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Development notes

- Keep `SCHEDULER_ENABLED=false` for reload/test workflows.
- The validated watchlist envelope is `25` issuers. `50` is the current hard cap.
- `100` issuers is not a current support claim.
- External API costs and infrastructure uptime remain the operator's responsibility.

## Docker

The included Docker files are a starting point for local self-hosting. For strict localhost-only access, host networking is preferred on Linux so the app can still bind `127.0.0.1`.

## Commands

```powershell
make test
make lint
make fmt
make up
make down
```

