#!/usr/bin/env python3
"""Print the section of CHANGELOG-PLUS.md that belongs to a release tag.

Usage: ``python3 scripts/plus/release_notes.py plus-2026.11.0 CHANGELOG-PLUS.md``

A section starts with a level-2 heading that begins with the tag
(``## plus-2026.11.0 (2026-11-02)``) and ends before the next level-2 heading.
Exits with status 1 when the tag has no section, so a release without notes fails.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TAG_PATTERN = re.compile(r"^plus-\d{4}\.\d{2}\.\d+$")


def release_notes(tag: str, changelog: str) -> str:
    """Body of the changelog section of ``tag`` (without the heading)."""
    lines = changelog.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith("## "):
            if start is not None:
                return "\n".join(lines[start:index]).strip() + "\n"
            heading = line[3:].strip()
            if heading == tag or heading.startswith(tag + " "):
                start = index + 1
    if start is None:
        raise KeyError(tag)
    return "\n".join(lines[start:]).strip() + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    tag, path = argv[1], Path(argv[2])
    if not TAG_PATTERN.match(tag):
        print(f"Tag '{tag}' does not look like plus-YYYY.MM.N", file=sys.stderr)
        return 2
    try:
        sys.stdout.write(release_notes(tag, path.read_text(encoding="utf-8")))
    except KeyError:
        print(f"No section '## {tag}' in {path}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
