from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from ml_score_segments import (  # noqa: E402
    accumulate_run_summary,
    empty_run_summary,
    fetch_segments,
    iter_segment_batches,
    load_protected_token_counts,
    load_run_summary,
)


class MlScoreStreamingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE source_segments (
              id INTEGER PRIMARY KEY,
              relative_path TEXT,
              source_key TEXT,
              source_line_number INTEGER,
              english_text TEXT,
              spanish_text TEXT,
              old_text TEXT,
              has_english INTEGER,
              has_old INTEGER,
              is_active INTEGER
            );
            CREATE TABLE output_segments (
              segment_id INTEGER PRIMARY KEY,
              portuguese_text TEXT
            );
            CREATE TABLE segment_confirmations (
              segment_id INTEGER PRIMARY KEY,
              confirmation_level TEXT,
              confirmed_text TEXT,
              confirmation_source TEXT,
              confirmation_label TEXT,
              locked INTEGER,
              confidence_score REAL
            );
            CREATE TABLE protected_tokens (
              id INTEGER PRIMARY KEY,
              segment_id INTEGER
            );
            CREATE TABLE ml_score_items (
              run_id INTEGER,
              final_action TEXT,
              model_action TEXT,
              deterministic_blocked INTEGER
            );
            """
        )
        for segment_id in range(1, 7):
            self.conn.execute(
                "INSERT INTO source_segments VALUES (?, 'events/test.yml', ?, ?, 'English', 'Spanish', 'Old', 1, 1, 1)",
                (segment_id, f"key_{segment_id}", segment_id),
            )
            self.conn.execute(
                "INSERT INTO output_segments VALUES (?, ?)",
                (segment_id, "" if segment_id == 6 else f"Texto {segment_id}"),
            )
        self.conn.executemany(
            "INSERT INTO segment_confirmations VALUES (?, ?, ?, 'test', 'correct', ?, ?)",
            [
                (1, "auto_confirmed", "Texto 1", 0, 0.2),
                (2, "auto_confirmed", "Texto 2", 0, 0.1),
                (4, "human_confirmed", "Texto 4", 0, 0.3),
                (5, "human_confirmed", "Texto 5", 1, 0.1),
            ],
        )
        self.conn.executemany(
            "INSERT INTO protected_tokens VALUES (?, ?)",
            [(1, 2), (2, 2), (3, 5)],
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_streaming_batches_match_the_existing_fetch_contract(self) -> None:
        token_counts = load_protected_token_counts(self.conn)
        expected = fetch_segments(
            self.conn,
            limit=None,
            path_like=None,
            include_locked=True,
            candidate_text_source="output",
            pairwise_evidence_by_candidate={},
            protected_token_counts_by_segment=token_counts,
        )
        statements: list[str] = []
        self.conn.set_trace_callback(statements.append)

        batches = list(
            iter_segment_batches(
                self.conn,
                batch_size=2,
                limit=None,
                path_like=None,
                include_locked=True,
                candidate_text_source="output",
                pairwise_evidence_by_candidate={},
                protected_token_counts_by_segment=token_counts,
            )
        )
        self.conn.set_trace_callback(None)
        actual = [row for batch in batches for row in batch]

        self.assertEqual([len(batch) for batch in batches], [2, 2, 1])
        self.assertEqual(actual, expected)
        self.assertEqual([row["segment_id"] for row in actual], [2, 1, 3, 5, 4])
        self.assertEqual(actual[0]["token_count"], 2)
        self.assertEqual(actual[3]["token_count"], 1)
        source_selects = [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("SELECT")
            and "FROM source_segments" in statement
        ]
        self.assertEqual(len(source_selects), 1)

    def test_streaming_resume_offset_and_limit_preserve_order(self) -> None:
        rows = [
            row
            for batch in iter_segment_batches(
                self.conn,
                batch_size=1,
                limit=2,
                path_like=None,
                include_locked=True,
                candidate_text_source="output",
                offset=3,
                pairwise_evidence_by_candidate={},
                protected_token_counts_by_segment=load_protected_token_counts(self.conn),
            )
            for row in batch
        ]

        self.assertEqual([row["segment_id"] for row in rows], [5, 4])

    def test_streaming_reader_survives_writer_commits_in_wal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "streaming.sqlite"
            seed = sqlite3.connect(db_path)
            self.conn.backup(seed)
            seed.execute("PRAGMA journal_mode = WAL")
            seed.execute("CREATE TABLE score_sink (segment_id INTEGER PRIMARY KEY)")
            seed.commit()
            seed.close()
            reader = sqlite3.connect(db_path)
            reader.row_factory = sqlite3.Row
            writer = sqlite3.connect(db_path)
            try:
                batches = iter_segment_batches(
                    reader,
                    batch_size=2,
                    limit=None,
                    path_like=None,
                    include_locked=True,
                    candidate_text_source="output",
                    pairwise_evidence_by_candidate={},
                    protected_token_counts_by_segment=load_protected_token_counts(reader),
                )
                streamed_ids: list[int] = []
                for batch in batches:
                    ids = [int(row["segment_id"]) for row in batch]
                    streamed_ids.extend(ids)
                    writer.executemany(
                        "INSERT INTO score_sink VALUES (?)",
                        [(segment_id,) for segment_id in ids],
                    )
                    writer.commit()
            finally:
                reader.close()
                writer.close()

        self.assertEqual(streamed_ids, [2, 1, 3, 5, 4])

    def test_incremental_run_summary_matches_action_transitions(self) -> None:
        summary = empty_run_summary()
        items = [
            {"model_action": "auto_safe", "final_action": "auto_safe", "deterministic_blocked": 0},
            {"model_action": "needs_human", "final_action": "auto_safe", "deterministic_blocked": 0},
            {"model_action": "auto_safe", "final_action": "needs_human", "deterministic_blocked": 1},
            {"model_action": "needs_autofix", "final_action": "needs_autofix", "deterministic_blocked": 1},
        ]

        accumulate_run_summary(summary, items[:2])
        accumulate_run_summary(summary, items[2:])

        self.assertEqual(summary["final_counts"]["auto_safe"], 2)
        self.assertEqual(summary["model_counts"]["auto_safe"], 2)
        self.assertEqual(summary["deterministic_blocks"], 2)
        self.assertEqual(summary["model_direct_auto_safe_count"], 1)
        self.assertEqual(summary["deterministic_promoted_auto_safe_count"], 1)
        self.assertEqual(summary["deterministic_demoted_auto_safe_count"], 1)

        self.conn.executemany(
            "INSERT INTO ml_score_items VALUES (77, ?, ?, ?)",
            [
                (item["final_action"], item["model_action"], item["deterministic_blocked"])
                for item in items
            ],
        )
        loaded = load_run_summary(self.conn, 77)
        self.assertEqual(loaded, summary)


if __name__ == "__main__":
    unittest.main()
