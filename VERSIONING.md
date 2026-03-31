# Versioning

This product should remain `0.x.y` until the buyer-facing runtime, support boundary, and documented install/upgrade behavior are intentionally frozen.

## Default target

- Default target tag for the first paid release: `v0.2.0`
- This is a planning target, not a hardcoded promise
- The final tag should be confirmed only after README, release boundary docs, and the release checklist are frozen

## SemVer-style intent

- `patch`:
  - buyer-safe bug fixes
  - docs updates
  - diagnostics/support improvements
  - no data-path or install-path surprises
- `minor`:
  - additive buyer-facing features that preserve the documented runtime shape
  - example: SMTP quality-of-life improvements or buyer-safe admin diagnostics
- `major`:
  - only when public product behavior or the supported install/runtime contract changes intentionally

## Why not `1.0.0` yet

- The first paid release is a polished commercial cut, but still intentionally narrow.
- Support boundaries are being documented now.
- Capacity claims remain conservative and not broadly benchmarked.
- The product is not yet claiming broad platform maturity.

## Release requirements

- Use git tags for every release bundle.
- Ship a SHA256 checksum with the bundle.
- Keep public docs aligned with the exact runtime you support.
- Do not market unverified capacity or support claims as if they were guaranteed.

## HUMAN LEGAL REVIEW REQUIRED

If you decide to market a release as `1.0.0`, freeze the public support and stability promise first.
