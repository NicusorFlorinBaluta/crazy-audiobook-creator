"""Tests for the cast distinctness convergence loop.

Before this loop existed, pairwise comparison ran only after the VoiceDesign
subprocess had been shut down to free VRAM for the speaker encoder, so a
collision could be reported but never repaired. A 52-character cast on the
live project left 22 flagged pairs for manual resolution, one of them a
character measured at 0.992 speaker similarity against the narrator.

VoiceDesign is a separate process, so the two models can take turns rather than
be co-resident. These tests pin that the loop converges when it can, stays
bounded when it cannot, honours cancellation, and costs nothing when the first
measurement is already clean.

Everything here uses a faked engine and library; no model is loaded.
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from shared.models import BootstrapVoiceResult, BootstrapVoicesRequest, Character
from voice.tts_server.voice_designer import VoiceDesigner


class _FakeEngine:
    """Similarity is looked up from a table keyed by unordered voice pair."""

    def __init__(self, similarity_by_pair: dict[frozenset[str], float]):
        self.similarity_by_pair = similarity_by_pair
        self.embedding_calls: list[str] = []

    def speaker_embedding(self, file_path):
        self.embedding_calls.append(str(file_path))
        return ("embedding", str(file_path))

    def embedding_similarity(self, left, right):
        left_id = Path(left[1]).stem.split("_")[0]
        right_id = Path(right[1]).stem.split("_")[0]
        return self.similarity_by_pair.get(frozenset({left_id, right_id}), 0.10)


class _FakeLibrary:
    def __init__(self, root: Path):
        self.root = root
        self.deleted: list[str] = []

    def get_voice_path(self, project_id, voice_id):
        return self.root / project_id / f"{voice_id}.wav"

    def delete_voice(self, project_id, voice_id):
        self.deleted.append(voice_id)


def _character(name: str) -> Character:
    return Character(
        id=name,
        name=name.title(),
        gender="male",
        age_range="30s",
        voice_description=f"A voice for {name}.",
    )


def _designer(
    engine: _FakeEngine,
    library: _FakeLibrary,
    *,
    rounds: int = 2,
    threshold: float = 0.90,
) -> VoiceDesigner:
    designer = object.__new__(VoiceDesigner)
    designer.engine = engine
    designer.library = library
    designer.validator = None
    designer.similarity_warning_threshold = threshold
    designer.distinctness_rounds = rounds
    designer.acoustic_regeneration_attempts = 0
    designer.wer_threshold = 0.20
    designer.voice_design_test_sentences = {
        "male": "A male reference sentence long enough for voice design.",
        "female": "A female reference sentence long enough for voice design.",
    }
    return designer


def _result(voice_id: str, root: Path, suffix: str = "v1") -> BootstrapVoiceResult:
    path = root / f"{voice_id}_{suffix}.wav"
    return BootstrapVoiceResult(
        id=voice_id,
        file=str(path),
        duration_seconds=10.0,
        sample_rate=24000,
        acoustic_metrics={
            "median_f0_hz": 120.0,
            "f0_range_hz": 50.0,
            "spectral_centroid_hz": 1500.0,
        },
        warnings=[],
        candidates=[],
    )


class CompareCastTests(unittest.TestCase):
    def _noop_emit(self, *_args) -> None:
        return None

    def _noop_cancel(self) -> None:
        return None

    def test_collisions_name_the_specific_voices_involved(self) -> None:
        """A redesign brief needs *who* to move away from, not just 'be different'."""
        root = Path("voices")
        engine = _FakeEngine({frozenset({"alice", "bob"}): 0.99})
        designer = _designer(engine, _FakeLibrary(root))

        voices = {name: _result(name, root) for name in ("alice", "bob", "carol")}
        embeddings = {name: ("embedding", str(root / f"{name}_v1.wav")) for name in voices}

        diagnostics, collisions = designer._compare_cast(embeddings, voices, self._noop_emit, self._noop_cancel)

        self.assertEqual(len(diagnostics), 3)  # 3 voices -> 3 pairs
        self.assertEqual(collisions, {"alice": {"bob"}, "bob": {"alice"}})

    def test_summary_reports_worst_colliding_pair_not_worst_overall(self) -> None:
        """Suppressed pairs must not mask whether the rounds are helping."""
        diagnostics = [
            type("D", (), {"status": "similar", "speaker_similarity": 0.991})(),
            type("D", (), {"status": "distinct", "speaker_similarity": 0.999})(),
        ]
        summary = VoiceDesigner._collision_summary(diagnostics)
        self.assertEqual(summary["similar_pairs"], 1)
        self.assertAlmostEqual(summary["max_similarity"], 0.991)

    def test_warnings_reflect_only_the_final_measurement(self) -> None:
        """A voice repaired in round 1 must not keep its round-0 warning."""
        root = Path("voices")
        voices = {name: _result(name, root) for name in ("alice", "bob")}
        voices["alice"].warnings = [
            "Sounds very similar to bob (speaker similarity 0.991).",
            "Some unrelated warning worth keeping.",
        ]

        # Final measurement says they are distinct.
        diagnostics = [
            type(
                "D",
                (),
                {
                    "status": "distinct",
                    "speaker_similarity": 0.42,
                    "left_voice_id": "alice",
                    "right_voice_id": "bob",
                },
            )()
        ]
        VoiceDesigner._apply_similarity_warnings(diagnostics, voices)

        self.assertEqual(voices["alice"].warnings, ["Some unrelated warning worth keeping."])


class ConvergenceLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path("voices")
        self.request = BootstrapVoicesRequest(
            project_id="proj",
            characters={name: _character(name) for name in ("alice", "bob", "carol")},
        )

    @contextmanager
    def _fake_service(self, *_args, **_kwargs):
        self.service_boots += 1
        yield

    def _install(self, designer: VoiceDesigner) -> None:
        self.service_boots = 0
        designer._voice_design_service = self._fake_service

    def test_only_the_colliding_voices_are_redesigned(self) -> None:
        """Carol is fine; regenerating her would waste time and discard a good take."""
        engine = _FakeEngine({})
        library = _FakeLibrary(self.root)
        designer = _designer(engine, library)
        self._install(designer)

        voices = {name: _result(name, self.root) for name in ("alice", "bob", "carol")}
        collisions = {"alice": {"bob"}, "bob": {"alice"}}

        with (
            patch.object(
                VoiceDesigner,
                "_generate_voice",
                side_effect=lambda pid, cid, ch, **kw: _result(cid, self.root, "v2"),
            ),
            patch.object(VoiceDesigner, "_acoustic_diagnostics", return_value=({}, [])),
        ):
            regenerated = designer._redesign_for_distinctness(
                self.request,
                collisions,
                voices,
                lambda *a: None,
                lambda: None,
                round_index=1,
            )

        self.assertEqual(regenerated, {"alice", "bob"})
        self.assertNotIn("carol", library.deleted)
        self.assertEqual(self.service_boots, 1)

    def test_redesign_brief_names_the_colliding_voices(self) -> None:
        engine = _FakeEngine({})
        designer = _designer(engine, _FakeLibrary(self.root))
        self._install(designer)

        voices = {name: _result(name, self.root) for name in ("alice", "bob")}
        seen: list[str] = []

        def _capture(pid, cid, character, **kw):
            seen.append(character.voice_description)
            return _result(cid, self.root, "v2")

        with (
            patch.object(VoiceDesigner, "_generate_voice", side_effect=_capture),
            patch.object(VoiceDesigner, "_acoustic_diagnostics", return_value=({}, [])),
        ):
            designer._redesign_for_distinctness(
                self.request,
                {"alice": {"bob"}},
                voices,
                lambda *a: None,
                lambda: None,
                round_index=1,
            )

        self.assertEqual(len(seen), 1)
        self.assertIn("acoustically too close to bob", seen[0])
        self.assertIn("A voice for alice.", seen[0])

    def test_a_failed_redesign_keeps_the_previous_reference(self) -> None:
        """Losing a usable voice to a failed retry would be worse than a warning."""
        engine = _FakeEngine({})
        designer = _designer(engine, _FakeLibrary(self.root))
        self._install(designer)

        voices = {name: _result(name, self.root) for name in ("alice", "bob")}
        original_file = voices["alice"].file

        with (
            patch.object(
                VoiceDesigner,
                "_generate_voice",
                side_effect=RuntimeError("design service died"),
            ),
            patch.object(VoiceDesigner, "_acoustic_diagnostics", return_value=({}, [])),
        ):
            regenerated = designer._redesign_for_distinctness(
                self.request,
                {"alice": {"bob"}},
                voices,
                lambda *a: None,
                lambda: None,
                round_index=1,
            )

        self.assertEqual(regenerated, set())
        self.assertEqual(voices["alice"].file, original_file)

    def test_cancellation_stops_the_round(self) -> None:
        engine = _FakeEngine({})
        designer = _designer(engine, _FakeLibrary(self.root))
        self._install(designer)
        voices = {name: _result(name, self.root) for name in ("alice", "bob")}

        def _cancel() -> None:
            raise RuntimeError("Voice bootstrapping cancelled")

        with (
            patch.object(VoiceDesigner, "_generate_voice") as generate,
            patch.object(VoiceDesigner, "_acoustic_diagnostics", return_value=({}, [])),
            self.assertRaises(RuntimeError),
        ):
            designer._redesign_for_distinctness(
                self.request,
                {"alice": {"bob"}},
                voices,
                lambda *a: None,
                _cancel,
                round_index=1,
            )
        generate.assert_not_called()

    def _constructed(self, rounds) -> VoiceDesigner:
        return VoiceDesigner(
            engine=_FakeEngine({}),
            library=_FakeLibrary(self.root),
            distinctness_rounds=rounds,
        )

    def test_zero_rounds_restores_report_only_behaviour(self) -> None:
        self.assertEqual(self._constructed(0).distinctness_rounds, 0)

    def test_round_count_is_clamped_by_the_constructor(self) -> None:
        """Each round re-boots a model subprocess; a typo must not cost an hour."""
        self.assertEqual(self._constructed(99).distinctness_rounds, 5)
        self.assertEqual(self._constructed(-3).distinctness_rounds, 0)

    def test_default_is_two_rounds(self) -> None:
        designer = VoiceDesigner(engine=_FakeEngine({}), library=_FakeLibrary(self.root))
        self.assertEqual(designer.distinctness_rounds, 2)

    # --- the loop itself ------------------------------------------------

    def _run_loop(self, designer, voices, embeddings):
        diagnostics, collisions = designer._compare_cast(embeddings, voices, lambda *a: None, lambda: None)
        return designer._converge_distinctness(
            self.request,
            voices,
            embeddings,
            diagnostics,
            collisions,
            lambda *a: None,
            lambda: None,
        )

    def test_loop_converges_and_stops_early(self) -> None:
        """One round is enough here, so the second must not be spent."""
        # alice/bob collide at v1; the redesigned v2 files do not.
        engine = _FakeEngine({frozenset({"alice", "bob"}): 0.99})
        designer = _designer(engine, _FakeLibrary(self.root), rounds=2)
        self._install(designer)

        voices = {name: _result(name, self.root) for name in ("alice", "bob", "carol")}
        embeddings = {name: ("embedding", str(self.root / f"{name}_v1.wav")) for name in voices}

        def _regenerate(pid, cid, character, **kw):
            # The new take is distinct: drop the colliding pair from the table.
            engine.similarity_by_pair.pop(frozenset({"alice", "bob"}), None)
            return _result(cid, self.root, "v2")

        with (
            patch.object(VoiceDesigner, "_generate_voice", side_effect=_regenerate),
            patch.object(VoiceDesigner, "_acoustic_diagnostics", return_value=({}, [])),
        ):
            diagnostics, rounds = self._run_loop(designer, voices, embeddings)

        self.assertEqual(len(rounds), 1, "converged, so only one round should run")
        self.assertEqual(self.service_boots, 1)
        self.assertEqual(rounds[0]["similar_pairs_before"], 1)
        self.assertEqual(rounds[0]["similar_pairs_after"], 0)
        self.assertEqual(sorted(rounds[0]["redesigned"]), ["alice", "bob"])
        self.assertFalse([d for d in diagnostics if d.status == "similar"])
        # No collisions left, so no warnings survive.
        self.assertFalse([w for w in voices["alice"].warnings if w.startswith("Sounds very similar")])

    def test_loop_is_bounded_and_degrades_to_warnings(self) -> None:
        """Voices that will not separate must end as warnings, not a hang."""
        engine = _FakeEngine({frozenset({"alice", "bob"}): 0.99})
        designer = _designer(engine, _FakeLibrary(self.root), rounds=2)
        self._install(designer)

        voices = {name: _result(name, self.root) for name in ("alice", "bob", "carol")}
        embeddings = {name: ("embedding", str(self.root / f"{name}_v1.wav")) for name in voices}

        with (
            patch.object(
                VoiceDesigner,
                "_generate_voice",
                # Every redesign keeps colliding.
                side_effect=lambda pid, cid, ch, **kw: _result(cid, self.root, "v1"),
            ),
            patch.object(VoiceDesigner, "_acoustic_diagnostics", return_value=({}, [])),
        ):
            diagnostics, rounds = self._run_loop(designer, voices, embeddings)

        # A round that does not improve on the best measurement is discarded
        # and the loop stops: resampling from a state already known to be no
        # better is not worth another VoiceDesign boot.
        self.assertEqual(len(rounds), 1)
        self.assertFalse(rounds[0]["kept"])
        self.assertEqual(self.service_boots, 1)
        self.assertEqual(rounds[-1]["similar_pairs_after"], 1)
        similar = [d for d in diagnostics if d.status == "similar"]
        self.assertEqual(len(similar), 1)
        # The unresolved collision is still surfaced for manual redesign.
        self.assertTrue(any(w.startswith("Sounds very similar to bob") for w in voices["alice"].warnings))

    def test_a_round_that_makes_things_worse_is_rolled_back(self) -> None:
        """Measured on real models: round 1 moved the worst pair 0.9881 -> 0.9915.

        A redesign is a resample from a stochastic model, not a monotonic
        improvement. Without this the loop keeps the regression and builds the
        next round on top of it.
        """
        engine = _FakeEngine({frozenset({"alice", "bob"}): 0.991})
        designer = _designer(engine, _FakeLibrary(self.root), rounds=2)
        self._install(designer)

        voices = {name: _result(name, self.root) for name in ("alice", "bob")}
        embeddings = {name: ("embedding", str(self.root / f"{name}_v1.wav")) for name in voices}
        original_files = {k: v.file for k, v in voices.items()}

        def _worse(pid, cid, character, **kw):
            # The new take is worse than what we already had.
            engine.similarity_by_pair[frozenset({"alice", "bob"})] = 0.998
            return _result(cid, self.root, "v2")

        with (
            patch.object(VoiceDesigner, "_generate_voice", side_effect=_worse),
            patch.object(VoiceDesigner, "_acoustic_diagnostics", return_value=({}, [])),
        ):
            diagnostics, rounds = self._run_loop(designer, voices, embeddings)

        self.assertEqual(len(rounds), 1)
        self.assertFalse(rounds[0]["kept"], "a regression must not be kept")
        self.assertGreater(rounds[0]["max_similarity_after"], rounds[0]["max_similarity_before"])
        # The cast is back to the better take, not the regressed one.
        self.assertEqual({k: v.file for k, v in voices.items()}, original_files)
        worst = max(d.speaker_similarity for d in diagnostics)
        self.assertAlmostEqual(worst, 0.991, places=3)

    def test_the_previous_take_is_not_deleted_before_a_redesign(self) -> None:
        """Rollback needs the file. `_generate_voice` re-registers the id anyway."""
        engine = _FakeEngine({})
        library = _FakeLibrary(self.root)
        designer = _designer(engine, library)
        self._install(designer)

        voices = {name: _result(name, self.root) for name in ("alice", "bob")}
        with (
            patch.object(
                VoiceDesigner,
                "_generate_voice",
                side_effect=lambda pid, cid, ch, **kw: _result(cid, self.root, "v2"),
            ),
            patch.object(VoiceDesigner, "_acoustic_diagnostics", return_value=({}, [])),
        ):
            designer._redesign_for_distinctness(
                self.request,
                {"alice": {"bob"}},
                voices,
                lambda *a: None,
                lambda: None,
                round_index=1,
            )
        self.assertEqual(library.deleted, [], "the previous take must survive")

    def test_a_clean_cast_costs_no_model_swap(self) -> None:
        """The common case must not pay for the loop at all."""
        engine = _FakeEngine({})
        designer = _designer(engine, _FakeLibrary(self.root), rounds=2)
        self._install(designer)

        voices = {name: _result(name, self.root) for name in ("alice", "bob")}
        embeddings = {name: ("embedding", str(self.root / f"{name}_v1.wav")) for name in voices}

        with patch.object(VoiceDesigner, "_generate_voice") as generate:
            _, rounds = self._run_loop(designer, voices, embeddings)

        self.assertEqual(rounds, [])
        self.assertEqual(self.service_boots, 0, "no VoiceDesign boot for a clean cast")
        generate.assert_not_called()

    def test_zero_rounds_measures_but_never_redesigns(self) -> None:
        engine = _FakeEngine({frozenset({"alice", "bob"}): 0.99})
        designer = _designer(engine, _FakeLibrary(self.root), rounds=0)
        self._install(designer)

        voices = {name: _result(name, self.root) for name in ("alice", "bob")}
        embeddings = {name: ("embedding", str(self.root / f"{name}_v1.wav")) for name in voices}

        with patch.object(VoiceDesigner, "_generate_voice") as generate:
            diagnostics, rounds = self._run_loop(designer, voices, embeddings)

        self.assertEqual(rounds, [])
        self.assertEqual(self.service_boots, 0)
        generate.assert_not_called()
        # Report-only behaviour is preserved exactly.
        self.assertEqual(len([d for d in diagnostics if d.status == "similar"]), 1)
        self.assertTrue(any(w.startswith("Sounds very similar to bob") for w in voices["alice"].warnings))

    def test_redesigned_references_are_transcript_checked(self) -> None:
        """A contrast brief moves pitch and rate, which can hurt intelligibility.

        The initial WER pass runs before any redesign exists, so without a
        re-check a regenerated reference would ship unchecked.
        """

        class _Validator:
            def __init__(self):
                self.transcribed: list[str] = []
                self.unloaded = False

            def transcribe(self, path):
                self.transcribed.append(str(path))
                return "not what was asked for"

            def calculate_wer(self, expected, actual):
                return 0.85

            def unload(self):
                self.unloaded = True

        engine = _FakeEngine({frozenset({"alice", "bob"}): 0.99})
        designer = _designer(engine, _FakeLibrary(self.root), rounds=1)
        designer.validator = _Validator()
        designer.wer_threshold = 0.20
        self._install(designer)

        voices = {name: _result(name, self.root) for name in ("alice", "bob")}
        embeddings = {name: ("embedding", str(self.root / f"{name}_v1.wav")) for name in voices}

        def _regenerate(pid, cid, character, **kw):
            new = _result(cid, self.root, "v2")
            new.candidates = []
            return new

        with (
            patch.object(VoiceDesigner, "_generate_voice", side_effect=_regenerate),
            patch.object(VoiceDesigner, "_acoustic_diagnostics", return_value=({}, [])),
        ):
            self._run_loop(designer, voices, embeddings)

        self.assertEqual(len(designer.validator.transcribed), 2)
        self.assertTrue(designer.validator.unloaded, "Whisper must be released")
        self.assertTrue(
            any("exceeded threshold" in w for w in voices["alice"].warnings),
            "a bad redesigned transcript must be surfaced, not silently kept",
        )

    def test_transcript_recheck_is_skipped_when_nothing_was_redesigned(self) -> None:
        class _Validator:
            def __init__(self):
                self.calls = 0

            def transcribe(self, path):
                self.calls += 1
                return ""

            def calculate_wer(self, expected, actual):
                return 0.0

            def unload(self):
                pass

        designer = _designer(_FakeEngine({}), _FakeLibrary(self.root), rounds=2)
        designer.validator = _Validator()
        designer.wer_threshold = 0.20
        self._install(designer)

        voices = {name: _result(name, self.root) for name in ("alice", "bob")}
        embeddings = {name: ("embedding", str(self.root / f"{name}_v1.wav")) for name in voices}
        self._run_loop(designer, voices, embeddings)
        self.assertEqual(designer.validator.calls, 0)


if __name__ == "__main__":
    unittest.main()
