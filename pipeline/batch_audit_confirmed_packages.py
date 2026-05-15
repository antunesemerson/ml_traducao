from __future__ import annotations

import argparse
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import db


RULE_VERSION = "batch_audit_confirmed_packages_v1"


@dataclass
class PackageAudit:
    relative_path: str
    total_segments: int
    human_confirmed: int
    auto_confirmed: int
    pending: int
    risk: str
    strong_hits: list[tuple[int, str, str, str]]

    @property
    def can_auto_promote(self) -> bool:
        return (
            self.pending == 0
            and self.auto_confirmed > 0
            and self.human_confirmed < self.total_segments
            and self.risk in {"low", "medium"}
            and not self.strong_hits
        )


STRONG_NEEDLES = [
    "¿",
    "¡",
    "«",
    "»",
    "voc?",
    "n?o",
    "ter?",
    "j?",
    "năo",
    "vocę",
    "Ningu?m",
    "N?o",
    "Ter?",
    "tornar?",
    "div?rcio",
    "Ofender?",
    "conex?o",
    "concess?es",
    "mádio",
    "máo",
    "personajes",
    "cautivos",
    "cortesanos",
    "situaciones",
    "decisiones",
    "rechaza",
    "rechazar",
    "Consejo",
    "Haz clic",
    "haz clic",
    "Coste",
    "coste",
    "jefa",
    "jefe",
    "te ves",
    "se ve",
    "desposado",
    "con tierras",
    "Clic para",
    "volver",
    "seleccionar",
    "almacenar",
    "puedes",
    "podéis",
    "pueden",
    "bonificación",
    "penalización",
    "éxito",
    "fracaso",
]

WORD_NEEDLES = [
    "vasall",
    "dinastía",
    "ejército",
    "ejercito",
    "señor",
    "señora",
    "heredero",
    "sucesor",
    "disponible",
    "creado",
    "creada",
]

FALSE_POSITIVE_WORDS = {
    "vassal",
    "vassals",
}

TOKEN_JOINED_PATTERN = re.compile(r"\][A-Za-zÀ-ÿ]")
CONCEPT_SPANISH_LITERAL_PATTERN = re.compile(
    r"Concept\(\s*['\"][^'\"]+['\"]\s*,\s*['\"]([^'\"]*(?:cortesanos|situaciones|decisiones|cautivos|desposado|con tierras|vasall|personajes)[^'\"]*)['\"]",
    re.IGNORECASE,
)


def classify_risk(relative_path: str) -> str:
    path = relative_path.lower()
    if (
        path.startswith("event_localization/")
        or path.startswith("religion/")
        or "religion" in path
        or path.startswith("names/")
        or path.startswith("dynasties/")
        or path.startswith("culture/")
        or "name" in path
        or path in {
            "core_l_spanish.yml",
            "decisions_l_spanish.yml",
            "important_actions_l_spanish.yml",
            "game_concepts_l_spanish.yml",
        }
    ):
        return "sensitive"
    if (
        path.startswith("modifiers/")
        or path.startswith("portraits/")
        or "message" in path
        or "modifier" in path
        or "window" in path
        or "council" in path
        or "character" in path
        or "knight" in path
        or "legacy" in path
        or "relationship" in path
        or path.startswith("dlc/bp2/")
    ):
        return "low"
    if path.startswith("dlc/"):
        return "medium"
    return "medium"


def package_rows(conn, path_like: str | None) -> list:
    params: list[str] = []
    where_path = ""
    if path_like:
        where_path = "AND s.relative_path LIKE ?"
        params.append(path_like)
    return conn.execute(
        f"""
        WITH pkg AS (
            SELECT
                s.relative_path,
                COUNT(*) AS total_segments,
                SUM(CASE WHEN c.confirmation_level IS NOT NULL THEN 1 ELSE 0 END) AS confirmed,
                SUM(CASE WHEN c.confirmation_level='human_confirmed' THEN 1 ELSE 0 END) AS human_confirmed,
                SUM(CASE WHEN c.confirmation_level='auto_confirmed' THEN 1 ELSE 0 END) AS auto_confirmed,
                COUNT(*) - SUM(CASE WHEN c.confirmation_level IS NOT NULL THEN 1 ELSE 0 END) AS pending
            FROM source_segments s
            LEFT JOIN segment_confirmations c ON c.segment_id=s.id
            WHERE s.is_active = 1
              {where_path}
            GROUP BY s.relative_path
        )
        SELECT *
        FROM pkg
        WHERE pending = 0
          AND auto_confirmed > 0
          AND human_confirmed < total_segments
        ORDER BY
            total_segments ASC,
            relative_path ASC
        """,
        params,
    ).fetchall()


def iter_package_segments(conn, relative_path: str) -> Iterable:
    return conn.execute(
        """
        SELECT
            s.id,
            s.source_key,
            s.english_text,
            COALESCE(c.confirmed_text, s.old_text, s.spanish_text, s.english_text, '') AS candidate_text
        FROM source_segments s
        LEFT JOIN segment_confirmations c ON c.segment_id=s.id
        WHERE s.relative_path = ?
          AND s.is_active = 1
        ORDER BY s.source_line_number, s.id
        """,
        (relative_path,),
    ).fetchall()


def find_strong_hits(conn, relative_path: str, max_hits: int) -> list[tuple[int, str, str, str]]:
    hits: list[tuple[int, str, str, str]] = []
    for row in iter_package_segments(conn, relative_path):
        text = row["candidate_text"] or ""
        english = row["english_text"] or ""
        reason: str | None = None
        if "?" in text and "?" not in english:
            reason = "question_or_corruption"
        if not reason:
            concept_match = CONCEPT_SPANISH_LITERAL_PATTERN.search(text)
            if concept_match:
                reason = f"spanish_concept_literal:{concept_match.group(1)[:40]}"
        if not reason:
            lower_text = text.lower()
            for needle in STRONG_NEEDLES:
                if needle.lower() in lower_text:
                    reason = needle
                    break
        if not reason:
            for needle in WORD_NEEDLES:
                if needle in FALSE_POSITIVE_WORDS:
                    continue
                if re.search(rf"\b{re.escape(needle)}\w*\b", text, re.IGNORECASE):
                    reason = needle
                    break
        if not reason and TOKEN_JOINED_PATTERN.search(text):
            reason = "missing_space_after_token"
        if reason:
            snippet = text[:360].replace("\n", "\\n")
            hits.append((int(row["id"]), row["source_key"], reason, snippet))
            if len(hits) >= max_hits:
                break
    return hits


def audit_packages(conn, path_like: str | None, limit: int | None, max_hits: int) -> list[PackageAudit]:
    audits: list[PackageAudit] = []
    rows = package_rows(conn, path_like)
    if limit is not None:
        rows = rows[:limit]
    for index, row in enumerate(rows, start=1):
        relative_path = row["relative_path"]
        print(f"[batch_audit] Auditing {index}/{len(rows)}: {relative_path}")
        risk = classify_risk(relative_path)
        hits = find_strong_hits(conn, relative_path, max_hits=max_hits)
        audits.append(
            PackageAudit(
                relative_path=relative_path,
                total_segments=int(row["total_segments"] or 0),
                human_confirmed=int(row["human_confirmed"] or 0),
                auto_confirmed=int(row["auto_confirmed"] or 0),
                pending=int(row["pending"] or 0),
                risk=risk,
                strong_hits=hits,
            )
        )
    return audits


def promote_package(conn, audit: PackageAudit) -> int:
    rows = iter_package_segments(conn, audit.relative_path)
    for row in rows:
        conn.execute(
            """
            INSERT INTO segment_confirmations (
                segment_id, confirmed_text, confirmation_level, confidence_score, locked,
                reviewer, confirmation_source, confirmation_label, confirmed_at, updated_at
            ) VALUES (?, ?, 'human_confirmed', 1.0, 1,
                'codex', 'batch_audit_confirmed_packages', 'accepted', datetime('now'), datetime('now'))
            ON CONFLICT(segment_id) DO UPDATE SET
                confirmed_text=excluded.confirmed_text,
                confirmation_level='human_confirmed',
                confidence_score=1.0,
                locked=1,
                reviewer='codex',
                confirmation_source='batch_audit_confirmed_packages',
                confirmation_label='accepted',
                confirmed_at=COALESCE(segment_confirmations.confirmed_at, datetime('now')),
                updated_at=datetime('now')
            """,
            (row["id"], row["candidate_text"] or ""),
        )
    return len(rows)


def write_audit_report(settings: dict, audits: list[PackageAudit], promoted: list[tuple[str, int]], apply: bool) -> None:
    risk_counts = Counter(audit.risk for audit in audits)
    auto_promotable = [audit for audit in audits if audit.can_auto_promote]
    blocked_by_hits = [audit for audit in audits if audit.strong_hits]
    sensitive = [audit for audit in audits if audit.risk == "sensitive"]
    lines = [
        "Batch audit confirmed packages report",
        f"Started at: {datetime.now().isoformat(timespec='seconds')}",
        f"Rule version: {RULE_VERSION}",
        f"Mode: {'apply' if apply else 'dry-run'}",
        "",
        "Summary:",
        f"- Audited packages: {len(audits)}",
        f"- Auto-promotable now: {len(auto_promotable)}",
        f"- Sensitive packages held for manual review: {len(sensitive)}",
        f"- Packages with strong hits: {len(blocked_by_hits)}",
        f"- Promoted packages: {len(promoted)}",
        f"- Promoted segments: {sum(total for _, total in promoted)}",
        "",
        "By risk:",
        *[f"- {risk}: {risk_counts[risk]}" for risk in sorted(risk_counts)],
        "",
        "Promoted:",
        *[f"- {segments} segments | {path}" for path, segments in promoted],
        "",
        "Auto-promotable dry-run queue:",
        *[
            (
                f"- {audit.total_segments} total, {audit.auto_confirmed} auto | "
                f"{audit.relative_path}"
            )
            for audit in auto_promotable[:80]
        ],
        "",
        "Manual/sensitive queue:",
        *[
            (
                f"- risk={audit.risk}, hits={len(audit.strong_hits)}, "
                f"{audit.total_segments} total, {audit.auto_confirmed} auto | {audit.relative_path}"
            )
            for audit in audits
            if audit.risk == "sensitive" or audit.strong_hits
        ][:120],
        "",
        "Strong hit samples:",
    ]
    for audit in audits:
        if not audit.strong_hits:
            continue
        lines.append(f"- {audit.relative_path} | risk={audit.risk} | hits={len(audit.strong_hits)}")
        for segment_id, source_key, reason, snippet in audit.strong_hits[:8]:
            lines.append(f"  - {segment_id} | {source_key} | {reason} | {snippet}")
    report_path = db.write_report(settings, "batch_audit_confirmed_packages", lines)
    print(f"[batch_audit] Report: {report_path}")


def main(
    *,
    apply: bool = False,
    limit: int | None = None,
    path_like: str | None = None,
    max_hits: int = 12,
) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[batch_audit] Starting batch audit for 100% auto-confirmed packages")
    print(f"[batch_audit] Database: {db.get_database_path(settings)}")
    print(f"[batch_audit] Apply: {apply}")
    if path_like:
        print(f"[batch_audit] Path filter: {path_like}")
    if limit is not None:
        print(f"[batch_audit] Limit: {limit}")

    promoted: list[tuple[str, int]] = []
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        audits = audit_packages(conn, path_like=path_like, limit=limit, max_hits=max_hits)
        if apply:
            for audit in audits:
                if not audit.can_auto_promote:
                    continue
                promoted_segments = promote_package(conn, audit)
                promoted.append((audit.relative_path, promoted_segments))
            conn.commit()

    auto_promotable = [audit for audit in audits if audit.can_auto_promote]
    sensitive = [audit for audit in audits if audit.risk == "sensitive"]
    blocked_by_hits = [audit for audit in audits if audit.strong_hits]
    elapsed = datetime.now() - started_at
    print(f"[batch_audit] Audited packages: {len(audits)}")
    print(f"[batch_audit] Auto-promotable: {len(auto_promotable)}")
    print(f"[batch_audit] Sensitive held: {len(sensitive)}")
    print(f"[batch_audit] Packages with strong hits: {len(blocked_by_hits)}")
    print(f"[batch_audit] Promoted packages: {len(promoted)}")
    print(f"[batch_audit] Promoted segments: {sum(total for _, total in promoted)}")
    print(f"[batch_audit] Elapsed: {elapsed}")
    write_audit_report(settings, audits, promoted, apply=apply)
    print("[batch_audit] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Audit 100% auto-confirmed packages and optionally promote safe clean packages."
    )
    parser.add_argument("--apply", action="store_true", help="Promote safe clean packages to human_confirmed.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum packages to inspect.")
    parser.add_argument("--path-like", default=None, help="Optional SQL LIKE filter for relative_path.")
    parser.add_argument("--max-hits", type=int, default=12, help="Maximum hit samples kept per package.")
    args = parser.parse_args()
    main(apply=args.apply, limit=args.limit, path_like=args.path_like, max_hits=args.max_hits)
