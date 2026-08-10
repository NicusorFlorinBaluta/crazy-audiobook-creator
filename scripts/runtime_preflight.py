"""Read-only CLI for runtime compatibility and environment manifests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.runtime_preflight import collect_runtime_report, write_runtime_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brain-config", default="brain/config.yaml")
    parser.add_argument("--voice-config", default="voice/config.yaml")
    parser.add_argument("--pip-check", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    report = collect_runtime_report(
        brain_config_path=args.brain_config,
        voice_config_path=args.voice_config,
        run_pip_check=args.pip_check,
    )
    if args.output:
        write_runtime_report(args.output, report)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if report["compatible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
