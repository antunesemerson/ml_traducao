from __future__ import annotations

import os
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
