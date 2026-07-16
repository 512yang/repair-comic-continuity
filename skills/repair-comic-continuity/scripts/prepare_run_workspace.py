"""Create a clean, byte-copied production run root from an immutable source project."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from pipeline_version import ORCHESTRATION_PIPELINE_ID
from project_common import discover_project, sha256_file, sorted_input_pages


def _is_within(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _copy_file(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    source_hash = sha256_file(source)
    if sha256_file(destination) != source_hash:
        raise ValueError(f"staged copy hash mismatch: {source}")
    return {"sha256": source_hash, "size": source.stat().st_size}


def prepare_run_workspace(source_root: Path, target_root: Path) -> dict[str, Any]:
    """Copy only immutable inputs into a new clean run root.

    Old outputs, candidates, work directories, and evidence are intentionally excluded.
    """
    project = discover_project(Path(source_root))
    source = project.root.resolve()
    target = Path(target_root).expanduser().resolve()
    if target == source or _is_within(source, target):
        raise ValueError("target run root must be outside source project")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError("target run root must be empty or absent")

    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".{target.name}.prepare-{uuid.uuid4().hex}"
    if stage.exists():
        raise ValueError(f"temporary staging path already exists: {stage}")
    stage.mkdir()
    try:
        novel_meta = _copy_file(project.novel, stage / project.novel.name)
        reference_rows: list[dict[str, Any]] = []
        for path in sorted(project.references.rglob("*")):
            if not path.is_file():
                continue
            if path.is_symlink():
                raise ValueError(f"reference symlink is not allowed: {path}")
            relative = path.relative_to(project.references)
            meta = _copy_file(path, stage / "人物参考图" / relative)
            reference_rows.append({"path": f"人物参考图/{relative.as_posix()}", **meta})

        input_rows: list[dict[str, Any]] = []
        for path in sorted_input_pages(project.input_dir):
            if path.is_symlink():
                raise ValueError(f"input symlink is not allowed: {path}")
            relative = path.relative_to(project.input_dir)
            meta = _copy_file(path, stage / "输入" / relative)
            input_rows.append({"path": f"输入/{relative.as_posix()}", **meta})

        (stage / "输出").mkdir()
        (stage / "evidence").mkdir()
        manifest = {
            "schema_version": "1.0",
            "pipeline_id": ORCHESTRATION_PIPELINE_ID,
            "status": "prepared",
            "source_root": str(source),
            "novel": {"path": project.novel.name, **novel_meta},
            "references": reference_rows,
            "inputs": input_rows,
            "input_count": len(input_rows),
        }
        manifest_path = stage / "run_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with manifest_path.open("r+b") as handle:
            os.fsync(handle.fileno())
        if target.exists():
            target.rmdir()
        os.replace(stage, target)
        return manifest
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--target-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = prepare_run_workspace(args.source_root, args.target_root)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "prepare_failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
