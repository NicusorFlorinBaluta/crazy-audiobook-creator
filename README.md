# Crazy Audiobook Creator

Crazy Audiobook Creator turns an EPUB into a multi-speaker, chaptered M4B on one Windows workstation. Ollama performs book-wide character analysis and script annotation; Qwen3-TTS VoiceDesign creates reusable reference voices; Qwen3-TTS Base clones those references; Whisper and audio checks validate every generated segment before mastering.

## What works

- Book-wide character analysis before audio generation
- Source-preserving scripts with exact fragment IDs and source spans
- Bounded same-speaker utterance grouping to reduce TTS calls without losing source traceability
- Stable character voices, including deterministic sharing when a book exceeds the voice cap
- Speaking-only casting with checked descriptions, previews, text redesign, and recorded-reference upload
- Chapter-selectable, resumable audio generation
- Per-line fingerprint cache that invalidates when text, voice, pronunciation, emotion, speed, or generation settings change
- Fail-closed hard validation plus visible non-blocking soft audio warnings
- Narrated chapter-title announcements, chapter WAV mastering, and full or partial M4B export
- Immediate lossy pause/cancel, scheduled chapter-boundary parking, and a global GPU lease
- Local dashboard with scalable chapter search/filtering, explicit current-work progress, editable working hours, script, character, quality, and log views
- Reviewed Google Books matching with manual edition search and metadata-only refresh of completed M4B packages
- Individually named reusable voice-reference downloads plus a complete cast ZIP

## Partial-book workflow

Character analysis and script generation intentionally cover the whole book first. This provides consistent identities, aliases, speaking styles, and voice assignments.

Audio is incremental:

1. Upload the EPUB and let analysis, scripting, and speaking-only voice bootstrap finish.
2. Preview or change the actual speaking cast and approve it once.
3. Select one or more chapters in the dashboard.
4. Start or continue the pipeline. Only those chapters are generated, validated, mastered, and exported.
5. Use the mastered chapter WAVs or the partial M4B immediately.
6. Select another batch later. Valid artifacts are reused with no repeated casting gate.
7. Select **All** to finish missing chapters and create the canonical full-book M4B.

Partial exports include the actual chapter set in the filename, for example `book_chapters_1-3_7.m4b`. A full export is refused until every chapter has a valid mastered artifact.
When script prompts, grouping rules, character dependencies, or schema versions change, the next run performs one book-wide script refresh before generating the selected audio batch. Existing partial M4B files remain on disk for listening during that one-time migration.

## Requirements

- Windows 10/11
- A PyTorch environment capable of running Qwen3-TTS VoiceDesign and Base on the local GPU
- Ollama with the model configured in `brain/config.yaml`
- FFmpeg on `PATH`
- Node.js only if using the Electron wrapper

The checked-in defaults assume the Python environment at `E:\PyTorch env\my_venv`. Change `voice_server.venv` in `brain/config.yaml` if yours is elsewhere.

## Quick start

```powershell
# From the repository root
.\scripts\setup-voice-server.ps1 -VenvPath "E:\PyTorch env\my_venv"

# Pull the model once; the pipeline starts its isolated Ollama service on demand
ollama pull qwen2.5:32b
& "E:\PyTorch env\my_venv\Scripts\python.exe" -m uvicorn brain.dashboard.api.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. The dashboard starts the local voice service on demand when `voice_server.auto_start` is enabled.
The checked-in workstation configuration also starts an app-owned Ollama server
on port 11435 with only Vulkan device 0 (the RX 7900 XTX) visible. This avoids
Ollama splitting the 32B model across the discrete and integrated GPUs.
The desktop wrapper follows the same single-owner lifecycle; it no longer starts a competing Voice process.

For the Electron shell:

```powershell
cd desktop
npm install
cd ..
.\start_desktop.cmd
```

## Important behavior

- Qwen3-TTS Base voice cloning does not expose a natural-language per-utterance instruction parameter. The project therefore applies requested speed plus restrained pitch/tone post-processing for emotion cues; it does not claim native clone-mode emotion control.
- Mastered output targets internal listening quality. It is not an ACX submission validator or an ACX MP3 export pipeline.
- External metadata lookup is opt-in and contacts Google Books only when requested or explicitly enabled. Manual matches are ranked and reviewed before application. Explicit approval adopts the reviewed title and author while retaining the EPUB identity as provenance; embedded cover art is preserved unless replacement is explicitly approved.
- Ollama and Voice bind to loopback. The dashboard may bind to the LAN;
  loopback and `dashboard.trusted_lan_cidrs` are allowed without a token.
- **Pause** immediately interrupts active work and releases app-owned GPU models;
  the current in-flight request may be lost. Closing
  the Electron app performs the same cleanup. Closing a browser tab opened by
  `start_app.pyw` only closes the UI; the background dashboard intentionally
  keeps running for scheduled/unattended work.

## Documentation

- [Architecture](docs/architecture.md)
- [Windows setup](docs/setup-windows.md)
- [Configuration](docs/configuration.md)
- [API reference](docs/api-reference.md)
- [Quality assurance](docs/quality-assurance.md)
- [Scripting quality and performance policy](docs/scripting-quality-performance-policy.md)
- [Dashboard guide](docs/dashboard-guide.md)
- [Voice design](docs/voice-design.md)
- [Prompt and source-fidelity rules](docs/prompts.md)
- [Production-readiness changes and next E2E gates](docs/production-readiness-2026-08-02.md)
- [Full-book release validation and metrics (2026-08-11)](docs/e2e-run-2026-08-11.md)
- [Earlier full-book E2E and echo incident baseline (2026-08-09)](docs/e2e-run-2026-08-09.md)
- [Post-E2E prioritized improvement plan (2026-08-09)](docs/improvement-plan-post-e2e-2026-08-09.md)
- [Post-release performance results and supported benchmarks (2026-08-11)](docs/performance-improvement-plan-post-release-2026-08-11.md)
- [Deferred model/GPU/listening validation plan (2026-08-10)](docs/live-validation-plan-2026-08-10.md)

`implementation_plan*.md` and the `*chat*history*.md` conversation dumps are historical records, not current specifications. Current behavior is defined by this README, `docs/`, models, and executable tests.

## Tests

```powershell
& "E:\PyTorch env\my_venv\Scripts\python.exe" -m unittest discover -s tests -v
```

The unit suite does not load the production TTS models. A real end-to-end smoke test still requires the configured Ollama, GPU models, and FFmpeg.

The 2026-08-12 low-resource release check passed 227 tests with 2 intentional
skips on Windows. The same suite also passed with the optional `whisper`
package hidden, matching GitHub Actions. Python compilation, JavaScript syntax,
local documentation links, and `git diff --check` also passed.

Low-resource verification and environment inspection can be run separately:

```powershell
& "E:\PyTorch env\my_venv\Scripts\python.exe" scripts\runtime_preflight.py --pip-check
& "E:\PyTorch env\my_venv\Scripts\python.exe" scripts\verify_pipeline.py --tier static
& "E:\PyTorch env\my_venv\Scripts\python.exe" scripts\summarize_metrics.py brain\projects\PROJECT_ID\performance_metrics.jsonl
```

Model-backed verification tiers require an explicit `--allow-models` opt-in.
The dashboard's **Runtime & storage** panel exposes the same preflight, a safe
cleanup preview, and a redacted support bundle without including book audio.

Supported performance tools are deliberately gated and reproducible:

- `scripts/benchmark_script_chunks.py` compares scripting chunk bounds.
- `scripts/benchmark_tts_fixture.py` runs the promotion-grade multi-voice TTS
  corpus.
- `scripts/benchmark_tts_dtype.py` screens supported numeric dtypes before a
  full corpus is considered.
- `scripts/analyze_grouping_candidates.py` performs model-free grouping
  headroom analysis.

Shared fingerprinting, ordering, dependency, and summary behavior lives in
`scripts/benchmark_support.py`. Removed ad-hoc benchmark runners must not be
restored as release evidence; new experiments should extend the supported
harnesses and retain their quality and promotion gates.
