"""Deterministic long-range continuity timelines for characters, props, and scenes."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from pipeline_contracts import canonical_hash, normalize_relative_image_path


SCHEMA_VERSION = "1.0"

_CRITICAL_FIELDS = {
    "character": frozenset(
        {
            "identity",
            "apparent_age",
            "face_shape",
            "skin",
            "skin_tone",
            "eyebrows",
            "eye_shape",
            "eye_color",
            "hair",
            "facial_hair",
            "marks",
            "height",
            "build",
            "shoulder_width",
            "musculature",
            "costume",
            "headwear",
            "crown",
            "ribbon",
            "clothes",
            "shoes",
            "accessories",
            "fixed_props",
            "fixed_weapons",
            "recurring_props",
        }
    ),
    "prop": frozenset(
        {
            "shape",
            "material",
            "color",
            "ornamentation",
            "scale",
            "owner",
            "location",
            "condition",
            "appearance",
            "fixed_attributes",
        }
    ),
    "scene": frozenset(
        {
            "fingerprint",
            "location",
            "indoor_outdoor",
            "entrances",
            "windows",
            "furniture",
            "time",
            "weather",
            "lighting",
            "environmental_state",
            "layout",
            "axis",
        }
    ),
}

_UNKNOWN_TEXT = frozenset({"unknown", "未知", "不明", "未确定", "不确定"})
_FACIAL_HAIR_FIELDS = frozenset({"beard", "moustache", "mustache", "sideburn", "stubble"})
_FACIAL_HAIR_KEY_ALIASES = {
    "beard": "beard",
    "moustache": "moustache",
    "mustache": "moustache",
    "sideburn": "sideburn",
    "sideburns": "sideburn",
    "stubble": "stubble",
}
_NONCRITICAL_STATE_FIELDS = frozenset(
    {
        "pose",
        "grip",
        "expression",
        "camera_angle",
        "camera_distance",
        "shot_size",
        "framing",
        "action",
        "temporary_action",
        "arrangement",
        "noncritical_arrangement",
        "action_equivalence",
        "gesture",
        "gaze",
        "body_position",
        "hand_count",
    }
)
_OBSERVATION_FIELDS = frozenset(
    {"entity_type", "entity_id", "page", "state", "story_order", "confidence"}
)
_TRANSITION_FIELDS = frozenset(
    {
        "entity_type",
        "entity_id",
        "from_page",
        "to_page",
        "kind",
        "source_ref",
        "changed_fields",
        "confidence",
    }
)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a nonempty string")
    result = unicodedata.normalize("NFKC", value).strip()
    if not result:
        raise ValueError(f"{field} must be a nonempty string")
    return result


def _entity_type(value: object) -> str:
    result = _required_text(value, "entity_type").casefold()
    if result not in _CRITICAL_FIELDS:
        raise ValueError(f"unknown entity_type: {value!r}")
    return result


def _confidence(value: object, field: str = "confidence") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number from 0 to 1")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError(f"{field} must be a finite number from 0 to 1")
    return result


def _canonical_json_value(value: object, field: str) -> object:
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field} contains a non-finite number")
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFKC", value).strip()
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field} mapping keys must be strings")
            normalized_key = unicodedata.normalize("NFKC", key).strip()
            if not normalized_key:
                raise ValueError(f"{field} mapping keys must be nonempty strings")
            if normalized_key in result:
                raise ValueError(f"{field} contains duplicate normalized key {normalized_key!r}")
            result[normalized_key] = _canonical_json_value(item, field)
        return {key: result[key] for key in sorted(result)}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical_json_value(item, field) for item in value]
    raise ValueError(f"{field} contains a non-JSON-safe value")


def _remove_unknown(value: object) -> object:
    if isinstance(value, str) and value.casefold() in _UNKNOWN_TEXT:
        return _MISSING
    if value is None:
        return _MISSING
    if isinstance(value, dict):
        cleaned = {
            key: cleaned_item
            for key, item in value.items()
            if (cleaned_item := _remove_unknown(item)) is not _MISSING
        }
        return cleaned if cleaned else _MISSING
    if isinstance(value, list):
        cleaned_list = [
            cleaned_item
            for item in value
            if (cleaned_item := _remove_unknown(item)) is not _MISSING
        ]
        return cleaned_list if cleaned_list else _MISSING
    return value


_MISSING = object()


def _canonical_state(entity_type: str, value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("state must be a mapping")
    canonical = _canonical_json_value(value, "state")
    assert isinstance(canonical, dict)
    allowed_fields = set(_CRITICAL_FIELDS[entity_type]) | set(
        _NONCRITICAL_STATE_FIELDS
    )
    if entity_type == "character":
        allowed_fields.update(_FACIAL_HAIR_FIELDS)
    unknown_fields = set(canonical) - allowed_fields
    if unknown_fields:
        raise ValueError(
            f"unknown state fields for {entity_type}: {sorted(unknown_fields)!r}"
        )
    result: dict[str, object] = {}
    for field in sorted(_CRITICAL_FIELDS[entity_type]):
        if field not in canonical:
            continue
        cleaned = _remove_unknown(canonical[field])
        if cleaned is not _MISSING:
            result[field] = cleaned
    if entity_type == "character":
        facial_hair = result.get("facial_hair")
        if facial_hair is not None and not isinstance(facial_hair, dict):
            raise ValueError("state facial_hair must be a mapping")
        facial_hair_state = _canonicalize_facial_hair(facial_hair or {})
        for alias in sorted(_FACIAL_HAIR_FIELDS):
            if alias not in canonical:
                continue
            cleaned = _remove_unknown(canonical[alias])
            if cleaned is _MISSING:
                continue
            canonical_alias = _FACIAL_HAIR_KEY_ALIASES[alias]
            if isinstance(cleaned, dict):
                cleaned = _canonicalize_facial_hair(cleaned)
            if (
                canonical_alias in facial_hair_state
                and facial_hair_state[canonical_alias] != cleaned
            ):
                raise ValueError(
                    f"state has conflicting facial_hair.{canonical_alias} values"
                )
            facial_hair_state[canonical_alias] = cleaned
        if facial_hair_state:
            result["facial_hair"] = {
                key: facial_hair_state[key] for key in sorted(facial_hair_state)
            }
    return result


def _canonicalize_facial_hair(
    value: Mapping[str, object], path: str = "facial_hair"
) -> dict[str, object]:
    result: dict[str, object] = {}
    for raw_key, raw_value in value.items():
        folded_key = unicodedata.normalize("NFKC", raw_key).strip().casefold()
        key = _FACIAL_HAIR_KEY_ALIASES.get(folded_key, raw_key)
        item = (
            _canonicalize_facial_hair(raw_value, f"{path}.{key}")
            if isinstance(raw_value, Mapping)
            else raw_value
        )
        if key in result and result[key] != item:
            raise ValueError(f"state has conflicting {path}.{key} values")
        result[key] = item
    return {key: result[key] for key in sorted(result)}


def _natural_path_key(path: str) -> tuple[object, ...]:
    normalized_exact = unicodedata.normalize("NFKC", path)
    components = normalized_exact.casefold().split("/")
    natural = tuple(
        tuple(
            (0, int(part)) if part.isdigit() else (1, part)
            for part in re.split(r"(\d+)", component)
            if part
        )
        for component in components
    )
    return natural, normalized_exact


def _page_identity(page: str) -> str:
    return unicodedata.normalize("NFKC", page).casefold()


def _canonical_observations(
    observations: object,
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], list[dict[str, Any]]]]:
    if isinstance(observations, (str, bytes)) or not isinstance(observations, Sequence):
        raise ValueError("observations must be a sequence")

    rows: list[dict[str, Any]] = []
    page_spellings: dict[str, str] = {}
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(observations):
        if not isinstance(raw, Mapping):
            raise ValueError(f"observation at index {index} must be a mapping")
        unknown_keys = set(raw) - _OBSERVATION_FIELDS
        if unknown_keys:
            raise ValueError(
                "observation at index "
                f"{index} has unsupported fields: {sorted(map(str, unknown_keys))!r}"
            )
        entity_type = _entity_type(raw.get("entity_type"))
        entity_id = _required_text(raw.get("entity_id"), "entity_id")
        try:
            page = normalize_relative_image_path(raw.get("page"))
        except ValueError as exc:
            raise ValueError(f"invalid observation page: {exc}") from exc
        identity = _page_identity(page)
        prior_spelling = page_spellings.get(identity)
        if prior_spelling is not None and prior_spelling != page:
            raise ValueError(
                f"page identity collision: {prior_spelling!r} and {page!r}"
            )
        page_spellings[identity] = page
        duplicate_key = (entity_type, entity_id.casefold(), identity)
        if duplicate_key in seen:
            raise ValueError(
                f"duplicate observation for {entity_type} {entity_id!r} on {page!r}"
            )
        seen.add(duplicate_key)

        state = _canonical_state(entity_type, raw.get("state"))
        row: dict[str, Any] = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "page": page,
            "state": state,
            "state_hash": canonical_hash(state),
        }
        if "story_order" in raw:
            story_order = raw["story_order"]
            if (
                isinstance(story_order, bool)
                or not isinstance(story_order, int)
                or story_order < 0
            ):
                raise ValueError("story_order must be a nonnegative integer")
            row["story_order"] = story_order
        if "confidence" in raw:
            row["confidence"] = _confidence(raw["confidence"])
        rows.append(row)

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    display_ids: dict[tuple[str, str], str] = {}
    for row in rows:
        key = (row["entity_type"], row["entity_id"].casefold())
        prior = display_ids.get(key)
        if prior is not None and prior != row["entity_id"]:
            raise ValueError(
                f"entity_id normalized collision: {prior!r} and {row['entity_id']!r}"
            )
        display_ids[key] = row["entity_id"]
        groups.setdefault(key, []).append(row)

    for key, group in groups.items():
        has_order = ["story_order" in row for row in group]
        if any(has_order) and not all(has_order):
            raise ValueError(
                f"mixed explicit story_order for {key[0]} {display_ids[key]!r}"
            )
        if all(has_order):
            order_values = [row["story_order"] for row in group]
            if len(set(order_values)) != len(order_values):
                raise ValueError(
                    f"duplicate story_order for {key[0]} {display_ids[key]!r}"
                )
            group.sort(key=lambda row: (row["story_order"], _natural_path_key(row["page"])))
        else:
            group.sort(key=lambda row: _natural_path_key(row["page"]))
    return rows, groups


def _canonical_transitions(
    transitions: object,
    groups: Mapping[tuple[str, str], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    if isinstance(transitions, (str, bytes)) or not isinstance(transitions, Sequence):
        raise ValueError("transitions must be a sequence")

    canonical: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(transitions):
        if not isinstance(raw, Mapping):
            raise ValueError(f"transition at index {index} must be a mapping")
        unknown_keys = set(raw) - _TRANSITION_FIELDS
        if unknown_keys:
            raise ValueError(
                f"transition at index {index} has unsupported fields: {sorted(map(str, unknown_keys))!r}"
            )
        entity_type = _entity_type(raw.get("entity_type"))
        entity_id = _required_text(raw.get("entity_id"), "entity_id")
        key = (entity_type, entity_id.casefold())
        if key not in groups:
            raise ValueError(f"transition references unknown entity: {entity_type} {entity_id!r}")
        try:
            from_page = normalize_relative_image_path(raw.get("from_page"))
            to_page = normalize_relative_image_path(raw.get("to_page"))
        except ValueError as exc:
            raise ValueError(f"invalid transition endpoint: {exc}") from exc
        kind = _required_text(raw.get("kind"), "transition kind")
        source_ref = _required_text(raw.get("source_ref"), "transition source_ref")
        changed_fields: list[str] | None = None
        if "changed_fields" in raw:
            fields_value = raw["changed_fields"]
            if (
                isinstance(fields_value, (str, bytes))
                or not isinstance(fields_value, Sequence)
                or not fields_value
            ):
                raise ValueError("transition changed_fields must be a nonempty sequence")
            changed_fields = sorted(
                {
                    _required_text(field, "transition changed_fields item")
                    for field in fields_value
                }
            )
            if len(changed_fields) != len(fields_value):
                raise ValueError("transition changed_fields contains duplicates")
            invalid_fields = set(changed_fields) - _CRITICAL_FIELDS[entity_type]
            if invalid_fields:
                raise ValueError(
                    "transition changed_fields are invalid for "
                    f"{entity_type}: {sorted(invalid_fields)!r}"
                )

        pages = [row["page"] for row in groups[key]]
        if from_page not in pages or to_page not in pages:
            raise ValueError(
                f"transition endpoint does not exist for {entity_type} {entity_id!r}"
            )
        from_index = pages.index(from_page)
        to_index = pages.index(to_page)
        if from_index >= to_index:
            raise ValueError("transition endpoint order must be forward")
        if to_index != from_index + 1:
            raise ValueError("transition endpoints must be adjacent entity observations")

        row: dict[str, Any] = {
            "entity_type": entity_type,
            "entity_id": groups[key][0]["entity_id"],
            "from_page": from_page,
            "to_page": to_page,
            "kind": kind,
            "source_ref": source_ref,
        }
        if changed_fields is not None:
            row["changed_fields"] = changed_fields
        if "confidence" in raw:
            row["confidence"] = _confidence(raw["confidence"], "transition confidence")
        digest = canonical_hash(row)
        if digest in seen:
            raise ValueError(f"duplicate transition for {entity_id!r} {from_page!r}->{to_page!r}")
        seen.add(digest)
        canonical.append(row)

    canonical.sort(
        key=lambda row: (
            row["entity_type"],
            row["entity_id"].casefold(),
            _natural_path_key(row["from_page"]),
            _natural_path_key(row["to_page"]),
            row["kind"],
        )
    )
    return canonical


def _validate_changes(
    groups: Mapping[tuple[str, str], list[dict[str, Any]]],
    transitions: list[dict[str, Any]],
) -> None:
    by_interval = {
        (
            row["entity_type"],
            row["entity_id"].casefold(),
            row["from_page"],
            row["to_page"],
        ): row
        for row in transitions
    }
    consumed: set[tuple[str, str, str, str]] = set()
    for key in sorted(groups):
        observations = groups[key]
        if not observations:
            continue
        effective = dict(observations[0]["state"])
        for previous, current in zip(observations, observations[1:]):
            next_effective = _deep_merge_state(effective, current["state"])
            changed = sorted(
                field
                for field in _CRITICAL_FIELDS[key[0]]
                if field in next_effective
                and field in effective
                and next_effective[field] != effective[field]
            )
            interval = (key[0], key[1], previous["page"], current["page"])
            supplied = by_interval.get(interval)
            if changed:
                if supplied is None:
                    raise ValueError(
                        "unsupported state transition for "
                        f"{key[0]} {previous['entity_id']!r} between "
                        f"{previous['page']!r} and {current['page']!r}: "
                        f"fields {changed!r}"
                    )
                if len(changed) > 1 and "changed_fields" not in supplied:
                    raise ValueError(
                        "transition covering multiple critical fields requires explicit "
                        f"changed_fields for {key[0]} {previous['entity_id']!r} "
                        f"between {previous['page']!r} and {current['page']!r}: "
                        f"fields {changed!r}"
                    )
                if (
                    "changed_fields" in supplied
                    and supplied["changed_fields"] != changed
                ):
                    raise ValueError(
                        "transition changed_fields mismatch for "
                        f"{key[0]} {previous['entity_id']!r} between "
                        f"{previous['page']!r} and {current['page']!r}: "
                        f"expected {changed!r}, got {supplied['changed_fields']!r}"
                    )
                consumed.add(interval)
            elif supplied is not None:
                raise ValueError(
                    "transition has no matching critical state change for "
                    f"{key[0]} {previous['entity_id']!r} between "
                    f"{previous['page']!r} and {current['page']!r}"
                )
            effective = next_effective

    if len(consumed) != len(transitions):
        raise ValueError("transition does not match an adjacent critical state change")


def _deep_merge_state(
    previous: Mapping[str, object], current: Mapping[str, object]
) -> dict[str, object]:
    """Merge partial mapping facts; lists and scalar values replace atomically."""
    result: dict[str, object] = dict(previous)
    for key, value in current.items():
        prior_value = result.get(key, _MISSING)
        if isinstance(prior_value, Mapping) and isinstance(value, Mapping):
            result[key] = _deep_merge_state(prior_value, value)
        else:
            result[key] = value
    return result


def build_timeline(observations: object, transitions: object) -> dict[str, Any]:
    """Build a canonical content-addressed continuity timeline."""
    _, groups = _canonical_observations(observations)
    canonical_transitions = _canonical_transitions(transitions, groups)

    entities: list[dict[str, Any]] = []
    for key in sorted(groups):
        group = groups[key]
        entities.append(
            {
                "entity_type": key[0],
                "entity_id": group[0]["entity_id"],
                "observations": [
                    {
                        item_key: item[item_key]
                        for item_key in (
                            "page",
                            "story_order",
                            "confidence",
                            "state",
                            "state_hash",
                        )
                        if item_key in item
                    }
                    for item in group
                ],
            }
        )
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "entities": entities,
        "transitions": canonical_transitions,
    }
    payload["content_hash"] = canonical_hash(payload)
    return payload


def validate_timeline(timeline: object) -> bool:
    """Rebuild a serialized timeline and reject any altered state or content hash."""
    if not isinstance(timeline, Mapping):
        raise ValueError("timeline must be a mapping")
    if timeline.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"timeline schema_version must be {SCHEMA_VERSION}")
    entities = timeline.get("entities")
    transitions = timeline.get("transitions")
    if isinstance(entities, (str, bytes)) or not isinstance(entities, Sequence):
        raise ValueError("timeline entities must be a sequence")

    flattened: list[dict[str, Any]] = []
    for entity_index, entity in enumerate(entities):
        if not isinstance(entity, Mapping):
            raise ValueError(f"timeline entity at index {entity_index} must be a mapping")
        observations = entity.get("observations")
        if (
            isinstance(observations, (str, bytes))
            or not isinstance(observations, Sequence)
        ):
            raise ValueError("timeline entity observations must be a sequence")
        for item in observations:
            if not isinstance(item, Mapping):
                raise ValueError("timeline observation must be a mapping")
            state = item.get("state")
            expected_state_hash = canonical_hash(state)
            if item.get("state_hash") != expected_state_hash:
                raise ValueError(
                    f"state_hash mismatch for {entity.get('entity_id')!r} {item.get('page')!r}"
                )
            row = {
                "entity_type": entity.get("entity_type"),
                "entity_id": entity.get("entity_id"),
                "page": item.get("page"),
                "state": state,
            }
            if "story_order" in item:
                row["story_order"] = item["story_order"]
            if "confidence" in item:
                row["confidence"] = item["confidence"]
            flattened.append(row)

    rebuilt = build_timeline(flattened, transitions)
    if timeline.get("content_hash") != rebuilt["content_hash"]:
        raise ValueError("content_hash mismatch")
    if dict(timeline) != rebuilt:
        raise ValueError("timeline content is not canonical")
    _, groups = _canonical_observations(flattened)
    assert isinstance(transitions, Sequence)
    canonical_transitions = _canonical_transitions(transitions, groups)
    _validate_changes(groups, canonical_transitions)
    return True


__all__ = ["SCHEMA_VERSION", "build_timeline", "validate_timeline"]
