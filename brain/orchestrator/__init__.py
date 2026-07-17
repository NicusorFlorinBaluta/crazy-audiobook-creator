"""Pipeline orchestrator module.

Coordinates the end-to-end audiobook production pipeline,
managing state persistence, job queue, and communication
with the Ubuntu Voice server.
"""

from brain.orchestrator.pipeline import Pipeline
from brain.orchestrator.ubuntu_client import UbuntuClient

__all__ = ["Pipeline", "UbuntuClient"]
