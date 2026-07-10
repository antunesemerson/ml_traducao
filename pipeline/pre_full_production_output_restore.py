from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE_MIRROR = ROOT / "source" / "spanish_source"
OUTPUT_SPANISH = ROOT / "output" / "spanish"
ARCHIVE_ROOT = ROOT / "memory" / "production_snapshot_archives"
REPORTS_DIR = ROOT / "reports"
MEMORY_DIR = ROOT / "memory"


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def assert_inside_workspace(path: Path) -> Path:
    resolved = path.resolve()
    root = ROOT.resolve()
    if resolved != root and root not in resolved.parents:
        raise RuntimeError(f"Path outside workspace: {resolved}")
    return resolved


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_index(root: Path) -> dict[str, dict[str, Any]]:
    if not root.exists():
        return {}
    index: dict[str, dict[str, Any]] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        stat = path.stat()
        index[relative] = {
            "size": stat.st_size,
            "sha256": file_sha256(path),
        }
    return index


def compare_indexes(left: dict[str, dict[str, Any]], right: dict[str, dict[str, Any]]) -> dict[str, Any]:
    left_keys = set(left)
    right_keys = set(right)
    missing_from_right = sorted(left_keys - right_keys)
    extra_in_right = sorted(right_keys - left_keys)
    changed = sorted(
        key for key in left_keys & right_keys
        if left[key]["sha256"] != right[key]["sha256"]
    )
    return {
        "left_count": len(left),
        "right_count": len(right),
        "missing_from_right_count": len(missing_from_right),
        "extra_in_right_count": len(extra_in_right),
        "changed_count": len(changed),
        "missing_from_right_sample": missing_from_right[:25],
        "extra_in_right_sample": extra_in_right[:25],
        "changed_sample": changed[:25],
    }


def zip_tree(source: Path, archive_path: Path) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(p for p in source.rglob("*") if p.is_file()):
            archive.write(path, path.relative_to(source).as_posix())


def clear_directory(path: Path) -> None:
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def build_summary(*, apply: bool) -> dict[str, Any]:
    source = assert_inside_workspace(SOURCE_MIRROR)
    target = assert_inside_workspace(OUTPUT_SPANISH)
    if not source.exists() or not source.is_dir():
        raise RuntimeError(f"Missing source mirror: {source}")
    if not target.exists() or not target.is_dir():
        raise RuntimeError(f"Missing output target: {target}")
    if rel(target) != "output\\spanish":
        raise RuntimeError(f"Unexpected target path: {target}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_before = tree_index(target)
    source_index = tree_index(source)
    before_vs_source = compare_indexes(source_index, output_before)
    archive_path = ARCHIVE_ROOT / f"output_spanish_broken_reference_{timestamp}.zip"

    summary: dict[str, Any] = {
        "schema_version": 1,
        "report_type": "pre_full_production_output_restore",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "apply" if apply else "dry_run",
        "source_mirror": rel(source),
        "output_target": rel(target),
        "archive_path": rel(archive_path),
        "before": {
            "source_file_count": len(source_index),
            "output_file_count": len(output_before),
            "source_vs_output": before_vs_source,
        },
        "guards": {
            "archive_created": False,
            "output_restored": False,
            "full_production": False,
            "reindex": False,
            "segment_state": False,
            "source_changed": False,
        },
    }
    if not apply:
        summary["recommendation"] = "Dry-run only. Rerun with --apply to archive output and restore it from source mirror."
        return summary

    zip_tree(target, archive_path)
    summary["guards"]["archive_created"] = archive_path.exists()
    clear_directory(target)
    shutil.copytree(source, target, dirs_exist_ok=True)

    output_after = tree_index(target)
    after_vs_source = compare_indexes(source_index, output_after)
    summary["after"] = {
        "output_file_count": len(output_after),
        "source_vs_output": after_vs_source,
        "restored_exactly": (
            after_vs_source["missing_from_right_count"] == 0
            and after_vs_source["extra_in_right_count"] == 0
            and after_vs_source["changed_count"] == 0
        ),
    }
    summary["guards"]["output_restored"] = bool(summary["after"]["restored_exactly"])
    summary["recommendation"] = (
        "Output restored from source mirror. Next step: run controlled full production from GUI."
        if summary["after"]["restored_exactly"]
        else "Output restore mismatch detected. Do not run full production before investigation."
    )
    return summary


def write_outputs(summary: dict[str, Any]) -> dict[str, str]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = REPORTS_DIR / f"{stamp}_pre_full_production_output_restore_{summary['mode']}"
    summary_path = base.with_name(base.name + "_summary.json")
    md_path = base.with_suffix(".md")
    latest_path = MEMORY_DIR / "pre_full_production_output_restore_latest.json"

    text = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    summary_path.write_text(text, encoding="utf-8")
    latest_path.write_text(text, encoding="utf-8")

    lines = [
        "# Pre-full Production Output Restore",
        "",
        f"- Mode: `{summary['mode']}`",
        f"- Source mirror: `{summary['source_mirror']}`",
        f"- Output target: `{summary['output_target']}`",
        f"- Archive path: `{summary['archive_path']}`",
        f"- Recommendation: {summary['recommendation']}",
        "",
        "## Before",
        "",
        f"- Source files: `{summary['before']['source_file_count']}`",
        f"- Output files: `{summary['before']['output_file_count']}`",
        f"- Changed vs source: `{summary['before']['source_vs_output']['changed_count']}`",
        f"- Missing vs source: `{summary['before']['source_vs_output']['missing_from_right_count']}`",
        f"- Extra vs source: `{summary['before']['source_vs_output']['extra_in_right_count']}`",
        "",
    ]
    if "after" in summary:
        lines.extend([
            "## After",
            "",
            f"- Output files: `{summary['after']['output_file_count']}`",
            f"- Restored exactly: `{str(summary['after']['restored_exactly']).lower()}`",
            f"- Changed vs source: `{summary['after']['source_vs_output']['changed_count']}`",
            f"- Missing vs source: `{summary['after']['source_vs_output']['missing_from_right_count']}`",
            f"- Extra vs source: `{summary['after']['source_vs_output']['extra_in_right_count']}`",
            "",
        ])
    lines.extend(["## Guards", ""])
    for key, value in summary["guards"].items():
        lines.append(f"- `{key}`: `{value}`")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"summary": str(summary_path), "markdown": str(md_path), "latest": str(latest_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive current output and restore output/spanish from source/spanish_source.")
    parser.add_argument("--apply", action="store_true", help="Perform archive and restore. Without this flag, runs dry-run only.")
    args = parser.parse_args()
    summary = build_summary(apply=args.apply)
    outputs = write_outputs(summary)
    print(json.dumps({"summary": summary, "outputs": outputs}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
