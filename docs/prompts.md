# Prompt and Source-Fidelity Rules

The LLM performs metadata analysis. It is not allowed to rewrite the audiobook’s source text.

## Pass 1: book-wide character analysis

The analyzer scans the complete extracted book. Books over the short-input threshold are divided into bounded text units, including multiple units for a single very large chapter. Results are merged by normalized character ID/display name.

The character prompt asks for:

- only entities that actually speak
- narrator plus every speaking entity regardless of type
- stable IDs and aliases
- gender, age range, traits, speaking style, and voice description
- approximate dialogue count for voice prioritization
- overall tone

Malformed analysis output is a pipeline error. A failed unit is not silently skipped because that would create incomplete identities for later chapters.

The runtime system prompt is in `brain/director/character_analyzer.py`; the readable template in `brain/director/prompts/character_extraction.md` mirrors its rules.

## Pass 2: immutable-fragment annotation

The application—not the LLM—splits chapter source into fragments. Each prompt includes an array like:

```json
[
  {"id": 0, "text": "The wind moved through the trees.", "dialogue": false},
  {"id": 1, "text": "“Who is there?”", "dialogue": true}
]
```

For every input ID, the LLM may return only:

```json
{
  "id": 1,
  "speaker": "character_id",
  "emotion": "quiet suspicion",
  "speed": 0.92,
  "pause_before_ms": 0,
  "pause_after_ms": 350
}
```

The source `text`, fragment ID, and source span are restored from the immutable input. LLM-returned prose is never trusted as audiobook text.

### Smart Hybrid Dialogue Attribution (Option D)
- **Dialogue Fragment Rule**: Quoted text (`"..."`) is treated as spoken
  dialogue unless the source establishes that it is a document, sign, or other
  unspoken quotation. Explicit named tags and unique generic roles such as
  `the boy` are repaired locally from source evidence. Ambiguous contradictions
  receive one bounded fragment-only correction request; they do not regenerate
  an otherwise structurally valid metadata chunk.
- **Dialogue Tag Grouping**: Spoken dialogue and an immediately attached short
  narrator tag remain separate synthesis lines and retain their correct voices.
  Both lines receive one `utterance_group_id`, so mastering joins them with no
  added pause and no crossfade. Same-speaker batching still handles ordinary
  adjacent narration and dialogue, avoiding unnecessary TTS fragmentation while
  preserving **100.00% exact source coverage**.
- **Minor Character Mapping**: Unnamed background speakers (e.g., `"a little girl"`, `"the boy"`) receive dedicated minor voice profiles (`child_female`, `child_male`, `minor_female`, `minor_male`) rather than collapsing into the Narrator.
- **Gender Normalization**: Register words like `"woman"`, `"female (elderly)"`, `"loremother"` are normalized to `Gender.FEMALE`, and `"boy"`, `"man"` map to `Gender.MALE`.

## Strict response contract

A metadata response is accepted only when:

- it contains exactly one item for every input fragment ID
- no IDs are missing, duplicated, negative, or out of range
- non-dialogue fragments use `narrator`
- dialogue speakers exist in the book-wide registry
- an attached `he`/`she` tag does not contradict a known binary-gender speaker,
  and an explicitly named tag does not name a different registered character
- numeric values can be parsed and clamped to model bounds

Malformed JSON, incomplete/duplicate IDs, and other structural corruption use
bounded full-chunk retries. Semantic attribution failures use deterministic
repair or a bounded fragment-only retry. If that focused retry remains
ambiguous, only the affected fragment receives the conservative fallback and
the decision is recorded in scripting metrics.

## Dialogue recognition

Static detection supports:

- straight double quotes: `"Hello."`
- curly double quotes: `“Hello.”`
- guarded straight/typographic single-quote dialogue
- em-dash dialogue at the start of a fragment

Attribution remains the LLM’s job, constrained to registered IDs. Narration containing a character’s name is not dialogue. Speech tags around quoted text do not make the tags themselves character dialogue when segmentation separates them.

## Long chapters

Fragments are accumulated into non-overlapping batches up to `script.chunk_size_words`. No overlap is used because overlap would synthesize repeated prose.

Each batch receives the character registry and short continuity context. Batch output is concatenated in original fragment order, and line IDs are assigned deterministically across the full chapter.

## Coverage proof

After parsing, the application verifies both:

1. Concatenated script text equals normalized source text.
2. Every recorded source span points to the exact normalized line content in its chapter.

A mismatch raises an error. This catches omissions, duplications, reorderings, and source rewrites before any expensive audio work.

## Cache invalidation

A script fingerprint covers:

- exact chapter source
- relevant character registry, including voice assignments
- Ollama model
- system prompt
- script schema version
- chunk size

A matching script file without matching metadata is not reused.

## Prompt-writing guidance

When modifying prompts:

- Preserve the exact-ID output contract.
- Ask for short, audible emotion descriptions rather than prose analysis.
- Avoid claiming the clone model receives arbitrary emotion instructions; current audio realizes them through restrained post-processing.
- Keep speaker rules concrete and type-agnostic.
- Do not add a rule that discards rare speakers; voice sharing belongs in deterministic application logic.
- Update the schema version or fingerprint input when a change can alter output semantics.

## Useful adversarial tests

- one chapter larger than the analysis threshold
- dialogue in curly, straight, single, and em-dash forms
- a supernatural or personified speaking entity
- narration that names a character but contains no speech
- an unknown speaker returned by the LLM
- duplicate/missing fragment IDs
- a chapter split across several batches
- source with unusual whitespace, poetry, and scene breaks
