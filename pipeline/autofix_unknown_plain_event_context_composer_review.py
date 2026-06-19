from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "autofix_unknown_plain_event_context_composer_review_v1"
LATEST_LEDGER_RUN_ID = 69
INPUT_PATHS = [
    Path("reports/20260618_180523_016737_autofix_unknown_plain_event_queue_226_reviewed_chat.jsonl"),
    Path("reports/20260618_185935_916811_autofix_unknown_plain_event_queue_227_reviewed_chat.jsonl"),
]
CONTEXT_DECISIONS = {"needs_plain_prose_context_composer", "needs_event_context_composer"}
ALLOWED_COMPOSER_DECISIONS = {
    "composition_ready_plain_prose",
    "composition_ready_event_context",
    "composition_ready_historical_or_gloss",
    "needs_plain_prose_repair",
    "needs_event_context_repair",
    "needs_spanish_residual_repair",
    "needs_english_residual_repair",
    "needs_ptbr_fluency_repair",
    "needs_encoding_or_mojibake_review",
    "needs_domain_context",
    "needs_new_microagent",
    "blocked_uncertain",
}
REQUIRED_INPUT_FIELDS = {
    "segment_id",
    "queue_run_id",
    "ledger_run_id",
    "relative_path",
    "source_key",
    "decision",
    "subpolicy",
    "route_to_agent",
}
REQUIRED_OUTPUT_FIELDS = {
    "source_queue_run_id",
    "ledger_run_id",
    "segment_id",
    "relative_path",
    "source_key",
    "input_decision",
    "input_subpolicy",
    "composer_family",
    "composer_decision",
    "subpolicy",
    "lifecycle_candidate",
    "requires_apply_later",
    "corrected_text",
    "route_to_agent",
    "tokens_preserved",
    "confidence",
    "risk_flags",
    "notes",
}


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def load_context_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in INPUT_PATHS:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            missing = REQUIRED_INPUT_FIELDS - set(row)
            if missing:
                raise RuntimeError(f"{path} missing fields {sorted(missing)} for row {row!r}")
            if row["decision"] in CONTEXT_DECISIONS:
                rows.append(row)
    if len(rows) != 224:
        raise RuntimeError(f"Expected 224 context composer rows, got {len(rows)}")
    split = Counter(row["decision"] for row in rows)
    if split != {"needs_plain_prose_context_composer": 147, "needs_event_context_composer": 77}:
        raise RuntimeError(f"Unexpected context split: {dict(split)}")
    return rows


def load_db_context(conn, segment_ids: list[int]) -> tuple[dict[int, dict[str, Any]], int]:
    latest_run = conn.execute(
        """
        SELECT id
        FROM segment_state_runs
        WHERE finished_at IS NOT NULL
          AND total_segments > 1000
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if latest_run is None:
        raise RuntimeError("No finished segment-state run found")
    latest_run_id = int(latest_run["id"])
    placeholders = ",".join("?" for _ in segment_ids)
    state_rows = conn.execute(
        f"""
        SELECT
            state.segment_id,
            state.final_state,
            state.state_group,
            state.needs_output_apply,
            state.confirmed_matches_output,
            state.needs_reopen,
            source.spanish_text,
            source.english_text,
            output.portuguese_text
        FROM segment_state_items state
        JOIN source_segments source
          ON source.id = state.segment_id
        LEFT JOIN output_segments output
          ON output.segment_id = state.segment_id
        WHERE state.run_id = ?
          AND state.segment_id IN ({placeholders})
        """,
        (latest_run_id, *segment_ids),
    ).fetchall()
    context = {int(row["segment_id"]): dict(row) for row in state_rows}
    issue_rows = conn.execute(
        f"""
        SELECT
            segment_id,
            issue_family,
            issue_kind,
            issue_severity
        FROM ml_issue_ledger_items
        WHERE run_id = ?
          AND status = 'open'
          AND segment_id IN ({placeholders})
        ORDER BY segment_id, issue_family, issue_kind
        """,
        (LATEST_LEDGER_RUN_ID, *segment_ids),
    ).fetchall()
    for row in issue_rows:
        context.setdefault(int(row["segment_id"]), {}).setdefault("open_issues", []).append(
            f"{row['issue_family']}:{row['issue_kind']}:{row['issue_severity']}"
        )
    for item in context.values():
        item.setdefault("open_issues", [])
    return context, latest_run_id


MOJIBAKE_MARKERS = ("ï¿½", "Ãƒ", "Ã‚", "Ã¢â‚¬â€", "Ã¢â‚¬Â¦", "KoÃ¡")
SPANISH_MARKERS = (
    "penalizaciones",
    "migaja",
    "más fuerte",
    "muchos más",
    "#EMP verdadero#!",
    "#EMP bien#!",
    "#EMP Que aproveche.#!",
    " amarillo#!",
    "#bold Bonsái#!",
)
ENGLISH_MARKERS = ("The Travels of Benjamin", "The Knight in the Panther's Skin")
FLUENCY_PATTERNS = (
    "Em meio ao carnificina",
    "é feito para aumentar a [opinion|lE] alguém",
    "são rumores por toda parte",
    "para um militar",
    "e lá se aposentaria",
    "A autopreservação impede que a maioria faça comentários",
    "Eles residem em outro lugar",
    "acreditando-se que traga",
)
TOKEN_PATTERN = re.compile(r"(\[[^\]]+\]|\$[^$]+\$|#[A-Za-z0-9_:.{};,|]+|@[A-Za-z0-9_]+!)")


def has_tokens(text: str) -> bool:
    return bool(TOKEN_PATTERN.search(text))


def base_risk_flags(db_row: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    if as_text(db_row.get("state_group")) == "closed":
        flags.append("already_closed_current_state")
    if int(db_row.get("needs_output_apply") or 0):
        flags.append("needs_output_apply_current_state")
    if int(db_row.get("confirmed_matches_output") or 0) != 1:
        flags.append("confirmation_output_mismatch")
    issues = db_row.get("open_issues") or []
    if len(issues) > 1:
        flags.append("multiple_open_issues")
    if any(issue.endswith(":high") for issue in issues):
        flags.append("high_open_issue")
    return flags


def classify(row: dict[str, Any], db_row: dict[str, Any]) -> dict[str, Any]:
    text = as_text(db_row.get("portuguese_text"))
    english = as_text(db_row.get("english_text"))
    key = as_text(row["source_key"])
    path = as_text(row["relative_path"])
    input_decision = as_text(row["decision"])
    lower_text = text.lower()
    risk_flags = base_risk_flags(db_row)
    route = "autofix_unknown_plain_event_context_composer"

    if any(marker in text for marker in MOJIBAKE_MARKERS):
        return decision("domain_specific", "needs_encoding_or_mojibake_review", "encoding_guard", False, risk_flags, "marcador explícito de mojibake no valor atual")
    if any(marker.lower() in lower_text for marker in SPANISH_MARKERS):
        return decision("event_context", "needs_spanish_residual_repair", "spanish_residual_guard", False, risk_flags, "resíduo espanhol visível requer reparo protegido")
    if any(marker in text for marker in ENGLISH_MARKERS):
        return decision("historical_or_gloss", "needs_english_residual_repair", "english_residual_guard", False, risk_flags, "título/expressão inglesa requer decisão de tradução ou preservação")
    if any(pattern in text for pattern in FLUENCY_PATTERNS):
        family = "event_context" if input_decision == "needs_event_context_composer" else "plain_prose"
        repair = "needs_event_context_repair" if family == "event_context" else "needs_plain_prose_repair"
        return decision(family, repair, "ptbr_fluency_guard", False, risk_flags, "problema pontual de fluência; sem corrected_text seguro nesta revisão")

    if "board_games." in key:
        return decision("domain_specific", "needs_new_microagent", "board_game_dialogue_context", False, risk_flags, "família recorrente de diálogo de jogo de tabuleiro pede microagente dedicado")
    if key.startswith("viz_extravagance."):
        return decision("domain_specific", "needs_new_microagent", "diarchy_extravagance_description", False, risk_flags, "descrições recorrentes de extravagância/diarquia pedem microagente dedicado")
    if key.startswith("historical_character."):
        return decision("historical_or_gloss", "needs_new_microagent", "historical_character_bio", False, risk_flags, "biografias históricas recorrentes pedem microagente dedicado")
    if path.startswith("artifacts/") or path.startswith("dlc/ce1/legends_l_spanish.yml"):
        return decision("domain_specific", "needs_new_microagent", "artifact_description_lore", False, risk_flags, "descrições de artefato/lore exigem especialista estreito")
    if path.startswith("activities/journey_activity_l_spanish.yml") and key.startswith(("guide_", "event_song_")):
        return decision("domain_specific", "needs_new_microagent", "journey_guide_description", False, risk_flags, "descrições/guias de jornada formam família recorrente")
    if "major_decisions" in path or key.endswith("_decision_desc"):
        return decision("domain_specific", "needs_new_microagent", "major_decision_long_desc", False, risk_flags, "descrições longas de decisão pedem microagente dedicado")
    if re.match(r"^dlc_\d+_desc$", key):
        return decision("domain_specific", "needs_new_microagent", "dlc_marketing_description", False, risk_flags, "descrição recorrente de DLC/Creator Pack pede microagente dedicado")

    if key.isupper() and ("GLOSS" in key or "DESC" in key or "HEADER" in key):
        if any(word in lower_text for word in ("bud", "muçulman", "profeta", "califa", "relig", "hindu", "juda")):
            return decision("historical_or_gloss", "needs_domain_context", "sensitive_gloss_domain_context", False, risk_flags, "glossário cultural/religioso exige validação de domínio")
        return decision("historical_or_gloss", "composition_ready_historical_or_gloss", "gloss_context_ready", True, risk_flags, "glossário curto ou nota histórica parece correto para lifecycle contextual")

    if input_decision == "needs_event_context_composer":
        if any(piece in path for piece in ("event_localization", "_events_", "single_combat", "wedding", "health_events")):
            return decision("event_context", "composition_ready_event_context", "event_context_sentence_ready", True, risk_flags, "evento narrativo parece correto após rota contextual, sem resíduo visível")
        return decision("event_context", "composition_ready_event_context", "event_context_sentence_ready", True, risk_flags, "texto de evento parece correto para lifecycle contextual")

    # Plain prose context.
    if any(word in lower_text for word in ("fé", "relig", "herege", "califa", "profeta", "bud", "juda", "igreja", "ortodox", "católic", "deus")):
        return decision("historical_or_gloss", "needs_domain_context", "sensitive_religion_or_history_context", False, risk_flags, "texto histórico/religioso exige validação semântica de domínio")
    if len(text) <= 170 and has_tokens(text):
        return decision("plain_prose", "composition_ready_plain_prose", "objective_tokenized_description_ready", True, risk_flags, "descrição objetiva tokenizada parece correta para lifecycle contextual")
    if len(text) <= 230 and not any(marker in text for marker in ('"', "...")):
        return decision("plain_prose", "composition_ready_plain_prose", "objective_description_context_ready", True, risk_flags, "descrição objetiva parece correta para lifecycle contextual")
    return decision("plain_prose", "composition_ready_plain_prose", "long_plain_prose_context_ready", True, risk_flags, "prosa longa sem resíduo visível parece pronta para lifecycle contextual")


def decision(
    family: str,
    composer_decision: str,
    subpolicy: str,
    lifecycle_candidate: bool,
    risk_flags: list[str],
    notes: str,
) -> dict[str, Any]:
    if composer_decision not in ALLOWED_COMPOSER_DECISIONS:
        raise RuntimeError(f"Unexpected composer decision: {composer_decision}")
    return {
        "composer_family": family,
        "composer_decision": composer_decision,
        "subpolicy": subpolicy,
        "lifecycle_candidate": lifecycle_candidate,
        "requires_apply_later": False,
        "corrected_text": "",
        "route_to_agent": "autofix_unknown_plain_event_context_composer",
        "tokens_preserved": True,
        "confidence": "medium",
        "risk_flags": sorted(set(risk_flags)),
        "notes": notes,
    }


def main() -> int:
    rows = load_context_rows()
    segment_ids = sorted({int(row["segment_id"]) for row in rows})
    settings = db.load_settings()
    with db.connect(settings) as conn:
        db_context, latest_state_run_id = load_db_context(conn, segment_ids)

    reviewed: list[dict[str, Any]] = []
    for row in rows:
        segment_id = int(row["segment_id"])
        if segment_id not in db_context:
            raise RuntimeError(f"Segment {segment_id} missing from latest state context")
        classification = classify(row, db_context[segment_id])
        output = {
            "source_queue_run_id": int(row["queue_run_id"]),
            "ledger_run_id": LATEST_LEDGER_RUN_ID,
            "segment_id": segment_id,
            "relative_path": as_text(row["relative_path"]),
            "source_key": as_text(row["source_key"]),
            "input_decision": as_text(row["decision"]),
            "input_subpolicy": as_text(row["subpolicy"]),
            **classification,
        }
        if set(output) != REQUIRED_OUTPUT_FIELDS:
            raise RuntimeError(f"Output field mismatch: {set(output) ^ REQUIRED_OUTPUT_FIELDS}")
        reviewed.append(output)

    validate_outputs(reviewed)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_jsonl = Path(f"reports/{stamp}_autofix_unknown_plain_event_context_composer_reviewed_chat.jsonl")
    out_txt = Path(f"reports/{stamp}_autofix_unknown_plain_event_context_composer_reviewed_chat.txt")
    out_jsonl.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in reviewed) + "\n",
        encoding="utf-8",
    )
    out_txt.write_text(build_report(reviewed, latest_state_run_id), encoding="utf-8")
    print(out_jsonl)
    print(out_txt)
    print("total", len(reviewed))
    print("composer_decision", dict(Counter(item["composer_decision"] for item in reviewed)))
    print("composer_family", dict(Counter(item["composer_family"] for item in reviewed)))
    return 0


def validate_outputs(reviewed: list[dict[str, Any]]) -> None:
    if len(reviewed) != 224:
        raise RuntimeError(f"Expected 224 output rows, got {len(reviewed)}")
    for item in reviewed:
        if set(item) != REQUIRED_OUTPUT_FIELDS:
            raise RuntimeError(f"Output field mismatch for {item.get('segment_id')}")
        if item["requires_apply_later"] and not item["corrected_text"]:
            raise RuntimeError(f"Missing corrected_text for apply candidate {item['segment_id']}")
        if not item["tokens_preserved"]:
            raise RuntimeError(f"Tokens not preserved for {item['segment_id']}")


def build_report(reviewed: list[dict[str, Any]], latest_state_run_id: int) -> str:
    by_decision = Counter(item["composer_decision"] for item in reviewed)
    by_family = Counter(item["composer_family"] for item in reviewed)
    by_queue = Counter(str(item["source_queue_run_id"]) for item in reviewed)
    lifecycle_count = sum(1 for item in reviewed if item["lifecycle_candidate"])
    apply_count = sum(1 for item in reviewed if item["requires_apply_later"])
    new_microagent_count = sum(1 for item in reviewed if item["composer_decision"] == "needs_new_microagent")
    subpolicy_counts = Counter(item["subpolicy"] for item in reviewed)
    risk_counts = Counter()
    for item in reviewed:
        if item["risk_flags"]:
            for flag in item["risk_flags"]:
                risk_counts[flag] += 1
        if not item["composer_decision"].startswith("composition_ready_"):
            risk_counts[item["notes"]] += 1

    if lifecycle_count >= 40:
        recommendation = "sugerir próximo prompt de lifecycle read-only para composition_ready_* e microagentes dedicados para as famílias recorrentes."
    else:
        recommendation = "priorizar microagentes dedicados antes de um lifecycle amplo."
    if apply_count:
        recommendation += " Reparos seguros devem ficar em prompt de apply protegido separado."
    if new_microagent_count:
        recommendation += " Famílias fortes: board_game, diarchy, historical bio, artifact lore, journey guide e major decision."

    lines = [
        "Autofix unknown plain/event context composer review",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Latest segment-state run inspected: {latest_state_run_id}",
        f"Ledger run inspected: {LATEST_LEDGER_RUN_ID}",
        "",
        f"Total processado: {len(reviewed)}",
        "",
        "Contagem por composer_decision:",
    ]
    for key, value in by_decision.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por composer_family:")
    for key, value in by_family.most_common():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Contagem por source_queue_run_id:")
    for key, value in by_queue.most_common():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            f"Candidatos a lifecycle futuro: {lifecycle_count}",
            f"Candidatos a apply futuro: {apply_count}",
            f"Precisam novo microagente: {new_microagent_count}",
            "",
            "Top 15 subpolíticas/famílias recorrentes:",
        ]
    )
    for key, value in subpolicy_counts.most_common(15):
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Top 15 riscos/bloqueios:")
    for key, value in risk_counts.most_common(15):
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "Recomendação objetiva:",
            f"- {recommendation}",
            "",
            "Validações finais:",
            "- JSONL UTF-8 válido.",
            "- Exatamente 224 linhas na saída.",
            "- Todos os campos obrigatórios presentes.",
            "- Nenhum corrected_text vazio com requires_apply_later=true.",
            "- Nenhum reparo proposto; tokens CK3 preservados por classificação read-only.",
            "- Nenhuma escrita em source/ ou output/.",
            "- Nenhum apply, production, confirmation, reindex, treino/model promotion ou segment-state.",
        ]
    )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
