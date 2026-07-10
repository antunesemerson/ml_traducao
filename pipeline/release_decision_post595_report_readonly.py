from __future__ import annotations

from pathlib import Path

import release_decision_post594_report_readonly as base


base.SOURCE = "release_decision_post595_report_readonly_v1"
base.DEFAULT_READINESS_SUMMARY = Path("reports/20260704_112936_579169_release_readiness_post544_diagnostic_summary.json")
base.DEFAULT_READINESS_JSONL = Path("reports/20260704_112936_579169_release_readiness_post544_diagnostic.jsonl")


if __name__ == "__main__":
    base.main()
