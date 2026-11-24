"""Local inference and subtitle rendering for verified media timelines."""

import re
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .subtitles import CommentCue, comment_text, srt_timestamp

_SRT_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ASS_EMOJI = re.compile("[\U0001f1e6-\U0001f1ff\U0001f300-\U0001faff\u2600-\u27bf\ufe0f\u200d\u20e3]")
_ASS_ESCAPE = str.maketrans({"\\": "＼", "{": "｛", "}": "｝"})


class SpeechBlocked(ValueError):
    """The configured local speech engine cannot run."""


@dataclass(frozen=True)
class SpeechCue:
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class AssResult:
    text: str
    omitted_comments: int
    dialogue_count: int


def recognize_local(
    audio: Path, model: Path, *, language: str = "ja", device: Literal["cuda", "cpu"] = "cuda"
) -> list[SpeechCue]:
    required_model_files = ("model.bin", "config.json", "tokenizer.json")
    if not model.is_dir() or not all((model / name).is_file() for name in required_model_files):
        raise SpeechBlocked("local model directory is missing or incomplete")
    if not audio.is_file():
        raise SpeechBlocked("local audio input is missing")
    if device not in {"cuda", "cpu"}:
        raise SpeechBlocked("speech device must be cuda or cpu")
    try:
        import numpy as np
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise SpeechBlocked("speech dependencies are unavailable") from error
    try:
        recognizer = WhisperModel(
            str(model.resolve()),
            device=device,
            compute_type="float16" if device == "cuda" else "int8",
            local_files_only=True,
            num_workers=1,
        )
    except (RuntimeError, ValueError) as error:
        raise SpeechBlocked(f"local speech model cannot initialize on {device}") from error
    with wave.open(str(audio), "rb") as source:
        if (
            source.getnchannels() != 1
            or source.getsampwidth() != 2
            or source.getframerate() != 16000
            or source.getcomptype() != "NONE"
        ):
            raise ValueError("speech audio must be 16 kHz mono signed 16-bit PCM")
        count = source.getnframes()
        frames = source.readframes(count)
    if not count or len(frames) != count * 2:
        raise ValueError("speech audio is empty or truncated")
    # FFmpeg already decoded and aligned this PCM. Avoid decoding it a second time.
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32)
    samples *= np.float32(1 / 32768)
    segments, _ = recognizer.transcribe(
        samples,
        language=language,
        temperature=0.0,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    return [SpeechCue(round(segment.start * 1000), round(segment.end * 1000), segment.text) for segment in segments]


def _bounded_speech(cues: list[SpeechCue], duration_ms: int) -> list[SpeechCue]:
    result = []
    for cue in sorted(cues, key=lambda value: (value.start_ms, value.end_ms)):
        start, end = max(0, cue.start_ms), min(cue.end_ms, duration_ms)
        lines = _SRT_CONTROL.sub("", cue.text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
        text = "\n".join(line.strip().replace("-->", "→") for line in lines if line.strip())
        if start < end and text:
            result.append(SpeechCue(start, end, text))
    return result


def render_speech_srt(cues: list[SpeechCue], duration_ms: int) -> str:
    return "\n".join(
        f"{number}\n{srt_timestamp(cue.start_ms)} --> {srt_timestamp(cue.end_ms)}\n{cue.text}\n"
        for number, cue in enumerate(_bounded_speech(cues, duration_ms), start=1)
    )


def _ass_time(centiseconds: int) -> str:
    seconds, fraction = divmod(centiseconds, 100)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}.{fraction:02}"


def _ass_text(text: str) -> str:
    return _ASS_EMOJI.sub("", text).translate(_ASS_ESCAPE).replace("\n", r"\N")


def _line(start: int, end: int, style: str, text: str, margin: int = 0) -> str:
    return f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},{style},,0,0,{margin},{text}"


def render_combined_ass(
    speech: list[SpeechCue],
    comments: list[CommentCue] | list[SpeechCue],
    duration_ms: int,
    width: int,
    height: int,
    *,
    max_comment_lanes: int = 3,
    comment_display_ms: int = 4000,
) -> AssResult:
    if min(width, height, max_comment_lanes, comment_display_ms, duration_ms) <= 0:
        raise ValueError("invalid ASS presentation or media dimensions")
    size = max(14, min(round(height * 0.041), round(width * 0.055)))
    margin = max(12, round(height * 0.045))
    style_format = (
        "Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
    )
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: {style_format}
Style: Speech,Meiryo,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,0,2,24,24,{margin},1
Style: Comment,Meiryo,{size},&H0000FFFF,&H0000FFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,0,2,24,24,{margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Text
"""
    lines: list[tuple[int, str]] = []
    bounded = _bounded_speech(speech, duration_ms)
    for index, cue in enumerate(bounded):
        next_start = bounded[index + 1].start_ms if index + 1 < len(bounded) else duration_ms
        display_until = cue.end_ms
        if next_start - cue.end_ms > 400:
            preferred_duration = min(10000, max(2500, len(cue.text) * 400))
            display_until = max(display_until, cue.start_ms + preferred_duration)
        end = min(next_start, duration_ms, display_until) // 10
        start = (cue.start_ms + 9) // 10
        text = _ass_text(cue.text)
        if start < end and text.strip():
            lines.append((start, _line(start, end, "Speech", text)))

    lane_space = (height - margin * 2) // (size * 2) - 1
    lanes = [0] * min(max_comment_lanes, max(0, lane_space))
    omitted = 0
    comment_captions = [
        cue
        if isinstance(cue, SpeechCue)
        else SpeechCue(cue.offset_ms, cue.offset_ms + comment_display_ms, comment_text(cue))
        for cue in comments
    ]
    for cue in sorted(comment_captions, key=lambda value: value.start_ms):
        start = (cue.start_ms + 9) // 10
        end = min(cue.end_ms, duration_ms) // 10
        if cue.start_ms < 0 or start >= end:
            continue
        text = _ass_text(cue.text)
        text_lines = text.count(r"\N") + 1
        lane = next(
            (
                index
                for index in range(len(lanes) - text_lines + 1)
                if all(free_after <= start for free_after in lanes[index : index + text_lines])
            ),
            None,
        )
        if lane is None:
            omitted += 1
            continue
        lanes[lane : lane + text_lines] = [end] * text_lines
        lines.append((start, _line(start, end, "Comment", text, margin + size * 2 * (lane + 1))))
    return AssResult(
        header + "\n".join(line for _, line in sorted(lines, key=lambda entry: entry[0])) + "\n", omitted, len(lines)
    )
