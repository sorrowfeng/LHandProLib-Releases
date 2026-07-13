# LHandProLib Releases Mirror

This repository is a release-only mirror for the private
`sorrowfeng/LHandProLib` repository. It intentionally contains no SDK source
code.

The source repository dispatches `.github/workflows/sync-releases.yml` after a
public Release is published. Each dispatch must provide one `YYYYMMDD` Release
tag; the mirror does not poll the source repository on a schedule.

Only these four assets are allowed in the public mirror:

- `LHandProLib-API-Windows-<tag>.7z`
- `LHandProLib-API-Linux-<tag>.tar.gz`
- `LHandProLib_SDK_Manual.md`
- `LHandProLib_SDK_功能手册.md`

GitHub may expose the Chinese manual through the API as
`LHandProLib_SDK_.md`. That sanitized name is accepted only when its asset label
is the full Chinese filename above. The one known mojibake label produced by the
legacy publisher is accepted for historical Releases and normalized back to the
correct Chinese label. All four asset categories must be present exactly once
before a target Release is created or updated.

Drafts, prereleases, invalid or customer-specific tags, source archives, debug
symbols, and unrelated assets are not published. Extra source assets are
skipped, while stale or non-public assets already attached to the mirrored
Release are removed.

The sync script preserves source asset labels and content types. It uses GitHub
SHA-256 digests when both sides provide them, with file size as the fallback for
older API responses.

Before changing the target repository, the workflow downloads all four source
assets into memory and verifies their declared sizes. When GitHub supplies a
`sha256:<hex>` digest, the downloaded bytes are verified against it as well.

Publishing is transactional at the Release level:

1. Find an existing public or draft Release for the requested tag.
2. Create it as a draft, or temporarily move the existing Release to draft.
3. Remove non-public assets, upload replacements, and read back all four assets.
4. Publish the Release only after the complete asset set passes verification.

If an upload or verification fails, the Release remains a draft. A later run
finds that draft and resumes safely. Mutating uploads are not blindly retried;
idempotent reads, draft metadata updates, and deletes use bounded retries.

Run the local regression tests with:

```powershell
python -B .github/scripts/test_sync_releases.py
```
