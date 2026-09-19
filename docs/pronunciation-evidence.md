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

And the corollary, which cost a listener 27 chapters to notice: **a name that
is mostly right is not therefore right.** The engine samples per line, so an
unfamiliar spelling is re-guessed on every line and can lose about one time in
ten. That is the `unstable` verdict below.

The reasoning and the numbers are in
[decisions/2026-09-12-respellings-are-measured-against-transcripts.md](decisions/2026-09-12-respellings-are-measured-against-transcripts.md)
and [decisions/2026-09-16-a-majority-is-not-a-verdict.md](decisions/2026-09-16-a-majority-is-not-a-verdict.md).

## The loop

1. **Propose.** `build_pronunciation_inventory(..., use_llm=True, client=...)`
   asks the local model for respellings. It is told to return a term unchanged
   unless the spelling changes the sound, and it mostly does: 33 of 269 terms
   came back changed across two books.
2. **Measure.** `scripts/measure_pronunciations.py <project_id>` scores every
   term the lexicon touches — active entries, keep-originals, and unrespelled
   candidates alike — against `quality_logs.details.transcribed_text`, the
   Whisper transcript of every segment already generated. Nothing is written.
   Entries already shipped are included on purpose: `Catti-brie` carried a
   respelling that never worked, and measuring only proposals meant nothing
   looked at it again.
3. **Trial.** For a term the measurement flags, `scripts/trial_respelling.py
   <project_id> <term> <control> <candidate>...` generates each spelling over
   real lines and transcribes the takes. **The control comes first**, and it
   often wins: `Drizzt`'s twelve bad lines came back 11/12 correct with the
   text unchanged, while both candidates scored worse. Needs the voice server
   on 8100, which the dashboard does not start.
4. **Apply.** `measure_pronunciations.py --apply` keeps the respellings
   measured as `mispronounced`, as well as active working substitutions
   that score `spoken_correctly` (so existing audio manifests remain valid),
   and resets unneeded proposals to a no-op. It rewrites the
   recommendation file only — verified entries in `pronunciation_dict.json`
   are human decisions and are never touched. Evidence for every term, kept or
   refused, is written to `pronunciation_measurement_audit.json`.

Run step 2 before resuming generation. Step 4 is what makes the lexicon safe
to leave unattended.

## Verdicts

| verdict | meaning | effect |
| --- | --- | --- |
| `mispronounced` | the commonest thing Whisper wrote does not sound like the term | the respelling is applied |
| `unstable` | the commonest rendering is right, but the engine does not hold it across the book | reported with the offending lines; nothing applied |
| `spoken_correctly` | one rendering dominates and it sounds like the term | reset to no-op |
| `undecided` | renderings agree on consonants but scatter across vowels | reset to no-op, flagged for a listener |
| `insufficient` | fewer than three transcribed lines | reset to no-op |

**Only `mispronounced` applies a respelling.** The other four are reports.

`undecided` exists because the comparison is deliberately deaf to vowel
quality — that is what lets Whisper's "wolfgar" match a correctly spoken
`Wulfgar`. It cannot then distinguish "BROO-nor" from "BRIN-or", so for a term
whose renderings differ only in vowels it says so instead of guessing.

`unstable` exists because the majority is not the whole story. `Drizzt` was
scored `spoken_correctly` while 12 of its 157 lines said "driz-ZIT". The audit
records `outliers`, `minority_renderings` and `outlier_lines` for these, so the
verdict arrives with the lines to listen to rather than a percentage.

## When the evidence is older than the lexicon

`measure_pronunciations.py` refuses to call an active entry a failure unless
the book has been through the engine since the lexicon last changed, and says
so instead. Emberdark is the case it protects: its respellings were written on
2026-09-12 and its newest transcript is from 2026-09-03, so every entry there
would otherwise read as applied-and-failed when none has ever been spoken.
Regenerate before trusting a verdict on an active entry.

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
project dictionary first. `measure_pronunciations.py` now lists keep-originals
as their own entry kind, so one masking a failure shows up in the report rather
than having to be guessed at: `the-finest-edge-of-twilight-book` carries four,
of which `Janquay`, `Artemis Entreri` and `Jarlaxle` are measured `unstable`.

Identity mappings (`"Term": "Term"`) are rejected at write time by the pronunciation router, and `build_pronunciation_inventory` treats them as unmapped rather than verified so they cannot artificially inflate coverage. Furthermore, candidate generation targets the specific failure modes (`heard_as` transcript patterns, such as word-splitting or intervocalic flapping) rather than echoing the raw term.

## What this cannot fix

The lexicon substitutes a *spelling*. Nothing constrains which phonemes the
engine picks for it — there is no IPA or SSML layer — so a respelling is a
guess until `trial_respelling.py` generates it. `Catti-brie` is the standing
proof: an entry applied on all 179 of its lines, still heard four different
ways.

## Repairing the lines rather than the name

When a term is `unstable` the failure is usually per-draw, and the fix is new
takes for those lines — not a respelling. `scripts/repair_outlier_lines.py
<project_id> [--term X] [--apply]` reads `outlier_lines`, redraws exactly those
lines, and **keeps a take only when it is better**: the name lands in the right
sound group and the take still passes the hard gates. A line that will not come
good is left exactly as it was.

It is deliberately not a chapter regeneration. Redrawing a whole chapter
subjects every line to the same failure rate, clearing old errors while
introducing new ones.

A kept take moves four stores together (`shared/segment_repair.py`), or the next run undoes it:

| store | what changes | why |
| --- | --- | --- |
| the segment wav | replaced | the repair itself |
| `chapter_NNN.segments.json` | segment `output_hash`, manifest `manifest_hash` | the reconciler drops a chapter from `generated` when a stored hash stops matching the file |
| `voice_cache.db` | `generation_fingerprints.output_hash` | the generation cache compares it to the file before reusing a line |
| `pipeline_state.db` | `quality_logs` row with repaired transcript | measurement audits read the latest quality log per line to reflect current audio |

`dependency_hash` excludes `output_hash`, so it does not move and the chapter
is not re-derived. The chapter's **master goes stale on purpose** and must be
re-mastered (`scripts/remaster_chapters.py`), and any affected deliveries must be
re-exported (`scripts/reexport_deliveries.py --stale`) for the repair to reach the listener.

## Cross-term regression guard: no name left behind

When a line contains multiple glossary terms (e.g. `Drizzt` and `Do'Urden`, or
`Artemis` and `Entreri`), redrawing the take to fix one name must not degrade
another.

`is_pronunciation_candidate_better()` in `shared/pronunciation_evidence.py` evaluates
every glossary name on the line. A candidate take is **rejected immediately** if any
term that was correct in the incumbent take becomes wrong, even if the target term is
fixed. Only takes that maintain or improve the total count of correct names on the line
while meeting all hard quality gates are accepted.

Terms verdicted `mispronounced` are excluded from blind redrawing: when the dominant rendering is
wrong, redrawing only reshuffles the failure, and the name needs a respelling
or a listener.

## Related

- [quality-assurance.md](quality-assurance.md) — the validation pass that
  produces the transcripts this relies on.
- [attribution-case-ledger.md](attribution-case-ledger.md) — the same
  evidence-before-shipping discipline, applied to attribution rules.
