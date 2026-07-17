import paramiko

def check():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("192.168.50.180", username="crazywiz", password="xardas", timeout=10)
    
    _, stdout, _ = client.exec_command("cd ~/crazy-audiobook-creator && git pull && cat voice/mastering/m4b_exporter.py")
    content = stdout.read().decode('utf-8', errors='replace')
    
    print(content)
            
    client.close()

if __name__ == "__main__":
    check()
