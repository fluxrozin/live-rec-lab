"""モード別の HF_TOKEN 解決。通常と隔離は相互にフォールバックしない。"""

from __future__ import annotations

import os
from pathlib import Path

from live_rec_lab.config import apply_hf_token, resolve_environment


def _dirs(tmp_path: Path, name: str) -> dict[str, str]:
    base = tmp_path / name
    return {
        "LIVE_REC_LAB_DATA_DIR": str(base / "data"),
        "LIVE_REC_LAB_STATE_DIR": str(base / "state"),
        "LIVE_REC_LAB_RECORDING_ROOT": str(base / "recordings"),
    }


def test_hf_token_is_scoped_to_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "live_rec_lab.config.dotenv_values",
        lambda _path: {"HF_TOKEN": "from-file", "DEV_HF_TOKEN": "dev-file"},
    )
    normal = resolve_environment(False, {**_dirs(tmp_path, "normal"), "HF_TOKEN": "from-env", "DEV_HF_TOKEN": "dev-env"})
    isolated = resolve_environment(True, {**_dirs(tmp_path, "iso"), "HF_TOKEN": "from-env", "DEV_HF_TOKEN": "dev-env"})
    assert normal.secrets["HF_TOKEN"] == "from-env"
    assert isolated.secrets["HF_TOKEN"] == "dev-env"


def test_hf_token_falls_back_to_mode_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "live_rec_lab.config.dotenv_values",
        lambda _path: {"HF_TOKEN": "from-file", "DEV_HF_TOKEN": "dev-file"},
    )
    normal = resolve_environment(False, _dirs(tmp_path, "normal"))
    isolated = resolve_environment(True, _dirs(tmp_path, "iso"))
    assert normal.secrets["HF_TOKEN"] == "from-file"
    assert isolated.secrets["HF_TOKEN"] == "dev-file"


def test_isolated_does_not_inherit_normal_hf_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("live_rec_lab.config.dotenv_values", lambda _path: {"HF_TOKEN": "from-file"})
    isolated = resolve_environment(True, {**_dirs(tmp_path, "iso"), "HF_TOKEN": "from-env"})
    assert "HF_TOKEN" not in isolated.secrets


def test_hf_token_absent_when_unset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("live_rec_lab.config.dotenv_values", lambda _path: {})
    env = resolve_environment(False, _dirs(tmp_path, "plain"))
    assert "HF_TOKEN" not in env.secrets


def test_apply_hf_token_replaces_shell_tokens(monkeypatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "shell")
    monkeypatch.setenv("HUGGING_FACE_HUB_TOKEN", "legacy")
    apply_hf_token("configured")
    assert os.environ["HF_TOKEN"] == "configured"
    assert "HUGGING_FACE_HUB_TOKEN" not in os.environ
    apply_hf_token(None)
    assert "HF_TOKEN" not in os.environ
