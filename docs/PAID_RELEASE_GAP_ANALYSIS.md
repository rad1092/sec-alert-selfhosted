# Paid Release Gap Analysis

## Current Architecture Summary

SEC Alert Self-Hosted is a FastAPI + Jinja2 application designed for a single user running locally or on a small self-hosted machine.

- Runtime posture: localhost-first, single-process, single-user
- Data store: SQLite only
- Deployment posture: local Python or simple Docker compose
- SEC access posture: shared in-memory request broker, conservative rate limiting, explicit `SEC_USER_AGENT`, near-real-time rather than instant
- Core workflows:
  - watchlist CRUD
  - manual 8-K ingest
  - manual Form 4 ingest
  - live polling with overlap recheck
  - repair and backfill
  - deterministic scoring and summary generation
  - optional OpenAI rewrite for presentation only
  - Slack, webhook, and SMTP notifications
- Existing product surfaces:
  - Inbox
  - All Signals
  - Watchlist
  - Notifications
  - Advanced / Issues / Settings

This is already a real self-hosted product shape, not a prototype or SaaS shell.

## What Is Already Strong Enough To Sell

The current repository already has a credible commercial core for a first paid self-hosted release.

- Clear product shape: focused SEC signal inbox for a watchlist
- Honest scope: self-hosted, local-first, BYOK, no SaaS dependency
- Explainable trust model: deterministic scoring, visible reasons, source links, filing detail view
- Useful watchlist loop: add ticker, run check, review filing, verify on SEC
- Practical self-hosting posture: Docker path, SQLite-first, environment-variable-based secrets
- AI is optional and correctly constrained to presentation-only rewrite
- Existing test suite is already strong enough to support a release-hardening pass

## Must-Have Changes Before First Paid Release

These are release blockers because they affect buyer trust, install confidence, or the ability to deliver a clean commercial package.

### Buyer-facing packaging

- Rewrite `README.md` into a product-facing page
- Add screenshots or clearly labeled placeholders
- Add buyer install, upgrade, backup/restore, and troubleshooting docs
- Add GitHub metadata recommendations in `docs/GITHUB_METADATA.md`

### Commercial and support boundary

- Document support/update boundary explicitly
- Add `SUPPORT.md`, `SECURITY.md`, `PRIVACY.md`, and `DISCLAIMER.md`
- Add one concrete commercial boundary file after the license strategy is chosen
- Mark uncertain legal text with `HUMAN LEGAL REVIEW REQUIRED`

### Release discipline

- Add `CHANGELOG.md`, `ROADMAP.md`, `VERSIONING.md`, and `RELEASE_CHECKLIST.md`
- Add a versioned release bundle with SHA256 checksums
- Add visible app version and build date in the UI
- Document a tag-based GitHub Release flow with executable commands

### Buyer operations and recovery

- Add `make doctor`, `make smoke`, `make backup`, `make restore`, and `make release-bundle`
- Add diagnostics for:
  - required env vars
  - `SEC_USER_AGENT`
  - writable runtime paths
  - notifier sanity
  - startup config problems
  - supported watchlist-cap settings
- Add a small diagnostics/support view under the existing operator surfaces

### Small safe product improvement

- Replace single-recipient SMTP handling with comma-separated multi-recipient support

## Should-Have Changes After First Customers

These improve the product, but they are not required to start selling the first paid release.

- Watchlist import/export
- Lightweight dashboard freshness polling
- More polished screenshot/GIF assets
- More refined diagnostics UX
- Additional notifier channels after real customer demand

## Do-Not-Build-Yet

These are explicitly out of scope for the first paid release.

- Hosted SaaS conversion
- Auth, accounts, teams, subscriptions, billing, Stripe
- Redis / Celery / Postgres architecture rewrite
- PostgreSQL support
- Multi-tenant permissions
- Raising the documented watchlist-capacity support claim
- Major scoring-model expansion
- Broader filing research/search platform features
- Discord and Telegram notifier expansion
- Richer AI/provider expansion

## License Strategy Decision

The first paid release needs a deliberate license strategy before public sale.

Options to evaluate:

1. Public demo repository plus separate commercial distribution
2. Business Source License 1.1
3. Custom commercial license / EULA

The repository should document the decision process and resulting support/update boundary clearly. The final commercial file choice should follow that decision, not precede it.
