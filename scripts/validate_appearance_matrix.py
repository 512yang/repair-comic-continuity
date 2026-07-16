#!/usr/bin/env python3
"""Validate the mandatory cross-page character appearance continuity matrix."""

from __future__ import annotations

import argparse
import copy
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath


TRAITS = (
    "skin_tone",
    "hair",
    "facial_hair",
    "clothing",
    "face_shape",
    "body_build",
)
OBSERVATION_STATES = {"match", "drift", "not_visible"}
MATRIX_STATES = {"confirmed", "defects_confirmed", "evidence_blocked"}
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value.strip()


def _sha(value, field):
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be lowercase sha256")
    return value


def _relative_path(value, field):
    text = _text(value, field).replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field} must be a safe relative path")
    return str(path)


def _artifact(value, field):
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an artifact mapping")
    return {
        "path": _relative_path(value.get("path"), f"{field}.path"),
        "sha256": _sha(value.get("sha256"), f"{field}.sha256"),
    }


def validate_matrix(value):
    if not isinstance(value, Mapping):
        raise ValueError("appearance matrix must be a mapping")
    result = copy.deepcopy(dict(value))
    if result.get("version") != 1:
        raise ValueError("version must be 1")
    status = result.get("status")
    if status not in MATRIX_STATES:
        raise ValueError(
            "status must be confirmed, defects_confirmed, or evidence_blocked"
        )
    result["cluster_id"] = _text(result.get("cluster_id"), "cluster_id")
    pages = result.get("cluster_pages")
    if (
        isinstance(pages, (str, bytes)) or not isinstance(pages, Sequence)
        or not pages
    ):
        raise ValueError("cluster_pages must be a non-empty list")
    pages = [_relative_path(page, "cluster_pages page") for page in pages]
    if len(set(pages)) != len(pages):
        raise ValueError("cluster_pages must be unique")
    result["cluster_pages"] = pages
    characters = result.get("characters")
    if (
        isinstance(characters, (str, bytes))
        or not isinstance(characters, Sequence)
        or not characters
    ):
        raise ValueError("characters must be a non-empty list")
    normalized_characters = []
    entity_ids = set()
    for index, raw in enumerate(characters):
        if not isinstance(raw, Mapping):
            raise ValueError(f"characters[{index}] must be a mapping")
        row = copy.deepcopy(dict(raw))
        entity_id = _text(row.get("entity_id"), f"characters[{index}].entity_id")
        if entity_id in entity_ids:
            raise ValueError(f"duplicate entity_id: {entity_id}")
        entity_ids.add(entity_id)
        row["entity_id"] = entity_id
        row["reference"] = _artifact(row.get("reference"), f"{entity_id}.reference")
        baseline = row.get("baseline")
        if not isinstance(baseline, Mapping) or set(baseline) != set(TRAITS):
            raise ValueError(f"{entity_id}.baseline must contain exactly {TRAITS}")
        row["baseline"] = {
            trait: _text(baseline.get(trait), f"{entity_id}.baseline.{trait}")
            for trait in TRAITS
        }
        if row.get("status") not in {"passed", "defect"}:
            raise ValueError(f"{entity_id}.status must be passed or defect")
        observations = row.get("observations")
        if isinstance(observations, (str, bytes)) or not isinstance(observations, Sequence):
            raise ValueError(f"{entity_id}.observations must be a list")
        normalized_observations = []
        observed_pages = []
        has_drift = False
        for obs_index, raw_observation in enumerate(observations):
            if not isinstance(raw_observation, Mapping):
                raise ValueError(f"{entity_id}.observations[{obs_index}] must be a mapping")
            observation = copy.deepcopy(dict(raw_observation))
            page = _relative_path(observation.get("page"), f"{entity_id}.observation.page")
            observed_pages.append(page)
            present = observation.get("present")
            if not isinstance(present, bool):
                raise ValueError(f"{entity_id} {page} present must be boolean")
            if observation.get("full_resolution") is not True:
                raise ValueError(f"{entity_id} {page} requires full_resolution=true")
            drift_traits = []
            for trait in TRAITS:
                trait_status = observation.get(trait)
                if trait_status not in OBSERVATION_STATES:
                    raise ValueError(f"{entity_id} {page} {trait} status is invalid")
                if present and trait_status == "not_visible":
                    raise ValueError(f"present observation cannot mark {trait} not_visible")
                if not present and trait_status != "not_visible":
                    raise ValueError(f"absent observation must mark {trait} not_visible")
                if trait_status == "drift":
                    has_drift = True
                    drift_traits.append(trait)
                    if status == "confirmed":
                        raise ValueError(f"{entity_id} {page} {trait} drift blocks confirmation")
            defect_review = observation.get("defect_review")
            if drift_traits and status == "defects_confirmed":
                if not isinstance(defect_review, Mapping):
                    raise ValueError(f"{entity_id} {page} confirmed drift requires defect_review")
                if set(defect_review) != {"decision", "traits", "reviewer_ids", "reason"}:
                    raise ValueError(f"{entity_id} {page} defect_review contract is invalid")
                if defect_review.get("decision") != "confirmed_defect":
                    raise ValueError(f"{entity_id} {page} defect_review decision is invalid")
                traits = defect_review.get("traits")
                if not isinstance(traits, Sequence) or isinstance(traits, (str, bytes)):
                    raise ValueError(f"{entity_id} {page} defect_review traits are invalid")
                if set(traits) != set(drift_traits) or len(traits) != len(drift_traits):
                    raise ValueError(f"{entity_id} {page} defect_review must cover drift traits")
                reviewers = defect_review.get("reviewer_ids")
                if (
                    not isinstance(reviewers, Sequence)
                    or isinstance(reviewers, (str, bytes))
                    or len({_text(item, "independent reviewer id") for item in reviewers}) < 2
                ):
                    raise ValueError(
                        f"{entity_id} {page} requires two independent reviewer ids"
                    )
                _text(defect_review.get("reason"), f"{entity_id} {page} defect_review.reason")
            elif defect_review is not None and not drift_traits:
                raise ValueError(f"{entity_id} {page} defect_review requires drift")
            observation["lighting_explanation"] = _text(
                observation.get("lighting_explanation"),
                f"{entity_id} {page} lighting_explanation",
            )
            observation["evidence"] = _artifact(
                observation.get("evidence"), f"{entity_id} {page} evidence"
            )
            observation["page"] = page
            normalized_observations.append(observation)
        if observed_pages != pages:
            raise ValueError(f"{entity_id} observations must exactly cover cluster_pages in order")
        if row["status"] == "passed" and has_drift:
            raise ValueError(f"{entity_id} passed status cannot contain drift")
        if status == "defects_confirmed" and has_drift and row["status"] != "defect":
            raise ValueError(f"{entity_id} confirmed drift requires defect status")
        if status == "confirmed" and row["status"] != "passed":
            raise ValueError(f"{entity_id} defect blocks confirmed matrix")
        row["observations"] = normalized_observations
        normalized_characters.append(row)
    result["characters"] = normalized_characters
    if status == "defects_confirmed" and not any(
        row["status"] == "defect" for row in normalized_characters
    ):
        raise ValueError("defects_confirmed matrix requires at least one defect")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("matrix", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.matrix.read_text(encoding="utf-8-sig"))
    normalized = validate_matrix(payload)
    print(json.dumps({
        "status": normalized["status"],
        "cluster_id": normalized["cluster_id"],
        "page_count": len(normalized["cluster_pages"]),
        "character_count": len(normalized["characters"]),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
