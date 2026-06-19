from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from apply_segment_state_updates import short, structural_tokens


RULE_VERSION = "issue_dynamic_literal_repair_diagnostic_v1"

SELECT_CSTRING_LITERAL_RE = re.compile(
    r"Select_CString\(\s*([^,]+?)\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)",
    re.IGNORECASE,
)
SELECT_CSTRING_DIRECT_NAME_RE = re.compile(
    r"Select_CString\(\s*([^,]+?\.IsLocalPlayer)\s*,\s*'([^']*)'\s*,\s*([^,)]+?\.GetShortUIName[^)]*)\s*\)",
    re.IGNORECASE,
)

LITERAL_TRANSLATIONS = {
    "apoya": "apoia",
    "apoyaste": "apoiou",
    "apoyo": "apoiou",
    "apoyó": "apoiou",
    "apoyas": "apoia",
    "decidio": "decidiu",
    "decidió": "decidiu",
    "decidiste": "decidiu",
    "deberia": "deveria",
    "deberias": "deveria",
    "esta": "est\u00e1",
    "estas": "est\u00e1",
    "las primeras": "as primeiras",
    "los primeros": "os primeiros",
    "ti": "voc\u00ea",
    "puedes alejarte": "consegue se afastar",
    "puede alejarse": "consegue se afastar",
    "adoras": "adora",
    "adora": "adora",
    "sueles aislarte": "costuma se isolar",
    "sueles aislarse": "costuma se isolar",
    "hiciste": "fez",
    "hizo": "fez",
    "tus": "seus",
    "sus": "seus",
    "te relacionaste": "se relacionou",
    "se relacionó": "se relacionou",
    "te convertirás": "se tornará",
    "se convertirá": "se tornará",
    "te convertiste": "se tornou",
    "se convirtió": "se tornou",
    "a la fugada": "a fugitiva",
    "al fugado": "o fugitivo",
    "esta cazadora": "esta caçadora",
    "este cazador": "este caçador",
    "a la ermitaña": "a eremita",
    "al ermitaño": "o eremita",
    "te enamoraste": "se apaixonou",
    "se enamoró": "se apaixonou",
    "portas": "porta",
    "porta": "porta",
    "estás": "está",
    "te tomas": "leva",
    "se toma": "leva",
    "te conviertes": "se torna",
    "se convierte": "se torna",
    "te atiborraste": "se empanturrou",
    "se atiborró": "se empanturrou",
    "encontraste": "encontrou",
    "encontró": "encontrou",
    "la dama": "a dama",
    "el señor": "o senhor",
    "te cases": "se case",
    "se case": "se case",
    "deshonraste": "desonrou",
    "deshonró": "desonrou",
    "tus dominios": "seus domínios",
    "comes": "come",
    "come": "come",
    "eres": "é",
    "es": "é",
    "ganaras": "ganhará",
    "ganarás": "ganhará",
    "ganaste": "ganhou",
    "gano": "ganhou",
    "ganó": "ganhou",
    "ganará": "ganhará",
    "ganara": "ganhará",
    "ha": "foi",
    "habla": "fala",
    "hablas": "fala",
    "has": "foi",
    "heredará": "herdará",
    "heredara": "herdará",
    "heredarás": "herdará",
    "heredaras": "herdará",
    "levantará": "levantará",
    "levantara": "levantará",
    "levantarás": "levantará",
    "levantaras": "levantará",
    "intentaras": "tentará",
    "intentará": "tentará",
    "intentarás": "tentará",
    "intentara": "tentará",
    "logra": "consegue",
    "logras": "consegue",
    "misionera": "missionária",
    "misionero": "missionário",
    "perderá": "perderá",
    "perdera": "perderá",
    "perdió": "perdeu",
    "perdio": "perdeu",
    "perdiste": "perdeu",
    "prefiere": "prefere",
    "prefieres": "prefere",
    "puede": "pode",
    "puedes": "pode",
    "sana y salva": "sã e salva",
    "sano y salvo": "são e salvo",
    "se apoyo": "se apoiou",
    "se apoyó": "se apoiou",
    "se compromete": "se compromete",
    "se hicieron": "se tornaram",
    "se olvida": "se esquece",
    "se opone": "se opõe",
    "se opuso": "se opôs",
    "son": "estão",
    "te apoyaste": "se apoiou",
    "te comprometes": "se compromete",
    "te olvidas": "se esquece",
    "te opones": "se opõe",
    "te opusiste": "se opôs",
    "tú": "Você",
    "tu": "Você",
}

TEXT_REPLACEMENTS = (
    ("'ganará', 'perderá'", "'ganhará', 'perderá'"),
    ("'ganarás', 'ganará'", "'ganhará', 'ganhará'"),
    ("'ganaste', 'ganó'", "'ganhou', 'ganhou'"),
    ("'intentarás', 'intentará'", "'tentará', 'tentará'"),
    ("'os hicisteis', 'se hicieron'", "'se tornaram', 'se tornaram'"),
    ("'proponerte', 'proponerse'", "'propor', 'propor'"),
    ("'sana y salva', 'sano y salvo'", "'sã e salva', 'são e salvo'"),
    ("'sois', 'son'", "'estão', 'estão'"),
    ("'te opones', 'se opone'", "'se opõe', 'se opõe'"),
    ("'te opusiste', 'se opuso'", "'se opôs', 'se opôs'"),
    ("'eras', 'era'", "'era', 'era'"),
    ("'ganaste', 'gan\u00f3'", "'ganhou', 'ganhou'"),
    ("'eres', 'es'", "'\u00e9', '\u00e9'"),
    ("'las primeras', 'los primeros'", "'as primeiras', 'os primeiros'"),
    ("'te conviertes', 'se convierte'", "'se torna', 'se torna'"),
    ("'te convertesses', 'se convertesse'", "'voc\u00ea se tornasse', 'se tornasse'"),
    ("'teu', 'seu'", "'seu', 'seu'"),
    ("'tus','sus'", "'seus','seus'"),
    ("'tus', 'sus'", "'seus', 'seus'"),
    ("'hereder'", "'herdeir'"),
    ("'la señora', 'el señor'", "'a senhora', 'o senhor'"),
    ("'mi señorío'", "'meu senhorio'"),
    ("#bold no#!", "#bold não#!"),
    ("#EMP no#!", "#EMP não#!"),
    (" no [Select_CString", " não [Select_CString"),
    (" un [camp|lE]", " um [camp|lE]"),
    ("Bajo el gobierno", "Sob o governo"),
    ("Select_CString(GetPlayer.IsFemale, 'la', 'el' )", "Select_CString(GetPlayer.IsFemale, 'a', 'o' )"),
    (
        "Select_CString(And(ROOT.Char.IsFemale, rival.IsFemale), 'vosotras', 'vosotros')",
        "Select_CString(And(ROOT.Char.IsFemale, rival.IsFemale), 'uma contra a outra', 'um contra o outro')",
    ),
    (")] os primeiros a receber", ")] a receber"),
    ("Jurar lealtad", "Jurar lealdade"),
    ("jurar lealtad", "jurar lealdade"),
    ("con una", "com uma"),
    ("demasiado buena", "boa demais"),
    ("encarcelad", "pres"),
    ("encarcelados", "presos"),
    ("ha sido inspirad", "foi inspirad"),
    ("hacerlo", "fazê-lo"),
    ("puede acabar", "pode acabar"),
    ("sorprender", "surpreender"),
    ("#EMP mucha#!", "#EMP muita#!"),
    (" es [close_family|lE]", " é [close_family|lE]"),
    ("vasallaje", "vassalagem"),
    ("vasalla", "vassala"),
    ("vasallo", "vassalo"),
    ("por el [clergy|lE]", "pelo [clergy|lE]"),
    ("a\u00f1os", "anos"),
    ("Desaprueba revocar baron\u00edas", "Desaprova revogar baronias"),
    ("pasan", "passam"),
    ("transforman", "transformam"),
    ("convierten", "convertem"),
    ("convierte", "converte"),
    (" es humillad", " é humilhad"),
    ("vocę", "você"),
    ("Vocę", "Você"),
    ("săo", "são"),
    ("năo", "não"),
)

RESIDUAL_MARKERS = (
    "apoya",
    "apoyaste",
    "apoyó",
    "apoyas",
    "Bajo el gobierno",
    "convierten",
    "convierte",
    "decidiste",
    "decidió",
    "encarcelados",
    "encarcelad",
    "es humillad",
    " es [close_family",
    "ganarás",
    "ganará",
    "ganaste",
    "ganó",
    "habla",
    "hablas",
    "heredará",
    "heredarás",
    "hacerlo",
    "hereder",
    "intentará",
    "intentarás",
    "levantarás",
    "Jurar lealtad",
    "jurar lealtad",
    "la señora",
    "el señor",
    "logra",
    "logras",
    "mi señorío",
    "misionera",
    "misionero",
    "mucha",
    "os hicisteis",
    "perdió",
    "perdiste",
    "prefiere",
    "prefieres",
    "proponerse",
    "proponerte",
    "puede acabar",
    "sana y salva",
    "sano y salvo",
    "se apoyó",
    "se hicieron",
    "se olvida",
    "se opone",
    "se opuso",
    "sois",
    "sorprender",
    "te apoyaste",
    "te olvidas",
    "te opones",
    "te opusiste",
    "transforman",
    "vasalla",
    "vasallo",
    "vasallaje",
    "'tus'",
    "'sus'",
    "a\u00f1o",
    "a\u00f1os",
    "baron\u00edas",
    "deber\u00eda",
    "deber\u00edas",
    "Desaprueba",
    "est\u00e1s",
    "las primeras",
    "los primeros",
    "qued\u00f3",
    "quedaste",
    "vosotras",
    "vosotros",
    "#bold no#!",
)


def normalize_literal(value: str) -> str:
    return value.strip().casefold()


def strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def translate_literal(value: str) -> str | None:
    normalized = normalize_literal(value)
    return LITERAL_TRANSLATIONS.get(normalized) or LITERAL_TRANSLATIONS.get(strip_accents(normalized))


def replace_select_literals(text: str, reasons: list[str]) -> str:
    def replace_direct_name(match: re.Match[str]) -> str:
        condition = match.group(1).strip()
        literal = match.group(2)
        fallback = match.group(3).strip()
        translated = translate_literal(literal)
        if not translated:
            return match.group(0)
        reasons.append(f"direct_name_literal:{literal}->{translated}")
        return f"Select_CString( {condition}, '{translated}', {fallback} )"

    text = SELECT_CSTRING_DIRECT_NAME_RE.sub(replace_direct_name, text)

    def replace_pair(match: re.Match[str]) -> str:
        condition = match.group(1).strip()
        first = match.group(2)
        second = match.group(3)
        translated_first = translate_literal(first)
        translated_second = translate_literal(second)
        if not translated_first and not translated_second:
            return match.group(0)
        new_first = translated_first or first
        new_second = translated_second or second
        reasons.append(f"select_literals:{first}/{second}->{new_first}/{new_second}")
        return f"Select_CString( {condition}, '{new_first}', '{new_second}' )"

    return SELECT_CSTRING_LITERAL_RE.sub(replace_pair, text)


def apply_text_replacements(text: str, reasons: list[str]) -> str:
    corrected = text
    for old, new in TEXT_REPLACEMENTS:
        if old in corrected:
            corrected = corrected.replace(old, new)
            reasons.append(f"text:{old}->{new}")
    return corrected


def propose_repair(text: str) -> tuple[str, list[str]]:
    reasons: list[str] = []
    corrected = apply_text_replacements(text, reasons)
    corrected = replace_select_literals(corrected, reasons)
    for old, new in TEXT_REPLACEMENTS:
        if old in corrected:
            corrected = corrected.replace(old, new)
            reasons.append(f"text:{old}->{new}")
    return corrected, reasons


def residual_hits(text: str) -> list[str]:
    low = text.casefold()
    return [marker for marker in RESIDUAL_MARKERS if marker.casefold() in low]


def token_status(current: str, corrected: str) -> str:
    if structural_tokens(current) == structural_tokens(corrected):
        return "same_structural_tokens"
    return "structural_token_change_review_required"


def classify_item(row: dict[str, Any]) -> dict[str, Any]:
    current = row.get("confirmed_text") or ""
    corrected, reasons = propose_repair(current)
    hits = residual_hits(corrected)
    if corrected == current:
        status = "needs_context"
        token = "no_text_delta"
        reasons.append("no_repair_proposal")
    else:
        token = token_status(current, corrected)
        if hits:
            status = "needs_context"
            reasons.append("residual_after_repair:" + ",".join(hits[:6]))
        elif token == "same_structural_tokens":
            status = "ready_shadow"
        else:
            status = "token_policy_review"

    if "has') foi" in current or "'has', 'ha'" in current:
        status = "needs_context"
        reasons.append("has_ha_auxiliary_requires_sentence_rewrite_review")

    return {
        "decision_id": row["decision_id"],
        "queue_item_id": row["queue_item_id"],
        "segment_id": row["segment_id"],
        "relative_path": row["relative_path"],
        "source_key": row["source_key"],
        "queue_bucket": row["queue_bucket"],
        "repair_status": status,
        "token_status": token,
        "current_text": current,
        "corrected_text": corrected if corrected != current else "",
        "english_text": row.get("english_text") or "",
        "spanish_text": row.get("spanish_text") or "",
        "reasons": reasons,
    }


def latest_decision_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT d.run_id
        FROM ml_issue_review_decisions d
        JOIN ml_issue_review_queue_items q ON q.id = d.queue_item_id
        WHERE q.agent_key = 'micro_dynamic_ck3_expression'
          AND d.normalized_decision = 'needs_repair'
        GROUP BY d.run_id
        ORDER BY MAX(d.created_at) DESC, d.run_id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("No dynamic issue repair decision run found.")
    return int(row["run_id"])


def fetch_rows(conn, decision_run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            d.id AS decision_id,
            d.queue_item_id,
            q.segment_id,
            q.relative_path,
            q.source_key,
            q.queue_bucket,
            q.english_text,
            q.spanish_text,
            q.confirmed_text
        FROM ml_issue_review_decisions d
        JOIN ml_issue_review_queue_items q ON q.id = d.queue_item_id
        WHERE d.run_id = ?
          AND d.normalized_decision = 'needs_repair'
          AND q.agent_key = 'micro_dynamic_ck3_expression'
        ORDER BY q.id
        """,
        (decision_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ml_issue_dynamic_literal_repair_runs (
            id INTEGER PRIMARY KEY,
            rule_version TEXT NOT NULL,
            decision_run_id INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            ready_shadow_count INTEGER NOT NULL DEFAULT 0,
            needs_context_count INTEGER NOT NULL DEFAULT 0,
            token_policy_review_count INTEGER NOT NULL DEFAULT 0,
            same_structural_token_count INTEGER NOT NULL DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS ml_issue_dynamic_literal_repair_items (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
            queue_item_id INTEGER NOT NULL,
            segment_id INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            source_key TEXT NOT NULL,
            queue_bucket TEXT NOT NULL,
            repair_status TEXT NOT NULL,
            token_status TEXT NOT NULL,
            current_text TEXT NOT NULL,
            corrected_text TEXT,
            english_text TEXT,
            spanish_text TEXT,
            reasons_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES ml_issue_dynamic_literal_repair_runs(id) ON DELETE CASCADE
        )
        """
    )


def report_paths(settings: dict[str, Any], decision_run_id: int) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_dynamic_literal_repair_decision_run_{decision_run_id}"
    return base.with_suffix(".txt"), base.with_suffix(".csv"), base.with_suffix(".jsonl")


def write_reports(
    *,
    txt_path: Path,
    csv_path: Path,
    jsonl_path: Path,
    decision_run_id: int,
    rows: list[dict[str, Any]],
    counts: Counter[str],
) -> None:
    fields = [
        "repair_status",
        "token_status",
        "decision_id",
        "queue_item_id",
        "segment_id",
        "relative_path",
        "source_key",
        "queue_bucket",
        "current_text",
        "corrected_text",
        "reasons",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            payload = dict(row)
            payload["reasons"] = "; ".join(row["reasons"])
            writer.writerow(payload)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "Issue dynamic literal repair diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Decision run id: {decision_run_id}",
        "",
        "Summary:",
        f"- Candidates: {len(rows):,}",
        f"- Ready shadow: {counts['ready_shadow']:,}",
        f"- Needs context: {counts['needs_context']:,}",
        f"- Token policy review: {counts['token_policy_review']:,}",
        f"- Same structural tokens: {counts['same_structural_tokens']:,}",
        "",
        "By queue bucket:",
        *[f"- {key}: {value:,}" for key, value in counts.items() if key.startswith("bucket:")],
        "",
        "Samples:",
    ]
    for row in rows[:80]:
        lines.extend(
            [
                (
                    f"- {row['repair_status']} | {row['token_status']} | "
                    f"segment={row['segment_id']} {row['relative_path']}::{row['source_key']}"
                ),
                f"  current: {short(row['current_text'], 220)}",
                f"  corrected: {short(row['corrected_text'], 220) if row['corrected_text'] else '<none>'}",
                f"  reasons: {', '.join(row['reasons'])}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety note:",
            "- This diagnostic does not write source/output and does not promote confirmations.",
            "- ready_shadow means the proposed text preserves structural token shape and can be reviewed by a later shadow/checkpoint step.",
            "- needs_context remains useful routing evidence, not automatic correction.",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(*, decision_run_id: int | None = None) -> dict[str, Any]:
    settings = db.load_settings()
    started_at = datetime.now().isoformat(timespec="seconds")
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        ensure_tables(conn)
        selected_decision_run_id = decision_run_id or latest_decision_run_id(conn)
        source_rows = fetch_rows(conn, selected_decision_run_id)
        classified = [classify_item(row) for row in source_rows]
        counts: Counter[str] = Counter(row["repair_status"] for row in classified)
        counts.update(f"bucket:{row['queue_bucket']}" for row in classified)
        counts["same_structural_tokens"] = sum(
            1 for row in classified if row["token_status"] == "same_structural_tokens"
        )
        txt_path, csv_path, jsonl_path = report_paths(settings, selected_decision_run_id)
        write_reports(
            txt_path=txt_path,
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            decision_run_id=selected_decision_run_id,
            rows=classified,
            counts=counts,
        )
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            """
            INSERT INTO ml_issue_dynamic_literal_repair_runs (
                rule_version,
                decision_run_id,
                candidate_count,
                ready_shadow_count,
                needs_context_count,
                token_policy_review_count,
                same_structural_token_count,
                report_path,
                csv_path,
                jsonl_path,
                started_at,
                finished_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RULE_VERSION,
                selected_decision_run_id,
                len(classified),
                counts["ready_shadow"],
                counts["needs_context"],
                counts["token_policy_review"],
                counts["same_structural_tokens"],
                str(txt_path),
                str(csv_path),
                str(jsonl_path),
                started_at,
                now,
                now,
            ),
        )
        run_id = int(cur.lastrowid)
        for row in classified:
            conn.execute(
                """
                INSERT INTO ml_issue_dynamic_literal_repair_items (
                    run_id,
                    decision_id,
                    queue_item_id,
                    segment_id,
                    relative_path,
                    source_key,
                    queue_bucket,
                    repair_status,
                    token_status,
                    current_text,
                    corrected_text,
                    english_text,
                    spanish_text,
                    reasons_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    row["decision_id"],
                    row["queue_item_id"],
                    row["segment_id"],
                    row["relative_path"],
                    row["source_key"],
                    row["queue_bucket"],
                    row["repair_status"],
                    row["token_status"],
                    row["current_text"],
                    row["corrected_text"],
                    row["english_text"],
                    row["spanish_text"],
                    json.dumps(row["reasons"], ensure_ascii=False),
                    now,
                ),
            )
        conn.commit()

    print("[issue_dynamic_literal_repair_diagnostic] Diagnostic generated")
    print(f"[issue_dynamic_literal_repair_diagnostic] Rule version: {RULE_VERSION}")
    print(f"[issue_dynamic_literal_repair_diagnostic] Run id: {run_id}")
    print(f"[issue_dynamic_literal_repair_diagnostic] Decision run id: {selected_decision_run_id}")
    print(f"[issue_dynamic_literal_repair_diagnostic] Candidates: {len(classified):,}")
    print(f"[issue_dynamic_literal_repair_diagnostic] Ready shadow: {counts['ready_shadow']:,}")
    print(f"[issue_dynamic_literal_repair_diagnostic] Needs context: {counts['needs_context']:,}")
    print(f"[issue_dynamic_literal_repair_diagnostic] Token policy review: {counts['token_policy_review']:,}")
    print(f"[issue_dynamic_literal_repair_diagnostic] Report: {txt_path}")
    return {
        "run_id": run_id,
        "decision_run_id": selected_decision_run_id,
        "candidate_count": len(classified),
        "ready_shadow": counts["ready_shadow"],
        "needs_context": counts["needs_context"],
        "token_policy_review": counts["token_policy_review"],
        "report_path": str(txt_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diagnose reusable repairs for dynamic literal issue-review rows.")
    parser.add_argument("--decision-run-id", type=int, default=None)
    args = parser.parse_args()
    main(decision_run_id=args.decision_run_id)
