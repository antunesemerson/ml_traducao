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
from pending_architecture_diagnostic import (
    TOKEN_PATTERN,
    domain_family,
    length_bucket,
    latest_run,
    percent,
    sample_text,
    token_bucket,
    top_package,
    word_count,
)


RULE_VERSION = "issue_autofix_unknown_cluster_diagnostic_v1"
FAMILY = "autofix_unknown_microagent"


UI_MARKERS = re.compile(r"#(?:T|I|X|help|S|V)\b|#!|@warning_icon!|@alert_icon!|Clique|Pressione", re.IGNORECASE)
TOOLTIP_KEY_MARKERS = re.compile(r"(?:_tt$|_tt_|_tooltip$|tooltip|_effect$|_valid|_invalid|_reason|_cost)", re.IGNORECASE)
EVENT_KEY_MARKERS = re.compile(r"\.(?:desc|a|b|c|d|e|f|g|h|i|j|k|l|m|n|o|p|q|r|s|t|toast|tt)$", re.IGNORECASE)
BUILDING_KEY_MARKERS = re.compile(r"^(?:building_|duchy_building_|special_building_)", re.IGNORECASE)
RULE_KEY_MARKERS = re.compile(r"(?:_rule$|^game_rule|_effect$|_trigger$|modifier|law|doctrine)", re.IGNORECASE)
QUESTION_MARKERS = re.compile(r"\?$|Tem certeza|Deseja|Você não pode|Não é possível|Cannot|Can not", re.IGNORECASE)
LIST_VALUE_MARKERS = re.compile(
    r"^\s*(?:\$EFFECT_LIST_BULLET\$|\$BULLET(?:_WITH_TAB)?\$|[-•]\s+)"
    r"|(?:\\n|\n)\s*(?:\$EFFECT_LIST_BULLET\$|\$BULLET(?:_WITH_TAB)?\$|[-•]\s+)",
    re.IGNORECASE,
)
COUNTER_KEY_MARKERS = re.compile(r"(?:COUNT|NUM|AMOUNT|TOTAL|_COUNT|_NUM|_AMOUNT|_VALUE|_INVALID|_LIST)", re.IGNORECASE)
SENTENCE_END_MARKERS = re.compile(r"[.!?](?:\\n|$)")
WARNING_MARKERS = re.compile(r"@warning_icon!|#X|Não é possível|Você não pode|cannot|invalid", re.IGNORECASE)
NARRATIVE_DESC_KEY_MARKERS = re.compile(r"(?:\.desc|_desc$|description|_flavor|proposal$)", re.IGNORECASE)


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_autofix_unknown_cluster_diagnostic"
    return base.with_suffix(".txt"), base.with_suffix(".json"), base.with_suffix(".csv")


def fetch_rows(conn, run_id: int | None) -> tuple[int, list[dict[str, Any]]]:
    if run_id is None:
        row = conn.execute("SELECT MAX(run_id) AS run_id FROM ml_issue_ledger_items").fetchone()
        if not row or row["run_id"] is None:
            raise RuntimeError("No ml_issue_ledger_items rows found.")
        run_id = int(row["run_id"])
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND issue_family = ?
        ORDER BY segment_id
        """,
        (run_id, FAMILY),
    ).fetchall()
    return run_id, [dict(row) for row in rows]


def parse_evidence(row: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(row.get("evidence_json") or "{}")
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def text_of(row: dict[str, Any]) -> str:
    return str(row.get("evidence_text") or "")


def surface_cluster(row: dict[str, Any]) -> str:
    text = text_of(row)
    key = str(row.get("source_key") or "")
    path = str(row.get("relative_path") or "").lower()
    token_count = len(TOKEN_PATTERN.findall(text))
    words = word_count(text)

    if WARNING_MARKERS.search(text) and (
        words <= 35 or TOOLTIP_KEY_MARKERS.search(key) or not NARRATIVE_DESC_KEY_MARKERS.search(key)
    ):
        return "ui_warning_or_blocker_text"
    if BUILDING_KEY_MARKERS.search(key) or "building" in path:
        return "building_or_holding_description"
    if "event" in path or EVENT_KEY_MARKERS.search(key):
        if words <= 10 and token_count == 0:
            return "event_short_title_or_option"
        return "event_sentence_or_description"
    if RULE_KEY_MARKERS.search(key) or any(part in path for part in ("effect", "trigger", "modifier", "game_rule")):
        return "rule_effect_or_modifier_text"
    if QUESTION_MARKERS.search(text):
        return "confirmation_or_question_text"
    if LIST_VALUE_MARKERS.search(text) or (COUNTER_KEY_MARKERS.search(key) and words <= 24):
        return "list_value_or_counter_text"
    if UI_MARKERS.search(text) and (
        TOOLTIP_KEY_MARKERS.search(key)
        or words <= 22
        or (len(text) <= 220 and text.count("\n") <= 2 and not NARRATIVE_DESC_KEY_MARKERS.search(key))
    ):
        return "ui_tooltip_or_markup_text"
    if token_count >= 4:
        return "token_dense_explanatory_text"
    if words <= 6 and len(text) <= 80:
        return "compact_label_without_known_issue"
    if SENTENCE_END_MARKERS.search(text) or words >= 12:
        return "plain_sentence_without_known_issue"
    return "misc_unclassified_surface"


def action_hint(cluster: str) -> str:
    return {
        "ui_warning_or_blocker_text": "create_warning_tooltip_style_microagent",
        "ui_tooltip_or_markup_text": "create_ui_markup_tooltip_microagent",
        "building_or_holding_description": "route_to_building_description_semantic_policy",
        "event_short_title_or_option": "route_to_event_surface_short_option_microagent",
        "event_sentence_or_description": "route_to_event_surface_sentence_microagent",
        "rule_effect_or_modifier_text": "route_to_rules_tooltip_microagent",
        "confirmation_or_question_text": "create_confirmation_prompt_style_microagent",
        "list_value_or_counter_text": "create_counter_list_value_microagent",
        "token_dense_explanatory_text": "create_token_dense_explanatory_microagent",
        "compact_label_without_known_issue": "route_to_short_label_style_microagent",
        "plain_sentence_without_known_issue": "route_to_semantic_sentence_review_router",
        "misc_unclassified_surface": "sample_for_manual_cluster_review",
    }.get(cluster, "sample_for_manual_cluster_review")


def authority_hint(cluster: str) -> str:
    if cluster in {
        "ui_warning_or_blocker_text",
        "ui_tooltip_or_markup_text",
        "confirmation_or_question_text",
        "list_value_or_counter_text",
        "compact_label_without_known_issue",
    }:
        return "candidate_shadow"
    if cluster in {
        "building_or_holding_description",
        "event_sentence_or_description",
        "plain_sentence_without_known_issue",
        "rule_effect_or_modifier_text",
        "token_dense_explanatory_text",
    }:
        return "needs_review_queue"
    return "sample_first"


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cluster_counts: Counter[str] = Counter()
    package_counts: Counter[str] = Counter()
    domain_counts: Counter[str] = Counter()
    length_counts: Counter[str] = Counter()
    token_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    cluster_by_package: dict[str, Counter[str]] = defaultdict(Counter)
    cluster_by_domain: dict[str, Counter[str]] = defaultdict(Counter)
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    csv_rows: list[dict[str, Any]] = []

    for row in rows:
        ev = parse_evidence(row)
        text = text_of(row)
        cluster = surface_cluster(row)
        package = str(ev.get("package") or top_package(row.get("relative_path")))
        domain = str(ev.get("domain") or "unknown")
        text_length = int(ev.get("text_length") or len(text))
        token_count_value = int(ev.get("token_count") or len(TOKEN_PATTERN.findall(text)))
        action = action_hint(cluster)
        authority = authority_hint(cluster)

        cluster_counts[cluster] += 1
        package_counts[package] += 1
        domain_counts[domain] += 1
        length_counts[length_bucket(text_length)] += 1
        token_counts[token_bucket(token_count_value)] += 1
        action_counts[action] += 1
        cluster_by_package[cluster][package] += 1
        cluster_by_domain[cluster][domain] += 1

        record = {
            "segment_id": row["segment_id"],
            "relative_path": row["relative_path"],
            "source_key": row["source_key"],
            "cluster": cluster,
            "action_hint": action,
            "authority_hint": authority,
            "package": package,
            "domain": domain,
            "text_length": text_length,
            "word_count": int(ev.get("word_count") or word_count(text)),
            "token_count": token_count_value,
            "text": sample_text(text, 260),
        }
        csv_rows.append(record)
        if len(examples[cluster]) < 8:
            examples[cluster].append(record)

    return {
        "cluster_counts": dict(cluster_counts.most_common()),
        "package_counts": dict(package_counts.most_common(25)),
        "domain_counts": dict(domain_counts.most_common()),
        "length_counts": dict(length_counts.most_common()),
        "token_counts": dict(token_counts.most_common()),
        "action_counts": dict(action_counts.most_common()),
        "cluster_by_package": {
            cluster: dict(counter.most_common(12)) for cluster, counter in cluster_by_package.items()
        },
        "cluster_by_domain": {
            cluster: dict(counter.most_common(12)) for cluster, counter in cluster_by_domain.items()
        },
        "examples": dict(examples),
        "csv_rows": csv_rows,
    }


def build_report(run_id: int, state_run: dict[str, Any], rows: list[dict[str, Any]], summary: dict[str, Any]) -> list[str]:
    total = len(rows)
    lines = [
        "Autofix unknown cluster diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Ledger run id: {run_id}",
        f"Segment-state run id: {state_run['id']}",
        "",
        "Summary:",
        f"- autofix_unknown items: {total:,}",
        f"- distinct segments: {len({row['segment_id'] for row in rows}):,}",
        f"- production authority: none; diagnostic only",
        "",
        "Interpretation:",
        "- This family is a router backlog: token status is already ok, but no specialized microagent currently explains the candidate autofix need.",
        "- Large clusters should become route policies or review queues before any lifecycle closure.",
        "- Candidate-shadow clusters are still evidence-only until sampled and checkpointed.",
        "",
        "Clusters:",
    ]
    for cluster, count in summary["cluster_counts"].items():
        lines.append(
            f"- {cluster}: {count:,} ({percent(count, total):.2f}%) | {action_hint(cluster)} | {authority_hint(cluster)}"
        )

    def add_counter(title: str, values: dict[str, int]) -> None:
        lines.extend(["", f"{title}:"])
        if not values:
            lines.append("- none")
        for label, count in values.items():
            lines.append(f"- {label}: {count:,} ({percent(count, total):.2f}%)")

    add_counter("Packages", summary["package_counts"])
    add_counter("Domains", summary["domain_counts"])
    add_counter("Length buckets", summary["length_counts"])
    add_counter("Token buckets", summary["token_counts"])
    add_counter("Recommended route actions", summary["action_counts"])

    lines.extend(["", "Cluster examples:"])
    for cluster, examples in summary["examples"].items():
        lines.append(f"{cluster}:")
        for example in examples:
            lines.append(
                f"- segment={example['segment_id']} | {example['relative_path']}:{example['source_key']} | "
                f"{example['text']}"
            )

    lines.extend(
        [
            "",
            "Recommended next step:",
            "1. Start with `ui_tooltip_or_markup_text` plus `ui_warning_or_blocker_text`, because they are structured UI surfaces and safer than broad semantic prose.",
            "2. Keep event descriptions and building descriptions as review queues, not auto-close policies.",
            "3. If the UI clusters validate well, promote them into narrow microagents and rerun segment-state to measure net pending reduction.",
        ]
    )
    return lines


def write_outputs(txt_path: Path, json_path: Path, csv_path: Path, payload: dict[str, Any], rows: list[dict[str, Any]], lines: list[str]) -> None:
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "segment_id",
            "relative_path",
            "source_key",
            "cluster",
            "action_hint",
            "authority_hint",
            "package",
            "domain",
            "text_length",
            "word_count",
            "token_count",
            "text",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cluster autofix_unknown_microagent backlog.")
    parser.add_argument("--ledger-run-id", type=int)
    args = parser.parse_args()

    settings = db.load_settings()
    txt_path, json_path, csv_path = report_paths(settings)
    with db.connect(settings) as conn:
        state_run = latest_run(conn, "segment_state_runs")
        run_id, rows = fetch_rows(conn, args.ledger_run_id)

    summary = summarize(rows)
    payload = {
        "rule_version": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "ledger_run_id": run_id,
        "segment_state_run_id": int(state_run["id"]),
        "summary": {key: value for key, value in summary.items() if key != "csv_rows"},
        "total_items": len(rows),
        "distinct_segments": len({row["segment_id"] for row in rows}),
        "safety": {
            "diagnostic_only": True,
            "production_release_allowed": False,
            "writes_output": False,
            "writes_source": False,
        },
    }
    lines = build_report(run_id, state_run, rows, summary)
    write_outputs(txt_path, json_path, csv_path, payload, summary["csv_rows"], lines)

    print("[issue_autofix_unknown_cluster_diagnostic] Diagnostic generated")
    print(f"[issue_autofix_unknown_cluster_diagnostic] Ledger run id: {run_id}")
    print(f"[issue_autofix_unknown_cluster_diagnostic] Items: {len(rows):,}")
    for cluster, count in list(summary["cluster_counts"].items())[:12]:
        print(f"[issue_autofix_unknown_cluster_diagnostic] cluster {cluster}: {count:,}")
    print(f"[issue_autofix_unknown_cluster_diagnostic] Report: {txt_path}")
    print(f"[issue_autofix_unknown_cluster_diagnostic] JSON: {json_path}")
    print(f"[issue_autofix_unknown_cluster_diagnostic] CSV: {csv_path}")


if __name__ == "__main__":
    main()
