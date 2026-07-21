from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "quality_promotion_cycle_v1"
PROVIDERS_DIR = Path(__file__).resolve().with_name("quality_promotion_providers")


@dataclass(frozen=True)
class PromotionProvider:
    provider_id: str
    label: str
    priority: int
    evidence_type: str
    shadow_script: Path
    shadow_args: tuple[str, ...]
    evidence_script: Path
    evidence_args: tuple[str, ...]
    discovery_issue_types: tuple[str, ...]
    manifest_path: Path


def _validated_script(value: Any, manifest_path: Path) -> Path:
    relative = Path(str(value or ""))
    if relative.is_absolute() or relative.suffix.casefold() != ".py":
        raise RuntimeError(f"Invalid provider script in {manifest_path}: {value!r}")
    resolved = (db.PROJECT_ROOT / relative).resolve()
    pipeline_root = (db.PROJECT_ROOT / "pipeline").resolve()
    if resolved.parent != pipeline_root or not resolved.is_file():
        raise RuntimeError(f"Provider script must be a direct pipeline module: {value!r}")
    return resolved


def _string_args(value: Any, manifest_path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeError(f"Provider args must be a string list: {manifest_path}")
    if "--apply" in value:
        raise RuntimeError(f"Provider manifests cannot grant apply directly: {manifest_path}")
    return tuple(value)


def _discovery_issue_types(value: Any, manifest_path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise RuntimeError(f"Provider discovery contract must be an object: {manifest_path}")
    issue_types = value.get("issue_types") or []
    if not isinstance(issue_types, list) or not all(isinstance(item, str) for item in issue_types):
        raise RuntimeError(f"Provider discovery issue types must be a string list: {manifest_path}")
    return tuple(sorted({item.strip() for item in issue_types if item.strip()}))


def load_providers(directory: Path = PROVIDERS_DIR) -> list[PromotionProvider]:
    providers: list[PromotionProvider] = []
    seen_ids: set[str] = set()
    seen_evidence: set[str] = set()
    for manifest_path in sorted(directory.glob("*.json")):
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not payload.get("enabled", True):
            continue
        if int(payload.get("schema_version") or 0) != 1:
            raise RuntimeError(f"Unsupported provider schema: {manifest_path}")
        provider_id = str(payload.get("provider_id") or "").strip()
        evidence_type = str(payload.get("evidence_type") or "").strip()
        if not provider_id or not evidence_type:
            raise RuntimeError(f"Provider id and evidence type are required: {manifest_path}")
        if provider_id in seen_ids or evidence_type in seen_evidence:
            raise RuntimeError(f"Duplicate provider id or evidence type: {manifest_path}")
        seen_ids.add(provider_id)
        seen_evidence.add(evidence_type)
        providers.append(
            PromotionProvider(
                provider_id=provider_id,
                label=str(payload.get("label") or provider_id),
                priority=int(payload.get("priority") or 1000),
                evidence_type=evidence_type,
                shadow_script=_validated_script(payload.get("shadow_script"), manifest_path),
                shadow_args=_string_args(payload.get("shadow_args"), manifest_path),
                evidence_script=_validated_script(payload.get("evidence_script"), manifest_path),
                evidence_args=_string_args(payload.get("evidence_args"), manifest_path),
                discovery_issue_types=_discovery_issue_types(payload.get("discovery"), manifest_path),
                manifest_path=manifest_path,
            )
        )
    return sorted(providers, key=lambda item: (item.priority, item.provider_id))


def _parse_json_payload(stdout: str) -> dict[str, Any]:
    stripped = stdout.strip()
    if not stripped:
        return {}
    try:
        payload = json.loads(stripped)
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        pass
    lines = stripped.splitlines()
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("{"):
            continue
        try:
            payload = json.loads("\n".join(lines[index:]))
        except json.JSONDecodeError:
            continue
        return payload if isinstance(payload, dict) else {}
    return {}


def _run_command(label: str, command: list[str], timeout: int = 1800) -> dict[str, Any]:
    process = subprocess.run(
        command,
        cwd=str(db.PROJECT_ROOT),
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if process.returncode:
        tail = "\n".join(((process.stdout or "") + "\n" + (process.stderr or "")).splitlines()[-40:])
        raise RuntimeError(f"Promotion provider stage failed ({label}):\n{tail}")
    return {
        "label": label,
        "command": command,
        "exit_code": process.returncode,
        "payload": _parse_json_payload(process.stdout or ""),
        "stdout_tail": (process.stdout or "").splitlines()[-20:],
    }


def run_diagnostic() -> dict[str, Any]:
    discovery = _run_command(
        "quality_pattern_discovery",
        [
            sys.executable,
            str(db.PROJECT_ROOT / "pipeline" / "quality_pattern_discovery.py"),
            "--apply",
        ],
    )
    proposal_command = [
        sys.executable,
        str(db.PROJECT_ROOT / "pipeline" / "quality_provider_proposal_generator.py"),
        "--apply",
    ]
    discovery_run_id = int((discovery.get("payload") or {}).get("run_id") or 0)
    audit_command = [
        sys.executable,
        str(db.PROJECT_ROOT / "pipeline" / "quality_closed_observation_audit.py"),
        "--apply",
    ]
    if discovery_run_id:
        audit_command.extend(("--discovery-run-id", str(discovery_run_id)))
    closed_observation_audit = _run_command(
        "quality_closed_observation_audit",
        audit_command,
    )
    if discovery_run_id:
        proposal_command.extend(("--discovery-run-id", str(discovery_run_id)))
    provider_proposals = _run_command(
        "quality_provider_proposals",
        proposal_command,
    )
    provider_results: list[dict[str, Any]] = []
    total_evidence = 0
    total_ready = 0
    for provider in load_providers():
        shadow = _run_command(
            f"{provider.provider_id}:shadow",
            [sys.executable, str(provider.shadow_script), *provider.shadow_args, "--persist-db"],
        )
        shadow_run_id = int(shadow["payload"].get("shadow_run_id") or 0)
        if not shadow_run_id:
            raise RuntimeError(
                f"Promotion provider {provider.provider_id} did not materialize a database shadow snapshot."
            )
        evidence = _run_command(
            f"{provider.provider_id}:evidence",
            [
                sys.executable,
                str(provider.evidence_script),
                *provider.evidence_args,
                "--shadow-run-id",
                str(shadow_run_id),
                "--apply",
            ],
        )
        evidence_count = int(evidence["payload"].get("evidence_count") or 0)
        gate: dict[str, Any] | None = None
        ready_count = 0
        if evidence_count:
            gate = _run_command(
                f"{provider.provider_id}:gate",
                [
                    sys.executable,
                    str(db.PROJECT_ROOT / "pipeline" / "quality_pairwise_monotonic_gate.py"),
                    "--evidence-type",
                    provider.evidence_type,
                    "--apply",
                ],
            )
            ready_count = int(gate["payload"].get("ready_count") or 0)
        total_evidence += evidence_count
        total_ready += ready_count
        provider_results.append(
            {
                "provider_id": provider.provider_id,
                "label": provider.label,
                "evidence_type": provider.evidence_type,
                "evidence_count": evidence_count,
                "promotion_ready_count": ready_count,
                "shadow_run_id": shadow_run_id,
                "shadow": shadow,
                "evidence": evidence,
                "gate": gate,
            }
        )
    calibration_consumption = _run_command(
        "pairwise_calibration_consumption",
        [
            sys.executable,
            str(db.PROJECT_ROOT / "pipeline" / "quality_pairwise_calibration_consumer.py"),
            "--all-ready",
            "--apply",
        ],
    )
    calibration_policy = _run_command(
        "pairwise_calibration_policy",
        [
            sys.executable,
            str(db.PROJECT_ROOT / "pipeline" / "quality_calibration_policy.py"),
            "--apply",
        ],
    )
    policy_payload = calibration_policy.get("payload") or {}
    calibration_decision = str(policy_payload.get("decision") or "required")
    if calibration_decision in {"sample", "required"}:
        calibration_review = _run_command(
            "pairwise_calibration_review",
            [
                sys.executable,
                str(db.PROJECT_ROOT / "pipeline" / "quality_pairwise_calibration_review_queue.py"),
                "--apply",
                "--control-count",
                str(int(policy_payload.get("recommended_control_count") or 0)),
            ],
        )
    else:
        calibration_review = {
            "label": "pairwise_calibration_review",
            "command": [],
            "exit_code": 0,
            "payload": {
                "schema_version": 1,
                "source": RULE_VERSION,
                "skipped": True,
                "policy_decision": calibration_decision,
                "policy_decision_id": policy_payload.get("policy_decision_id"),
                "reason_summary": policy_payload.get("reason_summary"),
                "review_count": 0,
                "priority_count": 0,
                "positive_control_count": 0,
                "negative_control_count": 0,
                "pending_count": 0,
                "confirmation_write_count": 0,
                "output_write_count": 0,
                "score_write_count": 0,
            },
            "stdout_tail": [],
        }
    return {
        "schema_version": 1,
        "source": RULE_VERSION,
        "mode": "diagnostic",
        "provider_count": len(provider_results),
        "evidence_count": total_evidence,
        "ready_count": total_ready,
        "confirmation_write_count": 0,
        "output_write_count": 0,
        "pattern_discovery": discovery,
        "closed_observation_audit": closed_observation_audit,
        "provider_proposals": provider_proposals,
        "providers": provider_results,
        "calibration_consumption": calibration_consumption,
        "calibration_policy": calibration_policy,
        "calibration_review": calibration_review,
    }


def promotion_evidence_types(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row["evidence_type"])
        for row in conn.execute(
            """
            SELECT DISTINCT evidence.evidence_type
            FROM ml_pairwise_quality_evidence evidence
            JOIN output_segments output ON output.segment_id = evidence.segment_id
            JOIN segment_state_items state
              ON state.segment_id = evidence.segment_id
             AND state.run_id = (
                SELECT id
                FROM segment_state_runs
                WHERE finished_at IS NOT NULL
                ORDER BY id DESC
                LIMIT 1
             )
            WHERE evidence.promotion_eligible = 1
              AND evidence.id = (
                SELECT MAX(latest.id)
                FROM ml_pairwise_quality_evidence latest
                WHERE latest.segment_id = evidence.segment_id
                  AND latest.evidence_type = evidence.evidence_type
              )
              AND output.portuguese_text = evidence.baseline_text
              AND evidence.candidate_text <> output.portuguese_text
            ORDER BY evidence.evidence_type
            """
        )
    ]


def _promotion_summary(stdout_tail: list[str]) -> dict[str, int]:
    result = {"ready": 0, "blocked": 0, "queued": 0}
    labels = {"Ready": "ready", "Blocked": "blocked", "Queued": "queued"}
    for line in stdout_tail:
        for label, key in labels.items():
            marker = f"] {label}:"
            if marker not in line:
                continue
            try:
                result[key] = int(line.rsplit(":", 1)[1].strip())
            except ValueError:
                pass
    return result


def _promotion_counts(stage: dict[str, Any]) -> dict[str, int]:
    payload = stage.get("payload") or {}
    if isinstance(payload, dict) and "ready_count" in payload:
        return {
            "ready": int(payload.get("ready_count") or 0),
            "blocked": int(payload.get("blocked_count") or 0),
            "queued": int(payload.get("queued_confirmation_count") or 0),
        }
    return _promotion_summary(stage.get("stdout_tail") or [])


def run_evaluation(*, apply: bool = True) -> dict[str, Any]:
    settings = db.load_settings()
    with db.connect(settings) as conn:
        evidence_types = promotion_evidence_types(conn)
    results: list[dict[str, Any]] = []
    total_ready = 0
    total_blocked = 0
    total_queued = 0
    for evidence_type in evidence_types:
        command = [
            sys.executable,
            str(db.PROJECT_ROOT / "pipeline" / "quality_pairwise_promotion_queue.py"),
            "--evidence-type",
            evidence_type,
        ]
        if apply:
            command.append("--apply")
        stage = _run_command(
            f"{evidence_type}:promotion_queue",
            command,
        )
        counts = _promotion_counts(stage)
        total_ready += counts["ready"]
        total_blocked += counts["blocked"]
        total_queued += counts["queued"]
        results.append({"evidence_type": evidence_type, **counts, "stage": stage})
    return {
        "schema_version": 1,
        "source": RULE_VERSION,
        "mode": "evaluation",
        "apply": apply,
        "evidence_type_count": len(evidence_types),
        "ready_count": total_ready,
        "blocked_count": total_blocked,
        "queued_count": total_queued if apply else 0,
        "confirmation_write_count": total_queued if apply else 0,
        "output_write_count": 0,
        "evidence_types": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run registered quality promotion providers.")
    parser.add_argument("mode", choices=("diagnostic", "evaluation"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate promotion queues without writing confirmations (evaluation only).",
    )
    args = parser.parse_args()
    if args.dry_run and args.mode != "evaluation":
        parser.error("--dry-run is only supported in evaluation mode")
    payload = run_diagnostic() if args.mode == "diagnostic" else run_evaluation(apply=not args.dry_run)
    if payload["mode"] == "diagnostic":
        print(f"[quality_promotion_cycle] Providers: {payload.get('provider_count', 0)}")
        print(f"[quality_promotion_cycle] Evidence: {payload.get('evidence_count', 0)}")
    else:
        print(f"[quality_promotion_cycle] Queues: {payload.get('evidence_type_count', 0)}")
    print(f"[quality_promotion_cycle] Ready: {payload.get('ready_count', 0)}")
    print(f"[quality_promotion_cycle] Blocked: {payload.get('blocked_count', 0)}")
    print(f"[quality_promotion_cycle] Queued: {payload.get('queued_count', 0)}")
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
