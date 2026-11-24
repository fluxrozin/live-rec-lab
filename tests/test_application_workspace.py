from pathlib import Path

import pytest

from live_rec_lab.application import ApplicationWorkspace
from live_rec_lab.isolation import IsolationError


def workspace(tmp_path, mode, *, env=None):
    return ApplicationWorkspace.resolve(mode, home=tmp_path, directory=tmp_path, environment=env or {})


def test_mode_paths_and_environment_are_isolated(tmp_path):
    (tmp_path / ".env").write_text("LIVE_REC_LAB_DATA_DIR=/mnt/d/forbidden\n", encoding="utf-8")
    (tmp_path / ".env.dev").write_text("LIVE_REC_LAB_DATA_DIR=/mnt/c/forbidden\n", encoding="utf-8")
    dev = workspace(tmp_path, "developer", env={"LIVE_REC_LAB_STATE_DIR": "/mnt/c/real"})
    assert dev.database == tmp_path / ".local/share/live-rec-lab-dev/data/app.sqlite3"
    assert dev.default_port == 8083
    dev.initialize()
    assert dev.media_path("recordings/sample.mp4") == dev.recordings / "sample.mp4"
    assert not (tmp_path / ".local/share/live-rec-lab").exists()
    assert workspace(tmp_path, "normal").database == Path("/mnt/d/forbidden/app.sqlite3")


def test_existing_unmarked_database_is_never_adopted(tmp_path):
    normal = workspace(tmp_path, "normal")
    normal.database.parent.mkdir(parents=True, mode=0o700)
    normal.database.write_bytes(b"data owned by another app")
    with pytest.raises(IsolationError):
        normal.initialize()
    assert normal.database.read_bytes() == b"data owned by another app"


def test_custom_paths_do_not_overlap_another_mode(tmp_path):
    first = workspace(tmp_path, "normal", env={"LIVE_REC_LAB_STATE_DIR": str(tmp_path / "custom" / "normal")})
    first.initialize()
    second = workspace(
        tmp_path,
        "developer",
        env={
            "DEV_LIVE_REC_LAB_STATE_DIR": str(first.events.parent / "child"),
        },
    )
    with pytest.raises(IsolationError, match="overlap"):
        second.initialize()
    assert not second.events.parent.exists()


def test_diagnosis_does_not_initialize_and_normal_media_fails_closed(tmp_path):
    from live_rec_lab.diagnosis import diagnose_application

    normal = workspace(tmp_path, "normal")
    report = diagnose_application(normal)
    assert report["status"] == "unavailable"
    assert report["data_environment"] == "live"
    assert not (tmp_path / ".local").exists()

    normal.initialize()
    assert normal.default_port == 8082
    with pytest.raises(IsolationError):
        normal.require_media()
    assert normal.recordings == Path("/mnt/d/LiveRecorder")


def test_application_modes_expose_only_the_intended_routes(tmp_path):
    from fastapi.testclient import TestClient

    from live_rec_lab.web.app import create_app

    for mode in ("normal", "developer"):
        target = workspace(tmp_path, mode).initialize()
        origin = f"http://127.0.0.1:{target.default_port}"
        with TestClient(create_app(target, target.default_port), base_url=origin) as client:
            assert client.get("/api/v1/health").json()["mode"] == mode
            docs_status = 404 if mode == "normal" else 200
            assert client.get("/openapi.json").status_code == docs_status
            assert client.get("/docs").status_code == docs_status
            assert client.get("/redoc").status_code == docs_status
            assert "/_dev/" not in client.get("/").text
            diagnosis = client.get("/api/v1/developer/diagnose")
            assert diagnosis.status_code == (404 if mode == "normal" else 200)
            assert ("通常運用" if mode == "normal" else "隔離データ") in client.get("/").text
            assert client.get("/settings").status_code == 200
            assert client.get("/system").status_code == 200


def test_start_url_and_stop_use_own_verified_application_workspace(tmp_path):
    import json
    import socket
    import subprocess
    import sys
    import time

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    bootstrap = (
        "import sys; from pathlib import Path; "
        "from live_rec_lab.application import ApplicationWorkspace as App; "
        "from live_rec_lab.cli import main; "
        "resolve = App.resolve; "
        "App.resolve = staticmethod(lambda mode: resolve(mode, home=Path(sys.argv[1]), "
        "directory=Path(sys.argv[1]), environment={'LIVE_REC_LAB_DISABLE_SCHEDULER': '1'})); "
        "raise SystemExit(main(sys.argv[2:]))"
    )
    executable = [sys.executable, "-c", bootstrap, str(tmp_path)]
    subprocess.run([*executable, "config", "set", "web_port", str(port)], check=True, timeout=5)
    with subprocess.Popen([*executable, "start"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as server:
        try:
            endpoint = workspace(tmp_path, "normal").runtime_path / "endpoint.json"
            deadline = time.monotonic() + 5
            while not endpoint.exists() and server.poll() is None and time.monotonic() < deadline:
                time.sleep(0.04)
            status = json.loads(subprocess.check_output([*executable, "status"], text=True, timeout=5))
            assert status["state"] == "running" and status["mode"] == "normal"
            assert status["pid"] == server.pid and len(status["code_revision"]) == 64
            assert subprocess.check_output([*executable, "url"], text=True, timeout=5).strip() == (
                f"http://127.0.0.1:{port}/"
            )
            stopped = subprocess.check_output([*executable, "status", "--dev"], text=True, timeout=5)
            assert json.loads(stopped)["state"] == "stopped"
            subprocess.run([*executable, "stop"], check=True, timeout=8)
            assert server.wait(timeout=5) == 0 and not endpoint.exists()
        finally:
            if server.poll() is None:
                server.terminate()
                server.wait(timeout=5)


def test_rotated_event_records_remain_searchable_and_redacted(tmp_path, monkeypatch):
    from live_rec_lab import isolation
    from live_rec_lab.web.screen_data import fixture_events

    target = workspace(tmp_path, "developer").initialize()
    monkeypatch.setattr(isolation, "_LOG_BYTES", 150)
    target.event("probe", "first", "aaa", ValueError("client_secret=synthetic-test-value"))
    target.event("probe", "second", "bbb")
    events, state = fixture_events(target)
    assert state == "available"
    assert [event["event"] for event in events] == ["second", "first"]
    assert "［秘匿］" in events[1]["detail"]
    assert "synthetic-test-value" not in repr(events)
    assert "synthetic-test-value" not in target.events.with_name("events.jsonl.1").read_text()
