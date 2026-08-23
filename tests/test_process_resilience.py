from pathlib import Path
from time import monotonic

import pytest

from brain.orchestrator.voice_client import VoiceClient, VoiceClientError


def test_real_local_voice_outage_fails_with_bounded_retries():
    # Port 9 is not used by this application.  This exercises a real socket
    # refusal rather than a mocked HTTP response while keeping the test local.
    client = VoiceClient(
        host="http://127.0.0.1:9",
        timeout=0.2,
        retries=2,
        retry_delay=0,
    )
    started = monotonic()
    try:
        with pytest.raises(VoiceClientError, match="failed after 2 attempts"):
            client.health_check()
    finally:
        client.close()
    assert monotonic() - started < 3.0


def test_dashboard_restart_recovers_only_after_port_is_confirmed_free():
    script = Path("scripts/restart_dashboard.ps1").read_text(encoding="utf-8")
    port_check = script.index("if ($listener) {")
    stale_end = script.index("schtasks.exe /End /TN $TaskName")
    restart = script.index("schtasks.exe /Run /TN $TaskName")
    assert port_check < stale_end < restart
    assert "remained Running after an explicit end" in script
