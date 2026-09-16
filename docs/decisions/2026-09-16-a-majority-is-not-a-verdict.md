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

**`Catti-brie`: a fix exists and was declined.** Two rounds over fourteen lines:

| variant | on target | dominant rendering |
| --- | --- | --- |
| `CattiBrie` (current) | 1/14 | caddy bree x4, cadibri x3, cadbury x3 |
| `Kattybree` | 0/14 | cadbury x5 |
| `Kattibree` | 2/14 | cadabri x9 |
| `Cattibree` | 2/14 | cadabri x9 |
| **`Katteebree`** | **9/14** | ketibri x4, katibri x3 |

The second round was chosen from the phonology rather than by trying more
shapes. English flaps a /t/ between a stressed and an unstressed vowel, which
is what turns "CAT-ti" into "caddy"; doubling the vowel moves the stress onto
the second syllable and the flap stops. That is a 9x improvement and the
mechanism is understood.

**It was not applied.** `Katteebree` buys accuracy on the consonant by shifting
the stress to "ka-TEE-bree", where the name is "KAT-ee-bree", and applying it
regenerates 179 lines of a delivered book. Offered with this evidence on
2026-09-16, the owner chose to leave the entry alone. Recorded so the trade is
not rediscovered and re-argued: the option is real, the cost is a different
wrongness, and the decision was to keep what ships.

**`Do'Urden`: the punctuation hypothesis was refuted.** The normaliser joins
hyphens out but leaves apostrophes untouched, so `doh'urden` reaches the engine
with its apostrophe, and terms containing one fail at 50% against 13% for
everything else. Removing it looked obvious:

| variant | on target |
| --- | --- |
| `doh'urden` (current) | **6/14** |
| `dohurden` | 0/14 -- dohertyn x12 |
| `DohUrden` | 0/14 -- dohertyn x10 |

Both punctuation-free forms collapse completely. The apostrophe is holding a
syllable boundary nothing else supplies, and stripping it turns the name into
"Doherty-n". The asymmetry in `normalize_phonetic_text` is therefore **correct
as written**, and should not be "fixed" by symmetry with hyphens.

## Closing the loop in the pipeline, not beside it

Measuring and repairing by hand leaves the pipeline free to make the same
mistake tomorrow. Three changes move it upstream.

**The validator was excusing the failure.** `_glossary_adjusted_wer` discounts
ASR spelling variants for glossary names, so Whisper writing "wolfgar" for
`Wulfgar` costs nothing -- correct, and the reason it exists. But the test was
a 0.45 character ratio or a three-letter prefix, which forgave nearly any
rendering: **"drizzit" scored 0.92 and cost nothing**, so the retry that would
have redrawn it never fired.

No similarity threshold can separate these, because on the phonetic key the
*wrong* rendering scores higher than a *right* one:

| pair | key similarity | correct? |
| --- | --- | --- |
| `drizzt` / `drizzit` | **0.91** | no |
| `guenhwyvar` / `guinevar` | 0.89 | yes |

What separates them is beats. "drizzit" adds a syllable; "guinevar" simplifies
a consonant cluster and keeps all three. `same_spoken_form` forgives a
rendering when it is the same sound, or when Whisper split the name across
words ("coker lee" for `Kokerlii`), and withdraws forgiveness when the syllable
count moves. WER is still diluted by line length, so this fires where the name
dominates the line and the per-line repair covers the rest.

**The reference clip now demonstrates the hard names.** The engine conditions
on the reference audio, so whatever it hears there shapes every later line in
that voice. `select_reference_text` takes `priority_terms` -- the same glossary
the ASR is given -- and prefers lines containing them. The bonus is small on
purpose: a shouted `DRIZZT! DRIZZT!` fragment is still a bad reference. This is
the conditioning-domain answer to "reuse a known-good pronunciation", and
unlike splicing a cached word it costs nothing at generation time and leaves
prosody alone. It affects future bootstraps only.

**The instrument is pinned.** Whisper was called with defaults, including a
`temperature` tuple that resamples at rising temperature when a segment trips
its thresholds, and `condition_on_previous_text=True`. Transcribing one clean
segment three times gave identical text, so this was a tail risk rather than an
active defect -- but it is a tail risk on exactly the hard segments a verdict
depends on. Now `temperature=0.0` and no carry-over. `beam_size=5` was measured
(0.64s against 0.44s, identical text) and left out for want of evidence.

**And a trap worth naming.** Whisper's `initial_prompt` would reliably improve
transcription of rare names, and using it here would be self-defeating: this
validator is an unbiased phonetic reporter, and priming it with the correct
spelling makes it write "Drizzt" for audio that said "driz-ZIT" -- erasing the
signal the whole method reads. A test asserts it is absent.

## What the repair actually achieved

`Drizzt` went from **141/157 correct to 154/157** -- 90% to 98% -- by redrawing
sixteen takes and touching nothing else. Verified by transcribing the segments
as they stand rather than by trusting either repair log, which is a distinction
that mattered: the audit is a snapshot, so a second pass re-reads outliers an
earlier pass had already fixed and reports "gave up" for lines that are fine.
`repair_outlier_lines.py` now listens to each line before redrawing it, which
makes a re-run idempotent and its report true.

Three lines resisted three passes: `ch22_0353`, `ch24_0166`, `ch24_0169`. All
three are short -- 0.9 to 2.4 seconds -- and on a line that brief the name is
most of the content, with little surrounding context to condition it. They are
left as they are.

**A fourth defect, found by the repair failing.** Those three kept producing
correct takes that were then refused, and the cause was not the audio.
`validate_single` never received `validation_terms`, so on the `/validate` path
the glossary discount was dead code and a short line carrying a fictional name
was judged on raw WER -- one name out of three words is 0.33, past the 0.20
threshold, failing the hard gate however well it was spoken. Their original
records show all three passed first time round *only* via
`approved_glossary_spelling_variant`, the very discount the endpoint was not
applying. `ValidateRequest` now carries the glossary, which also un-penalises
the dashboard's preview and review flows.

That is the fourth defect of the same shape in this area: a place where a
fictional name is judged by a rule written for ordinary prose.

## What this does not fix

The lexicon enforces a *spelling*. Nothing constrains the phonemes the engine
picks for it -- there is no IPA or SSML layer -- so a respelling is still a
guess whose only test is generating the book and listening again. `Catti-brie`
is the proof that the guess can be wrong, and the loop that catches it is now
at least closed.

The 48 `unstable` terms in the first book and 16 in Emberdark are reports, not
a work queue. Which of them are worth a regeneration is a listening decision,
and deliberately not taken here.

**The repair is a script, not a stage.** `repair_outlier_lines.py` and
`remaster_chapters.py` are run by hand after a measurement. The validator gate
now prevents most of what they clean up, so the loop is closed for new
generation; an existing book still needs someone to run them.

**WER dilutes.** The gate fires on `"Drizzt nodded."` (one wrong word in two)
and not inside a thirty-word line, where the same error is 0.03. A name-only
gate independent of line length would catch both, and would cost every retry
on a name the engine cannot say -- `Catti-brie` alone would be 179 lines times
the retry limit. The workable form is to gate only on terms the audit measured
`spoken_correctly` or `unstable`, where a retry has somewhere to land. Not
built.

## Related

- [Respellings are measured against transcripts](2026-09-12-respellings-are-measured-against-transcripts.md)
  -- the rule this extends, and the source of every threshold it did not change
- [Pronunciation lexicon ergonomics & Whisper STT language normalization](2026-09-07-pronunciation-lexicon-and-stt-validation.md)
