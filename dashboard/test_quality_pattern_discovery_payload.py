from __future__ import annotations

import json
import sqlite3
import unittest

from dashboard import backend


class QualityPatternDiscoveryPayloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE source_segments (
              id INTEGER PRIMARY KEY,
              relative_path TEXT,
              source_key TEXT,
              english_text TEXT,
              spanish_text TEXT,
              old_text TEXT
            );
            CREATE TABLE output_segments (
              segment_id INTEGER PRIMARY KEY,
              portuguese_text TEXT
            );
            CREATE TABLE ml_score_items (
              run_id INTEGER,
              segment_id INTEGER,
              candidate_text TEXT
            );
            CREATE TABLE ml_quality_pattern_discovery_runs (
              id INTEGER PRIMARY KEY,
              run_key TEXT,
              rule_version TEXT,
              quality_epoch_id INTEGER,
              score_run_id INTEGER,
              low_score_threshold REAL,
              active_segment_count INTEGER,
              evidence_segment_count INTEGER,
              family_count INTEGER,
              new_family_count INTEGER,
              recurring_family_count INTEGER,
              covered_family_count INTEGER,
              actionable_family_count INTEGER,
              ignored_score_only_count INTEGER,
              status TEXT,
              summary_json TEXT,
              finished_at TEXT,
              updated_at TEXT
            );
            CREATE TABLE ml_quality_pattern_families (
              id INTEGER PRIMARY KEY,
              family_key TEXT,
              evidence_kind TEXT,
              issue_type TEXT,
              token_context TEXT,
              file_family TEXT,
              text_relation TEXT,
              provider_id TEXT,
              evidence_type TEXT,
              observation_count INTEGER
            );
            CREATE TABLE ml_quality_pattern_observations (
              run_id INTEGER,
              family_id INTEGER,
              status TEXT,
              segment_count INTEGER,
              occurrence_count INTEGER,
              low_score_count INTEGER,
              low_score_share REAL,
              active_package_share REAL,
              average_score REAL,
              minimum_score REAL,
              maximum_score REAL,
              severity REAL,
              confidence REAL,
              novelty REAL,
              reach REAL,
              priority REAL,
              samples_json TEXT,
              metrics_json TEXT
            );
            CREATE TABLE ml_regenerative_review_decisions (
              id INTEGER PRIMARY KEY,
              queue_type TEXT,
              item_key TEXT,
              segment_id INTEGER,
              snapshot_id INTEGER,
              evidence_hash TEXT,
              decision TEXT,
              reason TEXT,
              reviewer TEXT,
              contract_version TEXT,
              is_active INTEGER,
              created_at TEXT,
              updated_at TEXT
            );
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_existing_truncated_sample_is_rehydrated_from_score_run(self) -> None:
        full_candidate = "Texto integral " + ("preservado " * 80)
        truncated_candidate = full_candidate[:500]
        self.conn.execute(
            "INSERT INTO source_segments VALUES (1, 'events/test.yml', 'test.key', 'English', 'Español', 'Antigo')"
        )
        self.conn.execute("INSERT INTO output_segments VALUES (1, ?)", (full_candidate,))
        self.conn.execute("INSERT INTO ml_score_items VALUES (10, 1, ?)", (full_candidate,))
        self.conn.execute(
            """
            INSERT INTO ml_quality_pattern_discovery_runs VALUES (
              7, 'run-7', 'v2', NULL, 10, 0.5, 1, 1, 1, 1, 0, 0, 1, 0,
              'completed', '{}', '2026-08-26T00:00:00Z', '2026-08-26T00:00:00Z'
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO ml_quality_pattern_families VALUES (
              3, 'family-3', 'explicit_issue', 'test_issue', 'plain_text', 'events',
              'distinct_candidate', NULL, NULL, 1
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO ml_quality_pattern_observations VALUES (
              7, 3, 'new_candidate', 1, 1, 1, 1.0, 1.0, 0.1, 0.1, 0.1,
              1.0, 0.8, 1.0, 0.5, 80.0, ?, '{}'
            )
            """,
            (json.dumps([{"segment_id": 1, "candidate_text": truncated_candidate}]),),
        )
        self.conn.commit()

        payload = backend._quality_pattern_discovery_payload(self.conn)

        sample = payload["families"][0]["samples"][0]
        self.assertEqual(sample["candidate_text"], full_candidate)
        self.assertEqual(sample["output_text"], full_candidate)

    def test_confirmed_spanish_pattern_is_routed_to_specific_correction_lane(self) -> None:
        self.conn.execute(
            "INSERT INTO source_segments VALUES (1, 'events/test.yml', 'test.key', 'English', 'Español', 'Antigo')"
        )
        self.conn.execute("INSERT INTO output_segments VALUES (1, 'Texto con palabra española')")
        self.conn.execute("INSERT INTO ml_score_items VALUES (10, 1, 'Texto con palabra española')")
        self.conn.execute(
            """
            INSERT INTO ml_quality_pattern_discovery_runs VALUES (
              9, 'run-9', 'v2', NULL, 10, 0.5, 1, 1, 1, 1, 0, 0, 1, 0,
              'completed', '{}', '2026-08-29T00:00:00Z', '2026-08-29T00:00:00Z'
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO ml_quality_pattern_families VALUES (
              1, 'family-spanish', 'explicit_issue', 'residual_spanish',
              'plain_text', 'events', 'distinct_candidate', NULL, NULL, 1
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO ml_quality_pattern_observations VALUES (
              9, 1, 'new_candidate', 1, 1, 1, 1.0, 1.0, 0.1, 0.1, 0.1,
              1.0, 0.8, 1.0, 0.5, 90.0, ?, '{}'
            )
            """,
            (json.dumps([{"segment_id": 1, "candidate_text": "Texto con palabra española"}]),),
        )
        initial = backend._quality_pattern_discovery_payload(self.conn)
        contract = initial["families"][0]["samples"][0]["review_contract"]
        self.conn.execute(
            """
            INSERT INTO ml_regenerative_review_decisions VALUES (
              1, 'discovery', ?, 1, 9, ?, 'supports_pattern', NULL,
              'test', 'v1', 1, '2026-08-29', '2026-08-29'
            )
            """,
            (contract["item_key"], contract["evidence_hash"]),
        )
        self.conn.commit()

        payload = backend._quality_pattern_discovery_payload(self.conn)

        family = payload["families"][0]
        self.assertEqual(family["routing_status"], "ready_for_correction")
        self.assertEqual(family["correction_lane"]["id"], "spanish_residual")
        self.assertEqual(payload["routing_summary"]["ready_family_count"], 1)

    def test_embedded_gender_discovery_explains_each_human_decision(self) -> None:
        guidance = backend._discovery_pattern_guidance(
            "embedded_gender_token_fragment",
            "gender_helper",
            "event_localization/activities",
        )

        self.assertEqual(guidance["title"], "Token de gênero embutido em uma palavra")
        self.assertIn("saída possível", guidance["confirm_when"])
        self.assertIn("todas as saídas", guidance["reject_when"])
        self.assertIn("limitar o detector", guidance["boundary_when"])
        self.assertEqual(guidance["scope"], "gender helper · event localization/activities")

    def test_discovery_review_batch_is_capped_at_fifty(self) -> None:
        self.conn.execute(
            """
            INSERT INTO ml_quality_pattern_discovery_runs VALUES (
              8, 'run-8', 'v2', NULL, 10, 0.5, 101, 101, 101, 101, 0, 0, 101, 0,
              'completed', '{}', '2026-08-29T00:00:00Z', '2026-08-29T00:00:00Z'
            )
            """
        )
        for segment_id in range(1, 102):
            self.conn.execute(
                "INSERT INTO source_segments VALUES (?, 'events/test.yml', ?, 'English', 'Español', 'Antigo')",
                (segment_id, f"test.key.{segment_id}"),
            )
            self.conn.execute(
                "INSERT INTO output_segments VALUES (?, ?)",
                (segment_id, f"Texto {segment_id}"),
            )
            self.conn.execute(
                "INSERT INTO ml_score_items VALUES (10, ?, ?)",
                (segment_id, f"Texto {segment_id}"),
            )
            self.conn.execute(
                """
                INSERT INTO ml_quality_pattern_families VALUES (
                  ?, ?, 'explicit_issue', 'test_issue', 'plain_text', 'events',
                  'distinct_candidate', NULL, NULL, 1
                )
                """,
                (segment_id, f"family-{segment_id}"),
            )
            self.conn.execute(
                """
                INSERT INTO ml_quality_pattern_observations VALUES (
                  8, ?, 'new_candidate', 1, 1, 1, 1.0, 0.01, 0.1, 0.1, 0.1,
                  1.0, 0.8, 1.0, 0.5, ?, ?, '{}'
                )
                """,
                (
                    segment_id,
                    200.0 - segment_id,
                    json.dumps([{"segment_id": segment_id, "candidate_text": f"Texto {segment_id}"}]),
                ),
            )
        self.conn.commit()

        payload = backend._quality_pattern_discovery_payload(self.conn)
        samples = [
            sample
            for family in payload["families"]
            for sample in family["samples"]
        ]

        self.assertEqual(payload["review_pending_count"], 50)
        self.assertEqual(
            sum(1 for sample in samples if sample["review_contract"]["available"]),
            50,
        )
        self.assertEqual(
            sum(1 for sample in samples if sample["review_status"] == "not_selected"),
            51,
        )

    def test_only_one_progressive_representative_per_family_is_visible(self) -> None:
        for segment_id in range(1, 4):
            self.conn.execute(
                "INSERT INTO source_segments VALUES (?, 'events/test.yml', ?, 'English', 'Español', 'Antigo')",
                (segment_id, f"test.key.{segment_id}"),
            )
            self.conn.execute(
                "INSERT INTO output_segments VALUES (?, ?)",
                (segment_id, f"Texto {segment_id}"),
            )
            self.conn.execute(
                "INSERT INTO ml_score_items VALUES (10, ?, ?)",
                (segment_id, f"Texto {segment_id}"),
            )
        self.conn.execute(
            """
            INSERT INTO ml_quality_pattern_discovery_runs VALUES (
              11, 'run-11', 'v3', NULL, 10, 0.5, 25, 25, 1, 1, 0, 0, 1, 0,
              'completed', '{}', '2026-08-31T00:00:00Z', '2026-08-31T00:00:00Z'
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO ml_quality_pattern_families VALUES (
              1, 'family-wide', 'explicit_issue', 'residual_spanish',
              'plain_text', 'events', 'distinct_candidate', NULL, NULL, 1
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO ml_quality_pattern_observations VALUES (
              11, 1, 'new_candidate', 25, 25, 20, 0.8, 1.0, 0.2, 0.1, 0.3,
              0.9, 0.85, 1.0, 0.8, 95.0, ?, '{}'
            )
            """,
            (json.dumps([
                {"segment_id": 1, "candidate_text": "Texto 1"},
                {"segment_id": 2, "candidate_text": "Texto 2"},
                {"segment_id": 3, "candidate_text": "Texto 3"},
            ]),),
        )
        self.conn.commit()

        first = backend._quality_pattern_discovery_payload(self.conn)
        family = first["families"][0]
        available = [
            sample for sample in family["samples"]
            if sample["review_contract"]["available"]
            and sample["review_status"] == "pending"
        ]

        self.assertEqual(len(available), 1)
        self.assertEqual(family["representative_policy"]["visible_at_once"], 1)
        self.assertEqual(family["human_review"]["required_confirmations"], 2)
        self.assertEqual(first["review_pending_count"], 1)


if __name__ == "__main__":
    unittest.main()
