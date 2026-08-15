"""
Benchmark Whisper `small` vs `medium` models for validation accuracy.
"""
import json
import logging
import sys
import time
from pathlib import Path

from colorama import Fore, Style, init

from voice.tts_server.qwen3_engine import Qwen3TTSEngine
from voice.validator.whisper_validator import WhisperValidator
from shared.models import ScriptLine

init(autoreset=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

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

def run_benchmark(project_id: str, chapter_num: int = 1, limit: int = 30):
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
        
    logger.info("Found %d lines for Whisper benchmarking.", len(lines))
    
    output_dir = workspace_dir / "benchmark_whisper"
    output_dir.mkdir(exist_ok=True, parents=True)
    segments_dir = workspace_dir / "segments"
    
    if not segments_dir.exists():
        logger.error("No existing segments found in %s to run benchmark on.", segments_dir)
        sys.exit(1)
        
    audio_paths = []
    for line in lines:
        out_file = segments_dir / f"{line.line_id}.wav"
        if out_file.exists():
            audio_paths.append((line, out_file))
            
    logger.info("Found %d pre-existing audio segments. Starting validation benchmark.", len(audio_paths))
    
    results = []
    
    for model_name in ["small", "medium"]:
        logger.info("\n--- Benchmarking Whisper '%s' ---", model_name)
        whisper = WhisperValidator(model_name=model_name, device="cpu")
        whisper.load()
        
        model_results = {"model": model_name, "total_time": 0, "lines": []}
        
        for line, out_file in audio_paths:
            t0 = time.time()
            transcribed = whisper.transcribe(str(out_file))
            val_time = time.time() - t0
            
            wer = whisper.calculate_wer(whisper._normalize_text(line.text), transcribed)
            
            model_results["lines"].append({
                "line_id": line.line_id,
                "text": line.text,
                "transcribed": transcribed,
                "wer": wer,
                "time": val_time
            })
            model_results["total_time"] += val_time
            logger.info("[%s] %s - WER: %.3f - Time: %.2fs", model_name, line.line_id, wer, val_time)
            
        results.append(model_results)
        whisper.unload()

    print("\n" + "="*80)
    print("WHISPER BENCHMARK SUMMARY")
    print("="*80)
    
    res_small = results[0]
    res_medium = results[1]
    
    avg_wer_small = sum(r["wer"] for r in res_small["lines"]) / len(res_small["lines"])
    avg_wer_medium = sum(r["wer"] for r in res_medium["lines"]) / len(res_medium["lines"])
    
    # A line "fails" if WER > 0.20
    fails_small = sum(1 for r in res_small["lines"] if r["wer"] > 0.20)
    fails_medium = sum(1 for r in res_medium["lines"] if r["wer"] > 0.20)
    
    print(f"Metrics          | Small                 | Medium")
    print(f"-----------------|-----------------------|----------------------")
    print(f"Avg WER          | {avg_wer_small:.3f}                 | {avg_wer_medium:.3f}")
    print(f"Total Validation | {res_small['total_time']:.2f}s                | {res_medium['total_time']:.2f}s")
    print(f"Val Time / Line  | {res_small['total_time']/len(lines):.2f}s                | {res_medium['total_time']/len(lines):.2f}s")
    print(f"Fails (WER>0.2)  | {fails_small}/{len(lines)}                   | {fails_medium}/{len(lines)}")
    print("="*80)
    
    # Print differences where one failed and other passed
    print("\nDifferences:")
    for rs, rm in zip(res_small["lines"], res_medium["lines"]):
        if rs["wer"] > 0.20 and rm["wer"] <= 0.20:
            print(f"Medium passed where Small failed: {rs['line_id']}")
            print(f"  Ref:   {rs['text']}")
            print(f"  Small: {rs['transcribed']} (WER: {rs['wer']:.3f})")
            print(f"  Medium:{rm['transcribed']} (WER: {rm['wer']:.3f})")
        elif rm["wer"] > 0.20 and rs["wer"] <= 0.20:
            print(f"Small passed where Medium failed: {rs['line_id']}")
            print(f"  Ref:   {rs['text']}")
            print(f"  Small: {rs['transcribed']} (WER: {rs['wer']:.3f})")
            print(f"  Medium:{rm['transcribed']} (WER: {rm['wer']:.3f})")

if __name__ == "__main__":
    from shared.live_test_guard import require_model_opt_in
    args = require_model_opt_in(sys.argv[1:])
    if len(args) < 1:
        print("Usage: python benchmark_whisper.py --allow-models <project_id> [chapter_num] [limit]")
        sys.exit(1)
    project_id = args[0]
    chapter_num = int(args[1]) if len(args) > 1 else 1
    limit = int(args[2]) if len(args) > 2 else 30
    
    run_benchmark(project_id, chapter_num, limit)
