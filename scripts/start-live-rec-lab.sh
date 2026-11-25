#!/usr/bin/env bash
# 非対話（Windowsのタスクスケジューラ等）からの通常運用起動。対話シェルのPATH設定に依存しない。
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd "$(dirname "$0")/.."
exec uv run --locked --extra speech live-rec-lab start
