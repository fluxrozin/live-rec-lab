"""Markdownの構文、相対リンク、行末空白を検査する。"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".venv", ".git"}
IGNORE_PREFIXES = (
    ROOT / ".agents" / "skills",
    ROOT / ".cursor" / "skills",
    ROOT / ".claude" / "skills",
    ROOT / ".specify" / "extensions" / "bug" / "commands",
)
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def documents() -> list[Path]:
    files = []
    for path in ROOT.rglob("*.md"):
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        if any(path.is_relative_to(prefix) for prefix in IGNORE_PREFIXES):
            continue
        files.append(path)
    return files


def main() -> int:
    errors: list[str] = []
    for path in documents():
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(ROOT)
        if text and not text.endswith("\n"):
            errors.append(f"{relative}: ファイル末尾に改行がありません")
        for number, line in enumerate(text.splitlines(), 1):
            if line.rstrip("\n").endswith((" ", "\t")):
                errors.append(f"{relative}:{number}: 行末に空白があります")
        parsed = subprocess.run(
            ["markdown-it", str(path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if parsed.returncode != 0:
            errors.append(f"{relative}: markdown-it が失敗しました: {parsed.stderr.strip()}")
        for match in LINK.finditer(text):
            url = match.group(1)
            if url.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = url.split("#", 1)[0]
            if target and not (path.parent / target).resolve().exists():
                errors.append(f"{relative}: リンク先がありません: {url}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"markdown validator: {len(documents())} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
