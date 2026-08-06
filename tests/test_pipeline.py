import json
from brain.orchestrator.pipeline import Pipeline
from shared.models import BootstrapVoicesResponse, BootstrapVoiceResult
from pydantic import BaseModel
import shutil
from pathlib import Path

pipeline = Pipeline("brain/config.yaml")

class MockVoiceClient:
    def bootstrap_voices(self, request):
        print('Mock bootstrap_voices called')
        return BootstrapVoicesResponse(
            voices_generated={
                "narrator": BootstrapVoiceResult(
                    status="success",
                    candidates={
                        "cand1": "/path/to/cand1.wav",
                        "cand2": "/path/to/cand2.wav",
                        "cand3": "/path/to/cand3.wav"
                    }
                )
            },
            cast_diagnostics={}
        )

pipeline.voice_client = MockVoiceClient()

project_dir = pipeline.registry.project_dir("fake_project")
project_dir.mkdir(parents=True, exist_ok=True)
cast_data = {
    "version": "1.0",
    "project_id": "fake_project",
    "voices": {
        "narrator": {
            "gender": "male",
            "effective_prompt": "test",
            "design_fingerprint": "123",
            "owner_character_id": "narrator",
            "status": "pending",
            "reference_sample": None
        }
    }
}
with open(project_dir / "voice_cast.json", "w") as f:
    json.dump(cast_data, f)

class MockCharacter(BaseModel):
    id: str = "narrator"
    name: str = "Narrator"
    gender: str = "male"
    voice_description: str = "test"

pipeline.registry.characters = {
    "narrator": MockCharacter()
}

try:
    pipeline._run_voices("fake_project")
except Exception as e:
    print(e)
    
with open(project_dir / "voice_cast.json", "r") as f:
    print(json.dumps(json.load(f), indent=2))
