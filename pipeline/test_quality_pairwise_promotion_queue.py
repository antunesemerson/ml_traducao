from __future__ import annotations

import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from quality_pairwise_promotion_queue import summarize_records  # noqa: E402


class PairwisePromotionQueueTests(unittest.TestCase):
    def test_summary_does_not_require_report_artifacts(self) -> None:
        records = [
            {"ready": True, "already_queued": False, "block_reasons": []},
            {"ready": True, "already_queued": True, "block_reasons": []},
            {"ready": False, "already_queued": False, "block_reasons": ["token_integrity_ok"]},
        ]

        summary = summarize_records(records, 745, "test_evidence", apply=True)

        self.assertEqual(summary["ready_count"], 2)
        self.assertEqual(summary["newly_queued_count"], 1)
        self.assertEqual(summary["already_queued_count"], 1)
        self.assertEqual(summary["blocked_count"], 1)
        self.assertEqual(summary["artifacts"], {})


if __name__ == "__main__":
    unittest.main()
