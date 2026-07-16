import importlib.util
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = SKILL_ROOT / "scripts" / "text_cleanup_router.py"


def load_module():
    spec = importlib.util.spec_from_file_location("text_cleanup_router", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load text cleanup router")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TextCleanupRouterTests(unittest.TestCase):
    def test_routes_flat_background_without_generation(self):
        router = load_module()
        self.assertEqual(
            router.choose_cleanup_route("flat", "not_applicable", False),
            "deterministic_fill",
        )

    def test_prefers_lama_only_after_a_passed_canary(self):
        router = load_module()
        self.assertEqual(router.choose_cleanup_route("texture", "pass", True), "lama")

    def test_falls_back_to_gpt_image_2_when_lama_is_unsafe(self):
        router = load_module()
        self.assertEqual(
            router.choose_cleanup_route("critical_art", "unsafe", True),
            "gpt_image_2",
        )
        request = router.build_image2_cleanup_request(
            page="分镜/0003.jpg",
            source_sha256="a" * 64,
            mask_sha256="b" * 64,
            width=900,
            height=1200,
            block_ids=["b1", "b2"],
        )
        self.assertEqual(request["operation"], "remove_text_only")
        self.assertFalse(request["allow_typesetting"])
        self.assertTrue(request["preserve_outside_mask"])
        self.assertIn("不得添加任何文字", request["prompt"])

    def test_blocks_when_no_safe_cleanup_route_exists(self):
        router = load_module()
        self.assertEqual(
            router.choose_cleanup_route("critical_art", "unsafe", False),
            "evidence_blocked",
        )


if __name__ == "__main__":
    unittest.main()
