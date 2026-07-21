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

print("Fixing Parler torch installation...")
commands = [
    "cd ~/crazy-audiobook-creator && source venv_parler/bin/activate && pip uninstall -y torch torchvision torchaudio",
    "cd ~/crazy-audiobook-creator && source venv_parler/bin/activate && pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121",
    "pkill -9 -f 'parler_server.py'"
]

for cmd in commands:
    print(f"Running: {cmd}")
    stdin, stdout, stderr = ssh.exec_command(cmd)
    print(stdout.read().decode())
    print(stderr.read().decode())

ssh.close()
print("Done!")
