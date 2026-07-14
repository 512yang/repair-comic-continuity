import copy
import math
import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from entity_timeline import build_timeline, validate_timeline  # noqa: E402


def observation(entity_type, entity_id, page, state, **extra):
    row = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "page": page,
        "state": state,
    }
    row.update(extra)
    return row


def transition(entity_type, entity_id, from_page, to_page, fields, **extra):
    row = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "from_page": from_page,
        "to_page": to_page,
        "kind": "story_change",
        "source_ref": "novel:120-145",
        "changed_fields": fields,
    }
    row.update(extra)
    return row


class EntityTimelineTests(unittest.TestCase):
    def test_unsupported_long_range_beard_change_is_rejected(self):
        rows = [
            observation(
                "character",
                "天朗真人",
                "章节十/0189（1）.webp",
                {"beard": "long-black"},
                story_order=189,
            ),
            observation(
                "character",
                "天朗真人",
                "章节十五/0273.png",
                {"beard": "none"},
                story_order=273,
            ),
        ]

        timeline = build_timeline(rows, [])
        with self.assertRaisesRegex(
            ValueError,
            r"天朗真人.*章节十/0189（1）\.webp.*章节十五/0273\.png.*facial_hair",
        ):
            validate_timeline(timeline)

    def test_supported_prop_transfer_and_material_change_is_valid(self):
        rows = [
            observation(
                "prop",
                "奎金狼毫笔",
                "章节十二/0240.jpg",
                {"owner": "邓正虎", "material": "wolf-hair"},
                story_order=240,
            ),
            observation(
                "prop",
                "奎金狼毫笔",
                "章节十三/0261.PNG",
                {"owner": "简正风", "material": "gold"},
                story_order=261,
            ),
        ]
        changes = [
            {
                "entity_type": "prop",
                "entity_id": "奎金狼毫笔",
                "from_page": "章节十二/0240.jpg",
                "to_page": "章节十三/0261.PNG",
                "kind": "transfer_and_transformation",
                "source_ref": "novel:120-145",
            }
        ]

        timeline = build_timeline(rows, changes)

        self.assertEqual("1.0", timeline["schema_version"])
        self.assertTrue(validate_timeline(timeline))
        self.assertNotIn("changed_fields", timeline["transitions"][0])

    def test_state_is_canonical_and_noncritical_shot_details_are_excluded(self):
        rows = [
            observation(
                "character",
                "邓正虎",
                "240.jpg",
                {
                    "hair": {"color": "black", "style": "tied"},
                    "costume": "blue robe",
                    "pose": "two hands holding pen",
                    "grip": "two-handed",
                    "expression": "calm",
                    "camera_angle": "wide",
                    "action": "writing",
                },
            ),
            observation(
                "character",
                "邓正虎",
                "241.jpg",
                {
                    "hair": {"style": "tied", "color": "black"},
                    "costume": "blue robe",
                    "pose": "one hand holding pen",
                    "grip": "one-handed",
                    "expression": "focused",
                    "camera_angle": "close-up",
                    "action": "raising pen",
                },
            ),
        ]

        timeline = build_timeline(rows, [])
        states = timeline["entities"][0]["observations"]

        self.assertEqual({"costume": "blue robe", "hair": {"color": "black", "style": "tied"}}, states[0]["state"])
        self.assertEqual(states[0]["state_hash"], states[1]["state_hash"])

    def test_scene_and_character_critical_fields_require_exact_transitions(self):
        rows = [
            observation("scene", "山门", "010.jpg", {"location": "east", "axis": "north"}),
            observation("scene", "山门", "011.jpg", {"location": "west", "axis": "south"}),
            observation("character", "守卫", "010.jpg", {"skin": "tan", "fixed_props": ["spear"]}),
            observation("character", "守卫", "011.jpg", {"skin": "pale", "fixed_props": ["spear"]}),
        ]
        wrong_entity = [transition("character", "守卫", "010.jpg", "011.jpg", ["skin"])]

        with self.assertRaisesRegex(ValueError, r"山门.*axis.*location"):
            validate_timeline(build_timeline(rows, wrong_entity))

    def test_partial_state_carries_prior_explicit_value_and_cannot_hide_conflict(self):
        rows = [
            observation("character", "甲", "001.jpg", {"hair": "black", "costume": "robe"}),
            observation("character", "甲", "050.jpg", {"costume": "robe", "hair": "unknown"}),
            observation("character", "甲", "099.jpg", {"hair": "white"}),
        ]

        with self.assertRaisesRegex(ValueError, r"050\.jpg.*099\.jpg.*hair"):
            validate_timeline(build_timeline(rows, []))

    def test_same_state_and_missing_fields_do_not_need_transition(self):
        rows = [
            observation("prop", "剑", "001.jpg", {"owner": "甲", "condition": "whole"}),
            observation("prop", "剑", "050.jpg", {"condition": "whole"}),
            observation("prop", "剑", "099.jpg", {"owner": "甲"}),
        ]
        self.assertTrue(validate_timeline(build_timeline(rows, [])))

    def test_explicit_story_order_controls_long_range_comparison(self):
        rows = [
            observation("prop", "印", "100.jpg", {"owner": "乙"}, story_order=2),
            observation("prop", "印", "020.jpg", {"owner": "甲"}, story_order=1),
        ]
        changes = [transition("prop", "印", "020.jpg", "100.jpg", ["owner"])]

        timeline = build_timeline(rows, changes)

        pages = [item["page"] for item in timeline["entities"][0]["observations"]]
        self.assertEqual(["020.jpg", "100.jpg"], pages)

    def test_input_order_does_not_change_content_hash_when_story_order_is_explicit(self):
        rows = [
            observation("character", "甲", "章节/002.png", {"hair": "black"}, story_order=2),
            observation("character", "甲", "章节/001.png", {"hair": "black"}, story_order=1),
        ]
        before = copy.deepcopy(rows)

        first = build_timeline(rows, [])
        second = build_timeline(list(reversed(rows)), [])

        self.assertEqual(first, second)
        self.assertEqual(before, rows)
        self.assertEqual(64, len(first["content_hash"]))

    def test_unicode_nested_paths_and_nfkc_identity_are_supported(self):
        rows = [
            observation("character", "  天朗真人  ", "章节一/252（1）.jpg", {"hair": "黑"})
        ]
        entity = build_timeline(rows, [])["entities"][0]
        self.assertEqual("天朗真人", entity["entity_id"])
        self.assertEqual("章节一/252（1）.jpg", entity["observations"][0]["page"])

    def test_duplicate_observation_and_nfkc_case_page_collision_are_rejected(self):
        duplicate = [
            observation("character", "甲", "A/001.JPG", {"hair": "black"}),
            observation("character", "甲", "A/001.JPG", {"hair": "black"}),
        ]
        with self.assertRaisesRegex(ValueError, "duplicate observation"):
            build_timeline(duplicate, [])

        collision = [
            observation("character", "甲", "Ａ/001.JPG", {"hair": "black"}),
            observation("character", "乙", "a/001.jpg", {"hair": "black"}),
        ]
        with self.assertRaisesRegex(ValueError, "page identity collision"):
            build_timeline(collision, [])

    def test_invalid_observation_fields_are_rejected(self):
        cases = [
            (observation("costume", "甲", "001.jpg", {}), "entity_type"),
            (observation("character", "   ", "001.jpg", {}), "entity_id"),
            (observation("character", "甲", "../001.jpg", {}), "relative image path"),
            (observation("character", "甲", "001.gif", {}), "extension"),
            (observation("character", "甲", "001.jpg", []), "state"),
            (observation("character", "甲", "001.jpg", {"hair": {1: "black"}}), "state"),
            (observation("character", "甲", "001.jpg", {"hair": math.nan}), "non-finite"),
            (observation("character", "甲", "001.jpg", {}, confidence=math.inf), "confidence"),
            (observation("character", "甲", "001.jpg", {}, confidence=True), "confidence"),
            (observation("character", "甲", "001.jpg", {}, confidence=1.1), "confidence"),
        ]
        for row, message in cases:
            with self.subTest(row=row, message=message):
                with self.assertRaisesRegex(ValueError, message):
                    build_timeline([row], [])

    def test_transition_validation_is_strict(self):
        rows = [
            observation("prop", "剑", "001.jpg", {"owner": "甲"}),
            observation("prop", "剑", "002.jpg", {"owner": "乙"}),
        ]
        good = transition("prop", "剑", "001.jpg", "002.jpg", ["owner"])
        cases = [
            ([good, copy.deepcopy(good)], "duplicate transition"),
            ([{**good, "entity_id": "刀"}], "unknown entity"),
            ([{**good, "from_page": "002.jpg", "to_page": "001.jpg"}], "order"),
            ([{**good, "from_page": "000.jpg"}], "endpoint"),
            ([{**good, "source_ref": "  "}], "source_ref"),
            ([{**good, "kind": {"bad": object()}}], "kind"),
            ([{**good, "confidence": math.nan}], "confidence"),
        ]
        for changes, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    build_timeline(rows, changes)

        with self.assertRaisesRegex(ValueError, "changed_fields"):
            validate_timeline(
                build_timeline(rows, [{**good, "changed_fields": ["material"]}])
            )

    def test_transition_cannot_cover_nonadjacent_interval_or_another_entity(self):
        rows = [
            observation("prop", "剑", "001.jpg", {"owner": "甲"}),
            observation("prop", "剑", "002.jpg", {"owner": "甲"}),
            observation("prop", "剑", "003.jpg", {"owner": "乙"}),
        ]
        broad = [transition("prop", "剑", "001.jpg", "003.jpg", ["owner"])]
        with self.assertRaisesRegex(ValueError, "adjacent"):
            build_timeline(rows, broad)

    def test_transition_values_must_be_hash_safe(self):
        rows = [observation("prop", "剑", "001.jpg", {"owner": "甲"})]
        bad = {
            "entity_type": "prop",
            "entity_id": "剑",
            "from_page": "001.jpg",
            "to_page": "001.jpg",
            "kind": "noop",
            "source_ref": "novel:1",
            "changed_fields": [],
            "metadata": {1: "bad"},
        }
        with self.assertRaisesRegex(ValueError, "transition"):
            build_timeline(rows, [bad])

    def test_tampered_hashes_fail_validation(self):
        timeline = build_timeline(
            [observation("character", "甲", "001.jpg", {"hair": "black"})], []
        )
        tampered_state = copy.deepcopy(timeline)
        tampered_state["entities"][0]["observations"][0]["state_hash"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "state_hash"):
            validate_timeline(tampered_state)

        tampered_content = copy.deepcopy(timeline)
        tampered_content["content_hash"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "content_hash"):
            validate_timeline(tampered_content)


if __name__ == "__main__":
    unittest.main()
