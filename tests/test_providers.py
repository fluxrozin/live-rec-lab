import httpx
import pytest

from live_rec_lab.providers import LiveState, parse_tiktok_room, parse_twitcasting_comments, parse_twitcasting_live


def test_tiktok_does_not_treat_ambiguous_states_as_live():
    def parse(status, room_id="12345"):
        return parse_tiktok_room(
            httpx.Response(
                200,
                json={
                    "data": {
                        "user": {"roomId": room_id},
                        "liveRoom": {"status": status, "title": "配信"},
                    }
                },
            )
        )

    assert parse(2).state is LiveState.LIVE
    assert parse(2).live_id == "12345"
    assert parse(4).state is LiveState.OFFLINE
    assert parse(1).state is LiveState.UNKNOWN
    assert parse(3).state is LiveState.UNKNOWN
    assert parse(2, "").state is LiveState.UNKNOWN


@pytest.mark.parametrize(("code", "reason"), [(403, "access_denied"), (429, "rate_limited"), (503, "upstream_error")])
def test_tiktok_classifies_http_status_before_non_json_body(code, reason):
    report = parse_tiktok_room(httpx.Response(code, text="<html>"))
    assert report.state is LiveState.ERROR
    assert report.reason == reason


def test_twitcasting_verifies_account_and_protection():
    base = {
        "id": "12345",
        "user_id": "42",
        "is_live": True,
        "title": "紙とインクの夜",
        "is_protected": False,
        "is_membership": False,
        "is_premier": False,
    }

    def parse(movie):
        response = httpx.Response(200, json={"movie": movie, "broadcaster": {"id": "42", "screen_id": "paper"}})
        return parse_twitcasting_live(response, "paper")

    assert parse(base).live_id == "12345"
    assert parse({**base, "user_id": "99"}).state is LiveState.UNKNOWN
    assert parse({**base, "is_membership": True}).state is LiveState.RESTRICTED
    assert parse_twitcasting_live(httpx.Response(404), "paper").state is LiveState.UNKNOWN


def test_twitcasting_parses_sparse_comments_without_guessing_anonymous_number():
    response = httpx.Response(
        200,
        json={
            "movie_id": "123",
            "all_count": 5,
            "comments": [
                {"id": "90", "message": "最新", "from_user": {"screen_id": "c:tw1"}, "created": 1479579471},
                {"id": "83", "message": "前の投稿", "from_user": None, "created": 1479579470},
            ],
        },
    )

    page = parse_twitcasting_comments(response, "123")

    assert [comment.source_id for comment in page.comments] == ["90", "83"]
    assert page.total_count == 5
    assert page.comments[0].anonymous is True
    assert page.comments[0].author_id is None
    assert page.comments[0].anonymous_number is None


def test_twitcasting_rejects_comments_for_other_movie():
    with pytest.raises(ValueError, match="movie_mismatch"):
        parse_twitcasting_comments(httpx.Response(200, json={"movie_id": "2", "comments": []}), "1")


def test_service_input_normalization_never_accepts_arbitrary_urls():
    from live_rec_lab.providers.target import normalize_target

    assert normalize_target("tiktok", " https://www.tiktok.com/@night_boat/live ") == "night_boat"
    assert normalize_target("tiktok", "@night.boat") == "night.boat"
    assert normalize_target("twitcasting", "https://twitcasting.tv/paper/") == "paper"
    for value in (
        "https://tiktok.com.evil.example/@allowed",
        "https://tiktok.com:8443/@allowed",
        "https://tiktok.com@external.example/@allowed",
        "https://tiktok.com/@allowed%2f..",
        "https://tiktok.com/@allowed?fragment=1",
        "https://tiktok.com/@allowed#live",
        "ni\tght",
        "..",
    ):
        with pytest.raises(ValueError):
            normalize_target("tiktok", value)


def test_public_room_probe_requires_matching_scope_and_uses_no_signed_service(tmp_path):
    from datetime import UTC, datetime, timedelta

    from live_rec_lab.budget import ExternalBudget, LivePermission
    from live_rec_lab.providers.monitor import ProbeClient

    directory = tmp_path / "shared"
    directory.mkdir(mode=0o700)
    requests = []

    def transport(request):
        requests.append(request)
        assert str(request.url).startswith("https://www.tiktok.com/api-live/user/room/?")
        assert "authorization" not in request.headers
        assert request.url.params["uniqueId"] == "night_boat"
        return httpx.Response(200, json={"data": {"user": {"roomId": "123"}, "liveRoom": {"status": 2}}})

    monitor = ProbeClient(
        ExternalBudget(directory / "external-budget.sqlite3"), transport=httpx.MockTransport(transport)
    )
    assert monitor.tiktok("night_boat", None).state is LiveState.UNKNOWN
    assert requests == []
    permission = LivePermission("tiktok", "night_boat", frozenset({"detect"}), datetime.now(UTC) + timedelta(minutes=1))
    assert monitor.tiktok("night_boat", permission).live_id == "123"
    assert len(requests) == 1


def test_twitcasting_short_page_paginates_until_known_sparse_boundary(tmp_path):
    from datetime import UTC, datetime, timedelta

    from live_rec_lab.budget import ExternalBudget, LivePermission
    from live_rec_lab.providers.monitor import ProbeClient

    directory = tmp_path / "shared"
    directory.mkdir(mode=0o700)
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.path.endswith("current_live"):
            return httpx.Response(
                200,
                json={
                    "movie": {
                        "id": "123",
                        "user_id": "42",
                        "is_live": True,
                        "is_protected": False,
                        "is_membership": False,
                        "is_premier": False,
                    },
                    "broadcaster": {"id": "42", "screen_id": "paper"},
                },
            )
        assert request.url.path == "/movies/123/comments"
        assert request.headers["Authorization"].startswith("Basic ")
        offset = int(request.url.params["offset"])
        rows = {0: ["1000", "777"], 2: ["205", "99"]}[offset]
        return httpx.Response(
            200,
            json={
                "movie_id": "123",
                "all_count": 100,
                "comments": [{"id": value, "message": value} for value in rows],
            },
        )

    permit = LivePermission(
        "twitcasting", "paper", frozenset({"detect", "comments"}), datetime.now(UTC) + timedelta(minutes=1)
    )
    client = ProbeClient(ExternalBudget(directory / "network.sqlite3"), transport=httpx.MockTransport(respond))
    observation = client.twitcasting("paper", permit, "fake-client", "fake-secret")
    batch = client.twitcasting_comments(
        "paper",
        observation,
        permit,
        "fake-client",
        "fake-secret",
        previous_id="205",
    )
    assert batch.complete
    assert batch.latest_id == "1000"
    assert [comment.source_id for comment in batch.comments] == ["1000", "777"]
    assert [call.url.params["offset"] for call in calls[1:]] == ["0", "2"]
    restricted = client.twitcasting_comments(
        "other",
        observation,
        permit,
        "fake-client",
        "fake-secret",
        previous_id="205",
    )
    assert not restricted.complete and len(calls) == 3


def test_twitcasting_moving_or_incomplete_pages_do_not_advance_cursor():
    from live_rec_lab.providers import CommentPage, ProviderComment
    from live_rec_lab.providers.comment_pages import collect_comment_pages

    def mock(offset):
        ids = {0: ["100", "85"], 2: ["85", "77"], 4: []}[offset]
        return CommentPage(tuple(ProviderComment(i, i, None, None, None) for i in ids), 6)

    batch = collect_comment_pages(mock, previous_id="15", max_pages=4)
    assert not batch.complete
    assert batch.latest_id == "15"
    assert batch.reason == "pagination_shifted"
    assert [comment.source_id for comment in batch.comments] == ["100", "85", "77"]


def test_twitcasting_mid_page_rate_limit_keeps_observed_comments_and_old_cursor():
    from live_rec_lab.providers import CommentPage, ProviderComment
    from live_rec_lab.providers.comment_pages import CommentPageError, collect_comment_pages

    def mock(offset):
        if offset:
            raise CommentPageError("rate_limited")
        return CommentPage((ProviderComment("100", "first", None, None, None),), 3)

    batch = collect_comment_pages(mock, previous_id="72", max_pages=4)
    assert not batch.complete and batch.latest_id == "72"
    assert batch.reason == "rate_limited"
    assert len(batch.comments) == 1


def test_twitcasting_pull_persists_observations_without_advancing_a_partial_cursor(tmp_path):
    import sqlite3
    from contextlib import closing

    from live_rec_lab.isolation import FixtureWorkspace
    from live_rec_lab.providers import LiveObservation, ProviderComment
    from live_rec_lab.providers.collection import sync_twitcasting_comments
    from live_rec_lab.providers.comment_pages import CommentBatch

    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    with closing(sqlite3.connect(workspace.database)) as db, db:
        db.execute("INSERT INTO streamers (platform, normalized_id, enabled) VALUES ('twitcasting', 'paper', 1)")
        db.execute("""
            INSERT INTO stream_sessions (streamer_id, source_id, title, started_at, stream_state)
            VALUES (1, '442', '架空ツイキャス', '2026-11-24T03:00:00+00:00', 'live')
        """)
    checkpoints = []
    observation = LiveObservation(LiveState.LIVE, live_id="442", service="twitcasting", account="paper")
    item = ProviderComment("110", "匿名の架空コメント", None, None, None, anonymous=True)

    class MockClient:
        def twitcasting_comments(self, _account, _observation, _permission, _client, _secret, *, previous_id):
            checkpoints.append(previous_id)
            with workspace.exclusive():
                pass
            if len(checkpoints) == 1:
                return CommentBatch((item,), previous_id, False, "rate_limited")
            return CommentBatch((item,), "110", True)

    first = sync_twitcasting_comments(workspace, 1, MockClient(), observation, None, "account", "secret")
    assert first.status == "partial" and first.saved == 1 and first.latest_id is None
    second = sync_twitcasting_comments(workspace, 1, MockClient(), observation, None, "account", "secret")
    assert second.status == "complete" and second.saved == 0 and second.latest_id == "110"
    third = sync_twitcasting_comments(workspace, 1, MockClient(), observation, None, "account", "secret")
    assert checkpoints == [None, None, "110"]
    assert third.saved == 0
    with closing(sqlite3.connect(workspace.database)) as db:
        assert db.execute("SELECT recording_offset_ms, offset_source FROM comments").fetchone() == (None, "unverified")
        assert db.execute("SELECT is_anonymous, anonymous_number FROM comment_provider_attributes").fetchone() == (
            1,
            None,
        )


def test_twitcasting_preserves_inflight_comments_when_detection_ends_the_session(tmp_path):
    import sqlite3
    from contextlib import closing

    from live_rec_lab.isolation import FixtureWorkspace
    from live_rec_lab.providers import LiveObservation, ProviderComment
    from live_rec_lab.providers.collection import sync_twitcasting_comments
    from live_rec_lab.providers.comment_pages import CommentBatch

    workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
    with closing(sqlite3.connect(workspace.database)) as database, database:
        database.execute("INSERT INTO streamers (platform, normalized_id, enabled) VALUES ('twitcasting', 'paper', 1)")
        database.execute("""
            INSERT INTO stream_sessions (streamer_id, source_id, title, started_at, stream_state)
            VALUES (1, '442', '隔離試験', '2026-11-24T03:00:00+00:00', 'live')
        """)

    class MockClient:
        def twitcasting_comments(self, *_args, **_kwargs):
            with closing(sqlite3.connect(workspace.database)) as database, database:
                database.execute("UPDATE stream_sessions SET stream_state = 'ended'")
            return CommentBatch((ProviderComment("110", "受信済み", None, None, None),), "110", True)

    observation = LiveObservation(LiveState.LIVE, live_id="442", service="twitcasting", account="paper")
    report = sync_twitcasting_comments(workspace, 1, MockClient(), observation, None, "get", "no-secret")

    assert report.saved == 1 and report.status == "complete"
    with closing(sqlite3.connect(workspace.database)) as database:
        assert database.execute("SELECT stream_state, comment_state FROM stream_sessions").fetchone() == (
            "ended",
            "partial",
        )
