from pathlib import Path

import pytest

from live_rec_lab.speech import SpeechBlocked, SpeechCue, recognize_local, render_combined_ass, render_speech_srt
from live_rec_lab.subtitles import CommentCue


def test_speech_srt_keeps_japanese_emoji_and_media_time():
    cues = [SpeechCue(-100, 420, "冒頭🙂"), SpeechCue(500, 2400, "次の発話"), SpeechCue(2001, 2300, "範囲外")]

    srt = render_speech_srt(cues, 2000)

    assert "00:00:00,000 --> 00:00:00,420\n冒頭🙂" in srt
    assert "00:00:00,500 --> 00:00:02,000\n次の発話" in srt
    assert "範囲外" not in srt


def test_missing_local_model_stays_blocked_without_running_inference(tmp_path):
    with pytest.raises(SpeechBlocked, match="local model"):
        recognize_local(Path("not-opened.wav"), tmp_path / "large-v3")


def test_incomplete_model_cannot_trigger_tokenizer_download(tmp_path):
    model = tmp_path / "large-v3"
    model.mkdir()
    (model / "model.bin").touch()
    (model / "config.json").write_text("{}")

    with pytest.raises(SpeechBlocked, match="local model"):
        recognize_local(Path("not-opened.wav"), model)


def test_combined_ass_is_one_track_and_bounds_lanes_and_escapes():
    result = render_combined_ass(
        [SpeechCue(500, 700, "音声🙂")],
        [
            CommentCue(510, "改行\n{\\pos(0,0)}🙂", "観客", "user"),
            CommentCue(520, "レーン超過", None, None),
        ],
        2500,
        1080,
        1920,
        max_comment_lanes=2,
    )
    assert result.omitted_comments == 1
    assert "Style: Speech" in result.text and "Style: Comment" in result.text
    assert "0:00:00.50,0:00:02.50,Speech" in result.text
    assert "Pos" not in result.text
    assert r"{\pos(0,0)}" not in result.text
    assert "🙂" not in result.text
    assert r"改行\N｛＼pos(0,0)｝ (観客 | @user)" in result.text
