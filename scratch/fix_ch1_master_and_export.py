import json
import logging
import os
import shutil
import time
from pathlib import Path
import numpy as np
import soundfile as sf

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

project_id = "sample_book-7"
project_dir = Path(f"brain/projects/{project_id}")
workspace_dir = Path(f"workspace/{project_id}")

chapters_dir = workspace_dir / "chapters"
segments_dir = workspace_dir / "segments"
chapters_dir.mkdir(parents=True, exist_ok=True)
segments_dir.mkdir(parents=True, exist_ok=True)

# Step 1: Ensure Voice Server is running
from brain.orchestrator.voice_client import VoiceClient
from shared.models import MasterChapterRequest, MasterSegmentInfo, ExportM4BRequest, ExportChapterInfo, AudiobookMetadata

voice_client = VoiceClient(host="http://127.0.0.1:8100")
health = voice_client.health_check()
logger.info("Voice server health: status=%s, gpu=%s", health.status, health.gpu)

# Step 2: Check Chapter 1 script and missing segment files
ch1_script = Path(project_dir / "script/chapter_001.json")
data = json.loads(ch1_script.read_text(encoding="utf-8"))
lines = data.get("lines", [])
logger.info("Chapter 1 script lines: %d", len(lines))

missing_lines = []
for line in lines:
    lid = line["line_id"]
    wav_path = segments_dir / f"{lid}.wav"
    if not wav_path.exists():
        missing_lines.append(line)

logger.info("Chapter 1 missing lines to synthesize: %d", len(missing_lines))

if missing_lines:
    from shared.models import GenerateChapterRequest, ScriptLine
    req_lines = [ScriptLine(**l) for l in missing_lines]
    gen_req = GenerateChapterRequest(
        project_id=project_id,
        chapter_number=1,
        lines=req_lines,
        validate=True,
        auto_retry=True,
        max_retries=3
    )
    logger.info("Requesting synthesis of %d missing Chapter 1 lines...", len(req_lines))
    res = voice_client.generate_chapter(gen_req)
    logger.info("Synthesis complete: generated=%d, failed=%d", res.generated, res.failed_validation)

# Step 3: Audio Mastering for Chapters 1, 2, 3 with AudioAssembler & LoudnessNormalizer
from voice.mastering.assembler import AudioAssembler
from voice.mastering.normalizer import LoudnessNormalizer

assembler = AudioAssembler(sample_rate=24000, chapter_start_silence_ms=1000, chapter_end_silence_ms=2000)
normalizer = LoudnessNormalizer(target_lufs=-19.0)

def get_announcement_audio(chapter_num: int, title: str) -> np.ndarray:
    """Generate clean Narrator voice announcement for chapter title."""
    ann_text = f"Chapter {chapter_num}. {title}."
    if chapter_num == 1 and "prologue" in title.lower():
        ann_text = "Prologue."
    elif chapter_num == 2:
        ann_text = "Chapter One."
    elif chapter_num == 3:
        ann_text = "Chapter Two."

    ann_wav_path = workspace_dir / f"announcements/ch_{chapter_num:03d}_announcement.wav"
    ann_wav_path.parent.mkdir(parents=True, exist_ok=True)

    if ann_wav_path.exists():
        audio, sr = sf.read(str(ann_wav_path))
        return audio

    try:
        from shared.models import GenerateLineRequest, ScriptLine
        s_line = ScriptLine(
            line_id=f"ann_{chapter_num:03d}",
            speaker="narrator",
            text=ann_text,
            emotion="neutral",
            speed=1.0,
            pause_before_ms=0,
            pause_after_ms=500
        )
        res = voice_client.generate_line(GenerateLineRequest(
            project_id=project_id,
            line=s_line,
            validate=False
        ))
        if res.audio_file and Path(res.audio_file).exists():
            shutil.copy2(res.audio_file, ann_wav_path)
            audio, sr = sf.read(str(ann_wav_path))
            return audio
    except Exception as e:
        logger.warning("Failed to generate announcement via API: %s", e)

    return np.array([], dtype=np.float32)

for ch_num, title in [(1, "Prologue"), (2, "Chapter One"), (3, "Chapter Two")]:
    ch_script = Path(project_dir / f"script/chapter_{ch_num:03d}.json")
    if not ch_script.exists():
        continue
    cdata = json.loads(ch_script.read_text(encoding="utf-8"))
    clines = cdata.get("lines", [])

    segments = [
        MasterSegmentInfo(
            line_id=l["line_id"],
            file=f"{project_id}/segments/{l['line_id']}.wav",
            pause_before_ms=l.get("pause_before_ms", 0),
            pause_after_ms=l.get("pause_after_ms", 500)
        )
        for l in clines
        if (segments_dir / f"{l['line_id']}.wav").exists()
    ]

    logger.info("Mastering Chapter %d (%s): %d segments...", ch_num, title, len(segments))
    
    ann_audio = get_announcement_audio(ch_num, title)
    assembly = assembler.assemble_chapter(segments=segments, workspace=Path("workspace"), announcement_audio=ann_audio)
    
    ch_wav = chapters_dir / f"chapter_{ch_num:03d}.wav"
    norm = normalizer.normalize(assembly["audio"], sample_rate=assembly["sample_rate"], output_path=str(ch_wav))
    
    dur_s = norm["duration_seconds"]
    logger.info("Mastered Chapter %d -> %s (duration: %.1fs = %.1f min, LUFS: %.1f)", 
                ch_num, ch_wav.name, dur_s, dur_s / 60, norm.get("lufs", -19.0))

# Step 4: Export Partial M4B for Chapters 1-3
from voice.mastering.m4b_exporter import M4BExporter
exporter = M4BExporter()

export_ch_info = [
    ExportChapterInfo(number=1, title="Prologue", file="chapters/chapter_001.wav"),
    ExportChapterInfo(number=2, title="Chapter One", file="chapters/chapter_002.wav"),
    ExportChapterInfo(number=3, title="Chapter Two", file="chapters/chapter_003.wav"),
]

res = exporter.export(
    project_id=project_id,
    metadata=AudiobookMetadata(title="sample_book", author="Unknown"),
    chapters=export_ch_info,
    workspace=Path("workspace")
)

root_m4b = Path(f"{project_id}_chapters_1-3.m4b")
if Path(res.output_file).exists():
    shutil.copy2(res.output_file, root_m4b)
    logger.info("Exported M4B to: %s (size: %.2f MB, duration: %s)", root_m4b.name, res.file_size_mb, res.total_duration)

# Step 5: Update SQLite DB state
import sqlite3
conn = sqlite3.connect("brain/projects/pipeline_state.db")
c = conn.cursor()
row = c.execute("SELECT state FROM jobs WHERE project_id='sample_book-7'").fetchone()
if row:
    state = json.loads(row[0])
    state["status"] = "paused"
    state["running"] = False
    state["mastered_chapters"] = [1, 2, 3]
    state["generated_chapters"] = [1, 2, 3]
    state["current_gen_chapter"] = None
    c.execute("UPDATE jobs SET state=? WHERE project_id='sample_book-7'", (json.dumps(state),))
    conn.commit()
    logger.info("Updated pipeline_state.db successfully.")
