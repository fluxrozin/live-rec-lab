import json
import os
import socket
import subprocess
import sys
import time
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path

import pytest

from live_rec_lab.isolation import FixtureWorkspace


def cli(*args, check=True):
    return subprocess.run(
        [sys.executable, "-m", "live_rec_lab.cli", "fixture", *map(str, args)],
        capture_output=True,
        text=True,
        check=check,
        timeout=8,
    )


def free_port():
    with closing(socket.socket()) as socket_:
        socket_.bind(("127.0.0.1", 0))
        return socket_.getsockname()[1]


def test_web_port_defaults_to_8083_and_is_persistent_without_opening_other_storage(tmp_path, monkeypatch):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    recording_root = tmp_path / "other-app"
    monkeypatch.setenv("LIVE_REC_LAB_RECORDING_ROOT", str(recording_root))
    before = workspace.database.read_bytes()

    read = json.loads(cli("config", workspace.root, "get", "web_port").stdout)
    assert read == {"web_port": 8083, "revision": 0}
    assert workspace.database.read_bytes() == before

    saved = json.loads(cli("config", workspace.root, "set", "web_port", 19035).stdout)
    assert saved == {"web_port": 19035, "revision": 1}
    assert json.loads(cli("config", workspace.root, "get", "web_port").stdout) == saved
    assert cli("config", workspace.root, "set", "web_port", 0, check=False).returncode != 0
    assert not recording_root.exists()


def test_owned_server_writes_endpoint_and_stop_waits_for_cleanup(tmp_path):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    port = free_port()
    cli("config", workspace.root, "set", "web_port", port)
    before = json.loads(cli("status", workspace.root).stdout)
    assert before["state"] == "stopped"
    assert not (workspace.events.parent / "runtime").exists()
    endpoint_file = workspace.events.parent / "runtime" / "endpoint.json"
    with subprocess.Popen(
        [sys.executable, "-m", "live_rec_lab.cli", "fixture", "serve", str(workspace.root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as child:
        try:
            deadline = time.monotonic() + 5
            while not endpoint_file.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            assert endpoint_file.is_file()
            endpoint = json.loads(endpoint_file.read_text())
            assert endpoint["pid"] == child.pid
            assert endpoint["port"] == port
            actual = json.loads(cli("status", workspace.root).stdout)
            assert actual["state"] == "running" and actual["instance_id"] == endpoint["instance_id"]
            assert cli("url", workspace.root).stdout.strip() == f"http://127.0.0.1:{port}/"
            assert cli("config", workspace.root, "set", "web_port", free_port(), check=False).returncode != 0
            with closing(HTTPConnection("127.0.0.1", port, timeout=2)) as browser:
                browser.request("GET", "/api/v1/health")
                response = browser.getresponse()
                assert response.status == 200 and json.loads(response.read())["ready"]

            other = FixtureWorkspace.initialize(tmp_path / "other-fixture")
            copied_endpoint = other.events.parent / "runtime" / endpoint_file.name
            copied_endpoint.parent.mkdir(mode=0o700)
            copied_endpoint.write_bytes(endpoint_file.read_bytes())
            copied_endpoint.chmod(0o600)
            assert json.loads(cli("status", other.root).stdout)["state"] == "unconfirmed"
            assert cli("stop", other.root, check=False).returncode != 0
            assert child.poll() is None

            stopped = json.loads(cli("stop", workspace.root).stdout)
            assert stopped["state"] == "stopped"
            assert child.wait(timeout=3) == 0
            assert not endpoint_file.exists()
            assert cli("url", workspace.root, check=False).stdout == ""
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)


def test_stale_endpoint_cannot_stop_unrelated_process(tmp_path):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    runtime = workspace.events.parent / "runtime"
    runtime.mkdir(mode=0o700)
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"]) as other:
        try:
            stat = Path(f"/proc/{other.pid}/stat").read_text()
            started_ticks = int(stat[stat.rindex(")") + 2 :].split()[19])
            endpoint = {
                "mode": "fixture",
                "pid": other.pid,
                "port": 18083,
                "url": "http://127.0.0.1:18083/",
                "started_at": "2026-11-24T00:00:00+00:00",
                "instance_id": "7" * 32,
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "start_ticks": started_ticks,
            }
            path = runtime / "endpoint.json"
            path.write_text(json.dumps(endpoint), encoding="utf-8")
            path.chmod(0o600)
            assert json.loads(cli("status", workspace.root).stdout)["state"] == "unconfirmed"
            assert cli("url", workspace.root, check=False).stdout == ""
            assert cli("stop", workspace.root, check=False).returncode != 0
            assert other.poll() is None
        finally:
            if other.poll() is None:
                other.terminate()
                other.wait(timeout=3)


def test_occupied_port_is_not_replaced_automatically(tmp_path):
    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    with closing(socket.socket()) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        cli("config", workspace.root, "set", "web_port", port)
        refused = cli("serve", workspace.root, check=False)
        assert refused.returncode == 1
        assert str(port) in refused.stderr
        assert refused.stdout == ""
        assert os.path.exists(workspace.database)
        assert not (workspace.events.parent / "runtime" / "endpoint.json").exists()


@pytest.mark.parametrize("action", ["status", "url", "stop"])
def test_control_commands_refuse_an_unverified_workspace(tmp_path, action):
    result = cli(action, tmp_path / "missing", check=False)
    assert result.returncode != 0
    assert not (tmp_path / "missing").exists()
