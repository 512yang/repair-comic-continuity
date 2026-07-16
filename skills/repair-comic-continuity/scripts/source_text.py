"""Read novel source text without ever rewriting its original bytes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


_UTF8_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True)
class DecodedSourceText:
    text: str
    encoding: str
    raw_sha256: str
    decoded_sha256: str


def decode_source_text(path: Path) -> DecodedSourceText:
    """Strictly decode *path* and bind both raw and decoded representations."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"unable to read source text: {path}") from exc

    raw_sha256 = hashlib.sha256(raw).hexdigest()
    if raw.startswith(_UTF8_BOM):
        try:
            text = raw.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"source text has invalid strict UTF-8 BOM payload: {path}"
            ) from exc
        encoding = "utf-8"
    else:
        try:
            text = raw.decode("utf-8", errors="strict")
            encoding = "utf-8"
        except UnicodeDecodeError:
            try:
                text = raw.decode("gb18030", errors="strict")
                encoding = "gb18030"
            except UnicodeDecodeError as exc:
                raise ValueError(
                    f"source text is not strict UTF-8 or GB18030: {path}"
                ) from exc

    decoded_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return DecodedSourceText(
        text=text,
        encoding=encoding,
        raw_sha256=raw_sha256,
        decoded_sha256=decoded_sha256,
    )
