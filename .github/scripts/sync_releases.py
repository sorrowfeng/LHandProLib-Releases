#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


API_VERSION = "2022-11-28"
API_ROOT = "https://api.github.com"
USER_AGENT = "LHandProLib-Releases-Sync"


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def api_request(method: str, url: str, token: str, *, data: dict | None = None, headers: dict | None = None):
    request_headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
    }
    if headers:
        request_headers.update(headers)

    payload = None
    if data is not None:
        payload = json.dumps(data).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=payload, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            body = response.read()
            if not body:
                return None
            return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: {exc.code} {detail}") from exc


def download_asset(asset: dict, token: str, dest_dir: Path) -> Path:
    target = dest_dir / asset["name"]
    headers = {
        "Accept": "application/octet-stream",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
    }
    request = urllib.request.Request(asset["url"], headers=headers, method="GET")
    with urllib.request.urlopen(request) as response, target.open("wb") as handle:
        handle.write(response.read())
    return target


def list_releases(repo: str, token: str) -> list[dict]:
    page = 1
    releases: list[dict] = []
    while True:
        url = f"{API_ROOT}/repos/{repo}/releases?per_page=100&page={page}"
        batch = api_request("GET", url, token)
        if not batch:
            break
        releases.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return releases


def get_release_by_tag(repo: str, tag: str, token: str) -> dict | None:
    url = f"{API_ROOT}/repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}"
    return api_request("GET", url, token)


def create_release(repo: str, token: str, payload: dict) -> dict:
    url = f"{API_ROOT}/repos/{repo}/releases"
    result = api_request("POST", url, token, data=payload)
    if result is None:
        raise RuntimeError(f"Failed to create release {payload['tag_name']}")
    return result


def update_release(repo: str, release_id: int, token: str, payload: dict) -> dict:
    url = f"{API_ROOT}/repos/{repo}/releases/{release_id}"
    result = api_request("PATCH", url, token, data=payload)
    if result is None:
        raise RuntimeError(f"Failed to update release {release_id}")
    return result


def delete_asset(repo: str, asset_id: int, token: str) -> None:
    url = f"{API_ROOT}/repos/{repo}/releases/assets/{asset_id}"
    api_request("DELETE", url, token)


def upload_asset(upload_url_template: str, asset_path: Path, token: str, *, label: str = "") -> None:
    upload_url = upload_url_template.split("{", 1)[0]
    query = f"name={urllib.parse.quote(asset_path.name, safe='')}"
    if label:
        query += f"&label={urllib.parse.quote(label, safe='')}"
    upload_url = f"{upload_url}?{query}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
    }
    data = asset_path.read_bytes()
    request = urllib.request.Request(upload_url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Asset upload failed for {asset_path.name}: {exc.code} {detail}") from exc


def sync_release(src_release: dict, dst_repo: str, src_token: str, dst_token: str, work_dir: Path) -> None:
    tag = src_release["tag_name"]
    name = src_release.get("name") or tag
    body = src_release.get("body") or ""

    payload = {
        "tag_name": tag,
        "target_commitish": "main",
        "name": name,
        "body": body,
        "draft": False,
        "prerelease": bool(src_release.get("prerelease", False)),
    }

    dst_release = get_release_by_tag(dst_repo, tag, dst_token)
    if dst_release is None:
        print(f"[create] {tag}", flush=True)
        dst_release = create_release(dst_repo, dst_token, payload)
    else:
        print(f"[update] {tag}", flush=True)
        dst_release = update_release(dst_repo, dst_release["id"], dst_token, payload)

    src_asset_names = {asset["name"] for asset in src_release.get("assets", [])}
    existing_assets = {asset["name"]: asset for asset in dst_release.get("assets", [])}

    for asset_name, dst_asset in existing_assets.items():
        if asset_name not in src_asset_names:
            delete_asset(dst_repo, dst_asset["id"], dst_token)

    for src_asset in src_release.get("assets", []):
        local_path = download_asset(src_asset, src_token, work_dir)
        old_asset = existing_assets.get(src_asset["name"])
        if old_asset is not None:
            delete_asset(dst_repo, old_asset["id"], dst_token)
        upload_asset(
            dst_release["upload_url"],
            local_path,
            dst_token,
            label=src_asset.get("label", "") or "",
        )


def main() -> int:
    src_repo = env("SRC_REPO")
    dst_repo = env("DST_REPO")
    src_token = env("SRC_TOKEN")
    dst_token = env("DST_TOKEN")

    src_releases = [
        release
        for release in list_releases(src_repo, src_token)
        if not release.get("draft", False)
    ]
    src_releases.sort(key=lambda item: item["tag_name"])

    with tempfile.TemporaryDirectory(prefix="release-sync-") as tmp:
        work_dir = Path(tmp)
        for release in src_releases:
            detailed_release = get_release_by_tag(src_repo, release["tag_name"], src_token)
            if detailed_release is None:
                raise RuntimeError(f"Source release disappeared while syncing: {release['tag_name']}")
            sync_release(detailed_release, dst_repo, src_token, dst_token, work_dir)

    print(f"Synced {len(src_releases)} releases from {src_repo} to {dst_repo}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise
