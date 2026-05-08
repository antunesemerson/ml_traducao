from __future__ import annotations

import argparse
import re
import unicodedata
from collections import Counter
from datetime import datetime

import db
import local_quality_validator
from auto_validate_segments import upsert_auto_confirmation


RULE_VERSION = "review_name_disagreements_v1"
ENCODED_NAME_SUFFIX_RE = re.compile(r"_[0-9A-Fa-f]{4,}$")
COMMON_NAME_TRANSLATIONS = {
    "ah",
    "ai",
    "altocumulo",
    "acasalamento",
    "autorizado",
    "barragem muculmana de kusoy",
    "coceira prolongada",
    "corporacao artistica",
    "couve galega",
    "de",
    "do doc",
    "douramento",
    "e",
    "ei",
    "em",
    "emprestimo",
    "embarcacao",
    "enfardamento",
    "eu",
    "fa",
    "fazer documento",
    "felicidade",
    "india pendurada",
    "ir",
    "ja",
    "lingua hang",
    "lituanizar",
    "manjericao",
    "matematica",
    "mensageiro",
    "na interface",
    "oi",
    "olho benyepo",
    "pa",
    "pendurado muda",
    "perfurador",
    "patrocinio",
    "pecado original",
    "polen solar",
    "presa longa",
    "reabastecer",
    "represa han kuz wanku",
    "sa",
    "se",
    "seyum germanica",
    "sincronizar",
    "semelhante",
    "satisfazendo",
    "tesouraria",
    "tesoureiro",
    "tu",
    "um",
    "viet longo",
    "xa",
}


def normalize(value: str | None) -> str:
    return local_quality_validator.normalize(value)


def repair_mojibake(value: str) -> str:
    try:
        return value.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return value


def ascii_key(value: str | None) -> str:
    repaired = repair_mojibake(str(value or ""))
    decomposed = unicodedata.normalize("NFD", repaired.casefold())
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return " ".join("".join(ch if ch.isalnum() else " " for ch in stripped).split())


def is_dynasty_key(source_key: str) -> bool:
    return source_key.startswith("dynn_")


def is_character_name_path(relative_path: str) -> bool:
    return relative_path.startswith("names/")


def has_encoded_name_suffix(source_key: str) -> bool:
    return bool(ENCODED_NAME_SUFFIX_RE.search(source_key))


def words(value: str | None) -> int:
    return local_quality_validator.word_count(str(value or ""))


def compact_name(value: str | None) -> str:
    text = normalize(value)
    return "".join(ch for ch in text if ch.isalnum())


def equivalence_status(conn, source_name: str, portuguese_name: str) -> str | None:
    existing = conn.execute(
        """
        SELECT status
        FROM name_equivalences
        WHERE source_name = ?
          AND portuguese_name = ?
          AND source_kind = 'character_name'
        """,
        (source_name.strip(), portuguese_name.strip()),
    ).fetchone()
    if not existing:
        return None
    return str(existing["status"] or "")


def classify(row: dict, conn=None) -> tuple[str, str | None]:
    english_text = str(row["english_text"] or "")
    spanish_text = str(row["spanish_text"] or "")
    old_text = str(row["old_text"] or "")
    relative_path = str(row["relative_path"] or "")
    source_key = str(row["source_key"] or "")

    english_norm = normalize(english_text)
    spanish_norm = normalize(spanish_text)
    old_norm = normalize(old_text)

    if english_norm != spanish_norm:
        return "needs_human_language_choice", None

    if (
        is_dynasty_key(source_key)
        and english_norm.endswith("id")
        and old_norm == f"{english_norm}a"
    ):
        return "candidate_restore_dynasty_id_name", english_text

    if (
        is_dynasty_key(source_key)
        and english_norm.endswith("vid")
        and old_norm == f"{english_norm}a"
    ):
        return "candidate_restore_dynasty_id_name", english_text

    if source_key.startswith("dynnp_"):
        return "needs_human_dynasty_prefix_choice", None

    if english_norm == spanish_norm and old_norm != english_norm:
        if (
            is_character_name_path(relative_path)
            and words(english_text) <= 2
            and words(old_text) > words(english_text)
        ):
            return "candidate_restore_character_name_literal_translation", english_text

        if is_character_name_path(relative_path) and has_encoded_name_suffix(source_key):
            return "candidate_restore_encoded_character_name", english_text

        if (
            is_character_name_path(relative_path)
            and compact_name(source_key) == compact_name(english_text)
        ):
            if conn is not None:
                status = equivalence_status(conn, english_text, old_text)
                if status == "rejected":
                    return "name_equivalence_rejected", None
                if status == "human_confirmed":
                    return "name_equivalence_confirmed", old_text
            if ascii_key(old_text) in COMMON_NAME_TRANSLATIONS:
                return "candidate_restore_character_name_common_word", english_text
            if words(old_text) <= words(english_text):
                return "candidate_accept_portuguese_historical_name", old_text
            return "candidate_restore_character_name_uncertain", english_text

        if (
            is_dynasty_key(source_key)
            and "-" in english_text
            and old_norm == normalize(english_text.replace("-", " "))
        ):
            return "candidate_restore_dynasty_hyphen", english_text

        if (
            is_dynasty_key(source_key)
            and words(english_text) == 1
            and words(old_text) > 1
        ):
            return "candidate_restore_dynasty_removed_scope", english_text

        if is_dynasty_key(source_key) and has_encoded_name_suffix(source_key):
            return "candidate_restore_encoded_dynasty_name", english_text

        if is_dynasty_key(source_key) and words(english_text) <= 3:
            return "source_equal_dynasty_old_differs_uncertain", None

        return "source_equal_old_differs_uncertain", None

    return "other", None


def fetch_rows(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            s.id AS segment_id,
            s.relative_path,
            s.source_key,
            s.source_line_number,
            s.english_text,
            s.spanish_text,
            s.old_text,
            a.classification,
            a.confidence_score
        FROM source_segments s
        JOIN segment_analysis a ON a.segment_id = s.id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = s.id
        WHERE s.is_active = 1
          AND sc.segment_id IS NULL
          AND a.classification = 'trusted'
          AND COALESCE(a.confidence_score, 0) >= 0.99
          AND s.has_old = 1
          AND s.old_text IS NOT NULL
          AND trim(s.old_text) != ''
          AND (
              s.relative_path LIKE 'names/%'
              OR s.relative_path LIKE 'dynasties/%'
          )
        ORDER BY s.relative_path ASC, s.id ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def main(
    limit: int = 80,
    apply_dynasty_id: bool = False,
    apply_literal_character_names: bool = False,
    apply_common_character_names: bool = False,
    apply_encoded_character_names: bool = False,
    apply_portuguese_historical_names: bool = False,
    apply_dynasty_hyphen: bool = False,
    apply_dynasty_removed_scope: bool = False,
    apply_encoded_dynasty_names: bool = False,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()

    print("[review_name_disagreements] Starting name disagreement review")
    print(f"[review_name_disagreements] Rule version: {RULE_VERSION}")
    print(f"[review_name_disagreements] Apply dynasty id restores: {apply_dynasty_id}")
    print(
        "[review_name_disagreements] "
        f"Apply literal character name restores: {apply_literal_character_names}"
    )
    print(
        "[review_name_disagreements] "
        f"Apply common character name restores: {apply_common_character_names}"
    )
    print(
        "[review_name_disagreements] "
        f"Apply encoded character name restores: {apply_encoded_character_names}"
    )
    print(
        "[review_name_disagreements] "
        f"Apply Portuguese historical names: {apply_portuguese_historical_names}"
    )
    print(f"[review_name_disagreements] Apply dynasty hyphen restores: {apply_dynasty_hyphen}")
    print(
        "[review_name_disagreements] "
        f"Apply dynasty removed-scope restores: {apply_dynasty_removed_scope}"
    )
    print(
        "[review_name_disagreements] "
        f"Apply encoded dynasty name restores: {apply_encoded_dynasty_names}"
    )
    print(f"[review_name_disagreements] Database: {db.get_database_path(settings)}")

    buckets: dict[str, list[tuple[dict, str | None]]] = {}
    counts: Counter[str] = Counter()
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        rows = fetch_rows(conn)
        for row in rows:
            bucket, proposed_text = classify(row, conn=conn)
            counts[bucket] += 1
            buckets.setdefault(bucket, []).append((row, proposed_text))

    applied = 0
    apply_buckets = []
    if apply_dynasty_id:
        apply_buckets.append(("candidate_restore_dynasty_id_name", "name_disagreement_restore_dynasty_id"))
    if apply_literal_character_names:
        apply_buckets.append(
            (
                "candidate_restore_character_name_literal_translation",
                "name_disagreement_restore_literal_character_name",
            )
        )
    if apply_common_character_names:
        apply_buckets.append(
            (
                "candidate_restore_character_name_common_word",
                "name_disagreement_restore_common_word_character_name",
            )
        )
    if apply_encoded_character_names:
        apply_buckets.append(
            (
                "candidate_restore_encoded_character_name",
                "name_disagreement_restore_encoded_character_name",
            )
        )
    if apply_portuguese_historical_names:
        apply_buckets.append(
            (
                "candidate_accept_portuguese_historical_name",
                "name_disagreement_accept_portuguese_historical_name",
            )
        )
    if apply_dynasty_hyphen:
        apply_buckets.append(("candidate_restore_dynasty_hyphen", "name_disagreement_restore_dynasty_hyphen"))
    if apply_dynasty_removed_scope:
        apply_buckets.append(
            (
                "candidate_restore_dynasty_removed_scope",
                "name_disagreement_restore_dynasty_removed_scope",
            )
        )
    if apply_encoded_dynasty_names:
        apply_buckets.append(
            (
                "candidate_restore_encoded_dynasty_name",
                "name_disagreement_restore_encoded_dynasty_name",
            )
        )

    if apply_buckets:
        with db.connect(settings) as conn:
            db.ensure_database(conn)
            for bucket_name, source in apply_buckets:
                for row, proposed_text in buckets.get(bucket_name, []):
                    if not proposed_text:
                        continue
                    item = {
                        "segment_id": row["segment_id"],
                        "candidate_text": proposed_text,
                        "candidate_source": source,
                        "feedback_id": None,
                    }
                    upsert_auto_confirmation(conn, item, 0.993)
                    applied += 1
            conn.commit()

    elapsed = datetime.now() - started_at
    report_lines = [
        "Name disagreement review report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Apply dynasty id restores: {apply_dynasty_id}",
        f"Apply literal character name restores: {apply_literal_character_names}",
        f"Apply common character name restores: {apply_common_character_names}",
        f"Apply encoded character name restores: {apply_encoded_character_names}",
        f"Apply Portuguese historical names: {apply_portuguese_historical_names}",
        f"Apply dynasty hyphen restores: {apply_dynasty_hyphen}",
        f"Apply dynasty removed-scope restores: {apply_dynasty_removed_scope}",
        f"Apply encoded dynasty name restores: {apply_encoded_dynasty_names}",
        "",
        "Summary:",
        f"- Rows inspected: {len(rows)}",
        f"- Applied safe restores: {applied}",
        *[f"- {bucket}: {total}" for bucket, total in counts.most_common()],
        "",
    ]

    for bucket, total in counts.most_common():
        report_lines.extend(
            [
                f"Samples: {bucket}",
                "",
            ]
        )
        for row, proposed_text in buckets[bucket][:limit]:
            report_lines.extend(
                [
                    f"- segment {row['segment_id']} | {row['relative_path']}::{row['source_key']}",
                    f"  EN: {row['english_text']}",
                    f"  ES: {row['spanish_text']}",
                    f"  OLD: {row['old_text']}",
                ]
            )
            if proposed_text is not None:
                report_lines.append(f"  PROPOSED: {proposed_text}")
        if not buckets[bucket]:
            report_lines.append("- No rows")
        report_lines.append("")

    report_path = db.write_report(settings, "review_name_disagreements", report_lines)
    print(f"[review_name_disagreements] Rows inspected: {len(rows)}")
    print(f"[review_name_disagreements] Applied safe restores: {applied}")
    for bucket, total in counts.most_common():
        print(f"[review_name_disagreements] {bucket}: {total}")
    print(f"[review_name_disagreements] Report: {report_path}")
    print("[review_name_disagreements] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Report remaining trusted name/dynasty disagreements.")
    parser.add_argument("--limit", type=int, default=80, help="Maximum samples per bucket.")
    parser.add_argument(
        "--apply-dynasty-id",
        action="store_true",
        help="Auto-confirm safe dynasty names where old_text only added a final 'a' to an EN=ES *id name.",
    )
    parser.add_argument(
        "--apply-literal-character-names",
        action="store_true",
        help="Auto-confirm character names where old_text translated a proper name as a common phrase.",
    )
    parser.add_argument(
        "--apply-common-character-names",
        action="store_true",
        help="Auto-confirm character names where old_text is a known common-word translation.",
    )
    parser.add_argument(
        "--apply-encoded-character-names",
        action="store_true",
        help="Auto-confirm encoded character names where EN=ES and old_text translated the name literally.",
    )
    parser.add_argument(
        "--apply-portuguese-historical-names",
        action="store_true",
        help="Auto-confirm character names where old_text is a plausible Portuguese historical form.",
    )
    parser.add_argument(
        "--apply-dynasty-hyphen",
        action="store_true",
        help="Auto-confirm dynasty names where old_text only removed a hyphen from EN=ES.",
    )
    parser.add_argument(
        "--apply-dynasty-removed-scope",
        action="store_true",
        help="Auto-confirm dynasty names where old_text added scope/location not present in EN=ES.",
    )
    parser.add_argument(
        "--apply-encoded-dynasty-names",
        action="store_true",
        help="Auto-confirm encoded dynasty names where EN=ES and old_text changed the name.",
    )
    args = parser.parse_args()
    main(
        limit=args.limit,
        apply_dynasty_id=args.apply_dynasty_id,
        apply_literal_character_names=args.apply_literal_character_names,
        apply_common_character_names=args.apply_common_character_names,
        apply_encoded_character_names=args.apply_encoded_character_names,
        apply_portuguese_historical_names=args.apply_portuguese_historical_names,
        apply_dynasty_hyphen=args.apply_dynasty_hyphen,
        apply_dynasty_removed_scope=args.apply_dynasty_removed_scope,
        apply_encoded_dynasty_names=args.apply_encoded_dynasty_names,
    )
