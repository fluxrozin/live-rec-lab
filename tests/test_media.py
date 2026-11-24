from datetime import datetime

import pytest

from live_rec_lab.media import automatic_stem, publish_exclusive


def test_media_stem_keeps_japanese_and_cleans_windows_characters():
    stem = automatic_stem("tiktok", "night-boat", datetime.fromisoformat("2026-11-24T21:18:00+09:00"), "夜:舟?.", 1000)
    assert stem == "tiktok_night-boat_20261124_211800_夜_舟__s1000"


def test_publish_refuses_case_folded_collision(tmp_path):
    temporary = tmp_path / ".new"
    temporary.write_bytes(b"recording")
    existing = tmp_path / "VIDEO.MP4"
    existing.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        publish_exclusive(temporary, tmp_path / "video.mp4")

    assert existing.read_bytes() == b"existing"
    assert temporary.read_bytes() == b"recording"
