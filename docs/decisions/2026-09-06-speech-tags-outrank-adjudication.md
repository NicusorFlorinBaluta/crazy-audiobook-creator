# The book's speech tag outranks the model — 2026-09-06

**Status:** Current

Extends [2026-09-02 Tiered dialogue attribution auto-fix](2026-09-02-tiered-dialogue-attribution-autofix.md).
That record's Guardrail 2 (gender/pronoun consistency) is superseded by the
version described here; the other three guardrails are unchanged.

## The failure

On `the-finest-edge-of-twilight-book`, `ch11_0149` was attributed to Dahlia at
confidence 0.98 with `review_required: false`. The line and its neighbours:

```
ch11_0147 [effron]   "And I have even done you small favors, as you mention."
ch11_0148 [effron]   "But that is all. You never come to the house I have built…"
ch11_0149 [dahlia]   "You will never be invited into my tower, mother,"     <- accepted
ch11_0150 [narrator] he replied, then whispered in her ear,
```

The next line says outright that the speaker is male and the listener female.
Dahlia is female. The attribution was wrong, and nothing stopped it.

Two adjudicators had reasoned about this line and both inverted it. The local
model wrote *"the line addresses the speaker as 'mother'"* — it addresses the
**listener** as mother. Gemini wrote that the vocative was *"used by Effron when
speaking to Dahlia"* and then named Dahlia as the speaker in the same sentence.

## Why the guardrail that exists for this abstained

`_check_gender_pronoun_consistency` was given `f"{reason} {evidence_quote}"` —
**the model's own prose**, not the book's narration. Model reasoning is written
in analytical register ("Effron is her son", "the vocative address 'mother'")
and matches none of the narration patterns the guard searches for:

```
local_qwen_micro reasoning     male=False female=False  vetoes_dahlia=False
gemini_api_triage reasoning    male=False female=False  vetoes_dahlia=False
the script's own narration     male=True  female=False  vetoes_dahlia=True
```

The guard was checking the suspect's alibi instead of the evidence.

## The prompt was manufacturing the error, not merely missing it

Every neighbouring label was rendered flat, as established fact:

```
    [ch11_0147] effron: "And I have even done you small favors…"
    [ch11_0148] effron: "But that is all. You never come to the house…"
>>> [ch11_0149] dahlia: "You will never be invited into my tower, mother,"
```

directly above **Rule 1: "In two-party dialogue without explicit speech tags,
turns strictly ALTERNATE between speakers."** With 0147 and 0148 both wrongly
labelled `effron`, strict alternation *requires* flipping the target away from
Effron. The model was not being careless; it was reasoning correctly from
corrupted premises, under a rule that made the corruption binding.

Rule 1 is also **factually false for this book**. Measured on the shipped
script: same-speaker runs of 4 and 14 consecutive turns occur, and 196 runs of
≥3 turns exist in total.

## Decisions

### 1. A speech tag names several people; only its subject is speaking

`_dialogue_tag_evidence` accepted any registered name near a speech verb. Three
guards now apply:

- a **pronoun subject** ("he said") settles the speaker, so a name after that
  verb is the addressee — inversion is suppressed for that verb and the
  pronoun's gender stands as the evidence instead
- a **clause boundary** between verb and name means the name belongs to the
  next clause
- a **possessive** modifies the real subject rather than being it

A fourth guard covers a data shape rather than a grammar one: a *possessive
alias* (`"Effron's"` → tokens `('effron','s')`) absorbed the very possessive
marker it should have been rejected for, so a name whose final token is a bare
`s` is no longer treated as a name. Aliases that merely *contain* a possessive
("Doregardo's second" is how the book names a character) still resolve.

Measured effect on the 11 tagged lines whose stored speaker contradicted the
book: 8 were caused by these defects.

```
"he asked Breezy."                      -> brie        (Breezy is addressed)
"he said, and Breezy gave a low growl." -> brie        (a following clause)
"she asked Jarlaxle."                   -> jarlaxle    (addressed)
"added Breezy, and Effron's head..."    -> effron_son  (a possessive)
```

All four were written by `repair_deterministic_named_attribution` at
**confidence 1.0 with `review_required: false`**, so they reached neither an LLM
nor the review inbox.

### 2. The attached tag outranks micro-adjudication

The guardrail now reads the narration the author attached, parsed by the same
`_dialogue_tag_evidence` used everywhere else:

- a tag that **names** someone is the answer, not evidence to weigh. The
  micro-adjudication is overruled, recorded under its own
  `deterministic_attached_tag` resolver and counted as `tag_overruled` in the
  report, so drift between the model and the book is visible.
- a tag that yields only a **gender** cannot name a winner but is decisive about
  who did *not* speak, so the line escalates rather than being accepted.

The model's prose is still checked, as a second and weaker signal.

### 3. Only a pronoun that is doing the speaking may veto

`_dialogue_tag_evidence` ends with a fallback: a lone `he`/`she` anywhere in the
tag yields that gender. On `ch28_0028` —

```
"the seated halfling said, then hopping to stand and bow as she neared."
```

— the only pronoun is the traveller approaching, while Ghaliver, who names
himself in the line above, is male. That looseness was survivable while the
gender was advisory. It is not once a tag can *refuse* an attribution: blocking
a correct answer is a worse failure than the one the veto fixes. The veto and
the prompt annotation therefore act only on gender tied to the speech verb.

### 4. Neighbouring labels carry what backs them

Each neighbour in the tier-1 prompt is now marked `[confirmed by the speech tag
below]`, `[CONTRADICTED: …]`, or `[unverified]`, and Rule 1 was rewritten: an
`[unverified]` neighbour is a previous guess, not evidence, and *"if they are
the only reason to change the target, they are the more likely thing to be
wrong."*

The Gemini tier carried the same rule verbatim in
`GeminiValidationService`'s grounding prompt — and `ch11_0149` was originally
mis-resolved by `gemini_api_triage`, that very tier. Its Rule 2 is now a
tendency rather than a law, explicitly subordinate to its own Rule 5 (the
speech tag must match), with the same instruction not to flip a quote merely to
satisfy alternation against untagged neighbours.

### 5. An alias may never be another character's canonical name

The cast contained `effron_son` — name *"Effron's Son"*, `age_range: unknown`,
4 lines, its own voice — a possessive misparsed into a whole character, holding
the aliases `['son', "Effron's Son", "Effron's", 'Effron']`.

Because the parser abstains when a name has more than one owner, claiming the
bare name `Effron` meant `"said Effron."` resolved to **nobody**. A spurious
4-line entry was silently suppressing every speech tag naming a 110-line
character, across the whole book.

Such an alias is now ignored. This is safe in a way that dropping a character
would not be: it only removes a claim that already resolved to nothing, and it
returns the name to its real owner. Descriptors genuinely shared between
characters are untouched, because they are nobody's canonical name — this cast
has `copper dragon` and `sister` on both dragon sisters, `dwarf`/`smith` on two
smiths, and `queen` on two queens. Abstaining on those remains correct.

## Rejected: propagating alternation between anchors

The obvious next step — anchor on lines the book names, and propagate turn
alternation between anchors — was measured before building and **does not
survive contact with the data**:

```
dialogue blocks (>=3 turns)   : 154
  strictly two-party          :  96
  named anchors found         :  29
  blocks with >=2 anchors     :   5      <- the entire addressable set
  parity conflicts            :   2      <- both false positives
```

Named anchors are too sparse to constrain anything, and the two conflicts it did
find were legitimate: `ch13_0069..0079` is four consecutive Drizzt turns, both
ends confirmed by tags (*"Drizzt reminded her"*, *"Drizzt went on"*), with Breezy
only snorting in between. Shipping this would have added a detector with 5
blocks of coverage and a 100% false-positive rate.

The underlying reason is the same fact that broke Rule 1: strict alternation is
false in real dialogue. See
[../plans/targeted-block-adjudication-2026-09-06.md](../plans/targeted-block-adjudication-2026-09-06.md)
for the approach that replaces it.

## Result

Measured on the shipped script, 3,125 spoken lines, 490 carrying a speech tag
that names or genders the speaker:

| | before | after |
| --- | --- | --- |
| Tagged lines contradicting the book | 11 | **0** of 490 |
| Silent reattributions | 30 | **16** |
| Escalated for review | 130 | **143** |
| Gemini contradictions needing revert | 10 | **1** |
| Blocking review items | 24 | **8** |

The review queue rising from 3 to 8 between the last two runs is the intended
direction, not a regression: lines the model wanted to flip but the book refutes
now reach a human instead of being accepted at 0.98.

## What this does not fix

> **Correction, 2026-09-10.** The 490 below is not the number of speech tags in
> the book. It is the number this machinery can **see**. Every tag check here
> requires the following narrator line to begin lower-case — the mark of a line
> continuing the quoted sentence — and that excludes another **574** narrator
> lines of the form `<Name> <speech verb>`, written as their own sentence.
>
> The exclusion is deliberate and its reasoning is sound as far as it goes:
> `_attached_tag_evidence` warns that capital-led narration may be a reaction
> ("Dahlia laughed at that.") and "reading those as tags is how a bystander ends
> up owning the line." Measured on this book, that is true of reaction verbs and
> false of speech verbs:
>
> ```
> capital-led, speech-verb        capital-led, reaction-verb
>   trailing    253 (36%)           trailing      4 (2%)
>   leading       1 (0%)            leading       8 (4%)
>   both-same   296 (42%)           both-same     3 (1%)
>   neither       4 (1%)            neither       6 (3%)
>   unparsed    152 (22%)           unparsed    194 (90%)
> ```
>
> Of 554 capital-led speech-verb tags the parser can read, **549 are consistent
> with a trailing tag and exactly 1 names the following speaker instead**. The
> parser is not the limitation either — `_dialogue_tag_evidence("Gregory replied
> with a blank stare.")` returns `gregory_antoine` today; it is simply never
> asked. One stored speaker in the ignored set contradicts its tag
> (`ch09_0108`), and a second (`ch13_0362`) was found by the block-adjudication
> diff.
>
> Not yet changed. See
> [the review record](2026-09-10-review-of-the-september-feature-run.md) for the
> proposal and its risks.

Speech tags cover **490 of 3,125 spoken lines (16%)**. Nothing here can see the
other 84%. The same chapter still demonstrates it: `ch11_0149` is now correctly
`effron`, but `ch11_0147` and `ch11_0148` remain `effron` and read as Dahlia's
complaint answered by Effron's reply — 0148 places the tower with the listener,
0149 places it with the speaker, so they cannot share a speaker. Neither line
carries a tag.
