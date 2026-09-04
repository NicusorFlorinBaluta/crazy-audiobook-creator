"""Voice Designer — Bootstrap character voices from text descriptions.

Orchestrates the Voice Design → Save → Clone workflow:
1. Takes character voice descriptions from the LLM
2. Generates reference clips using Qwen3-TTS VoiceDesign
3. Saves them to the Voice Library for reuse
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from shared import paths as shared_paths
from shared.constants import VOICE_DESIGN_TEST_SENTENCES, Gender
from shared.models import (
    BootstrapVoiceResult,
    BootstrapVoicesRequest,
    BootstrapVoicesResponse,
    CastPairDiagnostic,
    Character,
    VoiceCandidate,
)
from voice.tts_server.qwen3_engine import Qwen3TTSEngine
from voice.tts_server.voice_library import VoiceLibraryManager

logger = logging.getLogger(__name__)


class VoiceDesigner:
    """Generate unique voice reference clips for characters."""

    def __init__(
        self,
        engine: Qwen3TTSEngine,
        library: VoiceLibraryManager,
        validator: Any = None,
        voice_design_duration: float = 10.0,
        voice_design_model: str = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        voice_design_test_sentences: dict[str, str] | None = None,
        wer_threshold: float = 0.20,
        similarity_warning_threshold: float = 0.88,
        acoustic_regeneration_attempts: int = 1,
        distinctness_rounds: int = 2,
    ):
        self.engine = engine
        self.library = library
        self.validator = validator
        self.voice_design_duration = voice_design_duration
        self.voice_design_model = voice_design_model
        self.wer_threshold = wer_threshold
        self.similarity_warning_threshold = max(-1.0, min(1.0, similarity_warning_threshold))
        self.acoustic_regeneration_attempts = max(0, int(acoustic_regeneration_attempts))
        # Extra design/compare rounds spent separating colliding voices.
        # Bounded on purpose: each round re-boots the VoiceDesign subprocess,
        # so the cost is real, and a cast that will not separate must end as a
        # warning rather than an unbounded loop. 0 restores the previous
        # report-only behaviour.
        self.distinctness_rounds = max(0, min(5, int(distinctness_rounds)))
        configured_sentences = voice_design_test_sentences or {}
        self.voice_design_test_sentences = {
            "male": configured_sentences.get("male", VOICE_DESIGN_TEST_SENTENCES["male"]),
            "female": configured_sentences.get("female", VOICE_DESIGN_TEST_SENTENCES["female"]),
            "other": configured_sentences.get(
                "other",
                configured_sentences.get("neutral", VOICE_DESIGN_TEST_SENTENCES["other"]),
            ),
        }

    def _build_test_sentence(self, char_id: str, character: Character) -> str:
        gender_key = (character.gender.value if hasattr(character.gender, "value") else str(character.gender)).lower()
        fallback = self.voice_design_test_sentences.get(
            gender_key,
            self.voice_design_test_sentences.get(
                "neutral",
                self.voice_design_test_sentences.get(
                    "other", "This is a fallback test sentence to ensure adequate duration for voice design references."
                ),
            ),
        )

        importance = getattr(character, "importance", "minor")
        if not character.test_sentence:
            if importance == "minor":
                return fallback
            raise ValueError(
                f"Major voice '{char_id}' has no usable source-backed "
                "reference text; choose dialogue before bootstrapping"
            )

        # Pad short sentences to ensure VoiceDesign has enough text to generate ~10s of audio
        # Only do this for minor characters, to avoid making all major characters sound too similar.
        if len(character.test_sentence.split()) < 12 and importance == "minor":
            return f"{character.test_sentence.strip()} {fallback}"

        return character.test_sentence

    @staticmethod
    def _candidate_id(voice_id: str, candidate_index: int) -> str:
        """Keep candidate one canonical; suffix only optional alternatives."""
        if candidate_index < 1:
            raise ValueError("candidate_index must start at 1")
        return voice_id if candidate_index == 1 else f"{voice_id}_cand{candidate_index}"

    @contextmanager
    def _voice_design_service(
        self,
        check_cancelled: Callable[[], None],
        *,
        round_label: str = "",
    ) -> Iterator[None]:
        """Run the VoiceDesign microservice for the duration of the block.

        VoiceDesign is a separate process on port 8101 rather than an in-process
        model, so its VRAM is fully released when the block exits. That is what
        makes a regeneration round affordable: the speaker encoder and the
        design model never need to be co-resident, they simply take turns.

        The block may be entered more than once per bootstrap -- once for the
        initial cast, then once per convergence round -- so nothing here may
        assume it runs exactly once.
        """
        import httpx

        logger.info(
            "Booting Qwen VoiceDesign Microservice on port 8101...%s",
            f" ({round_label})" if round_label else "",
        )
        python_exe = Path(sys.executable)

        # Resolved from the repository root, not the working directory. Started
        # from anywhere else these two were a crash and a stray log file.
        design_script = shared_paths.REPO_ROOT / "qwen_voice_design_server.py"
        log_path = shared_paths.REPO_ROOT / "qwen-voice-design.log"
        log_file = open(log_path, "a", encoding="utf-8")

        popen_kwargs: dict[str, Any] = {
            "stdout": log_file,
            "stderr": subprocess.STDOUT,
            "env": {
                **os.environ,
                "QWEN_VOICE_DESIGN_MODEL": self.voice_design_model,
            },
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            design_proc = subprocess.Popen(
                [str(python_exe), str(design_script)],
                **popen_kwargs,
            )
        except Exception:
            log_file.close()
            raise

        try:
            # Allow up to 10 minutes for a first-time model download.
            for _ in range(300):
                check_cancelled()
                if design_proc.poll() is not None:
                    raise RuntimeError(f"Qwen VoiceDesign Microservice exited during startup; see {log_path}")
                try:
                    resp = httpx.get("http://127.0.0.1:8101/health", timeout=2.0)
                    if resp.status_code == 200 and resp.json().get("model_loaded") is True:
                        logger.info("Qwen VoiceDesign Microservice is ready!")
                        break
                except Exception:
                    pass
                time.sleep(2)
            else:
                raise RuntimeError(f"Qwen VoiceDesign Microservice failed to start; see {log_path}")
            yield
        finally:
            logger.info("Shutting down Qwen VoiceDesign Microservice...")
            if design_proc.poll() is None:
                design_proc.terminate()
                try:
                    design_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    design_proc.kill()
                    design_proc.wait(timeout=10)
            log_file.close()

    def bootstrap_voices(
        self,
        request: BootstrapVoicesRequest,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> BootstrapVoicesResponse:
        """Generate voice reference clips for all characters in a project.

                For each character:
                1. Check if a voice already exists (skip if idempotent)
                2. Select a test sentence based on gender
        3. Use Qwen3-TTS VoiceDesign to generate a reference clip
                4. Transcribe reference clip with Whisper for Full ICL mode
                5. Save to the voice library

                Args:
                    request: Bootstrap request with project ID and characters.

                Returns:
                    Response with generated voice file paths.
        """
        project_id = request.project_id
        voices_generated: dict[str, BootstrapVoiceResult] = {}

        def emit(
            phase: str,
            completed: int,
            total: int,
            message: str,
        ) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(
                    {
                        "phase": phase,
                        "completed": max(0, int(completed)),
                        "total": max(1, int(total)),
                        "message": message,
                    }
                )
            except Exception:
                logger.warning("Voice bootstrap progress callback failed", exc_info=True)

        def check_cancelled() -> None:
            if cancel_check is not None and cancel_check():
                raise RuntimeError("Voice bootstrapping cancelled")

        candidate_total = sum(
            max(
                1,
                min(
                    3,
                    int(
                        request.candidate_counts.get(
                            char_id,
                            3 if char_id == "narrator" or getattr(character, "importance", "minor") == "major" else 1,
                        )
                    ),
                ),
            )
            for char_id, character in request.characters.items()
        )
        emit(
            "loading_voice_design",
            0,
            1,
            "Loading the voice-design model",
        )

        logger.info(
            "Bootstrapping %d voices for project '%s'",
            len(request.characters),
            project_id,
        )

        # Boot the dedicated VoiceDesign model only for this stage so its
        # VRAM can be released before the cloning model is loaded.
        with self._voice_design_service(check_cancelled):
            emit(
                "designing_references",
                0,
                candidate_total,
                f"Preparing {candidate_total} voice reference candidates",
            )

            designed_candidates = 0
            for char_id, character in request.characters.items():
                check_cancelled()
                importance = getattr(character, "importance", "minor")
                default_count = 3 if char_id == "narrator" or importance == "major" else 1
                num_candidates = max(
                    1,
                    min(3, int(request.candidate_counts.get(char_id, default_count))),
                )

                candidates = []
                for cand_idx in range(1, num_candidates + 1):
                    check_cancelled()
                    # Candidate one is the canonical, initially assigned profile.
                    # Additional candidates are optional alternatives.  Keeping a
                    # real registry entry under char_id makes a newly bootstrapped
                    # major voice immediately usable and approvable.
                    cand_id = self._candidate_id(char_id, cand_idx)

                    if not request.force_regenerate and self.library.voice_exists(project_id, cand_id):
                        existing = self.library.get_voice_info(project_id, cand_id)
                        if existing and existing.get("source_type") == "uploaded":
                            candidates.append(
                                VoiceCandidate(
                                    id=cand_id,
                                    file=existing.get("file", ""),
                                    duration_seconds=existing.get("duration_seconds", 0.0),
                                    sample_rate=existing.get("sample_rate", 24000),
                                )
                            )
                            designed_candidates += 1
                            emit(
                                "designing_references",
                                designed_candidates,
                                candidate_total,
                                f"Prepared {designed_candidates} of {candidate_total} voice candidates",
                            )
                            continue
                        expected_fingerprint = request.design_fingerprints.get(char_id, "")
                        fingerprint_matches = (
                            not expected_fingerprint
                            or existing
                            and existing.get("design_fingerprint") == expected_fingerprint
                        )
                        if existing and fingerprint_matches:
                            candidates.append(
                                VoiceCandidate(
                                    id=cand_id,
                                    file=existing.get("file", ""),
                                    duration_seconds=existing.get("duration_seconds", 0.0),
                                    sample_rate=existing.get("sample_rate", 24000),
                                )
                            )
                            designed_candidates += 1
                            emit(
                                "designing_references",
                                designed_candidates,
                                candidate_total,
                                f"Prepared {designed_candidates} of {candidate_total} voice candidates",
                            )
                            continue

                    result = self._generate_voice(
                        project_id,
                        cand_id,
                        character,
                        design_fingerprint=request.design_fingerprints.get(char_id, ""),
                    )

                    for redesign_attempt in range(1, self.acoustic_regeneration_attempts + 1):
                        _, early_warnings = self._acoustic_diagnostics(Path(result.file), character)
                        if not any("requested adult" in warning for warning in early_warnings):
                            break
                        gender_key = (
                            character.gender.value if isinstance(character.gender, Gender) else str(character.gender)
                        ).lower()
                        correction = (
                            " Make the perceived vocal register unmistakably "
                            f"{gender_key} and consistent with the stated age; "
                            "preserve natural speech and do not caricature it."
                        )
                        self.library.delete_voice(project_id, cand_id)
                        corrected_character = character.model_copy(
                            update={"voice_description": character.voice_description + correction}
                        )
                        result = self._generate_voice(
                            project_id,
                            cand_id,
                            corrected_character,
                            design_fingerprint=request.design_fingerprints.get(char_id, ""),
                        )
                        result.warnings.append("Automatically redesigned once after an acoustic register mismatch.")

                    candidates.append(result)
                    designed_candidates += 1
                    emit(
                        "designing_references",
                        designed_candidates,
                        candidate_total,
                        f"Prepared {designed_candidates} of {candidate_total} voice candidates",
                    )

                voices_generated[char_id] = BootstrapVoiceResult(
                    id=char_id,
                    file=candidates[0].file,
                    duration_seconds=candidates[0].duration_seconds,
                    sample_rate=candidates[0].sample_rate,
                    transcription_wer=candidates[0].transcription_wer,
                    acoustic_metrics=candidates[0].acoustic_metrics,
                    warnings=candidates[0].warnings,
                    candidates=candidates,
                )

        # Validate only after VoiceDesign has released VRAM.
        if self.validator:
            validation_total = sum(len(result.candidates) for result in voices_generated.values())
            validation_completed = 0
            emit(
                "validating_transcripts",
                0,
                validation_total,
                "Checking reference transcripts",
            )
            for char_id, result in voices_generated.items():
                check_cancelled()
                character = request.characters[char_id]
                expected = self._build_test_sentence(char_id, character)

                validated_candidates: list[VoiceCandidate] = []
                for candidate_index, candidate in enumerate(result.candidates):
                    check_cancelled()
                    transcribed = self.validator.transcribe(candidate.file)
                    wer = self.validator.calculate_wer(expected, transcribed)
                    candidate.transcription_wer = float(wer)
                    logger.info(
                        "Reference check for '%s': WER=%.3f transcript=%r",
                        candidate.id,
                        wer,
                        transcribed[:60],
                    )
                    if wer > self.wer_threshold:
                        logger.warning(
                            "Reference candidate '%s' has elevated transcript WER=%.3f (threshold=%.2f); retaining for Voice Review",
                            candidate.id,
                            wer,
                            self.wer_threshold,
                        )
                        candidate.warnings.append(
                            f"Reference transcript WER {wer:.2f} exceeded threshold ({self.wer_threshold:.2f}); manual audition and approval required in Voice Review."
                        )
                    validated_candidates.append(candidate)
                    validation_completed += 1
                    emit(
                        "validating_transcripts",
                        validation_completed,
                        validation_total,
                        f"Checked {validation_completed} of {validation_total} reference transcripts",
                    )
                result.candidates = validated_candidates
                if not validated_candidates:
                    continue
                result.file = validated_candidates[0].file
                result.transcription_wer = result.candidates[0].transcription_wer
            # Do not make chapter-scale TTS/Whisper co-residency an implicit
            # requirement. Release Whisper before loading the speaker encoder.
            self.validator.unload()

        embeddings: dict[str, Any] = {}
        acoustic_total = sum(len(result.candidates) for result in voices_generated.values())
        acoustic_completed = 0
        emit(
            "measuring_references",
            0,
            acoustic_total,
            "Measuring reference audio quality",
        )
        for char_id, result in voices_generated.items():
            check_cancelled()
            character = request.characters[char_id]
            for cand in result.candidates:
                check_cancelled()
                metrics, warnings = self._acoustic_diagnostics(Path(cand.file), character)
                cand.acoustic_metrics = metrics
                cand.warnings.extend(warnings)
                acoustic_completed += 1
                emit(
                    "measuring_references",
                    acoustic_completed,
                    acoustic_total,
                    f"Measured {acoustic_completed} of {acoustic_total} references",
                )
            if result.candidates:
                result.acoustic_metrics = result.candidates[0].acoustic_metrics
                result.warnings = list(result.candidates[0].warnings)
            try:
                embeddings[char_id] = self.engine.speaker_embedding(result.file)
            except Exception as exc:
                logger.warning(
                    "Could not extract cast embedding for '%s': %s",
                    char_id,
                    exc,
                )
                result.warnings.append("Acoustic distinctness could not be checked.")

        cast_diagnostics, collisions = self._compare_cast(embeddings, voices_generated, emit, check_cancelled)
        cast_diagnostics, convergence_rounds = self._converge_distinctness(
            request,
            voices_generated,
            embeddings,
            cast_diagnostics,
            collisions,
            emit,
            check_cancelled,
        )

        emit(
            "complete",
            1,
            1,
            "Voice references are ready for review",
        )
        return BootstrapVoicesResponse(
            status="success",
            project_id=project_id,
            voices_generated=voices_generated,
            cast_diagnostics=cast_diagnostics,
            distinctness_rounds=convergence_rounds,
        )

    def _converge_distinctness(
        self,
        request: BootstrapVoicesRequest,
        voices_generated: dict[str, BootstrapVoiceResult],
        embeddings: dict[str, Any],
        cast_diagnostics: list[CastPairDiagnostic],
        collisions: dict[str, set[str]],
        emit: Callable[[str, int, int, str], None],
        check_cancelled: Callable[[], None],
    ) -> tuple[list[CastPairDiagnostic], list[dict[str, Any]]]:
        """Redesign colliding voices until they separate, or the rounds run out.

        Comparison used to be the end of the road: VoiceDesign had already been
        shut down to free VRAM for the speaker encoder, so a collision could be
        reported but never repaired. On a 52-character cast that left 22
        flagged pairs for a human to resolve by hand, one of them a character
        measured at 0.992 speaker similarity against the narrator.

        Because VoiceDesign is a subprocess, the two models can take turns
        instead of being co-resident. Each round re-boots it, redesigns only the
        colliding voices with a contrast brief naming who they collided with,
        re-embeds just those, and re-measures.

        Bounded by ``distinctness_rounds``: whatever is still colliding when the
        rounds run out is surfaced as a warning, exactly as before. Returns the
        final diagnostics and a per-round scoreboard.
        """
        convergence_rounds: list[dict[str, Any]] = []
        for round_index in range(1, self.distinctness_rounds + 1):
            if not collisions:
                break
            check_cancelled()

            before = self._collision_summary(cast_diagnostics)
            logger.info(
                "Distinctness round %d/%d: %d voice(s) collide, worst pair %.3f",
                round_index,
                self.distinctness_rounds,
                len(collisions),
                before["max_similarity"],
            )
            emit(
                "redesigning_cast",
                round_index - 1,
                self.distinctness_rounds,
                (
                    f"Redesigning {len(collisions)} voice(s) for distinctness "
                    f"(round {round_index} of {self.distinctness_rounds})"
                ),
            )

            regenerated = self._redesign_for_distinctness(
                request,
                collisions,
                voices_generated,
                emit,
                check_cancelled,
                round_index=round_index,
            )
            if not regenerated:
                logger.info(
                    "Distinctness round %d produced no new references; stopping",
                    round_index,
                )
                break

            # Re-embed only what changed; the rest of the cast is untouched.
            for char_id in regenerated:
                check_cancelled()
                try:
                    embeddings[char_id] = self.engine.speaker_embedding(voices_generated[char_id].file)
                except Exception as exc:
                    logger.warning(
                        "Could not re-extract cast embedding for '%s': %s",
                        char_id,
                        exc,
                    )

            cast_diagnostics, collisions = self._compare_cast(embeddings, voices_generated, emit, check_cancelled)
            after = self._collision_summary(cast_diagnostics)
            convergence_rounds.append(
                {
                    "round": round_index,
                    "redesigned": sorted(regenerated),
                    "similar_pairs_before": before["similar_pairs"],
                    "similar_pairs_after": after["similar_pairs"],
                    "max_similarity_before": before["max_similarity"],
                    "max_similarity_after": after["max_similarity"],
                }
            )
            logger.info(
                "Distinctness round %d complete: similar pairs %d -> %d, worst similarity %.3f -> %.3f",
                round_index,
                before["similar_pairs"],
                after["similar_pairs"],
                before["max_similarity"],
                after["max_similarity"],
            )

        if collisions:
            logger.warning(
                "%d voice(s) remain acoustically similar after %d round(s); "
                "surfacing for manual redesign in Voice Review",
                len(collisions),
                len(convergence_rounds),
            )

        # Warnings are attached once, against the final measurement, so a voice
        # repaired in round 1 does not carry its round-0 collision text.
        self._apply_similarity_warnings(cast_diagnostics, voices_generated)

        # Transcript-check whatever the rounds replaced. The initial pass ran
        # before any redesign existed, so without this a regenerated reference
        # would ship unchecked -- and a contrast brief that pushes pitch and
        # rate is exactly the kind of change that can hurt intelligibility.
        # Done once at the end rather than per round: one Whisper load, and
        # only when something was actually redesigned.
        redesigned = {char_id for entry in convergence_rounds for char_id in entry["redesigned"]}
        if redesigned and self.validator:
            self._revalidate_transcripts(request, redesigned, voices_generated)

        return cast_diagnostics, convergence_rounds

    def _revalidate_transcripts(
        self,
        request: BootstrapVoicesRequest,
        char_ids: set[str],
        voices_generated: dict[str, BootstrapVoiceResult],
    ) -> None:
        """Re-run the WER check over references replaced by a redesign round."""
        try:
            for char_id in sorted(char_ids):
                result = voices_generated.get(char_id)
                if result is None or not result.candidates:
                    continue
                candidate = result.candidates[0]
                expected = self._build_test_sentence(char_id, request.characters[char_id])
                transcribed = self.validator.transcribe(candidate.file)
                wer = float(self.validator.calculate_wer(expected, transcribed))
                candidate.transcription_wer = wer
                result.transcription_wer = wer
                if wer > self.wer_threshold:
                    logger.warning(
                        "Redesigned reference '%s' has elevated transcript "
                        "WER=%.3f (threshold=%.2f); retaining for Voice Review",
                        char_id,
                        wer,
                        self.wer_threshold,
                    )
                    message = (
                        f"Reference transcript WER {wer:.2f} exceeded threshold "
                        f"({self.wer_threshold:.2f}) after distinctness "
                        "redesign; manual audition and approval required in "
                        "Voice Review."
                    )
                    candidate.warnings.append(message)
                    result.warnings.append(message)
        except Exception:
            # Never fail bootstrap on the transcript check -- the same contract
            # the initial validation pass follows. The audio is kept and the
            # operator reviews it.
            logger.warning(
                "Transcript re-check after distinctness redesign failed",
                exc_info=True,
            )
        finally:
            try:
                self.validator.unload()
            except Exception:
                logger.debug("Validator unload after re-check failed", exc_info=True)

    def _compare_cast(
        self,
        embeddings: dict[str, Any],
        voices_generated: dict[str, BootstrapVoiceResult],
        emit: Callable[[str, int, int, str], None],
        check_cancelled: Callable[[], None],
    ) -> tuple[list[CastPairDiagnostic], dict[str, set[str]]]:
        """Measure every cast pair and report which voices collide.

        Returns the full diagnostic list plus an adjacency map of voice id to
        the ids it was judged too similar to. The map is what a redesign round
        needs: it names the specific voices a contrast direction must move
        away from, rather than asking the model to be vaguely "more distinct".
        """
        cast_diagnostics: list[CastPairDiagnostic] = []
        collisions: dict[str, set[str]] = {}
        voice_ids = list(embeddings)
        pair_total = len(voice_ids) * max(0, len(voice_ids) - 1) // 2
        pair_completed = 0
        emit(
            "comparing_cast",
            0,
            max(1, pair_total),
            "Comparing cast distinctness",
        )
        for index, left_id in enumerate(voice_ids):
            for right_id in voice_ids[index + 1 :]:
                check_cancelled()
                similarity = self.engine.embedding_similarity(
                    embeddings[left_id],
                    embeddings[right_id],
                )
                left_v = voices_generated[left_id]
                right_v = voices_generated[right_id]
                diagnostic = self._cast_pair_diagnostic(
                    left_id,
                    right_id,
                    similarity,
                    left_v.acoustic_metrics or {},
                    right_v.acoustic_metrics or {},
                    self.similarity_warning_threshold,
                )
                cast_diagnostics.append(diagnostic)
                pair_completed += 1
                if pair_completed % 25 == 0 or pair_completed == pair_total:
                    emit(
                        "comparing_cast",
                        pair_completed,
                        max(1, pair_total),
                        f"Compared {pair_completed} of {pair_total} cast pairs",
                    )
                if diagnostic.status == "similar":
                    collisions.setdefault(left_id, set()).add(right_id)
                    collisions.setdefault(right_id, set()).add(left_id)
                    logger.warning(
                        "Cast voices '%s' and '%s' are acoustically similar: %.3f",
                        left_id,
                        right_id,
                        similarity,
                    )
        return cast_diagnostics, collisions

    @staticmethod
    def _collision_summary(
        cast_diagnostics: list[CastPairDiagnostic],
    ) -> dict[str, Any]:
        """Round-over-round scoreboard: how many pairs collide, and how badly."""
        similar = [d for d in cast_diagnostics if d.status == "similar"]
        return {
            "similar_pairs": len(similar),
            # Worst similarity among pairs that actually count as collisions.
            # Reporting the max across *all* pairs would be dominated by pairs
            # that are objectively contrasted and therefore suppressed, hiding
            # whether the rounds are helping.
            "max_similarity": max((float(d.speaker_similarity) for d in similar), default=0.0),
        }

    @staticmethod
    def _apply_similarity_warnings(
        cast_diagnostics: list[CastPairDiagnostic],
        voices_generated: dict[str, BootstrapVoiceResult],
    ) -> None:
        """Attach collision warnings from the final measurement only.

        Applied once at the end rather than during each comparison pass, so a
        voice that was repaired mid-run does not keep the warning text from a
        measurement that no longer describes it.
        """
        stale = "Sounds very similar to "
        for result in voices_generated.values():
            result.warnings = [w for w in result.warnings if not w.startswith(stale)]
        for diagnostic in cast_diagnostics:
            if diagnostic.status != "similar":
                continue
            left, right = diagnostic.left_voice_id, diagnostic.right_voice_id
            similarity = float(diagnostic.speaker_similarity)
            if left in voices_generated:
                voices_generated[left].warnings.append(f"{stale}{right} (speaker similarity {similarity:.3f}).")
            if right in voices_generated:
                voices_generated[right].warnings.append(f"{stale}{left} (speaker similarity {similarity:.3f}).")

    def _redesign_for_distinctness(
        self,
        request: BootstrapVoicesRequest,
        collisions: dict[str, set[str]],
        voices_generated: dict[str, BootstrapVoiceResult],
        emit: Callable[[str, int, int, str], None],
        check_cancelled: Callable[[], None],
        *,
        round_index: int,
    ) -> set[str]:
        """Redesign the colliding voices once, with a targeted contrast brief.

        Only the canonical candidate is regenerated. Alternatives are left
        alone: they exist for a human to audition, and replacing them would
        discard a choice the operator may already have made.

        Returns the ids that were actually regenerated.
        """
        targets = [char_id for char_id in collisions if char_id in request.characters]
        if not targets:
            return set()

        regenerated: set[str] = set()
        with self._voice_design_service(check_cancelled, round_label=f"distinctness round {round_index}"):
            for position, char_id in enumerate(targets, start=1):
                check_cancelled()
                character = request.characters[char_id]
                collided_with = ", ".join(sorted(collisions[char_id]))
                contrast = (
                    " This voice was measured as acoustically too close to "
                    f"{collided_with}. Move it clearly away from them: choose a "
                    "different pitch centre, a different speaking rate and a "
                    "different timbre, while keeping the stated age, gender and "
                    "character intent intact. Do not caricature the voice."
                )
                contrasted = character.model_copy(update={"voice_description": character.voice_description + contrast})
                cand_id = self._candidate_id(char_id, 1)
                try:
                    self.library.delete_voice(request.project_id, cand_id)
                    result = self._generate_voice(
                        request.project_id,
                        cand_id,
                        contrasted,
                        design_fingerprint=request.design_fingerprints.get(char_id, ""),
                    )
                except Exception as exc:
                    # A failed redesign must not lose the voice we already had.
                    logger.warning("Distinctness redesign failed for '%s': %s", char_id, exc)
                    continue

                result.warnings.append(
                    f"Automatically redesigned in distinctness round {round_index} to separate it from {collided_with}."
                )
                existing = voices_generated[char_id]
                if existing.candidates:
                    existing.candidates[0] = result
                else:
                    existing.candidates = [result]
                existing.file = result.file
                existing.duration_seconds = result.duration_seconds
                existing.sample_rate = result.sample_rate
                existing.transcription_wer = result.transcription_wer

                metrics, acoustic_warnings = self._acoustic_diagnostics(Path(result.file), contrasted)
                result.acoustic_metrics = metrics
                result.warnings.extend(acoustic_warnings)
                existing.acoustic_metrics = metrics
                existing.warnings = list(result.warnings)

                regenerated.add(char_id)
                emit(
                    "redesigning_cast",
                    position,
                    len(targets),
                    f"Redesigned {position} of {len(targets)} colliding voices",
                )
        return regenerated

    @staticmethod
    def _cast_pair_diagnostic(
        left_id: str,
        right_id: str,
        speaker_similarity: float,
        left_metrics: dict[str, float],
        right_metrics: dict[str, float],
        warning_threshold: float,
    ) -> CastPairDiagnostic:
        """Combine identity, pitch, and spectral evidence without hard-failing."""
        left_f0 = float(left_metrics.get("median_f0_hz", 0.0) or 0.0)
        right_f0 = float(right_metrics.get("median_f0_hz", 0.0) or 0.0)
        pitch_delta = abs(left_f0 - right_f0) if left_f0 and right_f0 else 0.0
        left_range = float(left_metrics.get("f0_range_hz", 0.0) or 0.0)
        right_range = float(right_metrics.get("f0_range_hz", 0.0) or 0.0)
        range_delta = abs(left_range - right_range)
        left_centroid = float(left_metrics.get("spectral_centroid_hz", 0.0) or 0.0)
        right_centroid = float(right_metrics.get("spectral_centroid_hz", 0.0) or 0.0)
        centroid_delta = abs(left_centroid - right_centroid)

        pitch_similarity = 1.0 - min(pitch_delta / 180.0, 1.0)
        range_similarity = 1.0 - min(range_delta / 160.0, 1.0)
        spectral_similarity = 1.0 - min(centroid_delta / 2500.0, 1.0)
        composite = (
            0.75 * float(speaker_similarity)
            + 0.15 * pitch_similarity
            + 0.05 * range_similarity
            + 0.05 * spectral_similarity
        )
        composite = max(-1.0, min(1.0, composite))

        objectively_contrasted = pitch_delta >= 40.0 and centroid_delta >= 350.0
        similar = speaker_similarity >= warning_threshold and not objectively_contrasted
        return CastPairDiagnostic(
            left_voice_id=left_id,
            right_voice_id=right_id,
            speaker_similarity=float(speaker_similarity),
            composite_similarity=composite,
            pitch_delta_hz=pitch_delta,
            pitch_range_delta_hz=range_delta,
            spectral_centroid_delta_hz=centroid_delta,
            status="similar" if similar else "distinct",
            warning_suppressed=bool(speaker_similarity >= warning_threshold and objectively_contrasted),
            suppression_reason=(
                "speaker embedding is similar, but both pitch and spectral centroid are materially separated"
                if speaker_similarity >= warning_threshold and objectively_contrasted
                else None
            ),
        )

    @staticmethod
    def _acoustic_diagnostics(
        audio_path: Path,
        character: Character,
    ) -> tuple[dict[str, float], list[str]]:
        """Measure obvious audio/casting problems without making identity claims."""
        import librosa
        import numpy as np
        import soundfile as sf

        audio, sample_rate = sf.read(str(audio_path), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
        dc_offset = float(abs(np.mean(audio))) if audio.size else 0.0
        duration = float(len(audio) / sample_rate) if sample_rate else 0.0
        clipping_fraction = float(np.mean(np.abs(audio) >= 0.999)) if audio.size else 0.0
        silence_threshold = max(0.002, rms * 0.10)
        silence_ratio = float(np.mean(np.abs(audio) < silence_threshold)) if audio.size else 1.0
        warnings: list[str] = []
        voiced_f0: Any = np.array([], dtype=np.float32)
        if audio.size >= 2048:
            try:
                f0 = librosa.yin(
                    audio,
                    fmin=65.0,
                    fmax=500.0,
                    sr=sample_rate,
                )
                voiced_f0 = f0[np.isfinite(f0)]
            except Exception as exc:
                logger.debug("Pitch diagnostic failed for %s: %s", audio_path, exc)
        median_f0 = float(np.median(voiced_f0)) if voiced_f0.size else 0.0
        f0_p10 = float(np.percentile(voiced_f0, 10)) if voiced_f0.size else 0.0
        f0_p90 = float(np.percentile(voiced_f0, 90)) if voiced_f0.size else 0.0
        spectral_centroid = 0.0
        spectral_flatness = 0.0
        if audio.size >= 2048:
            try:
                spectral_centroid = float(np.median(librosa.feature.spectral_centroid(y=audio, sr=sample_rate)))
                spectral_flatness = float(np.median(librosa.feature.spectral_flatness(y=audio)))
            except Exception as exc:
                logger.debug("Spectral diagnostic failed for %s: %s", audio_path, exc)
        gender = (character.gender.value if isinstance(character.gender, Gender) else str(character.gender)).lower()
        age = character.age_range.lower()
        if "child" not in age and "young" not in age:
            if gender == "male" and median_f0 > 240.0:
                warnings.append("Pitch is unusually high for the requested adult male design; review this voice.")
            elif gender == "female" and 0.0 < median_f0 < 105.0:
                warnings.append("Pitch is unusually low for the requested adult female design; review this voice.")
        if peak >= 0.999:
            warnings.append("Reference preview reaches digital full scale; check clipping.")
        if rms < 0.005:
            warnings.append("Reference preview is unusually quiet.")
        if clipping_fraction > 0.001:
            warnings.append("Reference preview contains excessive clipped samples.")
        if dc_offset > 0.02:
            warnings.append("Reference preview has an unusual DC offset.")
        if silence_ratio > 0.60:
            warnings.append("Reference preview contains unusually much silence.")
        return {
            "duration_seconds": duration,
            "peak_amplitude": peak,
            "rms_amplitude": rms,
            "dc_offset": dc_offset,
            "clipping_fraction": clipping_fraction,
            "silence_ratio": silence_ratio,
            "median_f0_hz": median_f0,
            "f0_p10_hz": f0_p10,
            "f0_p90_hz": f0_p90,
            "f0_range_hz": max(0.0, f0_p90 - f0_p10),
            "spectral_centroid_hz": spectral_centroid,
            "spectral_flatness": spectral_flatness,
        }, warnings

    def regenerate_voice(
        self,
        project_id: str,
        character_id: str,
        character: Character,
    ) -> VoiceCandidate:
        """Force-regenerate a single character's voice."""
        logger.info("Regenerating voice for '%s' in project '%s'", character_id, project_id)
        response = self.bootstrap_voices(
            BootstrapVoicesRequest(
                project_id=project_id,
                characters={character_id: character},
                force_regenerate=True,
            )
        )
        return response.voices_generated[character_id]

    def _generate_voice(
        self,
        project_id: str,
        char_id: str,
        character: Character,
        design_fingerprint: str = "",
    ) -> VoiceCandidate:
        """Generate a single voice reference clip."""
        test_sentence = self._build_test_sentence(char_id, character)

        import uuid

        base_path = self.library.get_voice_path(project_id, char_id)
        output_path = base_path.with_name(f"{base_path.stem}_{uuid.uuid4().hex[:8]}{base_path.suffix}")

        logger.info(
            "Generating voice for '%s' (%s): %s",
            character.name,
            char_id,
            character.voice_description[:60],
        )

        import requests

        resp = requests.post(
            "http://127.0.0.1:8101/voices/design",
            json={
                "prompt": character.voice_description,
                "text": test_sentence,
                "output_path": str(output_path),
                "duration_seconds": self.voice_design_duration,
            },
            timeout=600,
        )

        if resp.status_code != 200:
            raise RuntimeError(f"Qwen VoiceDesign microservice failed: {resp.text}")

        # Try to read the file to get duration and sample rate
        import soundfile as sf

        audio, sr = sf.read(str(output_path))
        duration_seconds = len(audio) / sr

        # The synthesis input is the authoritative Full-ICL transcript.
        # Whisper validation is deferred until VoiceDesign has released VRAM.
        ref_text = test_sentence

        # Save to voice library registry
        gender_key = character.gender.value if hasattr(character.gender, "value") else str(character.gender)
        self.library.register_voice(
            project_id=project_id,
            character_id=char_id,
            name=character.name,
            description=character.voice_description,
            gender=gender_key,
            file_path=str(output_path),
            duration_seconds=duration_seconds,
            sample_rate=sr,
            ref_text=ref_text,
            design_fingerprint=design_fingerprint,
            source_type="generated",
        )

        return VoiceCandidate(
            id=char_id,
            file=str(output_path),
            duration_seconds=duration_seconds,
            sample_rate=sr,
        )
