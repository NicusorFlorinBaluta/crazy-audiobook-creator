# Agent Customizations

## Ubuntu Server Rules
**CRITICAL: DO NOT DO DESTRUCTIVE OPERATIONS ON THE UBUNTU HOST (192.168.50.180).**
- Do NOT restart or reboot the machine under any circumstances.
- Do NOT change network settings, firewall rules, or DNS.
- Do NOT modify global system settings or existing Docker/VM configurations.
- The machine hosts other projects (VMs and Docker). Your operations MUST remain strictly isolated to the project directory and its virtual environment.
- Any installation should be localized (`venv`, user-space) rather than system-wide whenever possible to avoid package conflicts.
- **RESOURCE LIMITS:** Ensure any processes run on this machine do not bottleneck existing apps. Use `nice`, limit thread counts, and ensure GPU memory isn't fully exhausted by the pipeline.

## Windows Server Rules
**CRITICAL: DO NOT DO DESTRUCTIVE OPERATIONS ON THE WINDOWS HOST (7900XTX).**
- Do NOT reboot the machine.
- Do NOT change network or global system settings.
- Do NOT uninstall or modify existing applications.
- Run everything locally within the project structure (venv, local installs) when possible.

## Code Reloading & Testing Rules
- **CRITICAL: Uvicorn Module Caching**: The background Uvicorn server runs without `--reload`. Modifying Python source files in `brain/` or `voice/` on disk does NOT automatically update in-memory modules of a running Uvicorn server.
- **Verification Rule**: Whenever core Python modules (`character_analyzer.py`, `script_generator.py`, `pipeline.py`) are modified:
  1. Either restart the Uvicorn server process so it re-imports the updated files, or
  2. Execute verification tests directly using `python.exe` with `$env:PYTHONPATH="."` to guarantee the test runs against the exact fresh code on disk.

## Android Companion APK Generation & Deployment Rules
**CRITICAL: Whenever compiling or generating a new Android APK (`app-free-debug.apk`):**
- **NEVER** leave the build output only inside `app/build/outputs/apk/free/debug/`. The user downloads the app directly from the 24/7 NAS streamer or the project roots.
- **MANDATORY**: You MUST immediately publish the new APK to all distribution endpoints after building:
  1. **24/7 Remote NAS Streamer**: `/mnt/nas/media/crazybooks/Voice-CrazyAudiobook-debug.apk` on `192.168.50.180` (served at `https://crazyha.mywire.org/bookplayer/Voice-CrazyAudiobook-debug.apk`)
  2. **Local Creator Root**: `e:\Projects\crazy-audiobook-creator\Voice-CrazyAudiobook-debug.apk` (served at `http://192.168.50.44:8000/api/mobile/v1/app`)
  3. **Local Voice Root**: `E:\Projects\Voice\Voice-CrazyAudiobook-debug.apk`
- **Standard Deployment Command**:
  - To deploy an already-built APK: `python scripts/deploy_voice_apk.py`
  - To build and deploy in one step: `python scripts/deploy_voice_apk.py --build`
- **Verification Rule**: Always verify that the NAS endpoint (`http://192.168.50.180:8005/Voice-CrazyAudiobook-debug.apk`) and dashboard endpoint (`http://127.0.0.1:8000/api/mobile/v1/app`) return HTTP 200/206 with the exact matching file size.

