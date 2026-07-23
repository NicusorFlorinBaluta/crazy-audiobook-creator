import os
import torch
import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pathlib import Path
from transformers import AutoTokenizer
from parler_tts import ParlerTTSForConditionalGeneration, ParlerTTSConfig
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
        ParlerTTSConfig.has_no_defaults_at_init = True
        ParlerTTSForConditionalGeneration._validate_model_kwargs = lambda self, kwargs: None
        ParlerTTSForConditionalGeneration._prepare_special_tokens = lambda self, *args, **kwargs: None

        def _prepare_model_inputs(self, inputs, bos_token_id, model_kwargs):
            if model_kwargs is not None:
                if inputs is None:
                    inputs = model_kwargs.get("prompt_input_ids")
                model_kwargs.pop("input_ids", None)
            return inputs, "input_ids", model_kwargs

        ParlerTTSForConditionalGeneration._prepare_model_inputs = _prepare_model_inputs

        orig_prepare_encoder = ParlerTTSForConditionalGeneration._prepare_text_encoder_kwargs_for_generation
        def _prepare_text_encoder_kwargs_for_generation(self, inputs_tensor, model_kwargs, *args, **kwargs):
            res = orig_prepare_encoder(self, inputs_tensor, model_kwargs, *args, **kwargs)
            res.pop("input_ids", None)
            return res
        ParlerTTSForConditionalGeneration._prepare_text_encoder_kwargs_for_generation = _prepare_text_encoder_kwargs_for_generation

        from transformers.generation import GenerationMixin

        if GenerationMixin not in ParlerTTSForConditionalGeneration.__bases__:
            ParlerTTSForConditionalGeneration.__bases__ = ParlerTTSForConditionalGeneration.__bases__ + (GenerationMixin,)

        def _prepare_attention_mask_for_generation(self, *args, **kwargs):
            for a in args:
                if isinstance(a, torch.Tensor) and a.ndim >= 2:
                    return torch.ones(a.shape, dtype=torch.long, device=a.device)
            for v in kwargs.values():
                if isinstance(v, torch.Tensor) and v.ndim >= 2:
                    return torch.ones(v.shape, dtype=torch.long, device=v.device)
            return None

        def _get_initial_cache_position(self, cur_len=0, device=None, model_kwargs=None, *args, **kwargs):
            if model_kwargs is None:
                for a in reversed(args):
                    if isinstance(a, dict):
                        model_kwargs = a
                        break
            if model_kwargs is None:
                model_kwargs = kwargs
            if isinstance(model_kwargs, dict) and "cache_position" not in model_kwargs:
                if isinstance(cur_len, torch.Tensor):
                    model_kwargs["cache_position"] = torch.arange(0, cur_len.shape[-1], device=cur_len.device)
                else:
                    dev = device if device is not None else get_device()
                    model_kwargs["cache_position"] = torch.arange(0, cur_len if isinstance(cur_len, int) else 1, device=dev)
            return model_kwargs

        def _expand_inputs_for_generation(self, input_ids=None, expand_size=1, is_encoder_decoder=False, **model_kwargs):
            model_kwargs.pop("input_ids", None)
            if expand_size > 1:
                if input_ids is not None:
                    input_ids = input_ids.repeat_interleave(expand_size, dim=0)
                for k, v in list(model_kwargs.items()):
                    if isinstance(v, torch.Tensor) and v.ndim >= 1:
                        model_kwargs[k] = v.repeat_interleave(expand_size, dim=0)
            return input_ids, model_kwargs

        ParlerTTSForConditionalGeneration._expand_inputs_for_generation = _expand_inputs_for_generation
        GenerationMixin._expand_inputs_for_generation = _expand_inputs_for_generation

        from transformers.cache_utils import DynamicCache, EncoderDecoderCache

        class KeyCacheProxy(list):
            def __init__(self, cache, is_key=True):
                self.cache = cache
                self.is_key = is_key
            def __getitem__(self, idx):
                if hasattr(self.cache, "layers") and idx < len(self.cache.layers):
                    layer = self.cache.layers[idx]
                    return getattr(layer, "keys", getattr(layer, "key", None)) if self.is_key else getattr(layer, "values", getattr(layer, "value", None))
                if hasattr(self.cache, "self_attention_cache"):
                    return KeyCacheProxy(self.cache.self_attention_cache, self.is_key)[idx]
                return None

        DynamicCache.key_cache = property(lambda self: KeyCacheProxy(self, True))
        DynamicCache.value_cache = property(lambda self: KeyCacheProxy(self, False))
        EncoderDecoderCache.key_cache = property(lambda self: KeyCacheProxy(self, True))
        EncoderDecoderCache.value_cache = property(lambda self: KeyCacheProxy(self, False))

        from transformers import PreTrainedModel
        PreTrainedModel._prepare_attention_mask_for_generation = _prepare_attention_mask_for_generation
        PreTrainedModel._get_initial_cache_position = _get_initial_cache_position
        ParlerTTSForConditionalGeneration._prepare_attention_mask_for_generation = _prepare_attention_mask_for_generation
        GenerationMixin._prepare_attention_mask_for_generation = _prepare_attention_mask_for_generation
        ParlerTTSForConditionalGeneration._get_initial_cache_position = _get_initial_cache_position
        GenerationMixin._get_initial_cache_position = _get_initial_cache_position

        model_name = "parler-tts/parler-tts-large-v1"
        dtype = torch.float32
        model = ParlerTTSForConditionalGeneration.from_pretrained(model_name, torch_dtype=dtype).to(device)
        
        from transformers import GenerationConfig
        GenerationConfig.disable_compile = True
        GenerationConfig._pad_token_tensor = property(lambda self: torch.tensor(1024, device=get_device()))
        GenerationConfig._eos_token_tensor = property(lambda self: torch.tensor(1024, device=get_device()))
        GenerationConfig._bos_token_tensor = property(lambda self: torch.tensor(1025, device=get_device()))
        GenerationConfig._decoder_start_token_tensor = property(lambda self: torch.tensor(1025, device=get_device()))
        
        model.generation_config = GenerationConfig.from_model_config(model.config.decoder)
        model.generation_config.disable_compile = True
        
        orig_prep_gen = ParlerTTSForConditionalGeneration.prepare_inputs_for_generation
        def prepare_inputs_for_generation(self, *args, **kwargs):
            try:
                return orig_prep_gen(self, *args, **kwargs)
            except RuntimeError as err:
                if "negative dimension" in str(err):
                    kwargs.pop("prompt_attention_mask", None)
                    return orig_prep_gen(self, *args, **kwargs)
                raise

        ParlerTTSForConditionalGeneration.prepare_inputs_for_generation = prepare_inputs_for_generation
        import types
        model._prepare_attention_mask_for_generation = types.MethodType(_prepare_attention_mask_for_generation, model)
        model._get_initial_cache_position = types.MethodType(_get_initial_cache_position, model)
        model._expand_inputs_for_generation = types.MethodType(_expand_inputs_for_generation, model)


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
        prompt_inputs = tokenizer(request.prompt, return_tensors="pt").to(device)
        
        generation = model.generate(
            input_ids=prompt_inputs.input_ids,
            prompt_input_ids=prompt_inputs.input_ids,
            prompt_attention_mask=prompt_inputs.attention_mask,
        )
        audio_arr = generation.cpu().numpy().squeeze().astype(np.float32)
        
        # Save audio
        out_path = Path(request.output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(out_path), audio_arr, model.config.sampling_rate)
        
        logger.info(f"Saved designed voice to {out_path}")
        return {"status": "success", "file": str(out_path)}
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"Error designing voice:\n{tb}")
        raise HTTPException(status_code=500, detail=f"{e}\n{tb}")

@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": model is not None}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8101)
