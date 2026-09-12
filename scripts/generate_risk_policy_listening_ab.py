"""Generate a matched listening A/B for the short-expressive clarity policy."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.models import ScriptLine
from voice.tts_server.embedding_store import EmbeddingStore
from voice.tts_server.qwen3_engine import Qwen3TTSEngine
from voice.tts_server.voice_library import VoiceLibraryManager
from voice.validator.audio_analyzer import AudioAnalyzer
from voice.validator.validation_loop import ValidationLoop
from voice.validator.whisper_validator import WhisperValidator


def audio_metrics(path: Path) -> dict[str, float]:
    audio, sample_rate = sf.read(path, dtype="float32", always_2d=False)
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim > 1:
        audio = np.mean(audio, axis=1)
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
    return {
        "duration_seconds": round(audio.size / sample_rate, 6),
        "peak_dbfs": round(20.0 * math.log10(max(peak, 1e-12)), 6),
        "rms_dbfs": round(20.0 * math.log10(max(rms, 1e-12)), 6),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="sample_book-1")
    parser.add_argument("--voice", default="starling")
    parser.add_argument("--seed", type=int, default=3107)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("workspace/sample_book-1/quality_ab/risk_policy"),
    )
    args = parser.parse_args()

    config = yaml.safe_load(Path("voice/config.yaml").read_text(encoding="utf-8"))
    tts_cfg = config["tts"]
    validation_cfg = config["validation"]
    storage_cfg = config.get("storage", {})
    library_root = Path(storage_cfg.get("voice_library_dir", "voice_library"))
    voices_path = library_root / args.project / "voices.json"
    voices = json.loads(voices_path.read_text(encoding="utf-8"))["voices"]
    voice = voices[args.voice]
    reference_path = Path(voice["file"])
    reference_text = voice["ref_text"]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = args.output_dir / "starling_uncle_authored_delivery.wav"
    policy_path = args.output_dir / "starling_uncle_clarity_policy.wav"
    report_path = args.output_dir / "report.json"

    store = EmbeddingStore("voice_cache.db")
    engine = Qwen3TTSEngine(
        model_name=tts_cfg["model"],
        device=tts_cfg.get("device", "cuda"),
        dtype=tts_cfg.get("dtype", "float16"),
        sample_rate=tts_cfg.get("sample_rate", 24000),
        embedding_store=store,
        generation_config=tts_cfg.get("generation", {}),
        max_text_length=tts_cfg.get("max_text_length", 500),
        language=tts_cfg.get("language", "English"),
        attn_implementation=tts_cfg.get("attn_implementation", "sdpa"),
    )
    whisper = WhisperValidator(
        model_name=validation_cfg.get("whisper_model", "small"),
        device=validation_cfg.get("whisper_device", "cuda"),
    )
    library = VoiceLibraryManager(library_dir=library_root)
    loop = ValidationLoop(
        whisper=whisper,
        analyzer=AudioAnalyzer(),
        engine=engine,
        library=library,
        embedding_store=store,
        risk_aware_first_attempt=False,
    )
    line = ScriptLine(
        line_id="risk_ab_uncle",
        speaker=args.voice,
        voice_id=args.voice,
        text="UNCLE!",
        emotion="excited shout",
        speed=1.25,
    )

    report: dict[str, object] = {
        "project": args.project,
        "voice": args.voice,
        "seed": args.seed,
        "expected_text": line.text,
        "samples": {},
    }
    try:
        engine.load()
        for label, enabled, output_path in (
            ("authored_delivery", False, baseline_path),
            ("clarity_policy", True, policy_path),
        ):
            loop.risk_aware_first_attempt = enabled
            synthesis_text, emotion, speed, voice_fx, reason = loop._initial_delivery(line, set())
            torch.manual_seed(args.seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(args.seed)
            engine.generate_speech(
                text=synthesis_text,
                voice_reference_path=reference_path,
                ref_text=reference_text,
                emotion_instruction=emotion or "",
                speed=speed,
                voice_fx=voice_fx,
                output_path=output_path,
            )
            report["samples"][label] = {
                "path": str(output_path.resolve()),
                "synthesis_text": synthesis_text,
                "emotion": emotion,
                "speed": speed,
                "override_reason": reason,
                "speaker_similarity": round(
                    float(engine.speaker_similarity(output_path, reference_path)),
                    6,
                ),
                **audio_metrics(output_path),
            }

        engine.unload()
        whisper.load()
        for sample in report["samples"].values():
            transcript = whisper.transcribe(sample["path"])
            sample["transcript"] = transcript
            sample["wer"] = round(
                whisper.calculate_wer(line.text, transcript),
                6,
            )
    finally:
        whisper.unload()
        engine.unload()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
