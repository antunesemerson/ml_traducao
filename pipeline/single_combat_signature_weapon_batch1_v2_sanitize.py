from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


DB_PATH = Path("memory/translation_engine.sqlite")
DIAGNOSTIC_JSONL = Path(
    "reports/20260617_164558_196604_single_combat_signature_weapon_composer_diagnostic_run_177.jsonl"
)
TARGET_PATH = "single_combat_events_l_spanish.yml"
TARGET_PREFIX = "single_combat.0031"
TOKEN_RE = re.compile(r"\[[^\]]+\]")
QUESTION_MARK_INSIDE_WORD_RE = re.compile(r"(?<=[A-Za-zÀ-ÿ])\?+(?=[A-Za-zÀ-ÿ])")


def token_counter(text: str | None) -> Counter[str]:
    return Counter(TOKEN_RE.findall(text or ""))


def fetch_metadata() -> dict[int, dict[str, Any]]:
    metadata: dict[int, dict[str, Any]] = {}
    for line in DIAGNOSTIC_JSONL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("relative_path") == TARGET_PATH and row.get("source_key_prefix") == TARGET_PREFIX:
            metadata[int(row["segment_id"])] = row
    return metadata


def output_texts() -> dict[int, str]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT segment_id, portuguese_text
        FROM output_segments
        WHERE segment_id IN (
            SELECT segment_id
            FROM source_segments
            WHERE relative_path = ?
              AND source_key LIKE ?
        )
        """,
        (TARGET_PATH, f"{TARGET_PREFIX}%"),
    ).fetchall()
    return {int(row["segment_id"]): str(row["portuguese_text"] or "") for row in rows}


def base_row(metadata: dict[int, dict[str, Any]], segment_id: int, decision: str) -> dict[str, Any]:
    item = metadata[segment_id]
    return {
        "segment_id": segment_id,
        "relative_path": item["relative_path"],
        "source_key": item["source_key"],
        "decision": decision,
        "subpattern": item["subpattern"],
        "tokens_preserved": True,
    }


def build_rows(metadata: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    proposed_delta: dict[int, str] = {
        246005: (
            "abrindo cortes sangrentos em [sc_loser.Custom('ArmArms')] e na parte superior do peito, "
            "extraindo meu sangue como se fosse \u00e1gua de po\u00e7o.\n\n"
            "Fica claro que sou um brinquedo para [sc_victor.GetHerHim] quando [sc_victor.GetSheHe] termina de brincar.\n\n"
            "Esgotad[sc_loser.Custom('ES_OA')], morrendo, caio de joelhos. Uma po\u00e7a do meu pr\u00f3prio sangue "
            "me aquece enquanto me esfor\u00e7o para olhar para meu assassin[sc_victor.Custom('ES_OA')].\n\n"
            "[sc_victor.GetFirstNameNoTooltip] retribui meu olhar com desinteresse vazio. "
            "[sc_victor.GetHerHis|U] [sc_victor.Custom('signature_weapon')] come\u00e7a a descer terrivelmente."
        ),
        245996: (
            "S\u00f3 quando avan\u00e7o demais percebo o que [sc_victor.GetSheHe] est\u00e1 fazendo. "
            "Minha [sc_loser.Custom('signature_weapon')] foi lan\u00e7ada longe demais \u00e0 minha frente; "
            "[sc_victor.GetFirstNameNoTooltip] gira [sc_victor.GetHerHis] [sc_victor.Custom('signature_weapon')] "
            "de volta, e uma dor incandescente explode no meu flanco.\n\n"
            "Desabo, ofegando e tossindo, engolindo golfadas de ar estranguladas. \u00c9 tudo in\u00fatil.\n\n"
            "[sc_victor.GetFirstNameNoTooltip] [sc_victor.Custom('SignatureWeaponActionThirdPerson')] "
            "[sc_victor.GetHerHis] [sc_victor.Custom('signature_weapon')] para o golpe fatal."
        ),
        245951: (
            "[sc_victor.GetSheHe|U] traz [sc_victor.GetHerHis] [sc_victor.Custom('signature_weapon')] para #EMP dentro#! "
            "da minha guarda, bem debaixo da minha garganta.\n\n"
            "Mexo minha [sc_loser.Custom('signature_weapon')] por reflexo, ponderando minhas op\u00e7\u00f5es em um sil\u00eancio constrangedor.\n\n"
            "[sc_victor.GetFirstNameNoTooltip] fecha a cara para mim, e seus m\u00fasculos se tensionam, prontos para atacar."
        ),
        245931: (
            "que me atinge bem nos n\u00f3s dos dedos da m\u00e3o em que seguro [sc_loser.Custom('signature_weapon')].\n\n"
            "Por reflexo, meus dedos se abrem, deixando minha arma cair no ch\u00e3o com estrondo.\n\n"
            "N\u00f3s nos encaramos #EMP incr\u00e9dulos#! por alguns segundos, at\u00e9 que "
            "[sc_victor.GetFirstNameNoTooltip] ergue [sc_victor.GetHerHis] [sc_victor.Custom('signature_weapon')] com hesita\u00e7\u00e3o.\n\n"
            "\"R-renda-se ou morra!\"."
        ),
        246100: (
            "[sc_victor.GetFirstNameNoTooltip] n\u00e3o me d\u00e1 tempo algum para me recompor; "
            "[sc_victor.GetHerHis] [sc_victor.Custom('signature_weapon')] "
            "[sc_victor.Custom('SignatureWeaponKillTypeTorso2PresentParticiple')] com um "
            "[sc_victor.Custom('SignatureWeaponAttemptedLight')]. Sangue irrompe pela minha garganta e pela minha boca, "
            "respingando de volta na minha [sc_loser.Custom('MaskFace')] numa mancha pegajosa.\n\n"
            "Em algum lugar sob camadas e camadas de dor, sinto meu cora\u00e7\u00e3o ainda batendo, ainda bombeando sangue "
            "por veias rasgadas e para fora de art\u00e9rias despeda\u00e7adas. N\u00e3o h\u00e1 volta depois disso."
        ),
        246125: (
            "me acerte na [sc_loser.Custom('MaskFace')] com o [sc_victor.Custom('SignatureWeaponEndType')] de "
            "[sc_victor.GetHerHis] [sc_victor.Custom('signature_weapon')].\n\n"
            "Cambaleio para tr\u00e1s, rosnando em f\u00faria cega diante da blasf\u00eamia, mas minha raiva tamb\u00e9m "
            "\u00e9 cortada de s\u00fabito quando [sc_victor.GetFirstNameNoTooltip] "
            "[sc_victor.Custom('SignatureWeaponWoundVerb5ThirdPersonPresent')] enfia sua arma em meu pulm\u00e3o. "
            "Eu me engasgo, balbuciando, mal conseguindo emitir um som enquanto o sangue come\u00e7a a encher minha cavidade tor\u00e1cica."
        ),
    }

    for segment_id, proposed in proposed_delta.items():
        row = base_row(metadata, segment_id, "needs_token_delta_review")
        row.update(
            {
                "corrected_text": "",
                "proposed_text_with_token_delta": proposed,
                "review_notes": (
                    "proposta adiciona/restaura tokens do ingl\u00eas ausentes no output/espanhol; "
                    "exige pol\u00edtica de token-delta antes de apply"
                ),
                "tokens_preserved": False,
                "token_delta_review_required": True,
                "requires_apply_later": False,
            }
        )
        rows.append(row)

    corrected: dict[int, tuple[str, str]] = {
        245948: (
            "minha guarda se rompe de pura frustra\u00e7\u00e3o.\n\n"
            "Nesse momento #EMP exato#!, [sc_victor.GetSheHe] ataca: "
            "[sc_victor.Custom('SignatureWeaponAttemptedLight')] arranca [sc_loser.Custom('signature_weapon')] "
            "da minha m\u00e3o com um floreio treinado.\n\n"
            "\"N\u00e3o se preocupe, [sc_victor.GetFirstNameNoTooltip]; voc\u00ea ainda segura melhor "
            "[sc_loser.Custom('signature_weapon')] do que seu [sc_loser.GetPrimaryTitle.GetTierAsNameNoTooltip]\".",
            "corre\u00e7\u00e3o revisada; tokens preservados exatamente em rela\u00e7\u00e3o ao output atual",
        ),
        246103: (
            "At\u00e9 que uma aparada inesperada rebate contra meu [sc_loser.Custom('MaskFace')], "
            "[sc_loser.Custom('SignatureWeaponWoundVerb6PresentParticiple')] um ferimento at\u00e9 o osso e me fazendo "
            "cambalear para tr\u00e1s. Solto um grito de agonia surpresa, afastando-me do choque das armas, quando meus "
            "p\u00e9s s\u00e3o varridos debaixo de mim.\n\n"
            "Tenho tempo apenas para gritar um protesto antes que [sc_victor.GetSheHe] "
            "[sc_victor.Custom('SignatureWeaponKillTypeGroinThirdPersonActive')], "
            "[sc_victor.Custom('SignatureWeaponKillTypeGroinThirdPersonActive_Aftermath')].\n\n"
            "A morte \u00e9 um al\u00edvio.",
            "corre\u00e7\u00e3o revisada; tokens preservados exatamente em rela\u00e7\u00e3o ao output atual",
        ),
        246110: (
            "avan\u00e7a e, com bastante calma, [sc_victor.Custom('SignatureWeaponWoundVerb4ThirdPersonPresent')] "
            "direto a minha garganta com um \u00fanico movimento r\u00e1pido.\n\n"
            "O fim \u00e9 quase\u2026 anticlim\u00e1tico. N\u00e3o h\u00e1 floreios, nem gritos, nem sinais de que o sobrenatural "
            "venha me buscar.\n\n"
            "Apenas dor latejante, meus pr\u00f3prios gorgolejos \u00famidos e os [my_foe.Custom('EyeEyes')] impass\u00edveis "
            "de [sc_victor.GetFirstNameNoTooltip] me vendo morrer.",
            "corre\u00e7\u00e3o revisada; tokens preservados exatamente em rela\u00e7\u00e3o ao output atual",
        ),
        246111: (
            "lan\u00e7o uma finta astuta, girando as costas para [sc_victor.GetFirstNameNoTooltip] enquanto tento aparar "
            "na dire\u00e7\u00e3o errada.\n\n"
            "Menos de um segundo depois, a [sc_victor.Custom('signature_weapon')] do meu inimigo "
            "[sc_victor.Custom('SignatureWeaponKillTypeRearTorsoThirdPersonActive')] com um "
            "[sc_victor.Custom('SignatureWeaponAttemptedHeavy')]. Desabo, dobrado de forma errada sobre minhas pernas, "
            "com a agonia reverberando por qualquer peda\u00e7o de carne esventrada e osso que ainda consiga se ligar aos "
            "meus nervos destru\u00eddos.",
            "corre\u00e7\u00e3o revisada; tokens preservados exatamente em rela\u00e7\u00e3o ao output atual",
        ),
        246093: (
            "A distra\u00e7\u00e3o permite que a [sc_victor.Custom('signature_weapon')] de [sc_victor.GetFirstNameNoTooltip] "
            "atinja meu torso, [sc_victor.Custom('SignatureWeaponKillTypeTorsoPresentParticiple')] com efici\u00eancia brutal.\n\n"
            "Meu corpo explode em agonia, e eu cambaleio para tr\u00e1s. Q-qu\u00ea? Como isso aconteceu? Eu...\n\n"
            "O sangue jorra do meu peito em arcos quentes e \u00famidos. Gorgolejo algo inintelig\u00edvel atrav\u00e9s dos "
            "pulm\u00f5es arruinados; nem #EMP eu#! sei ao certo o qu\u00ea. O golpe de miseric\u00f3rdia demora demais a chegar.",
            "corre\u00e7\u00e3o revisada; tokens preservados exatamente em rela\u00e7\u00e3o ao output atual",
        ),
    }

    for segment_id, (text, notes) in corrected.items():
        row = base_row(metadata, segment_id, "corrected")
        row.update(
            {
                "corrected_text": text,
                "review_notes": notes,
                "tokens_preserved": True,
                "token_delta_review_required": False,
                "requires_apply_later": True,
            }
        )
        rows.append(row)

    row = base_row(metadata, 246047, "already_good")
    row.update(
        {
            "corrected_text": "",
            "review_notes": (
                "texto atual est\u00e1 natural em PT-BR e preserva SignatureWeaponAttemptedLightPlural; "
                "reopen parece falso para este lote"
            ),
            "token_delta_review_required": False,
            "requires_apply_later": False,
        }
    )
    rows.append(row)

    for segment_id, notes in {
        246106: (
            "a frase depende do valor renderizado de SignatureWeaponAttemptedHeavy e "
            "SignatureWeaponKillTypeHead2ThirdPersonActive; artigo/adjetivo podem quebrar g\u00eanero ou categoria do helper"
        ),
        246067: (
            "SignatureWeaponEndType e SignatureWeaponKillTypeHeadFirstPersonFuture exigem saber se o helper renderiza "
            "substantivo, infinitivo ou frase futura; corre\u00e7\u00e3o segura depende desse contexto"
        ),
    }.items():
        row = base_row(metadata, segment_id, "needs_context")
        row.update(
            {
                "corrected_text": "",
                "review_notes": notes,
                "token_delta_review_required": False,
                "requires_apply_later": False,
            }
        )
        rows.append(row)

    return sorted(rows, key=lambda row: (row["source_key"], row["segment_id"]))


def validate_rows(rows: list[dict[str, Any]], outputs: dict[int, str]) -> tuple[list[str], list[int]]:
    errors: list[str] = []
    corrected_token_mismatch: list[int] = []
    expected_ids = {246005, 245996, 245951, 245948, 245931, 246103, 246047, 246110, 246111, 246093, 246100, 246106, 246067, 246125}
    actual_ids = {int(row["segment_id"]) for row in rows}
    if actual_ids != expected_ids:
        errors.append(f"id set mismatch: expected={sorted(expected_ids)} actual={sorted(actual_ids)}")

    for row in rows:
        if row["decision"] == "corrected":
            segment_id = int(row["segment_id"])
            if token_counter(row.get("corrected_text")) != token_counter(outputs.get(segment_id)):
                row["tokens_preserved"] = False
                corrected_token_mismatch.append(segment_id)
        if row["decision"] == "needs_token_delta_review":
            if not row.get("token_delta_review_required"):
                errors.append(f"missing token_delta_review_required for {row['segment_id']}")
            if row.get("requires_apply_later"):
                errors.append(f"token delta row marked apply later for {row['segment_id']}")
    return errors, corrected_token_mismatch


def write_artifacts(rows: list[dict[str, Any]], errors: list[str], mismatches: list[int], output_dir: Path) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    jsonl_path = output_dir / f"{stamp}_single_combat_signature_weapon_composer_batch1_decisions_reviewed_chat_v2.jsonl"
    txt_path = output_dir / f"{stamp}_single_combat_signature_weapon_composer_batch1_decisions_reviewed_chat_v2.txt"

    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    text = jsonl_path.read_text(encoding="utf-8")
    encoding_bad_markers = [marker for marker in ("\ufffd", "\u00c3\u0192", "\u00c3\u201a") if marker in text]
    total_question_marks = text.count("?")
    suspicious_question_marks = len(QUESTION_MARK_INSIDE_WORD_RE.findall(text))
    counts = Counter(row["decision"] for row in rows)
    ids_by_decision: dict[str, list[int]] = {}
    for decision in sorted(counts):
        ids_by_decision[decision] = [int(row["segment_id"]) for row in rows if row["decision"] == decision]

    lines = [
        "Single combat signature weapon composer batch 1 review v2",
        "Scope: relative_path=single_combat_events_l_spanish.yml; source_key_prefix=single_combat.0031",
        "Read-only sanitation: no apply, no confirmations, no source/output writes",
        "",
        f"Total revisado: {len(rows)}",
        "Contagem por decisão:",
    ]
    for decision, count in counts.most_common():
        lines.append(f"- {decision}: {count}")

    lines.extend(
        [
            "",
            f"IDs corrected aplicáveis: {ids_by_decision.get('corrected', [])}",
            f"IDs already_good: {ids_by_decision.get('already_good', [])}",
            f"IDs needs_context: {ids_by_decision.get('needs_context', [])}",
            f"IDs needs_token_delta_review: {ids_by_decision.get('needs_token_delta_review', [])}",
            "",
            "Validação de encoding:",
            f"- bad marker count: {len(encoding_bad_markers)}",
            f"- question marks no JSONL: {total_question_marks}",
            f"- suspicious question marks inside words: {suspicious_question_marks}",
            "- termos acentuados escritos em UTF-8 real pelo Python",
            "",
            "Validação de token preservation para corrected:",
            f"- corrected token mismatches: {mismatches or 'nenhum'}",
            f"- validation errors: {errors or 'nenhum'}",
            "",
            "Aviso: nenhum apply executado.",
            "",
            f"JSONL: {jsonl_path}",
            f"TXT: {txt_path}",
        ]
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl_path, txt_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="reports")
    args = parser.parse_args()

    metadata = fetch_metadata()
    outputs = output_texts()
    rows = build_rows(metadata)
    errors, mismatches = validate_rows(rows, outputs)
    jsonl_path, txt_path = write_artifacts(rows, errors, mismatches, Path(args.output_dir))

    print(f"jsonl={jsonl_path}")
    print(f"txt={txt_path}")
    print(f"rows={len(rows)}")
    print(f"errors={errors}")
    print(f"corrected_token_mismatches={mismatches}")
    return 1 if errors or mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
