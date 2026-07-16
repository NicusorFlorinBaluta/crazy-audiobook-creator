import paramiko
import os
from dotenv import load_dotenv

load_dotenv()
HOST = os.getenv("HA_SERVER_SSH_HOST", "192.168.50.180")
USER = os.getenv("HA_SERVER_SSH_USER", "crazywiz")
PASSWORD = os.getenv("HA_SERVER_SSH_PASSWORD", "")

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASSWORD)

sftp = ssh.open_sftp()
local_path = "voice/tts_server/qwen3_engine.py"
remote_path = "/home/crazywiz/crazy-audiobook-creator/voice/tts_server/qwen3_engine.py"

print(f"Uploading {local_path} to {remote_path}...")
sftp.put(local_path, remote_path)
sftp.close()

# Restart the server
print("Restarting voice server...")
ssh.exec_command("pkill -f 'python -m voice.tts_server.main'")
cmd = f"cd ~/crazy-audiobook-creator && source venv/bin/activate && nohup nice -n 10 python -m voice.tts_server.main > server.log 2>&1 &"
ssh.exec_command(cmd)
print("Restarted.")
ssh.close()
