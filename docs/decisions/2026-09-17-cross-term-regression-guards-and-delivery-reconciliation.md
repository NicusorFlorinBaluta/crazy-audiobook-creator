# Cross-term regression guards, atomic repair, and delivery reconciliation

**Status:** Current  
**Date:** 2026-09-17  

## Context

Following the 2026-09-16 decision ([A majority is not a verdict: pronunciations are measured for stability](2026-09-16-a-majority-is-not-a-verdict.md)), a batch repair pass was run across `unstable` terms. While overall outliers dropped significantly from 444 to 314, an independent review revealed that several high-profile names regressed:

| Term | Before Batch Repair | After Batch Repair | Cause |
| --- | --- | --- | --- |
| `Drizzt` | **3** | **7** | Lines shared with `Do'Urden`, `Entreri`, or `Artemis Entreri` were redrawn |
| `Artemis Entreri` | 8 | **18** | Redrawing `Entreri` lines that also contained `Artemis` broke the full name |
| `Zaknafein` | 14 | 15 | Unconstrained redraw on shared lines |

Furthermore, the review identified an accidental omission of `language=language` on the primary `_validate_segment` call in `voice/validator/validation_loop.py`, causing Whisper language auto-detection to re-emerge and invalidating fair best-of-N candidate comparisons.

Finally, delivery staleness detection showed that repairing segments and re-mastering chapters left the final M4B files out of date unless explicitly rebuilt.

---

## 1. Cross-term regression guard: no name left behind

### Root Cause
`scripts/repair_outlier_lines.py` historically computed a single `target_key = phonetic_key(term)` and checked only whether the target term matched the desired sound group. When a line contained multiple glossary names (e.g. `ch09_0129`, `ch13_0096`, `ch27_0148`, `ch27_0154`), a redrawn take that fixed the target name while breaking another name on the same line was accepted silently.

### Decision & Solution
1. **Shared Multi-Term Evidence Comparator**: Implemented `is_pronunciation_candidate_better()` and `terms_in_text()` in `shared/pronunciation_evidence.py`.
2. **Strict Non-Regression Gate**: Before a candidate take is accepted (both in `repair_outlier_lines.py` and in `ValidationLoop._is_pronunciation_candidate_better`), every glossary term present on the line is evaluated.
   - If any term that was previously heard correctly in the incumbent take becomes wrong in the candidate take, **the candidate is rejected immediately**.
   - Only takes that maintain or improve the total count of correct names on the line while passing all hard quality gates (WER, audio quality, no audio truncations) are eligible to replace the incumbent take.

### Verification & Results
When re-running repairs with this guard active:
- `Drizzt`: Dropped from 7 outliers down to **2** across the entire book (stability 0.981, `spoken_correctly`). Lines where redrawing would break an adjacent name (`ch22_0353` with `Catti-brie`, `ch05_0136` and `ch09_0060` with `D'aerthe`/`Bregan`) were safely rejected and left alone.
- `Artemis Entreri`: Outliers fell from 18 back down to **11** (stability rose from 0.486 to 0.686).
- `Entreri`: Outliers fell from 30 down to **18** (stability rose from 0.720 to 0.832).
- `Do'Urden`: Outliers fell from 11 down to **6** (stability rose from 0.756 to 0.867).

---

## 2. Four-store atomic segment repair (`shared/segment_repair.py`)

A segment repair cannot simply overwrite a WAV file on disk. Four independent stores maintain state about each segment, and all four must be updated atomically:

1. **Audio File**: The segment WAV on disk (`workspace/<project>/segments/<line_id>.wav`).
2. **Segment Manifest**: `manifests/chapter_NNN.segments.json` must update the segment's `output_hash` and the overall `manifest_hash`. The pipeline reconciler drops a chapter from `generated` if a stored segment hash diverges from the audio bytes on disk.
3. **Voice Cache DB**: `voice_cache.db` (`generation_fingerprints.output_hash`) must be updated so the generation cache does not flag the segment as stale or overwrite it.
4. **State DB Quality Logs**: `pipeline_state.db` (`quality_logs`) must record the fresh transcript and validation metadata. The measurement scripts read the latest quality log entry per line; without this, repairs remain invisible to measurement audits.

`shared/segment_repair.py:replace_segment()` provides a single unified entry point that updates all four stores together and backs up the replaced take to `segments/repair-backup/`.

---

## 3. Preservation of active working substitutions

### Problem
`scripts/measure_pronunciations.py --apply` previously reset any term whose verdict was not `mispronounced` to itself (`recs[term] = {"default": term, "alternate": term}`). However, terms with active default substitutions that were working and scored `spoken_correctly` (such as `Braelin Janquay -> Braelin Yanquay`) had their active entries reset to no-op. Because `spoken_text` participates in `manifest.dependency_hash`, dropping an active substitution invalidated segment manifests across 11 chapters without any audio changes.

### Decision
`measure_pronunciations.py --apply` now explicitly preserves active working substitutions:
```python
if item.verdict == "mispronounced" or (
    item.verdict == "spoken_correctly" and entries[term].get("applied")
):
    kept += 1
    continue
```
An active entry that succeeded is kept so that existing audio manifests remain valid and reproducible.

---

## 4. Delivery staleness & rebuilding discipline

A repair is not complete when the WAV is replaced on disk. The dependency chain propagates through mastering to the packaged audiobook:

```
repaired segment wav  ->  segment manifest (output_hash)
                      ->  chapter master (segment_manifest_hash stale)
                      ->  remaster_chapters.py
                      ->  chapter wav updated
                      ->  delivery M4B stale (older than chapter wav)
                      ->  reexport_deliveries.py
                      ->  listener receives repaired audio
```

`shared/staleness.py` and `scripts/reexport_deliveries.py --stale` inspect output timestamps against chapter WAV timestamps to surface stale deliveries. A repair run is only marked finished once all affected chapters are re-mastered and all stale M4B deliveries are re-exported and report `ok`.

---

## 5. Summary of Ground Rules

1. **Cross-term non-regression is mandatory**: Redrawing for term A must never degrade term B on the same line.
2. **`unstable` never ships a respelling; only `mispronounced` does.** Unstable terms require take redraws, not lexicon churn.
3. **Respellings require control-first measurement.** Candidate respellings must beat the control arm on real lines before adoption.
4. **Active working substitutions are immutable to prune passes.** Successful substitutions must not be reset to no-op.
5. **A repair is not done until the delivery is rebuilt.** Always run `reexport_deliveries.py --stale` and clear staleness.
