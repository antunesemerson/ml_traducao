from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MEMORY_DIR = ROOT / "memory"
STATUS_PATH = MEMORY_DIR / "learning_status.json"
LOCK_PATH = MEMORY_DIR / "training_lock.json"

DEFAULT_PHASES = [
    ("context", "Context and scope"),
    ("evidence", "Evidence review"),
    ("implementation", "Rule/model implementation"),
    ("audit", "Audit and metrics"),
    ("checkpoint", "Checkpoint/governance"),
    ("release", "Learning release"),
]

RUNNING_STATUSES = {"running", "blocked", "failed"}
SAFE_STATUSES = {"completed", "released", "idle"}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def read_status() -> dict[str, Any]:
    if not STATUS_PATH.exists():
        return {}
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "status": "blocked",
            "production_safe": False,
            "reason": "learning_status_json_invalid",
            "updated_at": now_iso(),
        }
    return data if isinstance(data, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def default_phase_rows() -> list[dict[str, Any]]:
    return [
        {
            "id": phase_id,
            "label": label,
            "status": "pending",
            "started_at": "",
            "finished_at": "",
            "summary": "",
        }
        for phase_id, label in DEFAULT_PHASES
    ]


def phase_index(phases: list[dict[str, Any]], phase_id: str) -> int:
    for index, phase in enumerate(phases):
        if phase.get("id") == phase_id:
            return index
    phases.append(
        {
            "id": phase_id,
            "label": phase_id.replace("_", " ").title(),
            "status": "pending",
            "started_at": "",
            "finished_at": "",
            "summary": "",
        }
    )
    return len(phases) - 1


def summarize_progress(phases: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(phases)
    completed = sum(1 for phase in phases if phase.get("status") == "done")
    running = next((phase for phase in phases if phase.get("status") == "running"), None)
    blocked = next((phase for phase in phases if phase.get("status") in {"blocked", "failed"}), None)
    return {
        "phase_total": total,
        "phase_completed": completed,
        "phase_pending": max(total - completed - (1 if running else 0), 0),
        "progress_pct": round(100.0 * completed / total, 2) if total else 0.0,
        "current_phase": (blocked or running or {}).get("id", ""),
        "current_phase_label": (blocked or running or {}).get("label", ""),
    }


def build_payload(
    *,
    status: str,
    objective: str,
    phases: list[dict[str, Any]],
    current_phase: str = "",
    owner: str = "learning_front_codex",
    last_report: str = "",
    blockers: list[str] | None = None,
    warnings: list[str] | None = None,
    next_action: str = "",
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = existing or {}
    progress = summarize_progress(phases)
    if current_phase:
        progress["current_phase"] = current_phase
        index = phase_index(phases, current_phase)
        progress["current_phase_label"] = str(phases[index].get("label") or current_phase)

    production_safe = status in SAFE_STATUSES and not blockers
    started_at = existing.get("started_at") or now_iso()
    finished_at = now_iso() if status in SAFE_STATUSES or status == "failed" else ""
    if status == "released":
        finished_at = existing.get("finished_at") or now_iso()

    payload = {
        "schema_version": 1,
        "owner": owner,
        "status": status,
        "production_safe": production_safe,
        "lock_reason": "" if production_safe else "learning_cycle_in_progress",
        "objective": objective,
        "started_at": started_at,
        "updated_at": now_iso(),
        "finished_at": finished_at,
        "current_phase": progress.get("current_phase") or "",
        "current_phase_label": progress.get("current_phase_label") or "",
        "phase_total": progress["phase_total"],
        "phase_completed": progress["phase_completed"],
        "phase_pending": progress["phase_pending"],
        "progress_pct": progress["progress_pct"],
        "phases": phases,
        "last_report": last_report or existing.get("last_report", ""),
        "blockers": blockers or [],
        "warnings": warnings or [],
        "next_action": next_action,
        "message": (
            "Production can run"
            if production_safe
            else "Production must wait for the learning cycle to finish"
        ),
    }
    return payload


def sync_lock(payload: dict[str, Any]) -> None:
    if payload.get("production_safe"):
        if LOCK_PATH.exists():
            LOCK_PATH.unlink()
        return
    lock_payload = {
        "schema_version": payload.get("schema_version", 1),
        "reason": payload.get("lock_reason") or "learning_cycle_in_progress",
        "owner": payload.get("owner"),
        "status": payload.get("status"),
        "objective": payload.get("objective"),
        "current_phase": payload.get("current_phase"),
        "current_phase_label": payload.get("current_phase_label"),
        "progress_pct": payload.get("progress_pct"),
        "started_at": payload.get("started_at"),
        "updated_at": payload.get("updated_at"),
        "last_report": payload.get("last_report"),
        "message": payload.get("message"),
    }
    write_json(LOCK_PATH, lock_payload)


def start(args: argparse.Namespace) -> dict[str, Any]:
    phases = default_phase_rows()
    if args.phase:
        index = phase_index(phases, args.phase)
        phases[index]["status"] = "running"
        phases[index]["started_at"] = now_iso()
        phases[index]["summary"] = args.summary or ""
    payload = build_payload(
        status="running",
        objective=args.objective,
        phases=phases,
        current_phase=args.phase or "context",
        last_report=args.report or "",
        next_action=args.next_action or "",
    )
    write_json(STATUS_PATH, payload)
    sync_lock(payload)
    return payload


def update_phase(args: argparse.Namespace) -> dict[str, Any]:
    existing = read_status()
    phases = existing.get("phases") if isinstance(existing.get("phases"), list) else default_phase_rows()
    index = phase_index(phases, args.phase)
    phase = phases[index]
    phase["status"] = args.phase_status
    if args.phase_status == "running" and not phase.get("started_at"):
        phase["started_at"] = now_iso()
    if args.phase_status in {"done", "blocked", "failed"}:
        phase["finished_at"] = now_iso()
    if args.summary:
        phase["summary"] = args.summary

    for prior in phases[:index]:
        if prior.get("status") in {"pending", "running"}:
            prior["status"] = "done"
            prior["finished_at"] = prior.get("finished_at") or now_iso()
    for other in phases:
        if other is not phase and other.get("status") == "running":
            other["status"] = "done"
            other["finished_at"] = other.get("finished_at") or now_iso()

    status = "running"
    if args.phase_status == "blocked":
        status = "blocked"
    elif args.phase_status == "failed":
        status = "failed"
    payload = build_payload(
        status=status,
        objective=args.objective or existing.get("objective", "Learning cycle"),
        phases=phases,
        current_phase=args.phase,
        last_report=args.report or existing.get("last_report", ""),
        blockers=args.blocker or existing.get("blockers", []),
        warnings=args.warning or existing.get("warnings", []),
        next_action=args.next_action or existing.get("next_action", ""),
        existing=existing,
    )
    write_json(STATUS_PATH, payload)
    sync_lock(payload)
    return payload


def finish(args: argparse.Namespace, *, released: bool) -> dict[str, Any]:
    existing = read_status()
    phases = existing.get("phases") if isinstance(existing.get("phases"), list) else default_phase_rows()
    for phase in phases:
        if phase.get("status") in {"pending", "running"}:
            phase["status"] = "done"
            phase["finished_at"] = phase.get("finished_at") or now_iso()
    status = "released" if released else "completed"
    payload = build_payload(
        status=status,
        objective=args.objective or existing.get("objective", "Learning cycle"),
        phases=phases,
        last_report=args.report or existing.get("last_report", ""),
        warnings=args.warning or existing.get("warnings", []),
        next_action=args.next_action or "Production may run with the promoted/active safe knowledge.",
        existing=existing,
    )
    write_json(STATUS_PATH, payload)
    sync_lock(payload)
    return payload


def block(args: argparse.Namespace, *, failed: bool = False) -> dict[str, Any]:
    existing = read_status()
    phases = existing.get("phases") if isinstance(existing.get("phases"), list) else default_phase_rows()
    current_phase = args.phase or existing.get("current_phase") or "audit"
    index = phase_index(phases, current_phase)
    phases[index]["status"] = "failed" if failed else "blocked"
    phases[index]["finished_at"] = now_iso()
    if args.summary:
        phases[index]["summary"] = args.summary
    payload = build_payload(
        status="failed" if failed else "blocked",
        objective=args.objective or existing.get("objective", "Learning cycle"),
        phases=phases,
        current_phase=current_phase,
        last_report=args.report or existing.get("last_report", ""),
        blockers=args.blocker or ["learning_cycle_requires_attention"],
        warnings=args.warning or existing.get("warnings", []),
        next_action=args.next_action or "Resolve learning blockers before production.",
        existing=existing,
    )
    write_json(STATUS_PATH, payload)
    sync_lock(payload)
    return payload


def show(_: argparse.Namespace) -> dict[str, Any]:
    payload = read_status()
    if not payload:
        payload = build_payload(
            status="idle",
            objective="No active learning cycle",
            phases=default_phase_rows(),
            next_action="Production may run.",
        )
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the learning-front production lock/status.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--objective", required=True)
    start_parser.add_argument("--phase", default="context")
    start_parser.add_argument("--summary", default="")
    start_parser.add_argument("--report", default="")
    start_parser.add_argument("--next-action", default="")

    phase_parser = subparsers.add_parser("phase")
    phase_parser.add_argument("--phase", required=True)
    phase_parser.add_argument("--phase-status", choices=["pending", "running", "done", "blocked", "failed"], default="running")
    phase_parser.add_argument("--objective", default="")
    phase_parser.add_argument("--summary", default="")
    phase_parser.add_argument("--report", default="")
    phase_parser.add_argument("--blocker", action="append", default=[])
    phase_parser.add_argument("--warning", action="append", default=[])
    phase_parser.add_argument("--next-action", default="")

    complete_parser = subparsers.add_parser("complete")
    complete_parser.add_argument("--objective", default="")
    complete_parser.add_argument("--report", default="")
    complete_parser.add_argument("--warning", action="append", default=[])
    complete_parser.add_argument("--next-action", default="")

    release_parser = subparsers.add_parser("release")
    release_parser.add_argument("--objective", default="")
    release_parser.add_argument("--report", default="")
    release_parser.add_argument("--warning", action="append", default=[])
    release_parser.add_argument("--next-action", default="")

    block_parser = subparsers.add_parser("block")
    block_parser.add_argument("--objective", default="")
    block_parser.add_argument("--phase", default="")
    block_parser.add_argument("--summary", default="")
    block_parser.add_argument("--report", default="")
    block_parser.add_argument("--blocker", action="append", default=[])
    block_parser.add_argument("--warning", action="append", default=[])
    block_parser.add_argument("--next-action", default="")

    fail_parser = subparsers.add_parser("fail")
    fail_parser.add_argument("--objective", default="")
    fail_parser.add_argument("--phase", default="")
    fail_parser.add_argument("--summary", default="")
    fail_parser.add_argument("--report", default="")
    fail_parser.add_argument("--blocker", action="append", default=[])
    fail_parser.add_argument("--warning", action="append", default=[])
    fail_parser.add_argument("--next-action", default="")

    subparsers.add_parser("show")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "start":
        payload = start(args)
    elif args.command == "phase":
        payload = update_phase(args)
    elif args.command == "complete":
        payload = finish(args, released=False)
    elif args.command == "release":
        payload = finish(args, released=True)
    elif args.command == "block":
        payload = block(args, failed=False)
    elif args.command == "fail":
        payload = block(args, failed=True)
    else:
        payload = show(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
