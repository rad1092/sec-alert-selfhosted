# PostgreSQL Future Work

PostgreSQL is explicitly out of scope for the first paid release. This document exists so future work is visible without expanding current support claims.

## What would need to change later

- dependency review for driver selection and SQLAlchemy configuration
- migration/test matrix expansion for SQLite + PostgreSQL
- Docker Compose service updates for a database container
- backup/restore docs and scripts that no longer assume a local SQLite file
- runtime helpers that currently assume local file paths for the database
- install docs that currently assume SQLite-first local persistence

## Why it stays out for now

- it would widen the support surface materially
- it would complicate buyer install/backup/recovery
- it is not required for the current single-user local-first product shape
