from __future__ import annotations

import os
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest import mock

from dashboard import backend


class FullDashboardPayloadCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.sqlite"
        self.db_path.write_bytes(b"sqlite-placeholder")
        self.original_cache_file = backend.FULL_DASHBOARD_CACHE_FILE
        self.original_cache = backend.FULL_DASHBOARD_CACHE
        self.original_refreshing = backend.FULL_DASHBOARD_CACHE_REFRESHING
        backend.FULL_DASHBOARD_CACHE_FILE = Path(self.temp_dir.name) / "dashboard_full_cache.json"
        backend.FULL_DASHBOARD_CACHE = None
        backend.FULL_DASHBOARD_CACHE_REFRESHING = False

    def tearDown(self) -> None:
        backend.FULL_DASHBOARD_CACHE_FILE = self.original_cache_file
        backend.FULL_DASHBOARD_CACHE = self.original_cache
        backend.FULL_DASHBOARD_CACHE_REFRESHING = self.original_refreshing
        self.temp_dir.cleanup()

    def test_payload_is_built_once_then_reused_from_cache(self) -> None:
        built_payload = {"generatedAt": "test", "databasePath": "old", "section": {"value": 1}}
        with mock.patch.object(
            backend,
            "_build_full_dashboard_payload",
            return_value=built_payload,
        ) as builder:
            first = backend._dashboard_payload(self.db_path)
            second = backend._dashboard_payload(self.db_path)

        self.assertEqual(builder.call_count, 1)
        self.assertTrue(backend.FULL_DASHBOARD_CACHE_FILE.exists())
        self.assertEqual(first["databasePath"], str(self.db_path))
        self.assertEqual(second["section"], {"value": 1})
        self.assertFalse(second["analyticsCache"]["stale"])

    def test_stale_payload_is_returned_while_refresh_starts(self) -> None:
        backend._write_full_dashboard_cache(self.db_path, {"section": {"value": "cached"}})
        current_mtime = self.db_path.stat().st_mtime
        os.utime(self.db_path, (current_mtime + 10, current_mtime + 10))

        with mock.patch.object(
            backend,
            "_start_full_dashboard_cache_refresh",
            return_value=True,
        ) as refresh:
            payload = backend._dashboard_payload(self.db_path)

        refresh.assert_called_once_with(self.db_path)
        self.assertEqual(payload["section"], {"value": "cached"})
        self.assertTrue(payload["analyticsCache"]["stale"])
        self.assertTrue(payload["analyticsCache"]["refreshing"])

    def test_publication_preflight_response_does_not_duplicate_app_state(self) -> None:
        payload = {
            "ok": True,
            "publication_preflight": {"status": "ready"},
            "app_state": {"large": ["unused"] * 100},
        }

        compact = backend._compact_publication_preflight_payload(payload)

        self.assertEqual(
            compact,
            {"ok": True, "publication_preflight": {"status": "ready"}},
        )

    def test_publication_preflight_refreshes_authoritative_app_state(self) -> None:
        app_state = {
            "release": {
                "latest_segment_state_run_id": 779,
                "post_release": {"diff_review": {"summary": {}}},
                "safety": {
                    "publication_gate": {
                        "can_publish_after_full_production_now": True,
                        "blocking_reasons": [],
                    }
                },
            }
        }
        with mock.patch.object(
            backend,
            "_app_state_payload",
            return_value=app_state,
        ) as app_state_builder:
            payload = backend._publication_preflight_payload(self.db_path)

        app_state_builder.assert_called_once_with(self.db_path, force=True)
        self.assertEqual(
            payload["publication_preflight"]["segment_state_run_id"],
            779,
        )

    def test_glossary_policy_signature_changes_after_review(self) -> None:
        policy_db_path = Path(self.temp_dir.name) / "policy.sqlite"
        with closing(sqlite3.connect(policy_db_path)) as con:
            con.execute(
                """
                CREATE TABLE glossary_display_case_policies (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  glossary_key TEXT NOT NULL UNIQUE,
                  policy TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  reviewer TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            con.commit()
        before = backend._glossary_display_case_policy_signature(policy_db_path)

        with closing(sqlite3.connect(policy_db_path)) as con:
            con.execute(
                """
                INSERT INTO glossary_display_case_policies (
                  glossary_key, policy, reason, reviewer, updated_at
                ) VALUES (
                  'ROMAIOS_GLOSS', 'case_sensitive', 'nome próprio',
                  'dashboard_human_review', '2026-07-30T18:00:00'
                )
                """
            )
            con.commit()
        after = backend._glossary_display_case_policy_signature(policy_db_path)

        self.assertNotEqual(before, after)
        self.assertIn('"count":1', after)

    def test_release_diff_review_cache_serializes_concurrent_builders(self) -> None:
        cache_file = Path(self.temp_dir.name) / "release_diff_review_cache.json"
        original_file = backend.RELEASE_DIFF_REVIEW_CACHE_FILE
        original_cache = backend.RELEASE_DIFF_REVIEW_CACHE
        backend.RELEASE_DIFF_REVIEW_CACHE_FILE = cache_file
        backend.RELEASE_DIFF_REVIEW_CACHE = {"key": None, "payload": None}
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        barrier = threading.Barrier(6)
        expected = {"summary": {"pending": 0}}

        def load_payload() -> dict[str, object]:
            barrier.wait()
            return backend._release_diff_review_payload(connection, 10)

        try:
            with (
                mock.patch.object(backend, "_release_diff_review_cache_key", return_value="key-10"),
                mock.patch.object(
                    backend,
                    "_compute_release_diff_review_payload",
                    return_value=expected,
                ) as builder,
                ThreadPoolExecutor(max_workers=6) as executor,
            ):
                results = list(executor.map(lambda _: load_payload(), range(6)))

            self.assertEqual(results, [expected] * 6)
            self.assertEqual(builder.call_count, 1)
            self.assertEqual(json.loads(cache_file.read_text(encoding="utf-8"))["payload"], expected)
        finally:
            connection.close()
            backend.RELEASE_DIFF_REVIEW_CACHE_FILE = original_file
            backend.RELEASE_DIFF_REVIEW_CACHE = original_cache

    def test_release_diff_review_cache_disk_lock_is_non_fatal(self) -> None:
        cache_file = Path(self.temp_dir.name) / "release_diff_review_cache.json"
        original_file = backend.RELEASE_DIFF_REVIEW_CACHE_FILE
        original_cache = backend.RELEASE_DIFF_REVIEW_CACHE
        backend.RELEASE_DIFF_REVIEW_CACHE_FILE = cache_file
        backend.RELEASE_DIFF_REVIEW_CACHE = {"key": None, "payload": None}
        connection = sqlite3.connect(":memory:")
        expected = {"summary": {"pending": 0}}

        try:
            with (
                mock.patch.object(backend, "_release_diff_review_cache_key", return_value="key-10"),
                mock.patch.object(
                    backend,
                    "_compute_release_diff_review_payload",
                    return_value=expected,
                ),
                mock.patch.object(backend.os, "replace", side_effect=PermissionError("locked")),
                mock.patch.object(backend.time, "sleep"),
            ):
                result = backend._release_diff_review_payload(connection, 10)

            self.assertEqual(result, expected)
            self.assertEqual(backend.RELEASE_DIFF_REVIEW_CACHE["payload"], expected)
        finally:
            connection.close()
            backend.RELEASE_DIFF_REVIEW_CACHE_FILE = original_file
            backend.RELEASE_DIFF_REVIEW_CACHE = original_cache

    def test_materialized_package_version_invalidates_release_diff_cache_key(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(
                """
                CREATE TABLE package_versions (
                  id INTEGER PRIMARY KEY,
                  version_number INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  package_hash TEXT,
                  frozen_at TEXT
                )
                """
            )
            before = backend._release_diff_review_cache_key(connection, None)

            connection.execute(
                """
                INSERT INTO package_versions (
                  id, version_number, status, package_hash, frozen_at
                ) VALUES (13, 13, 'materialized', 'hash-v13', '2026-09-04T21:34:35Z')
                """
            )
            connection.commit()
            after = backend._release_diff_review_cache_key(connection, None)

            self.assertNotEqual(before, after)
            self.assertIn('"materialized_package_version_id":13', after)
            self.assertIn('"materialized_package_hash":"hash-v13"', after)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
