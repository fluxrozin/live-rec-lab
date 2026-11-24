# Implementation Plan: LiveRecorder 初期版

> 実行は同じFeatureの [tasks.md](tasks.md) に沿って行い、検証結果を [traceability.md](traceability.md) に残す。

**Goal**: Windows上のWSL2で、両サービスの監視・録画・コメント記録から復旧・検索まで運用できるようにする。

**Branch**: `trial/full-sdd-02`（Featureのディレクトリは `001-initial-live-recorder`）

**Spec**: [spec.md](spec.md)

**Architecture**: 単一プロセスの非同期監視・ジョブ制御。有効な記録をSQLiteとWindows側に保持し、WebとCLIでサービスを共有する。

## Summary

このFeatureの実装は `src/live_rec_lab/` にある。過去の試作 `aa09977` は採用候補の参照であり、その仕様成果物、実行時ファイル、検証結果は現在の実装証跡に含めない。検証の区別は [traceability.md](traceability.md) を見る。

## Technical Context

Python 3.12.x、uvロック、FastAPI・Uvicorn 1 worker、Jinja2・固定HTMX・軽量JS、同期SQLAlchemy 2.x・Alembic・SQLite WAL、asyncio、TikTokLive、Streamlink、yt-dlp、FFmpeg／ffprobe、pytest。Windows 11＋WSL2 Ubuntu 24.04、LinuxローカルDB、Windowsドライブの動画・SRT。外部仕様と未実測条件は [research.md](research.md) に記録。

## Constitution Check

- I — ローカルHLSと模擬配信でCLI経路を先行検証する。Webは同じ業務処理を使う。
- II — 状態と2ジョブを独立させ、保存境界と排他、確定・削除・復旧の台帳を設ける。
- III — 取得失敗・未確認・終了未確認を保持し、外部実測は独立の受け入れ試験に残す。
- IV — ループバック、変更操作のOrigin／Host／CSRF、動画・SRTのファイル取得経路除外を検証する。
- V — 構造化ログ、障害診断・相関、読取前の境界検証と応答前の再秘匿を検証する。

設計後も上記ゲートを適用する。新たな憲法例外は設けない。

## Project Structure

| ファイル・領域 | 責務 |
| --- | --- |
| `pyproject.toml`、`uv.lock`、`src/live_rec_lab/config.py` | 依存の固定、環境・永続設定の境界、型と範囲 |
| `db/`、`alembic/`、`storage.py` | SQLite制約と移行、ルート照合、永続フォルダ、採番と非上書き |
| `monitoring/`、`services/`、`platforms/` | 監視枠、状態決定、個別ジョブ、配信サービス |
| `recording/`、`comments/`、`ops/recovery.py` | 子プロセス、録画確定、コメントキューとSRT版数、復旧 |
| `ops/logging_config.py`、`ops/log_reader.py`、`ops/backup.py` | 秘匿、段階的なログ読取、バックアップ |
| `app.py`、`web/`、`cli.py`、`scripts/` | エントリポイント、REST、7画面、運用と起動 |
| `tests/`、`specs/001-initial-live-recorder/` | ローカル・障害・実機の証跡、原本からの追跡 |

## Execution Order

1. 共通基盤の移行・設定・ストレージ・ログ。DDL、状態・削除・SRT契約は [data-model.md](data-model.md) と [運用契約](contracts/operations.md) を参照。
2. TikTok監視から録画・コメント・確定・SRTまでCLIで統合し、失敗と復帰を注入。
3. ツイキャスの公式API、割当制御とHLS・コメントの統合。実測可能な条件を別記。
4. [Web/API契約](contracts/http.md) とUI正本に従い7画面を統合。
5. WSL2運用・バックアップ、品質確認、実ブラウザ、補助資料更新、原本監査と収束。

## Review Focus

- 監視の未知状態やOFFが手動停止／終了済みと混同される。
- 2ジョブの採番競合、公開直前の同名、公開直後の再起動で取得物を失う。
- SRTの版数や期限削除がコメント追加と競合する。
- ローテーション中のログ、旧ログの未知の秘密値、符号化した資格情報が漏れる。
- DB保存済みパス、Windows再解析ポイント、PID再利用などで既存アプリと衝突する。
