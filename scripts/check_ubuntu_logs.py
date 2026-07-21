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

stdin, stdout, stderr = ssh.exec_command("tail -n 20 ~/crazy-audiobook-creator/server.log")
print("Ubuntu Log Tail:\n" + stdout.read().decode("utf-8", errors="replace").encode("ascii", "ignore").decode("ascii"))

ssh.close()
