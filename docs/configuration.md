# Configuration

The running configuration lives in `brain/config.yaml` and `voice/config.yaml`. Paths are resolved from the repository root unless stated otherwise.

## `brain/config.yaml`

### `ollama`

| Key | Meaning |
|---|---|
| `host` | Local Ollama base URL |
| `model` | Model used for both character and script metadata passes |
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
| `unload_after_scripting` | Release the Ollama model before loading Qwen TTS on the same GPU |

The application fingerprints the configured model and prompt. Changing them causes dependent script artifacts to be rebuilt. The default workstation configuration uses an isolated loopback port so a separately running Ollama desktop service cannot change its GPU placement.

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

Controls front-matter/TOC/appendix filtering and chapter detection. `min_chapter_words` and `max_chapter_words` influence extracted chapter structure, not the later source-preserving script fragments.

### `script`

- Segment bounds: `max_segment_sentences`, `min_segment_words`
- Default delivery: `default_speed`
- Pause requests: narrator, dialogue, scene, chapter, and paragraph values in milliseconds
- Voice assignment: `max_unique_voices`, `minor_character_threshold`, `group_minor_characters`
- LLM batching: `chunk_size_words`
- TTS-call grouping: `group_utterances`, `utterance_target_chars`, and `utterance_max_words`

`chunk_overlap_words` is retained for configuration compatibility but current source-fragment batching is non-overlapping by design.
Grouping merges only adjacent fragments with the same speaker, voice, and FX. It never crosses a blank paragraph and preserves all source fragment IDs and the exact combined source span.

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

`auto_fetch_external: false` is the privacy-preserving default. Enabling it sends book title/author queries to Google Books after project creation. The dashboard’s manual metadata button always performs the requested external lookup.

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

Cross-midnight windows belong to the weekday on which they start. The worker parks only at safe stage/chapter boundaries, releases a managed Voice process during long waits, and resumes when a configured window opens. A dashboard restart preserves scheduled jobs as resumable instead of claiming their old worker is still running.

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

Oversized script lines are split internally at sentence or whitespace boundaries, synthesized with the same voice prompt, and rejoined.

The reference-voice test sentences in this file are informational; the canonical gender-specific sentences are defined in `shared/constants.py`.

### `validation`

| Key | Meaning |
|---|---|
| `enabled` | Run validation during chapter generation |
| `whisper_model`, `whisper_device` | Transcription backend settings |
| `wer_threshold` | Maximum normalized word error rate |
| `max_retries` | Additional attempts for fail/flag outcomes |
| `keep_tts_and_whisper_resident` | Keep both models inside one chapter's retry loop; Whisper is still released at the chapter boundary |
| `risk_aware_first_attempt` | Listening-approved clarity policy for very short emphatic lines; longer dialogue and ordinary narration remain unchanged |
| `speaker_similarity_threshold` | Minimum Qwen speaker-encoder cosine similarity |
| `clipping_threshold` | Maximum sample peak in dBFS |
| `max_silence_seconds` | Longest permitted internal silence |
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

Only adjacent, pause-free segments are crossfaded. Output defaults to mono 44.1 kHz, 16-bit chapter WAVs.

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

Controls workspace and voice-library roots plus retention preferences. Automatic size enforcement is not currently implemented; monitor `workspace`, model caches, and partial exports on long books.

## Secrets

Do not commit tokens or service credentials. The checked-in `.env` is legacy and should not contain live secrets; current local operation does not require SSH credentials.
