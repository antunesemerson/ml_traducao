from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "domain_policy_vote_candidate_medium_dynamic_light_architecture_summary"
DIAGNOSTIC_PATH = Path("reports/20260629_121836_585965_domain_policy_vote_candidate_medium_dynamic_light_diagnostic.jsonl")
SUMMARY_PATH = Path("reports/20260629_121836_585965_domain_policy_vote_candidate_medium_dynamic_light_diagnostic_summary.json")
ARCH_CLASSES = {"precisa_arquitetura", "getter_perspective_omitted"}


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def top_counter(counter: Counter[str], limit: int = 50) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_txt(path: Path, summary: dict[str, Any], samples: list[dict[str, Any]]) -> None:
    lines = [
        "domain_policy_vote_candidate medium_dynamic_light architecture summary",
        "",
        "Architecture question:",
        "- How should domain_policy_vote_candidate handle single-token dynamic light surfaces where the issue is getter/scope/perspective preservation rather than plain semantic correction?",
        "",
        f"segment_state_run_id: {summary['segment_state_run_id']}",
        f"architecture_scope_count: {summary['architecture_scope_count']}",
        "",
        "class_counts:",
    ]
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["class_counts"])
    lines.extend(["", "surface_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["surface_counts"])
    lines.extend(["", "token_counts:"])
    lines.extend(f"- {item['count']} | {item['key']}" for item in summary["token_counts"][:30])
    lines.extend(
        [
            "",
            "Suggested architecture decisions:",
            "- Split medium_dynamic_light into a parser-backed dynamic-token lane before human text packets.",
            "- Treat source getter present + output getter absent as getter_perspective_omitted, not low-risk plain text.",
            "- Require explicit perspective policy for GetHerHim/GetSheHe/GetHerHis and scope getter surfaces.",
            "- Allow future dry-runs only after token role is classified as preservable, transformable, or intentionally omitted.",
            "",
            "representative_samples:",
        ]
    )
    for row in samples:
        lines.extend(
            [
                "",
                f"## {row['segment_id']} | {row['surface_bucket']} | {row['operational_class']}",
                f"- token: {row.get('output_token')}",
                f"- source_key: {row.get('source_key')}",
                f"- classification_reason: {row.get('classification_reason')}",
                f"- english_text: {row.get('english_text')}",
                f"- spanish_text: {row.get('spanish_text')}",
                f"- current_output_text: {row.get('current_output_text')}",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def representative_samples(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["surface_bucket"]), str(row["operational_class"]))].append(row)
    sample = []
    for key in sorted(grouped):
        sample.extend(grouped[key][:5])
    return sample


def main() -> None:
    diagnostic_summary = json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
    rows = [row for row in read_jsonl(DIAGNOSTIC_PATH) if row.get("operational_class") in ARCH_CLASSES]
    rows.sort(key=lambda row: (str(row["surface_bucket"]), str(row["operational_class"]), str(row.get("output_token") or ""), int(row["segment_id"])))
    class_counts = Counter(str(row["operational_class"]) for row in rows)
    surface_counts = Counter(str(row["surface_bucket"]) for row in rows)
    token_counts = Counter(str(row.get("output_token") or "") for row in rows)
    samples = representative_samples(rows)
    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_architecture_summary",
        "source": SOURCE,
        "source_diagnostic": str(DIAGNOSTIC_PATH),
        "segment_state_run_id": diagnostic_summary["segment_state_run_id"],
        "lane": "domain_policy_vote_candidate",
        "risk_bucket": "medium_dynamic_light",
        "architecture_scope_count": len(rows),
        "class_counts": top_counter(class_counts),
        "surface_counts": top_counter(surface_counts),
        "token_counts": top_counter(token_counts),
        "special_getter_omission_outside_scope": diagnostic_summary.get("special_getter_omission_outside_scope"),
        "suggested_architecture_decisions": [
            "split medium_dynamic_light into parser-backed dynamic-token lane",
            "classify source getter present plus output getter absent as getter_perspective_omitted",
            "require explicit perspective policy for GetHerHim/GetSheHe/GetHerHis and scope getters",
            "do not generate candidates until token role classification exists",
        ],
        "gates": {
            "candidate_generation": "not_run",
            "apply": "not_run",
            "lifecycle": "not_run",
            "segment_state": "not_run",
            "reindex": "not_run",
            "full_production": "not_run",
        },
        "output_files": {},
    }
    base = reports_dir() / f"{stamp()}_{SOURCE}"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")
    write_jsonl(jsonl_path, rows)
    summary["output_files"] = {
        "txt": str(txt_path),
        "jsonl": str(jsonl_path),
        "summary_json": str(summary_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_txt(txt_path, summary, samples)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
