"""Qwen3-TTS Engine wrapper — Manages the TTS model lifecycle and generation.

Handles:
  - Model loading/unloading for VRAM management
  - Voice Design mode (text description → voice clip)
  - Voice Cloning mode (reference clip + text → speech)
  - Emotion instruction application
  - Speed/pacing control
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
import yaml
from voice.tts_server.audio_effects import AudioPostProcessor

logger = logging.getLogger(__name__)

# Increment when _MOOD_PROFILES keyword lists or deltas change so that cached
# segments that relied on old post-FX settings are re-generated.
MOOD_TIER_VERSION: str = "v2"
AUDIO_PROCESSING_REVISION: str = "clean-output-v1"

# Six-tier emotion → post-processing profile table.
# pitch_delta: semitones added/subtracted from any character-level base offset.
# tone: overrides VoiceFXSettings.tone only when the existing value is "neutral".
# speed_mult: multiplied with the script's per-line speed (compounds).
_MOOD_PROFILES: list[dict] = [
    {
        "tier": "intense",
        "keywords": (
            "angry", "panic", "urgent", "excited", "shout", "furious",
            "enraged", "terrified", "desperate", "demand",
        ),
        "pitch_delta": 0.40,
        "tone": "bright",
        "speed_mult": 1.04,
    },
    {
        "tier": "soft",
        "keywords": (
            "somber", "sad", "weary", "hushed", "whisper", "gentle",
            "tender", "comfort", "soothing", "quiet",
        ),
        "pitch_delta": -0.30,
        "tone": "warm",
        "speed_mult": 0.96,
    },
    {
        "tier": "playful",
        "keywords": (
            "chuckle", "banter", "sarcastic", "teasing", "amused",
            "playful", "wry", "ironic", "lighthearted",
        ),
        "pitch_delta": 0.20,
        "tone": "bright",
        "speed_mult": 1.02,
    },
    {
        "tier": "tense",
        "keywords": (
            "suspenseful", "nervous", "wary", "cautious", "dread",
            "anxious", "uneasy", "foreboding", "grim",
        ),
        "pitch_delta": 0.15,
        "tone": "neutral",
        "speed_mult": 0.98,
    },
    {
        "tier": "authoritative",
        "keywords": (
            "commanding", "stern", "decisive", "firm", "warning",
            "authoritative", "solemn", "grave", "resolute",
        ),
        "pitch_delta": -0.15,
        "tone": "neutral",
        "speed_mult": 0.97,
    },
    {
        "tier": "reflective",
        "keywords": (
            "contemplative", "nostalgic", "thoughtful", "pensive",
            "reflective", "melancholic", "wistful", "introspective",
        ),
        "pitch_delta": -0.10,
        "tone": "warm",
        "speed_mult": 0.95,
    },
]


def mood_tier_for(emotion: str | None) -> str:
    """Return the single post-FX mood tier used for an emotion label."""
    mood = (emotion or "").lower()
    for profile in _MOOD_PROFILES:
        if any(word in mood for word in profile["keywords"]):
            return str(profile["tier"])
    return "neutral"


class Qwen3TTSEngine:
    """Wrapper around Qwen3-TTS 1.7B model for speech synthesis."""

    def __init__(
        self,
        model_name: str = "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        device: str = "cuda",
        dtype: str = "float16",
        sample_rate: int = 24000,
        embedding_store: Any | None = None,
        generation_config: dict[str, Any] | None = None,
        max_text_length: int = 500,
        language: str = "English",
        attn_implementation: str = "sdpa",
        post_processing_config: dict[str, Any] | None = None,
    ):
        self.model_name = model_name
        self.device = device
        self.dtype = dtype
        self.sample_rate = sample_rate
        self.post_processing_config = dict(post_processing_config or {})
        self.post_processing_enabled = bool(
            self.post_processing_config.get("enabled", False)
        )
        self.allow_phase_vocoder_fallback = bool(
            self.post_processing_config.get(
                "allow_phase_vocoder_fallback",
                False,
            )
        )
        self.fx = AudioPostProcessor(
            allow_phase_vocoder_fallback=self.allow_phase_vocoder_fallback
        )
        self.embedding_store = embedding_store
        self.generation_config = generation_config or {}
        self.max_text_length = max(100, int(max_text_length))
        self.language = language
        self.attn_implementation = attn_implementation

        self._model = None
        self._is_loaded = False
        self._load_time: float = 0.0
        self.last_generation_metrics: dict[str, Any] = {}
        self._last_part_generation_metrics: dict[str, Any] = {}
        self._last_prompt_cache_hit = False

    @property
    def is_loaded(self) -> bool:
        return self._is_loaded

    def load(self) -> None:
        """Load the Qwen3-TTS model into GPU memory.

        This downloads the model on first run if not cached.
        """
        if self._is_loaded:
            logger.info("Model already loaded")
            return

        logger.info("Loading %s to %s (dtype=%s)...", self.model_name, self.device, self.dtype)
        start = time.time()

        try:
            import torch
            from qwen_tts import Qwen3TTSModel

            # Determine torch dtype
            torch_dtype = {
                "float16": torch.float16,
                "bfloat16": torch.bfloat16,
                "float32": torch.float32,
            }.get(self.dtype, torch.float16)

            # Use snapshot_download to get local path
            from huggingface_hub import snapshot_download
            model_path = snapshot_download(repo_id=self.model_name, local_files_only=False)

            # Load model directly using qwen_tts with local path
            load_kwargs = {
                "device_map": self.device if self.device != "cpu" else "cpu",
                "dtype": torch_dtype,
                "attn_implementation": self.attn_implementation,
            }
            try:
                self._model = Qwen3TTSModel.from_pretrained(
                    model_path,
                    **load_kwargs,
                )
            except Exception:
                if self.attn_implementation == "eager":
                    raise
                logger.exception(
                    "Attention backend '%s' failed; falling back to eager",
                    self.attn_implementation,
                )
                self._model = None
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                load_kwargs["attn_implementation"] = "eager"
                self._model = Qwen3TTSModel.from_pretrained(
                    model_path,
                    **load_kwargs,
                )
                self.attn_implementation = "eager"

            self._is_loaded = True
            self._load_time = time.time() - start

            logger.info(
                "Model loaded in %.1fs (attention=%s)",
                self._load_time,
                self.attn_implementation,
            )

            # Log VRAM usage
            if self.device == "cuda":
                try:
                    vram_used = torch.cuda.memory_allocated() / 1e9
                    vram_total = torch.cuda.get_device_properties(0).total_mem / 1e9
                    logger.info("VRAM: %.1f / %.1f GB", vram_used, vram_total)
                except Exception:
                    pass

        except Exception as e:
            logger.error("Failed to load model: %s", e)
            self._is_loaded = False
            raise

    def unload(self) -> None:
        """Unload the model from GPU memory to free VRAM."""
        if not self._is_loaded:
            return

        logger.info("Unloading model from %s...", self.device)

        del self._model
        self._model = None
        self._is_loaded = False

        # Free CUDA cache
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

        logger.info("Model unloaded")

    def generate_speech(
        self,
        text: str,
        voice_reference_path: str | Path | None = None,
        ref_text: str = "",
        emotion_instruction: str = "",
        speed: float = 1.0,
        voice_fx: Any | None = None,
        output_path: str | Path | None = None,
    ) -> np.ndarray:
        """Generate speech audio for a script line.

        Uses the saved reference clip to clone the voice character. Optional
        pacing/tone post-processing is disabled by default because the previous
        phase-vocoder fallback caused echo-like artifacts.

        Args:
            text: Text to speak.
            voice_reference_path: Path to the character's voice reference .wav.
            ref_text: Reference text transcript for Full ICL mode.
            emotion_instruction: Mood label used for restrained post-processing.
            speed: Speed multiplier (0.8=slow, 1.0=normal, 1.2=fast).
            voice_fx: Optional VoiceFXSettings for pitch/tone processing.
            output_path: If provided, save the audio to this file.

        Returns:
            NumPy array of audio samples.
        """
        metrics: dict[str, Any] = {
            "schema_version": 1,
            "model_load_seconds": 0.0,
            "reference_prompt_seconds": 0.0,
            "autoregressive_generation_seconds": 0.0,
            "audio_decode_seconds": 0.0,
            "audio_concatenation_seconds": 0.0,
            "post_processing_seconds": 0.0,
            "wav_write_seconds": 0.0,
            "reference_prompt_cache_hits": 0,
            "reference_prompt_cache_misses": 0,
            "text_parts": 0,
            "cold_model_load": not self._is_loaded,
            "attention_implementation": self.attn_implementation,
        }
        self.last_generation_metrics = metrics
        generation_started = time.perf_counter()
        try:
            was_loaded = self._is_loaded
            load_started = time.perf_counter()
            try:
                self._ensure_loaded()
            finally:
                if not was_loaded:
                    metrics["model_load_seconds"] = (
                        time.perf_counter() - load_started
                    )
            metrics["attention_implementation"] = self.attn_implementation

            parts = self._split_tts_text(text)
            metrics["text_parts"] = len(parts)
            generated_parts: list[np.ndarray] = []
            for part in parts:
                self._last_part_generation_metrics = {}
                part_started = time.perf_counter()
                try:
                    generated_part = self._generate(
                        text=part,
                        voice_reference=str(voice_reference_path)
                        if voice_reference_path
                        else None,
                        ref_text=ref_text,
                    )
                finally:
                    part_elapsed = time.perf_counter() - part_started
                    part_metrics = dict(self._last_part_generation_metrics)
                    if not part_metrics:
                        part_metrics["autoregressive_generation_seconds"] = (
                            part_elapsed
                        )
                    for key in (
                        "reference_prompt_seconds",
                        "autoregressive_generation_seconds",
                        "audio_decode_seconds",
                    ):
                        metrics[key] += float(
                            part_metrics.get(key, 0.0) or 0.0
                        )
                    cache_hit = part_metrics.get("reference_prompt_cache_hit")
                    if cache_hit is True:
                        metrics["reference_prompt_cache_hits"] += 1
                    elif cache_hit is False:
                        metrics["reference_prompt_cache_misses"] += 1
                generated_parts.append(generated_part)

            concatenate_started = time.perf_counter()
            audio = (
                np.concatenate(generated_parts)
                if generated_parts
                else np.zeros(0, dtype=np.float32)
            )
            metrics["audio_concatenation_seconds"] = (
                time.perf_counter() - concatenate_started
            )

            post_started = time.perf_counter()
            effective_fx, matched_tier = self._effective_post_fx(
                voice_fx,
                speed=speed,
                emotion=emotion_instruction,
            )
            if self.post_processing_enabled and matched_tier != "neutral":
                logger.debug(
                    "Emotion post-FX: tier=%s pitch_delta=%.2f tone=%s speed=%.3f",
                    matched_tier,
                    effective_fx.pitch_semitones,
                    effective_fx.tone,
                    effective_fx.speed,
                )
            if (
                self.post_processing_enabled
                and self.fx
                and not effective_fx.is_identity()
            ):
                audio = self.fx.apply(
                    audio,
                    self.sample_rate,
                    effective_fx,
                    blend_override=0.0,
                )
            metrics["post_processing_seconds"] = (
                time.perf_counter() - post_started
            )

            if output_path:
                write_started = time.perf_counter()
                output_path = Path(output_path)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                self._write_audio_atomic(output_path, audio)
                metrics["wav_write_seconds"] = (
                    time.perf_counter() - write_started
                )

            return audio
        finally:
            metrics["total_seconds"] = time.perf_counter() - generation_started

    def post_processing_context(self) -> dict[str, Any]:
        """Return the audio-policy identity used by synthesis fingerprints."""
        return {
            "revision": AUDIO_PROCESSING_REVISION,
            "enabled": self.post_processing_enabled,
            "allow_phase_vocoder_fallback": self.allow_phase_vocoder_fallback,
        }

    def generate_speech_batch(
        self,
        batch_requests: list[dict[str, Any]],
    ) -> list[np.ndarray]:
        """Generate speech for multiple lines sequentially (batch mocked).
        
        Since qwen_tts does not natively support batching yet, we fallback
        to iterating over the items and calling generate_speech sequentially.
        """
        self._ensure_loaded()
        
        audios = []
        for req in batch_requests:
            try:
                audio = self.generate_speech(
                    text=req["text"],
                    voice_reference_path=req.get("voice_reference_path"),
                    ref_text=req.get("ref_text", ""),
                    emotion_instruction=req.get("emotion_instruction", ""),
                    speed=req.get("speed", 1.0),
                    voice_fx=req.get("voice_fx"),
                    output_path=req.get("output_path")
                )
                audios.append(audio)
            except Exception:
                logger.exception("Sequential TTS generation failed for batch item")
                raise
                
        return audios

    def _generate(
        self,
        text: str,
        voice_reference: str | None = None,
        ref_text: str = "",
    ) -> np.ndarray:
        """Internal generation method using qwen_tts."""
        part_metrics: dict[str, Any] = {}
        self._last_part_generation_metrics = part_metrics
        if not voice_reference:
            raise RuntimeError(
                "Qwen3-TTS Base generation requires a registered voice reference"
            )

        use_icl = bool(ref_text and ref_text.strip())
        x_vec_mode = not use_icl

        if x_vec_mode:
            logger.warning(
                "No ref_text available for %s — using x_vector_only_mode=True "
                "(quality/similarity may be reduced)",
                voice_reference,
            )
        else:
            logger.info(
                "Using Full ICL mode with ref_text (%d chars) for %s",
                len(ref_text),
                voice_reference,
            )

        prompt_started = time.perf_counter()
        try:
            clone_prompt = self._get_voice_clone_prompt(
                voice_reference,
                ref_text if use_icl else "",
                x_vec_mode,
            )
        finally:
            part_metrics["reference_prompt_seconds"] = (
                time.perf_counter() - prompt_started
            )
            part_metrics["reference_prompt_cache_hit"] = (
                self._last_prompt_cache_hit
            )
        generation_config = dict(self.generation_config)
        adaptive = generation_config.pop("adaptive_max_new_tokens", {}) or {}
        if adaptive.get("enabled", False):
            configured_cap = int(generation_config.get("max_new_tokens", 4096))
            adaptive_cap = int(adaptive.get("base_tokens", 256)) + int(
                len(text) * float(adaptive.get("tokens_per_character", 10.0))
            )
            adaptive_cap = max(
                int(adaptive.get("minimum_tokens", 512)),
                min(configured_cap, adaptive_cap),
            )
            generation_config["max_new_tokens"] = adaptive_cap
            logger.info(
                "Experimental adaptive decode cap: %d tokens for %d characters",
                adaptive_cap,
                len(text),
            )
        generation_started = time.perf_counter()
        try:
            wavs, _ = self._model.generate_voice_clone(
                text=text,
                language=self.language,
                voice_clone_prompt=clone_prompt,
                **generation_config,
            )
        finally:
            part_metrics["autoregressive_generation_seconds"] = (
                time.perf_counter() - generation_started
            )

        decode_started = time.perf_counter()
        try:
            audio = np.asarray(wavs[0], dtype=np.float32)

            # Preserve model dynamics. Only protect the file from numeric clipping;
            # chapter-level loudness is handled by the mastering stage.
            max_peak = np.max(np.abs(audio)) if len(audio) else 0.0
            if max_peak > 0.99:
                audio = audio * (0.99 / max_peak)
        finally:
            part_metrics["audio_decode_seconds"] = (
                time.perf_counter() - decode_started
            )

        return audio

    def _get_voice_clone_prompt(
        self,
        voice_reference: str,
        ref_text: str,
        x_vector_only_mode: bool,
    ) -> list[Any]:
        """Create or restore the complete Qwen clone prompt, not only an embedding."""
        from qwen_tts import VoiceClonePromptItem

        self._last_prompt_cache_hit = False
        cached = None
        if self.embedding_store:
            cached = self.embedding_store.get_voice_clone_prompt(
                voice_reference,
                ref_text,
                self.model_name,
            )
        if cached:
            self._last_prompt_cache_hit = True
            import torch

            items = []
            for item in cached:
                ref_code = item.get("ref_code")
                ref_embedding = item["ref_spk_embedding"]
                if torch.is_tensor(ref_code):
                    ref_code = ref_code.to(self.device)
                if torch.is_tensor(ref_embedding):
                    ref_embedding = ref_embedding.to(self.device)
                items.append(
                    VoiceClonePromptItem(
                        ref_code=ref_code,
                        ref_spk_embedding=ref_embedding,
                        x_vector_only_mode=bool(item["x_vector_only_mode"]),
                        icl_mode=bool(item["icl_mode"]),
                        ref_text=item.get("ref_text"),
                    )
                )
            return items

        items = self._model.create_voice_clone_prompt(
            ref_audio=voice_reference,
            ref_text=ref_text if not x_vector_only_mode else None,
            x_vector_only_mode=x_vector_only_mode,
        )
        if self.embedding_store:
            serializable = []
            for item in items:
                serializable.append(
                    {
                        "ref_code": item.ref_code.detach().cpu()
                        if item.ref_code is not None
                        else None,
                        "ref_spk_embedding": item.ref_spk_embedding.detach().cpu(),
                        "x_vector_only_mode": item.x_vector_only_mode,
                        "icl_mode": item.icl_mode,
                        "ref_text": item.ref_text,
                    }
                )
            self.embedding_store.save_voice_clone_prompt(
                voice_reference,
                ref_text,
                self.model_name,
                serializable,
            )
        return items

    def _split_tts_text(self, text: str) -> list[str]:
        """Split oversized input at sentence/whitespace boundaries."""
        remaining = text.strip()
        if not remaining:
            raise ValueError("Cannot synthesize empty text")
        parts: list[str] = []
        while len(remaining) > self.max_text_length:
            window = remaining[: self.max_text_length + 1]
            boundaries = [
                match.end()
                for match in re.finditer(r"[.!?…][\"'”’]?\s+|\s+", window)
            ]
            cut = boundaries[-1] if boundaries else self.max_text_length
            part = remaining[:cut].strip()
            if not part:
                cut = self.max_text_length
                part = remaining[:cut]
            parts.append(part)
            remaining = remaining[cut:].lstrip()
        if remaining:
            parts.append(remaining)
        return parts

    @staticmethod
    def _effective_post_fx(
        voice_fx: Any | None,
        *,
        speed: float,
        emotion: str,
    ) -> tuple[Any, str]:
        """Compute effective post-FX settings and the matched mood tier name.

        Returns:
            (effective_fx, matched_tier) where matched_tier is one of the
            _MOOD_PROFILES tier names, or "neutral" when no profile matches.
        """
        from shared.models import VoiceFXSettings

        if voice_fx is None:
            fx = VoiceFXSettings()
        else:
            fx = voice_fx.model_copy(deep=True)
        fx.speed = max(0.5, min(2.0, float(fx.speed) * float(speed)))

        matched_tier = mood_tier_for(emotion)
        for profile in _MOOD_PROFILES:
            if profile["tier"] == matched_tier:
                delta = profile["pitch_delta"]
                fx.pitch_semitones = max(-12.0, min(12.0, fx.pitch_semitones + delta))
                # Only apply the default speed_mult if the script speed is the default 1.0
                if speed == 1.0:
                    fx.speed = max(0.5, min(2.0, fx.speed * profile["speed_mult"]))
                if fx.tone == "neutral" and profile["tone"] != "neutral":
                    fx.tone = profile["tone"]
                break

        return fx, matched_tier

    def _write_audio_atomic(self, output_path: Path, audio: np.ndarray) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{output_path.stem}.",
            suffix=".wav",
            dir=str(output_path.parent),
        )
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            sf.write(str(temporary), audio, self.sample_rate)
            try:
                os.replace(temporary, output_path)
            except OSError:
                import shutil
                with open(output_path, "wb") as dst, open(temporary, "rb") as src:
                    shutil.copyfileobj(src, dst)
        finally:
            temporary.unlink(missing_ok=True)

    def _ensure_loaded(self) -> None:
        """Ensure the model is loaded."""
        if not self._is_loaded:
            self.load()

    def get_vram_info(self) -> dict[str, float]:
        """Get current VRAM usage."""
        try:
            import torch
            if torch.cuda.is_available():
                return {
                    "vram_total_gb": torch.cuda.get_device_properties(0).total_memory / 1e9,
                    "vram_used_gb": torch.cuda.memory_allocated() / 1e9,
                    "vram_reserved_gb": torch.cuda.memory_reserved() / 1e9,
                    "vram_peak_allocated_gb": torch.cuda.max_memory_allocated() / 1e9,
                    "vram_peak_reserved_gb": torch.cuda.max_memory_reserved() / 1e9,
                }
        except ImportError:
            pass
        return {"vram_total_gb": 0.0, "vram_used_gb": 0.0}

    def get_gpu_name(self) -> str:
        """Get the GPU name."""
        try:
            import torch
            if torch.cuda.is_available():
                return torch.cuda.get_device_name(0)
        except ImportError:
            pass
        return "Unknown"

    def speaker_similarity(
        self,
        generated_audio_path: str | Path,
        reference_audio_path: str | Path,
    ) -> float:
        """Compare generated and reference audio with Qwen's speaker encoder."""
        import torch

        with torch.inference_mode():
            generated = self.speaker_embedding(generated_audio_path)
            reference = self.speaker_embedding(reference_audio_path)
            return self.embedding_similarity(generated, reference)

    def speaker_embedding(self, audio_path: str | Path):
        """Extract one reusable Qwen speaker embedding from an audio file."""
        import torch
        pt_path = Path(audio_path).with_suffix(".pt")
        if pt_path.exists():
            try:
                return (
                    torch.load(pt_path, map_location="cpu", weights_only=True)
                    .detach()
                    .float()
                    .flatten()
                    .cpu()
                )
            except Exception as e:
                logger.warning("Could not load cached embedding %s: %s", pt_path, e)
                
        self._ensure_loaded()
        import librosa

        target_rate = self._model.model.speaker_encoder_sample_rate
        audio, sample_rate = sf.read(str(audio_path), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if sample_rate != target_rate:
            audio = librosa.resample(
                audio,
                orig_sr=sample_rate,
                target_sr=target_rate,
            )
        emb = self._model.model.extract_speaker_embedding(
            audio=audio,
            sr=target_rate,
        ).detach().float().flatten().cpu()
        
        temp_path = pt_path.with_name(f".{pt_path.name}.tmp")
        try:
            torch.save(emb, temp_path)
            temp_path.replace(pt_path)
        except Exception as e:
            logger.warning("Could not save cached embedding %s: %s", pt_path, e)
            temp_path.unlink(missing_ok=True)
            
        return emb

    @staticmethod
    def embedding_similarity(left, right) -> float:
        """Return cosine similarity for two previously extracted embeddings."""
        import torch.nn.functional as functional

        # Cached embeddings are intentionally CPU-resident.  Normalize both
        # operands here as a defensive boundary for callers holding an older
        # accelerator tensor.
        left = left.detach().float().flatten().cpu()
        right = right.detach().float().flatten().cpu()
        return float(
            functional.cosine_similarity(
                left.unsqueeze(0),
                right.unsqueeze(0),
            ).item()
        )
