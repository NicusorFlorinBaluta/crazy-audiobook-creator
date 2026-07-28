"""Benchmark sequential model swapping versus TTS/Whisper co-residency.

This script is deliberately opt-in. It does not change application settings.
Run it on the same GPU/runtime as the Voice server, then use the JSON result to
decide whether keeping both models loaded is safe on that machine.
"""

from __future__ import annotations

import argparse
import gc
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from voice.tts_server.qwen3_engine import Qwen3TTSEngine
from voice.tts_server.voice_library import VoiceLibraryManager
from voice.validator.whisper_validator import WhisperValidator


def _gpu_snapshot(label: str) -> dict[str, float | str]:
    import torch

    if not torch.cuda.is_available():
        return {"label": label, "cuda_available": 0.0}
    torch.cuda.synchronize()
    free_bytes, total_bytes = torch.cuda.mem_get_info()
    return {
        "label": label,
        "free_gib": free_bytes / 1024**3,
        "total_gib": total_bytes / 1024**3,
        "torch_allocated_gib": torch.cuda.memory_allocated() / 1024**3,
        "torch_reserved_gib": torch.cuda.memory_reserved() / 1024**3,
    }


def _timed_generation(
    engine: Qwen3TTSEngine,
    *,
    text: str,
    reference: Path,
    reference_text: str,
    output: Path,
) -> float:
    started = time.perf_counter()
    engine.generate_speech(
        text=text,
        voice_reference_path=reference,
        ref_text=reference_text,
        emotion_instruction="neutral natural audiobook narration",
        output_path=output,
    )
    return time.perf_counter() - started


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="sample_book-3")
    parser.add_argument("--voice", default="narrator")
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()

    config = yaml.safe_load(Path("voice/config.yaml").read_text("utf-8")) or {}
    tts_cfg = config.get("tts", {})
    val_cfg = config.get("validation", {})
    library = VoiceLibraryManager(
        config.get("storage", {}).get("voice_library_dir", "voice_library")
    )
    voice = library.get_voice_info(args.project, args.voice)
    if not voice:
        raise SystemExit(
            f"Voice '{args.voice}' not found in project '{args.project}'"
        )
    reference = Path(voice["file"]).resolve()
    reference_text = voice.get("ref_text", "")
    text = (
        "The rain eased at last, and a quiet silver light crossed the valley."
    )
    engine = Qwen3TTSEngine(
        model_name=tts_cfg.get("model", "Qwen/Qwen3-TTS-12Hz-1.7B-Base"),
        device=tts_cfg.get("device", "cuda"),
        dtype=tts_cfg.get("dtype", "float16"),
        sample_rate=tts_cfg.get("sample_rate", 24000),
        generation_config=tts_cfg.get("generation", {}),
        max_text_length=tts_cfg.get("max_text_length", 500),
        language=tts_cfg.get("language", "English"),
        attn_implementation=tts_cfg.get("attn_implementation", "sdpa"),
    )
    whisper = WhisperValidator(
        model_name=val_cfg.get("whisper_model", "small"),
        device=val_cfg.get("whisper_device", "auto"),
    )
    report: dict[str, object] = {
        "project": args.project,
        "voice": args.voice,
        "repeats": max(1, args.repeats),
        "snapshots": [_gpu_snapshot("initial")],
    }
    temporary = tempfile.TemporaryDirectory(prefix="tts-whisper-benchmark-")
    output_dir = Path(temporary.name)
    try:
        engine.load()
        report["snapshots"].append(_gpu_snapshot("tts_loaded"))
        _timed_generation(
            engine,
            text=text,
            reference=reference,
            reference_text=reference_text,
            output=output_dir / "warmup.wav",
        )
        tts_only = [
            _timed_generation(
                engine,
                text=text,
                reference=reference,
                reference_text=reference_text,
                output=output_dir / f"tts-only-{index}.wav",
            )
            for index in range(max(1, args.repeats))
        ]
        report["snapshots"].append(_gpu_snapshot("tts_warm"))

        whisper.load()
        report["snapshots"].append(_gpu_snapshot("tts_and_whisper_loaded"))
        co_resident = [
            _timed_generation(
                engine,
                text=text,
                reference=reference,
                reference_text=reference_text,
                output=output_dir / f"co-resident-{index}.wav",
            )
            for index in range(max(1, args.repeats))
        ]
        transcription_started = time.perf_counter()
        transcript = whisper.transcribe(
            str(output_dir / "co-resident-0.wav")
        )
        transcription_seconds = time.perf_counter() - transcription_started
        final_snapshot = _gpu_snapshot("after_co_resident_work")
        report["snapshots"].append(final_snapshot)

        tts_only_median = statistics.median(tts_only)
        co_resident_median = statistics.median(co_resident)
        slowdown = (
            (co_resident_median / tts_only_median) - 1.0
            if tts_only_median > 0
            else 1.0
        )
        free_gib = float(final_snapshot.get("free_gib", 0.0))
        report.update(
            {
                "tts_only_seconds": tts_only,
                "co_resident_tts_seconds": co_resident,
                "tts_only_median_seconds": tts_only_median,
                "co_resident_median_seconds": co_resident_median,
                "co_resident_slowdown_fraction": slowdown,
                "whisper_seconds": transcription_seconds,
                "transcript": transcript,
                # Conservative gate: leave enough room for long utterances,
                # allocator fragmentation, and OS/display GPU use.
                "recommend_keep_resident": (
                    free_gib >= 4.0 and slowdown <= 0.15
                ),
                "decision_rule": (
                    "At least 4 GiB free after work and no more than 15% "
                    "median TTS slowdown; repeat with a long chapter before "
                    "changing the production default."
                ),
            }
        )
        print(json.dumps(report, indent=2))
        return 0
    finally:
        whisper.unload()
        engine.unload()
        del whisper
        del engine
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
        temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
