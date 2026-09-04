# Review implementation record — 2026-09-03

Companion to [CODE_REVIEW_2026-09-03.md](CODE_REVIEW_2026-09-03.md), which holds
the reasoning. This document records what actually changed, how it was
verified, and what was deliberately left alone.

## Verification

All checks below were run on macOS against a CPU-only environment (no GPU, no
model loads). The project targets a Windows/ROCm workstation, so model-backed
paths were not exercised.

| Gate | Before | After |
| --- | --- | --- |
| `ruff check .` | not configured | **passes** (0 findings) |
| `python -m compileall brain voice shared scripts tools .` | passed (repo root never scanned) | **passes**, repo root included |
| `python -m unittest discover -s tests` | 1 failure, 11 errors | **480 tests, OK, 3 skipped** |
| `python -m pytest tests` | 471 passed, 6 failed | **529 passed, 3 skipped, 0 failed** |
| `node --check` on frontend | 3 of 4 scripts checked | **all 5 + desktop** |
| `scripts/check_markdown_links.py` | **20 broken links** | **passes** |
| Config validation | not in CI | **passes**, now a CI step |

Test count rose from 471 to 529: 58 new tests, all of which fail against the
pre-change code.

## Defects found during implementation that static review had missed

These were not in the original review. Each is a real defect in shipped code.

### 1. Latent `NameError` in the chapter-stream fallback

[brain/dashboard/api/main.py](brain/dashboard/api/main.py) called
`_chapter_duration(...)`, which is defined in
[brain/dashboard/api/mobile.py](brain/dashboard/api/mobile.py) and was never
imported. The path is reachable: it runs when a mastered chapter WAV is absent
but the full M4B exists — i.e. after `cleanup_intermediates` — and slices the
chapter out of the M4B. It would have raised `NameError` and returned 500.

Found by `ruff` rule `F821` on its first run. Fixed by importing the helper.

### 2. `ProgressEvent` was never written

Four call sites constructed a `ProgressEvent(...)` class that does not exist
anywhere in the repository, and called `job_queue.emit_progress(...)`, which is
also never defined. All four sat behind `if hasattr(job_queue,
"emit_progress")`, a guard that is always false — so the blocks were
unreachable, and character-discovery progress was being computed and discarded.

Rather than delete the callback, the two orchestrator sites were rewired onto
the mechanism that works (`job_queue.update_progress` +
`ProgressEstimator.snapshot`), restoring progress reporting during book-wide
character analysis — the longest opaque phase of a run. The two dashboard sites
were removed: the surrounding `update_job` call already carries that transition.

### 3. Install-breaking undeclared dependency

`from dotenv import load_dotenv` runs at import time in
[brain/dashboard/api/security.py](brain/dashboard/api/security.py),
[brain/dashboard/api/main.py](brain/dashboard/api/main.py) and
[brain/orchestrator/nas_syncer.py](brain/orchestrator/nas_syncer.py), but
`python-dotenv` was in no requirements file. Verified by installing
`brain/requirements.txt` into a clean venv: importing the dashboard fails
immediately. `paramiko` was also undeclared (guarded, so degraded rather than
fatal). Both are now declared.

### 4. Generation fingerprint silently dropped the TTS settings

`Pipeline._voice_generation_config()` read `Path("voice/config.yaml")` — a
working-directory-relative literal — and returned `{}` when not found there.
Any launch from another directory therefore omitted the entire `tts` and
`validation` blocks from the generation fingerprint, meaning **a dtype, WER
threshold or attention-backend change would not invalidate cached audio** and
stale segments would be silently reused.

This surfaced because fixing the CWD dependency changed a test that had been
passing *because* the config was unreachable. Regression tests added in
[tests/test_artifacts_and_script.py](tests/test_artifacts_and_script.py) assert
the config resolves identically from any working directory.

### 5. Stale CSS cache revision

`index.html` pinned `styles.css?v=20260821.2` while all four scripts were at
`20260902.1`, letting a browser mix old CSS with new JS — exactly what the
revisioning exists to prevent. `FRONTEND_BUILD` was stale too. Both aligned;
the revision test now derives the expected asset set from disk and cross-checks
`FRONTEND_BUILD`, so this cannot drift silently again.

## Findings from the review

### P0 — guardrails

- **[pyproject.toml](pyproject.toml)** — `ruff` configured for Python 3.12.
  Pyflakes (`F`) is documented as non-negotiable and is at zero. Remaining
  categories are in an explicit ratchet list, each with a count and a reason;
  false-positive rules (`S105`, `S608`, `B008`, `UP042`, `PTH105`) carry an
  explanation of *why* they are false positives here.
- **[.github/workflows/ci.yml](.github/workflows/ci.yml)** — added a Linux
  `lint` job, a `pip-audit` job (advisory), a config-validation step, and
  JavaScript checks that iterate every frontend script rather than three named
  ones. `compileall` now covers the repo root.
- **[requirements-ci.txt](requirements-ci.txt)** — CI previously hand-copied
  four packages out of `voice/requirements.txt` inline in the workflow, so the
  two drifted with nothing to detect it. Dependencies are now declared.
- **[.pre-commit-config.yaml](.pre-commit-config.yaml)** — same ruff gate
  locally, plus whitespace/YAML/JSON/AST checks.
- `ruff format` is **deliberately not enforced**: it would reformat 109 of 177
  files and bury every behavioural change in review. Both the workflow and the
  pre-commit config say so, and say to adopt it as its own commit.

### H1 — `/unload` stalled the voice service event loop

[voice/tts_server/main.py](voice/tts_server/main.py) — `/unload` was
`async def` performing a blocking `gpu_job_lock` acquire. Because `gpu_job()`
holds that lock for an entire chapter, the handler would park the uvicorn event
loop and freeze `/health`, `/cancel` and `/ws/progress` — the endpoints needed
to release it. Its own 409 "busy" branch was unreachable for the same reason.

Now a synchronous endpoint with a non-blocking acquire, so 409 is reachable and
cancellation stays answerable. Six tests in
[tests/test_voice_server_liveness.py](tests/test_voice_server_liveness.py)
pin the endpoint shapes and assert `/cancel` and `/health` respond while a
helper thread holds the lock.

**Correction to the original review:** the caller side was already correct —
`_release_gpu_resources` cancels first, waits 15 s, then unloads, all off-loop.
The exposure is narrower than first stated: only when cancellation misses that
window, which the untimed subprocess calls (below) made possible.

### H2 — three duplicate methods in `ScriptGenerator`

[brain/director/script_generator.py](brain/director/script_generator.py) —
`_metadata_line_map` and `_replace_metadata_line` were a byte-identical
double paste; `_resolve_dialogue_speaker` existed twice with **incompatible
contracts** (`str | None` vs `str`, `None` vs `"narrator"` as the unresolved
sentinel, 35 lines of gender matching vs 115 lines of evidence-based
resolution).

67 lines of dead code removed. The surviving resolver now carries a
"Contract, load-bearing" docstring recording that `"narrator"` is the sentinel
and *why* reintroducing the `None` contract would degrade into silent
under-attribution rather than an error. `F811` now prevents recurrence.

### H3 — security

- **`dashboard.trusted_lan_cidrs` now works.** It was documented in eight
  places as the auditable LAN trust boundary, absent from the `dashboard:`
  config block, and never passed to `dashboard_request_authorized` — so the
  real boundary was a hardcoded default spanning all RFC1918 plus Tailscale
  CGNAT, and could not be narrowed. Added
  `configured_trusted_lan_cidrs()`, wired both the HTTP middleware and the
  WebSocket handler, set `192.168.50.0/24` in config, and added startup CIDR
  validation so a typo fails loudly instead of widening trust. The key
  regression test asserts a peer **inside the default but outside the
  configured range is refused**.
- **The bind guard now runs.** It lived only in `main()`, which nothing
  invokes — `start_app.pyw` (`--host 0.0.0.0`), `desktop/main.js` and the
  documented quick-start all import `app` directly. `_assert_safe_bind` now
  runs at import, and `main()` re-checks so a `--host` override cannot widen
  exposure.
- **CORS narrowed** from `'*'` to the actual origins. With token-free LAN
  trust, `*` let any page a LAN user visited read the dashboard's data
  cross-origin.
- **Voice server**: `secrets.compare_digest` instead of `!=`, and a
  `CRAZY_AUDIOBOOK_VOICE_TOKEN` env fallback mirroring the dashboard so the
  secret need not be committed.
- **Removed a fictional control**: `voice_server.trusted_lan_cidrs` in
  `brain/config.yaml` described LAN-CIDR behaviour the Voice service never
  implemented, in a file it does not read. A comment describing a
  non-existent security control is worse than no comment.
- Deleted the dead `is_forwarded` parameter.

### Medium findings

| Finding | Change |
| --- | --- |
| M1 atomic writes degraded to `shutil.copyfile` (truncates first) | `_atomic_replace` retries `os.replace` with bounded backoff then raises; parent-directory fsync added. 5 tests, including one asserting prior content survives a failed write |
| M2 unbounded `subprocess.run` | Timeouts on all 6 production sites (4 inside `gpu_job()`, where a hang wedged the GPU lock permanently) plus 2 git-provenance calls |
| M3 chapter takeover queued silently | Signals the incumbent, waits a bounded period for the slot, then returns 503. The registry entry is no longer overwritten, so the incumbent's own cleanup matches. 2 tests |
| M4 repetition detection tied to log cadence | Split into `REPETITION_CHECK_INTERVAL_CHUNKS`, independent of `LOG_INTERVAL_CHUNKS` |
| M5 four divergent model-tag defaults | One `DEFAULT_OLLAMA_MODEL` in `shared/constants.py`; the outlier `qwen3:32b` removed |
| M6 second, weaker Ollama path | Pronunciation resolution takes an injected client via a `PronunciationLLM` Protocol (keeping `shared` free of a `brain` dependency), so it inherits retries, repetition detection and cancellation. Threaded through from the pipeline and all 3 dashboard call sites |
| M7 `qwen-tts` unpinned | Cannot be pinned from here without the workstation's resolved version. Instead: `runtime_preflight` now compares every audio-critical package against the tested-constraints file and reports anything unpinned or drifting, the constraints file carries the exact command to obtain the value, and the install docs reference `-c` |
| M11/M12 CORS + token compare | See H3 |
| M13 ~28 CWD-relative paths | New [shared/paths.py](shared/paths.py) resolves from `__file__`. `voice/config.yaml` was re-read at 7 sites; now loaded once per process. `Pipeline.workspace_dir` is an injectable attribute rather than a hidden global |
| M9 frontend "tests" assert source strings | Left as-is, but the asset-revision test was rewritten to derive from disk and to cross-check `FRONTEND_BUILD`. A behavioural DOM harness remains open work |

### Performance and quality (review §10)

- **Ollama runtime is now configurable and logged.** `gpu_backend`
  (`vulkan`/`rocm`), `visible_devices`, `num_parallel`, `kv_cache_type` and
  `keep_alive` are new config keys, validated at startup, with the effective
  values logged at server start. `num_parallel: 1` is the important one: the
  previous autodetected value (commonly 4) reserves KV cache *per slot* and
  scatters sequential chunks across slots, defeating prefix-cache reuse of a
  system prompt that is byte-identical across a chapter's chunks.
- **LLM telemetry is now aggregated.** `prompt_eval_count`, `eval_count` and
  the duration fields had been recorded by `OllamaClient` and forwarded by
  `ScriptGenerator` all along, but nothing consumed them — so the scripting
  half of the pipeline (≈48% of book wall time) had only wall-clock totals
  while TTS had a full substage breakdown. `summarize_metrics` gains
  `script_director_llm` with prefill/decode throughput and a **prefix-cache
  health ratio**: mean `prompt_eval_count` for a chapter's first request versus
  its later ones. Against the measured 2026-08-23 values this reproduces
  143 tok/s prefill, 29.8 tok/s decode and a 33.1% prefill share. 6 tests.
- **Synthesis is now deterministic.** `torch.manual_seed` appeared only in a
  benchmark script; production sampled with `temperature: 0.9` and no seed. Each
  line is now seeded from `(project, line_id, synthesis_text, voice, attempt)`.
  The attempt is included deliberately — a retry exists because the previous
  take failed, so reusing its seed would reproduce the failure. 9 tests.
- **Within-chapter consistency diagnostic added.** Every prior audio gate was
  per-segment or cross-chapter; nothing measured whether adjacent lines in the
  same chapter matched each other. `quality_trends` now reports per-voice pitch
  and speaking-rate spread plus the largest adjacent pitch jump, warning-only,
  computed entirely from measurements already paid for. This is the metric a
  future sampling change needs to be evaluated against. 7 tests.
- **Dead `generate_speech_batch` removed** — never called, and its own
  docstring conceded it was "batch mocked". Replaced with a comment naming the
  three specific upstream capabilities worth watching for, in value order
  (`past_key_values` reuse first, then batched `generate_voice_clone`, then
  static-cache support), because a code search would otherwise find it and
  conclude batching existed.

### Hygiene

- `temp.py` deleted — 612 lines of UTF-16 mojibake duplicating
  `voice_designer.py`, never imported, and invisible to `compileall` because it
  sat at the repo root.
- `datetime.utcnow()` → `datetime.now(timezone.utc)` (deprecated, and returned
  *naive* datetimes that compared incorrectly against the aware values used
  elsewhere).
- `escapeHtml` consolidated from four implementations into
  [js/dom-utils.js](brain/dashboard/frontend/js/dom-utils.js). The copy in
  `app.js` lacked `.toString()`, so passing a number threw. **No XSS existed** —
  every site was escaping; the finding was duplication.
- `empty_response_debug.txt` removed: written to an unpredictable CWD, and by
  definition of its branch always empty.
- `notifier` import hoisted from five `except` blocks to module scope.
- `.gitignore` no longer lists itself twice; caches added; `scratch/` carries a
  note that its contents must not be referenced from `brain/`, `voice/` or
  `shared/`, nor cited as release evidence.
- 439 mechanical `ruff` autofixes (import order, unused imports, deprecated
  aliases), plus 6 genuinely dead locals removed — including a
  `representatives` map in `character_analyzer` that computed the *documented*
  "share a compatible major-character voice" behaviour but was never read.
- 20 broken `file:///e:/Projects/...` documentation links rewritten to relative
  paths, removing a local-machine path leak.

### Documentation

- **[docs/decisions/README.md](docs/decisions/README.md)** created with a status
  convention and an index. The August 2026 record is now marked
  *Partially superseded* and its "joint discovery remains the default" claim is
  struck through with the correction and its evidence inline — it had been
  contradicted by the 2026-08-23 decision and by config for months.
- `README.md`: test count corrected (227 → current), Android app noted as a
  separate repository, emotion post-processing noted as currently **disabled**,
  `max_unique_voices: 0` default clarified, Ollama backend text updated.
- `docs/architecture.md`: voice-sharing description corrected to the archetype
  fallback that actually ships; new sections on deterministic synthesis,
  non-blocking control endpoints, atomic-write retry semantics, and the
  `script_director_llm` metrics.
- `docs/configuration.md`: the five new Ollama keys, and an explicit warning
  that a reverse proxy connects from its own address and is therefore inside
  `trusted_lan_cidrs`.

## Deliberately not done

- **`ruff format`** — 109 files. Needs its own commit.
- **God-object decomposition** (`main.py` 5.7k lines, `script_generator.py`
  4.5k, `pipeline.py` 3.7k) — the review's P3. High value, but not safe to
  attempt without the ability to run the real application.
- **Behavioural frontend tests** — needs jsdom or Playwright wiring; the
  string-presence tests were left in place rather than deleted.
- **The ratchet list** in `pyproject.toml` (`BLE001` ~198, `S110` ~75,
  `B023` ~31, `TRY400`/`TRY004` 22 each, `B904` 13). Each needs case-by-case
  review; mass-fixing them would change exception types and log volume.
- **`qwen-tts` pin** — needs the version from the workstation. `runtime_preflight`
  now reports it; `voice/constraints-windows-rocm-tested.txt` carries the command.
- **Verify the `gemini-3.5-*` model tags** against the current provider catalogue.
  Config validation now enforces structure, not tag existence.

## Worth your attention

**[Testing The Audiobook Pipeline 2.md](Testing%20The%20Audiobook%20Pipeline%202.md)**
(263 KB, repo root) is a raw assistant transcript containing verbatim
copyrighted book excerpts, local machine paths, and service topology. Your own
`.gitignore` has a `/*chat*.md` rule and
[docs/decisions/2026-08-quality-resilience-review.md](docs/decisions/2026-08-quality-resilience-review.md)
states plainly that "raw transcripts must not be committed because they can
contain source-book excerpts, machine paths, service topology, and operational
logs." This file is exactly that, but its name does not match the ignore
pattern. I did not delete it — it is your record and the call is yours — but it
is inconsistent with your stated policy. The same applies to `scratch/`, which
is ignored yet present with ~700 KB of stale PR-review dumps.

## Follow-up on the Windows workstation

Nothing here requires a GPU, but these need the real machine:

1. Run `python scripts/runtime_preflight.py`. It will now report `qwen-tts` as
   installed-but-unpinned; add the reported version to
   `voice/constraints-windows-rocm-tested.txt`.
2. Start the dashboard. The new import-time bind guard is satisfied by the
   `trusted_lan_cidrs` now in config, but confirm it starts cleanly, and confirm
   LAN clients still reach it from `192.168.50.0/24`.
3. Run one chapter of scripting, then
   `python scripts/summarize_metrics.py brain/projects/<id>` and read
   `script_director_llm.prefix_cache`. If `later_to_first_ratio` is near 1.0,
   the shared prompt prefix is being re-evaluated every chunk — screen
   `ollama.gpu_backend: rocm` and confirm `num_parallel: 1` took effect. That is
   the single largest unexplored performance lever, worth an estimated 10–21% of
   scripting wall time.
4. Because the generation fingerprint now correctly includes the TTS and
   validation config (defect 4 above), the first run after this change will
   treat existing cached segments as stale and re-synthesise. That is correct
   behaviour, but it is a one-time cost worth expecting.
