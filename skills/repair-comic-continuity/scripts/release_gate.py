"""Fail-closed release validation for hash-bound comic repair outputs."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Mapping

from candidate_preflight import (
    candidate_is_finally_eligible,
    validate_preflight_report,
    validate_review,
)
from closed_loop_controller import (
    build_annotation_binding,
    validate_annotation_binding,
    validate_annotation_review,
)
from pipeline_contracts import normalize_relative_image_path
from project_common import (
    discover_project,
    ensure_safe_subpath,
    sha256_file,
    sorted_input_pages,
)
from validate_appearance_matrix import validate_matrix
from validate_output import validate_project
from validate_source_text_audit import validate_source_text_audit
from validate_human_issue_annotations import validate_human_issue_annotations
from validate_human_visual_selection import validate_human_visual_selection


SCHEMA_VERSION = "1.1"
RELEASABLE_PAGE_CLASSES = frozenset(
    {"unchanged", "text_only", "full_page_redraw"}
)
PAGE_BINDING_KEYS = frozenset(
    {
        "relative_path",
        "page_class",
        "input_sha256",
        "appearance_matrix",
        "source_text_audit",
        "preflight",
        "review",
        "candidate",
        "output_sha256",
    }
)
ARTIFACT_KEYS = frozenset({"path", "sha256"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _add(errors: list[str], code: str) -> None:
    if code not in errors:
        errors.append(code)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON artifact must be an object")
    return value


def _relative_project_path(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("artifact path must be a nonempty string")
    normalized = value.replace("\\", "/")
    components = normalized.split("/")
    if (
        normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(component in {"", ".", ".."} for component in components)
    ):
        raise ValueError("artifact path must be a safe project-relative path")
    return ensure_safe_subpath(root, Path(*components))


def _bound_file(
    root: Path, value: object, *, code: str, errors: list[str]
) -> tuple[Path, str] | None:
    try:
        if not isinstance(value, Mapping) or set(value) != ARTIFACT_KEYS:
            raise ValueError("artifact binding must contain exactly path and sha256")
        digest = value.get("sha256")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise ValueError("artifact sha256 is invalid")
        path = _relative_project_path(root, value.get("path"))
        if not path.is_file() or sha256_file(path) != digest:
            raise ValueError("artifact current hash does not match binding")
        return path, digest
    except (OSError, UnicodeError, ValueError):
        _add(errors, code)
        return None


def _validate_matrix_files(
    root: Path, matrix: Mapping[str, Any], errors: list[str]
) -> None:
    for character in matrix.get("characters", []):
        if not isinstance(character, Mapping):
            _add(errors, "APPEARANCE_MATRIX_INVALID")
            continue
        _bound_file(
            root,
            character.get("reference"),
            code="APPEARANCE_MATRIX_INVALID",
            errors=errors,
        )
        for observation in character.get("observations", []):
            if not isinstance(observation, Mapping):
                _add(errors, "APPEARANCE_MATRIX_INVALID")
                continue
            _bound_file(
                root,
                observation.get("evidence"),
                code="APPEARANCE_MATRIX_INVALID",
                errors=errors,
            )


def validate_annotation_release_binding(
    *,
    expected_binding: Mapping[str, Any],
    annotation_review: Mapping[str, Any],
    preflight: Mapping[str, Any],
    main_review: Mapping[str, Any],
    candidate_sha256: str,
) -> bool:
    """Prove that an annotated repair stayed bound through prompt and review."""
    expected = validate_annotation_binding(expected_binding)
    redraw_request = preflight.get("redraw_request")
    declaration = (
        redraw_request.get("declaration")
        if isinstance(redraw_request, Mapping)
        else None
    )
    actual = (
        declaration.get("human_issue_binding")
        if isinstance(declaration, Mapping)
        else None
    )
    if actual is None:
        raise ValueError("prompt request is missing the human issue binding")
    if validate_annotation_binding(actual) != expected:
        raise ValueError("prompt request human issue binding mismatch")
    reviewed = validate_annotation_review(annotation_review, binding=expected)
    if reviewed["candidate_sha256"] != candidate_sha256:
        raise ValueError("annotation review candidate hash mismatch")
    if reviewed["preflight_id"] != preflight.get("preflight_id"):
        raise ValueError("annotation review preflight binding mismatch")
    if (
        reviewed["generator"] != main_review.get("generator")
        or reviewed["reviewer"] != main_review.get("reviewer")
    ):
        raise ValueError("annotation review actor binding mismatch")
    return True


def _sealed_inputs(
    manifest: Mapping[str, Any], input_names: list[str], input_hashes: dict[str, str]
) -> dict[str, str] | None:
    rows = manifest.get("sealed_inputs")
    if not isinstance(rows, list) or len(rows) != len(input_names):
        return None
    sealed: dict[str, str] = {}
    ordered_names: list[str] = []
    try:
        for row in rows:
            if not isinstance(row, Mapping) or set(row) != {
                "relative_path",
                "sha256",
            }:
                return None
            name = normalize_relative_image_path(row.get("relative_path"))
            digest = row.get("sha256")
            if (
                not isinstance(digest, str)
                or _SHA256_RE.fullmatch(digest) is None
                or name in sealed
            ):
                return None
            ordered_names.append(name)
            sealed[name] = digest
    except ValueError:
        return None
    if ordered_names != input_names:
        return None
    if any(sealed[name] != input_hashes[name] for name in input_names):
        return None
    return sealed


def _output_names(output_dir: Path) -> list[str]:
    return sorted(
        path.relative_to(output_dir).as_posix()
        for path in output_dir.rglob("*")
        if path.is_file()
    )


def validate_release_project(
    root: Path,
    evidence_dir: Path,
    release_manifest: Path,
    expected_count: int | None = None,
) -> dict[str, Any]:
    """Validate all current release artifacts without writing project state."""
    errors: list[str] = []
    counts = {"input": 0, "output": 0, "pages": 0}

    final_result = validate_project(root, evidence_dir, expected_count)
    if not final_result.get("ok"):
        _add(errors, "FINAL_VALIDATION_FAILED")

    try:
        project = discover_project(root)
        manifest_path = ensure_safe_subpath(project.root, release_manifest)
        manifest = _load_json(manifest_path)
        input_pages = sorted_input_pages(project.input_dir)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        _add(errors, "RELEASE_MANIFEST_INVALID")
        return {"ok": False, "errors": errors, "counts": counts}

    input_names = [
        page.relative_to(project.input_dir).as_posix() for page in input_pages
    ]
    input_hashes = {
        name: sha256_file(page) for name, page in zip(input_names, input_pages)
    }
    expected_annotation_bindings: dict[str, dict[str, Any]] = {}
    try:
        evidence_root = Path(evidence_dir).resolve()
        evidence_root.relative_to(project.root.resolve())
        annotation_manifest_path = evidence_root / "human_issue_annotations.json"
        if annotation_manifest_path.is_file():
            selection_path = evidence_root / "human_visual_selection.json"
            if not selection_path.is_file():
                raise ValueError("human visual selection is missing")
            selection = validate_human_visual_selection(
                _load_json(selection_path), project.input_dir, input_names
            )
            annotation_document = _load_json(annotation_manifest_path)
            annotations = validate_human_issue_annotations(
                annotation_document,
                project.input_dir,
                input_names,
                selection["selected_pages"],
            )
            manifest_relative = annotation_manifest_path.relative_to(
                project.root
            ).as_posix()
            manifest_hash = sha256_file(annotation_manifest_path)
            for page_name in annotations["annotated_pages"]:
                expected_annotation_bindings[page_name] = build_annotation_binding(
                    annotation_document,
                    page_path=page_name,
                    source_sha256=input_hashes[page_name],
                    manifest_path=manifest_relative,
                    manifest_sha256=manifest_hash,
                )
    except (OSError, UnicodeError, ValueError, KeyError, json.JSONDecodeError):
        _add(errors, "ANNOTATION_MANIFEST_INVALID")
        evidence_root = Path(evidence_dir).resolve()
    actual_outputs = _output_names(project.output_dir)
    counts.update(
        input=len(input_names), output=len(actual_outputs), pages=len(manifest.get("pages", []))
        if isinstance(manifest.get("pages"), list)
        else 0,
    )

    if set(manifest) != {"schema_version", "sealed_inputs", "pages"} or manifest.get(
        "schema_version"
    ) != SCHEMA_VERSION:
        _add(errors, "RELEASE_MANIFEST_INVALID")

    sealed = _sealed_inputs(manifest, input_names, input_hashes)
    if sealed is None:
        _add(errors, "SEALED_INPUT_MISMATCH")
        sealed = {}

    if actual_outputs != sorted(input_names):
        _add(errors, "OUTPUT_NAME_SET_MISMATCH")

    page_rows = manifest.get("pages")
    if not isinstance(page_rows, list) or len(page_rows) != len(input_names):
        _add(errors, "PAGE_BINDING_INVALID")
        page_rows = page_rows if isinstance(page_rows, list) else []

    matrix_coverage: dict[Path, tuple[set[str], set[str]]] = {}
    seen_pages: list[str] = []
    for row in page_rows:
        if not isinstance(row, Mapping) or set(row) != PAGE_BINDING_KEYS:
            _add(errors, "PAGE_BINDING_INVALID")
            continue
        try:
            name = normalize_relative_image_path(row.get("relative_path"))
        except ValueError:
            _add(errors, "PAGE_BINDING_INVALID")
            continue
        seen_pages.append(name)
        page_class = row.get("page_class")
        if page_class not in RELEASABLE_PAGE_CLASSES:
            _add(errors, "PAGE_BINDING_INVALID")

        sealed_hash = sealed.get(name)
        if (
            sealed_hash is None
            or row.get("input_sha256") != sealed_hash
            or input_hashes.get(name) != sealed_hash
        ):
            _add(errors, "SEALED_INPUT_MISMATCH")

        output = project.output_dir / Path(name)
        output_hash = sha256_file(output) if output.is_file() else None
        if output_hash != row.get("output_sha256"):
            _add(errors, "OUTPUT_HASH_MISMATCH")
        if page_class == "unchanged" and output_hash != sealed_hash:
            _add(errors, "UNCHANGED_SOURCE_MISMATCH")

        candidate_binding = _bound_file(
            project.root,
            row.get("candidate"),
            code="CANDIDATE_HASH_MISMATCH",
            errors=errors,
        )
        candidate_path = candidate_binding[0] if candidate_binding else None
        candidate_hash = candidate_binding[1] if candidate_binding else None
        if output_hash is not None and candidate_hash is not None and output_hash != candidate_hash:
            _add(errors, "OUTPUT_CANDIDATE_HASH_MISMATCH")

        matrix_binding = _bound_file(
            project.root,
            row.get("appearance_matrix"),
            code="APPEARANCE_MATRIX_INVALID",
            errors=errors,
        )
        if matrix_binding is not None:
            matrix_path = matrix_binding[0]
            try:
                matrix = validate_matrix(_load_json(matrix_path))
                if matrix.get("status") != "confirmed" or name not in matrix.get(
                    "cluster_pages", []
                ):
                    raise ValueError("matrix does not confirm this page")
                _validate_matrix_files(project.root, matrix, errors)
                declared, referenced = matrix_coverage.setdefault(
                    matrix_path,
                    (set(matrix["cluster_pages"]), set()),
                )
                referenced.add(name)
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                _add(errors, "APPEARANCE_MATRIX_INVALID")

        source_text_binding = _bound_file(
            project.root,
            row.get("source_text_audit"),
            code="SOURCE_TEXT_AUDIT_INVALID",
            errors=errors,
        )
        if source_text_binding is not None:
            try:
                source_text_audit = validate_source_text_audit(
                    _load_json(source_text_binding[0]), project.root
                )
                expected_source = (project.input_dir / Path(name)).resolve()
                bound_source = _relative_project_path(
                    project.root, source_text_audit["page"]["path"]
                )
                if (
                    source_text_audit.get("status") != "confirmed"
                    or bound_source.resolve() != expected_source
                    or source_text_audit["page"]["sha256"] != sealed_hash
                ):
                    raise ValueError("source text audit does not confirm current source page")
            except (OSError, UnicodeError, ValueError, KeyError, json.JSONDecodeError):
                _add(errors, "SOURCE_TEXT_AUDIT_INVALID")

        preflight = None
        preflight_binding = _bound_file(
            project.root,
            row.get("preflight"),
            code="PREFLIGHT_INVALID",
            errors=errors,
        )
        if preflight_binding is not None:
            try:
                preflight = _load_json(preflight_binding[0])
                validate_preflight_report(preflight, verify_files=True)
                expected_source = (project.input_dir / Path(name)).resolve()
                if (
                    preflight.get("page_class") != page_class
                    or Path(preflight["paths"]["original"]).resolve() != expected_source
                    or candidate_path is None
                    or Path(preflight["paths"]["candidate"]).resolve()
                    != candidate_path.resolve()
                    or preflight["hashes"]["original"] != sealed_hash
                    or preflight["hashes"]["candidate"] != candidate_hash
                ):
                    raise ValueError("preflight release binding mismatch")
            except (OSError, UnicodeError, ValueError, KeyError, json.JSONDecodeError):
                _add(errors, "PREFLIGHT_INVALID")
                preflight = None

        review = None
        review_binding = _bound_file(
            project.root,
            row.get("review"),
            code="REVIEW_INVALID",
            errors=errors,
        )
        if review_binding is not None and preflight is not None:
            try:
                review = _load_json(review_binding[0])
                validate_review(review, preflight)
                if not candidate_is_finally_eligible(preflight, review):
                    raise ValueError("candidate review is not accepted")
                if review.get("candidate_hash") != candidate_hash:
                    raise ValueError("review candidate hash mismatch")
            except (OSError, UnicodeError, ValueError, KeyError, json.JSONDecodeError):
                _add(errors, "REVIEW_INVALID")
        elif review_binding is not None:
            _add(errors, "REVIEW_INVALID")

        expected_annotation = expected_annotation_bindings.get(name)
        actual_annotation = None
        if preflight is not None:
            redraw_request = preflight.get("redraw_request")
            declaration = (
                redraw_request.get("declaration")
                if isinstance(redraw_request, Mapping)
                else None
            )
            if isinstance(declaration, Mapping):
                actual_annotation = declaration.get("human_issue_binding")
        if expected_annotation is not None:
            try:
                if preflight is None or review is None or candidate_hash is None:
                    raise ValueError("annotated page release evidence is incomplete")
                annotation_review_path = (
                    evidence_root / "annotation_reviews" / Path(name + ".json")
                )
                if not annotation_review_path.is_file():
                    raise ValueError("annotation review artifact is missing")
                validate_annotation_release_binding(
                    expected_binding=expected_annotation,
                    annotation_review=_load_json(annotation_review_path),
                    preflight=preflight,
                    main_review=review,
                    candidate_sha256=candidate_hash,
                )
            except (OSError, UnicodeError, ValueError, KeyError, json.JSONDecodeError):
                _add(errors, "ANNOTATION_RELEASE_BINDING_INVALID")
        elif actual_annotation is not None:
            _add(errors, "ANNOTATION_RELEASE_BINDING_INVALID")

    if seen_pages != input_names or len(set(seen_pages)) != len(seen_pages):
        _add(errors, "PAGE_BINDING_INVALID")
    for declared_pages, referenced_pages in matrix_coverage.values():
        if declared_pages != referenced_pages:
            _add(errors, "APPEARANCE_MATRIX_INVALID")

    return {"ok": not errors, "errors": errors, "counts": counts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--release-manifest", type=Path, required=True)
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = validate_release_project(
            args.root,
            args.evidence_dir,
            args.release_manifest,
            args.expected_count,
        )
    except (OSError, ValueError) as exc:
        result = {
            "ok": False,
            "errors": [str(exc)],
            "counts": {"input": 0, "output": 0, "pages": 0},
        }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["ok"]:
        print(f"PASS: {result['counts']['output']} release pages")
    else:
        print("FAIL")
        for error in result["errors"]:
            print(f"- {error}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
