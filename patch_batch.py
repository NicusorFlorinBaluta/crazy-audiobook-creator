import os
from pathlib import Path
import re

base_dir = Path("e:/Projects/crazy-audiobook-creator")

# 1. voice_designer.py
p1 = base_dir / "voice/tts_server/voice_designer.py"
c1 = p1.read_text(encoding="utf-8")
c1 = c1.replace("""        try:

        for char_id, character in request.characters.items():
            # Check if voice already exists and skip if not forcing regeneration
            if not request.force_regenerate and self.library.voice_exists(
                project_id, char_id
            ):
                existing = self.library.get_voice_info(project_id, char_id)
                if existing:
                    logger.info("Voice for '%s' already exists, skipping", char_id)
                    voices_generated[char_id] = BootstrapVoiceResult(
                        file=existing.get("file", ""),
                        duration_seconds=existing.get("duration_seconds", 0.0),
                        sample_rate=existing.get("sample_rate", 24000),
                    )
                    continue

            # Generate voice reference clip
            result = self._generate_voice(project_id, char_id, character)
            voices_generated[char_id] = result
        
        finally:""", """        try:
            for char_id, character in request.characters.items():
                # Check if voice already exists and skip if not forcing regeneration
                if not request.force_regenerate and self.library.voice_exists(
                    project_id, char_id
                ):
                    existing = self.library.get_voice_info(project_id, char_id)
                    if existing:
                        logger.info("Voice for '%s' already exists, skipping", char_id)
                        voices_generated[char_id] = BootstrapVoiceResult(
                            file=existing.get("file", ""),
                            duration_seconds=existing.get("duration_seconds", 0.0),
                            sample_rate=existing.get("sample_rate", 24000),
                        )
                        continue

                # Generate voice reference clip
                result = self._generate_voice(project_id, char_id, character)
                voices_generated[char_id] = result
        
        finally:""")
p1.write_text(c1, encoding="utf-8")

# 2. pipeline.py pass1_elapsed
p2 = base_dir / "brain/orchestrator/pipeline.py"
c2 = p2.read_text(encoding="utf-8")
c2 = c2.replace("""        t0 = time.time()

        # Pass 1: Character analysis""", """        t0 = time.time()
        pass1_elapsed = 0.0

        # Pass 1: Character analysis""")
# pipeline.py project_id
c2 = c2.replace("""        project_id = self._make_project_id(book.metadata.title)
        project_dir = self.projects_dir / project_id
        project_dir.mkdir(parents=True, exist_ok=True)""", """        project_id = self._make_project_id(book.metadata.title)
        
        # Ensure project ID is unique
        base_id = project_id
        counter = 1
        while True:
            try:
                self.job_queue.get_job(project_id)
                project_id = f"{base_id}-{counter}"
                counter += 1
            except KeyError:
                break

        project_dir = self.projects_dir / project_id
        project_dir.mkdir(parents=True, exist_ok=True)""")
p2.write_text(c2, encoding="utf-8")

# 3. watchdog.py
p3 = base_dir / "brain/orchestrator/watchdog.py"
c3 = p3.read_text(encoding="utf-8")
c3 = c3.replace("""        try:
            await asyncio.to_thread(self._execute_remote_restart)
            import time
            logger.info("Watchdog: Ubuntu Voice Server successfully restarted. Waiting 40s grace period for model load...")
            time.sleep(40)""", """        try:
            await asyncio.to_thread(self._execute_remote_restart)
            logger.info("Watchdog: Ubuntu Voice Server successfully restarted. Waiting 40s grace period for model load...")
            await asyncio.sleep(40)""")
p3.write_text(c3, encoding="utf-8")

# 4. dashboard main.py
p4 = base_dir / "brain/dashboard/api/main.py"
c4 = p4.read_text(encoding="utf-8")
c4 = c4.replace("""    temp_dir = Path("brain/projects/_uploads")
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / file.filename""", """    temp_dir = Path("brain/projects/_uploads")
    temp_dir.mkdir(parents=True, exist_ok=True)
    safe_filename = Path(file.filename).name
    temp_path = temp_dir / safe_filename""")
c4 = c4.replace("""    if project_id in running_tasks and not running_tasks[project_id].done():
        # Optionally wait for it to cancel or just cancel it
        running_tasks[project_id].cancel()""", """    if project_id in running_tasks and not running_tasks[project_id].done():
        pipeline.stop(project_id)""")
c4 = c4.replace("""    data = await request.json()
    stage = data.get("stage")
    if not stage:
        raise HTTPException(status_code=400, detail="Missing 'stage' in request body")
        
    try:
        job_queue.update_job(project_id, {"status": stage})
        return {"status": "success", "project_id": project_id, "stage": stage}""", """    data = await request.json()
    stage_value = data.get("stage")
    if not stage_value:
        raise HTTPException(status_code=400, detail="Missing 'stage' in request body")
        
    try:
        stage = PipelineStage(stage_value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid stage: {stage_value}")
        
    try:
        job_queue.update_job(project_id, {"status": stage.value})
        return {"status": "success", "project_id": project_id, "stage": stage.value}""")
p4.write_text(c4, encoding="utf-8")

# 5. voice server main.py
p5 = base_dir / "voice/tts_server/main.py"
c5 = p5.read_text(encoding="utf-8")
c5 = c5.replace("""    workspace = Path(config.get("storage", {}).get("workspace_dir", "workspace"))
    file_path = workspace / project_id / path""", """    workspace = Path(config.get("storage", {}).get("workspace_dir", "workspace")).resolve()
    project_dir = (workspace / project_id).resolve()
    
    file_path = (project_dir / path).resolve()
    if not file_path.is_relative_to(project_dir):
        raise HTTPException(status_code=403, detail="Access denied")""")
p5.write_text(c5, encoding="utf-8")

# 6. check_remote_m4b.py
p6 = base_dir / "scripts/check_remote_m4b.py"
c6 = p6.read_text(encoding="utf-8")
c6 = c6.replace("""import paramiko

def check():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("192.168.50.180", username="crazywiz", password="xardas", timeout=10)""", """import os
from dotenv import load_dotenv
import paramiko

def check():
    load_dotenv()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("192.168.50.180", username="crazywiz", password=os.getenv("HA_SERVER_SSH_PASSWORD"), timeout=10)""")
p6.write_text(c6, encoding="utf-8")

# 7. fix_ubuntu_config.py
p7 = base_dir / "scripts/fix_ubuntu_config.py"
c7 = p7.read_text(encoding="utf-8")
c7 = c7.replace("""import paramiko

def fix():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect("192.168.50.180", username="crazywiz", password="xardas", timeout=10)""", """import os
from dotenv import load_dotenv
import paramiko

def fix():
    load_dotenv()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect("192.168.50.180", username="crazywiz", password=os.getenv("HA_SERVER_SSH_PASSWORD"), timeout=10)""")
p7.write_text(c7, encoding="utf-8")
