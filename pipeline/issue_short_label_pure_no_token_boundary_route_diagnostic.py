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


RULE_VERSION = "issue_short_label_pure_no_token_boundary_route_diagnostic_v4"
DIAGNOSTIC_NAME = "short_label_pure_no_token_boundary_routes"


EVENT_KEY_RE = re.compile(r"\.\d+\.[a-z](?:\.|$)", flags=re.IGNORECASE)
WORD_RE = re.compile(r"[A-Za-zÀ-ÿ0-9]+", flags=re.UNICODE)


def latest_shadow_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_short_label_pure_no_token_shadow_policy_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No finished pure no-token shadow policy run found.")
    return int(row["id"])


def fetch_shadow_run(conn, *, run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_pure_no_token_shadow_policy_runs
        WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Pure no-token shadow policy run not found: {run_id}")
    return dict(row)


def report_base(settings: dict[str, Any], shadow_run_id: int) -> Path:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return reports_dir / f"{stamp}_issue_short_label_pure_no_token_boundary_routes_shadow_{shadow_run_id}"


def ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_short_label_pure_no_token_boundary_route_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            diagnostic_name TEXT NOT NULL,
            shadow_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            inspected_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            shadow_ready_count INTEGER NOT NULL DEFAULT 0,
            route_counts_json TEXT,
            agent_counts_json TEXT,
            action_counts_json TEXT,
            reason_counts_json TEXT,
            priority_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ml_issue_short_label_pure_no_token_boundary_route_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            shadow_item_id INTEGER NOT NULL,
            shadow_run_id INTEGER NOT NULL,
            ledger_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            shadow_status TEXT NOT NULL,
            shadow_decision TEXT NOT NULL,
            shadow_reason TEXT NOT NULL,
            route TEXT NOT NULL,
            recommended_agent TEXT NOT NULL,
            recommended_action TEXT NOT NULL,
            priority TEXT NOT NULL,
            path_domain TEXT NOT NULL,
            key_surface TEXT NOT NULL,
            text_signature TEXT NOT NULL,
            text_length INTEGER NOT NULL DEFAULT 0,
            word_count INTEGER NOT NULL DEFAULT 0,
            evidence_text TEXT,
            route_notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_short_label_pure_no_token_boundary_route_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_short_label_pure_boundary_items_run
        ON ml_issue_short_label_pure_no_token_boundary_route_items(run_id, route, recommended_agent);

        CREATE INDEX IF NOT EXISTS idx_short_label_pure_boundary_items_segment
        ON ml_issue_short_label_pure_no_token_boundary_route_items(segment_id, shadow_run_id);
        """
    )


def short(value: str | None, limit: int = 150) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def normalize_visible(text: str | None) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def word_count(text: str | None) -> int:
    return len(WORD_RE.findall(normalize_visible(text)))


def top_package(relative_path: str | None) -> str:
    value = relative_path or "unknown"
    return value.split("/", 1)[0] if "/" in value else value


def path_domain(relative_path: str | None) -> str:
    path = (relative_path or "").lower()
    if "event" in path:
        return "events"
    if "custom_localization" in path:
        return "custom_localization"
    if "trait" in path:
        return "traits"
    if "modifier" in path:
        return "modifiers"
    if "decision" in path:
        return "decisions"
    if "interaction" in path:
        return "interactions"
    if "activity" in path or "activities" in path:
        return "activities"
    if "gui" in path or "interface" in path:
        return "interface"
    if "dlc" in path:
        return "dlc"
    return top_package(relative_path)


def key_surface(source_key: str | None) -> str:
    key = (source_key or "").lower()
    if not key:
        return "unknown_key"
    if key == "debug" or key.startswith("debug") or "_debug" in key or ".debug" in key:
        return "debug_or_meta_key"
    if re.search(r"(\.tt(?:\.|_|$)|_tt(?:_|$)|tooltip|trigger_failure|not_enough|need_|needs_|required|requirement|unavailable|already_|cooldown|unlock|cost)", key):
        return "requirement_or_tooltip_key"
    if ".flavor" in key or key.endswith("_flavor"):
        return "flavor_key"
    if (
        key.endswith(".t")
        or key.endswith("_t")
        or key.endswith(".title")
        or key.endswith("_title")
        or re.search(r"(?:^|[._])t(?:[._]|$)", key)
    ):
        return "title_key"
    if ".desc" in key or key.endswith("_desc"):
        return "description_key"
    if ".success" in key or "_success" in key:
        return "success_key"
    if ".failure" in key or "_failure" in key or "_fail" in key:
        return "failure_key"
    if ".modifier" in key or "_modifier" in key:
        return "modifier_key"
    if ".opt_out" in key or "opt_out" in key:
        return "opt_out_key"
    if EVENT_KEY_RE.search(key) or re.search(r"\.[a-z](?:$|\.)", key) or re.search(r"_[a-z]$", key):
        return "option_letter_key"
    return "other_key"


def text_signature(text: str | None, *, source_key: str | None) -> str:
    visible = normalize_visible(text)
    lower = visible.lower()
    words = word_count(visible)
    if not visible:
        return "empty"
    if visible.startswith(('"', '\\"')) or visible.endswith(('"', '\\"')):
        return "quoted_fragment"
    if EVENT_KEY_RE.search(source_key or ""):
        return "event_key_surface"
    if re.search(r"[.!?;:]", visible):
        if words <= 5 and len(visible) <= 45:
            return "short_sentence_or_title"
        return "sentence_surface"
    if re.match(r"^(eu|meu|minha|n[oó]s|vamos|deixe|diga|quero|posso|preciso|farei|aceito|recuso|sim|n[aã]o)\b", lower):
        return "dialogue_option_phrase"
    if len(visible) > 90 or words > 12:
        return "long_clause_surface"
    if words <= 5 and len(visible) <= 45:
        return "plain_short_label"
    if words <= 8 and len(visible) <= 70:
        return "compact_phrase"
    return "mixed_sentence_fragment"


def classify_route(row: dict[str, Any]) -> dict[str, str]:
    reason = str(row.get("reason") or "")
    shadow_status = str(row.get("shadow_status") or "")
    relative_path = str(row.get("relative_path") or "")
    source_key = str(row.get("source_key") or "")
    text = str(row.get("evidence_text") or "")

    domain = path_domain(relative_path)
    key_kind = key_surface(source_key)
    signature = text_signature(text, source_key=source_key)

    if shadow_status == "shadow_ready":
        return {
            "route": "nominal_label_checkpointed",
            "recommended_agent": "micro_short_label_style",
            "recommended_action": "keep_as_checkpoint_seed_not_segment_closure",
            "priority": "low",
            "route_notes": "Already safe in shadow policy, but segment-level bridge did not close because broader coverage/gates were not satisfied.",
        }

    if key_kind == "debug_or_meta_key" or "debug" in relative_path.lower():
        return {
            "route": "debug_or_meta_event_surface",
            "recommended_agent": "micro_event_surface_router",
            "recommended_action": "hold_debug_or_meta_surface",
            "priority": "low",
            "route_notes": "Debug/meta event surface should not train or close the dialogue option neuron.",
        }

    if reason == "english_surface_hint":
        return {
            "route": "english_residual_short_label",
            "recommended_agent": "english_residual_microagent",
            "recommended_action": "create_residual_english_repair_queue",
            "priority": "high",
            "route_notes": "Portuguese-looking lane contains visible English hints; use residual-language repair before semantic closure.",
        }

    if reason == "custom_localization_fragment_requires_context" or domain == "custom_localization":
        return {
            "route": "custom_localization_fragment",
            "recommended_agent": "custom_localization_fragment_microagent",
            "recommended_action": "create_fragment_context_policy_or_queue",
            "priority": "medium",
            "route_notes": "Fragment likely depends on caller grammar; should not be judged as a standalone label.",
        }

    if reason == "event_option_or_dialogue_requires_context":
        if key_kind == "requirement_or_tooltip_key":
            return {
                "route": "requirement_tooltip_sentence",
                "recommended_agent": "micro_requirement_tooltip_surface",
                "recommended_action": "route_to_requirement_tooltip_checkpoint",
                "priority": "medium",
                "route_notes": "Event-shaped key is actually requirement/tooltip surface; route away from dialogue option.",
            }
        if key_kind in {"title_key", "description_key", "flavor_key"}:
            return {
                "route": "event_context_sentence",
                "recommended_agent": "micro_event_context_composer",
                "recommended_action": "expand_event_context_sentence_checkpoint",
                "priority": "high",
                "route_notes": "Event title/description/flavor surface needs context composer, not dialogue option.",
            }
        if key_kind in {"success_key", "failure_key"}:
            if signature in {"plain_short_label", "short_sentence_or_title", "compact_phrase", "event_key_surface"}:
                return {
                    "route": "event_outcome_short_label",
                    "recommended_agent": "micro_short_label_style",
                    "recommended_action": "try_event_outcome_short_label_checkpoint",
                    "priority": "high",
                    "route_notes": "Success/failure outcome label is closer to a compact event outcome surface than dialogue.",
                }
            return {
                "route": "event_context_sentence",
                "recommended_agent": "micro_event_context_composer",
                "recommended_action": "expand_event_context_sentence_checkpoint",
                "priority": "high",
                "route_notes": "Success/failure outcome is too contextual for the event dialogue option neuron.",
            }
        return {
            "route": "event_dialogue_option",
            "recommended_agent": "micro_event_dialogue_option",
            "recommended_action": "expand_dialogue_option_context_checkpoint",
            "priority": "high",
            "route_notes": "Event option/dialogue keys need event-level context; existing event dialogue neuron is the best owner.",
        }

    if reason == "quoted_dialogue_or_fragment_requires_context":
        if key_kind == "option_letter_key":
            return {
                "route": "quoted_dialogue_option",
                "recommended_agent": "micro_event_dialogue_option",
                "recommended_action": "expand_quoted_dialogue_option_checkpoint",
                "priority": "high",
                "route_notes": "Quoted option-letter text behaves like an event dialogue option, not event context prose.",
            }
        if key_kind in {"success_key", "failure_key"}:
            return {
                "route": "event_outcome_short_label",
                "recommended_agent": "micro_short_label_style",
                "recommended_action": "try_event_outcome_short_label_checkpoint",
                "priority": "high",
                "route_notes": "Quoted success/failure text is closer to compact event outcome text.",
            }
        if key_kind == "requirement_or_tooltip_key":
            return {
                "route": "requirement_tooltip_sentence",
                "recommended_agent": "micro_requirement_tooltip_surface",
                "recommended_action": "route_to_requirement_tooltip_checkpoint",
                "priority": "medium",
                "route_notes": "Quoted requirement/tooltip text needs the requirement surface neuron.",
            }
        if key_kind in {"title_key", "description_key", "flavor_key"}:
            return {
                "route": "event_context_sentence",
                "recommended_agent": "micro_event_context_composer",
                "recommended_action": "expand_event_context_sentence_checkpoint",
                "priority": "high",
                "route_notes": "Quoted title/description/flavor text needs event context composition.",
            }
        return {
            "route": "quoted_dialogue_fragment",
            "recommended_agent": "micro_event_context_composer",
            "recommended_action": "create_quoted_fragment_context_queue",
            "priority": "high",
            "route_notes": "Quoted text is usually narrative/dialogue, not a stable standalone short label.",
        }

    if reason == "sentence_or_dialogue_surface_requires_context":
        if domain == "traits" and key_kind == "description_key":
            return {
                "route": "trait_short_description",
                "recommended_agent": "trait_description_microagent",
                "recommended_action": "create_trait_short_desc_noop_or_semantic_policy",
                "priority": "medium",
                "route_notes": "Trait descriptions are short sentences but can be checked with trait-specific context.",
            }
        if key_kind == "requirement_or_tooltip_key":
            return {
                "route": "requirement_tooltip_sentence",
                "recommended_agent": "micro_requirement_tooltip_surface",
                "recommended_action": "route_to_requirement_tooltip_checkpoint",
                "priority": "medium",
                "route_notes": "Sentence surface is a requirement/tooltip, not a nominal label.",
            }
        if domain == "events" or key_kind in {"description_key", "flavor_key", "title_key"}:
            return {
                "route": "event_context_sentence",
                "recommended_agent": "micro_event_context_composer",
                "recommended_action": "expand_event_context_sentence_checkpoint",
                "priority": "high",
                "route_notes": "Sentence-like event surface needs event context and composition validation.",
            }
        return {
            "route": "semantic_sentence_context",
            "recommended_agent": "semantic_sentence_context_microagent",
            "recommended_action": "create_contextual_sentence_review_queue",
            "priority": "medium",
            "route_notes": "Sentence-like no-token text needs semantic/context validation rather than label policy.",
        }

    if reason == "long_or_clause_like_no_token_text":
        return {
            "route": "long_clause_context",
            "recommended_agent": "micro_event_context_composer",
            "recommended_action": "route_long_clause_to_context_composer",
            "priority": "medium",
            "route_notes": "Long/clause-like text should be evaluated as narrative or contextual prose.",
        }

    return {
        "route": "unclassified_boundary",
        "recommended_agent": "short_label_router_coordinator",
        "recommended_action": "manual_route_review",
        "priority": "low",
        "route_notes": f"No explicit route mapping for reason={reason}.",
    }


def fetch_items(conn, *, shadow_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_short_label_pure_no_token_shadow_policy_items
        WHERE run_id = ?
        ORDER BY shadow_status, reason, relative_path, source_line_number, segment_id
        """,
        (shadow_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def main(*, shadow_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = db.utc_now()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_shadow_run_id = shadow_run_id or latest_shadow_run_id(conn)
        shadow_run = fetch_shadow_run(conn, run_id=selected_shadow_run_id)
        raw_rows = fetch_items(conn, shadow_run_id=selected_shadow_run_id)

        enriched: list[dict[str, Any]] = []
        route_counts: Counter[str] = Counter()
        agent_counts: Counter[str] = Counter()
        action_counts: Counter[str] = Counter()
        reason_counts: Counter[str] = Counter()
        priority_counts: Counter[str] = Counter()
        for row in raw_rows:
            route = classify_route(row)
            domain = path_domain(row.get("relative_path"))
            key_kind = key_surface(row.get("source_key"))
            signature = text_signature(row.get("evidence_text"), source_key=row.get("source_key"))
            text = normalize_visible(row.get("evidence_text"))
            item = {
                **row,
                **route,
                "path_domain": domain,
                "key_surface": key_kind,
                "text_signature": signature,
                "text_length": len(text),
                "word_count": word_count(text),
            }
            route_counts[item["route"]] += 1
            agent_counts[item["recommended_agent"]] += 1
            action_counts[item["recommended_action"]] += 1
            reason_counts[item["reason"]] += 1
            priority_counts[item["priority"]] += 1
            enriched.append(item)

        blocked_count = sum(1 for row in enriched if row["shadow_status"] != "shadow_ready")
        ready_count = len(enriched) - blocked_count
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_short_label_pure_no_token_boundary_route_runs (
                rule_version, diagnostic_name, shadow_run_id, ledger_run_id,
                inspected_count, blocked_count, shadow_ready_count,
                route_counts_json, agent_counts_json, action_counts_json,
                reason_counts_json, priority_counts_json,
                started_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                DIAGNOSTIC_NAME,
                selected_shadow_run_id,
                int(shadow_run["ledger_run_id"]),
                len(enriched),
                blocked_count,
                ready_count,
                json.dumps(dict(route_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(agent_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(action_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(reason_counts), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(priority_counts), ensure_ascii=False, sort_keys=True),
                started_at,
                started_at,
            ),
        )
        run_id = int(cursor.lastrowid)
        created_at = db.utc_now()
        conn.executemany(
            """
            INSERT INTO ml_issue_short_label_pure_no_token_boundary_route_items (
                run_id, shadow_item_id, shadow_run_id, ledger_item_id,
                segment_id, relative_path, source_key, source_line_number,
                shadow_status, shadow_decision, shadow_reason,
                route, recommended_agent, recommended_action, priority,
                path_domain, key_surface, text_signature, text_length, word_count,
                evidence_text, route_notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    row["id"],
                    selected_shadow_run_id,
                    row["ledger_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["shadow_status"],
                    row["decision"],
                    row["reason"],
                    row["route"],
                    row["recommended_agent"],
                    row["recommended_action"],
                    row["priority"],
                    row["path_domain"],
                    row["key_surface"],
                    row["text_signature"],
                    row["text_length"],
                    row["word_count"],
                    row["evidence_text"],
                    row["route_notes"],
                    created_at,
                )
                for row in enriched
            ],
        )

        base = report_base(settings, selected_shadow_run_id)
        txt_path = base.with_suffix(".txt")
        csv_path = base.with_suffix(".csv")
        jsonl_path = base.with_suffix(".jsonl")
        conn.execute(
            """
            UPDATE ml_issue_short_label_pure_no_token_boundary_route_runs
            SET report_path = ?, csv_path = ?, jsonl_path = ?,
                finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (str(txt_path), str(csv_path), str(jsonl_path), db.utc_now(), db.utc_now(), run_id),
        )
        conn.commit()

    fields = [
        "run_id",
        "shadow_item_id",
        "shadow_run_id",
        "ledger_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "shadow_status",
        "shadow_decision",
        "shadow_reason",
        "route",
        "recommended_agent",
        "recommended_action",
        "priority",
        "path_domain",
        "key_surface",
        "text_signature",
        "text_length",
        "word_count",
        "evidence_text",
        "route_notes",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in enriched:
            writer.writerow(
                {
                    "run_id": run_id,
                    "shadow_item_id": row["id"],
                    "shadow_run_id": selected_shadow_run_id,
                    "shadow_decision": row["decision"],
                    "shadow_reason": row["reason"],
                    **{key: row.get(key) for key in fields if key not in {"run_id", "shadow_item_id", "shadow_run_id", "shadow_decision", "shadow_reason"}},
                }
            )
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in enriched:
            payload = {
                "run_id": run_id,
                "shadow_item_id": row["id"],
                "shadow_run_id": selected_shadow_run_id,
                "ledger_item_id": row["ledger_item_id"],
                "segment_id": row["segment_id"],
                "relative_path": row["relative_path"],
                "source_key": row["source_key"],
                "source_line_number": row["source_line_number"],
                "shadow_status": row["shadow_status"],
                "shadow_decision": row["decision"],
                "shadow_reason": row["reason"],
                "route": row["route"],
                "recommended_agent": row["recommended_agent"],
                "recommended_action": row["recommended_action"],
                "priority": row["priority"],
                "path_domain": row["path_domain"],
                "key_surface": row["key_surface"],
                "text_signature": row["text_signature"],
                "text_length": row["text_length"],
                "word_count": row["word_count"],
                "evidence_text": row["evidence_text"],
                "route_notes": row["route_notes"],
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    samples_by_route: dict[str, list[str]] = defaultdict(list)
    for row in enriched:
        if len(samples_by_route[row["route"]]) >= 8:
            continue
        samples_by_route[row["route"]].append(
            f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']} "
            f"| {row['reason']} | {short(row['evidence_text'])}"
        )

    lines = [
        "Short Label Pure No-Token Boundary Route Diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Shadow run id: {selected_shadow_run_id}",
        f"Ledger run id: {shadow_run['ledger_run_id']}",
        f"Inspected: {len(enriched):,}",
        f"Shadow ready: {ready_count:,}",
        f"Blocked needing routing: {blocked_count:,}",
        "",
        "Route counts:",
        *[f"- {key}: {value:,}" for key, value in route_counts.most_common()],
        "",
        "Recommended agent counts:",
        *[f"- {key}: {value:,}" for key, value in agent_counts.most_common()],
        "",
        "Recommended action counts:",
        *[f"- {key}: {value:,}" for key, value in action_counts.most_common()],
        "",
        "Shadow reason counts:",
        *[f"- {key}: {value:,}" for key, value in reason_counts.most_common()],
        "",
        "Priority counts:",
        *[f"- {key}: {value:,}" for key, value in priority_counts.most_common()],
        "",
        "Interpretation:",
        "- This is read-only routing evidence. It does not write output, confirmations, lifecycle closures, or source files.",
        "- The pure no-token lane is mostly not a nominal-label problem anymore; it is a coordinator problem.",
        "- High-impact next work should mature the largest routed specialists instead of promoting the generic no-token policy.",
        "",
        "Samples by route:",
    ]
    for route_name, samples in sorted(samples_by_route.items()):
        lines.append(f"{route_name}:")
        lines.extend(samples)
    lines.extend(["", f"CSV: {csv_path}", f"JSONL: {jsonl_path}"])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("[issue_short_label_pure_no_token_boundary_route_diagnostic] Diagnostic generated")
    print(f"[issue_short_label_pure_no_token_boundary_route_diagnostic] Run id: {run_id}")
    print(f"[issue_short_label_pure_no_token_boundary_route_diagnostic] Shadow run id: {selected_shadow_run_id}")
    print(f"[issue_short_label_pure_no_token_boundary_route_diagnostic] Inspected: {len(enriched):,}")
    print(f"[issue_short_label_pure_no_token_boundary_route_diagnostic] Blocked: {blocked_count:,}")
    print(f"[issue_short_label_pure_no_token_boundary_route_diagnostic] Report: {txt_path}")
    return {
        "run_id": run_id,
        "shadow_run_id": selected_shadow_run_id,
        "ledger_run_id": int(shadow_run["ledger_run_id"]),
        "inspected_count": len(enriched),
        "blocked_count": blocked_count,
        "shadow_ready_count": ready_count,
        "route_counts": dict(route_counts),
        "agent_counts": dict(agent_counts),
        "report_path": str(txt_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Route blocked pure no-token short labels to the right specialist.")
    parser.add_argument("--shadow-run-id", type=int, default=None)
    args = parser.parse_args()
    main(shadow_run_id=args.shadow_run_id)
