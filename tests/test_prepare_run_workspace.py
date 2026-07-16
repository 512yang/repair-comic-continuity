import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from prepare_run_workspace import prepare_run_workspace  # noqa: E402


class PrepareRunWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.target = self.root / "runs" / "run-001"
        for name in ("人物参考图", "输入", "输出"):
            (self.source / name).mkdir(parents=True)
        (self.source / "丹符神尊.txt").write_text("第一章\n测试", encoding="utf-8")
        Image.new("RGB", (12, 16), "white").save(self.source / "人物参考图" / "主角.png")
        nested = self.source / "输入" / "第一章"
        nested.mkdir()
        Image.new("RGB", (12, 16), "gray").save(nested / "0199.jpg")
        Image.new("RGB", (12, 16), "red").save(self.source / "输出" / "0199.jpg")
        stale = self.source / "work" / "candidates"
        stale.mkdir(parents=True)
        Image.new("RGB", (12, 16), "blue").save(stale / "0199.jpg")

    def tearDown(self):
        self.temp.cleanup()

    def tree_bytes(self, root):
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def test_creates_clean_byte_copied_run_root_without_old_outputs(self):
        before = self.tree_bytes(self.source)
        result = prepare_run_workspace(self.source, self.target)

        self.assertEqual(result["status"], "prepared")
        self.assertEqual(result["pipeline_id"], "continuity_v5_unified")
        self.assertEqual(result["input_count"], 1)
        self.assertEqual(before, self.tree_bytes(self.source))
        source_page = self.source / "输入" / "第一章" / "0199.jpg"
        staged_page = self.target / "输入" / "第一章" / "0199.jpg"
        self.assertEqual(source_page.read_bytes(), staged_page.read_bytes())
        self.assertFalse(source_page.samefile(staged_page))
        self.assertEqual([], list((self.target / "输出").rglob("*")))
        self.assertEqual([], list((self.target / "evidence").rglob("*")))
        self.assertFalse((self.target / "work").exists())
        manifest = json.loads((self.target / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(result, manifest)
        self.assertEqual(["输入/第一章/0199.jpg"], [row["path"] for row in manifest["inputs"]])

    def test_rejects_target_inside_source_or_nonempty_target(self):
        with self.assertRaisesRegex(ValueError, "outside source project"):
            prepare_run_workspace(self.source, self.source / "runs" / "bad")
        self.target.mkdir(parents=True)
        (self.target / "keep.txt").write_text("do not overwrite", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "empty or absent"):
            prepare_run_workspace(self.source, self.target)


if __name__ == "__main__":
    unittest.main()
