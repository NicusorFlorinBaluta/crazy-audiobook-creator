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
    alias_veto,
    apply_alias,
    apply_merge,
    choose_primary,
    conjunction_count,
    find_unlinked_speakers,
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

        # Build it the way the pipeline does. This used to be
        # `object.__new__(CharacterAnalyzer)` with the attributes hand-set,
        # which meant the fixture invented the object's shape: it supplied a
        # `config` attribute that `__init__` never created, so every test here
        # passed while the real analyzer raised AttributeError on the first
        # book it saw. Going through the constructor is what makes these tests
        # able to fail.
        analyzer = CharacterAnalyzer(
            ollama=None,
            external_validator=_Validator(),
            config={"external_validation": {"cast_adjudication": {"require_approval": require_approval}}},
        )

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


class UnlinkedSpeakerScanTests(unittest.TestCase):
    """Names that speak in the text but answer to no registry entry.

    The measured case: "Zak" appears 38 times in the real book and speaks
    repeatedly ("Zak said", "Zak explained", "Zak admitted"), while the
    registry holds Zaknafein with aliases ["the weapons master", "Zaknafein"].
    Nothing links them, so every one of those attributions is unresolvable.
    """

    def test_a_speaking_name_absent_from_the_registry_is_found(self) -> None:
        text = (
            '"So, you decided to join us," Zak said. He stood and stretched. '
            '"I have no desire to see him," Zak admitted quietly afterwards.'
        )
        cast = {"zaknafein": {"name": "Zaknafein", "aliases": ["the weapons master"]}}
        found = find_unlinked_speakers(text, cast)
        self.assertIn("Zak", found)
        self.assertEqual(found["Zak"], 2)

    def test_a_known_name_is_not_reported(self) -> None:
        text = '"Enough," Zaknafein said. "Enough," the weapons master said again.'
        cast = {"zaknafein": {"name": "Zaknafein", "aliases": ["the weapons master"]}}
        self.assertEqual(find_unlinked_speakers(text, cast), {})

    def test_pronouns_and_sentence_openers_are_not_speakers(self) -> None:
        """Without this the scan reports "She said" and "You asked" as names."""
        text = "She said nothing. You asked twice. They replied. She said it again."
        self.assertEqual(find_unlinked_speakers(text, {}), {})

    def test_the_frequency_floor_bounds_the_noise(self) -> None:
        """A floor of 1 yields mostly place names on real text; 2 is the default."""
        text = '"Yes," Kryptgarden said once. "No," Zak said. "Maybe," Zak added.'
        self.assertEqual(set(find_unlinked_speakers(text, {}, min_attributions=2)), {"Zak"})
        self.assertIn("Kryptgarden", find_unlinked_speakers(text, {}, min_attributions=1))


class AliasVetoTests(unittest.TestCase):
    """Adding an alias cannot lose a character, but it does redirect names."""

    TEXT = '"So," Zak said, and Zaknafein turned away. Drizzt watched them both.'

    def _cast(self):
        return {
            "zaknafein": {"name": "Zaknafein", "aliases": ["the weapons master"]},
            "drizzt": {"name": "Drizzt", "aliases": []},
            "narrator": {"name": "Narrator", "aliases": []},
        }

    def test_a_grounded_alias_is_allowed(self) -> None:
        self.assertIsNone(alias_veto("Zak", "zaknafein", self._cast(), self.TEXT))

    def test_an_invented_alias_is_refused(self) -> None:
        """A name the book never uses would redirect attributions that cannot occur."""
        veto = alias_veto("Zaknafeen", "zaknafein", self._cast(), self.TEXT)
        self.assertIn("does not appear in the source text", veto)

    def test_an_alias_owned_by_another_character_is_refused(self) -> None:
        """Two characters answering to one name leaves attribution unable to choose."""
        veto = alias_veto("Drizzt", "zaknafein", self._cast(), self.TEXT)
        self.assertIn("already belongs to", veto)

    def test_the_narrator_takes_no_aliases(self) -> None:
        self.assertIsNotNone(alias_veto("Zak", "narrator", self._cast(), self.TEXT))

    def test_an_unknown_target_is_refused(self) -> None:
        self.assertIsNotNone(alias_veto("Zak", "nobody", self._cast(), self.TEXT))

    def test_a_pronoun_is_refused(self) -> None:
        self.assertIsNotNone(alias_veto("She", "zaknafein", self._cast(), "She said so."))

    def test_applying_an_alias_is_additive(self) -> None:
        cast = self._cast()
        apply_alias("Zak", "zaknafein", cast)
        self.assertEqual(cast["zaknafein"]["aliases"], ["the weapons master", "Zak"])
        # Idempotent: re-applying does not duplicate.
        apply_alias("Zak", "zaknafein", cast)
        self.assertEqual(cast["zaknafein"]["aliases"].count("Zak"), 1)


class AdjudicationIsNotLoadBearingTests(unittest.TestCase):
    """The e2e failure of 2026-09-04, and the guard that keeps it non-fatal.

    A real run died five minutes in with `'CharacterAnalyzer' object has no
    attribute 'config'`. Two things were wrong and both are covered here.
    """

    def test_a_pipeline_built_analyzer_has_what_adjudication_reads(self) -> None:
        """The attribute the adjudication path reads must come from __init__.

        `self.config` was read for the approval-gate default and was never
        assigned anywhere. Every existing test passed because the fixture
        built the analyzer with `object.__new__` and set `config` by hand --
        the test supplied the very thing production was missing. Constructing
        it normally, with nothing passed, is the check that has teeth.
        """
        from brain.director.character_analyzer import CharacterAnalyzer

        analyzer = CharacterAnalyzer(ollama=None)
        self.assertEqual(analyzer.config, {}, "config must default, not be absent")
        # The exact expression that raised in production.
        require_approval = bool(
            (analyzer.config.get("external_validation", {}).get("cast_adjudication", {}) or {}).get(
                "require_approval", False
            )
        )
        self.assertFalse(require_approval)

    def test_the_pipeline_passes_its_config_to_the_analyzer(self) -> None:
        """A default of {} is only correct if the real caller supplies one."""
        import ast
        import inspect

        from brain.director.character_analyzer import CharacterAnalyzer
        from brain.orchestrator import pipeline as pipeline_module

        self.assertIn("config", inspect.signature(CharacterAnalyzer.__init__).parameters)

        tree = ast.parse(Path(pipeline_module.__file__).read_text(encoding="utf-8"))
        construction = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "CharacterAnalyzer"
        )
        passed = {kw.arg for kw in construction.keywords}
        self.assertIn("config", passed, f"pipeline builds the analyzer without config: {passed}")

    def test_a_failure_in_adjudication_does_not_fail_the_run(self) -> None:
        """Duplicate detection is advisory and must never end a book.

        The inner try covers only the Gemini call, so everything around it --
        roster construction, config, alias recovery -- reached the pipeline as
        a hard failure. A roster with a duplicate left in it is a much better
        outcome than a dead run.

        This reads the call site rather than driving a full `analyze()`, which
        would need a live LLM. It proves the guard is present and catches
        broadly; it cannot prove the body behaves, which is what the
        constructor test above is for.
        """
        import ast

        from brain.director import character_analyzer as module

        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        analyze = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "analyze")
        guarded = [
            handler
            for node in ast.walk(analyze)
            if isinstance(node, ast.Try)
            for call in ast.walk(node)
            if isinstance(call, ast.Attribute) and call.attr == "_adjudicate_cast_identity"
            for handler in node.handlers
        ]
        self.assertTrue(guarded, "_adjudicate_cast_identity is called outside a try")
        self.assertTrue(
            any(handler.type is None or getattr(handler.type, "id", None) == "Exception" for handler in guarded),
            "the guard must catch broadly -- any failure here is survivable",
        )


if __name__ == "__main__":
    unittest.main()
