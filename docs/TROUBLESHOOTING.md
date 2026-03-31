# Troubleshooting

This page covers the most common buyer-side issues for the first paid self-hosted release.

## First steps

Run:

```powershell
make doctor
make smoke
```

These should be your first checks before reading source code.

If `make` is unavailable, run:

```powershell
uv run --python 3.12 python -m app.cli.release doctor
uv run --python 3.12 python -m app.cli.release smoke
```

## The app will not start

Check:

- `SEC_USER_AGENT` is set
- `APP_HOST` is `127.0.0.1` or `localhost`
- `.env` exists and is readable
- the data directory is writable
- port `8000` is free

## The inbox is empty

Usually one of these is true:

- the watchlist is empty
- the watchlist is paused
- the first backfill has not finished
- no new filings have been discovered yet

Try a manual check from the UI after adding one enabled ticker.

## Signals are local-only

That is not a failure. It means no external destination is configured yet.

If you want external delivery:

- configure Slack, webhook, or SMTP
- test-send from `Notifications`

## SMTP email is not sending

Check:

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_FROM`
- `SMTP_TO`

`SMTP_TO` supports a comma-separated recipient list.

## OpenAI is not active

That is fine for deterministic-only operation.

If you expected rewrite support, confirm:

- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `Settings -> OpenAI rewrite active`

## A filing failed

A single filing failure does not mean the whole app is down.

- open `Advanced -> Issues`
- check whether the issue is filing-specific
- rerun repair or reparse if appropriate

## When to contact support

Contact support when:

- a valid `.env` still fails doctor/smoke
- Docker install fails on a clean machine
- backup or restore fails as documented
- failures look broader than one filing or one ticker
