"""Controlled current-vs-adaptive TTS fixture with sequential validation."""

from __future__ import annotations

import argparse
import copy
import json
import statistics
import sys
import time
from pathlib import Path

import soundfile as sf
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.live_test_guard import add_model_opt_in
from voice.tts_server.qwen3_engine import Qwen3TTSEngine
from voice.tts_server.voice_library import VoiceLibraryManager
from voice.validator.whisper_validator import WhisperValidator


FIXTURES = {
    "short": "Wait—listen carefully before you open that door.",
    "repeated_name": "Tuka, Tuka, wait for Starling; Starling knows the safer path.",
    "long": (
        "The rain eased at last, and a quiet silver light crossed the valley, "
        "revealing the old road as it curved between dark pines and weathered "
        "stones. Far below, the river moved with a patient sound, while the "
        "travellers gathered their cloaks and continued toward the distant "
        "tower before the remaining daylight finally disappeared."
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--voice", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--fixtures", default="short,repeated_name,long")
    parser.add_argument(
        "--order", choices=("current-first", "adaptive-first"),
        default="current-first",
    )
    parser.add_argument("--skip-validation", action="store_true")
    add_model_opt_in(parser)
    args = parser.parse_args()
    if not args.allow_models:
        parser.error("--allow-models is required")

    config = yaml.safe_load((ROOT / "voice" / "config.yaml").read_text(encoding="utf-8")) or {}
    tts = config.get("tts", {})
    validation = config.get("validation", {})
    library = VoiceLibraryManager(config.get("storage", {}).get("voice_library_dir", "voice_library"))
    info = library.get_voice_info(args.project, args.voice)
    if not info:
        raise SystemExit(f"Voice not found: {args.project}/{args.voice}")
    reference = Path(info["file"])
    reference_text = info.get("ref_text", "")
    base_generation = copy.deepcopy(tts.get("generation", {}))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    engine = Qwen3TTSEngine(
        model_name=tts.get("model", "Qwen/Qwen3-TTS-12Hz-1.7B-Base"),
        device=tts.get("device", "cuda"), dtype=tts.get("dtype", "float16"),
        sample_rate=tts.get("sample_rate", 24000),
        generation_config=base_generation,
        max_text_length=tts.get("max_text_length", 500),
        language=tts.get("language", "English"),
        attn_implementation=tts.get("attn_implementation", "sdpa"),
    )
    report = {"schema_version": 1, "project": args.project, "voice": args.voice, "runs": []}
    try:
        started = time.perf_counter()
        engine.load()
        report["tts_load_seconds"] = time.perf_counter() - started
        selected_fixtures = [
            name.strip() for name in args.fixtures.split(",")
            if name.strip() in FIXTURES
        ]
        modes = (
            ("adaptive", "current")
            if args.order == "adaptive-first"
            else ("current", "adaptive")
        )
        report["order"] = args.order
        for fixture_index, fixture in enumerate(selected_fixtures, 1):
            text = FIXTURES[fixture]
            for mode in modes:
                generation = copy.deepcopy(base_generation)
                generation.setdefault("adaptive_max_new_tokens", {})["enabled"] = mode == "adaptive"
                engine.generation_config = generation
                try:
                    import torch
                    torch.manual_seed(10_000 + fixture_index)
                except ImportError:
                    pass
                path = args.output_dir / f"{fixture}-{mode}.wav"
                started = time.perf_counter()
                engine.generate_speech(
                    text=text, voice_reference_path=reference,
                    ref_text=reference_text,
                    emotion_instruction="neutral clear audiobook narration",
                    output_path=path,
                )
                wall = time.perf_counter() - started
                duration = float(sf.info(str(path)).duration)
                report["runs"].append({
                    "fixture": fixture, "mode": mode, "words": len(text.split()),
                    "wall_seconds": wall, "audio_seconds": duration,
                    "realtime_factor": wall / duration,
                    "speaker_similarity": float(engine.speaker_similarity(path, reference)),
                    "path": str(path),
                })
                args.output_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    finally:
        engine.unload()

    validator = WhisperValidator(
        model_name=validation.get("whisper_model", "large-v3"),
        device=validation.get("whisper_device", "auto"),
        backend=validation.get("whisper_backend", "openai_whisper"),
        vad_filter=False,
    )
    if not args.skip_validation:
        try:
            started = time.perf_counter()
            validator.load()
            report["whisper_load_seconds"] = time.perf_counter() - started
            for run in report["runs"]:
                text = FIXTURES[run["fixture"]]
                started = time.perf_counter()
                transcript = validator.transcribe(run["path"])
                run["validation_seconds"] = time.perf_counter() - started
                run["wer"] = validator.calculate_wer(text, transcript)
        finally:
            validator.unload()

    for mode in ("current", "adaptive"):
        runs = [run for run in report["runs"] if run["mode"] == mode]
        report[f"{mode}_median_rtf"] = statistics.median(run["realtime_factor"] for run in runs)
        if not args.skip_validation:
            report[f"{mode}_average_wer"] = sum(run["wer"] for run in runs) / len(runs)
    report["adaptive_rtf_change_fraction"] = (
        report["adaptive_median_rtf"] / report["current_median_rtf"] - 1.0
    )
    report["promote_adaptive"] = False if args.skip_validation else (
        report["adaptive_rtf_change_fraction"] <= -0.10
        and report["adaptive_average_wer"] <= report["current_average_wer"]
        and all(run["wer"] <= 0.20 for run in report["runs"] if run["mode"] == "adaptive")
    )
    args.output_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
