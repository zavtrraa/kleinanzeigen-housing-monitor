"""Verify that the housing monitor can be staged without private runtime state."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED_PATHS = (
    ".env",
    ".env.production",
    "data/state.sqlite3",
    ".superpowers/sdd/release/progress.md",
)
PUBLISHED_PATHS = (".env.example", "config/runtime.yaml", "docs/verification.md")
REQUIRED_PATHS = ("LICENSE", ".github/workflows/verify.yml")


def is_ignored(path: str) -> bool:
    result = subprocess.run(["git", "check-ignore", "-q", "--", path], cwd=ROOT, check=False)
    if result.returncode not in {0, 1}:
        raise RuntimeError(f"git check-ignore failed for {path}")
    return result.returncode == 0


def main() -> int:
    failures = [f"{path} must be ignored" for path in IGNORED_PATHS if not is_ignored(path)]
    failures.extend(
        f"{path} must remain publishable" for path in PUBLISHED_PATHS if is_ignored(path)
    )
    failures.extend(
        f"{path} must exist" for path in REQUIRED_PATHS if not (ROOT / path).is_file()
    )
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]
    if "filelock>=4.2.1,<5" not in dependencies:
        failures.append("filelock must require a fixed version range")
    if failures:
        print("Publication verification failed:", *failures, sep="\n- ")
        return 1
    print("Publication boundary verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
