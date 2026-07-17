import os
import sys
try:
    from dotenv import load_dotenv
    import paramiko
except ImportError:
    sys.exit(1)

load_dotenv()
HOST = os.getenv("HA_SERVER_SSH_HOST", "192.168.50.180")
USER = os.getenv("HA_SERVER_SSH_USER", "crazywiz")
PASSWORD = os.getenv("HA_SERVER_SSH_PASSWORD", "")

def check_token_file():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(HOST, username=USER, password=PASSWORD, timeout=10)
        stdin, stdout, stderr = ssh.exec_command("cat ~/.cache/huggingface/token")
        out = stdout.read().decode()
        print("--- TOKEN FILE ---")
        print(out)
    except Exception as e:
        print(f"Error: {e}")
    finally:
        ssh.close()

if __name__ == "__main__":
    check_token_file()
