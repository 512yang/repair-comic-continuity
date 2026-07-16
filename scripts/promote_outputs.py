"""Atomically promote verified per-page release bindings into final output."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


SCHEMA_VERSION = "continuity_v5_release_bindings_v1"
PAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
PAGE_CLASSES = frozenset({"unchanged", "text_only", "full_page_redraw"})
DOCUMENT_FIELDS = frozenset(
    {"schema_version", "status", "expected_count", "pages"}
)
PAGE_FIELDS = frozenset(
    {
        "relative_path",
        "page_class",
        "source_sha256",
        "candidate_path",
        "candidate_sha256",
        "task_id",
        "status",
    }
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA256 digest")
    return value


def _relative_path(value: object, label: str) -> tuple[str, PurePosixPath]:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError(f"unsafe {label}")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or pure.as_posix() != value
        or any(part in {"", ".", ".."} or ":" in part for part in pure.parts)
    ):
        raise ValueError(f"unsafe {label}: {value!r}")
    return value, pure


def _safe_file(root: Path, relative: PurePosixPath, label: str) -> Path:
    path = root.joinpath(*relative.parts).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"unsafe {label}: path escapes its root") from exc
    if not path.is_file():
        raise ValueError(f"{label} file missing: {relative.as_posix()}")
    return path


def _load_document(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"release bindings unreadable: {path}") from exc
    if not isinstance(document, dict) or set(document) != DOCUMENT_FIELDS:
        raise ValueError("release binding document fields are invalid")
    if document["schema_version"] != SCHEMA_VERSION:
        raise ValueError("release binding schema_version is invalid")
    if document["status"] != "verified":
        raise ValueError("release binding document is not verified")
    expected_count = document["expected_count"]
    if (
        not isinstance(expected_count, int)
        or isinstance(expected_count, bool)
        or expected_count < 1
    ):
        raise ValueError("release binding expected_count must be a positive integer")
    if not isinstance(document["pages"], list):
        raise ValueError("release binding pages must be a list")
    return document


def _inventory_input(input_dir: Path) -> dict[str, Path]:
    pages: dict[str, Path] = {}
    normalized_paths: dict[str, str] = {}
    for path in sorted(
        (candidate for candidate in input_dir.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.relative_to(input_dir).as_posix(),
    ):
        relative = path.relative_to(input_dir).as_posix()
        if path.suffix.casefold() not in PAGE_EXTENSIONS:
            continue
        normalized = unicodedata.normalize("NFC", relative).casefold()
        if normalized in normalized_paths:
            raise ValueError(
                "duplicate Unicode path in input: "
                f"{normalized_paths[normalized]!r} and {relative!r}"
            )
        normalized_paths[normalized] = relative
        resolved = path.resolve()
        try:
            resolved.relative_to(input_dir)
        except ValueError as exc:
            raise ValueError(f"unsafe input path: {relative!r}") from exc
        pages[relative] = resolved
    return pages


def _validate_pages(
    document: Mapping[str, Any], input_pages: Mapping[str, Path]
) -> list[dict[str, Any]]:
    expected_count = document["expected_count"]
    raw_pages = document["pages"]
    validated: list[dict[str, Any]] = []
    exact_paths: dict[str, dict[str, Any]] = {}
    normalized_paths: dict[str, str] = {}

    for index, raw in enumerate(raw_pages, start=1):
        if not isinstance(raw, dict) or set(raw) != PAGE_FIELDS:
            raise ValueError(f"release binding page fields are invalid at index {index}")
        if raw["status"] != "verified":
            raise ValueError(f"release binding page {index} is not verified")
        relative_text, relative = _relative_path(raw["relative_path"], "relative_path")
        page_class = raw["page_class"]
        if page_class not in PAGE_CLASSES:
            raise ValueError(f"page_class is not promotable at {relative_text!r}")
        if relative.suffix.casefold() not in PAGE_EXTENSIONS:
            raise ValueError(f"unsupported output extension at {relative_text!r}")

        previous = exact_paths.get(relative_text)
        if previous is not None:
            if previous["task_id"] != raw["task_id"]:
                raise ValueError(f"competing tasks target page {relative_text!r}")
            raise ValueError(f"duplicate relative path: {relative_text!r}")
        normalized = unicodedata.normalize("NFC", relative_text).casefold()
        if normalized in normalized_paths:
            raise ValueError(
                "duplicate Unicode path: "
                f"{normalized_paths[normalized]!r} and {relative_text!r}"
            )
        normalized_paths[normalized] = relative_text
        exact_paths[relative_text] = raw

        source_hash = _sha256(raw["source_sha256"], "source_sha256")
        if page_class == "unchanged":
            if any(
                raw[field] is not None
                for field in ("candidate_path", "candidate_sha256", "task_id")
            ):
                raise ValueError(
                    f"unchanged page must bind only its source: {relative_text!r}"
                )
            candidate_relative = None
            candidate_hash = None
            task_id = None
        else:
            task_id = raw["task_id"]
            if not isinstance(task_id, str) or not task_id.strip():
                raise ValueError(f"task_id missing at {relative_text!r}")
            _, candidate_relative = _relative_path(
                raw["candidate_path"], "candidate_path"
            )
            candidate_hash = _sha256(
                raw["candidate_sha256"], "candidate_sha256"
            )

        validated.append(
            {
                "relative_path": relative_text,
                "relative": relative,
                "page_class": page_class,
                "source_sha256": source_hash,
                "candidate_relative": candidate_relative,
                "candidate_sha256": candidate_hash,
                "task_id": task_id,
            }
        )

    binding_paths = set(exact_paths)
    input_paths = set(input_pages)
    if (
        expected_count != len(raw_pages)
        or expected_count != len(input_pages)
        or binding_paths != input_paths
    ):
        missing = sorted(input_paths - binding_paths)
        unexpected = sorted(binding_paths - input_paths)
        raise ValueError(
            "release bindings must equal the exact input path set and total count; "
            f"expected_count={expected_count}, bindings={len(raw_pages)}, "
            f"inputs={len(input_pages)}, missing={missing}, unexpected={unexpected}"
        )
    return validated


class _PromotionLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.file_descriptor: int | None = None

    def __enter__(self) -> "_PromotionLock":
        try:
            self.file_descriptor = os.open(
                self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except FileExistsError as exc:
            raise RuntimeError(f"promotion lock already held: {self.path}") from exc
        payload = json.dumps({"pid": os.getpid()}, separators=(",", ":")).encode(
            "ascii"
        )
        os.write(self.file_descriptor, payload)
        os.fsync(self.file_descriptor)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.file_descriptor is not None:
            os.close(self.file_descriptor)
            self.file_descriptor = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _validate_root_layout(input_dir: Path, candidate_dir: Path, output_dir: Path) -> None:
    for root, label in ((input_dir, "input"), (candidate_dir, "candidate")):
        if not root.is_dir():
            raise ValueError(f"{label} directory missing: {root}")
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError(f"output path is not a directory: {output_dir}")
    for protected, label in ((input_dir, "input"), (candidate_dir, "candidate")):
        if output_dir == protected or output_dir in protected.parents or protected in output_dir.parents:
            raise ValueError(f"output directory overlaps {label} directory")


def _build_staging(
    staging: Path,
    pages: list[dict[str, Any]],
    input_pages: Mapping[str, Path],
    candidate_dir: Path,
) -> None:
    staged_paths: set[str] = set()
    for page in pages:
        relative_text = page["relative_path"]
        source = input_pages[relative_text]
        if _sha256_file(source) != page["source_sha256"]:
            raise ValueError(f"source SHA256 mismatch at {relative_text!r}")

        if page["page_class"] == "unchanged":
            selected = source
            expected_hash = page["source_sha256"]
        else:
            selected = _safe_file(
                candidate_dir, page["candidate_relative"], "candidate"
            )
            expected_hash = page["candidate_sha256"]
            if _sha256_file(selected) != expected_hash:
                raise ValueError(f"candidate SHA256 mismatch at {relative_text!r}")

        target = staging.joinpath(*page["relative"].parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(selected, target)
        if _sha256_file(target) != expected_hash:
            raise ValueError(f"staged SHA256 mismatch at {relative_text!r}")
        staged_paths.add(target.relative_to(staging).as_posix())

    expected_paths = {page["relative_path"] for page in pages}
    actual_paths = {
        path.relative_to(staging).as_posix()
        for path in staging.rglob("*")
        if path.is_file()
    }
    if staged_paths != expected_paths or actual_paths != expected_paths:
        raise ValueError("staging does not preserve the exact output path set")


def _unique_backup_path(output_dir: Path) -> Path:
    backup = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.backup-", dir=output_dir.parent)
    )
    backup.rmdir()
    return backup


def _swap_output(staging: Path, output_dir: Path) -> None:
    backup: Path | None = None
    if output_dir.exists():
        backup = _unique_backup_path(output_dir)
        os.replace(output_dir, backup)
    try:
        os.replace(staging, output_dir)
    except BaseException:
        if backup is not None:
            try:
                os.replace(backup, output_dir)
            except BaseException as rollback_error:
                raise RuntimeError(
                    "promotion failed and existing output rollback also failed"
                ) from rollback_error
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def promote_outputs(
    input_dir: Path,
    candidate_dir: Path,
    output_dir: Path,
    release_bindings_path: Path,
) -> dict[str, Any]:
    """Promote one verified, exact binding for every input page.

    All validation and copying happens in a sibling staging directory while an
    exclusive single-writer lock is held. The existing output is restored if
    the final directory swap fails.
    """

    input_root = Path(input_dir).expanduser().resolve()
    candidate_root = Path(candidate_dir).expanduser().resolve()
    output_root = Path(output_dir).expanduser().resolve()
    binding_path = Path(release_bindings_path).expanduser().resolve()
    output_root.parent.mkdir(parents=True, exist_ok=True)
    _validate_root_layout(input_root, candidate_root, output_root)
    document = _load_document(binding_path)
    lock_path = output_root.parent / f".{output_root.name}.promote.lock"

    with _PromotionLock(lock_path):
        input_pages = _inventory_input(input_root)
        pages = _validate_pages(document, input_pages)
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{output_root.name}.staging-", dir=output_root.parent
            )
        )
        try:
            _build_staging(staging, pages, input_pages, candidate_root)
            _swap_output(staging, output_root)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "promoted",
        "promoted_count": len(pages),
        "output_dir": str(output_root),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--candidate-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--release-bindings", type=Path, required=True)
    args = parser.parse_args(argv)
    result = promote_outputs(
        args.input_dir,
        args.candidate_dir,
        args.output_dir,
        args.release_bindings,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
