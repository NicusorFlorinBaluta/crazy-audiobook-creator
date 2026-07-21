import os
import torch
import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pathlib import Path
from transformers import AutoTokenizer
from parler_tts import ParlerTTSForConditionalGeneration
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("parler_server")

app = FastAPI(title="Parler TTS Microservice")

class VoiceDesignRequest(BaseModel):
    prompt: str
    text: str
    output_path: str

# Globals for lazy loading
model = None
tokenizer = None

def get_device():
    return "cuda:0" if torch.cuda.is_available() else "cpu"

@app.on_event("startup")
def load_model():
    global model, tokenizer
    device = get_device()
    logger.info(f"Loading Parler-TTS on {device}...")
    try:
        model_name = "parler-tts/parler-tts-large-v1"
        dtype = torch.float16 if "cuda" in device else torch.float32
        model = ParlerTTSForConditionalGeneration.from_pretrained(model_name, torch_dtype=dtype).to(device)
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        logger.info("Parler-TTS loaded successfully!")
    except Exception as e:
        logger.error(f"Failed to load Parler-TTS: {e}")

@app.post("/voices/design")
def design_voice(request: VoiceDesignRequest):
    if model is None or tokenizer is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    device = get_device()
    logger.info(f"Designing voice: {request.prompt[:50]}...")
    
    try:
        input_ids = tokenizer(request.text, return_tensors="pt").input_ids.to(device)
        prompt_input_ids = tokenizer(request.prompt, return_tensors="pt").input_ids.to(device)
        
        generation = model.generate(input_ids=input_ids, prompt_input_ids=prompt_input_ids)
        audio_arr = generation.cpu().numpy().squeeze().astype(np.float32)
        
        # Save audio
        out_path = Path(request.output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(out_path), audio_arr, model.config.sampling_rate)
        
        logger.info(f"Saved designed voice to {out_path}")
        return {"status": "success", "file": str(out_path)}
    except Exception as e:
        logger.error(f"Error designing voice: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": model is not None}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8101)
