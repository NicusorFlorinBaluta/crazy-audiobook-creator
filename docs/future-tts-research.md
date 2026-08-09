# Future Research — Alternative TTS Model Evaluation

> **Status: Deferred — not currently in scope.**
> Current production engine (Qwen3-TTS Base + VoiceDesign) is production-proven
> and stable. This document captures research for when the emotion-expressiveness
> ceiling becomes a practical blocker or when a compelling quality upgrade
> opportunity emerges.

---

## Context

Qwen3-TTS Base clone mode has no per-utterance emotion instruction parameter.
The pipeline compensates with post-processing (pitch/tone shifts up to ±0.4
semitones), but this is not equivalent to native expression control. When the
audience of an audiobook can reliably tell that every voice has the same
emotional flatness, these alternatives should be evaluated.

Trigger conditions to revisit:
- Post-processing emotion expansion (Phase 1 of the current improvement plan)
  is implemented and A/B tests still show insufficient dynamic range
- Qwen3-TTS CustomVoice is never open-sourced
- A new community benchmark puts one of the models below clearly ahead in
  audiobook-specific quality metrics

---

## Candidate 1 — Chatterbox TTS (Resemble AI)

### Model facts

| Property | Value |
|----------|-------|
| Developer | Resemble AI |
| License | **MIT** (fully commercial-usable) |
| Parameters | 0.5B (original), 350M (Turbo) |
| Voice cloning | ~5 seconds reference audio |
| Languages | English primary; Multilingual V3 supports 23+ |
| VRAM (estimated) | ~2–3 GB (0.5B), ~1.5 GB (Turbo) |
| Emotion control | **Native exaggeration parameter** (0.0 = calm, 1.0 = dramatic) |
| Inline tags (Turbo) | `[laugh]`, `[cough]`, `[sigh]`, `[excited]` |

### Emotion control — key differentiator

Chatterbox exposes a single float `exaggeration` parameter that acts as a
prosody intensity multiplier. This maps naturally onto the pipeline's existing
emotion taxonomy from the LLM:

```python
# Proposed mapping — emotion label → exaggeration value
_CHATTERBOX_EXAGGERATION = {
    "neutral":          0.3,
    "reflective":       0.35,
    "authoritative":    0.4,
    "tense":            0.5,
    "soft":             0.55,   # whisper/somber: slight exaggeration for clarity
    "playful":          0.65,
    "intense":          0.85,   # angry/panicked/shout
}
```

The Turbo variant also supports inline prosodic tags embedded in the synthesis
text (e.g., `"[laugh] I can't believe you said that!"`), which the pipeline
could inject for clearly marked humorous lines.

### AMD / Windows compatibility

- Uses standard PyTorch — works with the AMD ROCm venv already in use
- No custom CUDA kernels; `pip install chatterbox-tts` is sufficient
- Tested by community members on RDNA 3 with `HSA_OVERRIDE_GFX_VERSION=11.0.0`
- No WSL2 required

### Risk: voice consistency across engines

The primary concern is **timbre drift** when the same character is voiced by
two different TTS models in the same audiobook. A hybrid routing scheme
(Qwen3-TTS for neutral/reflective lines, Chatterbox for high-emotion lines)
would need to pass an A/B listening test confirming the transition is not
audible to a casual listener.

### Benchmark protocol

1. Install: `pip install chatterbox-tts` in the AMD venv
2. Clone the same 5 voice reference WAVs from an existing project
3. Generate 30 test lines (10 neutral, 10 medium-emotion, 10 high-emotion) with
   both Qwen3-TTS and Chatterbox using identical text
4. Run Whisper WER check and speaker-similarity measurement on all outputs
5. Subjective blind listen by comparing character continuity across a 3-minute
   mixed passage (Qwen3 for narration, Chatterbox for intense dialogue)

**Pass criteria for hybrid integration:**
- Speaker similarity >= 0.50 between Qwen3 and Chatterbox outputs for same character
- WER <= current average + 2% on Chatterbox outputs
- Subjective: < 30% of listeners can identify the engine switch

---

## Candidate 2 — Fish Speech S2 Pro (Fish Audio)

### Model facts

| Property | Value |
|----------|-------|
| Developer | Fish Audio |
| License | **Fish Audio Research License** — free for personal/research; commercial requires separate agreement |
| Architecture | Dual-AR (Slow 4B + Fast 400M parameters) |
| Voice cloning | 5–30 seconds reference audio |
| Languages | Chinese, English, Japanese, German, French, Korean, and more |
| VRAM (estimated) | ~8–10 GB (Slow AR 4B in fp16) |
| Emotion control | **Inline natural language tags**: `[whisper in small voice]`, `[excited]`, `[professional broadcast tone]` |
| Benchmark WER | 0.99% English, 0.54% Chinese (Seed-TTS Eval) — state of the art |

### Emotion control — inline tags

Fish Speech S2 Pro supports free-form inline emotion instructions embedded
directly in the synthesis text. This is the closest thing to "native acting
direction" in a locally-runnable open-source model:

```text
[whisper softly] I know what you did. [normal voice] And I am not pleased.
```

This would let the pipeline inject the LLM's rich emotion labels (currently
wasted in clone mode) directly into the TTS prompt. The 4B slow-AR model has
enough reasoning capacity to follow nuanced instructions.

### AMD / Windows compatibility — significant barrier

- **Primary support: NVIDIA CUDA + Linux**
- No official ROCm or Windows support from Fish Audio
- Community workarounds exist (manual patching, WSL2 + Docker) but are fragile
- The 4B slow-AR model requires ~8–10 GB VRAM; on the 7900 XTX this is fine,
  but only if ROCm support can be made to work reliably
- **Verdict: not viable for Windows without WSL2 infrastructure work**

### When to revisit

- Fish Audio adds official Windows/ROCm support (check GitHub quarterly)
- A community-maintained ROCm fork achieves stable multi-chapter generation
- The project moves any part of the pipeline to a Linux machine

### Licensing note

The commercial use restriction means this model cannot be used in a product
distributed to others without a Fish Audio agreement. For personal audiobook
production, the research license is sufficient.

---

## Candidate 3 — CosyVoice 2 (Alibaba/FunAudioLLM)

| Property | Value |
|----------|-------|
| Developer | Alibaba FunAudioLLM team |
| License | Apache 2.0 |
| Voice cloning | Yes (3s zero-shot or fine-tune) |
| Emotion control | Instruction-following, pronunciation inpainting |
| AMD / Windows | Untested; PyTorch-based, likely compatible with ROCm |
| VRAM | Varies by model size; ~4–8 GB |

CosyVoice 2's "pronunciation inpainting" feature is interesting for the
audiobook use case — it allows correcting specific word pronunciations (fantasy
names, proper nouns) without rewriting the entire synthesis. This is orthogonal
to the current pronunciation substitution system.

**Evaluation priority:** Lower than Chatterbox (less proven in English
long-form); higher than Fish Speech (no Windows blocker).

---

## Candidate 4 — Qwen3-TTS CustomVoice (Alibaba Cloud)

The ideal upgrade path for this project. Same model family as the current
engine, adds per-utterance instruction following, preserves voice identity
across VoiceDesign-bootstrapped references.

**Current status:** API-only (Alibaba Cloud). No open-source release announced.

**Action:** Monitor the Qwen3-TTS GitHub repository and HuggingFace quarterly.
If open-sourced, this is a direct drop-in replacement with zero integration cost.

---

## Recommended Research Order

```
Priority 1: Chatterbox TTS evaluation
  -> Lowest barrier (pip install, ROCm works, MIT)
  -> Fastest path to native emotion control
  -> Benchmark protocol above

Priority 2: CosyVoice 2 evaluation
  -> Apache 2.0, PyTorch-based
  -> Check ROCm compatibility first

Priority 3: Monitor Qwen3-TTS CustomVoice open-source release
  -> Zero-integration-cost upgrade if it happens
  -> Subscribe to Qwen GitHub releases

Deprioritize: Fish Speech S2 Pro
  -> Windows/AMD barrier is high
  -> Only revisit if WSL2 infrastructure is added or official ROCm support lands
```

---

## Hybrid Architecture Proposal (for when evaluation passes)

```
Script Line: emotion="panicked shout", speed=1.2
                       |
           +-----------v-----------+
           |    Emotion Router     |
           |   (mood_tier lookup)  |
           +-------+-------+-------+
                   |       |
       neutral/    |       |    intense/
       soft/       |       |    playful/
       reflective  |       |    tense
                   |       |
          +--------v--+ +--v------------------+
          | Qwen3-TTS | | Chatterbox TTS      |
          | Base      | | exaggeration=0.8    |
          | (cloning) | | (cloning)           |
          +--------+--+ +----------+----------+
                   |               |
           +-------v---------------v--------+
           |   Same validation pipeline     |
           |  (WER, speaker sim, clipping)  |
           +--------------------------------+
```

Both engines share the same voice library WAV references. The speaker
similarity validation will catch cases where a character sounds too different
across engine types.
