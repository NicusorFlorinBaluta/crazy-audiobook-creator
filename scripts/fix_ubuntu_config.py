import paramiko

def fix():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect("192.168.50.180", username="crazywiz", password="xardas", timeout=10)
        # Update config
        cmd1 = "sed -i 's/-Base/-Instruct/g' /home/crazywiz/crazy-audiobook-creator/voice/config.yaml"
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
