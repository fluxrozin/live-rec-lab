"""Room-bound TikTok SDK payload normalization without opening a signed connection."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import LiveObservation, LiveState, ProviderComment, _numeric_id, _text
from .comment_pages import CommentBatch

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class TikTokCommentBatch:
    account: str
    room_id: str
    batch: CommentBatch
    dropped: int = 0


class TikTokCommentWindow:
    """Buffer one SDK connection generation and release events only after Room confirmation."""

    def __init__(self, observation: LiveObservation, *, capacity: int = 1024) -> None:
        if (
            observation.service != "tiktok"
            or observation.state is not LiveState.LIVE
            or not observation.account
            or not observation.live_id
        ):
            raise ValueError("TikTok comment intake needs a confirmed room and account")
        if type(capacity) is not int or not 1 <= capacity <= 16384:
            raise ValueError("invalid TikTok comment queue capacity")
        self.account = observation.account
        self.room_id = observation.live_id
        self.capacity = capacity
        self._queue: list[ProviderComment] = []
        self._confirmed = False
        self._closed = False
        self._dropped = 0
        self._mismatched = False

    def connect(self, event) -> bool:
        if self._closed or self._mismatched:
            return False
        reported_id = _numeric_id(getattr(event, "room_id", None))
        reported_account = (_text(getattr(event, "unique_id", None)) or "").removeprefix("@")
        self._confirmed = reported_id == self.room_id and reported_account.casefold() == self.account.casefold()
        if not self._confirmed:
            self._dropped += len(self._queue)
            self._queue.clear()
            self._mismatched = True
        return self._confirmed

    def add(self, event, *, received_at: datetime | None = None) -> None:
        if self._closed or self._mismatched:
            self._dropped += 1
            return
        common = getattr(event, "common", None)
        room_id = _numeric_id(getattr(common, "room_id", None))
        body = getattr(event, "content", None)
        if room_id not in (None, self.room_id) or not isinstance(body, str) or len(self._queue) >= self.capacity:
            self._dropped += 1
            return
        user = getattr(event, "user", None)
        milliseconds = getattr(common, "create_time", None)
        posted_at = (
            _EPOCH + timedelta(milliseconds=milliseconds)
            if type(milliseconds) is int and 1_000_000_000_000 <= milliseconds <= 253402300799999
            else None
        )
        self._queue.append(
            ProviderComment(
                _numeric_id(getattr(common, "msg_id", None)),
                body,
                posted_at,
                _text(getattr(user, "display_id", None)) or _numeric_id(getattr(user, "id", None)),
                _text(getattr(user, "nickname", None)),
                received_at=received_at or datetime.now(UTC),
            )
        )

    def finish(self, previous_id: str | None, *, received_until_end: bool = False) -> TikTokCommentBatch:
        if self._closed:
            raise ValueError("TikTok comment connection generation is closed")
        self._closed = True
        if not self._confirmed:
            reason = "room_mismatch" if self._mismatched else "room_unconfirmed"
            return TikTokCommentBatch(
                self.account,
                self.room_id,
                CommentBatch((), previous_id, False, reason),
                self._dropped + len(self._queue),
            )
        complete = received_until_end and self._dropped == 0
        latest_id = next((item.source_id for item in reversed(self._queue) if item.source_id), previous_id)
        reason = None if complete else "unobserved_comments" if self._dropped else "comment_window_incomplete"
        return TikTokCommentBatch(
            self.account,
            self.room_id,
            CommentBatch(tuple(self._queue), latest_id if complete else previous_id, complete, reason),
            self._dropped,
        )
