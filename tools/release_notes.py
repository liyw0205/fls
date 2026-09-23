#!/usr/bin/env python3
"""Extract the changelog section for the version being released."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def extract_release_notes(changelog: str, version: str) -> str:
    heading = f"## {version}"
    lines = changelog.splitlines(keepends=True)

    start = next(
        (index for index, line in enumerate(lines) if line.rstrip("\r\n") == heading),
        None,
    )
    if start is None:
        raise ValueError(f"changelog.md 中未找到版本 {version} 的更新日志")

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^##\s+", lines[index]):
            end = index
            break

    notes = "".join(lines[start:end]).strip()
    if not notes:
        raise ValueError(f"版本 {version} 的更新日志为空")
    return notes + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version-file", default="version.json")
    parser.add_argument("--changelog", default="changelog.md")
    parser.add_argument("--output", default="release-notes.md")
    args = parser.parse_args()

    version_info = json.loads(Path(args.version_file).read_text(encoding="utf-8"))
    version = str(version_info.get("version") or "").strip()
    if not version:
        raise SystemExit("version.json.version 不能为空")

    changelog = Path(args.changelog).read_text(encoding="utf-8")
    notes = extract_release_notes(changelog, version)
    Path(args.output).write_text(notes, encoding="utf-8")
    print(f"Prepared release notes for {version}: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
