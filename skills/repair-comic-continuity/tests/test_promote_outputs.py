import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import promote_outputs as promote_module  # noqa: E402
from promote_outputs import promote_outputs  # noqa: E402


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PromoteOutputsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.input_dir = self.root / "输入"
        self.candidate_dir = self.root / "候选"
        self.output_dir = self.root / "输出"
        self.input_dir.mkdir()
        self.candidate_dir.mkdir()
        self.binding_path = self.root / "release_bindings.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_file(self, root: Path, relative_path: str, data: bytes) -> Path:
        path = root / Path(*relative_path.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def page(
        self,
        relative_path: str,
        page_class: str,
        source: bytes,
        *,
        task_id: str | None = None,
        candidate_path: str | None = None,
        candidate: bytes | None = None,
    ) -> dict[str, object]:
        self.write_file(self.input_dir, relative_path, source)
        if candidate_path is not None and candidate is not None:
            self.write_file(self.candidate_dir, candidate_path, candidate)
        return {
            "relative_path": relative_path,
            "page_class": page_class,
            "source_sha256": sha256_bytes(source),
            "candidate_path": candidate_path,
            "candidate_sha256": (
                sha256_bytes(candidate) if candidate is not None else None
            ),
            "task_id": task_id,
            "status": "verified",
        }

    def write_bindings(
        self, pages: list[dict[str, object]], *, expected_count: int | None = None
    ) -> None:
        document = {
            "schema_version": "continuity_v5_release_bindings_v1",
            "status": "verified",
            "expected_count": len(pages) if expected_count is None else expected_count,
            "pages": pages,
        }
        self.binding_path.write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )

    def snapshot_output(self) -> dict[str, bytes]:
        if not self.output_dir.exists():
            return {}
        return {
            path.relative_to(self.output_dir).as_posix(): path.read_bytes()
            for path in self.output_dir.rglob("*")
            if path.is_file()
        }

    def test_promotes_mixed_classes_to_exact_unicode_paths(self) -> None:
        unchanged_source = b"original-unchanged"
        text_source = b"original-text"
        text_candidate = b"repaired-text"
        redraw_source = b"original-redraw"
        redraw_candidate = b"redrawn-page"
        pages = [
            self.page("第一章/页面一.PNG", "unchanged", unchanged_source),
            self.page(
                "第一章/页面二.jpg",
                "text_only",
                text_source,
                task_id="task-text-2",
                candidate_path="task-text-2/候选.PNG",
                candidate=text_candidate,
            ),
            self.page(
                "第二章/终章.webp",
                "full_page_redraw",
                redraw_source,
                task_id="task-redraw-3",
                candidate_path="task-redraw-3/result.webp",
                candidate=redraw_candidate,
            ),
        ]
        # An unbound candidate for an unchanged page must never be selected.
        self.write_file(self.candidate_dir, "第一章/页面一.PNG", b"malicious")
        self.write_bindings(pages)

        result = promote_outputs(
            self.input_dir, self.candidate_dir, self.output_dir, self.binding_path
        )

        self.assertEqual(
            self.snapshot_output(),
            {
                "第一章/页面一.PNG": unchanged_source,
                "第一章/页面二.jpg": text_candidate,
                "第二章/终章.webp": redraw_candidate,
            },
        )
        self.assertEqual(result["promoted_count"], 3)
        self.assertEqual(result["status"], "promoted")

    def test_rejects_source_hash_mismatch_without_changing_existing_output(self) -> None:
        existing = self.write_file(self.output_dir, "旧版/保留.png", b"keep-me")
        pages = [self.page("页一.png", "unchanged", b"source")]
        pages[0]["source_sha256"] = "0" * 64
        self.write_bindings(pages)
        before = existing.read_bytes()

        with self.assertRaisesRegex(ValueError, "source SHA256 mismatch"):
            promote_outputs(
                self.input_dir, self.candidate_dir, self.output_dir, self.binding_path
            )

        self.assertEqual(existing.read_bytes(), before)
        self.assertEqual(self.snapshot_output(), {"旧版/保留.png": b"keep-me"})

    def test_rejects_candidate_hash_mismatch_without_creating_output(self) -> None:
        pages = [
            self.page(
                "页一.jpg",
                "text_only",
                b"source",
                task_id="task-1",
                candidate_path="task-1/page.jpg",
                candidate=b"candidate",
            )
        ]
        pages[0]["candidate_sha256"] = "f" * 64
        self.write_bindings(pages)

        with self.assertRaisesRegex(ValueError, "candidate SHA256 mismatch"):
            promote_outputs(
                self.input_dir, self.candidate_dir, self.output_dir, self.binding_path
            )

        self.assertFalse(self.output_dir.exists())

    def test_requires_verified_complete_exact_input_bijection(self) -> None:
        pages = [self.page("嵌套/页一.jpeg", "unchanged", b"one")]
        self.write_file(self.input_dir, "嵌套/页二.png", b"two")
        self.write_bindings(pages, expected_count=2)

        with self.assertRaisesRegex(ValueError, "exact input path set"):
            promote_outputs(
                self.input_dir, self.candidate_dir, self.output_dir, self.binding_path
            )

        self.assertFalse(self.output_dir.exists())

        self.write_bindings(pages)
        document = json.loads(self.binding_path.read_text(encoding="utf-8"))
        document["pages"][0]["status"] = "pending"
        self.binding_path.write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "not verified"):
            promote_outputs(
                self.input_dir, self.candidate_dir, self.output_dir, self.binding_path
            )

    def test_rejects_path_escape_and_duplicate_unicode_collisions(self) -> None:
        escape_page = self.page("页一.png", "unchanged", b"source")
        escape_page["relative_path"] = "../页一.png"
        self.write_bindings([escape_page])
        with self.assertRaisesRegex(ValueError, "unsafe relative_path"):
            promote_outputs(
                self.input_dir, self.candidate_dir, self.output_dir, self.binding_path
            )

        self.input_dir.joinpath("页一.png").unlink()
        composed = self.page("章节/é.png", "unchanged", b"composed")
        decomposed = self.page("章节/e\u0301.png", "unchanged", b"decomposed")
        self.write_bindings([composed, decomposed])
        with self.assertRaisesRegex(ValueError, "duplicate Unicode path"):
            promote_outputs(
                self.input_dir, self.candidate_dir, self.output_dir, self.binding_path
            )

    def test_rejects_two_tasks_competing_for_one_page(self) -> None:
        first = self.page(
            "页一.png",
            "text_only",
            b"source",
            task_id="task-a",
            candidate_path="task-a/page.png",
            candidate=b"candidate-a",
        )
        second = dict(first)
        second["task_id"] = "task-b"
        second["candidate_path"] = "task-b/page.png"
        self.write_file(self.candidate_dir, "task-b/page.png", b"candidate-b")
        second["candidate_sha256"] = sha256_bytes(b"candidate-b")
        self.write_bindings([first, second])

        with self.assertRaisesRegex(ValueError, "competing tasks"):
            promote_outputs(
                self.input_dir, self.candidate_dir, self.output_dir, self.binding_path
            )

    def test_single_writer_lock_rejects_concurrent_promotion(self) -> None:
        pages = [self.page("页一.png", "unchanged", b"source")]
        self.write_bindings(pages)
        lock = self.output_dir.parent / f".{self.output_dir.name}.promote.lock"
        lock.write_text("other-writer", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "promotion lock already held"):
            promote_outputs(
                self.input_dir, self.candidate_dir, self.output_dir, self.binding_path
            )

        self.assertFalse(self.output_dir.exists())
        self.assertEqual(lock.read_text(encoding="utf-8"), "other-writer")

    def test_failed_directory_swap_restores_existing_output(self) -> None:
        self.write_file(self.output_dir, "旧版.png", b"old-output")
        pages = [self.page("新版.png", "unchanged", b"new-output")]
        self.write_bindings(pages)
        real_replace = os.replace
        replace_calls = 0

        def fail_staging_swap(source: os.PathLike[str], target: os.PathLike[str]) -> None:
            nonlocal replace_calls
            replace_calls += 1
            if replace_calls == 2:
                raise OSError("injected swap failure")
            real_replace(source, target)

        with mock.patch.object(promote_module.os, "replace", side_effect=fail_staging_swap):
            with self.assertRaisesRegex(OSError, "injected swap failure"):
                promote_outputs(
                    self.input_dir,
                    self.candidate_dir,
                    self.output_dir,
                    self.binding_path,
                )

        self.assertEqual(self.snapshot_output(), {"旧版.png": b"old-output"})
        self.assertFalse(
            (self.output_dir.parent / f".{self.output_dir.name}.promote.lock").exists()
        )


if __name__ == "__main__":
    unittest.main()
