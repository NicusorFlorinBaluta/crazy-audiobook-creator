# Voice Design and Speaking Cast

The voice path has two separate jobs:

1. **Qwen3-TTS VoiceDesign** creates a reusable reference from a compiled
   natural-language instruction and speaks a known test sentence.
2. **Qwen3-TTS Base** clones that saved reference for each script line.

Keeping these roles separate matters: clone mode accepts reference audio and
its transcript, but does not accept a free-form acting instruction for every
utterance.

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

## Voice cap and sharing

`script.max_unique_voices` limits generated reference voices, not character
identities. Important speakers receive their own reference. Lower-dialogue
characters can point to a stable compatible voice through `voice_id`.

Scripts retain the real speaker ID, so attribution and later reassignment
remain possible. Generation resolves the assigned `voice_id` without merging
or deleting script lines.

## Bootstrap and one-time review

For each unique voice actually assigned to a speaker:

1. Build `voice_cast.json` from completed scripts.
2. Compile and lint the design direction.
3. Start the loopback-only VoiceDesign helper.
4. Generate and atomically save a known reference sentence.
5. Stop VoiceDesign to release GPU memory.
6. Transcribe the WAV with Whisper and compare it with that sentence.
7. Register only references within the bootstrap WER limit.
8. Pause a new project for one manual preview/approval step.
9. Load Qwen Base when chapter generation starts.

Each effective profile has a fingerprint containing its metadata, compiled
instruction, design model, and design configuration. Unchanged references are
reused. Existing projects created before the approval feature are
grandfathered. Newly created projects wait once; later partial chapter batches
do not prompt again.

## Generated and uploaded references

Each project has `voice_library/<project-id>/voices.json` plus its reference
WAVs. Registry entries include the path, exact spoken transcript (`ref_text`),
description, duration, sample rate, identity metadata, source type
(`generated` or `uploaded`), and design/content fingerprint.

The casting dashboard is organized by reusable voice profile. It shows only
real speakers and explicitly reports how many non-speaking analysis entries
were excluded. A speaker can be assigned to another cast voice. A profile can
be redesigned from text or replaced with an uploaded WAV, FLAC, MP3, M4A, AAC,
or OGG sample.

Uploads are converted to mono 24 kHz PCM WAV. They must contain one clean,
non-silent, non-clipped speaker and be 3–30 seconds long. The user supplies the
exact transcript so Qwen Base can use higher-quality full ICL cloning instead
of x-vector-only mode.

## Dependency invalidation

Reference content hashes are part of line-generation fingerprints. Reassigning
a speaker or replacing a profile marks only chapters that use it as stale.
Those chapters regenerate the next time they are selected; unrelated completed
chapters remain valid.

Playback is safe at any stage. Reassignment, redesign, and upload require the
pipeline to be stopped or parked at a safe boundary so a chapter cannot contain
a mid-generation voice change.

## Emotion and speed

The script supplies readable emotion and speed values. Their implemented
effects are deliberately restrained:

- Speed is deterministic audio post-processing.
- Character FX combine with line speed.
- A small set of mood words maps to subtle pitch/tone adjustments.
- Peak protection prevents overflow.

This preserves cloned timbre better than large transforms, but it is not
equivalent to native natural-language acting direction in clone mode.

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

## Operational notes

- Voice bootstrap is expensive, so it runs after book-wide scripting and is
  reused while its fingerprints remain current.
- Qwen VoiceDesign and Qwen Base are not kept in GPU memory together.
- The VoiceDesign helper binds only to `127.0.0.1:8101` and exists only during
  bootstrap.
- The narrator is a normal registered reference and also speaks chapter-title
  announcements.
