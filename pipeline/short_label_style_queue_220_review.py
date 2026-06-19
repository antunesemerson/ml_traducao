from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "reports" / "20260617_214002_issue_review_queue_micro_short_label_style.jsonl"
REPORTS = ROOT / "reports"

TOKEN_RE = re.compile(
    r"\[[^\]]+\]"
    r"|\$[^$\s]+\$"
    r"|@[A-Za-z0-9_.:-]+!"
    r"|Custom\([^)]*\)"
    r"|Select_CString\([^)]*\)"
)


def structural_tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text or "")


def has_dynamic_expression(text: str) -> bool:
    markers = ("Get", "Custom(", "Select_CString(", "ScriptValue(", "MakeScope", "SCOPE.", "ROOT.")
    return any(marker in (text or "") for marker in markers)


def has_gender_expression(text: str) -> bool:
    markers = ("Select_CString(", "Custom('", "Custom(\"", "ES_", "_Masc", "_Fem")
    return any(marker in (text or "") for marker in markers)


def looks_spanish_residual(text: str) -> bool:
    lowered = f" {text or ''} ".lower()
    spanish_markers = (
        " desbloquea ",
        " ningún ",
        " ninguna ",
        " tienes ",
        " tiene ",
        " del ",
        " de la fe ",
        " actualmente ",
        " ahora ",
        "#bold no#!",
        "#bold no#!",
    )
    portuguese_markers = (" não ", " você ", " seu ", " sua ", " desbloqueia ", " atualmente ")
    if "#bold no#!" in lowered and " não " in lowered:
        return True
    return any(marker in lowered for marker in spanish_markers) and not any(marker in lowered for marker in portuguese_markers)


def safe_short_repair(text: str) -> str | None:
    if not text:
        return None
    corrected = text
    corrected = corrected.replace("#bold No#!", "#bold Não#!")
    corrected = corrected.replace("#bold NO#!", "#bold NÃO#!")
    corrected = corrected.replace("#bold no#!", "#bold não#!")
    corrected = corrected.replace(" ;", ";").replace(" :", ":").replace(" ,", ",")
    corrected = re.sub(r"(\$[A-Z0-9_|.]+?\$)#bold", r"\1 #bold", corrected)
    if corrected == text:
        return None
    if structural_tokens(corrected) != structural_tokens(text):
        return None
    return corrected


def compact_candidate(row: dict) -> bool:
    evidence = row.get("evidence") or {}
    text = (row.get("texts") or {}).get("confirmed_text") or ""
    return evidence.get("token_count", 0) >= 3 or "\n" in text or "$TAB$" in text or "$BULLET" in text


def base_record(row: dict) -> dict:
    texts = row.get("texts") or {}
    return {
        "queue_run_id": row.get("queue_run_id"),
        "queue_item_id": row.get("queue_item_id"),
        "ledger_item_id": row.get("ledger_item_id"),
        "segment_id": row.get("segment_id"),
        "agent_key": row.get("agent_key"),
        "queue_bucket": row.get("queue_bucket"),
        "source_key": row.get("source_key"),
        "relative_path": row.get("relative_path"),
        "decision": None,
        "confidence": None,
        "issue_tags": [],
        "rationale": None,
        "current_text": texts.get("confirmed_text"),
        "english_text": texts.get("english_text"),
        "spanish_text": texts.get("spanish_text"),
        "corrected_text": None,
        "tokens_preserved": True,
        "requires_apply_later": False,
        "lifecycle_candidate": False,
        "suggested_subpolicy": None,
    }


def classify(row: dict) -> dict:
    rec = base_record(row)
    bucket = row.get("queue_bucket")
    evidence = row.get("evidence") or {}
    domain = evidence.get("domain")
    text = rec["current_text"] or ""
    source_key = row.get("source_key") or ""
    path = row.get("relative_path") or ""
    issue_codes = evidence.get("issue_codes") or []

    def finish(decision, confidence, tags, rationale, subpolicy=None, lifecycle=False, corrected=None):
        rec["decision"] = decision
        rec["confidence"] = confidence
        rec["issue_tags"] = tags
        rec["rationale"] = rationale
        rec["suggested_subpolicy"] = subpolicy
        rec["lifecycle_candidate"] = lifecycle
        if corrected:
            rec["corrected_text"] = corrected
            rec["tokens_preserved"] = structural_tokens(corrected) == structural_tokens(text)
            rec["requires_apply_later"] = True
            rec["lifecycle_candidate"] = False
        return rec

    if has_gender_expression(text):
        return finish(
            "needs_gender_token_agent",
            0.86,
            ["gender_or_select_cstring", "tokenized"],
            "Expressao de genero/Select_CString exige agente de genero/tokens, nao estilo curto generico.",
            "needs_gender_token_agent",
        )

    if bucket == "active_safe_candidate_autofix":
        return finish(
            "active_safe_candidate_confirmed",
            0.89,
            ["active_safe", "candidate_autofix_rejected"],
            "O texto ativo ja esta seguro para esta frente; a fila deve aprender a nao substituir por autofix generico.",
            "active_safe_candidate_autofix_false_reopen",
            lifecycle=True,
        )

    if bucket == "domain_titles_names" or domain == "domain_titles_names" or "title" in path or "TITLE" in text:
        return finish(
            "needs_title_policy",
            0.9,
            ["title_or_name", "domain_sensitive"],
            "Titulo, cargo ou nome proprio precisa de policy de dominio antes de fechamento por estilo.",
            "domain_title_short_label_needs_policy",
        )

    if bucket == "domain_religion" or domain == "domain_religion" or "faith" in text.lower() or "religion" in text.lower():
        return finish(
            "needs_religion_policy",
            0.9,
            ["religion_domain", "domain_sensitive"],
            "Conceito de fe/religiao/doutrina deve sair do estilo generico e ir para policy religiosa.",
            "domain_religion_short_label_needs_policy",
        )

    if bucket == "domain_culture" or domain == "domain_culture" or "CultureTradition" in text or "tradition_" in text:
        return finish(
            "needs_culture_policy",
            0.9,
            ["culture_domain", "domain_sensitive"],
            "Tradicao/cultura/inovacao deve ser roteada para policy cultural em vez de checkpoint de estilo generico.",
            "domain_culture_short_label_needs_policy",
        )

    if bucket in {"needs_human_conflict", "domain_events_longform"}:
        return finish(
            "needs_semantic_review",
            0.82,
            ["semantic_conflict", "not_style_only"],
            "O item depende de comparacao semantica ou contexto de evento, nao apenas de estilo de label curto.",
            "semantic_conflict_short_label_router",
        )

    corrected = safe_short_repair(text)
    if corrected and (bucket == "domain_rules_tooltips" or "space_before_punctuation" in issue_codes or looks_spanish_residual(text)):
        return finish(
            "needs_repair",
            0.88,
            ["short_microrepair", "tokens_preserved"],
            "Ha reparo curto e mecanico com tokens estruturais preservados.",
            "short_label_microrepair_safe",
            corrected=corrected,
        )

    if looks_spanish_residual(text):
        return finish(
            "spanish_residual_repair_needed",
            0.84,
            ["spanish_residual", "needs_repair"],
            "O texto atual ainda carrega residual espanhol claro e deve ir para microreparo protegido.",
            "short_label_microrepair_safe",
        )

    if bucket == "domain_rules_tooltips":
        if compact_candidate(row):
            return finish(
                "compact_ui_label_ready",
                0.84,
                ["rules_tooltip", "compact_ui", "tokenized"],
                "Tooltip/label compacto com tokens preservados e padrao reutilizavel de UI.",
                "trigger_requirement_short_label_guard" if "trigger" in path else "effect_reward_short_label_guard",
                lifecycle=True,
            )
        return finish(
            "short_label_style_ready",
            0.82,
            ["rules_tooltip", "short_label"],
            "Label curto de regra esta natural em PT-BR e e bom candidato para checkpoint de estilo.",
            "trigger_requirement_short_label_guard",
            lifecycle=True,
        )

    if bucket == "domain_interactions_activities":
        return finish(
            "needs_domain_policy",
            0.8,
            ["interaction_or_activity", "domain_context"],
            "Interacao/atividade precisa de contexto de dominio antes de virar regra generica de estilo.",
            "semantic_conflict_short_label_router",
        )

    if bucket == "package_dlc":
        if has_dynamic_expression(text) and evidence.get("token_count", 0) >= 8:
            return finish(
                "needs_dynamic_expression_agent",
                0.82,
                ["dlc", "dynamic_expression", "token_dense"],
                "Item DLC e denso em expressoes dinamicas; melhor roteamento para agente CK3 dinamico.",
                "semantic_conflict_short_label_router",
            )
        return finish(
            "needs_domain_policy",
            0.78,
            ["dlc", "domain_context"],
            "Item DLC depende de contexto de pacote/domino e nao deve ser fechado como estilo generico.",
            "semantic_conflict_short_label_router",
        )

    if bucket == "general_short_label":
        if has_dynamic_expression(text) and evidence.get("token_count", 0) >= 10:
            return finish(
                "needs_dynamic_expression_agent",
                0.84,
                ["dynamic_expression", "token_dense"],
                "Expressao dinamica longa deve ir para o microagente dinamico.",
                "semantic_conflict_short_label_router",
            )
        subpolicy = "compact_ui_tokenized_label_guard" if compact_candidate(row) else "compact_ui_no_token_label_guard"
        decision = "compact_ui_label_ready" if compact_candidate(row) else "short_label_style_ready"
        return finish(
            decision,
            0.83,
            ["general_short_label", "style_candidate"],
            "Label curto/compacto em PT-BR e reutilizavel como regra de estilo.",
            subpolicy,
            lifecycle=True,
        )

    return finish(
        "blocked_uncertain",
        0.55,
        ["unclassified", "needs_manual_review"],
        "Nao ha evidencia suficiente para fechar como estilo curto com seguranca.",
        None,
    )


def write_report(rows: list[dict], txt_path: Path, jsonl_path: Path) -> None:
    by_decision = Counter(row["decision"] for row in rows)
    by_bucket = Counter(row["queue_bucket"] for row in rows)
    by_subpolicy = Counter(row["suggested_subpolicy"] or "null" for row in rows)
    lifecycle = sum(1 for row in rows if row["lifecycle_candidate"])
    apply_later = sum(1 for row in rows if row["requires_apply_later"])
    out_routes = Counter(
        row["decision"]
        for row in rows
        if row["decision"]
        in {
            "needs_semantic_review",
            "needs_domain_policy",
            "needs_culture_policy",
            "needs_religion_policy",
            "needs_title_policy",
            "needs_dynamic_expression_agent",
            "needs_gender_token_agent",
        }
    )
    top_useful = [
        "Active/output seguro rejeita autofix generico em labels curtos.",
        "Tooltips de requisito/efeito com tokens preservados podem virar guard de checkpoint.",
        "Labels compactos gerais sem dominio sensivel podem alimentar guard tokenizado/no-token.",
        "Reparos de '#bold no#!' para '#bold nao#!' sao microreparo curto quando fora de dominio sensivel.",
        "Espaco antes de pontuacao em tooltip e reparo mecanico seguro com tokens preservados.",
    ]
    top_blocking = [
        "Titulos, cargos e nomes proprios precisam de policy propria.",
        "Religiao/fe/doutrina nao deve fechar como estilo generico.",
        "Cultura/tradicoes/inovacoes precisam de policy cultural.",
        "Eventos longform e conflitos humanos precisam de comparacao semantica.",
        "Expressoes CK3 densas com Get/ScriptValue devem ir ao agente dinamico.",
    ]

    lines = [
        "Short label style queue 220 reviewed chat",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Input: {INPUT.relative_to(ROOT)}",
        f"Output JSONL: {jsonl_path.relative_to(ROOT)}",
        "",
        f"Total reviewed: {len(rows)}",
        f"Lifecycle/checkpoint candidates: {lifecycle}",
        f"Requires future apply: {apply_later}",
        "",
        "Counts by decision:",
    ]
    lines += [f"- {key}: {value}" for key, value in by_decision.most_common()]
    lines += ["", "Counts by queue_bucket:"]
    lines += [f"- {key}: {value}" for key, value in by_bucket.most_common()]
    lines += ["", "Counts by suggested_subpolicy:"]
    lines += [f"- {key}: {value}" for key, value in by_subpolicy.most_common()]
    lines += ["", "Routes out of short_label_style:"]
    lines += [f"- {key}: {value}" for key, value in out_routes.most_common()]
    lines += ["", "Top 5 useful patterns:"]
    lines += [f"- {item}" for item in top_useful]
    lines += ["", "Top 5 blocking patterns:"]
    lines += [f"- {item}" for item in top_blocking]
    lines += [
        "",
        "Recommended next steps:",
        "- Criar checkpoint/lifecycle read-only para active_safe_candidate_autofix e guards compactos.",
        "- Criar microreparo protegido apenas para short_label_microrepair_safe.",
        "- Abrir filas dedicadas para title/religion/culture e semantica de eventos/conflitos.",
        "- Roteiar expressoes CK3 densas para micro_dynamic_ck3_expression.",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    rows = [json.loads(line) for line in INPUT.read_text(encoding="utf-8").splitlines() if line.strip()]
    reviewed = [classify(row) for row in rows]
    if len(reviewed) != 240:
        raise SystemExit(f"expected 240 reviewed rows, got {len(reviewed)}")
    for row in reviewed:
        json.dumps(row, ensure_ascii=False)
        if row["corrected_text"] == "":
            raise SystemExit(f"empty corrected_text for segment {row['segment_id']}")
        if row["corrected_text"] and not row["tokens_preserved"]:
            raise SystemExit(f"token preservation failed for segment {row['segment_id']}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    jsonl_path = REPORTS / f"{stamp}_short_label_style_queue_220_reviewed_chat.jsonl"
    txt_path = REPORTS / f"{stamp}_short_label_style_queue_220_reviewed_chat.txt"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in reviewed:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    write_report(reviewed, txt_path, jsonl_path)
    print(json.dumps({"jsonl": str(jsonl_path), "report": str(txt_path), "rows": len(reviewed)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
