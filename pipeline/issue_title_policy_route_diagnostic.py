from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import db
from pending_architecture_diagnostic import (
    TOKEN_PATTERN,
    latest_run,
    percent,
    sample_text,
    top_package,
    word_count,
)


RULE_VERSION = "issue_title_policy_route_diagnostic_v1"
FAMILY = "title_policy_microagent"


LANDED_PREFIXES = {
    "b_": "barony_place_names",
    "c_": "county_title_names",
    "d_": "duchy_title_names",
    "k_": "kingdom_title_names",
    "e_": "empire_title_names",
}

SPANISH_HINT_RE = re.compile(
    r"\b(?:"
    r"direcci[oó]n|producci[oó]n|dise[nñ]o|pruebas?|ingenier[íi]a|ingeniero|"
    r"ilustraciones?|programaci[oó]n|gesti[oó]n|gerente|anal[ií]tica|"
    r"escudo de armas|sonido|compatibilidad|subcontratad[oa]s?|"
    r"m[uú]sica|viol[ií]n|la[uú]d|percusi[oó]n|teclados"
    r")\b",
    re.IGNORECASE,
)

PORTUGUESE_HINT_RE = re.compile(
    r"\b(?:"
    r"dire[cç][aã]o|produ[cç][aã]o|desenho|testes?|engenharia|engenheiro|"
    r"ilustra[cç][oõ]es?|programa[cç][aã]o|gest[aã]o|comunidade|"
    r"compatibilidade|m[uú]sica|som|art[ií]stic[ao]s?"
    r")\b",
    re.IGNORECASE,
)


def report_paths(settings: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = db.project_path(settings["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = reports_dir / f"{stamp}_issue_title_policy_route_diagnostic"
    return base.with_suffix(".txt"), base.with_suffix(".json"), base.with_suffix(".csv")


def latest_ledger_run_id(conn) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM ml_issue_ledger_runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise RuntimeError("No finished ml_issue_ledger_runs found.")
    return int(row["id"])


def fetch_rows(conn, run_id: int | None) -> tuple[int, list[dict[str, Any]]]:
    selected_run = run_id or latest_ledger_run_id(conn)
    rows = conn.execute(
        """
        SELECT
            item.*,
            state.final_state AS state_final_state,
            state.state_group AS state_group,
            state.review_state AS review_state,
            state.apply_state AS apply_state
        FROM ml_issue_ledger_items item
        LEFT JOIN segment_state_items state
          ON state.id = item.state_item_id
        WHERE item.run_id = ?
          AND item.issue_family = ?
        ORDER BY item.relative_path, item.source_line_number, item.segment_id
        """,
        (selected_run, FAMILY),
    ).fetchall()
    return selected_run, [dict(row) for row in rows]


def parse_evidence(row: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(row.get("evidence_json") or "{}")
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def is_landed_titles_path(path: str) -> bool:
    return path == "titles_l_spanish.yml"


def route_lane(row: dict[str, Any]) -> str:
    path = str(row.get("relative_path") or "").replace("\\", "/").lower()
    key = str(row.get("source_key") or "")
    key_lower = key.lower()

    if path == "credits/credits_l_spanish.yml" or (path.endswith("credits_l_spanish.yml") and key.startswith("TITLE_")):
        return "credit_role_labels"

    if path == "core_l_spanish.yml" and key.startswith("TITLE_NOMAD"):
        return "nomad_title_templates"

    if is_landed_titles_path(path):
        if key_lower.endswith("_adj"):
            return "landed_title_adjectives"
        for prefix, lane in LANDED_PREFIXES.items():
            if key_lower.startswith(prefix):
                return lane
        return "landed_title_misc"

    if "custom_localization" in path and "title" in path:
        return "custom_localization_title_fragments"

    if (
        "culture" in path
        and "title" in path
        or "titles_cultural_names" in path
        or "cultural_titles" in path
        or "japan_titles" in path
        or "korea_titles" in path
        or "other_titles" in path
    ):
        return "culture_title_labels"

    if any(part in path for part in ("trigger", "effect", "succession", "interaction", "scheme", "regiment")):
        return "title_ui_logic_text"

    if key.startswith("TITLE_"):
        return "generic_title_key_labels"

    return "title_policy_misc"


def route_action(lane: str) -> str:
    return {
        "credit_role_labels": "create_credit_role_label_translation_microagent",
        "nomad_title_templates": "create_nomad_title_template_token_policy",
        "landed_title_adjectives": "create_landed_title_adjective_policy",
        "barony_place_names": "create_place_name_gazetteer_policy",
        "county_title_names": "create_place_name_gazetteer_policy",
        "duchy_title_names": "create_place_name_gazetteer_policy",
        "kingdom_title_names": "create_place_name_gazetteer_policy",
        "empire_title_names": "create_place_name_gazetteer_policy",
        "landed_title_misc": "sample_landed_title_misc",
        "custom_localization_title_fragments": "route_to_custom_localization_title_microagent",
        "culture_title_labels": "route_to_culture_title_label_microagent",
        "title_ui_logic_text": "route_to_title_ui_logic_microagent",
        "generic_title_key_labels": "sample_generic_title_key_labels",
        "title_policy_misc": "sample_title_policy_misc",
    }.get(lane, "sample_title_policy_misc")


def authority_hint(lane: str) -> str:
    if lane in {"credit_role_labels", "nomad_title_templates", "title_ui_logic_text"}:
        return "candidate_shadow_after_review"
    if lane in {"barony_place_names", "county_title_names", "duchy_title_names", "kingdom_title_names", "empire_title_names"}:
        return "needs_gazetteer_or_preservation_policy"
    if lane in {"landed_title_adjectives", "culture_title_labels", "custom_localization_title_fragments"}:
        return "needs_specialist_checkpoint"
    return "sample_first"


def residual_hint(text: str) -> str:
    if SPANISH_HINT_RE.search(text) and not PORTUGUESE_HINT_RE.search(text):
        return "spanish_residual_likely"
    if SPANISH_HINT_RE.search(text):
        return "spanish_or_shared_romance_term"
    return "none"


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    lane_counts: Counter[str] = Counter()
    package_counts: Counter[str] = Counter()
    path_counts: Counter[str] = Counter()
    key_prefix_counts: Counter[str] = Counter()
    residual_counts: Counter[str] = Counter()
    token_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    authority_counts: Counter[str] = Counter()
    lane_by_path: dict[str, Counter[str]] = defaultdict(Counter)
    lane_by_residual: dict[str, Counter[str]] = defaultdict(Counter)
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    csv_rows: list[dict[str, Any]] = []

    for row in rows:
        ev = parse_evidence(row)
        path = str(row.get("relative_path") or "")
        key = str(row.get("source_key") or "")
        text = str(row.get("evidence_text") or "")
        lane = route_lane(row)
        action = route_action(lane)
        authority = authority_hint(lane)
        package = str(ev.get("package") or top_package(path))
        token_count = int(ev.get("token_count") or len(TOKEN_PATTERN.findall(text)))
        residual = residual_hint(text)
        key_prefix = key.split("_", 1)[0] + "_" if "_" in key else key[:16]

        lane_counts[lane] += 1
        package_counts[package] += 1
        path_counts[path] += 1
        key_prefix_counts[key_prefix] += 1
        residual_counts[residual] += 1
        token_counts[f"tokens_{token_count if token_count < 9 else '9_plus'}"] += 1
        action_counts[action] += 1
        authority_counts[authority] += 1
        lane_by_path[lane][path] += 1
        lane_by_residual[lane][residual] += 1

        record = {
            "segment_id": row["segment_id"],
            "relative_path": path,
            "source_key": key,
            "source_line_number": row["source_line_number"],
            "lane": lane,
            "action_hint": action,
            "authority_hint": authority,
            "residual_hint": residual,
            "package": package,
            "key_prefix": key_prefix,
            "token_count": token_count,
            "word_count": int(ev.get("word_count") or word_count(text)),
            "state_final_state": row.get("state_final_state"),
            "text": sample_text(text, 260),
        }
        csv_rows.append(record)
        if len(examples[lane]) < 10:
            examples[lane].append(record)

    return {
        "lane_counts": dict(lane_counts.most_common()),
        "package_counts": dict(package_counts.most_common(25)),
        "path_counts": dict(path_counts.most_common(30)),
        "key_prefix_counts": dict(key_prefix_counts.most_common(25)),
        "residual_counts": dict(residual_counts.most_common()),
        "token_counts": dict(token_counts.most_common()),
        "action_counts": dict(action_counts.most_common()),
        "authority_counts": dict(authority_counts.most_common()),
        "lane_by_path": {lane: dict(counter.most_common(12)) for lane, counter in lane_by_path.items()},
        "lane_by_residual": {lane: dict(counter.most_common()) for lane, counter in lane_by_residual.items()},
        "examples": dict(examples),
        "csv_rows": csv_rows,
    }


def build_report(run_id: int, state_run: dict[str, Any], rows: list[dict[str, Any]], summary: dict[str, Any]) -> list[str]:
    total = len(rows)
    distinct = len({row["segment_id"] for row in rows})
    lines = [
        "Title policy route diagnostic",
        f"Rule version: {RULE_VERSION}",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Ledger run id: {run_id}",
        f"Segment-state run id: {state_run['id']}",
        "",
        "Summary:",
        f"- title_policy items: {total:,}",
        f"- distinct segments: {distinct:,}",
        "- production authority: none; diagnostic only",
        "",
        "Interpretation:",
        "- `title_policy_microagent` is currently overloaded: it mixes landed place names, demonyms/adjectives, cultural title labels, credits roles, custom localization fragments and UI logic.",
        "- Do not promote a broad title-policy lifecycle bridge from this diagnostic alone.",
        "- The shortest safe path is to split this family into route-specific microagents/checkpoints.",
        "",
        "Recommended split:",
    ]
    for lane, count in summary["lane_counts"].items():
        lines.append(
            f"- {lane}: {count:,} ({percent(count, total):.2f}%) | {route_action(lane)} | {authority_hint(lane)}"
        )
        for path, path_count in list(summary["lane_by_path"].get(lane, {}).items())[:5]:
            lines.append(f"  - {path}: {path_count:,}")

    def add_counter(title: str, values: dict[str, int]) -> None:
        lines.extend(["", f"{title}:"])
        if not values:
            lines.append("- none")
        for label, count in values.items():
            lines.append(f"- {label}: {count:,} ({percent(count, total):.2f}%)")

    add_counter("Authority hints", summary["authority_counts"])
    add_counter("Recommended route actions", summary["action_counts"])
    add_counter("Residual hints", summary["residual_counts"])
    add_counter("Top paths", summary["path_counts"])
    add_counter("Top packages", summary["package_counts"])
    add_counter("Key prefixes", summary["key_prefix_counts"])
    add_counter("Token counts", summary["token_counts"])

    lines.extend(["", "Lane examples:"])
    for lane, examples in summary["examples"].items():
        lines.append(f"{lane}:")
        for example in examples:
            lines.append(
                f"- segment={example['segment_id']} | "
                f"{example['relative_path']}:{example['source_line_number']} | "
                f"{example['source_key']} | residual={example['residual_hint']} | "
                f"{example['text']}"
            )

    lines.extend(
        [
            "",
            "Recommended next step:",
            "1. Start with `credit_role_labels`: it is small, strongly residual-Spanish, and should become a narrow repair/review microagent rather than a title-feudal policy.",
            "2. Build a separate gazetteer/preservation diagnostic for `barony_place_names` and other landed title names; do not auto-translate all place names.",
            "3. Keep `landed_title_adjectives` separate because demonyms/adjectives are linguistic, not the same problem as place-name preservation.",
            "4. Route `culture_title_labels` to the existing culture/title specialist family.",
        ]
    )
    return lines


def write_outputs(
    txt_path: Path,
    json_path: Path,
    csv_path: Path,
    payload: dict[str, Any],
    rows: list[dict[str, Any]],
    lines: list[str],
) -> None:
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "segment_id",
            "relative_path",
            "source_key",
            "source_line_number",
            "lane",
            "action_hint",
            "authority_hint",
            "residual_hint",
            "package",
            "key_prefix",
            "token_count",
            "word_count",
            "state_final_state",
            "text",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Route title_policy_microagent backlog by title subtype.")
    parser.add_argument("--ledger-run-id", type=int)
    args = parser.parse_args()

    settings = db.load_settings()
    txt_path, json_path, csv_path = report_paths(settings)
    with db.connect(settings) as conn:
        db.ensure_database(conn)
        state_run = latest_run(conn, "segment_state_runs")
        run_id, rows = fetch_rows(conn, args.ledger_run_id)

    summary = summarize(rows)
    payload = {
        "rule_version": RULE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "ledger_run_id": run_id,
        "segment_state_run_id": int(state_run["id"]),
        "summary": {key: value for key, value in summary.items() if key != "csv_rows"},
        "total_items": len(rows),
        "distinct_segments": len({row["segment_id"] for row in rows}),
        "safety": {
            "diagnostic_only": True,
            "production_release_allowed": False,
            "writes_output": False,
            "writes_source": False,
        },
    }
    lines = build_report(run_id, state_run, rows, summary)
    write_outputs(txt_path, json_path, csv_path, payload, summary["csv_rows"], lines)

    print("[issue_title_policy_route_diagnostic] Diagnostic generated")
    print(f"[issue_title_policy_route_diagnostic] Ledger run id: {run_id}")
    print(f"[issue_title_policy_route_diagnostic] Items: {len(rows):,}")
    for lane, count in list(summary["lane_counts"].items())[:12]:
        print(f"[issue_title_policy_route_diagnostic] lane {lane}: {count:,}")
    print(f"[issue_title_policy_route_diagnostic] Report: {txt_path}")
    print(f"[issue_title_policy_route_diagnostic] JSON: {json_path}")
    print(f"[issue_title_policy_route_diagnostic] CSV: {csv_path}")


if __name__ == "__main__":
    main()
