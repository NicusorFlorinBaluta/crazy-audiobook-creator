# Attribution case ledger

**Status:** Current

Every attribution rule that ships, the real line that forced it, what it was
measured at, and the test that stops it regressing. One row per rule, not per
incident: a rule with no line behind it was guessed at, and a line with no test
behind it will come back.

The `case` column is a real `line_id` from a book in `brain/projects`. The
`test` column is enforced — `tests/test_case_ledger.py` fails if a named test
file stops existing or stops mentioning its case.

Rejected rules are listed too, in their own table. Three of them were
re-proposed within a day of being rejected, twice by me, which is the argument
for writing them down.

## Rules in force

| case | what was wrong | rule | measured | test |
| --- | --- | --- | --- | --- |
| `ch11_0149` | accepted as Dahlia at 0.98 with *"he replied"* on the next line | the book's speech tag outranks the model | 11 of 490 tagged lines contradicted | `tests/test_speech_tag_subject.py` |
| `ch28_0028` | *"…as she neared"* read as gendering Ghaliver | a lone pronoun not tied to the speech verb may not veto | — | `tests/test_speech_tag_subject.py` |
| `ch13_0362` | *"Savahn flatly stated."* skipped for starting with a capital | a capital-led sentence can be a speech tag | coverage 324→883 and 755→1584 | `tests/test_speech_tag_subject.py` |
| `ch38_0057` | `one_of_the_ones_above_male` flagged against *"the man said"* | a generic description establishes a **gender and nothing else** | 3 real lines, all stored speakers correct | `tests/test_refutation_pass.py` |
| `ch11_0148` | Effron owns and disowns the tower in one unbroken turn | possessive contradiction refutes the speaker | 2 findings across two books | `tests/test_deterministic_refutation.py` |
| `ch38_0118` | *"the man said to Dusk."* — Dusk is addressed, not speaking | the addressee of a tag did not speak it | resolved to `dajer`, verified in text | `tests/test_refutation_pass.py` |
| `ch11_0222` | resolved by the block path at 0.95, wrong | retry sub-threshold lines with ~4× context | 4 of 150 retried (2.7%), all settled, +3.4% wall | `tests/test_wide_context_cascade.py` |
| `ch40_0090` | *"He squatted near Dusk and muttered,"* named Dusk | a name governed by a preposition is its object | 7 of 3,274 readings changed, none correct lost | `tests/test_tag_subject_governance.py` |
| `ch24_0096` | Athrogate's rhyming couplet stored as Jarlaxle | (found by the rule above) | — | `tests/test_tag_subject_governance.py` |
| `ch01_0295` | *"Effron spun around and glared at her."* — no speech verb, so invisible | an action beat attributes the quote **sharing its paragraph** | 2,584 quotes covered, 98.3% | `tests/test_action_beat_attribution.py` |
| `ch01_0130` | *"…the driver's cries of "Whoa!""* — the quote is the sentence's object | a beat must be a complete sentence | — | `tests/test_action_beat_attribution.py` |
| `ch08_0244` | Breezy leads the sentence, Holiday says the line | a beat naming two people abstains | — | `tests/test_action_beat_attribution.py` |
| `ch26_0321` | *"Crow snapped,"* is a fragment, so not a beat — but still names a rival | every name in the paragraph must agree | — | `tests/test_action_beat_attribution.py` |
| `ch24_0098` | second half of a couplet, left stale when the first was repaired | a rename re-opens its disagreeing neighbours | — | `tests/test_stale_neighbour_detection.py` |
| `ch08_0315` | `drominadian` — a speaker in no cast entry, `voice_id: None` | a repair may not assign an unregistered speaker | 2 lines shipped without a voice | `tests/test_unknown_speaker_guard.py` |
| — | `Gut-bus-ters` spoken as three words | no hyphen or added space in a respelling | 108 stored values corrected on read | `tests/test_pronunciation_no_spoken_breaks.py` |
| — | three trend kinds all described as "monotone" | a warning states what was measured | 47 items mislabelled | `tests/test_trend_review_wording.py` |
| — | an alias prune queued a re-script of a published book | no auto-re-script once a delivery is published | 8 generated, 5 mastered, Part 01 live | `tests/test_script_refresh_guard.py` |
| — | 291 review items, 0 blocking, 173 that mattered | separate "changes the audio" from advisory | 291→183 and 163→103 | `tests/test_review_inbox_actionability.py` |

## Rules measured and rejected

Kept because each was plausible enough to propose, and three were re-proposed
after rejection.

| rule | why it was tempting | measured | verdict |
| --- | --- | --- | --- |
| co-occurrence veto on cast merges | appositive aliases "rarely co-occur" | aliases co-occur *more* than distinct characters | removed 2026-09-10 |
| block adjudication | one call per exchange, self-consistent | 612/616 identical, net −2 correct; 4th error found later | removed 2026-09-10 |
| anchored alternation | both ends known, two speakers, fill the middle | 32%, then 42% once beats supplied anchors; 47.5% even at 2 slots | rejected |
| clause boundary before the verb | symmetry with the post-verbal branch | destroyed 23 correct readings to catch 9 | rejected |
| continuation propagation | "as he continued" means the same speaker | 84 pairs, 1 finding, and it was wrong | rejected |
| re-ask every neighbour of a deterministic line | cheap, and staleness is real | 51 lines, 49 confirmed, 0 improved | narrowed to renames only |
| scene-scoped possessive check | would catch `ch11_0222` | findings 2→19, precision ~½→~⅐ | rejected |
| reaction beat (*"glared at her"*) | it is how `ch01_0293` was solved by hand | 14 covered, 57.1% | rejected |
| wide context for confidently-wrong lines | the cascade already exists | narrow 2/4, wide 1/4 — worse | rejected |
| escalate this class to Gemini | it is the top tier | 0 of 3, and it *cleared* their review flags | rejected |

## Cases known wrong and left wrong

| case | why it is left | evidence |
| --- | --- | --- |
| `ch01_0291`, `ch01_0292`, `ch01_0293` | no beat, no tag, no refutation; every tier is confidently wrong | see the 2026-09-10 record, *the prologue exchange* |
| `ch08_0142`, `ch08_0144` | corrected by hand after the audit flagged them; the fix is in the script but not the delivered audio | one split utterance, tag says *"she"*, stored as the male `armored_alien` |
| `ch28_0027` | audit false positive; the rule that would suppress it costs a genuine catch in `sample_book-14` | recorded as an `acceptable` review disposition |
| Emberdark descriptor cast (9 `absent_character_in_chapter`) | real errors, but the book is published; fixing needs a re-master | verified: `deep voice` appears 0 times in chs 21/23/49/50 |

## Related

- [2026-09-10 review of the September feature run](decisions/2026-09-10-review-of-the-september-feature-run.md)
- [2026-09-06 speech tags outrank adjudication](decisions/2026-09-06-speech-tags-outrank-adjudication.md)
- [2026-09-04 whole-cast duplicate detection](decisions/2026-09-04-whole-cast-duplicate-detection.md)
- [Targeted block adjudication plan](plans/targeted-block-adjudication-2026-09-06.md)
