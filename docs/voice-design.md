# Voice Design and Speaking Cast

The voice path has two separate jobs:

1. **Qwen3-TTS VoiceDesign** creates a reusable reference from a compiled
   natural-language instruction and speaks a known test sentence.
2. **Qwen3-TTS Base** clones that saved reference for each script line.

Keeping these roles separate matters: clone mode accepts reference audio and
its transcript, but does not accept a free-form acting instruction for every
utterance.

## Per-character test sentences

During character analysis the LLM is asked to invent a **unique, natural
15–25 word sentence** for each character — a narrator-flavoured statement for
the narrator and a line of in-character dialogue for everyone else. The
sentence is stored on the `Character` model as `test_sentence`.

At bootstrap time the pipeline prefers enough exact dialogue from the completed
script to form a useful reference. It uses or augments the analyzed
`test_sentence` when dialogue is short, and `VoiceDesigner._build_test_sentence`
adds a gender-keyed global fallback for a short minor-character reference. The
final exact text and selection-policy version are included in the design
fingerprint. The sentence has two roles:

- It is spoken by the VoiceDesign model to produce the reference clip, giving
  the model a meaningful, character-specific utterance rather than an identical
  line shared across the whole cast.
- It becomes the `ref_text` stored in `voices.json` and passed back to the
  Base model for Full-ICL cloning during chapter generation.

**Why this matters**: identical reference sentences across characters produce
acoustically similar speaker embeddings and make the cast distinctness check
almost meaningless. Character-specific sentences give each voice a different
phoneme distribution and intonation pattern, improving acoustic separation
and the usefulness of the cast-pair diagnostic.

## Analysis registry versus speaking cast

The book-wide analysis registry deliberately retains potentially relevant
people, groups, creatures, and named entities. Casting is narrower: only
registry IDs that own at least one line in the completed book-wide script
receive a voice assignment or profile.

A speaking entity belongs in the cast whether it is human, animal, divine,
supernatural, artificial, or personified. A named entity with no spoken lines
does not receive a voice.

The final design instruction is compiled from the analyzed description plus
explicit gender and age metadata. Contradictory register terms are repaired
(for example, a female character described as a baritone becomes a contralto),
biographical descriptions receive audible pitch/resonance/pacing guidance, and
near-duplicate profiles receive deterministic contrasting directions.

### Voice Similarity & Acoustic Embeddings
- **Boilerplate Filtering**: Text token similarity comparison (`_token_similarity`) strips out common prompt template boilerplate words (`"clearly adult speaker"`, `"maintain vocal identity..."`) to prevent false similarity warnings between distinct character prompts.
- **Acoustic Speaker Embeddings**: cast diagnostics primarily use the Qwen speaker encoder. `compute_audio_similarity` provides a model-independent 514-value vector formed from the mean and standard deviation of a normalized 257-bin log spectrogram. Cosine distance compares actual audio rather than prompt wording.

## Voice cap and sharing

`script.max_unique_voices` limits generated reference voices, not character
identities. Important speakers receive their own reference. Lower-dialogue
characters can point to a stable compatible voice through `voice_id`.

Scripts retain the real speaker ID, so attribution and later reassignment
remain possible. Generation resolves the assigned `voice_id` without merging
or deleting script lines.

## Bootstrap and one-time review

For each unique voice actually assigned to a speaker (plus the two explicit
male/female narrator candidates shown at first-project review):

1. Build `voice_cast.json` from completed scripts.
2. Compile and lint the design direction.
3. Start the loopback-only VoiceDesign helper.
4. Generate and atomically save a known reference sentence.
5. Stop VoiceDesign to release GPU memory.
6. Transcribe the WAV with Whisper and compare it with that sentence.
7. Validate every exposed candidate and register only references within the bootstrap WER limit. A failed optional alternative is discarded without invalidating a good canonical candidate.
8. Pause a new project for one manual preview/approval step.

At voice review, the selected A/B option is stated explicitly. Applying an
option shows an in-progress state and a success or failure notification.
Regeneration replaces only the currently selected option; the button names
that option before the destructive replacement. Character reference text is
drawn from speaker-pure script lines, so narrator tags and action beats must
not be embedded in a character's preview.
9. Load Qwen Base when chapter generation starts.

Each effective profile has a fingerprint containing its metadata, compiled
instruction, exact final reference text, selection-policy version, design model,
and design configuration. Unchanged references are
reused. Existing projects created before the approval feature are
grandfathered. Newly created projects wait once; later partial chapter batches
do not prompt again.

The narrator is a deliberate exception to the no-unused-profiles rule. Because
the narrator speaks extensively and the choice materially changes the whole
book, bootstrap prepares one male and one female narrator reference. The review
banner plays both and stores the selected profile in `voice_cast.json`.
Only that selection is used by line generation; changing it later invalidates
only chapters that contain narration.

## Generated and uploaded references

Each project has `voice_library/<project-id>/voices.json` plus its reference
WAVs. Registry entries include the path, exact spoken transcript (`ref_text`),
description, duration, sample rate, identity metadata, source type
(`generated` or `uploaded`), and design/content fingerprint.

The `"file"` field in each registry entry is an **absolute path** to the actual
WAV, whose filename includes a UUID suffix (e.g. `narrator_male_7f8dfaa9.wav`).
`VoiceLibraryManager.get_voice_path` consults the registry first; it only falls
back to the legacy `<character_id>.wav` convention when no entry exists. Do not
assume a voice file is named `<character_id>.wav`. When a registry entry is
replaced, its unreferenced WAV and `.pt` speaker-embedding cache are removed.

The casting dashboard is organized by reusable voice profile. It shows only
real speakers and explicitly reports how many non-speaking analysis entries
were excluded. A speaker can be assigned to another cast voice. A profile can
be redesigned from text or replaced with an uploaded WAV, FLAC, MP3, M4A, AAC,
or OGG sample.

Every ready profile, including the selected narrator, also has a **Download
voice sample** action. It downloads the canonical reference WAV with a
descriptive `<book> - <character> - voice-reference.wav` filename. That file
can be imported into a matching character profile in a later book with the
normal upload action and the exact words spoken in the reference. The filename
is descriptive only; cloning still depends on the accompanying exact
transcript.

Uploads are converted to mono 24 kHz PCM WAV. They must contain one clean,
non-silent, non-clipped speaker and be 3–30 seconds long. The user supplies the
exact transcript so Qwen Base can use higher-quality full ICL cloning instead
of x-vector-only mode. Whisper verifies that transcript before the existing
reference is replaced. A material mismatch fails closed and reports what ASR
heard; harmless spacing/orthographic equivalence uses the same normalization as
chapter validation. This one-time check can take longer on a cold model start,
and managed model services are released when it finishes.

## Dependency invalidation

Reference content hashes are part of line-generation fingerprints. Reassigning
a speaker or replacing a profile marks only chapters that use it as stale.
Those chapters regenerate the next time they are selected; unrelated completed
chapters remain valid.

Playback is safe at any stage. Reassignment, redesign, and upload require the
pipeline to be stopped or parked at a safe boundary so a chapter cannot contain
a mid-generation voice change.

## Emotion and speed

The script supplies readable emotion and speed values, but production output
currently treats them as descriptive metadata. Automatic pitch/tempo/tone
post-processing is disabled by default after the 2026-08-09 full-book output
showed widespread echo-like smearing from the phase-vocoder fallback.

- Qwen Base output is preserved without time-stretch or pitch-shift effects.
- Numeric peak protection remains active.
- Explicit post-processing is experimental and must be enabled in
  `voice/config.yaml`.
- The librosa phase-vocoder fallback requires a second explicit unsafe opt-in
  and must not be used for production books.

Changing the clean-audio policy invalidates synthesis fingerprints. Audio made
under the old policy is therefore not silently reused by a clean-output run.
See [the echo incident report](audio-echo-incident-2026-08-10.md).

## Good design directions

Describe audible qualities:

```text
A low, clear adult voice with dry texture, restrained warmth, precise
consonants, and measured pacing. Calm authority without theatrical booming.
```

Useful dimensions include pitch range, vocal weight, texture, resonance, pace,
articulation, energy, and an accent only when the model can render it reliably.
Avoid named actors, appearance or biography without an audible consequence,
contradictory traits, extreme effects that harm intelligibility, and
instructions about what words to speak.

Accent is optional and should be light, story-appropriate, and consistent. It
is a secondary contrast dimension after register, vocal weight, resonance,
texture, articulation, and cadence. Heavy or arbitrary accent instructions can
reduce pronunciation accuracy, drift between samples, and become caricatured.

The official Qwen VoiceDesign family currently tops out at the 1.7B checkpoint
used here. Larger expressive TTS systems are not drop-in replacements for the
current VoiceDesign-reference plus Base-cloning workflow. They require a
separate benchmark for identity consistency, long-form stability, validation,
VRAM use, and resumability before becoming a production backend.

## Operational notes

- Voice bootstrap is expensive, so it runs after book-wide scripting and is
  reused while its fingerprints remain current.
- Qwen VoiceDesign and Qwen Base are not kept in GPU memory together.
- The VoiceDesign helper binds only to `127.0.0.1:8101` and exists only during
  bootstrap.
- The selected narrator candidate is a normal registered reference and also
  speaks chapter-title announcements.
- The narrator candidate selected at the voice review gate (e.g. `narrator_male`)
  is recorded only in `voice_cast.json` under `assigned_characters`. It is **not**
  written back to `characters.json`. During line generation, `_prepare_generation_lines`
  therefore reads `voice_cast.json` as the authoritative speaker → voice mapping before
  falling back to `characters.json`. Any code that resolves a voice for generation must
  follow this same precedence.
