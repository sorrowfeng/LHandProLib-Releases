#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from urllib import error, parse, request


API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
USER_AGENT = "LHandProLib-Releases-Sync"
ASSET_WAIT_ATTEMPTS = 12
ASSET_WAIT_SECONDS = 10
PUBLIC_RELEASE_TAG_RE = re.compile(r"^\d{8}$")

WINDOWS_CATEGORY = "windows_package"
LINUX_CATEGORY = "linux_package"
ENGLISH_MANUAL_CATEGORY = "english_manual"
CHINESE_MANUAL_CATEGORY = "chinese_manual"
PUBLIC_ASSET_CATEGORIES = (
    WINDOWS_CATEGORY,
    LINUX_CATEGORY,
    ENGLISH_MANUAL_CATEGORY,
    CHINESE_MANUAL_CATEGORY,
)
ENGLISH_MANUAL_NAME = "LHandProLib_SDK_Manual.md"
CHINESE_MANUAL_NAME = "LHandProLib_SDK_功能手册.md"
SANITIZED_CHINESE_MANUAL_NAME = "LHandProLib_SDK_.md"
SHA256_DIGEST_RE = re.compile(r"^sha256:([0-9a-fA-F]{64})$")
LEGACY_MOJIBAKE_CHINESE_MANUAL_LABEL = "LHandProLib_SDK_鍔熻兘鎵嬪唽.md"


def log(message: str) -> None:
    print(message, flush=True)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


class GitHubAPIError(RuntimeError):
    def __init__(self, status: int, method: str, url: str, detail: str) -> None:
        super().__init__(f"GitHub API error {status} on {method} {url}: {detail}")
        self.status = status


class GitHubNetworkError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token: str) -> None:
        self.token = token

    def _send(
        self,
        method: str,
        url: str,
        *,
        data: dict | bytes | None = None,
        headers: dict[str, str] | None = None,
        accept: str = "application/vnd.github+json",
        raw: bool = False,
        allow_not_found: bool = False,
    ):
        attempt = 0
        request_headers = {
            "Accept": accept,
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if headers:
            request_headers.update(headers)

        body = data
        if data is not None and not isinstance(data, (bytes, bytearray)):
            body = json.dumps(data).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json; charset=utf-8")

        can_retry = method in {"GET", "PATCH", "DELETE"}
        while True:
            attempt += 1
            req = request.Request(url, data=body, headers=request_headers, method=method)
            try:
                with request.urlopen(req, timeout=120) as response:
                    payload = response.read()
                    if raw:
                        return payload, response.headers
                    if not payload:
                        return None
                    return json.loads(payload.decode("utf-8"))
            except error.HTTPError as exc:
                payload = exc.read()
                if exc.code == 404 and allow_not_found:
                    return None
                if can_retry and exc.code in RETRYABLE_STATUS and attempt < 6:
                    delay = 2 ** (attempt - 1)
                    log(f"Retrying {method} {url} after HTTP {exc.code}, sleep {delay}s")
                    time.sleep(delay)
                    continue
                detail = payload.decode("utf-8", errors="replace").strip()
                raise GitHubAPIError(exc.code, method, url, detail) from exc
            except error.URLError as exc:
                if can_retry and attempt < 6:
                    delay = 2 ** (attempt - 1)
                    log(f"Retrying {method} {url} after network error, sleep {delay}s")
                    time.sleep(delay)
                    continue
                raise GitHubNetworkError(f"Network error on {method} {url}: {exc}") from exc

    def get(self, path: str, *, allow_not_found: bool = False):
        return self._send("GET", f"{API_ROOT}{path}", allow_not_found=allow_not_found)

    def post(self, path: str, data: dict):
        return self._send("POST", f"{API_ROOT}{path}", data=data)

    def patch(self, path: str, data: dict):
        return self._send("PATCH", f"{API_ROOT}{path}", data=data)

    def delete(self, path: str):
        return self._send("DELETE", f"{API_ROOT}{path}", allow_not_found=True)

    def download_asset(self, asset_url: str) -> bytes:
        payload, _ = self._send(
            "GET",
            asset_url,
            accept="application/octet-stream",
            raw=True,
        )
        return payload

    def upload_asset(
        self,
        upload_url: str,
        name: str,
        label: str,
        content_type: str,
        content: bytes,
    ):
        base_url = upload_url.split("{", 1)[0]
        query = parse.urlencode({"name": name, "label": label or ""})
        return self._send(
            "POST",
            f"{base_url}?{query}",
            data=content,
            headers={"Content-Type": content_type or "application/octet-stream"},
            accept="application/vnd.github+json",
        )


def validate_release_tag(tag_name: str) -> None:
    if not PUBLIC_RELEASE_TAG_RE.fullmatch(tag_name):
        raise RuntimeError(f"Release tag is not allowed for the public mirror: {tag_name}")
    try:
        parsed = datetime.strptime(tag_name, "%Y%m%d")
    except ValueError as exc:
        raise RuntimeError(f"Release tag is not a valid calendar date: {tag_name}") from exc
    if parsed.strftime("%Y%m%d") != tag_name:
        raise RuntimeError(f"Release tag is not a canonical YYYYMMDD date: {tag_name}")


def get_release_by_tag(
    client: GitHubClient,
    repo_full_name: str,
    tag_name: str,
    *,
    allow_not_found: bool = False,
) -> dict | None:
    encoded_tag = parse.quote(tag_name, safe="")
    return client.get(
        f"/repos/{repo_full_name}/releases/tags/{encoded_tag}",
        allow_not_found=allow_not_found,
    )


def public_asset_category(asset: dict, tag_name: str) -> str | None:
    name = asset.get("name") or ""
    label = asset.get("label") or ""
    if name == f"LHandProLib-API-Windows-{tag_name}.7z":
        return WINDOWS_CATEGORY
    if name == f"LHandProLib-API-Linux-{tag_name}.tar.gz":
        return LINUX_CATEGORY
    if name == ENGLISH_MANUAL_NAME:
        return ENGLISH_MANUAL_CATEGORY
    if name == CHINESE_MANUAL_NAME:
        return CHINESE_MANUAL_CATEGORY
    if name == SANITIZED_CHINESE_MANUAL_NAME and label in (
        CHINESE_MANUAL_NAME,
        LEGACY_MOJIBAKE_CHINESE_MANUAL_LABEL,
    ):
        return CHINESE_MANUAL_CATEGORY
    return None


def select_public_assets(source_release: dict, tag_name: str) -> tuple[dict[str, dict], list[dict]]:
    selected_by_category: dict[str, dict] = {}
    skipped: list[dict] = []
    for asset in source_release.get("assets", []):
        category = public_asset_category(asset, tag_name)
        if category is None:
            skipped.append(asset)
            continue
        if category in selected_by_category:
            first = selected_by_category[category]
            names = ", ".join(sorted((first.get("name") or "", asset.get("name") or "")))
            raise RuntimeError(
                f"Release {tag_name} has duplicate assets for {category}: {names}"
            )
        if (
            category == CHINESE_MANUAL_CATEGORY
            and asset.get("name") == SANITIZED_CHINESE_MANUAL_NAME
            and asset.get("label") == LEGACY_MOJIBAKE_CHINESE_MANUAL_LABEL
        ):
            asset = dict(asset)
            asset["label"] = CHINESE_MANUAL_NAME
        selected_by_category[category] = asset

    selected = {
        asset["name"]: asset
        for category, asset in selected_by_category.items()
        if category in PUBLIC_ASSET_CATEGORIES
    }
    return selected, skipped


def missing_public_asset_categories(source_assets: dict[str, dict], tag_name: str) -> list[str]:
    present = {
        category
        for asset in source_assets.values()
        if (category := public_asset_category(asset, tag_name)) is not None
    }
    return [category for category in PUBLIC_ASSET_CATEGORIES if category not in present]


def wait_for_source_release(
    source_client: GitHubClient,
    source_repo: str,
    tag_name: str,
) -> tuple[dict, dict[str, dict], list[dict]]:
    last_missing = list(PUBLIC_ASSET_CATEGORIES)
    for attempt in range(1, ASSET_WAIT_ATTEMPTS + 1):
        source_release = get_release_by_tag(
            source_client,
            source_repo,
            tag_name,
            allow_not_found=True,
        )
        if source_release and source_release.get("draft"):
            raise RuntimeError(f"Refusing to mirror draft release: {tag_name}")
        if source_release and source_release.get("prerelease"):
            raise RuntimeError(f"Refusing to mirror prerelease: {tag_name}")

        source_assets, skipped = select_public_assets(source_release or {}, tag_name)
        last_missing = missing_public_asset_categories(source_assets, tag_name)
        if source_release and not last_missing:
            return source_release, source_assets, skipped

        if attempt < ASSET_WAIT_ATTEMPTS:
            missing_text = ", ".join(last_missing)
            log(
                f"Release {tag_name} is not ready (missing: {missing_text}); "
                f"retrying in {ASSET_WAIT_SECONDS}s ({attempt}/{ASSET_WAIT_ATTEMPTS})"
            )
            time.sleep(ASSET_WAIT_SECONDS)

    missing_text = ", ".join(last_missing)
    raise RuntimeError(
        f"Release {tag_name} is missing required public assets after "
        f"{ASSET_WAIT_ATTEMPTS} attempts: {missing_text}"
    )


def download_and_validate_assets(
    source_client: GitHubClient,
    source_assets: dict[str, dict],
) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    for name, source_asset in sorted(source_assets.items()):
        log(f"Downloading source asset {name}")
        payload = source_client.download_asset(source_asset["url"])

        expected_size = source_asset.get("size")
        if not isinstance(expected_size, int) or expected_size < 0:
            raise RuntimeError(f"Source asset {name} has an invalid declared size: {expected_size!r}")
        if len(payload) != expected_size:
            raise RuntimeError(
                f"Source asset {name} size mismatch: expected {expected_size}, got {len(payload)}"
            )

        digest = source_asset.get("digest") or ""
        digest_match = SHA256_DIGEST_RE.fullmatch(digest)
        if digest_match:
            expected_digest = digest_match.group(1).lower()
            actual_digest = hashlib.sha256(payload).hexdigest()
            if actual_digest != expected_digest:
                raise RuntimeError(
                    f"Source asset {name} SHA-256 mismatch: "
                    f"expected {expected_digest}, got {actual_digest}"
                )

        payloads[name] = payload
    return payloads


def list_releases(client: GitHubClient, repo_full_name: str) -> list[dict]:
    releases: list[dict] = []
    page = 1
    while True:
        chunk = client.get(f"/repos/{repo_full_name}/releases?per_page=100&page={page}")
        if not chunk:
            break
        releases.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return releases


def find_target_release_by_tag(
    target_client: GitHubClient,
    target_repo: str,
    tag_name: str,
) -> dict | None:
    matches = [
        release
        for release in list_releases(target_client, target_repo)
        if release.get("tag_name") == tag_name
    ]
    if len(matches) > 1:
        release_ids = ", ".join(str(release.get("id")) for release in matches)
        raise RuntimeError(f"Target has multiple releases for tag {tag_name}: {release_ids}")
    return matches[0] if matches else None


def draft_release_payload(source_release: dict) -> dict:
    return {
        "tag_name": source_release["tag_name"],
        "name": source_release.get("name") or source_release["tag_name"],
        "body": source_release.get("body") or "",
        "draft": True,
        "prerelease": False,
    }


def stage_target_release(
    target_client: GitHubClient,
    target_repo: str,
    source_release: dict,
    existing_release: dict | None,
) -> dict:
    payload = draft_release_payload(source_release)
    if existing_release:
        log(f"Staging existing release {source_release['tag_name']} as draft")
        return target_client.patch(f"/repos/{target_repo}/releases/{existing_release['id']}", payload)

    log(f"Creating draft release {source_release['tag_name']}")
    try:
        return target_client.post(f"/repos/{target_repo}/releases", payload)
    except GitHubAPIError as exc:
        if exc.status != 422:
            raise
        log(
            f"Create returned HTTP 422 for {source_release['tag_name']}; "
            "checking for a release created by an earlier or concurrent run"
        )
        recovered_release = find_target_release_by_tag(
            target_client,
            target_repo,
            source_release["tag_name"],
        )
        if recovered_release is None:
            raise
        log(f"Recovered existing release {source_release['tag_name']} after HTTP 422")
        return target_client.patch(
            f"/repos/{target_repo}/releases/{recovered_release['id']}",
            payload,
        )


def publish_target_release(
    target_client: GitHubClient,
    target_repo: str,
    target_release: dict,
) -> dict:
    log(f"Publishing completed release {target_release['tag_name']}")
    return target_client.patch(
        f"/repos/{target_repo}/releases/{target_release['id']}",
        {"draft": False, "prerelease": False},
    )


def assets_match(source_asset: dict, target_asset: dict) -> bool:
    if (source_asset.get("label") or "") != (target_asset.get("label") or ""):
        return False
    if (source_asset.get("content_type") or "") != (target_asset.get("content_type") or ""):
        return False

    source_digest = source_asset.get("digest") or ""
    target_digest = target_asset.get("digest") or ""
    if source_digest and target_digest:
        return source_digest == target_digest
    return source_asset.get("size") == target_asset.get("size")


def sync_assets(
    target_client: GitHubClient,
    target_repo: str,
    source_assets: dict[str, dict],
    source_payloads: dict[str, bytes],
    target_release: dict,
) -> None:
    if set(source_payloads) != set(source_assets):
        raise RuntimeError("Pre-downloaded source asset payloads do not match selected assets")

    target_assets = {asset["name"]: asset for asset in target_release.get("assets", [])}

    for target_name, target_asset in sorted(target_assets.items()):
        if target_name not in source_assets:
            log(f"Deleting stale or non-public asset {target_release['tag_name']}/{target_name}")
            target_client.delete(f"/repos/{target_repo}/releases/assets/{target_asset['id']}")

    refreshed_release = target_client.get(f"/repos/{target_repo}/releases/{target_release['id']}")
    target_assets = {asset["name"]: asset for asset in refreshed_release.get("assets", [])}
    upload_url = refreshed_release["upload_url"]

    for name, source_asset in sorted(source_assets.items()):
        target_asset = target_assets.get(name)
        needs_upload = target_asset is None or not assets_match(source_asset, target_asset)
        if target_asset and needs_upload:
            log(f"Replacing asset {target_release['tag_name']}/{name}")
            target_client.delete(f"/repos/{target_repo}/releases/assets/{target_asset['id']}")
        if needs_upload:
            log(f"Uploading asset {target_release['tag_name']}/{name}")
            target_client.upload_asset(
                upload_url,
                name,
                source_asset.get("label") or "",
                source_asset.get("content_type") or "application/octet-stream",
                source_payloads[name],
            )
        else:
            log(f"Keeping asset {target_release['tag_name']}/{name}")

    completed_release = target_client.get(f"/repos/{target_repo}/releases/{target_release['id']}")
    completed_assets = {asset["name"]: asset for asset in completed_release.get("assets", [])}
    if set(completed_assets) != set(source_assets):
        raise RuntimeError(
            f"Target release {target_release['tag_name']} asset set did not converge"
        )
    for name, source_asset in source_assets.items():
        if not assets_match(source_asset, completed_assets[name]):
            raise RuntimeError(
                f"Target release {target_release['tag_name']} asset verification failed: {name}"
            )


def mirror_release(
    target_client: GitHubClient,
    target_repo: str,
    source_release: dict,
    source_assets: dict[str, dict],
    source_payloads: dict[str, bytes],
) -> dict:
    existing_release = find_target_release_by_tag(
        target_client,
        target_repo,
        source_release["tag_name"],
    )
    target_release = stage_target_release(
        target_client,
        target_repo,
        source_release,
        existing_release,
    )
    sync_assets(
        target_client,
        target_repo,
        source_assets,
        source_payloads,
        target_release,
    )
    return publish_target_release(target_client, target_repo, target_release)


def main() -> int:
    target_repo = require_env("TARGET_REPO")
    source_repo = require_env("SRC_REPO")
    target_token = require_env("GITHUB_TOKEN")
    source_token = require_env("SRC_REPO_TOKEN")
    release_tag = require_env("RELEASE_TAG")

    validate_release_tag(release_tag)

    source_client = GitHubClient(source_token)
    target_client = GitHubClient(target_token)
    source_release, source_assets, skipped_assets = wait_for_source_release(
        source_client,
        source_repo,
        release_tag,
    )
    for asset in skipped_assets:
        log(f"Skipping non-public asset {release_tag}/{asset.get('name') or '<unnamed>'}")

    source_payloads = download_and_validate_assets(source_client, source_assets)
    mirror_release(
        target_client,
        target_repo,
        source_release,
        source_assets,
        source_payloads,
    )

    log(f"Release sync completed for {release_tag}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR: {exc}")
        raise
