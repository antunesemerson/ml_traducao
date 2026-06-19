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
from apply_segment_state_updates import short
from issue_semantic_short_label_pair_assisted_review import classify as assisted_classify
from issue_semantic_short_label_pair_checkpoint import AGENT_KEY, CHECKPOINT_ACTION, PAIR_FAMILIES


RULE_VERSION = "issue_semantic_short_label_pair_guarded_expansion_dry_run_v2"
POLICY_NAME = "semantic_short_label_pair_guarded_expansion_shadow"
DRY_RUN_ACTION = "would_cover_semantic_short_label_pair_safe"
SAFE_DECISION = "safe_short_label"
DEFAULT_GUARD_PROFILE = "balanced_v1"
GUARD_PROFILES = {
    "balanced_v1",
    "ui_only_v2",
    "ui_only_v3",
    "ui_only_v4",
    "ui_only_v5",
    "ui_only_v6",
    "ui_only_v7",
    "ui_only_v8",
    "ui_only_v9",
    "ui_only_v10",
}

UI_ONLY_RISK_KEY_PARTS = (
    ".desc",
    "_desc",
    "desc_",
    "_flavor",
    ".flavor",
    "_memory",
    "memory_",
    "death_",
    "motto",
    "random.",
    ".random",
    "_story",
    "story_",
)

UI_ONLY_SAFE_KEY_PARTS = (
    "_name",
    "_title",
    "_label",
    "_button",
    "_value",
    "_type",
    "_tier",
    "_level",
    "_rank",
    "_modifier",
    "_effect",
    "_requirement",
    "_requirements",
    "_entry",
    "_cost",
    "_confirm",
    "_header",
    "_tab",
    "_tooltip",
    "_tt",
    "_interaction",
    "_law",
    "_laws",
    "_perk",
    "_focus",
    "_faction",
    "_filter",
    "_sort",
    "_building",
    "_slot",
)

UI_ONLY_V3_EXTRA_RISK_KEY_PARTS = (
    "gloss",
    "proposal",
    "debug_log",
    "_log",
)


def report_paths(settings: dict[str, Any], opportunity_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_semantic_short_label_pair_guarded_expansion_dry_run_{opportunity_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_semantic_short_label_pair_guarded_expansion_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            policy_name TEXT NOT NULL,
            policy_status TEXT NOT NULL,
            guard_profile TEXT NOT NULL DEFAULT 'balanced_v1',
            opportunity_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            checkpoint_run_id INTEGER,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            allowed_count INTEGER NOT NULL DEFAULT 0,
            new_allowed_count INTEGER NOT NULL DEFAULT 0,
            already_checkpointed_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            estimated_issue_gain INTEGER NOT NULL DEFAULT 0,
            profile_counts_json TEXT,
            decision_counts_json TEXT,
            block_counts_json TEXT,
            report_path TEXT,
            csv_path TEXT,
            jsonl_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_semantic_short_label_pair_guarded_expansion_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            opportunity_run_id INTEGER NOT NULL,
            opportunity_item_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            profile TEXT NOT NULL,
            classifier_decision TEXT NOT NULL,
            classifier_reason TEXT NOT NULL,
            dry_run_action TEXT NOT NULL,
            dry_run_allowed INTEGER NOT NULL,
            guard_profile TEXT NOT NULL DEFAULT 'balanced_v1',
            already_checkpointed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            char_count INTEGER NOT NULL DEFAULT 0,
            token_count INTEGER NOT NULL DEFAULT 0,
            text_sample TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_semantic_short_label_pair_guarded_expansion_runs(id) ON DELETE CASCADE
        )
        """
    )
    db.ensure_columns(
        conn,
        "ml_issue_semantic_short_label_pair_guarded_expansion_runs",
        [("guard_profile", "TEXT NOT NULL DEFAULT 'balanced_v1'")],
    )
    db.ensure_columns(
        conn,
        "ml_issue_semantic_short_label_pair_guarded_expansion_items",
        [("guard_profile", "TEXT NOT NULL DEFAULT 'balanced_v1'")],
    )


def latest_opportunity_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_semantic_short_label_pair_opportunity_runs
        WHERE finished_at IS NOT NULL
          AND reviewable_count > 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No semantic short-label pair opportunity run found.")
    return int(row["id"])


def fetch_opportunity_run(conn, *, opportunity_run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM ml_issue_semantic_short_label_pair_opportunity_runs
        WHERE id = ?
        """,
        (opportunity_run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Opportunity run not found: {opportunity_run_id}")
    return dict(row)


def latest_checkpoint_run_id(conn, *, ledger_run_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_semantic_short_label_pair_checkpoint_runs
        WHERE finished_at IS NOT NULL
          AND ledger_run_id = ?
          AND allowed_segment_count > 0
        ORDER BY id DESC
        LIMIT 1
        """,
        (ledger_run_id,),
    ).fetchone()
    return int(row["id"]) if row else None


def fetch_checkpointed_segments(conn, *, ledger_run_id: int, checkpoint_run_id: int | None) -> set[int]:
    ceiling_filter = ""
    params: list[Any] = [ledger_run_id]
    if checkpoint_run_id is not None:
        ceiling_filter = "AND run.id <= ?"
        params.append(checkpoint_run_id)
    rows = conn.execute(
        f"""
        SELECT DISTINCT item.segment_id
        FROM ml_issue_semantic_short_label_pair_checkpoint_items item
        JOIN ml_issue_semantic_short_label_pair_checkpoint_runs run
          ON run.id = item.checkpoint_run_id
        WHERE run.finished_at IS NOT NULL
          AND run.ledger_run_id = ?
          AND item.checkpoint_allowed = 1
          {ceiling_filter}
        """,
        tuple(params),
    ).fetchall()
    return {int(row["segment_id"]) for row in rows}


def fetch_reviewable_items(conn, *, opportunity_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ml_issue_semantic_short_label_pair_opportunity_items
        WHERE run_id = ?
          AND review_bucket = 'reviewable'
        ORDER BY profile, relative_path, source_line_number, source_key
        """,
        (opportunity_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_segment_families(conn, *, ledger_run_id: int, segment_ids: set[int]) -> dict[int, tuple[str, ...]]:
    if not segment_ids:
        return {}
    families: dict[int, list[str]] = defaultdict(list)
    ids = sorted(segment_ids)
    batch_size = 900
    for index in range(0, len(ids), batch_size):
        batch = ids[index : index + batch_size]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""
            SELECT segment_id, issue_family
            FROM ml_issue_ledger_items
            WHERE run_id = ?
              AND segment_id IN ({placeholders})
            ORDER BY segment_id, issue_family, issue_kind, id
            """,
            (ledger_run_id, *batch),
        ).fetchall()
        for row in rows:
            families[int(row["segment_id"])].append(str(row["issue_family"]))
    return {segment_id: tuple(values) for segment_id, values in families.items()}


def has_sentence_punctuation(text: str) -> bool:
    return any(mark in text for mark in (".", "!", "?", ":", ";", '"'))


def looks_like_upper_ui_key(source_key: str) -> bool:
    letters = [char for char in source_key if char.isalpha()]
    return bool(letters) and all(char.upper() == char for char in letters)


def looks_like_event_key(source_key: str, relative_path: str) -> bool:
    source_key_lower = source_key.lower()
    relative_path_lower = relative_path.lower()
    if "event_localization/" in relative_path_lower or "_events" in relative_path_lower:
        return True
    if ".events" in source_key_lower:
        return True
    return bool(re.search(r"(^|\.)\d{3,}(\.|$)", source_key_lower))


def starts_with_contextual_pronoun(text: str) -> bool:
    stripped = text.strip().lower()
    return stripped.startswith(
        (
            "eu ",
            "voce ",
            "você ",
            "seu ",
            "sua ",
            "meu ",
            "minha ",
            "alguem ",
            "alguém ",
        )
    )


def has_event_phrase_punctuation(text: str) -> bool:
    stripped = text.strip()
    return "\n" in text or "..." in text or "," in text or bool(re.search(r"[.!?]$", stripped))


def looks_like_event_option_surface(source_key: str) -> bool:
    source_key_lower = source_key.lower()
    if not re.search(r"\.\d{3,}\.[a-z](?:\.|$)", source_key_lower):
        return False
    return ".t" not in source_key_lower[-3:]


def ui_only_block_reason(item: dict[str, Any], *, guard_profile: str) -> str:
    source_key = str(item.get("source_key") or "")
    source_key_lower = source_key.lower()
    relative_path_lower = str(item.get("relative_path") or "").lower()
    text = str(item.get("text_sample") or "")
    text_lower = text.lower()
    profile = str(item.get("profile") or "")
    token_count = int(item.get("token_count") or 0)
    char_count = int(item.get("char_count") or 0)
    word_count = len(text.split())
    has_format_or_token = any(marker in text for marker in ("[", "]", "$", "#", "@"))

    if guard_profile in {"ui_only_v8", "ui_only_v9", "ui_only_v10"}:
        if looks_like_event_key(source_key, relative_path_lower) and (
            has_event_phrase_punctuation(text) or starts_with_contextual_pronoun(text) or word_count > 5
        ):
            return "ui_only_v8_blocks_event_short_phrase_surface"

    if guard_profile in {"ui_only_v9", "ui_only_v10"}:
        if looks_like_event_option_surface(source_key):
            return "ui_only_v9_blocks_event_option_surface"
        if "bane" in source_key_lower:
            return "ui_only_v10_blocks_artifact_bane_semantic_ambiguity"

    risk_parts = UI_ONLY_RISK_KEY_PARTS
    if guard_profile in {"ui_only_v3", "ui_only_v4", "ui_only_v5", "ui_only_v6", "ui_only_v7", "ui_only_v8", "ui_only_v9", "ui_only_v10"}:
        risk_parts = UI_ONLY_RISK_KEY_PARTS + UI_ONLY_V3_EXTRA_RISK_KEY_PARTS

    if guard_profile in {"ui_only_v5", "ui_only_v6", "ui_only_v7", "ui_only_v8", "ui_only_v9", "ui_only_v10"}:
        if "debug" in relative_path_lower or "debug" in source_key_lower:
            return f"{guard_profile}_blocks_debug_surface"
        if re.search(r"\b[\wÀ-ÿ]+ad\s+[ao]\b", text_lower, flags=re.IGNORECASE):
            return f"{guard_profile}_blocks_split_gender_suffix"

    if any(part in source_key_lower for part in risk_parts):
        return "ui_only_blocks_narrative_or_desc_key"

    if guard_profile in {"ui_only_v3", "ui_only_v4", "ui_only_v5", "ui_only_v6", "ui_only_v7", "ui_only_v8", "ui_only_v9", "ui_only_v10"}:
        if source_key_lower.startswith("start_") and "inicial" in text.lower():
            return f"{guard_profile}_blocks_start_false_friend"
        if has_sentence_punctuation(text):
            if has_format_or_token and char_count <= 60 and word_count <= 5:
                return ""
            return f"{guard_profile}_blocks_sentence_punctuation"
        if word_count > 6 and not has_format_or_token:
            return f"{guard_profile}_blocks_phrase_too_long_without_ui_signal"
        if looks_like_upper_ui_key(source_key) and word_count <= 6:
            return ""
        if any(part in source_key_lower for part in UI_ONLY_SAFE_KEY_PARTS):
            if has_format_or_token and char_count <= 100 and word_count <= 8:
                return ""
            if char_count <= 48 and word_count <= 5:
                return ""
            return f"{guard_profile}_safe_key_but_text_too_phrase_like"
        if token_count > 0 and has_format_or_token and char_count <= 90 and word_count <= 8:
            return ""
        if guard_profile in {"ui_only_v4", "ui_only_v5", "ui_only_v6", "ui_only_v7", "ui_only_v8", "ui_only_v9", "ui_only_v10"}:
            return f"{guard_profile}_missing_explicit_ui_signal"
        if profile == "pair_no_token_short_label" and char_count <= 42 and word_count <= 5:
            return ""
        return "ui_only_v3_missing_ui_signal"

    if looks_like_upper_ui_key(source_key):
        return ""
    if any(part in source_key_lower for part in UI_ONLY_SAFE_KEY_PARTS) and (has_format_or_token or char_count <= 64):
        return ""
    if token_count > 0 and has_format_or_token and char_count <= 100 and word_count <= 10:
        return ""
    if profile == "pair_no_token_short_label" and char_count <= 42 and word_count <= 5 and not has_sentence_punctuation(text):
        return ""
    if has_sentence_punctuation(text):
        return "ui_only_blocks_sentence_like_text"
    if word_count > 6:
        return "ui_only_blocks_phrase_too_long_without_ui_signal"
    return "ui_only_missing_ui_signal"


def classify_item(item: dict[str, Any], families: tuple[str, ...], *, guard_profile: str) -> tuple[str, str, int]:
    if families != PAIR_FAMILIES:
        return "blocked", "ledger_pair_mismatch:" + "|".join(families), 0
    decision, reason = assisted_classify(
        {
            "evidence_text": item.get("text_sample") or "",
            "source_key": item.get("source_key") or "",
            "queue_bucket": item.get("profile") or "",
            "token_count": item.get("token_count") or 0,
            "char_count": item.get("char_count") or 0,
        }
    )
    if decision != SAFE_DECISION:
        return decision, reason, 0
    if guard_profile in {"ui_only_v2", "ui_only_v3", "ui_only_v4", "ui_only_v5", "ui_only_v6", "ui_only_v7", "ui_only_v8", "ui_only_v9", "ui_only_v10"}:
        ui_block = ui_only_block_reason(item, guard_profile=guard_profile)
        if ui_block:
            return "needs_domain_context", ui_block, 0
    return decision, reason, 1


def write_outputs(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    run_id: int,
    opportunity_run: dict[str, Any],
    checkpoint_run_id: int | None,
    guard_profile: str,
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "item_id",
        "opportunity_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "profile",
        "classifier_decision",
        "classifier_reason",
        "guard_profile",
        "dry_run_allowed",
        "already_checkpointed",
        "block_reason",
        "char_count",
        "token_count",
        "text_sample",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    allowed = [row for row in rows if int(row["dry_run_allowed"]) == 1]
    new_allowed = [row for row in allowed if int(row["already_checkpointed"]) == 0]
    blocked = [row for row in rows if int(row["dry_run_allowed"]) == 0]
    allowed_by_profile = Counter(row["profile"] for row in allowed)
    new_by_profile = Counter(row["profile"] for row in new_allowed)
    blocked_by_profile = Counter(row["profile"] for row in blocked)
    decision_by_profile = Counter(f"{row['classifier_decision']}|{row['profile']}" for row in rows)

    lines = [
        "Semantic + short-label guarded expansion dry-run",
        f"Rule version: {RULE_VERSION}",
        f"Run id: {run_id}",
        f"Policy name: {POLICY_NAME}",
        f"Guard profile: {guard_profile}",
        "Production release allowed: 0",
        f"Opportunity run id: {opportunity_run['id']}",
        f"Ledger run id: {opportunity_run['ledger_run_id']}",
        f"Checkpoint run id ceiling used for already-covered detection: {checkpoint_run_id or 'all'}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Allowed by guard: {len(allowed):,}",
        f"- Already checkpointed: {sum(int(row['already_checkpointed']) for row in allowed):,}",
        f"- New allowed: {len(new_allowed):,}",
        f"- Blocked: {len(blocked):,}",
        f"- Estimated new issue coverage gain: {len(new_allowed) * 2:,}",
        f"- Estimated new full-segment coverage gain: {len(new_allowed):,}",
        "",
        "Allowed by profile:",
        *[f"- {key}: {value:,}" for key, value in allowed_by_profile.most_common()],
        "",
        "New allowed by profile:",
        *[f"- {key}: {value:,}" for key, value in new_by_profile.most_common()],
        "",
        "Blocked by profile:",
        *[f"- {key}: {value:,}" for key, value in blocked_by_profile.most_common()],
        "",
        "Classifier decisions:",
        *[f"- {key}: {value:,}" for key, value in counts.items() if key.startswith("decision:")],
        "",
        "Decision by profile:",
        *[f"- {key}: {value:,}" for key, value in decision_by_profile.most_common(20)],
        "",
        "Top block reasons:",
        *[f"- {key.removeprefix('block:')}: {value:,}" for key, value in counts.items() if key.startswith("block:")],
        "",
        "New allowed samples:",
    ]
    for row in new_allowed[:40]:
        lines.append(
            f"- {row['profile']} | segment={row['segment_id']} "
            f"{row['relative_path']}:{row['source_line_number']}::{row['source_key']} | "
            f"{short(row.get('text_sample'), 160)}"
        )
    if blocked:
        lines.extend(["", "Blocked samples:"])
        for row in blocked[:40]:
            lines.append(
                f"- {row['classifier_decision']} | {row['block_reason']} | "
                f"segment={row['segment_id']} {row['relative_path']}::{row['source_key']} | "
                f"{short(row.get('text_sample'), 140)}"
            )
    lines.extend(
        [
            "",
            "Safety note:",
            "- Dry-run only: no source/output reads, no confirmation updates, no checkpoint promotion and no lifecycle closure.",
            "- Allowed means the current conservative reviewer would classify the pair as safe_short_label.",
            "- This must feed an audit/review queue before any mass checkpoint or lifecycle bridge.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(
    *,
    opportunity_run_id: int | None = None,
    checkpoint_run_id: int | None = None,
    guard_profile: str = DEFAULT_GUARD_PROFILE,
) -> dict[str, Any]:
    if guard_profile not in GUARD_PROFILES:
        raise ValueError(f"Unknown guard profile: {guard_profile}")
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_opportunity_run_id = opportunity_run_id or latest_opportunity_run_id(conn)
        opportunity_run = fetch_opportunity_run(conn, opportunity_run_id=selected_opportunity_run_id)
        ledger_run_id = int(opportunity_run["ledger_run_id"])
        selected_checkpoint_run_id = checkpoint_run_id or latest_checkpoint_run_id(conn, ledger_run_id=ledger_run_id)
        checkpointed_segments = fetch_checkpointed_segments(
            conn,
            ledger_run_id=ledger_run_id,
            checkpoint_run_id=selected_checkpoint_run_id,
        )
        source_items = fetch_reviewable_items(conn, opportunity_run_id=selected_opportunity_run_id)
        families = fetch_segment_families(
            conn,
            ledger_run_id=ledger_run_id,
            segment_ids={int(row["segment_id"]) for row in source_items},
        )
        rows: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        for source in source_items:
            segment_id = int(source["segment_id"])
            decision, reason, allowed = classify_item(
                source,
                families.get(segment_id, ()),
                guard_profile=guard_profile,
            )
            already_checkpointed = int(segment_id in checkpointed_segments and allowed == 1)
            counts[f"decision:{decision}"] += 1
            counts[f"profile:{source['profile']}"] += 1
            if allowed:
                counts["allowed"] += 1
            else:
                counts["blocked"] += 1
                counts[f"block:{reason}"] += 1
            if already_checkpointed:
                counts["already_checkpointed"] += 1
            rows.append(
                {
                    "opportunity_item_id": int(source["id"]),
                    "ledger_run_id": ledger_run_id,
                    "segment_id": segment_id,
                    "relative_path": source["relative_path"],
                    "source_key": source["source_key"],
                    "source_line_number": source.get("source_line_number"),
                    "profile": source["profile"],
                    "classifier_decision": decision,
                    "classifier_reason": reason,
                    "guard_profile": guard_profile,
                    "dry_run_action": DRY_RUN_ACTION if allowed else "hold_for_review",
                    "dry_run_allowed": int(allowed),
                    "already_checkpointed": already_checkpointed,
                    "block_reason": "" if allowed else reason,
                    "char_count": int(source.get("char_count") or 0),
                    "token_count": int(source.get("token_count") or 0),
                    "text_sample": source.get("text_sample") or "",
                }
            )

        txt_path, csv_path, jsonl_path = report_paths(settings, selected_opportunity_run_id)
        allowed_count = counts["allowed"]
        already_count = counts["already_checkpointed"]
        new_allowed_count = allowed_count - already_count
        now = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            """
            INSERT INTO ml_issue_semantic_short_label_pair_guarded_expansion_runs (
                rule_version,
                policy_name,
                policy_status,
                guard_profile,
                opportunity_run_id,
                ledger_run_id,
                checkpoint_run_id,
                candidate_count,
                allowed_count,
                new_allowed_count,
                already_checkpointed_count,
                blocked_count,
                estimated_issue_gain,
                profile_counts_json,
                decision_counts_json,
                block_counts_json,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                POLICY_NAME,
                "dry_run",
                guard_profile,
                selected_opportunity_run_id,
                ledger_run_id,
                selected_checkpoint_run_id,
                len(rows),
                allowed_count,
                new_allowed_count,
                already_count,
                counts["blocked"],
                new_allowed_count * 2,
                json.dumps(
                    {key.removeprefix("profile:"): value for key, value in counts.items() if key.startswith("profile:")},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                json.dumps(
                    {key.removeprefix("decision:"): value for key, value in counts.items() if key.startswith("decision:")},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                json.dumps(
                    {key.removeprefix("block:"): value for key, value in counts.items() if key.startswith("block:")},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        for row in rows:
            item_cursor = conn.execute(
                """
                INSERT INTO ml_issue_semantic_short_label_pair_guarded_expansion_items (
                    run_id,
                    opportunity_run_id,
                    opportunity_item_id,
                    ledger_run_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    profile,
                    classifier_decision,
                    classifier_reason,
                    guard_profile,
                    dry_run_action,
                    dry_run_allowed,
                    already_checkpointed,
                    block_reason,
                    char_count,
                    token_count,
                    text_sample,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_opportunity_run_id,
                    row["opportunity_item_id"],
                    row["ledger_run_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["profile"],
                    row["classifier_decision"],
                    row["classifier_reason"],
                    row["guard_profile"],
                    row["dry_run_action"],
                    row["dry_run_allowed"],
                    row["already_checkpointed"],
                    row["block_reason"],
                    row["char_count"],
                    row["token_count"],
                    row["text_sample"],
                    now,
                ),
            )
            row["item_id"] = int(item_cursor.lastrowid)
        conn.commit()

    write_outputs(
        txt_path=txt_path,
        csv_path=csv_path,
        jsonl_path=jsonl_path,
        run_id=run_id,
        opportunity_run=opportunity_run,
        checkpoint_run_id=selected_checkpoint_run_id,
        guard_profile=guard_profile,
        rows=rows,
        counts=counts,
    )
    print("[issue_semantic_short_label_pair_guarded_expansion_dry_run] Dry-run generated")
    print(f"[issue_semantic_short_label_pair_guarded_expansion_dry_run] Run id: {run_id}")
    print(f"[issue_semantic_short_label_pair_guarded_expansion_dry_run] Opportunity run id: {selected_opportunity_run_id}")
    print(f"[issue_semantic_short_label_pair_guarded_expansion_dry_run] Guard profile: {guard_profile}")
    print(f"[issue_semantic_short_label_pair_guarded_expansion_dry_run] Candidates: {len(rows):,}")
    print(f"[issue_semantic_short_label_pair_guarded_expansion_dry_run] Allowed: {allowed_count:,}")
    print(f"[issue_semantic_short_label_pair_guarded_expansion_dry_run] Already checkpointed: {already_count:,}")
    print(f"[issue_semantic_short_label_pair_guarded_expansion_dry_run] New allowed: {new_allowed_count:,}")
    print(f"[issue_semantic_short_label_pair_guarded_expansion_dry_run] Estimated issue gain: {new_allowed_count * 2:,}")
    print(f"[issue_semantic_short_label_pair_guarded_expansion_dry_run] Blocked: {counts['blocked']:,}")
    print(f"[issue_semantic_short_label_pair_guarded_expansion_dry_run] Report: {txt_path}")
    return {
        "run_id": run_id,
        "opportunity_run_id": selected_opportunity_run_id,
        "guard_profile": guard_profile,
        "candidate_count": len(rows),
        "allowed_count": allowed_count,
        "already_checkpointed_count": already_count,
        "new_allowed_count": new_allowed_count,
        "estimated_issue_gain": new_allowed_count * 2,
        "blocked_count": counts["blocked"],
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dry-run guarded expansion for semantic + short-label pair coverage.")
    parser.add_argument("--opportunity-run-id", type=int, default=None)
    parser.add_argument("--checkpoint-run-id", type=int, default=None)
    parser.add_argument("--guard-profile", choices=sorted(GUARD_PROFILES), default=DEFAULT_GUARD_PROFILE)
    args = parser.parse_args()
    main(
        opportunity_run_id=args.opportunity_run_id,
        checkpoint_run_id=args.checkpoint_run_id,
        guard_profile=args.guard_profile,
    )
