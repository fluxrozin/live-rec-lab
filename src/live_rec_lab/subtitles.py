"""Render SRT from known recording-relative comment times."""

import re
from dataclasses import dataclass

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class CommentCue:
    offset_ms: int
    body: str
    author_name: str | None
    author_id: str | None


def srt_timestamp(ms: int) -> str:
    seconds, ms = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"


def comment_text(cue: CommentCue) -> str:
    name = " ".join(_CONTROL.sub("", cue.author_name or "").split())
    user_id = " ".join(_CONTROL.sub("", cue.author_id or "").split()).lstrip("@")
    author = " | ".join(filter(None, (name, f"@{user_id}" if user_id else ""))) or "投稿者不明"
    lines = _CONTROL.sub("", cue.body).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    body = "\n".join(line.replace("-->", "→") for line in lines if line.strip())
    return f"{body} ({author})" if body else f"({author})"


def render_comment_srt(cues: list[CommentCue], duration_ms: int, display_ms: int = 4000) -> str:
    blocks = []
    for cue in sorted(cues, key=lambda value: value.offset_ms):
        start = cue.offset_ms
        end = min(start + display_ms, duration_ms)
        if not 0 <= start < end:
            continue
        blocks.append(f"{len(blocks) + 1}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{comment_text(cue)}\n")
    return "\n".join(blocks)
