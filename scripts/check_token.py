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

def check_token():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(HOST, username=USER, password=PASSWORD, timeout=10)
        stdin, stdout, stderr = ssh.exec_command("grep -i 'HF_TOKEN' ~/.bashrc ~/.profile ~/.bash_profile /etc/environment ~/.bash_history 2>/dev/null | head -n 10")
        out = stdout.read().decode()
        print("--- GREP ---")
        print(out)
    except Exception as e:
        print(f"Error: {e}")
    finally:
        ssh.close()

if __name__ == "__main__":
    check_token()
