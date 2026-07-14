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


def make_output_names(count: int) -> list[str]:
    """Return the exact contiguous four-digit JPG output names."""
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        raise ValueError("count must be a positive integer")
    return [f"{index:04d}.jpg" for index in range(1, count + 1)]


def validate_bijection(
    inputs: Iterable[object],
    mappings: Iterable[Mapping[str, Any]],
    actual_outputs: Iterable[str] | None = None,
) -> None:
    """Validate the one-to-one source/output mapping contract."""
    normalized_inputs = [normalize_page_id(value) for value in inputs]
    if not normalized_inputs:
        raise ValueError("inputs must not be empty")
    if len(set(normalized_inputs)) != len(normalized_inputs):
        raise ValueError("duplicate input page id")

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
        mapped_sources.append(normalize_page_id(source_value))
        output_names.append(output_value)

    if len(set(mapped_sources)) != len(mapped_sources):
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

    expected_outputs = make_output_names(len(normalized_inputs))
    if len(set(output_names)) != len(output_names):
        raise ValueError("duplicate output name")
    if output_names != expected_outputs:
        raise ValueError(
            f"output names not exact: expected {expected_outputs!r}, got {output_names!r}"
        )

    if actual_outputs is not None:
        actual_output_list = list(actual_outputs)
        invalid_actual_outputs = [
            value
            for value in actual_output_list
            if not isinstance(value, str)
            or re.fullmatch(r"[0-9]{4}\.jpg", value) is None
        ]
        if invalid_actual_outputs:
            raise ValueError(
                "actual outputs contain invalid filename: "
                f"{invalid_actual_outputs!r}"
            )
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
