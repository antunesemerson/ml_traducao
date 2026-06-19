from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short, structural_tokens


RULE_VERSION = "issue_select_cstring_residual_literal_queue_v1"
QUEUE_NAME = "select_cstring_residual_literal_current_pending_v1"
QUEUE_STATUS = "learning_read_only"
PRODUCTION_RELEASE_ALLOWED = 0

TARGET_ISSUE_KINDS = {
    "select_cstring_expression",
    "select_cstring_gender_literal",
    "spanish_residue_in_literal",
    "short_label_dynamic_spanish_literal_reopened",
    "dynamic_expression",
    "custom_localization_expression",
}

SAFE_LITERAL_MAP = {
    "eres": "\u00e9",
    "es": "\u00e9",
    "est\u00e1s": "est\u00e1",
    "estas": "est\u00e1",
    "ganar\u00e1s": "ganhar\u00e1",
    "ganaras": "ganhar\u00e1",
    "ganar\u00e1": "ganhar\u00e1",
    "ganara": "ganhar\u00e1",
    "gana": "ganha",
    "ganas": "ganha",
    "habla": "fala",
    "hablas": "fala",
    "logra": "consegue",
    "logras": "consegue",
    "consigue": "consegue",
    "consigues": "consegue",
    "prefiere": "prefere",
    "prefieres": "prefere",
    "puede": "pode",
    "puedes": "pode",
    "deber\u00eda": "deveria",
    "deberia": "deveria",
    "deber\u00edas": "deveria",
    "deberias": "deveria",
    "heredar\u00e1": "herdar\u00e1",
    "heredara": "herdar\u00e1",
    "heredar\u00e1s": "herdar\u00e1",
    "heredaras": "herdar\u00e1",
    "intentar\u00e1": "tentar\u00e1",
    "intentara": "tentar\u00e1",
    "intentar\u00e1s": "tentar\u00e1",
    "intentaras": "tentar\u00e1",
    "sana y salva": "s\u00e3 e salva",
    "sano y salvo": "s\u00e3o e salvo",
    "se\u00f1ora": "senhora",
    "se\u00f1or": "senhor",
    "gobernadora": "governadora",
    "gobernador": "governador",
    "vasalla": "vassala",
    "vasallo": "vassalo",
    "misionera": "mission\u00e1ria",
    "misionero": "mission\u00e1rio",
    "coemperatriz": "coimperatriz",
    "coemperador": "coimperador",
}

CONTEXT_LITERAL_HINTS = {
    "tus": "seus/suas",
    "sus": "seus/suas",
    "Tus": "Seus/Suas",
    "Sus": "Seus/Suas",
    "te": "se/le/voce",
    "le": "se/le",
    "has": "auxiliary_or_preterite_context",
    "ha": "auxiliary_or_preterite_context",
    "te has": "reflexive_auxiliary_context",
    "se ha": "reflexive_auxiliary_context",
    "ganaste": "preterite_context",
    "gan\u00f3": "preterite_context",
    "gano": "preterite_context",
    "diste": "preterite_context",
    "dio": "preterite_context",
    "se convirti\u00f3": "preterite_context",
    "te convertiste": "preterite_context",
    "se convirti\u00f3": "preterite_context",
    "sigues": "aspect_context",
    "sigue": "aspect_context",
    "te negaste": "preterite_context",
    "se neg\u00f3": "preterite_context",
    "os": "plural_pronoun_context",
    "les": "plural_pronoun_context",
    "hab\u00e9is": "plural_auxiliary_context",
    "han": "plural_auxiliary_context",
    "hija m\u00eda": "family_phrase_context",
    "hijo m\u00edo": "family_phrase_context",
}

SPANISH_MARKERS = {
    "anciana",
    "anciano",
    "ayudaste",
    "ayud\u00f3",
    "comenzaste",
    "comenz\u00f3",
    "congeniasteis",
    "congeniaron",
    "deberia",
    "deberias",
    "dio",
    "diste",
    "eres",
    "es",
    "est\u00e1s",
    "estas",
    "gana",
    "ganas",
    "ganara",
    "ganar\u00e1",
    "ganaras",
    "ganar\u00e1s",
    "ganaste",
    "gan\u00f3",
    "gano",
    "gobernador",
    "gobernadora",
    "ha",
    "habla",
    "hablas",
    "has",
    "heredara",
    "heredar\u00e1",
    "heredaras",
    "heredar\u00e1s",
    "intentar\u00e1",
    "intentara",
    "intentaras",
    "intentar\u00e1s",
    "lealtad",
    "logra",
    "logras",
    "misionera",
    "misionero",
    "prefiere",
    "prefieres",
    "puede",
    "puedes",
    "se ha",
    "se convirti\u00f3",
    "se neg\u00f3",
    "se\u00f1or",
    "se\u00f1ora",
    "sigues",
    "sigue",
    "te",
    "te convertiste",
    "te has",
    "te negaste",
    "tus",
    "sus",
    "vasalla",
    "vasallo",
}

FALSE_POSITIVE_LITERALS = {
    "",
    "a",
    "o",
    "as",
    "os",
    "e",
    "\u00e9",
    "est\u00e1",
    "pode",
    "seu",
    "sua",
    "seus",
    "suas",
    "Sua",
    "Minha",
    "voc\u00ea",
    "voca",
    "vossa",
    "foi",
    "encontrou",
    "conseguiu",
    "disc\u00edpula",
    "disc\u00edpulo",
    "uma mulher",
    "um homem",
    "uma menina",
    "um menino",
}


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def fold(value: str) -> str:
    value = normalize(value).casefold()
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def latest_id(conn, table: str) -> int:
    row = conn.execute(f"SELECT MAX(id) AS id FROM {table}").fetchone()
    if row is None or row["id"] is None:
        raise RuntimeError(f"No rows found in {table}.")
    return int(row["id"])


def split_top_level_args(payload: str) -> list[str]:
    args: list[str] = []
    start = 0
    depth = 0
    in_quote = False
    index = 0
    while index < len(payload):
        char = payload[index]
        if char == "'" and (index == 0 or payload[index - 1] != "\\"):
            in_quote = not in_quote
        elif not in_quote:
            if char == "(":
                depth += 1
            elif char == ")" and depth > 0:
                depth -= 1
            elif char == "," and depth == 0:
                args.append(payload[start:index].strip())
                start = index + 1
        index += 1
    args.append(payload[start:].strip())
    return args


def unquote_literal(value: str) -> str | None:
    value = value.strip()
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1]
    return None


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "\\'") + "'"


def iter_select_cstrings(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    marker = "Select_CString("
    pos = 0
    select_index = 0
    while True:
        start = text.find(marker, pos)
        if start < 0:
            break
        payload_start = start + len(marker)
        depth = 1
        in_quote = False
        index = payload_start
        while index < len(text):
            char = text[index]
            if char == "'" and (index == 0 or text[index - 1] != "\\"):
                in_quote = not in_quote
            elif not in_quote:
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        break
            index += 1
        if depth != 0:
            pos = start + len(marker)
            continue
        raw = text[start : index + 1]
        payload = text[payload_start:index]
        args = split_top_level_args(payload)
        left = unquote_literal(args[1]) if len(args) > 1 else None
        right = unquote_literal(args[2]) if len(args) > 2 else None
        if left is not None and right is not None:
            rows.append(
                {
                    "select_index": select_index,
                    "start": start,
                    "end": index + 1,
                    "raw": raw,
                    "args": args,
                    "condition": args[0].strip() if args else "",
                    "left_literal": left,
                    "right_literal": right,
                }
            )
            select_index += 1
        pos = index + 1
    return rows


def condition_family(condition: str) -> str:
    low = condition.casefold()
    if "islocalplayer" in low and ("or(" in low or "and(" in low):
        return "compound_local_player_branch"
    if "islocalplayer" in low:
        return "local_player_branch"
    if "isfemale" in low:
        return "gender_branch"
    return "other_branch"


def literal_repair(value: str) -> tuple[bool, str, str]:
    clean = normalize(value)
    clean_fold = fold(clean)
    if clean in FALSE_POSITIVE_LITERALS or clean_fold in {fold(item) for item in FALSE_POSITIVE_LITERALS}:
        return False, "", "ptbr_or_empty_literal"
    if clean in CONTEXT_LITERAL_HINTS:
        return True, CONTEXT_LITERAL_HINTS[clean], "needs_context_literal"
    if clean_fold in {fold(key): key for key in CONTEXT_LITERAL_HINTS}:
        original_key = {fold(key): key for key in CONTEXT_LITERAL_HINTS}[clean_fold]
        return True, CONTEXT_LITERAL_HINTS[original_key], "needs_context_literal"
    if clean_fold in SAFE_LITERAL_MAP:
        return True, SAFE_LITERAL_MAP[clean_fold], "safe_literal_map"
    if clean in SAFE_LITERAL_MAP:
        return True, SAFE_LITERAL_MAP[clean], "safe_literal_map"
    if "ñ" in clean.casefold() or "¿" in clean or "¡" in clean:
        return True, "", "spanish_marker_no_safe_map"
    if clean_fold in {fold(item) for item in SPANISH_MARKERS}:
        return True, "", "spanish_marker_no_safe_map"
    return False, "", "no_residual_marker"


def classify_observation(row: dict[str, Any], select_row: dict[str, Any]) -> dict[str, Any]:
    left = select_row["left_literal"]
    right = select_row["right_literal"]
    left_residual, left_repair, left_status = literal_repair(left)
    right_residual, right_repair, right_status = literal_repair(right)
    residual = left_residual or right_residual
    statuses = {left_status, right_status}

    if not residual:
        literal_status = "ignored_no_residual"
    elif "needs_context_literal" in statuses:
        literal_status = "needs_context_microagent"
    elif "spanish_marker_no_safe_map" in statuses:
        literal_status = "needs_new_literal_map_or_review"
    elif left_residual and not left_repair:
        literal_status = "needs_left_literal_review"
    elif right_residual and not right_repair:
        literal_status = "needs_right_literal_review"
    else:
        literal_status = "ready_literal_map_shadow"

    args = list(select_row["args"])
    corrected_select = ""
    corrected_segment = ""
    token_status = "not_applicable"
    if literal_status == "ready_literal_map_shadow":
        args[1] = quote_literal(left_repair if left_residual else left)
        args[2] = quote_literal(right_repair if right_residual else right)
        corrected_select = "Select_CString( " + ", ".join(args) + " )"
        current_text = as_text(row["portuguese_text"])
        corrected_segment = (
            current_text[: int(select_row["start"])]
            + corrected_select
            + current_text[int(select_row["end"]) :]
        )
        token_status = (
            "same_structural_tokens"
            if structural_tokens(current_text) == structural_tokens(corrected_segment)
            else "structural_token_change_review_required"
        )
        if token_status != "same_structural_tokens":
            literal_status = "ready_but_structural_review_required"

    residual_reasons = []
    if left_residual:
        residual_reasons.append(f"left:{left_status}")
    if right_residual:
        residual_reasons.append(f"right:{right_status}")

    return {
        "segment_id": int(row["segment_id"]),
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "source_line_number": row["source_line_number"],
        "select_index": select_row["select_index"],
        "condition": select_row["condition"],
        "condition_family": condition_family(select_row["condition"]),
        "left_literal": left,
        "right_literal": right,
        "left_repair": left_repair,
        "right_repair": right_repair,
        "literal_status": literal_status,
        "token_status": token_status,
        "residual_reasons": residual_reasons,
        "current_select_text": select_row["raw"],
        "corrected_select_text": corrected_select,
        "current_text": as_text(row["portuguese_text"]),
        "corrected_segment_text": corrected_segment,
        "english_text": as_text(row["english_text"]),
        "spanish_text": as_text(row["spanish_text"]),
    }


def segment_status(items: list[dict[str, Any]]) -> str:
    active = [item for item in items if item["literal_status"] != "ignored_no_residual"]
    if not active:
        return "ignored_no_residual"
    if all(item["literal_status"] == "ready_literal_map_shadow" for item in active):
        return "ready_segment_literal_map_shadow"
    if any(item["literal_status"] == "ready_literal_map_shadow" for item in active):
        return "partial_ready_needs_context"
    if any(item["literal_status"] == "needs_context_microagent" for item in active):
        return "needs_context_microagent"
    return "needs_literal_map_or_review"


def fetch_rows(conn, *, segment_state_run_id: int, ledger_run_id: int, limit: int | None) -> list[dict[str, Any]]:
    limit_sql = "" if limit is None else "LIMIT ?"
    params: list[Any] = [segment_state_run_id, ledger_run_id, *sorted(TARGET_ISSUE_KINDS)]
    if limit is not None:
        params.append(limit)
    placeholders = ",".join("?" for _ in TARGET_ISSUE_KINDS)
    rows = conn.execute(
        f"""
        SELECT DISTINCT
            ssi.segment_id,
            ssi.relative_path,
            ssi.source_key,
            ssi.source_line_number,
            o.portuguese_text,
            src.english_text,
            src.spanish_text
        FROM segment_state_items ssi
        JOIN output_segments o
          ON o.segment_id = ssi.segment_id
        JOIN source_segments src
          ON src.id = ssi.segment_id
        JOIN ml_issue_ledger_items li
          ON li.segment_id = ssi.segment_id
         AND li.run_id = ?
        WHERE ssi.run_id = ?
          AND ssi.state_group = 'pending'
          AND li.issue_kind IN ({placeholders})
          AND o.portuguese_text LIKE '%Select_CString%'
        ORDER BY ssi.segment_id
        {limit_sql}
        """,
        [ledger_run_id, segment_state_run_id, *sorted(TARGET_ISSUE_KINDS), *( [limit] if limit is not None else [] )],
    ).fetchall()
    return [dict(row) for row in rows]


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_residual_literal_queue_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            queue_name TEXT NOT NULL,
            queue_status TEXT NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            candidate_segment_count INTEGER NOT NULL DEFAULT 0,
            observation_count INTEGER NOT NULL DEFAULT 0,
            residual_observation_count INTEGER NOT NULL DEFAULT 0,
            ready_observation_count INTEGER NOT NULL DEFAULT 0,
            ready_segment_count INTEGER NOT NULL DEFAULT 0,
            partial_segment_count INTEGER NOT NULL DEFAULT 0,
            context_segment_count INTEGER NOT NULL DEFAULT 0,
            review_segment_count INTEGER NOT NULL DEFAULT 0,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            status_counts_json TEXT,
            pair_counts_json TEXT,
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
        CREATE TABLE IF NOT EXISTS ml_issue_select_cstring_residual_literal_queue_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            segment_state_run_id INTEGER NOT NULL,
            ledger_run_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_line_number INTEGER,
            select_index INTEGER NOT NULL,
            condition TEXT,
            condition_family TEXT,
            left_literal TEXT,
            right_literal TEXT,
            left_repair TEXT,
            right_repair TEXT,
            literal_status TEXT NOT NULL,
            segment_status TEXT NOT NULL,
            token_status TEXT NOT NULL,
            residual_reasons_json TEXT,
            current_select_text TEXT,
            corrected_select_text TEXT,
            current_text TEXT,
            corrected_segment_text TEXT,
            english_text TEXT,
            spanish_text TEXT,
            production_release_allowed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_select_cstring_residual_literal_queue_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], ledger_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    base = reports_dir / f"{now_stamp()}_issue_select_cstring_residual_literal_queue_ledger_{ledger_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    segment_state_run_id: int,
    ledger_run_id: int,
    rows: list[dict[str, Any]],
    status_counts: Counter[str],
    segment_status_counts: Counter[str],
    pair_counts: Counter[tuple[str, str]],
    path_counts: Counter[str],
) -> None:
    fields = [
        "segment_id",
        "relative_path",
        "source_key",
        "source_line_number",
        "select_index",
        "condition_family",
        "condition",
        "left_literal",
        "right_literal",
        "left_repair",
        "right_repair",
        "literal_status",
        "segment_status",
        "token_status",
        "residual_reasons",
        "current_select_text",
        "corrected_select_text",
        "current_text",
        "corrected_segment_text",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            payload = dict(row)
            payload["residual_reasons"] = "; ".join(row["residual_reasons"])
            writer.writerow(payload)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    residual_rows = [row for row in rows if row["literal_status"] != "ignored_no_residual"]
    ready_rows = [row for row in rows if row["literal_status"] == "ready_literal_map_shadow"]
    context_rows = [row for row in rows if row["literal_status"] == "needs_context_microagent"]
    lines = [
        "Issue Select_CString residual literal queue",
        f"Rule version: {RULE_VERSION}",
        f"Segment-state run id: {segment_state_run_id}",
        f"Ledger run id: {ledger_run_id}",
        "",
        "Summary:",
        f"- Select_CString observations: {len(rows):,}",
        f"- Residual observations: {len(residual_rows):,}",
        f"- Ready literal-map observations: {len(ready_rows):,}",
        f"- Context/microagent observations: {len(context_rows):,}",
        "- Apply allowed: 0",
        "- Production release allowed: 0",
        "",
        "Segment status:",
        *[f"- {key}: {value:,}" for key, value in segment_status_counts.most_common()],
        "",
        "Literal status:",
        *[f"- {key}: {value:,}" for key, value in status_counts.most_common()],
        "",
        "Top residual pairs:",
    ]
    for (left, right), count in pair_counts.most_common(40):
        lines.append(f"- {left!r} -> {right!r}: {count:,}")
    lines.extend(["", "Top paths:"])
    for path, count in path_counts.most_common(30):
        lines.append(f"- {path}: {count:,}")
    lines.extend(["", "Ready samples:"])
    for row in ready_rows[:30]:
        lines.extend(
            [
                f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']}",
                f"  pair: {row['left_literal']!r}->{row['right_literal']!r} => {row['left_repair']!r}->{row['right_repair']!r}",
                f"  current: {short(row['current_select_text'], 160)}",
                f"  corrected: {short(row['corrected_select_text'], 160)}",
            ]
        )
    lines.extend(["", "Context samples:"])
    for row in context_rows[:30]:
        lines.extend(
            [
                f"- segment={row['segment_id']} {row['relative_path']}::{row['source_key']}",
                f"  pair: {row['left_literal']!r}->{row['right_literal']!r}",
                f"  reasons: {', '.join(row['residual_reasons'])}",
                f"  text: {short(row['current_text'], 220)}",
            ]
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, segment_state_run_id: int | None = None, ledger_run_id: int | None = None, limit: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_segment_state_run_id = segment_state_run_id or latest_id(conn, "segment_state_runs")
        selected_ledger_run_id = ledger_run_id or latest_id(conn, "ml_issue_ledger_runs")
        source_rows = fetch_rows(
            conn,
            segment_state_run_id=selected_segment_state_run_id,
            ledger_run_id=selected_ledger_run_id,
            limit=limit,
        )

        observations: list[dict[str, Any]] = []
        by_segment: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in source_rows:
            for select_row in iter_select_cstrings(as_text(row["portuguese_text"])):
                observation = classify_observation(row, select_row)
                observations.append(observation)
                by_segment[int(row["segment_id"])].append(observation)

        segment_status_by_id = {
            segment_id: segment_status(items)
            for segment_id, items in by_segment.items()
        }
        for observation in observations:
            observation["segment_status"] = segment_status_by_id[int(observation["segment_id"])]

        residual_observations = [
            row for row in observations if row["literal_status"] != "ignored_no_residual"
        ]
        status_counts = Counter(row["literal_status"] for row in observations)
        segment_status_counts = Counter(segment_status_by_id.values())
        pair_counts = Counter(
            (row["left_literal"], row["right_literal"])
            for row in residual_observations
        )
        path_counts = Counter(row["relative_path"] for row in residual_observations)

        txt_path, csv_path, jsonl_path = report_paths(settings, selected_ledger_run_id)
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            segment_state_run_id=selected_segment_state_run_id,
            ledger_run_id=selected_ledger_run_id,
            rows=observations,
            status_counts=status_counts,
            segment_status_counts=segment_status_counts,
            pair_counts=pair_counts,
            path_counts=path_counts,
        )

        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_select_cstring_residual_literal_queue_runs (
                rule_version,
                queue_name,
                queue_status,
                segment_state_run_id,
                ledger_run_id,
                candidate_segment_count,
                observation_count,
                residual_observation_count,
                ready_observation_count,
                ready_segment_count,
                partial_segment_count,
                context_segment_count,
                review_segment_count,
                production_release_allowed,
                status_counts_json,
                pair_counts_json,
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
                QUEUE_NAME,
                QUEUE_STATUS,
                selected_segment_state_run_id,
                selected_ledger_run_id,
                len(by_segment),
                len(observations),
                len(residual_observations),
                status_counts["ready_literal_map_shadow"],
                segment_status_counts["ready_segment_literal_map_shadow"],
                segment_status_counts["partial_ready_needs_context"],
                segment_status_counts["needs_context_microagent"],
                segment_status_counts["needs_literal_map_or_review"],
                PRODUCTION_RELEASE_ALLOWED,
                json.dumps(status_counts, ensure_ascii=False, sort_keys=True),
                json.dumps({f"{left} -> {right}": count for (left, right), count in pair_counts.most_common()}, ensure_ascii=False, sort_keys=True),
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cur.lastrowid)
        for row in observations:
            conn.execute(
                """
                INSERT INTO ml_issue_select_cstring_residual_literal_queue_items (
                    run_id,
                    segment_state_run_id,
                    ledger_run_id,
                    segment_id,
                    relative_path,
                    source_key,
                    source_line_number,
                    select_index,
                    condition,
                    condition_family,
                    left_literal,
                    right_literal,
                    left_repair,
                    right_repair,
                    literal_status,
                    segment_status,
                    token_status,
                    residual_reasons_json,
                    current_select_text,
                    corrected_select_text,
                    current_text,
                    corrected_segment_text,
                    english_text,
                    spanish_text,
                    production_release_allowed,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    selected_segment_state_run_id,
                    selected_ledger_run_id,
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["source_line_number"],
                    row["select_index"],
                    row["condition"],
                    row["condition_family"],
                    row["left_literal"],
                    row["right_literal"],
                    row["left_repair"],
                    row["right_repair"],
                    row["literal_status"],
                    row["segment_status"],
                    row["token_status"],
                    json.dumps(row["residual_reasons"], ensure_ascii=False),
                    row["current_select_text"],
                    row["corrected_select_text"],
                    row["current_text"],
                    row["corrected_segment_text"],
                    row["english_text"],
                    row["spanish_text"],
                    PRODUCTION_RELEASE_ALLOWED,
                    now,
                ),
            )
        conn.commit()

    print("[issue_select_cstring_residual_literal_queue] Queue generated")
    print(f"[issue_select_cstring_residual_literal_queue] Rule version: {RULE_VERSION}")
    print(f"[issue_select_cstring_residual_literal_queue] Run id: {run_id}")
    print(f"[issue_select_cstring_residual_literal_queue] Segment-state run id: {selected_segment_state_run_id}")
    print(f"[issue_select_cstring_residual_literal_queue] Ledger run id: {selected_ledger_run_id}")
    print(f"[issue_select_cstring_residual_literal_queue] Candidate segments: {len(by_segment):,}")
    print(f"[issue_select_cstring_residual_literal_queue] Observations: {len(observations):,}")
    print(f"[issue_select_cstring_residual_literal_queue] Residual observations: {len(residual_observations):,}")
    print(f"[issue_select_cstring_residual_literal_queue] Ready observations: {status_counts['ready_literal_map_shadow']:,}")
    print(f"[issue_select_cstring_residual_literal_queue] Ready segments: {segment_status_counts['ready_segment_literal_map_shadow']:,}")
    print("[issue_select_cstring_residual_literal_queue] Apply allowed: 0")
    print(f"[issue_select_cstring_residual_literal_queue] Report: {txt_path}")
    print(f"[issue_select_cstring_residual_literal_queue] CSV: {csv_path}")
    print(f"[issue_select_cstring_residual_literal_queue] JSONL: {jsonl_path}")
    return {
        "run_id": run_id,
        "segment_state_run_id": selected_segment_state_run_id,
        "ledger_run_id": selected_ledger_run_id,
        "candidate_segments": len(by_segment),
        "observations": len(observations),
        "residual_observations": len(residual_observations),
        "ready_observations": status_counts["ready_literal_map_shadow"],
        "ready_segments": segment_status_counts["ready_segment_literal_map_shadow"],
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a read-only queue for current pending Select_CString residual literal repairs.")
    parser.add_argument("--segment-state-run-id", type=int, default=None)
    parser.add_argument("--ledger-run-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    main(segment_state_run_id=args.segment_state_run_id, ledger_run_id=args.ledger_run_id, limit=args.limit)
