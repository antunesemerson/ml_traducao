from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import local_quality_validator
from apply_segment_state_updates import short, structural_tokens
from segment_token_policy_decisions import sha256_text


RULE_VERSION = "curated_mojibake_policy_decisions_v1"
DEFAULT_POLICY_RUN_ID = 31
SOURCE_BUCKET = "blocked_suspicious_confirmed_text"


CURATED_REPLACEMENTS: dict[int, list[tuple[str, str]]] = {
    27721: [(" ? palpável", " é palpável"), ("J?", "Já"), ("j?", "já"), ("v?.", "vá.")],
    27726: [("Não ?", "Não é"), ("?, certamente", "É, certamente"), ("j?", "já")],
    27743: [(" ? impressionante", " é impressionante"), ("j?", "já")],
    27754: [("J?", "Já"), ("algo ? comunidade", "algo à comunidade")],
    27769: [
        ("Derrubar árvores ? perigoso", "Derrubar árvores é perigoso"),
        ("j?", "já"),
        ("aponta ? enorme", "aponta é enorme"),
    ],
    27792: [("j? ?", "já é"), ("como ? costume", "como é costume")],
    27811: [("] ? surpreendentemente", "] é surpreendentemente"), ("j? que", "já que")],
    27813: [
        ("J?", "Já"),
        ("j?", "já"),
        ("dele/dela ? estressante", "dele/dela é estressante"),
        ("será que ? suficiente", "será que é suficiente"),
    ],
    27888: [("j?", "já")],
    27915: [("j?", "já"), ("L? dentro", "Lá dentro")],
    27946: [("j?", "já"), ("resta ? o que", "resta é o que")],
    27969: [("\"? certamente", "\"é certamente"), ("j?", "já")],
    28005: [("'a guardi?'", "'a guardiã'"), ("j?", "já"), ("está estáril", "está estéril")],
    28077: [("j?", "já"), ("'a artes?'", "'a artesã'")],
    28174: [("j? honraria", "já honraria"), ("cavalo ? todo", "cavalo é todo")],
    28229: [("j?", "já"), ("trar? muita", "trará muita"), ("! ? verdade", "! É verdade")],
    28244: [("j?", "já"), ("colocar? meus", "colocará meus")],
    28285: [("J?", "Já")],
    28395: [("j?", "já"), ("Ah! ? [", "Ah! É [")],
    28396: [("j?", "já"), ("Ah! ? [", "Ah! É [")],
    28398: [("j?", "já"), ("Ah! ? [", "Ah! É [")],
    28463: [
        ("reconhec?-la", "reconhecê-la"),
        ("j? que", "já que"),
        ("faz?-lo", "fazê-lo"),
        ("\"D?-me", "\"Dê-me"),
    ],
    28540: [("j?", "já"), ("Assassinato ? realmente", "Assassinato é realmente")],
    30374: [("interessaria aos leitores ? se fosse menos truncada", "interessaria aos leitores, se fosse menos truncada")],
    30407: [("faz?-lo", "fazê-lo")],
    30428: [
        ("\"Não! Não ? assim", "\"Não! Não é assim"),
        ("não d? sinais", "não dá sinais"),
        ("pronúncia ? o primeiro som ? forte, não suave, #EMP obviamente#! ?", "pronúncia - o primeiro som é forte, não suave, #EMP obviamente#! -"),
    ],
    30430: [
        ("em direção ? máquina", "em direção à máquina"),
        ("mas ? tarde demais", "mas é tarde demais"),
    ],
    30442: [("peg?-los", "pegá-los")],
    30576: [("entusiasmo ? acredite", "entusiasmo - acredite")],
    30605: [("#EMP ?#! agradável", "#EMP é#! agradável")],
    30614: [
        ("Est? evidente", "Está evidente"),
        ("A verdade ? que", "A verdade é que"),
    ],
    30617: [
        ("empreg?-los", "empregá-los"),
        ("benefécio", "benefício"),
    ],
    30619: [("lider?-la", "liderá-la")],
    30624: [("faz?-la prosperar", "fazê-la prosperar")],
    30672: [("Recuper?-las", "Recuperá-las")],
    30845: [("parecer? estar", "parecerá estar")],
    30926: [("termin?-la", "terminá-la")],
    30981: [("deix?-l[spouse.Custom('ES_OA')]", "deixá-l[spouse.Custom('ES_OA')]")],
    31151: [("anim?-lo", "animá-lo")],
    30616: [
        ("barraca ? beira da estrada", "barraca à beira da estrada"),
        ("este ? o melhor queijo", "este é o melhor queijo"),
        ("o queijo ? incrivelmente bom", "o queijo é incrivelmente bom"),
    ],
    30652: [("Seu dialeto ? estrangeiro", "Seu dialeto é estrangeiro")],
    30716: [("aquilo que ? dado aqui", "aquilo que é dado aqui")],
    30728: [("adicionar [human_tribute.GetHerHim] ? nossa corte", "adicionar [human_tribute.GetHerHim] à nossa corte")],
    30759: [
        ("[sad_peasant.GetWomanMan] ? beira da estrada", "[sad_peasant.GetWomanMan] à beira da estrada"),
        ("\"Est? feliz agora?", "\"Está feliz agora?"),
    ],
    30778: [("A maioria ? mais simpática", "A maioria é mais simpática")],
    30995: [("Este ? o melhor lugar", "Este é o melhor lugar")],
    30999: [("] ? bem conhecid", "] é bem conhecid")],
    31000: [("Tudo o que eu quero ? fazer", "Tudo o que eu quero é fazer")],
    31020: [("retribuo seu alegre cumprimento ? mal consigo", "retribuo seu alegre cumprimento - mal consigo")],
    31023: [("precisa ? ", "precisa é ")],
    31025: [
        ("[target.GetTitledFirstName|l] ?, no entanto", "[target.GetTitledFirstName|l] é, no entanto"),
        ("não ? corrupt", "não é corrupt"),
    ],
    31092: [
        ("em direção ? presa", "em direção à presa"),
        ("afast?-lo", "afastá-lo"),
    ],
    31093: [("#EMP Kumis!#! ? ótimo", "#EMP Kumis!#! é ótimo")],
    31101: [
        ("\"Est? um dia tão bonito", "\"Está um dia tão bonito"),
        ("companhia l? fora", "companhia lá fora"),
        ("ainda estar? l? depois", "ainda estará lá depois"),
    ],
    31132: [("grito agudo ? e foge", "grito agudo - e foge")],
    31140: [("Este personagem ? histórico", "Este personagem é histórico")],
    31141: [("Este personagem ? histórico", "Este personagem é histórico")],
    31150: [("anim?-lo", "animá-lo")],
}

OPTIONAL_FRAGMENTS = {"j?", "J?"}

STRUCTURAL_CLEANUP_POLICY_ITEM_IDS = {
    30374,
    30407,
    30428,
    30430,
    30442,
    30576,
    30605,
    30614,
    30617,
    30619,
    30624,
    30672,
    30845,
    30926,
    30981,
    30616,
    30652,
    30716,
    30728,
    30759,
    30778,
    30995,
    30999,
    31000,
    31020,
    31023,
    31025,
    31092,
    31093,
    31101,
    31132,
    31140,
    31141,
    31150,
    31151,
}


DEFERRED_POLICY_ITEM_IDS = {
    27959: "requires_sentence_rewrite_not_mechanical_accent_fix",
    28190: "requires_pronoun_token_phrase_review",
    28367: "requires_punctuation_style_review",
    28504: "changes_string_literals_inside_select_cstring",
}


def fetch_rows(conn, *, policy_run_id: int) -> list[dict[str, Any]]:
    policy_item_ids = sorted({*CURATED_REPLACEMENTS, *DEFERRED_POLICY_ITEM_IDS})
    placeholders = ", ".join("?" for _ in policy_item_ids)
    rows = conn.execute(
        f"""
        SELECT
            i.id AS policy_item_id,
            i.run_id AS policy_run_id,
            i.segment_id,
            i.relative_path,
            i.source_key,
            i.source_line_number,
            i.policy_bucket,
            i.risk_level,
            i.issue_flags_json,
            i.missing_tokens_json,
            i.extra_tokens_json,
            sc.confirmed_text,
            o.portuguese_text AS output_text
        FROM segment_token_policy_items i
        JOIN segment_confirmations sc ON sc.segment_id = i.segment_id
        LEFT JOIN output_segments o ON o.segment_id = i.segment_id
        WHERE i.run_id = ?
          AND i.id IN ({placeholders})
        ORDER BY i.id
        """,
        (policy_run_id, *policy_item_ids),
    ).fetchall()
    return [dict(row) for row in rows]


def apply_replacements(text: str, replacements: list[tuple[str, str]]) -> tuple[str, list[str], list[str]]:
    updated = text
    applied: list[str] = []
    missing: list[str] = []
    for before, after in replacements:
        if before not in updated:
            if before not in OPTIONAL_FRAGMENTS:
                missing.append(before)
            continue
        updated = updated.replace(before, after)
        applied.append(before)
    return updated, applied, missing


def classify(row: dict[str, Any]) -> tuple[str, list[str], str, list[str]]:
    policy_item_id = int(row["policy_item_id"])
    original = row["confirmed_text"] or ""
    if policy_item_id in DEFERRED_POLICY_ITEM_IDS:
        return (
            "deferred_manual_review",
            [DEFERRED_POLICY_ITEM_IDS[policy_item_id]],
            original,
            [],
        )
    replacements = CURATED_REPLACEMENTS[policy_item_id]
    fixed, applied, missing = apply_replacements(original, replacements)
    reasons: list[str] = []
    if missing:
        reasons.extend(f"missing_expected_fragment:{fragment}" for fragment in missing)
        return "blocked_missing_expected_fragment", reasons, fixed, applied
    if fixed == original:
        return "blocked_no_change", ["no_change_after_curated_replacements"], fixed, applied
    if structural_tokens(original) != structural_tokens(fixed):
        return "blocked_structural_token_change", ["structural_tokens_changed"], fixed, applied
    issues = local_quality_validator.validate_text(fixed)["issues"]
    issue_codes = {str(issue.get("code")) for issue in issues}
    if "replacement_question_mark_mojibake" in issue_codes:
        return "blocked_still_mojibake", ["validator_still_flags_question_mark_mojibake"], fixed, applied
    return "ready_decision", ["curated_mojibake_fix_reviewed"], fixed, applied


def write_outputs(
    settings: dict[str, Any],
    *,
    policy_run_id: int,
    rows: list[dict[str, Any]],
) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    decisions_path = reports_dir / f"{timestamp}_curated_mojibake_policy_decisions.jsonl"
    report_path = reports_dir / f"{timestamp}_curated_mojibake_policy_decisions.txt"

    with decisions_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            if row["curation_status"] != "ready_decision":
                continue
            decision = (
                "encoding_cleanup_required"
                if int(row["policy_item_id"]) in STRUCTURAL_CLEANUP_POLICY_ITEM_IDS
                else "fix_confirmed_text"
            )
            cleanup_note = (
                "; token_release_safe_but_text_hygiene_blocks_apply"
                if decision == "encoding_cleanup_required"
                else ""
            )
            handle.write(
                json.dumps(
                    {
                        "policy_item_id": row["policy_item_id"],
                        "decision": decision,
                        "corrected_text": row["fixed_text"],
                        "notes": (
                            f"{RULE_VERSION}; policy_run={policy_run_id}; "
                            f"applied={json.dumps(row['replacement_hits'], ensure_ascii=False)}"
                            f"{cleanup_note}"
                        ),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    counts = Counter(row["curation_status"] for row in rows)
    lines = [
        "Curated mojibake policy decisions",
        f"Rule version: {RULE_VERSION}",
        f"Policy run id: {policy_run_id}",
        f"Source bucket: {SOURCE_BUCKET}",
        f"Rows inspected: {len(rows)}",
        f"Ready decisions: {counts.get('ready_decision', 0)}",
        f"Decisions JSONL: {decisions_path}",
        "",
        "Status counts:",
        *[f"- {key}: {value}" for key, value in counts.most_common()],
        "",
        "Preview:",
    ]
    for row in rows:
        lines.extend(
            [
                (
                    f"- item {row['policy_item_id']} | segment {row['segment_id']} | "
                    f"{row['curation_status']} | {row['relative_path']}:{row['source_line_number']} | "
                    f"{row['source_key']}"
                ),
                f"  reasons: {json.dumps(row['curation_reasons'], ensure_ascii=False)}",
                f"  replacements: {json.dumps(row['replacement_hits'], ensure_ascii=False)}",
                f"  before: {short(row['confirmed_text'], 260)}",
                f"  after:  {short(row['fixed_text'], 260)}",
            ]
        )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path, decisions_path


def main(*, policy_run_id: int | None = None) -> None:
    settings = db.load_settings()
    selected_policy_run_id = policy_run_id or DEFAULT_POLICY_RUN_ID
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        raw_rows = fetch_rows(conn, policy_run_id=selected_policy_run_id)

    analyzed: list[dict[str, Any]] = []
    for row in raw_rows:
        status, reasons, fixed, replacement_hits = classify(row)
        analyzed.append(
            {
                **row,
                "curation_status": status,
                "curation_reasons": reasons,
                "fixed_text": fixed,
                "replacement_hits": replacement_hits,
                "confirmed_text_hash": sha256_text(row.get("confirmed_text")),
            }
        )
    report_path, decisions_path = write_outputs(
        settings,
        policy_run_id=selected_policy_run_id,
        rows=analyzed,
    )
    counts = Counter(row["curation_status"] for row in analyzed)
    print("[curated_mojibake_policy_decisions] Decisions generated")
    print(f"[curated_mojibake_policy_decisions] Rule version: {RULE_VERSION}")
    print(f"[curated_mojibake_policy_decisions] Policy run id: {selected_policy_run_id}")
    print(f"[curated_mojibake_policy_decisions] Rows inspected: {len(analyzed)}")
    for key, value in counts.most_common():
        print(f"[curated_mojibake_policy_decisions] {key}: {value}")
    print(f"[curated_mojibake_policy_decisions] Report: {report_path}")
    print(f"[curated_mojibake_policy_decisions] Decisions JSONL: {decisions_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build curated fix_confirmed_text decisions for mojibake token-policy rows.")
    parser.add_argument("--policy-run-id", type=int, default=None)
    args = parser.parse_args()
    main(policy_run_id=args.policy_run_id)
