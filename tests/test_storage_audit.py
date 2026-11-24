from live_rec_lab import storage_audit
from live_rec_lab.collection import run_fixture
from live_rec_lab.isolation import FixtureWorkspace


def test_storage_audit_reports_managed_size_change_and_unregistered_files(tmp_path):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    fixture = run_fixture(workspace)
    video = workspace.media_path(fixture["video"])
    video.write_bytes(b"shorter modified data")
    unregistered = video.with_name("outside-catalog.mp4")
    unregistered.write_bytes(b"user copy")
    temporary = video.with_name(".pending.part.mp4")
    temporary.write_bytes(b"unfinished")

    first = storage_audit.enqueue_storage_audit(workspace)
    assert storage_audit.enqueue_storage_audit(workspace)["id"] == first["id"]
    assert storage_audit.get_storage_audit(workspace)["current"]["state"] == "queued"

    result = storage_audit.run_storage_audit(workspace)

    assert result["state"] == "completed"
    assert result["counts"]["size_mismatch"] == 1
    assert result["counts"]["normal"] == 1
    assert result["counts"]["unregistered"] == 1
    assert result["counts"]["temporary"] == 1
    assert video.read_bytes() == b"shorter modified data"
    assert unregistered.read_bytes() == b"user copy"
    assert temporary.is_file()
    assert storage_audit.get_storage_audit(workspace)["last_completed"]["id"] == result["id"]


def test_cancel_before_start_and_recover_interrupted_work(tmp_path):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    first = storage_audit.enqueue_storage_audit(workspace)
    assert storage_audit.cancel_storage_audit(workspace, first["id"])["state"] == "cancelled"
    assert storage_audit.run_storage_audit(workspace) is None

    second = storage_audit.enqueue_storage_audit(workspace)
    storage_audit.interrupt_storage_audits(workspace)

    assert storage_audit.get_storage_audit(workspace, second["id"])["state"] == "interrupted"
    assert storage_audit.run_storage_audit(workspace) is None


def test_running_cancellation_keeps_partial_results(tmp_path, monkeypatch):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    run_fixture(workspace)
    request = storage_audit.enqueue_storage_audit(workspace)
    classify = storage_audit._registered_state

    def cancel_then_classify(*args):
        assert storage_audit.cancel_storage_audit(workspace, request["id"])["state"] == "running"
        return classify(*args)

    monkeypatch.setattr(storage_audit, "_registered_state", cancel_then_classify)
    result = storage_audit.run_storage_audit(workspace)
    assert result["state"] == "cancelled"
    assert result["counts"]["normal"] == 2
    assert result["checked"] == 2
    assert storage_audit.get_storage_audit(workspace)["current"] is None
