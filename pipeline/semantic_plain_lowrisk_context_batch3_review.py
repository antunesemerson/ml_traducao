from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import db


SOURCE = "semantic_plain_lowrisk_context_batch3_review_v1"
TARGET_DOMAINS = {"historical_characters", "culture"}

CORRECTIONS = {
    62379: "A principal fonte de lápis-lazúli no mundo, as minas de Sar-i Sang estão ativas desde tempos pré-históricos, e suas pedras azuis podiam ser vistas em todas as cortes da antiguidade. Trabalhadores locais trabalham aqui dia e noite para extrair as cobiçadas pedras azuis.",
    153479: "Karunakara foi um famoso general do rei chola Kulottunga e esmagou tanto inimigos quanto traidores da causa chola. A vida de Karunakara é descrita no poema Kalinkkattuparani, em que sua dominação de Kalinga e sua posterior homenagem a Kulottunga são exaltadas.",
    153493: "Hildegard von Bingen foi uma abadessa visionária, mística, escritora e compositora, célebre por suas visões religiosas e obras eruditas. Ela escreveu textos sobre teologia, medicina e música, contribuindo para o pensamento cristão medieval. Hildegard também era conhecida por suas composições litúrgicas e peças teatrais, que enfatizavam a cura espiritual e holística.",
    153499: "Famoso guerreiro-poeta islandês, Egil Skallagrímsson é a figura central de Egil's Saga. Conhecido por seu temperamento feroz e talento poético, ele é lembrado por seus atos violentos, versos poderosos e papel significativo no povoamento inicial da Islândia e nas incursões viquingues na Inglaterra.",
    153500: "Gunnlaug Ormstunga, protagonista de Gunnlaugs saga ormstungu, foi um guerreiro-poeta viquingue conhecido por suas viagens e rivalidades poéticas. A saga narra sua trágica história de amor e sua luta com um rival poético, retratando a dinâmica de honra e fama na sociedade viquingue.",
    153503: "Hasan al-Haytham foi um matemático e físico árabe do século XI, célebre por seu trabalho em óptica. Seu livro Kitab al-Manazir (O Livro de Óptica) estabeleceu os princípios da óptica moderna e influenciou a ciência ocidental durante a Idade Média. É lembrado por sua abordagem experimental rigorosa e por contribuições significativas à Era de Ouro Islâmica.",
    153504: "Ibn Battuta foi um explorador marroquino do século XIV conhecido por suas extensas viagens, percorrendo mais de 120.000 quilômetros pelo mundo islâmico e além. Seu relato de viagens, Rihla, oferece percepções detalhadas sobre culturas e sociedades medievais, tornando-o um dos viajantes mais célebres da história.",
}

ALREADY_OK = {62387, 153484, 153487, 153489}
HOLD_SEGMENTS = {
    153501: "hold_saga_title_localization_consistency",
}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def latest_review_packet() -> Path:
    matches = sorted(
        reports_dir().glob("*_semantic_plain_lowrisk_human_review_packet.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise SystemExit("missing semantic_plain_lowrisk_human_review_packet jsonl")
    return matches[0]


def read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("initial_classification") == "needs_more_context" and row.get("source_domain") in TARGET_DOMAINS:
                rows.append(row)
    return rows


def fetch_live_context(conn, segment_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS current_output_text
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        WHERE s.id = ?
        """,
        (segment_id,),
    ).fetchone()
    if not row:
        raise SystemExit(f"missing segment context: {segment_id}")
    return dict(row)


def classify(row: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    segment_id = int(row["segment_id"])
    current = str(live.get("current_output_text") or row.get("current_output_text") or "")
    corrected = CORRECTIONS.get(segment_id, "")
    if segment_id in HOLD_SEGMENTS:
        decision = "hold_term_or_title_consistency"
        rationale = HOLD_SEGMENTS[segment_id]
    elif segment_id in ALREADY_OK:
        decision = "already_ok"
        rationale = "translation is acceptable; no correction needed"
    elif corrected:
        decision = "human_correction_candidate"
        rationale = "biographical/culture prose fluency correction; no CK3 tokens"
    else:
        decision = "needs_human_context_review"
        rationale = "not enough signal for a safe correction"
    return {
        "segment_id": segment_id,
        "relative_path": live.get("relative_path"),
        "source_key": live.get("source_key"),
        "source_line_number": live.get("source_line_number"),
        "source_domain": row.get("source_domain"),
        "decision": decision,
        "rationale": rationale,
        "current_output_text": current,
        "corrected_text": corrected,
        "english_text": live.get("english_text"),
        "spanish_text": live.get("spanish_text"),
        "token_guard_ok": not any(marker in current or marker in corrected for marker in ("[", "]", "$", "#", "Select_CString", ".Custom('ES_")),
        "requires_apply_later": False,
        "requires_learning_if_approved": bool(corrected) or decision == "already_ok",
    }


def build_summary(input_path: Path, reviewed: list[dict[str, Any]]) -> dict[str, Any]:
    decision_counts = Counter(row["decision"] for row in reviewed)
    correction_candidates = sum(1 for row in reviewed if row["corrected_text"])
    already_ok_count = sum(1 for row in reviewed if row["decision"] == "already_ok")
    token_guard_failures = sum(1 for row in reviewed if not row["token_guard_ok"])
    return {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_packet": str(input_path),
        "target_domains": sorted(TARGET_DOMAINS),
        "reviewed_count": len(reviewed),
        "decision_counts": [{"key": key, "count": value} for key, value in decision_counts.most_common()],
        "correction_candidate_count": correction_candidates,
        "already_ok_count": already_ok_count,
        "token_guard_failure_count": token_guard_failures,
        "reviewed": reviewed,
        "apply_ready_now": 0,
        "production_full_recommended_now": False,
        "next_action": "human_review_batch3_corrections_ok_and_holds",
    }


def write_outputs(summary: dict[str, Any]) -> tuple[Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_semantic_plain_lowrisk_context_batch3_review"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in summary["reviewed"]:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "semantic plain lowrisk context batch3 review",
        f"source={SOURCE}",
        f"input_packet={summary['input_packet']}",
        f"target_domains={','.join(summary['target_domains'])}",
        f"reviewed_count={summary['reviewed_count']}",
        "",
        "decision_counts:",
    ]
    for item in summary["decision_counts"]:
        lines.append(f"- {item['count']} | {item['key']}")
    lines.extend(
        [
            "",
            f"correction_candidate_count={summary['correction_candidate_count']}",
            f"already_ok_count={summary['already_ok_count']}",
            f"token_guard_failure_count={summary['token_guard_failure_count']}",
            f"apply_ready_now={summary['apply_ready_now']}",
            f"production_full_recommended_now={str(summary['production_full_recommended_now']).lower()}",
            f"next_action={summary['next_action']}",
            "",
            "reviewed:",
        ]
    )
    for row in summary["reviewed"]:
        lines.append(f"- {row['segment_id']} | {row['decision']} | {row['rationale']}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path


def main() -> None:
    input_path = latest_review_packet()
    rows = read_rows(input_path)
    with db.connect(db.load_settings()) as conn:
        reviewed = [classify(row, fetch_live_context(conn, int(row["segment_id"]))) for row in rows]
    summary = build_summary(input_path, reviewed)
    txt_path, jsonl_path, summary_path = write_outputs(summary)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"reviewed_count={summary['reviewed_count']}")
    print(f"correction_candidate_count={summary['correction_candidate_count']}")
    print(f"already_ok_count={summary['already_ok_count']}")
    print(f"token_guard_failure_count={summary['token_guard_failure_count']}")
    print(f"next_action={summary['next_action']}")


if __name__ == "__main__":
    main()
