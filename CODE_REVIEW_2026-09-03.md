# Crazy Audiobook Creator — Independent Architecture & Code Review

**Date:** 2026-09-03
**Reviewer:** Claude (Opus 5), static review only
**Scope:** ~72k lines across [brain/](brain/), [voice/](voice/), [shared/](shared/), [scripts/](scripts/), [tools/](tools/), [tests/](tests/), [desktop/](desktop/), [docs/](docs/)
**Method:** Full read of architecture and decision records; targeted deep reads of the orchestrator, script director, Ollama client, voice service, artifact layer, security layer and frontend; AST-level duplicate-definition scan; static audit of subprocess, path, exception and dependency patterns. No code was executed against the pipeline; no git history was consulted.

> **Status: findings have been implemented.** This document is retained as the
> reasoning record. Every finding below was fixed on 2026-09-03 except where a
> deferral is stated explicitly in
> [REVIEW_IMPLEMENTATION_2026-09-03.md](REVIEW_IMPLEMENTATION_2026-09-03.md),
> which lists what changed, what was verified, and what remains open. Read that
> document for current state; read this one for *why*.
>
> Implementation also surfaced four defects that static review had not:
> a latent `NameError` in the chapter-stream fallback, a stale CSS cache
> revision, an install-breaking undeclared `python-dotenv` dependency, and a
> generation-fingerprint bug where TTS/validation settings were silently
> dropped whenever the process working directory differed from the repository
> root.

---

## 1. Verdict

**I agree with the core design.** The central architectural bets are not just defensible, they are unusually well-chosen for the problem:

- Segmentation owned by code, never the LLM
- Completion defined by manifests and hashes, never by file existence
- Book-wide analysis, chapter-incremental audio
- Fail-closed hard validation with a separate, visible soft-warning tier
- Evidence-and-confidence trails with human gates instead of silent guessing

These are the decisions that separate a working audiobook pipeline from a demo, and they were made correctly and early. The decision records in [docs/decisions/](docs/decisions/) show genuine engineering reasoning — quantified evidence, explicit priority ordering, and recorded rejections.

**Where I disagree is almost entirely about execution discipline, not design.** The codebase has grown to 72k lines with no linter, no formatter, no type checker and no pre-commit hook. That single gap is the direct upstream cause of the most serious findings below — including three duplicate method definitions in the script director, two of which are a byte-for-byte double paste and one of which has *divergent semantics between the dead and live copy*. A default `ruff` run would have flagged all three as `F811` on the day they landed.

The second theme is a **documentation/implementation gap concentrated in the security layer** — the one place where the docs are most emphatic and most specific. `dashboard.trusted_lan_cidrs` is described in eight documentation locations as the auditable LAN trust boundary. It is not present in the `dashboard:` config block, and no code path ever passes it. The real boundary is a hardcoded default covering all of RFC1918 plus the Tailscale CGNAT range, and it cannot be narrowed through configuration.

**Headline counts:** 3 high-severity correctness/security findings, 8 medium, and a set of hygiene items. Nothing suggests the pipeline produces bad audiobooks — the quality gates are real and they work. The risks are in operability, security posture, and the maintainability of the attribution code specifically.

---

## 2. What the design gets right

I want to be specific here, because these are load-bearing and should not be refactored away.

### 2.1 The source-fidelity invariant is correctly implemented

[shared/artifacts.py:106-141](shared/artifacts.py#L106-L141) does exactly what the architecture doc claims, and the normalization is strict in the right way:

```python
def normalize_for_coverage(text: str) -> str:
    """Normalize only whitespace for source-coverage comparisons."""
    return re.sub(r"\s+", "", text)
```

Whitespace-only normalization means a dropped word, a dropped punctuation mark, or a rewritten phrase **cannot** pass the coverage check. Combined with the per-line exact span comparison (`source_slice != line.text`) and the monotonic `source_start < previous_end` ordering check, this is a genuine end-to-end guarantee, not a heuristic. Many pipelines claim source fidelity; this one can actually prove it per chapter.

### 2.2 The Ollama streaming client is the best code in the repo

[brain/director/ollama_client.py](brain/director/ollama_client.py) separates concerns that are routinely conflated:

- **Inactivity watchdog vs. total wall clock** — `timeout` resets per chunk, `max_generation_seconds` bounds the whole generation
- **Client-side token cap as defense in depth** — explicitly because "a server version [may ignore] `num_predict`"
- **Repetition-loop detection** — a real failure mode for long structured-JSON generation
- **`done_reason == "length"` detection** — catches silent truncation that would otherwise become a schema error much later
- **Cooperative cancellation checked per streamed line**, with a cancellable retry sleep via `Event.wait()`

That last detail — using `self._cancel_event.wait(seconds)` instead of `time.sleep()` for backoff — is the kind of thing that only gets written by someone who has actually been unable to stop a running job.

### 2.3 Completion is an artifact property

The insistence that "file existence is never sufficient evidence of completion," with independently reconstructed generated/mastered chapter sets and separate synthesis vs. validation-acceptance checkpoints, is the correct answer to resumable GPU pipelines. The distinction that "a valid generated chapter does not imply a valid master" is subtle and right.

### 2.4 Upload and path handling are properly hardened

[brain/dashboard/api/main.py:945-966](brain/dashboard/api/main.py#L945-L966) rejects absolute member paths, `..` traversal, cumulative expansion over the configured cap, and per-member compression ratios above 1000:1. Uploads stream to a `uuid4()` filename opened `"xb"` with a hard byte cap. Every filesystem-facing endpoint in the voice service resolves and re-checks with `is_relative_to`. No `shell=True` anywhere; every subprocess call uses list-form argv. This is real defense in depth.

### 2.5 SQLite state handling is correct

[brain/orchestrator/job_queue.py:127-133](brain/orchestrator/job_queue.py#L127-L133) uses WAL mode, `busy_timeout=30000`, a 30s connection timeout, and `BEGIN IMMEDIATE` for read-modify-write transactions. This matches what the architecture doc promises.

### 2.6 The live-model opt-in guard

[shared/live_test_guard.py](shared/live_test_guard.py) refusing to load models without an explicit `--allow-models` flag, plus the `if: ${{ false }}` gating on the `model-gates` CI job, is a genuinely thoughtful pattern for a repo where an accidental test run costs GPU hours.

### 2.7 The decision records

[docs/decisions/separate-character-pass-and-attribution-gate-2026-08-23.md](docs/decisions/separate-character-pass-and-attribution-gate-2026-08-23.md) is a model of its kind. It quantifies the failure (138 blocking findings across 29 chapters), *isolates the variable* (45 findings in 14 Qwen 2.5 chapters vs. 93 in 49 Qwen 3.8 chapters, proving the model was not the cause), identifies the actual mechanism (the missing identity was absent from Gemini's allowed candidate set, so escalation could not fix it), and states the resulting safeguards. The explicit refusal in [docs/plans/unattended-full-app-audit-2026-08-23.md](docs/plans/unattended-full-app-audit-2026-08-23.md) — "Do not promote Qwen 3.8 into another stage solely because it is newer" — is exactly the right instinct.

---

## 3. High-severity findings

### H1 — `POST /unload` blocks the voice service event loop; its "busy" guard is unreachable

**[voice/tts_server/main.py:936-952](voice/tts_server/main.py#L936-L952)**

```python
@app.post("/unload")
async def unload_models():
    global engine, validator
    unloaded = []
    with gpu_job_lock:  # <-- blocking acquire on the event loop
        if active_gpu_jobs:
            raise HTTPException(status_code=409, detail="Models are busy; ...")
```

`gpu_job()` at [voice/tts_server/main.py:88-99](voice/tts_server/main.py#L88-L99) holds `gpu_job_lock` for the **entire** duration of the wrapped work. For `/generate/chapter` that is `validator.process_chapter(...)` — a multi-minute, whole-chapter operation ([voice/tts_server/main.py:673](voice/tts_server/main.py#L673)).

Two consequences:

1. **The 409 branch is dead code.** While any GPU job runs, the lock is held, so `unload_models` never reaches the `if active_gpu_jobs:` check. It blocks instead of failing fast — the opposite of the intent expressed in the code.
2. **The whole event loop stalls.** Because the handler is `async def`, the blocking `acquire()` runs on the uvicorn event loop. Every other async endpoint freezes with it: `/health`, `/cancel/{project_id}`, and `/ws/progress`.

That third one is the real problem. `/cancel` is the *only* way to stop a running chapter, and it is `async def`.

**Credit where due: the caller side is written correctly.** [`_release_gpu_resources`](brain/dashboard/api/main.py#L129-L178) calls `cancel_project` for every active project *first*, then `await asyncio.wait(active_tasks, timeout=15.0)`, and only then `unload_models` — each via `asyncio.to_thread`, so the dashboard's own loop never blocks. That ordering is right, and in the common case cancellation completes inside 15s and `/unload` arrives to an idle lock.

The exposure is the case where cancellation does **not** complete in 15 seconds. The dashboard proceeds to `/unload` anyway; the voice service's async handler blocks on the held lock; and `/health`, `/cancel` and `/ws/progress` freeze until the chapter finishes on its own. The client's `timeout=30` ([voice_client.py:300](brain/orchestrator/voice_client.py#L300)) makes the *caller* give up, but the server-side handler stays parked on `acquire()`, so the loop stays frozen well past that — and a retried `/cancel` cannot get through to fix it.

This is where [M2](#m2--unbounded-subprocessrun-inside-the-gpu-lock) turns a latent issue into a reachable one: an ffmpeg call with no timeout inside `gpu_job()` can hold the lock indefinitely, which is precisely the condition that makes cancellation miss the 15s window. The two findings should be fixed together.

The net effect on the documented behavior in [README.md](README.md) —

> **Pause** immediately interrupts active work and releases app-owned GPU models

— is that it holds under normal load and inverts exactly when a job is already stuck, which is when the operator most needs it.

**Fix:** make the handler `def` (threadpool) *and* use a non-blocking acquire, so the existing 409 becomes reachable:

```python
@app.post("/unload")
def unload_models():
    if not gpu_job_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Models are busy; cancel first")
    try:
        ...
    finally:
        gpu_job_lock.release()
```

Add a regression test that starts a fake long-running `gpu_job()` and asserts `/health` still responds and `/unload` returns 409 promptly.

---

### H2 — Three duplicate method definitions in `ScriptGenerator`, one with divergent semantics

AST scan of the repo found duplicates in exactly one file:

```
brain/director/script_generator.py::ScriptGenerator._metadata_line_map        lines [2207, 2239]
brain/director/script_generator.py::ScriptGenerator._replace_metadata_line    lines [2219, 2251]
brain/director/script_generator.py::ScriptGenerator._resolve_dialogue_speaker lines [3923, 4091]
```

Python silently keeps the **last** definition. The earlier ones are unreachable.

**`_metadata_line_map` / `_replace_metadata_line`** are a byte-for-byte double paste of a 34-line block — behaviorally harmless, but unambiguous evidence of an unreviewed merge.

**`_resolve_dialogue_speaker` is the serious one.** The dead and live copies have incompatible contracts:

| | Dead (3923) | Live (4091) |
|---|---|---|
| `registry` | keyword-only, **required** | positional, **optional** |
| Return type | `str \| None` | `str` |
| Unresolved result | `None` | `"narrator"` |
| `target_gender is None` | returns `None` immediately | infers gender from pronouns, then proceeds |
| Logic | ~35 lines, gender-match only | ~115 lines: speech tags, alias uniqueness, turn alternation, gender elimination |

Both production call sites ([script_generator.py:2296](brain/director/script_generator.py#L2296), [script_generator.py:2673](brain/director/script_generator.py#L2673)) pass `registry=registry` as a keyword and guard on `resolved != "narrator"`, so they are written against the live version and **behave correctly today**. This is latent, not active.

But it is a loaded gun, for a specific reason. The natural way to "clean up" a duplicate is to keep the first definition — it looks like the original. Doing that here would swap a 115-line evidence-based resolver for a 35-line gender-only one, and change the unresolved sentinel from `"narrator"` to `None`. Both call sites test `if resolved and resolved != "narrator"`, which tolerates `None` — so the failure would be *silent under-attribution*, not a crash. That is precisely the failure class documented in [docs/speaker-attribution-incident-2026-08-11.md](docs/speaker-attribution-incident-2026-08-11.md).

The live version is also, on inspection, clearly the better resolver — I agree with its conservative design, especially the two comments encoding hard-won judgment:

> "A unique adjacent alias mention is a candidate, never an automatic high-confidence correction. Multiple matches are intentionally ignored."

> "Gender evidence is usable only when it leaves one candidate. It must never select the most recent of several same-gender speakers."

**Fix:** delete lines 3922-3956 and 2206-2238. Add a docstring note on the survivor recording that `"narrator"` (not `None`) is the unresolved sentinel and that callers must keep contextual candidates below the audit threshold. Then add `ruff` with `F811` enabled so this cannot recur (see [P0](#p0--install-the-guardrail-that-would-have-caught-h2)).

---

### H3 — The documented LAN trust boundary is not wired; the bind guard is bypassed by every real launch path

Three defects that compound into one exposure.

**H3a — `dashboard.trusted_lan_cidrs` is inert.**

The key is referenced as *the* security control in eight documentation locations ([README.md:81](README.md#L81), [docs/architecture.md:267](docs/architecture.md#L267), [docs/api-reference.md:8](docs/api-reference.md#L8), [docs/configuration.md:136](docs/configuration.md#L136), [docs/setup-windows.md:110](docs/setup-windows.md#L110), [docs/production-readiness-2026-08-02.md:47](docs/production-readiness-2026-08-02.md#L47), and twice in [docs/home-assistant-integration-plan.md](docs/home-assistant-integration-plan.md)).

It appears nowhere in the `dashboard:` block of [brain/config.yaml](brain/config.yaml) — only under `voice_server:`. And the middleware never passes it:

```python
# brain/dashboard/api/main.py:1316-1322
and not dashboard_request_authorized(
    client_host=client_host,
    configured_token=token,
    presented_token=request.headers.get("X-API-Token"),
)   # <-- trusted_lan_cidrs never supplied
```

So `dashboard_request_authorized` falls back to its default ([brain/dashboard/api/security.py:17-24](brain/dashboard/api/security.py#L17-L24)): all of `10/8`, `172.16/12`, `192.168/16`, `100.64/10` (Tailscale CGNAT), `fc00::/7`, `fe80::/10`. The docs instruct operators to narrow this to their subnet. **They cannot.** The setting is not readable, not documented in the file it would live in, and not consulted.

**H3b — the bind-safety guard never executes.**

[brain/dashboard/api/main.py:5730-5735](brain/dashboard/api/main.py#L5730-L5735) refuses to bind beyond loopback without a token. But it lives in `main()`, and nothing launches the app through `main()`:

- [start_app.pyw:37-40](start_app.pyw#L37-L40) → `python -m uvicorn brain.dashboard.api.main:app --host 0.0.0.0`
- [README.md:57](README.md#L57) Quick start → `python -m uvicorn brain.dashboard.api.main:app --host 127.0.0.1 --port 8000`
- [desktop/main.js:88](desktop/main.js#L88) → `spawn(pythonExe, ['-m', 'uvicorn', ...])`

All three import `app` directly. The guard is unreachable in practice, and the shipped config is `host: 0.0.0.0` with `api_token: ''`.

**H3c — behind the reverse proxy, app-level auth is fully disabled.**

The middleware authorizes on the real TCP peer, which is correct and explicitly reasoned about ("forwarded headers never grant trust" — good). But the deployment proxies `https://crazyha.mywire.org/audiobook/` to `192.168.50.44:8000`. The TCP peer is then nginx's own LAN address, which is inside the default trusted CIDRs. **Every internet request arriving through the proxy is authorized as a trusted LAN peer, with no token.** The only remaining control is nginx basic auth, which lives outside this repo and outside its tests.

**Fix, in order:**

1. Read the key and pass it. Add `trusted_lan_cidrs` to the `dashboard:` block in [brain/config.yaml](brain/config.yaml) (`[192.168.50.0/24]` for this deployment) and pass `trusted_lan_cidrs=_dashboard_cfg.get("trusted_lan_cidrs", DEFAULT_TRUSTED_LAN_CIDRS)` in the middleware. Validate the CIDRs in [shared/config_validation.py](shared/config_validation.py) so a typo fails at startup rather than silently widening trust.
2. Move the bind guard out of `main()` into module import (or the `lifespan` startup), so it applies however `app` is loaded. Fail closed.
3. Require a token when the peer is the proxy. Either give the proxy its own address outside the trusted set, or require `X-API-Token` for any non-loopback peer and have nginx inject it. Do not rely on basic auth alone for a service with filesystem-reaching endpoints.
4. Delete the unused `is_forwarded` parameter ([security.py:82](brain/dashboard/api/security.py#L82)) — it is accepted and never read, which invites a caller to believe it does something.

---

## 4. Medium-severity findings

### M1 — Atomic writes silently degrade to non-atomic copies

**[shared/artifacts.py:66-69](shared/artifacts.py#L66-L69) and [shared/artifacts.py:88-91](shared/artifacts.py#L88-L91)**

```python
try:
    os.replace(temporary, destination)
except PermissionError:
    shutil.copyfile(temporary, destination)
```

On Windows, `os.replace` transiently fails with `PermissionError` when an antivirus scanner or search indexer holds the destination open — which is common, and clearly why this fallback exists. But `shutil.copyfile` **truncates the destination first**. A crash or power loss mid-copy leaves a truncated JSON state file. This is the exact failure mode that "atomic replacement is used for JSON state and final audio writes" ([docs/architecture.md](docs/architecture.md)) promises to prevent, and it degrades silently.

**Fix:** retry `os.replace` with bounded backoff (5 attempts, 50-500ms) and raise if it never succeeds. A loud failure is far better than a truncated `pipeline_state` or `voice_cast.json`. Consider also `os.fsync()` on the parent directory handle after the rename for true crash durability.

### M2 — Unbounded `subprocess.run` inside the GPU lock

Static scan found 13 `subprocess` calls with no `timeout`. Four are inside the voice service's audio path — [voice/tts_server/audio_effects.py:72,141,254,386](voice/tts_server/audio_effects.py#L72) — which executes **inside `gpu_job()`**. A hung ffmpeg there holds `gpu_job_lock` forever, permanently wedging the voice service (and, per [H1](#h1--post-unload-blocks-the-voice-service-event-loop-its-busy-guard-is-unreachable), the event loop with it). Two more are in a dashboard request path: [main.py:2416,2447](brain/dashboard/api/main.py#L2416).

**Fix:** add explicit `timeout=` to all six production call sites and handle `subprocess.TimeoutExpired`. The pattern already exists correctly at [m4b_exporter.py:351](voice/mastering/m4b_exporter.py#L351) — apply it uniformly.

`audio_effects.py` also resolves ffmpeg three different ways across the repo (a bundled `tools/ffmpeg/ffmpeg.exe` that is not present in this tree, a bare `"ffmpeg"` from PATH in [m4b_exporter.py:289](voice/mastering/m4b_exporter.py#L289), and `shutil.which("ffmpeg")` in main.py). Consolidate into one resolver in [shared/](shared/).

### M3 — Chapter-generation takeover does not wait for the displaced worker

**[voice/tts_server/main.py:636-655](voice/tts_server/main.py#L636-L655)**

After 5s of polling, the handler signals the old run's cancellation, overwrites the registry entry, and immediately starts a second worker thread — without waiting for the first to acknowledge. Because `gpu_job_lock` serializes them, there is no GPU corruption, and the old worker's `finally` correctly declines to pop an entry that is no longer identity-equal (`is cancellation`) — that detail is handled well.

But the new job then blocks on the lock for however long the old chapter takes to reach a cancellation boundary, streaming only keepalive newlines. The caller sees an apparently-live stream doing nothing, with no diagnostic. "Take over" is really "silently queue behind."

**Fix:** wait on an acknowledgement event with a bounded deadline, then either proceed or return `503` with a clear reason. Emit a `progress` event describing the wait so the dashboard can show it.

### M4 — Repetition-loop detection is accidentally coupled to logging cadence

**[brain/director/ollama_client.py:255-268](brain/director/ollama_client.py#L255-L268)**

`_has_repeated_tail()` is called *inside* the `if token_count - last_log_tokens >= 200:` block. Loop detection therefore only samples every 200 chunks, purely as a side effect of where it was placed. Changing the log interval silently changes a quality gate's sensitivity.

**Fix:** hoist the check to its own explicit interval counter with a named constant, independent of logging.

### M5 — Four scattered literal defaults for one model tag, two of them different

| Location | Default |
|---|---|
| [brain/config.yaml:9](brain/config.yaml#L9) | `qwen3.8:27b` |
| [brain/orchestrator/pipeline.py:137](brain/orchestrator/pipeline.py#L137) | **`qwen3:32b`** |
| [shared/pronunciation.py:258](shared/pronunciation.py#L258) | `qwen3.8:27b` |
| [scripts/repair_attributions.py:133](scripts/repair_attributions.py#L133) | `qwen3.8:27b` |
| [brain/validators/tiered_adjudicator.py:297,322](brain/validators/tiered_adjudicator.py#L297) | `qwen3.8:27b` (telemetry) |

If config is missing or unreadable, the orchestrator uses a *different model* than pronunciation resolution and attribution repair. With `fallback_models: []` and the comment "Never substitute an arbitrary installed model," these divergent fallbacks contradict the stated policy — one of them silently substitutes.

Worse, [shared/pronunciation.py:255](shared/pronunciation.py#L255) re-reads config from a **CWD-relative** path (`Path("brain/config.yaml")`) inside a `try/except Exception: pass`. Launch from any other directory and it silently falls back to the hardcoded tag with no warning.

**Fix:** one `DEFAULT_OLLAMA_MODEL` constant in [shared/constants.py](shared/constants.py); every site references it. Better: pass the configured `OllamaClient` into pronunciation resolution rather than letting it construct its own endpoint.

### M6 — A second, weaker Ollama client path that pause cannot interrupt

[shared/pronunciation.py:285-320](shared/pronunciation.py#L285-L320) calls Ollama through raw `urllib.request` instead of `OllamaClient`. It therefore bypasses every guarantee in [H2's praise section](#22-the-ollama-streaming-client-is-the-best-code-in-the-repo): no retry budget, no repetition-loop detection, no `done_reason == "length"` check, no cancellation. `pipeline.stop()` calls `self.ollama.cancel_current(force=True)`, which this path never sees — so a pause during pronunciation resolution does not interrupt it.

The repo has three HTTP stacks: `httpx` (OllamaClient, VoiceClient), `urllib.request` ([notifier.py](brain/orchestrator/notifier.py), pronunciation), and `requests` ([voice_designer.py:746](voice/tts_server/voice_designer.py#L746)).

**Fix:** route pronunciation resolution through `OllamaClient`. Standardize on `httpx` and drop `requests` from [voice/requirements.txt](voice/requirements.txt).

### M7 — `qwen-tts` is completely unpinned

**[voice/requirements.txt:11](voice/requirements.txt#L11)**

```
qwen-tts
```

No floor, no ceiling, on the package that produces the actual audio. Every other dependency has at least a `>=` floor, and `transformers` is hard-pinned to `4.57.3` — so the intent to control the TTS stack is clearly there, and this line is the gap in it. A silent upstream release can change voice-cloning behavior between two runs of the same book, invalidating benchmark comparisons and reference-voice reuse without any fingerprint change.

[voice/constraints-windows-rocm-tested.txt](voice/constraints-windows-rocm-tested.txt) records the versions from the successful 2026-08-09 run — a good practice — but it does not include `qwen-tts`, is not referenced by any install instruction in [README.md](README.md) or [docs/setup-windows.md](docs/setup-windows.md), and is not used by CI.

**Fix:** pin `qwen-tts==<the version that produced the 2026-08-11 release>`, add it to the tested-constraints file, and reference that file with `-c` in the documented install command. Record the resolved version in `performance_metrics.jsonl` alongside the model name so a benchmark is attributable to a stack.

### M8 — Provider model names are unvalidated and duplicated across budget keys

**[brain/config.yaml:129-149](brain/config.yaml#L129-L149)**

`gemini-3.5-flash-lite` and `gemini-3.5-flash` appear four times each — under `character_augmentation`, under `api`, and again as `daily_request_budgets` keys. Budgets are keyed by exact string, so renaming a model in one place silently disables its budget enforcement while leaving requests flowing. [shared/config_validation.py](shared/config_validation.py) validates numeric ranges and the `think` enum but does not cross-check that every configured model has a budget entry.

Given today's date, these tags are also worth verifying against the current Google model catalogue before the next run — a 404 from a renamed model would surface as a generic escalation failure, and [external_validation.circuit_breaker](brain/config.yaml#L136) would open after three failures, silently disabling the whole escalation ladder.

**Fix:** declare each model once and reference it; add a config-validation rule asserting every referenced model has a `daily_request_budgets` entry; log the provider's own model-not-found error distinctly from a transport failure so the circuit breaker does not mask a config typo.

### M9 — The frontend tests assert on source text, not behavior

**[tests/test_dashboard_frontend_ux.py](tests/test_dashboard_frontend_ux.py)**

```python
assert "handleTabKeydown" in app
assert page.count('role="tab"') == 4
assert "document.createElement('button')" in app
```

These pass if an identifier appears *anywhere* in a 152KB file. They do not verify that tab navigation works, that focus is trapped in the modal, or that Escape closes it. They break on any harmless rename, and they pass while the feature is broken.

This matters beyond the frontend: it characterizes how the suite generates confidence. 480 test functions and a green CI is a strong signal *for the Python core* — those tests are real, and the [artifacts](tests/test_artifacts_and_script.py)/[validation-loop](tests/test_validation_loop.py)/[delivery](tests/test_delivery_manager.py) suites are substantive. But string-presence assertions are a distinct and weaker category, and mixing them into the same count inflates the apparent coverage. It is also part of why [H2](#h2--three-duplicate-method-definitions-in-scriptgenerator-one-with-divergent-semantics) survived: the suite checks for the presence of strings, never the absence of duplication.

**Fix:** keep a handful as cheap regression pins, but rename them (`test_*_source_contains_*`) so they are not mistaken for behavioral coverage. Add real DOM tests for the flows that matter — modal focus trap, tab keyboard nav, review-gate banner rendering — with jsdom or Playwright (already a declared dependency).

### M10 — CI does not install the voice requirements or run any static analysis

**[.github/workflows/ci.yml](.github/workflows/ci.yml)**

```yaml
python -m pip install -r brain/requirements.txt pydub pyloudnorm librosa jiwer
```

[voice/requirements.txt](voice/requirements.txt) is never installed; four of its packages are hand-copied into the CI command instead. That list will drift from the real file, and nothing detects it. There is no `ruff`, no `mypy`, no `pip-audit`, no coverage measurement. `python -m compileall -q brain voice shared scripts` covers only those four directories — repo-root Python is never compiled (see [L4](#l4--temppy-is-a-utf-16-mojibake-duplicate-at-the-repo-root)).

**Fix:** see [P0](#p0--install-the-guardrail-that-would-have-caught-h2).

### M11 — CORS `*` allows cross-origin reads from any LAN user's browser

[brain/config.yaml:110-111](brain/config.yaml#L110-L111) sets `cors_origins: ['*']`. The code correctly derives `allow_credentials="*" not in _cors_origins` ([main.py:1281](brain/dashboard/api/main.py#L1281)), so credentialed wildcard requests are blocked — that part is right.

But with token-less LAN trust, any web page a LAN user visits can issue non-credentialed cross-origin `GET`s to `http://192.168.50.44:8000/api/...` and **read the responses**, because the response carries `Access-Control-Allow-Origin: *`. The `Sec-Fetch-Site` check ([security.py:28-33](brain/dashboard/api/security.py#L28-L33)) blocks cross-site *mutations* only. Book catalogues, scripts and character registries are readable; this is also a DNS-rebinding vector.

**Fix:** set `cors_origins` to the actual dashboard origins (loopback, LAN IP, and the proxy hostname). There is no reason for `*` on a single-operator tool.

### M12 — The voice service token comparison is not constant-time, and its LAN config is fiction

**[voice/tts_server/main.py:373-381](voice/tts_server/main.py#L373-L381)**

```python
if request.headers.get("X-API-Token") != token:
```

Plain `!=`, where the dashboard correctly uses `secrets.compare_digest` ([security.py:97](brain/dashboard/api/security.py#L97)). Inconsistent, and the safe pattern already exists in the repo.

Separately, [brain/config.yaml:39-45](brain/config.yaml#L39-L45) carries this under `voice_server:`:

```yaml
# Only the socket peer is considered; X-Forwarded-For cannot grant access.
# Home Assistant/nginx and other devices on these LANs may use the API
# without an application token.
trusted_lan_cidrs:
- 192.168.50.0/24
```

The voice service implements **no CIDR logic at all** — it checks a token or nothing. And it reads [voice/config.yaml](voice/config.yaml)'s `server:` block, not `brain/config.yaml`'s `voice_server:` block, so the key is in the wrong file besides. This comment describes a security control that does not exist, which is worse than no comment.

Also: unlike the dashboard, the voice service has no environment-variable token fallback, so its token can only be set by editing tracked YAML — inconsistent with the deliberate design of `CRAZY_AUDIOBOOK_DASHBOARD_TOKEN`.

**Fix:** use `compare_digest`; add a `CRAZY_AUDIOBOOK_VOICE_TOKEN` env fallback mirroring the dashboard; delete the misleading `trusted_lan_cidrs` block or implement it using the shared helper in [security.py](brain/dashboard/api/security.py).

### M13 — Roughly 28 CWD-relative hardcoded paths; `voice/config.yaml` re-read at six sites

Every launch path happens to `cd` to the repo root, so this works — but nothing enforces or documents it, and the failure mode is silent (a `try/except Exception: pass` fallback to defaults, as in [M5](#m5--four-scattered-literal-defaults-for-one-model-tag-two-of-them-different)).

[voice/config.yaml](voice/config.yaml) is re-read from disk at [pipeline.py:1939](brain/orchestrator/pipeline.py#L1939), [pipeline.py:3312](brain/orchestrator/pipeline.py#L3312), [pipeline.py:3668](brain/orchestrator/pipeline.py#L3668), [pipeline.py:3699](brain/orchestrator/pipeline.py#L3699), [main.py:349](brain/dashboard/api/main.py#L349), [main.py:710](brain/dashboard/api/main.py#L710) and [main.py:4707](brain/dashboard/api/main.py#L4707). Beyond redundant I/O, if the file is edited mid-run, different subsystems observe different values within a single chapter — which would be very hard to diagnose from artifacts.

**Fix:** add `shared/paths.py` resolving everything from `Path(__file__).resolve().parents[N]`, plus a cached config accessor loaded once per process. This is mechanical and high-value.

### M14 — 128 `except Exception:` handlers, 74 immediately `pass`

Some are correct and well-commented — the one at [pipeline.py:1267-1271](brain/orchestrator/pipeline.py#L1267-L1271) explains precisely why a missing voice service is expected during scripting-only runs. That is the right way to write a broad catch.

But 74 silent `pass` handlers across the orchestration and dashboard layers means real failures vanish with no log line. The notification blocks in [`_run_exclusive`](brain/orchestrator/pipeline.py#L1245) are the better pattern — they at least `logger.debug`. That should be the floor everywhere.

**Fix:** `ruff` with `BLE001`/`S110`, then triage: narrow the exception type, or log at `debug` with context. Also import `notifier` at module scope rather than four times inside `except` blocks.

---

## 5. Low-severity and hygiene

### L1 — `datetime.utcnow()` is deprecated
[shared/models.py:319,326,562,783](shared/models.py#L319) — deprecated since Python 3.12 (CI runs 3.12) and returns a naive datetime, which risks off-by-timezone comparisons against the `datetime.now(timezone.utc)` used elsewhere in [pipeline.py](brain/orchestrator/pipeline.py#L983). Replace with `lambda: datetime.now(timezone.utc)`.

### L2 — `atomic_write_json(default=str)` silently coerces
[shared/artifacts.py:93](shared/artifacts.py#L93) — a `Path`, `datetime` or Pydantic object that reaches state serialization becomes a string instead of raising. Convenient, but it hides type bugs in persisted state. Prefer explicit `model_dump(mode="json")` at call sites.

### L3 — `empty_response_debug.txt` written to the CWD
[brain/director/ollama_client.py](brain/director/ollama_client.py) writes this file on an empty response — but the content it writes is `text`, which is empty by definition of that branch. Always-useless file, written to an unpredictable directory, overwritten each time. Delete it; the adjacent `logger.error` already carries the useful information.

### L4 — `temp.py` is a UTF-16 mojibake duplicate at the repo root
612 lines, 52KB, UTF-16LE with CRLF, containing a corrupted copy of [voice/tts_server/voice_designer.py](voice/tts_server/voice_designer.py) (`ÔÇö` is a double-encoded em dash). Not imported anywhere. It cannot be parsed as Python 3 source, and CI's `compileall` only covers `brain voice shared scripts` — so it is invisible to every check. Delete it, and extend `compileall` to the repo root.

### L5 — `.gitignore` lists itself, twice
[.gitignore](.gitignore) contains `.gitignore` as an entry on two separate lines, alongside four individually-named `scratch/` files *and* a blanket `scratch/`. Self-ignoring does nothing for a tracked file. Meanwhile [scratch/](scratch/) is present with 29 files including ~700KB of stale CodeRabbit PR-comment dumps (`pr5_coderabbit_all_comments.json`, 346KB). Clean up the ignore file and move the scratch tooling into [tools/](tools/) or delete it.

### L6 — Four independent HTML-escape implementations
`escapeHtml` is defined separately in [app.js:3011](brain/dashboard/frontend/js/app.js#L3011), [pipeline.js:296](brain/dashboard/frontend/js/pipeline.js#L296) and [script-viewer.js:2254](brain/dashboard/frontend/js/script-viewer.js#L2254), plus a fourth inline variant in [log-console.js:34-46](brain/dashboard/frontend/js/log-console.js#L34-L46).

To be clear: **I checked, and there is no XSS here.** `highlightLine` escapes `&`, `<` and `>` before its `innerHTML` assignment, and the 52 `innerHTML` sites in `script-viewer.js` are backed by 94 `escapeHtml` calls. The finding is duplication, not vulnerability. But the log-console variant does not escape quotes, so it is only safe because it happens not to interpolate into an attribute — a constraint no comment records. Extract one shared helper.

### L7 — Documentation drift
- [README.md](README.md) reports "227 tests with 2 intentional skips"; [docs/plans/unattended-full-app-audit-2026-08-23.md](docs/plans/unattended-full-app-audit-2026-08-23.md) records "410 passed, 2 skipped"; the tree contains ~480 test functions.
- [docs/decisions/2026-08-quality-resilience-review.md](docs/decisions/2026-08-quality-resilience-review.md) states "Joint character discovery remains the default." It was superseded by [separate-character-pass-and-attribution-gate-2026-08-23.md](docs/decisions/separate-character-pass-and-attribution-gate-2026-08-23.md) and by `joint_analysis: false` in config — but carries no `Status:` or `Superseded-by:` header. A reader landing on it first gets the wrong answer. **This is the most valuable documentation fix in the list**, because the decision records are otherwise the best artifact in the repo and this undermines trust in them.
- [README.md](README.md) lists the CrazyVoice Android app under "What works" without noting that its source lives in a separate repository (correctly disclosed only at the bottom of [docs/crazy-voice-companion.md](docs/crazy-voice-companion.md)).
- README and [docs/architecture.md](docs/architecture.md) describe deterministic voice sharing "up to `script.max_unique_voices`", but the shipped default is `0` (unlimited), so that behavior is off by default. The code handles `0` correctly ([character_analyzer.py:922](brain/director/character_analyzer.py#L922)) — it is the docs that describe a non-default configuration as if it were the default.

### L8 — God objects
[brain/dashboard/api/main.py](brain/dashboard/api/main.py) 5,752 lines · [script_generator.py](brain/director/script_generator.py) 4,589 · [pipeline.py](brain/orchestrator/pipeline.py) 3,723 (with `_run_exclusive` a single 364-line state machine) · [app.js](brain/dashboard/frontend/js/app.js) 3,279 · [script-viewer.js](brain/dashboard/frontend/js/script-viewer.js) 2,325.

`_run_exclusive` also reads `current_stage` once at the top and then makes control-flow decisions on it hundreds of lines later ([pipeline.py:1101](brain/orchestrator/pipeline.py#L1101), [1112](brain/orchestrator/pipeline.py#L1112)) while re-fetching `state` five times in between. It works, but the staleness asymmetry is exactly where a future resume bug will land. There is also a dead assignment: `selection` is set at [line 1095](brain/orchestrator/pipeline.py#L1095) and unconditionally overwritten at [line 1121](brain/orchestrator/pipeline.py#L1121) before any read.

### L9 — `_raise_if_cancelled` raises `KeyboardInterrupt`
[brain/director/ollama_client.py:85-87](brain/director/ollama_client.py#L85-L87). This is deliberate and it works — `KeyboardInterrupt` is a `BaseException`, so it deliberately tunnels through all 128 `except Exception:` handlers up to the `except KeyboardInterrupt` in [`_run_exclusive`](brain/orchestrator/pipeline.py#L1290). Clever. But it conflates programmatic cancellation with a real Ctrl-C, and the voice service already defines a proper `GenerationCancelled` for the same job. Define `GenerationCancelled(BaseException)` in [shared/](shared/) and use it on both sides.

### L10 — ZIP symlink members are not checked
[_validate_epub_archive](brain/dashboard/api/main.py#L945) rejects absolute paths and `..`, but not symlink members (expressible via `external_attr`). Risk is low because `ebooklib` reads in memory rather than extracting, but a one-line mode check would close it.

---

## 6. Library and model version assessment

| Item | Current | Assessment |
|---|---|---|
| `transformers` | `==4.57.3` | **Good.** Hard-pinned, and the conflict with Parler's 4.46.1 is correctly isolated in [requirements-legacy-parler.txt](voice/requirements-legacy-parler.txt) with an explanatory comment. Textbook handling. |
| `qwen-tts` | *unpinned* | **Fix ([M7](#m7--qwen-tts-is-completely-unpinned)).** The one dependency whose output *is* the product, with no version bound. |
| PyTorch/ROCm | intentionally unconstrained | **Correct.** Machine-specific build; the comment in [constraints-windows-rocm-tested.txt](voice/constraints-windows-rocm-tested.txt) explains why. Agreed. |
| `faster-whisper` / `openai-whisper` | both installed | **Correct, and well-reasoned.** Config selects `openai_whisper` because CTranslate2's CUDA runtime targets NVIDIA on Windows while this rig is AMD/ROCm ([voice/config.yaml:118-120](voice/config.yaml#L118)). Keeping both costs little and preserves the option. |
| `whisper large-v3` | selected | **Sound.** Best accuracy tier; justified for a fail-closed WER gate at `0.20`. |
| `silero-vad` | installed, `vad_filter: false` | **Correct, evidence-based.** Disabled because raw Whisper preserved short/high-pitched speech better in the full-book ROCm run, with a raw-audio fallback retained. Exactly the right way to record a negative result. |
| `Qwen3-TTS-12Hz-1.7B-Base` / `-VoiceDesign` | pinned model IDs | **Reasonable.** Worth pinning a revision/commit hash too, so a Hub-side update cannot change voice cloning between runs. |
| Ollama model tag | `qwen3.8:27b` | **Verify + centralize ([M5](#m5--four-scattered-literal-defaults-for-one-model-tag-two-of-them-different)).** The tag is used consistently in the 2026-08/09 decision records, so it is presumably a real local tag — but `fallback_models: []` means a wrong tag is a hard failure, and one of the four in-code defaults disagrees with it. |
| Gemini tags | `gemini-3.5-flash{,-lite}` | **Verify ([M8](#m8--provider-model-names-are-unvalidated-and-duplicated-across-budget-keys)).** Duplicated across four config sites including budget keys, with no startup validation. |
| `electron` | `^35.0.0` | Caret on a major runtime, but [desktop/package-lock.json](desktop/package-lock.json) is committed, so builds are reproducible. Acceptable. |
| `playwright` | `>=1.54.0`, optional | **Good.** Correctly marked optional-unless-`browser.enabled`, matching the config gate. |
| Everything else | `>=` floors | Reasonable for a single-operator tool, given that [constraints-windows-rocm-tested.txt](voice/constraints-windows-rocm-tested.txt) records the known-good set. Wire that file into the documented install ([M7](#m7--qwen-tts-is-completely-unpinned)). |

**Model-selection reasoning: I agree with it.** Every choice above is accompanied by a recorded rationale, most of them citing a specific run. The refusal to expand Qwen 3.8 into metadata retrieval, TTS, mastering or export "solely because it is newer" is disciplined and correct. The `post_processing.enabled: false` decision — keeping output dry after the librosa phase-vocoder produced echo smearing in the 2026-08-09 E2E, documented in [docs/audio-echo-incident-2026-08-10.md](docs/audio-echo-incident-2026-08-10.md) — is precisely how a quality regression should be handled: disable, document, and require controlled listening tests before re-enabling.

The one framing I would push back on: the README says Qwen3-TTS Base "does not expose a natural-language per-utterance instruction parameter" and so the project applies "restrained pitch/tone post-processing for emotion cues." With `post_processing.enabled: false`, that post-processing is **off**. So emotion is currently conveyed by speed and by the reference voice alone. The README's honesty about not claiming native clone-mode emotion control is admirable, but it should also state that the compensating mechanism is presently disabled.

---

## 7. Assessment of the documented reasoning and history

The user asked specifically whether I agree with the thought patterns. Largely yes, and the pattern is consistent enough to name.

**What is working well:**

1. **Incidents produce durable artifacts.** Echo smearing, voice review, speaker attribution and the listening-QA gate each have a dated document. Each names a mechanism, not just a symptom.
2. **Variables get isolated before conclusions.** The 2026-08-23 attribution decision splits findings by model (45 vs 93) to prove the model was not the cause. That is real experimental hygiene, and rare.
3. **Negative results are recorded.** `vad_filter: false`, `post_processing.enabled: false`, `dialogue_focused_schema: false`, and "automatic per-segment loudness leveling is deliberately deferred" all record things that were tried and rejected, with reasons. This is the highest-value documentation in the repo.
4. **Priority ordering is explicit and consistently applied.** "Preserve source fidelity → preserve evidence and human intervention → improve speed only when the first two are equivalent or better." The `joint_analysis: false` decision cost throughput and was taken anyway. The stated priority is the operative one.
5. **Speculative work is gated, not deleted.** `dialogue_focused_schema`, `adaptive_max_new_tokens` and `joint_analysis` all sit behind flags marked experimental with promotion criteria. Correct.
6. **The repo distinguishes specification from history.** README states plainly that `implementation_plan*.md` and chat dumps "are historical records, not current specifications." Given that `Testing The Audiobook Pipeline 2.md` (removed from the repository on 2026-09-04) is a 263KB transcript containing recommendations (`Qwen2.5:32B` as production default) that were later superseded, that disclaimer is essential.

**Where the pattern breaks down:**

1. **Decision records lack lifecycle metadata ([L7](#l7--documentation-drift)).** Superseded conclusions read as current. This is the single fix I would prioritize in the docs, because it is what makes the corpus trustworthy rather than merely voluminous.
2. **Security is documented more precisely than it is implemented ([H3](#h3--the-documented-lan-trust-boundary-is-not-wired-the-bind-guard-is-bypassed-by-every-real-launch-path), [M12](#m12--the-voice-service-token-comparison-is-not-constant-time-and-its-lan-config-is-fiction)).** `dashboard.trusted_lan_cidrs` and `voice_server.trusted_lan_cidrs` both describe controls that do not exist. The specificity makes them more dangerous than vagueness would be — an operator reads "constrain unauthenticated LAN access" and reasonably believes they have.
3. **The audit culture is strong but does not close its own loops.** [docs/plans/unattended-full-app-audit-2026-08-23.md](docs/plans/unattended-full-app-audit-2026-08-23.md) explicitly scopes "duplicated logic" and "path safety" — yet three duplicate methods and ~28 CWD-relative paths remained. Thorough manual audits repeated by hand will keep missing what a linter catches for free on every commit. That is the structural argument for [P0](#p0--install-the-guardrail-that-would-have-caught-h2).
4. **Verification claims outpace verification mechanisms.** The "Verification Protocol" in [docs/architecture.md](docs/architecture.md) mandates running unit discovery after schema or stage changes, but nothing enforces it, and CI omits the voice requirements entirely ([M10](#m10--ci-does-not-install-the-voice-requirements-or-run-any-static-analysis)).

The `.agents/AGENTS.md` note about uvicorn module caching — that editing files on disk does not update a running server, so verification must either restart uvicorn or run `python.exe` directly with `PYTHONPATH=.` — is a genuinely useful piece of operational knowledge that most projects learn the hard way and never write down.

---

## 8. Improvement plan

Ordered so each phase reduces the cost of the next. Phase 0 first, deliberately: it is what stops these findings from recurring, and it would have caught the worst one.

### P0 — Install the guardrail that would have caught H2

*Effort: hours. Highest leverage item in this document.*

1. Add `pyproject.toml` with `ruff` configured for Python 3.12. Enable at minimum:
   - `F` (Pyflakes) — **`F811` catches [H2](#h2--three-duplicate-method-definitions-in-scriptgenerator-one-with-divergent-semantics) directly**, plus the dead `selection` assignment in [L8](#l8--god-objects)
   - `E`, `W`, `I` (isort), `UP` (pyupgrade — catches the `utcnow` in [L1](#l1--datetimeutcnow-is-deprecated))
   - `S` (bandit) — flags the `!=` token compare in [M12](#m12--the-voice-service-token-comparison-is-not-constant-time-and-its-lan-config-is-fiction) and the untimed subprocess calls in [M2](#m2--unbounded-subprocessrun-inside-the-gpu-lock)
   - `BLE001`, `S110` — surfaces the 74 silent handlers in [M14](#m14--128-except-exception-handlers-74-immediately-pass)

   Start with `--exit-non-zero-on-fix` off and a baseline ignore list so adoption is not blocked; ratchet down over time.
2. Fix CI ([M10](#m10--ci-does-not-install-the-voice-requirements-or-run-any-static-analysis)): install `-r voice/requirements.txt` instead of hand-copying four packages; add `ruff check` and `ruff format --check`; extend `compileall` to the repo root; add `pip-audit`.
3. Add `.pre-commit-config.yaml` running `ruff` and a whitespace check.
4. Optional but valuable: `mypy` in non-strict mode over [shared/](shared/) only. It is the most-imported package and already fully annotated, so the cost is low and it pins the data contracts.

### P1 — Correctness and security

*Effort: 1-2 days.*

1. **[H1](#h1--post-unload-blocks-the-voice-service-event-loop-its-busy-guard-is-unreachable)** — make `/unload` a sync `def` with non-blocking lock acquisition; add a regression test asserting `/health` and `/cancel` stay responsive during a simulated long `gpu_job()`. **Fix together with item 5 ([M2](#m2--unbounded-subprocessrun-inside-the-gpu-lock))** — the untimed subprocess calls are what make H1 reachable in practice.
2. **[H2](#h2--three-duplicate-method-definitions-in-scriptgenerator-one-with-divergent-semantics)** — delete the dead definitions at lines 3922-3956 and 2206-2238. Document the `"narrator"` sentinel on the survivor. Add a test asserting a genuinely ambiguous turn yields `"narrator"` *and* sets `attribution_review_required`, so the review-gate contract is pinned independently of the resolver's internals.
3. **[H3](#h3--the-documented-lan-trust-boundary-is-not-wired-the-bind-guard-is-bypassed-by-every-real-launch-path)** — wire `dashboard.trusted_lan_cidrs`, validate the CIDRs at startup, move the bind guard to import/lifespan time, and require a token for the proxy peer. Add tests for: a peer inside the configured CIDR (allow), a peer inside the *default* but outside the configured CIDR (**deny** — this is the regression that matters), and a public peer with and without a token.
4. **[M1](#m1--atomic-writes-silently-degrade-to-non-atomic-copies)** — replace the `copyfile` fallback with bounded `os.replace` retries.
5. **[M2](#m2--unbounded-subprocessrun-inside-the-gpu-lock)** — add timeouts to all six production subprocess sites; consolidate ffmpeg resolution.
6. **[M11](#m11--cors--allows-cross-origin-reads-from-any-lan-users-browser)**, **[M12](#m12--the-voice-service-token-comparison-is-not-constant-time-and-its-lan-config-is-fiction)** — narrow CORS; `compare_digest`; voice token env fallback; delete the fictional `trusted_lan_cidrs` comment.
7. **[M4](#m4--repetition-loop-detection-is-accidentally-coupled-to-logging-cadence)** — decouple repetition detection from logging cadence.

### P2 — Structural consistency

*Effort: 2-4 days. Mechanical, low-risk, high maintainability return.*

1. **[M13](#m13--roughly-28-cwd-relative-hardcoded-paths-voiceconfigyaml-re-read-at-six-sites)** — add `shared/paths.py` and a process-cached config accessor; replace all ~28 CWD-relative literals; load `voice/config.yaml` once.
2. **[M5](#m5--four-scattered-literal-defaults-for-one-model-tag-two-of-them-different)** — one `DEFAULT_OLLAMA_MODEL` constant; remove the divergent `qwen3:32b`.
3. **[M6](#m6--a-second-weaker-ollama-client-path-that-pause-cannot-interrupt)** — route pronunciation resolution through `OllamaClient`; drop `requests`.
4. **[M7](#m7--qwen-tts-is-completely-unpinned)** — pin `qwen-tts`; wire `constraints-windows-rocm-tested.txt` into the documented install; record resolved versions in performance metrics.
5. **[M8](#m8--provider-model-names-are-unvalidated-and-duplicated-across-budget-keys)** — declare each Gemini model once; validate model↔budget consistency at startup; distinguish model-not-found from transport failure so the circuit breaker cannot mask a typo.
6. **[M3](#m3--chapter-generation-takeover-does-not-wait-for-the-displaced-worker)** — bounded acknowledgement wait on chapter takeover, with a progress event.
7. **[L9](#l9--_raise_if_cancelled-raises-keyboardinterrupt)** — shared `GenerationCancelled(BaseException)`.

### P3 — Decomposition and real test coverage

*Effort: 1-2 weeks, incremental. Do this only after P0, which makes it safe.*

1. Split [main.py](brain/dashboard/api/main.py) (5,752 lines) into routers by domain: projects, script, voices, quality, delivery, runtime, metadata. Mechanical and independently testable.
2. Extract from [script_generator.py](brain/director/script_generator.py) (4,589 lines) the cohesive units that already exist as static-method clusters: fragment splitting, dialogue-tag evidence, attribution resolution, utterance grouping, compact-metadata inflation. The attribution logic in particular deserves its own module with its own focused tests — it is the highest-risk code in the repo and currently the hardest to isolate.
3. Decompose `_run_exclusive` into explicit per-stage functions with a single state-transition table, removing the stale-`current_stage` asymmetry noted in [L8](#l8--god-objects).
4. **[M9](#m9--the-frontend-tests-assert-on-source-text-not-behavior)** — rename the string-presence tests to `test_*_source_contains_*`; add real DOM tests for modal focus trap, tab keyboard navigation, and review-gate banner rendering.
5. Extract one shared `escapeHtml` ([L6](#l6--four-independent-html-escape-implementations)); consider ES modules for the two 3k-line frontend files.

### P4 — Hygiene and documentation

*Effort: hours.*

1. Delete [temp.py](temp.py) ([L4](#l4--temppy-is-a-utf-16-mojibake-duplicate-at-the-repo-root)); clean [.gitignore](.gitignore) ([L5](#l5--gitignore-lists-itself-twice)); remove or relocate [scratch/](scratch/).
2. Add `Status:` / `Superseded-by:` headers to every file in [docs/decisions/](docs/decisions/) — **start with [2026-08-quality-resilience-review.md](docs/decisions/2026-08-quality-resilience-review.md)**, and add a dated index. Highest-value doc fix.
3. Correct README's test count; note that the Android app lives in a separate repo; state that emotion post-processing is currently disabled; align the `max_unique_voices` description with the shipped default ([L7](#l7--documentation-drift)).
4. Fix [L1](#l1--datetimeutcnow-is-deprecated), [L2](#l2--atomic_write_jsondefaultstr-silently-coerces), [L3](#l3--empty_response_debugtxt-written-to-the-cwd), [L10](#l10--zip-symlink-members-are-not-checked); module-scope the `notifier` import.

---

## 9. Deliberately not recommended

Things I considered and rejected, to save re-litigating them:

- **Don't rewrite the pipeline as async.** The sync-`def`-in-threadpool pattern is correct here: GPU work is CPU/IO-blocking, and `workers: 1` is mandatory for a single GPU. [H1](#h1--post-unload-blocks-the-voice-service-event-loop-its-busy-guard-is-unreachable) is a bug in one handler that wrongly used `async def`, not evidence against the model.
- **Don't replace SQLite.** WAL plus `BEGIN IMMEDIATE` plus a 30s busy timeout is entirely adequate for one operator and one worker. A server database would add operational surface for no gain.
- **Don't add a frontend framework.** Vanilla JS with no build step is a legitimate choice for a local dashboard, and it keeps the deployment story trivial. Fix the duplication and add real tests; keep the architecture.
- **Don't consolidate the two YAML configs.** The brain/voice split mirrors the process and GPU-ownership boundary. Keeping them separate is correct — the problem is that each is read from too many places ([M13](#m13--roughly-28-cwd-relative-hardcoded-paths-voiceconfigyaml-re-read-at-six-sites)), not that there are two.
- **Don't re-enable `post_processing` or per-segment loudness leveling without listening A/B.** The existing decisions to defer both are correct and should be respected. Automatic gain changes that alter performance nuance need human ears, exactly as [docs/architecture.md](docs/architecture.md) states.
- **Don't loosen the fail-closed validation gates for throughput.** The `accepted_with_warning` tier already provides the right escape valve without weakening hard gates.
- **Don't expand Qwen 3.8 into further stages** absent the task-suitability and quality-preserving-fallback evidence the project's own audit plan requires. That constraint is well-reasoned; keep it.

---

## 10. Generation quality and performance opportunities

This section is scoped to things **not already closed** by [docs/performance-improvement-plan-post-release-2026-08-11.md](docs/performance-improvement-plan-post-release-2026-08-11.md) or [docs/benchmarks/scripting-speed-experiments-2026-08-23.md](docs/benchmarks/scripting-speed-experiments-2026-08-23.md). Those two documents have already screened and rejected SDPA alternatives, bfloat16, adaptive token caps, larger scripting chunks, larger same-speaker groups, TTS/Whisper co-residency during synthesis, and Whisper VAD. I am not re-proposing any of them.

One framing observation first. Every closed performance decision concerns either **TTS runtime** or **prompt shape** (chunk size, rows per request, schema, thinking mode). The **Ollama server's own runtime configuration** has never been screened — and scripting is 8,711s of a ~17,900s book, essentially tied with TTS at 8,083s. That is where the largest unexamined lever sits.

### 10.1 Performance — `prompt_eval` is 33% of each scripting request, at an anomalously low prefill rate

**This is the highest-value unexplored finding in the review.** It comes from your own benchmark artifact, [docs/benchmarks/candidate-qwen38-27b-8k-nonthinking-f40-ch07-offset0-2026-08-23.json](docs/benchmarks/candidate-qwen38-27b-8k-nonthinking-f40-ch07-offset0-2026-08-23.json):

```
prompt_eval_count        = 3,635 tokens
prompt_eval_duration_ns  = 25,409,571,000   →  25.41 s
eval_count               = 1,527 tokens
load_duration_ns         = 45,240,959,300   →  45.24 s
elapsed_seconds          = 121.97
```

Decomposed:

| Substage | Time | Rate |
|---|---:|---:|
| Model load (cold) | 45.24 s | — |
| **Prompt evaluation** | **25.41 s** | **143 tok/s prefill** |
| Decode | 51.32 s | 29.8 tok/s |
| Working time (excl. load) | 76.73 s | — |

**Prompt evaluation is 33.1% of working time**, and 143 tok/s prefill on a 27B model on an RX 7900 XTX is far below what that hardware should do. Prefill is a batched matmul — it is normally *much* faster than the 29.8 tok/s decode, often by an order of magnitude. Here it is only 4.8x decode speed, which points at a runtime configuration problem rather than a model limit.

Two independent causes are likely, and both are one-line changes:

**(a) `OLLAMA_NUM_PARALLEL` is never set.** [`_start_ollama_server`](brain/orchestrator/pipeline.py#L369-L383) sets `OLLAMA_HOST`, `GGML_VK_VISIBLE_DEVICES`, `OLLAMA_FLASH_ATTENTION`, `OLLAMA_MODELS` and `OLLAMA_DEBUG` — but not `OLLAMA_NUM_PARALLEL`. Ollama then auto-selects (commonly 4). That has two costs:

- **KV cache is allocated per slot.** With `num_ctx: 16384` and 4 slots, llama.cpp reserves up to 65,536 tokens of KV cache. On a 27B model that is a large VRAM commitment which can silently force layers off the GPU *despite* `"num_gpu": 99`, degrading both prefill and decode. The observed 29.8 tok/s decode is consistent with partial offload.
- **Prefix-cache reuse is defeated.** Sequential chunk requests can land on different slots, so the shared prefix must be re-evaluated from scratch each time.

  That second point matters a lot here, because your prompt is *already structured correctly for prefix reuse*: [script_generator.py:1043-1062](brain/director/script_generator.py#L1043-L1062) builds a chapter-scoped `system_prompt` (registry, previous summary, chapter number/title) that is **byte-identical across every chunk in a chapter**, and varies only the user message. That is exactly the layout prefix caching needs — the mechanism is in place and may simply not be getting a stable slot.

**(b) Ollama is running on the Vulkan backend.** `GGML_VK_VISIBLE_DEVICES` selects Vulkan. On RDNA3, llama.cpp's ROCm/hipBLAS backend is typically substantially faster than Vulkan for *prompt processing* specifically, and `OLLAMA_FLASH_ATTENTION=1` is largely a CUDA/ROCm feature — under Vulkan it may be silently inactive, so you may believe flash attention is on when it is not. Your stated reason for Vulkan is device isolation ("avoids Ollama splitting the 32B model across the discrete and integrated GPUs" — [README.md:63](README.md#L63)), and that goal does **not** require Vulkan: `HIP_VISIBLE_DEVICES=0` / `ROCR_VISIBLE_DEVICES=0` achieves the same isolation on ROCm.

**Size of the prize.** At ~25s of prompt eval per chunk request, and taking the static prefix as roughly 60% of the 3,635 tokens:

| Chunks/book | Total prompt_eval | Recoverable | % of scripting |
|---:|---:|---:|---:|
| 60 | 1,525 s | ~915 s | 10.5% |
| 83 | 2,109 s | ~1,265 s | 14.5% |
| 120 | 3,049 s | ~1,829 s | 21.0% |

For comparison, your own plan notes that "a genuine 10% TTS improvement would save about 808 seconds." **Fixing prefill and prefix reuse plausibly beats every remaining TTS candidate**, and unlike them it carries *zero* quality risk — the tokens evaluated are identical either way, so no output changes and no fingerprint invalidation.

**Recommended experiment** (uses the existing [benchmark_script_chunks.py](scripts/benchmark_script_chunks.py) harness, so it inherits your ABBA ordering and invariant gates):

1. **Measure first, free.** Run one chapter and read `prompt_eval_count` per request from the existing telemetry. If chunk 2+ within a chapter reports ~3,635 tokens rather than a small delta, prefix caching is not working and (a) is confirmed. This costs one chapter and no code.
2. Screen `OLLAMA_NUM_PARALLEL=1`. Expect lower `prompt_eval_count` on chunks 2+ and possibly better decode from reclaimed VRAM.
3. Screen ROCm vs Vulkan as a separate one-factor change. Primary metric: prefill tok/s from `prompt_eval_duration_ns`.
4. Screen `OLLAMA_KV_CACHE_TYPE=q8_0` (with flash attention active) to cut KV VRAM and secure full GPU offload at 16k context.

Add all four env vars to config with documented defaults so they are auditable rather than inherited from Ollama's autodetection.

### 10.2 Performance — LLM telemetry is collected but never aggregated

[`OllamaClient.last_generation_metrics`](brain/director/ollama_client.py#L313) captures `prompt_eval_count`, `eval_count`, `prompt_eval_duration_ns`, `eval_duration_ns` and `load_duration_ns`, and [script_generator.py:1659](brain/director/script_generator.py#L1659) forwards them into call metrics. But [`summarize_metrics`](shared/performance.py#L187-L242) aggregates only `pass1_seconds`, `pass2_seconds`, `reconciliation_seconds`, `total_seconds`, `chapters` and `segments` for scripting — **no token counts, no prefill/decode split, no cache indication.** TTS got a full substage breakdown with p50/p90/p95; the LLM half, which is 48.5% of book wall time, got wall-clock totals only.

The consequence is that [10.1](#101--performance--prompt_eval-is-33-of-each-scripting-request-at-an-anomalously-low-prefill-rate) was invisible for a month despite the data sitting in the artifacts.

**Fix:** extend `summarize_metrics` with a `script_director_llm` block reporting, per run: total prompt tokens, total generated tokens, prefill tok/s, decode tok/s, model-load time, and — the key derived metric — **mean `prompt_eval_count` for the first chunk of a chapter vs. subsequent chunks.** That ratio is a direct, continuous prefix-cache health indicator. Surface it in the dashboard's Runtime panel alongside the TTS percentiles.

### 10.3 Quality — production TTS sampling is unseeded, so audio is not reproducible

`torch.manual_seed` appears **only** in [scripts/benchmark_tts_fixture.py:270](scripts/benchmark_tts_fixture.py#L270). The production path ([`generate_speech`](voice/tts_server/qwen3_engine.py#L259) → [`_generate`](voice/tts_server/qwen3_engine.py#L442) → `generate_voice_clone`) never sets a seed, and runs with `do_sample: true`, `temperature: 0.9`, `top_p: 1.0`.

So every synthesis is an independent, non-reproducible draw. Three consequences, in increasing order of importance:

1. **The fingerprint cache is honest about *when* to regenerate but not about *what* regeneration produces.** "Same fingerprint ⇒ same audio" holds only because the WAV is cached, not because generation is deterministic. [`_purge_project_cache`](brain/dashboard/api/main.py#L969) exists, and after a purge the same script yields a *different* audiobook. That quietly weakens the artifact model described in [docs/architecture.md](docs/architecture.md).
2. **Repaired lines land as fresh high-temperature samples among older neighbours.** This is precisely the workflow in [docs/tiered-attribution-and-audio-regeneration-2026-09-03.md](docs/tiered-attribution-and-audio-regeneration-2026-09-03.md): fix an attribution, regenerate that line. The new take has independently sampled prosody, pace and energy, sitting between two lines generated under a different draw — a plausible source of audible seams that no current gate would catch. WER and speaker similarity both pass on a take that simply does not *match* its neighbours.
3. **Your benchmarks use paired seeds — correctly — so they measure a determinism condition production does not have.** That is the right protocol for a benchmark, but it means benchmark variance is systematically lower than production variance.

**Fix, and it is cheap:** derive a deterministic seed from the line's existing generation fingerprint.

```python
# in _generate, before generate_voice_clone
seed = int(line_fingerprint[:16], 16) % (2**63 - 1)
torch.manual_seed(seed)
```

Properties: identical line + identical settings ⇒ byte-identical audio forever; different lines still get different seeds so no loss of variety; regeneration of a repaired line is reproducible; a cache purge becomes safe; production A/B listening tests become repeatable; and the cost is zero. Record the seed in the segment manifest so a take is reproducible from the artifact alone.

This is the single highest-value quality change I can identify, because it converts an existing invariant from "true by caching" to "true by construction."

### 10.4 Quality — `top_p: 1.0` leaves the sampling tail unconstrained

With `temperature: 0.9` and `top_p: 1.0`, only `top_k: 50` trims the distribution. Nucleus filtering is fully disabled. Low-probability tail tokens are a common source of TTS artifacts — mispronunciations, glitches, dropped or doubled words — and your `wer_threshold: 0.20` will catch gross failures but not a subtly odd delivery that still transcribes correctly.

Current quality is genuinely good (average WER 0.0 and speaker similarity 0.987 in the dtype screen; only 4 of 605 lines retried), so this is not a defect report. It is an available variance-reduction lever that your existing metrics cannot see, because **nothing measures cross-line prosodic consistency.** You measure per-segment WER, clipping, duration, pitch CV, dynamic range, and speaker similarity — all *within* a segment — plus `cross_chapter_voice_drift` in [quality_trends.py](brain/orchestrator/quality_trends.py#L107) for identity drift. There is no *within-chapter, between-line* consistency metric.

**Suggested order of work:**

1. Do [10.3](#103--quality--production-tts-sampling-is-unseeded-so-audio-is-not-reproducible) first. Without seeding, any sampling A/B is confounded by draw variance.
2. Add a within-chapter consistency diagnostic (warning-only, in the spirit of the existing soft tier): per-speaker standard deviation of `pitch_median` and of speaking rate (characters per audio second) across a chapter's accepted lines, plus the max line-to-line delta. This is computed entirely from data you already store in `QualityResult`, needs no model, and would give you a metric to *evaluate* a sampling change against.
3. Only then screen `top_p: 0.9` against the current setting, using the existing fixture harness with the new consistency metric as the primary gate and WER/similarity as guardrails.

Note that any sampling change invalidates the generation fingerprint and forces full re-synthesis, so it belongs with a deliberate release, not a patch.

### 10.5 Performance — `generate_speech_batch` is dead code and does not batch

[voice/tts_server/qwen3_engine.py:412-440](voice/tts_server/qwen3_engine.py#L412-L440) is never called from anywhere in the repo (only from itself). Its docstring is candid — "batch mocked" — and it loops `generate_speech` sequentially.

Your plan already lists "Native Qwen batching | Unavailable in current wrapper | Revisit only after dependency support changes," so I am not claiming this is available today. Three concrete notes to make that revisit cheaper:

1. **There is a signal worth a ten-minute check.** [`_generate`](voice/tts_server/qwen3_engine.py#L505) calls `wavs, _ = self._model.generate_voice_clone(text=text, ...)` and then takes [`wavs[0]`](voice/tts_server/qwen3_engine.py#L518). A function returning a *list* of waveforms for a single text is often one that accepts a list of texts. Inspect the installed signature before assuming it is blocked.
2. **`qwen-tts` is unpinned ([M7](#m7--qwen-tts-is-completely-unpinned)), so it is already floating.** The dependency-blocked verdict was recorded on 2026-08-11 against whatever resolved then. The revisit trigger has plausibly already fired without anyone noticing — which is another argument for pinning it and reviewing the pin deliberately.
3. **Name the specific capabilities to watch for**, rather than "batching" generically: (i) batched `generate_voice_clone` accepting `list[str]`; (ii) `past_key_values` reuse, so a voice's reference prefix is prefilled once per voice instead of once per line — the TTS analogue of [10.1](#101--performance--prompt_eval-is-33-of-each-scripting-request-at-an-anomalously-low-prefill-rate), and given 605 calls per book with the narrator dominating, likely the larger of the two; (iii) `_supports_static_cache` flipping to `True`, which unblocks the `torch.compile` path you already investigated.

Batching would also directly attack your RTF tail (p50 1.245 vs p95 4.469): short dialogue lines are batch-size-1 decodes where the GPU is memory-bandwidth-bound on weight loading rather than compute-bound, which is exactly what a p95 of 4.5x audio duration looks like.

**In the meantime:** delete `generate_speech_batch` or mark it clearly as an unimplemented placeholder. As written it will pass a code search for "batching support" and mislead.

### 10.6 Confirmed correct — things I checked and would not change

Recorded so these are not re-examined:

- **Whisper residency is correctly implemented and does not contradict the closed decision.** `keep_tts_and_whisper_resident: true` looked at first like a conflict with "keep TTS and Whisper sequential during initial chapter synthesis." It is not. [`process_chapter`](voice/validator/validation_loop.py#L88) is genuinely two-phase: synthesis runs at [line 340](voice/validator/validation_loop.py#L340) before `whisper.load()` at [line 449](voice/validator/validation_loop.py#L449), the flag only suppresses unload/reload thrash *between validation retry sub-cycles*, and [line 699](voice/validator/validation_loop.py#L699) unloads Whisper unconditionally at the chapter boundary. Matches the documentation exactly.
- **Cross-chapter voice drift is already covered**, and covered thoughtfully — [quality_trends.py:107](brain/orchestrator/quality_trends.py#L107) explicitly refuses to treat pitch alone as identity drift and requires a speaker-similarity signal to escalate. Correct.
- **`risk_aware_first_attempt` is well-scoped** — [`_initial_delivery`](voice/validator/validation_loop.py#L864) narrowly targets ≤3-word emphatic lines (all-caps or `!`), flattening to plain text at speed 1.0. Tightly bounded, exactly as a listening-approved special case should be.
- **The chapter-integrated loudness / single-timing-owner / grouped-utterance design is right.** Taking the larger of two adjacent pauses rather than summing, applying crossfades only across genuinely adjacent audio, and having `utterance_group_id` force a zero-pause no-crossfade boundary is the correct model for quote-plus-tag.
- **`num_predict` + client-side token cap + wall-clock cap + repetition detection** is the right defensive stack for structured JSON generation from a local model.

### 10.7 Priority ordering for this section

| # | Item | Type | Effort | Expected value |
|---|---|---|---|---|
| 1 | Measure `prompt_eval_count` per chunk ([10.1](#101--performance--prompt_eval-is-33-of-each-scripting-request-at-an-anomalously-low-prefill-rate) step 1) | Perf | ~1 chapter, no code | Confirms or kills a ~10-21% scripting win |
| 2 | Deterministic per-line TTS seed ([10.3](#103--quality--production-tts-sampling-is-unseeded-so-audio-is-not-reproducible)) | Quality | Hours | Reproducible audio; safe cache purge; seam-free repairs |
| 3 | `OLLAMA_NUM_PARALLEL=1` screen ([10.1](#101--performance--prompt_eval-is-33-of-each-scripting-request-at-an-anomalously-low-prefill-rate)b) | Perf | One screen | Prefix reuse + reclaimed VRAM |
| 4 | ROCm vs Vulkan backend screen ([10.1](#101--performance--prompt_eval-is-33-of-each-scripting-request-at-an-anomalously-low-prefill-rate)b) | Perf | One screen | Targets the 143 tok/s prefill directly |
| 5 | Aggregate LLM telemetry ([10.2](#102--performance--llm-telemetry-is-collected-but-never-aggregated)) | Observability | Hours | Makes 1/3/4 continuously visible |
| 6 | Within-chapter consistency diagnostic ([10.4](#104--quality--top_p-10-leaves-the-sampling-tail-unconstrained)) | Quality | 1 day | Creates the metric a sampling change needs |
| 7 | Re-check `qwen-tts` capabilities; delete dead batch method ([10.5](#105--performance--generate_speech_batch-is-dead-code-and-does-not-batch)) | Perf | Hours to check | Unblocks the largest theoretical TTS win |
| 8 | `top_p: 0.9` screen ([10.4](#104--quality--top_p-10-leaves-the-sampling-tail-unconstrained)) | Quality | One screen, after 2 and 6 | Variance reduction; needs full re-synthesis |

Items 1-5 carry **no audio-output change** and therefore no fingerprint invalidation, which makes them safe to land incrementally. Items 6-8 change output and belong to a deliberate release.

---

## 11. Bottom line

This is a **well-architected system with a discipline gap, not a design gap.** The hard problems — source fidelity, resumability, attribution confidence, GPU serialization, incremental delivery, fail-closed validation — are solved thoughtfully and documented with real evidence. I would not change the architecture.

What it needs is the boring infrastructure that a 72k-line codebase requires to keep those good decisions from eroding: a linter, honest CI, one path/config resolution layer, and decision records that say when they stopped being true. The three duplicate methods in the script director are the tell — not because they are currently breaking anything, but because they sat in the most safety-critical file in the repo through multiple thorough manual audits, and a default `ruff` run would have caught them in under a second.

Fix P0 first. It is a few hours of work, and it is the difference between finding the next [H2](#h2--three-duplicate-method-definitions-in-scriptgenerator-one-with-divergent-semantics) automatically and finding it in a re-read a year from now.

The security items in [H3](#h3--the-documented-lan-trust-boundary-is-not-wired-the-bind-guard-is-bypassed-by-every-real-launch-path) deserve attention sooner than their "personal tool" framing suggests, because the dashboard is proxied to the public internet and the app-level control the documentation credits for protecting it is not actually running.
