"""Check local Markdown links without network access."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    failures: list[str] = []
    for document in [root / "README.md", *(root / "docs").glob("*.md")]:
        text = document.read_text(encoding="utf-8", errors="replace")
        for target in LINK.findall(text):
            target = target.strip().strip("<>")
            if not target or target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            local = unquote(target.split("#", 1)[0])
            if not local:
                continue
            candidate = (document.parent / local).resolve()
            if not candidate.exists():
                failures.append(f"{document.relative_to(root)} -> {target}")
    if failures:
        print("Broken local Markdown links:")
        print("\n".join(failures))
        return 1
    print("Local Markdown links are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
