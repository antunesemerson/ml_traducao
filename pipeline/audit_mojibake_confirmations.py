from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime

import db
import local_quality_validator
from apply_safe_output_updates import protected_tokens


RULE_VERSION = "audit_mojibake_confirmations_v1"

REPLACEMENTS = {
    "Voc?": "Você",
    "voc?": "você",
    "n?o": "não",
    "N?o": "Não",
    "pr?prio": "próprio",
    "pr?pria": "própria",
    "dom?nio": "domínio",
    "Dom?nio": "Domínio",
    "imp?rio": "império",
    "Imp?rio": "Império",
    "p?tria": "pátria",
    "P?tria": "Pátria",
    "pr?spera": "próspera",
    "Pr?spera": "Próspera",
    "mar?": "maré",
    "Nort?mbria": "Nortúmbria",
    "Pol?nia": "Polônia",
    "pol?tica": "política",
    "Pol?tica": "Política",
    "eg?pcia": "egípcia",
    "seguran?a": "segurança",
    "Seguran?a": "Segurança",
    "atrav?s": "através",
    "s?culos": "séculos",
    "S?culos": "Séculos",
    "torn?-la": "torná-la",
    "It?lia": "Itália",
    "consumir?": "consumirá",
    "ambi??es": "ambições",
    "ambi??o": "ambição",
    "conclus?o": "conclusão",
    "?ndia": "Índia",
    "mu?ulmanos": "muçulmanos",
    "f?s": "fés",
    "Ib?ria": "Ibéria",
    "j?": "já",
    "J?": "Já",
    "al?m": "além",
    "Al?m": "Além",
    "s? deles": "só deles",
    "s? ": "só ",
    " m?e": " mãe",
    "M?e": "Mãe",
    "influ?ncia": "influência",
    "posi??o": "posição",
    "Posi??o": "Posição",
    "for?a": "força",
    "For?a": "Força",
    "f??": "fé?",
    " f? ": " fé ",
    " f?#!": " fé#!",
    "flores?a": "floresça",
    "aceita??o": "aceitação",
    "amea?a": "ameaça",
    "Amea?a": "Ameaça",
    "g?ridas": "gúridas",
    "g?rida": "gúrida",
    "hist?ria": "história",
    "Hist?ria": "História",
    "vit?ria": "vitória",
    "Vit?ria": "Vitória",
    "sin?nimo": "sinônimo",
    "ca?ador": "caçador",
    "Ca?ador": "Caçador",
    "sult?o": "sultão",
    "Sult?o": "Sultão",
    "m?os": "mãos",
    "M?os": "Mãos",
    "P?rsia": "Pérsia",
    "n?mesis": "nêmesis",
    "come?ou": "começou",
    "come?o": "começo",
    "Come?o": "Começo",
    "reuni?o": "reunião",
    "Reuni?o": "Reunião",
    "sal?rio": "salário",
    "Sal?rio": "Salário",
    "sal?rios": "salários",
    "Sal?rios": "Salários",
    "servi?o": "serviço",
    "Servi?o": "Serviço",
    "opini?o": "opinião",
    "Opini?o": "Opinião",
    "ex?rcito": "exército",
    "Ex?rcito": "Exército",
    "agr?colas": "agrícolas",
    "Agr?colas": "Agrícolas",
    "for?ar": "forçar",
    "For?ar": "Forçar",
    "vis?vel": "visível",
    "Vis?vel": "Visível",
    "not?vel": "notável",
    "Not?vel": "Notável",
    "ningu?m": "ninguém",
    "Ningu?m": "Ninguém",
    "algu?m": "alguém",
    "Algu?m": "Alguém",
    "n?veis": "níveis",
    "N?veis": "Níveis",
    "n?mero": "número",
    "N?mero": "Número",
    "di?rio": "diário",
    "Di?rio": "Diário",
    "governan?a": "governança",
    "Governan?a": "Governança",
    "deserá?o": "deserção",
    "Deserá?o": "Deserção",
    "estar?o": "estarão",
    "aumentar?o": "aumentarão",
    "pr?xima": "próxima",
    "Pr?xima": "Próxima",
    "?ustria": "Áustria",
    "?guas": "águas",
    "? sua": "à sua",
    "?s ": "às ",
    " ? um": " é um",
    " ? uma": " é uma",
    " ? mais": " é mais",
    " ? poss?vel": " é possível",
    " ? ": " é ",
    "p?r": "pôr",
    "conseguir?": "conseguirá",
    "poder?": "poderá",
    "custar?": "custará",
    "ficar?": "ficará",
    "receber?": "receberá",
    "ter?": "terá",
    "dar?": "dará",
    "tomar?": "tomará",
    "ecoar?": "ecoará",
    "curvar?": "curvará",
    "ir?": "irá",
    "alcan?ar": "alcançar",
    "Alcan?ar": "Alcançar",
    "avan?os": "avanços",
    "Avan?os": "Avanços",
    "gera??es": "gerações",
    "Gera??es": "Gerações",
    "persegui??o": "perseguição",
    "Persegui??o": "Perseguição",
    "crist?os": "cristãos",
    "Crist?os": "Cristãos",
    "om?ada": "omíada",
    "Om?ada": "Omíada",
    "c?taros": "cátaros",
    "C?taros": "Cátaros",
    "B?snia": "Bósnia",
    "Arm?nia": "Armênia",
    "futuras": "futuras",
    "fi?is": "fiéis",
    "Fi?is": "Fiéis",
    "v?nculos": "vínculos",
    "V?nculos": "Vínculos",
    "sal?es": "salões",
    "Sal?es": "Salões",
    "reuni?es": "reuniões",
    "Reuni?es": "Reuniões",
    "cortes?s": "cortesãs",
    "Cortes?s": "Cortesãs",
    "magnific?ncia": "magnificência",
    "Magnific?ncia": "Magnificência",
    "can??es": "canções",
    "Can??es": "Canções",
    "del?cias": "delícias",
    "Del?cias": "Delícias",
    "magn?ficas": "magníficas",
    "Magn?ficas": "Magníficas",
    "constru?do": "construído",
    "Constru?do": "Construído",
    "colaborar?o": "colaborarão",
    "come?a": "começa",
    "Come?a": "Começa",
    "prec?ria": "precária",
    "Prec?ria": "Precária",
    "disposi??o": "disposição",
    "Disposi??o": "Disposição",
    "n?rdicos": "nórdicos",
    "N?rdicos": "Nórdicos",
    "anglo-sax?es": "anglo-saxões",
    "t?o": "tão",
    "T?o": "Tão",
    "g?is": "góis",
    "pag?os": "pagãos",
    "Pag?os": "Pagãos",
    "carol?ngios": "carolíngios",
    "rel?gio": "relógio",
    "m?ritos": "méritos",
    "M?ritos": "Méritos",
    "preemin?ncia": "preeminência",
    "Preemin?ncia": "Preeminência",
    "servi?al": "serviçal",
    "Servi?al": "Serviçal",
    "irritadi?o": "irritadiço",
    "poss?veis": "possíveis",
    "Poss?veis": "Possíveis",
    "dire??o": "direção",
    "Dire??o": "Direção",
    "desd?m": "desdém",
    "audi?ncias": "audiências",
    "Audi?ncias": "Audiências",
    "?ltima": "última",
    "? a": "é a",
    "po??o": "poção",
    "pal?cio": "palácio",
    "Pal?cio": "Palácio",
    "crian?a": "criança",
    "Crian?a": "Criança",
    "pr?ximo": "próximo",
    "Pr?ximo": "Próximo",
    "revela??es": "revelações",
    "espet?culo": "espetáculo",
    "Espet?culo": "Espetáculo",
    "ap?s": "após",
    "Ap?s": "Após",
    "clich?s": "clichês",
    "p?blicos": "públicos",
    "saud?vel": "saudável",
    "Saud?vel": "Saudável",
    "disc?rdia": "discórdia",
    "aten??o": "atenção",
    "Aten??o": "Atenção",
    "ningu?m?": "ninguém?",
    "subterf?gio": "subterfúgio",
    "p?nico": "pânico",
    "esp?cie": "espécie",
    "esp?cies": "espécies",
    "?nico": "único",
    "s?quito": "séquito",
    "S?quito": "Séquito",
    "diferen?as": "diferenças",
    "Diferen?as": "Diferenças",
    "tr?s": "trás",
    "?ltima vez": "última vez",
    "? for?a": "à força",
    "viol?ncia": "violência",
    "Viol?ncia": "Violência",
    "estalajadeiro": "estalajadeiro",
    "propriet?rio": "proprietário",
    "Propriet?rio": "Proprietário",
    "sal?o": "salão",
    "Sal?o": "Salão",
    "not?cia": "notícia",
    "Not?cia": "Notícia",
    "cabe?a": "cabeça",
    "Cabe?a": "Cabeça",
    "?rea": "área",
    "transfer?ncia": "transferência",
    "espec?fico": "específico",
    "Espec?fico": "Específico",
    "reduzir?o": "reduzirão",
    "atribu?da": "atribuída",
    "atribu?do": "atribuído",
}


def fix_text(value: str) -> tuple[str, list[str]]:
    fixed = value
    hits: list[str] = []
    for before, after in sorted(REPLACEMENTS.items(), key=lambda item: len(item[0]), reverse=True):
        if before in fixed:
            fixed = fixed.replace(before, after)
            hits.append(before)
    return fixed, hits


def has_question_mojibake(value: str) -> bool:
    issues = local_quality_validator.validate_text(value)["issues"]
    return any(issue["code"] == "replacement_question_mark_mojibake" for issue in issues)


def main(
    *,
    apply: bool | None = None,
    limit: int | None = None,
    path_like: str | None = None,
) -> None:
    if apply is None:
        parser = argparse.ArgumentParser(description="Audit and fix confirmed text with accent mojibake as '?'.")
        parser.add_argument("--apply", action="store_true", help="Update segment_confirmations. Default is dry-run.")
        parser.add_argument("--limit", type=int, default=None, help="Maximum candidate confirmations to inspect.")
        parser.add_argument("--path-like", default=None, help="Optional SQL LIKE filter for source relative_path.")
        args = parser.parse_args()
        apply = args.apply
        limit = args.limit
        path_like = args.path_like

    started_at = datetime.now()
    settings = db.load_settings()
    print("[audit_mojibake_confirmations] Starting mojibake confirmation audit")
    print(f"[audit_mojibake_confirmations] Rule version: {RULE_VERSION}")
    print(f"[audit_mojibake_confirmations] Apply: {apply}")
    print(f"[audit_mojibake_confirmations] Limit: {limit if limit else 'none'}")
    print(f"[audit_mojibake_confirmations] Path filter: {path_like if path_like else 'none'}")

    where = ["sc.confirmed_text LIKE '%?%'"]
    params: list[object] = []
    if path_like:
        where.append("s.relative_path LIKE ?")
        params.append(path_like)
    limit_sql = "LIMIT ?" if limit else ""
    if limit:
        params.append(limit)

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        rows = conn.execute(
            f"""
            SELECT
                sc.id,
                sc.segment_id,
                sc.confirmation_level,
                sc.confirmation_source,
                sc.confirmation_label,
                sc.locked,
                sc.confirmed_text,
                s.relative_path,
                s.source_key,
                s.spanish_text
            FROM segment_confirmations sc
            JOIN source_segments s ON s.id = sc.segment_id
            WHERE {" AND ".join(where)}
            ORDER BY sc.locked DESC, sc.confirmation_level DESC, s.relative_path, s.source_key
            {limit_sql}
            """,
            params,
        ).fetchall()

        inspected = 0
        suspicious = 0
        fixable: list[dict] = []
        skipped = Counter()

        for row in rows:
            inspected += 1
            original = row["confirmed_text"] or ""
            if not has_question_mojibake(original):
                skipped["not_question_mojibake"] += 1
                continue
            suspicious += 1
            fixed, hits = fix_text(original)
            if fixed == original:
                skipped["no_replacement_rule"] += 1
                continue
            if protected_tokens(original) != protected_tokens(fixed):
                skipped["token_mismatch"] += 1
                continue
            if has_question_mojibake(fixed):
                skipped["still_question_mojibake"] += 1
                continue

            fixable.append(
                {
                    "id": row["id"],
                    "segment_id": row["segment_id"],
                    "relative_path": row["relative_path"],
                    "source_key": row["source_key"],
                    "confirmation_level": row["confirmation_level"],
                    "confirmation_source": row["confirmation_source"],
                    "locked": row["locked"],
                    "before": original,
                    "after": fixed,
                    "hits": hits,
                }
            )

        if apply and fixable:
            now = db.utc_now()
            for item in fixable:
                conn.execute(
                    """
                    UPDATE segment_confirmations
                    SET confirmed_text = ?,
                        confirmation_label = COALESCE(confirmation_label, '') || ';mojibake_fixed',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (item["after"], now, item["id"]),
                )
            conn.commit()

    elapsed = datetime.now() - started_at
    print(f"[audit_mojibake_confirmations] Confirmations inspected: {inspected}")
    print(f"[audit_mojibake_confirmations] Suspicious confirmations: {suspicious}")
    print(f"[audit_mojibake_confirmations] Fixable confirmations: {len(fixable)}")
    print(f"[audit_mojibake_confirmations] Applied fixes: {len(fixable) if apply else 0}")

    report_lines = [
        "Mojibake confirmations audit report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        f"Apply: {apply}",
        f"Limit: {limit if limit else 'none'}",
        f"Path filter: {path_like if path_like else 'none'}",
        "",
        "Summary:",
        f"- Confirmations inspected: {inspected}",
        f"- Suspicious confirmations: {suspicious}",
        f"- Fixable confirmations: {len(fixable)}",
        f"- Applied fixes: {len(fixable) if apply else 0}",
        "",
        "Skipped:",
    ]
    for reason, count in skipped.most_common():
        report_lines.append(f"- {reason}: {count}")

    by_source = Counter(item["confirmation_source"] for item in fixable)
    report_lines.extend(["", "Fixable by source:"])
    for source, count in by_source.most_common():
        report_lines.append(f"- {source}: {count}")

    report_lines.extend(["", "Preview:"])
    for item in fixable[:80]:
        before = item["before"].replace("\n", "\\n")
        after = item["after"].replace("\n", "\\n")
        if len(before) > 220:
            before = before[:220] + "..."
        if len(after) > 220:
            after = after[:220] + "..."
        report_lines.append(
            f"- segment {item['segment_id']} | locked={item['locked']} | {item['relative_path']}::{item['source_key']}"
        )
        report_lines.append(f"  hits: {', '.join(item['hits'][:20])}")
        report_lines.append(f"  before: {before}")
        report_lines.append(f"  after:  {after}")

    report_path = db.write_report(settings, "audit_mojibake_confirmations", report_lines)
    print(f"[audit_mojibake_confirmations] Report: {report_path}")
    print("[audit_mojibake_confirmations] Done")


if __name__ == "__main__":
    main()
