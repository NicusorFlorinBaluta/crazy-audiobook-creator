import json
import shutil
from pathlib import Path

from shared.models import ScriptLine
from voice.tts_server.qwen3_engine import Qwen3TTSEngine


def generate_ab_test():
    print("Testing Phase 4: Scene Prosody Listening A/B")

    script_path = Path(r"e:\Projects\crazy-audiobook-creator\brain\projects\sample_book-1\script\chapter_001.json")
    voice_library = Path(r"e:\Projects\crazy-audiobook-creator\voice_library\sample_book-1")

    with open(script_path, encoding="utf-8") as f:
        data = json.load(f)

    lines = [ScriptLine(**line) for line in data["lines"][:5]]  # Only the first 5 lines for A/B test

    engine = Qwen3TTSEngine()
    engine.load()

    ab_dir = Path("ab_test_output")
    if ab_dir.exists():
        shutil.rmtree(ab_dir)
    ab_dir.mkdir(parents=True, exist_ok=True)

    print("Generating BASELINE (Neutral / Default Speed)...")
    for line in lines:
        out_path = ab_dir / f"{line.line_id}_baseline.wav"
        voice_ref = voice_library / f"{line.speaker}.wav"
        if not voice_ref.exists():
            print(f"Skipping {line.speaker} (no ref)")
            continue
        print(f"  -> {line.line_id} (Speaker: {line.speaker})")
        engine.generate_speech(
            text=line.text,
            voice_reference_path=str(voice_ref),
            emotion_instruction="neutral",
            speed=1.0,
            output_path=str(out_path),
        )

    print("\nGenerating SCENE-AWARE (Dynamic Prosody)...")
    for line in lines:
        out_path = ab_dir / f"{line.line_id}_scene_aware.wav"
        voice_ref = voice_library / f"{line.speaker}.wav"
        if not voice_ref.exists():
            continue
        print(f"  -> {line.line_id} (Speaker: {line.speaker}, Emotion: {line.emotion}, Speed: {line.speed})")
        engine.generate_speech(
            text=line.text,
            voice_reference_path=str(voice_ref),
            emotion_instruction=line.emotion,
            speed=line.speed,
            output_path=str(out_path),
        )

    print(f"\nA/B test generated successfully in {ab_dir.absolute()}!")


if __name__ == "__main__":
    generate_ab_test()
