from __future__ import annotations

import hashlib
import sqlite3
import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from quality_utf8_mojibake_human_review import (  # noqa: E402
    EVIDENCE_TYPE,
    REVIEW_LABEL_TAG,
    REVIEW_SOURCE_TAG,
    apply_reviews,
    collect_reviews,
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def make_connection(*, second_stale: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE ml_pairwise_quality_runs (
          id INTEGER PRIMARY KEY,
          evidence_type TEXT,
          finished_at TEXT
        );
        CREATE TABLE ml_pairwise_quality_evidence (
          id INTEGER PRIMARY KEY,
          last_run_id INTEGER,
          segment_id INTEGER,
          relative_path TEXT,
          source_key TEXT,
          baseline_text TEXT,
          candidate_text TEXT,
          baseline_hash TEXT,
          candidate_hash TEXT,
          evidence_type TEXT,
          preference_label TEXT,
          training_target TEXT,
          recommended_route TEXT,
          token_integrity_ok INTEGER,
          post_validation_clean INTEGER,
          blockers_json TEXT
        );
        CREATE TABLE source_segments (
          id INTEGER PRIMARY KEY,
          relative_path TEXT,
          source_key TEXT,
          spanish_text TEXT,
          english_text TEXT
        );
        CREATE TABLE output_segments (
          segment_id INTEGER PRIMARY KEY,
          portuguese_text TEXT
        );
        CREATE TABLE segment_confirmations (
          id INTEGER PRIMARY KEY,
          segment_id INTEGER,
          confirmation_level TEXT,
          confirmed_text TEXT,
          confirmation_source TEXT,
          confirmation_label TEXT,
          locked INTEGER,
          confidence_score REAL,
          reviewer TEXT,
          confirmed_at TEXT,
          updated_at TEXT
        );
        INSERT INTO ml_pairwise_quality_runs VALUES (18, 'deterministic_utf8_mojibake_repair', '2026-07-21T00:00:00Z');
        """
    )
    values = [
        (101, "A decisÃ£o vencerÃ¡.", "A decisão vencerá."),
        (102, "A Ã¡guia caÃ§adora.", "A águia caçadora."),
    ]
    for position, (segment_id, baseline, candidate) in enumerate(values, start=1):
        relative_path = f"events/{segment_id}_l_spanish.yml"
        source_key = f"key_{segment_id}"
        conn.execute(
            "INSERT INTO source_segments VALUES (?, ?, ?, 'Fuente', 'Source')",
            (segment_id, relative_path, source_key),
        )
        output = "texto concorrente" if second_stale and segment_id == 102 else baseline
        conn.execute("INSERT INTO output_segments VALUES (?, ?)", (segment_id, output))
        conn.execute(
            "INSERT INTO segment_confirmations VALUES (?, ?, 'human_confirmed', ?, 'codex_review', 'prior', 1, 1.0, 'codex', 'old', 'old')",
            (position, segment_id, baseline),
        )
        conn.execute(
            """
            INSERT INTO ml_pairwise_quality_evidence VALUES (
              ?, 18, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate_preferred',
              'pairwise_preference_only', 'human_unlock_review', 1, 1, '[]'
            )
            """,
            (
                position,
                segment_id,
                relative_path,
                source_key,
                baseline,
                candidate,
                digest(baseline),
                digest(candidate),
                EVIDENCE_TYPE,
            ),
        )
    conn.commit()
    return conn


class QualityUtf8MojibakeHumanReviewTests(unittest.TestCase):
    def test_collect_reviews_accepts_exact_guarded_rows(self) -> None:
        conn = make_connection()

        run_id, reviews = collect_reviews(conn, expected_count=2)

        self.assertEqual(run_id, 18)
        self.assertEqual([review["status"] for review in reviews], ["ready", "ready"])
        self.assertEqual([review["repair_count"] for review in reviews], [2, 2])
        conn.close()

    def test_apply_updates_confirmations_but_preserves_locks_and_output(self) -> None:
        conn = make_connection()

        _, reviews, applied = apply_reviews(conn, expected_count=2)

        self.assertEqual(applied, 2)
        rows = conn.execute(
            """
            SELECT confirmation.confirmed_text, confirmation.confirmation_source,
                   confirmation.confirmation_label, confirmation.locked,
                   output.portuguese_text AS output_text
            FROM segment_confirmations confirmation
            JOIN output_segments output ON output.segment_id = confirmation.segment_id
            ORDER BY confirmation.segment_id
            """
        ).fetchall()
        self.assertEqual(rows[0]["confirmed_text"], reviews[0]["candidate_text"])
        self.assertEqual(rows[1]["confirmed_text"], reviews[1]["candidate_text"])
        self.assertEqual([row["locked"] for row in rows], [1, 1])
        self.assertEqual(rows[0]["output_text"], reviews[0]["baseline_text"])
        self.assertIn(REVIEW_SOURCE_TAG, rows[0]["confirmation_source"])
        self.assertIn(REVIEW_LABEL_TAG, rows[0]["confirmation_label"])
        conn.close()

    def test_stale_output_blocks_the_whole_transaction(self) -> None:
        conn = make_connection(second_stale=True)

        with self.assertRaisesRegex(RuntimeError, "stale_output_text"):
            apply_reviews(conn, expected_count=2)

        confirmations = conn.execute(
            "SELECT confirmed_text FROM segment_confirmations ORDER BY segment_id"
        ).fetchall()
        self.assertEqual(confirmations[0]["confirmed_text"], "A decisÃ£o vencerÃ¡.")
        self.assertEqual(confirmations[1]["confirmed_text"], "A Ã¡guia caÃ§adora.")
        conn.close()


if __name__ == "__main__":
    unittest.main()
