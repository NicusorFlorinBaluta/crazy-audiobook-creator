# Recalibration of Monotone Delivery Detection

**Status:** Current

## Context & Problem

Audit of 7,928 audio segments on `the-finest-edge-of-twilight-book` revealed that the monotone prosody detector had fired zero times across all chapters (`monotone_fraction = 0.000` on every single row).

Investigation confirmed the defect was in `voice/validator/prosody_scorer.py`:
```python
is_monotone = pitch_cv < self.pitch_cv_threshold and dynamic_range < self.dynamic_range_threshold
```
where `pitch_cv_threshold = 0.06` and `dynamic_range_threshold = 4.0`.

While 396 of 7,928 segments (5%) satisfied the pitch condition (`pitch_cv < 0.06`), `dynamic_range` (crest factor = `peak / mean_rms`) on natural speech has:
- Median: **7.83**
- 5th percentile (p05): **5.29**
- Samples below 4.0: exactly 1 in 398 sampled segments.

Because dynamic range was required as an `and` condition, it acted as a strict veto that permanently suppressed all monotone warnings. The conjunction was mathematically unsatisfiable for natural speech, rendering the detector a constant `False`.

In contrast to the long-form trend thresholds (which are documented baselines deliberately tuned quiet to detect changes between runs), the code comment in `prosody_scorer.py` noted that these prosody thresholds were "empirical and should be tuned".

## Decision & Implementation

1. **Non-Veto Corroboration:**
   - Low pitch variation (`pitch_cv < pitch_cv_threshold`, default `0.06`) is treated as the primary indicator of flat/monotone delivery.
   - Dynamic range (`dynamic_range < dynamic_range_threshold`, default `5.29`, the measured p05) acts as corroboration for borderline pitch variation (`pitch_cv < 1.5 * pitch_cv_threshold`) rather than a veto on genuinely flat pitch:
     ```python
     is_monotone = (pitch_cv < self.pitch_cv_threshold) or (
         dynamic_range < self.dynamic_range_threshold and pitch_cv < (self.pitch_cv_threshold * 1.5)
     )
     ```
2. **Configurability:**
   - Added `prosody.pitch_cv_threshold` and `prosody.dynamic_range_threshold` to `brain/config.yaml` and `voice/config.yaml`, with schema validation in `shared/config_validation.py`.
   - Tuned default `dynamic_range_threshold` to **5.29** (p05) instead of 4.0.
3. **Quality Trends Integration:**
   - `brain/orchestrator/quality_trends.py` evaluates monotone delivery using the calibrated rule when calculating `monotone_fraction`, allowing historical segments in `quality_logs` to reflect actual prosodic variation.

## Evidence & Verification

- Replaying the 7,928 stored segments through the calibrated rule produces 103 monotone-flagged segments among selected takes (~1.38%), giving a non-zero `monotone_fraction` on 65 of 200 chapter-voice rows (typically 1%–5%).
- **Run-to-run comparison note**: Because monotone detection was previously a constant `False`, activating it downgrades ~103 previously `PASS` takes to `ACCEPTED_WITH_WARNING`. Any run-to-run "not clean" / advisory take count comparisons across this change are therefore not like-for-like; the increase in advisory warnings reflects previously blind detector activation rather than audio quality degradation.
- Unit tests in `tests/test_prosody_scorer.py` pin:
  1. Synthetic flat-pitch audio with normal dynamic range flags monotone; expressive frequency-sweep audio does not.
  2. Borderline pitch variation is flagged only when corroborated by low dynamic range.
  3. Negative test on stored book metrics verifies warning count is neither 0 nor excessively high.
