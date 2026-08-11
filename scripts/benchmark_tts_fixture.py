"""Run a warmed, balanced A/B TTS benchmark on immutable text fixtures."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import soundfile as sf
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.artifacts import atomic_write_json
from shared.live_test_guard import add_model_opt_in
from voice.tts_server.qwen3_engine import Qwen3TTSEngine
from voice.tts_server.voice_library import VoiceLibraryManager
from voice.validator.whisper_validator import WhisperValidator


FIXTURES = {
    "short_emphasis": "Wait--listen carefully before you open that door.",
    "repeated_name": (
        "Tuka, Tuka, wait for Starling; Starling knows the safer path."
    ),
    "ordinary_dialogue": (
        "I checked the western trail at dawn, and the bridge is still safe."
    ),
    "long_narration": (
        "The rain eased at last, and a quiet silver light crossed the valley, "
        "revealing the old road as it curved between dark pines and weathered "
        "stones. Far below, the river moved with a patient sound, while the "
        "travellers gathered their cloaks and continued toward the distant "
        "tower before the remaining daylight finally disappeared."
    ),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _git_identity() -> dict[str, Any]:
    def run(*args: str) -> str | None:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    status = run("status", "--porcelain", "--untracked-files=normal")
    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty": bool(status),
        "status": status.splitlines() if status else [],
    }


def _dependency_versions() -> dict[str, str | None]:
    result: dict[str, str | None] = {"python": sys.version.split()[0]}
    for package in ("torch", "qwen-tts", "soundfile", "openai-whisper"):
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = None
    return result


def _deep_update(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_update(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _balanced_order(repetitions: int, pattern: str) -> list[str]:
    """Return equal A/B counts while retaining an ABBA or BAAB order."""
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    pattern = pattern.upper()
    if pattern not in {"ABBA", "BAAB"}:
        raise ValueError("pattern must be ABBA or BAAB")
    target = {"A": repetitions, "B": repetitions}
    counts = {"A": 0, "B": 0}
    result: list[str] = []
    while counts != target:
        for item in pattern:
            if counts[item] >= target[item]:
                continue
            result.append(item)
            counts[item] += 1
    return result


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _summarize_mode(runs: list[dict[str, Any]]) -> dict[str, Any]:
    rtfs = [float(run["realtime_factor"]) for run in runs]
    walls = [float(run["wall_seconds"]) for run in runs]
    similarities = [
        float(run["speaker_similarity"])
        for run in runs
        if isinstance(run.get("speaker_similarity"), (int, float))
    ]
    wers = [
        float(run["wer"])
        for run in runs
        if isinstance(run.get("wer"), (int, float))
    ]
    return {
        "runs": len(runs),
        "rtf_p50": _percentile(rtfs, 0.50),
        "rtf_p90": _percentile(rtfs, 0.90),
        "rtf_p95": _percentile(rtfs, 0.95),
        "wall_seconds_p50": _percentile(walls, 0.50),
        "wall_seconds_p95": _percentile(walls, 0.95),
        "average_wer": statistics.fmean(wers) if wers else None,
        "maximum_wer": max(wers) if wers else None,
        "average_speaker_similarity": (
            statistics.fmean(similarities) if similarities else None
        ),
        "minimum_speaker_similarity": min(similarities) if similarities else None,
    }


def _candidate_patch(path: Path | None) -> dict[str, Any]:
    if path is not None:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("candidate generation patch must be a JSON object")
        return value
    return {"adaptive_max_new_tokens": {"enabled": True}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument(
        "--voices",
        required=True,
        help="Comma-separated voice IDs; include narrator and several characters",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument(
        "--fixtures",
        default=",".join(FIXTURES),
        help="Comma-separated immutable fixture IDs",
    )
    parser.add_argument("--candidate-name", default="candidate")
    parser.add_argument("--candidate-generation-json", type=Path)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--order", choices=("ABBA", "BAAB"), default="ABBA")
    parser.add_argument("--seed", type=int, default=10_000)
    parser.add_argument("--skip-validation", action="store_true")
    add_model_opt_in(parser)
    args = parser.parse_args()
    if not args.allow_models:
        parser.error("--allow-models is required")
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
    if (
        args.candidate_name == "control"
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", args.candidate_name)
    ):
        parser.error("--candidate-name must be a safe non-control identifier")

    config_path = ROOT / "voice" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    tts = config.get("tts", {})
    validation = config.get("validation", {})
    library = VoiceLibraryManager(
        config.get("storage", {}).get("voice_library_dir", "voice_library")
    )
    voice_ids = [item.strip() for item in args.voices.split(",") if item.strip()]
    fixture_ids = [
        item.strip()
        for item in args.fixtures.split(",")
        if item.strip() in FIXTURES
    ]
    if not voice_ids or not fixture_ids:
        parser.error("at least one valid voice and fixture are required")

    voices: dict[str, dict[str, Any]] = {}
    for voice_id in voice_ids:
        info = library.get_voice_info(args.project, voice_id)
        if not info:
            raise SystemExit(f"Voice not found: {args.project}/{voice_id}")
        reference = Path(info["file"])
        if not reference.is_absolute():
            reference = library.get_voice_path(args.project, voice_id)
        if not reference.is_file():
            raise SystemExit(f"Voice reference is missing: {reference}")
        voices[voice_id] = {
            "reference": reference,
            "ref_text": str(info.get("ref_text", "")),
        }

    base_generation = copy.deepcopy(tts.get("generation", {}))
    candidate_generation = _deep_update(
        base_generation,
        _candidate_patch(args.candidate_generation_json),
    )
    modes = {
        "A": ("control", base_generation),
        "B": (args.candidate_name, candidate_generation),
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema_version": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project": args.project,
        "source_control": _git_identity(),
        "dependencies": _dependency_versions(),
        "config_sha256": _sha256(config_path),
        "fixture_manifest_sha256": _json_fingerprint(
            {name: FIXTURES[name] for name in fixture_ids}
        ),
        "protocol": {
            "warmup": "one unmeasured control generation",
            "order": args.order,
            "repetitions_per_mode": args.repetitions,
            "seed": args.seed,
            "validation_enabled": not args.skip_validation,
        },
        "runtime": {
            "model": tts.get("model"),
            "device": tts.get("device"),
            "dtype": tts.get("dtype"),
            "sample_rate": tts.get("sample_rate"),
            "language": tts.get("language"),
            "attention_implementation": tts.get("attn_implementation"),
            "max_text_length": tts.get("max_text_length"),
            "post_processing": tts.get("post_processing", {}),
            "validator_model": validation.get("whisper_model"),
            "validator_backend": validation.get("whisper_backend"),
            "vad_filter": False,
        },
        "modes": {
            label: {
                "name": name,
                "generation_config": generation,
                "generation_config_sha256": _json_fingerprint(generation),
            }
            for label, (name, generation) in modes.items()
        },
        "fixtures": [
            {
                "id": name,
                "text_sha256": hashlib.sha256(
                    FIXTURES[name].encode("utf-8")
                ).hexdigest(),
                "characters": len(FIXTURES[name]),
                "words": len(FIXTURES[name].split()),
            }
            for name in fixture_ids
        ],
        "voices": [
            {
                "id": voice_id,
                "reference_sha256": _sha256(values["reference"]),
                "reference_text_sha256": hashlib.sha256(
                    values["ref_text"].encode("utf-8")
                ).hexdigest(),
            }
            for voice_id, values in voices.items()
        ],
        "runs": [],
    }
    atomic_write_json(args.output_json, report)

    engine = Qwen3TTSEngine(
        model_name=tts.get("model", "Qwen/Qwen3-TTS-12Hz-1.7B-Base"),
        device=tts.get("device", "cuda"),
        dtype=tts.get("dtype", "float16"),
        sample_rate=tts.get("sample_rate", 24000),
        generation_config=copy.deepcopy(base_generation),
        max_text_length=tts.get("max_text_length", 500),
        language=tts.get("language", "English"),
        attn_implementation=tts.get("attn_implementation", "sdpa"),
        post_processing_config=tts.get("post_processing", {}),
    )
    validator: WhisperValidator | None = None
    try:
        load_started = time.perf_counter()
        engine.load()
        report["tts_load_seconds"] = time.perf_counter() - load_started

        warm_voice_id = voice_ids[0]
        warm_fixture_id = fixture_ids[0]
        warm_path = args.output_dir / ".warmup.wav"
        engine.generation_config = copy.deepcopy(base_generation)
        engine.generate_speech(
            text=FIXTURES[warm_fixture_id],
            voice_reference_path=voices[warm_voice_id]["reference"],
            ref_text=voices[warm_voice_id]["ref_text"],
            emotion_instruction="neutral clear audiobook narration",
            output_path=warm_path,
        )
        report["warmup"] = {
            "voice": warm_voice_id,
            "fixture": warm_fixture_id,
            "metrics": dict(engine.last_generation_metrics),
        }
        warm_path.unlink(missing_ok=True)

        combo_index = 0
        for voice_id, voice in voices.items():
            for fixture_id in fixture_ids:
                combo_index += 1
                occurrence = {"A": 0, "B": 0}
                for slot, mode_label in enumerate(
                    _balanced_order(args.repetitions, args.order),
                    1,
                ):
                    occurrence[mode_label] += 1
                    repetition = occurrence[mode_label]
                    mode_name, generation = modes[mode_label]
                    engine.generation_config = copy.deepcopy(generation)
                    try:
                        import torch

                        torch.manual_seed(
                            args.seed + combo_index * 100 + repetition
                        )
                    except ImportError:
                        pass
                    output_path = (
                        args.output_dir
                        / voice_id
                        / fixture_id
                        / f"{mode_name}-r{repetition}.wav"
                    )
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    started = time.perf_counter()
                    engine.generate_speech(
                        text=FIXTURES[fixture_id],
                        voice_reference_path=voice["reference"],
                        ref_text=voice["ref_text"],
                        emotion_instruction="neutral clear audiobook narration",
                        output_path=output_path,
                    )
                    wall = time.perf_counter() - started
                    duration = float(sf.info(str(output_path)).duration)
                    run = {
                        "voice": voice_id,
                        "fixture": fixture_id,
                        "slot": slot,
                        "mode_label": mode_label,
                        "mode": mode_name,
                        "repetition": repetition,
                        "seed": args.seed + combo_index * 100 + repetition,
                        "wall_seconds": wall,
                        "audio_seconds": duration,
                        "realtime_factor": wall / duration,
                        "speaker_similarity": float(
                            engine.speaker_similarity(
                                output_path,
                                voice["reference"],
                            )
                        ),
                        "audio_sha256": _sha256(output_path),
                        "relative_path": str(
                            output_path.relative_to(args.output_dir)
                        ),
                        "engine_metrics": dict(engine.last_generation_metrics),
                    }
                    report["runs"].append(run)
                    atomic_write_json(args.output_json, report)
    finally:
        engine.unload()

    if not args.skip_validation:
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
                path = args.output_dir / run["relative_path"]
                started = time.perf_counter()
                transcript = validator.transcribe(str(path))
                run["validation_seconds"] = time.perf_counter() - started
                run["wer"] = validator.calculate_wer(
                    FIXTURES[run["fixture"]],
                    transcript,
                )
                run["transcript_sha256"] = hashlib.sha256(
                    transcript.encode("utf-8")
                ).hexdigest()
                atomic_write_json(args.output_json, report)
        finally:
            validator.unload()

    report["summary"] = {
        modes[label][0]: _summarize_mode(
            [run for run in report["runs"] if run["mode_label"] == label]
        )
        for label in ("A", "B")
    }
    control = report["summary"]["control"]
    candidate = report["summary"][args.candidate_name]
    fixture_combinations = len(voice_ids) * len(fixture_ids)
    corpus_gate = bool(
        len(voice_ids) >= 4
        and any(voice_id.casefold().startswith("narrator") for voice_id in voice_ids)
        and 12 <= fixture_combinations <= 24
    )
    report["decision"] = {
        "rtf_change_fraction": (
            candidate["rtf_p50"] / control["rtf_p50"] - 1.0
            if control["rtf_p50"]
            else None
        ),
        "fixture_combinations": fixture_combinations,
        "corpus_coverage_gate_pass": corpus_gate,
        "promotion_gate_evaluated": not args.skip_validation,
        "promote": False,
    }
    if not args.skip_validation:
        report["decision"]["promote"] = bool(
            corpus_gate
            and report["decision"]["rtf_change_fraction"] is not None
            and report["decision"]["rtf_change_fraction"] <= -0.10
            and candidate["maximum_wer"] is not None
            and candidate["maximum_wer"] <= 0.20
            and candidate["average_wer"] <= control["average_wer"]
            and candidate["minimum_speaker_similarity"]
            >= control["minimum_speaker_similarity"] - 0.02
        )
    atomic_write_json(args.output_json, report)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
