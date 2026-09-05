"""Every function-local import of our own code must actually resolve.

A module-level import that is wrong fails immediately and loudly. One inside a
function body fails only when that branch runs -- and if the branch is inside a
`try`, the failure is swallowed and reported as something else entirely.

Found on 2026-09-05, in the path that removes a failed chapter from
`book_script.json`:

    from brain.utils.file_utils import atomic_write_text

`brain/utils` has never existed; the function lives in `shared/artifacts.py`.
So the prune had never once worked, and every chapter failure since it was
written left the failed chapter in the merged script. It surfaced only because
the S110 pass had just given that `except` a log line, which said:

    Could not prune chapter 8 ...; the merged script keeps the failed
    chapter: No module named 'brain.utils'

This test walks the tree so the next one is caught at commit time instead.
"""

from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIRST_PARTY = {"brain", "shared", "voice", "tests"}
SKIP_DIRS = {"venv", ".venv", "node_modules", ".git", "build", "dist", "__pycache__", "scratch"}


def _python_files() -> list[Path]:
    return [p for p in ROOT.rglob("*.py") if not SKIP_DIRS & set(p.relative_to(ROOT).parts)]


def _resolves(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ModuleNotFoundError, ValueError, AttributeError):
        return False


class LazyImportsResolveTests(unittest.TestCase):
    def test_every_first_party_import_inside_a_function_resolves(self) -> None:
        broken: list[str] = []

        for path in _python_files():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue

            for func in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)):
                for node in ast.walk(func):
                    targets: list[str] = []
                    if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                        targets = [node.module]
                    elif isinstance(node, ast.Import):
                        targets = [alias.name for alias in node.names]

                    for module in targets:
                        if module.split(".")[0] not in FIRST_PARTY:
                            continue
                        if not _resolves(module):
                            rel = path.relative_to(ROOT).as_posix()
                            broken.append(f"{rel}:{node.lineno} imports {module!r}, which does not exist")

        self.assertEqual(
            broken, [], "unresolvable first-party imports inside function bodies:\n  " + "\n  ".join(broken)
        )


if __name__ == "__main__":
    unittest.main()
