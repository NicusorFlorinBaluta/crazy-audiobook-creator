"""A respelling ships only where the engine measurably says the name wrong.

Every case here is real, taken from the 2026-09-12 run over
`the-finest-edge-of-twilight-book` (2,667 transcribed lines) and
`isles-of-the-emberdark-a-cosmere-novel-secret-projects-book-5` (9,135). The
LLM proposed 28 respellings for the first book; measurement kept 6.

The expensive half of this module is what it *refuses*. Eleven of those 28
were for names Whisper reported faithfully, and applying them would have
replaced working audio with a guess.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from shared.pronunciation_evidence import (
    TermEvidence,
    best_matching_span,
    is_pronunciation_candidate_better,
    measure_terms,
    phonetic_key,
    same_spoken_form,
    sound_similarity,
    terms_in_text,
)


class TestSoundNotSpelling:
    """Whisper picks its own spelling; that is not the engine being wrong."""

    def test_wulfgar_and_wolfgar_are_one_sound(self) -> None:
        """65 of 83 lines came back "wolfgar". The engine was correct."""
        assert phonetic_key("Wulfgar") == phonetic_key("Wolfgar")

    def test_drizzt_and_drist_are_one_sound(self) -> None:
        """ "drist" x34 is how `Drizzt` is meant to sound."""
        assert sound_similarity("Drizzt", "drist") >= 0.86

    def test_sylfae_survives_whispers_ph_spelling(self) -> None:
        """sylphay / sylphae / sylphie / sylfey are one rendering, not four."""
        keys = {phonetic_key(w) for w in ("Sylfae", "sylphay", "sylphae", "sylfey")}
        assert len(keys) == 1

    def test_guen_and_gwen_are_one_sound(self) -> None:
        """`gu` before a vowel is /gw/; without this the correct "gwen" scored 14%."""
        assert phonetic_key("Guen") == phonetic_key("Gwen")

    def test_entreri_and_entrary_are_one_sound(self) -> None:
        """Treating `y` as a vowel; without it these missed the threshold by 0.003."""
        assert sound_similarity("Entreri", "entrary") >= 0.86

    def test_a_genuinely_different_name_does_not_match(self) -> None:
        """The key is lossy, not useless."""
        assert sound_similarity("Catti-brie", "Cadbury") < 0.86
        assert sound_similarity("Xisis", "Jesus") < 0.86


class TestTheWholeTermIsMatched:
    def test_a_two_word_name_rendered_as_one_word_is_a_failure(self) -> None:
        """`Braelin Janquay` came back as "braylon" -- half the name is gone.

        Matching only the first word scored this 94% correct.
        """
        span, score = best_matching_span("Braelin Janquay", ["braylon", "said", "to", "him"])
        assert span.startswith("braylon")
        assert score < 0.86, "half the name is missing, so it must not read as a match"

    def test_a_name_split_across_words_is_still_found(self) -> None:
        """ "coker lee" is `Kokerlii` spoken correctly, just transcribed apart."""
        span, score = best_matching_span("Kokerlii", ["the", "bird", "coker", "lee", "chirped"])
        assert span == "coker lee"
        assert score >= 0.86


def _corpus(term: str, heard: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    texts = {f"ch01_{i:04d}": f"And then {term} spoke." for i in range(len(heard))}
    transcripts = {f"ch01_{i:04d}": f"And then {value} spoke." for i, value in enumerate(heard)}
    return texts, transcripts


class TestVerdicts:
    def test_a_faithfully_rendered_name_needs_no_respelling(self) -> None:
        """`Regis`: 56 of 56 lines came back "Regis"."""
        texts, transcripts = _corpus("Regis", ["Regis"] * 8)
        evidence = measure_terms(["Regis"], texts, transcripts)["Regis"]
        assert evidence.verdict == "spoken_correctly"

    def test_a_mangled_name_is_flagged(self) -> None:
        """`Catti-brie` was heard as caddy bree / cadibri / cadbury."""
        texts, transcripts = _corpus("Catti-brie", ["caddy bree", "cadibri", "cadbury", "caddy", "cadibri"])
        evidence = measure_terms(["Catti-brie"], texts, transcripts)["Catti-brie"]
        assert evidence.verdict == "mispronounced"

    def test_a_name_varying_only_in_vowels_is_undecided_not_decided(self) -> None:
        """`Bruenor`: bruinor / brunor / "bryn or" / briennor over 127 lines.

        The phonetic key discards vowel quality, so all four are one sound
        group and the average agreement is 98%. But "BROO-nor" and "BRIN-or"
        are different names to a listener, and the key cannot tell them apart.

        Calling this `spoken_correctly` would overstate the evidence; calling
        it `mispronounced` would ship a respelling nothing measured. It is a
        question for a human ear, and the verdict says so.
        """
        texts, transcripts = _corpus(
            "Bruenor",
            ["bruinor", "brunor", "bryn or", "briennor", "brynor", "bruinor", "bryn or", "briennor"],
        )
        evidence = measure_terms(["Bruenor"], texts, transcripts)["Bruenor"]
        assert evidence.stability >= 0.60, "one sound group, because vowels are discarded"
        assert evidence.spelling_stability < 0.50, "but no single rendering dominates"
        assert evidence.verdict == "undecided"

    def test_the_commonest_rendering_decides_not_the_average(self) -> None:
        """`Ten-Towns` agreed on 64% of lines but was heard as "ten towns".

        Long lines whose best span landed elsewhere dragged the average under
        the threshold. Judging on the average would have justified "Tentowns",
        which the engine would then read as "tent owns" -- so the average must
        not be what condemns a term.

        It is `unstable` rather than `spoken_correctly` because two of these
        six lines really do say something else; see `TestStability`. The point
        this case defends is that neither reading is `mispronounced`, which is
        the only verdict that would ship the respelling.
        """
        texts, transcripts = _corpus(
            "Ten-Towns",
            ["ten towns", "ten towns", "ten towns", "ten towns", "into", "ten stones"],
        )
        evidence = measure_terms(["Ten-Towns"], texts, transcripts)["Ten-Towns"]
        assert evidence.agree_rate < 0.70
        assert evidence.dominant_matches
        assert evidence.verdict != "mispronounced"

    def test_too_little_evidence_never_justifies_a_respelling(self) -> None:
        """11 of the 28 proposals had under 3 generated lines to judge by."""
        texts, transcripts = _corpus("Qadeej", ["kadeej", "kadeej"])
        evidence = measure_terms(["Qadeej"], texts, transcripts)["Qadeej"]
        assert evidence.samples == 2
        assert evidence.verdict == "insufficient"

    def test_a_term_with_no_generated_audio_is_insufficient_not_correct(self) -> None:
        evidence = measure_terms(["Galathae"], {"ch01_0000": "Galathae waited."}, {})["Galathae"]
        assert evidence.samples == 0
        assert evidence.verdict == "insufficient"

    def test_xisis_is_heard_as_jesus(self) -> None:
        """The one Emberdark term measurement kept."""
        texts, transcripts = _corpus("Xisis", ["jesus", "cysis", "zesus", "zissus", "jesus"])
        evidence = measure_terms(["Xisis"], texts, transcripts)["Xisis"]
        assert evidence.verdict == "mispronounced"


class TestEvidenceReporting:
    def test_the_report_names_what_was_heard(self) -> None:
        """A verdict a reviewer cannot check is not evidence."""
        texts, transcripts = _corpus("Xisis", ["jesus", "jesus", "cysis"])
        payload = measure_terms(["Xisis"], texts, transcripts)["Xisis"].as_dict()
        assert payload["samples"] == 3
        assert payload["verdict"] == "mispronounced"
        assert any("jesus" in str(entry[0]) for entry in payload["heard_as"])

    def test_an_empty_evidence_record_is_safe_to_read(self) -> None:
        blank = TermEvidence(term="Nobody")
        assert blank.agree_rate == 0.0
        assert blank.stability == 0.0
        assert blank.verdict == "insufficient"


class TestStability:
    """A name the engine mostly gets right can still be wrong on some lines.

    Added 2026-09-16, after a listener reached chapter 27 of
    `the-finest-edge-of-twilight-book` and reported that `Drizzt` was being
    said two different ways. It was: 141 of 157 lines say "drist", and 12 say
    "driz-ZIT". The measurement pass four days earlier had called it
    `spoken_correctly`, because the dominant rendering is right and no rule
    looked at the rest.
    """

    def test_drizzt_is_unstable_not_correct(self) -> None:
        """The real distribution: the majority is right and a tenth is not."""
        texts, transcripts = _corpus("Drizzt", ["drist"] * 18 + ["drizzt"] * 9 + ["drizzit", "drizit", "drizzet"])
        evidence = measure_terms(["Drizzt"], texts, transcripts)["Drizzt"]
        assert evidence.dominant_matches, "the commonest rendering is still correct"
        assert evidence.spelling_stability >= 0.50, "and its commonest spelling still dominates"
        assert evidence.verdict == "unstable", "but a tenth of the lines say a different name"

    def test_an_entry_that_worked_leaves_nothing_behind(self) -> None:
        """`Luskan` -> `Laskan`: 27 of 27 lines came back "laskin"."""
        texts, transcripts = _corpus("Luskan", ["laskin"] * 27)
        evidence = measure_terms(["Luskan"], texts, transcripts)["Luskan"]
        assert evidence.outliers == 0
        assert evidence.verdict == "spoken_correctly"

    def test_the_minority_renderings_are_named_not_just_counted(self) -> None:
        """A percentage is not actionable; "it says driz-ZIT here" is."""
        texts, transcripts = _corpus("Drizzt", ["drist"] * 18 + ["drizzt"] * 9 + ["drizzit", "drizit", "drizzet"])
        evidence = measure_terms(["Drizzt"], texts, transcripts)["Drizzt"]
        minority = dict(evidence.minority_renderings)
        assert set(minority) == {"drizzit", "drizit", "drizzet"}
        assert "drist" not in minority and "drizzt" not in minority

    def test_the_outlying_lines_are_identified(self) -> None:
        """Seeds are per line, so the bad takes are a fixed, nameable set.

        They never heal on a rerun, which is what makes listing them worth
        more than a stability percentage.
        """
        texts, transcripts = _corpus("Drizzt", ["drist"] * 18 + ["drizzt"] * 9 + ["drizzit", "drizit", "drizzet"])
        evidence = measure_terms(["Drizzt"], texts, transcripts)["Drizzt"]
        assert [line_id for line_id, _ in evidence.outlier_lines] == ["ch01_0027", "ch01_0028", "ch01_0029"]
        assert evidence.outliers == 3

    def test_instability_never_ships_a_respelling(self) -> None:
        """`unstable` is a report. Only `mispronounced` applies a respelling."""
        texts, transcripts = _corpus("Jarlaxle", ["jarlaxle"] * 20 + ["jarl axel", "jar laxal"])
        evidence = measure_terms(["Jarlaxle"], texts, transcripts)["Jarlaxle"]
        assert evidence.verdict == "unstable"
        assert evidence.verdict != "mispronounced"


class TestWholePhraseEvidence:
    """A multi-word term is only evidenced by lines that contain all of it."""

    def test_lines_naming_only_the_forename_are_not_evidence(self) -> None:
        """The book calls him "Gregory" on most of his lines.

        Selecting evidence on the first word counted those as failures to say
        "Gregory Antoine" and reported a name the engine was never asked for
        as mispronounced.
        """
        texts = {
            "ch01_0000": "Gregory Antoine bowed.",
            "ch01_0001": "Gregory said nothing.",
            "ch01_0002": "Gregory turned away.",
        }
        transcripts = {
            "ch01_0000": "Gregory Antoine bowed.",
            "ch01_0001": "Gregory said nothing.",
            "ch01_0002": "Gregory turned away.",
        }
        evidence = measure_terms(["Gregory Antoine"], texts, transcripts)["Gregory Antoine"]
        assert evidence.samples == 1, "only the line that actually says the full name"

    def test_a_dropped_surname_is_still_caught(self) -> None:
        """The `Braelin Janquay` case must keep working.

        Its lines do say the whole name; the engine is what drops half of it.
        """
        texts = {f"ch01_{i:04d}": "Braelin Janquay waited." for i in range(4)}
        transcripts = {f"ch01_{i:04d}": "braylon waited." for i in range(4)}
        evidence = measure_terms(["Braelin Janquay"], texts, transcripts)["Braelin Janquay"]
        assert evidence.samples == 4
        assert evidence.verdict == "mispronounced"


class TestSameSpokenForm:
    """The validator's question: Whisper's spelling, or a different word?

    `_glossary_adjusted_wer` forgave a 0.45 character ratio or a three-letter
    prefix, which waved through almost any rendering of a name. "drizzit" for
    `Drizzt` scored 0.92 and cost nothing, so the mispronunciation a listener
    noticed never triggered the retry that would have redrawn it.
    """

    def test_whispers_own_spelling_is_forgiven(self) -> None:
        """What the forgiveness was built for, and must keep doing."""
        assert same_spoken_form("wulfgar", "wolfgar")
        assert same_spoken_form("drizzt", "drist")
        assert same_spoken_form("luskan", "laskin")
        assert same_spoken_form("sylfae", "sylphay")
        assert same_spoken_form("entreri", "entrary")

    def test_an_added_syllable_is_not_forgiven(self) -> None:
        """The case this exists for: "driz-ZIT" is a different name."""
        assert not same_spoken_form("drizzt", "drizzit")
        assert not same_spoken_form("drizzt", "drizit")
        assert not same_spoken_form("drizzt", "drizzet")

    def test_a_similarity_threshold_could_not_have_done_this(self) -> None:
        """Why syllables and not a score.

        On the phonetic key the wrong rendering scores *higher* than the right
        one, so no single cutoff separates them. Both pairs are real.
        """
        wrong = SequenceMatcher(None, phonetic_key("drizzt"), phonetic_key("drizzit")).ratio()
        right = SequenceMatcher(None, phonetic_key("guenhwyvar"), phonetic_key("guinevar")).ratio()
        assert wrong > right, "the mispronunciation is the closer of the two by score"
        assert not same_spoken_form("drizzt", "drizzit")
        assert same_spoken_form("guenhwyvar", "guinevar")

    def test_a_name_split_across_words_is_still_forgiven(self) -> None:
        """Whisper writes long names as two tokens; the first is not an error."""
        assert same_spoken_form("kokerlii", "coker")
        assert same_spoken_form("jarlaxle", "jarl")
        assert same_spoken_form("dalereckoning", "dale")

    def test_a_genuinely_different_word_is_not_forgiven(self) -> None:
        assert not same_spoken_form("catti", "caddy")
        assert not same_spoken_form("xisis", "jesus")

    def test_a_fragment_too_short_to_judge_is_not_forgiven(self) -> None:
        assert not same_spoken_form("drizzt", "dr")
        assert not same_spoken_form("drizzt", "")


class TestTermsInText:
    def test_single_word_terms(self) -> None:
        terms = {"Drizzt", "Entreri", "Bruenor"}
        text = "Drizzt fought with fury."
        assert terms_in_text(text, terms) == {"Drizzt"}

    def test_multi_word_terms(self) -> None:
        terms = {"Drizzt", "Artemis Entreri", "Entreri", "Do'Urden"}
        text = "He spoke with Artemis Entreri and Drizzt Do'Urden yesterday."
        assert terms_in_text(text, terms) == {"Drizzt", "Artemis Entreri", "Entreri", "Do'Urden"}

    def test_partial_match_ignored(self) -> None:
        terms = {"Artemis Entreri"}
        text = "Artemis smiled alone."
        assert terms_in_text(text, terms) == set()


class TestIsPronunciationCandidateBetter:
    def test_fixes_target_term_without_regression(self) -> None:
        from shared.models import QualityResult, ValidationStatus

        current = QualityResult(
            line_id="ch01_0001",
            status=ValidationStatus.PASS,
            wer=0.0,
            transcribed_text="drist and trary went ahead",
        )
        candidate = QualityResult(
            line_id="ch01_0001",
            status=ValidationStatus.PASS,
            wer=0.0,
            transcribed_text="drist entrary went ahead",
        )
        assert is_pronunciation_candidate_better(candidate, current, {"Drizzt", "Entreri"})

    def test_cross_term_guard_rejects_breaking_other_name(self) -> None:
        """Defect 1: Fixing Entreri while breaking Drizzt ('drizzit') must be rejected."""
        from shared.models import QualityResult, ValidationStatus

        current = QualityResult(
            line_id="ch01_0001",
            status=ValidationStatus.PASS,
            wer=0.0,
            quality_score=0.85,
            transcribed_text="drist and trary went ahead",
        )
        candidate = QualityResult(
            line_id="ch01_0001",
            status=ValidationStatus.PASS,
            wer=0.0,
            quality_score=0.95,  # Higher quality score must NOT override broken name
            transcribed_text="drizzit entrary went ahead",
        )
        assert not is_pronunciation_candidate_better(candidate, current, {"Drizzt", "Entreri"})

    def test_refuses_failed_hard_gates(self) -> None:
        from shared.models import QualityResult, ValidationStatus

        current = QualityResult(
            line_id="ch01_0001",
            status=ValidationStatus.PASS,
            wer=0.0,
            transcribed_text="drizzit",
        )
        candidate = QualityResult(
            line_id="ch01_0001",
            status=ValidationStatus.FAIL,
            wer=0.0,
            clipping_detected=True,
            transcribed_text="drist",
        )
        assert not is_pronunciation_candidate_better(candidate, current, {"Drizzt"})
