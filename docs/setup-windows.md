# Windows Setup

**Status:** Reference — Describes current behaviour. Keep it accurate when the code changes.

The supported default runs the Brain, Voice, Ollama, dashboard, and audio tools on one Windows workstation.

## Troubleshooting

### "Access is denied" writing under `brain/projects/`

Symptom: a pipeline step that rewrites a project artifact fails with
`PermissionError: [WinError 5] Access is denied`, naming a `.tmp` file and its
target. Reading works; only the atomic replace fails.

Cause: the target file is **owned by `BUILTIN\Administrators`**, left that way
by a run started from an elevated shell. `os.replace` over an existing file
needs the DELETE right on the target, and an unelevated token inherits only
`BUILTIN\Users: Write, ReadAndExecute`, which does not include it. The
dashboard scheduled task runs at `RunLevel: Limited`, so it is affected too --
this is not a permissions problem you can see from the file being "writable".

Measured on this workstation on 2026-09-04: of the first 500 files under
`brain/projects`, **259 were owned by Administrators and 241 by the normal
user**. The mix is the tell -- some runs were elevated and some were not.

Diagnose:

```powershell
Get-ChildItem "E:\Projects\crazy-audiobook-creator\brain\projects" -Recurse -File |
  Group-Object { (Get-Acl $_.FullName).Owner } |
  ForEach-Object { "{0,6}  {1}" -f $_.Count, $_.Name }
```

Fix, from an **elevated** PowerShell:

```powershell
takeown /F "E:\Projects\crazy-audiobook-creator\brain\projects" /R /D Y
icacls "E:\Projects\crazy-audiobook-creator\brain\projects" /grant "${env:USERNAME}:(OI)(CI)F" /T
```

### Confirmed on this workstation, 2026-09-04

The fix above was applied and the cause verified end to end:

| | Before `takeown`/`icacls` | After |
| --- | --- | --- |
| Sampled files under `brain/projects` | 259 Administrators / 241 user | 500 user |
| `os.replace` over `characters.json` | denied | succeeds |
| `atomic_write_json` on a project artifact | raised | succeeds |

`.pytest_cache` needs the same treatment and is not covered by the command
above -- its ACL cannot even be *read* by the normal user, which is why every
test run prints `Access is denied` warnings for it. It is a regenerable cache,
so the simplest fix is to delete it from an elevated shell:

```powershell
Remove-Item -Recurse -Force "E:\Projects\crazy-audiobook-creator\.pytest_cache"
```

### Preventing recurrence

Nothing in normal operation needs elevation. Home Assistant drives the app over
HTTP, and `/api/system/restart` runs `schtasks /Run` against a task the user
already owns.

#### `RunLevel: Limited` is not enough on its own — corrected 2026-09-04

This page used to say both tasks run unelevated because they are registered at
`RunLevel: Limited`. That is not what happens. The dashboard task also used
`-LogonType S4U`, and for an account that belongs to Administrators a
service-for-user logon returns a **full** token: UAC filtering applies to
interactive-style logons, not to S4U. So the dashboard ran elevated, silently,
for as long as that registration stood.

Measured with two throwaway tasks differing only in logon type, `RunLevel`
held at `Limited`:

| Logon type | Token elevated | Files it creates |
| --- | --- | --- |
| `S4U` | yes | `BUILTIN\Administrators` |
| `Interactive` | no | the user |

`install_dashboard_task.ps1` now registers the dashboard with
`-LogonType Password`, which yields the same filtered token as `Interactive`
while still running with nobody logged on — the one property S4U was chosen
for. It prompts for the credential at install time and stores nothing itself;
Windows keeps it in the Task Scheduler vault.

**An existing task keeps its old principal.** Registering over it does not
change the logon type, so a machine set up before this must unregister and
re-run the installer. The script warns when it finds an S4U registration.

#### Why the first fix regressed

The `takeown`/`icacls` above was applied to `brain/projects` only. That tree
survived the next elevated run untouched, because the inheritable `(OI)(CI)`
grant means new Administrators-owned files are still replaceable. The trees
that never got the grant did not: on 2026-09-04 an unelevated `os.replace` of
a speaker embedding under `voice_library` returned `[WinError 5]` while the
same operation under `brain/projects` succeeded, same process, same moment.

Apply the grant to every tree the app writes:

```powershell
foreach ($d in "brain\projects","workspace","voice_library","voice") {
    $p = "E:\Projects\crazy-audiobook-creator\$d"
    takeown /F $p /R /D Y | Out-Null
    icacls $p /grant "${env:USERNAME}:(OI)(CI)F" /T | Out-Null
}
```

`install-windows.ps1` does this for a fresh install. The inheritance flags are
the point: a bare `takeown` fixes only the files that exist when it runs.

Only one-time setup needs an elevated shell: registering the scheduled tasks,
the ACL grant above, and the `netsh portproxy` / firewall scripts.

Two guards now make a lapse loud instead of silent:

- The dashboard logs a prominent warning at startup if it is running elevated,
  naming the consequence rather than just the fact.
- A replace that is denied no longer surfaces as a bare `WinError 5` against a
  temp file. `shared/artifacts.py` reads the destination's owner and, when it
  is `BUILTIN\Administrators`, says so and points here.

## Prerequisites

- Windows 10/11
- Python environment with a GPU-enabled PyTorch build appropriate for the machine
- Ollama
- FFmpeg and ffprobe on `PATH`
- Git
- Optional: Node.js for the Electron shell

The repository’s current defaults target the existing environment `E:\PyTorch env\my_venv`. A path without spaces is preferable for GPU toolchains that launch helper executables; otherwise a Windows 8.3 short path may be needed by launchers.

## 1. Clone and open the repository

```powershell
git clone <repository-url> crazy-audiobook-creator
Set-Location crazy-audiobook-creator
```

All commands and path safety checks assume the repository root is the current directory.

## 2. Prepare Python

Install the GPU-enabled PyTorch build separately according to the selected runtime. Then install application dependencies into the same environment:

```powershell
$python = "E:\PyTorch env\my_venv\Scripts\python.exe"
& $python -m pip install -r brain\requirements.txt
# Always install the voice stack through the tested constraints file. Without
# `-c`, a reinstall can silently change the packages that produce the audio.
& $python -m pip install -r voice\requirements.txt -c voice\constraints-windows-rocm-tested.txt
.\scripts\setup-voice-server.ps1 -VenvPath "E:\PyTorch env\my_venv"
```

The installer resolves `voice/requirements.txt` through
`voice/constraints-windows-rocm-tested.txt`, which records the direct runtime
versions observed in the successful 2026-08-09 AMD run. PyTorch remains a
separate machine-specific ROCm installation. Use `-DryRun` to run only the
read-only preflight and dependency-consistency checks.

The setup script removes a stale `parler-tts` installation, installs the supported Qwen/Whisper requirements, runs `pip check`, and checks for FFmpeg. `legacy/parler_server.py` is legacy-only; if it must be evaluated, use `voice/requirements-legacy-parler.txt` in a separate environment because its Transformers requirement conflicts with Qwen. See [../legacy/README.md](../legacy/README.md).

Verify core imports:

```powershell
& $python -c "import torch, fastapi, soundfile; print(torch.__version__, torch.cuda.is_available())"
ffmpeg -version
ffprobe -version
```

The application expects the GPU PyTorch build to expose the device through PyTorch’s `cuda` interface, including compatible AMD-on-Windows environments.

## 3. Configure Ollama

Install/start Ollama and pull the model named by `brain/config.yaml`:

```powershell
ollama pull qwen3.8:27b
ollama list
```

If using another model, update `ollama.model`. Character and script fingerprints include the model name, so changing it correctly invalidates dependent scripts.

The checked-in configuration starts a second, app-owned Ollama endpoint on
`127.0.0.1:11435` and reuses `E:\.ollama\models`. It exposes only
device 0 to that process, because device 0 is the RX 7900 XTX on the configured
workstation. If hardware enumeration changes, use `ollama ps` / the Ollama
server log to identify the discrete index and update `ollama.visible_devices`.

The backend is selected explicitly by `ollama.gpu_backend`:

- `vulkan` sets `GGML_VK_VISIBLE_DEVICES` (the current default).
- `rocm` sets `HIP_VISIBLE_DEVICES` and `ROCR_VISIBLE_DEVICES`.

Both isolate the discrete GPU. They differ in throughput: on RDNA3 the
ROCm/hipBLAS backend is usually markedly faster at *prompt processing*, and
`OLLAMA_FLASH_ATTENTION` only takes effect there. Screen a backend change with
`scripts/benchmark_script_chunks.py` and compare prefill tokens/second from
`prompt_eval_duration_ns` before promoting it.

`ollama.num_parallel` is pinned to `1`. Ollama otherwise autodetects (commonly
4), which reserves KV cache *per slot* — 4 x 16384 tokens on a 27B model can
push layers off the GPU despite `num_gpu=99` — and lets sequential chunk
requests land on different slots, defeating prefix-cache reuse of a system
prompt that is byte-identical across every chunk of a chapter.

## 4. Review local configuration

At minimum verify:

```yaml
# brain/config.yaml
ollama:
  host: "http://127.0.0.1:11435"
  model: "qwen3.8:27b"
  auto_start: true
  models_dir: "E:\\.ollama\\models"
  gpu_backend: "vulkan"      # or "rocm"
  visible_devices: "0"
  num_parallel: 1            # keep at 1; see section 3
  flash_attention: true
  keep_alive: "30m"

voice_server:
  host: "http://127.0.0.1:8100"
  venv: "E:\\PyTorch env\\my_venv"
  auto_start: true

dashboard:
  host: "127.0.0.1"
  port: 8000
  # Required when host is not loopback: either this or an application token.
  # Omitting it falls back to every RFC1918 range plus the Tailscale CGNAT
  # range, which is almost always wider than intended.
  trusted_lan_cidrs:
    - "192.168.50.0/24"
  cors_origins:
    - "http://127.0.0.1:8000"
```

Binding the dashboard to a non-loopback host is refused at startup unless
either `dashboard.trusted_lan_cidrs` or a token
(`CRAZY_AUDIOBOOK_DASHBOARD_TOKEN`, or `dashboard.api_token`) is configured.
That check runs however the process is started, including
`uvicorn brain.dashboard.api.main:app`.

And:

```yaml
# voice/config.yaml
tts:
  model: "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
  device: "cuda"

server:
  host: "127.0.0.1"
  port: 8100
  workers: 1
  # Leave empty for the supported loopback setup. The service also accepts
  # CRAZY_AUDIOBOOK_VOICE_TOKEN from the environment, so the secret need not be
  # committed here; set the same value in `voice_server.api_token` on the Brain
  # side if you use it. Binding beyond loopback without a token is refused.
  api_token: ""
```

Keep Ollama and Voice on loopback. If the dashboard binds to the LAN, restrict
Windows Firewall and `dashboard.trusted_lan_cidrs` to the intended subnet and
configure explicit CORS origins. A dashboard token remains available for peers
outside the trusted LAN.

**A reverse proxy connects from its own address.** If nginx on this LAN proxies
the dashboard to the public internet, the proxy's address is inside
`trusted_lan_cidrs`, so every request arriving through it is authorized as a
trusted LAN peer and this application performs no authentication of its own.
Set `CRAZY_AUDIOBOOK_DASHBOARD_TOKEN` and have the proxy inject `X-API-Token`
if the public endpoint must be authenticated by the application rather than
only by the proxy.

## 5. Run tests

```powershell
& $python -m compileall brain voice shared scripts tools .
& $python -m unittest discover -s tests -v

# Static analysis, matching the CI `lint` job:
& $python -m pip install ruff
& $python -m ruff check .
```

These tests verify state/artifact/source/validation logic with fake engines. They do not download or run production models.

## 6. Start the application

Start the dashboard with the self-healing supervisor:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\start_dashboard.ps1
```

The supervisor runs the dashboard, monitors `http://127.0.0.1:8000/health` on 10-second intervals, auto-recovers from unexpected network/socket drops, and cleanly exits when you shut down via the UI or console.

To make external LAN connections immune to physical interface/router resets, optionally enable Windows PortProxy (run once as Administrator):

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\setup_portproxy.ps1
```

For more details, see [Socket Resilience & Supervision](../docs/socket-resilience-and-supervision.md).

After installing the `Crazy Audiobook Dashboard` scheduled task, reload code on
port 8000 with the controlled restart helper:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\restart_dashboard.ps1
```

The helper reads `CRAZY_AUDIOBOOK_DASHBOARD_TOKEN` from the repository `.env`,
requests application cleanup through `/api/system/shutdown`, waits for port 8000
to close, starts the registered task, and waits for dashboard health. It does not
need to expose the token on the command line.

For the Electron wrapper:

```powershell
Set-Location desktop
npm install
npm start
```

## 7. First smoke test

Use a short EPUB:

1. Upload it and wait for book-wide scripting/voice bootstrap.
2. Select one chapter.
3. Start the pipeline and download its WAV/partial M4B.
4. Re-run unchanged to confirm cache reuse.
5. Select a second batch.
6. Select All only when ready to generate remaining chapters and produce the full M4B.

Model downloads and the first voice bootstrap can take substantially longer than subsequent work.

## Troubleshooting

### Ollama unavailable

```powershell
Invoke-RestMethod http://127.0.0.1:11435/api/tags
```

Confirm the service, host, and configured model.

### Ollama scripting is unexpectedly slow

Inspect the Ollama server log. A 32B model split between a discrete GPU and an
integrated GPU can be many times slower even though both devices are reported as
GPU offload. Confirm the managed process logged only the configured discrete
Vulkan device. Pause the project after changing device selection so the old
model is unloaded before retrying.

### Voice health unavailable

```powershell
Invoke-RestMethod http://127.0.0.1:8100/health
Get-Content .\qwen-voice-design.log -Tail 100
```

The VoiceDesign helper log is relevant during reference bootstrap. Qwen Base and Whisper lifecycle also appears in dashboard project logs.

### FFmpeg export failure

Ensure both `ffmpeg` and `ffprobe` resolve in the environment inherited by the dashboard, not only in another terminal profile.

### GPU out of memory

Keep `server.workers: 1`. Do not start parallel project pipelines. The service starts models lazily and keeps VoiceDesign, Qwen Base, and Whisper out of VRAM concurrently unless an explicitly benchmarked validation setting says otherwise.

On AMD/ROCm Windows, keep `validation.whisper_backend: openai_whisper`.
The installed `faster-whisper` package uses CTranslate2's CUDA backend and must
only be selected on a compatible runtime; selecting its `cuda` device on this
AMD setup can terminate the native Voice service rather than raising a Python
exception.

### Python path with spaces warning

If a GPU SDK helper mishandles `E:\PyTorch env\...`, create the environment under a path without spaces or update the configured path to a working short-path alias. This warning originates in the installed runtime, not in EPUB/script logic.

### Stale output after a change

Do not delete random files first. Re-select the chapter and run it: dependency fingerprints should invalidate changed artifacts. If they do not, capture the script/manifest/state files and logs as a reproducible bug.
