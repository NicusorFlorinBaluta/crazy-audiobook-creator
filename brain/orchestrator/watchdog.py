import asyncio
import logging
import os
import subprocess
from typing import Optional

import httpx
import paramiko
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

class ServiceWatchdog:
    """Background watchdog service that monitors local Ollama and remote Voice Server.
    Restarts them safely if they become unresponsive.
    """
    def __init__(self, check_interval_seconds: int = 60):
        self.check_interval = check_interval_seconds
        self.running = False
        self._task: Optional[asyncio.Task] = None
        
        load_dotenv()
        self.ubuntu_host = os.getenv("HA_SERVER_SSH_HOST", "192.168.50.180")
        self.ubuntu_user = os.getenv("HA_SERVER_SSH_USER", "crazywiz")
        self.ubuntu_password = os.getenv("HA_SERVER_SSH_PASSWORD", "")
        self.ubuntu_dir = "crazy-audiobook-creator"

        self.voice_server_url = f"http://{self.ubuntu_host}:8100/health"
        self.ollama_url = "http://localhost:11434"
        
        self.client = httpx.AsyncClient(timeout=10.0)
        
        # Prevent rapid loop restarts if a service is taking a long time to boot
        self._ollama_restarting = False
        self._voice_restarting = False
        
        # Max restart limits to prevent infinite loops
        self.max_restarts = 5
        self._ollama_restart_count = 0
        self._voice_restart_count = 0

        # Setup dedicated file logger for Watchdog events
        if not any(isinstance(h, logging.FileHandler) for h in logger.handlers):
            file_handler = logging.FileHandler("watchdog.log")
            file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
            logger.addHandler(file_handler)

    def start(self):
        """Start the watchdog background loop."""
        if not self.running:
            self.running = True
            self._task = asyncio.create_task(self._loop())
            logger.info("Service Watchdog started. Checking every %d seconds.", self.check_interval)

    async def stop(self):
        """Gracefully stop the watchdog."""
        if self.running:
            self.running = False
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            await self.client.aclose()
            logger.info("Service Watchdog stopped.")

    async def _loop(self):
        while self.running:
            try:
                if not self._ollama_restarting:
                    await self._check_ollama()
                
                if not self._voice_restarting:
                    await self._check_voice_server()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Watchdog unexpected error: %s", e)
            
            await asyncio.sleep(self.check_interval)

    async def _check_ollama(self):
        """Ping local Ollama. Restart via PowerShell if dead."""
        try:
            response = await self.client.get(self.ollama_url)
            response.raise_for_status()
            # Reset counter on successful health check
            if self._ollama_restart_count > 0:
                logger.info("Watchdog: Ollama is healthy again. Resetting restart counter.")
                self._ollama_restart_count = 0
        except httpx.RequestError as e:
            if self._ollama_restart_count >= self.max_restarts:
                logger.error("Watchdog: Ollama is down! Max restarts (%d) reached. Giving up.", self.max_restarts)
                return
            logger.warning("Watchdog: Ollama is down! (%s) Restarting...", e)
            self._ollama_restarting = True
            self._ollama_restart_count += 1
            # Fire and forget the restart so we don't block the watchdog completely
            asyncio.create_task(self._restart_ollama_task())

    async def _restart_ollama_task(self):
        try:
            # Run PowerShell command locally to kill and restart ollama serve
            cmd = (
                'Stop-Process -Name "ollama*" -Force; '
                'Start-Sleep -Seconds 2; '
                '$env:OLLAMA_MODELS="E:\\.ollama\\models"; '
                'Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden'
            )
            # Use to_thread since subprocess.run is blocking
            await asyncio.to_thread(subprocess.run, ["powershell", "-Command", cmd], check=False)
            logger.info("Watchdog: Ollama successfully restarted.")
        except Exception as e:
            logger.error("Watchdog: Failed to restart Ollama: %s", e)
        finally:
            # Wait a bit before allowing checks again
            await asyncio.sleep(10)
            self._ollama_restarting = False

    async def _check_voice_server(self):
        """Ping remote Voice Server. Restart via SSH if dead."""
        try:
            response = await self.client.get(self.voice_server_url)
            response.raise_for_status()
            # Reset counter on successful health check
            if self._voice_restart_count > 0:
                logger.info("Watchdog: Ubuntu Voice Server is healthy again. Resetting restart counter.")
                self._voice_restart_count = 0
        except httpx.RequestError as e:
            if self._voice_restart_count >= self.max_restarts:
                logger.error("Watchdog: Ubuntu Voice Server is down! Max restarts (%d) reached. Giving up.", self.max_restarts)
                return
            logger.warning("Watchdog: Ubuntu Voice Server is down! (%s) Restarting via SSH...", e)
            self._voice_restarting = True
            self._voice_restart_count += 1
            asyncio.create_task(self._restart_voice_server_task())

    async def _restart_voice_server_task(self):
        try:
            await asyncio.to_thread(self._execute_remote_restart)
            import time
            logger.info("Watchdog: Ubuntu Voice Server successfully restarted. Waiting 40s grace period for model load...")
            time.sleep(40)
        except Exception as e:
            logger.error("Watchdog: Failed to restart Voice Server via SSH: %s", e)
        finally:
            # Give the heavy PyTorch model ~30-60 seconds to compile and bind the port
            await asyncio.sleep(30)
            self._voice_restarting = False

    def _execute_remote_restart(self):
        """Blocking SSH code to gracefully restart the remote Voice Server"""
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh.connect(
                self.ubuntu_host, 
                username=self.ubuntu_user, 
                password=self.ubuntu_password, 
                timeout=10
            )
            
            # Kill exactly the python module being run, ensuring other processes remain untouched
            kill_cmd = "pkill -9 -f 'voice.tts_server.main'"
            ssh.exec_command(kill_cmd)
            
            import time
            time.sleep(2)
            
            # Relaunch the module isolated within the project directory using its venv
            start_cmd = f"cd ~/{self.ubuntu_dir} && nohup ./venv/bin/python -m voice.tts_server.main > server.log 2>&1 &"
            ssh.exec_command(start_cmd)
            
        finally:
            ssh.close()
