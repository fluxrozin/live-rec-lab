"""初期 Feature の指定を読取専用で照合する。"""

import json
import os
from collections.abc import Mapping
from pathlib import Path

FEATURE = Path("specs/001-initial-live-recorder")


def check_feature(root: Path, environment: Mapping[str, str]) -> Path:
    root = root.resolve()
    expected = root / FEATURE
    configured = json.loads((root / ".specify/feature.json").read_text(encoding="utf-8"))["feature_directory"]
    if not isinstance(configured, str) or (root / configured).resolve() != expected:
        raise ValueError("保存済みのFeature指定が初期Featureと一致しません")
    override = environment.get("SPECIFY_FEATURE_DIRECTORY")
    if override and (root / override).resolve() != expected:
        raise ValueError("SPECIFY_FEATURE_DIRECTORY が保存済みFeatureと一致しません")
    hint = environment.get("SPECIFY_FEATURE")
    if hint and hint != FEATURE.name:
        raise ValueError("SPECIFY_FEATURE が保存済みFeatureと一致しません")
    candidates = list(expected.parent.glob("*-initial-live-recorder"))
    if any(path != expected or not path.is_dir() or path.is_symlink() for path in candidates):
        raise ValueError("初期Featureの候補が複数あるか、安全なディレクトリではありません")
    if expected.resolve() != expected:
        raise ValueError("初期Featureが別の場所を指しています")
    return expected


if __name__ == "__main__":
    try:
        print(check_feature(Path(__file__).resolve().parents[1], os.environ))
    except (KeyError, OSError, ValueError) as error:
        raise SystemExit(f"Feature照合に失敗しました: {error}") from error
