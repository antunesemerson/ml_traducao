from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"


SOURCE_REPORT = REPORTS / "20260708_154105_038877_publication_token_mismatch_remaining_rewrite_queue.jsonl"
GLOSSARY_APPROVED = REPORTS / "20260708_155722_814225_publication_token_mismatch_glossary_literal_policy_queue.jsonl"
NICK_LABELS_APPROVED_SUMMARY = REPORTS / "20260708_160047_507966_publication_token_mismatch_nickname_select_policy_queue_labels_only_summary.json"


SPANISH_MARKERS = {
    " el ",
    " la ",
    " los ",
    " las ",
    " una ",
    " un ",
    " y ",
    " de ",
    " que ",
    " para ",
    " con ",
    " su ",
    " sus ",
    " al ",
    " a la ",
    " eres",
    " es ",
    " resultas",
    " resulta",
    " usas",
    " usa",
    " gastas",
    " gasta",
    " ganas",
    " gana",
    " pierdes",
    " pierde",
    " posees",
    " posee",
    " hiciste",
    " hizo",
    " tuviste",
    " tuvo",
}

PT_MARKERS = {
    " é ",
    " está ",
    " estão ",
    " usa ",
    " gasta ",
    " ganha ",
    " perde ",
    " possui ",
    " deixa",
    " deixará",
    " dispensa ",
    " contribuição",
    " envolvidos",
    " fugitiv",
    " justiça",
}


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def short(text: str | None, limit: int = 220) -> str:
    text = (text or "").replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def lower_visible(text: str | None) -> str:
    return f" {(text or '').lower()} "


def contains_spanish_marker(text: str | None) -> bool:
    lower = lower_visible(text)
    return any(marker in lower for marker in SPANISH_MARKERS)


def contains_pt_marker(text: str | None) -> bool:
    lower = lower_visible(text)
    return any(marker in lower for marker in PT_MARKERS)


def select_cstring_count(text: str | None) -> int:
    return len(re.findall(r"Select_CString\s*\(", text or ""))


def dynamic_scope_count(text: str | None) -> int:
    return len(re.findall(r"\[[^\]]+\.(?:Custom|LocalPlayerString|GetShortUIName|GetShortUINameNoTooltip|GetUIName|GetName)[^\]]*\]", text or ""))


def load_approved_ids() -> set[int]:
    approved = {int(r["segment_id"]) for r in read_jsonl(GLOSSARY_APPROVED)}
    if NICK_LABELS_APPROVED_SUMMARY.exists():
        data = json.loads(NICK_LABELS_APPROVED_SUMMARY.read_text(encoding="utf-8"))
        approved.update(int(x) for x in data.get("ids", []))
    return approved


def classify(row: dict) -> tuple[str, str]:
    segment_id = int(row["segment_id"])
    key = row.get("source_key", "")
    path = row.get("relative_path", "")
    src = row.get("spanish_text") or ""
    conf = row.get("confirmed_text") or ""
    route = row.get("rewrite_route")

    if segment_id in {23044, 23045} and key.startswith("Loc_ES_el_GetShortUIName"):
        return "ptbr_article_custom_loc_omission", "ready_policy_exception"

    if route == "rewrite_select_cstring_current_tokens":
        src_selects = select_cstring_count(src)
        conf_selects = select_cstring_count(conf)
        if conf_selects > 0 and contains_pt_marker(conf) and not contains_spanish_marker(conf):
            return "translated_select_cstring_literal", "ready_policy_exception"
        if src_selects > 0 and conf_selects == 0 and contains_pt_marker(conf) and not contains_spanish_marker(conf):
            return "neutralized_select_cstring_with_explicit_subject", "ready_policy_exception"
        if "nicknames" in path and key.endswith("_desc"):
            return "nickname_description_select_context", "hold_context"
        return "select_cstring_manual_review", "hold_context"

    if route == "rewrite_dynamic_scope_current_tokens":
        if "LocalPlayerString" in src and "LocalPlayerString" not in conf and contains_pt_marker(conf) and not contains_spanish_marker(conf):
            return "neutralized_local_player_string_with_explicit_subject", "ready_policy_exception"
        if dynamic_scope_count(src) >= 1 and dynamic_scope_count(conf) >= 1 and not contains_spanish_marker(conf):
            return "dynamic_scope_ptbr_visible_ok", "ready_policy_exception"
        return "dynamic_scope_manual_review", "hold_context"

    return "mixed_token_manual_review", "hold_context"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    approved = load_approved_ids()
    source_rows = [r for r in read_jsonl(SOURCE_REPORT) if int(r["segment_id"]) not in approved]

    rows = []
    decisions = []
    for row in source_rows:
        policy_family, status = classify(row)
        item = dict(row)
        item["policy_family"] = policy_family
        item["policy_status"] = status
        item["source_short"] = short(item.get("spanish_text"))
        item["confirmed_short"] = short(item.get("confirmed_text"))
        rows.append(item)
        if status == "ready_policy_exception" and (args.limit <= 0 or len(decisions) < args.limit):
            decisions.append(
                {
                    "policy_item_id": int(row["policy_item_id"]),
                    "segment_id": int(row["segment_id"]),
                    "decision": "accept_policy_candidate",
                    "corrected_text": row.get("confirmed_text"),
                    "note": f"{policy_family}: PT-BR output intentionally uses a stable visible form while preserving runtime meaning.",
                }
            )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = REPORTS / f"{stamp}_publication_token_mismatch_ptbr_dynamic_policy_queue"
    jsonl_path = base.with_suffix(".jsonl")
    csv_path = base.with_suffix(".csv")
    md_path = base.with_suffix(".md")
    summary_path = Path(str(base) + "_summary.json")
    decisions_path = Path(str(base) + "_decisions.jsonl")

    with jsonl_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "segment_id",
                "policy_item_id",
                "policy_status",
                "policy_family",
                "rewrite_route",
                "relative_path",
                "source_key",
                "source_short",
                "confirmed_short",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in writer.fieldnames})

    with decisions_path.open("w", encoding="utf-8") as f:
        for row in decisions:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    status_counts = Counter(row["policy_status"] for row in rows)
    family_counts = Counter(row["policy_family"] for row in rows)
    route_counts = Counter(row["rewrite_route"] for row in rows)
    summary = {
        "source": "publication_token_mismatch_ptbr_dynamic_policy_queue_v1",
        "remaining_input_count": len(source_rows),
        "ready_policy_exception_count": status_counts.get("ready_policy_exception", 0),
        "hold_context_count": status_counts.get("hold_context", 0),
        "decision_count": len(decisions),
        "status_counts": dict(status_counts),
        "family_counts": dict(family_counts),
        "route_counts": dict(route_counts),
        "outputs": {
            "jsonl": str(jsonl_path),
            "csv": str(csv_path),
            "markdown": str(md_path),
            "decisions_jsonl": str(decisions_path),
        },
        "guards": {
            "read_only": True,
            "db_writes": 0,
            "output_writes": 0,
            "segment_state": 0,
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    with md_path.open("w", encoding="utf-8") as f:
        f.write("# Publication Token Mismatch PT-BR Dynamic Policy Queue\n\n")
        f.write("Read-only classification for remaining publication token mismatches after old-safe fixes and approved literal policies.\n\n")
        f.write("## Summary\n\n")
        for key in ["remaining_input_count", "ready_policy_exception_count", "hold_context_count", "decision_count"]:
            f.write(f"- `{key}`: `{summary[key]}`\n")
        f.write("\n## Families\n\n")
        for key, value in family_counts.most_common():
            f.write(f"- `{key}`: `{value}`\n")
        f.write("\n## Ready Policy Exceptions\n\n")
        for row in rows:
            if row["policy_status"] != "ready_policy_exception":
                continue
            f.write(f"### {row['segment_id']} - `{row['source_key']}`\n\n")
            f.write(f"- family: `{row['policy_family']}`\n")
            f.write(f"- route: `{row['rewrite_route']}`\n")
            f.write(f"- path: `{row['relative_path']}`\n")
            f.write(f"- source: `{row['source_short']}`\n")
            f.write(f"- confirmed: `{row['confirmed_short']}`\n\n")
        f.write("\n## Holds\n\n")
        for row in rows:
            if row["policy_status"] == "ready_policy_exception":
                continue
            f.write(f"- `{row['segment_id']}` `{row['policy_family']}` `{row['source_key']}`\n")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
