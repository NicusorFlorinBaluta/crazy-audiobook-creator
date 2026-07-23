import asyncio
import logging
import os
import subprocess
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

class ServiceWatchdog:
    """Background watchdog service that monitors local Ollama and local Voice Server.
    Restarts them safely if they become unresponsive.
    """
    def __init__(self, check_interval_seconds: int = 60):
        self.check_interval = check_interval_seconds
        self.running = False
        self._task: Optional[asyncio.Task] = None
        
        self.voice_server_url = "http://127.0.0.1:8100/health"
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
            logger.info("Watchdog: Ubuntu Voice Server successfully restarted. Waiting 40s grace period for model load...")
            await asyncio.sleep(40)
        except Exception as e:
            logger.error("Watchdog: Failed to restart Voice Server via SSH: %s", e)
        finally:
            # Give the heavy PyTorch model ~30-60 seconds to compile and bind the port
            await asyncio.sleep(30)
            self._voice_restarting = False

    def _execute_remote_restart(self):
        """Restart local Voice Server process."""
        import sys
        venv_py = Path(r"E:\PyTorch env\my_venv\Scripts\python.exe")
        if not venv_py.exists():
            venv_py = Path(sys.executable)

        logger.info("Watchdog: Relaunching local Voice Server via %s", venv_py)
        import os
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd())

        subprocess.Popen(
            [str(venv_py), "-m", "voice.tts_server.main"],
            cwd=str(Path.cwd()),
            env=env,
        )
