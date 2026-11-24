"""Explicitly budgeted, fixed-endpoint monitoring candidates."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from threading import Event

import httpx

from ..budget import BudgetDenied, ExternalBudget, Limit, LivePermission
from . import (
    CommentPage,
    LiveObservation,
    LiveState,
    parse_tiktok_room,
    parse_twitcasting_comments,
    parse_twitcasting_live,
)
from .comment_pages import CommentBatch, CommentPageError, collect_comment_pages
from .target import normalize_target

_MINUTE = timedelta(minutes=1)


class ProbeClient:
    def __init__(
        self, budget: ExternalBudget, *, transport: httpx.BaseTransport | None = None, stop: Event | None = None
    ) -> None:
        self.budget = budget
        self.transport = transport
        self.stop = stop

    @property
    def stopping(self) -> bool:
        return self.stop is not None and self.stop.is_set()

    def tiktok(self, account: str, permission: LivePermission | None) -> LiveObservation:
        account = normalize_target("tiktok", account)
        try:
            response = self._get(
                "tiktok",
                account,
                permission,
                (Limit("tiktok", 10, _MINUTE),),
                "https://www.tiktok.com/api-live/user/room/",
                params={"uniqueId": account, "sourceType": "54"},
            )
        except (BudgetDenied, httpx.RequestError) as error:
            return _probe_failure(error)
        return replace(parse_tiktok_room(response, account), service="tiktok", account=account)

    def twitcasting(
        self,
        account: str,
        permission: LivePermission | None,
        client_id: str,
        client_secret: str,
        *,
        monitor_limit: int = 10,
        known_movie_id: str | None = None,
    ) -> LiveObservation:
        account = normalize_target("twitcasting", account)
        if not client_id or not client_secret or not 1 <= monitor_limit <= 60:
            return LiveObservation(LiveState.UNKNOWN, reason="configuration_unavailable")
        limits = (Limit("twitcasting", 60, _MINUTE), Limit("twitcasting-monitor", monitor_limit, _MINUTE))
        auth = httpx.BasicAuth(client_id, client_secret)
        try:
            response = self._get(
                "twitcasting",
                account,
                permission,
                limits,
                f"https://apiv2.twitcasting.tv/users/{account}/current_live",
                auth=auth,
            )
            if (
                response.status_code == 404
                and known_movie_id
                and known_movie_id.isascii()
                and known_movie_id.isdecimal()
                and len(known_movie_id) <= 30
            ):
                detail = self._get(
                    "twitcasting",
                    account,
                    permission,
                    limits,
                    f"https://apiv2.twitcasting.tv/movies/{known_movie_id}",
                    auth=auth,
                )
                confirmed = parse_twitcasting_live(detail, account)
                if confirmed.live_id == known_movie_id:
                    return confirmed
        except (BudgetDenied, httpx.RequestError) as error:
            return _probe_failure(error)
        return parse_twitcasting_live(response, account)

    def twitcasting_comments(
        self,
        account: str,
        observation: LiveObservation,
        permission: LivePermission | None,
        client_id: str,
        client_secret: str,
        *,
        previous_id: str | None = None,
        max_pages: int = 4,
    ) -> CommentBatch:
        account = normalize_target("twitcasting", account)
        movie_id = observation.live_id
        if not (
            observation.service == "twitcasting"
            and observation.account == account
            and observation.state is LiveState.LIVE
            and movie_id
            and movie_id.isascii()
            and movie_id.isdecimal()
            and len(movie_id) <= 30
        ):
            return CommentBatch((), previous_id, False, "unconfirmed_movie")
        if not client_id or not client_secret:
            return CommentBatch((), previous_id, False, "configuration_unavailable")

        def fetch(offset: int) -> CommentPage:
            try:
                response = self._get(
                    "twitcasting",
                    account,
                    permission,
                    (Limit("twitcasting", 60, _MINUTE),),
                    f"https://apiv2.twitcasting.tv/movies/{movie_id}/comments",
                    params={"offset": str(offset), "limit": "50"},
                    auth=httpx.BasicAuth(client_id, client_secret),
                    operation="comments",
                )
                return parse_twitcasting_comments(response, movie_id)
            except (BudgetDenied, httpx.RequestError) as error:
                raise CommentPageError(_probe_failure(error).reason) from None
            except ValueError as error:
                raise CommentPageError(str(error)) from None

        return collect_comment_pages(fetch, previous_id=previous_id, max_pages=max_pages)

    def _get(
        self,
        service: str,
        account: str,
        permission: LivePermission | None,
        limits: tuple[Limit, ...],
        url: str,
        *,
        params: dict[str, str] | None = None,
        auth: httpx.Auth | None = None,
        operation: str = "detect",
    ) -> httpx.Response:
        if self.stopping:
            raise BudgetDenied("external collection is stopping")
        with self.budget.connection(permission, service, account, operation, limits) as budget:
            if self.stopping:
                raise BudgetDenied("external collection is stopping")
            headers = {"Accept": "application/json"}
            if service == "twitcasting":
                headers["X-Api-Version"] = "2.0"
            with httpx.Client(
                headers=headers,
                timeout=8.0,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
                auth=auth,
            ) as client:
                response = client.get(url, params=params)
            if response.status_code == 429:
                budget.defer(service, _retry_after(response))
            return response


def _retry_after(response: httpx.Response) -> datetime:
    now = datetime.now(UTC)
    value = response.headers.get("retry-after", "")
    reset = response.headers.get("x-ratelimit-reset", "")
    try:
        if reset.isascii() and reset.isdecimal():
            return max(now, datetime.fromtimestamp(int(reset), UTC))
        if value.isascii() and value.isdecimal():
            return now + timedelta(seconds=int(value))
        deadline = parsedate_to_datetime(value)
        if deadline is not None and deadline.utcoffset() is not None:
            return max(now, deadline)
    except (ValueError, OverflowError, OSError):
        pass
    return now + _MINUTE


def _probe_failure(error: BudgetDenied | httpx.RequestError) -> LiveObservation:
    if isinstance(error, BudgetDenied):
        return LiveObservation(LiveState.UNKNOWN, reason="external_request_deferred")
    if isinstance(error, httpx.TimeoutException):
        return LiveObservation(LiveState.ERROR, reason="upstream_timeout")
    return LiveObservation(LiveState.ERROR, reason="upstream_connection_error")
