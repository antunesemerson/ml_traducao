from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from index_source import Segment, upsert_source_segment


class SourceSegmentIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(
            """
            CREATE TABLE source_segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                relative_path TEXT NOT NULL,
                source_line_number INTEGER NOT NULL,
                source_key TEXT NOT NULL,
                version_index TEXT,
                spanish_text TEXT,
                english_text TEXT,
                old_text TEXT,
                spanish_raw_line TEXT,
                english_raw_line TEXT,
                old_raw_line TEXT,
                spanish_hash TEXT,
                english_hash TEXT,
                old_hash TEXT,
                has_english INTEGER NOT NULL DEFAULT 0,
                has_old INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                first_indexed_at TEXT NOT NULL,
                last_indexed_at TEXT NOT NULL,
                UNIQUE(relative_path, source_line_number, source_key)
            );
            """
        )

    def tearDown(self) -> None:
        self.con.close()

    @staticmethod
    def segment(line_number: int, text: str = "Texto") -> Segment:
        return Segment(
            relative_path="events/update_l_spanish.yml",
            line_number=line_number,
            key="update.event.desc",
            version_index="0",
            text=text,
            raw_line=f' update.event.desc:0 "{text}"',
        )

    def test_line_shift_preserves_segment_id_and_history_anchor(self) -> None:
        original_id = upsert_source_segment(self.con, self.segment(10), None, None)
        self.con.execute("UPDATE source_segments SET is_active = 0")

        shifted_id = upsert_source_segment(
            self.con,
            self.segment(14, "Texto atualizado"),
            None,
            None,
        )

        row = self.con.execute(
            "SELECT id, source_line_number, spanish_text, is_active FROM source_segments"
        ).fetchone()
        self.assertEqual(shifted_id, original_id)
        self.assertEqual(int(row["id"]), original_id)
        self.assertEqual(int(row["source_line_number"]), 14)
        self.assertEqual(row["spanish_text"], "Texto atualizado")
        self.assertEqual(int(row["is_active"]), 1)


if __name__ == "__main__":
    unittest.main()
