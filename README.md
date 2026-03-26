# SEC Alert Self-Hosted

Self-hosted SEC alert application with a localhost-first, BYOK posture.

## Current scope

This repository currently implements `Phase 6: optional OpenAI rewrite on top of deterministic SEC signals`.

- FastAPI + Jinja2 app shell
- SQLite models and startup validation
- localhost-only runtime guard
- single-process lock to prevent duplicate schedulers
- shared in-memory SEC request broker with queue metrics
- watchlist CRUD
- `company_tickers.json` resolver with manual CIK override precedence
- manual broker-backed `Run 8-K Ingest Now` path for `8-K` / `8-K/A`
- manual broker-backed `Run Form 4 Ingest Now` path for `4` / `4/A`
- scheduler-enqueued live 8-K polling with overlap recheck
- scheduler-enqueued live Form 4 ownership discovery with overlap recheck
- rolling repair over the last 2 business days using EDGAR daily master index files
- automatic and manual 30-day backfill for newly enabled watchlist entries
- deterministic 8-K parsing, scoring, summary generation, and multi-channel delivery
- ownership-first Latest Filings/RSS discovery for Form 4 candidates
- XML-first Form 4 parsing with deterministic scoring, summary generation, and multi-channel delivery
- filing detail page with broker-backed reparse
- manual `Run repair now` and watchlist `Backfill now` controls
- global Slack / webhook / SMTP destinations with test-send
- optional OpenAI rewrite for summary text fields only

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
6. Add a watchlist entry, configure one or more global destinations, then use either `Run 8-K Ingest Now` or `Run Form 4 Ingest Now` from the dashboard.

The app starts in a manual-only posture unless `SCHEDULER_ENABLED=true`. Scheduler-enabled runs add live 8-K polling, live Form 4 discovery, and nightly recent repair on top of the manual controls.

## Development notes

- Keep `SCHEDULER_ENABLED=false` for reload/test workflows.
- Creating or re-enabling a watchlist entry automatically queues a 30-day background backfill.
- `SEC_LIVE_8K_OVERLAP_ROWS` defaults to `20` and tunes how many recent filtered 8-K rows the live poller rescans each cycle.
- `ALERT_WEBHOOK_URL` is a single global webhook endpoint and must use `https` unless `LOCALHOST_WEBHOOK_TEST_MODE=true` allows localhost http for testing.
- `SMTP_TO` is a single global Phase 5 recipient; SMTP credentials remain env-only.
- OpenAI rewrite is active only when both `OPENAI_API_KEY` and `OPENAI_MODEL` are set.
- Destination rows store metadata only. Secrets and endpoints stay in `.env`.
- The validated watchlist envelope is `25` issuers. `50` is the current hard cap.
- `100` issuers is not a current support claim.
- External API costs and infrastructure uptime remain the operator's responsibility.

## Optional OpenAI Rewrite

OpenAI rewrite is optional. Deterministic scoring remains authoritative, and OpenAI is used only to rewrite summary text fields.

- The app uses the Responses API and requests structured output via `text.format`.
- Requests are sent with `store=false` as an implementation policy.
- If the configured model is unsupported, unavailable, invalid, or returns output that fails schema validation, the app falls back to deterministic summaries automatically.
- OpenAI failure never blocks ingestion, parsing, scoring, alert creation, or delivery.
- Using OpenAI sends filing-derived summary input to OpenAI. API usage costs are the operator's responsibility.
- Use a pinned snapshot model for stable behavior. Current examples: `gpt-5-mini-2025-08-07` and `gpt-4.1-mini-2025-04-14`.

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
