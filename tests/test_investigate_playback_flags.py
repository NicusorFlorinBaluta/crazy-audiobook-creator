"""Tests for tools/investigate_playback_flags.py repair functionality."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from brain.orchestrator.job_queue import JobQueue
from shared.constants import ValidationStatus
from shared.models import GenerateLineResponse, QualityResult
from shared.segment_repair import SegmentRepairResult
from tools.investigate_playback_flags import diagnose_flag, main


@pytest.fixture
def mock_project_env(tmp_path: Path):
    proj_dir = tmp_path / "projects" / "test_proj"
    ws_dir = tmp_path / "workspace" / "test_proj"
    proj_dir.mkdir(parents=True)
    ws_dir.mkdir(parents=True)
    (proj_dir / "script").mkdir(parents=True)

    db_path = proj_dir / "pipeline_state.db"
    jq = JobQueue(db_path=str(db_path))

    # Create script file for chapter 1
    script_file = proj_dir / "script" / "chapter_001.json"
    initial_script = {
        "chapter_number": 1,
        "lines": [
            {
                "line_id": "ch01_0001",
                "speaker": "kelsier",
                "text": "Hello world",
                "spoken_text": "Hello world",
                "emotion": "normal",
            }
        ],
    }
    script_file.write_text(json.dumps(initial_script, indent=2), encoding="utf-8")

    # Create playback flag
    flag = jq.create_playback_flag(
        project_id="test_proj",
        flag_id="flag-12345",
        chapter_number=1,
        position_ms=1000,
        issue_type="wrong_speaker",
        user_note="Wrong speaker",
        source="phone",
        line_id="ch01_0001",
        enriched_data={"active_line": {"line_id": "ch01_0001", "speaker": "kelsier", "text": "Hello world"}},
    )

    # Write initial playback_flags.json
    flags_json = proj_dir / "playback_flags.json"
    flags_json.write_text(json.dumps({"project_id": "test_proj", "total_flags": 1, "flags": [flag]}), encoding="utf-8")

    return {
        "proj_dir": proj_dir,
        "ws_dir": ws_dir,
        "job_queue": jq,
        "flag_id": flag["flag_id"],
        "script_file": script_file,
    }


def test_repair_dry_run_does_not_commit(mock_project_env, tmp_path: Path):
    env = mock_project_env
    flag_id = env["flag_id"]

    cand_wav = tmp_path / "cand.wav"
    cand_wav.write_bytes(b"dummy")

    mock_client = MagicMock()
    mock_client.generate_line.return_value = GenerateLineResponse(
        line_id="repair-ch01_0001",
        audio_file=str(cand_wav),
        duration_seconds=1.0,
    )
    mock_client.validate_segment.return_value = QualityResult(
        line_id="repair-ch01_0001",
        status=ValidationStatus.PASS,
        wer=0.0,
        passed_hard_gates=True,
    )

    with (
        patch("tools.investigate_playback_flags._load_project_data") as mock_load,
        patch("tools.investigate_playback_flags.VoiceClient", return_value=mock_client),
        patch("tools.investigate_playback_flags.replace_segment") as mock_replace,
    ):
        mock_load.return_value = (env["proj_dir"], env["ws_dir"], env["job_queue"], None, None)

        exit_code = main(["test_proj", "--repair", flag_id, "--speaker", "elend"])
        assert exit_code == 0

        # Must not have called replace_segment
        mock_replace.assert_not_called()

        # Script must be unchanged
        script_data = json.loads(env["script_file"].read_text(encoding="utf-8"))
        assert script_data["lines"][0]["speaker"] == "kelsier"

        # Flag status must still be open
        current_flag = env["job_queue"].get_playback_flag("test_proj", flag_id)
        assert current_flag["status"] == "open"


def test_repair_replace_segment_failure_keeps_flag_open(mock_project_env, tmp_path: Path):
    env = mock_project_env
    flag_id = env["flag_id"]

    cand_wav = tmp_path / "cand.wav"
    cand_wav.write_bytes(b"dummy")

    mock_client = MagicMock()
    mock_client.generate_line.return_value = GenerateLineResponse(
        line_id="repair-ch01_0001",
        audio_file=str(cand_wav),
        duration_seconds=1.0,
    )
    mock_client.validate_segment.return_value = QualityResult(
        line_id="repair-ch01_0001",
        status=ValidationStatus.PASS,
        wer=0.0,
        passed_hard_gates=True,
    )

    with (
        patch("tools.investigate_playback_flags._load_project_data") as mock_load,
        patch("tools.investigate_playback_flags.VoiceClient", return_value=mock_client),
        patch("tools.investigate_playback_flags.replace_segment") as mock_replace,
    ):
        mock_load.return_value = (env["proj_dir"], env["ws_dir"], env["job_queue"], None, None)
        mock_replace.return_value = SegmentRepairResult(
            success=False,
            line_id="ch01_0001",
            chapter=1,
            new_hash="",
            error="Manifest update failed",
        )

        exit_code = main(["test_proj", "--repair", flag_id, "--speaker", "elend", "--apply"])
        assert exit_code == 1

        mock_replace.assert_called_once()

        # Script must not be modified
        script_data = json.loads(env["script_file"].read_text(encoding="utf-8"))
        assert script_data["lines"][0]["speaker"] == "kelsier"

        # Flag status must remain open
        current_flag = env["job_queue"].get_playback_flag("test_proj", flag_id)
        assert current_flag["status"] == "open"


def test_repair_replace_segment_success_resolves_flag(mock_project_env, tmp_path: Path):
    env = mock_project_env
    flag_id = env["flag_id"]

    cand_wav = tmp_path / "cand.wav"
    cand_wav.write_bytes(b"dummy")

    mock_client = MagicMock()
    mock_client.generate_line.return_value = GenerateLineResponse(
        line_id="repair-ch01_0001",
        audio_file=str(cand_wav),
        duration_seconds=1.0,
    )
    mock_client.validate_segment.return_value = QualityResult(
        line_id="repair-ch01_0001",
        status=ValidationStatus.PASS,
        wer=0.0,
        passed_hard_gates=True,
    )

    with (
        patch("tools.investigate_playback_flags._load_project_data") as mock_load,
        patch("tools.investigate_playback_flags.VoiceClient", return_value=mock_client),
        patch("tools.investigate_playback_flags.replace_segment") as mock_replace,
    ):
        mock_load.return_value = (env["proj_dir"], env["ws_dir"], env["job_queue"], None, None)
        mock_replace.return_value = SegmentRepairResult(
            success=True,
            line_id="ch01_0001",
            chapter=1,
            new_hash="newhash123",
            error=None,
        )

        dummy_ledger = tmp_path / "ledger.md"
        dummy_test = tmp_path / "test_attribution_audit.py"
        exit_code = main(
            [
                "test_proj",
                "--repair",
                flag_id,
                "--speaker",
                "elend",
                "--apply",
                "--ledger-path",
                str(dummy_ledger),
                "--test-path",
                str(dummy_test),
            ]
        )
        assert exit_code == 0

        mock_replace.assert_called_once()

        # Script must be updated with new speaker
        script_data = json.loads(env["script_file"].read_text(encoding="utf-8"))
        assert script_data["lines"][0]["speaker"] == "elend"
        assert script_data["lines"][0]["speaker_confidence"] == 1.0

        # Flag status must be resolved and resolution must name chapter
        current_flag = env["job_queue"].get_playback_flag("test_proj", flag_id)
        assert current_flag["status"] == "resolved"
        assert "chapter 1" in current_flag["resolution"].lower()


def test_repair_creates_ledger_entry_and_regression_test(mock_project_env, tmp_path: Path):
    env = mock_project_env
    flag_id = env["flag_id"]

    cand_wav = tmp_path / "cand.wav"
    cand_wav.write_bytes(b"dummy")

    mock_client = MagicMock()
    mock_client.generate_line.return_value = GenerateLineResponse(
        line_id="repair-ch01_0001",
        audio_file=str(cand_wav),
        duration_seconds=1.0,
    )
    mock_client.validate_segment.return_value = QualityResult(
        line_id="repair-ch01_0001",
        status=ValidationStatus.PASS,
        wer=0.0,
        passed_hard_gates=True,
    )

    dummy_ledger = tmp_path / "attribution-case-ledger.md"
    dummy_ledger.write_text(
        "# Attribution case ledger\n\n## Rules in force\n\n"
        "| case | what was wrong | rule | measured | test | provenance |\n"
        "| --- | --- | --- | --- | --- | --- |\n\n"
        "## Rules measured and rejected\n",
        encoding="utf-8",
    )

    dummy_test = tmp_path / "test_attribution_audit.py"
    dummy_test.write_text(
        "import unittest\n\n\nclass AttributionAuditTests(unittest.TestCase):\n"
        '    def test_existing(self) -> None:\n        pass\n\n\nif __name__ == "__main__":\n    unittest.main()\n',
        encoding="utf-8",
    )

    with (
        patch("tools.investigate_playback_flags._load_project_data") as mock_load,
        patch("tools.investigate_playback_flags.VoiceClient", return_value=mock_client),
        patch("tools.investigate_playback_flags.replace_segment") as mock_replace,
    ):
        mock_load.return_value = (env["proj_dir"], env["ws_dir"], env["job_queue"], None, None)
        mock_replace.return_value = SegmentRepairResult(
            success=True,
            line_id="ch01_0001",
            chapter=1,
            new_hash="newhash123",
            error=None,
        )

        exit_code = main(
            [
                "test_proj",
                "--repair",
                flag_id,
                "--speaker",
                "elend",
                "--reason",
                "Speech tag explicitly names Elend",
                "--apply",
                "--ledger-path",
                str(dummy_ledger),
                "--test-path",
                str(dummy_test),
            ]
        )
        assert exit_code == 0

        # Verify ledger row
        ledger_text = dummy_ledger.read_text(encoding="utf-8")
        assert "ch01_0001" in ledger_text
        assert "kelsier -> elend in ch 1" in ledger_text
        assert "Speech tag explicitly names Elend" in ledger_text
        assert "1 flag resolved" in ledger_text
        assert "test_attribution_audit.py" in ledger_text

        # Verify test generation
        test_text = dummy_test.read_text(encoding="utf-8")
        assert "def test_attribution_regression_ch01_0001(self) -> None:" in test_text
        assert "ch01_0001" in test_text

        # Verify the generated test fails against pre-repair script and passes against repaired script
        # 1. Pre-repair script has speaker = "kelsier"
        pre_repair_script = {
            "chapter_number": 1,
            "lines": [{"line_id": "ch01_0001", "speaker": "kelsier", "text": "Hello world"}],
        }
        env["script_file"].write_text(json.dumps(pre_repair_script), encoding="utf-8")

        # Compile and execute the generated test method against the pre-repair script
        # Point the test's script lookup to env["script_file"]
        patched_test_text = test_text.replace(
            'Path("brain/projects/test_proj/script/chapter_001.json")',
            f'Path(r"{env["script_file"]}")',
        )
        scope: dict = {}
        exec(patched_test_text, scope)
        test_case_instance = scope["AttributionAuditTests"]()

        with pytest.raises(AssertionError):
            test_case_instance.test_attribution_regression_ch01_0001()

        # 2. Repaired script has speaker = "elend"
        repaired_script = {
            "chapter_number": 1,
            "lines": [{"line_id": "ch01_0001", "speaker": "elend", "text": "Hello world"}],
        }
        env["script_file"].write_text(json.dumps(repaired_script), encoding="utf-8")
        # Now it passes!
        test_case_instance.test_attribution_regression_ch01_0001()


def test_repair_validation_failure_aborts(mock_project_env, tmp_path: Path):
    env = mock_project_env
    flag_id = env["flag_id"]

    cand_wav = tmp_path / "cand.wav"
    cand_wav.write_bytes(b"dummy")

    mock_client = MagicMock()
    mock_client.generate_line.return_value = GenerateLineResponse(
        line_id="repair-ch01_0001",
        audio_file=str(cand_wav),
        duration_seconds=1.0,
    )
    mock_client.validate_segment.return_value = QualityResult(
        line_id="repair-ch01_0001",
        status=ValidationStatus.FAIL,
        wer=0.85,
        passed_hard_gates=False,
    )

    with (
        patch("tools.investigate_playback_flags._load_project_data") as mock_load,
        patch("tools.investigate_playback_flags.VoiceClient", return_value=mock_client),
        patch("tools.investigate_playback_flags.replace_segment") as mock_replace,
    ):
        mock_load.return_value = (env["proj_dir"], env["ws_dir"], env["job_queue"], None, None)

        exit_code = main(["test_proj", "--repair", flag_id, "--speaker", "elend", "--apply"])
        assert exit_code == 1

        mock_replace.assert_not_called()

        # Flag remains open
        current_flag = env["job_queue"].get_playback_flag("test_proj", flag_id)
        assert current_flag["status"] == "open"


def test_auto_diagnose_benchmark_cases(tmp_path: Path):
    """Pin the four playback flag benchmark cases:
    - ch13_0424: Narration line flagged for speaker; verdict INCONCLUSIVE, no suggested speaker.
    - ch15_0062: Narration line flagged for speaker; verdict INCONCLUSIVE, no suggested speaker.
    - ch14_0358: Valid dialogue attributed to Jarlaxle; verdict LIKELY_CORRECT_ATTRIBUTION, suggested Jarlaxle.
    - ch18_0132: Attached tag 'she accused.' identifies speech verb and attributes dialogue to female speaker Kyrnill.
    """
    proj_dir = tmp_path / "proj"
    script_dir = proj_dir / "script"
    script_dir.mkdir(parents=True)

    def _mk(cid: str, name: str, gender: str) -> dict:
        return {
            "id": cid,
            "name": name,
            "gender": gender,
            "age_range": "adult",
            "voice_description": f"{name} voice",
            "aliases": [],
        }

    char_data = {
        "characters": {
            "jarlaxle": _mk("jarlaxle", "Jarlaxle", "male"),
            "kyrnill": _mk("kyrnill", "Kyrnill", "female"),
            "dininae": _mk("dininae", "Dininae", "female"),
            "breezy": _mk("breezy", "Breezy", "female"),
        }
    }

    # Chapter 13: ch13_0424 narration
    (script_dir / "chapter_013.json").write_text(
        json.dumps(
            {
                "chapter_number": 13,
                "chapter_title": "Chapter 13",
                "lines": [
                    {
                        "line_id": "ch13_0424",
                        "speaker": "narrator",
                        "text": (
                            "And a folded parchment, a note from her parents, she knew, "
                            "and a long, long while would pass before she even gathered up the courage to approach it."
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    flag_ch13 = {
        "flag_id": "flag-ch13",
        "chapter_number": 13,
        "position_ms": 20078546,
        "line_id": "ch13_0424",
        "enriched_data": {
            "active_line": {
                "line_id": "ch13_0424",
                "speaker": "narrator",
                "text": "And a folded parchment...",
            }
        },
    }
    d_ch13 = diagnose_flag(flag_ch13, proj_dir, {}, char_data)
    assert d_ch13["verdict"] in ("INCONCLUSIVE", "LIKELY_CORRECT_ATTRIBUTION")
    assert d_ch13["suggested_speaker"] is None

    # Chapter 15: ch15_0062 narration
    (script_dir / "chapter_015.json").write_text(
        json.dumps(
            {
                "chapter_number": 15,
                "chapter_title": "Chapter 15",
                "lines": [
                    {
                        "line_id": "ch15_0062",
                        "speaker": "narrator",
                        "text": (
                            "Avelyere showed Breezy the portal, and promised great things beyond, "
                            "but explained that Breezy wasn't ready for that adventure yet."
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    flag_ch15 = {
        "flag_id": "flag-ch15",
        "chapter_number": 15,
        "position_ms": 326859,
        "line_id": "ch15_0062",
        "enriched_data": {
            "active_line": {
                "line_id": "ch15_0062",
                "speaker": "narrator",
                "text": "Avelyere showed Breezy the portal...",
            }
        },
    }
    d_ch15 = diagnose_flag(flag_ch15, proj_dir, {}, char_data)
    assert d_ch15["verdict"] in ("INCONCLUSIVE", "LIKELY_CORRECT_ATTRIBUTION")
    assert d_ch15["suggested_speaker"] is None

    # Chapter 14: ch14_0358 dialogue (Jarlaxle)
    (script_dir / "chapter_014.json").write_text(
        json.dumps(
            {
                "chapter_number": 14,
                "chapter_title": "Chapter 14",
                "lines": [
                    {
                        "line_id": "ch14_0358",
                        "speaker": "jarlaxle",
                        "speaker_confidence": 0.95,
                        "text": '"And you think the Monastery of the Yellow Rose the best place to explore it?"',
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    flag_ch14 = {
        "flag_id": "flag-ch14",
        "chapter_number": 14,
        "position_ms": 1856616,
        "line_id": "ch14_0358",
        "enriched_data": {
            "active_line": {
                "line_id": "ch14_0358",
                "speaker": "jarlaxle",
                "text": '"And you think the Monastery of the Yellow Rose the best place to explore it?"',
            }
        },
    }
    d_ch14 = diagnose_flag(flag_ch14, proj_dir, {}, char_data)
    assert d_ch14["verdict"] == "LIKELY_CORRECT_ATTRIBUTION"
    assert d_ch14["suggested_speaker"] == "jarlaxle"

    # Chapter 18: ch18_0132 speech tag attached to preceding dialogue
    (script_dir / "chapter_018.json").write_text(
        json.dumps(
            {
                "chapter_number": 18,
                "chapter_title": "Chapter 18",
                "lines": [
                    {
                        "line_id": "ch18_0131",
                        "speaker": "kyrnill",
                        "speaker_confidence": 0.95,
                        "text": '"You vowed,"',
                    },
                    {
                        "line_id": "ch18_0132",
                        "speaker": "narrator",
                        "text": "she accused.",
                    },
                    {
                        "line_id": "ch18_0133",
                        "speaker": "kyrnill",
                        "speaker_confidence": 0.95,
                        "text": '"To Matron Zhindia. You vowed."',
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    flag_ch18 = {
        "flag_id": "flag-ch18",
        "chapter_number": 18,
        "position_ms": 814147,
        "line_id": "ch18_0132",
        "enriched_data": {
            "active_line": {
                "line_id": "ch18_0132",
                "speaker": "narrator",
                "text": "she accused.",
            },
            "candidate_lines": [
                {"line_id": "ch18_0131", "speaker": "kyrnill", "text": '"You vowed,"'},
                {"line_id": "ch18_0132", "speaker": "narrator", "text": "she accused."},
            ],
        },
    }
    d_ch18 = diagnose_flag(flag_ch18, proj_dir, {}, char_data)
    assert d_ch18["suggested_speaker"] == "kyrnill"
    assert d_ch18["verdict"] == "LIKELY_CORRECT_ATTRIBUTION"
    assert any("kyrnill" in f.lower() or "accused" in f.lower() for f in d_ch18["findings"])
