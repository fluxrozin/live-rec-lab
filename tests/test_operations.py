import fcntl
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI, Request

from live_rec_lab import cli
from live_rec_lab.config import DEFAULTS, load_paths, validate_setting
from live_rec_lab.ops.backup import backup_database
from live_rec_lab.web.security import LocalSecurityMiddleware, tokens_match


def test_endpoint_rejects_a_live_pid_without_instance_identity(tmp_path):
    endpoint = tmp_path / "endpoint.json"
    endpoint.write_text(json.dumps({
        "pid": os.getpid(),
        "url": "http://127.0.0.1:8765",
        "instance_id": "b65129bd-c08e-440f-984e-e02035cc0cd8",
        "started_at": datetime.now(UTC).isoformat(),
    }))
    assert cli.read_endpoint(endpoint) is None


@pytest.mark.parametrize("raw", ("[]", '{"pid":"number"}', '"incomplete"', "{"))
def test_endpoint_rejects_malformed_records(tmp_path, raw):
    endpoint = tmp_path / "endpoint.json"
    endpoint.write_text(raw)
    assert cli.read_endpoint(endpoint) is None


def test_start_checks_database_lock_even_with_distinct_state_directory(paths, monkeypatch):
    paths["data"].mkdir(mode=0o700)
    with (paths["data"] / "instance.lock").open("w") as active_instance:
        fcntl.flock(active_instance, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def unexpected_migration():
            pytest.fail("二重起動でマイグレーションを実行しました")

        monkeypatch.setattr(cli, "upgrade_database", unexpected_migration)
        assert cli._start() == 1


def test_backup_does_not_replace_an_existing_snapshot(paths, tmp_path):
    paths["data"].mkdir(mode=0o700)
    with closing(sqlite3.connect(load_paths().db_path)) as source:
        source.execute("CREATE TABLE comments (message TEXT)")
    target = tmp_path / "previous.sqlite"
    target.write_bytes(b"old backup bytes")

    with pytest.raises(FileExistsError):
        backup_database(target)

    assert target.read_bytes() == b"old backup bytes"


def test_online_backup_includes_wal_and_reports_retention_boundary(paths, tmp_path, capsys):
    paths["data"].mkdir(mode=0o700)
    db_path = load_paths().db_path
    snapshot = tmp_path / "snapshot.sqlite"
    with closing(sqlite3.connect(db_path)) as source:
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("PRAGMA wal_autocheckpoint=0")
        source.execute("CREATE TABLE comments (message TEXT)")
        source.execute("INSERT INTO comments VALUES ('保存期限前の本文')")
        source.commit()
        assert db_path.with_name(f"{db_path.name}-wal").stat().st_size > 0
        assert cli._backup(str(snapshot)) == 0
        source.execute("DELETE FROM comments")
        source.commit()
        assert source.execute("SELECT COUNT(*) FROM comments").fetchone() == (0,)

    with closing(sqlite3.connect(snapshot)) as copied:
        assert copied.execute("SELECT message FROM comments").fetchone() == ("保存期限前の本文",)
    assert snapshot.stat().st_mode & 0o777 == 0o600
    captured = capsys.readouterr()
    assert str(snapshot) in captured.out
    assert "自動削除の対象外" in captured.err
    assert "手動復元" in captured.err


def test_backup_rejects_database_sidecar_even_when_absent(paths):
    paths["data"].mkdir(mode=0o700)
    db_path = load_paths().db_path
    with closing(sqlite3.connect(db_path)) as source:
        source.execute("CREATE TABLE comments (message TEXT)")
    wal = db_path.with_name(f"{db_path.name}-wal")
    with pytest.raises(ValueError, match="SQLite"):
        backup_database(wal)
    assert not wal.exists()


def test_setting_can_leave_comments_unlimited_or_expire_after_whole_days():
    assert DEFAULTS["comment_retention_days"] == 0
    assert DEFAULTS["auto_delete_recordings"] is False
    assert validate_setting("comment_retention_days", "35") == 35
    assert validate_setting("comment_retention_days", 0) == 0
    for invalid in (True, -1, 1.5, "1.5", ""):
        with pytest.raises(ValueError):
            validate_setting("comment_retention_days", invalid)


def test_existing_runtime_directory_mode_is_not_changed(paths):
    paths["state"].mkdir(mode=0o755)
    with pytest.raises(ValueError, match="権限"):
        load_paths().ensure()
    assert paths["state"].stat().st_mode & 0o777 == 0o755


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def guarded_web():
    from fastapi.responses import Response

    app = FastAPI()
    app.add_middleware(LocalSecurityMiddleware)

    @app.get("/")
    async def index(request: Request):
        return {"csrf": request.state.csrf}

    @app.post("/")
    async def mutate(request: Request):
        valid = tokens_match(request.state.csrf, request.headers.get("x-csrf-token"))
        return Response(status_code=204 if valid else 403)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123") as client:
        yield client


@pytest.mark.anyio
@pytest.mark.parametrize("origin", ("http://127.0.0.1:8124", "http://localhost:8123", "http://127.0.0.1:8123/path"))
async def test_mutation_rejects_a_different_origin_even_with_a_correct_token(guarded_web, origin):
    token = (await guarded_web.get("/")).json()["csrf"]
    response = await guarded_web.post("/", headers={"Origin": origin, "X-CSRF-Token": token})
    assert response.status_code == 403


@pytest.mark.anyio
async def test_mutation_token_is_not_chosen_by_the_client(guarded_web):
    guarded_web.cookies.set("lrl_csrf", "client-supplied")
    assert (await guarded_web.post("/", headers={"X-CSRF-Token": "client-supplied"})).status_code == 403


@pytest.mark.anyio
async def test_loopback_mutation_allows_exact_origin_and_denies_forwarded_authority(guarded_web):
    response = await guarded_web.get("/")
    token = response.headers["X-CSRF-Token"]
    headers = {"Origin": "http://127.0.0.1:8123", "X-CSRF-Token": token}
    assert (await guarded_web.post("/", headers=headers)).status_code == 204
    assert (await guarded_web.get("/", headers={"Host": "127.0.0.1:invalid"})).status_code == 403
    assert (await guarded_web.get("/", headers={"X-Forwarded-Host": "example.test"})).status_code == 403
