from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import source_tree_snapshot


class SourceTreeSnapshotTests(unittest.TestCase):
    def test_unchanged_file_reuses_hash_from_metadata_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            path = root / "events_l_spanish.yml"
            path.write_text('l_spanish:\n key:0 "Texto"\n', encoding="utf-8")

            with mock.patch.object(source_tree_snapshot.db, "PROJECT_ROOT", root.parent):
                _, initial_files = source_tree_snapshot.inspect_tree(root)
            cached = {initial_files[0]["relative_path"]: initial_files[0]}

            with mock.patch.object(source_tree_snapshot.db, "PROJECT_ROOT", root.parent), mock.patch.object(
                source_tree_snapshot.db, "file_hash",
                side_effect=AssertionError("unchanged file should not be reread"),
            ):
                summary, repeated_files = source_tree_snapshot.inspect_tree(root, cached)

            self.assertEqual(summary["file_count"], 1)
            self.assertEqual(repeated_files[0]["file_hash"], initial_files[0]["file_hash"])


if __name__ == "__main__":
    unittest.main()
