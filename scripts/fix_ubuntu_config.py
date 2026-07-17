import os
from dotenv import load_dotenv
import paramiko

def fix():
    load_dotenv()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect("192.168.50.180", username="crazywiz", password=os.getenv("HA_SERVER_SSH_PASSWORD"), timeout=10)
        # Update m4b_exporter.py
        cmd1 = "sed -i 's/result.stderr\\[:500\\]/result.stderr\\[-1000:\\]/g' /home/crazywiz/crazy-audiobook-creator/voice/mastering/m4b_exporter.py"
        client.exec_command(cmd1)
        
        # Kill the voice server so the Watchdog on Windows or the run.sh script restarts it
        cmd2 = "pkill -f 'voice.tts_server.main'"
        client.exec_command(cmd2)
        print("Successfully updated config and killed old server.")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        client.close()

if __name__ == "__main__":
    fix()
