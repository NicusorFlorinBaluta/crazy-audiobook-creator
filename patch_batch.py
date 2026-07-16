import re

with open('voice/tts_server/qwen3_engine.py', 'r') as f:
    code = f.read()

new_loop = '''        for req in batch_requests:
            texts.append(req["text"])
            instructions.append(self._build_instruction(
                req.get("emotion_instruction", ""), 
                req.get("speed", 1.0)
            ))
            
            v_ref = req.get("voice_reference_path")
            voice_fx = req.get("voice_fx")
            
            if v_ref and self.fx and voice_fx and not voice_fx.is_identity():
                v_ref = self.fx.prepare_prompt_audio(str(v_ref), voice_fx)
                req["_temp_v_ref"] = v_ref
            
            if v_ref:
                # Load and resample audio
                import librosa
                ref_audio, ref_sr = sf.read(str(v_ref))
                if ref_sr != self.sample_rate:
                    ref_audio = librosa.resample(
                        ref_audio, orig_sr=ref_sr, target_sr=self.sample_rate
                    )
                voice_refs.append(ref_audio)
            else:
                voice_refs.append(None)
                
        # Generate batch
        audios = self._generate_batch(
            texts=texts,
            instructions=instructions,
            voice_references=voice_refs,
        )
        
        # Save to disk if requested and apply FX
        processed_audios = []
        for req, audio in zip(batch_requests, audios):
            temp_v_ref = req.get("_temp_v_ref")
            if temp_v_ref and hasattr(temp_v_ref, "unlink"):
                temp_v_ref.unlink(missing_ok=True)
                
            voice_fx = req.get("voice_fx")
            if self.fx and voice_fx and not voice_fx.is_identity():
                audio = self.fx.apply_post_pipeline(audio, self.sample_rate, voice_fx)
            processed_audios.append(audio)
            
            out_path = req.get("output_path")
            if out_path:
                out_path = Path(out_path)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                sf.write(str(out_path), audio, self.sample_rate)
                
        return processed_audios'''

code = re.sub(r'        for req in batch_requests:.*?return audios', new_loop, code, flags=re.DOTALL)

with open('voice/tts_server/qwen3_engine.py', 'w') as f:
    f.write(code)
