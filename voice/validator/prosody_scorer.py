import logging
from pathlib import Path
import numpy as np
import librosa
import soundfile as sf

logger = logging.getLogger(__name__)

class ProsodyScorer:
    """Non-blocking prosody analyzer for generated speech.
    Flags potential monotone or flat deliveries for review.
    """
    
    def __init__(self, sample_rate: int = 24000):
        self.sample_rate = sample_rate

    def analyze(self, audio_path: str | Path, text: str) -> dict[str, float | str | bool]:
        """
        Analyze audio for prosodic variation.
        Returns a dictionary of metrics and flags.
        """
        path = Path(audio_path)
        if not path.exists():
            return {"error": "File not found", "monotone_warning": False}
            
        try:
            # Load audio
            y, sr = sf.read(str(path))
            if y.ndim > 1:
                y = y.mean(axis=1) # Mono
                
            # If audio is too short, skip
            duration = len(y) / sr
            if duration < 1.0:
                return {"monotone_warning": False, "reason": "Too short for analysis"}
                
            # 1. Pitch Variance (F0 Standard Deviation)
            # using pyin for fundamental frequency estimation
            f0, voiced_flag, voiced_probs = librosa.pyin(
                y, 
                fmin=librosa.note_to_hz('C2'), 
                fmax=librosa.note_to_hz('C7'), 
                sr=sr
            )
            valid_f0 = f0[voiced_flag] if f0 is not None else []
            
            if len(valid_f0) > 10:
                pitch_std = float(np.std(valid_f0))
                pitch_mean = float(np.mean(valid_f0))
                # Coefficient of variation for pitch
                pitch_cv = pitch_std / pitch_mean if pitch_mean > 0 else 0
            else:
                pitch_cv = 0.0
                pitch_std = 0.0
                
            # 2. Dynamic Range (Peak to RMS)
            rms = librosa.feature.rms(y=y)[0]
            mean_rms = float(np.mean(rms))
            peak = float(np.max(np.abs(y)))
            
            # Avoid divide by zero
            if mean_rms > 1e-5:
                dynamic_range = peak / mean_rms
            else:
                dynamic_range = 0.0
                
            # Heuristics for "monotone" delivery
            # These thresholds are empirical and should be tuned.
            # A low pitch coefficient of variation (< 0.05) and low dynamic range (< 5.0) often means flat.
            is_monotone = (pitch_cv < 0.06) and (dynamic_range < 4.0)
            
            return {
                "pitch_std_hz": pitch_std,
                "pitch_cv": pitch_cv,
                "dynamic_range": dynamic_range,
                "monotone_warning": is_monotone
            }
            
        except Exception as e:
            logger.warning("Prosody analysis failed for %s: %s", path.name, e)
            return {"error": str(e), "monotone_warning": False}
