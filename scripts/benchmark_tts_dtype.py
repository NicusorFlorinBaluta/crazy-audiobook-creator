"""Run a balanced, warmed float16-versus-bfloat16 TTS screening benchmark."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import soundfile as sf
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_support import (
    balanced_order,
    dependency_versions,
    git_identity,
    json_fingerprint,
    percentile,
    sha256_file,
    summarize_tts_runs,
)
from scripts.benchmark_tts_fixture import FIXTURES
from shared.artifacts import atomic_write_json
from shared.live_test_guard import add_model_opt_in
from voice.tts_server.qwen3_engine import Qwen3TTSEngine
from voice.tts_server.voice_library import VoiceLibraryManager
from voice.validator.whisper_validator import WhisperValidator

CONTROL_DTYPE = "float16"
CANDIDATE_DTYPE = "bfloat16"


def _session_order(repetitions: int, pattern: str) -> list[str]:
    if repetitions < 2:
        raise ValueError("dtype screening requires at least two sessions per mode")
    return balanced_order(repetitions, pattern)


def _mode_summary(runs: list[dict[str, Any]], loads: list[float]) -> dict[str, Any]:
    summary = summarize_tts_runs(runs)
    summary.update(
        {
            "sessions": len(loads),
            "load_seconds_p50": percentile(loads, 0.50),
            "load_seconds_p95": percentile(loads, 0.95),
            "total_measured_wall_seconds": sum(float(run["wall_seconds"]) for run in runs),
            "autoregressive_seconds": sum(
                float(run["engine_metrics"].get("autoregressive_generation_seconds", 0.0)) for run in runs
            ),
        }
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--voice", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument(
        "--fixtures",
        default="short_emphasis,ordinary_dialogue,long_narration",
    )
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--order", choices=("ABBA", "BAAB"), default="ABBA")
    parser.add_argument("--seed", type=int, default=20_000)
    add_model_opt_in(parser)
    args = parser.parse_args()
    if not args.allow_models:
        parser.error("--allow-models is required")
    try:
        order = _session_order(args.repetitions, args.order)
    except ValueError as exc:
        parser.error(str(exc))

    fixture_ids = [item.strip() for item in args.fixtures.split(",") if item.strip() in FIXTURES]
    if not fixture_ids:
        parser.error("at least one valid fixture is required")

    config_path = ROOT / "voice" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    tts = config.get("tts", {})
    validation = config.get("validation", {})
    library = VoiceLibraryManager(config.get("storage", {}).get("voice_library_dir", "voice_library"))
    info = library.get_voice_info(args.project, args.voice)
    if not info:
        raise SystemExit(f"Voice not found: {args.project}/{args.voice}")
    reference = Path(info["file"])
    if not reference.is_absolute():
        reference = library.get_voice_path(args.project, args.voice)
    if not reference.is_file():
        raise SystemExit(f"Voice reference is missing: {reference}")
    ref_text = str(info.get("ref_text", ""))

    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    modes = {"A": ("float16", CONTROL_DTYPE), "B": ("bfloat16", CANDIDATE_DTYPE)}
    report: dict[str, Any] = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "project": args.project,
        "source_control": git_identity(ROOT),
        "dependencies": dependency_versions(),
        "config_sha256": sha256_file(config_path),
        "fixture_manifest_sha256": json_fingerprint({name: FIXTURES[name] for name in fixture_ids}),
        "protocol": {
            "session_order": order,
            "warmup": "one unmeasured generation after every model load",
            "sessions_per_mode": args.repetitions,
            "paired_seeds": True,
            "validation_enabled": True,
            "screen_only": True,
        },
        "runtime": {
            "model": tts.get("model"),
            "device": tts.get("device"),
            "control_dtype": CONTROL_DTYPE,
            "candidate_dtype": CANDIDATE_DTYPE,
            "attention_implementation": tts.get("attn_implementation"),
            "validator_model": validation.get("whisper_model"),
            "validator_backend": validation.get("whisper_backend"),
        },
        "voice": {
            "id": args.voice,
            "reference_sha256": sha256_file(reference),
            "reference_text_sha256": hashlib.sha256(ref_text.encode("utf-8")).hexdigest(),
        },
        "fixtures": [
            {
                "id": name,
                "characters": len(FIXTURES[name]),
                "words": len(FIXTURES[name].split()),
                "text_sha256": hashlib.sha256(FIXTURES[name].encode("utf-8")).hexdigest(),
            }
            for name in fixture_ids
        ],
        "sessions": [],
        "runs": [],
    }
    atomic_write_json(args.output_json, report)

    occurrences = {"A": 0, "B": 0}
    generation_config = copy.deepcopy(tts.get("generation", {}))
    for session_index, mode_label in enumerate(order, 1):
        occurrences[mode_label] += 1
        repetition = occurrences[mode_label]
        mode_name, dtype = modes[mode_label]
        engine = Qwen3TTSEngine(
            model_name=tts.get("model", "Qwen/Qwen3-TTS-12Hz-1.7B-Base"),
            device=tts.get("device", "cuda"),
            dtype=dtype,
            sample_rate=tts.get("sample_rate", 24000),
            generation_config=copy.deepcopy(generation_config),
            max_text_length=tts.get("max_text_length", 500),
            language=tts.get("language", "English"),
            attn_implementation=tts.get("attn_implementation", "sdpa"),
            post_processing_config=tts.get("post_processing", {}),
        )
        session: dict[str, Any] = {
            "index": session_index,
            "mode_label": mode_label,
            "mode": mode_name,
            "dtype": dtype,
            "repetition": repetition,
        }
        try:
            load_started = time.perf_counter()
            engine.load()
            session["load_seconds"] = time.perf_counter() - load_started
            warm_path = args.output_dir / f".warmup-{session_index}.wav"
            engine.generate_speech(
                text=FIXTURES[fixture_ids[0]],
                voice_reference_path=reference,
                ref_text=ref_text,
                emotion_instruction="neutral clear audiobook narration",
                output_path=warm_path,
            )
            session["warmup_metrics"] = dict(engine.last_generation_metrics)
            warm_path.unlink(missing_ok=True)

            for fixture_index, fixture_id in enumerate(fixture_ids, 1):
                seed = args.seed + repetition * 100 + fixture_index
                try:
                    import torch

                    torch.manual_seed(seed)
                    if torch.cuda.is_available():
                        torch.cuda.reset_peak_memory_stats()
                except (ImportError, RuntimeError):
                    pass
                output_path = args.output_dir / mode_name / f"session-{repetition}" / f"{fixture_id}.wav"
                output_path.parent.mkdir(parents=True, exist_ok=True)
                started = time.perf_counter()
                engine.generate_speech(
                    text=FIXTURES[fixture_id],
                    voice_reference_path=reference,
                    ref_text=ref_text,
                    emotion_instruction="neutral clear audiobook narration",
                    output_path=output_path,
                )
                wall_seconds = time.perf_counter() - started
                audio_seconds = float(sf.info(str(output_path)).duration)
                peak_memory_bytes = None
                try:
                    import torch

                    if torch.cuda.is_available():
                        peak_memory_bytes = int(torch.cuda.max_memory_allocated())
                except (ImportError, RuntimeError):
                    pass
                report["runs"].append(
                    {
                        "session": session_index,
                        "mode_label": mode_label,
                        "mode": mode_name,
                        "dtype": dtype,
                        "repetition": repetition,
                        "fixture": fixture_id,
                        "seed": seed,
                        "wall_seconds": wall_seconds,
                        "audio_seconds": audio_seconds,
                        "realtime_factor": wall_seconds / audio_seconds,
                        "speaker_similarity": float(engine.speaker_similarity(output_path, reference)),
                        "peak_memory_bytes": peak_memory_bytes,
                        "audio_sha256": sha256_file(output_path),
                        "relative_path": str(output_path.relative_to(args.output_dir)),
                        "engine_metrics": dict(engine.last_generation_metrics),
                    }
                )
                atomic_write_json(args.output_json, report)
        finally:
            engine.unload()
        report["sessions"].append(session)
        atomic_write_json(args.output_json, report)

    validator = WhisperValidator(
        model_name=validation.get("whisper_model", "large-v3"),
        device=validation.get("whisper_device", "auto"),
        backend=validation.get("whisper_backend", "openai_whisper"),
        vad_filter=False,
    )
    try:
        load_started = time.perf_counter()
        validator.load()
        report["whisper_load_seconds"] = time.perf_counter() - load_started
        for run in report["runs"]:
            transcript = validator.transcribe(str(args.output_dir / run["relative_path"]))
            run["wer"] = validator.calculate_wer(FIXTURES[run["fixture"]], transcript)
            run["transcript_sha256"] = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
            atomic_write_json(args.output_json, report)
    finally:
        validator.unload()

    summaries: dict[str, Any] = {}
    for mode_label, (mode_name, _) in modes.items():
        mode_runs = [run for run in report["runs"] if run["mode_label"] == mode_label]
        mode_loads = [
            float(session["load_seconds"]) for session in report["sessions"] if session["mode_label"] == mode_label
        ]
        summaries[mode_name] = _mode_summary(mode_runs, mode_loads)
    report["summary"] = summaries
    control = summaries["float16"]
    candidate = summaries["bfloat16"]
    rtf_change = candidate["rtf_p50"] / control["rtf_p50"] - 1.0
    quality_pass = bool(
        candidate["maximum_wer"] is not None
        and candidate["maximum_wer"] <= 0.20
        and candidate["average_wer"] <= control["average_wer"]
        and candidate["minimum_speaker_similarity"] >= control["minimum_speaker_similarity"] - 0.02
    )
    report["decision"] = {
        "rtf_change_fraction": rtf_change,
        "quality_gate_pass": quality_pass,
        "screen_speed_gate_pass": rtf_change <= -0.10,
        "advance_to_full_corpus": bool(quality_pass and rtf_change <= -0.10),
        "promote": False,
        "promotion_reason": "screen-only; a full multi-voice corpus is required",
    }
    atomic_write_json(args.output_json, report)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
