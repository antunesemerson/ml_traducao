from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from issue_event_short_phrase_guarded_noop_checkpoint import mask_ck3_references


RULE_VERSION = "issue_event_surface_subcluster_report_v3"
DEFAULT_BLOCK_REASON = "not_requirement_tooltip_surface"


def latest_event_checkpoint_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_event_short_phrase_checkpoint_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished event short phrase checkpoint run found.")
    return int(row["id"])


def fetch_event_run(conn, *, run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_event_short_phrase_checkpoint_runs
        WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Event checkpoint run not found: {run_id}")
    return dict(row)


def short(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def clean_visible(text: str | None) -> str:
    visible = mask_ck3_references(text or "")
    visible = re.sub(r"\s+", " ", visible).strip()
    return visible


def normalize_text(text: str | None) -> str:
    visible = clean_visible(text).lower()
    visible = re.sub(r"#[a-z0-9_!]+", " ", visible, flags=re.IGNORECASE)
    visible = re.sub(r"[^a-z0-9a-zA-ZÀ-ÿ]+", " ", visible)
    return re.sub(r"\s+", " ", visible).strip()


def top_package(relative_path: str | None) -> str:
    value = relative_path or "unknown"
    return value.split("/", 1)[0] if "/" in value else value


def path_domain(relative_path: str | None) -> str:
    path = (relative_path or "").lower()
    if "event" in path:
        return "events"
    if "activity" in path or "activities" in path:
        return "activities"
    if "dlc" in path:
        return "dlc"
    if "gui" in path or "interface" in path:
        return "interface"
    if "decision" in path:
        return "decisions"
    if "interaction" in path:
        return "interactions"
    if "modifier" in path:
        return "modifiers"
    return top_package(relative_path)


def key_surface(source_key: str | None) -> str:
    key = source_key or ""
    lower = key.lower()
    if lower == "debug" or lower.startswith("debug") or "_debug" in lower or ".debug" in lower or "debug_gui" in lower:
        return "debug_or_meta_key"
    if re.search(r"(\.tt(?:\.|_|$)|_tt(?:_|$)|tooltip|trigger_failure|not_enough|need_|needs_|required|requirement|unavailable|already_|cooldown|unlock|cost)", lower):
        return "requirement_or_tooltip_key"
    if ".flavor" in lower or lower.endswith("_flavor"):
        return "flavor_key"
    if (
        lower.endswith(".t")
        or lower.endswith("_t")
        or lower.endswith(".title")
        or lower.endswith("_title")
        or re.search(r"(?:^|[._])t(?:[._]|$)", lower)
    ):
        return "title_key"
    if ".desc" in lower or lower.endswith("_desc") or lower.endswith(".desc"):
        return "description_key"
    if ".success" in lower or "_success" in lower:
        return "success_key"
    if ".failure" in lower or "_failure" in lower or "_fail" in lower:
        return "failure_key"
    if ".modifier" in lower or "_modifier" in lower:
        return "modifier_key"
    if ".opt_out" in lower or "opt_out" in lower:
        return "opt_out_key"
    if re.search(r"\.[a-z](?:$|\.)", lower) or re.search(r"_[a-z]$", lower):
        return "option_letter_key"
    return "other_key"


def text_signature(text: str | None, *, char_count: int, token_count: int) -> str:
    visible = clean_visible(text)
    lower = visible.lower()
    if not visible:
        return "empty_visible"
    if token_count > 5:
        return "token_dense_surface"
    if char_count > 180:
        return "long_context_sentence"
    if re.match(r"^experi[êe]ncia mensal\b", lower):
        return "monthly_experience_label"
    if re.match(r"^fa[çc]o um juramento\b", lower):
        return "oath_formula"
    if re.match(r"^(ganha|ganhe|recebe|receba|obt[eé]m|perde|aumenta|reduz|adiciona|remove|custa|paga|concede|d[aá])\b", lower):
        return "effect_or_reward_phrase"
    if re.match(r"^(voc[eê]|precisa|requer|n[aã]o pode|n[aã]o tem|j[aá] existe|deve estar|deve ter)\b", lower):
        return "requirement_like_phrase"
    if re.match(r"^(eu|meu|minha|n[oó]s|vamos|deixe|diga|quero|posso|preciso|farei|aceito|recuso)\b", lower):
        return "dialogue_option_phrase"
    if re.match(r"^(sim|n[aã]o|talvez|claro|muito bem|excelente|perfeito)[.!?]?$", lower):
        return "short_dialogue_ack"
    if char_count <= 45 and token_count <= 4 and not re.search(r"[.!?;:]", visible):
        return "plain_short_label"
    if char_count <= 90 and token_count <= 6 and re.search(r"[.!?]$", visible):
        return "short_sentence"
    if char_count <= 90 and token_count <= 6:
        return "short_phrase"
    return "mixed_event_surface"


def recommended_next_step(row: dict[str, Any]) -> tuple[str, str]:
    signature = row["text_signature"]
    key = row["key_surface"]
    if key == "debug_or_meta_key":
        return "micro_event_surface_router", "hold_debug_or_meta_surface"
    if key == "requirement_or_tooltip_key":
        return "micro_requirement_tooltip_surface", "tighten_existing_requirement_gate"
    if key in {"title_key", "description_key", "flavor_key"}:
        return "micro_event_context_composer", "route_to_contextual_event_review"
    if key in {"success_key", "failure_key"}:
        if signature == "plain_short_label":
            return "micro_short_label_style", "try_guarded_outcome_label_bridge"
        return "micro_event_context_composer", "route_outcome_sentence_to_context_review"
    if key == "modifier_key":
        return "micro_effect_reward_phrase", "create_modifier_or_effect_phrase_policy"
    if key == "opt_out_key":
        return "micro_event_dialogue_option", "create_dialogue_option_review_queue"
    if signature == "monthly_experience_label":
        return "micro_modifier_label_surface", "candidate_noop_or_glossary_label_policy"
    if signature == "oath_formula":
        return "micro_event_dialogue_formula", "create_formulaic_dialogue_review_queue"
    if key == "option_letter_key" or signature in {"dialogue_option_phrase", "short_dialogue_ack"}:
        return "micro_event_dialogue_option", "create_dialogue_option_review_queue"
    if signature == "effect_or_reward_phrase":
        return "micro_effect_reward_phrase", "create_effect_phrase_policy_or_queue"
    if signature == "requirement_like_phrase":
        return "micro_requirement_tooltip_surface", "tighten_existing_requirement_gate"
    if signature == "plain_short_label":
        return "micro_short_label_style", "try_guarded_short_label_bridge"
    if signature in {"long_context_sentence", "mixed_event_surface"}:
        return "micro_event_context_composer", "route_to_contextual_event_review"
    if signature == "token_dense_surface":
        return "micro_dynamic_ck3_expression", "route_to_dynamic_token_context"
    return "micro_event_surface_router", "needs_subcluster_review"


def fetch_rows(conn, *, run_id: int, block_reason: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_event_short_phrase_checkpoint_items
        WHERE run_id = ?
          AND checkpoint_allowed = 0
          AND block_reason = ?
        ORDER BY relative_path, source_line_number, source_key
        """,
        (run_id, block_reason),
    ).fetchall()
    enriched: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        row["path_domain"] = path_domain(row.get("relative_path"))
        row["top_package"] = top_package(row.get("relative_path"))
        row["key_surface"] = key_surface(row.get("source_key"))
        row["text_signature"] = text_signature(
            row.get("current_text"),
            char_count=int(row.get("char_count") or 0),
            token_count=int(row.get("token_count") or 0),
        )
        agent, action = recommended_next_step(row)
        row["recommended_agent"] = agent
        row["recommended_action"] = action
        row["visible_text"] = clean_visible(row.get("current_text"))
        row["normalized_prefix"] = " ".join(normalize_text(row.get("current_text")).split()[:6])
        row["cluster_key"] = "|".join(
            [
                row["text_signature"],
                row["key_surface"],
                row["path_domain"],
                row["recommended_agent"],
            ]
        )
        enriched.append(row)
    return enriched


def report_paths(settings: dict[str, Any], run_id: int, block_reason: str) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_reason = re.sub(r"[^A-Za-z0-9_]+", "_", block_reason).strip("_")
    base = reports_dir / f"{stamp}_issue_event_surface_subcluster_report_run_{run_id}_{safe_reason}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def sample_rows(rows: list[dict[str, Any]], *, key: str, value: str, limit: int) -> list[dict[str, Any]]:
    return [row for row in rows if str(row.get(key) or "") == value][:limit]


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    event_run: dict[str, Any],
    rows: list[dict[str, Any]],
    block_reason: str,
    sample_limit: int,
) -> None:
    counters = {
        "text_signature": Counter(row["text_signature"] for row in rows),
        "key_surface": Counter(row["key_surface"] for row in rows),
        "path_domain": Counter(row["path_domain"] for row in rows),
        "recommended_agent": Counter(row["recommended_agent"] for row in rows),
        "cluster_key": Counter(row["cluster_key"] for row in rows),
        "normalized_prefix": Counter(row["normalized_prefix"] for row in rows if row["normalized_prefix"]),
    }

    cluster_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if len(cluster_samples[row["cluster_key"]]) < sample_limit:
            cluster_samples[row["cluster_key"]].append(row)

    fields = [
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "block_reason",
        "path_domain",
        "top_package",
        "key_surface",
        "text_signature",
        "recommended_agent",
        "recommended_action",
        "cluster_key",
        "char_count",
        "token_count",
        "visible_text",
        "current_text",
        "corrected_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps({field: row.get(field) for field in fields}, ensure_ascii=False, sort_keys=True) + "\n")

    lines: list[str] = []
    lines.append("Issue event surface subcluster report")
    lines.append(f"Rule version: {RULE_VERSION}")
    lines.append(f"Event checkpoint run: {event_run['id']}")
    lines.append(f"Dry-run id: {event_run.get('dry_run_id')}")
    lines.append(f"Ledger run id: {event_run.get('ledger_run_id')}")
    lines.append(f"Block reason: {block_reason}")
    lines.append(f"Rows analyzed: {len(rows)}")
    lines.append("")

    for name in ("text_signature", "key_surface", "path_domain", "recommended_agent"):
        lines.append(f"Top {name}")
        for value, count in counters[name].most_common(25):
            lines.append(f"- {value}: {count}")
        lines.append("")

    lines.append("Top cluster keys")
    for value, count in counters["cluster_key"].most_common(30):
        lines.append(f"- {value}: {count}")
        for sample in cluster_samples[value][: min(sample_limit, 3)]:
            lines.append(
                "  "
                + f"{sample['segment_id']} {sample['relative_path']}:{sample.get('source_line_number')} "
                + f"{sample['source_key']} :: {short(sample['visible_text'], 140)}"
            )
    lines.append("")

    lines.append("Top repeated visible prefixes")
    for value, count in counters["normalized_prefix"].most_common(40):
        lines.append(f"- {value}: {count}")
    lines.append("")

    lines.append("Recommended focus")
    for agent, count in counters["recommended_agent"].most_common():
        action_counts = Counter(
            row["recommended_action"] for row in rows if row["recommended_agent"] == agent
        )
        action_text = ", ".join(f"{action}={amount}" for action, amount in action_counts.most_common())
        lines.append(f"- {agent}: {count} ({action_text})")

    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only subcluster report for event surface blockers."
    )
    parser.add_argument("--run-id", type=int, default=None)
    parser.add_argument("--block-reason", default=DEFAULT_BLOCK_REASON)
    parser.add_argument("--sample-limit", type=int, default=5)
    args = parser.parse_args()

    settings = db.load_settings()
    conn = db.connect(settings)
    try:
        run_id = args.run_id if args.run_id is not None else latest_event_checkpoint_run_id(conn)
        event_run = fetch_event_run(conn, run_id=run_id)
        rows = fetch_rows(conn, run_id=run_id, block_reason=args.block_reason)
        txt_path, csv_path, jsonl_path = report_paths(settings, run_id, args.block_reason)
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            event_run=event_run,
            rows=rows,
            block_reason=args.block_reason,
            sample_limit=args.sample_limit,
        )
    finally:
        conn.close()

    print(f"Event checkpoint run: {run_id}")
    print(f"Rows analyzed: {len(rows)}")
    print(f"Report: {txt_path}")
    print(f"CSV: {csv_path}")
    print(f"JSONL: {jsonl_path}")


if __name__ == "__main__":
    main()
