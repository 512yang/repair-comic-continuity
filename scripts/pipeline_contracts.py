"""Shared deterministic contracts for the comic continuity pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from pathlib import PurePath
from typing import Any, Iterable, Mapping


PAGE_CLASSES = frozenset(
    {"unchanged", "text_only", "full_page_redraw", "evidence_blocked"}
)
TASK_STATES = frozenset({"queued", "leased", "completed", "failed"})
ALIGNMENT_STATES = frozenset({"unresolved", "provisional", "confirmed"})
REVIEW_STATES = frozenset(
    {"pending", "machine_passed", "independent_passed", "rejected"}
)

_PAGE_ID_PATTERN = re.compile(r"[A-Za-z0-9]+(?:\([0-9]+\))?\Z")
_INPUT_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
# Task 3 must remove the integer overload and this marker when manifest
# initialization supplies source-relative paths directly.
REMOVE_LEGACY_INT_OUTPUT_NAMES_IN_TASK_3 = True


def normalize_page_id(value: object) -> str:
    """Return the canonical identifier represented by a page filename."""
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("page id must be a string or path-like value")
    path_value = os.fspath(value)
    if not isinstance(path_value, str):
        raise ValueError("page id must be a string or path-like value")
    normalized = unicodedata.normalize("NFKC", path_value)
    basename = PurePath(normalized.replace("\\", "/")).name
    stem = basename.rsplit(".", 1)[0].strip() if "." in basename else basename.strip()
    if not _PAGE_ID_PATTERN.fullmatch(stem):
        raise ValueError(f"invalid page id: {value!r}")
    return stem


def canonical_hash(obj: object) -> str:
    """Hash the canonical compact UTF-8 JSON representation of *obj*."""
    try:
        payload = json.dumps(
            obj,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except ValueError as exc:
        if "Out of range float values" in str(exc):
            raise ValueError("canonical JSON contains a non-finite number") from exc
        raise
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_relative_image_path(value: object) -> str:
    """Normalize separators while preserving a safe relative image filename."""
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("relative image path must be a string or path-like value")
    path_value = os.fspath(value)
    if not isinstance(path_value, str):
        raise ValueError("relative image path must be a string or path-like value")
    normalized = path_value.replace("\\", "/")
    components = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(component in {"", ".", ".."} for component in components)
    ):
        raise ValueError(f"invalid relative image path: {value!r}")
    suffix = PurePath(components[-1]).suffix
    if suffix.casefold() not in _INPUT_IMAGE_EXTENSIONS:
        raise ValueError(f"invalid relative image path extension: {value!r}")
    return "/".join(components)


def make_output_names(inputs: Iterable[object] | int) -> list[str]:
    """Return exact source-relative output names.

    The integer overload is a temporary Task 2 compatibility seam for manifest
    initialization and must be removed when Task 3 supplies source paths.
    """
    if isinstance(inputs, int) and not isinstance(inputs, bool):
        if inputs <= 0:
            raise ValueError("count must be a positive integer")
        return [f"{index:04d}.jpg" for index in range(1, inputs + 1)]
    if isinstance(inputs, (str, bytes, os.PathLike)):
        raise ValueError("inputs must be a non-empty iterable of relative image paths")
    try:
        names = [normalize_relative_image_path(value) for value in inputs]
    except TypeError as exc:
        raise ValueError("inputs must be a non-empty iterable of relative image paths") from exc
    if not names:
        raise ValueError("inputs must not be empty")
    return names


def validate_bijection(
    inputs: Iterable[object],
    mappings: Iterable[Mapping[str, Any]],
    actual_outputs: Iterable[str] | None = None,
) -> None:
    """Validate the one-to-one source/output mapping contract."""
    normalized_inputs = [normalize_relative_image_path(value) for value in inputs]
    if not normalized_inputs:
        raise ValueError("inputs must not be empty")
    if len({value.casefold() for value in normalized_inputs}) != len(normalized_inputs):
        raise ValueError("duplicate input relative path")

    mapping_rows = list(mappings)
    mapped_sources: list[str] = []
    output_names: list[str] = []
    for index, row in enumerate(mapping_rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"invalid mapping at index {index}")
        source_value = row.get("source_page")
        if source_value is None:
            raise ValueError(f"missing source_page in mapping at index {index}")
        output_value = row.get("output_name")
        if output_value is None:
            raise ValueError(f"missing output_name in mapping at index {index}")
        if not isinstance(output_value, str):
            raise ValueError(f"invalid output_name in mapping at index {index}")
        mapped_sources.append(normalize_relative_image_path(source_value))
        output_names.append(normalize_relative_image_path(output_value))

    if len({value.casefold() for value in mapped_sources}) != len(mapped_sources):
        raise ValueError("duplicate source mapping")
    for source in normalized_inputs:
        if source not in mapped_sources:
            raise ValueError(f"missing source: {source}")
    if len(mapping_rows) != len(normalized_inputs):
        raise ValueError(
            "mapping count mismatch: "
            f"expected {len(normalized_inputs)}, got {len(mapping_rows)}"
        )
    for index, (source, expected_source) in enumerate(
        zip(mapped_sources, normalized_inputs)
    ):
        if source != expected_source:
            raise ValueError(
                "source order mismatch at index "
                f"{index}: expected {expected_source!r}, got {source!r}"
            )

    expected_outputs = make_output_names(normalized_inputs)
    if len({value.casefold() for value in output_names}) != len(output_names):
        raise ValueError("duplicate output name")
    for index, (source, output) in enumerate(zip(normalized_inputs, output_names)):
        if output != source:
            raise ValueError(
                "output name must equal source relative path at index "
                f"{index}: expected {source!r}, got {output!r}"
            )

    if actual_outputs is not None:
        actual_output_list = list(actual_outputs)
        try:
            actual_output_list = [
                normalize_relative_image_path(value) for value in actual_output_list
            ]
        except ValueError as exc:
            raise ValueError(f"actual outputs contain invalid filename: {exc}") from exc
        expected_output_set = set(expected_outputs)
        actual_output_set = set(actual_output_list)
        extra_outputs = sorted(actual_output_set - expected_output_set)
        if extra_outputs:
            raise ValueError(f"extra actual outputs: {extra_outputs!r}")
        missing_outputs = sorted(expected_output_set - actual_output_set)
        if missing_outputs:
            raise ValueError(f"missing actual outputs: {missing_outputs!r}")
        if (
            len(actual_output_list) != len(expected_outputs)
            or actual_output_set != expected_output_set
        ):
            raise ValueError(
                "actual outputs mismatch: "
                f"expected {sorted(expected_outputs)!r}, "
                f"got {sorted(actual_output_list)!r}"
            )
