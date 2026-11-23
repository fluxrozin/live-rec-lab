# 検証手順

1. `uv sync --locked`、`uv run --locked ruff check .`、`uv run --locked pytest` をPython 3.12で実行する。録画試験は実際に解決したFFmpeg／ffprobeを記録する。
2. Linux側の専用データ・状態ディレクトリを指定し、`uv run --locked live-rec-lab start` を起動する。別シェルで同じ環境を使用し、`status` と `url`、`backup DEST` と `stop` を順に確認する。
3. 許諾と分離条件を満たすまで外部録画を有効化しない。ローカルHLSと模擬イベントで監視→録画・コメント→確定→SRTを実行し、動画・DB・SRTとログのセッションIDを照合する。
4. 主要7画面と下位ビューを1024／1280／1440pxで操作し、検索保持、履歴導線、フォント、設定、空・失敗・競合時の表示、キーボード操作を記録する。
5. 実機ではWindowsの録画ルート照合、DrvFSのファイル操作、公開／許諾済み配信、Windows画面ロック・画面OFFとスリープ・休止、NAT／mirroredを個別に試験する。項目別の検証先は [traceability.md](traceability.md)。

Spec Kitを再実行する前に `python scripts/check_feature.py` でFeatureの指定を照合する。既存の仕様や完了済みの成果物は使い、別Featureに対する上書きを防止する。
