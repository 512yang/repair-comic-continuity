"""Deterministic scene clustering and style-reference pack contracts."""

from __future__ import annotations

import math
import ntpath
import os
import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

from pipeline_contracts import canonical_hash, normalize_page_id


REFERENCE_ROLES = frozenset(
    {
        "target_original",
        "adjacent_style",
        "identity_only",
        "composition_only",
        "primary_style",
    }
)


def cluster_size_controls(
    total_count: int,
    member_count: int,
    covers_all: bool,
    min_size: int = 8,
) -> dict[str, Any]:
    """Return the shared safe controls for normal and undersized clusters."""
    for name, value in (
        ("total_count", total_count),
        ("member_count", member_count),
        ("min_size", min_size),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if not isinstance(covers_all, bool):
        raise ValueError("covers_all must be a boolean")
    undersized = member_count < min_size
    boundary_exception = (
        undersized
        and total_count < min_size
        and member_count == total_count
        and covers_all
    )
    blocked = undersized and not boundary_exception
    return {
        "undersized": undersized,
        "boundary_exception": boundary_exception,
        "undersized_reason": (
            "project_total_below_min"
            if boundary_exception
            else "scene_fragment_below_min"
            if undersized
            else None
        ),
        "blocked": blocked,
        "blocker_codes": ["UNDERSIZED_CLUSTER"] if blocked else [],
    }


def _text_or_unknown(value: object) -> str:
    if value is None:
        return "unknown"
    text = unicodedata.normalize("NFKC", str(value)).strip()
    return text or "unknown"


def scene_key(page: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """Return the comparable canonical scene key for a page record."""
    if not isinstance(page, Mapping):
        raise ValueError("page must be a mapping")
    return tuple(
        _text_or_unknown(page.get(field))
        for field in ("chapter", "location", "story_time", "scene_id")
    )


def _page_id(page: Mapping[str, Any]) -> str:
    if "page_id" not in page:
        raise ValueError("page is missing page_id")
    return normalize_page_id(page["page_id"])


def _validate_sizes(min_size: int, max_size: int, context_radius: int) -> None:
    for name, value in (
        ("min_size", min_size),
        ("max_size", max_size),
        ("context_radius", context_radius),
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} must be an integer")
    if min_size <= 0 or max_size <= 0 or min_size > max_size:
        raise ValueError("require 0 < min_size <= max_size")
    if context_radius < 0 or context_radius > 2:
        raise ValueError("context_radius must be between 0 and 2")


def _split_run(run: list[Mapping[str, Any]], max_size: int) -> list[list[Mapping[str, Any]]]:
    chunk_count = math.ceil(len(run) / max_size)
    base, remainder = divmod(len(run), chunk_count)
    chunks: list[list[Mapping[str, Any]]] = []
    offset = 0
    for index in range(chunk_count):
        size = base + (1 if index < remainder else 0)
        chunks.append(run[offset : offset + size])
        offset += size
    return chunks


def _chapter_location(chunk: list[Mapping[str, Any]]) -> tuple[str, str] | None:
    keys = {(scene_key(page)[0], scene_key(page)[1]) for page in chunk}
    if len(keys) != 1:
        return None
    key = next(iter(keys))
    return None if "unknown" in key else key


def _merge_short_chunks(
    chunks: list[list[Mapping[str, Any]]], min_size: int, max_size: int
) -> list[list[Mapping[str, Any]]]:
    merged = [list(chunk) for chunk in chunks]
    index = 0
    while index < len(merged):
        current = merged[index]
        if len(current) >= min_size:
            index += 1
            continue
        current_location = _chapter_location(current)
        if (
            index > 0
            and current_location is not None
            and _chapter_location(merged[index - 1]) == current_location
            and len(merged[index - 1]) + len(current) <= max_size
        ):
            merged[index - 1].extend(current)
            del merged[index]
            index = max(index - 1, 0)
            continue
        if (
            index + 1 < len(merged)
            and current_location is not None
            and _chapter_location(merged[index + 1]) == current_location
            and len(current) + len(merged[index + 1]) <= max_size
        ):
            current.extend(merged[index + 1])
            del merged[index + 1]
            continue
        index += 1
    return merged


def _cluster_scene_key(chunk: list[Mapping[str, Any]]) -> tuple[str, str, str, str]:
    keys = [scene_key(page) for page in chunk]
    if all(key == keys[0] for key in keys[1:]):
        return keys[0]
    chapter_location = _chapter_location(chunk)
    if chapter_location is None:
        raise ValueError("cluster crosses chapter or location")
    return (chapter_location[0], chapter_location[1], "mixed", "mixed")


def _canary_page(chunk: list[Mapping[str, Any]]) -> str:
    scored: list[tuple[float, int]] = []
    for index, page in enumerate(chunk):
        score = page.get("risk_score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            numeric_score = float(score)
            if not math.isfinite(numeric_score):
                raise ValueError("risk_score must be finite")
            scored.append((numeric_score, index))
    if not scored:
        return _page_id(chunk[(len(chunk) - 1) // 2])
    highest = max(score for score, _ in scored)
    selected_index = next(index for score, index in scored if score == highest)
    return _page_id(chunk[selected_index])


def build_scene_clusters(
    pages: Iterable[Mapping[str, Any]],
    min_size: int = 8,
    max_size: int = 20,
    context_radius: int = 2,
) -> list[dict[str, Any]]:
    """Build stable scene clusters without changing input story order."""
    _validate_sizes(min_size, max_size, context_radius)
    ordered_pages = list(pages)
    if not ordered_pages:
        return []
    if any(not isinstance(page, Mapping) for page in ordered_pages):
        raise ValueError("every page must be a mapping")

    ordered_ids = [_page_id(page) for page in ordered_pages]
    if len(set(ordered_ids)) != len(ordered_ids):
        raise ValueError("page_id values must be unique")

    runs: list[list[Mapping[str, Any]]] = []
    for page in ordered_pages:
        if not runs or scene_key(page) != scene_key(runs[-1][-1]):
            runs.append([page])
        else:
            runs[-1].append(page)

    split_chunks = [
        chunk for run in runs for chunk in _split_run(run, max_size=max_size)
    ]
    chunks = _merge_short_chunks(split_chunks, min_size=min_size, max_size=max_size)
    positions = {page_id: index for index, page_id in enumerate(ordered_ids)}

    clusters: list[dict[str, Any]] = []
    for chunk in chunks:
        member_pages = [_page_id(page) for page in chunk]
        first_position = positions[member_pages[0]]
        last_position = positions[member_pages[-1]]
        key = _cluster_scene_key(chunk)
        identity = {"member_pages": member_pages, "scene_key": key}
        cluster_id = f"cluster-{canonical_hash(identity)[:16]}"
        controls = cluster_size_controls(
            len(ordered_ids),
            len(chunk),
            member_pages == ordered_ids,
            min_size,
        )
        clusters.append(
            {
                "cluster_id": cluster_id,
                "member_pages": member_pages,
                "context_before": ordered_ids[
                    max(0, first_position - context_radius) : first_position
                ],
                "context_after": ordered_ids[
                    last_position + 1 : last_position + 1 + context_radius
                ],
                "scene_key": key,
                "reference_pack_id": None,
                "reference_pack_state": "unbound",
                "canary_page": _canary_page(chunk),
                **controls,
            }
        )
    return clusters


def _normalized_path(value: object) -> str:
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("reference path must be a string or path-like value")
    raw_path = os.fspath(value)
    if not isinstance(raw_path, str):
        raise ValueError("reference path must resolve to text")
    text = unicodedata.normalize("NFKC", raw_path).strip()
    if not text:
        raise ValueError("reference path must not be empty")
    return ntpath.normpath(text).replace("\\", "/").casefold()


def _looks_contaminated(path: str) -> bool:
    compact = path.replace("-", "_").replace(" ", "_")
    markers = (
        "人物参考图",
        "人设",
        "identity_sheet",
        "identity_reference",
        "character_sheet",
        "character_reference",
        "page_candidates/",
        "rejected",
    )
    return any(marker in compact for marker in markers)


def validate_reference_pack(
    pack: Mapping[str, Any], stable_pages: Iterable[object] | None = None
) -> bool:
    """Reject invalid roles, duplicates, and contaminated primary style sources."""
    if not isinstance(pack, Mapping) or "references" not in pack:
        raise ValueError("reference pack must contain references")
    references = pack["references"]
    if not isinstance(references, list) or not references:
        raise ValueError("references must be a nonempty list")
    stable = (
        {_normalized_path(path) for path in stable_pages}
        if stable_pages is not None
        else None
    )
    seen: set[tuple[str, str]] = set()
    for reference in references:
        if not isinstance(reference, Mapping):
            raise ValueError("every reference must be a mapping")
        path = _normalized_path(reference.get("path"))
        role = reference.get("role")
        if not isinstance(role, str) or role not in REFERENCE_ROLES:
            raise ValueError(f"unknown reference role: {role!r}")
        marker = (path, role)
        if marker in seen:
            raise ValueError("duplicate reference path and role")
        seen.add(marker)
        if role == "primary_style":
            if _looks_contaminated(path):
                raise ValueError("primary_style reference is contaminated")
            if stable is None or path not in stable:
                raise ValueError("primary_style reference is not a stable page")
    return True


def build_reference_pack(
    cluster: Mapping[str, Any],
    references: Iterable[Mapping[str, Any]],
    stable_pages: Iterable[object] | None = None,
) -> dict[str, Any]:
    """Return a validated, deterministic reference pack for one cluster."""
    if not isinstance(cluster, Mapping) or not cluster.get("cluster_id"):
        raise ValueError("cluster must contain cluster_id")
    cluster_id = str(cluster["cluster_id"])
    canonical_references: list[dict[str, str]] = []
    for reference in references:
        if not isinstance(reference, Mapping):
            raise ValueError("every reference must be a mapping")
        path = reference.get("path")
        role = reference.get("role")
        canonical_reference = {"path": _normalized_path(path), "role": role}
        if "sha256" in reference:
            digest = reference.get("sha256")
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
                raise ValueError("reference sha256 must be 64 hexadecimal characters")
            canonical_reference["sha256"] = digest.lower()
        canonical_references.append(canonical_reference)
    canonical_references.sort(
        key=lambda item: (_normalized_path(item["path"]), str(item["role"]))
    )
    reference_pack_id = canonical_hash(
        {"cluster_id": cluster_id, "references": canonical_references}
    )
    pack = {
        "reference_pack_id": reference_pack_id,
        "cluster_id": cluster_id,
        "references": canonical_references,
    }
    validate_reference_pack(pack, stable_pages=stable_pages)
    pack["reference_binding_hash"] = canonical_hash(
        {
            "reference_pack_id": reference_pack_id,
            "references": canonical_references,
        }
    )
    return pack


def bind_reference_pack(
    cluster: Mapping[str, Any], pack: Mapping[str, Any]
) -> dict[str, Any]:
    """Return a bound cluster copy; reject silent replacement of an existing pack."""
    if not isinstance(cluster, Mapping) or not cluster.get("cluster_id"):
        raise ValueError("cluster must contain cluster_id")
    if not isinstance(pack, Mapping) or pack.get("cluster_id") != cluster["cluster_id"]:
        raise ValueError("reference pack cluster_id mismatch")
    references = pack.get("references")
    stable_pages = [
        reference.get("path")
        for reference in references
        if isinstance(reference, Mapping) and reference.get("role") == "primary_style"
    ] if isinstance(references, list) else []
    rebuilt = build_reference_pack(
        cluster,
        references if isinstance(references, list) else [],
        stable_pages=stable_pages,
    )
    if (
        pack.get("reference_pack_id") != rebuilt["reference_pack_id"]
        or pack.get("reference_binding_hash") != rebuilt["reference_binding_hash"]
    ):
        raise ValueError("reference pack content identity mismatch")

    current_id = cluster.get("reference_pack_id")
    current_state = cluster.get("reference_pack_state", "unbound")
    if current_state == "bound" and current_id != rebuilt["reference_pack_id"]:
        raise ValueError("reference pack rebind requires explicit audited workflow")
    if current_state not in {"unbound", "bound"}:
        raise ValueError("reference_pack_state must be unbound or bound")
    result = dict(cluster)
    result["reference_pack_id"] = rebuilt["reference_pack_id"]
    result["reference_pack_state"] = "bound"
    return result
