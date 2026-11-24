"""Application mode selection and the shared storage boundary."""

import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values

from .db import initialize_database
from .isolation import IsolationError, Workspace, _check_file, check_owned_directory, owned_lock

Mode = Literal["normal", "developer"]
_RECORDING_DEFAULT = Path("/mnt/d/LiveRecorder")
_LINUX_FILESYSTEMS = {"ext4"}


def _absolute(value: str | Path, home: Path) -> Path:
    raw = str(value)
    if raw.startswith("~/"):
        raw = str(home / raw[2:])
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise IsolationError("a managed path must be absolute and normalized")
    return path


def _no_alias(path: Path) -> None:
    if _resolved(path) != path:
        raise IsolationError(f"managed path has a symbolic alias: {path}")


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError) as error:
        raise IsolationError(f"managed path identity could not be confirmed: {path}") from error


def _overlap(left: Path, right: Path) -> bool:
    if left.is_relative_to("/mnt") and right.is_relative_to("/mnt"):
        left, right = Path(str(left).casefold()), Path(str(right).casefold())
    return left.is_relative_to(right) or right.is_relative_to(left)


def _mount(path: Path) -> tuple[Path, str]:
    matches = []
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        metadata, filesystem = line.split(" - ", 1)
        if len(metadata.split()) < 5:
            continue
        mount = Path(re.sub(r"\\([0-7]{3})", lambda found: chr(int(found[1], 8)), metadata.split()[4]))
        if path.is_relative_to(mount):
            matches.append((mount, filesystem.split()[0]))
    if not matches:
        raise IsolationError("filesystem cannot be verified")
    return max(matches, key=lambda item: len(item[0].parts))


def _check_linux(path: Path) -> None:
    _no_alias(path)
    if path.is_relative_to("/mnt") or _mount(path)[1] not in _LINUX_FILESYSTEMS:
        raise IsolationError(f"management data requires WSL Linux ext4: {path}")


def _read_json(path: Path) -> dict:
    _check_file(path)
    with path.open(encoding="utf-8") as stream:
        value = json.loads(stream.read(256 * 1024))
    if not isinstance(value, dict):
        raise IsolationError("invalid managed path information")
    return value


def _write_json(path: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        staged = Path(stream.name)
        try:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)


def validate_windows_root(path: Path) -> Path:
    if len(path.parts) < 4 or path.parts[1] != "mnt" or re.fullmatch("[a-z]", path.parts[2]) is None:
        raise IsolationError("recordings require a directory under a Windows drive")
    if any(part.endswith((" ", ".")) or re.search(r'[<>"|?*:\\]', part) for part in path.parts[3:]):
        raise IsolationError("ambiguous Windows recording path")
    _no_alias(path)
    return path


@dataclass(frozen=True)
class ApplicationWorkspace(Workspace):
    mode: Mode
    data: Path
    state: Path
    configured_recordings: Path
    shared: Path
    defaults: dict[str, tuple[Path, Path]] = field(repr=False)
    existing_media_paths: tuple[Path, ...] | None = None
    scheduler_disabled: bool = False
    twitcasting_credentials: tuple[str, str] = field(default=("", ""), repr=False)
    signing_key: str = field(default="", repr=False)

    @classmethod
    def resolve(
        cls,
        mode: Mode,
        *,
        home: Path | None = None,
        directory: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> "ApplicationWorkspace":
        home = Path.home() if home is None else home
        directory = Path.cwd() if directory is None else directory
        environment = os.environ if environment is None else environment
        developer = mode == "developer"
        env_file = directory / (".env.dev" if developer else ".env")
        if os.path.lexists(env_file):
            _check_file(env_file)
            values = dotenv_values(env_file, interpolate=False)
        else:
            values = {}
        prefix = "DEV_" if developer else ""

        def value(key: str, default: str = "") -> str:
            return environment.get(prefix + key, values.get(prefix + key) or default)

        defaults = {
            name: (home / ".local/share" / directory_name / "data", home / ".local/state" / directory_name)
            for name, directory_name in (("normal", "live-rec-lab"), ("developer", "live-rec-lab-dev"))
        }
        data, state = defaults[mode]
        data = _absolute(value("LIVE_REC_LAB_DATA_DIR", str(data)), home)
        state = _absolute(value("LIVE_REC_LAB_STATE_DIR", str(state)), home)
        default_media = data.parent / "recordings" if developer else _RECORDING_DEFAULT
        recordings = _absolute(value("LIVE_REC_LAB_RECORDING_ROOT", str(default_media)), home)
        existing = value("LIVE_REC_LAB_EXISTING_RECORDING_ROOTS")
        protected = None if not existing else ()
        if existing and existing != "none":
            protected = tuple(_absolute(item, home) for item in existing.split(";"))
        disabled = value("LIVE_REC_LAB_DISABLE_SCHEDULER", "0")
        if disabled not in {"0", "1"}:
            raise IsolationError("invalid scheduler setting")
        return cls(
            mode,
            data,
            state,
            recordings,
            home / ".local/state/live-rec-lab-shared",
            defaults,
            protected,
            disabled == "1",
            (value("TWITCASTING_CLIENT_ID"), value("TWITCASTING_CLIENT_SECRET")),
            value("SIGN_API_KEY"),
        )

    @property
    def root(self) -> Path:
        return self.data.parent

    @property
    def default_port(self) -> int:
        return 8083 if self.mode == "developer" else 8082

    @property
    def recordings(self) -> Path:
        if self.database.is_file():
            from .settings import read_operations_settings

            saved = read_operations_settings(self).recording_root
            if saved:
                candidate = Path(saved)
                if self.mode == "normal":
                    return validate_windows_root(candidate)
                if not candidate.is_relative_to(self.root / "recordings"):
                    raise IsolationError("developer recording root is outside its dedicated directory")
                _check_linux(candidate)
                return candidate
        return self.configured_recordings

    @property
    def database(self) -> Path:
        return self.data / "app.sqlite3"

    @property
    def events(self) -> Path:
        return self.state / "logs" / "events.jsonl"

    @property
    def lock(self) -> Path:
        return self.state / "application.lock"

    @property
    def restore_marker(self) -> Path:
        return self.state / "restore-in-progress.json"

    @property
    def runtime_path(self) -> Path:
        return self.state / "runtime"

    @property
    def backup_path(self) -> Path:
        return self.state / "backups"

    @property
    def models(self) -> Path:
        return self.data / "models"

    @property
    def reservation(self) -> Path:
        return self.shared / "protected-paths.json"

    @property
    def _identity(self) -> dict:
        return {"version": 1, "mode": self.mode, "data": str(self.data), "state": str(self.state)}

    def _selected_paths(self) -> tuple[Path, ...]:
        selected = (self.data, self.state)
        if self.mode == "developer":
            if self.data.name != "data" or not self.configured_recordings.is_relative_to(self.root / "recordings"):
                raise IsolationError("developer recordings require a dedicated recordings directory")
            selected += (self.root / "recordings",)
        return selected

    def _check_layout(self, registry: dict) -> None:
        selected = self._selected_paths()
        other_mode = "developer" if self.mode == "normal" else "normal"
        protected = (*self.defaults[other_mode], self.shared)
        if self.mode == "normal":
            protected += (self.defaults["developer"][0].parent / "recordings",)
        instances = registry.get("instances", [])
        if not isinstance(instances, list):
            raise IsolationError("invalid protected path configuration")
        for item in instances:
            if not isinstance(item, dict) or set(item) != {"identity", "paths"}:
                raise IsolationError("invalid protected path entry")
            if item["identity"] == self._identity and item["paths"] == [str(path) for path in selected]:
                continue
            if not isinstance(item["paths"], list) or not all(isinstance(path, str) for path in item["paths"]):
                raise IsolationError("invalid protected path entry")
            protected += tuple(_absolute(path, self.root) for path in item["paths"])
        external_paths = registry.get("external_paths", [])
        if not isinstance(external_paths, list) or not all(isinstance(path, str) for path in external_paths):
            raise IsolationError("invalid protected path entry")
        protected += tuple(_absolute(path, self.root) for path in external_paths)
        protected += self.existing_media_paths or ()
        protected += tuple(_resolved(path) for path in protected)
        for index, path in enumerate(selected):
            _check_linux(path)
            if any(_overlap(path, item) for item in (*selected[:index], *protected)):
                raise IsolationError("managed paths overlap a protected location")
        if self.mode == "developer":
            _check_linux(self.configured_recordings)

    def initialize(self) -> "ApplicationWorkspace":
        _check_linux(self.shared)
        self.shared.mkdir(mode=0o700, parents=True, exist_ok=True)
        check_owned_directory(self.shared, private=True)
        with owned_lock(self.shared / "settings.lock"):
            registry = _read_json(self.reservation) if os.path.lexists(self.reservation) else {"instances": []}
            self._check_layout(registry)
            entry = {"identity": self._identity, "paths": [str(path) for path in self._selected_paths()]}
            missing = []
            for path in (self.data, self.state):
                marker = path / ".live-rec-lab-workspace"
                if path.exists():
                    check_owned_directory(path, private=True)
                    if os.path.lexists(marker):
                        if _read_json(marker) != self._identity:
                            raise IsolationError("managed directory belongs to another workspace")
                    elif any(path.iterdir()):
                        raise IsolationError("cannot adopt an unmarked management directory")
                    else:
                        missing.append(path)
                else:
                    missing.append(path)
            if len(missing) == 1:
                raise IsolationError("workspace setup is incomplete")
            if (
                missing
                and self.mode == "developer"
                and self.configured_recordings.exists()
                and (any(self.configured_recordings.iterdir()))
            ):
                raise IsolationError("cannot adopt an unmarked recording directory")
            instances = registry.setdefault("instances", [])
            if entry not in instances:
                instances.append(entry)
                _write_json(self.reservation, registry)
            if missing:
                for path in missing:
                    path.mkdir(mode=0o700, parents=True, exist_ok=True)
                    check_owned_directory(path, private=True)
                initialize_database(self.database)
                for path in missing:
                    _write_json(path / ".live-rec-lab-workspace", self._identity)
            for path in (self.events.parent, self.models):
                _no_alias(path)
                path.mkdir(mode=0o700, exist_ok=True)
                check_owned_directory(path, private=True)
            if self.mode == "developer":
                self.configured_recordings.mkdir(mode=0o700, parents=True, exist_ok=True)
                check_owned_directory(self.configured_recordings, private=True)
        return self.recheck()

    def recheck(self) -> "ApplicationWorkspace":
        check_owned_directory(self.shared, private=True)
        self._check_layout(_read_json(self.reservation))
        for path in (self.data, self.state):
            check_owned_directory(path, private=True)
            if _read_json(path / ".live-rec-lab-workspace") != self._identity:
                raise IsolationError("workspace identity changed")
        self.check_directory(self.events.parent, private=True)
        _check_file(self.database)
        for path in (self.events, self.lock, *self.data.glob(self.database.name + "-*")):
            _check_file(path, optional=True)
        return self

    def require_media(self) -> None:
        self.recheck()
        self.require_ready()
        root = self.recordings
        if self.mode == "developer":
            if not root.is_relative_to(self.root / "recordings"):
                raise IsolationError("developer recording root is outside its dedicated directory")
            self.check_directory(root, private=True)
            return
        self.check_windows_recording_root(root)
        raise IsolationError("Windows recording publication is awaiting verification")

    def check_windows_recording_root(self, root: Path) -> None:
        validate_windows_root(root)
        if self.existing_media_paths is None:
            raise IsolationError("existing Windows recording roots are unconfirmed")
        registry = _read_json(self.reservation)
        protected = (*self.existing_media_paths, *(Path(path) for path in registry.get("external_paths", [])))
        if any(_overlap(root, path) or _overlap(root, _resolved(path)) for path in protected):
            raise IsolationError("recording root overlaps a protected Windows location")
        mount, filesystem = _mount(root)
        if str(mount) != f"/mnt/{root.parts[2]}" or filesystem not in {"9p", "drvfs"}:
            raise IsolationError("Windows recording drive is unavailable")

    def check_directory(self, path: Path, *, private: bool = False) -> None:
        _check_linux(path)
        check_owned_directory(path, private=private)

    def check_backup_destination(self, path: Path) -> None:
        _check_linux(path)
        self.check_directory(path.parent)
        registry = _read_json(self.reservation)
        managed = [self.shared, self.data, self.state, self.recordings]
        for item in registry.get("instances", []):
            managed.extend(Path(value) for value in item["paths"])
        managed.extend(Path(value) for value in registry.get("external_paths", []))
        if any(_overlap(path, protected) for protected in managed):
            raise IsolationError("backup destination overlaps a managed or protected location")
