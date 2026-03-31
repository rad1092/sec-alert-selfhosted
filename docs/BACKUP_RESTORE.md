# Backup and Restore

The supported backup unit for this release is the local runtime state, centered on the SQLite database and `data/` directory.

## Create a backup

```powershell
make backup
```

Fallback without `make`:

```powershell
uv run --python 3.12 python -m app.cli.release backup
```

This creates a timestamped archive in `backups/`.

## Restore from a backup

```powershell
make restore BACKUP_ARCHIVE=backups\sec-alert-backup-YYYYMMDD-HHMMSS.zip
```

Fallback without `make`:

```powershell
uv run --python 3.12 python -m app.cli.release restore --archive backups\sec-alert-backup-YYYYMMDD-HHMMSS.zip
```

## Recommended workflow

1. stop the app before backup or restore
2. keep the backup zip and checksum together
3. verify the restored app with:

```powershell
make doctor
make smoke
```

## What is restored

- SQLite database
- runtime data files
- optional `.env` copy if it was included in the backup

## Safety behavior

The restore flow creates safety copies of the current local runtime before overwriting database or env files.
