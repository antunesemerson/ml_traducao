from __future__ import annotations

import sys
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from quality_pairwise_monotonic_gate import summarize_records  # noqa: E402


class PairwiseMonotonicGateTests(unittest.TestCase):
    def test_summary_does_not_require_report_artifacts(self) -> None:
        records = [
            {"ready": True, "block_reasons": []},
            {"ready": False, "block_reasons": ["token_integrity_ok"]},
        ]

        summary = summarize_records(records, 744, "test_evidence", apply=True)

        self.assertEqual(summary["ready_count"], 1)
        self.assertEqual(summary["blocked_count"], 1)
        self.assertEqual(summary["block_reason_counts"], {"token_integrity_ok": 1})
        self.assertEqual(summary["artifacts"], {})


if __name__ == "__main__":
    unittest.main()
