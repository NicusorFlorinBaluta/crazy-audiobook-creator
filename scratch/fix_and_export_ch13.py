"""Fix defective lines across Chapters 1-3, master audio, and export M4B audiobook."""

import os
import json
import shutil
from pathlib import Path

os.environ['HF_HUB_DISABLE_SYMLINKS'] = '1'

import soundfile as sf
import numpy as np

project_id = 'sample_book-7'
workspace = Path('workspace') / project_id
script_dir = Path('brain/projects') / project_id / 'script'

from voice.validator.whisper_validator import WhisperValidator
from voice.mastering.assembler import AudioAssembler
from voice.mastering.normalizer import LoudnessNormalizer
from voice.mastering.m4b_exporter import M4BExporter
from voice.tts_server.qwen3_engine import Qwen3TTSEngine
from voice.tts_server.voice_library import VoiceLibraryManager
from shared.models import MasterSegmentInfo, AudiobookMetadata, ExportChapterInfo

print('=== RE-GENERATING DEFECTIVE LINES & MASTERING CHAPTERS 1, 2, 3 ===')

engine = Qwen3TTSEngine(model_name='Qwen/Qwen3-TTS-12Hz-1.7B-Base', device='cuda')
engine.load()

validator = WhisperValidator(model_name='small', device='cuda')
validator.load()

library = VoiceLibraryManager(library_dir='voice_library')
assembler = AudioAssembler(crossfade_ms=30, sample_rate=24000)
normalizer = LoudnessNormalizer(target_lufs=-19.0, peak_limit_dbfs=-1.0, output_sample_rate=44100)
exporter = M4BExporter()

# Check and fix lines for chapters 1, 2, 3
for ch_num in [1, 2, 3]:
    script_file = script_dir / f'chapter_{ch_num:03d}.json'
    script_data = json.loads(script_file.read_text(encoding='utf-8'))
    lines = script_data.get('lines', [])
    
    print(f'\n--- Processing Chapter {ch_num} ({len(lines)} lines) ---')
    fixed_count = 0
    
    for line in lines:
        lid = line.get('line_id') or f"ch{ch_num:02d}_{line.get('id'):03d}"
        expected = line.get('text', '')
        seg_file = workspace / 'segments' / f'{lid}.wav'
        
        needs_regen = False
        if not seg_file.exists():
            needs_regen = True
        else:
            transcription = validator.transcribe(str(seg_file))
            wer = validator.calculate_wer(expected, transcription)
            if wer > 0.20:
                needs_regen = True
                
        if needs_regen:
            speaker = line.get('speaker', 'narrator')
            voice_ref = library.get_voice_path(project_id, speaker)
            ref_text = library.get_voice_ref_text(project_id, speaker)
            if not voice_ref.exists():
                voice_ref = library.get_voice_path(project_id, 'narrator')
                ref_text = library.get_voice_ref_text(project_id, 'narrator')
                
            audio = engine.generate_speech(
                text=expected,
                voice_reference_path=voice_ref,
                ref_text=ref_text,
                emotion_instruction=line.get('emotion'),
                speed=line.get('speed', 1.0),
                output_path=seg_file,
            )
            fixed_count += 1
            print(f'  [Re-generated] Line {lid} for speaker {speaker}')

    print(f'Chapter {ch_num}: {fixed_count} defective lines re-generated cleanly.')
    
    # Master Chapter
    segments = []
    for line in lines:
        lid = line.get('line_id')
        if not lid and 'id' in line:
            lid = f"ch{ch_num:02d}_{line['id']:03d}"
        segments.append(
            MasterSegmentInfo(
                line_id=lid,
                file=f"{project_id}/segments/{lid}.wav",
                pause_before_ms=line.get('pause_before_ms', 0),
                pause_after_ms=line.get('pause_after_ms', 500),
            )
        )
    
    assembled = assembler.assemble_chapter(segments=segments, workspace=Path('workspace'))
    ch_out_dir = workspace / 'chapters'
    ch_out_dir.mkdir(parents=True, exist_ok=True)
    ch_out_path = ch_out_dir / f'chapter_{ch_num:03d}.wav'
    
    master_result = normalizer.normalize(
        audio=assembled['audio'],
        sample_rate=assembled['sample_rate'],
        output_path=str(ch_out_path),
    )
    print(f'  -> Mastered chapter_{ch_num:03d}.wav: duration={master_result["duration_seconds"]:.1f}s, LUFS={master_result["lufs"]:.1f}')

# Export M4B for Chapters 1-3
print('\n=== EXPORTING PARTIAL M4B AUDIOBOOK (CHAPTERS 1-3) ===')
export_chapters = [
    ExportChapterInfo(number=ch, title=f'Chapter {ch}', file=f'chapters/chapter_{ch:03d}.wav')
    for ch in [1, 2, 3]
]
m4b_resp = exporter.export(
    project_id=project_id,
    metadata=AudiobookMetadata(title='Sixth of the Dusk (Sample)', author='Brandon Sanderson'),
    chapters=export_chapters,
    workspace=Path('workspace'),
)
print('Export Result:', m4b_resp)

# Copy output to root project dir for easy download
local_m4b = Path(f'{project_id}_chapters_1-3.m4b')
shutil.copy2(m4b_resp.output_file, local_m4b)
print(f'\nM4B Audiobook ready for download at: {local_m4b.resolve()}')
