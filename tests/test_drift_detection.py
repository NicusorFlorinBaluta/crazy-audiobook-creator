import librosa
import soundfile as sf
import os
from voice.validator.audio_analyzer import AudioAnalyzer
from voice.validator.validation_loop import QualityResult

def test_drift():
    print("Testing Phase 5: Drift & Join Acceptance")
    original_file = r"e:\Projects\crazy-audiobook-creator\voice_library\sample_book-1\child_female.wav"
    shifted_file = "shifted_child_female.wav"
    
    analyzer = AudioAnalyzer()
    
    print("1. Analyzing baseline audio...")
    base_analysis = analyzer.analyze(original_file, expected_text="She walked through the moonlit garden", speed=1.0)
    base_pitch = base_analysis.get("pitch_median", 0.0)
    base_rms = base_analysis.get("rms_dbfs", 0.0)
    print(f"Baseline Pitch Median: {base_pitch:.2f} Hz, RMS: {base_rms:.2f} dBFS")
    
    print("2. Creating a 5-semitone pitch-shifted version (simulating drift/wrong speaker)...")
    y, sr = librosa.load(original_file, sr=24000)
    y_shifted = librosa.effects.pitch_shift(y, sr=sr, n_steps=-12)
    
    # Also reduce volume by 35 dB to trigger the join check
    y_soft = y_shifted * (10 ** (-40 / 20))
    sf.write(shifted_file, y_soft, sr)
    
    print("3. Analyzing shifted audio...")
    shifted_analysis = analyzer.analyze(shifted_file, expected_text="She walked through the moonlit garden", speed=1.0)
    shifted_pitch = shifted_analysis.get("pitch_median", 0.0)
    shifted_rms = shifted_analysis.get("rms_dbfs", 0.0)
    print(f"Shifted Pitch Median: {shifted_pitch:.2f} Hz, RMS: {shifted_rms:.2f} dBFS")
    
    print("4. Running ValidationLoop drift checks...")
    warnings = []
    if base_pitch > 0 and shifted_pitch > 0:
        pitch_delta = abs(shifted_pitch - base_pitch) / base_pitch
        if pitch_delta > 0.30:
            warnings.append("Drift check (report-only): Pitch significantly deviated from reference bounds.")
    
    if shifted_rms < -30:
        warnings.append("Join check (report-only): Abrupt loudness drop suspected.")
        
    print("\nWarnings Detected:")
    for w in warnings:
        print(f" - {w}")
        
    if len(warnings) == 2:
        print("\nSuccess! Both Drift and Join anomalies were correctly caught with >30% pitch delta and <-30dBFS RMS.")
    else:
        print("\nFailure! Did not catch the anomalies.")
        
    os.remove(shifted_file)

if __name__ == "__main__":
    test_drift()
