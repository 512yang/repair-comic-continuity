import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath


_CHAPTER_ORDER = {"一": 1, "二": 2, "三": 3}
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
_PAGE_PATTERN = re.compile(r"^(\d+)(?:[（(](\d+)[）)])?")
_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


def page_sort_key(path: Path) -> tuple[int, int, int, str]:
    chapter = next(
        (_CHAPTER_ORDER[part] for part in path.parts if part in _CHAPTER_ORDER),
        len(_CHAPTER_ORDER) + 1,
    )
    match = _PAGE_PATTERN.match(path.name)
    if match:
        page = int(match.group(1))
        variant = int(match.group(2) or 0)
    else:
        page = variant = 2**31 - 1
    return chapter, page, variant, str(path).casefold()


def _validated_member_parts(member_name: str) -> tuple[str, ...]:
    normalized = member_name.replace("\\", "/")
    member = PurePosixPath(normalized)
    if (
        normalized.startswith("/")
        or _WINDOWS_DRIVE_PATTERN.match(normalized)
        or member.is_absolute()
        or ".." in member.parts
    ):
        raise ValueError(f"unsafe ZIP member path: {member_name!r}")
    parts = tuple(part for part in member.parts if part not in ("", "."))
    for part in parts:
        windows_name = part.rstrip(" .").split(".", 1)[0].rstrip(" .").casefold()
        if ":" in part or windows_name in _WINDOWS_RESERVED_NAMES:
            raise ValueError(f"unsafe ZIP member path: {member_name!r}")
    return parts


def stage_zip(zip_path: Path, stage_dir: Path) -> list[Path]:
    stage_dir.mkdir(parents=True, exist_ok=True)
    stage_root = stage_dir.resolve()

    with zipfile.ZipFile(zip_path) as archive:
        validated = []
        seen_targets = {}
        for info in archive.infolist():
            parts = _validated_member_parts(info.filename)
            if not parts:
                continue
            target = stage_dir.joinpath(*parts)
            if not target.resolve().is_relative_to(stage_root):
                raise ValueError(f"unsafe ZIP member path: {info.filename!r}")
            target_key = tuple(part.rstrip(" .").casefold() for part in parts)
            if target_key in seen_targets:
                raise ValueError(
                    "ZIP member collision: "
                    f"{seen_targets[target_key][0]!r} and {info.filename!r}"
                )
            normalized_name = info.filename.replace("\\", "/")
            is_directory = info.is_dir() or normalized_name.endswith("/")
            seen_targets[target_key] = (info.filename, is_directory)
            validated.append((info, parts, target))

        for target_key, (member_name, _) in seen_targets.items():
            for depth in range(1, len(target_key)):
                parent = seen_targets.get(target_key[:depth])
                if parent is not None and not parent[1]:
                    raise ValueError(
                        f"file/directory conflict: {parent[0]!r} and {member_name!r}"
                    )

        for info, parts, target in validated:
            normalized_name = info.filename.replace("\\", "/")
            if info.is_dir() or normalized_name.endswith("/"):
                continue
            if Path(parts[-1]).suffix.lower() not in _IMAGE_EXTENSIONS:
                continue
            for parent in target.parents:
                if parent == stage_dir:
                    break
                if parent.exists() and not parent.is_dir():
                    raise ValueError(f"file/directory conflict: {parent}")
            if target.exists():
                raise ValueError(f"target already exists: {target}")

        images = []
        for info, parts, target in validated:
            normalized_name = info.filename.replace("\\", "/")
            if info.is_dir() or normalized_name.endswith("/"):
                continue
            if Path(parts[-1]).suffix.lower() not in _IMAGE_EXTENSIONS:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            images.append(target)

    return sorted(images, key=page_sort_key)


def write_rename_map(images: list[Path], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Rename Map",
        "",
        "| New filename | Source path |",
        "| --- | --- |",
    ]
    lines.extend(
        f"| {index}.jpg | {source} |"
        for index, source in enumerate(images, start=1)
    )
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
