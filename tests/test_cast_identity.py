"""Whole-cast duplicate detection: the local guards, and how merges apply.

The gap this closes was measured, not assumed. On the real 57-character cast of
`the-finest-edge-of-twilight-book`, `_adjudicate_name_candidates` can only ever
*consider* 24 of 1,540 pairs (1.6%), because it proposes a pair only from a
shared distinctive token or an id-suffix match. A character recorded once as a
proper name and again as an appellative shares nothing lexical.

The tests that matter most here are the refusals. A wrong merge collapses two
characters into one voice for an entire book, and the operator has opted out of
reviewing proposals to avoid spoilers, so the local vetoes are the last line.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from brain.director.cast_identity import (
    apply_merge,
    choose_primary,
    conjunction_count,
    merge_veto,
)

# Excerpt shapes taken from the real book, which is where the discriminator was
# validated. Aliases appear in apposition; distinct people appear conjoined.
APPOSITION = (
    "Jarlaxle swept off his hat. Brie had called him Uncle Jax since she "
    "could speak, and he had never once corrected her."
)
CONJUNCTION = (
    "Ilnezhara and Tazmikella stood together on the balcony, sisters in every "
    "way that mattered, and neither of them looked away."
)


def _cast(**overrides):
    base = {
        "jarlaxle": {"name": "Jarlaxle", "aliases": ["Uncle Jax"], "gender": "male", "dialogue_count": 376},
        "uncle_jax": {"name": "Uncle Jax", "aliases": [], "gender": "male", "dialogue_count": 12},
        "ilnezhara": {
            "name": "Ilnezhara",
            "aliases": ["copper dragon", "sister"],
            "gender": "female",
            "dialogue_count": 15,
        },
        "tazmikella": {
            "name": "Tazmikella",
            "aliases": ["copper dragon", "sister"],
            "gender": "female",
            "dialogue_count": 13,
        },
        "narrator": {"name": "Narrator", "aliases": [], "gender": "female", "dialogue_count": 0},
    }
    base.update(overrides)
    return base


class ConjunctionVetoTests(unittest.TestCase):
    """The discriminator, and why proximity was rejected in favour of it.

    Measured on the real book, counting occurrences within 200 characters:
    Ilnezhara/Tazmikella (distinct) 6, Jarlaxle/Uncle Jax (same) 10,
    Regis/Rumblebelly (same) 15. Aliases co-occur *more* than distinct
    characters, because prose introduces an alias beside the name it replaces.
    Conjunction separated all eight probe pairs cleanly.
    """

    def test_conjoined_names_are_counted(self) -> None:
        self.assertGreaterEqual(conjunction_count(CONJUNCTION, ["Ilnezhara"], ["Tazmikella"]), 1)

    def test_apposition_is_not_a_conjunction(self) -> None:
        self.assertEqual(conjunction_count(APPOSITION, ["Jarlaxle"], ["Uncle Jax"]), 0)

    def test_conjunction_is_symmetric(self) -> None:
        left = conjunction_count(CONJUNCTION, ["Ilnezhara"], ["Tazmikella"])
        right = conjunction_count(CONJUNCTION, ["Tazmikella"], ["Ilnezhara"])
        self.assertEqual(left, right)

    def test_generic_terms_cannot_carry_a_veto(self) -> None:
        """ "the dwarf and the elf" says nothing about two registry entries."""
        text = "The dwarf and the elf argued all the way down the mountain."
        cast = _cast(
            a={"name": "Athrogate", "aliases": ["the dwarf"], "gender": "male", "dialogue_count": 7},
            b={"name": "Allefaero", "aliases": ["the elf"], "gender": "male", "dialogue_count": 214},
        )
        # Only the generic aliases are conjoined, so no veto fires.
        self.assertIsNone(merge_veto("b", "a", cast, text))

    def test_a_serial_list_is_a_conjunction(self) -> None:
        """The real form in this book is "Bruenor, Drizzt, and Catti-brie"."""
        text = "the rain fell by the time Bruenor, Drizzt, and Catti-brie got out"
        self.assertGreaterEqual(conjunction_count(text, ["Bruenor"], ["Drizzt"]), 1)

    def test_apposition_is_still_not_a_list(self) -> None:
        """A bare comma must not veto: this is one person, named twice.

        Vetoing here would refuse exactly the merges the feature exists to
        find, so the list rule requires the list to actually continue.
        """
        text = "Jarlaxle, Uncle Jax to the girl, swept off his hat."
        self.assertEqual(conjunction_count(text, ["Jarlaxle"], ["Uncle Jax"]), 0)


class PositionalSiblingTests(unittest.TestCase):
    """Ids the extractor numbered because it could not name them.

    `dwarf_blacksmith_1` and `_2` are two anonymous people, never one person
    twice -- and every term they own is generic, so the conjunction veto is
    blind to them.
    """

    def test_numbered_siblings_are_refused(self) -> None:
        cast = {
            "dwarf_blacksmith_1": {"name": "Dwarf Blacksmith 1", "aliases": [], "dialogue_count": 5},
            "dwarf_blacksmith_2": {"name": "Dwarf Blacksmith 2", "aliases": [], "dialogue_count": 2},
        }
        veto = merge_veto("dwarf_blacksmith_1", "dwarf_blacksmith_2", cast, "")
        self.assertIn("positional or numeric marker", veto)

    def test_positional_siblings_are_refused(self) -> None:
        cast = {
            "driver_left": {"name": "Driver Left", "aliases": [], "dialogue_count": 2},
            "driver_right": {"name": "Driver Right", "aliases": [], "dialogue_count": 1},
        }
        self.assertIsNotNone(merge_veto("driver_left", "driver_right", cast, ""))

    def test_an_unrelated_numbered_id_is_not_a_sibling(self) -> None:
        """Different stems are different characters, not a numbered pair."""
        cast = {
            "guard_1": {"name": "Guard 1", "aliases": [], "dialogue_count": 1},
            "sailor_2": {"name": "Sailor 2", "aliases": [], "dialogue_count": 1},
        }
        self.assertIsNone(merge_veto("guard_1", "sailor_2", cast, ""))


class MergeVetoTests(unittest.TestCase):
    def test_the_twins_are_never_merged(self) -> None:
        """The case that motivated the veto: siblings sharing generic aliases."""
        veto = merge_veto("ilnezhara", "tazmikella", _cast(), CONJUNCTION)
        self.assertIsNotNone(veto)
        self.assertIn("separate participants", veto)

    def test_a_genuine_alias_pair_is_allowed(self) -> None:
        self.assertIsNone(merge_veto("jarlaxle", "uncle_jax", _cast(), APPOSITION))

    def test_the_narrator_is_never_merged(self) -> None:
        for pair in (("narrator", "jarlaxle"), ("jarlaxle", "narrator")):
            self.assertIn("narrator", merge_veto(*pair, _cast(), APPOSITION))

    def test_disagreeing_explicit_genders_are_refused(self) -> None:
        cast = _cast(
            she={"name": "Donnola", "aliases": [], "gender": "female", "dialogue_count": 2},
            he={"name": "Regis", "aliases": [], "gender": "male", "dialogue_count": 127},
        )
        veto = merge_veto("he", "she", cast, "Regis smiled at Donnola across the room.")
        self.assertIn("genders disagree", veto)

    def test_other_gender_does_not_block_a_merge(self) -> None:
        """`other` means unresolved, not a third gender; it must not veto."""
        cast = _cast(
            a={"name": "Kimmuriel", "aliases": [], "gender": "other", "dialogue_count": 1},
            b={"name": "The Hive Mind", "aliases": [], "gender": "male", "dialogue_count": 0},
        )
        self.assertIsNone(merge_veto("a", "b", cast, "Kimmuriel spoke for the hive mind."))

    def test_self_merge_and_unknown_ids_are_refused(self) -> None:
        self.assertIsNotNone(merge_veto("jarlaxle", "jarlaxle", _cast(), ""))
        self.assertIsNotNone(merge_veto("jarlaxle", "nobody", _cast(), ""))


class ChoosePrimaryTests(unittest.TestCase):
    def test_the_louder_character_survives(self) -> None:
        primary, duplicate = choose_primary("uncle_jax", "jarlaxle", _cast())
        self.assertEqual((primary, duplicate), ("jarlaxle", "uncle_jax"))

    def test_ties_prefer_the_fuller_name_and_are_deterministic(self) -> None:
        cast = {
            "pwent": {"name": "Pwent", "aliases": [], "dialogue_count": 7},
            "thibbledorf_pwent": {"name": "Thibbledorf Pwent", "aliases": [], "dialogue_count": 7},
        }
        self.assertEqual(choose_primary("pwent", "thibbledorf_pwent", cast)[0], "thibbledorf_pwent")
        self.assertEqual(choose_primary("thibbledorf_pwent", "pwent", cast)[0], "thibbledorf_pwent")


class ApplyMergeTests(unittest.TestCase):
    def test_a_merge_loses_nothing(self) -> None:
        cast = _cast()
        record = apply_merge("jarlaxle", "uncle_jax", cast)

        self.assertNotIn("uncle_jax", cast)
        survivor = cast["jarlaxle"]
        # Dialogue is combined, so importance and casting see one character with
        # the full weight rather than two halves.
        self.assertEqual(survivor["dialogue_count"], 376 + 12)
        # Every name the duplicate answered to becomes an alias, so attribution
        # can still resolve lines written under the old id.
        self.assertIn("Uncle Jax", survivor["aliases"])
        self.assertIn("uncle jax", survivor["aliases"])
        self.assertEqual(record["merged_id"], "uncle_jax")
        self.assertEqual(record["combined_dialogue_count"], 388)

    def test_the_survivor_keeps_the_richer_description(self) -> None:
        cast = {
            "a": {"name": "A", "aliases": [], "dialogue_count": 5, "voice_description": "A voice."},
            "b": {
                "name": "B",
                "aliases": [],
                "dialogue_count": 1,
                "voice_description": "A gravelled baritone worn thin by decades of shouting.",
            },
        }
        apply_merge("a", "b", cast)
        self.assertIn("gravelled baritone", cast["a"]["voice_description"])

    def test_the_survivors_own_name_is_not_added_as_an_alias(self) -> None:
        cast = {
            "a": {"name": "Regis", "aliases": [], "dialogue_count": 9},
            "b": {"name": "Regis", "aliases": [], "dialogue_count": 1},
        }
        apply_merge("a", "b", cast)
        self.assertNotIn("Regis", cast["a"]["aliases"])


class CastAdjudicationWiringTests(unittest.TestCase):
    """The analyzer must treat the model as a proposer, never an authority."""

    def _analyzer(self, merges, *, require_approval=False):
        import tempfile

        from brain.director.character_analyzer import CharacterAnalyzer
        from shared.constants import Gender
        from shared.models import (
            BookMetadata,
            Character,
            CharacterRegistry,
            ExtractedBook,
            ExtractedChapter,
        )

        class _Validator:
            cast_adjudication_enabled = True

            def __init__(self):
                self.roster_seen = None

            def adjudicate_cast(self, *, project_dir, roster, evidence_for):
                self.roster_seen = roster
                return {"merges": merges, "review": [], "trace": []}

        analyzer = object.__new__(CharacterAnalyzer)
        analyzer.external_validator = _Validator()
        analyzer.config = {"external_validation": {"cast_adjudication": {"require_approval": require_approval}}}

        def char(cid, name, aliases, gender, count):
            return Character(
                id=cid,
                name=name,
                gender=gender,
                age_range="40s",
                aliases=aliases,
                voice_description=f"{name}'s voice.",
                dialogue_count=count,
            )

        registry = CharacterRegistry(
            characters={
                "jarlaxle": char("jarlaxle", "Jarlaxle", ["Uncle Jax"], Gender.MALE, 376),
                "uncle_jax": char("uncle_jax", "Uncle Jax", [], Gender.MALE, 12),
                "ilnezhara": char("ilnezhara", "Ilnezhara", [], Gender.FEMALE, 15),
                "tazmikella": char("tazmikella", "Tazmikella", [], Gender.FEMALE, 13),
            }
        )
        book = ExtractedBook(
            metadata=BookMetadata(title="T", author="A"),
            chapters=[
                ExtractedChapter(
                    number=1,
                    title="One",
                    text=(
                        "Jarlaxle swept off his hat; Brie had called him Uncle Jax "
                        "for years. Ilnezhara and Tazmikella watched from the balcony."
                    ),
                )
            ],
        )
        self._tmp = tempfile.TemporaryDirectory()
        return analyzer, registry, book, Path(self._tmp.name)

    def tearDown(self) -> None:
        tmp = getattr(self, "_tmp", None)
        if tmp is not None:
            tmp.cleanup()

    def test_a_grounded_merge_is_applied(self) -> None:
        merges = [
            {
                "left_id": "jarlaxle",
                "right_id": "uncle_jax",
                "confidence": 0.99,
                "reason": "same person",
                "evidence": ["called him Uncle Jax"],
                "grounded": True,
            }
        ]
        analyzer, registry, book, tmp = self._analyzer(merges)
        analyzer._adjudicate_cast_identity(registry, book, tmp)

        self.assertNotIn("uncle_jax", registry.characters)
        survivor = registry.characters["jarlaxle"]
        self.assertEqual(survivor.dialogue_count, 388)
        self.assertIn("Uncle Jax", survivor.aliases)

    def test_a_local_veto_overrides_the_model(self) -> None:
        """The whole point: Gemini proposing a merge is not authority to make it."""
        merges = [
            {
                "left_id": "ilnezhara",
                "right_id": "tazmikella",
                "confidence": 1.0,
                "reason": "surely the same dragon",
                "evidence": ["watched from the balcony"],
                "grounded": True,
            }
        ]
        analyzer, registry, book, tmp = self._analyzer(merges)
        analyzer._adjudicate_cast_identity(registry, book, tmp)

        self.assertIn("ilnezhara", registry.characters)
        self.assertIn("tazmikella", registry.characters)
        audit = json.loads((tmp / "cast_identity_audit.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["applied"], [])
        self.assertIn("separate participants", audit["refused"][0]["veto"])

    def test_the_approval_gate_holds_merges_instead_of_applying_them(self) -> None:
        merges = [
            {
                "left_id": "jarlaxle",
                "right_id": "uncle_jax",
                "confidence": 0.99,
                "reason": "same person",
                "evidence": ["called him Uncle Jax"],
                "grounded": True,
            }
        ]
        analyzer, registry, book, tmp = self._analyzer(merges, require_approval=True)
        analyzer._adjudicate_cast_identity(registry, book, tmp)

        self.assertIn("uncle_jax", registry.characters, "the gate must not apply")
        audit = json.loads((tmp / "cast_identity_audit.json").read_text(encoding="utf-8"))
        self.assertTrue(audit["require_approval"])
        self.assertIn("operator approval", audit["refused"][0]["veto"])

    def test_the_roster_carries_no_book_text(self) -> None:
        """Stage one is names only, so the cheap call leaks no prose."""
        analyzer, registry, book, tmp = self._analyzer([])
        analyzer._adjudicate_cast_identity(registry, book, tmp)

        roster = analyzer.external_validator.roster_seen
        self.assertIsNotNone(roster)
        self.assertNotIn("narrator", roster)
        for entry in roster.values():
            self.assertEqual(
                set(entry) & {"evidence_snippets", "context", "text"},
                set(),
                "the roster prompt must not carry source text",
            )

    def test_a_disabled_adjudicator_is_a_no_op(self) -> None:
        analyzer, registry, book, tmp = self._analyzer([])
        analyzer.external_validator.cast_adjudication_enabled = False
        before = set(registry.characters)
        analyzer._adjudicate_cast_identity(registry, book, tmp)
        self.assertEqual(set(registry.characters), before)
        self.assertFalse((tmp / "cast_identity_audit.json").exists())


if __name__ == "__main__":
    unittest.main()
