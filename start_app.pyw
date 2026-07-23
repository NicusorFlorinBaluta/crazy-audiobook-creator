import subprocess
import time
import webbrowser
import sys
import os

def main():
    # Base directory of script
    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    # 0. Kill any existing server processes on ports 8000 & 8100
    if os.name == 'nt':
        try:
            subprocess.run(["powershell", "-Command", "Stop-Process -Id (Get-NetTCPConnection -LocalPort 8000,8100 -ErrorAction SilentlyContinue).OwningProcess -Force -ErrorAction SilentlyContinue"], capture_output=True)
        except Exception:
            pass

    # 8.3 Short path Python executable to avoid ROCm spaces path bug
    python_exe = r"E:\PYTORC~1\my_venv\Scripts\python.exe"
    if not os.path.exists(python_exe):
        python_exe = sys.executable

    cmd = [
        python_exe,
        "-m", "uvicorn",
        "brain.dashboard.api.main:app",
        "--host", "127.0.0.1",
        "--port", "8000"
    ]
    
    # Launch uvicorn silently without creating a console window on Windows
    kwargs = {}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

    subprocess.Popen(cmd, cwd=base_dir, **kwargs)
    
    # Wait 2 seconds for FastAPI server startup
    time.sleep(2)
    
    # Open default browser to localhost
    webbrowser.open("http://127.0.0.1:8000")

if __name__ == '__main__':
    main()
