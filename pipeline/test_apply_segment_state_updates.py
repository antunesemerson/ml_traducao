from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from apply_segment_state_updates import (  # noqa: E402
    pairwise_intentional_elision_authorized,
    sha256_text,
    validate_candidate,
)
from quality_spanish_dynamic_literal_authorization import (  # noqa: E402
    PAIRWISE_ELISION_CONFIRMATION_LABEL,
    PAIRWISE_ELISION_CONFIRMATION_SOURCE,
    PAIRWISE_ELISION_EVIDENCE_TYPE,
    PAIRWISE_ELISION_SOURCE,
)
from quality_redundant_select_cstring_authorization import (  # noqa: E402
    PAIRWISE_ELISION_CONFIRMATION_LABEL as REDUNDANT_SELECT_CONFIRMATION_LABEL,
    PAIRWISE_ELISION_EVIDENCE_TYPE as REDUNDANT_SELECT_EVIDENCE_TYPE,
    PAIRWISE_ELISION_SOURCE as REDUNDANT_SELECT_SOURCE,
)


class ApplySegmentStateUpdatesTests(unittest.TestCase):
    def evidence_row(self, baseline: str, candidate: str) -> dict[str, object]:
        return {
            "pairwise_evidence_id": 841,
            "confirmation_source": PAIRWISE_ELISION_CONFIRMATION_SOURCE,
            "confirmation_label": PAIRWISE_ELISION_CONFIRMATION_LABEL,
            "pairwise_evidence_type": PAIRWISE_ELISION_EVIDENCE_TYPE,
            "pairwise_baseline_hash": sha256_text(baseline),
            "pairwise_candidate_hash": sha256_text(candidate),
            "pairwise_token_integrity_ok": 1,
            "pairwise_post_validation_clean": 1,
            "pairwise_training_eligible": 1,
            "pairwise_promotion_eligible": 1,
            "pairwise_source_metadata_json": json.dumps(
                {
                    "source": PAIRWISE_ELISION_SOURCE,
                    "source_token_status": "intentional_elision",
                }
            ),
        }

    def test_authorizes_exact_evidence_backed_intentional_elision(self) -> None:
        baseline = (
            "[actor.GetName] "
            "[Select_CString(actor.IsLocalPlayer,'ganaste','ganó')] adquiriu ouro"
        )
        candidate = "[actor.GetName] adquiriu ouro"
        row = self.evidence_row(baseline, candidate)

        self.assertTrue(pairwise_intentional_elision_authorized(row, baseline, candidate))

    def test_rejects_stale_hash_or_unrelated_token_loss(self) -> None:
        baseline = (
            "[actor.GetName] "
            "[Select_CString(actor.IsLocalPlayer,'ganaste','ganó')] adquiriu ouro"
        )
        candidate = "[actor.GetName] adquiriu ouro"
        row = self.evidence_row(baseline, candidate)

        self.assertFalse(
            pairwise_intentional_elision_authorized(
                row,
                baseline + ".",
                candidate,
            )
        )
        self.assertFalse(
            pairwise_intentional_elision_authorized(
                row,
                baseline,
                "adquiriu ouro",
            )
        )

    def test_authorizes_exact_redundant_select_cstring_elision(self) -> None:
        baseline = (
            "[actor.GetName] "
            "[Select_CString(actor.IsLocalPlayer, 'sua', 'sua')] corte"
        )
        candidate = "[actor.GetName] sua corte"
        row = {
            "pairwise_evidence_id": 1370,
            "confirmation_source": PAIRWISE_ELISION_CONFIRMATION_SOURCE,
            "confirmation_label": REDUNDANT_SELECT_CONFIRMATION_LABEL,
            "pairwise_evidence_type": REDUNDANT_SELECT_EVIDENCE_TYPE,
            "pairwise_baseline_hash": sha256_text(baseline),
            "pairwise_candidate_hash": sha256_text(candidate),
            "pairwise_token_integrity_ok": 1,
            "pairwise_post_validation_clean": 1,
            "pairwise_training_eligible": 1,
            "pairwise_promotion_eligible": 1,
            "pairwise_source_metadata_json": json.dumps(
                {
                    "source": REDUNDANT_SELECT_SOURCE,
                    "lane": "pairwise_evidence_eligible",
                    "token_integrity_mode": "intentional_exact_select_elision",
                    "blockers": [],
                }
            ),
        }

        self.assertTrue(
            pairwise_intentional_elision_authorized(
                row,
                baseline,
                candidate,
            )
        )
        self.assertFalse(
            pairwise_intentional_elision_authorized(
                row,
                baseline,
                "[actor.GetName] corte",
            )
        )

    def test_validate_candidate_exposes_audited_ready_status(self) -> None:
        baseline = (
            "[actor.GetName] "
            "[Select_CString(actor.IsLocalPlayer,'ganaste','ganó')] adquiriu ouro"
        )
        candidate = "[actor.GetName] adquiriu ouro"
        row = {
            **self.evidence_row(baseline, candidate),
            "confirmed_text": candidate,
            "current_output_text": baseline,
            "spanish_text": baseline,
            "locked": 0,
            "review_state": "auto_confirmed",
            "output_line_number": 1,
            "relative_path": "test_l_spanish.yml",
            "token_policy_decision_id": None,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / row["relative_path"]).write_text(
                f'test_key:0 "{baseline}"\n',
                encoding="utf-8-sig",
            )

            status, current_line, new_line = validate_candidate(
                row,
                root,
                allow_locked_token_override=False,
                require_token_policy_decision=False,
                allow_token_policy_decision=False,
                include_intentional_blank=False,
            )

        self.assertEqual(status, "ready_pairwise_intentional_elision")
        self.assertIsNotNone(current_line)
        self.assertIn(candidate, new_line or "")


if __name__ == "__main__":
    unittest.main()
