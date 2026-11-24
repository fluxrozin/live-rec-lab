import struct
import sys
import wave
from pathlib import Path
from types import ModuleType, SimpleNamespace

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


def test_recognition_uses_ffmpeg_pcm_without_another_decoder(tmp_path, monkeypatch):
    numpy = pytest.importorskip("numpy")
    model = tmp_path / "large-v3-turbo"
    model.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (model / name).touch()
    audio = tmp_path / "owned.wav"
    audio_samples = struct.pack("<4h", -32768, 0, 16384, 32767)

    def write_audio(frequency):
        with wave.open(str(audio), "wb") as track:
            track.setparams((1, 2, frequency, 0, "NONE", "not compressed"))
            track.writeframes(audio_samples)

    class StubWhisperModel:
        def __init__(self, path, **options):
            assert path == str(model)
            assert options["local_files_only"] is True

        def transcribe(self, samples, **options):
            assert samples.dtype == numpy.float32
            assert samples.tolist() == [-1.0, 0.0, 0.5, 32767 / 32768]
            assert options["vad_filter"] is True
            return [SimpleNamespace(start=0.1, end=0.3, text="日本語")], None

    fake_whisper = ModuleType("faster_whisper")
    fake_whisper.WhisperModel = StubWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_whisper)
    write_audio(16000)
    assert recognize_local(audio, model, device="cpu") == [SpeechCue(100, 300, "日本語")]
    write_audio(44100)
    with pytest.raises(ValueError, match="16 kHz mono"):
        recognize_local(audio, model, device="cpu")
