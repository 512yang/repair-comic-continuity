"""Deterministic semantic scene clusters and traceable reference packs."""

from __future__ import annotations

import math
import ntpath
import os
import re
import unicodedata
from datetime import datetime
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
    if not isinstance(value, list):
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
    if not isinstance(value, list):
        raise ValueError("issue_schedule must be a list")
    records: list[object] = []
    seen: set[str] = set()
    for record in value:
        if isinstance(record, str):
            normalized: object = _required_text(record, "issue_schedule")
        elif isinstance(record, Mapping):
            if any(not isinstance(key, str) for key in record):
                raise ValueError("issue_schedule mapping keys must be strings")
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


def _semantic_resolved(page: Mapping[str, Any]) -> bool:
    return all(value != "unknown" for value in scene_key(page))


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
    if explicit is not None and not isinstance(explicit, bool):
        raise ValueError("has_visual_task must be a boolean")
    visual_tasks = page.get("visual_tasks")
    if visual_tasks is not None and not isinstance(visual_tasks, list):
        raise ValueError("visual_tasks must be a list")
    inferred = bool(visual_tasks) or page.get("page_class") == "full_page_redraw"
    for issue in _issue_records(page.get("issue_schedule")):
        text = str(issue).casefold()
        if any(term in text for term in _VISUAL_TERMS):
            inferred = True
    if explicit is False and inferred:
        raise ValueError("has_visual_task=false contradicts visual issue metadata")
    return bool(explicit) or inferred


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
        signature_payload: dict[str, object] = _semantic_signature(page)
        if not _semantic_resolved(page):
            # Unknown primary boundaries cannot prove that adjacent pages share a scene.
            signature_payload = {
                **signature_payload,
                "unresolved_page": _page_reference(page),
            }
        signature = canonical_hash(signature_payload)
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
            "explicit_transition": _text_or_unknown(
                chunk[0].get("explicit_transition")
            ),
        }
        identity_payload = {
            "member_pages": member_pages,
            "scene_fingerprint": canonical_hash(fingerprint_payload),
        }
        semantic_status = (
            "resolved"
            if all(_semantic_resolved(page) for page in chunk)
            else "unresolved"
        )
        controls = cluster_size_controls(
            len(ordered_ids),
            len(chunk),
            member_pages == ordered_ids,
            min_size,
            semantically_bounded=semantic_status == "resolved",
        )
        if semantic_status == "unresolved":
            controls.update(
                boundary_exception=False,
                undersized_reason="unresolved_semantic_boundary",
                short_scene_reason=None,
                blocked=True,
                blocker_codes=["UNRESOLVED_SEMANTIC_BOUNDARY"],
            )
        boundary_reason = {
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
        }
        if semantic_status == "unresolved":
            boundary_reason = {
                "start": ["unresolved_semantic_boundary"],
                "end": ["unresolved_semantic_boundary"],
            }
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
                "semantic_status": semantic_status,
                "cast": cast,
                "persistent_props": persistent_props,
                "costume_state": costume_state,
                "scene_fingerprint": canonical_hash(fingerprint_payload),
                "boundary_reason": boundary_reason,
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


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise ValueError(f"{label} sha256 must be 64 hexadecimal characters")
    return value.lower()


def _evidence_path(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} evidence_path must be a relative path")
    normalized = value.replace("\\", "/").strip()
    components = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(component in {"", ".", ".."} for component in components)
    ):
        raise ValueError(f"{label} evidence_path must be a safe relative path")
    return normalized


def _review_record(
    value: object, *, required: bool, label: str
) -> dict[str, object] | None:
    if value is None and not required:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} review evidence must be a mapping")
    status = _required_text(value.get("status"), f"{label} review status")
    if status.casefold() not in {"passed", "approved"}:
        raise ValueError(f"reviewed {label} status must be passed or approved")
    if value.get("full_size") is not True:
        raise ValueError(f"{label} review must be full-size")
    reviewer = _required_text(value.get("reviewer"), f"{label} reviewer")
    reviewed_at = _required_text(value.get("reviewed_at"), f"{label} reviewed_at")
    try:
        parsed_time = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} reviewed_at must be ISO 8601") from exc
    if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
        raise ValueError(f"{label} reviewed_at must include timezone")
    return {
        "status": status.casefold(),
        "full_size": True,
        "reviewer": reviewer,
        "reviewed_at": reviewed_at,
        "evidence_path": _evidence_path(value.get("evidence_path"), label),
        "evidence_sha256": _sha256(
            value.get("evidence_sha256"), f"{label} evidence"
        ),
    }


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
        digest = _sha256(row.get("sha256"), "stable page")
        marker = _identity(path)
        if marker in seen:
            raise SceneClusterContractError(
                "DUPLICATE_STABLE_PAGE", "duplicate stable page", path=path
            )
        seen.add(marker)
        records.append({"path": path, "sha256": digest, "review": review})
    return sorted(records, key=lambda row: _identity(str(row["path"])))


def _validate_contract_version(contract_version: str) -> None:
    if contract_version not in {"v3", "v4"}:
        raise ValueError("contract_version must be v3 or v4")


def _reference_schema_preflight(
    references: list[Mapping[str, Any]], contract_version: str
) -> None:
    for row in references:
        role = row.get("role")
        if not isinstance(role, str):
            raise SceneClusterContractError(
                "INVALID_REFERENCE_ROLE",
                "reference role must be a string",
                role_type=type(role).__name__,
            )
    roles = {row.get("role") for row in references}
    v3_only = roles & (LEGACY_REFERENCE_ROLES - REFERENCE_ROLES)
    v4_only = roles & (REFERENCE_ROLES - LEGACY_REFERENCE_ROLES)
    if contract_version == "v4" and v3_only:
        code = (
            "MIXED_REFERENCE_SCHEMA"
            if v4_only or "identity_only" in roles
            else "LEGACY_ADAPTER_REQUIRED"
        )
        raise SceneClusterContractError(
            code,
            "V3 and V4 reference records must not be mixed; use the explicit V3 adapter",
            roles=sorted(str(role) for role in roles),
        )
    if contract_version == "v3" and v4_only:
        raise SceneClusterContractError(
            "MIXED_REFERENCE_SCHEMA",
            "V4 reference roles are forbidden in the explicit V3 adapter",
            roles=sorted(str(role) for role in roles),
        )
    if contract_version == "v3" and any(
        any(field in row for field in ("subject", "source", "review"))
        for row in references
    ):
        raise SceneClusterContractError(
            "MIXED_REFERENCE_SCHEMA",
            "V4 trace fields are forbidden in the explicit V3 adapter",
        )


def _canonical_reference(
    reference: Mapping[str, Any], contract_version: str
) -> dict[str, object]:
    role = reference.get("role")
    if contract_version == "v3":
        if not isinstance(role, str) or role not in LEGACY_REFERENCE_ROLES:
            raise ValueError(f"unknown V3 reference role: {role!r}")
        canonical_legacy: dict[str, object] = {
            "path": _legacy_reference_path(reference.get("path")),
            "role": role,
        }
        if "sha256" in reference:
            canonical_legacy["sha256"] = _sha256(
                reference.get("sha256"), "reference"
            )
        return canonical_legacy

    path = _reference_path(reference.get("path"))
    if not isinstance(role, str) or role not in REFERENCE_ROLES:
        raise SceneClusterContractError(
            "UNKNOWN_REFERENCE_ROLE", f"unknown V4 reference role: {role!r}"
        )
    for field in ("subject", "source"):
        if field not in reference:
            raise SceneClusterContractError(
                "MISSING_REFERENCE_FIELD",
                f"V4 reference is missing {field}",
                role=role,
                field=field,
            )
    subject = _required_text(reference["subject"], "subject")
    source = _required_text(reference["source"], "source")
    canonical: dict[str, object] = {
        "path": path,
        "role": role,
        "subject": subject,
        "source": source,
        "sha256": _sha256(reference.get("sha256"), "reference"),
    }
    if role == "target_composition" and source != "immutable_input":
        raise ValueError("target_composition source must be immutable_input")
    if role == "comic_style_anchor":
        if source not in {"reviewed_comic_page", "reviewed_comic_identity_anchor"}:
            raise ValueError(
                "comic_style_anchor source is not an allowed reviewed comic page"
            )
        if _looks_contaminated(_identity(path)):
            raise ValueError("comic_style_anchor must not use a character sheet")
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


def _issues_imply_visual(cluster: Mapping[str, Any]) -> bool:
    return any(
        any(term in issue for term in _VISUAL_TERMS)
        for issue in _issue_texts(cluster)
    )


def _validate_cluster_visual_contract(cluster: Mapping[str, Any]) -> None:
    has_visual_tasks = cluster.get("has_visual_tasks")
    if not isinstance(has_visual_tasks, bool):
        raise ValueError("has_visual_tasks must be a boolean")
    singular = cluster.get("has_visual_task")
    if singular is not None:
        if not isinstance(singular, bool):
            raise ValueError("has_visual_task must be a boolean")
        if singular != has_visual_tasks:
            raise ValueError("singular/plural visual flag conflict")
    targets = _string_list(cluster.get("visual_targets"), "visual_targets")
    canary = cluster.get("canary_page")
    if not has_visual_tasks and (
        targets or canary is not None or _issues_imply_visual(cluster)
    ):
        raise ValueError(
            "has_visual_tasks=false cannot retain visual targets, canary, or visual issue schedule"
        )
    if canary is not None:
        canary_text = _required_text(canary, "canary_page")
        if _identity(canary_text) not in {_identity(target) for target in targets}:
            raise ValueError("canary_page must belong to visual_targets")
    if has_visual_tasks:
        if not targets:
            raise ValueError("has_visual_tasks=true requires visual_targets")
        return


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
    _validate_cluster_visual_contract(cluster)
    if not cluster["has_visual_tasks"]:
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
            and row.get("review", {}).get("status") in {"passed", "approved"}
            and row.get("review", {}).get("full_size") is True
        )
    }
    missing_cast = [
        character
        for character in required_characters
        if _identity(character) not in identity_subjects
    ]
    if missing_cast:
        raise ValueError(f"cast coverage missing: {missing_cast}")
    allowed_identity_subjects = {_identity(value) for value in required_characters}
    unexpected_identity = [
        str(row["subject"])
        for row in references
        if row["role"] == "identity_only"
        and _identity(str(row["subject"])) not in allowed_identity_subjects
    ]
    if unexpected_identity:
        raise ValueError(
            f"identity_only subject is outside cluster cast: {unexpected_identity}"
        )

    targets = _string_list(
        cluster.get("visual_targets") or cluster.get("member_pages"),
        "visual_targets",
    )
    target_rows = [row for row in references if row["role"] == "target_composition"]
    target_identities = {_identity(target) for target in targets}
    target_row_subjects = [_identity(str(row["subject"])) for row in target_rows]
    if (
        len(target_rows) != len(targets)
        or len(set(target_row_subjects)) != len(target_row_subjects)
        or set(target_row_subjects) != target_identities
    ):
        raise ValueError(
            "target_composition rows (target composition) must exactly match the visual target set"
        )
    for target in targets:
        target_path = normalize_relative_image_path(target)
        matches = [
            row
            for row in target_rows
            if _identity(str(row["subject"])) == _identity(target_path)
        ]
        if not matches:
            raise ValueError(f"target composition coverage missing: {target_path}")
        for row in matches:
            reference_path = str(row["path"])
            components = reference_path.split("/")
            if components and _identity(components[0]) in {"输入", "input"}:
                reference_path = "/".join(components[1:])
            if _identity(reference_path) != _identity(target_path):
                raise ValueError(
                    f"target_composition path does not match target: {target_path}"
                )

    stable_by_path = {
        _identity(str(row["path"])): str(row["sha256"]) for row in stable_pages
    }
    style_rows = [
        row for row in references if row["role"] == "comic_style_anchor"
    ]
    if not style_rows:
        raise ValueError("visual pack requires a reviewed comic_style_anchor")
    if any(
        "review" not in row
        or stable_by_path.get(_identity(str(row["path"]))) != row["sha256"]
        for row in style_rows
    ):
        raise ValueError(
            "every comic_style_anchor must match a reviewed stable page path and sha256"
        )

    prop_required = _requires_issue_anchor(cluster, "prop")
    scene_required = _requires_issue_anchor(cluster, "scene")
    prop_rows = [row for row in references if row["role"] == "prop_anchor"]
    scene_rows = [row for row in references if row["role"] == "scene_anchor"]
    if prop_rows and not prop_required:
        raise ValueError("unexpected prop_anchor without a scheduled prop issue")
    if scene_rows and not scene_required:
        raise ValueError("unexpected scene_anchor without a scheduled scene issue")
    if prop_required and not prop_rows:
        raise ValueError("prop_anchor required by issue schedule")
    if scene_required and not scene_rows:
        raise ValueError("scene_anchor required by issue schedule")

    issue_records = _issue_records(cluster.get("issue_schedule"))
    prop_subjects = {
        _identity(value)
        for value in _string_list(cluster.get("persistent_props"), "persistent_props")
    }
    scene_subjects: set[str] = set()
    key = cluster.get("scene_key")
    if isinstance(key, (list, tuple)) and len(key) >= 2 and key[1] != "unknown":
        scene_subjects.add(_identity(str(key[1])))
    for issue in issue_records:
        if not isinstance(issue, Mapping) or not isinstance(issue.get("subject"), str):
            continue
        text = str(issue).casefold()
        if any(term in text for term in ("prop", "道具")):
            prop_subjects.add(_identity(issue["subject"]))
        if any(term in text for term in ("scene", "location", "场景", "地点")):
            scene_subjects.add(_identity(issue["subject"]))
    if prop_subjects and any(
        _identity(str(row["subject"])) not in prop_subjects for row in prop_rows
    ):
        raise ValueError("prop_anchor subject does not match declared prop subjects")
    if scene_subjects and any(
        _identity(str(row["subject"])) not in scene_subjects for row in scene_rows
    ):
        raise ValueError("scene_anchor subject does not match declared scene subjects")


def validate_reference_pack(
    pack: Mapping[str, Any],
    stable_pages: Iterable[object] | None = None,
    cluster: Mapping[str, Any] | None = None,
    *,
    contract_version: str = "v4",
) -> bool:
    """Validate roles, traceability, review evidence, and target coverage."""
    if not isinstance(pack, Mapping) or "references" not in pack:
        raise ValueError("reference pack must contain references")
    raw_references = pack["references"]
    if not isinstance(raw_references, list) or not raw_references:
        raise ValueError("references must be a nonempty list")
    _validate_contract_version(contract_version)
    if any(not isinstance(row, Mapping) for row in raw_references):
        raise ValueError("every reference must be a mapping")
    reference_rows = [row for row in raw_references if isinstance(row, Mapping)]
    _reference_schema_preflight(reference_rows, contract_version)
    canonical_references = [
        _canonical_reference(row, contract_version) for row in reference_rows
    ]
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
    if contract_version == "v3":
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
    if cluster is None:
        raise ValueError("V4 reference pack validation requires cluster coverage")
    stable_records = _stable_page_records(stable_pages)
    _validate_visual_coverage(cluster, canonical_references, stable_records)
    return True


def build_reference_pack(
    cluster: Mapping[str, Any],
    references: Iterable[Mapping[str, Any]],
    stable_pages: Iterable[object] | None = None,
    *,
    contract_version: str = "v4",
) -> dict[str, Any]:
    """Return a deterministic, cast-complete reference pack for one cluster."""
    if not isinstance(cluster, Mapping) or not cluster.get("cluster_id"):
        raise ValueError("cluster must contain cluster_id")
    cluster_id = str(cluster["cluster_id"])
    _validate_contract_version(contract_version)
    raw_references = list(references)
    if any(not isinstance(reference, Mapping) for reference in raw_references):
        raise ValueError("every reference must be a mapping")
    reference_rows = [row for row in raw_references if isinstance(row, Mapping)]
    _reference_schema_preflight(reference_rows, contract_version)
    canonical_references = [
        _canonical_reference(reference, contract_version)
        for reference in reference_rows
    ]
    canonical_references.sort(
        key=lambda item: (
            _identity(str(item["path"])),
            str(item["role"]),
            _identity(str(item.get("subject", ""))),
        )
    )
    if contract_version == "v3":
        reference_pack_id = canonical_hash(
            {"cluster_id": cluster_id, "references": canonical_references}
        )
        pack = {
            "reference_pack_id": reference_pack_id,
            "cluster_id": cluster_id,
            "references": canonical_references,
        }
        validate_reference_pack(
            pack, stable_pages=stable_pages, contract_version="v3"
        )
        pack["reference_binding_hash"] = canonical_hash(
            {
                "reference_pack_id": reference_pack_id,
                "references": canonical_references,
            }
        )
        return pack
    stable_records = _stable_page_records(stable_pages)
    reference_pack_id = canonical_hash(
        {
            "cluster_id": cluster_id,
            "references": canonical_references,
            "stable_pages": stable_records,
        }
    )
    pack: dict[str, Any] = {
        "reference_pack_id": reference_pack_id,
        "cluster_id": cluster_id,
        "references": canonical_references,
        "stable_pages": stable_records,
    }
    validate_reference_pack(
        pack,
        stable_pages=stable_records,
        cluster=cluster,
        contract_version="v4",
    )
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
    *,
    contract_version: str = "v4",
) -> dict[str, Any]:
    """Bind an intact pack; reject unaudited replacement of an existing pack."""
    if not isinstance(cluster, Mapping) or not cluster.get("cluster_id"):
        raise ValueError("cluster must contain cluster_id")
    if not isinstance(pack, Mapping) or pack.get("cluster_id") != cluster["cluster_id"]:
        raise ValueError("reference pack cluster_id mismatch")
    references = pack.get("references")
    if not isinstance(references, list):
        raise ValueError("reference pack must contain references")
    _validate_contract_version(contract_version)
    if contract_version == "v3":
        stable = stable_pages
        if stable is None:
            stable = [
                row.get("path")
                for row in references
                if isinstance(row, Mapping) and row.get("role") == "primary_style"
            ]
    else:
        stable = stable_pages if stable_pages is not None else pack.get("stable_pages", [])
    rebuilt = build_reference_pack(
        cluster,
        references,
        stable_pages=stable,
        contract_version=contract_version,
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
