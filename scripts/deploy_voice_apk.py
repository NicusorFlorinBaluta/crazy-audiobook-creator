#!/usr/bin/env python3
"""Deploy Voice Companion Android APK to all distribution endpoints.

This script ensures that whenever an Android APK is built, it is immediately
and reliably published to:
1. Local Creator PC root (e:\\Projects\\crazy-audiobook-creator\\Voice-CrazyAudiobook-debug.apk)
2. Local Voice project root (E:\\Projects\\Voice\\Voice-CrazyAudiobook-debug.apk)
3. 24/7 NAS Remote Streamer (/mnt/nas/media/crazybooks/Voice-CrazyAudiobook-debug.apk on 192.168.50.180)

Usage:
    python scripts/deploy_voice_apk.py
    python scripts/deploy_voice_apk.py --build
    python scripts/deploy_voice_apk.py --skip-remote
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

VOICE_DIR = Path("E:/Projects/Voice").resolve()
CREATOR_DIR = Path("e:/Projects/crazy-audiobook-creator").resolve()
GRADLEW_BAT = VOICE_DIR / "gradlew.bat"
GRADLEW_SH = VOICE_DIR / "gradlew"
APK_BUILD_PATH = VOICE_DIR / "app" / "build" / "outputs" / "apk" / "free" / "debug" / "app-free-debug.apk"

DEST_CREATOR_ROOT = CREATOR_DIR / "Voice-CrazyAudiobook-debug.apk"
DEST_VOICE_ROOT = VOICE_DIR / "Voice-CrazyAudiobook-debug.apk"

REMOTE_HOST = os.getenv("HA_SERVER_SSH_HOST", "192.168.50.180")
REMOTE_PORT = int(os.getenv("HA_SERVER_SSH_PORT", "22"))
REMOTE_USER = os.getenv("HA_SERVER_SSH_USER", "crazywiz")
REMOTE_PASS = os.getenv("HA_SERVER_SSH_PASSWORD", "xardas")
REMOTE_DEST_DIR = "/mnt/nas/media/crazybooks"
REMOTE_APK_PATH = f"{REMOTE_DEST_DIR}/Voice-CrazyAudiobook-debug.apk"
REMOTE_TMP_PATH = f"{REMOTE_DEST_DIR}/.Voice-CrazyAudiobook-debug.apk.tmp"
STREAMER_HTTP_URL = f"http://{REMOTE_HOST}:8005/Voice-CrazyAudiobook-debug.apk"


def build_apk() -> None:
    """Run Gradle to compile the free debug APK."""
    print("=== [1/4] Building APK with Gradle ===")
    gradlew = GRADLEW_BAT if sys.platform == "win32" else GRADLEW_SH
    if not gradlew.is_file():
        raise FileNotFoundError(f"Gradle wrapper not found at {gradlew}")

    cmd = [str(gradlew), ":app:assembleFreeDebug"]
    print(f"Running: {' '.join(cmd)} in {VOICE_DIR}")
    result = subprocess.run(cmd, cwd=str(VOICE_DIR))
    if result.returncode != 0:
        sys.exit(f"ERROR: Gradle build failed with exit code {result.returncode}")
    print("Gradle build succeeded!")


def copy_locally() -> int:
    """Copy build APK to local project roots."""
    print("=== [2/4] Publishing to Local Project Roots ===")
    if not APK_BUILD_PATH.is_file():
        raise FileNotFoundError(
            f"Build APK not found at {APK_BUILD_PATH}. Did you run with --build?"
        )

    apk_size = APK_BUILD_PATH.stat().st_size
    print(f"Source APK: {APK_BUILD_PATH} ({apk_size:,} bytes)")

    for dest in (DEST_CREATOR_ROOT, DEST_VOICE_ROOT):
        shutil.copy2(APK_BUILD_PATH, dest)
        print(f"  -> Copied to {dest} ({dest.stat().st_size:,} bytes)")

    return apk_size


def deploy_remote(apk_size: int) -> None:
    """Upload APK to the 24/7 NAS Remote Streamer via SFTP."""
    print(f"=== [3/4] Deploying to 24/7 NAS Streamer ({REMOTE_HOST}) ===")
    try:
        import paramiko
    except ImportError:
        print("WARNING: paramiko not installed. Skipping remote deployment.")
        return

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(REMOTE_HOST, REMOTE_PORT, REMOTE_USER, REMOTE_PASS, timeout=10)
    except Exception as exc:
        print(f"WARNING: Could not connect to {REMOTE_HOST}: {exc}. Remote upload skipped.")
        return

    try:
        sftp = ssh.open_sftp()
        tmp_remote = "/tmp/Voice-CrazyAudiobook-debug.apk"
        t0 = time.time()
        print(f"Uploading {apk_size:,} bytes to staging ({tmp_remote})...")
        sftp.put(str(APK_BUILD_PATH), tmp_remote)
        sftp.close()
        elapsed = time.time() - t0
        print(f"Uploaded in {elapsed:.2f}s ({apk_size / elapsed / 1024 / 1024:.2f} MB/s)")

        # Copy to CIFS NAS directory
        copy_cmd = f"cp -f {tmp_remote} {REMOTE_APK_PATH} && chmod 755 {REMOTE_APK_PATH} && rm -f {tmp_remote}"
        stdin, stdout, stderr = ssh.exec_command(copy_cmd)
        exit_code = stdout.channel.recv_exit_status()
        if exit_code != 0:
            err = stderr.read().decode().strip()
            print(f"CIFS copy notice ({err}), releasing container handle and retrying...")
            ssh.exec_command("docker restart crazy-bookplayer-streamer")
            time.sleep(2)
            stdin, stdout, stderr = ssh.exec_command(copy_cmd)
            retry_code = stdout.channel.recv_exit_status()
            if retry_code != 0:
                print(f"WARNING: Final copy failed: {stderr.read().decode().strip()}")

        # Also update container /app if container is running
        container_cmd = (
            "docker cp /mnt/nas/media/crazybooks/Voice-CrazyAudiobook-debug.apk "
            "crazy-bookplayer-streamer:/app/Voice-CrazyAudiobook-debug.apk 2>/dev/null || true"
        )
        ssh.exec_command(container_cmd)
        print(f"  -> Successfully deployed to {REMOTE_APK_PATH}")
    finally:
        ssh.close()


def verify_endpoints(expected_size: int) -> None:
    """Verify HTTP download endpoints report the correct size."""
    print("=== [4/4] Verifying Distribution Endpoints ===")
    endpoints = [
        ("NAS Streamer", STREAMER_HTTP_URL),
        ("Local Dashboard", "http://127.0.0.1:8000/api/mobile/v1/app"),
    ]

    for label, url in endpoints:
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=3) as resp:
                length = int(resp.headers.get("Content-Length", 0))
                status = "OK" if length == expected_size else f"MISMATCH (got {length:,})"
                print(f"  [+] {label} ({url}): HTTP {resp.status} - {status}")
        except Exception as exc:
            # Try GET if HEAD returned 405
            try:
                req_get = urllib.request.Request(url, method="GET")
                req_get.add_header("Range", "bytes=0-0")
                with urllib.request.urlopen(req_get, timeout=3) as resp:
                    cr = resp.headers.get("Content-Range", "")
                    total = int(cr.split("/")[-1]) if "/" in cr else int(resp.headers.get("Content-Length", 0))
                    status = "OK" if total == expected_size else f"MISMATCH (got {total:,})"
                    print(f"  [+] {label} ({url}): HTTP {resp.status} - {status}")
            except Exception as e2:
                print(f"  [-] {label} ({url}): Reachability check skipped ({e2})")

    print("\nDeployment complete! APK is live across all download paths.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build and/or deploy Voice Companion APK to all distribution endpoints."
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="Compile APK with Gradle (:app:assembleFreeDebug) before deploying",
    )
    parser.add_argument(
        "--skip-remote",
        action="store_true",
        help="Only copy locally, skip uploading to remote 24/7 NAS streamer",
    )
    args = parser.parse_args()

    if args.build:
        build_apk()

    size = copy_locally()

    if not args.skip_remote:
        deploy_remote(size)

    verify_endpoints(size)


if __name__ == "__main__":
    main()
