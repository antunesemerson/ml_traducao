from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import ml_promote_model  # noqa: E402


class ModelPromotionDatabaseHoldoutTests(unittest.TestCase):
    def test_holdout_metrics_are_loaded_from_database_without_report_file(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE ml_holdout_eval_runs (
              id INTEGER PRIMARY KEY,
              dataset_run_id INTEGER,
              predicted_safe_count INTEGER,
              false_safe_count INTEGER,
              false_safe_rate REAL,
              safe_precision REAL,
              safe_recall REAL,
              accuracy REAL,
              macro_f1 REAL,
              report_path TEXT,
              finished_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO ml_holdout_eval_runs VALUES
              (4, 12, 50, 0, 0.0, 1.0, 0.8, 0.9, 0.88,
               'reports/deleted_ml_holdout_eval.txt', '2026-07-19T00:00:00Z')
            """
        )

        metrics = ml_promote_model.latest_holdout_metrics_for_dataset(conn, 12)

        self.assertEqual(metrics["holdout_run_id"], 4)
        self.assertEqual(metrics["predicted_safe"], 50)
        self.assertEqual(metrics["false_safe"], 0)
        self.assertEqual(metrics["metrics_source"], "ml_holdout_eval_runs")
        conn.close()


if __name__ == "__main__":
    unittest.main()
