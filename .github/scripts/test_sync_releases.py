#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import io
import unittest
from unittest import mock

import sync_releases


TAG = "20260710"


def asset(name: str, **overrides) -> dict:
    value = {
        "name": name,
        "label": "",
        "size": 100,
        "content_type": "application/octet-stream",
        "digest": "sha256:abc",
        "url": f"https://api.github.test/assets/{name}",
    }
    value.update(overrides)
    return value


def required_assets(tag_name: str = TAG) -> list[dict]:
    return [
        asset(f"LHandProLib-API-Windows-{tag_name}.7z"),
        asset(f"LHandProLib-API-Linux-{tag_name}.tar.gz"),
        asset(sync_releases.ENGLISH_MANUAL_NAME),
        asset(sync_releases.CHINESE_MANUAL_NAME),
    ]


class ReleaseTagTests(unittest.TestCase):
    def test_accepts_valid_calendar_date(self):
        sync_releases.validate_release_tag(TAG)
        sync_releases.validate_release_tag("20240229")

    def test_rejects_noncanonical_or_invalid_dates(self):
        for tag_name in (
            "2026071",
            "2026-07-10",
            "../20260710",
            "20260710-LumiBot",
            "20260229",
            "20261301",
            "00000000",
        ):
            with self.subTest(tag_name=tag_name):
                with self.assertRaises(RuntimeError):
                    sync_releases.validate_release_tag(tag_name)


class PublicAssetFilterTests(unittest.TestCase):
    def test_selects_exactly_four_public_assets_and_skips_extras(self):
        expected = required_assets()
        extras = [
            asset("LHandProLib.pdb"),
            asset(f"LHandProLib-API-Windows-{TAG}-customer.7z"),
            asset("source.zip"),
            asset(f"LHandProLib-API-Linux-20260709.tar.gz"),
            asset("LHandProLib_SDK_Manual.pdf"),
        ]

        selected, skipped = sync_releases.select_public_assets(
            {"assets": expected + extras},
            TAG,
        )

        self.assertEqual({item["name"] for item in expected}, set(selected))
        self.assertEqual([item["name"] for item in extras], [item["name"] for item in skipped])
        self.assertEqual([], sync_releases.missing_public_asset_categories(selected, TAG))

    def test_accepts_sanitized_chinese_manual_only_with_original_label(self):
        items = required_assets()[:-1]
        sanitized = asset(
            sync_releases.SANITIZED_CHINESE_MANUAL_NAME,
            label=sync_releases.CHINESE_MANUAL_NAME,
        )

        selected, skipped = sync_releases.select_public_assets(
            {"assets": items + [sanitized]},
            TAG,
        )

        self.assertIn(sync_releases.SANITIZED_CHINESE_MANUAL_NAME, selected)
        self.assertEqual([], skipped)
        self.assertEqual([], sync_releases.missing_public_asset_categories(selected, TAG))

        wrong_label = asset(
            sync_releases.SANITIZED_CHINESE_MANUAL_NAME,
            label="",
        )
        selected, skipped = sync_releases.select_public_assets(
            {"assets": items + [wrong_label]},
            TAG,
        )
        self.assertEqual([wrong_label], skipped)
        self.assertEqual(
            [sync_releases.CHINESE_MANUAL_CATEGORY],
            sync_releases.missing_public_asset_categories(selected, TAG),
        )

    def test_normalizes_legacy_mojibake_chinese_manual_label(self):
        items = required_assets()[:-1]
        legacy = asset(
            sync_releases.SANITIZED_CHINESE_MANUAL_NAME,
            label=sync_releases.LEGACY_MOJIBAKE_CHINESE_MANUAL_LABEL,
        )

        selected, skipped = sync_releases.select_public_assets(
            {"assets": items + [legacy]},
            TAG,
        )

        normalized = selected[sync_releases.SANITIZED_CHINESE_MANUAL_NAME]
        self.assertEqual(sync_releases.CHINESE_MANUAL_NAME, normalized["label"])
        self.assertEqual([], skipped)
        self.assertEqual([], sync_releases.missing_public_asset_categories(selected, TAG))

    def test_reports_each_missing_category(self):
        selected, _ = sync_releases.select_public_assets(
            {"assets": required_assets()[:2]},
            TAG,
        )

        self.assertEqual(
            [
                sync_releases.ENGLISH_MANUAL_CATEGORY,
                sync_releases.CHINESE_MANUAL_CATEGORY,
            ],
            sync_releases.missing_public_asset_categories(selected, TAG),
        )

    def test_rejects_duplicate_assets_in_one_category(self):
        duplicate_chinese_manuals = required_assets() + [
            asset(
                sync_releases.SANITIZED_CHINESE_MANUAL_NAME,
                label=sync_releases.CHINESE_MANUAL_NAME,
            )
        ]

        with self.assertRaisesRegex(RuntimeError, "duplicate assets for chinese_manual"):
            sync_releases.select_public_assets(
                {"assets": duplicate_chinese_manuals},
                TAG,
            )


class AssetIdentityTests(unittest.TestCase):
    def test_matching_digests_take_priority_over_size(self):
        source = asset("package.7z", size=100, digest="sha256:same")
        target = asset("package.7z", size=999, digest="sha256:same")

        self.assertTrue(sync_releases.assets_match(source, target))

    def test_changed_digest_requires_replacement(self):
        source = asset("package.7z", size=100, digest="sha256:source")
        target = asset("package.7z", size=100, digest="sha256:target")

        self.assertFalse(sync_releases.assets_match(source, target))

    def test_missing_digest_falls_back_to_size(self):
        source = asset("package.7z", size=100, digest=None)
        same_size = asset("package.7z", size=100, digest="sha256:target")
        different_size = asset("package.7z", size=101, digest="sha256:target")

        self.assertTrue(sync_releases.assets_match(source, same_size))
        self.assertFalse(sync_releases.assets_match(source, different_size))

    def test_mime_or_label_change_requires_replacement(self):
        source = asset(
            "package.tar.gz",
            label="Linux",
            content_type="application/x-gtar",
        )
        changed_mime = dict(source, content_type="application/x-tar")
        changed_label = dict(source, label="")

        self.assertFalse(sync_releases.assets_match(source, changed_mime))
        self.assertFalse(sync_releases.assets_match(source, changed_label))

    def test_upload_uses_source_content_type(self):
        client = sync_releases.GitHubClient("token")
        with mock.patch.object(client, "_send", return_value={}) as send:
            client.upload_asset(
                "https://uploads.github.test/releases/1/assets{?name,label}",
                "package.tar.gz",
                "Linux",
                "application/x-gtar",
                b"payload",
            )

        self.assertEqual("application/x-gtar", send.call_args.kwargs["headers"]["Content-Type"])


class SourceAssetDownloadTests(unittest.TestCase):
    def test_validates_size_and_sha256_before_returning_payloads(self):
        content = b"verified package bytes"
        digest = hashlib.sha256(content).hexdigest()
        source_asset = asset(
            "package.7z",
            size=len(content),
            digest=f"sha256:{digest}",
        )
        source_client = mock.Mock()
        source_client.download_asset.return_value = content

        payloads = sync_releases.download_and_validate_assets(
            source_client,
            {source_asset["name"]: source_asset},
        )

        self.assertEqual({"package.7z": content}, payloads)

    def test_rejects_size_mismatch(self):
        source_asset = asset("package.7z", size=99, digest=None)
        source_client = mock.Mock()
        source_client.download_asset.return_value = b"short"

        with self.assertRaisesRegex(RuntimeError, "size mismatch"):
            sync_releases.download_and_validate_assets(
                source_client,
                {source_asset["name"]: source_asset},
            )

    def test_rejects_sha256_mismatch(self):
        content = b"package"
        source_asset = asset(
            "package.7z",
            size=len(content),
            digest=f"sha256:{'0' * 64}",
        )
        source_client = mock.Mock()
        source_client.download_asset.return_value = content

        with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
            sync_releases.download_and_validate_assets(
                source_client,
                {source_asset["name"]: source_asset},
            )


class DraftTransactionTests(unittest.TestCase):
    def test_existing_public_release_is_drafted_synced_then_published(self):
        events: list[str] = []
        existing = {"id": 7, "tag_name": TAG, "draft": False, "assets": []}
        target_client = mock.Mock()

        def get(path, **_kwargs):
            events.append("find")
            self.assertIn("releases?per_page=100&page=1", path)
            return [existing]

        def patch(path, data):
            self.assertEqual("/repos/owner/mirror/releases/7", path)
            if data["draft"]:
                events.append("stage-draft")
                return dict(existing, draft=True)
            events.append("publish")
            return dict(existing, draft=False)

        target_client.get.side_effect = get
        target_client.patch.side_effect = patch

        with mock.patch.object(
            sync_releases,
            "sync_assets",
            side_effect=lambda *_args: events.append("sync-assets"),
        ):
            sync_releases.mirror_release(
                target_client,
                "owner/mirror",
                {"tag_name": TAG, "name": TAG, "body": "notes"},
                {item["name"]: item for item in required_assets()},
                {item["name"]: b"payload" for item in required_assets()},
            )

        self.assertEqual(["find", "stage-draft", "sync-assets", "publish"], events)

    def test_sync_failure_leaves_release_in_draft(self):
        existing = {"id": 7, "tag_name": TAG, "draft": False, "assets": []}
        target_client = mock.Mock()
        target_client.get.return_value = [existing]
        target_client.patch.return_value = dict(existing, draft=True)

        with mock.patch.object(
            sync_releases,
            "sync_assets",
            side_effect=RuntimeError("upload failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "upload failed"):
                sync_releases.mirror_release(
                    target_client,
                    "owner/mirror",
                    {"tag_name": TAG},
                    {},
                    {},
                )

        self.assertEqual(1, target_client.patch.call_count)
        self.assertTrue(target_client.patch.call_args.args[1]["draft"])


class GitHubClientRetryTests(unittest.TestCase):
    @staticmethod
    def http_error(status: int) -> sync_releases.error.HTTPError:
        return sync_releases.error.HTTPError(
            "https://api.github.test/resource",
            status,
            "test error",
            {},
            io.BytesIO(b"test error"),
        )

    def test_post_is_not_retried(self):
        client = sync_releases.GitHubClient("token")
        with mock.patch.object(
            sync_releases.request,
            "urlopen",
            side_effect=self.http_error(503),
        ) as urlopen:
            with self.assertRaises(sync_releases.GitHubAPIError):
                client.post("/repos/owner/repo/releases", {"draft": True})

        self.assertEqual(1, urlopen.call_count)

    def test_get_and_patch_retry_transient_failures(self):
        for method in ("get", "patch"):
            with self.subTest(method=method):
                client = sync_releases.GitHubClient("token")
                response = mock.MagicMock()
                response.__enter__.return_value.read.return_value = b'{"ok": true}'
                with (
                    mock.patch.object(
                        sync_releases.request,
                        "urlopen",
                        side_effect=[self.http_error(503), response],
                    ) as urlopen,
                    mock.patch.object(sync_releases.time, "sleep") as sleep,
                ):
                    if method == "get":
                        result = client.get("/repos/owner/repo/releases")
                    else:
                        result = client.patch("/repos/owner/repo/releases/1", {"draft": True})

                self.assertEqual({"ok": True}, result)
                self.assertEqual(2, urlopen.call_count)
                sleep.assert_called_once_with(1)

    def test_delete_treats_not_found_as_success(self):
        client = sync_releases.GitHubClient("token")
        with mock.patch.object(
            sync_releases.request,
            "urlopen",
            side_effect=self.http_error(404),
        ) as urlopen:
            result = client.delete("/repos/owner/repo/releases/assets/1")

        self.assertIsNone(result)
        self.assertEqual(1, urlopen.call_count)

    def test_create_422_recovers_draft_by_listing_releases(self):
        recovered = {"id": 9, "tag_name": TAG, "draft": True, "assets": []}
        target_client = mock.Mock()
        target_client.post.side_effect = sync_releases.GitHubAPIError(
            422,
            "POST",
            "https://api.github.test/releases",
            "already_exists",
        )
        target_client.get.return_value = [recovered]
        target_client.patch.return_value = recovered

        result = sync_releases.stage_target_release(
            target_client,
            "owner/mirror",
            {"tag_name": TAG},
            None,
        )

        self.assertEqual(recovered, result)
        target_client.post.assert_called_once()
        target_client.get.assert_called_once_with(
            "/repos/owner/mirror/releases?per_page=100&page=1"
        )
        self.assertTrue(target_client.patch.call_args.args[1]["draft"])


if __name__ == "__main__":
    unittest.main()
