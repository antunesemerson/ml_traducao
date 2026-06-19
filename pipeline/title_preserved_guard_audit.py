from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "title_preserved_guard_audit_v1"

POSITIVE_LABELS = {"correct", "contextual_exception"}
NEGATIVE_LABELS = {
    "major_fix",
    "minor_fix",
    "rejected",
    "rejected_suggestion",
    "residual_spanish",
    "semantic_error",
    "structure_error",
    "token_mismatch",
}

DIRECTION_MARKERS = {
    "noreste": "Nordeste",
    "sureste": "Sudeste",
    "occidental": "Ocidental",
    "ruta de": "Rota de",
}

EXONYM_MARKERS = {
    "alejandreta": "Alexandreta/Alexandreta PT-BR",
    "azerbaiyan": "Azerbaijao/Azerbaijao PT-BR",
    "basilea": "Basileia",
    "beluchistan": "Beluchistao/Beluchistao PT-BR",
    "bruselas": "Bruxelas",
    "carcasona": "Carcassona/Carcassonne PT-BR",
    "castellon": "Castelao/Castellon PT-BR",
    "colonia": "Colonia/Colonia PT-BR com acento quando aplicavel",
    "egipto": "Egito",
    "finlandia": "Finlandia PT-BR com acento",
    "juzistan": "Khuzestan/Juzistao PT-BR",
    "kurdistan": "Curdistao/Kurdistao PT-BR",
    "luristan": "Luristao/Luristan PT-BR",
    "nankin": "Nanquim/Nanjing conforme politica",
    "normandia": "Normandia PT-BR sem acento espanhol",
    "polonia": "Polonia PT-BR com acento",
    "rusia": "Russia PT-BR",
    "saboya": "Saboia/Savoia",
    "tanger": "Tanger/Tanger PT-BR",
    "venecia": "Veneza",
}

DEMONYM_MARKERS = {
    "norfolques": "norfolques/norfolques espanholizado",
    "bedfordes": "bedfordes espanholizado",
    "oestewalano": "oeste + demonym cultural espanholizado",
    "ipuzcoano": "ipuzcoano espanholizado",
}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def normalize(value: str | None) -> str:
    text = value or ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold()


def percent(part: int, total: int) -> str:
    return f"{(part / total * 100) if total else 0:.2f}%"


def latest_complete_score_run(conn: sqlite3.Connection, model_run_id: int | None = None) -> int | None:
    params: list[Any] = []
    where = "WHERE finished_at IS NOT NULL AND scored_count > 0"
    if model_run_id is not None:
        where += " AND model_run_id = ?"
        params.append(model_run_id)
    row = conn.execute(
        f"""
        SELECT id
        FROM ml_score_runs
        {where}
        ORDER BY id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    return int(row["id"]) if row else None


def latest_segment_state_run(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["id"]) if row else None


def next_lote_number(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT mode
        FROM local_learning_runs
        WHERE mode LIKE 'human_review_%_lote%'
        """
    ).fetchall()
    lote_numbers: list[int] = []
    for row in rows:
        match = re.search(r"lote(\d+)", str(row["mode"] or ""))
        if match:
            lote_numbers.append(int(match.group(1)))
    return (max(lote_numbers) + 1) if lote_numbers else 1


def fetch_title_preserved_rows(conn: sqlite3.Connection, state_run_id: int | None) -> list[dict[str, Any]]:
    state_join = ""
    state_select = "'unknown' AS state_group, 'unknown' AS final_state, 'unknown' AS apply_state"
    params: list[Any] = []
    if state_run_id is not None:
        state_join = "LEFT JOIN segment_state_items st ON st.segment_id = s.id AND st.run_id = ?"
        state_select = "COALESCE(st.state_group, 'unknown') AS state_group, COALESCE(st.final_state, 'unknown') AS final_state, COALESCE(st.apply_state, 'unknown') AS apply_state"
        params.append(state_run_id)

    rows = conn.execute(
        f"""
        WITH labels AS (
            SELECT
                segment_id,
                SUM(CASE WHEN human_label IN ({",".join("?" for _ in POSITIVE_LABELS)}) THEN 1 ELSE 0 END) AS positive_count,
                SUM(CASE WHEN human_label IN ({",".join("?" for _ in NEGATIVE_LABELS)}) THEN 1 ELSE 0 END) AS negative_count,
                GROUP_CONCAT(DISTINCT human_label) AS labels
            FROM local_learning_candidates
            WHERE local_status = 'reviewed_human'
            GROUP BY segment_id
        )
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS output_text,
            COALESCE(l.positive_count, 0) AS positive_count,
            COALESCE(l.negative_count, 0) AS negative_count,
            COALESCE(l.labels, '') AS reviewed_labels,
            {state_select}
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN labels l ON l.segment_id = s.id
        {state_join}
        WHERE s.relative_path = 'titles_l_spanish.yml'
          AND TRIM(COALESCE(s.spanish_text, '')) = TRIM(COALESCE(s.old_text, ''))
          AND TRIM(COALESCE(s.spanish_text, '')) = TRIM(COALESCE(o.portuguese_text, ''))
        """,
        (*sorted(POSITIVE_LABELS), *sorted(NEGATIVE_LABELS), *params),
    ).fetchall()
    return [dict(row) for row in rows]


def score_actions(conn: sqlite3.Connection, run_ids: dict[str, int | None], segment_ids: list[int]) -> dict[int, dict[str, str]]:
    if not segment_ids:
        return {}
    result: dict[int, dict[str, str]] = defaultdict(dict)
    placeholders = ",".join("?" for _ in segment_ids)
    for label, run_id in run_ids.items():
        if run_id is None:
            continue
        rows = conn.execute(
            f"""
            SELECT segment_id, final_action
            FROM ml_score_items
            WHERE run_id = ?
              AND segment_id IN ({placeholders})
            """,
            (run_id, *segment_ids),
        ).fetchall()
        for row in rows:
            result[int(row["segment_id"])][label] = row["final_action"]
    return result


def classify_guards(row: dict[str, Any]) -> list[dict[str, str]]:
    text = row.get("output_text") or row.get("old_text") or row.get("spanish_text") or ""
    normalized = normalize(text)
    source_key = row.get("source_key") or ""
    guards: list[dict[str, str]] = []

    for marker, expected in DIRECTION_MARKERS.items():
        if marker in normalized:
            guards.append(
                {
                    "guard_key": "title_preserved_direction_residual_guard",
                    "marker": marker,
                    "expected_hint": expected,
                }
            )
            break

    for marker, expected in EXONYM_MARKERS.items():
        if re.search(rf"(^|[^a-z]){re.escape(marker)}([^a-z]|$)", normalized):
            guards.append(
                {
                    "guard_key": "title_preserved_exonym_residual_guard",
                    "marker": marker,
                    "expected_hint": expected,
                }
            )
            break

    if "_adj" in source_key or "adj_" in source_key:
        for marker, expected in DEMONYM_MARKERS.items():
            if marker in normalized:
                guards.append(
                    {
                        "guard_key": "title_preserved_demonym_suffix_guard",
                        "marker": marker,
                        "expected_hint": expected,
                    }
                )
                break

    return guards


def candidate_payload(row: dict[str, Any], guard: dict[str, str], score_map: dict[int, dict[str, str]]) -> dict[str, Any]:
    segment_id = int(row["segment_id"])
    reasons = [
        f"rule:{RULE_VERSION}",
        "pattern:title_preserved_old_spanish_output",
        f"guard:{guard['guard_key']}",
        f"marker:{guard['marker']}",
        f"expected_hint:{guard['expected_hint']}",
        f"active_score_action:{score_map.get(segment_id, {}).get('active_336', 'unknown')}",
        f"candidate_score_action:{score_map.get(segment_id, {}).get('candidate_337', 'unknown')}",
        f"title_model_328_action:{score_map.get(segment_id, {}).get('title_342', 'unknown')}",
        f"state_group:{row.get('state_group')}",
        f"final_state:{row.get('final_state')}",
    ]
    return {
        "segment_id": segment_id,
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "source_line_number": row["source_line_number"],
        "source_section": "Title preserved guard audit",
        "focus_group": guard["guard_key"],
        "group_name": guard["guard_key"],
        "candidate_kind": "title_preserved_guard_shadow_review",
        "final_action": "guard_shadow_review",
        "risk_class": "high",
        "model_safe_probability": 0.0,
        "issue_count": 1,
        "token_status": "unknown",
        "english_text": row.get("english_text"),
        "spanish_text": row.get("spanish_text"),
        "old_text": row.get("old_text"),
        "current_output_text": row.get("output_text"),
        "suggested_text": row.get("output_text"),
        "candidate_text": row.get("output_text"),
        "auditor_reasons_json": json.dumps(reasons, ensure_ascii=False),
        "human_label": "pending",
        "corrected_text": None,
        "reason": "",
    }


def build_review_template(
    selected: list[dict[str, Any]],
    report_path: Path,
    first_lote: int,
    batch_size: int,
) -> dict[str, Any]:
    batches = []
    for offset in range(0, len(selected), batch_size):
        rows = selected[offset : offset + batch_size]
        if not rows:
            continue
        batches.append(
            {
                "lote_number": first_lote + len(batches),
                "source_section": "Title preserved guard audit",
                "focus_group": "title_preserved_negative_guards",
                "queue_source": "ml_group_candidate_queue",
                "candidates": rows,
            }
        )
    return {
        "rule_version": "parallel_review_loop_v1",
        "prepared_by": RULE_VERSION,
        "prepared_at": now(),
        "source_type": "ml_group_candidate_queue",
        "score_run_id": None,
        "source_report": str(report_path.relative_to(db.PROJECT_ROOT)).replace("\\", "/"),
        "batch_size": batch_size,
        "batches": batches,
        "instructions": {
            "valid_labels": [
                "correct",
                "contextual_exception",
                "minor_fix",
                "major_fix",
                "semantic_error",
                "residual_spanish",
                "structure_error",
                "token_mismatch",
                "rejected_suggestion",
                "rejected",
            ],
            "recommended_labels": ["residual_spanish", "semantic_error", "correct", "contextual_exception"],
            "label_guidance": {
                "residual_spanish": "Use quando o guard detecta espanhol visivel que deve ser corrigido.",
                "semantic_error": "Use quando o marcador nao e apenas espanhol, mas altera significado/contexto.",
                "correct": "Use somente se o marcador e aceitavel em PT-BR ou nome proprio preservado.",
                "contextual_exception": "Use para excecao segura e especifica que nao deve virar regra ampla.",
            },
            "do_not_run": ["apply-output", "ml-promote-model"],
        },
    }


def main(limit: int = 120, per_guard: int = 40, batch_size: int = 25) -> None:
    settings = db.load_settings()
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = timestamp()
    report_path = reports_dir / f"{stamp}_title_preserved_guard_audit.txt"
    template_path = reports_dir / f"{stamp}_title_preserved_guard_review_template.json"

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        state_run_id = latest_segment_state_run(conn)
        run_ids = {
            "active_336": 336,
            "candidate_337": 337,
            "title_342": 342,
        }
        rows = fetch_title_preserved_rows(conn, state_run_id)
        segment_ids = [int(row["segment_id"]) for row in rows]
        score_map = score_actions(conn, run_ids, segment_ids)
        first_lote = next_lote_number(conn)

    guard_rows: list[tuple[dict[str, Any], dict[str, str]]] = []
    for row in rows:
        for guard in classify_guards(row):
            guard_rows.append((row, guard))

    by_guard: dict[str, list[tuple[dict[str, Any], dict[str, str]]]] = defaultdict(list)
    for row, guard in guard_rows:
        by_guard[guard["guard_key"]].append((row, guard))

    selected_payloads: list[dict[str, Any]] = []
    lines = [
        "Title preserved guard audit",
        f"Started at: {now()}",
        f"Rule version: {RULE_VERSION}",
        f"Segment state run id: {state_run_id or 'none'}",
        "",
        "Scope:",
        "- titles_l_spanish.yml",
        "- spanish_text == old_text == current output_text",
        "- symbolic negative guards only; no output/model writes",
        "",
        "Overall:",
        f"- title preserved rows scanned: {len(rows)}",
        f"- guard hits: {len(guard_rows)}",
        f"- unique guard-hit segments: {len({int(row['segment_id']) for row, _ in guard_rows})}",
        "",
        "By guard:",
    ]

    queue_candidates: list[tuple[int, dict[str, Any], dict[str, str]]] = []
    for guard_key in sorted(by_guard):
        items = by_guard[guard_key]
        counts = Counter()
        markers = Counter()
        examples: list[str] = []
        for row, guard in items:
            sid = int(row["segment_id"])
            actions = score_map.get(sid, {})
            markers[guard["marker"]] += 1
            counts["reviewed_positive"] += int(row.get("positive_count") or 0) > 0
            counts["reviewed_negative"] += int(row.get("negative_count") or 0) > 0
            counts["unreviewed"] += not (int(row.get("positive_count") or 0) or int(row.get("negative_count") or 0))
            counts["active_auto_safe"] += actions.get("active_336") == "auto_safe"
            counts["candidate_auto_safe"] += actions.get("candidate_337") == "auto_safe"
            counts["title_328_auto_safe"] += actions.get("title_342") == "auto_safe"
            counts["pending_state"] += row.get("state_group") == "pending"
            if len(examples) < 8:
                examples.append(
                    f"  - {row['segment_id']} | {row['source_key']} | marker={guard['marker']} | "
                    f"text={row.get('output_text') or ''} | labels={row.get('reviewed_labels') or '-'} | "
                    f"state={row.get('state_group')}/{row.get('final_state')}"
                )
            priority = 0
            priority += 100 if not (int(row.get("positive_count") or 0) or int(row.get("negative_count") or 0)) else 0
            priority += 50 if actions.get("active_336") == "auto_safe" else 0
            priority += 20 if row.get("state_group") == "pending" else 0
            queue_candidates.append((priority, row, guard))

        lines.extend(
            [
                f"- {guard_key}: {len(items)} hits",
                f"  - reviewed positive: {counts['reviewed_positive']}",
                f"  - reviewed negative: {counts['reviewed_negative']}",
                f"  - unreviewed: {counts['unreviewed']}",
                f"  - active score 336 auto_safe: {counts['active_auto_safe']}",
                f"  - candidate score 337 auto_safe: {counts['candidate_auto_safe']}",
                f"  - title model 328 / score 342 auto_safe: {counts['title_328_auto_safe']}",
                f"  - pending in latest segment-state: {counts['pending_state']}",
                f"  - markers: {json.dumps(dict(markers.most_common()), ensure_ascii=False)}",
                "  - sample:",
                *examples,
            ]
        )

    selected_seen: set[tuple[int, str]] = set()
    selected_by_guard = Counter()
    for _, row, guard in sorted(
        queue_candidates,
        key=lambda item: (-item[0], item[2]["guard_key"], item[1]["source_key"] or "", int(item[1]["segment_id"])),
    ):
        guard_key = guard["guard_key"]
        key = (int(row["segment_id"]), guard_key)
        if key in selected_seen:
            continue
        if selected_by_guard[guard_key] >= per_guard:
            continue
        if len(selected_payloads) >= limit:
            break
        selected_seen.add(key)
        selected_by_guard[guard_key] += 1
        selected_payloads.append(candidate_payload(row, guard, score_map))

    payload = build_review_template(selected_payloads, report_path, first_lote=first_lote, batch_size=batch_size)
    template_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines.extend(
        [
            "",
            "Review queue:",
            f"- selected candidates: {len(selected_payloads)}",
            f"- selected by guard: {json.dumps(dict(selected_by_guard), ensure_ascii=False)}",
            f"- template: {template_path}",
            "",
            "Interpretation:",
            "- Negative guards are useful if they catch rows the old broad safe path would release.",
            "- Positive releases should wait until these guards have low or zero conflict with reviewed safe rows.",
            "- This is a shadow audit only; it does not promote a model, confirmation, or output.",
        ]
    )
    report_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    print("[title_preserved_guard_audit] Done")
    print(f"[title_preserved_guard_audit] Guard hits: {len(guard_rows)}")
    print(f"[title_preserved_guard_audit] Review candidates: {len(selected_payloads)}")
    print(f"[title_preserved_guard_audit] Report: {report_path}")
    print(f"[title_preserved_guard_audit] Template: {template_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit title-preserved negative guards without output/model writes.")
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--per-guard", type=int, default=40)
    parser.add_argument("--batch-size", type=int, default=25)
    args = parser.parse_args()
    main(limit=args.limit, per_guard=args.per_guard, batch_size=args.batch_size)
