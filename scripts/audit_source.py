#!/usr/bin/env python3
"""Independent repository checks for sensitive literals and unreadable text files."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", "__pycache__"}
PATTERNS = {
    "GitHub token": re.compile(rb"ghp_[A-Za-z0-9]{36}"),
    "GitHub fine-grained token": re.compile(rb"github_pat_[A-Za-z0-9_]{40,}"),
    "AWS access key": re.compile(rb"AKIA[0-9A-Z]{16}"),
    "generic API key": re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    "private home path": re.compile(rb"/home/[A-Za-z0-9._-]+/"),
}


def main() -> int:
    failures: list[str] = []
    checked = 0
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
            continue
        if path.suffix == ".pyc":
            continue
        relative = path.relative_to(ROOT)
        data = path.read_bytes()
        checked += 1
        if b"\x00" in data:
            failures.append(f"{relative}: contains a NUL byte")
            continue
        if len(data) > 1_000_000:
            failures.append(f"{relative}: file exceeds 1 MB")
        if b"\xe2\x80\x94" in data:
            failures.append(f"{relative}: contains an em dash")
        for label, pattern in PATTERNS.items():
            if pattern.search(data):
                failures.append(f"{relative}: contains a {label} shaped literal")
    if failures:
        for failure in failures:
            print(f"SOURCE AUDIT FAIL: {failure}", file=sys.stderr)
        return 1
    print(f"SOURCE AUDIT PASS: {checked} readable files, no sensitive literals")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
