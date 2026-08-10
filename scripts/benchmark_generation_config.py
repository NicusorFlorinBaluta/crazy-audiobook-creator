"""
Benchmark generation config parameters (temperature, top_p, repetition_penalty)
to measure impact on WER, speaker similarity, and subjective prosody.
"""
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import yaml
from colorama import Fore, Style, init

from voice.tts_server.qwen3_engine import Qwen3TTSEngine
from voice.validator.whisper_validator import WhisperValidator
from shared.models import ScriptLine

init(autoreset=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Config A: Defaults / Old settings
CONFIG_A = {
    "temperature": 0.7,
    "top_p": 0.9,
    "repetition_penalty": 1.1,
}

# Config B: Current / New settings
CONFIG_B = {
    "temperature": 0.9,
    "top_p": 1.0,
    "repetition_penalty": 1.05,
}

def load_test_chapter(project_dir: Path, chapter_num: int) -> list[ScriptLine]:
    script_path = project_dir / "script" / f"chapter_{chapter_num:03d}.json"
    if not script_path.exists():
        logger.error("Test chapter not found: %s", script_path)
        sys.exit(1)
    
    with open(script_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    return [ScriptLine(**item) for item in data.get("lines", [])]

def get_voice_ref(voice_library_dir: Path, voice_id: str) -> Path:
    ref_path = voice_library_dir / f"{voice_id}.wav"
    if not ref_path.exists():
        fallback = voice_library_dir / "narrator.wav"
        if fallback.exists():
            return fallback
    return ref_path

def run_benchmark(project_id: str, chapter_num: int = 1, limit: int = 10):
    root_dir = Path(__file__).resolve().parent.parent
    project_dir = root_dir / "brain" / "projects" / project_id
    workspace_dir = root_dir / "workspace" / project_id
    voice_library_dir = root_dir / "voice_library" / project_id
    
    if not project_dir.exists():
        logger.error("Project not found: %s", project_dir)
        sys.exit(1)
        
    lines = load_test_chapter(project_dir, chapter_num)[:limit]
    if not lines:
        logger.error("No script lines found.")
        sys.exit(1)
        
    logger.info("Found %d lines for benchmarking.", len(lines))
    
    # Load TTS and Whisper
    engine = Qwen3TTSEngine(
        model_name="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        device="cuda",
        dtype="float16",
        sample_rate=24000,
    )
    engine.load()
    
    whisper = WhisperValidator(
        model_name="small",
        device="cuda",
    )
    whisper.load()
    
    results = []
    output_dir = workspace_dir / "benchmark_configs"
    output_dir.mkdir(exist_ok=True, parents=True)
    
    for idx, line in enumerate(lines):
        logger.info("--- Line %d/%d: %s ---", idx + 1, len(lines), line.line_id)
        
        voice_ref = get_voice_ref(voice_library_dir, line.voice_id or line.speaker)
        if not voice_ref.exists():
            logger.warning("Voice ref not found, skipping line: %s", line.line_id)
            continue
            
        line_results = {"line_id": line.line_id, "text": line.text}
        
        # Test both configs
        for config_name, gen_config in [("A", CONFIG_A), ("B", CONFIG_B)]:
            engine.generation_config = gen_config
            out_file = output_dir / f"{line.line_id}_config_{config_name}.wav"
            
            t0 = time.time()
            engine.generate_speech(
                text=line.text,
                voice_reference_path=voice_ref,
                emotion_instruction=line.emotion,
                speed=line.speed,
                voice_fx=line.voice_fx,
                output_path=out_file,
            )
            gen_time = time.time() - t0
            
            # Validate
            transcribed = whisper.transcribe(str(out_file))
            wer = whisper.calculate_wer(whisper._normalize_text(line.text), transcribed)
            
            # Speaker similarity
            try:
                sim = engine.speaker_similarity(out_file, voice_ref)
            except Exception:
                sim = 0.0
                
            line_results[config_name] = {
                "gen_time": gen_time,
                "wer": wer,
                "sim": sim,
            }
            logger.info("Config %s: WER=%.3f, Sim=%.2f, Time=%.2fs", config_name, wer, sim, gen_time)
            
        results.append(line_results)
        
    engine.unload()
    whisper.unload()
    
    # Print summary
    print("\n" + "="*80)
    print("BENCHMARK SUMMARY")
    print("="*80)
    
    if not results:
        logger.error("No successful results to summarize.")
        return
        
    avg_wer_a = sum(r["A"]["wer"] for r in results) / len(results)
    avg_wer_b = sum(r["B"]["wer"] for r in results) / len(results)
    
    avg_sim_a = sum(r["A"]["sim"] for r in results) / len(results)
    avg_sim_b = sum(r["B"]["sim"] for r in results) / len(results)
    
    avg_time_a = sum(r["A"]["gen_time"] for r in results) / len(results)
    avg_time_b = sum(r["B"]["gen_time"] for r in results) / len(results)
    
    print(f"Metrics          | Config A (Defaults)  | Config B (New)")
    print(f"-----------------|----------------------|----------------------")
    print(f"Avg WER          | {avg_wer_a:.3f}                | {avg_wer_b:.3f}")
    print(f"Avg Speaker Sim  | {avg_sim_a:.3f}                | {avg_sim_b:.3f}")
    print(f"Avg Gen Time     | {avg_time_a:.2f}s               | {avg_time_b:.2f}s")
    print("\nCompare the generated files in:")
    print(str(output_dir))
    print("="*80)

if __name__ == "__main__":
    from shared.live_test_guard import require_model_opt_in
    args = require_model_opt_in(sys.argv[1:])
    if len(args) < 1:
        print("Usage: python benchmark_generation_config.py --allow-models <project_id> [chapter_num] [limit]")
        sys.exit(1)
    project_id = args[0]
    chapter_num = int(args[1]) if len(args) > 1 else 1
    limit = int(args[2]) if len(args) > 2 else 10
    
    run_benchmark(project_id, chapter_num, limit)
