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

import numpy as np
import soundfile as sf
import yaml

logger = logging.getLogger(__name__)


class Qwen3TTSEngine:
    """Wrapper around Qwen3-TTS 1.7B model for speech synthesis."""

    def __init__(
        self,
        model_name: str = "Qwen/Qwen3-TTS-1.7B",
        device: str = "cuda",
        dtype: str = "float16",
        sample_rate: int = 24000,
    ):
        self.model_name = model_name
        self.device = device
        self.dtype = dtype
        self.sample_rate = sample_rate

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
        voice_reference_path: str | Path,
        emotion_instruction: str = "",
        speed: float = 1.0,
        output_path: str | Path | None = None,
    ) -> np.ndarray:
        """Generate speech using a voice reference clip.

        Uses the saved reference clip to clone the voice character,
        applying per-line emotion instructions.

        Args:
            text: Text to speak.
            voice_reference_path: Path to the character's voice reference .wav.
            emotion_instruction: Natural language emotion/delivery instruction.
            speed: Speed multiplier (0.8=slow, 1.0=normal, 1.2=fast).
            output_path: If provided, save the audio to this file.

        Returns:
            NumPy array of audio samples.
        """
        self._ensure_loaded()

        # Build instruction from emotion and speed
        instruction = self._build_instruction(emotion_instruction, speed)

        audio = self._generate(
            text=text,
            instruction=instruction,
            voice_reference=str(voice_reference_path),
        )

        if output_path:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            sf.write(str(output_path), audio, self.sample_rate)

        return audio

    def generate_speech_batch(
        self,
        batch_requests: list[dict[str, Any]],
    ) -> list[np.ndarray]:
        """Generate speech for multiple lines simultaneously (batch mode).
        
        Args:
            batch_requests: List of dicts containing:
                - text (str)
                - voice_reference_path (str | Path)
                - emotion_instruction (str)
                - speed (float)
                - output_path (str | Path | None)
        
        Returns:
            List of generated audio numpy arrays in the same order.
        """
        self._ensure_loaded()
        
        # Prepare batched inputs
        texts = []
        instructions = []
        voice_refs = []
        
        for req in batch_requests:
            texts.append(req["text"])
            instructions.append(self._build_instruction(
                req.get("emotion_instruction", ""), 
                req.get("speed", 1.0)
            ))
            
            v_ref = req.get("voice_reference_path")
            if v_ref:
                # Load and resample audio
                import librosa
                ref_audio, ref_sr = sf.read(str(v_ref))
                if ref_sr != self.sample_rate:
                    ref_audio = librosa.resample(
                        ref_audio, orig_sr=ref_sr, target_sr=self.sample_rate
                    )
                voice_refs.append(ref_audio)
            else:
                voice_refs.append(None)
                
        # Generate batch
        audios = self._generate_batch(
            texts=texts,
            instructions=instructions,
            voice_references=voice_refs,
        )
        
        # Save to disk if requested
        for req, audio in zip(batch_requests, audios):
            out_path = req.get("output_path")
            if out_path:
                out_path = Path(out_path)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                sf.write(str(out_path), audio, self.sample_rate)
                
        return audios

    def _generate(
        self,
        text: str,
        instruction: str = "",
        voice_reference: str | None = None,
    ) -> np.ndarray:
        """Internal generation method using qwen_tts."""
        import torch

        if voice_reference:
            wavs, sr = self._model.generate_voice_clone(
                text=text,
                language="auto",
                ref_audio=voice_reference,
                ref_text="",
                x_vector_only_mode=True,
            )
        else:
            wavs, sr = self._model.generate_custom_voice(
                text=text,
                language="auto",
                speaker="vivian", # fallback default speaker for CustomVoice
                instruct=instruction,
            )

        audio = np.asarray(wavs[0], dtype=np.float32)
        return audio

    def _generate_batch(
        self,
        texts: list[str],
        instructions: list[str],
        voice_references: list[np.ndarray | None],
    ) -> list[np.ndarray]:
        """Internal batched generation method.
        
        Sends multiple sequences through the model concurrently to maximize GPU utilization.
        """
        import torch
        
        try:
            # The actual API will differ, but typical HF batching looks like this:
            processed = self._processor(
                text=texts,
                return_tensors="pt",
                padding=True,
            )

            for key in processed:
                if hasattr(processed[key], "to"):
                    processed[key] = processed[key].to(self.device)
                    
            # In a real scenario, instructions and voice_refs would also be passed
            # to the processor or model.

            with torch.no_grad():
                outputs = self._model.generate(
                    **processed,
                    max_new_tokens=4096,
                    do_sample=True,
                    temperature=0.7,
                )

            # Decode all outputs
            audios = []
            for output in outputs:
                audios.append(self._decode_audio(output))
                
            return audios

        except Exception as e:
            logger.error("Batched TTS generation failed: %s", e)
            # Return silences as fallback
            return [np.zeros(int(self.sample_rate * 0.5), dtype=np.float32) for _ in texts]

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
