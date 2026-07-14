import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pipeline_contracts import (  # noqa: E402
    ALIGNMENT_STATES,
    PAGE_CLASSES,
    REVIEW_STATES,
    REMOVE_LEGACY_INT_OUTPUT_NAMES_IN_TASK_3,
    TASK_STATES,
    canonical_hash,
    make_output_names,
    normalize_relative_image_path,
    normalize_page_id,
    validate_bijection,
)


class PipelineContractTests(unittest.TestCase):
    def test_normalize_page_id_converts_fullwidth_parentheses_and_drops_extension(self):
        self.assertEqual(normalize_page_id("252（1）.jpg"), "252(1)")

    def test_normalize_page_id_uses_basename_nfkc_and_strip(self):
        self.assertEqual(
            normalize_page_id(r"C:\comic\ Ａ12（2）.JPEG "),
            "A12(2)",
        )

    def test_normalize_page_id_rejects_noncanonical_characters(self):
        with self.assertRaisesRegex(ValueError, "invalid page id"):
            normalize_page_id("252-1.jpg")

    def test_normalize_page_id_rejects_non_path_values(self):
        for value in (None, True, 252):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "string or path-like"):
                    normalize_page_id(value)

    def test_normalize_page_id_accepts_pathlike_values(self):
        self.assertEqual(normalize_page_id(Path("252（1）.jpg")), "252(1)")

    def test_canonical_hash_uses_compact_sorted_utf8_json(self):
        self.assertEqual(
            canonical_hash({"文": "字", "a": 1}),
            "34c6fc414ec748193fef5f5ac8e503f1876061cbdf575e8ee8f62a805f67f65f",
        )

    def test_canonical_hash_rejects_non_finite_numbers_as_value_error(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "non-finite"):
                    canonical_hash({"value": value})

    def test_make_output_names_preserves_exact_relative_paths_and_extensions(self):
        self.assertEqual(
            make_output_names(["252（1）.jpg", r"chapter\2.webp", "彩页/03.PNG"]),
            ["252（1）.jpg", "chapter/2.webp", "彩页/03.PNG"],
        )

    def test_make_output_names_keeps_deprecated_int_seam_until_task_3(self):
        self.assertTrue(REMOVE_LEGACY_INT_OUTPUT_NAMES_IN_TASK_3)
        self.assertEqual(make_output_names(3), ["0001.jpg", "0002.jpg", "0003.jpg"])

    def test_make_output_names_rejects_case_insensitive_duplicates(self):
        with self.assertRaisesRegex(ValueError, "duplicate output name"):
            make_output_names(["Chapter/Page.jpg", "chapter/page.JPG"])

    def test_normalize_relative_image_path_rejects_unsafe_or_unsupported_paths(self):
        invalid = (
            "",
            "/absolute.jpg",
            r"C:\absolute.jpg",
            "../escape.jpg",
            "a//b.jpg",
            "a/./b.jpg",
            "page.gif",
            "control/line\n.jpg",
            "bad<name.jpg",
            "bad>name.jpg",
            'bad"name.jpg',
            "bad|name.jpg",
            "bad?name.jpg",
            "bad*name.jpg",
            "stream:name.jpg",
            "folder./page.jpg",
            "folder /page.jpg",
            "page.jpg ",
            "CON.jpg",
            "dir/prn.PNG",
            "AUX.txt.jpg",
            "nul.webp",
            "COM1.jpeg",
            "com9.jpg",
            "LPT1.jpg",
            "lpt9.jpg",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "relative image path"):
                    normalize_relative_image_path(value)

        self.assertEqual("COM10.jpg", normalize_relative_image_path("COM10.jpg"))
        self.assertEqual("LPT10.png", normalize_relative_image_path("LPT10.png"))

    def test_validate_bijection_rejects_a_missing_source_mapping(self):
        inputs = ["252.jpg", "253.jpg"]
        mappings = [{"source_page": "252.jpg", "output_name": "0001.jpg"}]

        with self.assertRaisesRegex(ValueError, "missing source"):
            validate_bijection(inputs, mappings)

    def test_validate_bijection_rejects_legacy_mapping_aliases(self):
        invalid_mappings = (
            ({"source": "252.jpg", "output_name": "0001.jpg"}, "missing source_page"),
            ({"source_page": "252.jpg", "output": "0001.jpg"}, "missing output_name"),
        )
        for mapping, message in invalid_mappings:
            with self.subTest(mapping=mapping):
                with self.assertRaisesRegex(ValueError, message):
                    validate_bijection(["252.jpg"], [mapping])

    def test_validate_bijection_accepts_the_exact_contract(self):
        mappings = [
            {"source_page": "252（1）.jpg", "output_name": "252（1）.jpg"},
            {"source_page": r"chapter\253.webp", "output_name": "chapter/253.webp"},
        ]

        self.assertIsNone(
            validate_bijection(
                ["252（1）.jpg", "chapter/253.webp"],
                mappings,
                actual_outputs=["chapter/253.webp", "252（1）.jpg"],
            )
        )

    def test_validate_bijection_rejects_renamed_output(self):
        with self.assertRaisesRegex(ValueError, "output name must equal source"):
            validate_bijection(
                ["252（1）.jpg"],
                [{"source_page": "252（1）.jpg", "output_name": "0001.jpg"}],
            )

    def test_validate_bijection_rejects_duplicate_normalized_inputs(self):
        with self.assertRaisesRegex(ValueError, "duplicate input"):
            validate_bijection(
                ["Page.jpg", "page.JPG"],
                [
                    {"source_page": "Page.jpg", "output_name": "Page.jpg"},
                    {"source_page": "page.JPG", "output_name": "page.JPG"},
                ],
            )

    def test_validate_bijection_rejects_mapping_count_mismatch(self):
        with self.assertRaisesRegex(ValueError, "mapping count"):
            validate_bijection(
                ["252.jpg"],
                [
                    {"source_page": "252.jpg", "output_name": "0001.jpg"},
                    {"source_page": "253.jpg", "output_name": "0002.jpg"},
                ],
            )

    def test_validate_bijection_rejects_duplicate_source_mappings(self):
        with self.assertRaisesRegex(ValueError, "duplicate source"):
            validate_bijection(
                ["252.jpg", "253.jpg"],
                [
                    {"source_page": "252.jpg", "output_name": "0001.jpg"},
                    {"source_page": "252.jpg", "output_name": "0002.jpg"},
                ],
            )

    def test_validate_bijection_rejects_source_order_mismatch(self):
        with self.assertRaisesRegex(ValueError, "source order"):
            validate_bijection(
                ["252.jpg", "253.jpg"],
                [
                    {"source_page": "253.jpg", "output_name": "0001.jpg"},
                    {"source_page": "252.jpg", "output_name": "0002.jpg"},
                ],
            )

    def test_validate_bijection_rejects_nonexact_output_names(self):
        with self.assertRaisesRegex(ValueError, "output name must equal source"):
            validate_bijection(
                ["252.jpg", "253.jpg"],
                [
                    {"source_page": "252.jpg", "output_name": "0002.jpg"},
                    {"source_page": "253.jpg", "output_name": "0001.jpg"},
                ],
            )

    def test_validate_bijection_rejects_duplicate_output_names(self):
        with self.assertRaisesRegex(ValueError, "duplicate output"):
            validate_bijection(
                ["252.jpg", "253.jpg"],
                [
                    {"source_page": "252.jpg", "output_name": "0001.jpg"},
                    {"source_page": "253.jpg", "output_name": "0001.jpg"},
                ],
            )

    def test_validate_bijection_rejects_actual_output_set_mismatch(self):
        with self.assertRaisesRegex(ValueError, "actual outputs"):
            validate_bijection(
                ["252.jpg", "253.jpg"],
                [
                    {"source_page": "252.jpg", "output_name": "252.jpg"},
                    {"source_page": "253.jpg", "output_name": "253.jpg"},
                ],
                actual_outputs=["252.jpg", "unexpected.jpg"],
            )

    def test_validate_bijection_rejects_extra_canonical_actual_output(self):
        with self.assertRaisesRegex(ValueError, "extra actual outputs"):
            validate_bijection(
                ["252.jpg", "253.jpg"],
                [
                    {"source_page": "252.jpg", "output_name": "252.jpg"},
                    {"source_page": "253.jpg", "output_name": "253.jpg"},
                ],
                actual_outputs=["252.jpg", "253.jpg", "254.jpg"],
            )

    def test_validate_bijection_rejects_missing_canonical_actual_output(self):
        with self.assertRaisesRegex(ValueError, "missing actual outputs"):
            validate_bijection(
                ["252.jpg", "253.jpg"],
                [
                    {"source_page": "252.jpg", "output_name": "252.jpg"},
                    {"source_page": "253.jpg", "output_name": "253.jpg"},
                ],
                actual_outputs=["252.jpg"],
            )

    def test_validate_bijection_rejects_non_string_actual_outputs_as_value_error(self):
        with self.assertRaisesRegex(ValueError, "actual outputs"):
            validate_bijection(
                ["252.jpg", "253.jpg"],
                [
                    {"source_page": "252.jpg", "output_name": "252.jpg"},
                    {"source_page": "253.jpg", "output_name": "253.jpg"},
                ],
                actual_outputs=["252.jpg", 2],
            )

    def test_contract_state_sets_match_the_canonical_values_exactly(self):
        self.assertEqual(
            PAGE_CLASSES,
            {"unchanged", "text_only", "full_page_redraw", "evidence_blocked"},
        )
        self.assertEqual(
            TASK_STATES,
            {"queued", "leased", "completed", "failed"},
        )
        self.assertEqual(
            ALIGNMENT_STATES,
            {"unresolved", "provisional", "confirmed"},
        )
        self.assertEqual(
            REVIEW_STATES,
            {"pending", "machine_passed", "independent_passed", "rejected"},
        )


if __name__ == "__main__":
    unittest.main()
