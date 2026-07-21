import paramiko
import os
import time
from dotenv import load_dotenv

load_dotenv()

HOST = os.getenv("HA_SERVER_SSH_HOST", "192.168.50.180")
USER = os.getenv("HA_SERVER_SSH_USER", "crazywiz")
PASSWORD = os.getenv("HA_SERVER_SSH_PASSWORD", "")

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASSWORD)

sftp = ssh.open_sftp()
sftp.put("e:/Projects/crazy-audiobook-creator/voice/tts_server/voice_designer.py", f"/home/{USER}/crazy-audiobook-creator/voice/tts_server/voice_designer.py")
sftp.close()
print("Uploaded voice_designer.py")

print("Killing running TTS server and Parler...")
ssh.exec_command("pkill -9 -f 'python -m voice.tts_server.main'")
ssh.exec_command("pkill -9 -f 'parler_server.py'")
print("Waiting 10 seconds for VRAM to clear...")
time.sleep(10)

print("Starting TTS server in background...")
stdin, stdout, stderr = ssh.exec_command("cd ~/crazy-audiobook-creator && source venv/bin/activate && nohup nice -n 10 python -m voice.tts_server.main > tts.log 2>&1 &")
time.sleep(2)

ssh.close()
print("Done!")
