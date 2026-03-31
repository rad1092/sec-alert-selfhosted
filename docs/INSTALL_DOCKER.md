# Docker Install

Docker is the supported buyer install path for the first paid release.

## Requirements

- Docker Desktop or compatible Docker Engine/Compose
- `.env` with `SEC_USER_AGENT` filled in
- local access to port `8000`

## Install

1. Copy the sample environment file.

```powershell
Copy-Item .env.example .env
```

2. Edit `.env` and fill in:

- `SEC_USER_AGENT`
- optional notification credentials
- optional OpenAI BYOK settings

3. Run diagnostics.

```powershell
make doctor
make smoke
```

If `make` is not installed:

```powershell
uv run --python 3.12 python -m app.cli.release doctor
uv run --python 3.12 python -m app.cli.release smoke
```

4. Start the app.

```powershell
docker compose up --build
```

5. Open:

```text
http://127.0.0.1:8000
```

## Supported Docker posture

- localhost/local-first
- SQLite-first
- env-only secrets
- single-process runtime
- buyer-owned data directory

## SMTP note

If you use email delivery, `SMTP_TO` may contain a comma-separated recipient list.

## Before upgrading

Run a backup while the app is stopped:

```powershell
make backup
```

Then follow [UPGRADE.md](UPGRADE.md).
