# Changelog

## Unreleased

No unreleased changes.

## 0.2.0 — 2026-04-14

First self-hosted paid release.

- Added buyer-facing release docs, including quickstart, Docker install, upgrade, backup/restore, troubleshooting, metadata guidance, PostgreSQL future-work notes, and a capacity benchmark plan.
- Added release/support boundary docs: commercial license placeholder, support, security, privacy, and disclaimer.
- Added release tooling via `make doctor`, `make smoke`, `make backup`, `make restore`, and `make release-bundle`.
- Added visible version/build metadata in the UI plus support diagnostics in `Settings` and `Advanced`.
- Hardened first-run config handling for blank optional env values and session secret defaults.
- Documented and tested SMTP comma-separated multi-recipient support.

## 0.1.0

Initial self-hosted SEC signal inbox release candidate.
