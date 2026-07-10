from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "domain_policy_vote_candidate_human_packet_learning_ingest_v1"
QUEUE_SOURCE = "domain-policy-vote-candidate-human-packet"
ORIGIN = "human_confirmed_domain_policy_vote_candidate_human_packet"
MATCH_TYPE_ALREADY_OK = "domain_policy_vote_candidate_human_packet_already_ok"
MATCH_TYPE_CORRECTION = "domain_policy_vote_candidate_human_packet_correction"
REVIEWER = "user_human_review_domain_policy_vote_candidate_human_packet"
FOCUS_GROUP = "domain_policy_vote_candidate_human_packet"
SEGMENT_STATE_RUN_ID = 482

APPROVED_ALREADY_OK_SEGMENT_IDS = [
    20165,
    20113,
    16562,
    16643,
    16690,
    16728,
    9580,
    10548,
    9438,
    16590,
    16548,
    16540,
]

APPROVED_CORRECTIONS = {
    20182: "Os governantes desta cultura frequentemente mantêm várias concubinas em sua casa, independentemente de sua fé.",
    6130: "Este é o emblema do cargo de Imperador do Sacro Império Romano. Garantido para chamar a atenção de todos os duques que passarem.",
    20278: "Embora nem todos nasçam lutadores, com a têmpera certa e um braço forte para a espada, qualquer um pode mostrar que tem coração de guerreiro e assim se tornar um na prática.",
    20071: "Membros desta cultura frequentemente convivem com pessoas de diferentes fés e crenças, e se adaptam bem à adoção de elementos estrangeiros em seu próprio culto.",
    20238: "Aqueles que vivem na Estepe sempre terão muito em comum com aqueles que compartilham seu modo de vida. Estrangeiros podem cultuar de forma diferente, mas ainda vivem na sela.",
    10532: "Um antigo mosteiro erguido exatamente no lugar onde Santa Nina plantou uma cruz milagrosa enquanto evangelizava a Geórgia.\nO edifício sagrado atrai peregrinos de todo o Cáucaso.",
    6450: "Este grande recipiente é onde o fogo sagrado, central para muitas das cerimônias da fé zoroástrica, é aceso. Uma inscrição antiga fala sobre a pessoa que originalmente o doou ao sacerdócio.",
    16566: "A lei do mais forte vigorou por gerações, mas, se tivermos uma justificativa para nossas guerras, podemos evitar provocar a ira de nossos vizinhos e a retaliação que muitas vezes vem com ela.",
    16600: "Além de fortificar apenas a torre de menagem, construir uma muralha externa resistente que possa ser guarnecida com segurança por nossos arqueiros dará aos nossos castelos uma camada extra de proteção contra invasores.",
    20167: "Os membros desta cultura acreditam que o melhor missionário é aquele que carrega uma espada. Embora o apoio às guerras santas seja generalizado, os motivos são analisados para garantir que os poderes divinos aprovem.",
    16869: "Esta cultura adotou a arte marcial e o culto ritual das espadas largas de duas mãos, permitindo cultivar guerreiros fortes e capazes. As espadas pesadas permitem que eles ceifem infantaria e cavalaria igualmente.",
    16722: "Uma evolução dos sistemas bancários anteriores, as notas promissórias em papel permitem que nossos cidadãos negociem bens e serviços com rapidez e segurança a grandes distâncias sem serem sobrecarregados por moeda metálica pesada.",
    20216: "Esta cultura favorece um arco mais poderoso do que a maioria conseguiria puxar, e praticar com ele uma vez por semana é tão importante quanto qualquer ritual de culto, das aldeias mais rurais às maiores metrópoles urbanas.",
    16576: "O povo do norte tem uma longa tradição de comércio, mas também de pirataria. Poucos estão preparados para lutar contra uma ameaça que aparece sem aviso nas margens de um rio, e os guerreiros vikings são mestres nesse tipo de guerra.",
    16755: "Com um simples pedaço de magnetita, torna-se possível saber sempre em que direção uma pessoa está viajando. Com o tempo e mais desenvolvimento, talvez se torne possível se orientar sem nunca precisar parar e pedir informações a alguém.",
    16684: "Negociações particularmente grandes ou caras frequentemente requerem uma enorme quantidade de moedas pesadas. Depositar essa riqueza em tesourarias seguras e transferir a propriedade sem mover as moedas físicas permitirá que grandes quantidades de bens sejam negociadas com mais facilidade.",
    16772: "Os registros precisam acompanhar a crescente complexidade das finanças e da economia. Registrar todos os pagamentos de entrada e saída separadamente ajuda a manter balanços confiáveis e a garantir que as entradas não sejam ignoradas por acidente — ou \"por acidente\" — ao revisar as contas.",
    10476: "O templo sagrado da poderosa Atena ergue-se orgulhosamente mais uma vez, incontaminado por superstições tolas.\nEstas majestosas colunas e muralhas, erguidas há mais de 1000 anos, resistiram à prova do tempo e ainda protegem do alto os lares dos fiéis, um símbolo silencioso da bênção da Deusa Virgem que nomeou a cidade.",
    16676: "A terra é a recompensa mais valiosa que podemos conceder aos nossos súditos, mas há um limite para o que possuímos diretamente. No entanto, se lhes dermos permissão para começar a cultivar aquela terra improdutiva ali... quem se oporia? Mesmo que isso resulte em conflito, estaremos claramente com a razão ao defender nossos súditos!",
    10306: "Embora Pagan tenha milhares de pagodes, templos e santuários espalhados pela cidade e pelas planícies circundantes, o Templo de Ananda se destaca como uma obra-prima entre eles. A estrutura, de proporções perfeitas, contém muitas placas e imagens de pedra únicas. É um lugar de culto e um centro de aprendizado para educar o povo nos valores religiosos.",
    10540: "A influência bizantina sobre este edifício é forte: uma pequena igreja com batistério, de tijolos vermelhos aninhada sobre rochas nuas, com planta em \"cruz inscrita\" e uma rica decoração de ciclos de afrescos, dominando o vale do riacho Stilaro.\nA igreja é uma joia isolada no meio de uma natureza hostil, onde o homem pode sentir-se mais próximo de Deus.",
    16731: "Esses grandes instrumentos murais podem ser usados para ler e medir com precisão a posição de objetos celestiais.",
    2215: "Refere-se à elegância e ao refinamento cortesãos, um ideal de beleza e sensibilidade central na cultura aristocrática japonesa.",
    16871: "Hábil no uso da espada khanda, nossa valiosa infantaria é apta tanto a lutar na selva quanto a derrubar a cavalaria leve inimiga.",
    16655: "Uma maravilha da engenharia, o trabuco de contrapeso pode lançar projéteis pesados a distâncias maiores do que qualquer máquina de cerco que o precedeu.",
    16712: "É nosso direito divino possuir nossas terras de jure, e é hora de o mundo reconhecer esse fato. Não seremos mais impedidos por protocolos e decoro obsoletos.",
    16857: "Totalmente envolta em armaduras protetoras de ferro, nossa intimidadora cavalaria catafrata pode romper quase qualquer linha defensiva atacando diretamente com suas lanças.",
    16578: "Nossos drakkars são embarcações marítimas formidáveis que podem viajar para quase qualquer lugar e até mesmo enfrentar o mau tempo, dando-nos uma mobilidade excepcional no mar.",
}

HELD_SEGMENT_IDS = []


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def reports_dir() -> Path:
    path = db.project_path(db.load_settings()["reports_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fetch_context(conn, segment_id: int):
    return conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            o.portuguese_text AS current_output_text,
            c.confirmed_text,
            c.locked,
            state.state_group,
            state.final_state,
            state.needs_output_apply,
            state.confirmed_matches_output
        FROM source_segments s
        LEFT JOIN output_segments o ON o.segment_id = s.id
        LEFT JOIN segment_confirmations c ON c.segment_id = s.id
        LEFT JOIN segment_state_items state
          ON state.segment_id = s.id
         AND state.run_id = ?
        WHERE s.id = ?
        """,
        (SEGMENT_STATE_RUN_ID, segment_id),
    ).fetchone()


def fetch_snapshot(conn, segment_ids: list[int]) -> dict[str, Any]:
    placeholders = ",".join("?" for _ in segment_ids)
    return {
        "local_learning_candidates": [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM local_learning_candidates WHERE segment_id IN ({placeholders}) ORDER BY segment_id, id",
                tuple(segment_ids),
            )
        ],
        "segment_confirmations": [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM segment_confirmations WHERE segment_id IN ({placeholders}) ORDER BY segment_id, id",
                tuple(segment_ids),
            )
        ],
        "segment_state_items": [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM segment_state_items WHERE run_id = ? AND segment_id IN ({placeholders}) ORDER BY segment_id",
                (SEGMENT_STATE_RUN_ID, *segment_ids),
            )
        ],
    }


def existing_candidate(conn, segment_id: int, suggested_hash: str):
    return conn.execute(
        """
        SELECT id
        FROM local_learning_candidates
        WHERE segment_id = ?
          AND suggested_hash = ?
          AND queue_source = ?
          AND origin = ?
        ORDER BY id ASC
        LIMIT 1
        """,
        (segment_id, suggested_hash, QUEUE_SOURCE, ORIGIN),
    ).fetchone()


def create_run(conn, timestamp: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO local_learning_runs (
            mode, limit_count, auto_confidence_threshold, candidate_count,
            high_confidence_count, pending_human_count, status, notes,
            started_at, finished_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            QUEUE_SOURCE,
            0,
            1.0,
            0,
            0,
            0,
            "completed",
            f"{RULE_VERSION}; no_source_output_write",
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    return int(cursor.lastrowid)


def insert_candidate(conn, run_id: int, context, suggested: str, match_type: str, source_language: str, reason: str, timestamp: str) -> int:
    reasons = [
        RULE_VERSION,
        "domain_policy_vote_candidate_human_packet",
        "human_confirmed_in_chat",
        "no_source_write",
        "no_output_write",
    ]
    if match_type == MATCH_TYPE_CORRECTION:
        reasons.append("requires_protected_output_apply")
    cursor = conn.execute(
        """
        INSERT INTO local_learning_candidates (
            run_id, feedback_id, suggestion_id, segment_id, relative_path,
            source_key, source_line_number, english_text, spanish_text, old_text,
            current_output_text, suggested_text, suggested_hash, source_language,
            origin, match_type, match_score, token_status, suggestion_status,
            local_confidence_score, local_status, human_label, corrected_text,
            reason, reviewer, reviewed_at, reasons_json, created_at, updated_at,
            queue_source, focus_group
        )
        VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            int(context["segment_id"]),
            context["relative_path"],
            context["source_key"],
            context["source_line_number"],
            context["english_text"],
            context["spanish_text"],
            context["old_text"],
            context["current_output_text"],
            suggested,
            sha256_text(suggested),
            source_language,
            ORIGIN,
            match_type,
            1.0,
            "ok",
            "safe",
            1.0,
            "high_confidence",
            "correct",
            reason,
            REVIEWER,
            timestamp,
            json.dumps(reasons, ensure_ascii=True),
            timestamp,
            timestamp,
            QUEUE_SOURCE,
            FOCUS_GROUP,
        ),
    )
    return int(cursor.lastrowid)


def validate_context(context, suggested: str, is_correction: bool) -> list[str]:
    if context is None:
        return ["missing_segment"]
    reasons: list[str] = []
    if context["state_group"] != "pending" or context["final_state"] != "reopen_auto_confirmed_autofix":
        reasons.append("unexpected_segment_state")
    if int(context["needs_output_apply"] or 0) != 0:
        reasons.append("needs_output_apply_not_zero")
    if int(context["confirmed_matches_output"] or 0) != 1:
        reasons.append("confirmed_matches_output_not_one")
    if int(context["locked"] or 0) == 1:
        reasons.append("confirmation_already_locked")
    current = str(context["current_output_text"] or "")
    if not current:
        reasons.append("missing_current_output_text")
    if is_correction and current == suggested:
        reasons.append("correction_already_matches_output")
    if not is_correction and current != suggested:
        reasons.append("already_ok_text_mismatch")
    return reasons


def write_outputs(records: list[dict[str, Any]], summary: dict[str, Any], before_snapshot: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    base = reports_dir() / f"{stamp()}_domain_policy_vote_candidate_human_packet_learning_ingest"
    txt_path = base.with_suffix(".txt")
    jsonl_path = base.with_suffix(".jsonl")
    summary_path = reports_dir() / f"{base.name}_summary.json"
    snapshot_path = reports_dir() / f"{base.name}_before_snapshot.json"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    snapshot_path.write_text(json.dumps(before_snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "domain policy vote candidate human packet learning ingest",
        f"rule_version={RULE_VERSION}",
        f"run_id={summary['run_id']}",
        f"inserted_count={summary['inserted_count']}",
        f"inserted_already_ok_count={summary['inserted_already_ok_count']}",
        f"inserted_correction_count={summary['inserted_correction_count']}",
        f"blocked_count={summary['blocked_count']}",
        f"skipped_existing_count={summary['skipped_existing_count']}",
        f"held_count={len(HELD_SEGMENT_IDS)}",
        "source_changed=false",
        "output_changed=false",
        "production_full_recommended_now=false",
        f"next_action={summary['next_action']}",
    ]
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_path, jsonl_path, summary_path, snapshot_path


def main() -> None:
    segment_ids = sorted(set(APPROVED_ALREADY_OK_SEGMENT_IDS) | set(APPROVED_CORRECTIONS) | set(HELD_SEGMENT_IDS))
    timestamp = now()
    records: list[dict[str, Any]] = []
    inserted = 0
    inserted_ok = 0
    inserted_corrections = 0
    skipped = 0
    blocked = 0
    with db.connect(db.load_settings()) as conn:
        db.ensure_database(conn)
        before_snapshot = fetch_snapshot(conn, segment_ids)
        run_id = create_run(conn, timestamp)
        for segment_id in APPROVED_ALREADY_OK_SEGMENT_IDS:
            context = fetch_context(conn, segment_id)
            suggested = str(context["current_output_text"] if context else "")
            reasons = validate_context(context, suggested, False)
            candidate_id = None
            status = "blocked" if reasons else "inserted"
            if not reasons:
                existing = existing_candidate(conn, segment_id, sha256_text(suggested))
                if existing:
                    status = "skipped_existing"
                    skipped += 1
                else:
                    candidate_id = insert_candidate(
                        conn,
                        run_id,
                        context,
                        suggested,
                        MATCH_TYPE_ALREADY_OK,
                        "human_already_ok",
                        "human-approved manual semantic triage already-ok signal",
                        timestamp,
                    )
                    inserted += 1
                    inserted_ok += 1
            else:
                blocked += 1
            records.append({"segment_id": segment_id, "status": status, "kind": "already_ok", "candidate_id": candidate_id, "block_reasons": reasons})
        for segment_id, corrected in APPROVED_CORRECTIONS.items():
            context = fetch_context(conn, segment_id)
            reasons = validate_context(context, corrected, True)
            candidate_id = None
            status = "blocked" if reasons else "inserted"
            if not reasons:
                existing = existing_candidate(conn, segment_id, sha256_text(corrected))
                if existing:
                    status = "skipped_existing"
                    skipped += 1
                else:
                    candidate_id = insert_candidate(
                        conn,
                        run_id,
                        context,
                        corrected,
                        MATCH_TYPE_CORRECTION,
                        "human_correction",
                        "human-approved manual semantic triage correction",
                        timestamp,
                    )
                    inserted += 1
                    inserted_corrections += 1
            else:
                blocked += 1
            records.append({"segment_id": segment_id, "status": status, "kind": "correction", "candidate_id": candidate_id, "block_reasons": reasons})
        for segment_id in HELD_SEGMENT_IDS:
            records.append({"segment_id": segment_id, "status": "held_context", "kind": "hold", "candidate_id": None, "block_reasons": ["human_hold_context"]})
        conn.execute(
            """
            UPDATE local_learning_runs
            SET limit_count = ?,
                candidate_count = ?,
                high_confidence_count = ?,
                notes = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                inserted + skipped + blocked,
                inserted,
                inserted,
                f"{RULE_VERSION}; skipped_existing={skipped}; blocked={blocked}; held={len(HELD_SEGMENT_IDS)}; no_source_output_write",
                timestamp,
                run_id,
            ),
        )
        conn.commit()
    summary = {
        "schema_version": 1,
        "rule_version": RULE_VERSION,
        "generated_at": timestamp,
        "run_id": run_id,
        "segment_state_run_id": SEGMENT_STATE_RUN_ID,
        "inserted_count": inserted,
        "inserted_already_ok_count": inserted_ok,
        "inserted_correction_count": inserted_corrections,
        "blocked_count": blocked,
        "skipped_existing_count": skipped,
        "held_segment_ids": HELD_SEGMENT_IDS,
        "approved_already_ok_segment_ids": APPROVED_ALREADY_OK_SEGMENT_IDS,
        "approved_correction_segment_ids": sorted(APPROVED_CORRECTIONS),
        "source_changed": False,
        "output_changed": False,
        "production_full_recommended_now": False,
        "records": records,
        "next_action": "learn_feedback_then_correction_diff_preview",
    }
    txt_path, jsonl_path, summary_path, snapshot_path = write_outputs(records, summary, before_snapshot)
    print(f"txt={txt_path}")
    print(f"jsonl={jsonl_path}")
    print(f"summary={summary_path}")
    print(f"before_snapshot={snapshot_path}")
    print(f"run_id={run_id}")
    print(f"inserted_count={inserted}")
    print(f"inserted_already_ok_count={inserted_ok}")
    print(f"inserted_correction_count={inserted_corrections}")
    print(f"blocked_count={blocked}")
    print(f"skipped_existing_count={skipped}")
    print(f"held_count={len(HELD_SEGMENT_IDS)}")
    print("source_changed=false")
    print("output_changed=false")
    print("production_full_recommended_now=false")


if __name__ == "__main__":
    main()
