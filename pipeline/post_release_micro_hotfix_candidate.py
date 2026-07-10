from __future__ import annotations

import difflib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "source" / "spanish_old"
CANDIDATE_ROOT = ROOT / "release_candidates" / "spanish_post_release_micro_hotfix_20260706_v1" / "spanish"
REPORTS_DIR = ROOT / "reports"


@dataclass(frozen=True)
class HotfixItem:
    item_id: str
    segment_id: int | None
    relative_path: str
    source_key: str
    current_line: str
    proposed_line: str
    reason: str
    risk: str


ITEMS = [
    HotfixItem(
        item_id="qa-visual-003-event-other-person-leading-exclamation",
        segment_id=124725,
        relative_path="event_localization/relation_events/adultery_events_l_spanish.yml",
        source_key="adultery.0002.d",
        current_line=' adultery.0002.d: "![lover_spouse.Custom(\'ES_ElElla\')|U] jamais desrespeitaria a santidade do nosso casamento!"',
        proposed_line=' adultery.0002.d: "[lover_spouse.Custom(\'ES_ElElla\')|U] jamais desrespeitaria a santidade do nosso casamento!"',
        reason="Remove leading literal ! before dynamic pronoun token.",
        risk="low_token_preserving",
    ),
    HotfixItem(
        item_id="qa-visual-004-ai-personality-honor-label",
        segment_id=5419,
        relative_path="ai_personality_l_spanish.yml",
        source_key="honor_neg_adj",
        current_line=' honor_neg_adj: "Desonrado [Select_CString( CHARACTER.IsFemale, \'deshonrosa\', \'deshonroso\' )]"',
        proposed_line=' honor_neg_adj: "[Select_CString( CHARACTER.IsFemale, \'Desonrada\', \'Desonrado\' )]"',
        reason="Replace mixed Portuguese prefix plus Spanish Select_CString payload.",
        risk="select_payload_rewrite",
    ),
    HotfixItem(
        item_id="qa-visual-005-ai-personality-villain-label",
        segment_id=5494,
        relative_path="ai_personality_l_spanish.yml",
        source_key="compassion_strong_neg_noun",
        current_line=' compassion_strong_neg_noun: "Vilão [Select_CString( CHARACTER.IsFemale, \'Villana\', \'Villano\' )]"',
        proposed_line=' compassion_strong_neg_noun: "[Select_CString( CHARACTER.IsFemale, \'Vilã\', \'Vilão\' )]"',
        reason="Replace mixed Portuguese prefix plus Spanish Select_CString payload.",
        risk="select_payload_rewrite",
    ),
    HotfixItem(
        item_id="qa-visual-002-ai-personality-vengefulness-duplicate",
        segment_id=None,
        relative_path="ai_personality_l_spanish.yml",
        source_key="vengefulness_adj",
        current_line=' vengefulness_adj: "Ressentido [Select_CString( CHARACTER.IsFemale, \'resentida\', \'resentido\' )]"',
        proposed_line=' vengefulness_adj: "[Select_CString( CHARACTER.IsFemale, \'Ressentida\', \'Ressentido\' )]"',
        reason="Avoid rendered repetition such as Ressentido ressentido Apostador.",
        risk="composition_component_requires_visual_validation",
    ),
    HotfixItem(
        item_id="qa-visual-006-ai-personality-honor-explanation",
        segment_id=5427,
        relative_path="ai_personality_l_spanish.yml",
        source_key="honor_strong_neg_explanation",
        current_line=' honor_strong_neg_explanation: "Este personagem é #Bold muy deshonroso#! e é significativamente mais provável que inicie [hostile_schemes|lE], abuse de seu cargo como [regent|lE] faça [blackmail|lE], use a [councillor_task|lE] \\"$task_find_secrets$\\", retenha [hostages|lE] e traia [friends|lE] e [spouses|lE]. É muito menos provável que cumpra seus compromissos e pode se rebaixar incrivelmente para alcançar seus objetivos."',
        proposed_line=' honor_strong_neg_explanation: "Este personagem é #Bold muito desonrado#! e é significativamente mais provável que inicie [hostile_schemes|lE], abuse de seu cargo como [regent|lE] faça [blackmail|lE], use a [councillor_task|lE] \\"$task_find_secrets$\\", retenha [hostages|lE] e traia [friends|lE] e [spouses|lE]. É muito menos provável que cumpra seus compromissos e pode se rebaixar incrivelmente para alcançar seus objetivos."',
        reason="Replace literal Spanish residue in personality explanation tooltip.",
        risk="low_literal_residue",
    ),
]


TOKEN_RE = re.compile(r"\[[^\]]+\]|\$[^$]+\$|#[A-Za-z_]+|#!|@[A-Za-z0-9_]+!")


def decode_preserving_bom(path: Path) -> tuple[str, bool]:
    raw = path.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    return raw.decode("utf-8-sig" if has_bom else "utf-8"), has_bom


def write_preserving_bom(path: Path, text: str, has_bom: bool) -> None:
    path.write_bytes(text.encode("utf-8-sig" if has_bom else "utf-8"))


def token_signature(text: str) -> list[str]:
    return TOKEN_RE.findall(text)


def validate_item(candidate_root: Path, item: HotfixItem) -> dict[str, Any]:
    path = candidate_root / item.relative_path
    text, has_bom = decode_preserving_bom(path)
    current_count = text.count(item.current_line)
    proposed_count = text.count(item.proposed_line)
    record: dict[str, Any] = {
        "item_id": item.item_id,
        "segment_id": item.segment_id,
        "relative_path": item.relative_path,
        "source_key": item.source_key,
        "reason": item.reason,
        "risk": item.risk,
        "current_count": current_count,
        "proposed_count_before": proposed_count,
        "applied": False,
        "blocked": False,
        "block_reason": None,
        "token_signature_before": token_signature(item.current_line),
        "token_signature_after": token_signature(item.proposed_line),
        "token_signature_equal": token_signature(item.current_line) == token_signature(item.proposed_line),
        "structure_guard_ok": item.source_key in item.current_line and item.source_key in item.proposed_line,
        "canonical_changes": item.current_line != item.proposed_line,
        "diff_preview": list(
            difflib.unified_diff(
                [item.current_line],
                [item.proposed_line],
                fromfile="current",
                tofile="proposed",
                lineterm="",
            )
        ),
    }
    if current_count != 1:
        record["blocked"] = True
        record["block_reason"] = f"expected_current_line_once_found_{current_count}"
        return record
    if not record["structure_guard_ok"]:
        record["blocked"] = True
        record["block_reason"] = "structure_guard_failed"
        return record
    if not record["canonical_changes"]:
        record["blocked"] = True
        record["block_reason"] = "no_canonical_change"
        return record
    text = text.replace(item.current_line, item.proposed_line, 1)
    write_preserving_bom(path, text, has_bom)
    record["applied"] = True
    return record


def count_files(path: Path) -> int:
    return sum(1 for p in path.rglob("*") if p.is_file())


def changed_files(left_root: Path, right_root: Path) -> list[str]:
    left_files = {p.relative_to(left_root).as_posix(): p for p in left_root.rglob("*") if p.is_file()}
    right_files = {p.relative_to(right_root).as_posix(): p for p in right_root.rglob("*") if p.is_file()}
    keys = sorted(set(left_files) | set(right_files))
    changed: list[str] = []
    for key in keys:
        left = left_files.get(key)
        right = right_files.get(key)
        if left is None or right is None:
            changed.append(key)
            continue
        if left.read_bytes() != right.read_bytes():
            changed.append(key)
    return changed


def write_reports(records: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, str]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    base = REPORTS_DIR / "20260706_post_release_micro_hotfix_candidate_v1"
    summary_path = base.with_name(base.name + "_summary.json")
    jsonl_path = base.with_suffix(".jsonl")
    md_path = base.with_suffix(".md")

    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "# Post-release Micro-hotfix Candidate v1",
        "",
        f"- Source baseline: `{summary['source_baseline']}`",
        f"- Candidate: `{summary['candidate']}`",
        f"- Applied to candidate: `{summary['applied_count']}`",
        f"- Blocked: `{summary['blocked_count']}`",
        f"- Changed files: `{summary['changed_file_count']}`",
        "",
        "## Items",
        "",
    ]
    for record in records:
        lines.append(f"### {record['source_key']}")
        lines.append("")
        lines.append(f"- Status: `{'applied_to_candidate' if record['applied'] else 'blocked'}`")
        lines.append(f"- Segment: `{record['segment_id']}`")
        lines.append(f"- File: `{record['relative_path']}`")
        lines.append(f"- Risk: `{record['risk']}`")
        lines.append(f"- Token signature equal: `{record['token_signature_equal']}`")
        if record["block_reason"]:
            lines.append(f"- Block reason: `{record['block_reason']}`")
        lines.append("")
        lines.append("```diff")
        lines.extend(record["diff_preview"])
        lines.append("```")
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "summary": str(summary_path),
        "jsonl": str(jsonl_path),
        "markdown": str(md_path),
    }


def main() -> int:
    if not SOURCE_ROOT.exists():
        raise SystemExit(f"Missing source baseline: {SOURCE_ROOT}")
    if CANDIDATE_ROOT.exists():
        raise SystemExit(f"Candidate already exists: {CANDIDATE_ROOT}")
    CANDIDATE_ROOT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE_ROOT, CANDIDATE_ROOT)

    records = [validate_item(CANDIDATE_ROOT, item) for item in ITEMS]
    changed = changed_files(SOURCE_ROOT, CANDIDATE_ROOT)
    summary = {
        "schema_version": 1,
        "report_type": "post_release_micro_hotfix_candidate",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_baseline": str(SOURCE_ROOT.relative_to(ROOT)),
        "candidate": str(CANDIDATE_ROOT.relative_to(ROOT)),
        "source_file_count": count_files(SOURCE_ROOT),
        "candidate_file_count": count_files(CANDIDATE_ROOT),
        "record_count": len(records),
        "applied_count": sum(1 for record in records if record["applied"]),
        "blocked_count": sum(1 for record in records if record["blocked"]),
        "changed_file_count": len(changed),
        "changed_files": changed,
        "guards": {
            "source_changed": False,
            "output_changed": False,
            "segment_state": 0,
            "reindex": 0,
            "production_full": 0,
        },
        "requires_visual_validation": True,
        "can_publish_without_validation": False,
    }
    outputs = write_reports(records, summary)
    print(json.dumps({"summary": summary, "outputs": outputs}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
