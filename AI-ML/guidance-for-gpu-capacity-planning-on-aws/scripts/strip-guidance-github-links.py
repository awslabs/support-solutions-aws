#!/usr/bin/env python3
"""One-shot: strip aspirational github.com/aws-solutions-library-samples/...
GitHub links from workshop module Markdown files.

Attendees have the guidance code locally in their sandbox (Module 0
downloads a zip and extracts it), so `click to view on GitHub` links
are misleading — the URL 404s until SLIB submission completes.

This script rewrites the mechanical pattern:

    [`src/path.py`](https://github.com/aws-solutions-library-samples/.../blob/main/src/path.py)
    -->
    `src/path.py`

Files with special-case URLs (e.g. Module 6 with anchor fragments,
different text) are left untouched — those need handcrafted edits.

Usage:
    python3 scripts/strip-guidance-github-links.py <file> [<file> ...]
"""
import re
import sys
from pathlib import Path

# Match markdown links of the form:
#   [<link-text>](https://github.com/aws-solutions-library-samples/guidance-for-gpu-capacity-planning-on-aws/(blob|tree)/main/<path>)
# and replace with just <link-text>. Only touches links where the text
# is a backtick-quoted path (the "click to view code" pattern) —
# leaves non-code URLs alone for handcrafted review.
PATTERN = re.compile(
    r'\['
    r'(`[^`]+`)'
    r'\]\('
    r'https://github\.com/aws-solutions-library-samples/'
    r'guidance-for-gpu-capacity-planning-on-aws/'
    r'(?:blob|tree)/main/[^)]+'
    r'\)'
)


def process(path: Path) -> tuple[int, str]:
    src = path.read_text()
    new, n = PATTERN.subn(r'\1', src)
    if n:
        path.write_text(new)
    return n, path.as_posix()


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: strip-guidance-github-links.py <file> [<file> ...]")
        return 2
    total = 0
    for arg in argv:
        p = Path(arg)
        if not p.exists():
            print(f"  SKIP (missing): {p}")
            continue
        n, name = process(p)
        total += n
        print(f"  {name}: {n} replacement(s)")
    print(f"total: {total} replacements")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
