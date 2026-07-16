#!/usr/bin/env python3
"""Content-addressed cache keys for machine-derived comic audit artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Iterable


CACHEABLE_KINDS = frozenset(
    {
        "machine_alignment",
        "ocr_proposal",
        "source_crop",
        "unchanged_evidence",
        "appearance_measurement",
    }
)


def make_audit_cache_key(
    *,
    pipeline_version: str,
    mode: str,
    novel_sha256: str,
    reference_sha256s: Iterable[str],
    source_page_sha256: str,
    cluster_id: str,
    rule_sha256s: Iterable[str],
) -> str:
    if not pipeline_version or not mode or not cluster_id:
        raise ValueError("pipeline_version, mode, and cluster_id are required")
    references = _hash_set(reference_sha256s, "reference_sha256s")
    rules = _hash_set(rule_sha256s, "rule_sha256s")
    _require_sha256(novel_sha256, "novel_sha256")
    _require_sha256(source_page_sha256, "source_page_sha256")
    facts = {
        "pipeline_version": pipeline_version,
        "mode": mode,
        "novel_sha256": novel_sha256,
        "reference_sha256s": references,
        "source_page_sha256": source_page_sha256,
        "cluster_id": cluster_id,
        "rule_sha256s": rules,
    }
    encoded = json.dumps(
        facts, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_cache_entry(
    *,
    kind: str,
    artifact_path: str,
    artifact_sha256: str,
    **facts,
) -> dict:
    if kind not in CACHEABLE_KINDS:
        raise ValueError(f"artifact kind is not cacheable: {kind}")
    if not artifact_path or Path(artifact_path).is_absolute() or ".." in Path(artifact_path).parts:
        raise ValueError("artifact_path must be a safe relative path")
    _require_sha256(artifact_sha256, "artifact_sha256")
    key = make_audit_cache_key(**facts)
    return {
        "schema_version": "comic-audit-cache-v1",
        "cache_key": key,
        "kind": kind,
        "artifact_path": artifact_path.replace("\\", "/"),
        "artifact_sha256": artifact_sha256,
        "facts": {
            **facts,
            "reference_sha256s": sorted(set(facts["reference_sha256s"])),
            "rule_sha256s": sorted(set(facts["rule_sha256s"])),
        },
        "human_approval_cached": False,
    }


def cache_entry_matches(entry: object, **facts) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("kind") not in CACHEABLE_KINDS:
        return False
    if entry.get("human_approval_cached") is not False:
        return False
    try:
        return entry.get("cache_key") == make_audit_cache_key(**facts)
    except (KeyError, TypeError, ValueError):
        return False


def _hash_set(values: Iterable[str], field: str) -> list[str]:
    result = sorted(set(values))
    for value in result:
        _require_sha256(value, field)
    return result


def _require_sha256(value: str, field: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{field} must contain lowercase sha256 values")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("facts_json")
    args = parser.parse_args()
    facts = json.loads(Path(args.facts_json).read_text(encoding="utf-8"))
    print(json.dumps({"cache_key": make_audit_cache_key(**facts)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
