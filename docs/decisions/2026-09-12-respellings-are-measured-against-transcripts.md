# A respelling ships only where the engine is measurably heard to be wrong

**Status:** Current

The pronunciation lexicon is applied without a human gate. A recommendation's
`default` is loaded by `load_pronunciation_dictionary(include_defaults=True)`,
rewrites `spoken_text`, and `spoken_text` participates in the segment
manifest's dependency hash. So a respelling is not a suggestion sitting in a
review queue -- it is a pending edit to the audio, and the only reason nothing
had gone wrong was that the stored recommendations were all identity strings
that substituted nothing.

That changed the moment they were resolved properly.

## What the LLM proposed

`the-finest-edge-of-twilight-book` had 176 unresolved terms and Emberdark 93,
all carrying useless placeholders from the deterministic fallback -- `default`
equal to the term itself and an `alternate` that marked syllables with hyphens
the engine speaks as breaks.

One Qwen pass over both books (~2 minutes on the managed server, 176/176 and
93/93 resolved, no misses) produced real respellings. Rule 3 of the prompt --
"return it unchanged rather than inventing a variant" -- did most of the work:
only 28 and 5 terms came back changed.

Those 33 respellings were the entire risk surface, and reviewing them by
reading was not enough. `Regis` -> "Reegis" looked wrong to me because of the
hard *g*; `Wulfgar` -> "Wulfger" looked wrong because it moves the vowel. Both
were hunches. Two others I would have accepted.

## The evidence was already on disk

Every generated segment is transcribed by Whisper during validation, and the
transcript is kept in `quality_logs.details.transcribed_text` -- 2,667 lines
for the first book, 9,028 for Emberdark. What Whisper wrote is a phonetic
report of what the engine actually said.

| term | Whisper heard | reading |
| --- | --- | --- |
| `Catti-brie` | caddy bree x15, cadibri x14, cadbury x4 | mangled |
| `Xisis` | jesus x5, cysis x5, zesus x4 | mangled |
| `Braelin Janquay` | braylon x27 | half the name is gone |
| `Regis` | regis x56, nothing else | correct |
| `Wulfgar` | wolfgar x65, wulfgar x17 | correct; Whisper spells it its own way |
| `Drizzt` | drist x34, drizzt x18 | correct -- "drist" *is* the pronunciation |
| `Jarlaxle` | jarlaxle x107, jarl axel x2 | correct in 94% of lines |

Measuring the 33 proposals against that evidence kept **6** and refused
**27**. Eleven were for names the engine already said correctly. Applying them
would have replaced working audio with a guess -- and `Ten-Towns` -> "Tentowns"
would have been read back as "tent owns".

It also corrected a claim this project had been repeating: the "Jarl Axel"
failure is 2 lines out of 114, not a systemic mispronunciation.

## Comparing sound, not spelling

Two attempts failed before the comparison worked, and both failures pointed at
the same mistake -- treating Whisper's orthography as if it were phonetics.

**Exact string match** condemned `Wulfgar` and `Drizzt`, which the engine says
correctly. Fixed by scoring a phonetic key that discards vowel quality and
normalises ph/f, c/k, z/s, and `gu` before a vowel.

**First-word match** cleared `Braelin Janquay` at 94% while the engine was
dropping "Janquay" entirely. Fixed by matching the widest run of transcript
words against the whole term.

**Averaging** condemned `Ten-Towns`, whose long lines dragged the mean under
the threshold although the commonest rendering was "ten towns". The primary
test is now whether the *modal* rendering sounds like the term.

## Where the method stops

The phonetic key ignores vowel quality on purpose -- that is what lets
"wolfgar" match "wulfgar" -- so it cannot judge a term whose renderings differ
only in vowels. `Bruenor` came back as bruinor / brunor / "bryn or" /
briennor across 127 lines: one sound group by the key, four different names to
a listener.

Terms like that are reported `undecided`. They are not respelled, because
nothing measured the fix, and they are not called correct either. `undecided`
is a question for a human ear, and saying so is more useful than a confident
verdict in either direction.

Verdicts are `mispronounced`, `undecided`, `spoken_correctly`, or
`insufficient` (under three transcribed lines). **Only `mispronounced` applies
a respelling.**

## Two live defects this exposed

**Junk in the lexicon.** `You're` reached Emberdark's candidate list -- a
capitalised contraction at the head of a quotation looks like an unfamiliar
proper noun, and the dictionary check misses it because "you're" is not a
dictionary entry. The LLM proposed "Youre", which would have been substituted
into 79 lines across 24 chapters. `MW-` and the cast descriptor
`white-haired being` reached it the same way. Candidates are now filtered for
contractions, extractor fragments, and phrases built entirely from English
words; see `tests/test_pronunciation_candidate_noise.py`.

**A verified entry masking a measured failure.** Emberdark's project
dictionary held `"Xisis": "xisis"` -- a keep-original that overrides any
recommendation. The engine reads that name as "Jesus". The entry was almost
certainly generated rather than chosen, and it silently outranked the
evidence. Corrected to `Zeeziss`.

## Cost

The measured set regenerates 117 segments (66 across nine chapters of the
first book, 51 across eleven of Emberdark) rather than the 259 the unmeasured
set would have. Both books have published deliveries, so republishing those
parts is a separate decision and is deliberately not taken here.

## Consequences

- `shared/pronunciation_evidence.py` holds the comparison; thresholds are
  calibrated on these two books and the calibration set is named in the source.
- `scripts/measure_pronunciations.py <project> [--apply]` scores a project and,
  with `--apply`, resets everything not measured wrong to a no-op. The evidence
  is written to `pronunciation_measurement_audit.json` either way, so a refused
  respelling stays visible beside the reason it was refused.
- Run it after a Qwen resolution pass, before resuming generation. A respelling
  with no transcript behind it is a guess with a dependency hash attached.

## Related

- [Pronunciation lexicon ergonomics & Whisper STT language normalization](2026-09-07-pronunciation-lexicon-and-stt-validation.md)
- [../attribution-case-ledger.md](../attribution-case-ledger.md) -- the same
  evidence-before-shipping rule, for attribution
