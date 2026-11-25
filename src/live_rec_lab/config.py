"""モード別の設定解決と隔離境界（要求§15.2.1）。

通常運用は ``LIVE_REC_LAB_*`` / ``.env``、隔離（--dev）は ``DEV_LIVE_REC_LAB_*`` / ``.env.dev`` だけを読む。
相互にフォールバックしない。設定・DB読込より前にモードを決定する。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

HOME = Path.home()
SHARED_DIR = HOME / ".local/state/live-rec-lab-shared"
PROTECTED_FILE = SHARED_DIR / "protected-paths.json"
WINDOWS_PERMIT_FILE = SHARED_DIR / "windows-test-permit.json"

NORMAL_DATA = HOME / ".local/share/live-rec-lab/data"
NORMAL_STATE = HOME / ".local/state/live-rec-lab"
DEV_BASE = HOME / ".local/share/live-rec-lab-dev"
DEV_STATE = HOME / ".local/state/live-rec-lab-dev"
DEFAULT_NORMAL_PORT = 8082
DEFAULT_DEV_PORT = 8083


class ConfigError(RuntimeError):
    """境界を確認できない・衝突している場合の起動拒否。"""


def repo_root() -> Path:
    here = Path(__file__).resolve().parents[2]
    if (here / "pyproject.toml").is_file():
        return here
    return Path.cwd()


def norm(path: str | os.PathLike[str]) -> Path:
    """ホーム展開後の絶対パスへ正規化（シンボリックリンクは解決）。"""
    p = Path(os.path.expanduser(str(path)))
    if not p.is_absolute():
        raise ConfigError(f"絶対パスで指定してください: {path}")
    return Path(os.path.realpath(p))


def _fold(p: Path) -> str:
    s = str(p)
    # Windowsマウント上は大文字小文字を区別しないため比較は畳み込む
    return s.lower() if s.startswith("/mnt/") else s


def overlaps(a: Path, b: Path) -> bool:
    fa, fb = _fold(a).rstrip("/") + "/", _fold(b).rstrip("/") + "/"
    return fa.startswith(fb) or fb.startswith(fa)


def inside(child: Path, parent: Path) -> bool:
    return (_fold(child).rstrip("/") + "/").startswith(_fold(parent).rstrip("/") + "/")


def has_symlink(path: Path) -> bool:
    """既存の構成要素にシンボリックリンクが含まれるか。"""
    cur = Path("/")
    for part in Path(os.path.abspath(path)).parts[1:]:
        cur = cur / part
        if cur.is_symlink():
            return True
        if not cur.exists():
            break
    return False


def load_protected_paths() -> list[Path]:
    if not PROTECTED_FILE.is_file():
        return []
    try:
        data = json.loads(PROTECTED_FILE.read_text("utf-8"))
        return [norm(p) for p in data.get("paths", [])]
    except (OSError, ValueError, ConfigError) as exc:
        raise ConfigError(f"保護パス設定を読めません（操作を拒否）: {exc}") from exc


def load_windows_permit() -> Path | None:
    if not WINDOWS_PERMIT_FILE.is_file():
        return None
    try:
        data = json.loads(WINDOWS_PERMIT_FILE.read_text("utf-8"))
        return norm(data["root"])
    except (OSError, ValueError, KeyError, ConfigError) as exc:
        raise ConfigError(f"Windows試験許可を読めません: {exc}") from exc


@dataclass(frozen=True)
class Environment:
    dev: bool
    data_dir: Path
    state_dir: Path
    recording_root: Path
    default_port: int
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    flags: dict[str, str] = field(default_factory=dict)

    @property
    def mode(self) -> str:
        return "developer" if self.dev else "normal"

    @property
    def data_environment(self) -> str:
        return "isolated" if self.dev else "live"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "live-rec-lab.sqlite3"

    @property
    def runtime_dir(self) -> Path:
        return self.state_dir / "runtime"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def model_dir(self) -> Path:
        return self.data_dir / "models"


def _gather(dev: bool, environ: dict[str, str] | None) -> dict[str, str]:
    """モード別キーだけを集める（プロセス環境 > .env / .env.dev）。"""
    prefix = "DEV_" if dev else ""
    environ = dict(os.environ) if environ is None else environ
    file_vals = dotenv_values(repo_root() / (".env.dev" if dev else ".env"))
    out: dict[str, str] = {}
    keys = {
        "LIVE_REC_LAB_DATA_DIR", "LIVE_REC_LAB_STATE_DIR", "LIVE_REC_LAB_RECORDING_ROOT",
        "LIVE_REC_LAB_EXISTING_RECORDING_ROOTS", "LIVE_REC_LAB_FFMPEG_BIN", "LIVE_REC_LAB_DISABLE_SCHEDULER",
        "LIVE_REC_LAB_TWITCASTING_FORCE_ENGINE", "TWITCASTING_CLIENT_ID", "TWITCASTING_CLIENT_SECRET",
        "SIGN_API_KEY", "HF_TOKEN",
    }
    for k in keys:
        full = prefix + k
        v = environ.get(full)
        if v is None:
            v = file_vals.get(full)
        if v not in (None, ""):
            out[k] = v
    return out


def resolve_environment(dev: bool, environ: dict[str, str] | None = None) -> Environment:
    vals = _gather(dev, environ)
    secret_keys = ("TWITCASTING_CLIENT_ID", "TWITCASTING_CLIENT_SECRET", "SIGN_API_KEY", "HF_TOKEN")
    secrets = {k: vals[k] for k in secret_keys if k in vals}
    flags = {k: v for k, v in vals.items() if k not in secret_keys}

    if dev:
        data_dir = norm(vals.get("LIVE_REC_LAB_DATA_DIR", DEV_BASE / "data"))
        if data_dir.name != "data":
            raise ConfigError("隔離データは専用基底直下の data にしてください")
        base = data_dir.parent
        state_dir = norm(vals.get("LIVE_REC_LAB_STATE_DIR", DEV_STATE))
        recording_root = norm(vals.get("LIVE_REC_LAB_RECORDING_ROOT", base / "recordings"))
        port = DEFAULT_DEV_PORT
    else:
        data_dir = norm(vals.get("LIVE_REC_LAB_DATA_DIR", NORMAL_DATA))
        state_dir = norm(vals.get("LIVE_REC_LAB_STATE_DIR", NORMAL_STATE))
        recording_root = norm(vals.get("LIVE_REC_LAB_RECORDING_ROOT", "/mnt/d/LiveRecorder"))
        port = DEFAULT_NORMAL_PORT
    env = Environment(dev, data_dir, state_dir, recording_root, port, secrets, flags)
    validate_boundaries(env)
    return env


def validate_boundaries(env: Environment) -> None:
    """DB/状態/媒体/保護パスの重複・別名・親子・リンクを拒否する。"""
    managed = {"data": env.data_dir, "state": env.state_dir}
    if env.data_dir.name == "data" and overlaps(env.data_dir, env.state_dir):
        raise ConfigError("データと状態のディレクトリが重複しています")
    for label, p in managed.items():
        if has_symlink(p):
            raise ConfigError(f"{label} にシンボリックリンクが含まれます: {p}")
        if str(p).startswith("/mnt/"):
            raise ConfigError(f"{label} はLinuxファイルシステム上に置いてください: {p}")
    if overlaps(env.recording_root, env.data_dir) or overlaps(env.recording_root, env.state_dir):
        raise ConfigError("録画ルートがDB/状態の管理領域と重複しています")
    for prot in load_protected_paths():
        for label, p in (("data", env.data_dir), ("state", env.state_dir), ("recording_root", env.recording_root)):
            if overlaps(p, prot):
                raise ConfigError(f"{label} が保護パスと衝突しています: {prot}")
    other = (
        (NORMAL_DATA.parent, NORMAL_STATE) if env.dev else (DEV_BASE, DEV_STATE)
    )
    for p in (env.data_dir, env.state_dir, env.recording_root):
        for o in other:
            if overlaps(p, o):
                raise ConfigError(f"他方のモードの領域と衝突しています: {o}")
    if overlaps(env.data_dir, SHARED_DIR) or overlaps(env.state_dir, SHARED_DIR):
        raise ConfigError("共有状態ディレクトリと重複しています")

    if env.dev:
        base = env.data_dir.parent
        if not inside(env.recording_root, base):
            permit = load_windows_permit()
            if permit is None or not inside(env.recording_root, permit):
                raise ConfigError(
                    "隔離の録画ルートは専用録画領域か、許可済みWindows専用試験フォルダ配下に限られます"
                )


def apply_hf_token(token: str | None) -> None:
    """モデル取得プロセスの HF 認証を、指定したトークンだけにする。

    シェルに残っている ``HF_TOKEN`` / ``HUGGING_FACE_HUB_TOKEN`` は消す。
    ``token`` が空なら未認証のままにする。推論子は ``None`` を渡し、トークンを送らない。
    """
    os.environ.pop("HF_TOKEN", None)
    os.environ.pop("HUGGING_FACE_HUB_TOKEN", None)
    if token:
        os.environ["HF_TOKEN"] = token


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.stat(path).st_uid != os.getuid():
        raise ConfigError(f"所有者が異なります: {path}")
    os.chmod(path, 0o700)
