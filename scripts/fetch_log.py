import paramiko

def fetch():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("192.168.50.180", username="crazywiz", password="xardas", timeout=10)
    
    # Read the last 100 lines of server.log
    _, stdout, _ = client.exec_command("tail -n 50 ~/crazy-audiobook-creator/server.log")
    
    # Safely print ignoring unicode errors
    print(stdout.read().decode('utf-8', errors='replace'))
    client.close()

if __name__ == "__main__":
    fetch()
