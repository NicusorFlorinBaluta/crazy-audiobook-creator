"""Summarize recurring native-runtime warnings from managed service logs."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


PATTERNS = {
    "miopen_fallback": re.compile(r"miopen.*(?:fallback|find|workspace)", re.I),
    "memory_allocation": re.compile(r"(?:out of memory|allocation failed|bad alloc)", re.I),
    "native_process_exit": re.compile(r"(?:access violation|segmentation fault|exit code -?\d+)", re.I),
    "openmp_conflict": re.compile(r"(?:libiomp|openmp).*(?:conflict|already initialized)", re.I),
}


def summarize(path: Path) -> dict[str, object]:
    counts = {name: 0 for name in PATTERNS}
    examples: dict[str, list[str]] = {name: [] for name in PATTERNS}
    if not path.is_file():
        return {"path": str(path), "exists": False, "counts": counts, "examples": examples}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        for name, pattern in PATTERNS.items():
            if pattern.search(raw):
                counts[name] += 1
                if len(examples[name]) < 3:
                    examples[name].append(raw[-500:])
    return {"path": str(path), "exists": True, "counts": counts, "examples": examples}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("logs", nargs="+", type=Path)
    args = parser.parse_args()
    print(json.dumps([summarize(path) for path in args.logs], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
