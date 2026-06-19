from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "pending_architecture_diagnostic_v1"

WORD_PATTERN = re.compile(r"[A-Za-z\u00c0-\u00ff]+", re.UNICODE)
TOKEN_PATTERN = re.compile(
    r"\[[^\]]+\]|\$[^$\s]+\$|#[A-Za-z0-9_]+|#!|@[A-Za-z0-9_]+!|\\n",
    re.UNICODE,
)
SPANISH_MARKERS = re.compile(
    r"[\u00bf\u00a1\u00ab\u00bb]|\b("
    r"se\u00f1or(?:a|es)?|vuestr[ao]s?|nuestr[ao]s?|"
    r"cortesanos?|cortesanas?|consejo|decisiones?|situaciones?|"
    r"rechaza|rechazar|cr\u00eda|cr\u00edo|robaste|rob\u00f3|"
    r"asaltaste|asalt\u00f3|salvaste|salv\u00f3|dejaste|dej\u00f3|"
    r"decidiste|decidi\u00f3|conseguiste|consigui\u00f3"
    r")\b",
    re.IGNORECASE,
)
SPANISH_REASON_MARKERS = (
    "spanish_residue",
    "residual_spanish",
    "spanish_punctuation",
    "spanish_residue_in_literal",
    "inline_spanish_literal",
    "dynamic_spanish_literal",
    "spanish_literal",
)
GENDER_TOKEN_MARKERS = re.compile(
    r"ES_(?:OA|AO|ElLa|DelDela|A|O)|Select_CString|GetHerHis|GetSheHe|GetWomanMan",
    re.IGNORECASE,
)
DYNAMIC_LOC_MARKERS = re.compile(
    r"Custom\(|Select_CString|LocalPlayerString|Concept\(|Get[A-Za-z_]+\(",
    re.IGNORECASE,
)
NICKNAME_SELECT_CSTRING_SPANISH_LITERAL_PATTERN = re.compile(
    r"\b(?:"
    r"apoyas|apoya|habl[aá]is|hablan|hablas|luchas|lucha|puedes|puede|"
    r"apareciste|apareci[oó]|comes|come|alabasteis|alabaron|"
    r"posees|inspiras|hacerte|termina|comportamiento|cielos"
    r")\b",
    re.IGNORECASE,
)


def parse_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def percent(part: int | float, total: int | float) -> float:
    if not total:
        return 0.0
    return float(part) / float(total) * 100


def sample_text(value: str | None, limit: int = 180) -> str:
    text = (value or "").replace("\n", "\\n").replace("\t", "\\t").strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def top_package(relative_path: str | None) -> str:
    path = relative_path or "unknown"
    if "/" in path:
        return path.split("/", 1)[0]
    return path


def text_for_analysis(row: dict[str, Any]) -> str:
    for key in (
        "confirmed_text",
        "portuguese_text",
        "candidate_text",
        "old_text",
        "spanish_text",
        "english_text",
    ):
        value = row.get(key)
        if value is not None:
            return str(value)
    return ""


def strip_tokens(value: str) -> str:
    return TOKEN_PATTERN.sub(" ", value)


def word_count(value: str) -> int:
    return len(WORD_PATTERN.findall(strip_tokens(value)))


def length_bucket(length: int) -> str:
    if length == 0:
        return "blank"
    if length <= 40:
        return "tiny_1_40"
    if length <= 100:
        return "short_41_100"
    if length <= 250:
        return "medium_101_250"
    if length <= 600:
        return "long_251_600"
    return "very_long_601_plus"


def token_bucket(count: int) -> str:
    if count == 0:
        return "tokens_0"
    if count == 1:
        return "tokens_1"
    if count <= 3:
        return "tokens_2_3"
    if count <= 8:
        return "tokens_4_8"
    return "tokens_9_plus"


def issue_codes(row: dict[str, Any]) -> set[str]:
    codes: set[str] = set()
    for prefix in ("active", "candidate"):
        for issue in parse_json(row.get(f"{prefix}_issues_json"), []):
            if isinstance(issue, dict) and issue.get("code"):
                codes.add(str(issue["code"]))
    return codes


def reason_text(row: dict[str, Any]) -> str:
    chunks: list[str] = []
    for key in ("state_reasons_json", "active_reasons_json", "candidate_reasons_json", "policy_reasons_json"):
        value = parse_json(row.get(key), [])
        if isinstance(value, list):
            chunks.extend(str(item) for item in value)
        elif isinstance(value, dict):
            chunks.extend(f"{k}:{v}" for k, v in value.items())
    return " ".join(chunks).lower()


def domain_family(row: dict[str, Any]) -> str:
    path = str(row.get("relative_path") or "").lower()
    key = str(row.get("source_key") or "").lower()
    if "religion" in path or key.startswith(("faith_", "religion_", "doctrine_")):
        return "domain_religion"
    if "culture" in path or "tradition" in path or key.startswith(("culture_", "tradition_")):
        return "domain_culture"
    if "title" in path or "titles" in path or key.startswith(("title_", "nick_")) or "nickname" in path:
        return "domain_titles_names"
    if "event_localization" in path or ".desc" in key or ".toast" in key:
        return "domain_events_longform"
    if "effect" in path or "trigger" in path or "modifier" in path:
        return "domain_rules_tooltips"
    if "interaction" in path or "scheme" in path or "activity" in path:
        return "domain_interactions_activities"
    return "domain_general"


def micro_families(row: dict[str, Any]) -> list[str]:
    text = text_for_analysis(row)
    visible = strip_tokens(text)
    reasons = reason_text(row)
    codes = issue_codes(row)
    path = str(row.get("relative_path") or "").lower()
    key = str(row.get("source_key") or "").lower()
    families: list[str] = []

    active_action = row.get("active_action") or ""
    candidate_action = row.get("candidate_action") or ""
    policy_action = row.get("policy_action") or ""
    candidate_token = row.get("candidate_token_status") or ""
    active_token = row.get("active_token_status") or ""
    high_issue_count = int(row.get("candidate_high_issue_count") or 0) + int(row.get("active_high_issue_count") or 0)

    if (
        "blocked_structure" in {active_action, candidate_action, policy_action}
        or candidate_token not in {"", "ok"}
        or active_token not in {"", "ok"}
        or "token_mismatch" in codes
    ):
        families.append("structural_token_gate")

    if GENDER_TOKEN_MARKERS.search(text) or "gender" in reasons or "gender" in " ".join(codes):
        families.append("gender_token_microagent")

    if DYNAMIC_LOC_MARKERS.search(text) or "select_cstring" in reasons or "custom_loc" in reasons:
        families.append("dynamic_ck3_expression_microagent")

    if (
        path == "nicknames_l_spanish.yml"
        and "select_cstring" in text.casefold()
        and NICKNAME_SELECT_CSTRING_SPANISH_LITERAL_PATTERN.search(text)
    ):
        families.append("nickname_select_cstring_spanish_residual_microagent")

    if (
        SPANISH_MARKERS.search(visible)
        or "spanish_residue" in codes
        or "spanish_residue_in_literal" in codes
        or any(marker in reasons for marker in SPANISH_REASON_MARKERS)
    ):
        families.append("spanish_residual_microagent")

    if "boundary" in reasons or "missing_space" in " ".join(codes) or "punctuation" in " ".join(codes):
        families.append("surface_boundary_microagent")

    if high_issue_count > 0:
        families.append("high_issue_auditor")

    words = word_count(text)
    if words >= 70 or len(text) >= 600:
        families.append("long_text_composer")
    elif words <= 8 or len(text) <= 80:
        families.append("short_label_style_microagent")

    if "nickname" in path or key.startswith("nick_"):
        families.append("nickname_name_policy")
    if "title" in path or "titles" in path or key.startswith("title_"):
        families.append("title_policy_microagent")
    if "religion" in path:
        families.append("religion_semantic_microagent")
    if "culture" in path or "tradition" in path:
        families.append("culture_semantic_microagent")

    if candidate_action == "needs_autofix" and not any(fam.endswith("microagent") for fam in families):
        families.append("autofix_unknown_microagent")
    if candidate_action == "needs_human" or active_action == "needs_human":
        families.append("semantic_review_router")
    if str(row.get("final_state") or "").startswith("reopen_auto_confirmed"):
        families.append("legacy_auto_confirmation_reopen")

    if not families:
        families.append("unclassified_pending")
    return sorted(set(families))


def primary_family(families: list[str]) -> str:
    priority = [
        "structural_token_gate",
        "gender_token_microagent",
        "dynamic_ck3_expression_microagent",
        "nickname_select_cstring_spanish_residual_microagent",
        "spanish_residual_microagent",
        "surface_boundary_microagent",
        "long_text_composer",
        "title_policy_microagent",
        "nickname_name_policy",
        "religion_semantic_microagent",
        "culture_semantic_microagent",
        "short_label_style_microagent",
        "autofix_unknown_microagent",
        "semantic_review_router",
        "legacy_auto_confirmation_reopen",
        "unclassified_pending",
    ]
    for item in priority:
        if item in families:
            return item
    return families[0] if families else "unclassified_pending"


def latest_run(conn, table_name: str) -> dict[str, Any]:
    row = conn.execute(f"SELECT * FROM {table_name} ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        raise RuntimeError(f"No rows in {table_name}.")
    return dict(row)


def fetch_pending_rows(conn, state_run: dict[str, Any], limit: int | None) -> list[dict[str, Any]]:
    limit_sql = "LIMIT ?" if limit else ""
    params: list[Any] = [
        int(state_run["id"]),
        int(state_run["active_score_run_id"] or 0),
        int(state_run["candidate_score_run_id"] or 0),
        int(state_run["policy_run_id"] or 0),
    ]
    if limit:
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            ssi.*,
            s.spanish_text,
            s.english_text,
            s.old_text,
            o.portuguese_text,
            sc.confirmed_text,
            sc.confirmation_source,
            sc.confidence_score AS confirmation_confidence,
            active.final_action AS active_final_action,
            active.risk_class AS active_risk_class,
            active.token_status AS active_token_status,
            active.issue_count AS active_issue_count,
            active.high_issue_count AS active_high_issue_count,
            active.medium_issue_count AS active_medium_issue_count,
            active.word_count AS active_word_count,
            active.reasons_json AS active_reasons_json,
            active.issues_json AS active_issues_json,
            candidate.candidate_text AS candidate_text,
            candidate.final_action AS candidate_final_action,
            candidate.risk_class AS candidate_risk_class,
            candidate.token_status AS candidate_token_status,
            candidate.issue_count AS candidate_issue_count,
            candidate.high_issue_count AS candidate_high_issue_count,
            candidate.medium_issue_count AS candidate_medium_issue_count,
            candidate.word_count AS candidate_word_count,
            candidate.reasons_json AS candidate_reasons_json,
            candidate.issues_json AS candidate_issues_json,
            policy.policy_group AS policy_group,
            policy.new_safe AS policy_new_safe,
            policy.learned_positive AS policy_learned_positive,
            policy.learned_negative AS policy_learned_negative,
            policy.reasons_json AS policy_reasons_json
        FROM segment_state_items ssi
        JOIN source_segments s ON s.id = ssi.segment_id
        LEFT JOIN output_segments o ON o.segment_id = ssi.segment_id
        LEFT JOIN segment_confirmations sc ON sc.segment_id = ssi.segment_id
        LEFT JOIN ml_score_items active
            ON active.run_id = ?
           AND active.segment_id = ssi.segment_id
        LEFT JOIN ml_score_items candidate
            ON candidate.run_id = ?
           AND candidate.segment_id = ssi.segment_id
        LEFT JOIN ml_policy_items policy
            ON policy.run_id = ?
           AND policy.segment_id = ssi.segment_id
        WHERE ssi.run_id = ?
          AND ssi.state_group = 'pending'
        ORDER BY ssi.priority_score DESC, ssi.segment_id ASC
        {limit_sql}
        """,
        (params[1], params[2], params[3], params[0], *params[4:]),
    ).fetchall()
    return [dict(row) for row in rows]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counters: dict[str, Counter[str]] = {
        "final_state": Counter(),
        "action_combo": Counter(),
        "package": Counter(),
        "domain": Counter(),
        "length_bucket": Counter(),
        "token_bucket": Counter(),
        "primary_family": Counter(),
        "micro_family": Counter(),
        "issue_code": Counter(),
        "confirmation_label": Counter(),
        "policy_group": Counter(),
    }
    family_by_package: dict[str, Counter[str]] = defaultdict(Counter)
    family_by_domain: dict[str, Counter[str]] = defaultdict(Counter)
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in rows:
        text = text_for_analysis(row)
        token_count = len(TOKEN_PATTERN.findall(text))
        families = micro_families(row)
        primary = primary_family(families)
        package = top_package(row.get("relative_path"))
        domain = domain_family(row)
        combo = "|".join(
            str(row.get(key) or "")
            for key in ("active_action", "candidate_action", "policy_action", "final_state")
        )

        counters["final_state"][str(row.get("final_state") or "unknown")] += 1
        counters["action_combo"][combo] += 1
        counters["package"][package] += 1
        counters["domain"][domain] += 1
        counters["length_bucket"][length_bucket(len(text))] += 1
        counters["token_bucket"][token_bucket(token_count)] += 1
        counters["primary_family"][primary] += 1
        counters["confirmation_label"][str(row.get("confirmation_label") or "unknown")] += 1
        counters["policy_group"][str(row.get("policy_group") or "none")] += 1

        for family in families:
            counters["micro_family"][family] += 1
            family_by_package[family][package] += 1
            family_by_domain[family][domain] += 1
            if len(examples[family]) < 5:
                examples[family].append(
                    {
                        "segment_id": row["segment_id"],
                        "relative_path": row["relative_path"],
                        "source_key": row["source_key"],
                        "line": row["source_line_number"],
                        "active_action": row["active_action"],
                        "candidate_action": row["candidate_action"],
                        "policy_action": row["policy_action"],
                        "final_state": row["final_state"],
                        "text": sample_text(text),
                    }
                )

        for code in issue_codes(row):
            counters["issue_code"][code] += 1

    return {
        "total": len(rows),
        "counters": {key: dict(counter.most_common()) for key, counter in counters.items()},
        "family_by_package": {
            family: dict(counter.most_common(12)) for family, counter in family_by_package.items()
        },
        "family_by_domain": {
            family: dict(counter.most_common(12)) for family, counter in family_by_domain.items()
        },
        "examples": dict(examples),
    }


def build_lines(state_run: dict[str, Any], policy_run: dict[str, Any] | None, summary: dict[str, Any], txt_path: Path, json_path: Path) -> list[str]:
    total = int(state_run["total_segments"] or 0)
    pending = int(state_run["pending_count"] or 0)
    closed = int(state_run["closed_count"] or 0)
    output_apply = int(state_run["output_apply_pending_count"] or 0)
    rows_total = int(summary["total"])
    counters = summary["counters"]

    lines = [
        "Pending architecture diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Segment-state run id: {state_run['id']}",
        f"Active score run id: {state_run.get('active_score_run_id')}",
        f"Candidate score run id: {state_run.get('candidate_score_run_id')}",
        f"Policy run id: {state_run.get('policy_run_id')}",
        "",
        "State summary:",
        f"- Total active segments: {total:,}",
        f"- Closed/consolidated: {closed:,} ({percent(closed, total):.2f}%)",
        f"- Pending operational: {pending:,} ({percent(pending, total):.2f}%)",
        f"- Needs output apply: {output_apply:,}",
        f"- Pending rows inspected: {rows_total:,} ({percent(rows_total, pending):.2f}% of pending)",
    ]
    if policy_run:
        lines.extend(
            [
                "",
                "Latest policy:",
                f"- Policy run id: {policy_run['id']}",
                f"- Rule version: {policy_run.get('rule_version')}",
                f"- Policy auto-safe: {int(policy_run.get('policy_auto_safe_count') or 0):,}",
                f"- New safe: {int(policy_run.get('new_safe_count') or 0):,}",
                f"- Demoted safe: {int(policy_run.get('demoted_safe_count') or 0):,}",
            ]
        )

    def add_counter(title: str, key: str, limit: int = 18) -> None:
        lines.extend(["", f"{title}:"])
        items = list(counters.get(key, {}).items())[:limit]
        if not items:
            lines.append("- none")
        for label, count in items:
            lines.append(f"- {label}: {count:,} ({percent(count, rows_total):.2f}%)")

    add_counter("Final states", "final_state")
    add_counter("Action combos", "action_combo", 15)
    add_counter("Top packages", "package", 20)
    add_counter("Domains", "domain")
    add_counter("Length buckets", "length_bucket")
    add_counter("Token buckets", "token_bucket")
    add_counter("Primary micro-agent family", "primary_family", 20)
    add_counter("Multi-label micro-agent opportunities", "micro_family", 30)
    add_counter("Top issue codes", "issue_code", 30)
    add_counter("Top policy groups", "policy_group", 20)

    lines.extend(
        [
            "",
            "Interpretation:",
            "- The old pending diagnostic is almost empty because most pending rows already have confirmations.",
            "- The current backlog is mostly lifecycle/composite disagreement: confirmed rows reopened by the modern scorer/policy.",
            "- If one specialist must close a whole segment, gains will stay small and manual work will grow.",
            "- The next architecture should store issue-level observations, let multiple micro-agents vote or repair the same segment, then run a final coordinator/auditor.",
            "",
            "Recommended architecture shift:",
            "1. Add an issue ledger per segment: each detected span/problem becomes a row with family, evidence, proposed repair, token impact and validator result.",
            "2. Add cross-cutting micro-agents first: structural_token_gate, gender_token_microagent, dynamic_ck3_expression_microagent, spanish_residual_microagent, surface_boundary_microagent.",
            "3. Keep domain specialists as context reviewers, not the only closer: titles/religion/culture should arbitrate style and terminology after micro-repairs.",
            "4. Add a composer/coordinator that merges accepted micro-repairs and only emits a final candidate when token parity, lifecycle policy and semantic guards agree.",
            "5. Track promotion/discard per micro-agent by coverage, false-safe count, repair yield, and net pending reduction after production.",
            "",
            "Report files:",
            f"- Text: {txt_path}",
            f"- JSON: {json_path}",
        ]
    )

    lines.extend(["", "Family examples:"])
    for family, family_examples in summary["examples"].items():
        lines.append(f"{family}:")
        for example in family_examples[:3]:
            lines.append(
                "- segment {segment_id} | {relative_path}:{line} | {source_key} | "
                "active={active_action} candidate={candidate_action} policy={policy_action} state={final_state}".format(
                    **example
                )
            )
            lines.append(f"  text: {example['text']}")

    return lines


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = reports_dir / f"{stamp}_pending_architecture_diagnostic"
    return base.with_suffix(".txt"), base.with_suffix(".json")


def main(limit: int | None = None) -> None:
    settings = db.load_settings()
    print("[pending_architecture_diagnostic] Starting")
    print(f"[pending_architecture_diagnostic] Rule version: {RULE_VERSION}")
    print(f"[pending_architecture_diagnostic] Database: {db.get_database_path(settings)}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        state_run = latest_run(conn, "segment_state_runs")
        policy_run = None
        if state_run.get("policy_run_id"):
            row = conn.execute(
                "SELECT * FROM ml_policy_runs WHERE id = ?",
                (int(state_run["policy_run_id"]),),
            ).fetchone()
            policy_run = dict(row) if row else None
        rows = fetch_pending_rows(conn, state_run, limit)

    summary = summarize(rows)
    txt_path, json_path = report_paths(settings)
    payload = {
        "rule_version": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "state_run": state_run,
        "policy_run": policy_run,
        "summary": summary,
    }
    lines = build_lines(state_run, policy_run, summary, txt_path, json_path)
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[pending_architecture_diagnostic] Pending inspected: {summary['total']}")
    print(f"[pending_architecture_diagnostic] Report: {txt_path}")
    print(f"[pending_architecture_diagnostic] JSON: {json_path}")
    for family, count in list(summary["counters"]["primary_family"].items())[:10]:
        print(f"[pending_architecture_diagnostic] primary {family}: {count}")
    print("[pending_architecture_diagnostic] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diagnose pending segment-state backlog for neural architecture planning.")
    parser.add_argument("--limit", type=int, default=None, help="Optional pending row limit for quick sampling.")
    args = parser.parse_args()
    main(limit=args.limit)
