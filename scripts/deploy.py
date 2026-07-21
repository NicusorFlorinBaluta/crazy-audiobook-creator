import os
import stat
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    import paramiko
except ImportError:
    print("Please install required packages: pip install python-dotenv paramiko")
    sys.exit(1)

# Load environment variables
load_dotenv()

HOST = os.getenv("HA_SERVER_SSH_HOST", "192.168.50.180")
USER = os.getenv("HA_SERVER_SSH_USER", "crazywiz")
PASSWORD = os.getenv("HA_SERVER_SSH_PASSWORD", "")

if not PASSWORD:
    print("Error: HA_SERVER_SSH_PASSWORD not found in .env")
    sys.exit(1)

REMOTE_DIR = "crazy-audiobook-creator"

def create_sftp_dir(sftp, remote_directory):
    """Create directory structure on SFTP."""
    if remote_directory == '/':
        sftp.chdir('/')
        return
    if remote_directory == '':
        return
    try:
        sftp.chdir(remote_directory)
    except IOError:
        dirname, basename = os.path.split(remote_directory.rstrip('/'))
        create_sftp_dir(sftp, dirname)
        sftp.mkdir(basename)
        sftp.chdir(basename)

def deploy():
    print(f"Connecting to {USER}@{HOST}...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        ssh.connect(HOST, username=USER, password=PASSWORD, timeout=10)
        print("Connected successfully!")
        
        # Ensure remote directory exists
        stdin, stdout, stderr = ssh.exec_command(f"mkdir -p ~/{REMOTE_DIR}")
        stdout.read()
        
        sftp = ssh.open_sftp()
        sftp.chdir(REMOTE_DIR)
        
        # Files and directories to transfer
        local_dir = Path(__file__).parent.parent
        transfer_dirs = ["voice", "shared", "scripts"]
        
        print("Transferring files...")
        
        # Upload parler_server.py
        sftp.put(str(local_dir / "parler_server.py"), f"/home/{USER}/{REMOTE_DIR}/parler_server.py")
        print("  Copied parler_server.py")
        
        for t_dir in transfer_dirs:
            for root, dirs, files in os.walk(local_dir / t_dir):
                if "__pycache__" in root:
                    continue
                
                rel_path = Path(root).relative_to(local_dir).as_posix()
                create_sftp_dir(sftp, rel_path)
                sftp.chdir(f"/home/{USER}/{REMOTE_DIR}")
                
                for file in files:
                    local_path = os.path.join(root, file)
                    remote_path = f"/home/{USER}/{REMOTE_DIR}/{rel_path}/{file}"
                    sftp.put(local_path, remote_path)
                    print(f"  Copied {rel_path}/{file}")

        # Make install script executable
        sftp.chmod(f"/home/{USER}/{REMOTE_DIR}/scripts/install-ubuntu.sh", stat.S_IRWXU | stat.S_IRGRP | stat.S_IROTH)
        sftp.close()
        
        print("\nFiles transferred. Running safe localized setup (install-ubuntu.sh)...")
        # Run install script
        stdin, stdout, stderr = ssh.exec_command(
            f"cd ~/{REMOTE_DIR} && ./scripts/install-ubuntu.sh"
        )
        
        for line in iter(stdout.readline, ""):
            try:
                print(line, end="")
            except UnicodeEncodeError:
                print(line.encode('ascii', 'ignore').decode('ascii'), end="")
        for line in iter(stderr.readline, ""):
            try:
                print(f"ERROR: {line}", end="")
            except UnicodeEncodeError:
                print(f"ERROR: {line.encode('ascii', 'ignore').decode('ascii')}", end="")
            
        print("\nSetup complete! The Voice server can now be started.")
        print("To run the voice server with restricted priority (nice):")
        print(f"  ssh {USER}@{HOST}")
        print(f"  cd {REMOTE_DIR}")
        print("  source venv/bin/activate")
        print("  nice -n 10 python -m voice.tts_server.main")
        
    except Exception as e:
        print(f"Deployment failed: {e}")
    finally:
        ssh.close()

if __name__ == "__main__":
    deploy()
