from __future__ import annotations

import sys
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch


PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from quality_promotion_cycle import (  # noqa: E402
    PROVIDERS_DIR,
    load_providers,
    promotion_evidence_types,
    run_diagnostic,
)


class QualityPromotionCycleTests(unittest.TestCase):
    def test_registered_providers_are_unique_and_version_agnostic(self) -> None:
        providers = load_providers()

        self.assertGreaterEqual(len(providers), 2)
        self.assertEqual(len({item.provider_id for item in providers}), len(providers))
        self.assertEqual(len({item.evidence_type for item in providers}), len(providers))
        for provider in providers:
            self.assertNotRegex(provider.provider_id.casefold(), r"(^|[_-])v\d+($|[_-])")
            self.assertNotRegex(provider.shadow_script.name.casefold(), r"(^|[_-])v\d+($|[_-])")
            self.assertNotRegex(provider.evidence_script.name.casefold(), r"(^|[_-])v\d+($|[_-])")
        mapped_issue_types = [
            issue_type
            for provider in providers
            for issue_type in provider.discovery_issue_types
        ]
        self.assertEqual(len(mapped_issue_types), len(set(mapped_issue_types)))

    def test_every_manifest_resolves_to_executable_pipeline_stages(self) -> None:
        manifests = sorted(PROVIDERS_DIR.glob("*.json"))
        providers = load_providers()

        self.assertEqual(len(manifests), len(providers))
        for provider in providers:
            self.assertTrue(provider.shadow_script.is_file())
            self.assertTrue(provider.evidence_script.is_file())
            self.assertEqual(provider.shadow_script.parent, PIPELINE_DIR)
            self.assertEqual(provider.evidence_script.parent, PIPELINE_DIR)

    def test_mojibake_lexicon_provider_is_registered(self) -> None:
        providers = {item.provider_id: item for item in load_providers()}

        self.assertIn("mojibake_lexicon", providers)
        provider = providers["mojibake_lexicon"]
        self.assertEqual(provider.evidence_type, "deterministic_mojibake_lexicon_repair")
        self.assertEqual(provider.shadow_args, ("--minimum-support", "2"))

    def test_dynamic_name_de_prefix_provider_is_registered(self) -> None:
        providers = {item.provider_id: item for item in load_providers()}

        self.assertIn("dynamic_name_de_prefix", providers)
        provider = providers["dynamic_name_de_prefix"]
        self.assertEqual(
            provider.evidence_type,
            "deterministic_dynamic_name_de_prefix_repair",
        )

    def test_only_current_unapplied_suggestion_types_are_consumed(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE segment_state_runs (id INTEGER PRIMARY KEY, finished_at TEXT);
            CREATE TABLE segment_state_items (run_id INTEGER, segment_id INTEGER);
            CREATE TABLE output_segments (segment_id INTEGER PRIMARY KEY, portuguese_text TEXT);
            CREATE TABLE ml_pairwise_quality_evidence (
              id INTEGER PRIMARY KEY,
              segment_id INTEGER,
              evidence_type TEXT,
              baseline_text TEXT,
              candidate_text TEXT,
              promotion_eligible INTEGER
            );
            INSERT INTO segment_state_runs VALUES (1, '2026-07-15T00:00:00Z');
            INSERT INTO segment_state_items VALUES (1, 1), (1, 2), (1, 3);
            INSERT INTO output_segments VALUES (1, 'old'), (2, 'new'), (3, 'other');
            INSERT INTO ml_pairwise_quality_evidence VALUES
              (1, 1, 'active_type', 'old', 'new', 1),
              (2, 2, 'already_applied_type', 'old', 'new', 1),
              (3, 3, 'stale_type', 'old', 'new', 1),
              (4, 1, 'not_eligible_type', 'old', 'new', 0),
              (5, 1, 'superseded_type', 'old', 'new', 1),
              (6, 1, 'superseded_type', 'different baseline', 'newer', 1);
            """
        )

        self.assertEqual(promotion_evidence_types(conn), ["active_type"])
        conn.close()

    def test_diagnostic_skips_queue_when_calibration_policy_skips(self) -> None:
        labels: list[str] = []

        def fake_run(label: str, command: list[str], timeout: int = 1800):
            labels.append(label)
            if label == "pairwise_calibration_policy":
                return {
                    "label": label,
                    "command": command,
                    "exit_code": 0,
                    "payload": {
                        "decision": "skip",
                        "policy_decision_id": 7,
                        "reason_summary": "Lote estavel.",
                        "recommended_control_count": 0,
                    },
                    "stdout_tail": [],
                }
            return {
                "label": label,
                "command": command,
                "exit_code": 0,
                "payload": {},
                "stdout_tail": [],
            }

        with patch("quality_promotion_cycle.load_providers", return_value=[]), patch(
            "quality_promotion_cycle._run_command", side_effect=fake_run
        ):
            payload = run_diagnostic()

        self.assertIn("pairwise_calibration_policy", labels)
        self.assertEqual(labels[0], "quality_pattern_discovery")
        self.assertEqual(labels[1], "quality_closed_observation_audit")
        self.assertEqual(labels[2], "quality_provider_proposals")
        self.assertIn("closed_observation_audit", payload)
        self.assertIn("provider_proposals", payload)
        self.assertNotIn("pairwise_calibration_review", labels)
        self.assertTrue(payload["calibration_review"]["payload"]["skipped"])
        self.assertEqual(payload["calibration_review"]["payload"]["policy_decision"], "skip")


if __name__ == "__main__":
    unittest.main()
