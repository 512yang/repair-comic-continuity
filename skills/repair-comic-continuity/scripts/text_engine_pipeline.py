# -*- coding: utf-8 -*-
import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import zipfile
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from comic_repair.font_catalog import (
    FontCandidate,
    available_fonts,
    candidates_for_role,
    load_catalog,
)
from comic_repair.font_matcher import FontMatch, match_font
from comic_repair.input_order import page_sort_key, stage_zip
from comic_repair.qa import (
    QA_THRESHOLDS,
    WARNING_CODES,
    aggregate_warning_counts,
    check_page,
    make_contact_sheet,
    write_exception_report,
    write_font_match_report,
)
from comic_repair.style_analysis import TextStyle, analyze_text_style
from pipeline_contracts import canonical_hash
from text_style_contract import StyleEvidenceBlocked, style_lock_from_measurements


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
SFX_CHARS = set("呼吁咻嗖轰隆砰嘭啪哗哐咔嚓嗡唰刷噗呲铛嗒哒嘀咚哎啊呀呃嗯")

SKILL_ROOT = Path(__file__).resolve().parents[1]


def bundled_font_root():
    """Return the font resource directory bundled inside this Skill."""
    return SKILL_ROOT / "assets" / "text_fonts"


def discover_lama_root():
    """Locate the Skill-managed LaMa runtime cache without a second project."""
    candidates = (
        os.environ.get("REPAIR_COMIC_LAMA_ROOT"),
        SKILL_ROOT / "runtime" / "lama",
        Path(r"D:\AI\LaMa_IOPaint"),
    )
    for value in candidates:
        if not value:
            continue
        root = Path(value).expanduser()
        if (root / "venv311" / "Scripts" / "iopaint.exe").is_file():
            return root
    return SKILL_ROOT / "runtime" / "lama"


def final_relative_output_name(value):
    """Validate without renaming, normalizing Unicode, or changing extension case."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("relative output name must be a nonempty string")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError("relative output name must not be absolute")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("relative output name contains an unsafe component")
    if Path(parts[-1]).suffix.lower() not in IMAGE_EXTS:
        raise ValueError("relative output name must keep a supported image extension")
    return normalized


DEFAULT_CATALOG = bundled_font_root() / "catalog.json"
ROLE_MAP = {
    "dialogue": "dialogue",
    "narration": "narration",
    "black_caption": "black_caption",
    "vertical_text": "vertical_text",
}
VERTICAL_GLYPHS = {
    "，": "︐", "、": "︑", "。": "︒", "：": "︓", "；": "︔",
    "！": "︕", "？": "︖", "（": "︵", "）": "︶", "【": "︻",
    "】": "︼", "《": "︽", "》": "︾",
}


class LayoutOverflowError(RuntimeError):
    def __init__(self, message, layouts=None):
        super().__init__(message)
        self.layouts = layouts or []


@dataclass
class TextLine:
    text: str
    conf: float
    box: tuple[int, int, int, int]
    polygon: list[list[float]]


@dataclass
class TextBlock:
    lines: list[TextLine]
    box: tuple[int, int, int, int]
    kind: str
    rewrite_text: str
    original_text: str
    confidence: float
    orientation: str
    fill: tuple[int, int, int]
    source_match: dict | None = None
    warnings: list[str] = field(default_factory=list)
    style: TextStyle | None = None
    font_match: FontMatch | None = None
    block_id: str | None = None
    reviewed_style_lock: dict | None = None
    reviewed_lines: list[str] | None = None
    background_cleanup: str = "lama"


@dataclass(frozen=True)
class InputPage:
    order: int
    source_relative: str
    internal_name: str
    internal_path: Path

    @property
    def chapter(self):
        return next(
            (part for part in Path(self.source_relative).parts if part in {"一", "二", "三"}),
            "其他",
        )


@dataclass(frozen=True)
class PreparedInput:
    pages: list[InputPage]
    output_dir: Path
    staged_input_dir: Path
    source_kind: str
    source_path: Path


GENERATED_DIRECTORIES = (
    "staged_zip", "staged_input", "selected_input", "masks", "clean_lama", "final",
    "ocr_overlay", "font_matches", "comparison",
)
GENERATED_REPORTS = (
    "run_report.json", "运行报告.md", "exception_report.json",
    "exception_report.md", "sample_selection.json", "source_map.json",
    "analysis_manifest.json", "style_gate_report.json",
    "layout_preflight.json",
)
SAMPLE_ROLES = {
    "dialogue", "narration", "black_caption", "vertical_text", "sfx_keep", "sfx",
}
OUTPUT_MARKER = ".comic_text_repair_output.json"
OUTPUT_MARKER_PAYLOAD = {"tool": "comic_text_repair", "version": 2}


class _WorkflowArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, default_input, **kwargs):
        super().__init__(*args, **kwargs)
        self.default_input = str(default_input)

    def parse_args(self, args=None, namespace=None):
        parsed = super().parse_args(args, namespace)
        if parsed.zip_input is None and parsed.input is None:
            parsed.input = self.default_input
        return parsed


def _non_negative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("sample-count must be >= 0")
    return number


def build_parser(script_dir=None):
    root = Path(script_dir) if script_dir is not None else Path(__file__).resolve().parent
    parser = _WorkflowArgumentParser(
        description="Repair comic text from a ZIP archive or image folder.",
        default_input=root / "输入",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--zip-input")
    source.add_argument("--input")
    parser.add_argument("--output", default=str(root / "输出" / "字体匹配测试_v3"))
    parser.add_argument("--source", default=str(root / "小说.txt"))
    parser.add_argument(
        "--alignment",
        help="Confirmed novel_alignment.json used for page-scoped text repair",
    )
    parser.add_argument(
        "--reviewed-manifest",
        help="Confirmed reviewed text/style manifest required before rendering",
    )
    parser.add_argument("--lama-root", default=str(discover_lama_root()))
    parser.add_argument(
        "--image2-clean-dir",
        help="Reviewed GPT Image 2 textless cleanup candidates, using input names",
    )
    parser.add_argument("--font-catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--sample-count", type=_non_negative_int, default=0)
    parser.add_argument("--skip-lama", action="store_true")
    parser.add_argument("--keep-staging", action="store_true")
    parser.add_argument("--no-clean", action="store_true")
    return parser


def _is_d_drive(path):
    return Path(path).drive.casefold() == "d:"


def _validate_output_root(output_dir):
    output = Path(output_dir).absolute()
    if output == Path(output.anchor):
        raise ValueError(f"output cannot be a filesystem root: {output}")
    current = Path(output.anchor)
    for part in output.parts[1:]:
        current /= part
        if current.exists() and _is_reparse_point(current):
            raise RuntimeError(f"output path contains reparse/symlink: {current}")
    return output.resolve()


def validate_managed_output_path(output_dir, project_root):
    project = Path(project_root).absolute()
    output = Path(output_dir).absolute()
    output_root = project / "输出"
    if output in {Path(output.anchor), project, output_root}:
        raise ValueError(f"output cannot equal a protected root: {output}")
    try:
        output.relative_to(output_root)
    except ValueError as exc:
        raise ValueError(f"output must be below {output_root}: {output}") from exc
    current = Path(output.anchor)
    for part in output.parts[1:]:
        current /= part
        if current.exists() and _is_reparse_point(current):
            raise RuntimeError(f"output path contains reparse/symlink: {current}")
    resolved_project = project.resolve()
    resolved_root = output_root.resolve()
    resolved_output = output.resolve()
    if resolved_root != resolved_project / "输出":
        raise RuntimeError(f"project output root resolves unexpectedly: {resolved_root}")
    if not resolved_output.is_relative_to(resolved_root) or resolved_output == resolved_root:
        raise ValueError(f"resolved output must be below project output root: {resolved_output}")
    return output


def initialize_managed_output(output_dir, project_root):
    output = validate_managed_output_path(output_dir, project_root)
    output.mkdir(parents=True, exist_ok=True)
    marker = output / OUTPUT_MARKER
    entries = list(output.iterdir())
    if marker.exists():
        if _is_reparse_point(marker) or not marker.is_file():
            raise RuntimeError(f"output marker is unsafe: {marker}")
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"output marker is invalid: {marker}") from exc
        if payload != OUTPUT_MARKER_PAYLOAD:
            raise RuntimeError(f"output marker does not match this tool/version: {marker}")
    elif entries:
        raise RuntimeError(f"refusing non-empty output without marker: {output}")
    else:
        marker.write_text(
            json.dumps(OUTPUT_MARKER_PAYLOAD, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return output


def validate_generated_paths(output_dir):
    output = Path(output_dir).absolute()
    for name in (*GENERATED_DIRECTORIES, *GENERATED_REPORTS):
        target = output / name
        if target.exists() or target.is_symlink():
            _assert_no_reparse_tree(target)
            if not target.resolve().is_relative_to(output.resolve()):
                raise ValueError(f"generated path resolves outside output: {target}")


def _assert_no_reparse_tree(path):
    if _is_reparse_point(path):
        raise RuntimeError(f"refusing reparse/symlink generated path: {path}")
    if not path.is_dir():
        return
    for root, directories, files in os.walk(path, followlinks=False):
        for name in [*directories, *files]:
            candidate = Path(root) / name
            if _is_reparse_point(candidate):
                raise RuntimeError(f"refusing reparse/symlink generated path: {candidate}")


def safe_remove_generated_path(output_dir, candidate):
    output = _validate_output_root(output_dir)
    target = Path(candidate).absolute()
    try:
        target.relative_to(output)
    except ValueError as exc:
        raise ValueError(f"generated path is outside output: {target}") from exc
    if target == output:
        raise ValueError("refusing to remove output root")
    if not target.exists() and not target.is_symlink():
        return
    _assert_no_reparse_tree(target)
    resolved_output = output.resolve()
    if not target.resolve().is_relative_to(resolved_output):
        raise ValueError(f"resolved generated path is outside output: {target}")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


def fresh_cleanup(output_dir, project_root=None):
    project = Path(project_root) if project_root is not None else Path(__file__).resolve().parent
    output = initialize_managed_output(output_dir, project)
    targets = [output / name for name in (*GENERATED_DIRECTORIES, *GENERATED_REPORTS)]
    for target in targets:
        if target.exists() or target.is_symlink():
            _assert_no_reparse_tree(target)
    for target in targets:
        safe_remove_generated_path(output, target)


def _discover_folder_images(input_dir):
    root = Path(input_dir)
    if not root.is_dir():
        raise SystemExit(f"Input folder not found: {root}")
    images = [path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_EXTS]
    return sorted(images, key=lambda path: page_sort_key(path.relative_to(root)))


def _flatten_images(images, source_root, staged_input_dir):
    staged_input_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    for index, source in enumerate(images, start=1):
        extension = source.suffix.lower()
        internal_name = f"page_{index:04d}{extension}"
        destination = staged_input_dir / internal_name
        if destination.exists():
            raise RuntimeError(f"internal staging collision: {destination}")
        shutil.copy2(source, destination)
        relative = source.relative_to(source_root).as_posix()
        pages.append(InputPage(index, relative, internal_name, destination))
    return pages


def _source_map_payload(pages):
    return [
        {
            "order": page.order,
            "source_relative": page.source_relative,
            "internal_name": page.internal_name,
            "internal_path": str(page.internal_path.resolve()),
        }
        for page in pages
    ]


def prepare_input_workflow(args, project_root=None, output_dir=None):
    project = Path(project_root) if project_root is not None else Path(__file__).resolve().parent
    requested_output = output_dir if output_dir is not None else args.output
    output_dir = initialize_managed_output(requested_output, project)
    if not args.no_clean:
        fresh_cleanup(output_dir, project)
    else:
        validate_generated_paths(output_dir)
    staged_input = output_dir / "staged_input"

    if args.zip_input:
        archive = Path(args.zip_input)
        if not archive.is_file():
            raise SystemExit(f"ZIP input not found: {archive}")
        stage_root = output_dir / "staged_zip"
        try:
            images = stage_zip(archive, stage_root)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise SystemExit(f"ZIP staging failed: {exc}") from exc
        if not images:
            raise SystemExit(f"ZIP contains no supported images: {archive}")
        source_kind = "zip"
        source_path = archive
        source_root = stage_root
    else:
        source_path = Path(args.input)
        images = _discover_folder_images(source_path)
        if not images:
            raise SystemExit(f"Input folder contains no supported images: {source_path}")
        source_kind = "folder"
        source_root = source_path

    pages = _flatten_images(images, source_root, staged_input)
    (output_dir / "source_map.json").write_text(
        json.dumps(_source_map_payload(pages), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return PreparedInput(pages, output_dir, staged_input, source_kind, source_path)


def prepare_selected_input(output_dir, selected_pages):
    output = _validate_output_root(output_dir)
    destination = output / "selected_input"
    safe_remove_generated_path(output, destination)
    destination.mkdir(parents=True, exist_ok=True)
    for page in selected_pages:
        target = destination / page.internal_name
        if target.exists():
            raise RuntimeError(f"selected input collision: {target}")
        shutil.copy2(page.internal_path, target)
    return destination


def _image_complexity(image_np):
    gray = cv2.cvtColor(image_np, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 60, 150)
    edge_density = float(np.count_nonzero(edges)) / max(1, edges.size)
    laplacian = cv2.Laplacian(gray, cv2.CV_32F)
    texture = min(1.0, float(np.std(laplacian)) / 80.0)
    return round(min(1.0, max(edge_density * 3.0, texture)), 4)


def analyze_sample_metadata(ocr, pages):
    metadata = {}
    for page in pages:
        try:
            image = Image.open(page.internal_path).convert("RGB")
            image_np = np.array(image)
            lines = detect_lines(ocr, page.internal_path)
            groups = group_lines(lines, image.size)
            kinds = []
            for group in groups:
                box = union_box([line.box for line in group])
                fill = sample_fill(image_np, box)
                kinds.append(classify_block(group, box, image.size, fill))
            complexity = _image_complexity(image_np)
            metadata[page.internal_name] = {
                "kinds": sorted(set(kinds)),
                "background_risk": complexity,
                "complexity": complexity,
                "warnings": [],
                "lines": lines,
            }
        except Exception as exc:
            metadata[page.internal_name] = {
                "kinds": [],
                "background_risk": 0.0,
                "complexity": 0.0,
                "warnings": ["metadata_analysis_failed"],
                "warning_detail": f"{type(exc).__name__}: {exc}",
                "lines": [],
            }
    return metadata


def _sample_metadata(page, analysis):
    metadata = analysis.get(page.internal_name, {}) if analysis else {}
    kinds = sorted(SAMPLE_ROLES.intersection(metadata.get("kinds", [])))
    risk = float(metadata.get("background_risk", 0.0) or 0.0)
    complexity = float(metadata.get("complexity", risk) or 0.0)
    warnings = list(metadata.get("warnings", []) or [])
    return kinds, risk, complexity, warnings


def select_sample_pages(pages, sample_count, analysis=None):
    pages = list(pages)
    reasons = {}
    if sample_count == 0 or sample_count >= len(pages):
        selected = pages
        mode = "all"
        reasons = {page.internal_name: "full_batch" for page in pages}
    else:
        chapters = []
        for page in pages:
            if page.chapter not in chapters:
                chapters.append(page.chapter)
        base, remainder = divmod(sample_count, len(chapters))
        quotas = {chapter: base + (index < remainder) for index, chapter in enumerate(chapters)}
        selected = []
        covered = set()
        for chapter in chapters:
            candidates = [page for page in pages if page.chapter == chapter]
            for _ in range(min(quotas[chapter], len(candidates))):
                choice = max(
                    candidates,
                    key=lambda page: (
                        len(set(_sample_metadata(page, analysis)[0]) - covered),
                        max(_sample_metadata(page, analysis)[1:3]),
                        -page.order,
                    ),
                )
                kinds, risk, complexity, _warnings = _sample_metadata(choice, analysis)
                new_roles = sorted(set(kinds) - covered)
                reasons[choice.internal_name] = (
                    f"chapter_balance; new_roles={','.join(new_roles) or 'none'}; "
                    f"background_risk={risk:.4f}; complexity={complexity:.4f}"
                )
                selected.append(choice)
                covered.update(kinds)
                candidates.remove(choice)
        if len(selected) < sample_count:
            remaining = [page for page in pages if page not in selected]
            extras = remaining[:sample_count - len(selected)]
            selected.extend(extras)
            for page in extras:
                reasons[page.internal_name] = "deterministic_fill"
        selected.sort(key=lambda page: page.order)
        mode = "sample"

    selected_pages = []
    covered_roles = set()
    for page in selected:
        kinds, risk, complexity, warnings = _sample_metadata(page, analysis)
        covered_roles.update(kinds)
        selected_pages.append({
            "order": page.order,
            "chapter": page.chapter,
            "source_relative": page.source_relative,
            "internal_name": page.internal_name,
            "roles": kinds,
            "background_risk": risk,
            "complexity": complexity,
            "selection_reason": reasons[page.internal_name],
            "warnings": warnings,
        })
    return {
        "mode": mode,
        "requested_count": sample_count,
        "selected_internal_names": [page.internal_name for page in selected],
        "selected_pages": selected_pages,
        "covered_roles": sorted(covered_roles),
        "selection_basis": ["chapter_balance", "block_kind_coverage", "background_risk"],
    }


def _workflow_rename_lines(pages, outcomes):
    lines = [
        "# 输出命名映射", "",
        "| 最终名 | 压缩包/文件夹原相对路径 | 内部名 | 状态 |",
        "|---|---|---|---|",
    ]
    for page in pages:
        outcome = outcomes.get(page.internal_name, {"status": "failed", "reason": "not processed"})
        if outcome.get("status") == "ok":
            final_name = outcome["final_name"]
            status = "OK"
        else:
            final_name = "-"
            status = f"FAILED: {outcome.get('reason', 'unknown failure')}"
        lines.append(f"| {final_name} | {page.source_relative} | {page.internal_name} | {status} |")
    return lines


def write_workflow_rename_map(destination, pages, outcomes):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(_workflow_rename_lines(pages, outcomes)) + "\n", encoding="utf-8")


def attach_source_identity(records, source_lookup):
    for record in records:
        internal_name = record.get("internal_name") or record.get("page") or record.get("file")
        page = source_lookup.get(internal_name)
        if page is None:
            continue
        record["internal_name"] = page.internal_name
        record["source_relative"] = page.source_relative
    return records


def augment_font_match_report(report_path, page, final_name=None, status="ok"):
    report = Path(report_path)
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload.update({
        "source_relative": page.source_relative,
        "internal_name": page.internal_name,
        "status": status,
    })
    if final_name is not None:
        payload["final_name"] = final_name
    temporary = report.with_name(f"{report.name}.__augment_tmp__")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(report)
    finally:
        temporary.unlink(missing_ok=True)
    return report


def _set_font_weight(font, weight):
    axes_getter = getattr(font, "get_variation_axes", None)
    setter = getattr(font, "set_variation_by_axes", None)
    if axes_getter is None or setter is None:
        return False
    try:
        values = []
        found_weight = False
        for axis in axes_getter() or []:
            name = axis.get("name", b"")
            if isinstance(name, bytes):
                name = name.decode("ascii", "ignore")
            is_weight = "weight" in str(name).lower()
            requested = weight if is_weight else axis.get("default", axis["minimum"])
            values.append(max(axis["minimum"], min(axis["maximum"], requested)))
            found_weight = found_weight or is_weight
        if not values or not found_weight:
            return False
        setter(values)
        return True
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def safe_font_match(role, candidates=None, warning=None):
    source = candidates if candidates is not None else available_fonts(load_catalog(DEFAULT_CATALOG))
    pool = candidates_for_role(list(source), role)
    preferred_family = "Noto Serif SC" if role == "narration" else "Noto Sans SC"
    preferred_weight = 600 if role in {"narration", "black_caption"} else 400
    preferred = [item for item in pool if item.family == preferred_family]
    choices = preferred or [item for item in pool if item.family.startswith("Noto ")] or pool
    if not choices:
        raise ValueError(f"no available safe font for role {role}")
    selected = min(choices, key=lambda item: abs(item.weight - preferred_weight))
    warnings = [warning] if warning else []
    return FontMatch(
        family=selected.family,
        path=selected.path,
        weight=selected.weight,
        score=0.0,
        confidence=0.0,
        fallback_used=True,
        top_candidates=[{
            "family": selected.family,
            "path": selected.path,
            "weight": selected.weight,
            "score": 0.0,
        }],
        warnings=warnings,
    )


@lru_cache(maxsize=256)
def _cached_font(path, size, weight):
    font = ImageFont.truetype(str(path), max(8, int(size)))
    warnings = []
    try:
        axes = font.get_variation_axes()
    except (AttributeError, OSError):
        axes = []
    if axes and not _set_font_weight(font, weight):
        warnings.append("variable_weight_apply_failed")
    return font, tuple(warnings)


def clear_font_cache():
    _cached_font.cache_clear()


def font_cache_info():
    return _cached_font.cache_info()


def _get_font_and_match(size, font_match=None, role="dialogue", candidates=None):
    match = font_match or safe_font_match(role, candidates)
    warnings = []
    try:
        font, cached_warnings = _cached_font(str(match.path), max(8, int(size)), match.weight)
    except (OSError, ValueError):
        warnings.append("font_load_failed")
        match = safe_font_match(role, candidates, warning="font_load_failed")
        font, cached_warnings = _cached_font(str(match.path), max(8, int(size)), match.weight)
    warnings.extend(cached_warnings)
    return font, warnings, match


def get_font(size, font_match=None, role="dialogue", candidates=None):
    font, warnings, _actual_match = _get_font_and_match(
        size, font_match, role, candidates
    )
    return font, warnings


def validate_font_candidates(candidates):
    validated = list(candidates)
    if not validated:
        raise ValueError("no available fonts")
    for item in validated:
        try:
            font = ImageFont.truetype(str(item.path), 16)
        except (OSError, ValueError) as exc:
            raise ValueError(f"font file invalid: {item.path}") from exc
        if item.variable_weight and not _set_font_weight(font, item.weight):
            raise ValueError(f"variable weight unavailable: {item.path} weight={item.weight}")
    return validated


def read_text_file(path):
    if not path or not Path(path).exists():
        return ""
    data = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def normalize_for_match(text):
    text = text.replace("\n", "")
    text = re.sub(r"\s+", "", text)
    text = text.replace("“", "").replace("”", "").replace("‘", "").replace("’", "")
    text = re.sub(r"[，。！？、,.!?;；:：\"'《》（）()【】\[\]…—\-·]", "", text)
    return text


def normalize_display_text(text):
    text = text.strip()
    text = text.replace("'", "，")
    text = text.replace("?", "？")
    text = re.sub(r"\s+", "", text)
    return text


def split_source_units(text):
    text = re.sub(r"\s+", "", text)
    text = text.replace("……", "…")
    if not text:
        return []
    pieces = re.split(r"(?<=[。！？；;])", text)
    units = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if len(piece) > 48:
            chunks = re.split(r"(?<=[，,、])", piece)
            units.extend([c for c in chunks if c])
        else:
            units.append(piece)
    return units


def load_ocr():
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception as exc:
        raise SystemExit(
            "RapidOCR is not installed. Run: D:/AI/LaMa_IOPaint/venv311/Scripts/python.exe -m pip install rapidocr_onnxruntime"
        ) from exc
    return RapidOCR()


def detect_lines(ocr, image_path):
    result, _ = ocr(str(image_path))
    lines = []
    for item in result or []:
        polygon, text, conf = item
        if not text or conf < 0.35:
            continue
        text = normalize_display_text(str(text))
        if not normalize_for_match(text):
            continue
        xs = [p[0] for p in polygon]
        ys = [p[1] for p in polygon]
        box = (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
        lines.append(TextLine(text=text, conf=float(conf), box=box, polygon=polygon))
    return lines


def expanded(box, px, py, size):
    x1, y1, x2, y2 = box
    w, h = size
    return (max(0, x1 - px), max(0, y1 - py), min(w, x2 + px), min(h, y2 + py))


def boxes_touch(a, b, mx, my):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    return not (ax2 + mx < bx1 or bx2 + mx < ax1 or ay2 + my < by1 or by2 + my < ay1)


def union_box(boxes):
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def group_lines(lines, image_size):
    groups = [[line] for line in lines]

    def overlap_ratio(a1, a2, b1, b2):
        overlap = max(0, min(a2, b2) - max(a1, b1))
        return overlap / max(1, min(a2 - a1, b2 - b1))

    def should_merge(a, b):
        ax1, ay1, ax2, ay2 = union_box([line.box for line in a])
        bx1, by1, bx2, by2 = union_box([line.box for line in b])
        aw, ah = ax2 - ax1, ay2 - ay1
        bw, bh = bx2 - bx1, by2 - by1
        a_vertical = ah > aw * 1.35
        b_vertical = bh > bw * 1.35
        x_overlap = overlap_ratio(ax1, ax2, bx1, bx2)
        y_overlap = overlap_ratio(ay1, ay2, by1, by2)
        x_gap = max(0, max(ax1, bx1) - min(ax2, bx2))
        y_gap = max(0, max(ay1, by1) - min(ay2, by2))
        if a_vertical or b_vertical:
            return x_gap <= 42 and y_overlap >= 0.22
        same_paragraph_stack = y_gap <= 22 and x_overlap >= 0.22
        same_line_split = x_gap <= 28 and y_overlap >= 0.45
        return same_paragraph_stack or same_line_split

    changed = True
    while changed:
        changed = False
        merged = []
        used = [False] * len(groups)
        for i, group in enumerate(groups):
            if used[i]:
                continue
            current = list(group)
            used[i] = True
            cbox = union_box([line.box for line in current])
            for j in range(i + 1, len(groups)):
                if used[j]:
                    continue
                if should_merge(current, groups[j]):
                    current.extend(groups[j])
                    used[j] = True
                    cbox = union_box([line.box for line in current])
                    changed = True
            merged.append(current)
        groups = merged
    return groups


def sort_lines_for_block(lines, orientation):
    if orientation == "vertical":
        return sorted(lines, key=lambda line: (-line.box[0], line.box[1]))
    return sorted(lines, key=lambda line: (line.box[1], line.box[0]))


def infer_orientation(lines, box):
    x1, y1, x2, y2 = box
    w = max(1, x2 - x1)
    h = max(1, y2 - y1)
    vertical_lines = 0
    for line in lines:
        lx1, ly1, lx2, ly2 = line.box
        if (ly2 - ly1) > (lx2 - lx1) * 1.6:
            vertical_lines += 1
    if vertical_lines >= max(1, len(lines) // 2) or h > w * 1.45:
        return "vertical"
    return "horizontal"


def sample_fill(image_np, box):
    x1, y1, x2, y2 = box
    crop = image_np[max(0, y1):max(0, y2), max(0, x1):max(0, x2)]
    if crop.size == 0:
        return (0, 0, 0)
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    median = float(np.median(gray))
    return (255, 255, 255) if median < 95 else (0, 0, 0)


def classify_block(lines, box, image_size, fill):
    ordered = sort_lines_for_block(lines, infer_orientation(lines, box))
    text = "".join(line.text for line in ordered)
    clean = normalize_for_match(text)
    x1, y1, x2, y2 = box
    w, h = image_size
    bw = x2 - x1
    bh = y2 - y1
    area_ratio = (bw * bh) / max(1, w * h)
    has_punc = any(ch in text for ch in "，。！？,.!?；：")
    likely_sfx_word = bool(clean) and len(clean) <= 4 and any(ch in SFX_CHARS for ch in clean)
    isolated_short = len(clean) <= 4 and len(lines) <= 2 and area_ratio < 0.04 and not has_punc
    if likely_sfx_word or isolated_short:
        return "sfx_keep"
    if fill == (255, 255, 255):
        return "black_caption"
    if bh > bw * 1.2:
        return "vertical_text"
    if y1 < h * 0.18 or y2 > h * 0.82:
        return "narration"
    return "dialogue"


def make_source_matcher(source_units):
    state = {"cursor": 0}

    def match(ocr_text):
        clean_ocr = normalize_for_match(ocr_text)
        if not clean_ocr or not source_units:
            return None
        start = max(0, state["cursor"] - 8)
        end = min(len(source_units), state["cursor"] + 45)
        best = None
        for i in range(start, end):
            for span in range(1, 4):
                j = min(len(source_units), i + span)
                candidate = "".join(source_units[i:j])
                clean_candidate = normalize_for_match(candidate)
                if not clean_candidate:
                    continue
                ratio_len = len(clean_candidate) / max(1, len(clean_ocr))
                if ratio_len < 0.55 or ratio_len > 1.85:
                    continue
                score = SequenceMatcher(None, clean_ocr, clean_candidate).ratio()
                if best is None or score > best["score"]:
                    best = {
                        "text": candidate,
                        "score": score,
                        "start": i,
                        "end": j,
                    }
        if best and best["score"] >= 0.82:
            state["cursor"] = best["end"]
            return best
        return best if best and best["score"] >= 0.72 else None

    return match


def _normalized_source_offsets(text):
    normalized = []
    offsets = []
    for index, char in enumerate(text):
        if normalize_for_match(char):
            normalized.append(char)
            offsets.append(index)
    return "".join(normalized), offsets


def make_page_source_matcher(source_text):
    """Match ordered OCR blocks inside one confirmed page-level novel excerpt."""
    normalized_source, offsets = _normalized_source_offsets(source_text)
    state = {"cursor": 0}

    def match(ocr_text):
        clean_ocr = normalize_for_match(ocr_text)
        if not clean_ocr or not normalized_source:
            return None
        target_length = len(clean_ocr)
        minimum = max(1, int(target_length * 0.65))
        maximum = min(len(normalized_source), int(target_length * 1.35) + 6)
        search_start = max(0, state["cursor"] - 24)
        best = None
        for start in range(search_start, len(normalized_source) - minimum + 1):
            for length in range(minimum, maximum + 1):
                end = start + length
                if end > len(normalized_source):
                    break
                candidate = normalized_source[start:end]
                score = SequenceMatcher(None, clean_ocr, candidate).ratio()
                rank = (score, -abs(length - target_length), -start)
                if best is None or rank > best["rank"]:
                    original_start = offsets[start]
                    original_end = offsets[end - 1] + 1
                    best = {
                        "text": source_text[original_start:original_end],
                        "score": score,
                        "start": start,
                        "end": end,
                        "rank": rank,
                    }
        if best is None or best["score"] < 0.72:
            return None
        state["cursor"] = best["end"]
        return {key: value for key, value in best.items() if key != "rank"}

    return match


def stable_text_block_id(source_relative, block):
    body = {
        "source_relative": final_relative_output_name(source_relative),
        "box": list(block.box),
        "line_boxes": [list(line.box) for line in block.lines],
        "original_text": block.original_text,
    }
    digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"text-{digest[:20]}"


def apply_reviewed_page(*, blocks, source_relative, review_rows, font_candidates):
    """Apply a complete reviewed block manifest; missing or stale rows block rendering."""
    from prompt_compiler import _normalize_style_lock

    if not isinstance(review_rows, list):
        raise ValueError("reviewed page blocks must be a list")
    block_lookup = {block.block_id: block for block in blocks}
    row_lookup = {
        row.get("block_id"): row
        for row in review_rows
        if isinstance(row, dict) and isinstance(row.get("block_id"), str)
    }
    if set(row_lookup) != set(block_lookup):
        raise ValueError(f"reviewed block set does not match live analysis for {source_relative}")
    candidate_rows = []
    for candidate in font_candidates:
        candidate_rows.append((
            candidate,
            hashlib.sha256(Path(candidate.path).read_bytes()).hexdigest(),
        ))
    for block_id, block in block_lookup.items():
        row = row_lookup[block_id]
        action = row.get("action")
        if action == "preserve":
            block.kind = "sfx_keep"
            block.rewrite_text = block.original_text
            block.reviewed_style_lock = None
            block.background_cleanup = "preserve"
            continue
        if action != "replace":
            raise ValueError(f"reviewed block {block_id} has unsupported action")
        replacement = row.get("replacement_text")
        if not isinstance(replacement, str) or not replacement.strip():
            raise ValueError(f"reviewed block {block_id} replacement_text is missing")
        orientation = "vertical" if block.orientation == "vertical" else "horizontal"
        canvas = {
            "width": max(block.box[2], *(line.box[2] for line in block.lines)),
            "height": max(block.box[3], *(line.box[3] for line in block.lines)),
        }
        lock = _normalize_style_lock(
            row.get("style_lock"), name=f"reviewed block {block_id}.style_lock",
            canvas=canvas, block_bbox=list(block.box), orientation=orientation,
        )
        requested_weight = row.get("font_weight")
        if not isinstance(requested_weight, int) or isinstance(requested_weight, bool):
            raise ValueError(f"reviewed block {block_id} font_weight is missing")
        matches = [
            candidate for candidate, digest in candidate_rows
            if digest == lock["font"]["asset_sha256"]
            and candidate.family == lock["font"]["family"]
            and candidate.weight == requested_weight
        ]
        if not matches:
            raise ValueError(f"reviewed block {block_id} font asset or weight is unavailable")
        font = matches[0]
        block.rewrite_text = replacement.strip()
        replacement_lines = row.get("replacement_lines")
        if replacement_lines is not None:
            if (
                not isinstance(replacement_lines, list)
                or not replacement_lines
                or any(not isinstance(line, str) or not line for line in replacement_lines)
            ):
                raise ValueError(f"reviewed block {block_id} replacement_lines are invalid")
            if len(replacement_lines) != len(lock["line_boxes"]):
                raise ValueError(f"reviewed block {block_id} line count changed")
            if normalize_for_match("".join(replacement_lines)) != normalize_for_match(replacement):
                raise ValueError(f"reviewed block {block_id} replacement_lines mismatch text")
            block.reviewed_lines = list(replacement_lines)
        else:
            block.reviewed_lines = None
        block.orientation = "vertical" if lock["writing_mode"].startswith("vertical") else "horizontal"
        block.fill = tuple(lock["fill_rgba"][:3])
        block.style = TextStyle(
            fill=tuple(lock["fill_rgba"][:3]),
            orientation=block.orientation,
            font_size=lock["font_size_px"],
            weight_score=requested_weight / 1000.0,
            width_ratio=lock["horizontal_scale"],
            char_gap=lock["letter_spacing_px"],
            line_gap=lock["line_spacing_px"],
            stroke_width=lock["stroke_width_px"],
            category_scores={}, confidence=1.0, warnings=[],
        )
        block.font_match = FontMatch(
            family=font.family, path=Path(font.path), weight=font.weight,
            score=1.0, confidence=1.0, fallback_used=False,
            top_candidates=[], warnings=[],
        )
        block.reviewed_style_lock = lock
        background_cleanup = row.get("background_cleanup", "lama")
        if background_cleanup not in {"lama", "flat_inpaint", "gpt_image_2"}:
            raise ValueError(
                f"reviewed block {block_id} has unsupported background_cleanup"
            )
        block.background_cleanup = background_cleanup
        block.warnings = [
            warning for warning in block.warnings
            if warning not in {
                "style_low_confidence", "font_match_low_confidence",
                "font_match_line_fallback", "source_match_low_confidence",
                "source_match_too_long_kept_ocr",
            }
        ]


def load_confirmed_page_matchers(alignment_path, pages):
    """Bind every staged page to its independently confirmed novel context."""
    payload = json.loads(Path(alignment_path).read_text(encoding="utf-8-sig"))
    if payload.get("status") != "confirmed":
        raise ValueError("page-scoped alignment must have confirmed status")
    rows = payload.get("pages")
    if not isinstance(rows, list):
        raise ValueError("confirmed alignment pages are missing")
    lookup = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("status") != "confirmed":
            raise ValueError("every page-scoped alignment row must be confirmed")
        name = row.get("output_name")
        source_text = row.get("context_excerpt") or row.get("source_excerpt")
        if not isinstance(name, str) or not isinstance(source_text, str) or not source_text.strip():
            raise ValueError("confirmed alignment row is missing output_name or source text")
        lookup[name] = source_text
    matchers = {}
    for page in pages:
        if page.source_relative not in lookup:
            raise ValueError(f"confirmed alignment is missing {page.source_relative}")
        matchers[page.internal_name] = make_page_source_matcher(lookup[page.source_relative])
    return matchers


def load_confirmed_review_pages(review_path, pages):
    """Load hash-bound reviewed text/style decisions for every selected source page."""
    payload = json.loads(Path(review_path).read_text(encoding="utf-8-sig"))
    if payload.get("status") != "confirmed":
        raise ValueError("reviewed manifest must have confirmed status")
    rows = payload.get("pages")
    if not isinstance(rows, list):
        raise ValueError("reviewed manifest pages are missing")
    lookup = {
        row.get("source_relative"): row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("source_relative"), str)
    }
    result = {}
    for page in pages:
        row = lookup.get(page.source_relative)
        if row is None:
            raise ValueError(f"reviewed manifest is missing {page.source_relative}")
        expected_hash = row.get("source_sha256")
        live_hash = hashlib.sha256(page.internal_path.read_bytes()).hexdigest()
        if expected_hash != live_hash:
            raise ValueError(f"reviewed manifest source hash drift for {page.source_relative}")
        if not isinstance(row.get("blocks"), list):
            raise ValueError(f"reviewed manifest blocks are missing for {page.source_relative}")
        result[page.internal_name] = row["blocks"]
    if set(lookup) != {page.source_relative for page in pages}:
        raise ValueError("reviewed manifest page set does not match selected input")
    return result


def analyze_image(
    ocr, image_path, source_matcher, font_candidates=None, precomputed_lines=None,
):
    image = Image.open(image_path).convert("RGB")
    image_np = np.array(image)
    font_candidates = list(font_candidates or available_fonts(load_catalog(DEFAULT_CATALOG)))
    lines = list(precomputed_lines) if precomputed_lines is not None else detect_lines(ocr, image_path)
    groups = group_lines(lines, image.size)
    blocks = []
    for group in groups:
        box = union_box([line.box for line in group])
        orientation = infer_orientation(group, box)
        ordered = sort_lines_for_block(group, orientation)
        original_text = "\n".join(line.text for line in ordered) if orientation == "horizontal" else "".join(line.text for line in ordered)
        fill = sample_fill(image_np, box)
        kind = classify_block(group, box, image.size, fill)
        style_failed = False
        try:
            style = analyze_text_style(
                image_np, box, orientation, [line.box for line in ordered]
            )
        except Exception:
            style_failed = True
            fallback_block = TextBlock(
                lines=group, box=box, kind=kind, rewrite_text=original_text,
                original_text=original_text, confidence=min(line.conf for line in group),
                orientation=orientation, fill=fill,
            )
            style = _default_style(fallback_block)
        fill = style.fill
        source_match = None
        rewrite_text = normalize_display_text(original_text)
        warnings = list(style.warnings)
        if style_failed:
            warnings.append("style_analysis_failed")
        font_result = None
        if kind != "sfx_keep":
            try:
                source_match = source_matcher(original_text) if source_matcher else None
            except Exception:
                source_match = None
                warnings.append("source_match_failed_kept_ocr")
            if source_match and source_match["score"] >= 0.82:
                ocr_len = len(normalize_for_match(original_text))
                match_len = len(normalize_for_match(source_match["text"]))
                length_ok = match_len <= max(ocr_len + 6, int(ocr_len * 1.28))
                if not length_ok:
                    warnings.append("source_match_too_long_kept_ocr")
                else:
                    rewrite_text = source_match["text"]
            elif source_match and source_match["score"] >= 0.72:
                warnings.append("source_match_low_confidence")
            if min(line.conf for line in group) < 0.72:
                warnings.append("ocr_low_confidence")
            role = ROLE_MAP.get(kind, "dialogue")
            role_candidates = candidates_for_role(font_candidates, role)
            x1, y1, x2, y2 = box
            crop = image_np[max(0, y1):min(image_np.shape[0], y2), max(0, x1):min(image_np.shape[1], x2)]
            try:
                font_result = match_font(
                    crop, original_text, style, role_candidates, role=role
                )
            except Exception:
                warnings.append("font_match_failed")
                font_result = safe_font_match(
                    role, font_candidates, warning="font_match_failed"
                )
            warnings.extend(font_result.warnings)
        blocks.append(
            TextBlock(
                lines=group,
                box=box,
                kind=kind,
                rewrite_text=rewrite_text,
                original_text=original_text,
                confidence=min(line.conf for line in group),
                orientation=orientation,
                fill=fill,
                source_match=source_match,
                warnings=list(dict.fromkeys(warnings)),
                style=style,
                font_match=font_result,
            )
        )
    return image.size, lines, sorted(blocks, key=lambda b: (b.box[1], b.box[0]))


def make_mask(image_size, blocks, dest=None, pad=None):
    width, height = image_size
    mask_np = np.zeros((height, width), dtype=np.uint8)
    for block in blocks:
        if block.kind == "sfx_keep":
            continue
        if pad is None:
            style = block.style
            font_size = style.font_size if style else 16
            safe_stroke = min(2, max(0, style.stroke_width if style else 0))
            block_pad = min(18, max(4, int(round(font_size * 0.16)) + safe_stroke))
        else:
            block_pad = min(18, max(0, int(pad)))
        line_masks = []
        for line in block.lines:
            local = Image.new("L", image_size, 0)
            draw = ImageDraw.Draw(local)
            polygon = [tuple(point[:2]) for point in line.polygon if len(point) >= 2]
            if len(polygon) >= 3:
                draw.polygon(polygon, fill=255)
            else:
                x1, y1, x2, y2 = expanded(line.box, 0, 0, image_size)
                if x2 > x1 and y2 > y1:
                    draw.rectangle((x1, y1, x2 - 1, y2 - 1), fill=255)
            line_np = np.asarray(local, dtype=np.uint8)
            if block_pad:
                kernel_size = block_pad * 2 + 1
                kernel = cv2.getStructuringElement(
                    cv2.MORPH_ELLIPSE, (kernel_size, kernel_size)
                )
                line_np = cv2.dilate(line_np, kernel)
            line_masks.append(line_np)
        if not line_masks:
            x1, y1, x2, y2 = expanded(block.box, block_pad, block_pad, image_size)
            if x2 > x1 and y2 > y1:
                mask_np[y1:y2, x1:x2] = 255
        else:
            for line_np in line_masks:
                mask_np = np.maximum(mask_np, line_np)
    mask = Image.fromarray(mask_np, mode="L")
    if dest is not None:
        mask.save(dest)
    return mask


def detect_lama_device():
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def run_lama(lama_root, input_dir, mask_dir, clean_dir, device):
    clean_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HF_HOME"] = str(lama_root / "models" / "hf")
    env["HUGGINGFACE_HUB_CACHE"] = str(lama_root / "models" / "hf" / "hub")
    env["TORCH_HOME"] = str(lama_root / "models" / "torch")
    cmd = [
        str(lama_root / "venv311" / "Scripts" / "iopaint.exe"),
        "run",
        "--model",
        "lama",
        "--device",
        device,
        "--model-dir",
        str(lama_root / "models"),
        "--image",
        str(input_dir),
        "--mask",
        str(mask_dir),
        "--output",
        str(clean_dir),
    ]
    subprocess.run(cmd, check=True, env=env)


def find_clean(clean_dir, src):
    for candidate in [
        clean_dir / f"{src.stem}.png",
        clean_dir / f"{src.stem}.jpg",
        clean_dir / f"{src.stem}.jpeg",
        clean_dir / src.name,
    ]:
        if candidate.exists():
            return candidate
    return None


def measure_text(draw, text, font):
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def _default_style(block):
    fill = (255, 255, 255) if sum(block.fill) >= 384 else (0, 0, 0)
    return TextStyle(
        fill=fill,
        orientation=block.orientation,
        font_size=max(8, min(32, block.box[3] - block.box[1])),
        weight_score=0.4,
        width_ratio=1.0,
        char_gap=0,
        line_gap=4,
        stroke_width=0,
        category_scores={name: 1 / 6 for name in (
            "sans", "serif", "rounded", "handwriting", "display", "condensed"
        )},
        confidence=0.0,
        warnings=["style_missing_fallback"],
    )


def _split_by_ratios(text, lengths, count):
    text = "".join(text.split())
    if count <= 1:
        return [text]
    lengths = list(lengths[:count])
    if len(lengths) < count:
        lengths.extend([1] * (count - len(lengths)))
    total = max(1, sum(max(1, value) for value in lengths))
    result = []
    cursor = 0
    for index, value in enumerate(lengths):
        if index == count - 1:
            cut = len(text)
        else:
            ideal = round(len(text) * sum(max(1, item) for item in lengths[:index + 1]) / total)
            remaining = count - index - 1
            cut = min(len(text) - remaining, max(cursor + 1, ideal))
        result.append(text[cursor:cut])
        cursor = cut
    return result


def _glyph_bbox(font, glyph, stroke):
    box = font.getbbox(glyph, stroke_width=stroke, anchor="lt")
    return tuple(int(value) for value in box)


def _union_or_zero(boxes):
    if not boxes:
        return (0, 0, 0, 0)
    return (
        min(box[0] for box in boxes), min(box[1] for box in boxes),
        max(box[2] for box in boxes), max(box[3] for box in boxes),
    )


def _clamp_box(box, outer):
    x1, y1, x2, y2 = box
    ox1, oy1, ox2, oy2 = outer
    x1, x2 = max(ox1, min(ox2, x1)), max(ox1, min(ox2, x2))
    y1, y2 = max(oy1, min(oy2, y1)), max(oy1, min(oy2, y2))
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def _inside(box, outer):
    return (
        outer[0] <= box[0] <= box[2] <= outer[2]
        and outer[1] <= box[1] <= box[3] <= outer[3]
    )


def _shape_horizontal_line(font, text, char_gap, stroke, horizontal_scale=1.0):
    cursor = 0.0
    glyphs = []
    for original in text:
        rendered = original
        bbox = _glyph_bbox(font, rendered, stroke)
        raw_box = (
            int(np.floor(cursor + bbox[0] * horizontal_scale)), bbox[1],
            int(np.ceil(cursor + bbox[2] * horizontal_scale)), bbox[3],
        )
        glyphs.append({
            "original": original, "rendered": rendered,
            "raw_box": raw_box, "raw_draw": (cursor, 0.0),
        })
        cursor += float(font.getlength(rendered)) * horizontal_scale + char_gap
    bounds = _union_or_zero([item["raw_box"] for item in glyphs])
    return glyphs, bounds


def _shape_vertical_column(font, text, char_gap, stroke, horizontal_scale=1.0):
    cursor = 0
    glyphs = []
    max_width = 0
    for original in text:
        rendered = VERTICAL_GLYPHS.get(original, original)
        bbox = _glyph_bbox(font, rendered, stroke)
        width = max(1, int(round((bbox[2] - bbox[0]) * horizontal_scale)))
        height = max(1, bbox[3] - bbox[1])
        glyphs.append({
            "original": original, "rendered": rendered, "font_box": bbox,
            "top": cursor, "width": width, "height": height,
        })
        max_width = max(max_width, width)
        cursor += height + char_gap
    height = max(0, cursor - char_gap) if glyphs else 0
    return glyphs, max_width, height


def _horizontal_candidate(block, style, size, font, stroke):
    x1, y1, x2, y2 = block.box
    width, height = max(0, x2 - x1), max(0, y2 - y1)
    text = "".join(block.rewrite_text.split())
    original_lines = max(1, len(block.lines))
    lengths = [len(normalize_for_match(line.text)) for line in sort_lines_for_block(block.lines, "horizontal")]
    char_gap = max(-size // 4, int(style.char_gap))
    line_gap = max(0, int(style.line_gap))
    line_count = original_lines
    reviewed_lock = getattr(block, "reviewed_style_lock", None) or {}
    reviewed_lines = getattr(block, "reviewed_lines", None)
    lines = (
        list(reviewed_lines)
        if reviewed_lines is not None
        else _split_by_ratios(text, lengths, line_count)
    )
    horizontal_scale = float(
        (getattr(block, "reviewed_style_lock", None) or {}).get(
            "horizontal_scale", getattr(style, "width_ratio", 1.0)
        )
    )
    shaped = [
        _shape_horizontal_line(font, line, char_gap, stroke, horizontal_scale)
        for line in lines
    ]
    widths = [bounds[2] - bounds[0] for _glyphs, bounds in shaped]
    heights = [bounds[3] - bounds[1] for _glyphs, bounds in shaped]
    total_height = sum(heights) + line_gap * max(0, len(lines) - 1)
    target_line_boxes = reviewed_lock.get("line_boxes")
    if target_line_boxes and len(target_line_boxes) == len(lines):
        fits = bool(width > 0 and height > 0) and all(
            line_width <= target[2] - target[0]
            and line_height <= target[3] - target[1]
            for line_width, line_height, target in zip(widths, heights, target_line_boxes)
        )
    else:
        fits = bool(width > 0 and height > 0 and max(widths or [0]) <= width and total_height <= height)
    top = y1 + max(0, (height - min(height, total_height)) // 2)
    glyphs, boxes = [], []
    y = top
    for index, ((raw_glyphs, bounds), line_width, line_height) in enumerate(
        zip(shaped, widths, heights)
    ):
        if target_line_boxes and len(target_line_boxes) == len(lines):
            target = target_line_boxes[index]
            alignment = reviewed_lock.get("alignment", "left")
            if alignment == "right":
                left = target[2] - line_width
            elif alignment == "center":
                left = target[0] + max(0, (target[2] - target[0] - line_width) // 2)
            else:
                left = target[0]
            y = target[1]
        else:
            left = x1 + max(0, (width - min(width, line_width)) // 2)
        shift_x = left - bounds[0]
        shift_y = y - bounds[1]
        absolute_boxes = []
        for item in raw_glyphs:
            raw = item["raw_box"]
            absolute = tuple(int(value) for value in (
                raw[0] + shift_x, raw[1] + shift_y,
                raw[2] + shift_x, raw[3] + shift_y,
            ))
            glyphs.append({
                "original": item["original"], "rendered": item["rendered"],
                "box": absolute,
                "draw": (float(item["raw_draw"][0] + shift_x), float(shift_y)),
            })
            absolute_boxes.append(absolute)
        line_box = _union_or_zero(absolute_boxes)
        if not absolute_boxes:
            line_box = (left, y, left, y)
        boxes.append(_clamp_box(line_box, block.box))
        if not target_line_boxes or len(target_line_boxes) != len(lines):
            y += line_height + line_gap
    drawn_count = sum(_inside(item["box"], block.box) for item in glyphs)
    return {
        "fits": fits, "lines": lines, "line_boxes": boxes,
        "glyphs": glyphs, "drawn_char_count": drawn_count,
        "char_gap": char_gap, "line_gap": line_gap,
    }


def _vertical_candidate(block, style, size, font, stroke):
    x1, y1, x2, y2 = block.box
    width, height = max(0, x2 - x1), max(0, y2 - y1)
    text = "".join(block.rewrite_text.split())
    original_cols = max(1, len(block.lines))
    lengths = [len(normalize_for_match(line.text)) for line in sort_lines_for_block(block.lines, "vertical")]
    char_gap = max(-size // 4, int(style.char_gap))
    line_gap = max(0, int(style.line_gap))
    col_count = original_cols
    cols = _split_by_ratios(text, lengths, col_count)
    horizontal_scale = float(
        (getattr(block, "reviewed_style_lock", None) or {}).get(
            "horizontal_scale", getattr(style, "width_ratio", 1.0)
        )
    )
    shaped = [
        _shape_vertical_column(font, col, char_gap, stroke, horizontal_scale)
        for col in cols
    ]
    while col_count < max(1, len(text)) and any(item[2] > height for item in shaped):
        col_count += 1
        cols = _split_by_ratios(text, [1] * col_count, col_count)
        shaped = [
            _shape_vertical_column(font, col, char_gap, stroke, horizontal_scale)
            for col in cols
        ]
    total_width = sum(item[1] for item in shaped) + line_gap * max(0, len(cols) - 1)
    fits = bool(width > 0 and height > 0 and total_width <= width and max([item[2] for item in shaped] or [0]) <= height)
    right = x2 - max(0, (width - min(width, total_width)) // 2)
    glyphs, boxes = [], []
    current_right = right
    for raw_glyphs, col_width, col_height in shaped:
        col_left = current_right - col_width
        top = y1 + max(0, (height - min(height, col_height)) // 2)
        absolute_boxes = []
        for item in raw_glyphs:
            bbox = item["font_box"]
            glyph_left = col_left + (col_width - item["width"]) // 2
            glyph_top = top + item["top"]
            absolute = (
                int(glyph_left), int(glyph_top),
                int(glyph_left + item["width"]), int(glyph_top + item["height"]),
            )
            glyphs.append({
                "original": item["original"], "rendered": item["rendered"],
                "box": absolute,
                "draw": (float(glyph_left - bbox[0]), float(glyph_top - bbox[1])),
            })
            absolute_boxes.append(absolute)
        col_box = _union_or_zero(absolute_boxes)
        if not absolute_boxes:
            col_box = (col_left, top, col_left, top)
        boxes.append(_clamp_box(col_box, block.box))
        current_right = col_left - line_gap
    drawn_count = sum(_inside(item["box"], block.box) for item in glyphs)
    return {
        "fits": fits, "lines": cols, "line_boxes": boxes,
        "glyphs": glyphs, "drawn_char_count": drawn_count,
        "char_gap": char_gap, "line_gap": line_gap,
    }


def layout_text_block(block):
    if block.kind == "sfx_keep":
        return {
            "skipped": True, "line_boxes": [], "glyphs": [],
            "overflow": False, "input_char_count": 0, "drawn_char_count": 0,
        }
    style = block.style or _default_style(block)
    role = ROLE_MAP.get(block.kind, "dialogue")
    match = block.font_match or safe_font_match(role)
    stroke = min(2, max(0, int(round(style.stroke_width))))
    start_size = max(8, int(round(style.font_size)))
    selected = None
    font_warnings = []
    actual_match = match
    reviewed_style_lock = getattr(block, "reviewed_style_lock", None)
    sizes = [start_size] if reviewed_style_lock else range(start_size, 7, -1)
    for size in sizes:
        font, warnings, actual_match = _get_font_and_match(size, match, role)
        font_warnings.extend(warnings)
        candidate = (
            _vertical_candidate(block, style, size, font, stroke)
            if block.orientation == "vertical"
            else _horizontal_candidate(block, style, size, font, stroke)
        )
        selected = (size, font, candidate)
        if candidate["fits"]:
            break
    size, font, candidate = selected
    overflow = not candidate["fits"]
    if overflow and "text_overflow" not in block.warnings:
        block.warnings.append("text_overflow")
    block.warnings.extend(warning for warning in font_warnings if warning not in block.warnings)
    return {
        "skipped": False,
        "orientation": block.orientation,
        "font_path": str(actual_match.path),
        "font_weight": actual_match.weight,
        "font_fallback_used": bool(
            actual_match.fallback_used or Path(actual_match.path) != Path(match.path)
        ),
        "font_size": size,
        "line_boxes": candidate["line_boxes"],
        "lines": candidate["lines"],
        "glyphs": [
            {key: value for key, value in glyph.items() if key != "draw"}
            for glyph in candidate["glyphs"]
        ],
        "input_char_count": len("".join(block.rewrite_text.split())),
        "drawn_char_count": candidate["drawn_char_count"],
        "char_gap": candidate["char_gap"],
        "line_gap": candidate["line_gap"],
        "stroke_width": stroke,
        "overflow": overflow,
        "warnings": list(dict.fromkeys(font_warnings + (["text_overflow"] if overflow else []))),
        "_font": font,
        "_glyphs": candidate["glyphs"],
    }


def write_layout_preflight(output_dir, block_map, source_lookup):
    """Prove reviewed text fits at the locked size before running LaMa."""
    pages = []
    overflow_block_count = 0
    for internal_name, blocks in block_map.items():
        page_blocks = []
        for block in blocks:
            if block.kind == "sfx_keep":
                continue
            layout = layout_text_block(block)
            overflow_block_count += int(bool(layout["overflow"]))
            page_blocks.append({
                "block_id": block.block_id,
                "box": list(block.box),
                "font_path": layout["font_path"],
                "font_weight": layout["font_weight"],
                "font_size": layout["font_size"],
                "line_boxes": layout["line_boxes"],
                "lines": layout["lines"],
                "input_char_count": layout["input_char_count"],
                "drawn_char_count": layout["drawn_char_count"],
                "overflow": layout["overflow"],
                "warnings": layout["warnings"],
            })
        source_relative = (
            source_lookup[internal_name].source_relative
            if internal_name in source_lookup
            else internal_name
        )
        pages.append({
            "internal_name": internal_name,
            "source_relative": source_relative,
            "blocks": page_blocks,
        })
    report = {
        "status": "passed" if overflow_block_count == 0 else "evidence_blocked",
        "page_count": len(pages),
        "overflow_block_count": overflow_block_count,
        "pages": pages,
    }
    output_dir = Path(output_dir)
    (output_dir / "layout_preflight.json").write_text(
        json.dumps(_stringify_paths(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def draw_text_block(image, block, layout=None):
    layout = layout or layout_text_block(block)
    if layout["skipped"]:
        return layout
    if layout["overflow"]:
        raise LayoutOverflowError(
            f"text overflow in block {block.box}: "
            f"{layout['drawn_char_count']}/{layout['input_char_count']} characters fit",
            [layout],
        )
    x1, y1, x2, y2 = block.box
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(image.width, x2), min(image.height, y2)
    if x2 <= x1 or y2 <= y1:
        return layout
    reviewed_style_lock = getattr(block, "reviewed_style_lock", None)
    if reviewed_style_lock:
        fill_rgba = reviewed_style_lock.get("fill_rgba", [*block.style.fill, 255])
        stroke_rgba = reviewed_style_lock.get("stroke_rgba", [0, 0, 0, 0])
        fill = tuple(int(value) for value in fill_rgba[:3])
        stroke_fill = (
            tuple(int(value) for value in stroke_rgba[:3])
            if int(stroke_rgba[3]) > 0
            else fill
        )
        for glyph in layout["_glyphs"]:
            font = layout["_font"]
            stroke = layout["stroke_width"]
            natural = font.getbbox(glyph["rendered"], stroke_width=stroke, anchor="lt")
            natural_width = max(1, natural[2] - natural[0])
            natural_height = max(1, natural[3] - natural[1])
            tile = Image.new("RGBA", (natural_width, natural_height), (0, 0, 0, 0))
            tile_draw = ImageDraw.Draw(tile)
            tile_draw.text(
                (-natural[0], -natural[1]), glyph["rendered"], font=font,
                fill=(*fill, 255), stroke_width=stroke,
                stroke_fill=(*stroke_fill, 255), anchor="lt",
            )
            gx1, gy1, gx2, gy2 = glyph["box"]
            target_size = (max(1, gx2 - gx1), max(1, gy2 - gy1))
            if tile.size != target_size:
                tile = tile.resize(target_size, Image.Resampling.LANCZOS)
            image.paste(tile, (gx1, gy1), tile)
        return layout
    mask = Image.new("L", (x2 - x1, y2 - y1), 0)
    mask_draw = ImageDraw.Draw(mask)
    font = layout["_font"]
    stroke = layout["stroke_width"]
    for glyph in layout["_glyphs"]:
        x, y = glyph["draw"]
        mask_draw.text(
            (x - x1, y - y1), glyph["rendered"], font=font, fill=255,
            stroke_width=stroke, stroke_fill=255, anchor="lt",
        )
    binary = mask.point(lambda value: 255 if value >= 128 else 0)
    style = block.style or _default_style(block)
    fill = (255, 255, 255) if sum(style.fill) >= 384 else (0, 0, 0)
    color = Image.new("RGB", binary.size, fill)
    image.paste(color, (x1, y1), binary)
    return layout


def draw_horizontal(draw, block):
    return draw_text_block(draw._image, block)


def draw_vertical(draw, block):
    return draw_text_block(draw._image, block)


def redraw_clean(clean_path, dest_path, blocks, expected_size=None):
    image = Image.open(clean_path).convert("RGB")
    if expected_size is not None and tuple(image.size) != tuple(expected_size):
        raise ValueError(
            f"clean image size {image.size} does not match analyzed image size {expected_size}"
        )
    renderable = [block for block in blocks if block.kind != "sfx_keep"]
    layouts = [layout_text_block(block) for block in renderable]
    overflow = [layout for layout in layouts if layout["overflow"]]
    if overflow:
        raise LayoutOverflowError(
            f"page contains {len(overflow)} overflowing text block(s)", overflow
        )
    for block, layout in zip(renderable, layouts):
        draw_text_block(image, block, layout)
    image.save(dest_path, quality=95)


def apply_reviewed_background_cleanup(original_path, clean_path, blocks):
    """Replace only reviewed flat-background masks with deterministic inpainting."""
    selected = [
        block for block in blocks
        if getattr(block, "background_cleanup", "lama") == "flat_inpaint"
        and block.kind != "sfx_keep"
    ]
    if not selected:
        return 0
    original = np.asarray(Image.open(original_path).convert("RGB"), dtype=np.uint8)
    clean = np.asarray(Image.open(clean_path).convert("RGB"), dtype=np.uint8).copy()
    if original.shape != clean.shape:
        raise ValueError("original and LaMa clean image sizes differ")
    for block in selected:
        mask = np.asarray(make_mask(
            (original.shape[1], original.shape[0]), [block]
        ), dtype=np.uint8)
        deterministic = cv2.inpaint(original, mask, 5, cv2.INPAINT_TELEA)
        clean[mask > 0] = deterministic[mask > 0]
    Image.fromarray(clean, mode="RGB").save(clean_path)
    return len(selected)


def page_cleanup_backend(blocks):
    """Return one generative cleanup backend per page; flat cleanup may coexist."""
    methods = {
        getattr(block, "background_cleanup", "lama")
        for block in blocks
        if block.kind != "sfx_keep"
    }
    if "lama" in methods and "gpt_image_2" in methods:
        raise ValueError("a page cannot mix LaMa and GPT Image 2 cleanup")
    if "gpt_image_2" in methods:
        return "gpt_image_2"
    if "lama" in methods:
        return "lama"
    return "deterministic_fill"


def prepare_non_lama_clean_images(images, clean_dir, image2_clean_dir, block_map):
    """Stage original or reviewed Image 2 pages without mutating either source."""
    clean_root = Path(clean_dir)
    image2_root = Path(image2_clean_dir) if image2_clean_dir else None
    staged = 0
    for image_path in images:
        blocks = block_map.get(image_path.name, [])
        backend = page_cleanup_backend(blocks)
        if backend == "lama":
            continue
        if backend == "gpt_image_2":
            if image2_root is None:
                raise ValueError(
                    f"GPT Image 2 cleanup is required for {image_path.name}; "
                    "provide --image2-clean-dir"
                )
            source = find_clean(image2_root, image_path)
            if source is None:
                raise FileNotFoundError(
                    f"GPT Image 2 clean image not found: {image_path.name}"
                )
        else:
            source = image_path
        destination = clean_root / f"{image_path.stem}.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        staged += 1
    return staged


def draw_overlay(src_path, dest_path, blocks, qa_result=None):
    image = Image.open(src_path).convert("RGB")
    draw = ImageDraw.Draw(image, "RGBA")
    label_match = safe_font_match("black_caption")
    qa_warnings = (qa_result or {}).get("block_warnings", [])
    label_metadata = []
    overlay_outlines = []
    for index, block in enumerate(blocks):
        x1, y1, x2, y2 = block.box
        warnings = qa_warnings[index] if index < len(qa_warnings) else []
        if block.kind == "sfx_keep":
            color = (0, 190, 0, 95)
        elif "background_damage_risk" in warnings:
            color = (220, 0, 220, 95)
        elif any(code in warnings for code in (
            "font_match_low_confidence", "ocr_low_confidence",
            "source_match_low_confidence", "source_match_too_long_kept_ocr",
            "text_overflow", "sfx_classification_review",
        )):
            color = (235, 190, 0, 95)
        else:
            color = (220, 0, 0, 85)
        overlay_outlines.append(((x1, y1, x2, y2), color))
        draw.rectangle((x1, y1, x2, y2), outline=color[:3] + (220,), width=3, fill=color)
        label = block.kind + (":" + ",".join(warnings[:2]) if warnings else "")
        fitted = None
        for font_size in range(18, 3, -1):
            label_font, _warnings = get_font(font_size, label_match)
            text_box = draw.textbbox((0, 0), label, font=label_font)
            width = text_box[2] - text_box[0]
            height = text_box[3] - text_box[1]
            if width <= image.width and height <= image.height:
                fitted = (label, False, font_size, label_font, text_box)
                break
        if fitted is None:
            font_size = 4
            label_font, _warnings = get_font(font_size, label_match)
            candidates = [label[:prefix_length] + "…"
                          for prefix_length in range(len(label) - 1, -1, -1)]
            candidates.append(label[:1])
            for candidate in dict.fromkeys(candidates):
                if not candidate:
                    continue
                text_box = draw.textbbox((0, 0), candidate, font=label_font)
                width = text_box[2] - text_box[0]
                height = text_box[3] - text_box[1]
                if width <= image.width and height <= image.height:
                    fitted = (candidate, True, font_size, label_font, text_box)
                    break
        if fitted is None:
            label_metadata.append({
                "bbox": None, "drawn_text": "", "truncated": True,
                "skipped": True, "font_size": None,
            })
            continue
        drawn_text, truncated, font_size, label_font, text_box = fitted
        width = text_box[2] - text_box[0]
        height = text_box[3] - text_box[1]
        actual_left = max(0, min(x1 + 3, image.width - width))
        actual_top = max(0, min(y1 - height, image.height - height))
        label_x = actual_left - text_box[0]
        label_y = actual_top - text_box[1]
        draw.text((label_x, label_y), drawn_text, font=label_font, fill=(0, 0, 0, 255))
        actual_box = draw.textbbox((label_x, label_y), drawn_text, font=label_font)
        if not (0 <= actual_box[0] < actual_box[2] <= image.width
                and 0 <= actual_box[1] < actual_box[3] <= image.height):
            raise RuntimeError("overlay label bbox escaped image bounds")
        label_metadata.append({
            "bbox": list(actual_box), "drawn_text": drawn_text,
            "truncated": truncated, "skipped": False, "font_size": font_size,
        })
    for box, color in overlay_outlines:
        draw.rectangle(box, outline=color[:3] + (220,), width=3)
    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    image.save(dest_path, quality=92)
    return {"labels": label_metadata}


def make_comparison(pairs, dest):
    entries = [{"page": src.name, "original": src, "final": out, "warnings": []}
               for src, out in pairs]
    return make_contact_sheet(entries, dest, max_pages_per_sheet=12)


def run_page_qa(original_path, clean_path, final_path, mask_path, blocks, layouts=None):
    result = check_page(original_path, clean_path, final_path, mask_path, blocks, layouts)
    result["page"] = Path(original_path).name
    result["blocks"] = [
        {**block_to_json(block), "warnings": result["block_warnings"][index]}
        for index, block in enumerate(blocks)
    ]
    return result


def _public_warning_codes(raw_warnings):
    warnings = [code for code in raw_warnings if code in WARNING_CODES]
    if any(code not in WARNING_CODES for code in raw_warnings):
        warnings.append("internal_review_warning")
    if "page_processing_failed" not in warnings:
        warnings.append("page_processing_failed")
    return list(dict.fromkeys(warnings))


def qa_failure(page, stage, exc, raw_warnings=None):
    raw = list(raw_warnings or [])
    if isinstance(exc, LayoutOverflowError):
        raw.append("text_overflow")
    raw.append("page_processing_failed")
    problem = f"{type(exc).__name__}: {exc}"
    return {
        "page": Path(page).name,
        "stage": stage,
        "stages": [stage],
        "warnings": _public_warning_codes(raw),
        "raw_warnings": list(dict.fromkeys(raw)),
        "problem": problem,
        "problems": [problem],
        "blocks": [],
        "thresholds": dict(QA_THRESHOLDS),
    }


def _merge_failure_results(page, records):
    stages = []
    problems = []
    raw_warnings = []
    for record in records:
        stages.extend(record.get("stages") or [record.get("stage", "processing")])
        problems.extend(record.get("problems") or [record.get("problem", "page processing failed")])
        raw_warnings.extend(record.get("raw_warnings") or record.get("warnings", []))
    stages = list(dict.fromkeys(stage for stage in stages if stage))
    problems = list(dict.fromkeys(problem for problem in problems if problem))
    raw_warnings = list(dict.fromkeys(raw_warnings + ["page_processing_failed"]))
    return {
        "page": Path(page).name,
        "stage": stages[0] if len(stages) == 1 else "multiple",
        "stages": stages,
        "warnings": _public_warning_codes(raw_warnings),
        "raw_warnings": raw_warnings,
        "problem": " | ".join(problems),
        "problems": problems,
        "blocks": [],
        "thresholds": dict(QA_THRESHOLDS),
    }


def _write_placeholder(destination, size, title, detail):
    image = Image.new("RGB", size, (245, 245, 245))
    draw = ImageDraw.Draw(image)
    border = max(1, min(image.size) // 40)
    draw.rectangle((0, 0, image.width - 1, image.height - 1),
                   outline=(190, 35, 35), width=border)
    message = f"{title}\n{detail}".encode("ascii", "backslashreplace").decode("ascii")
    font = ImageFont.load_default()
    text_box = draw.multiline_textbbox((0, 0), message, font=font, spacing=4)
    text_width = text_box[2] - text_box[0]
    text_height = text_box[3] - text_box[1]
    draw.multiline_text(
        (max(0, (image.width - text_width) // 2),
         max(0, (image.height - text_height) // 2)),
        message, font=font, fill=(150, 20, 20), spacing=4, align="center",
    )
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination)
    return destination


def _failure_contact_entry(page, original_path, comparison_dir, failure):
    original_path = Path(original_path) if original_path else None
    source_size = (640, 480)
    source_display = original_path
    try:
        if original_path is None:
            raise FileNotFoundError("original path missing")
        with Image.open(original_path) as source:
            source.verify()
        with Image.open(original_path) as source:
            source_size = source.size
    except (OSError, ValueError):
        source_display = _write_placeholder(
            comparison_dir / "failed_pages" / f"{Path(page).stem}_original.png",
            source_size, "SOURCE UNAVAILABLE", Path(page).name,
        )
    final_display = _write_placeholder(
        comparison_dir / "failed_pages" / f"{Path(page).stem}_failed.png",
        source_size, "QA FAILED", ", ".join(failure["warnings"]),
    )
    temporary_files = [final_display]
    if source_display != original_path:
        temporary_files.append(source_display)
    return {
        "page": Path(page).name,
        "original": source_display,
        "final": final_display,
        "warnings": failure["warnings"],
        "_temporary_files": temporary_files,
    }


def _is_reparse_point(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & reparse_flag
    )


def _safe_unlink_artifact(path, allowed_dir):
    path = Path(os.path.abspath(os.fspath(path)))
    allowed_dir = Path(os.path.abspath(os.fspath(allowed_dir)))
    if path.drive.upper() != "D:" or allowed_dir.drive.upper() != "D:":
        raise ValueError(f"artifact cleanup is restricted to D drive: {path}")
    if path == allowed_dir or allowed_dir not in path.parents:
        raise ValueError(f"artifact is outside cleanup whitelist: {path}")

    current = Path(allowed_dir.anchor)
    for part in allowed_dir.parts[1:]:
        current /= part
        if _is_reparse_point(current):
            raise ValueError(f"reparse point in cleanup whitelist: {current}")
    current = allowed_dir
    for part in path.relative_to(allowed_dir).parts:
        current /= part
        if _is_reparse_point(current):
            raise ValueError(f"reparse point in artifact path: {current}")

    resolved_dir = allowed_dir.resolve(strict=True)
    resolved_path = path.resolve(strict=False)
    if resolved_path == resolved_dir or resolved_dir not in resolved_path.parents:
        raise ValueError(f"artifact is outside cleanup whitelist: {resolved_path}")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISREG(info.st_mode):
        path.unlink()


def build_qa_outputs(pairs, clean_dir, mask_dir, overlay_dir, font_matches_dir,
                     comparison_dir, block_map, problems, source_lookup=None):
    overlay_dir = Path(overlay_dir)
    font_matches_dir = Path(font_matches_dir)
    comparison_dir = Path(comparison_dir)
    for directory in (overlay_dir, font_matches_dir, comparison_dir):
        directory.mkdir(parents=True, exist_ok=True)
    qa_results = []
    entries = []
    for original_path, final_path in pairs:
        clean_path = find_clean(clean_dir, original_path)
        mask_path = mask_dir / f"{original_path.stem}.png"
        blocks = block_map.get(original_path.name, [])
        font_report = font_matches_dir / f"{original_path.stem}.json"
        overlay_path = overlay_dir / original_path.name
        font_temp = font_matches_dir / f"{original_path.stem}.__qa_tmp__.json"
        overlay_temp = overlay_dir / (
            f"{original_path.stem}.__qa_tmp__{original_path.suffix}"
        )
        try:
            _safe_unlink_artifact(font_temp, font_matches_dir)
            _safe_unlink_artifact(overlay_temp, overlay_dir)
            if clean_path is None:
                raise FileNotFoundError("LaMa clean image not found")
            result = run_page_qa(
                original_path, clean_path, final_path, mask_path, blocks, []
            )
            write_font_match_report(
                original_path.stem, blocks,
                font_temp,
            )
            if source_lookup and original_path.name in source_lookup:
                augment_font_match_report(
                    font_temp,
                    source_lookup[original_path.name],
                    final_name=final_path.name,
                    status="ok",
                )
            draw_overlay(original_path, overlay_temp, blocks, result)
            overlay_temp.replace(overlay_path)
            font_temp.replace(font_report)
            result["artifacts"] = {
                "overlay": str(overlay_path),
                "font_report": str(font_report),
            }
            qa_results.append(result)
            entries.append({"page": original_path.name, "original": original_path,
                            "final": final_path, "warnings": result["warnings"]})
        except Exception as exc:
            _safe_unlink_artifact(font_temp, font_matches_dir)
            _safe_unlink_artifact(overlay_temp, overlay_dir)
            _safe_unlink_artifact(font_report, font_matches_dir)
            _safe_unlink_artifact(overlay_path, overlay_dir)
            failure = qa_failure(original_path.name, "qa", exc)
            qa_results.append(failure)
            entries.append(_failure_contact_entry(
                original_path.name, original_path, comparison_dir, failure,
            ))
    known = {item["page"] for item in qa_results}
    for problem in problems:
        page = Path(problem["file"]).name
        record = {
            "page": page,
            "stage": problem.get("stage", "processing"),
            "stages": problem.get("stages") or [problem.get("stage", "processing")],
            "problem": problem.get("problem", "page processing failed"),
            "problems": problem.get("problems") or [problem.get("problem", "page processing failed")],
            "warnings": problem.get("warnings", ["page_processing_failed"]),
            "raw_warnings": problem.get("raw_warnings") or problem.get("warnings", []),
        }
        existing_index = next(
            (index for index, item in enumerate(qa_results) if item["page"] == page),
            None,
        )
        if existing_index is None:
            failure = _merge_failure_results(page, [record])
            qa_results.append(failure)
            known.add(page)
        else:
            failure = _merge_failure_results(page, [qa_results[existing_index], record])
            qa_results[existing_index] = failure
        entry_index = next(
            (index for index, item in enumerate(entries) if item["page"] == page),
            None,
        )
        original_path = problem.get("original") or problem.get("path")
        if original_path is None and entry_index is not None:
            original_path = entries[entry_index].get("original")
        failure_entry = _failure_contact_entry(
            page, original_path, comparison_dir, failure,
        )
        if entry_index is None:
            entries.append(failure_entry)
        else:
            entries[entry_index] = failure_entry
    comparisons = make_contact_sheet(entries, comparison_dir, 12)
    exceptions = [entry for entry in entries if entry["warnings"]]
    exception_sheets = make_contact_sheet(
        exceptions, comparison_dir / "exceptions", 12
    )
    placeholder_dir = comparison_dir / "failed_pages"
    for entry in entries:
        for temporary_file in entry.get("_temporary_files", []):
            _safe_unlink_artifact(temporary_file, placeholder_dir)
    if placeholder_dir.is_dir() and not any(placeholder_dir.iterdir()):
        placeholder_dir.rmdir()
    return qa_results, comparisons, exception_sheets


def block_to_json(block):
    style = None
    if block.style is not None:
        style = {
            "fill": block.style.fill,
            "orientation": block.style.orientation,
            "font_size": block.style.font_size,
            "weight_score": block.style.weight_score,
            "width_ratio": block.style.width_ratio,
            "char_gap": block.style.char_gap,
            "line_gap": block.style.line_gap,
            "stroke_width": block.style.stroke_width,
            "category_scores": block.style.category_scores,
            "confidence": block.style.confidence,
            "warnings": block.style.warnings,
        }
    font_match_payload = None
    if block.font_match is not None:
        font_match_payload = {
            "family": block.font_match.family,
            "path": str(block.font_match.path),
            "weight": block.font_match.weight,
            "score": block.font_match.score,
            "confidence": block.font_match.confidence,
            "fallback_used": block.font_match.fallback_used,
            "top_candidates": [
                {**candidate, "path": str(candidate["path"])}
                for candidate in block.font_match.top_candidates
            ],
            "warnings": block.font_match.warnings,
        }
    line_boxes = [list(line.box) for line in block.lines]
    style_lock = None
    if block.reviewed_style_lock:
        style_lock = block.reviewed_style_lock
        style_gate = {"status": "ready", "reason": "reviewed manifest"}
    elif block.kind == "sfx_keep":
        style_gate = {
            "status": "not_applicable",
            "reason": "reviewed art text or sound effect is preserved",
        }
    else:
        try:
            style_lock = style_lock_from_measurements(
                block.style,
                block.font_match,
                bbox=block.box,
                line_boxes=line_boxes,
                alignment="left",
            )
            style_gate = {"status": "ready", "reason": ""}
        except (StyleEvidenceBlocked, TypeError, ValueError) as exc:
            style_gate = {"status": "evidence_blocked", "reason": str(exc)}
    return {
        "block_id": block.block_id,
        "box": block.box,
        "line_boxes": line_boxes,
        "kind": block.kind,
        "orientation": block.orientation,
        "original_text": block.original_text,
        "rewrite_text": block.rewrite_text,
        "background_cleanup": block.background_cleanup,
        "confidence": round(block.confidence, 4),
        "fill": block.fill,
        "source_match": block.source_match,
        "warnings": block.warnings,
        "style": style,
        "font_match": font_match_payload,
        "style_lock": style_lock,
        "style_gate": style_gate,
    }


def write_analysis_artifacts(output_dir, manifest, problems):
    """Persist dry-run analysis and a machine-enforced style gate summary."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    blocked_pages = []
    ready_block_count = 0
    blocked_block_count = 0
    ordinary_block_count = 0
    for page in manifest:
        page_blocked = False
        for block in page.get("blocks", []):
            if block.get("kind") == "sfx_keep":
                continue
            ordinary_block_count += 1
            gate = block.get("style_gate") or {}
            if gate.get("status") == "ready" and isinstance(block.get("style_lock"), dict):
                ready_block_count += 1
            else:
                blocked_block_count += 1
                page_blocked = True
        if page_blocked:
            blocked_pages.append(page.get("source_relative") or page.get("file"))
    failed_pages = sorted({
        str(problem.get("source_relative") or problem.get("file"))
        for problem in problems
        if isinstance(problem, dict)
    })
    status = "passed" if not blocked_pages and not failed_pages else "evidence_blocked"
    analysis_payload = {
        "status": status,
        "page_count": len(manifest),
        "pages": manifest,
        "problems": problems,
    }
    gate_report = {
        "status": status,
        "page_count": len(manifest),
        "ordinary_block_count": ordinary_block_count,
        "ready_block_count": ready_block_count,
        "blocked_block_count": blocked_block_count,
        "blocked_pages": blocked_pages,
        "failed_pages": failed_pages,
    }
    (output_dir / "analysis_manifest.json").write_text(
        json.dumps(_stringify_paths(analysis_payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "style_gate_report.json").write_text(
        json.dumps(_stringify_paths(gate_report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return gate_report


def _stringify_paths(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _stringify_paths(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stringify_paths(item) for item in value]
    return value


def collect_qa_paths(overlay_dir, font_matches_dir, qa_results,
                     comparison_files, exception_sheets,
                     exception_report_json, exception_report_md):
    overlay_dir = Path(overlay_dir)
    successful_artifacts = [
        result.get("artifacts", {})
        for result in qa_results
        if "page_processing_failed" not in result.get("warnings", [])
    ]
    overlay_files = sorted({
        str(Path(artifacts["overlay"]))
        for artifacts in successful_artifacts
        if artifacts.get("overlay") and Path(artifacts["overlay"]).is_file()
    })
    font_reports = sorted({
        str(Path(artifacts["font_report"]))
        for artifacts in successful_artifacts
        if artifacts.get("font_report") and Path(artifacts["font_report"]).is_file()
    })
    return _stringify_paths({
        "comparison": comparison_files,
        "exception_sheets": exception_sheets,
        "font_reports": font_reports,
        "exception_report_json": exception_report_json,
        "exception_report_md": exception_report_md,
        "overlay_files": overlay_files,
        "overlay_dir": overlay_dir,
    })


def build_run_report(output_dir, final_dir, manifest, qa_results,
                     processed_count, failed_count, lama_device, paths=None):
    warning_counts = aggregate_warning_counts(qa_results or [])
    family_counts = {}
    fallback_count = 0
    for item in manifest:
        for block in item.get("blocks", []):
            font = block.get("font_match")
            if font:
                family = font.get("family") or "unknown"
                family_counts[family] = family_counts.get(family, 0) + 1
                fallback_count += int(bool(font.get("fallback_used")))
    failed_pages = {
        result.get("page")
        for result in (qa_results or [])
        if "page_processing_failed" in result.get("warnings", [])
    }
    exception_pages = {
        result.get("page")
        for result in (qa_results or [])
        if result.get("warnings")
    }
    qa_summary = {
        "font_family_distribution": dict(sorted(family_counts.items())),
        "fallback_count": fallback_count,
        "processed_count": processed_count,
        "failed_count": len(failed_pages) if failed_pages else failed_count,
        "exception_page_count": len(exception_pages),
        "warning_counts": warning_counts,
        "lama_device": lama_device,
        "qa_thresholds": dict(QA_THRESHOLDS),
        "paths": _stringify_paths(paths or {}),
    }
    return {
        "output": str(output_dir),
        "final": str(final_dir),
        "manifest": manifest,
        "qa_results": qa_results or [],
        "qa_summary": qa_summary,
    }


def write_report(output_dir, final_dir, manifest, pairs, lama_device,
                 qa_results=None, problems=None, paths=None):
    qa_results = qa_results or []
    problems = problems or []
    unique_problem_pages = {
        problem.get("file", f"__problem_{index}")
        for index, problem in enumerate(problems)
    }
    report = build_run_report(
        output_dir, final_dir, manifest, qa_results,
        processed_count=len(pairs), failed_count=len(unique_problem_pages),
        lama_device=lama_device, paths=paths,
    )
    summary = report["qa_summary"]
    lines = [
        "# 批量漫画文字修复 V2 运行报告",
        "",
        f"- 输出目录：`{final_dir}`",
        f"- 处理成功：{summary['processed_count']} 页",
        f"- 处理失败：{summary['failed_count']} 页",
        f"- 异常页数：{summary['exception_page_count']} 页",
        f"- LaMa 设备：`{summary['lama_device']}`",
        f"- processed_count：{summary['processed_count']}",
        f"- failed_count：{summary['failed_count']}",
        f"- fallback_count：{summary['fallback_count']}",
        "",
        "## 字体家族分布",
        "",
    ]
    lines.extend(f"- {family}：{count}" for family, count in summary["font_family_distribution"].items())
    if not summary["font_family_distribution"]:
        lines.append("- 无")
    lines += ["", "## QA Warning 统计", ""]
    lines.extend(f"- `{code}`：{count}" for code, count in summary["warning_counts"].items())
    if not summary["warning_counts"]:
        lines.append("- 无")
    lines += ["", "## QA thresholds", "",
              f"`{json.dumps(summary['qa_thresholds'], ensure_ascii=False, sort_keys=True)}`",
              "", "## QA 产物路径", ""]
    lines.extend(
        f"- `{name}`：`{json.dumps(value, ensure_ascii=False)}`"
        for name, value in summary["paths"].items()
    )
    if not summary["paths"]:
        lines.append("- 无")
    lines += ["", "## 说明", "",
              "QA 是自动风险检测，不声称绝对正确；黄色、品红和绿色框页需结合分页对比人工复核。"]
    (output_dir / "运行报告.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def _page_problem(image_path, stage, exc):
    raw_warnings = ["page_processing_failed"]
    if isinstance(exc, LayoutOverflowError):
        raw_warnings.insert(0, "text_overflow")
    return {
        "file": image_path.name,
        "original": str(Path(image_path).resolve()),
        "stage": stage,
        "problem": f"{type(exc).__name__}: {exc}",
        "warnings": raw_warnings,
        "raw_warnings": raw_warnings,
    }


def process_analysis_pages(
    images, ocr, source_matcher, font_candidates, mask_dir, overlay_dir,
    source_lookup=None, precomputed_lines=None, reviewed_pages=None,
):
    manifest = []
    block_map = {}
    image_sizes = {}
    problems = []
    for image_path in images:
        mask_path = mask_dir / f"{image_path.stem}.png"
        overlay_path = overlay_dir / image_path.name
        try:
            page_source_matcher = (
                source_matcher.get(image_path.name)
                if isinstance(source_matcher, dict)
                else source_matcher
            )
            image_size, _lines, blocks = analyze_image(
                ocr, image_path, page_source_matcher, font_candidates,
                precomputed_lines=(precomputed_lines or {}).get(image_path.name),
            )
            source_relative = (
                source_lookup[image_path.name].source_relative
                if source_lookup and image_path.name in source_lookup
                else image_path.name
            )
            for block in blocks:
                block.block_id = stable_text_block_id(source_relative, block)
            if reviewed_pages is not None:
                if image_path.name not in reviewed_pages:
                    raise ValueError(f"reviewed manifest is missing {source_relative}")
                apply_reviewed_page(
                    blocks=blocks,
                    source_relative=source_relative,
                    review_rows=reviewed_pages[image_path.name],
                    font_candidates=font_candidates,
                )
            make_mask(image_size, blocks, mask_path)
            draw_overlay(image_path, overlay_path, blocks)
            block_map[image_path.name] = blocks
            image_sizes[image_path.name] = image_size
            entry = {
                "file": image_path.name,
                "blocks": [block_to_json(block) for block in blocks],
            }
            if source_lookup and image_path.name in source_lookup:
                page = source_lookup[image_path.name]
                entry.update({
                    "internal_name": page.internal_name,
                    "source_relative": page.source_relative,
                })
            manifest.append(entry)
        except Exception as exc:
            for artifact in (mask_path, overlay_path):
                try:
                    artifact.unlink(missing_ok=True)
                except OSError:
                    pass
            problems.append(_page_problem(image_path, "analysis", exc))
    return manifest, block_map, image_sizes, problems


def process_redraw_pages(
    images, clean_dir, final_dir, block_map, image_sizes, source_lookup=None,
):
    pairs = []
    problems = []
    outcomes = {}
    for image_path in images:
        if image_path.name not in block_map:
            reason = "analysis failed; redraw skipped"
            problems.append(_page_problem(image_path, "redraw", RuntimeError(reason)))
            outcomes[image_path.name] = {"status": "failed", "reason": reason}
            continue
        clean_path = find_clean(clean_dir, image_path)
        if clean_path is None:
            reason = "LaMa clean image not found"
            problems.append(_page_problem(image_path, "redraw", FileNotFoundError(reason)))
            outcomes[image_path.name] = {"status": "failed", "reason": reason}
            continue
        if source_lookup and image_path.name in source_lookup:
            relative_name = final_relative_output_name(
                source_lookup[image_path.name].source_relative
            )
        else:
            relative_name = final_relative_output_name(image_path.name)
        dest = final_dir.joinpath(*relative_name.split("/"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            apply_reviewed_background_cleanup(
                image_path, clean_path, block_map[image_path.name]
            )
            redraw_clean(
                clean_path, dest, block_map[image_path.name], image_sizes[image_path.name]
            )
        except Exception as exc:
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            problem = _page_problem(image_path, "redraw", exc)
            problems.append(problem)
            outcomes[image_path.name] = {
                "status": "failed", "reason": problem["problem"],
            }
            continue
        pairs.append((image_path, dest))
        outcomes[image_path.name] = {"status": "ok", "final_name": dest.name}
    if source_lookup:
        ordered_pages = [source_lookup[image.name] for image in images]
    else:
        ordered_pages = [
            InputPage(index, image.name, image.name, image)
            for index, image in enumerate(images, start=1)
        ]
    rename_lines = _workflow_rename_lines(ordered_pages, outcomes)
    return pairs, problems, rename_lines


def main():
    script_dir = Path.cwd()
    args = build_parser(script_dir).parse_args()

    try:
        catalog = load_catalog(args.font_catalog)
        font_candidates = validate_font_candidates(available_fonts(catalog))
        if len(font_candidates) != len(catalog):
            raise ValueError("catalog contains unavailable font files")
        for role in ROLE_MAP.values():
            if not candidates_for_role(font_candidates, role):
                raise ValueError(f"no available font for role {role}")
        safe_font_match("dialogue", font_candidates)
        safe_font_match("black_caption", font_candidates)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Font catalog unavailable: {exc}") from exc

    requested_output = validate_managed_output_path(args.output, script_dir)
    work_output = requested_output / "dry_run" if args.skip_lama else requested_output
    prepared = prepare_input_workflow(
        args, project_root=script_dir, output_dir=work_output,
    )
    input_dir = prepared.staged_input_dir
    output_dir = prepared.output_dir
    lama_root = Path(args.lama_root)
    mask_dir = output_dir / "masks"
    overlay_dir = output_dir / "ocr_overlay"
    for folder in (mask_dir, overlay_dir):
        folder.mkdir(parents=True, exist_ok=True)

    ocr = load_ocr()
    sample_metadata = {}
    precomputed_lines = None
    if args.sample_count > 0:
        sample_metadata = analyze_sample_metadata(ocr, prepared.pages)
        precomputed_lines = {
            name: item.get("lines", []) for name, item in sample_metadata.items()
        }
    selection = select_sample_pages(prepared.pages, args.sample_count, sample_metadata)
    (output_dir / "sample_selection.json").write_text(
        json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    selected_names = set(selection["selected_internal_names"])
    selected_pages = [page for page in prepared.pages if page.internal_name in selected_names]
    images = [page.internal_path for page in selected_pages]
    lama_input_dir = input_dir
    if args.sample_count > 0:
        lama_input_dir = prepare_selected_input(output_dir, selected_pages)
    source_lookup = {page.internal_name: page for page in prepared.pages}
    source_units = split_source_units(read_text_file(args.source))
    if args.alignment:
        try:
            source_matcher = load_confirmed_page_matchers(args.alignment, prepared.pages)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Confirmed page alignment unavailable: {exc}") from exc
    else:
        source_matcher = make_source_matcher(source_units) if source_units else None
    if args.reviewed_manifest:
        try:
            reviewed_pages = load_confirmed_review_pages(
                args.reviewed_manifest, selected_pages
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Confirmed reviewed manifest unavailable: {exc}") from exc
    else:
        reviewed_pages = None

    manifest, block_map, image_sizes, problems = process_analysis_pages(
        images, ocr, source_matcher, font_candidates, mask_dir, overlay_dir,
        source_lookup, precomputed_lines, reviewed_pages,
    )
    style_gate_report = write_analysis_artifacts(output_dir, manifest, problems)

    layout_preflight = None
    if style_gate_report["status"] == "passed" and args.reviewed_manifest:
        layout_preflight = write_layout_preflight(
            output_dir, block_map, source_lookup
        )

    if args.skip_lama:
        print(
            f"Dry run completed: {output_dir} "
            f"style_gate={style_gate_report['status']} "
            f"layout_gate={layout_preflight['status'] if layout_preflight else 'not_run'}"
        )
        return

    if style_gate_report["status"] != "passed":
        raise SystemExit(
            "Text style gate is evidence_blocked; review analysis_manifest.json "
            "and style_gate_report.json before LaMa or rendering"
        )

    if not args.reviewed_manifest:
        raise SystemExit(
            "Confirmed reviewed manifest is required before LaMa or rendering"
        )
    if layout_preflight is None or layout_preflight["status"] != "passed":
        raise SystemExit(
            "Reviewed text does not fit at locked style; inspect layout_preflight.json "
            "before LaMa"
        )

    clean_dir = output_dir / "clean_lama"
    final_dir = output_dir / "final"
    comparison_dir = output_dir / "comparison"
    font_matches_dir = output_dir / "font_matches"
    for folder in (clean_dir, final_dir, comparison_dir, font_matches_dir):
        folder.mkdir(parents=True, exist_ok=True)

    try:
        cleanup_backends = {
            image.name: page_cleanup_backend(block_map[image.name])
            for image in images
        }
    except ValueError as exc:
        raise SystemExit(f"Invalid reviewed cleanup routing: {exc}") from exc
    lama_pages = [
        page
        for page in selected_pages
        if cleanup_backends.get(page.internal_name) == "lama"
    ]
    if lama_pages:
        iopaint = lama_root / "venv311" / "Scripts" / "iopaint.exe"
        if not iopaint.exists():
            raise SystemExit(f"LaMa/IOPaint not found: {lama_root}")
        lama_device = detect_lama_device()
        print(f"Using LaMa device: {lama_device}")
        lama_run_input = prepare_selected_input(output_dir, lama_pages)
        run_lama(lama_root, lama_run_input, mask_dir, clean_dir, lama_device)
    else:
        lama_device = "not_required"
        print("LaMa not required for reviewed cleanup routes")
    try:
        prepare_non_lama_clean_images(
            images,
            clean_dir,
            args.image2_clean_dir,
            block_map,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Reviewed text cleanup candidate unavailable: {exc}") from exc

    pairs, redraw_problems, rename_lines = process_redraw_pages(
        images, clean_dir, final_dir, block_map, image_sizes, source_lookup
    )
    problems.extend(redraw_problems)
    attach_source_identity(problems, source_lookup)

    qa_results, comparison_files, exception_sheets = build_qa_outputs(
        pairs, clean_dir, mask_dir, overlay_dir, font_matches_dir,
        comparison_dir, block_map, problems, source_lookup,
    )
    attach_source_identity(qa_results, source_lookup)
    exception_json, exception_md = write_exception_report(
        [item for item in qa_results if item.get("warnings")],
        output_dir / "exception_report.json",
    )
    (final_dir / "rename_map.md").write_text("\n".join(rename_lines) + "\n", encoding="utf-8")
    qa_paths = collect_qa_paths(
        overlay_dir, font_matches_dir, qa_results,
        comparison_files, exception_sheets, exception_json, exception_md,
    )
    run_report = build_run_report(
        output_dir, final_dir, manifest, qa_results,
        processed_count=len(pairs),
        failed_count=len({problem["file"] for problem in problems}),
        lama_device=lama_device, paths=qa_paths,
    )
    run_report.update({
        "input": str(prepared.source_path.resolve()),
        "input_kind": prepared.source_kind,
        "source_map": str((output_dir / "source_map.json").resolve()),
        "sample_selection": selection,
        "source_units": len(source_units),
        "problems": problems,
        "qa": {
            "summary": run_report["qa_summary"],
            "thresholds": dict(QA_THRESHOLDS),
            "results": qa_results,
        },
    })
    (output_dir / "run_report.json").write_text(json.dumps(run_report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(
        output_dir, final_dir, manifest, pairs, lama_device,
        qa_results, problems, qa_paths,
    )
    print(final_dir)
    if not args.keep_staging:
        for staging_name in ("staged_zip", "staged_input", "selected_input"):
            safe_remove_generated_path(output_dir, output_dir / staging_name)


if __name__ == "__main__":
    main()
