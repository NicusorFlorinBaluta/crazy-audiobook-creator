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

print("Patching parler_server.py to use CPU...")
commands = [
    "sed -i 's/return \"cuda:0\" if torch.cuda.is_available() else \"cpu\"/return \"cpu\"/g' ~/crazy-audiobook-creator/parler_server.py",
    "pkill -9 -f 'parler_server.py'",
    "pkill -9 -f 'python -m voice.tts_server.main'"
]

for cmd in commands:
    print(f"Running: {cmd}")
    ssh.exec_command(cmd)

ssh.close()
print("Done! We killed the TTS server so the pipeline will retry.")
