from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import db


RULE_VERSION = "ml_export_model_v1"
MODEL_KIND = "risk_action_classifier"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_model(conn, model_run_id: int | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if model_run_id is None:
        row = conn.execute(
            """
            SELECT runs.*, registry.promoted_at, registry.policy_version AS registry_policy_version
            FROM ml_model_registry registry
            JOIN ml_model_runs runs ON runs.id = registry.active_model_run_id
            WHERE registry.model_kind = ?
            LIMIT 1
            """,
            (MODEL_KIND,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                SELECT *
                FROM ml_model_runs
                WHERE model_kind = ?
                  AND model_path IS NOT NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (MODEL_KIND,),
            ).fetchone()
        registry = conn.execute(
            """
            SELECT *
            FROM ml_model_registry
            WHERE model_kind = ?
            """,
            (MODEL_KIND,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT *
            FROM ml_model_runs
            WHERE id = ?
            """,
            (model_run_id,),
        ).fetchone()
        registry = None
    if row is None:
        raise RuntimeError("No model found to export.")
    return dict(row), dict(registry) if registry else None


def fetch_dataset(conn, dataset_run_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM ml_dataset_runs
        WHERE id = ?
        """,
        (dataset_run_id,),
    ).fetchone()
    return dict(row) if row else None


def fetch_latest_score(conn, model_run_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM ml_score_runs
        WHERE model_run_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (model_run_id,),
    ).fetchone()
    return dict(row) if row else None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def readme_text(manifest: dict[str, Any]) -> str:
    model = manifest["model"]
    dataset = manifest.get("dataset") or {}
    score = manifest.get("latest_score") or {}
    return "\n".join(
        [
            f"# Exported ML Model: {model['model_version']}",
            "",
            "This export contains the active/local CK3 PT-BR risk classifier model.",
            "",
            "## Contents",
            "",
            "- `model.joblib`: serialized scikit-learn pipeline.",
            "- `manifest.json`: model, dataset, registry and checksum metadata.",
            "- `metrics.json`: compact metrics for quick inspection.",
            "- `README.md`: this file.",
            "",
            "## Model",
            "",
            f"- Model run id: {model['id']}",
            f"- Model kind: {model['model_kind']}",
            f"- Version: {model['model_version']}",
            f"- Dataset run id: {model['dataset_run_id']}",
            f"- False safe: {model['false_safe_count']}",
            f"- False safe rate: {model['false_safe_rate']}",
            f"- Safe precision: {model['safe_precision']}",
            f"- Safe recall: {model['safe_recall']}",
            f"- Macro F1: {model['macro_f1']}",
            "",
            "## Dataset",
            "",
            f"- Total examples: {dataset.get('total_count', 'unknown')}",
            f"- Positive examples: {dataset.get('positive_count', 'unknown')}",
            f"- Negative examples: {dataset.get('negative_count', 'unknown')}",
            f"- Strong negative examples: {dataset.get('strong_negative_count', 'unknown')}",
            "",
            "## Latest Score",
            "",
            f"- Score run id: {score.get('id', 'none')}",
            f"- Scored segments: {score.get('scored_count', 'unknown')}",
            f"- Final auto safe: {score.get('final_auto_safe_count', 'unknown')}",
            f"- Needs human: {score.get('needs_human_count', 'unknown')}",
            f"- Blocked structure: {score.get('blocked_structure_count', 'unknown')}",
            "",
            "## Restore Notes",
            "",
            "Copy `model.joblib` into `memory/models/` or keep the exported directory as a backup artifact.",
            "The SQLite registry is not modified by this export. Use the project scripts to promote a model again after restore.",
            "",
        ]
    )


def main(model_run_id: int | None = None, export_root: str | None = None) -> None:
    settings = db.load_settings()
    started_at = datetime.now()
    print("[ml_export_model] Starting model export")
    print(f"[ml_export_model] Rule version: {RULE_VERSION}")

    with db.connect(settings) as conn:
        db.ensure_database(conn)
        model, registry = fetch_model(conn, model_run_id)
        dataset = fetch_dataset(conn, int(model["dataset_run_id"]))
        latest_score = fetch_latest_score(conn, int(model["id"]))

    source_model_path = db.project_path(model["model_path"])
    if not source_model_path.exists():
        raise RuntimeError(f"Model file does not exist: {source_model_path}")

    base_export_root = db.project_path(export_root or settings.get("model_exports_dir", "memory/model_exports"))
    export_dir = base_export_root / model["model_version"]
    export_dir.mkdir(parents=True, exist_ok=True)
    exported_model_path = export_dir / "model.joblib"
    shutil.copy2(source_model_path, exported_model_path)

    checksum = sha256(exported_model_path)
    manifest = {
        "rule_version": RULE_VERSION,
        "exported_at": now(),
        "project_root": str(db.PROJECT_ROOT),
        "model": {
            key: model.get(key)
            for key in [
                "id",
                "model_version",
                "model_kind",
                "dataset_run_id",
                "model_path",
                "training_examples",
                "test_examples",
                "safe_threshold",
                "accuracy",
                "macro_f1",
                "predicted_safe_count",
                "false_safe_count",
                "false_safe_rate",
                "safe_precision",
                "safe_recall",
                "started_at",
                "finished_at",
            ]
        },
        "registry": registry,
        "dataset": dataset,
        "latest_score": latest_score,
        "artifact": {
            "file": "model.joblib",
            "size_bytes": exported_model_path.stat().st_size,
            "sha256": checksum,
        },
    }
    metrics = {
        "model_version": model["model_version"],
        "model_run_id": model["id"],
        "dataset_run_id": model["dataset_run_id"],
        "false_safe_count": model["false_safe_count"],
        "false_safe_rate": model["false_safe_rate"],
        "safe_precision": model["safe_precision"],
        "safe_recall": model["safe_recall"],
        "macro_f1": model["macro_f1"],
        "accuracy": model["accuracy"],
        "latest_score": latest_score,
    }

    write_json(export_dir / "manifest.json", manifest)
    write_json(export_dir / "metrics.json", metrics)
    (export_dir / "README.md").write_text(readme_text(manifest), encoding="utf-8")

    elapsed = datetime.now() - started_at
    report_lines = [
        "ML model export report",
        f"Started at: {started_at.isoformat(timespec='seconds')}",
        f"Elapsed: {elapsed}",
        f"Rule version: {RULE_VERSION}",
        "",
        f"Model run id: {model['id']}",
        f"Model version: {model['model_version']}",
        f"Source model: {source_model_path}",
        f"Export directory: {export_dir}",
        f"Artifact sha256: {checksum}",
        f"Artifact size bytes: {exported_model_path.stat().st_size}",
        "",
        "Files:",
        f"- {exported_model_path}",
        f"- {export_dir / 'manifest.json'}",
        f"- {export_dir / 'metrics.json'}",
        f"- {export_dir / 'README.md'}",
    ]
    report_path = db.write_report(settings, "ml_export_model", report_lines)

    print(f"[ml_export_model] Model: {model['model_version']}")
    print(f"[ml_export_model] Export directory: {export_dir}")
    print(f"[ml_export_model] SHA256: {checksum}")
    print(f"[ml_export_model] Report: {report_path}")
    print("[ml_export_model] Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export active ML model artifact with metadata.")
    parser.add_argument("--model-run-id", type=int, default=None)
    parser.add_argument("--export-root", default=None)
    args = parser.parse_args()
    main(model_run_id=args.model_run_id, export_root=args.export_root)
