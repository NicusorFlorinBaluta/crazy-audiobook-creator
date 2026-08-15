# CodeRabbit Review Comments Audit for PR #5 (Total: 30)

## Comment #1 (ID: 3635745767, Reply to: None)
**File:** `brain/dashboard/api/main.py` (Line: 257) | **Commit:** `ad2dc49`

```markdown
_🩺 Stability & Availability_ | _🟡 Minor_ | _⚡ Quick win_

**Store a reference to the background metadata-fetch task.**

`asyncio.create_task(...)` result isn't retained; the task can be garbage-collected before it completes since the event loop only holds a weak reference. Store it in a module-level set (and discard on completion) so the auto-fetch reliably runs to completion.




<details>
<summary>♻️ Proposed fix</summary>

```diff
+_background_tasks: set = set()
+
 ...
-        asyncio.create_task(asyncio.to_thread(_auto_fetch_metadata_sync, status.project_id))
+        task = asyncio.create_task(asyncio.to_thread(_auto_fetch_metadata_sync, status.project_id))
+        _background_tasks.add(task)
+        task.add_done_callback(_background_tasks.discard)
```
</details>

<!-- suggestion_start -->

<details>
<summary>📝 Committable suggestion</summary>

> ‼️ **IMPORTANT**
> Carefully review the code before committing. Ensure that it accurately replaces the highlighted code, contains no missing lines, and has no issues with indentation. Thoroughly test & benchmark the code to ensure it meets the requirements.

```suggestion
_background_tasks: set = set()

        logger.info("[DashboardAPI] Uploaded EPUB '%s' (%d bytes) for project creation", file.filename, len(content))

        status = pipeline.create_project(str(temp_path))
        logger.info("[DashboardAPI] Created project '%s' (%d chapters detected)", status.project_id, status.total_chapters)

        # Automatically fetch metadata and artwork in background
        task = asyncio.create_task(asyncio.to_thread(_auto_fetch_metadata_sync, status.project_id))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
```

</details>

<!-- suggestion_end -->

<details>
<summary>🧰 Tools</summary>

<details>
<summary>🪛 Ruff (0.15.21)</summary>

[warning] 257-257: Store a reference to the return value of `asyncio.create_task`

(RUF006)

</details>

</details>

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@brain/dashboard/api/main.py` around lines 251 - 257, Retain the task created
in the project-creation flow around _auto_fetch_metadata_sync by adding it to a
module-level set of background tasks, and register a completion callback that
discards it from that set. Keep the existing asynchronous metadata-fetch
behavior while ensuring the task reference is held until completion.
```

</details>

<!-- fingerprinting:phantom:medusa:beignet -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:0236d2ed97743642dcf43d9e -->

_Source: Linters/SAST tools_

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #2 (ID: 3635745782, Reply to: None)
**File:** `brain/orchestrator/pipeline.py` (Line: 628) | **Commit:** `ad2dc49`

```markdown
_🗄️ Data Integrity & Integration_ | _🔴 Critical_ | _🏗️ Heavy lift_

<details>
<summary>🧩 Analysis chain</summary>

🏁 Script executed:

```shell
#!/bin/bash
rg -n "def assemble|missing|FileNotFoundError|segments_dir" voice/mastering/assembler.py
```

Repository: NicusorFlorinBaluta/crazy-audiobook-creator

Length of output: 210

---

🏁 Script executed:

```shell
#!/bin/bash
set -euo pipefail

echo '== pipeline outline =='
ast-grep outline brain/orchestrator/pipeline.py --view expanded || true

echo '== assembler outline =='
ast-grep outline voice/mastering/assembler.py --view expanded || true

echo '== pipeline slice =='
sed -n '560,720p' brain/orchestrator/pipeline.py

echo '== assembler slice =='
sed -n '1,220p' voice/mastering/assembler.py
```

Repository: NicusorFlorinBaluta/crazy-audiobook-creator

Length of output: 14981

---

🏁 Script executed:

```shell
#!/bin/bash
set -euo pipefail

echo '== pipeline references =='
rg -n "chapter_script\.lines|merged_lines|line_id|segments/" brain/orchestrator/pipeline.py

echo '== assembler references =='
rg -n "line_id|segments|missing|FileNotFoundError|open\(" voice/mastering/assembler.py

echo '== pipeline around _run_mastering =='
python3 - <<'PY'
from pathlib import Path
p = Path('brain/orchestrator/pipeline.py')
text = p.read_text()
for needle in ['def _run_mastering', 'chapter_script.lines', 'merged_lines', 'segments/']:
    idx = text.find(needle)
    print(f'--- {needle} @ {idx} ---')
    if idx != -1:
        start = max(0, idx - 400)
        end = min(len(text), idx + 900)
        print(text[start:end])
PY
```

Repository: NicusorFlorinBaluta/crazy-audiobook-creator

Length of output: 6975

---



</details>

**Keep mastered segment files aligned with original `line_id`s.** `AudioAssembler.assemble_chapter()` logs a warning and skips missing files, so any merged-away line IDs will be omitted from the final chapter. Either write the merged audio under every original `line_id`, or make mastering consume the same merged groups it generated.

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@brain/orchestrator/pipeline.py` around lines 606 - 628, Update the
line-merging flow before GenerateChapterRequest and
AudioAssembler.assemble_chapter() so merged segments remain aligned with the
original line_id values: either preserve/write the generated audio for every
original line_id in each merged group, or pass the same merged groups into
mastering so it consumes the grouped IDs consistently. Ensure no merged-away
original line_id is omitted when assembling the final chapter.
```

</details>

<!-- fingerprinting:phantom:medusa:beignet -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:2c6801884f7a3267af2a1bfe -->

<!-- This is an auto-generated comment by CodeRabbit -->

✅ Addressed in commits a4fddb4 to 1fa56c3
```

---

## Comment #3 (ID: 3635745784, Reply to: None)
**File:** `brain/orchestrator/voice_client.py` (Line: 55) | **Commit:** `1fa56c3`

```markdown
_🩺 Stability & Availability_ | _🟠 Major_ | _⚡ Quick win_

**Use a short timeout for health checks.**

`wait_for_server(max_wait_seconds=120)` calls `health_check()` with the new 3600-second default. If the port accepts a connection but never responds, startup blocks for up to an hour instead of honoring its 120-second budget. Pass a small explicit timeout for `/health`.

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@brain/orchestrator/voice_client.py` around lines 44 - 53, Update
wait_for_server and its health_check invocation to use a small explicit timeout
for the /health request instead of the VoiceClient default timeout, ensuring an
unresponsive server cannot block startup beyond max_wait_seconds. Keep the
existing retry and overall wait-budget behavior unchanged.
```

</details>

<!-- fingerprinting:phantom:poseidon:terra -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:c7fa3f96eba9dba7df17f536 -->

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #4 (ID: 3635745786, Reply to: None)
**File:** `brain/orchestrator/watchdog.py` (Line: 159) | **Commit:** `ad2dc49`

```markdown
_🩺 Stability & Availability_ | _🔴 Critical_ | _⚡ Quick win_

<details>
<summary>🧩 Analysis chain</summary>

🏁 Script executed:

```shell
#!/bin/bash
rg -n "^import|^from" brain/orchestrator/watchdog.py
rg -n "Path" brain/orchestrator/watchdog.py
```

Repository: NicusorFlorinBaluta/crazy-audiobook-creator

Length of output: 497

---

🏁 Script executed:

```shell
#!/bin/bash
sed -n '1,40p' brain/orchestrator/watchdog.py
printf '\n---\n'
sed -n '136,165p' brain/orchestrator/watchdog.py
```

Repository: NicusorFlorinBaluta/crazy-audiobook-creator

Length of output: 2460

---



</details>

**Import `Path` here before the restart path runs.** `Path` is used in this method but never imported in `brain/orchestrator/watchdog.py`, so the watchdog will hit a `NameError` the first time it tries to relaunch the voice server. Add `from pathlib import Path` with the other imports.

<details>
<summary>🧰 Tools</summary>

<details>
<summary>🪛 ast-grep (0.44.1)</summary>

[error] 154-158: Command coming from incoming request
Context: subprocess.Popen(
            [str(venv_py), "-m", "voice.tts_server.main"],
            cwd=str(Path.cwd()),
            env=env,
        )
Note: [CWE-78] Improper Neutralization of Special Elements used in an OS Command ('OS Command Injection').

(subprocess-from-request)

</details>
<details>
<summary>🪛 Ruff (0.15.21)</summary>

[error] 146-146: Undefined name `Path`

(F821)

---

[error] 148-148: Undefined name `Path`

(F821)

---

[error] 153-153: Undefined name `Path`

(F821)

---

[error] 155-155: `subprocess` call: check for execution of untrusted input

(S603)

---

[error] 157-157: Undefined name `Path`

(F821)

</details>

</details>

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@brain/orchestrator/watchdog.py` around lines 144 - 159, Add the missing
pathlib Path import alongside the existing imports in the watchdog module so the
restart logic using Path in the voice server relaunch method executes without a
NameError.
```

</details>

<!-- fingerprinting:phantom:medusa:beignet -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:b24bd6b0b1006dd37c35661a -->

_Source: Linters/SAST tools_

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #5 (ID: 3635745791, Reply to: None)
**File:** `create_shortcut.ps1` (Line: 10) | **Commit:** `1fa56c3`

```markdown
_🎯 Functional Correctness_ | _🟡 Minor_ | _⚡ Quick win_

**Resolve the fallback interpreter to an absolute path.**

`pythonw.exe` is not guaranteed to be discoverable from Explorer’s environment, so this shortcut can fail when the fixed venv is absent. Resolve it with `Get-Command` and use its source path, or stop with a clear setup error.

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@create_shortcut.ps1` around lines 9 - 10, Update the fallback logic following
the PythonW path check to resolve pythonw.exe via Get-Command and assign its
absolute source path to PythonW; if no command is found, stop with a clear setup
error instead of retaining a bare executable name.
```

</details>

<!-- fingerprinting:phantom:poseidon:terra -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:1f213b92a4e4b5d9ac612c62 -->

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #6 (ID: 3635745794, Reply to: None)
**File:** `implementation_plan_db_improvements.md` (Line: 241) | **Commit:** `1fa56c3`

```markdown
_🎯 Functional Correctness_ | _🟡 Minor_ | _⚡ Quick win_

**Make the cache smoke test exercise the cache.**

Line 235 passes a nonexistent reference path, so `save_embedding()` returns without storing anything; `b"dummy"` is not a tensor either. Use a real temporary WAV and tensor, then assert that `get_embedding(...)` returns a value.

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@implementation_plan_db_improvements.md` at line 235, Update the cache smoke
test command in implementation_plan_db_improvements.md to create and use a real
temporary WAV file and valid tensor data, ensuring save_embedding() stores the
entry instead of returning early. Add an assertion that get_embedding('test',
'narrator') returns a value after saving.
```

</details>

<!-- fingerprinting:phantom:poseidon:terra -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:bbdaacd1945b438f4ebbacbb -->

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #7 (ID: 3635745797, Reply to: None)
**File:** `parler_server.py` (Line: 130) | **Commit:** `1fa56c3`

```markdown
_🩺 Stability & Availability_ | _🟠 Major_ | _⚡ Quick win_

<details>
<summary>🧩 Analysis chain</summary>

🌐 Web query:

`parler-tts-large-v1 VRAM float32 memory requirement`

💡 Result:

The Parler-TTS Large v1 model, which contains approximately 2.2 billion to 2.3 billion parameters [1][2][3], has a significant VRAM requirement when loaded in full float32 precision. Based on technical benchmarks and resource estimations for this model, running Parler-TTS Large v1 in FP16 (half-precision) requires approximately 68GB of VRAM [4][5]. Since float32 precision typically doubles the memory footprint compared to float16, the theoretical VRAM requirement for the model weights alone in float32 is approximately 136GB. In practice, users generally avoid running this model in float32 because it consumes excessive VRAM without offering tangible improvements in audio quality for inference [6][4]. Most practitioners utilize float16 or bfloat16 for standard usage, or GGUF-based quantization (such as Q4 or Q8) if they are working with more limited hardware [4][5]. Key considerations: - Default Precision: While the model configuration may default to float32 [7], it is highly recommended to use torch_dtype=torch.float16 or torch_dtype=torch.bfloat16 during initialization to reduce VRAM requirements by 50% [6][8]. - Quantization: If you do not have access to massive VRAM capacities (e.g., 68GB+ for FP16), consider using quantized versions, which can reduce requirements significantly (e.g., ~17GB for Q4_K_M or ~34GB for Q8) [4][5].

Citations:

- 1: https://huggingface.co/parler-tts/parler-tts-large-v1
- 2: https://github.com/huggingface/parler-tts/
- 3: https://huggingface.co/parler-tts/parler-tts-large-v1/blob/main/README.md
- 4: https://localai.computer/models/parler-tts-parler-tts-large-v1
- 5: https://localai.computer/models/parler-tts-parler-tts-large-v1/requirements/q4
- 6: https://github.com/huggingface/parler-tts/issues/19
- 7: https://huggingface.co/parler-tts/parler-tts-large-v1/commit/0cd6f2f58dd2f0112459691116ed9c4898b9c66a
- 8: https://gist.github.com/eustlb/1201fbd66dfe552a9fe1fd21d51cb929

---

🏁 Script executed:

```shell
#!/bin/bash
set -euo pipefail

# Locate the file and inspect the relevant region with line numbers.
git ls-files | rg '^parler_server\.py$|/parler_server\.py$|^.*parler_server\.py$' || true
wc -l parler_server.py 2>/dev/null || true
cat -n parler_server.py | sed -n '1,220p'
```

Repository: NicusorFlorinBaluta/crazy-audiobook-creator

Length of output: 11135

---

🏁 Script executed:

```shell
#!/bin/bash
set -euo pipefail

# Find the relevant symbols and nearby logic.
rg -n "float32|float16|bfloat16|device|cuda|ParlerTTSForConditionalGeneration|from_pretrained" parler_server.py

echo
echo "---- relevant section ----"
sed -n '100,160p' parler_server.py | cat -n
```

Repository: NicusorFlorinBaluta/crazy-audiobook-creator

Length of output: 7318

---



</details>

**Load a lower-precision dtype on CUDA.** `torch.float32` is used unconditionally here, so the model is moved to GPU in full precision when available. On the documented 8GB RTX 2080 Super, that is likely to OOM; gate FP32 to CPU and keep float16/bfloat16 on CUDA.

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@parler_server.py` around lines 126 - 128, Update the model-loading setup
around model_name, dtype, and ParlerTTSForConditionalGeneration.from_pretrained
so dtype is torch.float32 only on CPU and uses an appropriate lower-precision
dtype, such as float16 or bfloat16, on CUDA. Preserve the existing device
placement and model-loading flow.
```

</details>

<!-- fingerprinting:phantom:medusa:capybara -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:f96fb55aa84ec3a343bf71af -->

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #8 (ID: 3635745808, Reply to: None)
**File:** `parler_server.py` (Line: 177) | **Commit:** `ad2dc49`

```markdown
_🎯 Functional Correctness_ | _🟠 Major_ | _⚡ Quick win_

<details>
<summary>🧩 Analysis chain</summary>

🌐 Web query:

`parler-tts generate input_ids prompt_input_ids description vs prompt`

💡 Result:

In the Parler-TTS framework, the generate function distinguishes between the descriptive conditioning of the voice and the actual text to be spoken [1][2]. The parameters input_ids and prompt_input_ids serve distinct roles: input_ids: These represent the tokenized description of the desired speech characteristics (e.g., gender, pitch, speaking rate, acoustic quality, and background noise) [2][3][4]. These IDs are processed to guide the model's style and prosody [4]. prompt_input_ids: These represent the tokenized text that the model should convert into speech [2][5]. This is the actual content or transcript you want the model to generate [2]. In practice, you tokenize the description text to create input_ids and the prompt text to create prompt_input_ids, both of which are then passed to the model.generate method [1][5][6]. Depending on the model version, you may use a single tokenizer for both inputs or separate tokenizers for the description and the prompt [5][6]. In newer multilingual or updated versions, it is common to use a specific tokenizer for the description (often accessed via the model config) and a standard tokenizer for the prompt text [5][6].

Citations:

- 1: https://github.com/huggingface/parler-tts/blob/main/INFERENCE.md
- 2: https://github.com/huggingface/parler-tts/blob/main/README.md
- 3: https://huggingface.co/parler-tts/parler-tts-large-v1
- 4: https://huggingface.co/parler-tts/parler-tts-mini-v1
- 5: https://huggingface.co/parler-tts/parler-tts-mini-multilingual
- 6: https://huggingface.co/parler-tts/parler-tts-mini-v1.1

---

🏁 Script executed:

```shell
#!/bin/bash
set -euo pipefail

# Inspect the relevant file and surrounding context
if [ -f parler_server.py ]; then
  wc -l parler_server.py
  sed -n '140,210p' parler_server.py
fi

# Find any other Parler-TTS generate calls or tokenizer usage in the repo
rg -n "prompt_input_ids|input_ids=.*prompt|generate\(" -S .
```

Repository: NicusorFlorinBaluta/crazy-audiobook-creator

Length of output: 4141

---

🏁 Script executed:

```shell
#!/bin/bash
set -euo pipefail

# Inspect the request schema and the endpoint around the bug
sed -n '1,120p' parler_server.py
printf '\n---\n'
sed -n '150,190p' parler_server.py

printf '\n=== voice-design docs/usages ===\n'
rg -n "voices/design|VoiceDesignRequest|request\.prompt|request\.text|voice_description|test sentence" parler_server.py docs README* -S
```

Repository: NicusorFlorinBaluta/crazy-audiobook-creator

Length of output: 10432

---



</details>

**`prompt_input_ids` should use `request.text`**
`input_ids` should stay on the voice description, but `prompt_input_ids` is the text to synthesize; right now both are tokenized from `request.prompt`, so `request.text` is ignored and the model will generate the description instead of the requested speech.

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@parler_server.py` around lines 171 - 177, Update the generation setup around
model.generate so input_ids continues using tokenized request.prompt, while
prompt_input_ids is tokenized from request.text and uses the corresponding text
attention mask. Ensure request.text drives the synthesized speech instead of the
voice description.
```

</details>

<!-- fingerprinting:phantom:medusa:capybara -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:bdc5161a1c7aaa5da060c595 -->

<!-- This is an auto-generated comment by CodeRabbit -->

✅ Addressed in commits a4fddb4 to 1fa56c3
```

---

## Comment #9 (ID: 3635745817, Reply to: None)
**File:** `parler_server.py` (Line: 206) | **Commit:** `1fa56c3`

```markdown
_🔒 Security & Privacy_ | _🟡 Minor_ | _⚡ Quick win_

**Avoid returning the full traceback in the HTTP response detail.**

Echoing `traceback.format_exc()` into the 500 `detail` leaks internal paths/stack info to clients. Log the traceback (as done) but return a generic message; also chain with `raise ... from e` (B904).





<details>
<summary>🛡️ Proposed change</summary>

```diff
     except Exception as e:
         import traceback
         tb = traceback.format_exc()
         logger.error(f"Error designing voice:\n{tb}")
-        raise HTTPException(status_code=500, detail=f"{e}\n{tb}")
+        raise HTTPException(status_code=500, detail="Voice design failed") from e
```
</details>

<!-- suggestion_start -->

<details>
<summary>📝 Committable suggestion</summary>

> ‼️ **IMPORTANT**
> Carefully review the code before committing. Ensure that it accurately replaces the highlighted code, contains no missing lines, and has no issues with indentation. Thoroughly test & benchmark the code to ensure it meets the requirements.

```suggestion
        import traceback
        tb = traceback.format_exc()
        logger.error(f"Error designing voice:\n{tb}")
        raise HTTPException(status_code=500, detail="Voice design failed") from e
```

</details>

<!-- suggestion_end -->

<details>
<summary>🧰 Tools</summary>

<details>
<summary>🪛 Ruff (0.15.21)</summary>

[warning] 191-191: Within an `except` clause, raise exceptions with `raise ... from err` or `raise ... from None` to distinguish them from errors in exception handling

(B904)

</details>

</details>

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@parler_server.py` around lines 188 - 191, Update the exception handling
around the voice-design error in the visible handler: keep logging the full
traceback via logger.error, but replace the HTTPException detail with a generic
client-safe message and raise it using explicit exception chaining from e.
```

</details>

<!-- fingerprinting:phantom:medusa:capybara -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:c71e80a65b8ce31a30297fe0 -->

_Source: Linters/SAST tools_

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #10 (ID: 3635745824, Reply to: None)
**File:** `scratch/fix_and_export_ch13.py` (Line: 80) | **Commit:** `1fa56c3`

```markdown
_🗄️ Data Integrity & Integration_ | _🟠 Major_ | _⚡ Quick win_

**Validate regenerated audio before mastering.**

`generate_speech()` is not followed by transcription/WER validation, so a newly defective segment is accepted and reported as clean before it is assembled. Re-run the same WER gate after generation and retry or fail the script when it remains above the threshold. `voice/validator/whisper_validator.py:100-170` provides the required contract.

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@scratch/fix_and_export_ch13.py` around lines 69 - 80, Update the regeneration
flow around generate_speech() to run the existing Whisper transcription/WER
validation contract from whisper_validator.py before incrementing fixed_count or
reporting success. Apply the same WER threshold used for defective-line
detection, and retry regeneration or fail the script when validation remains
above that threshold; only accept and count segments that pass.
```

</details>

<!-- fingerprinting:phantom:poseidon:terra -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:8b4e78fe3381b60f29c5ee16 -->

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #11 (ID: 3635745829, Reply to: None)
**File:** `scratch/fix_ch1_master_and_export.py` (Line: 125) | **Commit:** `1fa56c3`

```markdown
_🗄️ Data Integrity & Integration_ | _🟠 Major_ | _⚡ Quick win_

**Do not export chapters with silently omitted lines.**

This filter drops every missing segment. Only Chapter 1 is regenerated above, so Chapters 2 and 3 can be mastered and exported as truncated audio; a Chapter 1 validation failure has the same outcome. Generate missing segments for every requested chapter, or fail before assembly when any expected `line_id` is absent.

<details>
<summary>🧰 Tools</summary>

<details>
<summary>🪛 Ruff (0.15.21)</summary>

[error] 123-123: Ambiguous variable name: `l`

(E741)

</details>

</details>

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@scratch/fix_ch1_master_and_export.py` around lines 116 - 125, Update the
segment assembly flow that builds the MasterSegmentInfo list so missing expected
line_id files are never silently filtered out. Before mastering or exporting
each requested chapter, ensure every expected segment is generated for that
chapter, or fail immediately when any segment is absent, including when Chapter
1 validation fails; do not allow Chapters 2 or 3 to proceed with truncated
audio.
```

</details>

<!-- fingerprinting:phantom:poseidon:terra -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:3142931a45ed8c15ea2031e0 -->

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #12 (ID: 3635745837, Reply to: None)
**File:** `scratch/fix_ch1_master_and_export.py` (Line: 173) | **Commit:** `1fa56c3`

```markdown
_🗄️ Data Integrity & Integration_ | _🟠 Major_ | _⚡ Quick win_

**Derive pipeline completion state from verified artifacts.** Both scripts mark all three chapters generated and mastered regardless of missing, failed, or truncated audio; the dashboard returns this persisted state directly, so users can see a false completed state and resume logic may skip required work.
- `scratch/fix_ch1_master_and_export.py#L168-L173`: populate chapter lists only after every expected segment is present and the mastered/exported artifact succeeds.
- `scratch/update_db.py#L10-L17`: remove the unconditional completion mutation or derive it from the same verified chapter results.

<details>
<summary>🧰 Tools</summary>

<details>
<summary>🪛 ast-grep (0.44.1)</summary>

[info] 172-172: use jsonify instead of json.dumps for JSON output
Context: json.dumps(state)
Note: [CWE-116] Improper Encoding or Escaping of Output.

(use-jsonify)

</details>

</details>

<details>
<summary>📍 Affects 2 files</summary>

- `scratch/fix_ch1_master_and_export.py#L168-L173` (this comment)
- `scratch/update_db.py#L10-L17`

</details>

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@scratch/fix_ch1_master_and_export.py` around lines 168 - 173, Derive
completion state from verified chapter artifacts instead of unconditionally
marking all chapters complete: in scratch/fix_ch1_master_and_export.py lines
168-173, populate mastered_chapters and generated_chapters only after every
expected segment exists and mastering/export succeeds; in scratch/update_db.py
lines 10-17, remove the unconditional completion mutation or derive it from
those same verified results.
```

</details>

<!-- consolidated_sites_start -->
<!--
<consolidated_sites>
<site>
<role>anchor</role>
<file>scratch/fix_ch1_master_and_export.py</file>
<line_range>168-173</line_range>
</site>
<site>
<role>sibling</role>
<file>scratch/update_db.py</file>
<line_range>10-17</line_range>
</site>
</consolidated_sites>
-->
<!-- consolidated_sites_end -->

<!-- fingerprinting:phantom:poseidon:terra -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:9dcb8fd87b97a7cc892e386f -->

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #13 (ID: 3635745842, Reply to: None)
**File:** `scratch/repair_ch1_script.py` (Line: 41) | **Commit:** `1fa56c3`

```markdown
_🎯 Functional Correctness_ | _🟠 Major_ | _🏗️ Heavy lift_

**Do not assign all quoted dialogue to `starling`.**

This heuristic overwrites every fully quoted utterance with Starling’s voice, regardless of the speaker registry, and misses dialogue that includes attribution outside the quotes. Reuse the project’s speaker-attribution logic and `CharacterRegistry` rather than regenerating Chapter 1 with this hard-coded mapping.

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@scratch/repair_ch1_script.py` around lines 25 - 41, Replace the hard-coded
speaker assignment in the fragments loop with the project’s existing
speaker-attribution logic, using CharacterRegistry to resolve quoted dialogue
and attribution outside quotation marks. Preserve the default narrator behavior
when attribution is absent, and avoid assigning every fully quoted fragment to
starling.
```

</details>

<!-- fingerprinting:phantom:poseidon:terra -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:779765e16fa6418870571733 -->

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #14 (ID: 3635745849, Reply to: None)
**File:** `scratch/test_e2e_chapter1.py` (Line: 27) | **Commit:** `1fa56c3`

```markdown
_🩺 Stability & Availability_ | _🟠 Major_ | _⚡ Quick win_

**Bound all HTTP calls and the polling loop.**

A stalled local server or request can block this E2E test forever; repeated non-200 status responses also loop forever. Add per-request timeouts and an overall pipeline deadline that fails with the last observed state.  
   


Also applies to: 48-48, 60-60, 68-68, 77-77, 88-93, 123-123

<details>
<summary>🧰 Tools</summary>

<details>
<summary>🪛 Ruff (0.15.21)</summary>

[error] 27-27: Probable use of `requests` call without timeout

(S113)

</details>

</details>

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@scratch/test_e2e_chapter1.py` at line 27, Update the E2E workflow in
test_e2e_chapter1.py so every requests.get/post call, including the listed
project, polling, and pipeline requests, uses an explicit per-request timeout.
Bound the polling loop with an overall deadline, and when it expires or repeated
non-200 responses persist, fail the test while reporting the last observed
state.
```

</details>

<!-- fingerprinting:phantom:poseidon:terra -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:c553cb95fa480f6f7bf82a61 -->

_Source: Linters/SAST tools_

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #15 (ID: 3635745853, Reply to: None)
**File:** `scratch/test_e2e_chapter1.py` (Line: 117) | **Commit:** `1fa56c3`

```markdown
_🎯 Functional Correctness_ | _🟠 Major_ | _⚡ Quick win_

**Match the download endpoint’s output search behavior.**

The dashboard can serve an M4B from `workspace/{project_id}/output` (and other workspace locations), but this assertion only searches `brain/projects/{project_id}`. A valid completed export can therefore fail this E2E test before the download endpoint is checked. Search the same locations or validate the download response directly.

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@scratch/test_e2e_chapter1.py` around lines 112 - 117, Update the M4B
validation in the E2E flow around project_dir and m4b_files to match the
download endpoint’s workspace output search behavior, including
workspace/{project_id}/output and other supported workspace locations, or
validate the download response directly. Keep the failure assertion for
genuinely missing exports.
```

</details>

<!-- fingerprinting:phantom:poseidon:terra -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:3234e4e2fcdfbe52ef579af5 -->

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #16 (ID: 3635745859, Reply to: None)
**File:** `scratch/verify_m4b.py` (Line: 10) | **Commit:** `1fa56c3`

```markdown
_🩺 Stability & Availability_ | _🟡 Minor_ | _⚡ Quick win_

**Validate the input and `ffprobe` result before parsing.**

A missing M4B, unavailable `ffprobe`, or probe failure currently crashes at `stat()` or `json.loads()`. Check `m4b.is_file()` and run `ffprobe` with `check=True`/error reporting before decoding stdout.

<details>
<summary>🧰 Tools</summary>

<details>
<summary>🪛 ast-grep (0.44.1)</summary>

[error] 8-8: Command coming from incoming request
Context: subprocess.run(['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_chapters', str(m4b)], capture_output=True, text=True)
Note: [CWE-78] Improper Neutralization of Special Elements used in an OS Command ('OS Command Injection').

(subprocess-from-request)

</details>
<details>
<summary>🪛 Ruff (0.15.21)</summary>

[error] 9-9: `subprocess` call: check for execution of untrusted input

(S603)

---

[error] 9-9: Starting a process with a partial executable path

(S607)

</details>

</details>

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@scratch/verify_m4b.py` around lines 5 - 10, Update the verification flow
around m4b and subprocess.run to validate m4b.is_file() before accessing its
size, and run ffprobe with check=True while surfacing execution errors. Only
pass successful stdout to json.loads, preserving the existing chapter data
parsing behavior.
```

</details>

<!-- fingerprinting:phantom:poseidon:terra -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:ab554a2dab3926fc44433f4d -->

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #17 (ID: 3635745863, Reply to: None)
**File:** `shared/single_instance.py` (Line: 59) | **Commit:** `1fa56c3`

```markdown
_🩺 Stability & Availability_ | _🟠 Major_ | _⚡ Quick win_

**Do not unlink an advisory-lock file during release.**

After the unlock at Line 46/49, another process can acquire the existing lock before Line 57 removes its pathname. A third process can then create and lock a new file, allowing two instances concurrently. Keep the lock file; OS-managed advisory locks are released when the process exits.

<details>
<summary>🧰 Tools</summary>

<details>
<summary>🪛 Ruff (0.15.21)</summary>

[error] 58-59: `try`-`except`-`pass` detected, consider logging the exception

(S110)

---

[warning] 58-58: Do not catch blind exception: `Exception`

(BLE001)

</details>

</details>

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@shared/single_instance.py` around lines 55 - 59, Remove the
lock_file.unlink() call from the release cleanup in the single-instance lock
implementation, while retaining the unlock operation. Keep the existing lock
file so other processes always contend on the same pathname; rely on the OS to
release the advisory lock when the process exits.
```

</details>

<!-- fingerprinting:phantom:poseidon:terra -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:25665a3d26d021d8c9bad667 -->

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #18 (ID: 3635745867, Reply to: None)
**File:** `start_app.pyw` (Line: 52) | **Commit:** `1fa56c3`

```markdown
_🩺 Stability & Availability_ | _🟡 Minor_ | _⚡ Quick win_

**Restore readiness polling before opening the browser.**

A fixed two-second sleep races dashboard startup. On a slower startup, the browser receives a connection failure and will not automatically retry. Poll a lightweight local endpoint with a bounded timeout before opening the URL.

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@start_app.pyw` around lines 31 - 35, Replace the fixed time.sleep delay in
the startup flow before webbrowser.open with bounded readiness polling against a
lightweight local endpoint. Continue polling until the endpoint responds or the
timeout expires, then preserve the existing browser-opening behavior without
allowing startup to wait indefinitely.
```

</details>

<!-- fingerprinting:phantom:poseidon:terra -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:7e79628f725064de7675bc02 -->

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #19 (ID: 3635745870, Reply to: None)
**File:** `voice/tts_server/main.py` (Line: 279) | **Commit:** `ad2dc49`

```markdown
_🎯 Functional Correctness_ | _🟠 Major_ | _⚡ Quick win_

**Do not silently substitute the narrator for a missing character voice.**

A typo or failed voice bootstrap now produces narrator audio while reporting generation success, permanently masking an incorrect character performance. Preserve the 404, or make fallback explicitly opt-in and return the effective speaker.

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@voice/tts_server/main.py` around lines 276 - 279, Update the voice resolution
flow around library.get_voice_path so a missing requested character voice does
not silently switch to "narrator"; preserve the existing not-found/404 failure
behavior, or require an explicit opt-in before fallback. If fallback remains
supported, return the effective speaker alongside the generated result.
```

</details>

<!-- fingerprinting:phantom:poseidon:terra -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:a308556979180ed2738b2529 -->

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #20 (ID: 3635759027, Reply to: None)
**File:** `desktop/main.js` (Line: 72) | **Commit:** `ad2dc49`

```markdown
_🩺 Stability & Availability_ | _🟠 Major_ | _⚡ Quick win_

<details>
<summary>🧩 Analysis chain</summary>

🏁 Script executed:

```shell
#!/bin/bash
set -euo pipefail

# Show the relevant file with line numbers
wc -l desktop/main.js
sed -n '1,220p' desktop/main.js

# Look for any existing child-process error handling in the desktop app
rg -n "once\\('error'|on\\('error'|spawn\\(|pythonProcesses|stop.*process|kill\\(" desktop -S
```

Repository: NicusorFlorinBaluta/crazy-audiobook-creator

Length of output: 5156

---

🏁 Script executed:

```shell
#!/bin/bash
set -euo pipefail

# Inspect Node.js child_process.spawn semantics from installed docs if available in the runtime
node - <<'JS'
const { spawn } = require('child_process');
console.log(typeof spawn);
JS
```

Repository: NicusorFlorinBaluta/crazy-audiobook-creator

Length of output: 190

---



</details>

**Handle `spawn()` errors for the Python services.**  
If `python` is missing or the executable cannot be started, both `spawn()` calls emit an unhandled `error` event and can terminate Electron before startup failure is reported.

<details>
<summary>Proposed fix</summary>

```diff
   const voiceProc = spawn(pythonExe, ['-m', 'voice.tts_server.main'], {
     cwd: rootDir,
     env: env,
     stdio: 'ignore'
   });
+  voiceProc.once('error', (error) => {
+    console.error('[Electron] Voice Server failed to start:', error);
+  });
+
   if (voiceProc.pid) {
     pythonProcesses.push(voiceProc);
     console.log(`[Electron] Launched Voice Server (PID ${voiceProc.pid})`);
   }
@@
   const dashProc = spawn(pythonExe, ['-m', 'uvicorn', 'brain.dashboard.api.main:app', '--host', '127.0.0.1', '--port', '8000'], {
     cwd: rootDir,
     env: env,
     stdio: 'ignore'
   });
+  dashProc.once('error', (error) => {
+    console.error('[Electron] Dashboard API failed to start:', error);
+  });
```
</details>

<!-- suggestion_start -->

<details>
<summary>📝 Committable suggestion</summary>

> ‼️ **IMPORTANT**
> Carefully review the code before committing. Ensure that it accurately replaces the highlighted code, contains no missing lines, and has no issues with indentation. Thoroughly test & benchmark the code to ensure it meets the requirements.

```suggestion
  const voiceProc = spawn(pythonExe, ['-m', 'voice.tts_server.main'], {
    cwd: rootDir,
    env: env,
    stdio: 'ignore'
  });
  voiceProc.once('error', (error) => {
    console.error('[Electron] Voice Server failed to start:', error);
  });

  if (voiceProc.pid) {
    pythonProcesses.push(voiceProc);
    console.log(`[Electron] Launched Voice Server (PID ${voiceProc.pid})`);
  }

  // 2. Start Dashboard API (port 8000)
  const dashProc = spawn(pythonExe, ['-m', 'uvicorn', 'brain.dashboard.api.main:app', '--host', '127.0.0.1', '--port', '8000'], {
    cwd: rootDir,
    env: env,
    stdio: 'ignore'
  });
  dashProc.once('error', (error) => {
    console.error('[Electron] Dashboard API failed to start:', error);
  });
  if (dashProc.pid) {
    pythonProcesses.push(dashProc);
    console.log(`[Electron] Launched Dashboard API (PID ${dashProc.pid})`);
  }
```

</details>

<!-- suggestion_end -->

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@desktop/main.js` around lines 53 - 72, Update the Voice Server and Dashboard
API process startup around the two spawn calls to register error handlers for
each ChildProcess before startup can fail. Report a clear service-specific
startup error and preserve the existing process tracking and launch logging for
successfully spawned processes, preventing unhandled spawn error events from
terminating Electron.
```

</details>

<!-- fingerprinting:phantom:poseidon:terra -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:ecf4d92edd14b32e6f275e67 -->

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #21 (ID: 3635759041, Reply to: None)
**File:** `desktop/main.js` (Line: 154) | **Commit:** `1fa56c3`

```markdown
_🩺 Stability & Availability_ | _🟠 Major_ | _⚡ Quick win_

<details>
<summary>🧩 Analysis chain</summary>

🏁 Script executed:

```shell
#!/bin/bash
set -euo pipefail

printf '\n== desktop/main.js excerpts ==\n'
nl -ba desktop/main.js | sed -n '100,210p'

printf '\n== search stopPythonProcesses ==\n'
rg -n "stopPythonProcesses|before-quit|window-all-closed|activate|quit" desktop/main.js desktop -S
```

Repository: NicusorFlorinBaluta/crazy-audiobook-creator

Length of output: 253

---

🏁 Script executed:

```shell
#!/bin/bash
set -euo pipefail
nl -ba desktop/main.js | sed -n '1,260p'
```

Repository: NicusorFlorinBaluta/crazy-audiobook-creator

Length of output: 221

---

🏁 Script executed:

```shell
#!/bin/bash
set -euo pipefail

printf '\n== desktop/main.js (relevant lines) ==\n'
awk 'NR>=100 && NR<=220 { printf "%4d  %s\n", NR, $0 }' desktop/main.js

printf '\n== lifecycle/search matches ==\n'
rg -n "stopPythonProcesses|before-quit|window-all-closed|activate|quit" desktop/main.js desktop -S
```

Repository: NicusorFlorinBaluta/crazy-audiobook-creator

Length of output: 3036

---



</details>

**Keep backend shutdown in `before-quit` only.**  
On macOS, closing the last window does not exit Electron, so `stopPythonProcesses()` here leaves the app running with no backend when the window is reopened. Gate this cleanup on an actual app quit and leave the window handlers to window lifecycle only.

<details>
<summary>📍 Affects 1 file</summary>

- `desktop/main.js#L129-L135` (this comment)
- `desktop/main.js#L162-L168`

</details>

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@desktop/main.js` around lines 129 - 135, Remove the stopPythonProcesses()
call and related quit-state mutation from the mainWindow close handler, keeping
it limited to window lifecycle behavior. Ensure backend shutdown remains
exclusively in the app’s before-quit handler at desktop/main.js lines 162-168.
```

</details>

<!-- consolidated_sites_start -->
<!--
<consolidated_sites>
<site>
<role>anchor</role>
<file>desktop/main.js</file>
<line_range>129-135</line_range>
</site>
<site>
<role>sibling</role>
<file>desktop/main.js</file>
<line_range>162-168</line_range>
</site>
</consolidated_sites>
-->
<!-- consolidated_sites_end -->

<!-- fingerprinting:phantom:poseidon:terra -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:ebbb304b431d99d13e9c5ef5 -->

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #22 (ID: 3640741737, Reply to: None)
**File:** `brain/dashboard/api/main.py` (Line: 525) | **Commit:** `ad2dc49`

```markdown
_🎯 Functional Correctness_ | _🟠 Major_ | _⚡ Quick win_

**Count actual generated segments, not synthetic merged batches.**

`ValidationLoop` generates and writes one WAV for each `ScriptLine`, while this block merges adjacent lines into a smaller denominator. Chapters with consecutive matching speakers/emotions can show 100% progress after only a fraction of their segments exist.

<details>
<summary>Proposed fix</summary>

```diff
-                    # Count merged line batches matching TTS generation merging
-                    merged_count = 0
-                    prev_speaker = None
-                    prev_emotion = None
-                    prev_words = 0
-
-                    for l in raw_lines:
-                        spk = l.get("speaker")
-                        emo = (l.get("emotion") or "").strip().lower()
-                        words = len((l.get("text") or "").split())
-                        if prev_speaker == spk and prev_emotion == emo and (prev_words + words < 250):
-                            prev_words += words
-                        else:
-                            merged_count += 1
-                            prev_speaker = spk
-                            prev_emotion = emo
-                            prev_words = words
-
-                    total_lines = merged_count or len(raw_lines)
+                    total_lines = len(raw_lines)
```
</details>

<!-- suggestion_start -->

<details>
<summary>📝 Committable suggestion</summary>

> ‼️ **IMPORTANT**
> Carefully review the code before committing. Ensure that it accurately replaces the highlighted code, contains no missing lines, and has no issues with indentation. Thoroughly test & benchmark the code to ensure it meets the requirements.

```suggestion
                    total_lines = len(raw_lines)
```

</details>

<!-- suggestion_end -->

<details>
<summary>🧰 Tools</summary>

<details>
<summary>🪛 Ruff (0.15.21)</summary>

[error] 513-513: Ambiguous variable name: `l`

(E741)

</details>

</details>

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@brain/dashboard/api/main.py` around lines 507 - 525, Update the progress
denominator calculation in the block iterating over raw_lines to count actual
generated segments, using the number of ScriptLine entries rather than synthetic
speaker/emotion/word-based merged batches. Remove the merged_count state and
associated merging logic, while preserving the existing fallback behavior for
empty raw_lines if needed by the surrounding progress calculation.
```

</details>

<!-- fingerprinting:phantom:poseidon:terra -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:74b154e9a293ebb3a9924a02 -->

<!-- This is an auto-generated comment by CodeRabbit -->

✅ Addressed in commits a4fddb4 to 1fa56c3
```

---

## Comment #23 (ID: 3640741756, Reply to: None)
**File:** `brain/director/script_generator.py` (Line: 793) | **Commit:** `1fa56c3`

```markdown
_🎯 Functional Correctness_ | _🟡 Minor_ | _⚡ Quick win_

**Guard against explicit `null` speaker in model output.**

`str(meta.get("speaker", "narrator"))` only falls back to `"narrator"` when the `"speaker"` key is *missing*; if the LLM emits `"speaker": null`, `meta.get` returns `None` (key is present) and this becomes the literal string `"none"`, which then gets registered as a bogus character via `_detect_new_characters`. Two lines below, `pause_before_ms`/`pause_after_ms` already guard against this exact case with `... or 0`/`... or 500` — the same pattern should be applied here.

<details>
<summary>🐛 Proposed fix</summary>

```diff
-            speaker = str(meta.get("speaker", "narrator")).lower().replace(" ", "_")
+            speaker = str(meta.get("speaker") or "narrator").lower().replace(" ", "_")
```
</details>

<!-- suggestion_start -->

<details>
<summary>📝 Committable suggestion</summary>

> ‼️ **IMPORTANT**
> Carefully review the code before committing. Ensure that it accurately replaces the highlighted code, contains no missing lines, and has no issues with indentation. Thoroughly test & benchmark the code to ensure it meets the requirements.

```suggestion

            text_trimmed = text.strip()
            is_quote = bool(quote_pattern.match(text_trimmed))
            
            # CRITICAL RULE: Non-dialogue text outside quotation marks MUST be narrator!
            speaker = str(meta.get("speaker") or "narrator").lower().replace(" ", "_")
            if not is_quote:
                speaker = "narrator"
                
            lines.append(
                ScriptLine(
                    line_id=f"ch{fallback_number:02d}_{i:03d}",
                    speaker=speaker,
```

</details>

<!-- suggestion_end -->

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@brain/director/script_generator.py` around lines 499 - 511, Update speaker
extraction in the script-line construction flow to treat an explicit null
speaker like a missing speaker by applying a narrator fallback before string
normalization. Preserve the existing lowercasing, space replacement, and
non-quote narrator override, and ensure _detect_new_characters never receives
the literal “none” speaker.
```

</details>

<!-- fingerprinting:phantom:medusa:beignet -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:a8f3b27ef102ca4ed06b1f81 -->

<!-- This is an auto-generated comment by CodeRabbit -->

✅ Addressed in commits a4fddb4 to 1fa56c3
```

---

## Comment #24 (ID: 3640741771, Reply to: None)
**File:** `scratch/check_progress.py` (Line: 7) | **Commit:** `1fa56c3`

```markdown
_🩺 Stability & Availability_ | _🟡 Minor_ | _⚡ Quick win_

**Local dashboard/voice HTTP requests need bounded waits.**

- `scratch/check_progress.py#L7-L7`: add a timeout to the voice health request.
- `scratch/check_progress.py#L14-L15`: add a timeout to the dashboard status request.
- `scratch/force_full_rebuild.py#L13-L14`: add a timeout to the stop request.
- `scratch/master_all_and_export.py#L29-L31`: add a timeout to the start request.
- `scratch/reset_all_chapters.py#L12-L13`: add a timeout to the stop request.
- `scratch/reset_all_chapters.py#L60-L61`: add a timeout to the start request.

<details>
<summary>📍 Affects 4 files</summary>

- `scratch/check_progress.py#L7-L7` (this comment)
- `scratch/check_progress.py#L14-L15`
- `scratch/force_full_rebuild.py#L13-L14`
- `scratch/master_all_and_export.py#L29-L31`
- `scratch/reset_all_chapters.py#L12-L13`
- `scratch/reset_all_chapters.py#L60-L61`

</details>

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@scratch/check_progress.py` at line 7, Bound all local dashboard and voice
HTTP requests with an explicit timeout: update the health and dashboard status
requests in scratch/check_progress.py, the stop request in
scratch/force_full_rebuild.py, the start request in
scratch/master_all_and_export.py, and the stop and start requests in
scratch/reset_all_chapters.py. Use the same finite timeout consistently across
these request calls.
```

</details>

<!-- consolidated_sites_start -->
<!--
<consolidated_sites>
<site>
<role>anchor</role>
<file>scratch/check_progress.py</file>
<line_range>7-7</line_range>
</site>
<site>
<role>sibling</role>
<file>scratch/check_progress.py</file>
<line_range>14-15</line_range>
</site>
<site>
<role>sibling</role>
<file>scratch/force_full_rebuild.py</file>
<line_range>13-14</line_range>
</site>
<site>
<role>sibling</role>
<file>scratch/master_all_and_export.py</file>
<line_range>29-31</line_range>
</site>
<site>
<role>sibling</role>
<file>scratch/reset_all_chapters.py</file>
<line_range>12-13</line_range>
</site>
<site>
<role>sibling</role>
<file>scratch/reset_all_chapters.py</file>
<line_range>60-61</line_range>
</site>
</consolidated_sites>
-->
<!-- consolidated_sites_end -->

<!-- fingerprinting:phantom:poseidon:terra -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:fc420883f179d55a10354888 -->

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #25 (ID: 3640741778, Reply to: None)
**File:** `scratch/fix_dialogue_quote_speakers.py` (Line: 63) | **Commit:** `1fa56c3`

```markdown
_🎯 Functional Correctness_ | _🔴 Critical_ | _⚡ Quick win_

**Index-out-of-bounds crash / silent wraparound in adjacency check.**

`lines[idx + 1]` and `lines[idx - 1]` are accessed a second time inside the `adjacent_narrative` expression without the bounds guard already applied to `next_text`/`prev_text`. This will raise `IndexError` whenever a quoted line is the last line in a chapter script (`idx + 1 == len(lines)`), and will silently wrap to `lines[-1]` when `idx == 0`, corrupting the adjacency check with unrelated text.

<details>
<summary>🐛 Proposed fix</summary>

```diff
         if is_quote:
-            next_text = lines[idx + 1]["text"].lower() if idx + 1 < len(lines) else ""
-            prev_text = lines[idx - 1]["text"].lower() if idx > 0 else ""
-            adjacent_narrative = (next_text if not quote_pattern.match(lines[idx + 1]["text"].strip()) else "") + " " + (prev_text if not quote_pattern.match(lines[idx - 1]["text"].strip()) else "")
+            has_next = idx + 1 < len(lines)
+            has_prev = idx > 0
+            next_text = lines[idx + 1]["text"].lower() if has_next else ""
+            prev_text = lines[idx - 1]["text"].lower() if has_prev else ""
+            next_is_quote = has_next and quote_pattern.match(lines[idx + 1]["text"].strip())
+            prev_is_quote = has_prev and quote_pattern.match(lines[idx - 1]["text"].strip())
+            adjacent_narrative = (next_text if has_next and not next_is_quote else "") + " " + (prev_text if has_prev and not prev_is_quote else "")
```
</details>

<!-- suggestion_start -->

<details>
<summary>📝 Committable suggestion</summary>

> ‼️ **IMPORTANT**
> Carefully review the code before committing. Ensure that it accurately replaces the highlighted code, contains no missing lines, and has no issues with indentation. Thoroughly test & benchmark the code to ensure it meets the requirements.

```suggestion
    for idx, line in enumerate(lines):
        text_trimmed = line.get("text", "").strip()
        is_quote = bool(quote_pattern.match(text_trimmed))
        spk = line.get("speaker", "narrator").lower()
        
        if is_quote:
            has_next = idx + 1 < len(lines)
            has_prev = idx > 0
            next_text = lines[idx + 1]["text"].lower() if has_next else ""
            prev_text = lines[idx - 1]["text"].lower() if has_prev else ""
            next_is_quote = has_next and quote_pattern.match(lines[idx + 1]["text"].strip())
            prev_is_quote = has_prev and quote_pattern.match(lines[idx - 1]["text"].strip())
            adjacent_narrative = (next_text if has_next and not next_is_quote else "") + " " + (prev_text if has_prev and not prev_is_quote else "")
            
            # Check 1: Explicit character name in adjacent narration (e.g. "Vathi said", "Dusk called")
            found_cid = None
            for name_key, cid in name_to_cid.items():
                if re.search(r'\b' + re.escape(name_key) + r'\b\s*(said|whispered|asked|replied|cried|smiled|called|nodded|thought|spoke)', adjacent_narrative):
                    found_cid = cid
                    break
            
            if found_cid and found_cid != spk:
                line["speaker"] = found_cid
                ch_fixes += 1
                quote_fixes += 1
                print(f"Name Tag Fix [{s_file.name} / {line['line_id']}]: '{spk}' -> '{found_cid}' (tag: '{adjacent_narrative.strip()}')")
                continue
```

</details>

<!-- suggestion_end -->

<details>
<summary>🧰 Tools</summary>

<details>
<summary>🪛 ast-grep (0.44.1)</summary>

[warning] 53-53: Regex pattern passed to re is built from a non-literal (variable, call, concatenation, or f-string) value. If that value is attacker-controlled it can introduce a malicious pattern with catastrophic backtracking (ReDoS). Use a hardcoded literal pattern, or validate/escape untrusted input with re.escape() and bound the regex complexity before compiling.
Context: re.search(r'\b' + re.escape(name_key) + r'\b\s*(said|whispered|asked|replied|cried|smiled|called|nodded|thought|spoke)', adjacent_narrative)
Note: [CWE-1333] Inefficient Regular Expression Complexity.

(redos-non-literal-regex-python)

</details>

</details>

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@scratch/fix_dialogue_quote_speakers.py` around lines 41 - 63, Fix the
adjacency check in the quote-processing loop by reusing the bounds-safe
next_text and prev_text values, or otherwise guard both neighbor accesses before
reading their text. Update the adjacent_narrative construction around the
is_quote branch so last-line quotes do not raise IndexError and first-line
quotes do not inspect unrelated trailing text.
```

</details>

<!-- fingerprinting:phantom:medusa:beignet -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:e29c4059bf154fd3eaf215fa -->

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #26 (ID: 3640741784, Reply to: None)
**File:** `scratch/force_full_rebuild.py` (Line: 18) | **Commit:** `1fa56c3`

```markdown
_🗄️ Data Integrity & Integration_ | _🟠 Major_ | _⚡ Quick win_

**Destructive resets must require a confirmed pipeline stop.**

- `scratch/force_full_rebuild.py#L11-L18`: fail the script when stopping fails and verify the pipeline is no longer running before deleting files.
- `scratch/reset_all_chapters.py#L10-L17`: fail the script when stopping fails and verify the pipeline is no longer running before deleting segments.

<details>
<summary>🧰 Tools</summary>

<details>
<summary>🪛 ast-grep (0.44.1)</summary>

[warning] 13-13: Request-controlled URL passed to urlopen; validate against an allowlist to prevent SSRF.
Context: urllib.request.urlopen(req)
Note: [CWE-918] Server-Side Request Forgery (SSRF).

(urlopen-unsanitized-data)

</details>
<details>
<summary>🪛 Ruff (0.15.21)</summary>

[error] 14-14: Audit URL open for permitted schemes. Allowing use of `file:` or custom schemes is often unexpected.

(S310)

---

[warning] 17-17: Do not catch blind exception: `Exception`

(BLE001)

</details>

</details>

<details>
<summary>📍 Affects 2 files</summary>

- `scratch/force_full_rebuild.py#L11-L18` (this comment)
- `scratch/reset_all_chapters.py#L10-L17`

</details>

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@scratch/force_full_rebuild.py` around lines 11 - 18, Update the stop-request
handling in scratch/force_full_rebuild.py lines 11-18 and
scratch/reset_all_chapters.py lines 10-17 so any request failure terminates the
script instead of being logged and ignored, then query the Dashboard API to
confirm the pipeline is no longer running before proceeding to delete files or
segments; abort the destructive reset if it remains active.
```

</details>

<!-- consolidated_sites_start -->
<!--
<consolidated_sites>
<site>
<role>anchor</role>
<file>scratch/force_full_rebuild.py</file>
<line_range>11-18</line_range>
</site>
<site>
<role>sibling</role>
<file>scratch/reset_all_chapters.py</file>
<line_range>10-17</line_range>
</site>
</consolidated_sites>
-->
<!-- consolidated_sites_end -->

<!-- fingerprinting:phantom:poseidon:terra -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:1cf9b82a3d4395e4bef6f712 -->

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #27 (ID: 3640741788, Reply to: None)
**File:** `scratch/force_full_rebuild.py` (Line: 30) | **Commit:** `1fa56c3`

```markdown
_🗄️ Data Integrity & Integration_ | _🟠 Major_ | _⚡ Quick win_

**Artifact deletion must succeed before reset state is committed.**

- `scratch/force_full_rebuild.py#L20-L30`: remove `ignore_errors=True` and stop before resetting state when workspace/chapter deletion fails.
- `scratch/reset_all_chapters.py#L21-L27`: report or raise failed `unlink()` calls instead of claiming every segment was removed.

<details>
<summary>📍 Affects 2 files</summary>

- `scratch/force_full_rebuild.py#L20-L30` (this comment)
- `scratch/reset_all_chapters.py#L21-L27`

</details>

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@scratch/force_full_rebuild.py` around lines 20 - 30, Ensure artifact deletion
succeeds before committing reset state: in scratch/force_full_rebuild.py lines
20-30, remove ignore_errors=True from both shutil.rmtree calls and stop
execution if either deletion fails; in scratch/reset_all_chapters.py lines
21-27, report or raise any failed unlink() calls instead of claiming all
segments were removed.
```

</details>

<!-- consolidated_sites_start -->
<!--
<consolidated_sites>
<site>
<role>anchor</role>
<file>scratch/force_full_rebuild.py</file>
<line_range>20-30</line_range>
</site>
<site>
<role>sibling</role>
<file>scratch/reset_all_chapters.py</file>
<line_range>21-27</line_range>
</site>
</consolidated_sites>
-->
<!-- consolidated_sites_end -->

<!-- fingerprinting:phantom:poseidon:terra -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:76fccc60a6249c76008ce3cd -->

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #28 (ID: 3640741795, Reply to: None)
**File:** `scratch/force_full_rebuild.py` (Line: 58) | **Commit:** `1fa56c3`

```markdown
_🎯 Functional Correctness_ | _🟠 Major_ | _⚡ Quick win_

**Start the rebuild or leave the job in a non-running reset state.**

The script sets `status` to `generating` but explicitly sets `running` to `False` and never invokes `/api/projects/{project_id}/start`. The dashboard will show an idle job as generating rather than performing the promised rebuild. The supplied start handler is the intended mechanism to create the background task.

<details>
<summary>🧰 Tools</summary>

<details>
<summary>🪛 ast-grep (0.44.1)</summary>

[info] 58-58: use jsonify instead of json.dumps for JSON output
Context: json.dumps(state)
Note: [CWE-116] Improper Encoding or Escaping of Output.

(use-jsonify)

</details>

</details>

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@scratch/force_full_rebuild.py` around lines 52 - 58, Update the rebuild
initialization flow so it invokes the supplied start handler at
/api/projects/{project_id}/start after resetting the state, allowing the
background rebuild task to begin; alternatively, leave the reset state
non-running with a non-generating status. Ensure state["status"] and
state["running"] remain consistent and the dashboard does not show an idle job
as generating.
```

</details>

<!-- fingerprinting:phantom:poseidon:terra -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:d306b43eee6a57e7318db89f -->

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #29 (ID: 3640741802, Reply to: None)
**File:** `scratch/test_script_fixes.py` (Line: 16) | **Commit:** `1fa56c3`

```markdown
_📐 Maintainability & Code Quality_ | _🟡 Minor_ | _⚡ Quick win_

**Rename ambiguous variable `l`.**

Ruff flags this as an `E741` error (not just a style warning); renaming avoids a potential lint-gate failure.

<details>
<summary>🐛 Proposed fix</summary>

```diff
-for l in lines:
-    orig_spk = l["speaker"]
-    text = l["text"].strip()
+for line in lines:
+    orig_spk = line["speaker"]
+    text = line["text"].strip()
     is_quote = bool(quote_pattern.match(text))
     
     if not is_quote and orig_spk != "narrator":
         fixed_lines += 1
-        print(f"FIXED [{l['line_id']}]: '{orig_spk}' -> 'narrator' | Text: {text}")
+        print(f"FIXED [{line['line_id']}]: '{orig_spk}' -> 'narrator' | Text: {text}")
```
</details>

<details>
<summary>🧰 Tools</summary>

<details>
<summary>🪛 Ruff (0.15.21)</summary>

[error] 16-16: Ambiguous variable name: `l`

(E741)

</details>

</details>

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@scratch/test_script_fixes.py` at line 16, Rename the ambiguous loop variable
l in the lines iteration to a descriptive name, and update every reference
within that loop accordingly so Ruff no longer reports E741.
```

</details>

<!-- fingerprinting:phantom:medusa:beignet -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:6d314a7ee4711a478aaf678e -->

_Source: Linters/SAST tools_

<!-- This is an auto-generated comment by CodeRabbit -->
```

---

## Comment #30 (ID: 3640741808, Reply to: None)
**File:** `voice/tts_server/main.py` (Line: 473) | **Commit:** `ad2dc49`

```markdown
_🩺 Stability & Availability_ | _🟠 Major_ | _⚡ Quick win_

**Do not report success when Whisper unloading fails.**

The broad `except Exception: pass` suppresses unload failures, so the endpoint can return `"status": "unloaded"` while Whisper remains resident in VRAM. Catch the expected exception type, log the failure, and return a partial-failure status—or let the request fail.

<details>
<summary>🧰 Tools</summary>

<details>
<summary>🪛 Ruff (0.15.21)</summary>

[error] 470-471: `try`-`except`-`pass` detected, consider logging the exception

(S110)

---

[warning] 470-470: Do not catch blind exception: `Exception`

(BLE001)

</details>

</details>

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Verify each finding against current code. Fix only still-valid issues, skip the
rest with a brief reason, keep changes minimal, and validate.

In `@voice/tts_server/main.py` around lines 466 - 473, Update the Whisper unload
handling in the validator unload block to catch the expected exception type
instead of broadly suppressing failures. Log any unload failure and ensure the
endpoint does not return “status”: “unloaded” when Whisper remains loaded;
return a partial-failure status or propagate the exception while preserving
successful unload reporting.
```

</details>

<!-- fingerprinting:phantom:triton:luna -->

<!-- cr-indicator-types:potential_issue -->

<!-- cr-comment:v1:625bbb1a96839654c3e34137 -->

_Source: Linters/SAST tools_

<!-- This is an auto-generated comment by CodeRabbit -->
```

---
