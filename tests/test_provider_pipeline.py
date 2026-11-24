import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from threading import Event

import httpx
import pytest

from live_rec_lab.budget import ExternalBudget, LivePermission
from live_rec_lab.isolation import FixtureWorkspace
from live_rec_lab.media import _make_fixture_hls
from live_rec_lab.providers import LiveState, ProviderComment
from live_rec_lab.providers.comment_pages import CommentBatch
from live_rec_lab.providers.monitor import ProbeClient
from live_rec_lab.providers.tiktok_events import TikTokCommentBatch

_START = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
_ACCOUNT = "night_boat"


class Service:
    def __init__(self, tmp_path, platform):
        self.workspace = FixtureWorkspace.initialize(tmp_path / "fixture")
        self.platform = platform
        self.room_id = "123"
        self.state = 2
        self.calls = []
        self.record_entered = Event()
        self.comments_entered = Event()
        with closing(sqlite3.connect(self.workspace.database)) as database, database:
            database.execute(
                "INSERT INTO streamers (id, platform, normalized_id, enabled) VALUES (1, ?, ?, 1)", (platform, _ACCOUNT)
            )
        directory = tmp_path / "external-budget"
        directory.mkdir(mode=0o700)
        self.probe = ProbeClient(ExternalBudget(directory / "budget.sqlite3"), transport=httpx.MockTransport(self.http))
        self.permission = LivePermission(
            platform, _ACCOUNT, frozenset({"detect", "record", "comments"}), datetime.now(UTC) + timedelta(minutes=10)
        )

    def http(self, request):
        self.calls.append((request.url.host, request.url.path))
        if request.url.host == "www.tiktok.com":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "user": {"roomId": self.room_id},
                        "liveRoom": {"status": self.state, "title": "架空配信"},
                    }
                },
            )
        if request.url.path.endswith("current_live"):
            return httpx.Response(
                200,
                json={
                    "movie": {
                        "id": self.room_id,
                        "user_id": "42",
                        "is_live": self.state == 2,
                        "title": "架空配信",
                        "created": int(_START.timestamp()),
                        "is_protected": self.state == 9,
                        "is_membership": False,
                        "is_premier": False,
                        "hls_url": "https://twitcasting.tv/night_boat/metastream.m3u8/?video=1",
                    },
                    "broadcaster": {"id": "42", "screen_id": _ACCOUNT},
                },
            )
        assert request.url.path == f"/movies/{self.room_id}/comments"
        self.comments_entered.set()
        assert self.record_entered.wait(2)
        return httpx.Response(
            200,
            json={
                "movie_id": self.room_id,
                "all_count": 1,
                "comments": [{"id": "50", "message": "架空コメント", "created": int(_START.timestamp()) + 1}],
            },
        )

    def input(self):
        from live_rec_lab.providers.pipeline import RecordingInput

        source = self.workspace.database.parent / "recording-inputs" / self.room_id
        source.mkdir(parents=True, mode=0o700)
        playlist = source / "clip.m3u8"
        _make_fixture_hls(playlist)
        return RecordingInput(self.room_id, playlist, _START, self.platform, _ACCOUNT, self.room_id, complete=True)

    def runner(self, *, record=None, tiktok_comments=None):
        from live_rec_lab.providers.pipeline import CollectionRunner

        return CollectionRunner(
            self.workspace,
            self.probe,
            record=record,
            tiktok_comments=tiktok_comments,
            twitcasting_credentials=("synthetic-id", "synthetic-secret"),
        )

    def tiktok_comments(self, observation, previous):
        assert observation.live_id == self.room_id and observation.account == _ACCOUNT
        self.comments_entered.set()
        assert self.record_entered.wait(2)
        comments = (
            () if previous else (ProviderComment("50", "架空コメント", _START + timedelta(seconds=1), None, None),)
        )
        return TikTokCommentBatch(_ACCOUNT, self.room_id, CommentBatch(comments, "50", True))


@pytest.mark.parametrize("platform", ["tiktok", "twitcasting"])
def test_provider_pipeline_collects_scoped_media_and_comments_then_confirms_end(tmp_path, platform):
    service = Service(tmp_path, platform)
    media = service.input()

    def record(observation):
        assert observation.live_id == media.input_key
        service.record_entered.set()
        assert service.comments_entered.wait(2)
        return media

    runner = service.runner(record=record, tiktok_comments=service.tiktok_comments)
    assert runner.poll(1, None, now=_START).state is LiveState.UNKNOWN
    assert service.calls == []
    live = runner.poll(1, service.permission, now=_START)
    assert live.state is LiveState.LIVE and live.session_id == 1
    assert runner.poll(1, service.permission, now=_START + timedelta(seconds=10)).session_id == 1
    service.state = 4
    assert runner.poll(1, service.permission, now=_START + timedelta(seconds=20)).stream_state == "ending"
    assert runner.poll(1, service.permission, now=_START + timedelta(seconds=36)).stream_state == "ended"

    with closing(sqlite3.connect(service.workspace.database)) as database:
        assert database.execute(
            "SELECT source_id, stream_state, recording_state, comment_state FROM stream_sessions"
        ).fetchall() == [("123", "ended", "completed", "partial")]
        [(video, duration)] = database.execute("SELECT video_path, duration_ms FROM recording_segments").fetchall()
        assert (service.workspace.root / video).is_file() and duration > 1000
        [(subtitle,)] = database.execute("SELECT relative_path FROM subtitle_publications WHERE kind = 'comment'")
        assert "00:00:01,000" in (service.workspace.root / subtitle).read_text()
        assert database.execute("SELECT body FROM comments").fetchall() == [("架空コメント",)]


def test_ambiguous_detection_preserves_open_session_and_recording_failure_preserves_comments(tmp_path):
    service = Service(tmp_path, "tiktok")

    def broken(_):
        service.record_entered.set()
        raise ValueError("unverified media secret https://invalid.example/?token=must-not-log")

    runner = service.runner(record=broken, tiktok_comments=service.tiktok_comments)
    assert runner.poll(1, service.permission, now=_START).session_id == 1
    service.state = 4
    assert runner.poll(1, service.permission, now=_START + timedelta(seconds=1)).stream_state == "ending"
    service.state = 3
    unknown = runner.poll(1, service.permission, now=_START + timedelta(seconds=26))
    assert unknown.state is LiveState.UNKNOWN and unknown.stream_state == "ending"
    service.state = 2
    assert runner.poll(1, service.permission, now=_START + timedelta(seconds=27)).stream_state == "live"
    with closing(sqlite3.connect(service.workspace.database)) as database:
        assert database.execute("SELECT recording_state, comment_state FROM stream_sessions").fetchall() == [
            ("failed", "running")
        ]
        assert database.execute("SELECT count(*) FROM comments").fetchone() == (1,)
    assert "must-not-log" not in service.workspace.events.read_text()


def test_twitcasting_refuses_protected_media_before_calling_any_acquisition_source(tmp_path):
    service = Service(tmp_path, "twitcasting")
    service.state = 9
    runner = service.runner(record=lambda _: pytest.fail("restricted recording ran"))
    assert runner.poll(1, service.permission, now=_START).state is LiveState.RESTRICTED
    assert len(service.calls) == 1
    with closing(sqlite3.connect(service.workspace.database)) as database:
        assert database.execute("SELECT count(*) FROM stream_sessions").fetchone() == (0,)


def test_tiktok_events_use_actual_sdk_fields_and_discard_unconfirmed_or_late_events():
    from types import SimpleNamespace

    from TikTokLive.events import CommentEvent, ConnectEvent
    from TikTokLiveProto.v3.webcast.shared.message import CommonMessageData

    from live_rec_lab.providers import LiveObservation
    from live_rec_lab.providers.tiktok_events import TikTokCommentWindow

    def event(room_id, message_id):
        return CommentEvent(
            common=CommonMessageData(room_id=room_id, msg_id=message_id, create_time=int(_START.timestamp() * 1000)),
            content="架空コメント",
        )

    observation = LiveObservation(LiveState.LIVE, "123", service="tiktok", account=_ACCOUNT)
    denied = TikTokCommentWindow(observation)
    denied.add(event(123, 10))
    assert denied.connect(ConnectEvent(unique_id=_ACCOUNT, room_id=500)) is False
    rejected = denied.finish(None)
    assert (rejected.batch.reason, rejected.batch.comments, rejected.dropped) == ("room_mismatch", (), 1)

    current = TikTokCommentWindow(observation, capacity=2)
    current.add(event(123, 20))
    current.add(event(500, 21))
    assert current.connect(ConnectEvent(unique_id=_ACCOUNT, room_id=123)) is True
    current.add(SimpleNamespace(common=None, user=None, content="IDなし"), received_at=_START)
    current.add(event(123, 25))
    received = current.finish("10", received_until_end=True)
    current.add(event(123, 26))
    assert received.room_id == "123" and received.account == _ACCOUNT and received.dropped == 2
    assert received.batch.latest_id == "10" and not received.batch.complete
    assert [comment.source_id for comment in received.batch.comments] == ["20", None]
    assert received.batch.comments[0].posted_at == _START
    assert received.batch.comments[1].received_at == _START
    with pytest.raises(ValueError, match="closed"):
        current.finish(None)


def test_pipeline_rejects_recordings_and_comments_from_another_room(tmp_path):
    from dataclasses import replace

    service = Service(tmp_path, "tiktok")
    media = replace(service.input(), live_id="unrelated")
    comment = ProviderComment("10", "別Room", _START, None, None)

    runner = service.runner(
        record=lambda _: media,
        tiktok_comments=lambda *_: TikTokCommentBatch(_ACCOUNT, "unrelated", CommentBatch((comment,), "10", True)),
    )
    assert runner.poll(1, service.permission).state is LiveState.LIVE
    with closing(sqlite3.connect(service.workspace.database)) as database:
        assert database.execute("SELECT count(*) FROM comments").fetchone() == (0,)
        assert database.execute("SELECT count(*) FROM recording_segments").fetchone() == (0,)
        assert database.execute("SELECT reason FROM comment_checkpoints").fetchone() == ("room_mismatch",)


def test_stop_signal_refuses_next_comment_page_and_keeps_observed_data(tmp_path):
    service = Service(tmp_path, "twitcasting")
    stop = Event()
    transport_calls = []

    def respond(request):
        if request.url.path.endswith("current_live"):
            return service.http(request)
        transport_calls.append(request.url.path)
        stop.set()
        return httpx.Response(
            200,
            json={
                "movie_id": service.room_id,
                "all_count": 2,
                "comments": [{"id": "8", "message": "取得済み"}],
            },
        )

    service.probe = ProbeClient(service.probe.budget, transport=httpx.MockTransport(respond), stop=stop)
    runner = service.runner()
    assert runner.poll(1, service.permission).state is LiveState.LIVE
    assert runner.poll(1, service.permission).reason == "stopping"
    assert transport_calls == ["/movies/123/comments"]
    with closing(sqlite3.connect(service.workspace.database)) as database:
        assert database.execute("SELECT body FROM comments").fetchone() == ("取得済み",)
        assert database.execute("SELECT latest_id, state FROM comment_checkpoints").fetchone() == (None, "partial")


def test_official_twitcasting_media_candidate_is_scoped_and_has_no_public_repr(tmp_path):
    service = Service(tmp_path, "twitcasting")
    observed = service.probe.twitcasting(_ACCOUNT, service.permission, "synthetic-id", "synthetic-secret")
    assert observed.hls_url == "https://twitcasting.tv/night_boat/metastream.m3u8/?video=1"
    assert "metastream" not in repr(observed)

    from live_rec_lab.providers import _official_twitcasting_hls

    for value in (
        "https://twitcasting.tv@example.invalid/night_boat/metastream.m3u8/",
        "https://127.0.0.1/night_boat/metastream.m3u8/",
        "https://twitcasting.tv/other/metastream.m3u8/",
        "http://twitcasting.tv/night_boat/metastream.m3u8/",
    ):
        assert _official_twitcasting_hls(value, _ACCOUNT) is None


def test_twitcasting_current_live_404_requires_matching_movie_end_evidence(tmp_path):
    service = Service(tmp_path, "twitcasting")
    ended_movie_id = "123"
    paths = []

    def respond(request):
        paths.append(request.url.path)
        if request.url.path.endswith("current_live"):
            return httpx.Response(404)
        assert request.url.path == "/movies/123"
        return httpx.Response(
            200,
            json={
                "movie": {"id": ended_movie_id, "user_id": "42", "is_live": False},
                "broadcaster": {"id": "42", "screen_id": _ACCOUNT},
            },
        )

    probe = ProbeClient(service.probe.budget, transport=httpx.MockTransport(respond))
    creds = ("synthetic-id", "synthetic-secret")
    assert probe.twitcasting(_ACCOUNT, service.permission, *creds).state is LiveState.UNKNOWN
    assert paths == [f"/users/{_ACCOUNT}/current_live"]
    assert probe.twitcasting(_ACCOUNT, service.permission, *creds, known_movie_id="123").state is LiveState.OFFLINE
    ended_movie_id = "567"
    assert probe.twitcasting(_ACCOUNT, service.permission, *creds, known_movie_id="123").state is LiveState.UNKNOWN


def test_recording_name_keeps_first_timezone_for_later_segments(tmp_path):
    from dataclasses import replace

    from live_rec_lab.web.preferences import DisplayPreferences, save_preferences

    service = Service(tmp_path, "tiktok")
    source = service.input()
    inputs = iter((replace(source, complete=False), replace(source, input_key="second-chunk")))
    runner = service.runner(record=lambda _: next(inputs))
    permission = replace(service.permission, operations=frozenset({"detect", "record"}))
    assert runner.poll(1, permission, now=_START).stream_state == "live"

    with closing(sqlite3.connect(service.workspace.database)) as database:
        save_preferences(database, DisplayPreferences(display_timezone="Europe/Paris"))

    assert runner.poll(1, permission, now=_START + timedelta(seconds=10)).stream_state == "live"
    with closing(sqlite3.connect(service.workspace.database)) as database:
        paths = [row[0] for row in database.execute("SELECT video_path FROM recording_segments ORDER BY number")]
    assert paths == [
        "recordings/night_boat/2026-11-24/tiktok_night_boat_20261124_190000_架空配信_s001.mp4",
        "recordings/night_boat/2026-11-24/tiktok_night_boat_20261124_190000_架空配信_s002.mp4",
    ]


def test_second_provider_session_chooses_a_fixed_collision_identifier(tmp_path):
    from dataclasses import replace
    from hashlib import sha256

    service = Service(tmp_path, "tiktok")
    initial = service.input()
    permission = replace(service.permission, operations=frozenset({"detect", "record"}))
    runner = service.runner(record=lambda _: initial)
    assert runner.poll(1, permission, now=_START).session_id == 1

    service.room_id = "124"
    next_input = replace(initial, live_id=service.room_id, input_key="second-room")
    runner = service.runner(record=lambda _: next_input)
    assert runner.poll(1, permission, now=_START).session_id == 2
    with closing(sqlite3.connect(service.workspace.database)) as db:
        paths = [row[0] for row in db.execute("SELECT video_path FROM recording_segments ORDER BY id")]
    key = sha256(b"tiktok:night_boat:124").hexdigest()[:10]
    assert paths[1] == paths[0].replace("_s001.mp4", f"_{key}_s001.mp4")
    for relative in paths:
        assert (service.workspace.root / relative).is_file()
