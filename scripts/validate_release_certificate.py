#!/usr/bin/env python3
"""Verify the signed generic V5 detection-benchmark release certificate."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from pipeline_version import ORCHESTRATION_PIPELINE_ID, RELEASE_CERTIFICATE_ID
from validate_detection_benchmark import validate_detection_benchmark


CERTIFICATE_PATH = Path("assets/release_certificate.json")
PUBLIC_KEY_PATH = Path("assets/release_public_key.json")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
RSA_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
REQUIRED_CONTROL_FILES = frozenset(
    {
        "SKILL.md",
        "agents/openai.yaml",
        "references/continuity-rules.md",
        "references/failure-learning.md",
        "references/qa-checklist.md",
        "references/scene-cluster-pipeline.md",
        "references/text-engine.md",
        "scripts/pipeline_version.py",
        "scripts/closed_loop_controller.py",
        "scripts/release_gate.py",
        "scripts/human_issue_learning.py",
        "scripts/prompt_compiler.py",
        "scripts/validate_appearance_matrix.py",
        "scripts/validate_audit.py",
        "scripts/validate_detection_benchmark.py",
        "scripts/validate_human_issue_annotations.py",
        "scripts/validate_human_visual_selection.py",
        "scripts/validate_release_certificate.py",
        "scripts/validate_source_text_audit.py",
        "assets/release_evidence/golden_text_detection_v2.json",
        "assets/release_evidence/detected_text_detection_v2.json",
    }
)


def _load_json(path: Path, field: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} is unreadable") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _exact(value: object, keys: set[str], field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{field} must contain exactly {sorted(keys)}")
    return value


def _sha256(path: Path) -> str:
    """Hash signed text canonically so Git/Windows line endings stay portable."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"signed text artifact is unreadable: {path.name}") from exc
    canonical = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _safe_path(root: Path, value: object, field: str) -> Path:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{field} must be a nonempty relative path")
    relative = PurePosixPath(value.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
        raise ValueError(f"{field} must be a safe relative path")
    root = root.resolve()
    path = (root / Path(*relative.parts)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{field} escapes Skill root") from exc
    return path


def _artifact(root: Path, value: object, field: str) -> Path:
    row = _exact(value, {"path", "sha256"}, field)
    digest = row.get("sha256")
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise ValueError(f"{field} sha256 is invalid")
    path = _safe_path(root, row.get("path"), f"{field}.path")
    if not path.is_file() or _sha256(path) != digest:
        raise ValueError(f"{field} hash mismatch")
    return path


def _canonical_payload(certificate: Mapping[str, Any]) -> bytes:
    payload = dict(certificate)
    payload.pop("signature", None)
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def verify_certificate_signature(
    certificate: Mapping[str, Any], public_key: Mapping[str, Any]
) -> None:
    key = _exact(
        public_key,
        {"schema_version", "algorithm", "key_id", "modulus_hex", "exponent"},
        "release public key",
    )
    if key.get("schema_version") != "1.0" or key.get("algorithm") != "rsa-pkcs1v15-sha256":
        raise ValueError("release public key contract mismatch")
    if certificate.get("key_id") != key.get("key_id"):
        raise ValueError("certificate key_id mismatch")
    try:
        modulus = int(str(key.get("modulus_hex")), 16)
        exponent = int(key.get("exponent"))
        signature = base64.b64decode(str(certificate.get("signature")), validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("certificate signature encoding is invalid") from exc
    width = (modulus.bit_length() + 7) // 8
    if width < len(RSA_SHA256_DIGEST_INFO) + 35 or len(signature) != width:
        raise ValueError("certificate signature size is invalid")
    digest = hashlib.sha256(_canonical_payload(certificate)).digest()
    expected = (
        b"\x00\x01"
        + b"\xff" * (width - len(RSA_SHA256_DIGEST_INFO) - len(digest) - 3)
        + b"\x00"
        + RSA_SHA256_DIGEST_INFO
        + digest
    )
    decoded = pow(int.from_bytes(signature, "big"), exponent, modulus).to_bytes(
        width, "big"
    )
    if not hmac.compare_digest(decoded, expected):
        raise ValueError("certificate signature verification failed")


def _validate_manifest(root: Path, path: Path, release_id: str) -> None:
    manifest = _load_json(path, "runtime manifest")
    _exact(manifest, {"schema_version", "release_id", "scope", "files"}, "runtime manifest")
    if (
        manifest.get("schema_version") != "1.0"
        or manifest.get("release_id") != release_id
        or manifest.get("scope") != "generic_detection_control_plane"
    ):
        raise ValueError("runtime manifest contract mismatch")
    rows = manifest.get("files")
    if not isinstance(rows, list) or not rows:
        raise ValueError("runtime manifest files must be nonempty")
    seen: set[str] = set()
    for index, item in enumerate(rows):
        row = _exact(item, {"path", "sha256"}, f"runtime manifest files[{index}]")
        relative = row.get("path")
        if not isinstance(relative, str) or relative in seen:
            raise ValueError("runtime manifest contains duplicate or invalid path")
        seen.add(relative)
        digest = row.get("sha256")
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise ValueError("runtime manifest sha256 is invalid")
        current = _safe_path(root, relative, "runtime manifest path")
        if not current.is_file() or _sha256(current) != digest:
            raise ValueError(f"runtime manifest hash mismatch: {relative}")
    if not REQUIRED_CONTROL_FILES.issubset(seen):
        missing = sorted(REQUIRED_CONTROL_FILES - seen)[0]
        raise ValueError(f"runtime manifest is missing required control file: {missing}")


def validate_release_certificate(root: Path) -> dict[str, Any]:
    root = Path(root).resolve()
    certificate = _load_json(root / CERTIFICATE_PATH, "release certificate")
    public_key = _load_json(root / PUBLIC_KEY_PATH, "release public key")
    _exact(
        certificate,
        {
            "schema_version",
            "release_id",
            "pipeline_id",
            "scope",
            "issued_at",
            "key_id",
            "signature_algorithm",
            "runtime_manifest",
            "golden",
            "detected",
            "benchmark",
            "signature",
        },
        "release certificate",
    )
    if (
        certificate.get("schema_version") != "1.0"
        or certificate.get("release_id") != RELEASE_CERTIFICATE_ID
        or certificate.get("pipeline_id") != ORCHESTRATION_PIPELINE_ID
        or certificate.get("scope") != "generic_detection_benchmark"
        or certificate.get("signature_algorithm") != "rsa-pkcs1v15-sha256"
    ):
        raise ValueError("release certificate contract mismatch")
    try:
        issued = datetime.fromisoformat(str(certificate.get("issued_at")).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("release certificate issued_at is invalid") from exc
    if issued.utcoffset() is None:
        raise ValueError("release certificate issued_at requires timezone")
    verify_certificate_signature(certificate, public_key)

    manifest_path = _artifact(root, certificate.get("runtime_manifest"), "runtime manifest")
    golden_path = _artifact(root, certificate.get("golden"), "golden benchmark")
    detected_path = _artifact(root, certificate.get("detected"), "detected benchmark")
    _validate_manifest(root, manifest_path, RELEASE_CERTIFICATE_ID)
    benchmark = validate_detection_benchmark(golden_path, detected_path)
    declared = _exact(
        certificate.get("benchmark"),
        {
            "page_count",
            "missed_confirmed_defects",
            "false_positive_defects",
            "correct_page_false_positive_count",
            "text_recall",
        },
        "certificate benchmark",
    )
    actual = {
        "page_count": benchmark["page_count"],
        "missed_confirmed_defects": len(benchmark["missed_confirmed_defects"]),
        "false_positive_defects": len(benchmark["false_positive_defects"]),
        "correct_page_false_positive_count": benchmark[
            "correct_page_false_positive_count"
        ],
        "text_recall": benchmark["text_recall"],
    }
    if dict(declared) != actual:
        raise ValueError("certificate benchmark claims do not match current evidence")
    return {
        "status": "certificate_valid",
        "release_id": RELEASE_CERTIFICATE_ID,
        "pipeline_id": ORCHESTRATION_PIPELINE_ID,
        **actual,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = validate_release_certificate(args.root)
    except (OSError, ValueError) as exc:
        result = {"status": "certificate_invalid", "error": str(exc)}
        code = 1
    else:
        code = 0
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    elif code == 0:
        print(f"PASS: {result['release_id']}")
    else:
        print(f"FAIL: {result['error']}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
