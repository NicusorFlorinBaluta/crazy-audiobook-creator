import time
import os
import gc
import torch
import numpy as np
from voice.tts_server.qwen3_engine import Qwen3TTSEngine
from shared.models import ScriptLine

def run_benchmark(attn_implementation: str):
    print(f"\n--- Benchmarking attn_implementation='{attn_implementation}' ---")
    
    # 1. Load Model
    start_load = time.time()
    engine = Qwen3TTSEngine(attn_implementation=attn_implementation)
    load_time = time.time() - start_load
    print(f"Model Load Time: {load_time:.2f}s")
    
    text = "The ancient tower stood against the darkening sky as rain swept across the weathered stone. Lightning flashed, illuminating the worn faces of the gargoyles perched high above the courtyard. He pulled his cloak tighter, hoping the storm would mask his approach."
    
    # Create dummy ScriptLine
    line = ScriptLine(
        line_id="test_001",
        speaker="narrator",
        text=text,
        emotion="neutral",
        speed=1.0
    )
    
    # 2. Warmup Run
    print("Running warmup...")
    # Generate without a reference audio file, just unconditional fallback or default narrator
    # Wait, the engine requires a reference audio file? Let's check.
    try:
        engine.generate(line, "dummy_ref.wav", output_path="dummy_out.wav")
    except Exception as e:
        print(f"Warmup warning (expected if missing reference): {e}")
        # Actually I need a real reference to run generation!
        
def main():
    original_file = r"e:\Projects\crazy-audiobook-creator\voice_library\sample_book-1\child_female.wav"
    
    for attn in ["sdpa", "eager"]:
        print(f"\n--- Benchmarking attn_implementation='{attn}' ---")
        
        start_load = time.time()
        engine = Qwen3TTSEngine(attn_implementation=attn)
        load_time = time.time() - start_load
        print(f"Model Load Time: {load_time:.2f}s")
        
        text = "The ancient tower stood against the darkening sky as rain swept across the weathered stone. Lightning flashed, illuminating the worn faces of the gargoyles perched high above the courtyard. He pulled his cloak tighter, hoping the storm would mask his approach."
        
        line = ScriptLine(
            line_id="test_001",
            speaker="narrator",
            text=text,
            emotion="neutral",
            speed=1.0
        )
        
        # Warmup
        engine.generate_speech(
            text=line.text, 
            voice_reference_path=original_file, 
            output_path="dummy_out.wav",
            speed=line.speed
        )
        
        # Actual runs
        run_times = []
        durations = []
        
        for i in range(3):
            start = time.time()
            engine.generate_speech(
                text=line.text, 
                voice_reference_path=original_file, 
                output_path="dummy_out.wav",
                speed=line.speed
            )
            end = time.time()
            
            run_time = end - start
            run_times.append(run_time)
            
            # Measure duration
            import soundfile as sf
            audio, sr = sf.read("dummy_out.wav")
            durations.append(len(audio) / sr)
            
        avg_time = sum(run_times) / len(run_times)
        avg_dur = sum(durations) / len(durations)
        rtf = avg_time / avg_dur if avg_dur > 0 else 0
        
        print(f"Average Generation Time: {avg_time:.2f}s")
        print(f"Average Audio Duration: {avg_dur:.2f}s")
        print(f"Real-Time Factor (RTF): {rtf:.2f}")
        
        # Cleanup
        del engine
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

if __name__ == "__main__":
    import sys
    from shared.live_test_guard import require_model_opt_in
    require_model_opt_in(sys.argv[1:])
    main()
