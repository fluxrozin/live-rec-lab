# LiveRecorder

TikTok LIVE・ツイキャスのライブ配信を自動監視し、動画・コメント・字幕（コメントSRT／音声SRT／統合ASS）をWindowsドライブへ保存する個人利用向けローカルWebアプリケーション。Windows 11 / WSL2 Ubuntu 24.04 上の単一Pythonプロセス（FastAPI + Jinja2/HTMX + SQLite WAL）として稼働し、日本語の8画面（ダッシュボード・配信者・録画履歴・コメント・音声字幕・設定・システム状態・ログ）で操作する。動画のWeb再生・ダウンロードはない。

実装状態と実機確認の範囲は [tasks.md](tasks.md)、導入・運用・障害対応は [docs/manual.md](docs/manual.md)。

## 構成

| 領域 | 採用 |
|---|---|
| 言語・環境 | CPython 3.12（`.python-version`）、uv、`pyproject.toml` / `uv.lock` |
| Web | FastAPI + Uvicorn（1 worker）、Jinja2 + HTMX 2.0.4（`web/static` に同梱、BSD-0）、固定ポート直バインド |
| DB | SQLite（WAL）、SQLAlchemy 2、Alembic 初期revision（`src/live_rec_lab/migrations`） |
| TikTok | 公開room API（監視）、Streamlink／yt-dlp（録画）、TikTokLive + Euler Stream 無料署名（コメント） |
| ツイキャス | 公式API v2（Basic認証）の current_live／comments、公式HLSをffmpegでstream copy（代替: Streamlink） |
| 音声 | faster-whisper（`speech` extra、CUDA優先・CPUは明示選択のみ）。推論は専用の子プロセス |
| メディア | ffmpeg / ffprobe（実プロセス環境で解決した組を診断） |

主要モジュール: `config.py`（モード別の設定解決と隔離境界）、`external.py`（外部通信の既定拒否・共通ロック・永続予算）、`storage.py`（Windows安全名・非上書き確定）、`supervisor.py`（監視→セッション→録画/コメント→収束）、`recording.py` / `comments.py` / `srt.py` / `speech.py` / `ass.py`、`archives.py`（削除・容量・照合）、`restore.py`（停止中の手動復元）、`web/`（画面とAPI）。

## 起動

```bash
uv sync --locked --extra speech      # 音声を使わない場合は --extra speech を省く
cp .env.example .env && chmod 600 .env   # 資格情報を設定（任意）
uv run --locked live-rec-lab start       # 通常運用 http://127.0.0.1:8082
uv run --locked live-rec-lab start --dev # 隔離（開発者モード）http://127.0.0.1:8083、外部通信は既定で無効
```

`status` / `url` / `stop` / `backup DEST` / `config get|set web_port` / `diagnose --json` / `restore prepare|finish|abort` は `--dev` で隔離対象を選べる。詳細な手順・設定・障害対応は [docs/manual.md](docs/manual.md)。

## 文書

| 文書 | 役割 |
|---|---|
| `docs/requirements.md` | 製品の確定機能と安全性（正本） |
| `docs/ui-spec.md` | UIの確定条件（正本） |
| `docs/system-design.md` | 基本設計・公開API・CLI・診断契約 |
| `docs/manual.md` | 導入・運用・障害対応（実装と実測に基づく） |
| `ui/design.md` | UIの5案比較と採用記録（採用: A案） |
| `tasks.md` | 機能ごとの実装状態・動作確認・利用可否・再開点 |
| `AGENTS.md` / `prompts/` | 開発の指示 |

## テスト

```bash
uv run --locked pytest -n auto -m "not serial and not live and not hardware"
uv run --locked pytest --run-hardware tests/test_windows_media.py tests/test_speech.py   # D:\LiveRecorder の許可とGPU・モデルが必要
```

実配信の試験は `--run-live` と利用者の許可が必要（マーカーだけでは接続しない）。
