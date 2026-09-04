from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

from dashboard import backend


PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import db  # noqa: E402


class RegenerativeReviewContractsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.ensure_database(self.conn)
        self.conn.execute(
            """
            INSERT INTO source_segments (
              id, relative_path, source_line_number, source_key,
              spanish_text, english_text, old_text, is_active,
              first_indexed_at, last_indexed_at
            ) VALUES (42, 'events/test_l_spanish.yml', 1, 'test.key',
                      'texto español', 'English text', 'Texto antigo', 1,
                      '2026-09-01T00:00:00', '2026-09-01T00:00:00')
            """
        )
        self.conn.execute(
            """
            INSERT INTO output_segments (
              segment_id, relative_path, output_line_number,
              portuguese_text, last_indexed_at
            ) VALUES (42, 'events/test_l_spanish.yml', 1,
                      'Texto atual', '2026-09-01T00:00:00')
            """
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def _contract_item(self, evidence: dict[str, object]) -> dict[str, object]:
        return backend._attach_regenerative_review_contract(
            {"segment_id": 42},
            queue_type="discovery",
            item_key="family:test:segment:42",
            snapshot_id=7,
            evidence=evidence,
            decisions_by_evidence=backend._regenerative_review_lookup(
                self.conn,
                "discovery",
            ),
        )

    def test_exact_evidence_is_reused_but_changed_evidence_reopens(self) -> None:
        evidence = {
            "family_key": "test",
            "segment_id": 42,
            "candidate_hash": "candidate-a",
        }
        pending = self._contract_item(evidence)
        contract = pending["review_contract"]

        self.assertEqual(pending["review_status"], "pending")
        result = backend._record_regenerative_review_decision(
            self.conn,
            queue_type="discovery",
            item_key=contract["item_key"],
            segment_id=42,
            snapshot_id=contract["snapshot_id"],
            evidence_hash=contract["evidence_hash"],
            evidence=contract["evidence"],
            decision="supports_pattern",
            reason=None,
            reviewer="test_reviewer",
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["operational_writes"])
        self.assertTrue(result["learning"]["synced"])
        learned = self.conn.execute(
            """
            SELECT human_label, origin, learned_at
            FROM local_learning_candidates
            WHERE segment_id = 42
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        self.assertEqual(learned["human_label"], "semantic_error")
        self.assertEqual(learned["origin"], "regenerative_discovery_review")
        self.assertIsNone(learned["learned_at"])
        reviewed = self._contract_item(evidence)
        self.assertEqual(reviewed["review_status"], "reviewed")
        self.assertEqual(reviewed["review_decision"], "supports_pattern")

        changed = self._contract_item({**evidence, "candidate_hash": "candidate-b"})
        self.assertEqual(changed["review_status"], "pending")

    def test_redecision_preserves_history_and_only_latest_is_active(self) -> None:
        item = self._contract_item({"family_key": "test", "segment_id": 42})
        contract = item["review_contract"]
        common = {
            "queue_type": "discovery",
            "item_key": contract["item_key"],
            "segment_id": 42,
            "snapshot_id": contract["snapshot_id"],
            "evidence_hash": contract["evidence_hash"],
            "evidence": contract["evidence"],
            "reason": None,
            "reviewer": "test_reviewer",
        }
        backend._record_regenerative_review_decision(
            self.conn,
            decision="supports_pattern",
            **common,
        )
        backend._record_regenerative_review_decision(
            self.conn,
            decision="boundary_case",
            **common,
        )

        rows = self.conn.execute(
            """
            SELECT decision, is_active
            FROM ml_regenerative_review_decisions
            ORDER BY id
            """
        ).fetchall()
        self.assertEqual(
            [(row["decision"], row["is_active"]) for row in rows],
            [("supports_pattern", 0), ("boundary_case", 1)],
        )

    def test_confirmed_angular_quote_pattern_creates_a_token_safe_correction(self) -> None:
        self.conn.execute(
            "UPDATE output_segments SET portuguese_text = ? WHERE segment_id = 42",
            ("«Texto» [actor.Custom('ES_ElLa')]",),
        )
        self.conn.commit()
        item = self._contract_item(
            {
                "family_key": "quotes",
                "segment_id": 42,
                "issue_type": "spanish_angular_quotes",
                "candidate_hash": "quotes-a",
            }
        )
        contract = item["review_contract"]
        backend._record_regenerative_review_decision(
            self.conn,
            queue_type="discovery",
            item_key=contract["item_key"],
            segment_id=42,
            snapshot_id=contract["snapshot_id"],
            evidence_hash=contract["evidence_hash"],
            evidence=contract["evidence"],
            decision="supports_pattern",
            reason="Normalizar aspas para pt-BR",
            reviewer="test_reviewer",
        )
        learned = self.conn.execute(
            """
            SELECT human_label, suggested_text, corrected_text, token_status
            FROM local_learning_candidates
            WHERE segment_id = 42
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        self.assertEqual(learned["human_label"], "semantic_error")
        self.assertEqual(learned["suggested_text"], '"Texto" [actor.Custom(\'ES_ElLa\')]')
        self.assertEqual(learned["corrected_text"], learned["suggested_text"])
        self.assertEqual(learned["token_status"], "ok")

    def test_angular_quote_guidance_is_not_described_as_residual_spanish(self) -> None:
        guidance = backend._discovery_pattern_guidance(
            "spanish_angular_quotes",
            "gender_helper",
            "root",
        )

        self.assertEqual(guidance["title"], "Aspas angulares fora do padrão PT-BR")
        self.assertIn("trocar somente « »", guidance["decision_question"])
        self.assertNotIn("Espanhol residual", guidance["title"])

    def test_rejects_decision_outside_queue_contract(self) -> None:
        item = self._contract_item({"family_key": "test", "segment_id": 42})
        contract = item["review_contract"]
        with self.assertRaisesRegex(ValueError, "decision must be one of"):
            backend._record_regenerative_review_decision(
                self.conn,
                queue_type="discovery",
                item_key=contract["item_key"],
                segment_id=42,
                snapshot_id=contract["snapshot_id"],
                evidence_hash=contract["evidence_hash"],
                evidence=contract["evidence"],
                decision="candidate_valid",
                reason=None,
                reviewer="test_reviewer",
            )

    def test_repair_acceptance_requires_a_real_proposal_ready_change(self) -> None:
        evidence = {
            "segment_id": 42,
            "dry_run_lane": "blocked",
            "candidate_hash": "same",
            "output_hash": "same",
        }
        item = backend._attach_regenerative_review_contract(
            {"segment_id": 42},
            queue_type="repair",
            item_key="repair:segment:42",
            snapshot_id=7,
            evidence=evidence,
            decisions_by_evidence={},
        )
        contract = item["review_contract"]
        with self.assertRaisesRegex(ValueError, "proposal-ready repair"):
            backend._record_regenerative_review_decision(
                self.conn,
                queue_type="repair",
                item_key=contract["item_key"],
                segment_id=42,
                snapshot_id=contract["snapshot_id"],
                evidence_hash=contract["evidence_hash"],
                evidence=contract["evidence"],
                decision="candidate_valid",
                reason=None,
                reviewer="test_reviewer",
            )

    def test_needs_context_does_not_become_positive_training_evidence(self) -> None:
        self.assertIsNone(
            backend._regenerative_learning_label(
                "repair",
                "needs_context",
                {"issue_codes": ["spanish_residue_in_literal"]},
            )
        )

    def test_decision_invalidates_release_diff_review_cache_key(self) -> None:
        before = backend._release_diff_review_cache_key(self.conn, None)
        item = self._contract_item({"family_key": "test", "segment_id": 42})
        contract = item["review_contract"]

        backend._record_regenerative_review_decision(
            self.conn,
            queue_type="discovery",
            item_key=contract["item_key"],
            segment_id=42,
            snapshot_id=contract["snapshot_id"],
            evidence_hash=contract["evidence_hash"],
            evidence=contract["evidence"],
            decision="supports_pattern",
            reason=None,
            reviewer="test_reviewer",
        )

        after = backend._release_diff_review_cache_key(self.conn, None)
        self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
