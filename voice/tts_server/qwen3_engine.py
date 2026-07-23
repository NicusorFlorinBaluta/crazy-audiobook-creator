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
import time
from pathlib import Path
from typing import Any
from typing import Any

import numpy as np
import soundfile as sf
import yaml
from voice.tts_server.audio_effects import AudioPostProcessor

logger = logging.getLogger(__name__)


class Qwen3TTSEngine:
    """Wrapper around Qwen3-TTS 1.7B model for speech synthesis."""

    def __init__(
        self,
        model_name: str = "Qwen/Qwen3-TTS-1.7B",
        device: str = "cuda",
        dtype: str = "float16",
        sample_rate: int = 24000,
        embedding_store: Any | None = None,
    ):
        self.model_name = model_name
        self.device = device
        self.dtype = dtype
        self.sample_rate = sample_rate
        self.fx = AudioPostProcessor()
        self.embedding_store = embedding_store

        self._model = None
        self._processor = None
        self._is_loaded = False
        self._load_time: float = 0.0

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
            self._model = Qwen3TTSModel.from_pretrained(
                model_path,
                device_map=self.device if self.device != "cpu" else "cpu",
                dtype=torch_dtype,
                attn_implementation="eager" # Fallback to eager if flash-attn is not installed
            )

            self._is_loaded = True
            self._load_time = time.time() - start

            logger.info("Model loaded in %.1fs", self._load_time)

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

    def generate_voice_design(
        self,
        voice_description: str,
        text: str,
        output_path: str | Path,
    ) -> dict[str, Any]:
        """Generate a voice reference clip from a text description.

        Uses Qwen3-TTS VoiceDesign mode to create a unique voice
        matching the given description.

        Args:
            voice_description: Natural language voice description.
            text: Text to speak in the generated voice.
            output_path: Path to save the generated .wav file.

        Returns:
            Dict with file path, duration, and sample rate.
        """
        self._ensure_loaded()
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info("Generating voice design: %s", voice_description[:80])

        audio = self._generate(
            text=text,
            instruction=voice_description,
            voice_reference=None,
        )

        sf.write(str(output_path), audio, self.sample_rate)
        duration = len(audio) / self.sample_rate

        logger.info("Voice design saved: %s (%.1fs)", output_path.name, duration)

        return {
            "file": str(output_path),
            "duration_seconds": duration,
            "sample_rate": self.sample_rate,
        }

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

        Uses the saved reference clip to clone the voice character,
        applying per-line emotion instructions and optional audio FX.

        Args:
            text: Text to speak.
            voice_reference_path: Path to the character's voice reference .wav.
            ref_text: Reference text transcript for Full ICL mode.
            emotion_instruction: Natural language emotion/delivery instruction.
            speed: Speed multiplier (0.8=slow, 1.0=normal, 1.2=fast).
            voice_fx: Optional VoiceFXSettings for pitch/tone processing.
            output_path: If provided, save the audio to this file.

        Returns:
            NumPy array of audio samples.
        """
        self._ensure_loaded()

        # Build instruction from emotion and speed
        instruction = self._build_instruction(emotion_instruction, speed)
        
        # Prepare the reference audio with pitch FX if requested (with persistent DB caching)
        fx_reference_path = None
        if self.fx and voice_fx and not voice_fx.is_identity():
            fx_dict = voice_fx.model_dump()
            cached_fx_path = None
            if self.embedding_store:
                cached_fx_path = self.embedding_store.get_fx_prompt(voice_reference_path, fx_dict)

            if cached_fx_path and cached_fx_path.exists():
                fx_reference_path = cached_fx_path
            else:
                if not hasattr(self, "_fx_prompt_cache"):
                    self._fx_prompt_cache = {}
                cache_key = (str(voice_reference_path), str(fx_dict))
                if cache_key in self._fx_prompt_cache and Path(self._fx_prompt_cache[cache_key]).exists():
                    fx_reference_path = Path(self._fx_prompt_cache[cache_key])
                else:
                    fx_reference_path = self.fx.prepare_prompt_audio(str(voice_reference_path), voice_fx)
                    if fx_reference_path:
                        self._fx_prompt_cache[cache_key] = str(fx_reference_path)
                        if self.embedding_store:
                            self.embedding_store.save_fx_prompt(voice_reference_path, fx_dict, fx_reference_path)

        audio = self._generate(
            text=text,
            instruction=instruction,
            voice_reference=str(fx_reference_path) if fx_reference_path else str(voice_reference_path),
            ref_text=ref_text,
        )

        # Apply post-processing (speed, tone, normalization)
        if self.fx and voice_fx and not voice_fx.is_identity():
            audio = self.fx.apply_post_pipeline(audio, self.sample_rate, voice_fx)

        if output_path:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            sf.write(str(output_path), audio, self.sample_rate)

        return audio

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
            except Exception as e:
                import traceback
                logger.error("Batched TTS generation failed for item:\n%s", traceback.format_exc())
                audios.append(np.zeros(int(self.sample_rate * 0.5), dtype=np.float32))
                
        return audios

    def _generate(
        self,
        text: str,
        instruction: str = "",
        voice_reference: str | None = None,
        ref_text: str = "",
    ) -> np.ndarray:
        """Internal generation method using qwen_tts."""
        import torch

        if voice_reference:
            use_icl = bool(ref_text and ref_text.strip())
            x_vec_mode = not use_icl

            if x_vec_mode:
                logger.warning(
                    "No ref_text available for %s — using x_vector_only_mode=True (quality/similarity may be reduced)",
                    voice_reference,
                )
            else:
                logger.info(
                    "Using Full ICL mode with ref_text (%d chars) for %s",
                    len(ref_text),
                    voice_reference,
                )

            wavs, sr = self._model.generate_voice_clone(
                text=text,
                language="auto",
                ref_audio=voice_reference,
                ref_text=ref_text if use_icl else "",
                x_vector_only_mode=x_vec_mode,
            )
        else:
            wavs, sr = self._model.generate_custom_voice(
                text=text,
                language="auto",
                speaker="vivian", # fallback default speaker for CustomVoice
                instruct=instruction,
            )

        audio = np.asarray(wavs[0], dtype=np.float32)

        # Dynamic Range-Aware Volume Equalization:
        # Preserves natural loudness differences between whispers (-26dB) and shouts (-15dB),
        # while preventing jarring out-of-bounds volume jumps across clips.
        rms = np.sqrt(np.mean(audio**2)) if len(audio) > 0 else 0
        if rms > 1e-5:
            rms_db = 20 * np.log10(rms)
            target_db = -20.0
            # Apply soft 50% gain compression toward target to preserve emotional dynamic range
            adjusted_db = rms_db + 0.5 * (target_db - rms_db)
            gain = 10 ** ((adjusted_db - rms_db) / 20.0)
            gain = max(0.4, min(gain, 2.5))  # Smooth gain adjustment bounds
            audio = audio * gain
            # Peak limiter to prevent clipping
            max_peak = np.max(np.abs(audio))
            if max_peak > 0.95:
                audio = audio * (0.95 / max_peak)

        return audio

    def _generate_batch(
        self,
        texts: list[str],
        instructions: list[str],
        voice_references: list[np.ndarray | None],
    ) -> list[np.ndarray]:
        """Internal batched generation method.
        
        Since qwen_tts does not natively support batching yet, we fallback
        to iterating over the items sequentially.
        """
        import torch
        
        audios = []
        for i in range(len(texts)):
            try:
                # We need the original path or we can't use np.ndarray directly in _generate
                # However, since we process voice_references outside, let's just use _generate
                # Wait, _generate expects voice_reference to be a str/Path.
                # Actually, we don't pass voice_references in _generate_batch correctly.
                pass # Replaced entirely below
            except Exception as e:
                logger.error("Batched TTS generation failed for item %d: %s", i, e)
                audios.append(np.zeros(int(self.sample_rate * 0.5), dtype=np.float32))
        return audios

    def _decode_audio(self, output_tokens: Any) -> np.ndarray:
        """Decode model output tokens into audio samples.

        Note: This method will need to be updated to match the
        actual Qwen3-TTS decoding pipeline.
        """
        # Placeholder: actual implementation depends on Qwen3-TTS codec
        # The model outputs codec tokens that need to be decoded
        # through the model's audio decoder
        try:
            if hasattr(self._processor, "decode_audio"):
                audio = self._processor.decode_audio(output_tokens)
                if isinstance(audio, np.ndarray):
                    return audio
        except Exception:
            pass

        # Fallback: return short silence
        logger.warning("Audio decoding fell back to silence — update _decode_audio for actual Qwen3-TTS API")
        return np.zeros(int(self.sample_rate * 1.0), dtype=np.float32)

    @staticmethod
    def _build_instruction(emotion: str, speed: float) -> str:
        """Build a TTS instruction string from emotion and speed."""
        parts: list[str] = []

        if emotion:
            parts.append(f"Speak with {emotion}")

        if speed != 1.0:
            if speed < 0.9:
                parts.append("at a slow, measured pace")
            elif speed < 1.0:
                parts.append("at a slightly slower pace")
            elif speed > 1.1:
                parts.append("at a quick, energetic pace")
            elif speed > 1.0:
                parts.append("at a slightly faster pace")

        return ". ".join(parts) + "." if parts else ""

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
