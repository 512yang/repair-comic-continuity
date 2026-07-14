import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pipeline_contracts import (  # noqa: E402
    ALIGNMENT_STATES,
    PAGE_CLASSES,
    REVIEW_STATES,
    TASK_STATES,
    canonical_hash,
    make_output_names,
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

    def test_make_output_names_returns_contiguous_four_digit_jpg_names(self):
        self.assertEqual(
            make_output_names(3),
            ["0001.jpg", "0002.jpg", "0003.jpg"],
        )

    def test_make_output_names_requires_a_positive_integer(self):
        for invalid_count in (0, -1, 1.5, True):
            with self.subTest(invalid_count=invalid_count):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    make_output_names(invalid_count)

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
            {"source_page": "252.jpg", "output_name": "0001.jpg"},
            {"source_page": "252（1）.jpg", "output_name": "0002.jpg"},
        ]

        self.assertIsNone(
            validate_bijection(
                ["252.jpg", "252（1）.jpg"],
                mappings,
                actual_outputs=["0002.jpg", "0001.jpg"],
            )
        )

    def test_validate_bijection_rejects_duplicate_normalized_inputs(self):
        with self.assertRaisesRegex(ValueError, "duplicate input"):
            validate_bijection(
                ["252（1）.jpg", "252(1).png"],
                [
                    {"source_page": "252（1）.jpg", "output_name": "0001.jpg"},
                    {"source_page": "252(1).png", "output_name": "0002.jpg"},
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
        with self.assertRaisesRegex(ValueError, "output names"):
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
                    {"source_page": "252.jpg", "output_name": "0001.jpg"},
                    {"source_page": "253.jpg", "output_name": "0002.jpg"},
                ],
                actual_outputs=["0001.jpg", "unexpected.jpg"],
            )

    def test_validate_bijection_rejects_extra_canonical_actual_output(self):
        with self.assertRaisesRegex(ValueError, "extra actual outputs"):
            validate_bijection(
                ["252.jpg", "253.jpg"],
                [
                    {"source_page": "252.jpg", "output_name": "0001.jpg"},
                    {"source_page": "253.jpg", "output_name": "0002.jpg"},
                ],
                actual_outputs=["0001.jpg", "0002.jpg", "0003.jpg"],
            )

    def test_validate_bijection_rejects_missing_canonical_actual_output(self):
        with self.assertRaisesRegex(ValueError, "missing actual outputs"):
            validate_bijection(
                ["252.jpg", "253.jpg"],
                [
                    {"source_page": "252.jpg", "output_name": "0001.jpg"},
                    {"source_page": "253.jpg", "output_name": "0002.jpg"},
                ],
                actual_outputs=["0001.jpg"],
            )

    def test_validate_bijection_rejects_non_string_actual_outputs_as_value_error(self):
        with self.assertRaisesRegex(ValueError, "actual outputs"):
            validate_bijection(
                ["252.jpg", "253.jpg"],
                [
                    {"source_page": "252.jpg", "output_name": "0001.jpg"},
                    {"source_page": "253.jpg", "output_name": "0002.jpg"},
                ],
                actual_outputs=["0001.jpg", 2],
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
