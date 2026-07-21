"""Silent desktop launcher for Crazy Audiobook Creator."""
import os
import sys
import subprocess
import time
import webbrowser
from pathlib import Path

def main():
    project_root = Path(__file__).parent.resolve()
    os.chdir(project_root)

    # Use current python executable or venv
    venv_py = project_root / "venv" / "Scripts" / "python.exe"
    python_exe = str(venv_py) if venv_py.exists() else sys.executable

    cmd = [python_exe, "-m", "uvicorn", "brain.dashboard.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
    
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(cmd, cwd=str(project_root), creationflags=creationflags)

    # Poll for server readiness before opening browser
    import urllib.request
    max_wait_seconds = 15
    start = time.time()
    server_ready = False

    while time.time() - start < max_wait_seconds:
        if proc.poll() is not None:
            # Process exited early
            break
        try:
            with urllib.request.urlopen("http://localhost:8000/api/projects", timeout=1) as resp:
                if resp.status == 200:
                    server_ready = True
                    break
        except Exception:
            pass
        time.sleep(0.5)

    if server_ready or proc.poll() is None:
        webbrowser.open("http://localhost:8000")

if __name__ == "__main__":
    main()
