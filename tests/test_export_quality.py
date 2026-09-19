"""Tests for export_quality.json accepted_failures reporting (Spec F16)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain.orchestrator.job_queue import JobQueue
from brain.orchestrator.pipeline import collect_accepted_failures


@pytest.fixture
def project_environment(tmp_path: Path):
    proj_dir = tmp_path / "projects" / "test_book"
    proj_dir.mkdir(parents=True)
    (proj_dir / "script").mkdir(parents=True)

    db_path = tmp_path / "projects" / "pipeline_state.db"
    jq = JobQueue(db_path=str(db_path))
    jq.create_job("test_book", {"title": "Test Book", "author": "Author"})

    # Create chapter 1 script
    script_data = {
        "chapter_number": 1,
        "lines": [
            {"line_id": "ch01_0001", "text": '"Ye comin\' by this on yer own?"'},
            {"line_id": "ch01_0002", "text": '"I am ready."'},
            {"line_id": "ch01_0003", "text": '"Halt!"'},
            {"line_id": "ch01_0004", "text": '"Never."'},
        ],
    }
    (proj_dir / "script" / "chapter_001.json").write_text(json.dumps(script_data), encoding="utf-8")

    return {
        "proj_dir": proj_dir,
        "job_queue": jq,
        "project_id": "test_book",
    }


def test_collect_accepted_failures_reports_accepted_hard_failures(project_environment):
    env = project_environment
    jq: JobQueue = env["job_queue"]
    pid = env["project_id"]

    # Line 1: Hard failure, human-accepted
    jq.log_quality(
        project_id=pid,
        line_id="ch01_0001",
        chapter_number=1,
        attempt=1,
        wer=0.35,
        quality_score=0.7,
        status="fail",
        details={
            "selected": True,
            "passed_hard_gates": False,
            "transcribed_text": "Ye coming by this on your own?",
        },
    )
    jq.set_review_item(
        project_id=pid,
        item_type="segment",
        item_id="ch01_0001",
        disposition="acceptable",
        note="Dialect variation accepted",
    )

    # Line 2: Passing selected take (should NOT appear)
    jq.log_quality(
        project_id=pid,
        line_id="ch01_0002",
        chapter_number=1,
        attempt=1,
        wer=0.0,
        quality_score=1.0,
        status="pass",
        details={
            "selected": True,
            "passed_hard_gates": True,
            "transcribed_text": "I am ready.",
        },
    )
    jq.set_review_item(
        project_id=pid,
        item_type="segment",
        item_id="ch01_0002",
        disposition="approved",
    )

    # Line 3: Ground-rule-9 trap!
    # Attempt 1 was accepted and selected (passed_hard_gates=True)
    # Attempt 2 was an exploratory retry that failed (passed_hard_gates=False)
    # Because Attempt 1 is selected, it must NOT appear in accepted_failures!
    jq.log_quality(
        project_id=pid,
        line_id="ch01_0003",
        chapter_number=1,
        attempt=1,
        wer=0.0,
        quality_score=0.95,
        status="pass",
        details={
            "selected": True,
            "passed_hard_gates": True,
            "transcribed_text": "Halt!",
        },
    )
    jq.log_quality(
        project_id=pid,
        line_id="ch01_0003",
        chapter_number=1,
        attempt=2,
        wer=0.8,
        quality_score=0.2,
        status="fail",
        details={
            "selected": False,
            "passed_hard_gates": False,
            "transcribed_text": "Help!",
        },
    )
    jq.set_review_item(
        project_id=pid,
        item_type="segment",
        item_id="ch01_0003",
        disposition="acceptable",
    )

    # Line 4: Hard failure, but unreviewed (should NOT appear)
    jq.log_quality(
        project_id=pid,
        line_id="ch01_0004",
        chapter_number=1,
        attempt=1,
        wer=0.9,
        quality_score=0.1,
        status="fail",
        details={
            "selected": True,
            "passed_hard_gates": False,
            "transcribed_text": "Ever.",
        },
    )

    failures = collect_accepted_failures(
        project_id=pid,
        project_dir=env["proj_dir"],
        job_queue=jq,
    )

    assert len(failures) == 1
    assert failures[0]["line_id"] == "ch01_0001"
    assert failures[0]["chapter"] == 1
    assert failures[0]["wer"] == 0.35
    assert failures[0]["authored_text"] == '"Ye comin\' by this on yer own?"'
    assert failures[0]["transcript"] == "Ye coming by this on your own?"


def test_collect_accepted_failures_filters_by_chapters(project_environment):
    env = project_environment
    jq: JobQueue = env["job_queue"]
    pid = env["project_id"]

    # ch 1 failure
    jq.log_quality(
        project_id=pid,
        line_id="ch01_0001",
        chapter_number=1,
        attempt=1,
        wer=0.3,
        quality_score=0.7,
        status="fail",
        details={"selected": True, "passed_hard_gates": False, "transcribed_text": "ch1"},
    )
    jq.set_review_item(project_id=pid, item_type="segment", item_id="ch01_0001", disposition="acceptable")

    # ch 2 failure
    jq.log_quality(
        project_id=pid,
        line_id="ch02_0001",
        chapter_number=2,
        attempt=1,
        wer=0.4,
        quality_score=0.6,
        status="fail",
        details={"selected": True, "passed_hard_gates": False, "transcribed_text": "ch2"},
    )
    jq.set_review_item(project_id=pid, item_type="segment", item_id="ch02_0001", disposition="acceptable")

    # Filter only chapter 1
    ch1_failures = collect_accepted_failures(pid, env["proj_dir"], chapters=[1], job_queue=jq)
    assert len(ch1_failures) == 1
    assert ch1_failures[0]["line_id"] == "ch01_0001"

    # All chapters
    all_failures = collect_accepted_failures(pid, env["proj_dir"], chapters=[1, 2], job_queue=jq)
    assert len(all_failures) == 2
