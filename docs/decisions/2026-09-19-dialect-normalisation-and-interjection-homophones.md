# Dialect Normalisation, Interjection Homophones, and Glossary-Only Misses

**Status:** Current

## Context & Motivation

During quality validation across chapters with character dialect (dwarves like Bruenor, Athrogate), Whisper's ASR silently standardises dialect spellings back into standard English. For example, a dwarf speaking *"Ye comin' by this on yer own, are ye?"* is transcribed as *"Ye coming by this on your own, are ye?"*, causing false WER penalties on lines where audio synthesis was faithful and in-character.

Furthermore, single-word interjections such as *"Aye"* are frequently transcribed by Whisper as different words entirely (e.g. *"I"*), producing a 100% WER failure despite pristine acoustics. In production, this resulted in three blind redraws and subsequent human acceptance of clean takes.

Lastly, when a segment failed validation purely due to an unfamiliar proper noun / glossary term, blind redraws reshuffled takes without fixing the underlying unrecognised pronunciation, consuming retry budgets needed for real audio defects.

## Decision & Implementation

1. **Symmetric Dialect Normalisation:**
   - A canonical mapping table `DIALECT_NORMALISATIONS` (`ye -> you`, `yer -> your`, `yerself -> yourself`, `telled -> told`, `em -> them`, ~~`o -> of`~~, `d'ye -> do you`, `ain't -> is not`, `nay -> no`, ~~`aye -> yes`~~, `bah -> bah`) and suffix rule (`-in' -> -ing`) are applied to **both** reference text and ASR hypothesis within `WhisperValidator._normalize_text`.
   - *Correction (2026-09-19 code review):* `o -> of` was removed because it turns `"o'clock"` into `"of clock"` and exclamation `"O"` into `"of"`. `aye -> yes` was removed because mapping authored `"aye"` to `"yes"` combined with interjection matching caused authored `"Yes."` to falsely pass when the engine spoke `"I"`.
   - Applied per-token on word boundaries (`\b`), preventing collisions with longer words (e.g., `lawyer`, `yellow`, `emblem`, `often` remain unchanged).
   - **Source-fidelity check:** Under `docs/architecture.md` §"Source-fidelity invariant", this change is safe: it lives exclusively in `WhisperValidator._normalize_text` for transcript comparison and never touches `_prepare_synthesis_text` or changes what is synthesised.

2. **One-Word Interjection Homophones:**
   - `INTERJECTION_HOMOPHONES` (`aye: {i, aye, eye}`, `bah`, `nay`, `hmph`) allows clean acoustic takes of single-word lines to pass the text gate when the transcript matches a known homophone (e.g., authored *"Aye"* transcribed as *"I"*). Keyed strictly on authentic dialect interjections so standard authored `"Yes."` or `"No."` cannot pass on mismatched words.
   - Hard audio gates (`clipping_detected`, `has_long_silence`, `duration_ok`) remain strictly enforced: poor acoustics still fail.

3. **Glossary-Only Miss Handling:**
   - When a validation failure's only difference is an unrecognised glossary term (`_is_glossary_only_miss`), the result is marked with `glossary_only_miss=True` and excluded from blind retry redraws, recording the failure for lexicon review instead of burning synthesis budget.

## Evidence & Verification

- Unit tests in `tests/test_dialect_normalisation.py` assert symmetric normalization for dialect sentences, verify negative substring collisions (`lawyer`, `yellow`, `emblem`, `often`), assert acoustic pass/fail on interjections, assert failure on real WER defects (`Bedorijay fumed.`), and verify candidate retry exclusion.
