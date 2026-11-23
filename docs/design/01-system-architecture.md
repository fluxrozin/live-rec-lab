# 01. システム基本設計 — 構成・責務・依存関係

> 出典：[`requirements.md`](../../requirements.md) §1–3、§10.1.1。**設計ドラフト**。

## 1. システム境界

- 個人用・ローカル実行のライブ配信記録システム。初期の対象サービスはTikTok LIVEとツイキャス。
- Windows 11の**WSL2 Ubuntu 24.04**内で1つのPythonアプリプロセスを常駐させ、WindowsのPCブラウザからlocalhostで操作する。
- UIは管理機能のみ。動画・ライブ映像のWeb再生、公開配信、ログイン、外部向けマルチユーザー運用は対象外。
- ネットワークは初期 `127.0.0.1` のみ。ブラウザは最初に割り当てられた実行ポートへ接続する。
- 録画とコメント取得は**同一配信セッションに紐付くが、独立したジョブ**。どちらかの失敗がもう一方を停止させない。

## 2. 実行時構成

```text
Windows 11
  ├─ PC browser ── localhost ──┐
  └─ recording volume (NTFS)  │   /mnt/<drive>/LiveRecorder/recordings/
                               v
WSL2 Ubuntu 24.04 LTS       FastAPI + Uvicorn (single process, 1 worker)
  ├─ Web UI (Jinja2/HTMX) ─────┤
  ├─ REST /api/v1 ─────────────┤
  ├─ CLI (start/status/url/backup)
  ├─ services/ ── sessions/jobs coordination
  ├─ monitoring/ ── per-platform scheduler
  │                     ├─ TikTok adapter
  │                     └─ TwitCasting adapter
  ├─ recording/ ── recorder subprocess(es) ── Windows recording volume
  ├─ comments/ ── collector/SQLite ── SRT generation ── Windows volume
  ├─ db/ ── SQLite WAL (WSL2 ext4 filesystem)
  └─ ops/ ── logs, endpoint.json, restart reconciliation, online DB backup
```

**採用技術：** CPython 3.12、uv、FastAPI/Uvicorn、Jinja2/HTMX/軽量JS、SQLite WAL、SQLAlchemy 2.x、Alembic、asyncio、Streamlink / yt-dlp / FFmpeg / ffprobe、pytest。

## 3. プロセスとライフサイクル

- 初期版は**1 Pythonプロセス + 1 Uvicorn worker**。API・UI・監視・ジョブ統括を同居させる。録画エンジンは子プロセス。
- 起動時には多重起動防止、設定・SQLite・保存先・依存ツールの診断、未完了ジョブの復旧照合を行う。
- OSの空きポート割当（`port=0`）を標準とし、確保したソケットを閉じずにサーバーへ渡す。endpoint情報はPID・起動時刻・instance ID付きで原子的に保存し、生存確認なしに古いURLを案内しない。
- `uvicorn --reload` と複数workerによる監視の二重起動を避ける。
- 終了時は新規受付停止 → ジョブ停止通知 → 録画子プロセス安全終了 → コメント書込 → 状態記録 → 制限時間内に終了。
- `systemd` のアプリ再起動機能と、WSL2インスタンス自体の起動維持は**別問題**。Windowsサインイン後の起動と復帰照合を設計対象にする。

## 4. コンポーネント責務

| 部品（予定位置） | 責務／境界 |
| --- | --- |
| `cli.py`、`__main__.py` | start/status/url/backupコマンド。UIと同じサービス層を使用 |
| `app.py` | FastAPIの組立、ルータ登録、lifespan。ビジネスロジックを置かない |
| `web/pages.py`、`web/api.py` | HTMLとREST `/api/v1` のアダプタ。DBへ直接業務操作を書かない |
| `services/sessions.py`、`services/jobs.py` | セッションの一意性、ジョブの生成・停止・連携を調整 |
| `platforms/` | サービス固有の認証・通信・イベント正規化 |
| `monitoring/` | 独立したサービス別監視枠、巡回公平性、LIVE検出 |
| `recording/` | エンジン選択、子プロセス、再試行、セグメントの検証・確定 |
| `comments/` | 収集・重複排除・永続化、録画セグメント別SRT出力 |
| `storage.py` | Windows互換ファイル名、保存ルート、衝突防止、パス対応を一元化 |
| `db/`、`ops/` | DBモデル・マイグレーション、ログ、復旧、バックアップ |

パッケージ名は `src/live_rec_lab/`。Web も `src/live_rec_lab/web/` に集約する（`requirements.md` §10.1.1.1、§10.1.1.2、§10.1.1.7、現行リポジトリ）。

## 5. 依存方向

```text
Web (Jinja2/HTMX) ───┐
REST API ────────────┼──> services/ ─┬─> monitoring/ ─> platforms/
CLI ────────────────┘                ├─> recording/ ──> storage.py
                                    ├─> comments/ ───> storage.py
                                    └─> db/
```

将来のWindowsトレイ・デスクトップクライアントは `/api/v1` を通す。直接SQLiteを変更しない。初期段階では使われないインターフェース、分散キュー、Docker構成を先に構築しない。

## 6. 初期起動から録画への主要フロー

1. CLI起動 → 単一インスタンス確認 → DBと設定の初期化／検証。
2. 以前の未完了セッション・セグメント・ファイルを照合し、未確定を誤って完成扱いにしない。
3. 監視対象を読み込み、サービス別スケジューラを起動。
4. `LIVE`検出 → 配信IDの照合 → セッションの重複防止・登録。
5. 設定に従って録画／コメントジョブを**別々に**開始。
6. 取得内容とジョブ状態を保存し、Web UIへ表示。
7. セグメントを検証・確定し、取得済みコメントからセグメント別SRTを生成。

## 7. 将来の拡張境界

後処理（Whisper等）、通知、他の配信サービス、ネイティブクライアントは将来機能。初期段階で依存関係や空パッケージを増やさない。新サービス追加時は必要最低限のアダプタ契約を検証してから導入する。
