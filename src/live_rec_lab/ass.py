"""統合ASS（発話=下中央・白 Speech / コメント=その上の複数レーン・黄 Comment）。単一トラック。"""

from __future__ import annotations

import re

_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF️‍]")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_LANES = 6


def ass_time(ms: int) -> str:
    ms = max(0, int(ms))
    cs = ms // 10
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def escape(text: str) -> str:
    """ASS制御文字・タグ・改行・バックスラッシュを安全化し、ASS本文のみ絵文字を除去する。"""
    t = _CTRL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    t = _EMOJI.sub("", t)
    t = t.replace("\\", "＼").replace("{", "｛").replace("}", "｝")
    return "\\N".join(line.strip() for line in t.split("\n") if line.strip())


def speech_display(cues: list[tuple[int, int, str]], duration_ms: int) -> list[tuple[int, int, str]]:
    out = []
    for i, (s, e, text) in enumerate(cues):
        nxt = cues[i + 1][0] if i + 1 < len(cues) else None
        if nxt is not None and nxt - e <= 400:
            end = e
        else:
            end = s + int(max(2500, min(10000, len(text) * 400)))
        if nxt is not None:
            end = min(end, nxt)
        end = min(end, duration_ms)
        if end > s:
            out.append((s, end, text))
    return out


def build(width: int, height: int, speech: list[tuple[int, int, str]], comments: list[tuple[int, int, str]],
          duration_ms: int) -> tuple[str, dict]:
    """(ASS文字列, 診断{lanes, omitted})。レーン飽和時は決定的に省略し件数を返す。"""
    fs_speech = max(18, int(height * 0.05))
    fs_comment = max(14, int(height * 0.036))
    margin_v = max(10, int(height * 0.04))
    margin_lr = max(20, int(width * 0.05))
    line_h = int(fs_comment * 1.35)
    base_y = margin_v + int(fs_speech * 2.8)  # 発話2行分の上から開始（重なり防止）
    max_lanes = max(1, min(MAX_LANES, (height - base_y - margin_v) // line_h))
    header = (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
        f"PlayResX: {width}\nPlayResY: {height}\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Speech,Yu Gothic UI,{fs_speech},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2,1,2,{margin_lr},{margin_lr},{margin_v},1\n"
        f"Style: Comment,Yu Gothic UI,{fs_comment},&H0000FFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,{margin_lr},{margin_lr},{base_y},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    lines = []
    for s, e, text in speech:
        lines.append(f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Speech,,0,0,0,,{escape(text)}")
    lane_free = [0] * max_lanes
    omitted = 0
    for s, e, text in sorted(comments, key=lambda c: (c[0], c[2])):
        t = escape(text)
        if not t:
            continue
        lane = next((i for i, free in enumerate(lane_free) if free <= s), None)
        if lane is None:
            omitted += 1
            continue
        lane_free[lane] = e
        lines.append(f"Dialogue: 1,{ass_time(s)},{ass_time(e)},Comment,,0,0,{base_y + lane * line_h},,{t}")
    return header + "\n".join(lines) + "\n", {"lanes": max_lanes, "omitted_comments": omitted}
