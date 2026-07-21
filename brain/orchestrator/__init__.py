"""Pipeline orchestrator module.

Coordinates the end-to-end audiobook production pipeline,
managing state persistence, job queue, and communication
with the Voice server.
"""

from brain.orchestrator.pipeline import Pipeline
from brain.orchestrator.voice_client import VoiceClient

__all__ = ["Pipeline", "VoiceClient"]

