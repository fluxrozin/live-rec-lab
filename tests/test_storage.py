from datetime import UTC, datetime

import pytest

from live_rec_lab import storage
from live_rec_lab.storage import broadcast_filename_time, normalize_account, recording_paths, safe_component


def test_safe_component_rejects_reserved():
    import pytest

    with pytest.raises(ValueError):
        safe_component("CON")
    assert safe_component("ok_name") == "ok_name"


def test_one_broadcast_shares_the_start_time_and_increments_the_number(tmp_path):
    broadcast = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    detected = datetime(2026, 11, 23, 12, 4, tzinfo=UTC)
    assert broadcast_filename_time(broadcast, detected) == broadcast
    assert broadcast_filename_time(None, detected) == detected
    _folder, _temp, first = recording_paths(tmp_path, "tiktok", "streamer01", broadcast, "Asia/Tokyo", 1)
    _folder, _temp, second = recording_paths(tmp_path, "tiktok", "streamer01", broadcast, "Asia/Tokyo", 2)
    assert first.parent.name == "2026-11-23"
    assert first.name == "tiktok_streamer01_20261123_210000_s001.mp4"
    assert second.name == "tiktok_streamer01_20261123_210000_s002.mp4"


def test_profile_url_kept():
    parsed = normalize_account("tiktok", "https://www.tiktok.com/@abc")
    assert parsed.profile_url == "https://www.tiktok.com/@abc"


def test_recording_root_requires_compared_existing_configuration(tmp_path, monkeypatch):
    new_root = tmp_path / "new"
    monkeypatch.delenv("LIVE_REC_LAB_EXISTING_RECORDING_ROOTS", raising=False)
    with pytest.raises(ValueError, match="確認"):
        storage.ensure_recording_root(new_root, require_windows=False)
    assert not new_root.exists()


def test_recording_root_checks_windows_parent_identity(tmp_path, monkeypatch):
    old_root = tmp_path / "Old"
    old_root.mkdir()
    monkeypatch.setenv("LIVE_REC_LAB_EXISTING_RECORDING_ROOTS", str(old_root))
    with pytest.raises(ValueError, match="重複"):
        storage.ensure_recording_root(tmp_path / "old" / "new", require_windows=False)
    assert not (tmp_path / "old").exists()


def test_publish_rejects_existing_windows_filename(tmp_path):
    source = tmp_path / ".incoming"
    source.write_bytes(b"ours")
    unrelated = tmp_path / "Movie.MP4"
    unrelated.write_bytes(b"unrelated")

    with pytest.raises(FileExistsError):
        storage.publish_exclusive(source, tmp_path / "movie.mp4")

    assert source.read_bytes() == b"ours"
    assert unrelated.read_bytes() == b"unrelated"
    assert not (tmp_path / "movie.mp4").exists()


def test_managed_path_rejects_an_internal_directory_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="リンク"):
        storage.safe_managed_path(tmp_path, alias / "video.mp4")


def test_folder_mapping_is_stable_across_windows_name_collisions(db, tmp_path):
    first = storage.streamer_directory(db, tmp_path, "Example")
    db.commit()
    second = storage.streamer_directory(db, tmp_path, "example")
    db.commit()

    assert first.name == "Example"
    assert second.name.startswith("example_")
    assert storage.streamer_directory(db, tmp_path, "Example") == first
    assert storage.streamer_directory(db, tmp_path, "example") == second


def test_existing_unmanaged_folder_is_not_taken_over(db, tmp_path):
    unrelated = tmp_path / "Person"
    unrelated.mkdir()

    with pytest.raises(ValueError, match="管理外"):
        storage.streamer_directory(db, tmp_path, "person")

    assert list(unrelated.iterdir()) == []
