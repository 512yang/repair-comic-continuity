from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


KNOWN_CATEGORIES = frozenset(
    {"sans", "serif", "rounded", "handwriting", "display", "condensed"}
)
KNOWN_ROLES = frozenset(
    {"dialogue", "narration", "black_caption", "title", "vertical_text"}
)


@dataclass(frozen=True)
class FontCandidate:
    family: str
    path: Path
    category: str
    weight: int
    roles: tuple[str, ...]
    license_name: str
    source_url: str
    license_url: str
    license_path: Path
    identifier: str
    sha256: str | None = None
    license_sha256: str | None = None
    variable_weight: bool = False
    resource_root: Path = field(default=Path("font_resources"), repr=False, compare=False)


def _require_text(item: FontCandidate, field_name: str) -> None:
    value = getattr(item, field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{item.identifier or '<unknown>'}: missing {field_name}")


def _validate_url(
    item: FontCandidate, field_name: str, *, allow_file_urls: bool
) -> None:
    _require_text(item, field_name)
    parsed = urlparse(getattr(item, field_name))
    if parsed.scheme == "https" and parsed.hostname:
        return
    if parsed.scheme == "file" and allow_file_urls:
        return
    if parsed.scheme == "file":
        raise ValueError(f"{item.identifier}: {field_name} file URL is test-only")
    raise ValueError(f"{item.identifier}: invalid {field_name}")


def _is_reparse_point(path: Path) -> bool:
    if os.path.islink(path):
        return True
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & getattr(os, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _validate_whitelist_root(root: Path) -> None:
    if _is_reparse_point(root):
        raise ValueError(f"resource whitelist root is a reparse point: {root}")
    if root.exists() and not root.is_dir():
        raise ValueError(f"resource whitelist root is not a directory: {root}")


def _validate_resource_roots(items: list[FontCandidate]) -> None:
    checked: set[Path] = set()
    for item in items:
        for root in (item.resource_root / "fonts", item.resource_root / "licenses"):
            absolute = root.absolute()
            if absolute not in checked:
                _validate_whitelist_root(absolute)
                checked.add(absolute)


def _validate_variable_weight(item: FontCandidate) -> None:
    if type(item.variable_weight) is not bool:
        raise ValueError(f"{item.identifier}: variable_weight must be a bool")


def _unsupported_version(payload: dict[str, object]) -> bool:
    version = payload.get("version")
    return type(version) is not int or version != 1


def _validate_catalog_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or not isinstance(payload.get("fonts"), list):
        raise ValueError("catalog must contain a fonts list")
    if _unsupported_version(payload):
        raise ValueError("catalog version must equal supported version 1")
    return payload


def _variable_weight(raw: dict[str, object]) -> bool:
    value = raw.get("variable_weight", False)
    if type(value) is not bool:
        raise ValueError("invalid catalog entry: variable_weight must be a bool")
    return value


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def _validate_digest(item: FontCandidate, field_name: str) -> None:
    digest = getattr(item, field_name)
    if digest is None:
        raise ValueError(f"{item.identifier}: missing {field_name}")
    if len(digest) != 64 or any(char not in "0123456789abcdefABCDEF" for char in digest):
        raise ValueError(f"{item.identifier}: invalid {field_name}")


def validate_catalog(
    items: list[FontCandidate], *, allow_file_urls: bool = False
) -> None:
    _validate_resource_roots(items)
    identifiers: set[str] = set()
    for item in items:
        for field_name in ("identifier", "family", "license_name"):
            _require_text(item, field_name)
        _validate_url(item, "source_url", allow_file_urls=allow_file_urls)
        _validate_url(item, "license_url", allow_file_urls=allow_file_urls)
        _validate_variable_weight(item)

        if item.identifier in identifiers:
            raise ValueError(f"duplicate identifier: {item.identifier}")
        identifiers.add(item.identifier)

        if item.category not in KNOWN_CATEGORIES:
            raise ValueError(f"{item.identifier}: unknown category {item.category!r}")
        if not isinstance(item.weight, int) or isinstance(item.weight, bool) or not 1 <= item.weight <= 1000:
            raise ValueError(f"{item.identifier}: invalid weight {item.weight!r}")
        if not item.roles:
            raise ValueError(f"{item.identifier}: roles must not be empty")
        unknown_roles = set(item.roles) - KNOWN_ROLES
        if unknown_roles:
            raise ValueError(f"{item.identifier}: unknown role {sorted(unknown_roles)[0]!r}")

        if not _is_within(item.path, item.resource_root / "fonts"):
            raise ValueError(f"{item.identifier}: font path escapes font_resources/fonts")
        if not _is_within(item.license_path, item.resource_root / "licenses"):
            raise ValueError(f"{item.identifier}: license path escapes font_resources/licenses")
        _validate_digest(item, "sha256")
        _validate_digest(item, "license_sha256")


def load_catalog(
    path: str | Path, *, allow_file_urls: bool = False
) -> list[FontCandidate]:
    catalog_path = Path(path).resolve()
    payload = _validate_catalog_payload(
        json.loads(catalog_path.read_text(encoding="utf-8"))
    )

    resource_root = catalog_path.parent
    items: list[FontCandidate] = []
    for raw in payload["fonts"]:
        if not isinstance(raw, dict):
            raise ValueError("each catalog font must be an object")
        try:
            items.append(
                FontCandidate(
                    identifier=raw["identifier"],
                    family=raw["family"],
                    path=resource_root / raw["path"],
                    category=raw["category"],
                    weight=raw["weight"],
                    roles=tuple(raw["roles"]),
                    license_name=raw["license_name"],
                    source_url=raw["source_url"],
                    license_url=raw["license_url"],
                    license_path=resource_root / raw["license_path"],
                    sha256=raw["sha256"],
                    license_sha256=raw["license_sha256"],
                    variable_weight=_variable_weight(raw),
                    resource_root=resource_root,
                )
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"invalid catalog entry: {exc}") from exc
    validate_catalog(items, allow_file_urls=allow_file_urls)
    return items


def candidates_for_role(
    items: list[FontCandidate],
    role: str,
    categories: set[str] | frozenset[str] | None = None,
) -> list[FontCandidate]:
    if role not in KNOWN_ROLES:
        raise ValueError(f"unknown role {role!r}")
    return [
        item
        for item in items
        if role in item.roles and (categories is None or item.category in categories)
    ]


def available_fonts(items: list[FontCandidate]) -> list[FontCandidate]:
    return [item for item in items if item.path.is_file()]
