# Pronunciation evidence

**Status:** Reference — Describes current behaviour. Keep it accurate when the code changes.

A respelling in the pronunciation lexicon is not a suggestion. It is applied
without a human gate, and it changes audio:

```
pronunciation_recommendations.json  ->  load_pronunciation_dictionary(include_defaults=True)
                                    ->  apply_pronunciations()  ->  line.spoken_text
                                    ->  spoken_text_hash  ->  manifest dependency_hash
                                    ->  those segments regenerate
```

So a term with a respelling nothing measured is a pending edit to the book.
The rule is therefore: **a respelling ships only where the engine is measurably
heard to say the name wrong.**

The reasoning and the numbers behind that rule are in
[decisions/2026-09-12-respellings-are-measured-against-transcripts.md](decisions/2026-09-12-respellings-are-measured-against-transcripts.md).

## The loop

1. **Propose.** `build_pronunciation_inventory(..., use_llm=True, client=...)`
   asks the local model for respellings. It is told to return a term unchanged
   unless the spelling changes the sound, and it mostly does: 33 of 269 terms
   came back changed across two books.
2. **Measure.** `scripts/measure_pronunciations.py <project_id>` scores each
   proposal against `quality_logs.details.transcribed_text` — the Whisper
   transcript of every segment already generated. Nothing is written.
3. **Apply.** The same command with `--apply` keeps only the respellings
   measured as `mispronounced` and resets the rest to a no-op. Evidence for
   every term, kept or refused, is written to
   `pronunciation_measurement_audit.json`.

Run step 2 before resuming generation. Step 3 is what makes the lexicon safe
to leave unattended.

## Verdicts

| verdict | meaning | effect |
| --- | --- | --- |
| `mispronounced` | the commonest thing Whisper wrote does not sound like the term | the respelling is applied |
| `spoken_correctly` | the commonest rendering sounds like the term, and one rendering dominates | reset to no-op |
| `undecided` | renderings agree on consonants but scatter across vowels | reset to no-op, flagged for a listener |
| `insufficient` | fewer than three transcribed lines | reset to no-op |

`undecided` exists because the comparison is deliberately deaf to vowel
quality — that is what lets Whisper's "wolfgar" match a correctly spoken
`Wulfgar`. It cannot then distinguish "BROO-nor" from "BRIN-or", so for a term
whose renderings differ only in vowels it says so instead of guessing.

## Reading the evidence

`heard_as` in the audit is the raw transcript spellings and their counts. It
is the part worth reading: `caddy bree x15, cadibri x14, cadbury x4` is a
failure anyone can confirm, and `regis x56` is a name that needs no help.

Whisper picks its own spelling for an unfamiliar name, so a rendering that
looks wrong often is not — "drist" is how `Drizzt` is meant to sound. Compare
sounds, and read the counts.

## What must not enter the lexicon

A term with no pronunciation to get right is not harmless: the model will
propose a respelling for it and that respelling will be applied. Three shapes
are filtered out, each found live on 2026-09-12:

- **contractions** — "You're" heads a quotation, so it looks capitalised and
  mid-sentence, and the dictionary check misses it because the contraction is
  not a dictionary entry;
- **extractor fragments** — `MW-`;
- **descriptors built from English words** — Emberdark casts a
  "White-Haired Being", and cast aliases are exempt from the dictionary check
  by design.

A hyphenated compound is one word, not a phrase: `Catti-brie` and `Ten-Towns`
must survive the filter, and `tests/test_pronunciation_candidate_noise.py`
holds them there.

## Verified entries outrank evidence

`pronunciation_dict.json` (project, then global) overrides recommendations,
including an entry that maps a term to itself — the "keep original"
convention. Emberdark carried `"Xisis": "xisis"` while the engine read that
name as "Jesus", and the measurement could not correct it.

When a measured `mispronounced` term shows no active substitution, check the
project dictionary first.

## Related

- [quality-assurance.md](quality-assurance.md) — the validation pass that
  produces the transcripts this relies on.
- [attribution-case-ledger.md](attribution-case-ledger.md) — the same
  evidence-before-shipping discipline, applied to attribution rules.
