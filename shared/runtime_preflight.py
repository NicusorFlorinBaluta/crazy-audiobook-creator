"""Read-only runtime compatibility reporting without importing GPU models."""

from __future__ import annotations

import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

PACKAGE_NAMES = (
    "torch",
    "torchaudio",
    "transformers",
    "qwen-tts",
    "openai-whisper",
    "faster-whisper",
    "ctranslate2",
    "silero-vad",
    "fastapi",
    "uvicorn",
    "tzdata",
    "soundfile",
    "numpy",
    "scipy",
)


# Packages whose version directly changes generated audio or transcription, and
# which must therefore be pinned for a run to be reproducible and comparable to
# a recorded benchmark.
AUDIO_CRITICAL_PACKAGES = (
    "qwen-tts",
    "transformers",
    "openai-whisper",
    "faster-whisper",
    "silero-vad",
)


def _package_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in PACKAGE_NAMES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not-installed"
    return versions


def _parse_constraints(path: Path) -> dict[str, str]:
    """Read `name==version` pins from a pip constraints file."""
    pins: dict[str, str] = {}
    if not path.is_file():
        return pins
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or "==" not in line:
            continue
        name, _, version = line.partition("==")
        name = name.strip().lower().replace("_", "-")
        version = version.strip()
        if name and version:
            pins[name] = version
    return pins


def _constraint_findings(
    packages: dict[str, str],
    constraints_path: Path,
) -> list[str]:
    """Report audio-critical packages that are unpinned or drifting.

    An unpinned `qwen-tts` can change voice cloning between two runs of the same
    book with no fingerprint change, so a recorded benchmark stops being
    attributable to a stack. Reporting it here surfaces the gap on the machine
    that actually has the package installed.
    """
    findings: list[str] = []
    pins = _parse_constraints(constraints_path)
    if not pins:
        findings.append(
            f"No tested version constraints found at {constraints_path}; "
            "generated audio is not reproducible across reinstalls"
        )
        return findings
    for name in AUDIO_CRITICAL_PACKAGES:
        installed = packages.get(name, "not-installed")
        if installed == "not-installed":
            continue
        pinned = pins.get(name.lower().replace("_", "-"))
        if pinned is None:
            findings.append(
                f"{name}=={installed} is installed but not pinned in "
                f"{constraints_path.name}; add it so the audio stack is "
                "reproducible"
            )
        elif pinned != installed:
            findings.append(
                f"{name} is {installed} but {constraints_path.name} pins "
                f"{pinned}; benchmark results are not comparable across this "
                "difference"
            )
    return findings


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def collect_runtime_report(
    *,
    brain_config_path: str | Path = "brain/config.yaml",
    voice_config_path: str | Path = "voice/config.yaml",
    constraints_path: str | Path = "voice/constraints-windows-rocm-tested.txt",
    run_pip_check: bool = False,
) -> dict[str, Any]:
    """Collect a non-model-loading environment/config compatibility report."""
    brain_config = _load_yaml(Path(brain_config_path))
    voice_config = _load_yaml(Path(voice_config_path))
    voice_server = brain_config.get("voice_server", {})
    tts = voice_config.get("tts", {})
    validation = voice_config.get("validation", {})
    backend = str(validation.get("whisper_backend", "auto"))
    device = str(validation.get("whisper_device", "auto"))
    packages = _package_versions()
    torch_version = packages.get("torch", "")
    amd_rocm_profile = "rocm" in torch_version.lower()

    errors: list[str] = []
    warnings: list[str] = []
    if amd_rocm_profile and backend == "faster_whisper" and device != "cpu":
        errors.append(
            "faster_whisper GPU mode is unsupported for the tested Windows "
            "AMD/ROCm profile; select openai_whisper or explicitly use CPU"
        )
    if amd_rocm_profile and backend == "auto":
        errors.append(
            "Automatic Whisper backend selection is unsafe on the tested "
            "Windows AMD/ROCm profile; select openai_whisper explicitly"
        )
    if validation.get("whisper_vad_filter", False):
        warnings.append(
            "VAD is enabled; the 2026-08-09 E2E showed false negatives for short, high-pitched, and repeated speech"
        )
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg:
        errors.append("FFmpeg is not available on PATH")
    if not ffprobe:
        warnings.append("ffprobe is not available; export verification is limited")
    warnings.extend(_constraint_findings(packages, Path(constraints_path)))

    pip_check: dict[str, Any] = {"ran": False, "ok": None, "output": ""}
    if run_pip_check:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "check"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        output = (result.stdout + result.stderr).strip()
        pip_check = {
            "ran": True,
            "ok": result.returncode == 0,
            "output": output[-4000:],
        }
        if result.returncode != 0:
            errors.append("pip check reported incompatible dependencies")

    return {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "compatible": not errors,
        "python": {
            "executable": sys.executable,
            "version": platform.python_version(),
            "platform": platform.platform(),
        },
        "packages": packages,
        "effective_runtime": {
            "voice_venv": str(voice_server.get("venv", "")),
            "tts_model": str(tts.get("model", "")),
            "tts_device": str(tts.get("device", "")),
            "tts_dtype": str(tts.get("dtype", "")),
            "attention_backend": str(tts.get("attn_implementation", "")),
            "whisper_model": str(validation.get("whisper_model", "")),
            "whisper_backend": backend,
            "whisper_device": device,
            "whisper_vad_filter": bool(validation.get("whisper_vad_filter", False)),
            "amd_rocm_profile": amd_rocm_profile,
        },
        "executables": {"ffmpeg": ffmpeg, "ffprobe": ffprobe},
        "audio_stack_pins": _parse_constraints(Path(constraints_path)),
        "pip_check": pip_check,
        "errors": errors,
        "warnings": warnings,
    }


def write_runtime_report(path: str | Path, report: dict[str, Any]) -> Path:
    """Write a report to a caller-selected path."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output
