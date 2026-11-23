import asyncio
import json
import logging

import httpx

from live_rec_lab.ops.logging_config import JsonFormatter
from live_rec_lab.platforms.twitcasting import TwitCastingAdapter


def _adapter(handler) -> TwitCastingAdapter:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return TwitCastingAdapter("id", "secret", client=client)


def test_not_found_is_offline():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": 404, "message": "not found"}})

    adapter = _adapter(handler)
    snap = asyncio.run(adapter.check("caster"))
    asyncio.run(adapter.client.aclose())
    assert snap.status == "OFFLINE"


def test_auth_failure_is_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": 1000, "message": "bad"}})

    adapter = _adapter(handler)
    snap = asyncio.run(adapter.check("caster"))
    asyncio.run(adapter.client.aclose())
    assert snap.status == "ERROR"


def test_comments_are_incremental_and_have_no_anonymous_number():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"X-RateLimit-Remaining": "20"},
            json={
                "comments": [
                    {
                        "id": "10",
                        "message": "モイ",
                        "created": 1700000000,
                        "from_user": {"id": "5", "name": "なな"},
                    },
                    {"id": "8", "message": "古い", "created": 1699990000, "from_user": {"id": "6", "name": "old"}},
                ]
            },
        )

    adapter = _adapter(handler)
    batch = asyncio.run(adapter.comments("caster", "99", "8"))
    asyncio.run(adapter.client.aclose())
    assert [item.platform_comment_id for item in batch.items] == ["10"]
    assert batch.items[0].anonymous_label is None
    assert batch.next_after_id == "10"


def test_check_keeps_official_hls_url():
    hls_url = "http://twitcasting.tv/caster/metastream.m3u8?mode=source"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/current_live")
        return httpx.Response(
            200,
            json={"movie": {"id": "843278207", "is_live": True, "title": "Radio", "hls_url": hls_url}},
        )

    adapter = _adapter(handler)
    snap = asyncio.run(adapter.check("caster"))
    asyncio.run(adapter.client.aclose())
    assert snap.status == "LIVE"
    assert snap.media_url == hls_url


def test_restricted_live_has_no_recording_url():
    hls_url = "http://twitcasting.tv/caster/metastream.m3u8?mode=source"
    for flag in ("is_protected", "is_membership"):

        def handler(request: httpx.Request, flag: str = flag) -> httpx.Response:
            movie = {"id": "1", "is_live": True, "hls_url": hls_url, flag: True}
            return httpx.Response(200, json={"movie": movie})

        adapter = _adapter(handler)
        snap = asyncio.run(adapter.check("caster"))
        asyncio.run(adapter.client.aclose())
        assert snap.status == "LIVE"
        assert snap.media_url is None


def test_low_remaining_blocks_monitor():
    adapter = TwitCastingAdapter("id", "secret", client=httpx.AsyncClient())
    adapter.gate.note(2)
    assert adapter.gate.allow("monitor") is False
    assert adapter.gate.allow("comments") is True
    asyncio.run(adapter.client.aclose())


def test_malformed_current_live_is_not_offline():
    adapter = _adapter(lambda request: httpx.Response(200, json={"movie": {"id": "99"}}))
    snap = asyncio.run(adapter.check("caster"))
    asyncio.run(adapter.client.aclose())
    assert snap.status == "UNKNOWN"


def test_comments_keep_quota_after_monitor_stops():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Basic aWQ6c2VjcmV0"
        assert request.url.path == "/movies/99/comments"
        return httpx.Response(200, json={"movie_id": "99", "comments": []})

    adapter = _adapter(handler)
    adapter.gate.note(2)
    assert asyncio.run(adapter.check("caster")).status == "UNKNOWN"
    assert not asyncio.run(adapter.comments("caster", "99", None)).error
    asyncio.run(adapter.client.aclose())
    assert len(requests) == 1


def test_discontinuity_does_not_delete_program_start():
    from live_rec_lab.recording.hls_playlist import filter_preroll_playlist

    playlist = """#EXTM3U
#EXT-X-MEDIA-SEQUENCE:10
#EXTINF:6,
program-start.ts
#EXT-X-DISCONTINUITY
#EXTINF:6,
after-encoding-change.ts
"""
    filtered = filter_preroll_playlist(playlist, "https://twitcasting.tv/live.m3u8")
    assert filtered.kept_segments == 2
    assert "program-start.ts" in filtered.text
    assert "#EXT-X-DISCONTINUITY" in filtered.text


def test_hls_redirect_is_rejected_before_requesting_untrusted_host():
    from live_rec_lab.recording.hls_playlist import HlsMirror, HlsPlaylistError

    async def run():
        requests = []

        def handler(request):
            requests.append(request.url.host)
            return httpx.Response(302, headers={"Location": "https://untrusted.example/playlist.m3u8"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            mirror = HlsMirror("https://twitcasting.tv/live.m3u8", client=client)
            try:
                await mirror.ffmpeg_input(asyncio.Event())
            except HlsPlaylistError:
                pass
            else:
                raise AssertionError("redirect was accepted")
        assert requests == ["twitcasting.tv"]

    asyncio.run(run())


def test_official_app_quota_recovers_when_reset_expires(monkeypatch):
    import time

    now = [1000]
    monkeypatch.setattr(time, "time", lambda: now[0])
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                403,
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1010"},
                json={"error": {"code": 2000}},
            )
        return httpx.Response(200, json={"movie_id": "99", "comments": []})

    adapter = _adapter(handler)
    result = asyncio.run(adapter.check("caster"))
    assert result.detail == "rate_limit"
    assert result.retry_at == 1010
    assert asyncio.run(adapter.comments("caster", "99", None)).error == "rate_limit_wait"
    now[0] = 1011
    assert not asyncio.run(adapter.comments("caster", "99", None)).error
    asyncio.run(adapter.client.aclose())
    assert len(requests) == 2


def test_comment_api_rejection_first_records_the_upstream_response(caplog):
    adapter = _adapter(lambda _request: httpx.Response(429, headers={"Retry-After": "15"}))
    with caplog.at_level(logging.WARNING, logger="live_rec_lab.platforms.twitcasting"):
        result = asyncio.run(adapter.comments("caster", "99", None))
    asyncio.run(adapter.client.aclose())

    assert result.error == "rate_limit"
    (record,) = caplog.records
    line = json.loads(JsonFormatter().format(record))
    assert line["service"] == "twitcasting"
    assert line["metadata"]["api_endpoint"] == "comments"
    assert line["metadata"]["http_status"] == 429
    assert line["metadata"]["retry_at"] == result.retry_at


def test_comment_pagination_retrieves_more_than_one_incremental_page():
    requests = []

    def handler(request):
        requests.append(request)
        ids = range(100, 50, -1) if len(requests) == 1 else range(50, 0, -1)
        rows = [{"id": str(item), "message": str(item), "from_user": {"id": "c:tw1"}} for item in ids]
        return httpx.Response(200, json={"movie_id": "99", "all_count": 100, "comments": rows})

    adapter = _adapter(handler)
    result = asyncio.run(adapter.comments("caster", "99", "5"))
    asyncio.run(adapter.client.aclose())
    assert requests[0].url.params["slice_id"] == "5"
    assert requests[1].url.params["offset"] == "50"
    assert "slice_id" not in requests[1].url.params
    assert result.error == result.gap == ""
    assert len(result.items) == 95
    assert [item.platform_comment_id for item in result.items[-2:]] == ["7", "6"]
    assert result.items[0].platform_user_id is None
    assert result.next_after_id == "100"
