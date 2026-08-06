import os
import time
import logging
from pathlib import Path

# Placeholder for actual model imports when benchmark runs
try:
    from voice.tts_server.qwen3_engine import Qwen3TTSEngine
except ImportError:
    Qwen3TTSEngine = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Benchmark")

def run_benchmark():
    if Qwen3TTSEngine is None:
        logger.error("TTS Engine not found. Make sure to run from project root.")
        return

    test_sentences = [
        "This is a warm-up sentence.",
        "The quick brown fox jumps over the lazy dog.",
        "A somewhat longer sentence that tests the attention span of the model over many tokens and requires a breath.",
        "Short. Punchy. Action."
    ]

    configs = [
        {"name": "Baseline SDPA", "kwargs": {"attn_implementation": "sdpa"}},
        {"name": "Eager", "kwargs": {"attn_implementation": "eager"}},
        {"name": "Flash Attention 2", "kwargs": {"attn_implementation": "flash_attention_2"}},
    ]

    results = []
    
    for config in configs:
        logger.info(f"--- Testing {config['name']} ---")
        try:
            engine = Qwen3TTSEngine(**config["kwargs"])
            engine.load()
            
            # Warm up
            _ = engine.generate_speech(test_sentences[0], output_path="warmup.wav")
            
            # Benchmark
            start = time.time()
            for i, sentence in enumerate(test_sentences[1:]):
                _ = engine.generate_speech(sentence, output_path=f"test_{i}.wav")
            end = time.time()
            
            elapsed = end - start
            results.append((config["name"], elapsed))
            logger.info(f"{config['name']} took {elapsed:.2f}s")
            
            engine.unload()
        except Exception as e:
            logger.error(f"Failed {config['name']}: {e}")
            results.append((config["name"], float("inf")))

    print("\n\n--- Benchmark Results ---")
    for name, elapsed in results:
        print(f"{name}: {elapsed:.2f}s")

if __name__ == "__main__":
    run_benchmark()
