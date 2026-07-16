import importlib.util
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = SKILL_ROOT / "scripts" / "audit_cache.py"


def load_module():
    spec = importlib.util.spec_from_file_location("audit_cache", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load audit cache")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AuditCacheTests(unittest.TestCase):
    def facts(self):
        return {
            "pipeline_version": "continuity_v5_unified",
            "mode": "human_visual_auto_text",
            "novel_sha256": "a" * 64,
            "reference_sha256s": ["c" * 64, "b" * 64],
            "source_page_sha256": "d" * 64,
            "cluster_id": "scene-001",
            "rule_sha256s": ["e" * 64],
        }

    def test_key_is_stable_when_reference_order_changes(self):
        cache = load_module()
        left = self.facts()
        right = {**left, "reference_sha256s": list(reversed(left["reference_sha256s"]))}
        self.assertEqual(cache.make_audit_cache_key(**left), cache.make_audit_cache_key(**right))

    def test_any_source_fact_change_invalidates_the_entry(self):
        cache = load_module()
        facts = self.facts()
        entry = cache.build_cache_entry(
            kind="ocr_proposal",
            artifact_path="evidence/cache/0001.json",
            artifact_sha256="f" * 64,
            **facts,
        )
        self.assertTrue(cache.cache_entry_matches(entry, **facts))
        self.assertFalse(
            cache.cache_entry_matches(
                entry,
                **{**facts, "source_page_sha256": "0" * 64},
            )
        )

    def test_human_or_release_approval_can_never_be_cached(self):
        cache = load_module()
        for kind in ("human_approval", "candidate_approval", "release_receipt"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValueError, "not cacheable"):
                    cache.build_cache_entry(
                        kind=kind,
                        artifact_path="evidence/unsafe.json",
                        artifact_sha256="f" * 64,
                        **self.facts(),
                    )


if __name__ == "__main__":
    unittest.main()
