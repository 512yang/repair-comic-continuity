from __future__ import annotations

import argparse
import errno
import hashlib
import http.client
import os
import shutil
import stat
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError

from comic_repair.font_catalog import FontCandidate, load_catalog


DownloadFunc = Callable[[str, Path], None]
FONT_SIGNATURES = {b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf", b"wOFF", b"wOF2"}


@dataclass
class DownloadStats:
    installed: int = 0
    skipped: int = 0
    failed: int = 0
    missing: int = 0


@dataclass(frozen=True)
class _Resource:
    url: str
    target: Path
    allowed_root: Path
    sha256: str | None
    is_font: bool


class ResourceValidationError(ValueError):
    pass


@dataclass(frozen=True)
class _RootState:
    path: Path
    resolved: Path
    device: int
    inode: int


def _is_reparse_point(path: Path) -> bool:
    if os.path.islink(path):
        return True
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & getattr(os, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _capture_safe_root(root: Path) -> _RootState:
    root = root.absolute()
    if _is_reparse_point(root):
        raise ResourceValidationError(f"resource root is a reparse point: {root}")
    try:
        root_stat = root.stat(follow_symlinks=False)
    except OSError as exc:
        raise ResourceValidationError(f"resource root is unavailable: {root}") from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise ResourceValidationError(f"resource root is not a directory: {root}")
    return _RootState(root, root.resolve(strict=True), root_stat.st_dev, root_stat.st_ino)


def _assert_directory_safe(directory: Path, root_state: _RootState) -> None:
    current = _capture_safe_root(root_state.path)
    if current != root_state:
        raise ResourceValidationError(f"resource root changed: {root_state.path}")
    try:
        resolved = directory.resolve(strict=True)
        relative = resolved.relative_to(root_state.resolved)
    except (OSError, ValueError) as exc:
        raise ResourceValidationError(
            f"target directory escapes resource root: {directory}"
        ) from exc

    lexical_parent = root_state.path
    for component in relative.parts:
        lexical_parent /= component
        if _is_reparse_point(lexical_parent):
            raise ResourceValidationError(
                f"target parent contains a reparse point: {lexical_parent}"
            )


def _assert_destination_safe(path: Path, root_state: _RootState) -> None:
    _assert_directory_safe(path.parent, root_state)


def _prepare_allowed_root(root: Path) -> _RootState:
    root = root.absolute()
    if os.path.lexists(root):
        return _capture_safe_root(root)

    container_state = _capture_safe_root(root.parent)
    _assert_destination_safe(root, container_state)
    root.mkdir(exist_ok=False)
    _assert_directory_safe(root, container_state)
    return _capture_safe_root(root)


def _controlled_mkdir(directory: Path, root_state: _RootState) -> None:
    directory = directory.absolute()
    try:
        relative = directory.relative_to(root_state.path)
    except ValueError as exc:
        raise ResourceValidationError(
            f"target directory escapes resource root: {directory}"
        ) from exc

    current = root_state.path
    _assert_directory_safe(current, root_state)
    for component in relative.parts:
        current /= component
        if os.path.lexists(current):
            _assert_directory_safe(current, root_state)
            continue
        _assert_directory_safe(current.parent, root_state)
        current.mkdir(exist_ok=False)
        _assert_directory_safe(current, root_state)
    _assert_directory_safe(directory, root_state)


def _safe_unlink_part(part: Path, root_state: _RootState) -> None:
    _assert_destination_safe(part, root_state)
    if _is_reparse_point(part):
        raise ResourceValidationError(f"part file is a reparse point: {part}")
    part.unlink(missing_ok=True)
    _assert_destination_safe(part, root_state)


NETWORK_ERRNOS = frozenset(
    value
    for name in (
        "ECONNABORTED",
        "ECONNREFUSED",
        "ECONNRESET",
        "EHOSTUNREACH",
        "ENETDOWN",
        "ENETRESET",
        "ENETUNREACH",
        "EPIPE",
        "ETIMEDOUT",
    )
    if (value := getattr(errno, name, None)) is not None
)
NETWORK_WINERRORS = frozenset({10050, 10051, 10052, 10053, 10054, 10060, 10061, 10065})


def _is_retryable_network_error(exc: BaseException) -> bool:
    if isinstance(exc, HTTPError):
        return exc.code in {408, 425, 429} or 500 <= exc.code <= 599
    if isinstance(exc, URLError):
        reason = exc.reason
        if isinstance(reason, BaseException):
            return _is_retryable_network_error(reason)
        message = str(reason).lower()
        return any(
            marker in message
            for marker in (
                "timed out",
                "timeout",
                "temporary failure",
                "temporarily unavailable",
                "connection reset",
                "connection aborted",
                "connection interrupted",
                "connection was interrupted",
            )
        ) or message.strip() == "temporary"
    if isinstance(
        exc,
        (
            TimeoutError,
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
        ),
    ):
        return True
    return isinstance(exc, OSError) and (
        exc.errno in NETWORK_ERRNOS
        or getattr(exc, "winerror", None) in NETWORK_WINERRORS
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_download(url: str, target: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "comic-font-downloader/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)


def _validate_resource(resource: _Resource, path: Path) -> None:
    if resource.sha256 and _sha256(path).lower() != resource.sha256.lower():
        raise ResourceValidationError("SHA-256 mismatch")
    if resource.is_font:
        with path.open("rb") as stream:
            signature = stream.read(4)
        if signature not in FONT_SIGNATURES:
            raise ResourceValidationError(f"invalid font signature {signature!r}")


def _resources(items: list[FontCandidate]) -> list[_Resource]:
    resources: dict[Path, _Resource] = {}
    for item in items:
        candidates = (
            _Resource(
                item.source_url,
                item.path,
                item.resource_root / "fonts",
                item.sha256,
                True,
            ),
            _Resource(
                item.license_url,
                item.license_path,
                item.resource_root / "licenses",
                item.license_sha256,
                False,
            ),
        )
        for resource in candidates:
            existing = resources.get(resource.target)
            if existing and existing != resource:
                raise ValueError(f"conflicting resource metadata for {resource.target}")
            resources[resource.target] = resource
    return list(resources.values())


def _already_valid(resource: _Resource) -> bool:
    try:
        root_state = _capture_safe_root(resource.allowed_root)
        _assert_destination_safe(resource.target, root_state)
    except ResourceValidationError:
        return False
    if _is_reparse_point(resource.target) or not resource.target.is_file():
        return False
    try:
        _validate_resource(resource, resource.target)
    except (OSError, ResourceValidationError):
        return False
    return True


def _install_resource(
    resource: _Resource,
    download_func: DownloadFunc,
    sleep_func: Callable[[float], None],
) -> None:
    root_state = _prepare_allowed_root(resource.allowed_root)
    _controlled_mkdir(resource.target.parent, root_state)
    part = resource.target.with_name(resource.target.name + ".part")
    _safe_unlink_part(part, root_state)
    try:
        for attempt in range(3):
            try:
                _assert_destination_safe(part, root_state)
                download_func(resource.url, part)
                break
            except Exception as exc:
                if not _is_retryable_network_error(exc):
                    raise
                _safe_unlink_part(part, root_state)
                if attempt == 2:
                    raise
                sleep_func(0.25 * (attempt + 1))
        _assert_destination_safe(part, root_state)
        _validate_resource(resource, part)
        _assert_destination_safe(resource.target, root_state)
        os.replace(part, resource.target)
    finally:
        _safe_unlink_part(part, root_state)


def download_catalog(
    project_root: str | Path,
    *,
    dry_run: bool = False,
    download_func: DownloadFunc = _default_download,
    sleep_func: Callable[[float], None] = time.sleep,
    output: Callable[[str], None] = print,
) -> DownloadStats:
    root = Path(project_root).resolve()
    embedded = root / "assets" / "text_fonts" / "catalog.json"
    catalog_path = embedded if embedded.is_file() else root / "font_resources" / "catalog.json"
    items = load_catalog(catalog_path)
    stats = DownloadStats()

    for resource in _resources(items):
        if _already_valid(resource):
            stats.skipped += 1
            output(f"SKIP {resource.target}")
            continue
        if dry_run:
            stats.missing += 1
            output(f"MISSING {resource.target} <- {resource.url}")
            continue
        try:
            _install_resource(resource, download_func, sleep_func)
        except (OSError, http.client.HTTPException, ResourceValidationError) as exc:
            stats.failed += 1
            output(f"FAILED {resource.target}: {exc}")
        else:
            stats.installed += 1
            output(f"INSTALLED {resource.target}")

    output(
        f"SUMMARY installed={stats.installed} skipped={stats.skipped} "
        f"failed={stats.failed} missing={stats.missing}"
    )
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download licensed comic fonts.")
    parser.add_argument("--dry-run", action="store_true", help="list missing resources only")
    args = parser.parse_args(argv)
    project_root = Path(__file__).resolve().parents[1]
    stats = download_catalog(project_root, dry_run=args.dry_run)
    return 1 if stats.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
