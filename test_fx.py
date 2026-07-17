import requests
import json
import time

URL = "http://192.168.50.180:8100/generate/line"

payload = {
    "project_id": "sample_book",
    "line": {
        "line_id": "test_pitch_shift",
        "speaker": "narrator",
        "text": "This is a test of the pitch shifting post processor. My voice should sound significantly deeper.",
        "emotion": "neutral",
        "speed": 0.8,
        "pause_before_ms": 0,
        "pause_after_ms": 0,
        "voice_fx": {
            "pitch_semitones": -4.0,
            "speed": 0.8,
            "tone": "neutral"
        }
    }
}

print("Testing direct generation with VoiceFX...")
try:
    start = time.time()
    resp = requests.post(URL, json=payload, timeout=60)
    print(f"Time: {time.time()-start:.1f}s")
    if resp.ok:
        data = resp.json()
        print("Success:", json.dumps(data, indent=2))
        print("File saved at:", data.get("audio_file"))
    else:
        print("Failed:", resp.status_code, resp.text)
except Exception as e:
    print("Error:", e)
