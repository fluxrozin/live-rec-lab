import json
import os
from datetime import UTC, datetime

import pytest

from live_rec_lab.ops import log_reader


def _row(second, **fields):
    return {
        "timestamp": f"2026-11-23T09:00:{second:02}+0900",
        "level": "INFO",
        "component": "live_rec_lab.worker",
        "message": "処理開始",
        **fields,
    }


def _line(row):
    return json.dumps(row, ensure_ascii=False).encode() + b"\n"


def test_reads_only_configured_files_in_timestamp_order_and_paginates(tmp_path):
    (tmp_path / "app.jsonl").write_bytes(_line(_row(3)) + _line(_row(1)))
    (tmp_path / "app.jsonl.1").write_bytes(_line(_row(2)))
    (tmp_path / "app.jsonl.5").write_bytes(_line(_row(0)))
    (tmp_path / "app.jsonl.6").write_bytes(_line(_row(4)))
    (tmp_path / "other.jsonl").write_bytes(_line(_row(5)))

    result = log_reader.read_logs(tmp_path, page=2, page_size=2)

    assert [entry.timestamp.second for entry in result.entries] == [1, 0]
    assert result.total == 4
    assert result.oldest == datetime(2026, 11, 23, tzinfo=UTC)
    assert result.newest == datetime(2026, 11, 23, 0, 0, 3, tzinfo=UTC)
    assert not result.partial
    assert not result.unavailable


def test_combines_only_recorded_fields_for_filtering(tmp_path):
    (tmp_path / "app.jsonl").write_bytes(
        _line(_row(1, level="WARNING", message="Stream Reset retry 5"))
        + _line(_row(2, level="WARNING", message="Stream Reset", streamer_id=5, stream_session_id=9))
        + _line(_row(3, level="WARNING", message="Stream Reset", streamer_id=5, stream_session_id=9,
                     correlation_id="corr-2", event="retry", job_id=40, service="tiktok"))
        + _line(_row(4, level="WARNING", message="Stream Reset", streamer_id=5, stream_session_id=9,
                     correlation_id="corr-2"))
    )

    result = log_reader.read_logs(
        tmp_path,
        start=datetime(2026, 11, 23, 0, 0, 2, tzinfo=UTC),
        end=datetime(2026, 11, 23, 0, 0, 3, tzinfo=UTC),
        level="warning",
        component="live_rec_lab.worker",
        query="stream reset",
        correlation_id="corr-2",
        streamer_id=5,
        session_id=9,
    )

    assert result.total == 1
    entry, = result.entries
    assert (entry.level, entry.event, entry.job_id, entry.service) == ("WARNING", "retry", 40, "tiktok")
    assert result.oldest.second == 1
    assert result.newest.second == 4
    assert log_reader.read_logs(tmp_path, streamer_id=5).total == 3


def test_masks_displayed_values_before_searching(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGN_API_KEY", "environment-value")
    (tmp_path / "app.jsonl").write_bytes(_line(_row(
        1,
        message="request https://example.test/stream?signature=signed-value Authorization: Bearer bearer-value",
        exception=("Traceback\nsecret=local-value\napi_key=standalone-value\nerror environment-value\n"
                   "TWITCASTING_CLIENT_SECRET=old-value\nbasic operation was retried"),
        correlation_id="local-value",
    )))

    result = log_reader.read_logs(tmp_path, secrets=("local-value",))

    entry, = result.entries
    assert entry.message.startswith("request [url]")
    assert entry.exception.startswith("Traceback\n")
    assert entry.correlation_id is None
    assert entry.exception.endswith("basic operation was retried")
    for value in ("signed-value", "bearer-value", "standalone-value", "environment-value", "local-value", "old-value"):
        assert value not in entry.message + entry.exception
        assert log_reader.read_logs(tmp_path, query=value, secrets=("local-value",)).total == 0


def test_skips_corrupt_and_oversize_lines_without_losing_following_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(log_reader, "_MAX_LINE_BYTES", 256)
    (tmp_path / "app.jsonl").write_bytes(
        b"not JSON\n" + b"\xff\n" + _line(_row(1, timestamp="2026-11-23T09:00:01"))
        + _line(_row(2, message="x" * 300)) + _line(_row(3))
    )

    result = log_reader.read_logs(tmp_path)

    assert [entry.timestamp.second for entry in result.entries] == [3]
    assert result.partial
    assert not result.unavailable


@pytest.mark.parametrize("limit,value", [("_MAX_FILE_BYTES", 300), ("_MAX_ROWS_PER_FILE", 2)])
def test_reports_limited_source_and_keeps_its_newest_rows(tmp_path, monkeypatch, limit, value):
    monkeypatch.setattr(log_reader, limit, value)
    (tmp_path / "app.jsonl").write_bytes(b"".join(_line(_row(second)) for second in range(10)))

    result = log_reader.read_logs(tmp_path)

    assert [entry.timestamp.second for entry in result.entries] == [9, 8]
    assert result.total == 2
    assert result.partial
    assert not result.unavailable


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "hardlink"])
def test_rejects_nonlocal_and_nonregular_sources(tmp_path, kind):
    allowed = tmp_path / "logs"
    allowed.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(_line(_row(1)))
    candidate = allowed / "app.jsonl"
    if kind == "symlink":
        candidate.symlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(candidate)
    elif kind == "hardlink":
        os.link(outside, candidate)
    else:
        candidate.mkdir()

    result = log_reader.read_logs(allowed)

    assert result.total == 0
    assert result.partial
    assert result.unavailable


def test_rejects_symlinked_directory_components(tmp_path):
    logs = tmp_path / "real" / "logs"
    logs.mkdir(parents=True)
    (logs / "app.jsonl").write_bytes(_line(_row(1)))
    symlink = tmp_path / "alias"
    symlink.symlink_to(logs.parent, target_is_directory=True)

    assert log_reader.read_logs(symlink / "logs").unavailable


@pytest.mark.parametrize("failure", [PermissionError, FileNotFoundError])
def test_reports_open_failure_after_lookup_and_preserves_other_sources(tmp_path, monkeypatch, failure):
    (tmp_path / "app.jsonl").write_bytes(_line(_row(1)))
    (tmp_path / "app.jsonl.1").write_bytes(_line(_row(2)))
    real_open = os.open

    def open_log(path, *args, **kwargs):
        if path == "app.jsonl":
            raise failure(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_log)
    result = log_reader.read_logs(tmp_path)

    assert [entry.timestamp.second for entry in result.entries] == [2]
    assert result.partial
    assert not result.unavailable


def test_distinguishes_empty_logs_from_missing_directory(tmp_path):
    assert log_reader.read_logs(tmp_path).total == 0
    assert not log_reader.read_logs(tmp_path).unavailable
    assert log_reader.read_logs(tmp_path / "missing").unavailable


def test_marks_missing_active_log_when_rotated_data_remains(tmp_path):
    (tmp_path / "app.jsonl.1").write_bytes(_line(_row(1)))

    result = log_reader.read_logs(tmp_path)

    assert result.total == 1
    assert result.partial
    assert not result.unavailable


def test_limits_page_size_on_large_requests(tmp_path):
    (tmp_path / "app.jsonl").write_bytes(_line(_row(1)) * 150)

    result = log_reader.read_logs(tmp_path, page_size=150)

    assert result.total == 150
    assert len(result.entries) == 100
    assert not result.partial


def test_old_log_response_masks_nested_metadata_and_separate_traceback(tmp_path):
    (tmp_path / "app.jsonl").write_bytes(_line(_row(
        1,
        metadata={
            "http_status": 429,
            "headers": {"Authorization": "legacy-basic", "X-RateLimit-Remaining": "0"},
            "stderr": "接続を再試行しました",
        },
        exception_type="TimeoutError",
        traceback="timeout\nAuthorization: Basic dXNlcjpzZWNyZXQ=\n非秘匿の診断情報",
    )))
    entry, = log_reader.read_logs(tmp_path).entries

    assert entry.metadata == {
        "http_status": 429,
        "headers": {"Authorization": "[secret]", "X-RateLimit-Remaining": "0"},
        "stderr": "接続を再試行しました",
    }
    assert entry.exception_type == "TimeoutError"
    assert "dXNlcjpzZWNyZXQ" not in entry.exception
    assert entry.exception.endswith("非秘匿の診断情報")
    assert log_reader.read_logs(tmp_path, query="接続を再試行").total == 1
    assert log_reader.read_logs(tmp_path, query="legacy-basic").total == 0


def test_formatter_masks_structured_credentials_and_records_utc(monkeypatch):
    import logging
    import time

    from live_rec_lab.ops.logging_config import JsonFormatter

    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    try:
        record = logging.makeLogRecord({
            "name": "live_rec_lab.test",
            "msg": "入力エラー url=https://example.test/live?signature=signed-key",
            "created": 0,
            "metadata": {"exit_code": 2, "credentials": {"api_key": "fixture-api-key"}},
        })
        payload = json.loads(JsonFormatter().format(record))
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()

    assert payload["timestamp"] == "1970-01-01T00:00:00+0000"
    assert payload["metadata"]["exit_code"] == 2
    assert payload["metadata"]["credentials"]["api_key"] == "[secret]"
    assert payload["message"] == "入力エラー url=[url]"
