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

import ast
import json
import unittest
from pathlib import Path

from brain.director.cast_identity import (
    _vetoable_terms,
    alias_veto,
    apply_alias,
    apply_merge,
    choose_primary,
    conjunction_count,
    distinct_participant_veto,
    find_unlinked_speakers,
    merge_veto,
    prune_ambiguous_fragment_aliases,
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


class FragmentAliasPruneTests(unittest.TestCase):
    """`_derive_character_aliases` splits names into words; most words are not names.

    "White-Haired Being" arrives carrying `Being`, `White` and `Haired`. Those
    mislead the whole-cast roster prompt, which reads aliases and no book text,
    and a fragment two characters both claim makes the speech-tag parser abstain
    for both. The rule removes a fragment only when it is ambiguous (more than
    one owner) or never used as a name (never capitalised mid-sentence).
    """

    def test_a_character_named_only_by_a_generic_phrase_keeps_its_identity(self) -> None:
        """The case that constrains this whole rule.

        Some books never give a character a proper name -- "The Dark One", "The
        Master", "The Elder Ones" -- for most or all of the book. Stripping
        generic words would leave them unidentifiable, so the full name form is
        never a candidate for removal, whatever it is made of.
        """
        cast = {
            "the_master": {"name": "The Master", "aliases": ["The Master"]},
            "the_dark_one": {"name": "The Dark One", "aliases": ["The Dark One", "Dark", "One"]},
            "elder_ones": {"name": "The Elder Ones", "aliases": ["The Elder Ones", "Ones"]},
        }
        source = (
            "He knelt before The Master, and the Master did not speak. "
            "Only the Dark One remembered, for the Dark One had been there. "
            "The Elder Ones watched; the Ones above do not forget."
        )
        prune_ambiguous_fragment_aliases(cast, source)
        for cid, required in (
            ("the_master", "The Master"),
            ("the_dark_one", "The Dark One"),
            ("elder_ones", "The Elder Ones"),
        ):
            with self.subTest(cid=cid):
                self.assertIn(required, cast[cid]["aliases"], "the full name is never removable")
                self.assertTrue(cast[cid]["aliases"])

    def test_word_salad_fragments_are_dropped(self) -> None:
        cast = {
            "white_haired_being": {
                "name": "White-Haired Being",
                "aliases": ["White-Haired Being", "White-Haired", "Being", "White", "Haired"],
            }
        }
        # Proportions matter, not presence: each fragment is overwhelmingly an
        # ordinary lower-case word outside the name it was split from, which is
        # what it looks like in the real book (`Being` scores 2 capitalised
        # against 95 any-case there).
        source = (
            "The White-Haired Being spoke once. A white-haired figure stood there. "
            "It was a strange being, that being, and being early was his habit; "
            "being late was not, for being seen mattered to a being like him. "
            "The white snow fell on white stone, and white banners hung over white walls. "
            "His haired scalp itched; the haired beast circled the haired thing."
        )
        removed = prune_ambiguous_fragment_aliases(cast, source)
        self.assertEqual({r["alias"] for r in removed}, {"Being", "White", "Haired"})
        self.assertEqual(cast["white_haired_being"]["aliases"], ["White-Haired Being", "White-Haired"])

    def test_a_fragment_two_characters_claim_is_dropped_from_both(self) -> None:
        """`Brie` belongs to Catti-brie and to Breezy, so it names neither.

        The parser already abstains on a name with two owners, so removing it
        costs no attribution -- but it stops the roster stage seeing a shared
        alias and proposing a merge, which is what happened live with `master`.
        """
        cast = {
            "catti_brie": {"name": "Catti-brie", "aliases": ["Catti-brie", "Catti", "Brie"]},
            "breezy": {"name": "Breezy Do'Urden", "aliases": ["Breezy Do'Urden", "Breezy", "Brie"]},
        }
        source = "Catti-brie spoke to Breezy. Then Catti nodded, and Brie laughed at Brie."
        prune_ambiguous_fragment_aliases(cast, source)
        self.assertNotIn("Brie", cast["catti_brie"]["aliases"])
        self.assertNotIn("Brie", cast["breezy"]["aliases"])
        self.assertIn("Catti", cast["catti_brie"]["aliases"], "an unambiguous fragment survives")

    def test_a_surname_mentioned_once_is_still_a_surname(self) -> None:
        """Frequency is not a criterion; `Applecheeks` occurs once in a real book."""
        cast = {"numtummy": {"name": "Numtummy Applecheeks", "aliases": ["Numtummy Applecheeks", "Applecheeks"]}}
        source = "The halfling called Applecheeks grinned at him from the doorway."
        prune_ambiguous_fragment_aliases(cast, source)
        self.assertIn("Applecheeks", cast["numtummy"]["aliases"])

    def test_an_entry_is_never_stripped_to_nothing(self) -> None:
        """`child_girl` owns only `Child`, which `child_boy` also claims."""
        cast = {
            "child_girl": {"name": "Girl", "aliases": ["Child"]},
            "child_boy": {"name": "Boy", "aliases": ["Child"]},
        }
        prune_ambiguous_fragment_aliases(cast, "The Child ran. Another Child followed.")
        self.assertTrue(cast["child_girl"]["aliases"], "a character must remain addressable")

    def test_an_unambiguous_model_supplied_alias_is_left_alone(self) -> None:
        """A model-supplied name only one character claims is not a candidate."""
        cast = {"insect_god": {"name": "Insect God", "aliases": ["Insect God", "The Thing", "Insect"]}}
        prune_ambiguous_fragment_aliases(cast, "It spoke without a mouth.")
        self.assertIn("The Thing", cast["insect_god"]["aliases"])
        self.assertNotIn("Insect", cast["insect_god"]["aliases"])

    def test_an_ambiguous_alias_goes_even_when_nobody_derived_it(self) -> None:
        """The alias that actually caused a bogus merge proposal, live.

        On 2026-09-10 the roster stage proposed merging `hoid` with
        `white_haired_being`. Nothing in the text links them; both simply
        carried the alias `master`, which neither name nor id contains, so a
        fragment-only rule would leave it in place. Two claimants means it
        identifies neither, and the roster prompt reads it as evidence.
        """
        cast = {
            "hoid": {"name": "Hoid", "aliases": ["Hoid", "Master"]},
            "white_haired_being": {"name": "White-Haired Being", "aliases": ["White-Haired Being", "master"]},
        }
        prune_ambiguous_fragment_aliases(cast, "The Master walked on. Hoid smiled at that.")
        self.assertEqual(cast["hoid"]["aliases"], ["Hoid"])
        self.assertEqual(cast["white_haired_being"]["aliases"], ["White-Haired Being"])

    def test_an_alias_that_is_another_character_s_real_name_is_protected(self) -> None:
        """`Effron` is a person; `effron_child` merely mis-claims it.

        Deleting the name would punish its owner for the mis-claim. Attribution
        already handles the collision (2026-09-06: an alias may not be another
        character's canonical name), so this pass leaves names alone.
        """
        cast = {
            "effron": {"name": "Effron", "aliases": ["Effron"]},
            "effron_child": {"name": "Effron's Son", "aliases": ["Effron's Son", "Effron", "Son"]},
        }
        prune_ambiguous_fragment_aliases(cast, "Effron turned away. Effron had said enough.")
        self.assertIn("Effron", cast["effron"]["aliases"])
        self.assertIn("Effron", cast["effron_child"]["aliases"])
        self.assertNotIn("Son", cast["effron_child"]["aliases"])

    def test_a_compound_name_does_not_leak_its_halves(self) -> None:
        """Triangulated on a third book, whose names are hyphenated compounds.

        `_derive_character_aliases` turns the id `the_battle_grim` into the
        aliases `Battle` and `Grim`. In the book, `Grim` appears 128 times and
        127 of those are inside "Battle-Grim"; `Troll`, split out of
        "Half-Troll", appears 6 times capitalised and 135 times in lower case.
        Alias lookup is casefolded, so keeping them is 135 chances to
        mis-attribute against 6 to help.
        """
        cast = {
            "the_battle_grim": {"name": "The Battle-Grim", "aliases": ["The Battle-Grim", "Battle", "Grim"]},
            "einar_half_troll": {"name": "Einar Half-Troll", "aliases": ["Einar Half-Troll", "Half", "Troll"]},
        }
        source = (
            "The Battle-Grim rowed on. Battle-Grim oars struck the water, and the Battle-Grim sang. "
            "It was a grim day for a grim errand, grim as the grim sea. "
            "Einar Half-Troll laughed. The troll had a troll's stink, half a troll and half a man, "
            "half again as tall, the troll-kin of the half-world."
        )
        removed = {r["alias"] for r in prune_ambiguous_fragment_aliases(cast, source)}
        self.assertEqual(removed, {"Battle", "Grim", "Half", "Troll"})
        self.assertEqual(cast["the_battle_grim"]["aliases"], ["The Battle-Grim"])
        self.assertIn("Einar Half-Troll", cast["einar_half_troll"]["aliases"])

    def test_an_epithet_only_character_keeps_its_epithet(self) -> None:
        """The article-stripping that fixes compounds must not eat these.

        "The Bloodsworn" and "The Tainted" are named by epithet and nothing
        else. Excluding the article-less form of the name would exclude every
        occurrence of the epithet itself, leaving no evidence and condemning
        exactly the characters this rule exists to protect. Caught by
        triangulating on the third book before it shipped.
        """
        cast = {
            "the_bloodsworn": {"name": "The Bloodsworn", "aliases": ["The Bloodsworn", "Bloodsworn"]},
            "the_tainted": {"name": "The Tainted", "aliases": ["The Tainted", "Tainted"]},
        }
        source = (
            "The Bloodsworn came ashore. Bloodsworn shields locked, and the Bloodsworn roared. "
            "The Tainted were hunted; a Tainted child was worth silver, and the Tainted knew it."
        )
        self.assertEqual(prune_ambiguous_fragment_aliases(cast, source), [])
        self.assertIn("Bloodsworn", cast["the_bloodsworn"]["aliases"])
        self.assertIn("Tainted", cast["the_tainted"]["aliases"])

    def test_a_shared_title_is_dropped_from_every_holder(self) -> None:
        """Three characters hold `Jarl`; it therefore identifies none of them."""
        cast = {
            "jarl_helka": {"name": "Jarl Helka", "aliases": ["Jarl Helka", "Jarl", "Helka"]},
            "jarl_storr": {"name": "Jarl Storr", "aliases": ["Jarl Storr", "Jarl", "Storr"]},
            "wave_jarl": {"name": "Wave-Jarl", "aliases": ["Wave-Jarl", "Wave", "Jarl"]},
        }
        source = (
            "Jarl Helka rode out. Helka met Jarl Storr, and Storr bowed to Helka. "
            "The Wave-Jarl waited at anchor. Every jarl in the land had come."
        )
        prune_ambiguous_fragment_aliases(cast, source)
        for cid in cast:
            with self.subTest(cid=cid):
                self.assertNotIn("Jarl", cast[cid]["aliases"])
                self.assertTrue(cast[cid]["aliases"], "each holder keeps its own name")
        self.assertIn("Helka", cast["jarl_helka"]["aliases"], "a real name beside the title survives")

    def test_no_source_text_means_no_pruning(self) -> None:
        cast = {"a": {"name": "White-Haired Being", "aliases": ["White-Haired Being", "Being"]}}
        self.assertEqual(prune_ambiguous_fragment_aliases(cast, ""), [])
        self.assertIn("Being", cast["a"]["aliases"])


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

    def test_a_clean_roster_still_leaves_an_audit(self) -> None:
        """Finding nothing must not look like never running.

        The audit used to be written only when something happened, so a clean
        roster produced no file -- the same evidence a disabled pass leaves,
        and the same a failed one leaves.
        """
        analyzer, registry, book, tmp = self._analyzer([])
        analyzer._adjudicate_cast_identity(registry, book, tmp)

        audit_path = tmp / "cast_identity_audit.json"
        self.assertTrue(audit_path.is_file(), "a clean run must still record that it ran")
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(audit["outcome"], "completed")
        self.assertEqual(audit["applied"], [])
        self.assertEqual(audit["refused"], [])

    def test_a_failed_adjudication_says_so_in_the_audit(self) -> None:
        """The 2026-09-04 run: every rung failed and the project kept no trace."""
        analyzer, registry, book, tmp = self._analyzer([])

        class _Broken:
            cast_adjudication_enabled = True

            def adjudicate_cast(self, **_kwargs):
                raise RuntimeError("all rungs unavailable")

        analyzer.external_validator = _Broken()
        analyzer._adjudicate_cast_identity(registry, book, tmp)

        audit_path = tmp / "cast_identity_audit.json"
        self.assertTrue(audit_path.is_file(), "a failure must be recorded, not silent")
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(audit["outcome"], "failed")
        self.assertIn("all rungs unavailable", json.dumps(audit["trace"]))

    def test_recovered_aliases_reach_the_audit(self) -> None:
        """The success path dropped them, so the audit contradicted the roster."""
        analyzer, registry, book, tmp = self._analyzer([])

        recovered = [{"character_id": "jarlaxle", "alias": "Jax"}]
        analyzer._recover_unlinked_speakers = lambda *a, **k: recovered
        analyzer._adjudicate_cast_identity(registry, book, tmp)

        audit = json.loads((tmp / "cast_identity_audit.json").read_text(encoding="utf-8"))
        self.assertEqual(
            audit["recovered_aliases"],
            recovered,
            "an alias applied to the roster must appear in the record of it",
        )

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


class CheckpointLifetimeTests(unittest.TestCase):
    """The checkpoint must outlive every step that can still fail.

    Observed in an e2e run on 2026-09-04: Pass 1 finished all 9 units, the
    checkpoint was deleted, adjudication then raised, and the retry restarted
    at unit 1. Discovery is the expensive half of scripting, so throwing it
    away for a failure in an advisory step afterwards is the worst possible
    trade.
    """

    def _analyze_source(self):
        import ast

        from brain.director import character_analyzer as module

        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        return next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "analyze")

    def test_the_checkpoint_is_deleted_after_the_fallible_steps(self) -> None:
        analyze = self._analyze_source()

        unlink_lines = [
            node.lineno
            for node in ast.walk(analyze)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "unlink"
        ]
        self.assertEqual(len(unlink_lines), 1, "expected exactly one checkpoint deletion")

        fallible = [
            node.lineno
            for node in ast.walk(analyze)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr
            in {
                "_adjudicate_cast_identity",
                "_augment_characters_with_gemini",
                "_assign_voice_ids",
                "_write_reference_audit",
            }
        ]
        self.assertTrue(fallible, "the steps this guards did not parse")
        self.assertGreater(
            unlink_lines[0],
            max(fallible),
            "the checkpoint is deleted while work that can still fail is pending",
        )

    def test_distinct_participant_veto_catches_interactive_dialogue(self) -> None:
        c1 = {"name": "Catti-brie", "aliases": ["Catti-brie", "Catti", "Brie"]}
        c2 = {"name": "Brie", "aliases": ["Brie", "Breezy", "Briennelle"]}
        text = '"Do not believe that you are escaping this," Catti-brie said to Breezy.'
        reason = distinct_participant_veto(text, c1, "catti_brie", c2, "brie")
        self.assertIsNotNone(reason)
        self.assertIn("interacting as distinct individuals", reason)

    def test_distinct_participant_veto_catches_adversative_narrative(self) -> None:
        c1 = {"name": "Catti-brie", "aliases": ["Catti-brie", "Catti"]}
        c2 = {"name": "Brie", "aliases": ["Breezy", "Briennelle"]}
        text = "Catti-brie moved as if to hug her, but Breezy held her hand up."
        reason = distinct_participant_veto(text, c1, "catti_brie", c2, "brie")
        self.assertIsNotNone(reason)
        self.assertIn("interacting as distinct individuals", reason)

    def test_distinct_participant_veto_allows_identity_reveals(self) -> None:
        c1 = {"name": "Artemis Entreri", "aliases": ["Artemis Entreri", "Barrabus the Gray"]}
        c2 = {"name": "Barrabus the Gray", "aliases": ["Barrabus the Gray", "Artemis Entreri"]}
        text = "Barrabus had traveled through the shadows for days."
        reason = distinct_participant_veto(text, c1, "entreri", c2, "barrabus")
        self.assertIsNone(reason)

    def test_distinct_participant_veto_allows_apposition_aliases(self) -> None:
        c1 = {"name": "Jarlaxle", "aliases": ["Jarlaxle", "Uncle Jax"]}
        c2 = {"name": "Uncle Jax", "aliases": ["Uncle Jax", "Jarlaxle"]}
        reason = distinct_participant_veto(APPOSITION, c1, "jarlaxle", c2, "uncle_jax")
        self.assertIsNone(reason)

    def test_repeated_apposition_does_not_veto_a_name_and_appellative(self) -> None:
        """The duplicate shape this whole feature exists to merge.

        `avelyere` and "the veteran wizard" share no token, so the roster stage
        is the only thing that can ever propose them -- and the source names
        them together in every sentence that introduces the appellative. A
        proximity or co-occurrence rule therefore refuses hardest exactly the
        merges the feature was built to find, which is why the 2026-09-04
        record measured proximity and rejected it: within 200 characters the
        distinct twins co-occur 6 times while the alias pairs co-occur 10 and
        15. A co-occurrence veto shipped anyway on 2026-09-06 and broke this.
        """
        cast = {
            "avelyere": {"name": "Avelyere", "aliases": [], "gender": "female", "dialogue_count": 44},
            "veteran_wizard": {
                "name": "The Veteran Wizard",
                "aliases": [],
                "gender": "female",
                "dialogue_count": 6,
            },
        }
        # Four sentences, each naming both in apposition -- the shape prose uses
        # to introduce an appellative beside the name it stands in for.
        book = chr(10).join([
            "Avelyere lifted her hand, and the veteran wizard's spell took shape.",
            "Catti-brie had studied under Avelyere once, the veteran wizard's methods exacting.",
            'It was Avelyere who answered, the veteran wizard speaking softly.',
            'Avelyere frowned; the veteran wizard was rarely wrong.',
        ])
        self.assertEqual(conjunction_count(book, ["Avelyere"], ["veteran wizard"]), 0)
        self.assertIsNone(merge_veto("avelyere", "veteran_wizard", cast, book))

    def test_bare_rank_and_divine_appellatives_cannot_carry_a_veto(self) -> None:
        """A bare "God" or "Captain" alias is as generic as "king" or "lord".

        `insect_god` carried the alias "God" and `patji` carried "The God";
        matching one against the other found five "co-occurrences" that were
        entirely prose about Patji. Ranks and divine titles now join the
        generic list -- measured on both books as costing zero real vetoes.
        """
        for generic in ("God", "The God", "Captain", "Colonel", "the Admiral", "Voice"):
            with self.subTest(term=generic):
                # The id is derived from the name, so it is generic too; passing
                # a distinctive id would leave a vetoable term behind and make
                # this pass for the wrong reason.
                char_id = generic.lower().replace(" ", "_")
                self.assertEqual(
                    _vetoable_terms({"name": generic, "aliases": []}, char_id),
                    [],
                    f"{generic!r} must not be able to carry a veto on its own",
                )
        # ...but a rank qualified by a real name still can.
        self.assertTrue(_vetoable_terms({"name": "Colonel Dajer", "aliases": []}, "dajer"))
        self.assertTrue(_vetoable_terms({"name": "Insect God", "aliases": []}, "insect_god"))

    def test_a_generic_word_a_named_character_depends_on_is_not_suppressed(self) -> None:
        """`officer`, `one` and `first` were measured and deliberately left out.

        For characters the book never named -- "Police Officer", "One of the
        Ones Above", "First of the Sky" -- the generic word is the only term
        they own, and suppressing it cost 2, 2 and 3 real refusals across the
        two books. Shaving the list to the measurement would have removed them.
        """
        for name in ("Police Officer", "One of the Ones Above", "First of the Sky"):
            with self.subTest(name=name):
                self.assertTrue(
                    _vetoable_terms({"name": name, "aliases": []}, name.lower().replace(" ", "_")),
                    f"{name!r} would lose its only vetoable term",
                )

    def test_interaction_veto_still_separates_family_members(self) -> None:
        """Removing the co-occurrence rule must not give back the Catti-brie bug.

        All three forms the book actually uses for these two are interaction
        beats, which rule 2 catches on its own.
        """
        c1 = {"name": "Catti-brie", "aliases": ["Catti-brie", "Catti"]}
        c2 = {"name": "Brie", "aliases": ["Breezy", "Briennelle"]}
        for text in (
            '"Do not believe that you are escaping this," Catti-brie said to Breezy.',
            "Catti-brie moved as if to hug her, but Breezy held her hand up.",
            "Breezy looked to Catti-brie for an answer.",
        ):
            with self.subTest(text=text):
                reason = distinct_participant_veto(text, c1, "catti_brie", c2, "brie")
                self.assertIsNotNone(reason, f"must still refuse: {text}")
                self.assertIn("interacting as distinct individuals", reason)


if __name__ == "__main__":
    unittest.main()
