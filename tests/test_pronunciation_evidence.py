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

from shared.pronunciation_evidence import (
    TermEvidence,
    best_matching_span,
    measure_terms,
    phonetic_key,
    sound_similarity,
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
        which the engine would then read as "tent owns".
        """
        texts, transcripts = _corpus(
            "Ten-Towns",
            ["ten towns", "ten towns", "ten towns", "ten towns", "into", "ten stones"],
        )
        evidence = measure_terms(["Ten-Towns"], texts, transcripts)["Ten-Towns"]
        assert evidence.agree_rate < 0.70
        assert evidence.dominant_matches
        assert evidence.verdict == "spoken_correctly"

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
