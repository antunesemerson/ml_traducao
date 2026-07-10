from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT / "reports"
MEMORY_DIR = ROOT / "memory"


DEFAULT_POLICY_PATH = MEMORY_DIR / "production_safety_policy.json"
DEFAULT_POST_RELEASE_PATH = MEMORY_DIR / "post_release_feedback_status.json"
DEFAULT_LOCKS_PATH = MEMORY_DIR / "manual_visual_locks.json"
DEFAULT_RESTORE_STATUS_PATH = MEMORY_DIR / "pre_full_production_output_restore_latest.json"


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_index(root: Path) -> dict[str, dict[str, Any]]:
    if not root.exists():
        return {}
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        stat = path.stat()
        files[relative] = {
            "size": stat.st_size,
            "sha256": file_hash(path),
        }
    return files


def compare_trees(left_root: Path, right_root: Path, *, sample_limit: int = 25) -> dict[str, Any]:
    left = tree_index(left_root)
    right = tree_index(right_root)
    left_keys = set(left)
    right_keys = set(right)
    missing_from_right = sorted(left_keys - right_keys)
    extra_in_right = sorted(right_keys - left_keys)
    changed = sorted(
        key
        for key in (left_keys & right_keys)
        if left[key]["sha256"] != right[key]["sha256"]
    )
    return {
        "left": rel(left_root),
        "right": rel(right_root),
        "left_file_count": len(left),
        "right_file_count": len(right),
        "missing_from_right_count": len(missing_from_right),
        "extra_in_right_count": len(extra_in_right),
        "changed_count": len(changed),
        "total_delta_count": len(missing_from_right) + len(extra_in_right) + len(changed),
        "missing_from_right_sample": missing_from_right[:sample_limit],
        "extra_in_right_sample": extra_in_right[:sample_limit],
        "changed_sample": changed[:sample_limit],
    }


def load_context() -> dict[str, Any]:
    policy = read_json(DEFAULT_POLICY_PATH, {})
    post_release = read_json(DEFAULT_POST_RELEASE_PATH, {})
    locks = read_json(DEFAULT_LOCKS_PATH, {})
    restore_status = read_json(DEFAULT_RESTORE_STATUS_PATH, {})
    paths = policy.get("paths", {})
    return {
        "policy": policy,
        "post_release": post_release,
        "locks": locks,
        "restore_status": restore_status,
        "paths": {
            "current_stable_baseline": ROOT / paths.get("stable_mod_baseline", "source\\spanish_old"),
            "previous_stable_mod_baseline": ROOT / paths.get("previous_stable_mod_baseline", "source\\spanish_mod"),
            "source_mirror_baseline": ROOT / paths.get("source_mirror_baseline", "source\\spanish_source"),
            "current_broken_output_reference": ROOT / paths.get("current_broken_output_reference", "output\\spanish"),
            "current_hotfix_candidate": ROOT / paths.get("current_hotfix_candidate", "release_candidates\\spanish_hotfix_from_stable_20260706\\spanish"),
        },
    }


def build_summary() -> dict[str, Any]:
    context = load_context()
    paths = context["paths"]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_target = MEMORY_DIR / "production_snapshot_archives" / f"output_spanish_broken_reference_{timestamp}.zip"
    restore_source = paths["source_mirror_baseline"]
    restore_target = paths["current_broken_output_reference"]

    comparisons = {
        "output_vs_source_mirror": compare_trees(paths["source_mirror_baseline"], paths["current_broken_output_reference"]),
        "output_vs_current_stable": compare_trees(paths["current_stable_baseline"], paths["current_broken_output_reference"]),
        "candidate_vs_current_stable": compare_trees(paths["current_stable_baseline"], paths["current_hotfix_candidate"]),
        "current_stable_vs_previous_stable_mod": compare_trees(paths["previous_stable_mod_baseline"], paths["current_stable_baseline"]),
        "candidate_vs_source_mirror": compare_trees(paths["source_mirror_baseline"], paths["current_hotfix_candidate"]),
    }
    post_summary = context["post_release"].get("summary", {})
    restore_status = context.get("restore_status", {})
    restore_after = restore_status.get("after") if isinstance(restore_status, dict) else None
    output_restored_exactly = bool(restore_after and restore_after.get("restored_exactly"))
    locks = context["locks"].get("locks", [])

    publication_blocking_reasons: list[str] = []
    if post_summary.get("known_open_findings", 0):
        publication_blocking_reasons.append("known_open_findings_present")
    if post_summary.get("approved_pending_apply", 0):
        publication_blocking_reasons.append("approved_pending_apply_present")
    if post_summary.get("applied_pending_validation", 0):
        publication_blocking_reasons.append("applied_pending_validation_present")
    if not locks:
        publication_blocking_reasons.append("manual_visual_locks_not_loaded")

    evaluation_blocking_reasons: list[str] = []
    if not output_restored_exactly:
        evaluation_blocking_reasons.append("output_not_restored_from_source_mirror")
    if not locks:
        evaluation_blocking_reasons.append("manual_visual_locks_not_loaded")

    return {
        "schema_version": 1,
        "report_type": "safe_full_production_preflight_readonly",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "policy_path": rel(DEFAULT_POLICY_PATH),
        "post_release_feedback_status_path": rel(DEFAULT_POST_RELEASE_PATH),
        "manual_visual_locks_path": rel(DEFAULT_LOCKS_PATH),
        "output_restore_status_path": rel(DEFAULT_RESTORE_STATUS_PATH),
        "guards": {
            "read_only": True,
            "copied_output": False,
            "restored_output": False,
            "full_production": False,
            "reindex": False,
            "source_changed": False,
            "output_changed": False,
        },
        "paths": {key: rel(value) for key, value in paths.items()},
        "comparisons": comparisons,
        "locks": {
            "count": len(locks),
            "ids": [item.get("id") for item in locks],
        },
        "post_release_summary": post_summary,
        "planned_full_production_sequence": [
            {
                "step": "archive_current_output_reference",
                "execute_now": False,
                "source": rel(paths["current_broken_output_reference"]),
                "target": rel(archive_target),
                "reason": "Keep broken/current output as regression evidence before resetting output.",
            },
            {
                "step": "restore_output_from_source_mirror",
                "execute_now": False,
                "source": rel(restore_source),
                "target": rel(restore_target),
                "reason": "Start full production from a clean mirror of the current game source localization.",
            },
            {
                "step": "run_full_production_from_gui",
                "execute_now": False,
                "reason": "Output writes are restricted to the controlled GUI production flow.",
            },
            {
                "step": "compare_new_output_against_stable_baseline_and_source",
                "execute_now": False,
                "reason": "Require quality and anti-regression evidence before publication.",
            },
        ],
        "can_start_evaluation_full_production_now": not evaluation_blocking_reasons,
        "evaluation_blocking_reasons": evaluation_blocking_reasons,
        "can_publish_after_full_production_now": not publication_blocking_reasons,
        "publication_blocking_reasons": publication_blocking_reasons,
        "can_start_safe_full_production_now": not publication_blocking_reasons,
        "blocking_reasons": publication_blocking_reasons,
        "output_restore_status": {
            "mode": restore_status.get("mode") if isinstance(restore_status, dict) else None,
            "restored_exactly": output_restored_exactly,
            "archive_path": restore_status.get("archive_path") if isinstance(restore_status, dict) else None,
        },
        "recommendation": (
            "Evaluation full production is allowed; publication remains blocked until findings are closed or accepted."
            if not evaluation_blocking_reasons and publication_blocking_reasons
            else "Resolve preflight blockers before production."
            if evaluation_blocking_reasons
            else "Preflight is clear for controlled production and publication gate."
        ),
    }


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{stamp}_safe_full_production_preflight_readonly"
    summary_path = REPORTS_DIR / f"{base}_summary.json"
    markdown_path = REPORTS_DIR / f"{base}.md"
    latest_path = MEMORY_DIR / "safe_full_production_preflight_latest.json"

    summary_text = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    summary_path.write_text(summary_text, encoding="utf-8")
    latest_path.write_text(summary_text, encoding="utf-8")

    lines = [
        "# Safe Full Production Preflight - Read-only",
        "",
        f"Generated: `{summary['generated_at']}`",
        "",
        "## Result",
        "",
        f"- Can start evaluation full production now: `{str(summary['can_start_evaluation_full_production_now']).lower()}`",
        f"- Evaluation blocking reasons: `{', '.join(summary['evaluation_blocking_reasons']) if summary['evaluation_blocking_reasons'] else 'none'}`",
        f"- Can publish after full production now: `{str(summary['can_publish_after_full_production_now']).lower()}`",
        f"- Publication blocking reasons: `{', '.join(summary['publication_blocking_reasons']) if summary['publication_blocking_reasons'] else 'none'}`",
        f"- Recommendation: {summary['recommendation']}",
        "",
        "## Paths",
        "",
    ]
    for key, value in summary["paths"].items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Comparisons", ""])
    for key, value in summary["comparisons"].items():
        lines.append(
            f"- `{key}`: changed `{value['changed_count']}`, "
            f"missing `{value['missing_from_right_count']}`, extra `{value['extra_in_right_count']}`"
        )
    lines.extend(["", "## Planned Sequence", ""])
    for step in summary["planned_full_production_sequence"]:
        lines.append(f"- `{step['step']}`: execute_now=`{str(step['execute_now']).lower()}` - {step['reason']}")
    lines.extend(["", "## Guards", ""])
    for key, value in summary["guards"].items():
        lines.append(f"- `{key}`: `{value}`")
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary_path, markdown_path, latest_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only preflight for safe full production.")
    parser.add_argument("--json", action="store_true", help="Print summary JSON to stdout.")
    args = parser.parse_args()
    summary = build_summary()
    summary_path, markdown_path, latest_path = write_outputs(summary)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"summary={summary_path}")
        print(f"markdown={markdown_path}")
        print(f"latest={latest_path}")
        print(f"can_start_safe_full_production_now={summary['can_start_safe_full_production_now']}")
        print(f"blocking_reasons={','.join(summary['blocking_reasons']) if summary['blocking_reasons'] else 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
