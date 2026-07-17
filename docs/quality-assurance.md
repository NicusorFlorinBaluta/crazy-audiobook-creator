# Quality Assurance & Validation

## Overview

This document details the AI-driven quality assurance system that automatically validates every audio segment without human intervention. The system catches pronunciation errors, audio artifacts, and consistency issues before they make it into the final audiobook.

---

## Validation Pipeline

```
Generated Audio Segment
        ↓
┌───────────────────────────┐
│  Layer 1: Intelligibility │  Whisper STT → WER Check
│  "Did the TTS say the     │  Threshold: WER < 5%
│   right words?"           │
└───────────┬───────────────┘
            ↓ PASS
┌───────────────────────────┐
│  Layer 2: Audio Quality   │  Signal Analysis
│  "Is the audio clean?"    │  Clipping, noise, silence
└───────────┬───────────────┘
            ↓ PASS
┌───────────────────────────┐
│  Layer 3: Duration Check  │  Word Count vs Duration
│  "Is the pacing right?"   │  Tolerance: ±30%
└───────────┬───────────────┘
            ↓ PASS
┌───────────────────────────┐
│  Composite Quality Score  │  Weighted combination
│  Score ≥ 0.7 → PASS       │  Score < 0.7 → FLAG
└───────────────────────────┘
```

---

## Layer 1: Intelligibility (Whisper + WER)

### How It Works

1. **Transcribe**: Feed the generated `.wav` into `faster-whisper` (medium model)
2. **Normalize**: Both original text and transcription are normalized:
   - Convert to lowercase
   - Remove punctuation
   - Expand numbers: "42" → "forty two"
   - Expand common abbreviations: "Dr." → "doctor"
   - Collapse whitespace
3. **Compare**: Calculate Word Error Rate (WER) using `jiwer`

### WER Calculation

```
WER = (Substitutions + Insertions + Deletions) / Total Reference Words
```

| WER | Meaning | Action |
|-----|---------|--------|
| 0-2% | Perfect/near-perfect | Pass |
| 2-5% | Minor variations (acceptable) | Pass |
| 5-10% | Noticeable errors | Fail → Retry |
| 10%+ | Significant errors | Fail → Retry (likely garbled) |

### Why 5% Threshold?

- Whisper itself has ~2% baseline WER on clean English audio
- Qwen3-TTS typically produces ~1.8% WER on standard text
- Combined: 3-4% is expected for well-generated audio
- 5% gives a small buffer for unusual words (fantasy names, archaic language)
- Going below 3% would cause too many false positives on fantasy names

### Fantasy Name Handling

Fantasy books have unusual names that Whisper may transcribe differently:
- "Kvothe" might be transcribed as "quote" or "cove"
- "Auri" might be "ory" or "awry"

**Mitigation**:
- Before WER check, replace known fantasy names in both texts with phonetic equivalents
- Maintain a project-level dictionary of fantasy terms
- If WER fails and the only errors are fantasy names, auto-pass with a note

---

## Layer 2: Audio Quality

### Clipping Detection
```python
peak = np.max(np.abs(audio_signal))
peak_dbfs = 20 * np.log10(peak)
clipping = peak_dbfs > -0.5  # -0.5 dBFS threshold
```

Clipping causes harsh distortion. If detected:
- **Mild** (peak > -0.5 dBFS): Flag, don't retry (mastering will fix with limiter)
- **Severe** (peak > 0 dBFS): Fail → Retry

### Noise Floor Analysis
```python
# Find the quietest 10% of the signal (silence between words)
silence_segments = find_silence(audio, threshold=-40)
noise_floor = np.mean([rms_db(seg) for seg in silence_segments])
```

| Noise Floor | Quality | Action |
|-------------|---------|--------|
| < -60 dB | Excellent | Pass |
| -50 to -60 dB | Good | Pass |
| -40 to -50 dB | Acceptable | Pass with note |
| > -40 dB | Poor | Fail → Retry |

### Silence Gap Detection

Checks for unnatural pauses within a segment:
- Expected: brief pauses between sentences (0.2-1.0 seconds)
- Suspicious: silence > 3 seconds within a segment
- Action: Flag for review (might be a TTS failure or very long pause)

### Sample Rate Check
- Verify output is 24000 Hz (Qwen3-TTS native rate)
- Flag if different (indicates a processing error)

---

## Layer 3: Duration Sanity

### Expected Duration Calculation
```python
word_count = len(text.split())
words_per_minute = 150 * speed  # 150 WPM adjusted by speed parameter
expected_seconds = (word_count / words_per_minute) * 60
tolerance = 0.3  # ±30%

min_expected = expected_seconds * (1 - tolerance)
max_expected = expected_seconds * (1 + tolerance)

passed = min_expected <= actual_seconds <= max_expected
```

### Why 150 WPM?
- Average audiobook narration: 150-160 WPM
- Our TTS with default speed (1.0x) targets ~150 WPM
- Speed parameter adjusts: 0.85x → ~128 WPM, 1.15x → ~173 WPM

### Duration Failures

| Issue | Likely Cause | Action |
|-------|-------------|--------|
| Way too short | TTS truncated output | Retry |
| Way too long | TTS repeated/stuttered | Retry |
| Slightly off | Normal variation | Pass |

---

## Composite Quality Score

Each segment receives a score from 0.0 to 1.0:

```python
quality_score = (
    (1 - wer) * 0.6 +           # Intelligibility: 60% weight
    artifact_score * 0.3 +       # Audio quality: 30% weight
    duration_score * 0.1          # Duration accuracy: 10% weight
)
```

Where:
- `artifact_score`: 1.0 if no issues, reduced for each detected problem
- `duration_score`: 1.0 if within tolerance, scaled by deviation

### Score Interpretation

| Score | Rating | Action |
|-------|--------|--------|
| 0.9-1.0 | Excellent | Pass |
| 0.7-0.9 | Good | Pass |
| 0.5-0.7 | Acceptable | Pass with flag |
| 0.3-0.5 | Poor | Fail → Retry |
| 0.0-0.3 | Very Poor | Fail → Retry |

---

## Retry Logic

```
MAX_RETRIES = 3

for attempt in range(1, MAX_RETRIES + 1):
    audio = generate(line, attempt=attempt)
    result = validate(audio)
    
    if result.passed:
        save(audio)
        log_quality(result)
        break
    
    if attempt < MAX_RETRIES:
        log(f"Retry {attempt}: WER={result.wer:.3f}")
        # TTS is non-deterministic — same input may produce better output
    
    if attempt == MAX_RETRIES:
        # Save best attempt (lowest WER across all tries)
        save(best_audio)
        flag_for_review(line, result)
        log_warning(f"Line {line.id} failed after {MAX_RETRIES} attempts")
```

### Why Retries Work

TTS models like Qwen3-TTS use sampling-based generation. The same input text can produce slightly different outputs each time. If one generation garbles a word, the next attempt might pronounce it perfectly. In practice, ~90% of validation failures are resolved within 2 retries.

---

## Quality Dashboard

The web dashboard displays quality metrics:

### Per-Segment View
- WER score with pass/fail indicator
- Audio waveform visualization
- Play button for quick review
- Retry button for manual regeneration

### Per-Chapter Summary
- Average WER
- Number of segments passed/failed/flagged
- Total retry count
- Duration accuracy

### Per-Book Summary
- Overall quality score
- Worst segments (sorted by WER)
- Character-specific quality (some voices may generate more errors)
- Generation time vs audio duration ratio

---

## Quality Report Schema

```json
{
  "project_id": "name-of-the-wind",
  "generated_at": "2026-07-13T22:00:00Z",
  "summary": {
    "total_segments": 3850,
    "passed": 3842,
    "failed": 3,
    "flagged": 5,
    "total_retries": 47,
    "average_wer": 0.021,
    "median_wer": 0.015,
    "worst_wer": 0.089,
    "average_quality_score": 0.94,
    "total_audio_duration": "10:34:21",
    "total_generation_time": "8:12:45"
  },
  "by_chapter": [
    {
      "chapter": 1,
      "segments": 247,
      "average_wer": 0.019,
      "retries": 3,
      "flagged": 0,
      "duration": "30:34"
    }
  ],
  "flagged_segments": [
    {
      "line_id": "ch15_103",
      "speaker": "kvothe",
      "text": "Tehlu and his angels...",
      "wer": 0.067,
      "issue": "Fantasy name 'Tehlu' transcribed as 'tell you'",
      "quality_score": 0.68
    }
  ]
}
```

---

## VRAM Management During Validation

With 8GB VRAM on the RTX 2080 Super:

### Sequential Mode (Default)
```
Phase 1: Load Qwen3-TTS → Generate ALL segments for chapter → Unload TTS
Phase 2: Load Whisper → Validate ALL segments → Unload Whisper
Phase 3: If retries needed → Load TTS → Regenerate failed → Unload → Validate
```

Model swap time: ~10-15 seconds per swap
Total overhead per chapter: ~30-60 seconds (negligible compared to generation time)

### CPU Whisper Mode (Alternative)
```yaml
validation:
  whisper_device: "cpu"
```

Run Whisper on CPU while TTS stays on GPU. Slower validation (~1-2x real-time instead of ~10x) but zero model swapping. Recommended if you have a strong CPU (8+ cores).

### Skip Validation Mode
```yaml
validation:
  enabled: false
```

For testing or when speed matters more than quality. Not recommended for final output.
