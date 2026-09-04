# Whole-cast duplicate detection — 2026-09-04

**Status:** Current

## The gap, measured

`CharacterAnalyzer._adjudicate_name_candidates` already merges duplicate
registry entries, and it is careful about it: a name-containment match only
*proposes* a pair, and a merge additionally requires a positive LLM identity
decision backed by source evidence.

But it only ever proposes a pair from **lexical** overlap — a shared
distinctive token, or one id being a suffix of the other (`pwent` ↔
`thibbledorf_pwent`). Run over the real 57-character cast of
`the-finest-edge-of-twilight-book`:

> **24 of 1,540 pairs (1.6%) can ever be considered. 1,516 are structurally
> invisible.**

A character recorded once by proper name and again by appellative shares no
token, so no candidate is ever generated, nothing adjudicates it, and the
duplicate reaches casting as a second character with its own voice, its own
reference clip, and its own share of the dialogue. That cast already carries
`zaknafein` with the alias "the weapons master" and `avelyere` with "the
veteran wizard" — the shape is present in the data.

Separately, `augment_characters` cannot help: its decision type is
`update | abstain` over per-character *attributes*. It has no vocabulary for
merge, split, or delete, and it never sees the cast as a set.

## The decision

A whole-cast pass after book-wide character analysis, before augmentation and
voice assignment — there is no point enriching or casting a voice for an entry
about to be absorbed.

Two Gemini stages and a local veto layer:

1. **Roster.** The entire cast goes in one prompt as names, aliases, gender and
   dialogue counts — **no book text**. A model reading the roster can spot
   "Jarlaxle" and "Uncle Jax" without any passage, and keeping the source out
   makes the call cheap.
2. **Local vetoes.** Deterministic, and a model proposal can only ever survive
   them, never override one.
3. **Grounding.** Surviving pairs get a second call with evidence snippets and
   must return a citation that is a literal substring of what was supplied.
   Ungrounded merge claims are dropped, never applied.
4. **Apply**, if the confidence clears `min_confidence` (default 0.95 — higher
   than an attribute enrichment, because a merge gives two characters one voice
   for a whole book).

### The conjunction veto, and why proximity was rejected

The load-bearing guard: if the source ever names the two as separate
participants, they are two people.

The obvious version — do the names appear near each other — **does not work**,
and this was measured rather than assumed. Counting occurrences within 200
characters in the real book:

| Pair | Relationship | Co-occurrences |
| --- | --- | --- |
| Ilnezhara / Tazmikella | distinct twins | 6 |
| Jarlaxle / Uncle Jax | **same person** | 10 |
| Regis / Rumblebelly | **same person** | 15 |

Aliases co-occur *more* than distinct characters, because prose introduces an
alias right beside the name it replaces. Proximity is worse than useless here.

Conjunction separates them cleanly. Across eight probe pairs, every distinct
pair was conjoined at least once and no alias pair ever was:

| Distinct | count | Same person | count |
| --- | --- | --- | --- |
| Ilnezhara / Tazmikella | 2 | Jarlaxle / Uncle Jax | 0 |
| Bruenor / Drizzt | 2 | Regis / Rumblebelly | 0 |
| Catti-brie / Wulfgar | 1 | Drizzt / Drizzt Do'Urden | 0 |
| Entreri / Jarlaxle | 3 | Catti-brie / Catti-brie Do'Urden | 0 |

A **bare comma is deliberately not enough**. "Jarlaxle, Uncle Jax to the girl,
swept off his hat" is apposition naming one person; vetoing on that would
refuse exactly the merges this feature exists to find. The list rule therefore
requires the list to continue — `A, B, and C` or `A, B and C` — which is the
form the real text actually uses ("Bruenor, Drizzt, and Catti-brie").

### The other vetoes

- The narrator is never merged.
- Explicit genders must not disagree. `other` means unresolved, not a third
  gender, so it does not block.
- **Positional siblings.** Ids differing only by a numeric or positional marker
  — `dwarf_blacksmith_1` / `_2`, `driver_left` / `driver_right` — are two
  anonymous people the book never named. Every term they own is generic, so the
  conjunction veto is blind to them; this catches them instead.
- Terms made only of generic words ("the dwarf", "the elf") cannot carry a
  veto, or the guard would refuse real merges.

### Measured veto performance

Run over the real cast against ten pairs that must be refused: **9 of 10
refused**, each for a stated reason.

The miss is `zaknafein` / `drizzt` — father and son. The book never conjoins
them directly; the closest forms are "Drizzt and Catti-brie, Jarlaxle and
Zaknafein" (a list of *pairs*) and "Drizzt and Catti-brie are with Zaknafein"
(intervening words). Widening the regex to catch it would start matching
apposition, so the veto stops here and the grounded adjudication stage carries
that case. **The veto is a safety net for what text can settle
deterministically, not a complete classifier.**

## The approval gate is optional, and off by default

`require_approval: false` applies merges that clear every veto and the
confidence bar, recording all of them — including refusals and the reason — in
`cast_identity_audit.json`.

This is the deliberate default, and the reason is spoilers. Approving a merge
means reading the verbatim excerpts that justify it, which is a plot summary of
a book the operator has not read yet. The audit file lives in the project
directory rather than the review inbox for the same reason: it is available
when wanted, and never pushed in front of anyone.

`require_approval: true` holds every merge instead. Safer, and the right choice
for a book already read or a cast that must be exactly right.

## Unlinked speaker recovery

A second, measured gap, found while answering "could Gemini's general knowledge
recover missing speakers?".

> **"Zak" appears 38 times in the real book and speaks repeatedly** — *"Zak
> said"*, *"Zak explained"*, *"Zak admitted"* — while the registry holds
> Zaknafein with aliases `["the weapons master", "Zaknafein"]` and no "Zak".

Nothing links them. "Zak" is not a registry entry, so identity adjudication
never sees it; it is not lexically derivable from `zaknafein` either. Every one
of those attributions is unresolvable at scripting time.

Note the category: this is a **missing alias on an existing character**, not a
missing character. Neither the original design nor the question that prompted it
had that case in view.

`cast_identity.find_unlinked_speakers` scans for names attributed with a speech
verb that no registry entry answers to. On the real book, a floor of 2
attributions yields three candidates — `Zak` ×12, `Spider` ×3, `Ten-` ×2 —
against sixteen at a floor of 1, where the extra thirteen are pronouns and
place names the regex catches (`Taulmaril` is a bow, `Kryptgarden` a forest).
The floor decides nothing; it bounds how much is sent for adjudication.

Each candidate is classified `alias` / `new_character` / `not_a_person` /
`abstain` against verbatim excerpts. **Only `alias` is acted on.** Its vetoes:
the alias must appear verbatim in the source, must not already belong to
another character (two characters answering to one name leaves attribution
unable to choose), must not be a pronoun, and the narrator takes none.

## On Gemini's general knowledge, and web search

The original idea was to ask Gemini which characters speak, using what it knows
about the book. Tested against both books in the library:

| Book | Result |
| --- | --- |
| The Finest Edge of Twilight — R. A. Salvatore | empty list |
| Isles of the Emberdark — Brandon Sanderson | empty list |

Asked again **without** permission to decline — the phrasing that should
provoke confabulation — it still returned zero names. So the failure mode is
not hallucination for these books; it is simply that both are recent releases
outside training data.

That makes the feature's value inversely correlated with the need: useful for
classics, empty for exactly the new fiction this pipeline processes. Not built.

**Search grounding was tested**, through the persistent Gemini web session
rather than the API — the web tier has search built in and does not consume the
API quota, which had been exhausted by the experiments above. Asked to search
for the book and list only characters it could support for *this* volume:

| Metric | Result |
| --- | --- |
| Names returned | 2 — `Breezy Do'Urden`, `Wulfgar` |
| Sources cited | 1 — an AbeBooks product listing |
| Already in the registry | 2 |
| Recovered (in text, missing from registry) | **0** |
| Hallucinated (absent from the book) | **0** |

So it is **safe but low-yield**. It invented nothing, and the predicted
series-wiki failure did not occur — but only because no wiki exists for this
book yet; the single source was a bookseller page, which is exactly the
material a new release has. It recovered nothing the pipeline did not already
know.

Not built, on that evidence. The caution stands for a book that *does* have a
wiki: a series wiki lists the series cast, not this volume's speakers, so
grounding could return confidently-sourced names of characters who never appear
in it — a worse failure than an empty answer, because it looks well-supported.
Any future implementation keeps the same rule as everything else here: a name
absent from the extracted text is rejected, whatever the citation says.

This run also served as the **first live verification of the web escalation
path** in this codebase — the browser profile, the persistent per-purpose
conversation, and JSON extraction all worked end to end.

## Not built, and why — revisit with evidence

### Spurious entries (deleting a "character" that is not a person)

The original objection was that deleting destroys dialogue. **That objection
was wrong for this pass**, and the correction belongs on the record: it assumed
post-attribution timing, and this runs *before* attribution. At that point no
lines are assigned, so deletion loses only pass 1's estimated `dialogue_count`.
If a real character were deleted, attribution would have no candidate and would
mis-assign or flag — and the attribution gate already blocks on unresolved
dialogue, so the failure is visible rather than silent.

It is not built because there is **no measured case**. The scan's
`not_a_person` verdict already records candidates of this shape without acting
on them; if `cast_identity_audit.json` starts showing real registry entries
classified that way, that is the evidence to build on.

### Splits (one entry covering two people)

Also defused by the timing: pre-attribution, a split is just "add the second
character to the registry" and let attribution decide which lines go where. No
re-attribution pass is needed.

Not built for the same reason — no measured case in this library. The test
would be a registry entry whose dialogue, on inspection, is spoken by two
different people.

### New characters

The `new_character` verdict is recorded but never applied. Adding a character
creates a voice, a reference clip and an attribution candidate; doing that on
an unmeasured verdict is a bigger bet than annotating an existing entry. Build
it when the audit shows a grounded `new_character` that is genuinely absent.

## Verification

`tests/test_cast_identity.py`, 36 tests, no model loaded. The refusals are the
important half:

- the twins are refused, with the conjunction reason;
- a genuine alias pair is allowed;
- **a local veto overrides a model proposal at confidence 1.0**;
- the narrator, self-merges and unknown ids are refused;
- disagreeing explicit genders are refused; `other` is not treated as one;
- positional and numbered siblings are refused, and unrelated numbered ids are
  not mistaken for siblings;
- serial lists count as conjunction, apposition does not;
- generic-only terms cannot carry a veto;
- a merge combines dialogue counts and keeps every absorbed name as an alias,
  so attribution can still resolve lines written under the old id;
- the roster prompt carries no source text;
- the approval gate holds merges instead of applying them;
- a disabled adjudicator is a no-op that writes nothing;
- a speaking name absent from the registry is found, and a known one is not;
- pronouns and sentence openers are not reported as speakers;
- an invented alias, an alias owned by another character, a pronoun alias and
  an unknown target are all refused;
- applying an alias is additive and idempotent.

All three Gemini calls escalate `gemini_api_triage` → `gemini_api_adjudication`
→ `gemini_web`, matching the rest of the module. The first implementation of
the cast pass was API-only, which meant a 429 disabled it entirely rather than
falling back to the browser session — fixed.

**Not yet run against the live Gemini API.** The local guards are measured
against real book text; the two Gemini stages are covered only by fakes. The
next real bootstrap on a book with a known duplicate is what will show whether
the roster stage proposes the right pairs and whether grounding holds. Read
`cast_identity_audit.json` — `trace` records every proposal and why it was
applied, refused or dropped.

## Related

- [README.md](README.md) — status convention and index
- [../architecture.md](../architecture.md) — character and voice model
- [../character-augmentation-and-gender-resolution-2026-08-19.md](../character-augmentation-and-gender-resolution-2026-08-19.md)
