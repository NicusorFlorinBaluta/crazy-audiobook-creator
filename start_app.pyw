import subprocess
import time
import webbrowser
import sys
import os
import socket


def is_listening(host, port):
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False

def main():
    # Base directory of script
    base_dir = os.path.dirname(os.path.abspath(__file__))

    # Reuse an existing dashboard. Never terminate arbitrary processes that
    # happen to own the configured ports.
    if is_listening("127.0.0.1", 8000):
        webbrowser.open("http://127.0.0.1:8000")
        return

    candidates = [
        os.environ.get("CAC_PYTHON"),
        os.path.join(base_dir, "venv", "Scripts", "python.exe"),
        sys.executable,
    ]
    python_exe = next(
        candidate for candidate in candidates if candidate and os.path.exists(candidate)
    )

    cmd = [
        python_exe,
        "-m", "uvicorn",
        "brain.dashboard.api.main:app",
        "--host", "0.0.0.0",
        "--port", "8000"
    ]
    
    # Launch uvicorn silently without creating a console window on Windows
    kwargs = {}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

    child_env = os.environ.copy()
    child_env.setdefault("ROCM_SDK_TARGET_FAMILY", "custom")
    subprocess.Popen(cmd, cwd=base_dir, env=child_env, **kwargs)
    
    # Wait 2 seconds for FastAPI server startup
    time.sleep(2)
    
    # Open default browser to localhost
    webbrowser.open("http://127.0.0.1:8000")

if __name__ == '__main__':
    main()
