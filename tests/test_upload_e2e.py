import httpx
import sys
import shutil

BASE_URL = "http://127.0.0.1:8000"
PROJECT_ID = "sample_book-1"
VOICE_ID = "fake_cand"
FILE_PATH = r"e:\Projects\crazy-audiobook-creator\voice_library\sample_book-1\child_female.wav"
TEMP_FILE_PATH = "temp_child_female.wav"
TRANSCRIPT = "She walked through the moonlit garden, listening as fallen leaves whispered beneath each careful step."

shutil.copy(FILE_PATH, TEMP_FILE_PATH)

def test_upload():
    print(f"Testing upload of {TEMP_FILE_PATH} to {VOICE_ID}...")
    
    with open(TEMP_FILE_PATH, "rb") as f:
        files = {"file": ("child_female.wav", f, "audio/wav")}
        data = {"transcript": TRANSCRIPT}
        
        response = httpx.post(
            f"{BASE_URL}/api/projects/{PROJECT_ID}/voices/{VOICE_ID}/upload",
            files=files,
            data=data,
            timeout=300.0
        )
    
    if response.status_code == 200:
        print("Success!")
        print(response.json())
    else:
        print(f"Failed: {response.status_code}")
        print(response.text)
        sys.exit(1)

def test_upload_mismatch():
    print(f"Testing upload of {TEMP_FILE_PATH} to {VOICE_ID} with mismatched transcript...")
    
    with open(TEMP_FILE_PATH, "rb") as f:
        files = {"file": ("child_female.wav", f, "audio/wav")}
        data = {"transcript": "This is completely different text that should fail."}
        
        response = httpx.post(
            f"{BASE_URL}/api/projects/{PROJECT_ID}/voices/{VOICE_ID}/upload",
            files=files,
            data=data,
            timeout=300.0
        )
    
    if response.status_code != 200:
        print("Expected Failure Caught:")
        print(f"Status: {response.status_code}")
        print(response.text)
    else:
        print(f"Test failed! Mismatch was incorrectly accepted: {response.status_code}")
        sys.exit(1)

if __name__ == "__main__":
    test_upload_mismatch()
    print("-" * 40)
    test_upload()
