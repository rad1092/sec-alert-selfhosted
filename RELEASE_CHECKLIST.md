# Release Checklist

This checklist is intentionally command-driven so a maintainer can cut a release without improvising.

## 1. Final preflight

```powershell
git status --short
make doctor
make smoke
uv run --python 3.12 pytest
uv run --python 3.12 ruff check .
```

If `make` is unavailable:

```powershell
uv run --python 3.12 python -m app.cli.release doctor
uv run --python 3.12 python -m app.cli.release smoke
```

Confirm:

- README is product-facing and current
- install/upgrade/backup/restore docs are current
- support/privacy/security/license/disclaimer docs are present
- screenshots are either real captures or clearly labeled placeholders
- changelog entry is ready

## 2. Confirm version target

Default target for the first paid release is `v0.2.0`, but confirm before tagging.

Optional release metadata for the bundle/UI:

```powershell
$env:APP_BUILD_DATE = (Get-Date).ToString("yyyy-MM-dd")
$env:APP_BUILD_SHA = (git rev-parse --short HEAD)
$env:APP_BUILD_SOURCE = "release"
```

## 3. Build the release bundle

```powershell
make release-bundle VERSION=v0.2.0
```

Fallback without `make`:

```powershell
uv run --python 3.12 python -m app.cli.release release-bundle --version v0.2.0
```

Expected output:

- `dist/releases/sec-alert-self-hosted-v0.2.0.zip`
- `dist/releases/sec-alert-self-hosted-v0.2.0.sha256`
- `dist/releases/sec-alert-self-hosted-v0.2.0.manifest.json`

Verify the checksum file exists and matches the archive name.

## 4. Create the git tag

```powershell
git tag -a v0.2.0 -m "First paid self-hosted release candidate"
git push origin v0.2.0
```

## 5. Draft the GitHub Release

If you use GitHub CLI:

```powershell
gh release create v0.2.0 `
  dist/releases/sec-alert-self-hosted-v0.2.0.zip `
  dist/releases/sec-alert-self-hosted-v0.2.0.sha256 `
  dist/releases/sec-alert-self-hosted-v0.2.0.manifest.json `
  --draft `
  --title "v0.2.0" `
  --notes-file CHANGELOG.md
```

Then review:

- release title
- attached assets
- changelog/release notes
- checksum filename
- whether screenshots and README render correctly on GitHub

## 6. Publish

After review, publish the draft release in GitHub UI.

## 7. Manual GitHub metadata apply

These steps are not done by repo file edits alone. Apply them manually in GitHub:

- repository description
- topic list
- About text
- social preview image

Use the exact copy in:

- `docs/GITHUB_METADATA.md`

## 8. Post-release verification

```powershell
gh release view v0.2.0
```

Check:

- the release resolves to the right tag
- all assets are downloadable
- README links render
- buyer quickstart still matches the shipped bundle

## HUMAN LEGAL REVIEW REQUIRED

This checklist is operational guidance, not legal advice.
