# Buyer Quickstart

This is the shortest path from a fresh checkout to a first useful SEC signal.

## You need

- Docker Desktop or Python 3.12 with `uv`
- a valid `SEC_USER_AGENT`
- a filled `.env`
- one ticker you already care about
- optional notification credentials if you want external delivery

## Fast path

1. Copy the env file.

```powershell
Copy-Item .env.example .env
```

2. Fill in at least `SEC_USER_AGENT`.

3. Validate the runtime.

```powershell
make doctor
```

If `make` is unavailable on your system:

```powershell
uv run --python 3.12 python -m app.cli.release doctor
```

4. Start the app with Docker.

```powershell
docker compose up --build
```

5. Open:

```text
http://127.0.0.1:8000
```

6. Add one ticker in `Watchlist`.
7. Run `Run 8-K Check` or `Run Form 4 Check`.
8. Open the first filing detail page.

## What success looks like

- the app loads locally without any hosted account
- the watchlist entry is enabled
- `Inbox` explains what needs review now
- filing detail explains:
  - what happened
  - why it was flagged
  - how strong the signal is
  - where to verify it on the SEC site
- `Notifications` makes it clear whether signals stay local or are sent externally

## Useful commands

```powershell
make doctor
make smoke
make backup
```

## Notes

- OpenAI is optional and never required for the core product.
- `SMTP_TO` supports a comma-separated recipient list.
- Historical catch-up can populate the archive before the live inbox feels busy.
