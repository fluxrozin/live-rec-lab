"""Serve-owned collection and subtitle loops."""

import logging
import sqlite3
import subprocess
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from math import ceil, floor
from threading import Event, Thread
from uuid import uuid4

from .application import ApplicationWorkspace, _read_json
from .ass_jobs import process_ass
from .budget import ExternalBudget, LivePermission
from .isolation import FixtureWorkspace, IsolationError, owned_lock
from .providers.monitor import ProbeClient
from .providers.pipeline import CollectionRunner
from .settings import read_operations_settings
from .speech_jobs import process_speech

_LOG = logging.getLogger(__name__)
_ERRORS = (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError)


class RuntimeWorkers:
    def __init__(
        self, workspace: ApplicationWorkspace | FixtureWorkspace, *, permissions: tuple[LivePermission, ...] = ()
    ) -> None:
        self.workspace = workspace
        self.permissions = {(permission.service, permission.target): permission for permission in permissions}
        self._stop = Event()
        self._threads: list[Thread] = []

    def start(self) -> None:
        if self.workspace.mode == "fixture":
            self._launch("audit", self._audit)
            return
        if self.workspace.scheduler_disabled:
            return
        if self.workspace.mode == "normal" or self.permissions:
            self._launch("collection", self._monitor)
        self._launch("speech", lambda: self._poll(process_speech))
        self._launch("ass", lambda: self._poll(process_ass))
        self._launch("retention", self._maintain)
        self._launch("audit", self._audit)

    def signal_stop(self) -> None:
        self._stop.set()

    def stop(self) -> None:
        self.signal_stop()
        deadline = time.monotonic() + 8
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))
            if thread.is_alive():
                _LOG.error("worker did not stop within its budget: %s", thread.name)

    def _launch(self, worker: str, target: Callable[[], None]) -> None:
        thread = Thread(target=target, name=f"live-rec-lab-{worker}", daemon=True)
        self._threads.append(thread)
        thread.start()

    def _poll(self, fn: Callable) -> None:
        while not self._stop.is_set():
            try:
                self.workspace.require_media()
            except (OSError, IsolationError):
                self._stop.wait(5)
                continue
            try:
                result = fn(self.workspace, automatic=True, stop=self._stop)
            except _ERRORS as error:
                self._failure(fn.__name__, error)
                result = None
            self._stop.wait(0.25 if result else 5)

    def _failure(self, component: str, error: Exception) -> None:
        try:
            # Provider and inference errors may carry URLs, credentials or paths.
            self.workspace.event(component, "held", uuid4().hex, RuntimeError(type(error).__name__))
        except _ERRORS:
            _LOG.error("could not write worker diagnostic for %s", component)

    def _monitor(self) -> None:
        registry = self.workspace.reservation
        while not self._stop.is_set():
            try:
                if _read_json(registry).get("external_use_confirmed") is not True:
                    self._stop.wait(10)
                    continue
                with owned_lock(self.workspace.shared / "external-owner.lock"):
                    self._monitor_owned()
            except _ERRORS as error:
                self._failure("collection", error)
                self._stop.wait(5)

    def _monitor_owned(self) -> None:
        workspace = self.workspace
        probe = ProbeClient(ExternalBudget(workspace.shared / "external-budget.sqlite3"), stop=self._stop)
        next_poll: dict[str, float] = {}
        last_id: dict[str, int] = {}
        while not self._stop.is_set():
            if _read_json(workspace.reservation).get("external_use_confirmed") is not True:
                return
            settings = read_operations_settings(workspace)
            with closing(sqlite3.connect(workspace.database.as_uri() + "?mode=ro", uri=True)) as database:
                registered = database.execute(
                    """
                    SELECT id, platform, normalized_id, comments_enabled FROM streamers
                    WHERE enabled = 1 AND archived_at IS NULL ORDER BY id
                    """
                ).fetchall()
            runner = CollectionRunner(
                workspace,
                probe,
                twitcasting_credentials=workspace.twitcasting_credentials,
                twitcasting_monitor_limit=max(1, int(settings.twitcasting_per_minute)),
            )
            rates = {"tiktok": settings.tiktok_per_minute, "twitcasting": settings.twitcasting_per_minute}
            for service, rate in rates.items():
                now = time.monotonic()
                if self._stop.is_set() or now < next_poll.get(service, 0):
                    continue
                available = []
                for streamer_id, platform, target, comments in registered:
                    if platform == service and (permit := self._permit(platform, target, bool(comments))):
                        if not settings.comments_globally_enabled:
                            permit = LivePermission(platform, target, permit.operations - {"comments"}, permit.expires)
                        available.append((streamer_id, permit))
                if not available:
                    continue
                streamer_id, permit = next(
                    (account for account in available if account[0] > last_id.get(service, 0)),
                    available[0],
                )
                next_poll[service] = now + 60 / rate
                last_id[service] = streamer_id
                runner.poll(streamer_id, permit)
            self._stop.wait(0.5)

    def _permit(self, platform: str, target: str, comments: bool) -> LivePermission | None:
        if self.workspace.mode == "developer":
            permit = self.permissions.get((platform, target))
            if permit and permit.expires > datetime.now(UTC):
                return permit
            return None
        operations = frozenset({"detect", "comments"} if comments else {"detect"})
        return LivePermission(platform, target, operations, datetime.now(UTC) + timedelta(minutes=1))

    def _maintain(self) -> None:
        from .retention import MediaRetentionPolicy, resume_media_deletions, run_comment_retention, run_media_retention

        while not self._stop.is_set():
            try:
                self.workspace.require_media()
            except (OSError, IsolationError):
                self._stop.wait(60)
                continue
            try:
                resume_media_deletions(self.workspace)
                settings = read_operations_settings(self.workspace)
                if self._stop.is_set():
                    return
                run_comment_retention(self.workspace, settings.comment_retention_days)
                if self._stop.is_set():
                    return
                run_media_retention(
                    self.workspace,
                    MediaRetentionPolicy(
                        enabled=settings.auto_delete_enabled,
                        keep_days=settings.video_retention_days,
                        maximum_bytes=floor(settings.maximum_recording_gib * 2**30),
                        minimum_free_bytes=ceil(settings.minimum_free_gib * 2**30),
                    ),
                )
            except _ERRORS as error:
                self._failure("retention", error)
            self._stop.wait(60)

    def _audit(self) -> None:
        from .storage_audit import interrupt_storage_audits, run_storage_audit

        try:
            interrupt_storage_audits(self.workspace)
        except _ERRORS as error:
            self._failure("storage_audit", error)
        while not self._stop.is_set():
            try:
                run_storage_audit(self.workspace, timeout=5, stop=self._stop)
            except _ERRORS as error:
                self._failure("storage_audit", error)
            self._stop.wait(2)
