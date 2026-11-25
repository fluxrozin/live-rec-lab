"""再起動の停止順序とモードの回帰確認。"""

from live_rec_lab import cli
from live_rec_lab.config import Environment


def test_restart_waits_for_stop_and_keeps_dev_mode(tmp_path, monkeypatch):
    env = Environment(True, tmp_path / "data", tmp_path / "state", tmp_path / "recordings", 8083)
    events = []
    monkeypatch.setattr(cli, "resolve_environment", lambda dev: env if dev else None)
    monkeypatch.setattr(cli, "read_endpoint", lambda _env: {"pid": 123})
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: events.append((pid, sig)))
    monkeypatch.setattr(cli, "lock_held", lambda _path: False)

    def wait_until(predicate, timeout):
        events.append("wait")
        assert timeout == 30
        return predicate()

    def run_server(target, *, allow_live):
        assert target is env
        assert not allow_live
        events.append("start")
        return 0

    monkeypatch.setattr(cli, "wait_until", wait_until)
    monkeypatch.setattr(cli.server, "run_server", run_server)
    assert cli.main(["restart", "--dev"]) == 0
    assert events == [(123, cli.signal.SIGTERM), "wait", "start"]


def test_restart_refuses_to_start_if_stop_times_out(tmp_path, monkeypatch):
    env = Environment(False, tmp_path / "data", tmp_path / "state", tmp_path / "recordings", 8082)
    monkeypatch.setattr(cli, "resolve_environment", lambda dev: env)
    monkeypatch.setattr(cli, "read_endpoint", lambda _env: {"pid": 123})
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(cli, "wait_until", lambda predicate, timeout: False)
    monkeypatch.setattr(cli.server, "run_server", lambda *_args, **_kwargs: 42)
    assert cli.main(["restart"]) == 1


def test_restart_checks_allow_live_before_stopping(tmp_path, monkeypatch):
    env = Environment(False, tmp_path / "data", tmp_path / "state", tmp_path / "recordings", 8082)
    monkeypatch.setattr(cli, "resolve_environment", lambda dev: env)
    monkeypatch.setattr(cli, "read_endpoint", lambda _env: {"pid": 123})

    def unexpected_signal(pid, sig):
        raise AssertionError("無効な引数で停止シグナルを送信した")

    monkeypatch.setattr(cli.os, "kill", unexpected_signal)
    assert cli.main(["restart", "--allow-live"]) == 3


def test_restart_starts_when_already_stopped(tmp_path, monkeypatch, capsys):
    env = Environment(True, tmp_path / "data", tmp_path / "state", tmp_path / "recordings", 8083)
    monkeypatch.setattr(cli, "resolve_environment", lambda dev: env if dev else None)
    monkeypatch.setattr(cli, "read_endpoint", lambda _env: None)

    def run_server(target, *, allow_live):
        assert target is env
        assert allow_live
        return 0

    monkeypatch.setattr(cli.server, "run_server", run_server)
    assert cli.main(["restart", "--dev", "--allow-live"]) == 0
    assert "稼働中のインスタンスはありません" in capsys.readouterr().out
