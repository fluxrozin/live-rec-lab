# 01. システム基本設計 — 構成・責務・依存関係

> 出典：[`requirements.md`](../../requirements.md) §1–3、§10.1.1。**設計ドラフト**。

## 1. システム境界

- 個人用・ローカル実行のライブ配信記録システム。初期の対象サービスはTikTok LIVEとツイキャス。
- Windows 11の**WSL2 Ubuntu 24.04**内で1つのPythonアプリプロセスを常駐させ、WindowsのPCブラウザからlocalhostで操作する。
- UIは主要7画面の管理機能で構成する。システム状態は健康状態と診断要約、ログは既存の運用記録の詳細調査を担う。動画・ライブ映像のWeb再生、公開配信、ログイン、外部向けマルチユーザー運用は対象外。
- ネットワークは初期 `127.0.0.1` のみ。ブラウザは最初に割り当てられた実行ポートへ接続する。
- 録画とコメント取得は**同一配信セッションに紐付くが、独立したジョブ**。どちらかの失敗がもう一方を停止させない。

## 2. 実行時構成

```text
Windows 11
  ├─ PC browser ── localhost ──┐
  └─ recording volume (NTFS)  │   /mnt/<drive>/live-rec-lab/recordings/
                               v
WSL2 Ubuntu 24.04 LTS       FastAPI + Uvicorn (single process, 1 worker)
  ├─ Web UI (Jinja2/HTMX) ─────┤
  ├─ REST /api/v1 ─────────────┤
  ├─ CLI (start/status/url/backup/stop)
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
- 起動時には多重起動防止、設定・SQLite・保存先・依存ツールの診断、未完了ジョブの復旧照合を行う。録画と検証に使用する `ffmpeg`・`ffprobe` は**実際の常駐プロセス環境から解決される実行ファイル**とバージョン・コーデック対応を確認する。対話シェルの `PATH` だけを根拠に正常判定しない。
- OSの空きポート割当（`port=0`）を標準とし、確保したソケットを閉じずにサーバーへ渡す。endpoint情報はPID・起動時刻・instance ID付きで原子的に保存し、生存確認なしに古いURLを案内しない。
- `uvicorn --reload` と複数workerによる監視の二重起動を避ける。
- 終了時は新規受付停止 → ジョブ停止通知 → 録画子プロセス安全終了・回収と有意な一時ファイルの保全 → コメント受信タスク終了・キュー残件保存 → 状態記録 → 制限時間内に終了。タイムアウトや内部取消を利用者の手動停止と混同しない。
- `systemd` のアプリ再起動機能と、WSL2インスタンス自体の起動維持は**別問題**。Windowsサインイン後の起動と復帰照合を設計対象にする。

## 4. コンポーネント責務

| 部品（`src/live_rec_lab/`） | 責務／境界 |
| --- | --- |
| `cli.py`、`__main__.py` | `start`／`status`／`url`／`backup`／`stop`。起動・停止とバックアップを扱い、画面の業務操作は行わない |
| `app.py` | FastAPIの組立、ルータ登録、lifespan。ビジネスロジックを置かない |
| `web/pages.py`、`web/api.py`、`web/security.py` | HTML、REST `/api/v1`、変更時のHost・Origin・CSRF。HTML断片は `web/templates/partials/`。DBへ直接業務操作を書かない |
| `services/sessions.py`、`services/jobs.py`、`services/retention.py`、`services/media_files.py` | セッションの一意性、ジョブの生成・停止・連携、期限削除、管理ファイルの照合 |
| `platforms/` | サービス固有の認証・通信・イベント正規化 |
| `monitoring/` | 独立したサービス別監視枠、巡回公平性、LIVE検出。HTTP／通信失敗と明示的OFFLINEを識別し、終了猶予の根拠を管理 |
| `recording/` | 実績のある録画エンジン選択、子プロセス回収、再試行、確定後の実ファイル検証、セグメントの確定。ツイキャスは `hls_playlist.py` で初期HLSと404後の再解決を扱い、配信継続中は同じジョブで再接続する |
| `comments/` | 受信タスクの寿命・例外・停止処理、収集・重複排除・永続化、録画セグメント別SRT出力／字幕対象なしのスキップ |
| `storage.py` | Windows互換ファイル名、保存ルート、既存アプリとの実効ルート衝突検知、配信者ID→フォルダ安全名のサービス非依存マッピング、衝突防止（初回衝突時のセッション固定識別子・上書き禁止の排他確定）、パス対応を一元化。同一配信のファイル名は配信開始時刻を共有し、**保持する**セグメントの番号をジョブをまたいでセッション単位で進める。空の破棄試行では連番を消費しない |
| `db/`、`ops/` | DBモデル・マイグレーション、コメント期限削除（SRT先行照合・操作台帳・再開）、既存構造化ログの検索／段階的読取、復旧、バックアップ |

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
4. `LIVE`検出 → 配信IDの照合 → セッションの重複防止・登録。進行中の配信に対する `UNKNOWN`／`ERROR` は明示的な終了証拠ではない。
5. 監視・録画・コメントの有効条件（全体コメント設定と配信者別設定はAND）と手動停止指示を判定し、録画／コメントジョブを**別々に**開始。稼働中の設定OFFは影響するジョブを安全に停止し、他方のジョブは可能な限り維持する。
6. 取得内容とジョブ状態を保存し、Web UIへ表示。
7. 保持する**最終ファイル**の映像・音声・長さ・実在を検証してセグメントを確定し、対象区間に字幕コメントがあるときだけSRTを生成。対象なしはスキップとする。
8. セッション概要は最新ジョブだけで作らず、**全録画ジョブ・保持セグメント・コメントジョブ・欠損記録**を照合する。

## 7. コンポーネントをまたぐ不変条件

- **配信状態・ジョブ状態・セグメント検証・セッション全体の取得品質を分ける。** 最新ジョブ `COMPLETED` は過去の `FAILED`、`invalid`／`pending`、記録済み欠損を打ち消さない。集計根拠が不足した場合は「完全成功」にせず未判定とする。
- `ENDING` は明示的終了根拠に基づく猶予であり録画停止命令ではない。`UNKNOWN`／`ERROR` だけで終了を確定しない。監視不能かつ取得ジョブ終了が継続した場合は期限付きで再照合し、確認できなければ「終了未確認」として収束・欠損を残す。監視OFFは明示OFFLINEではなく、その配信者の稼働ジョブを安全に停止する。
- 録画エンジンの起動成功、ファイルの非0バイト、コンテナのduration、映像コーデックの判明、実際の本編・全区間の収録は別の確認段階。検証段階を飛ばして成果物を成功扱いにしない。
- コマンド・受信タスク・ファイル・DB・ログの各境界で、処理IDと発生時刻を連携する。ただし確認できない相関情報をログ本文から推定生成しない。

## 8. 将来の拡張境界

後処理（Whisper等）、通知、他の配信サービス、ネイティブクライアントは将来機能。初期段階で依存関係や空パッケージを増やさない。新サービス追加時は必要最低限のアダプタ契約を検証してから導入する。
