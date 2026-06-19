from __future__ import annotations

import collections
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


QUEUE_219 = Path(
    "reports/20260617_203727_935310_issue_semantic_short_label_blocked_surface_queue_micro_event_context_composer_dry_run_39.jsonl"
)
QUEUE_218 = Path(
    "reports/20260617_203727_934319_issue_semantic_short_label_blocked_surface_queue_micro_requirement_tooltip_surface_dry_run_39.jsonl"
)

TOKEN_RE = re.compile(r"\$[^$\s]+\$|\[[^\]]+\]|#[A-Za-z0-9_]+|#!|@[A-Za-z0-9_]+!|\\n")
BAD_MARKERS = ("�", "Ãƒ", "Ã‚", "Ã¯Â¿Â½")


SAFE_CORRECTIONS = {
    64650: "Seu [player_heir|lE] tem menos de #high 3#! [friends|lE]",
    76130: "Requer a [house_aspiration|El] #V $prosperity_house_aspiration$#!",
}


def tokens(text: str | None) -> collections.Counter[str]:
    return collections.Counter(TOKEN_RE.findall(text or ""))


def load_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def current_text(row: dict[str, Any]) -> str:
    return str(row.get("confirmed_text") or row.get("evidence_text") or "")


def base_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "queue_run_id": int(row["queue_run_id"]),
        "queue_item_id": row.get("queue_item_id"),
        "segment_id": int(row["segment_id"]),
        "agent_key": row.get("agent_key"),
        "source_key": row.get("source_key"),
        "relative_path": row.get("relative_path"),
        "current_text": current_text(row),
    }


def decide_219(row: dict[str, Any]) -> dict[str, Any]:
    text = current_text(row)
    signature = str(row.get("text_signature") or "")
    path_domain = str(row.get("path_domain") or "")
    source_key = str(row.get("source_key") or "")
    tags = [str(row.get("block_reason")), signature, path_domain]
    corrected = None

    if any(marker in text for marker in BAD_MARKERS):
        decision = "encoding_mojibake_repair_needed"
        confidence = 0.9
        rationale = "O texto real contém marcador de encoding quebrado e precisa de saneamento antes de virar checkpoint."
        lifecycle = False
        subpolicy = "mojibake_domain_context_guard"
    elif signature == "requirement_like_phrase":
        decision = "needs_new_microagent"
        confidence = 0.78
        rationale = "O item tem forma de requisito/tooltip dentro da fila de contexto de evento; é melhor roteá-lo para uma subpolítica própria."
        lifecycle = False
        subpolicy = "event_context_requirement_like_router"
    elif signature in {"mixed_event_surface", "dialogue_option_phrase"} or "\\n\\n" in text or '"' in text:
        decision = "already_good_context_confirmed"
        confidence = 0.82
        rationale = "O texto atual parece aceitável, mas depende do contexto narrativo/evento para não ser tratado como short-label simples."
        lifecycle = True
        subpolicy = "event_context_sentence_or_dialogue_guard"
    elif source_key.endswith("_desc") or ".desc" in source_key or signature in {"short_sentence", "short_phrase"}:
        decision = "context_composer_ready"
        confidence = 0.88
        rationale = "Descrição curta em PT-BR natural; bom candidato para checkpoint do compositor de contexto de evento."
        lifecycle = True
        subpolicy = "event_context_short_description_guard"
    else:
        decision = "needs_domain_context"
        confidence = 0.68
        rationale = "O texto pode estar correto, mas o domínio do uso ainda não ficou forte o bastante para fechamento automático."
        lifecycle = False
        subpolicy = "event_context_domain_context_needed"

    result = base_record(row)
    result.update(
        {
            "decision": decision,
            "confidence": confidence,
            "issue_tags": [tag for tag in tags if tag],
            "rationale": rationale,
            "corrected_text": corrected,
            "tokens_preserved": True,
            "requires_apply_later": False,
            "lifecycle_candidate": lifecycle,
            "suggested_subpolicy": subpolicy,
        }
    )
    return result


def decide_218(row: dict[str, Any]) -> dict[str, Any]:
    text = current_text(row)
    signature = str(row.get("text_signature") or "")
    key_surface = str(row.get("key_surface") or "")
    path_domain = str(row.get("path_domain") or "")
    segment_id = int(row["segment_id"])
    tags = [str(row.get("block_reason")), signature, key_surface, path_domain]
    corrected = SAFE_CORRECTIONS.get(segment_id)

    if corrected is not None:
        decision = "needs_repair"
        confidence = 0.94
        rationale = "Correção curta e inequívoca de PT-BR em tooltip, preservando os tokens estruturais."
        lifecycle = False
        subpolicy = "requirement_tooltip_microrepair_safe"
    elif any(marker in text for marker in BAD_MARKERS):
        decision = "encoding_mojibake_repair_needed"
        confidence = 0.9
        rationale = "O texto real contém marcador de encoding quebrado e precisa de saneamento antes de virar checkpoint."
        lifecycle = False
        subpolicy = "mojibake_requirement_tooltip_guard"
    elif key_surface != "requirement_or_tooltip_key":
        decision = "needs_domain_context"
        confidence = 0.72
        rationale = "Apesar da forma de requisito, a chave não é claramente de tooltip/requisito e pede contexto de uso."
        lifecycle = False
        subpolicy = "requirement_tooltip_key_surface_guard"
    elif signature in {"requirement_like_phrase", "short_phrase", "short_sentence", "effect_or_reward_phrase"}:
        decision = "requirement_tooltip_ready"
        confidence = 0.88
        rationale = "Tooltip/requisito curto em PT-BR aceitável; bom candidato para checkpoint de superfície de requisito."
        lifecycle = True
        subpolicy = "requirement_tooltip_short_phrase_guard"
    elif signature == "mixed_event_surface":
        decision = "already_good_context_confirmed"
        confidence = 0.8
        rationale = "O texto parece correto, mas mistura superfície de tooltip com contexto narrativo e deve exigir guard contextual."
        lifecycle = True
        subpolicy = "requirement_tooltip_mixed_surface_guard"
    else:
        decision = "needs_domain_context"
        confidence = 0.68
        rationale = "O texto pode estar correto, mas precisa de evidência contextual antes de fechamento automático."
        lifecycle = False
        subpolicy = "requirement_tooltip_domain_context_needed"

    preserved = True if corrected is None else tokens(text) == tokens(corrected)
    result = base_record(row)
    result.update(
        {
            "decision": decision,
            "confidence": confidence,
            "issue_tags": [tag for tag in tags if tag],
            "rationale": rationale,
            "corrected_text": corrected,
            "tokens_preserved": preserved,
            "requires_apply_later": corrected is not None,
            "lifecycle_candidate": lifecycle,
            "suggested_subpolicy": subpolicy,
        }
    )
    return result


def validate(rows: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    serialized = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    for marker in BAD_MARKERS:
        if marker in serialized:
            errors.append(f"bad_encoding_marker:{marker}")
    for row in rows:
        corrected = row.get("corrected_text")
        if corrected is not None:
            if corrected == "":
                errors.append(f"empty_corrected_text:{row['segment_id']}")
            if not row.get("tokens_preserved"):
                errors.append(f"token_mismatch:{row['segment_id']}")
            if tokens(row.get("current_text")) != tokens(corrected):
                errors.append(f"token_validation_failed:{row['segment_id']}")
    return errors


def write_review(queue_id: int, rows: list[dict[str, Any]], timestamp: str) -> tuple[Path, Path]:
    jsonl_path = Path("reports") / f"{timestamp}_semantic_short_label_domain_context_queue_{queue_id}_reviewed_chat.jsonl"
    txt_path = Path("reports") / f"{timestamp}_semantic_short_label_domain_context_queue_{queue_id}_reviewed_chat.txt"
    errors = validate(rows)
    if errors:
        raise RuntimeError("; ".join(errors))

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    counts = collections.Counter(row["decision"] for row in rows)
    subpolicies = collections.Counter(row["suggested_subpolicy"] for row in rows)
    lifecycle_count = sum(1 for row in rows if row["lifecycle_candidate"])
    apply_count = sum(1 for row in rows if row["requires_apply_later"])
    lines = [
        f"Semantic short-label domain-context queue {queue_id} reviewed by chat",
        f"items={len(rows)}",
        f"lifecycle_candidates={lifecycle_count}",
        f"requires_apply_later={apply_count}",
        "",
        "Decisions:",
    ]
    for decision, count in counts.most_common():
        lines.append(f"- {decision}: {count}")
    lines.append("")
    lines.append("Top subpolicy patterns:")
    for subpolicy, count in subpolicies.most_common(10):
        lines.append(f"- {subpolicy}: {count}")
    lines.append("")
    lines.append("Examples:")
    for row in rows[:12]:
        lines.append(
            f"- {row['segment_id']} | {row['decision']} | {row['source_key']} | {row['rationale']}"
        )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path


def main() -> None:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    rows_219 = [decide_219(row) for row in load_rows(QUEUE_219)]
    rows_218 = [decide_218(row) for row in load_rows(QUEUE_218)]
    paths = [
        *write_review(219, rows_219, timestamp),
        *write_review(218, rows_218, timestamp),
    ]
    for path in paths:
        print(path)
    for queue_id, rows in ((219, rows_219), (218, rows_218)):
        print(f"queue={queue_id} items={len(rows)} counts={dict(collections.Counter(row['decision'] for row in rows))}")


if __name__ == "__main__":
    main()
