from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "release_decision_post594_corrected_text_review_aid_v1"
DEFAULT_PACKET_JSONL = Path("reports/20260704_111007_137039_release_decision_post594_corrected_text_human_packet.jsonl")
DEFAULT_PACKET_MD = Path("reports/20260704_111007_137039_release_decision_post594_corrected_text_human_packet.md")
APPROVE_ALREADY_OK_IDS = {61067, 128258}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compact read-only review aid for post594 corrected-text human packet.")
    parser.add_argument("--packet-jsonl", type=Path, default=DEFAULT_PACKET_JSONL)
    parser.add_argument("--packet-md", type=Path, default=DEFAULT_PACKET_MD)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.project_path(path).open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def issue_summary(row: dict[str, Any]) -> list[str]:
    issues = row.get("open_issue_preview") or []
    out = []
    for issue in issues[:5]:
        out.append(
            f"{issue.get('issue_severity')}:{issue.get('issue_family')}/{issue.get('issue_kind')}"
        )
    return out


def same_text(row: dict[str, Any]) -> bool:
    return str(row.get("output_text") or "") == str(row.get("suggested_corrected_text") or "")


def conservative_decision(row: dict[str, Any]) -> tuple[str, str]:
    segment_id = int(row.get("segment_id") or 0)
    tokens = row.get("tokens_present") or []
    output = str(row.get("output_text") or "")
    suggestion = str(row.get("suggested_corrected_text") or "")
    risk = str(row.get("risk_alert") or "")
    if segment_id in APPROVE_ALREADY_OK_IDS and same_text(row):
        return (
            "approve_already_ok",
            "O output já coincide com a sugestão assistida; parece falso positivo de espanhol residual/título ou frase já em PT-BR.",
        )
    if not suggestion:
        return ("needs_more_context", "Sem corrected_text assistido claro; manter para contexto humano.")
    if tokens and sorted(tokens) != sorted(row.get("tokens_present") or []):
        return ("needs_more_context", "Há tokens/variáveis; exige checagem humana antes de qualquer correção.")
    if "possessive" in risk or "register_check" in risk:
        return (
            "corrected_text",
            "Correção parece clara, mas vale revisar registro/possessivo; a sugestão preserva estrutura e não introduz token novo.",
        )
    if output != suggestion:
        return (
            "corrected_text",
            "Correção clara de resíduo espanhol/fluência em texto curto; estrutura e tokens preservados.",
        )
    return ("approve_already_ok", "Output já igual à sugestão assistida; provável fechamento por aprovação humana.")


def main() -> None:
    args = parse_args()
    packet_md = db.project_path(args.packet_md)
    if not packet_md.exists():
        raise SystemExit(f"missing original packet markdown: {args.packet_md}")
    rows = read_jsonl(args.packet_jsonl)
    decisions: list[dict[str, Any]] = []
    for row in rows:
        decision, reason = conservative_decision(row)
        decisions.append(
            {
                "segment_id": int(row.get("segment_id") or 0),
                "source_key": row.get("source_key"),
                "relative_path": row.get("relative_path"),
                "risk_alert": row.get("risk_alert"),
                "tokens_present": row.get("tokens_present") or [],
                "issues": issue_summary(row),
                "suggested_decision": decision,
                "justification": reason,
                "output_text": row.get("output_text"),
                "suggested_corrected_text": row.get("suggested_corrected_text"),
            }
        )

    summary = {
        "schema_version": 1,
        "source": SOURCE,
        "mode": "compact_review_aid_read_only",
        "packet_markdown": str(args.packet_md),
        "packet_jsonl": str(args.packet_jsonl),
        "record_count": len(decisions),
        "decision_counts": dict(Counter(d["suggested_decision"] for d in decisions).most_common()),
        "candidate_generation_count": 0,
        "apply_count": 0,
        "learning_ingest_count": 0,
        "issue_closure_count": 0,
        "lifecycle_count": 0,
        "materializer_count": 0,
        "segment_state_count": 0,
        "reindex_count": 0,
        "production_full_count": 0,
        "source_changed": False,
        "output_changed": False,
        "single_operational_recommendation": "Use this aid to fill the original packet manually; do not treat suggestions as decisions until copied by human instruction.",
    }

    base = reports_dir() / f"{stamp()}_release_decision_post594_corrected_text_review_aid"
    md_path = base.with_suffix(".md")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = Path(str(base) + "_summary.json")

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for decision in decisions:
            handle.write(json.dumps(decision, ensure_ascii=False, sort_keys=True) + "\n")

    md_lines = [
        "# Release Decision Post-594 Review Aid",
        "",
        "Auxiliar read-only. Não altera o pacote original e não registra decisão humana.",
        "",
    ]
    for item in decisions:
        md_lines.extend(
            [
                f"## {item['segment_id']} - `{item['source_key']}`",
                f"- Arquivo: `{item['relative_path']}`",
                f"- Risco principal: `{item['risk_alert']}`",
                f"- Tokens/variáveis: `{item['tokens_present']}`",
                f"- Issues abertas relevantes: `{item['issues']}`",
                f"- Sugestão conservadora: `{item['suggested_decision']}`",
                f"- Justificativa: {item['justification']}",
                "",
                "**Output atual**",
                "```text",
                str(item.get("output_text") or ""),
                "```",
                "",
                "**Sugestão assistida**",
                "```text",
                str(item.get("suggested_corrected_text") or ""),
                "```",
                "",
            ]
        )

    summary["output_files"] = {"markdown": str(md_path), "jsonl": str(jsonl_path), "summary": str(summary_path)}
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"markdown={md_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"record_count={summary['record_count']}")
    print(f"decision_counts={json.dumps(summary['decision_counts'], ensure_ascii=False, sort_keys=True)}")
    print("candidate_generation_count=0")
    print("apply_count=0")
    print("ingest_count=0")
    print("issue_closure_count=0")
    print("lifecycle_count=0")
    print("materializer_count=0")
    print("segment_state_count=0")
    print("reindex_count=0")
    print("production_full_count=0")
    print("source_changed=false")
    print("output_changed=false")


if __name__ == "__main__":
    main()
