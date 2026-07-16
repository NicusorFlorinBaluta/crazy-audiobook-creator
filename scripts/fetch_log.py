import os
import sys
try:
    from dotenv import load_dotenv
    import paramiko
except ImportError:
    print("Missing packages")
    sys.exit(1)

load_dotenv()
HOST = os.getenv("HA_SERVER_SSH_HOST", "192.168.50.180")
USER = os.getenv("HA_SERVER_SSH_USER", "crazywiz")
PASSWORD = os.getenv("HA_SERVER_SSH_PASSWORD", "")
REMOTE_DIR = "crazy-audiobook-creator"

def fetch_log():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(HOST, username=USER, password=PASSWORD, timeout=10)
        cmd = f"tail -n 50 ~/{REMOTE_DIR}/server.log"
        stdin, stdout, stderr = ssh.exec_command(cmd)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        print("--- STDOUT ---")
        try:
            print(out)
        except UnicodeEncodeError:
            print(out.encode("ascii", "replace").decode("ascii"))
        if err:
            print("--- STDERR ---")
            print(err)
    except Exception as e:
        print(f"Error: {e}")
    finally:
        ssh.close()

if __name__ == "__main__":
    fetch_log()
