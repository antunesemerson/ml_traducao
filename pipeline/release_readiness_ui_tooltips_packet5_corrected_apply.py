from __future__ import annotations

from pathlib import Path

import release_readiness_ui_tooltips_packet2_corrected_apply as base


base.SOURCE = "release_readiness_ui_tooltips_packet5_corrected_apply_v1"
base.PREVIEW_JSONL = Path("reports/20260703_140550_343219_release_readiness_ui_tooltips_packet4_corrected_preview.jsonl")
base.PREVIEW_SUMMARY = Path("reports/20260703_140550_343219_release_readiness_ui_tooltips_packet4_corrected_preview_summary.json")
base.CONFIRMATION_SOURCE = "codex_release_readiness_human_review"
base.CONFIRMATION_LABEL = "ui_tooltips_packet5_corrected"
base.EXPECTED_COUNT = 8
base.OUTPUT_SLUG = "release_readiness_ui_tooltips_packet5_corrected_apply"
base.BACKUP_SLUG = "release_readiness_ui_tooltips_packet5_corrected"
base.ALLOWED_ISSUE_CLASSES = {"resolved_by_corrected_text", "unrelated_or_superseded"}


if __name__ == "__main__":
    base.main()
