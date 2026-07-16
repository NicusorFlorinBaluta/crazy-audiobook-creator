import os
import sys

try:
    from dotenv import load_dotenv
    import paramiko
except ImportError:
    print("Please install python-dotenv and paramiko")
    sys.exit(1)

load_dotenv()

HOST = os.getenv("HA_SERVER_SSH_HOST", "192.168.50.180")
USER = os.getenv("HA_SERVER_SSH_USER", "crazywiz")
PASSWORD = os.getenv("HA_SERVER_SSH_PASSWORD", "")
REMOTE_DIR = "crazy-audiobook-creator"

def start_server():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(HOST, username=USER, password=PASSWORD, timeout=10)
        
        # Kill any existing instance first
        ssh.exec_command("pkill -f 'python -m voice.tts_server.main'")
        
        # Start server with nice -n 10 in the background
        cmd = f"cd ~/{REMOTE_DIR} && source venv/bin/activate && nohup nice -n 10 python -m voice.tts_server.main > server.log 2>&1 &"
        stdin, stdout, stderr = ssh.exec_command(cmd)
        
        print("Voice server started on Ubuntu in the background (nice -n 10).")
    except Exception as e:
        print(f"Failed to start server: {e}")
    finally:
        ssh.close()

if __name__ == "__main__":
    start_server()
