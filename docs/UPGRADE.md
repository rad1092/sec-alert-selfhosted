# Upgrade

This project is sold as a self-hosted runtime, so upgrades are the operator's responsibility.

## Safe upgrade flow

1. Stop the app.
2. Create a backup.
3. Pull the new code or unpack the new release bundle.
4. Apply database migrations if included.
5. Run doctor and smoke.
6. Start the app again.

## Commands

```powershell
docker compose down
make backup
git pull
make migrate
make doctor
make smoke
docker compose up --build
```

If `make` is unavailable, replace the release helpers with:

```powershell
uv run --python 3.12 python -m app.cli.release backup
uv run --python 3.12 python -m app.cli.release doctor
uv run --python 3.12 python -m app.cli.release smoke
```

## If using a release bundle instead of git

1. unpack the new bundle into a fresh directory
2. copy forward your `.env`
3. restore your backup if needed
4. run the same doctor/smoke checks

## Upgrade notes

- keep the existing `data/` directory unless a documented migration requires otherwise
- do not change the supported SQLite-first runtime shape during a normal upgrade
- confirm the release notes and changelog before replacing a working install
