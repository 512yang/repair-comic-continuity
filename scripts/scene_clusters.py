"""Deterministic semantic scene clusters and traceable reference packs."""

from __future__ import annotations

import math
import ntpath
import os
import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

from pipeline_contracts import (
    canonical_hash,
    normalize_page_id,
    normalize_relative_image_path,
)


REFERENCE_ROLES = frozenset(
    {
        "target_composition",
        "comic_style_anchor",
        "identity_only",
        "prop_anchor",
        "scene_anchor",
    }
)

# Read-only compatibility for V3 proposal validation. V4 packs never use these roles.
LEGACY_REFERENCE_ROLES = frozenset(
    {
        "target_original",
        "adjacent_style",
        "identity_only",
        "composition_only",
        "primary_style",
    }
)


class SceneClusterContractError(ValueError):
    """A machine-routable scene-cluster or reference-pack rejection."""

    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def cluster_size_controls(
    total_count: int,
    member_count: int,
    covers_all: bool,
    min_size: int = 8,
    *,
    semantically_bounded: bool = False,
) -> dict[str, Any]:
    """Return throughput hints without blocking a real semantic short scene."""
    for name, value in (
        ("total_count", total_count),
        ("member_count", member_count),
        ("min_size", min_size),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if not isinstance(covers_all, bool):
        raise ValueError("covers_all must be a boolean")
    if not isinstance(semantically_bounded, bool):
        raise ValueError("semantically_bounded must be a boolean")

    undersized = member_count < min_size
    boundary_exception = (
        undersized
        and total_count < min_size
        and member_count == total_count
        and covers_all
    )
    semantic_short = undersized and semantically_bounded and not boundary_exception
    blocked = undersized and not boundary_exception and not semantic_short
    short_scene_reason = (
        "project_total_below_preferred_size"
        if boundary_exception
        else "semantic_scene_below_preferred_size"
        if semantic_short
        else None
    )
    return {
        "undersized": undersized,
        "boundary_exception": boundary_exception,
        "undersized_reason": (
            "project_total_below_min"
            if boundary_exception
            else "semantic_scene_below_preferred_size"
            if semantic_short
            else "scene_fragment_below_min"
            if undersized
            else None
        ),
        "short_scene_reason": short_scene_reason,
        "blocked": blocked,
        "blocker_codes": ["UNDERSIZED_CLUSTER"] if blocked else [],
    }


def _text_or_unknown(value: object) -> str:
    if value is None:
        return "unknown"
    text = unicodedata.normalize("NFKC", str(value)).strip()
    return text or "unknown"


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"reference {field} must be a nonempty string")
    text = unicodedata.normalize("NFKC", value).strip()
    if not text:
        raise ValueError(f"reference {field} must be a nonempty string")
    return text


def scene_key(page: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """Return the canonical primary story-boundary fields for a page."""
    if not isinstance(page, Mapping):
        raise ValueError("page must be a mapping")
    return tuple(
        _text_or_unknown(page.get(field))
        for field in ("chapter", "location", "story_time", "scene_id")
    )


def _page_reference(page: Mapping[str, Any]) -> str:
    value = page.get("relative_path")
    if value is not None:
        try:
            return normalize_relative_image_path(value)
        except ValueError as exc:
            raise SceneClusterContractError(
                "INVALID_PAGE_REFERENCE", str(exc), value=str(value)
            ) from exc
    if "page_id" not in page:
        raise SceneClusterContractError(
            "MISSING_PAGE_REFERENCE", "page is missing page_id or relative_path"
        )
    try:
        return normalize_page_id(page["page_id"])
    except ValueError as exc:
        raise SceneClusterContractError(
            "INVALID_PAGE_REFERENCE", str(exc), value=str(page.get("page_id"))
        ) from exc


def _identity(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _string_list(value: object, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise ValueError(f"{field} must be a list of nonempty strings")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _required_text(item, field)
        marker = _identity(text)
        if marker not in seen:
            seen.add(marker)
            result.append(text)
    return result


def _costume_state(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("costume_state must be a mapping")
    result: dict[str, str] = {}
    for character, state in value.items():
        name = _required_text(character, "costume character")
        description = _required_text(state, "costume state")
        if _identity(name) in {_identity(existing) for existing in result}:
            raise ValueError("costume_state contains duplicate character identity")
        result[name] = description
    return dict(sorted(result.items(), key=lambda item: _identity(item[0])))


def _issue_records(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, str):
        return [_required_text(value, "issue_schedule")]
    if not isinstance(value, Iterable):
        raise ValueError("issue_schedule must be a list")
    records: list[object] = []
    seen: set[str] = set()
    for record in value:
        if isinstance(record, str):
            normalized: object = _required_text(record, "issue_schedule")
        elif isinstance(record, Mapping):
            normalized = dict(record)
            canonical_hash(normalized)
        else:
            raise ValueError("issue_schedule entries must be strings or mappings")
        marker = canonical_hash(normalized)
        if marker not in seen:
            seen.add(marker)
            records.append(normalized)
    return records


def _semantic_signature(page: Mapping[str, Any]) -> dict[str, object]:
    return {
        "scene_key": scene_key(page),
        "cast": sorted(_string_list(page.get("cast"), "cast"), key=_identity),
        "persistent_props": sorted(
            _string_list(page.get("persistent_props"), "persistent_props"),
            key=_identity,
        ),
        "costume_state": _costume_state(page.get("costume_state")),
        "explicit_transition": _text_or_unknown(page.get("explicit_transition")),
    }


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


def _split_run(
    run: list[Mapping[str, Any]], max_size: int
) -> list[list[Mapping[str, Any]]]:
    chunk_count = math.ceil(len(run) / max_size)
    base, remainder = divmod(len(run), chunk_count)
    chunks: list[list[Mapping[str, Any]]] = []
    offset = 0
    for index in range(chunk_count):
        size = base + (1 if index < remainder else 0)
        chunks.append(run[offset : offset + size])
        offset += size
    return chunks


_VISUAL_TERMS = (
    "visual",
    "character",
    "identity",
    "costume",
    "prop",
    "scene",
    "style",
    "anatomy",
    "beard",
    "hair",
    "skin",
    "人物",
    "身份",
    "衣服",
    "服装",
    "道具",
    "场景",
    "画风",
    "肢体",
    "胡子",
    "发型",
    "肤色",
)


def _page_has_visual_task(page: Mapping[str, Any]) -> bool:
    explicit = page.get("has_visual_task", page.get("has_visual_tasks"))
    if explicit is not None:
        if not isinstance(explicit, bool):
            raise ValueError("has_visual_task must be a boolean")
        return explicit
    visual_tasks = page.get("visual_tasks")
    if visual_tasks:
        return True
    if page.get("page_class") == "full_page_redraw":
        return True
    for issue in _issue_records(page.get("issue_schedule")):
        text = str(issue).casefold()
        if any(term in text for term in _VISUAL_TERMS):
            return True
    return False


def _risk_score(page: Mapping[str, Any]) -> float:
    value = page.get("risk_score", 0)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("risk_score must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError("risk_score must be finite")
    return numeric


def _canary_page(chunk: list[Mapping[str, Any]]) -> str | None:
    visual = [
        (index, page)
        for index, page in enumerate(chunk)
        if _page_has_visual_task(page)
    ]
    if not visual:
        return None
    # max() uses -story_position as the deterministic first-in-story tie breaker.
    _, selected = max(visual, key=lambda item: (_risk_score(item[1]), -item[0]))
    return _page_reference(selected)


def _merge_metadata(
    chunk: list[Mapping[str, Any]], field: str
) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for page in chunk:
        for value in _string_list(page.get(field), field):
            marker = _identity(value)
            if marker not in seen:
                seen.add(marker)
                merged.append(value)
    return merged


def _merge_costumes(chunk: list[Mapping[str, Any]]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for page in chunk:
        for character, state in _costume_state(page.get("costume_state")).items():
            previous = next(
                (name for name in merged if _identity(name) == _identity(character)),
                None,
            )
            if previous is not None and merged[previous] != state:
                raise ValueError("one semantic cluster contains conflicting costume_state")
            merged[character] = state
    return dict(sorted(merged.items(), key=lambda item: _identity(item[0])))


def _confidence(chunk: list[Mapping[str, Any]]) -> float | None:
    scores: list[float] = []
    for page in chunk:
        value = page.get("scene_confidence", page.get("confidence"))
        if value is None:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("scene confidence must be numeric")
        numeric = float(value)
        if not math.isfinite(numeric) or not 0 <= numeric <= 1:
            raise ValueError("scene confidence must be finite and between 0 and 1")
        scores.append(numeric)
    return min(scores) if scores else None


def _boundary_changes(
    left: Mapping[str, Any] | None, right: Mapping[str, Any] | None
) -> list[str]:
    if left is None:
        return ["project_start"]
    if right is None:
        return ["project_end"]
    left_signature = _semantic_signature(left)
    right_signature = _semantic_signature(right)
    changes = [
        field
        for field in (
            "scene_key",
            "cast",
            "persistent_props",
            "costume_state",
            "explicit_transition",
        )
        if left_signature[field] != right_signature[field]
    ]
    return changes or ["max_size_split"]


def build_scene_clusters(
    pages: Iterable[Mapping[str, Any]],
    min_size: int = 8,
    max_size: int = 20,
    context_radius: int = 2,
) -> list[dict[str, Any]]:
    """Build clusters from semantic boundaries while preserving story order."""
    _validate_sizes(min_size, max_size, context_radius)
    ordered_pages = list(pages)
    if not ordered_pages:
        return []
    if any(not isinstance(page, Mapping) for page in ordered_pages):
        raise ValueError("every page must be a mapping")

    ordered_ids = [_page_reference(page) for page in ordered_pages]
    identities = [_identity(page_id) for page_id in ordered_ids]
    if len(set(identities)) != len(identities):
        raise SceneClusterContractError(
            "DUPLICATE_PAGE_REFERENCE",
            "duplicate case-insensitive page reference",
            page_references=ordered_ids,
        )

    runs: list[list[Mapping[str, Any]]] = []
    signatures: list[str] = []
    for page in ordered_pages:
        signature = canonical_hash(_semantic_signature(page))
        if not runs or signature != signatures[-1]:
            runs.append([page])
            signatures.append(signature)
        else:
            runs[-1].append(page)
    chunks = [chunk for run in runs for chunk in _split_run(run, max_size)]
    positions = {page_id: index for index, page_id in enumerate(ordered_ids)}

    clusters: list[dict[str, Any]] = []
    for chunk in chunks:
        member_pages = [_page_reference(page) for page in chunk]
        first_position = positions[member_pages[0]]
        last_position = positions[member_pages[-1]]
        cast = _merge_metadata(chunk, "cast")
        persistent_props = _merge_metadata(chunk, "persistent_props")
        costume_state = _merge_costumes(chunk)
        visual_targets = [
            _page_reference(page) for page in chunk if _page_has_visual_task(page)
        ]
        repair_characters = _merge_metadata(chunk, "repair_characters")
        issue_schedule: list[object] = []
        seen_issues: set[str] = set()
        for page in chunk:
            for issue in _issue_records(page.get("issue_schedule")):
                marker = canonical_hash(issue)
                if marker not in seen_issues:
                    seen_issues.add(marker)
                    issue_schedule.append(issue)
        fingerprint_payload = {
            "scene_key": scene_key(chunk[0]),
            "cast": cast,
            "persistent_props": persistent_props,
            "costume_state": costume_state,
        }
        identity_payload = {
            "member_pages": member_pages,
            "scene_fingerprint": canonical_hash(fingerprint_payload),
        }
        controls = cluster_size_controls(
            len(ordered_ids),
            len(chunk),
            member_pages == ordered_ids,
            min_size,
            semantically_bounded=True,
        )
        clusters.append(
            {
                "cluster_id": f"cluster-{canonical_hash(identity_payload)[:16]}",
                "member_pages": member_pages,
                "context_before": ordered_ids[
                    max(0, first_position - context_radius) : first_position
                ],
                "context_after": ordered_ids[
                    last_position + 1 : last_position + 1 + context_radius
                ],
                "scene_key": scene_key(chunk[0]),
                "cast": cast,
                "persistent_props": persistent_props,
                "costume_state": costume_state,
                "scene_fingerprint": canonical_hash(fingerprint_payload),
                "boundary_reason": {
                    "start": _boundary_changes(
                        ordered_pages[first_position - 1]
                        if first_position > 0
                        else None,
                        ordered_pages[first_position],
                    ),
                    "end": _boundary_changes(
                        ordered_pages[last_position],
                        ordered_pages[last_position + 1]
                        if last_position + 1 < len(ordered_pages)
                        else None,
                    ),
                },
                "confidence": _confidence(chunk),
                "has_visual_tasks": bool(visual_targets),
                "visual_targets": visual_targets,
                "repair_characters": repair_characters,
                "issue_schedule": issue_schedule,
                "reference_pack_id": None,
                "reference_pack_state": "unbound",
                "canary_page": _canary_page(chunk),
                **controls,
            }
        )
    return clusters


def _reference_path(value: object) -> str:
    try:
        return normalize_relative_image_path(value)
    except ValueError as exc:
        raise SceneClusterContractError(
            "INVALID_REFERENCE_PATH", str(exc), value=str(value)
        ) from exc


def _legacy_reference_path(value: object) -> str:
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


def _review_record(value: object, *, required: bool, label: str) -> dict[str, str] | None:
    if value is None and not required:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} review evidence must be a mapping")
    status = _required_text(value.get("status"), f"{label} review status")
    evidence_id = _required_text(
        value.get("evidence_id"), f"{label} review evidence_id"
    )
    if status.casefold() not in {"reviewed", "approved"}:
        if required:
            if label == "comic_style_anchor":
                raise ValueError("reviewed comic_style_anchor evidence is required")
            raise ValueError(f"{label} must have reviewed review evidence")
    result = {"status": status.casefold(), "evidence_id": evidence_id}
    reviewer = value.get("reviewer")
    if reviewer is not None:
        result["reviewer"] = _required_text(reviewer, f"{label} reviewer")
    return result


def _stable_page_records(stable_pages: Iterable[object] | None) -> list[dict[str, object]]:
    if stable_pages is None:
        return []
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in stable_pages:
        if not isinstance(row, Mapping):
            raise ValueError("stable page review evidence must be an explicit mapping")
        path = _reference_path(row.get("path"))
        review = _review_record(
            row.get("review"), required=True, label="stable page"
        )
        marker = _identity(path)
        if marker in seen:
            raise SceneClusterContractError(
                "DUPLICATE_STABLE_PAGE", "duplicate stable page", path=path
            )
        seen.add(marker)
        records.append({"path": path, "review": review})
    return sorted(records, key=lambda row: _identity(str(row["path"])))


def _canonical_reference(reference: Mapping[str, Any]) -> dict[str, object]:
    role = reference.get("role")
    legacy_identity = (
        role == "identity_only"
        and "subject" not in reference
        and "source" not in reference
    )
    if isinstance(role, str) and (
        role in LEGACY_REFERENCE_ROLES - REFERENCE_ROLES or legacy_identity
    ):
        canonical_legacy: dict[str, object] = {
            "path": _legacy_reference_path(reference.get("path")),
            "role": role,
        }
        if "sha256" in reference:
            digest = reference.get("sha256")
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
                raise ValueError("reference sha256 must be 64 hexadecimal characters")
            canonical_legacy["sha256"] = digest.lower()
        return canonical_legacy
    path = _reference_path(reference.get("path"))
    if not isinstance(role, str) or role not in REFERENCE_ROLES:
        raise ValueError(f"unknown reference role: {role!r}")
    subject = _required_text(reference.get("subject"), "subject")
    source = _required_text(reference.get("source"), "source")
    canonical: dict[str, object] = {
        "path": path,
        "role": role,
        "subject": subject,
        "source": source,
    }
    if "sha256" in reference:
        digest = reference.get("sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
            raise ValueError("reference sha256 must be 64 hexadecimal characters")
        canonical["sha256"] = digest.lower()
    review = _review_record(
        reference.get("review"),
        required=role in {"comic_style_anchor", "prop_anchor", "scene_anchor"},
        label=role,
    )
    if review is not None:
        canonical["review"] = review
    return canonical


def _issue_texts(cluster: Mapping[str, Any]) -> list[str]:
    return [str(value).casefold() for value in _issue_records(cluster.get("issue_schedule"))]


def _requires_issue_anchor(cluster: Mapping[str, Any], kind: str) -> bool:
    terms = {
        "prop": ("prop", "道具"),
        "scene": ("scene", "location", "场景", "地点"),
    }[kind]
    return any(any(term in issue for term in terms) for issue in _issue_texts(cluster))


def _validate_visual_coverage(
    cluster: Mapping[str, Any],
    references: list[Mapping[str, Any]],
    stable_pages: list[Mapping[str, object]],
) -> None:
    if not cluster.get("has_visual_tasks"):
        return
    required_characters = _string_list(cluster.get("cast"), "cast")
    for character in _string_list(cluster.get("repair_characters"), "repair_characters"):
        if _identity(character) not in {_identity(value) for value in required_characters}:
            required_characters.append(character)
    identity_subjects = {
        _identity(str(row["subject"]))
        for row in references
        if row["role"] == "identity_only"
        or (
            row["role"] == "comic_style_anchor"
            and row.get("source") == "reviewed_comic_identity_anchor"
            and "review" in row
        )
    }
    missing_cast = [
        character
        for character in required_characters
        if _identity(character) not in identity_subjects
    ]
    if missing_cast:
        raise ValueError(f"cast coverage missing: {missing_cast}")

    targets = _string_list(
        cluster.get("visual_targets") or cluster.get("member_pages"),
        "visual_targets",
    )
    target_subjects = {
        _identity(str(row["subject"]))
        for row in references
        if row["role"] == "target_composition"
    }
    missing_targets = [target for target in targets if _identity(target) not in target_subjects]
    if missing_targets:
        raise ValueError(f"target composition coverage missing: {missing_targets}")

    stable_paths = {_identity(str(row["path"])) for row in stable_pages}
    reviewed_style = [
        row
        for row in references
        if row["role"] == "comic_style_anchor"
        and "review" in row
        and _identity(str(row["path"])) in stable_paths
    ]
    if not reviewed_style:
        raise ValueError("visual pack requires a reviewed comic_style_anchor")

    if _requires_issue_anchor(cluster, "prop") and not any(
        row["role"] == "prop_anchor" for row in references
    ):
        raise ValueError("prop_anchor required by issue schedule")
    if _requires_issue_anchor(cluster, "scene") and not any(
        row["role"] == "scene_anchor" for row in references
    ):
        raise ValueError("scene_anchor required by issue schedule")


def validate_reference_pack(
    pack: Mapping[str, Any],
    stable_pages: Iterable[object] | None = None,
    cluster: Mapping[str, Any] | None = None,
) -> bool:
    """Validate roles, traceability, review evidence, and target coverage."""
    if not isinstance(pack, Mapping) or "references" not in pack:
        raise ValueError("reference pack must contain references")
    raw_references = pack["references"]
    if not isinstance(raw_references, list) or not raw_references:
        raise ValueError("references must be a nonempty list")
    references = [
        _canonical_reference(row) if isinstance(row, Mapping) else None
        for row in raw_references
    ]
    if any(row is None for row in references):
        raise ValueError("every reference must be a mapping")
    canonical_references = [row for row in references if row is not None]
    seen: set[tuple[str, str]] = set()
    for row in canonical_references:
        marker = (_identity(str(row["path"])), str(row["role"]))
        if marker in seen:
            raise SceneClusterContractError(
                "DUPLICATE_REFERENCE",
                "duplicate reference path and role",
                path=row["path"],
                role=row["role"],
            )
        seen.add(marker)
    legacy = all(
        str(row["role"]) in LEGACY_REFERENCE_ROLES and "subject" not in row
        for row in canonical_references
    )
    if legacy:
        stable_paths = (
            {_legacy_reference_path(path) for path in stable_pages}
            if stable_pages is not None
            else None
        )
        for row in canonical_references:
            if row["role"] == "primary_style":
                path = str(row["path"])
                if _looks_contaminated(path):
                    raise ValueError("primary_style reference is contaminated")
                if stable_paths is None or path not in stable_paths:
                    raise ValueError("primary_style reference is not a stable page")
        return True
    stable_records = _stable_page_records(stable_pages)
    if cluster is not None:
        _validate_visual_coverage(cluster, canonical_references, stable_records)
    return True


def build_reference_pack(
    cluster: Mapping[str, Any],
    references: Iterable[Mapping[str, Any]],
    stable_pages: Iterable[object] | None = None,
) -> dict[str, Any]:
    """Return a deterministic, cast-complete reference pack for one cluster."""
    if not isinstance(cluster, Mapping) or not cluster.get("cluster_id"):
        raise ValueError("cluster must contain cluster_id")
    cluster_id = str(cluster["cluster_id"])
    raw_references = list(references)
    canonical_references = [
        _canonical_reference(reference)
        if isinstance(reference, Mapping)
        else (_ for _ in ()).throw(ValueError("every reference must be a mapping"))
        for reference in raw_references
    ]
    canonical_references.sort(
        key=lambda item: (
            _identity(str(item["path"])),
            str(item["role"]),
            _identity(str(item.get("subject", ""))),
        )
    )
    legacy = bool(canonical_references) and all(
        str(row["role"]) in LEGACY_REFERENCE_ROLES and "subject" not in row
        for row in canonical_references
    )
    if legacy:
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
    stable_records = _stable_page_records(stable_pages)
    reference_pack_id = canonical_hash(
        {"cluster_id": cluster_id, "references": canonical_references}
    )
    pack: dict[str, Any] = {
        "reference_pack_id": reference_pack_id,
        "cluster_id": cluster_id,
        "references": canonical_references,
        "stable_pages": stable_records,
    }
    validate_reference_pack(pack, stable_pages=stable_records, cluster=cluster)
    pack["reference_binding_hash"] = canonical_hash(
        {
            "reference_pack_id": reference_pack_id,
            "references": canonical_references,
            "stable_pages": stable_records,
        }
    )
    return pack


def bind_reference_pack(
    cluster: Mapping[str, Any],
    pack: Mapping[str, Any],
    stable_pages: Iterable[object] | None = None,
) -> dict[str, Any]:
    """Bind an intact pack; reject unaudited replacement of an existing pack."""
    if not isinstance(cluster, Mapping) or not cluster.get("cluster_id"):
        raise ValueError("cluster must contain cluster_id")
    if not isinstance(pack, Mapping) or pack.get("cluster_id") != cluster["cluster_id"]:
        raise ValueError("reference pack cluster_id mismatch")
    references = pack.get("references")
    if not isinstance(references, list):
        raise ValueError("reference pack must contain references")
    legacy = bool(references) and all(
        isinstance(row, Mapping)
        and row.get("role") in LEGACY_REFERENCE_ROLES
        and "subject" not in row
        for row in references
    )
    if legacy:
        stable = stable_pages
        if stable is None:
            stable = [
                row.get("path")
                for row in references
                if isinstance(row, Mapping) and row.get("role") == "primary_style"
            ]
    else:
        stable = stable_pages if stable_pages is not None else pack.get("stable_pages", [])
    rebuilt = build_reference_pack(cluster, references, stable_pages=stable)
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
