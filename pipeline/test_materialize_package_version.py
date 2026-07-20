from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import materialize_package_version as materializer  # noqa: E402


STATE = {
    "id": 711,
    "active_score_run_id": 367,
    "candidate_score_run_id": 368,
    "pending_count": 0,
    "output_apply_pending_count": 0,
}
EPOCH = {
    "id": 4,
    "segment_state_run_id": 701,
    "old_score_run_id": 367,
    "output_score_run_id": 368,
    "status": "evaluated",
}


class MaterializePackageVersionTests(unittest.TestCase):
    def run_preflight(self, summary: dict) -> dict:
        with (
            mock.patch.object(materializer.versions, "latest_state", return_value=STATE),
            mock.patch.object(materializer, "_latest_epoch", return_value=EPOCH),
            mock.patch.object(
                materializer.versions.dashboard_backend,
                "_release_diff_review_payload",
                return_value={"summary": summary},
            ),
            mock.patch.object(
                materializer.versions,
                "package_tree_hash",
                side_effect=[("old-hash", 10), ("output-hash", 10)],
            ),
            mock.patch.object(
                materializer,
                "_latest_materialized_version",
                return_value={
                    "id": 3,
                    "version_number": 3,
                    "version_label": "Baseline materializada v3",
                    "package_hash": "parent-hash",
                    "file_count": 10,
                },
            ),
        ):
            return materializer._preflight(object(), Path("old"), Path("output"))

    def test_calibrated_raw_regressions_do_not_block_materialization(self) -> None:
        result = self.run_preflight(
            {
                "score_regressions": 68,
                "effective_package_score_regressions": 0,
                "needs_apply": 0,
                "changed_vs_old": 183,
            }
        )

        self.assertEqual(result["raw_score_regression_count"], 68)
        self.assertEqual(result["effective_score_regression_count"], 0)
        self.assertEqual(result["changed_count"], 183)
        self.assertTrue(result["eligible"])

    def test_identical_current_package_returns_no_delta_before_score_alignment(self) -> None:
        mismatched_state = {
            **STATE,
            "active_score_run_id": 353,
            "candidate_score_run_id": 383,
        }
        shared_epoch = {
            **EPOCH,
            "old_score_run_id": 383,
            "output_score_run_id": 383,
        }
        release_payload = mock.Mock()
        with (
            mock.patch.object(materializer.versions, "latest_state", return_value=mismatched_state),
            mock.patch.object(materializer, "_latest_epoch", return_value=shared_epoch),
            mock.patch.object(
                materializer.versions.dashboard_backend,
                "_release_diff_review_payload",
                release_payload,
            ),
            mock.patch.object(
                materializer.versions,
                "package_tree_hash",
                side_effect=[("same-hash", 10), ("same-hash", 10)],
            ),
            mock.patch.object(
                materializer,
                "_latest_materialized_version",
                return_value={
                    "id": 6,
                    "version_number": 6,
                    "version_label": "Baseline materializada v6",
                    "package_hash": "same-hash",
                    "file_count": 10,
                },
            ),
        ):
            result = materializer._preflight(object(), Path("old"), Path("output"))

        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "no_package_delta")
        self.assertEqual(result["current_version_number"], 6)
        self.assertEqual(result["changed_count"], 0)
        self.assertIn("já representa output/spanish", result["message"])
        release_payload.assert_not_called()

    def test_apply_rejects_no_delta_before_writing_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            old_root = Path(temporary_directory) / "old"
            output_root = Path(temporary_directory) / "output"
            old_root.mkdir()
            output_root.mkdir()
            connection = mock.MagicMock()
            max_version_cursor = mock.MagicMock()
            max_version_cursor.fetchone.return_value = (7,)
            parent_cursor = mock.MagicMock()
            parent_cursor.fetchone.return_value = {"id": 6}
            connection.execute.side_effect = [max_version_cursor, parent_cursor]
            connection_context = mock.MagicMock()
            connection_context.__enter__.return_value = connection
            connection_context.__exit__.return_value = False
            no_delta = {
                "state": STATE,
                "epoch": EPOCH,
                "old_hash": "same-hash",
                "output_hash": "same-hash",
                "output_files": 10,
                "changed_count": 0,
                "raw_score_regression_count": 0,
                "effective_score_regression_count": 0,
                "eligible": False,
                "reason": "no_package_delta",
                "message": "Baseline materializada v6 já representa output/spanish; não há alterações no pacote para materializar.",
                "current_version_id": 6,
                "current_version_number": 6,
                "current_version_label": "Baseline materializada v6",
            }
            with (
                mock.patch.object(materializer.db, "load_settings", return_value={
                    "spanish_traduzido_old": "old",
                    "output_spanish": "output",
                }),
                mock.patch.object(materializer.db, "project_path", side_effect=[old_root, output_root]),
                mock.patch.object(materializer.db, "connect", return_value=connection_context),
                mock.patch.object(materializer.db, "ensure_database"),
                mock.patch.object(materializer, "_preflight", return_value=no_delta),
                mock.patch.object(materializer.versions, "insert_version") as insert_version,
            ):
                with self.assertRaisesRegex(RuntimeError, "já representa output/spanish"):
                    materializer.materialize(apply=True)

        insert_version.assert_not_called()

    def test_effective_regressions_still_block_materialization(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "2 effective score regressions"):
            self.run_preflight(
                {
                    "score_regressions": 68,
                    "effective_package_score_regressions": 2,
                    "needs_apply": 0,
                }
            )

    def test_legacy_summary_falls_back_to_raw_regressions(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "3 effective score regressions"):
            self.run_preflight({"score_regressions": 3, "needs_apply": 0})

    def test_evaluation_next_action_uses_effective_regressions(self) -> None:
        payload = materializer.versions.dashboard_backend._evaluation_decision_payload(
            {
                "instrumented": True,
                "summary": {
                    "score_regressions": 68,
                    "effective_package_score_regressions": 0,
                    "needs_apply": 0,
                    "promotions_vs_old": 0,
                    "unhandled_by_network": 0,
                    "low_score": 0,
                },
                "score_regression_segments": [{"segment_id": 1}],
            },
            run_id="test",
        )

        self.assertEqual(payload["counts"]["regressions"], 68)
        self.assertEqual(payload["counts"]["effective_regressions"], 0)
        self.assertNotIn("review_score_regressions", payload["next_actions"])
        self.assertIn("candidate_clear_for_publication_gate", payload["next_actions"])


if __name__ == "__main__":
    unittest.main()
