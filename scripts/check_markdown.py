"""Markdownの構文、相対リンク、見出し断片、行末を検査する。

実行例:
  markdownlint -c .markdownlint.json -d '**/*.md'
  python3 scripts/check_markdown.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".venv", ".git", "node_modules", ".pytest_cache"}
IGNORE_PREFIXES = (
    ROOT / ".agents" / "skills",
    ROOT / ".cursor" / "skills",
    ROOT / ".claude" / "skills",
    ROOT / ".specify" / "extensions" / "bug" / "commands",
)
# github-slugger が除去する記号。ハイフンと空白は残す。
_SPECIAL = re.compile(r"[\u2000-\u206F\u2E00-\u2E7F\\'\"!#$%&()*+,./:;<=>?@\[\]^`{|}~]")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^(`{3,}|~{3,})")
_HTML_ID = re.compile(r"""<[^>]*\bid=["']([^"']+)["']""")
_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_HTML_TAG = re.compile(r"<[^>]+>")
_CODE_SPAN = re.compile(r"`+")


def documents() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*.md"):
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        if any(path.is_relative_to(prefix) for prefix in IGNORE_PREFIXES):
            continue
        files.append(path)
    return sorted(files)


def _strip_inline_code(text: str) -> str:
    parts: list[str] = []
    cursor = 0
    while cursor < len(text):
        match = _CODE_SPAN.match(text, cursor)
        if not match:
            parts.append(text[cursor:])
            break
        closer = text.find(match.group(0), match.end())
        if closer < 0:
            parts.append(text[cursor:])
            break
        parts.append(text[cursor : match.start()])
        cursor = closer + len(match.group(0))
    return "".join(parts)


def _heading_text(raw: str) -> str:
    text = _HTML_TAG.sub("", raw)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return _strip_inline_code(text).strip()


def _slug(text: str) -> str:
    value = unicodedata.normalize("NFC", text).casefold().strip()
    value = re.sub(r"[\u0000-\u001f]", "", value)
    value = _SPECIAL.sub("", value)
    return re.sub(r"\s", "-", value)


def anchors(text: str) -> set[str]:
    """明示idと、GitHub互換の見出しslugを集める。"""
    found: set[str] = set()
    occurrences: dict[str, int] = {}
    in_fence = False
    fence = ""
    for line in text.splitlines():
        marker = _FENCE.match(line.strip())
        if marker:
            token = marker.group(1)[0]
            if not in_fence:
                in_fence = True
                fence = token
            elif token == fence:
                in_fence = False
                fence = ""
            continue
        if in_fence:
            continue
        visible = _strip_inline_code(line)
        for explicit in _HTML_ID.findall(visible):
            found.add(explicit)
        heading = _HEADING.match(line)
        if not heading:
            continue
        slug = _slug(_heading_text(heading.group(2)))
        if not slug:
            continue
        count = occurrences.get(slug, 0)
        occurrences[slug] = count + 1
        found.add(slug if count == 0 else f"{slug}-{count}")
    return found


def _link_target(raw: str) -> str | None:
    target = raw.strip()
    if not target or target.startswith("<"):
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1].strip()
        else:
            return None
    target = target.split(maxsplit=1)[0]
    if target.startswith(("#", "http://", "https://", "mailto:")):
        return target if target.startswith("#") else None
    return target


def links_outside_code(text: str) -> list[tuple[int, str]]:
    results: list[tuple[int, str]] = []
    in_fence = False
    fence = ""
    for number, line in enumerate(text.splitlines(), 1):
        marker = _FENCE.match(line.strip())
        if marker:
            token = marker.group(1)[0]
            if not in_fence:
                in_fence = True
                fence = token
            elif token == fence:
                in_fence = False
                fence = ""
            continue
        if in_fence:
            continue
        visible = _strip_inline_code(line)
        for match in _LINK.finditer(visible):
            target = _link_target(match.group(1))
            if target is not None:
                results.append((number, target))
    return results


def main() -> int:
    errors: list[str] = []
    files = documents()
    anchor_cache: dict[Path, set[str]] = {}

    def file_anchors(path: Path) -> set[str]:
        cached = anchor_cache.get(path)
        if cached is None:
            cached = anchors(path.read_text(encoding="utf-8"))
            anchor_cache[path] = cached
        return cached

    for path in files:
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(ROOT)
        if text and not text.endswith("\n"):
            errors.append(f"{relative}: ファイル末尾に改行がありません")
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.rstrip("\n")
            if stripped.endswith("\t") or (
                stripped.endswith(" ") and not stripped.endswith("  ")
            ):
                errors.append(f"{relative}:{number}: 行末に空白があります")
            elif stripped.endswith("   "):
                errors.append(f"{relative}:{number}: 行末の空白が2個を超えています")
        parsed = subprocess.run(
            ["markdown-it", str(path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if parsed.returncode != 0:
            detail = parsed.stderr.strip() or parsed.stdout.strip()
            errors.append(f"{relative}: markdown-it が失敗しました: {detail}")
        for number, url in links_outside_code(text):
            fragment = ""
            location = url
            if url.startswith("#"):
                target_path = path
                fragment = url[1:]
            else:
                location, separator, fragment = url.partition("#")
                if not separator:
                    fragment = ""
                target_path = (path.parent / location).resolve()
                try:
                    target_path.relative_to(ROOT.resolve())
                except ValueError:
                    errors.append(f"{relative}:{number}: リポジトリ外へのリンクです: {url}")
                    continue
                if not target_path.exists():
                    errors.append(f"{relative}:{number}: リンク先がありません: {url}")
                    continue
                if target_path.suffix.lower() != ".md" or not fragment:
                    continue
            if fragment and fragment not in file_anchors(target_path):
                errors.append(f"{relative}:{number}: 見出しが見つかりません: {url}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"markdown validator: {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
