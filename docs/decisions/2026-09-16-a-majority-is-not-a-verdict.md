# A majority is not a verdict: pronunciations are measured for stability

**Status:** Current

A listener reached chapter 27 of `the-finest-edge-of-twilight-book` and said
`Drizzt` was being pronounced two different ways. It was. The measurement pass
four days earlier had scored that name `spoken_correctly` and moved on.

Both statements were true at once, and the gap between them is the decision
this record makes.

## What the listener heard

157 of the book's lines say the name. Whisper's transcripts of them:

| rendering | lines |
| --- | --- |
| drist / drizzt -- one syllable, correct | 141 |
| **drizzit / drizit / drizzet -- two syllables** | **12** |
| dris / driz -- truncated | 2 |
| trist, "desires" | 2 |

Twelve lines in twenty-five chapters, which is roughly one every other chapter:
often enough for the first person to sit through the book to notice, rare
enough that every aggregate the pipeline computed called the name correct.

## Why the gate passed it

`TermEvidence.verdict` asked two questions: does the *commonest* rendering
sound like the term, and does the commonest *spelling* hold half the lines.
Drizzt answered yes to both. `stability` -- the share held by the dominant
sound group -- was computed, written into the audit as `0.897`, and never
consulted by anything.

That is the whole defect. The 2026-09-12 record was right that the modal
rendering must be the primary test, because averaging condemned `Ten-Towns`
for line-length artifacts. It did not follow that the minority could be
discarded.

**Sampling is per line, and so is the seed.** `_line_seed` derives from
`(project, line_id, text, voice, attempt)`, so each line is an independent draw
at an unfamiliar orthography *and* a permanent one. The twelve bad takes are a
fixed, nameable set. They do not heal on a rerun, and no amount of regenerating
the other 141 touches them.

## The second thing measurement was not looking at

Widening the term set to everything the lexicon touches -- not just the
respellings an LLM had recently proposed -- turned up the same failure from the
other side.

`Catti-brie` carries a verified respelling, `CattiBrie`, applied on all 179 of
its lines. The audio is heard as cadibri x50, cadbury x49, caddy bree x23,
katibri x12. The substitution happens every time; the pronunciation still does
not. Nothing had looked at it since the day it shipped, because once a
respelling moves into `pronunciation_dict.json` it stops being a proposal and
the script only measured proposals.

Five of this book's twelve active entries are in that state. Three others
are the reason to keep the mechanism:

| entry | lines | outside the dominant sound |
| --- | --- | --- |
| `Luskan` -> `Laskan` | 27 | 0 |
| `Bruenor` -> `brewnor` | 186 | 0 |
| `Guenhwyvar` -> `Gwenevar` | 11 | 0 |
| `Do'Urden` -> `doh'urden` | 45 | 23 |
| `Catti-brie` -> `CattiBrie` | 179 | 87 |

A respelling that works leaves nothing behind. That is what makes the
measurement worth running: the successes are as legible as the failures.

## What changed

`SOUND_STABLE_THRESHOLD = 0.95` and a fifth verdict, `unstable`: the dominant
rendering is right, but the engine does not hold it across the book. It is
tested before the spelling check because it is the more actionable of the two
-- `unstable` names specific lines, `undecided` only reports that the method
cannot tell. Calibrated on this book's regenerated audio, where Luskan,
Bruenor and Guenhwyvar sit at 1.00 and Wulfgar and Regis at 0.99, against
Jarlaxle at 0.94 and Drizzt at 0.90.

**`unstable` never ships a respelling.** Only `mispronounced` does. The 09-12
rule that a respelling needs measured evidence behind it is unchanged; this
adds a report, not a new licence to guess.

The evidence record now carries `outliers`, `minority_renderings` and
`outlier_lines`, so a verdict arrives with the line ids to listen to rather
than a percentage.

## Three defects found while doing it

**Multi-word terms were evidenced by the wrong lines.** Selecting a term's
lines on its first word counted 189 "Gregory" lines as evidence about "Gregory
Antoine", found "gregory" in the transcripts, and called the name
mispronounced -- for a surname the script never asked the engine to say. Lines
must contain the whole term. The `Braelin Janquay` case that motivated whole-
term *span* matching in 09-12 is unaffected: its lines do say the full name,
and the engine is what drops half of it.

**The recommendation file accumulates fragments.** `brie` is in it, left over
from splitting `Catti-brie`. Seeded into the term set it matched all 179 of
that name's lines and topped the report. The term set is now drawn from the
lexicon and the curated inventory, never from the recommendation file's keys.

**Evidence can predate the lexicon.** Emberdark's respellings were written on
2026-09-12, including the `Xisis` -> `Zeeziss` fix for a name read as "Jesus";
its newest transcript is from 2026-09-03, because republishing was deliberately
deferred. Measured naively, every Emberdark entry reads as a respelling that
was applied and failed. None has ever been spoken. `measure_pronunciations.py`
now refuses to call an active entry a failure unless the book has been through
the engine since the lexicon last changed.

## The candidates were then generated, and both were refused

Measuring is half of it. `scripts/trial_respelling.py` synthesizes a candidate
over real lines and transcribes each take, so a respelling can be judged before
it reaches the lexicon. **The first argument is always the control**, and that
is what the two trials turned on.

**`Drizzt`: the control won.** Run over the twelve lines whose stored
transcript says "driz-ZIT", with the text unchanged:

| variant | on target | heard |
| --- | --- | --- |
| `Drizzt` (control) | **11/12** | drist x7, drizzt x4, drizzit x1 |
| `Drist` | 10/12 | drist x10, **dris x2** |
| `Drizt` | 10/12 | drist x8, **driss x2** |

Those twelve lines are not prone to the failure. Redrawn, eleven come back
right -- 92%, which is the book's own rate. **The failure is a per-draw event
at roughly one line in ten, not a property of the name or the context.** Both
candidates scored worse and each invented a rendering the book had never
produced. A book-wide substitution across 157 lines in 25 chapters would have
been shipped to fix twelve unlucky takes.

**`Catti-brie`: nothing was good enough to ship.** Fourteen lines spread across
the book:

| variant | on target | dominant rendering |
| --- | --- | --- |
| `CattiBrie` (current) | 1/14 | caddy bree x4, cadibri x3, cadbury x3 |
| `Kattybree` | 0/14 | cadbury x5, caddy bree x3 |
| `Kattibree` | 4/14 | cadabri x8 |

`Kattibree` is the best of the three and still wrong two times in three. It is
recorded here because it is interesting in the wrong direction: it is the most
*consistent* of the three -- one rendering on 8 of 14 lines against the
current entry's four-way spread -- while being no more correct. Consistency and
accuracy came apart, and no metric in this repository can choose between them.
That one is a listening decision and is left open.

## What this does not fix

The lexicon enforces a *spelling*. Nothing constrains the phonemes the engine
picks for it -- there is no IPA or SSML layer -- so a respelling is still a
guess whose only test is generating the book and listening again. `Catti-brie`
is the proof that the guess can be wrong, and the loop that catches it is now
at least closed.

The 48 `unstable` terms in the first book and 16 in Emberdark are reports, not
a work queue. Which of them are worth a regeneration is a listening decision,
and deliberately not taken here.

**There is no per-line repair.** The `Drizzt` trial says the fix for a
per-draw failure is redrawing those twelve takes, and the pipeline's only
regeneration granularity is the chapter -- which would redraw every line in
25 chapters at the same one-in-ten rate, introducing fresh failures while
clearing the old ones. Validation cannot catch these either: "drizzit" for
"Drizzt" is a small enough edit distance to pass WER. The missing piece is a
repair keyed to `outlier_lines`, reusing the line-level regeneration
validation already has and the `attempt` bump that gives it a fresh seed.

## Related

- [Respellings are measured against transcripts](2026-09-12-respellings-are-measured-against-transcripts.md)
  -- the rule this extends, and the source of every threshold it did not change
- [Pronunciation lexicon ergonomics & Whisper STT language normalization](2026-09-07-pronunciation-lexicon-and-stt-validation.md)
