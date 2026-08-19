# Configuration

The running configuration lives in `brain/config.yaml` and `voice/config.yaml`. Paths are resolved from the repository root unless stated otherwise.

## `brain/config.yaml`

### `ollama`

| Key | Meaning |
|---|---|
| `host` | Local Ollama base URL |
| `model` | Model used for both character and script metadata passes |
| `fallback_models` | Ordered allowlist of exact model tags permitted after a 404; empty means fail closed |
| `auto_start` | Start a pipeline-owned Ollama server when the configured endpoint is unavailable |
| `executable` | Ollama executable used by the managed server |
| `models_dir` | Existing Ollama model store passed as `OLLAMA_MODELS` |
| `vulkan_visible_devices` | Vulkan device IDs exposed to managed Ollama, such as `0` for the discrete GPU |
| `startup_timeout_seconds` | Maximum wait for the managed server and configured model |
| `context_window` | Per-request context tokens; reduce cautiously when VRAM is tight |
| `temperature_pass1` | Character-analysis temperature |
| `temperature_pass2` | Script-annotation temperature |
| `top_p` | Sampling nucleus |
| `timeout` | Request timeout in seconds |
| `max_retries` | Ollama transport/JSON retry budget |
| `max_retry_seconds` | Total time budget for failed attempts and retry backoff |
| `max_output_tokens` | Server and client-side output ceiling for one generation |
| `max_generation_seconds` | Total wall-clock ceiling for one continuously active generation |
| `repetition_window_chars`, `repetition_count` | Exact periodic-tail loop detector (maximum searched period and required repetitions); set the window to `0` only to disable it |
| `unload_after_scripting` | Release the Ollama model before loading Qwen TTS on the same GPU |

The application fingerprints the configured model and prompt. Changing them causes dependent script artifacts to be rebuilt. The default workstation configuration uses an isolated loopback port so a separately running Ollama desktop service cannot change its GPU placement. It never selects an arbitrary installed model: only entries in `fallback_models` may replace the primary model, and the checked-in configuration intentionally leaves that list empty. JSON calls also enable Ollama's structured JSON mode. Output count, total generation time, and repeated-tail detection independently terminate a response that fails to stop normally; diagnostics record only counts and the termination reason, not book text.

### `voice_server`

| Key | Meaning |
|---|---|
| `host` | Voice API URL; default `http://127.0.0.1:8100` |
| `api_token` | Value sent as `X-API-Token`; blank is acceptable only for loopback |
| `venv` | Windows virtual-environment directory used for auto-start |
| `auto_start` | Start the voice API as a managed subprocess when unavailable |
| `startup_timeout_seconds` | Maximum wait for voice health |
| `timeout`, `retries`, `retry_delay` | HTTP behavior |
| `rocm_target_family` | Explicit ROCm package target; `custom` avoids the broken `offload-arch` shim in the configured environment |

### `extraction`

Controls front-matter/TOC/appendix filtering, reference material ingestion, and chapter detection:

| Key | Meaning |
|---|---|
| `skip_toc` | Filter out Table of Contents documents and sections |
| `skip_appendices` | Filter appendices/back matter and capture recognized character references as supplemental analysis input |
| `skip_front_matter` | Filter out copyright, dedications, and title pages |
| `skip_preface` | When `true` (default), filters out out-of-universe prefaces, forewords, author's notes, and afterwords from narration |
| `min_chapter_words` | Minimum word count for a standalone chapter (shorter fragments merge into adjacent chapters) |
| `max_chapter_words` | Target maximum chapter length before safe subdivision |
| `chapter_detection` | Boundary detection strategy (`auto`, `heading`, `pattern`, `none`) |

Short narrative headings such as prologues and interludes are retained even
below `min_chapter_words`; other short fragments merge into an adjacent chapter
instead of being discarded. Long chapters split at paragraph or sentence
boundaries. Repeated running headers are removed only after exact cross-document
detection—uppercase story text is not treated as a header.

**Reference Material (Glossary & Dramatis Personae)**:
When `skip_appendices` is enabled, sections titled *Glossary*, *Dramatis Personae*, *Character List*, or *Cast of Characters* are extracted into `ExtractedBook.reference_material`. They are excluded from narration and supplied as bounded, supplemental input to the Stage ② Character Analyzer; direct narrative evidence takes precedence if they conflict.

Reference material is applied only after narrative speaker discovery. The model
may propose a richer voice description, traits, aliases, or a missing speaking
style for an existing speaker, but every automatic patch needs confidence of at
least 0.90 and a verbatim excerpt from the bounded reference corpus. Unknown
character IDs, unsupported evidence, incomplete descriptions, and gender
conflicts are rejected and audited. If this optional pass is unavailable,
narrative character analysis continues unchanged.

### `script`

- `joint_analysis`: when enabled, character discovery and source-fragment
  attribution share one book-text pass. Character IDs remain provisional until
  a compact evidence-based reconciliation step completes; the attribution audit
  and external fallback still run afterward.
- Segment bounds: `max_segment_sentences`, `min_segment_words`
- Default delivery: `default_speed`
- Pause requests: narrator, dialogue, scene, chapter, and paragraph values in milliseconds
- Voice assignment: `max_unique_voices`, `minor_character_threshold`, `group_minor_characters`
- LLM batching: `chunk_size_words`
- TTS-call grouping: `group_utterances`, `utterance_target_chars`, and `utterance_max_words`

`chunk_overlap_words` is retained for configuration compatibility but current source-fragment batching is non-overlapping by design.
Grouping merges only adjacent fragments with the same speaker, voice, and FX. It never crosses a blank paragraph and preserves all source fragment IDs and the exact combined source span.

### Incremental delivery

Incremental delivery is project state, configured from project creation or the
dashboard. `enabled` turns part publication on and `batch_size` accepts 1–20
chapters. Manual chapter selection and incremental delivery can be used
together: when a subset is selected, parts and the combined export contain
only those chapters in canonical book order. With no manual selection, the
delivery plan covers the full book.
After the first publication, the enabled state and batch size are locked until
delivery artifacts are reset; the selected chapter boundaries are locked by the
same rule. A graceful pause request is honored only between parts. Stale or
hash-invalid parts are never offered as downloads.

### `dashboard`

| Key | Meaning |
|---|---|
| `host`, `port` | Dashboard bind address |
| `cors_origins` | Exact browser origins allowed |
| `api_token` | Optional token for peers outside trusted LANs |
| `trusted_lan_cidrs` | TCP-peer CIDRs allowed without an application token |
| `max_upload_size_mb` | Compressed upload limit |
| `max_epub_expanded_mb` | Total expanded EPUB limit |

Loopback and configured trusted-LAN peers may use the dashboard without an
application token. Authorization uses the socket peer, not `X-Forwarded-For`.
Public remote access should still be placed behind authenticated Home Assistant
or a reverse proxy and protected by firewall rules.

### `metadata`

`auto_fetch_external: false` is the privacy-preserving default. Enabling it
sends book title/author queries to Google Books after project creation. The
automatic path fills only missing description, ISBN, genre, and year fields and
never replaces an embedded cover. The dashboard's **Find book details** action
shows a candidate and confidence before applying it; the EPUB title and author
remain authoritative only for automatic enrichment. Explicit human approval
adopts the reviewed identity and retains the EPUB values as provenance.
Replacing an existing cover requires an explicit checkbox. If automatic
matching finds no sufficiently close candidate, the
same dialog opens a manual title/author search. It returns up to ten editions
with author, year, and ISBN so the user can select the exact Google Books
volume for review. Explicit application uses the reviewed title and author,
while retaining the EPUB values internally as source provenance. Existing M4B
exports are atomically remuxed with the new tags and selected cover without
re-encoding audio. Provider failures and no-match results are reported
distinctly.

`cache_hours: 24` keeps a query-specific candidate in the project directory so
repeat reviews and duplicate clicks do not make another provider request. Use
the API's `refresh` option to bypass a still-valid candidate. Cover downloads
are restricted to Google image hosts, HTTPS, supported image signatures,
bounded dimensions, and 8 MB.

Set `GOOGLE_BOOKS_API_KEY` in `.env` to identify requests and receive the
Google Cloud project's dedicated Books API quota. The key remains server-side,
is never returned to the dashboard, and should be restricted to the Books API.
When Google returns `429`, the lookup does not amplify it with immediate
retries and reports any numeric `Retry-After` value to the user.

### `external_validation` and Gemini Services

Character gender & voice augmentation (Pass 1 and Joint modes), ambiguous extraction,
speaker attribution, and subjective audio warnings use Gemini:

1. local deterministic/director result & whole-book evidence dossier;
2. batched Gemini 3.5 Flash Lite API triage (`gemini-3.5-flash-lite`);
3. stronger Gemini 3.5 Flash API adjudication & character augmentation (`gemini-3.5-flash`);
4. Gemini web Pro in a persistent project conversation;
5. dashboard review when no result reaches `auto_accept_confidence`.

Extraction, attribution, and audio QA have separate saved web conversations for every
project. Their URLs are stored under `external_validation/browser_state.json`.
The browser profile is shared only for authentication; book conversations are
never shared between projects. A purpose chat rolls over only after
`max_turns_per_conversation`, avoiding an unbounded context while still not
creating a new chat per case. Audio is uploaded to the audio-QA conversation,
while attribution sends bounded text cases in batches.

Set `GEMINI_API_KEY` in `.env`. `daily_request_budgets` are local safety caps,
not a source of truth for Google quota; the application counts retries too and
resets its counters at midnight Pacific. Free-tier prompts may be used by
Google to improve its products, so use a paid API project if that data handling
is unsuitable.

Gemini may clear only subjective warnings. It cannot override deterministic
hard gates such as clipping, missing speech, invalid silence, transcript
failure, or speaker-embedding failure. Every external decision stores provider,
model, decision, reason, and confidence. Anything below the automatic threshold
stops before mastering and appears in **Audio requiring your decision**.
High-confidence external rejection automatically invalidates and regenerates
only that WAV up to `max_audio_regenerations`; only an exhausted retry or an
inconclusive decision enters the manual queue.

The web fallback is disabled until its dedicated Chrome profile is initialized:

```powershell
$env:PYTHONPATH = "."
& "E:\PyTorch env\my_venv\Scripts\python.exe" tools/setup_gemini_browser.py
```

Sign in and select the premium Pro model once, then set
`external_validation.browser.enabled: true`. Routine runs are headless and
reuse the saved attribution/audio conversations. If Google requires a fresh
login, changes its page selectors, or presents an anti-automation challenge,
the fallback fails closed into dashboard review; it does not attempt to bypass
the challenge.

### `pipeline`

State is stored in `pipeline_state.db`. GPU work is chapter-batched and serialized independently of `batch_mode`. Valid line, generated-chapter, and mastered-chapter artifacts are reused through fingerprints and manifests.

### `schedule`

The dashboard may add a `schedule` section:

```yaml
schedule:
  enabled: true
  timezone: Europe/Bucharest
  windows:
    - start: "22:00"
      end: "06:00"
      days: [Monday, Tuesday, Wednesday, Thursday, Friday]
```

Scheduling is global: every queued audiobook project uses these windows. Each
window must select at least one weekday and its start/end times must differ.
Windows include the start minute and exclude the end minute, so adjacent
windows do not both claim the same boundary.
Cross-midnight windows belong to the weekday on which they start. The worker
parks only at safe stage/chapter boundaries, releases a managed Voice process
during long waits, and resumes when a configured window opens. A dashboard
restart preserves scheduled jobs as resumable instead of claiming their old
worker is still running. Disabling scheduling opens work immediately, including
jobs that were left in `paused_scheduled` by a restart.

## `voice/config.yaml`

### `tts`

| Key | Meaning |
|---|---|
| `model` | Qwen3-TTS Base model used for cloning |
| `device`, `dtype` | PyTorch placement |
| `sample_rate` | Native Qwen output rate |
| `max_text_length` | Maximum characters per Qwen inference chunk |
| `attn_implementation` | Transformer attention backend; `sdpa` falls back to `eager` when unsupported |
| `generation.*` | Qwen sampling parameters |
| `post_processing.enabled` | Opt in to pitch/tempo/tone processing. Production default is `false` to preserve clean model output. |
| `post_processing.allow_phase_vocoder_fallback` | Experimental legacy fallback. Keep `false`; the 2026-08-09 E2E associated it with audible echo/smearing. |

Oversized script lines are split internally at sentence or whitespace boundaries, synthesized with the same voice prompt, and rejoined.

Script emotion and speed remain descriptive metadata when post-processing is
disabled. Enabling post-processing does not silently authorize the librosa
phase vocoder: pitch/tempo changes are skipped when no quality-approved backend
is available unless the unsafe experimental fallback is separately enabled.
The complete policy and its revision are part of the synthesis fingerprint.

The reference-voice test sentences in this file are informational; the canonical gender-specific sentences are defined in `shared/constants.py`.

### `validation`

| Key | Meaning |
|---|---|
| `enabled` | Run validation during chapter generation |
| `whisper_model`, `whisper_device` | Transcription model and device settings |
| `whisper_backend`, `whisper_vad_filter` | Backend selection and optional VAD preprocessing. AMD/ROCm defaults to raw OpenAI Whisper because VAD can remove valid short or high-pitched speech. |
| `whisper_backend` | `openai_whisper` for AMD/ROCm PyTorch, `faster_whisper` for a compatible CTranslate2 runtime, or `auto` |
| `wer_threshold` | Maximum normalized word error rate |
| `emotion_wer_allowance` | Optional WER relaxation for post-FX lines; defaults to `0.0` so transcript accuracy remains strict without benchmark evidence |
| `max_retries` | Additional attempts for fail/flag outcomes |
| `keep_tts_and_whisper_resident` | Keep both models inside one chapter's retry loop; Whisper is still released at the chapter boundary |
| `risk_aware_first_attempt` | Listening-approved clarity policy for very short emphatic lines; longer dialogue and ordinary narration remain unchanged |
| `speaker_similarity_threshold` | Minimum Qwen speaker-encoder cosine similarity |
| `clipping_threshold` | Maximum sample peak in dBFS |
| `max_silence_seconds` | Longest permitted internal silence |
| `prosody.*` | Nonblocking monotone-warning enablement and explicit duration/pitch/dynamic-range thresholds; changes invalidate validation cache |
| `duration_tolerance` | Expected-duration tolerance |

Missing audio and missing line IDs always fail regardless of thresholds.

### Pronunciation dictionaries

`brain/pronunciation_dict.json` supplies optional application-wide mappings;
`brain/projects/<project_id>/pronunciation_dict.json` supplies book-local
overrides. Both files are JSON objects mapping authored spelling to a
deterministic synthesis form, for example:

```json
{
  "Patji": "Pah-chee",
  "Eelakin": "Ee-lah-kin"
}
```

Matching is case-insensitive, phrase-aware, longest-first, and non-recursive.
The original script text remains unchanged and remains the validation target;
the replacement is stored only as `spoken_text` on the generation request.
Changing one mapping invalidates only segments containing that mapping. Empty,
non-text, overlong, or control-character entries fail closed before generation.

### `mastering`

`target_lufs` is the internal chapter loudness target. `peak_limit_dbfs` is
enforced using an oversampled peak estimate. `peak_ceiling_mode` is either
`global` (attenuating the whole chapter) or
`soft_limiter` (only samples entering the upper 20% of peak headroom are
compressed before the final true-peak guard). `soft_limiter` is the production
default after the matched sample_book-1 listening A/B found it equally natural
while reducing four-chapter loudness spread from 2.022 LU to 0.032 LU. The
noise gate is normally disabled for synthesized audio; if enabled, threshold,
attack, and release control its envelope.

Only adjacent, pause-free segments are crossfaded. A dialogue/tag pair sharing
an `utterance_group_id` is joined at zero gap without crossfade so the narrator
tag remains a separate voice without sounding like an unrelated turn. Output
defaults to mono 44.1 kHz, 16-bit chapter WAVs.

### `export`

Controls AAC codec, bitrate, channel count, and default tags. Partial filenames are chosen by the orchestrator and contain the included chapter set; `filename_template` does not override that safety rule.

### `server`

| Key | Meaning |
|---|---|
| `host`, `port` | Voice API bind |
| `workers` | Must remain `1` for shared GPU state |
| `cors_origins` | Allowed dashboard origins |
| `api_token` | Required for non-loopback binding |
| `idle_unload_seconds` | Unload models after this period with no active GPU job |

The Brain’s `voice_server.api_token` and Voice’s `server.api_token` must match.

### `storage`

Controls workspace and voice-library roots. Incremental delivery automatically
retains the current part plus its two newest superseded revisions, and keeps the
two newest timestamped `delivery_history/` archives. Other workspace and model
cache size limits are not automatically enforced, so monitor them on long books.

## Secrets

Do not commit tokens or service credentials. The checked-in `.env` is legacy and should not contain live secrets; current local operation does not require SSH credentials.
## Runtime validation and experimental performance settings

Both YAML files are validated before clients or models are constructed. Invalid
URLs, schedules, thresholds, attention backends, and validator backends fail
fast with a combined actionable error. `scripts/runtime_preflight.py` reports
the resolved Python/packages, FFmpeg, TTS attention backend, Whisper backend,
model, device, and VAD mode without importing a GPU model.

`tts.adaptive_max_new_tokens` is experimental and disabled by default. It must
not be promoted until the fixed TTS fixture proves that its bounded cap and
retry-on-truncation behavior improve median RTF without missing speech. The
current `sdpa`, raw-audio OpenAI Whisper path, and one-worker ownership remain
the production baseline.
