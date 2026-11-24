# LiveRecorder — 統合システム設計書

> 製品レベルの設計基準。要求・UI正本に従う。未確定の技術案は合理的に選択し、未実証を区別する。確定製品要件の変更はAGENTS.mdに従う。

## 0. 文書の位置付け・横断設計

### ステータスの読み方

- **確定要件の抽出**：出典に明記された必須事項。設計文書の言い換えで仕様を変えない。
- **設計方針**：要件が指示する責務や処理の整理。詳細なAPIシグネチャやDDLなどは実装フェーズで検証する。
- **要検証／要決定**：外部APIや実機の挙動、未定の振る舞い。未実装・未確認を「対応済み」と表現しない。

設計詳細の拘束力は要求§1.3に従う。MUSTの安全性・確定機能を維持する限り、内部構造・状態の実装・処理手順・検証方法はSHOULDとして簡素化できる。以下のDDL・APIシグネチャ・内部状態・台帳分割は基準案であり、同じ公開上の意味と不変条件を保つ等価実装を認める。採用差分は簡潔に記録する。追加品質・候補はMAY。 §16〜18のテーブル・列・内部API・台帳・処理順は同じ不変条件と公開契約を満たす等価方式に置換可。個別の「確定方針」は結果・不変条件を固定するもので、要求§1.3に従い内部手段まで固定しない。

### 横断的な設計の確認事項

- **録画ジョブの完了はセッションの完全取得ではない。** 過去の失敗・無効ファイル・欠損を、最新ジョブの成功で隠さない。
- 空の試行で保持セグメントの連番を消費せず、セッションIDと親ジョブの複合外部キー・セッション内一意制約・採番予約で複数ジョブの番号を重複させない。未確定ファイルを先に記録しつつ、最終ファイルの存在とメディア内容で検証する。
- 配信者の論理削除後の同一キー再登録は既存行を再有効化する。ファイル名の初回同名衝突にはセッション固定の識別子と排他的な確定を使う。コメント期限は初期無期限、期限削除時はコメントSRT・統合ASSを先に削除しSQLiteコメントを消去して期限削除済み状態を記録する。
- SRTの対象コメント0件は「字幕対象なし（スキップ）」。コメントを収集できなかった場合や生成失敗と混同せず、後着コメント待ちなら暫定として扱い、入力版数と生成時版数で再同期を判定する。SRT公開前後の版数競合・旧正常版保全・0件への正常更新時の旧SRT除去も検証する。
- 監視の `UNKNOWN`・`ERROR` は明示的な配信終了ではない。監視・録画・コメントの設定OFFは停止対象と復帰条件を分ける。自動削除は確定済みファイルのみに限定し、履歴のみ削除を提供しない。HLSの短い先頭や検証できないメディアも本編全体の録画完了とは扱わない。
- テスト結果は実機・ローカル・未検証を分け、§17の固定 `TC` と§23の `AC` に対応付ける。既存アプリとの保存ルート分離、配信者フォルダの永続的な衝突解消、古いSQLiteバックアップの手動破棄・復元前確認、ログの閲覧時再マスキングも横断テストに含める。検証環境固有のバージョン・パス・閾値を固定要件にしない。未確認の外部API仕様や実装済み状態を創作しない。

### 変更・利用ルール

- 機能要件を変更する場合、先に `docs/requirements.md` を更新し、影響する設計・Spec Kit featureへ反映する。UI固有変更は `docs/ui-spec.md` も更新する。
- 本書の設計詳細は要求を実装可能にするための設計基準である。要求の意味を変更しない。重複記述が矛盾した場合は原本を確認する。
- REST・データ制約・状態遷移の正本は本書§16〜18。Featureのdata-model.md、contracts/、実装後のalembic/versions/はこれらから具体化する。確定契約の意味と互換性を維持し、内部方式の合理的な差分は要求§1.3に従って記録・横断整合する。
- `src/` 内に実装のない空モジュールや、将来機能用のダミーコードを生成しない。
- 初期対象は **Windows 11 + WSL2 Ubuntu 24.04 / Python 3.12 / uv / TikTok LIVE + ツイキャス**。要求仕様に従い、別構成のWindowsネイティブ案を無断で混入しない。

## 3. システムアーキテクチャ

以下を基本構成としてください。

```text
Windows Browser
      |
      v
WSL2: FastAPI Web Application（1プロセス）
      |
      +---- Web UI / REST API (/api/v1)
      |        |
      |        +---- SQLite（設定・履歴・コメント・イベント）
      |
      +---- Monitoring Scheduler
      |        |
      |        +---- TikTok Adapter（独立監視枠）
      |        +---- TwitCasting Adapter（独立監視枠）
      |                    |
      |                 Live Detection
      |                    |
      |           +--------+---------+
      |           |                  |
      |           v                  v
      |      Video Recorder     Comment Collector
      |           |                  |
      |      Streamlink/yt-dlp       v
      |      FFmpeg / ffprobe      SQLite
      |           |
      |           v
      |      Windows Drive（/mnt/<drive>/...）
      |
      +---- Runtime endpoint.json / Structured Logs
```

録画処理、コメント取得処理、Web UI、データベース操作の責務を明確に分離してください。配信を検出したら、配信IDの取得、セッション重複確認、セッション作成、録画ジョブとコメントジョブの独立起動、状態・欠損記録、Web UIへの反映の順に処理します。録画とコメント取得は一方が失敗しても他方を止めないでください。

各プラットフォーム固有処理はアダプターとして実装し、将来的にYouTube Live、ニコニコ生放送、Twitch、SHOWROOMを追加できる構造としてください。`normalize_identity`、`check_live`、`collect_comments`、`resolve_recording_source`、`capabilities` は最小限の共通契約の候補ですが、使わない抽象化は先行実装しないでください。コメント取得はWebSocket・イベント購読・APIポーリングを許容し、対応していない機能は明示してください。

Web管理画面とは別に、将来Windowsトレイアイコンアプリやデスクトップ管理アプリを作れるよう、`/api/v1` とサービス層をUIから独立させます。将来のクライアントがSQLiteを直接変更する方式にはしないでください。

---

## 9. データベース設計

SQLiteで以下のテーブルを基本として設計してください。WALモード、`busy_timeout`、短いトランザクション、外部キー制約（`PRAGMA foreign_keys=ON`）を基本とし、DBはWSL2内のローカルLinuxファイルシステムに置いてください。

### 9.1 streamers

監視対象配信者情報。

フィールド例：

- id
- platform
- platform_user_id
- display_name
- profile_url
- monitoring_enabled
- recording_enabled
- comments_enabled
- last_checked_at
- last_live_at
- sort_order（有効一覧内の正整数順位、論理削除中は順位対象外）
- archived_at（履歴あり配信者の論理削除）
- created_at
- updated_at

### 9.2 stream_sessions

ライブ配信単位の情報。

- id
- streamer_id
- platform_stream_id
- title
- platform_started_at
- detected_at
- platform_ended_at
- ended_at（アプリが収束を記録した時刻）
- comments_purged_at／purged_comment_count（コメント期限削除の時刻・削除件数。未削除はNULL／0）
- filename_datetime（最初の正式名予約時のIANAタイムゾーン変換済み固定日時文字列。未予約はNULL）
- filename_title（安全化・短縮済みのセッション固定タイトル。未予約はNULL。元の `title` と独立）
- filename_disambiguator（同名衝突があったセッションだけの固定識別子。通常はNULL）
- end_reason（確認済み終了／終了未確認／監視停止等）
- last_seen_live_at
- next_segment_number（保持セグメントの次回採番、セッション単位）
- status
- created_at
- updated_at

プラットフォーム側の配信IDが取得できない場合の識別方法も設計してください。セッションの再作成は一時的なUNKNOWN/OFFLINEだけでは行わず、状態確認・終了猶予・既存ジョブから判断してください。

### 9.3 recording_jobs

録画ジョブ情報。

- id
- stream_session_id
- status
- recorder_engine
- started_at
- finished_at
- retry_count（ジョブ固有の追加試行統計）
- retry_budget_id（§15.7の共有予算世代を参照）
- last_error
- manual_stop_requested
- created_at
- updated_at

### 9.4 recording_segments

録画ファイル単位の情報。

- id
- recording_job_id
- stream_session_id（親ジョブと整合するセッションID）
- segment_number（未採番中はNULL）
- recorder_engine（実際に当該セグメントを取得したエンジン）／attempt_id（試行イベントとの関連）
- started_at
- finished_at
- file_path
- file_size
- duration_ms
- first_media_at（取得可能なら）
- media_start_pts（取得可能なら）
- sync_offset_ms（取得可能なら）
- sync_method（取得可能なら）
- container_format
- validation_status
- srt_path（生成成功時）
- srt_status（未生成／生成成功／生成失敗等）
- srt_generated_at（取得可能なら）
- srt_input_version／srt_generated_from_version（コメント・補正変更と再同期の照合用）
- srt_provisional（コメント確定待ち）
- files_deleted_at（動画・全対応字幕の意図的削除時、検証状態とは区別）
- created_at

録画の再接続・分割保存に対応してください。**セグメントはDBへ未確定状態で先に登録し、Windows側の同一ボリューム上の一時ファイルに録画→ffprobe等で検証→正式名へリネーム→DBへ確定結果を記録**する順序を基本とします。再起動時は一時ファイル・正式ファイル・DBを照合し、途中で停止した段階を判定して重複録画やデータ消失を防いでください。録画ファイルの確定とDB更新を完全に同時実行できるとは仮定しません。

`segment_number` は**同一配信セッション内で、保持するセグメントについて一意かつ時間順に増加**させ、録画ジョブが分割されても同じセッションの前回最大番号から継続してください。SQLiteでは `UNIQUE(stream_session_id, segment_number)` を用い、NULLの未採番行を許容します。`recording_jobs` の `(id, stream_session_id)` を参照する**複合外部キー**でセグメントの親セッションとジョブの不一致を防ぎ、親側にも対応する複合一意キーを設けます。保持すべき実データがあると判定した時点で、DB書込トランザクション内でセッションの `next_segment_number` を排他的に更新し、番号と正式ファイル名の予約を確定します。**空試行は予約しません**。予約後はクラッシュ・削除・ファイル確定失敗でも番号を再使用せず、DB予約／一時ファイル／正式ファイルを起動時に照合します。保持した `invalid` や `pending` の実記録は欠損の証跡として区別し、過去のファイル名・DB番号を表示の都合だけで振り直しません。

### 9.5 comment_collection_jobs

コメント取得ジョブ情報。

- id
- stream_session_id
- status
- started_at
- finished_at
- received_count
- stored_count
- duplicate_count
- discarded_unverified_count／discarded_overflow_count／discarded_other_count
- save_failed_count／pending_count
- last_received_at／last_durable_at（受信と永続確定を区別）
- counts_complete（永続前の消失等で不完全ならfalse）
- error_count（処理エラー事象数）
- reconnect_count（ジョブ固有の追加接続試行統計）
- retry_budget_id（§15.7の共有予算世代を参照）
- last_error
- connection_generation／verified_platform_stream_id（接続世代と確認済み配信ID）
- verified_boundary（確実な取得境界、公式APIの契約に応じた形式）
- created_at
- updated_at

### 9.6 comments

コメント情報。

- id
- stream_session_id
- platform_comment_id
- platform_user_id
- nickname
- is_anonymous（判定できる場合）
- anonymous_label（番号が取得できた場合のみ）
- comment_source（API／WebSocketなど）
- message
- posted_at
- received_at
- stream_offset_ms
- recording_offset_ms
- created_at

検索対象フィールドに適切なインデックスを設定してください。同一配信内でプラットフォームのコメントIDが得られる場合は `(stream_session_id, platform_comment_id)` に一意性を設け、RESTとWebSocketの重複を除いてください。NULLの投稿者IDや匿名番号から実在人物を推定しないでください。

### 9.7 collection_gaps

データ欠損区間情報。

- id
- stream_session_id
- job_type
- started_at
- ended_at
- reason
- recovery_key（同一中断の冪等性識別、必要時）
- start_basis／end_basis（実測・近似・不明と根拠。時刻不明はNULL）
- gap_status（open／closed。時刻不明で閉じた区間と復旧待ちの開区間を区別）
- created_at

### 9.8 system_settings

システム設定情報。

- key
- value
- updated_at

固定Webポート、自動更新間隔（整数秒）、録画ルートを非秘密設定として `system_settings` に保存する。設定値の初期値・検証範囲・反映時期・優先順位は `docs/requirements.md` §11.8.1〜3 に従う。設定キーは `web_port`、`web_refresh_interval_seconds`、`recording_root` とする。

APIキーやパスワードなどの秘密情報は、安全な環境変数または適切な秘密情報管理方式で扱い、通常の設定値と同じ形式で不用意に保存しないでください。

### 9.9 runtime_instances

認証用usersテーブルは作成しません。代わりに必要なら単一インスタンスの実行状態を管理してください。

- id
- instance_id
- started_at
- stopped_at
- last_heartbeat_at
- process_id
- status

実行時のポート番号・URLは実行時状態ディレクトリ（通常 `~/.local/state/live-rec-lab/runtime/endpoint.json`）に公開し、PID・起動時刻・インスタンスIDの照合により終了後の古い情報を誤用しないようにしてください。

### 9.10 system_events

監視・録画・コメント収集などのイベントログ。

- id
- event_type
- severity
- stream_session_id
- message
- metadata
- created_at

`system_events` は障害・事象の検索用要約であり、構造化ログファイルの全行を保存するテーブルではありません。イベントID／処理ID等が実際に記録できる場合に限り、対応するログ詳細を `/logs` で調べられるようにしてください。本文から対象配信者・セッションIDを推測して関連付けないでください。

### 9.11 deletion_operations

動画・全対応字幕とDBの削除が途中で停止しても再開できる、最小限の**削除操作台帳**です。

- id
- target_type／target_id（削除後にも監査できる識別子）
- mode（手動：ファイルのみ／ファイル＋履歴、自動：ファイルのみの容量・期限削除／コメント期限削除）
- status（準備中／実行中／一部失敗／完了）
- target_paths（許可済み保存ルートで再照合する対象）
- target_entries（対象ごとの管理ID、実効ルート識別、相対パス、期待するファイル世代、確認済み不在／削除済み／保留／失敗の状態・理由）
- started_at／finished_at／last_error

操作台帳を先にコミットしてからファイル削除を行い、実際の削除結果を確認後にDBのファイル削除状態または履歴削除をコミットします。コメント期限削除では派生コメントSRT・統合ASSの削除を先に照合してからSQLiteコメントを消去し、セッションの期限削除状態を確定します。履歴を消した後にも必要な操作台帳は残し、削除失敗の再試行を冪等にしてください。パスに関する安全条件は§10.4に従います。

ファイル世代は、WSL2 ext4とWindowsマウントで安定性を実証したデバイス／ファイルID等とサイズ・mtime・ctime等の組合せを使用する。単独のinodeやパスを所有の証明としない。ルートのマウント識別、ファイルの実体、管理ID、必要な識別情報を照合し、利用できない場合は保留する。ファイル操作直前の再照合と、置換競合を防ぐOSの操作手段／隔離を検証する。外部の同時置換に対し安全な削除を保証できない場合は対象を保留する。全削除方式は同じ台帳・所有判定を使用する。

必要に応じてテーブル追加・正規化を行ってください。ただし、将来の機能のためだけの巨大なスキーマは先行実装しないでください。

Alembicによるマイグレーションを必須としてください。

外部キー制約、ユニーク制約、インデックス、削除時の参照整合性も設計してください。

- 配信者の一意性はサービス名と正規化IDの組み合わせで管理する。`archived_at` が設定済みの同一アカウントを再登録したときは新規INSERTせず既存行を再有効化（`archived_at=NULL`）する。過去セッション・履歴・IDは維持し、監視／録画／コメントの各ON/OFFは登録フォームの明示値で再設定する（未指定時は入力エラーとし、以前のON設定を自動復元しない）。稼働ジョブの二重生成を防ぎ、復元失敗を新規登録成功と表示しない
- 配信IDが取得できる場合は同じ配信の二重セッション作成を防ぐ
- コメントIDがある場合は同じセッション内で一意性を保つ
- セッションを削除するときのジョブ・コメント・録画ファイルの扱いを明確にする
- DBスキーマの変更時はAlembicマイグレーションを残し、手作業で既存データを破壊しない

---

## 10.1.1 リポジトリ・配置設計

この節はリポジトリ配置設計を定義します。以下のフォルダ構成、Web画面・REST APIの配置、責務、将来拡張先を設計制約とします。未実装機能の空ファイルやダミー実装は作成不要です。

- 設計の位置付け: **配置先と責務の予約**。未実装機能の空ファイルやダミーコードの生成は要求しない。
- 実行基盤: Windows 11 + WSL2 Ubuntu、Python 3.12、uv、FastAPI/Jinja2/HTMX、SQLite。
- 方針: 趣味の個人利用に必要な単純さを優先し、初期版は単一アプリケーションプロセスとする。

### 10.1.1.1 フォルダ・ファイル構成

記号: `[生]` は開発開始後のSpec Kit／SDD生成物、`[必]` は必須の基準文書、`[初]` は初期版の実装候補、`[後]` は将来機能の配置予定、`[任]` は実際に必要になったときに作成。**この一覧のファイルをすべて初回コミットで作る意味ではない。**

```text
live-rec-lab/
├── AGENTS.md                               [初] Codex等向けのリポジトリ共通指示
├── CLAUDE.md                               [初] Claude Code向けプロジェクト指示
├── .specify/                               [生] Spec Kit初期化で生成
│   ├── integration.json
│   ├── integrations/
│   ├── memory/
│   │   └── constitution.md
│   ├── scripts/
│   └── templates/
├── specs/                                  [生] SDD実行で生成する仕様成果物
│   └── NNN-initial-live-recorder/
│       ├── spec.md
│       ├── plan.md
│       └── tasks.md
├── .claude/                                [生] 選択したClaude Code統合
│   ├── skills/
│   │   └── speckit-*/SKILL.md
│   └── settings.json                      [任] Spec Kitイベント設定等
├── .agents/                                [生] 選択したCodex統合
│   └── skills/
│       └── speckit-*/SKILL.md
├── .codex/                                 [任] Codex固有設定
│   └── config.toml
├── .cursor/                                [生] 選択したCursor統合
│   ├── skills/
│   │   └── speckit-*/SKILL.md
│   ├── rules/
│   │   └── project.mdc                    [任] Cursor固有ルール
│   └── hooks.json                         [任] Spec Kitイベント設定等
├── .python-version                         [初] Python 3.12
├── .env.example                            [初] 機密値はダミーのみ
├── .gitignore                              [初]
├── pyproject.toml                          [初] ビルド設定・CLIエントリポイント
├── uv.lock                                 [初]
├── README.md                               [初]
├── alembic.ini                             [初]
├── alembic/                                [初] SQLiteスキーマ変更履歴
│   ├── env.py
│   └── versions/
├── src/
│   └── live_rec_lab/
│       ├── __init__.py                     [初]
│       ├── __main__.py                     [初] CLIへの薄い委譲
│       ├── cli.py                          [初] start/status/url/backup等
│       ├── app.py                          [初] FastAPI初期化・ルーター登録・lifespan
│       ├── config.py                       [初] 設定の読み書き・検証
│       ├── storage.py                      [初] Windows保存パス・名前生成・検証
│       ├── services/                       [初] UI/API/CLI共通の業務操作
│       │   ├── sessions.py                 [初] 配信セッションの調整
│       │   └── jobs.py                     [初] 録画・コメント処理の起動調整
│       ├── platforms/                      [初] プラットフォーム依存処理
│       │   ├── base.py                     [初] 必要最小限の共通契約
│       │   ├── tiktok.py                   [初] TikTokLive・Euler Stream等
│       │   ├── twitcasting.py              [初] Basic認証・公式API・匿名WebSocket
│       │   ├── youtube.py                  [後] YouTube Live
│       │   ├── niconico.py                  [後] ニコニコ生放送
│       │   ├── twitch.py                   [後] Twitch
│       │   └── showroom.py                 [後] SHOWROOM
│       ├── monitoring/                     [初]
│       │   ├── scheduler.py                [初] サービス別監視頻度・レート制限
│       │   └── state.py                    [初] LIVE/OFFLINE/UNKNOWN等
│       ├── recording/                      [初]
│       │   ├── manager.py                  [初] subprocessライフサイクル
│       │   ├── engines.py                  [初] Streamlink/yt-dlp/FFmpeg
│       │   └── segments.py                 [初] 分割・確定・検証
│       ├── comments/                       [初]
│       │   ├── store.py                    [初] コメント正規化・永続化
│       │   └── srt.py                      [初] 録画セグメント単位のコメントSRT
│       ├── db/                             [初]
│       │   ├── models.py                   [初] SQLAlchemyモデル
│       │   ├── session.py                  [初] SQLite接続・トランザクション
│       │   └── queries.py                  [初] 共通クエリ
│       ├── web/                            [初] Web管理画面とREST APIを同居
│       │   ├── __init__.py                 [初]
│       │   ├── pages.py                    [初] 画面用HTMLルーター
│       │   ├── api.py                      [初] JSON REST API /api/v1
│       │   ├── partials.py                 [任] HTMX HTML断片レスポンス
│       │   ├── schemas.py                  [任] APIの入出力モデル
│       │   ├── templates/                  [初] Jinja2
│       │   │   ├── base.html               [初] 共通レイアウト・メニュー
│       │   │   ├── dashboard.html          [初] ダッシュボード
│       │   │   ├── streamers/
│       │   │   │   ├── list.html           [初] 配信者一覧
│       │   │   │   └── form.html           [初] 登録・編集
│       │   │   ├── recordings/
│       │   │   │   ├── list.html           [初] 録画履歴
│       │   │   │   └── detail.html         [初] 録画詳細
│       │   │   ├── comments/
│       │   │   │   └── list.html           [初] コメント検索・閲覧
│       │   │   ├── system/
│       │   │   │   └── index.html          [初] システムの健康状態・診断要約
│       │   │   ├── logs/
│       │   │   │   └── index.html          [初] ログ一覧・絞り込み・詳細展開
│       │   │   ├── settings/
│       │   │   │   └── index.html          [初] 各サービス監視頻度・表示設定等
│       │   │   └── partials/
│       │   │       ├── live_status.html    [任] 状態部品
│       │   │       └── streamer_row.html   [任] 一覧行部品
│       │   └── static/
│       │       ├── css/
│       │       │   └── app.css            [初] PC向けスタイル
│       │       ├── js/
│       │       │   └── app.js             [初] 最小限のJS
│       │       ├── vendor/
│       │       │   └── htmx.min.js        [初] バージョン固定した配布物
│       │       └── img/                    [任] ローカル画像・アイコン
│       ├── ops/                            [初] 運用・エラー追跡
│       │   ├── logging_config.py          [初] ログ設定・相関ID
│       │   ├── log_reader.py               [初] 現行・ローテーション済みログの安全な絞り込み／ページング／詳細参照
│       │   ├── recovery.py                [初] 再起動時の状態復元
│       │   └── backup.py                  [初] SQLiteオンラインバックアップ
│       ├── postprocess/                    [初] 録画後の動画・音声AI処理
│       │   ├── jobs.py                    [初] 非同期後処理ジョブ
│       │   ├── transcription.py           [初] Whisper文字起こし
│       │   ├── audio_cleanup.py           [後] BGM/背景音の低減
│       │   ├── diarization.py             [後] 話者分離
│       │   ├── target_speaker.py          [後] 映像も使った対象話者選別
│       │   └── ass.py                     [初] 発言+コメントのASS字幕
│       └── notifications/                  [後] イベント配信・外部通知
│           ├── dispatcher.py             [後] 宛先・再送制御
│           └── channels/                  [後]
│               ├── windows.py            [後] Windows通知・トレイ連携
│               ├── discord.py            [後] Discord Webhook
│               └── email.py              [後] メール通知
├── clients/                                [後] Windowsネイティブクライアント
│   ├── windows-tray/                       [後] トレイ常駐UI
│   └── windows-desktop/                    [後] デスクトップ管理UI
├── tests/
│   ├── conftest.py                         [初]
│   ├── test_monitoring.py                 [初]
│   ├── test_recording.py                  [初]
│   ├── test_storage.py                    [初]
│   ├── test_comments.py                   [初]
│   ├── test_speech.py                     [初]
│   ├── test_ass.py                        [初]
│   ├── test_srt.py                        [初]
│   ├── test_api.py                        [初]
│   ├── test_web_ui.py                     [初] 主要8画面・状態表示・画面間導線
│   ├── test_logs.py                       [初] ログの検索・権限制御・秘匿
│   ├── test_db.py                         [初]
│   └── fixtures/                          [初]
├── docs/
│   ├── requirements.md                   [必] 製品要求仕様
│   ├── system-design.md                  [必] 統合設計
│   └── ui-spec.md                        [必] UI/UX仕様
├── prompts/
│   ├── implement.md                      [必] 全体実装プロンプト
│   └── redesign-ui.md                    [必] UI設計・実装プロンプト
├── manual/
│   └── README.md                         [初] 実装後の統合マニュアル
├── scripts/
│   └── start_windows.ps1                  [初] Windows側からWSL2起動
└── config/                                 [任] 外部設定が必要なとき
```

> `__init__.py` は必要なPythonサブパッケージに適宜配置。`[後]` に属する未実装フォルダ・ファイルは初期リポジトリに作らない。初期スターターはREADME・AGENTS・CLAUDE・.gitignore・pyproject.toml・docsの3文書・promptsの2文書・manual/README.mdのみ。`.specify/`・`specs/`・エージェント用Skillsは同梱せず、開発開始時に公式初期化とSDDの各工程で生成しGit管理する。実行時のDB・ログ・endpoint.jsonは通常リポジトリ外へ置く。

### 10.1.1.2 Web UI の実装・URL体系

Webのコードを別リポジトリやWindows側へ置かず、**`src/live_rec_lab/web/` に集約**する。`app.py` はFastAPIインスタンス生成・ルーター登録・静的ファイルマウントだけを扱い、画面表示処理やDBクエリを直接書かない。

| URL例          | 処理対象                         | 実装箇所                                   | 返すもの     |
| -------------- | -------------------------------- | ------------------------------------------ | ------------ |
| `/`            | ダッシュボード                   | `web/pages.py` → `dashboard.html`          | HTML         |
| `/streamers`   | 配信者一覧・追加／編集           | `web/pages.py` → `streamers/`              | HTML         |
| `/recordings`  | 録画履歴・配信セッション詳細     | `web/pages.py` → `recordings/`             | HTML         |
| `/comments`    | コメント検索・閲覧               | `web/pages.py` → `comments/`               | HTML         |
| `/speech`      | 音声字幕・統合ASS管理            | `web/pages.py` → `speech/index.html`       | HTML         |
| `/speech/cues` | 発話本文閲覧・検索               | `web/pages.py` → `speech/cues.html`        | HTML         |
| `/system`      | システム健康状態・障害の診断要約 | `web/pages.py` → `system/`                 | HTML         |
| `/logs`        | アプリケーションログの検索・閲覧 | `web/pages.py` → `logs/`                   | HTML         |
| `/settings`    | 設定画面                         | `web/pages.py` → `settings/`               | HTML         |
| `/partials/*`  | 必要なHTMX部分更新               | `web/partials.py` と `templates/partials/` | HTML断片     |
| `/api/v1/*`    | Web以外の管理操作にも利用        | `web/api.py`                               | JSON         |
| `/static/*`    | CSS・JS・画像                    | `web/static/`                              | 静的ファイル |

- 初期Web管理画面は **Windows PCブラウザ向け**、ログインなし・`127.0.0.1`バインドのみ。動画再生やライブプレビューは設けない。
- `base.html` でナビ・共通スタイルを一元管理する。主要ナビは運用・データ5項目（ダッシュボード／配信者一覧／録画履歴／コメント／音声字幕）、管理3項目（システム状態／ログ／設定）の順。物理配置は採用デザインに従う。
- `/logs` は独立したHTML画面として提供する。ログ読取はサーバー側に限定し、必要な検索・絞り込みも取得可能な情報の範囲で行う。ログ閲覧だけのための新たな外部公開APIや保存基盤を必須にしない。
- 最初は `pages.py` と `api.py` を少数ファイルで維持する。肥大化した際は `web/pages/` と `web/api/v1/` に機能別ルーターを移す（HTTP URLは変更しない）。
- HTMX用の部分更新は専用エンドポイントを設けてもよいが、初期に不要なら `partials.py` は作らない。
- JavaScriptを必要最小限とし、CSSとHTMLはJinja2で管理する。Node.js・React・Vueなどの別ビルド環境は初期に導入しない。
- 将来のトレイ・デスクトップアプリは `/api/v1` を利用する。`web/pages.py` やSQLiteに直接依存させない。
- 認証なしの変更系操作はCSRF・Origin/Host検証を行い、LAN・インターネット公開しない。要求§13.7の隔離非ブラウザ経路のみ限定例外とする。別端末接続を導入する場合は認証設計を追加する。

### 10.1.1.3 依存方向と責務の原則

```text
Jinja2/HTMX Web ──┐
JSON REST API ────┼──> services/ ──> monitoring/ ──> platforms/
CLI ──────────────┘         ├──> recording/ ───> storage.py
                            ├──> comments/ ────> storage.py
                            └──> db/

将来: Windows tray/desktop ──HTTP API──> services/ (同じ処理)
将来: postprocess/ ──> 保存済み録画・セグメント・コメント
将来: notifications/ <── system_events
```

- `services/sessions.py` は1配信全体の状態、`services/jobs.py` は録画・コメントジョブの起動停止を調整する。
- `recording/manager.py` は実際の録画子プロセスの管理、`recording/segments.py` は保存ファイルの検証・確定を担当する。
- `monitoring/scheduler.py` はTikTok最大10回／分、ツイキャス最大60回／分（初期値10回／分）の**サービス別・設定可能な監視頻度**を制御する。公式APIの実際の制限単位を確認し、共有枠があればコメント取得を優先して監視を減速する。
- `platforms/` は外部通信・認証・レスポンス正規化を担当。ツイキャスの匿名番号WebSocketは、実証後に `twitcasting.py` から `platforms/twitcasting/anonymous_ws.py` に分離してよい。
- `storage.py` が録画フォルダ・安全なファイル名・セグメントと全字幕のパス対応を一元管理し、Web・録画・コメント・音声・ASS処理が各自でパスを組み立てない。
- `db/` は永続化層であり、Web/WindowsクライアントがSQLiteを直接書き換えない。
- 複数workerを持つUvicorn設定は、監視ループの多重起動を招くため初期は禁止する。開発時のリロード機構も監視ジョブの重複起動を防ぐ。

### 10.1.1.4 Windowsへの録画保存規則（既存確定仕様を維持）

```text
D:\LiveRecorder\
└── streamer01\
    └── 2026-11-23\
        ├── tiktok_streamer01_20261123_210000_夜の雑談_s001.mp4
        ├── tiktok_streamer01_20261123_210000_夜の雑談_s001_comment.srt
        ├── tiktok_streamer01_20261123_210000_夜の雑談_s001_speech.srt
        ├── tiktok_streamer01_20261123_210000_夜の雑談_s001.ass
        ├── tiktok_streamer01_20261123_210000_夜の雑談_s002.mp4
        ├── tiktok_streamer01_20261123_210000_夜の雑談_s002_comment.srt
        ├── tiktok_streamer01_20261123_210000_夜の雑談_s002_speech.srt
        ├── tiktok_streamer01_20261123_210000_夜の雑談_s002.ass
        ├── twitcasting_streamer01_20261123_220000_作業配信_s001.mp4
        ├── twitcasting_streamer01_20261123_220000_作業配信_s001_comment.srt
        ├── twitcasting_streamer01_20261123_220000_作業配信_s001_speech.srt
        └── twitcasting_streamer01_20261123_220000_作業配信_s001.ass
```

- 同じ正規化済み配信者IDはサービス横断で**同一配信者フォルダ**。サービス名は**ファイル名**に残す。DBのアカウント・配信は混同しない。
- 日付は設定IANAタイムゾーンの配信開始日。配信開始日時が無いときは検知日。同一配信のセグメントは同じ日付フォルダに置く。動画と全字幕は同じフォルダに置き、命名は要求§10.3に従う。
- WindowsドライブはWSL2から 既定 `/mnt/d/LiveRecorder`（変更可能）でアクセスし、実効保存ルートが既存アプリ／別インスタンスと衝突しないことを起動前に照合する。パスの正規化・予約名・衝突を `storage.py` が処理する。
- SQLiteはWSL2 ext4内に置き、動画・SRTはWindows側に保存する。音声SRTと統合ASSも同一セグメントに関連付ける。命名は要求§10.3に従う。
- SQLite・ログ・実行時情報は**通常運用では10.1節のリポジトリ外のディレクトリを既定値**とする。開発・別worktreeの同時起動時にはDB・ログ・endpoint.jsonに加え、録画動画・SRTの保存ルートも分離し、同一DBの共有・ロック競合や録画ファイルの上書きを防ぐ。

### 10.1.1.5 将来拡張の配置先と着手条件

| 将来機能               | 予定ディレクトリ                | 導入の条件・方針                             |
| ---------------------- | ------------------------------- | -------------------------------------------- |
| YouTube Live           | `platforms/youtube.py`          | YouTube公式API・コメント購読の実証後         |
| ニコニコ生放送         | `platforms/niconico.py`         | 配信取得・コメント取得方式の実証後           |
| Twitch                 | `platforms/twitch.py`           | 配信状態・イベント購読方式の実証後           |
| SHOWROOM               | `platforms/showroom.py`         | 利用許諾と必要なアクセス方式の検証後         |
| 背景音・BGM低減        | `postprocess/audio_cleanup.py`  | モデル・音源分離方式を選定後                 |
| 話者分離               | `postprocess/diarization.py`    | 区間・精度の評価方法確定後                   |
| ターゲット話者         | `postprocess/target_speaker.py` | 音声・映像照合と不確実性の扱いを実証後       |
| Windows・外部通知      | `notifications/`                | 送信イベント・配送先の実装が必要になったら   |
| トレイアプリ           | `clients/windows-tray/`         | Windows側UIが必要になったら。REST API使用    |
| デスクトップ管理アプリ | `clients/windows-desktop/`      | ネイティブ画面が必要になったら。REST API使用 |

- Windowsクライアントの言語・UIフレームワークは**未確定**とする。技術選定だけのための依存関係追加は行わない。
- Windowsクライアントは既定の固定ポートで接続し、設定変更後の実行URLはWSL2のCLI/`endpoint.json` と生存確認から取得できる。
- 重いAI後処理は録画・コメントのリアルタイム処理に影響させない。必要に応じて別プロセスへ分離しても、ジョブ/セグメント/DBの整合性を維持する。
- 将来機能のための空ディレクトリ・未使用抽象クラス・仮実装・GPU依存を先行して作らない。

### 10.1.1.6 実装・拡張の順序

`prompts/implement.md` §3 を正とする。最小隔離・SQLite・ログからTikTok垂直スライスを先に通す。全設定、詳細診断・削除台帳・完全復旧基盤は後から拡充し、安全性未整備の操作は有効化しない。初期計画直後に隔離UI比較3案を提示し、選択待ちは独立実装を続ける。

### 10.1.1.7 ファイル作成・変更の運用ルール

- `src/live_rec_lab/web/` にUIを集める。`templates/` と `static/` のHTML/CSS/JSを `src/` 直下や別の `frontend/` に複製しない。
- Pythonモジュール名・ディレクトリ役割は本書を目安としつつ、使われないファイルは作らない。規模が増えたらファイルを分割し、対応表とテストを更新する。
- 静的資産をPythonパッケージとして配布する場合、Jinja2テンプレート・CSS・JSがパッケージへ含まれるようビルド設定を確認する。
- `.env`、`.venv`、SQLite実体、ログ、録画動画、実行時情報、AIモデルキャッシュはGitに追加しない。
- 要求仕様の既存詳細要件を本節の構成記述だけで上書き・削除しない。あくまで構成・責務・拡張先を具体化するものとする。

## 11. コンポーネント別横断設計

以下はコンポーネントごとの設計基準。要求値・画面要件・UI探索手順が登場する場合はそれぞれ要求仕様・UI仕様・実行プロンプトから導出した記述であり、独立した正本ではない。

### 01. システム基本設計 — 構成・責務・依存関係

> 出典：[`docs/requirements.md`](requirements.md) §1–3、§10.1.1。**設計ドラフト**。

#### 1. システム境界

- 個人用・ローカル実行のライブ配信記録システム。初期の対象サービスはTikTok LIVEとツイキャス。
- Windows 11の**WSL2 Ubuntu 24.04**内で1つのPythonアプリプロセスを常駐させ、WindowsのPCブラウザからlocalhostで操作する。
- UIは主要8画面の管理機能で構成する。システム状態は健康状態と診断要約、ログは既存の運用記録の詳細調査を担う。動画・ライブ映像のWeb再生、公開配信、ログイン、外部向けマルチユーザー運用は対象外。
- ネットワークは初期 `127.0.0.1` のみ。ブラウザは設定された固定ポートへ接続する。
- 録画とコメント取得は**同一配信セッションに紐付くが、独立したジョブ**。どちらかの失敗がもう一方を停止させない。

#### 2. 実行時構成

```text
Windows 11
  ├─ PC browser ── localhost ──┐
  └─ recording volume (NTFS)  │   /mnt/<drive>/LiveRecorder/
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
  ├─ postprocess/ ── queue ── managed inference child + ASS ── Windows volume
  ├─ db/ ── SQLite WAL (WSL2 ext4 filesystem)
  └─ ops/ ── logs, endpoint.json, restart reconciliation, online DB backup
```

**採用技術：** CPython 3.12、uv、FastAPI/Uvicorn、Jinja2/HTMX/軽量JS、SQLite WAL、SQLAlchemy 2.x、Alembic、asyncio、Streamlink / yt-dlp / FFmpeg / ffprobe、faster-whisper（CTranslate2）、pytest・pytest-xdist（開発依存）。

#### 3. プロセスとライフサイクル

- 初期版は**1 管理Pythonプロセス + 1 Uvicorn worker**。API・UI・監視・ジョブ統括を同居させる。録画エンジンと音声推論は管理対象の子プロセス。常駐管理プロセスは1つ。
- 起動時には多重起動防止、設定・SQLite・保存先・依存ツールの診断、未完了ジョブの復旧照合を行う。録画と検証に使用する `ffmpeg`・`ffprobe` は**実際の常駐プロセス環境から解決される実行ファイル**とバージョン・コーデック対応を確認する。対話シェルの `PATH` だけを根拠に正常判定しない。
- 要求仕様§2.2に従いDB設定または既定の固定ポートへ直接バインドし、確保したソケットを閉じずにサーバーへ渡す。競合時は起動を中止し、ジョブは開始せず、自動代替をしない。停止状態のCLI `config get web_port` で設定を確認し、`config set web_port PORT` で検証済み設定を保存できるようにし、実行中は設定画面を使用する。endpoint情報はPID・起動時刻・instance ID付きで原子的に保存し、生存確認なしに古いURLを案内しない。
- `uvicorn --reload` と複数workerによる監視の二重起動を避ける。
- 終了時は新規受付停止 → ジョブ停止通知 → 録画子プロセス安全終了・回収と有意な一時ファイルの保全 → コメント受信タスク終了・キュー残件保存 → 状態記録 → 制限時間内に終了。タイムアウトや内部取消を利用者の手動停止と混同しない。
- `systemd` のアプリ再起動機能と、WSL2インスタンス自体の起動維持は**別問題**。Windowsサインイン後の起動と復帰照合を設計対象にする。

#### 4. コンポーネント責務

| 部品（`src/live_rec_lab/`）                                                                    | 責務／境界                                                                                                                                                                                                                                                                                                                                                                                |
| ---------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cli.py`、`__main__.py`                                                                        | `start`／`status`／`url`／`backup`／`stop`。起動・停止とバックアップを扱い、画面の業務操作は行わない                                                                                                                                                                                                                                                                                      |
| `app.py`                                                                                       | FastAPIの組立、ルータ登録、lifespan。ビジネスロジックを置かない                                                                                                                                                                                                                                                                                                                           |
| `web/pages.py`、`web/api.py`、`web/security.py`                                                | HTML、REST `/api/v1`、変更時のHost・Origin・CSRF。HTML断片は `web/templates/partials/`。DBへ直接業務操作を書かない                                                                                                                                                                                                                                                                        |
| `services/sessions.py`、`services/jobs.py`、`services/retention.py`、`services/media_files.py` | セッションの一意性、ジョブの生成・停止・連携、期限削除、管理ファイルの照合                                                                                                                                                                                                                                                                                                                |
| `platforms/`                                                                                   | サービス固有の認証・通信・イベント正規化                                                                                                                                                                                                                                                                                                                                                  |
| `monitoring/`                                                                                  | 独立したサービス別監視枠、巡回公平性、LIVE検出。HTTP／通信失敗と明示的OFFLINEを識別し、終了猶予の根拠を管理                                                                                                                                                                                                                                                                               |
| `recording/`                                                                                   | 実績のある録画エンジン選択、子プロセス回収、再試行、確定後の実ファイル検証、セグメントの確定。ツイキャスは `hls_playlist.py` で初期HLSと404後の再解決を扱い、配信継続中は同じジョブで再接続する                                                                                                                                                                                           |
| `postprocess/`                                                                                 | 音声・ASSキュー、推論子、結果の公開・再生成・復旧（§14）                                                                                                                                                                                                                                                                                                                                  |
| `comments/`                                                                                    | 受信タスクの寿命・例外・停止処理、収集・重複排除・永続化、録画セグメント別SRT出力／字幕対象なしのスキップ                                                                                                                                                                                                                                                                                 |
| `storage.py`                                                                                   | Windows互換ファイル名、保存ルート、既存アプリとの実効ルート衝突検知、配信者ID→フォルダ安全名のサービス非依存マッピング、衝突防止（初回衝突時のセッション固定識別子・上書き禁止の排他確定）、パス対応を一元化。同一配信のファイル名はR §10.3の固定日時・安全化済みタイトルを共有し、**保持する**セグメントの番号をジョブをまたいでセッション単位で進める。空の破棄試行では連番を消費しない |
| `db/`、`ops/`                                                                                  | DBモデル・マイグレーション、コメント期限削除（コメントSRT・ASS先行照合・操作台帳・再開）、既存構造化ログの検索／段階的読取、復旧、バックアップ                                                                                                                                                                                                                                            |

パッケージ名は `src/live_rec_lab/`。Web も `src/live_rec_lab/web/` に集約する（`docs/requirements.md` §10.1.1.1、§10.1.1.2、§10.1.1.7、現行リポジトリ）。

#### 5. 依存方向

```text
Web (Jinja2/HTMX) ───┐
REST API ────────────┼──> services/ ─┬─> monitoring/ ─> platforms/
CLI ────────────────┘                ├─> recording/ ──> storage.py
                                    ├─> comments/ ───> storage.py
                                    └─> db/
```

将来のWindowsトレイ・デスクトップクライアントは `/api/v1` を通す。直接SQLiteを変更しない。初期段階では使われないインターフェース、分散キュー、Docker構成を先に構築しない。

#### 6. 初期起動から録画への主要フロー

1. CLI起動 → 単一インスタンス確認 → DBと設定の初期化／検証。
2. 以前の未完了セッション・セグメント・ファイルを照合し、未確定を誤って完成扱いにしない。
3. 監視対象を読み込み、サービス別スケジューラを起動。
4. `LIVE`検出 → 配信IDの照合 → セッションの重複防止・登録。進行中の配信に対する `UNKNOWN`／`ERROR` は明示的な終了証拠ではない。
5. 監視・録画・コメントの有効条件（全体コメント設定と配信者別設定はAND）と手動停止指示を判定し、録画／コメントジョブを**別々に**開始。稼働中の設定OFFは影響するジョブを安全に停止し、他方のジョブは可能な限り維持する。
6. 取得内容とジョブ状態を保存し、Web UIへ表示。
7. 保持する**最終ファイル**の映像・音声・長さ・実在を検証してセグメントを確定し、対象区間に字幕コメントがあるときだけSRTを生成。対象なしはスキップとする。
8. セッション概要は最新ジョブだけで作らず、**全録画ジョブ・保持セグメント・コメントジョブ・欠損記録**を照合する。

#### 7. コンポーネントをまたぐ不変条件

- **配信状態・ジョブ状態・セグメント検証・セッション全体の取得品質を分ける。** 最新ジョブ `COMPLETED` は過去の `FAILED`、`invalid`／`pending`、記録済み欠損を打ち消さない。集計根拠が不足した場合は「完全成功」にせず未判定とする。
- `ENDING` は明示的終了根拠に基づく猶予であり録画停止命令ではない。`UNKNOWN`／`ERROR` だけで終了を確定しない。監視不能かつ取得ジョブ終了が継続した場合は期限付きで再照合し、確認できなければ「終了未確認」として収束・欠損を残す。監視OFFは明示OFFLINEではなく、その配信者の稼働ジョブを安全に停止する。
- 録画エンジンの起動成功、ファイルの非0バイト、コンテナのduration、映像コーデックの判明、実際の本編・全区間の収録は別の確認段階。検証段階を飛ばして成果物を成功扱いにしない。
- コマンド・受信タスク・ファイル・DB・ログの各境界で、処理IDと発生時刻を連携する。ただし確認できない相関情報をログ本文から推定生成しない。

#### 8. 将来の拡張境界

高度な音源分離・話者識別等、通知、他の配信サービス、ネイティブクライアントは将来機能。初期段階で依存関係や空パッケージを増やさない。新サービス追加時は必要最低限のアダプタ契約を検証してから導入する。

### 02. プラットフォーム連携・監視設計

> 出典：[`docs/requirements.md`](requirements.md) §4–5、§10.1.1.3。**設計ドラフト**。

#### 1. プラットフォームアダプタ

各アダプタが外部サービス固有の認証、アカウントID正規化、LIVE状態・配信ID取得、コメント受信、録画元の解決を担当する。`normalize_identity`、`check_live`、`collect_comments`、`resolve_recording_source`、`capabilities` は候補であり、必要になった契約だけ実装する。対応外の機能は「取得不可／未対応」として返す。

| 項目     | TikTok LIVE                                           | ツイキャス                                                       |
| -------- | ----------------------------------------------------- | ---------------------------------------------------------------- |
| 登録     | ID、`@username`、プロフィールURLを正規化              | ユーザーID                                                       |
| 配信ID   | Room ID（取れた場合）                                 | Movie ID                                                         |
| LIVE検出 | TikTokLiveを優先、実APIを実証                         | 公式API `GET /users/:user_id/current_live` を検証                |
| コメント | TikTokLive CommentEvent、Euler Stream無料枠依存の実証 | 公式API `GET /movies/:movie_id/comments` + `slice_id`等を実証    |
| 録画     | Streamlink→yt-dlp→正当なFFmpeg連携を候補              | 取得可能なHLS等の仕様・権限を実証                                |
| 特記事項 | 非公式実装・署名・無償利用枠の変更に備える            | Client ID/SecretのHTTP Basic。匿名番号は視聴用WSの補助取得を実証 |

両サービスとも公開配信・録画許可範囲に限定。認証、DRM、CAPTCHA、地域制限等の回避は設計しない。動作・利用条件を未検証のまま保証しない。

#### 2. 監視対象の識別

- 内部の配信者アカウント一意キーは `(platform, normalized_platform_user_id)`。
- 同じ文字列IDを異なるプラットフォームで使う場合も、DBレコード・セッション・コメントは統合しない。
- 同一アカウントの重複登録を拒否。URLとIDは入力時に検証・正規化し、外部コマンドへ無検証の任意URLを渡さない。
- 配信IDが得られるときはセッションの一意性に使う。得られない場合の推定再接続は慎重に扱い、短い `UNKNOWN` / `OFFLINE` だけで新セッションを作らない。

#### 3. 監視頻度の設計

**独立したサービス別の配信状態確認枠**を維持し、配信者ごとの独立枠とはしない。

| サービス    |   初期値 | ユーザー設定の範囲    | 初期サービス内上限 |
| ----------- | -------: | --------------------- | -----------------: |
| TikTok LIVE | 10回／分 | `0 < r <= 10`、小数可 |           10回／分 |
| ツイキャス  | 10回／分 | `0 < r <= 60`、小数可 |           60回／分 |

- 2サービス合計の**理論最大監視回数は70回／分**。これは実効レートの保証ではない。
- 確認回数の上限は1サービスの全配信者で共有し、対象者の増加で同サービス内の1人あたり確認間隔が伸びる。他サービスの監視枠を削らない。
- 確認タスクは配信者ごとである。個別の失敗・一時停止・応答待ちは他の人の確認や独立した録画を止めない。サービス全体の明示的な制限は要求§4.1.2・§5.2に従う。
- 定期確認・手動確認・再試行・開始／終了再確認も、該当する監視枠に算入する。
- 監視頻度はSQLiteに永続化し、変更後は再起動なしで次回の巡回から反映する。監視停止は対象者別ON/OFFで扱い、`r=0` を許さない。
- 均等・公平な巡回、タイムアウト、指数バックオフ、ジッター、障害対象の隔離、最終確認時刻の保存を必要とする。HTTP 403／429等の制限とタイムアウト時の待機は、該当対象の障害として追跡し、無関係な監視・録画・コメントジョブを巻き込まない。**同じ警告の重複抑止と障害の回復は別**であり、継続時間・最終発生・回復を保持する。

**ツイキャスAPI枠の優先順位：** ①配信中コメント増分取得 → ②録画復旧の配信確認・HLS情報 → ③定期監視 → ④手動更新。応答のレート制限情報に基づき、共有制限が判明したら統合的に枠を管理する。録画通信やコメント取得をアプリ独自の「配信状態監視枠」に自動算入しないが、実際のAPI側共有枠を無視してはならない。

#### 4. 状態と遷移の原則

| 監視状態   | 意味                        |
| ---------- | --------------------------- |
| `DISABLED` | 監視設定がOFF               |
| `OFFLINE`  | 有効な確認結果で非配信      |
| `LIVE`     | 有効な確認結果で配信中      |
| `UNKNOWN`  | 情報不足で配信中か判断不能  |
| `ERROR`    | 認証・通信・API等で確認失敗 |

`UNKNOWN`、`ERROR`を `OFFLINE` と表示しない。各状態には最終確認日時と最後の正常確認日時を併記できる設計にする。**進行中の配信で単発・継続の `UNKNOWN`／`ERROR` のみを理由に `ENDING`／確認済み `ENDED` へ遷移しない。** 明示的な終了根拠から `ENDING` と猶予を開始し、猶予中も録画・コメントを継続。`LIVE` 再確認時は同セッションに復帰し、猶予満了時も終了根拠を再確認する。監視不明と録画・コメント双方の終了が続く孤立セッションは、期限付き再照合の後も根拠がなければ終了未確認として収束し、最終LIVE確認時刻・未確認理由・欠損を残す。配信IDを再検知したら同一セッションに結び付ける。猶予・再照合期限はfeatureで定義して検証する。

TikTok の `liveRoom.status` は、`2` を配信中、`4` を終了とする。`1` と `3` は意味を確認できるまで `UNKNOWN` とし、開いている録画を終了にしない。取得失敗とそれ以外の値は `ERROR` または `UNKNOWN` とし、終了と混同しない。解釈できない応答では次の確認を止めず、`4` を見逃さない。HTTPステータスを本文より先に判定し、403／429／5xx、空本文／HTML本文、不正JSON、DNS・接続・読取タイムアウトを取得できた根拠に応じて分類する。確認失敗を `OFFLINE` に丸めない。

#### 5. 配信検知からセッション作成

```text
rate-limited check_live
  → LIVEと配信IDの取得
  → (platform, stream ID, streamer)による既存セッション確認
  → なければstream_sessions作成
  → recording_enabled ? 録画ジョブを起動 : スキップ
  → comments_enabled ? コメントジョブを起動 : スキップ
  → 個別ジョブ状態を永続化、画面に反映
```

配信開始を検知しても、監視設定・録画設定・コメント設定の3つを混同しない。**監視OFFは当該配信者の監視と稼働中両ジョブを安全に止めるが、明示的な配信終了とはしない**。録画OFFは録画のみ、コメントOFFはコメントのみ停止。コメント収集は全体設定と配信者設定のAND条件。ONに戻したら同一配信内の重複を防いで新ジョブを起動できるが、録画の手動停止指示があれば優先する。停止理由は設定OFF／手動／配信終了／内部取消を区別。一方の障害が他方を停止させない。

#### 6. 外部連携の機能別検証

要求§4の利用条件・優先候補を守り、有望な方式から実装する。採用経路の実証は機能ごとの利用可否を判定する。未実証ならUNKNOWN／保留を明示し、ローカルfixtureによる検証と独立機能の実装を継続する。代替候補は主経路が不適合の場合に評価する。

#### 7. SDK受信とHLSの境界条件

##### TikTokのイベント受信

- SDKの `start()`／`connect()` の戻り値と、実際のWebSocket受信タスクの寿命は採用バージョンの実装で確かめる。接続開始処理が戻ったことを受信完了と扱わない。
- 遅延コメントを取りこぼさないよう受信タスクを監視・待機し、内部例外を収集ジョブの終了理由へ伝播させる。終了時は受信タスクを止め、キュー残件を保存し、接続資源を解放する。

##### ツイキャスのHLS

- 公式APIが返す権限のある `hls_url` を優先し、URL解決と実際の録画入力方式をローカル・実配信で別々に検証する。アクセス制限の回避や、任意の外部URLへの無制限リクエストは行わない。
- 明示キューの対、または先頭のプリロール用メディア／初期化URIと本編のメディア／初期化情報への切り替わりを組み合わせて判定する。不連続タグの省略時にも使えるようにし、共有する初期化情報や暗黙のバイト範囲に依存する本編を保全する。本編用の入力を待つ間はプレイリストを更新する。
- 初期化情報の切り替わりに合わせて必要な場合はFFmpeg入力を分け、同じ録画ジョブで後続を収録する。HLSだけで判別できない先頭候補は、[録画の確定処理](#7-セッション全体の取得結果と検証境界)で既知素材との照合に回す。
- 先頭の短いファイルが再生できることと、配信本編が録れたことは異なる。録画時間・冒頭・末尾・配信継続中の再接続を対象とした複数パターンの実配信検証が必要。

### 03. 録画・コメント収集・SRT設計

> 出典：[`docs/requirements.md`](requirements.md) §6–8、§17。**設計ドラフト**。

#### 1. ジョブ境界と不変条件

- 1回の配信は**1つの配信セッション**に属し、録画ジョブとコメント収集ジョブは独立に実行する。
- 録画失敗でもコメントは保存し、コメント取得失敗でも動画は保存する。
- セッションIDで両者を関連付け、動画が複数セグメントでもコメント原本はセッション単位でSQLiteに保持する。
- 未取得・未保存・未生成を成功扱いせず、開始遅延・通信断・欠損区間を記録する。

#### 2. 録画ジョブ

**想定状態：** `PENDING → STARTING → RECORDING → (RECONNECTING)* → FINALIZING → COMPLETED/PARTIAL/FAILED/CANCELLED`。これは代表的経路であり、完全な遷移表とガード条件は機能Specで定義する。

- 配信検知後に設定・同時録画数・保存先容量を確認し、録画エンジン子プロセスを安全な引数渡しで起動。
- 初期エンジン順は要求§11.8.1に従う。保存済みの優先順・失敗原因・追加試行予算を評価して許可された候補へ切り替え可能。エンジンとバージョン、失敗理由、切り替え履歴を記録。保護回避を目的とするフォールバックは禁止。
- 原則は利用可能な最高品質のstream copy／再エンコードしない録画。GPUエンコードは必須でない。
- 一時ファイルに録画、必要に応じてMP4へ無劣化リマックス、ffprobe等で検証して正式ファイルに確定。MP4化できない場合は再生可能と**検証できた**元形式だけを代替の成果物とし、未検証の一時データは回収対象として保全する。
- `ffmpeg`／`ffprobe` の実行ファイル・バージョン・実コーデック対応を、アプリ起動プロセスの環境から解決する。ファイルサイズやメディア長、音声だけで `valid` とせず、録画対象の**映像ストリームと識別可能な映像コーデック**を確かめ、0バイトMP4やリマックス失敗を成功にしない。DBの長さ・SRTの基準は**確定後に保持したファイル**から取り直す。
- 切断→指数バックオフ→配信終了の再確認→再接続。`UNKNOWN`／`ERROR` では終了を確定せず、`ENDING` の猶予中も録画を継続する。オフラインの終了根拠が確定し猶予を過ぎた場合は無用な再接続を止める。
- 再接続後は**新セグメント**として保存し、初期版では自動結合しない。同一配信のファイル名は配信開始日時（無いときは検知日時）の変換済み固定値と安全化済みタイトルを共有し、番号を**同一セッションの全ジョブを通じて単調に増やす**。保持しない空試行では番号を消費しないが、保全する無効・未確定ファイルや欠損・試行履歴を隠さない。過去の番号・ファイル名をUI都合で改名しない。各セグメントの録画開始時刻はDBに残す。
- 手動停止は同じ配信セッション内での自動再開を抑止し、手動再開では新セグメントを作る。次の別配信では通常の自動録画を許容。監視OFFで録画・コメント双方を、録画OFFで録画のみ、コメントOFFでコメントのみ安全に停止し、理由を区別する。各ON復帰では監視ON・LIVE確認・個別設定（コメントは全体設定とのAND）・手動停止指示を再評価してジョブ重複を防ぐ。
- 停止タイムアウト／内部タスク取消でも子プロセスを回収し、有意な一時データを可能な限り保全・確定する。利用者の手動停止ではない取消を安易に `CANCELLED` としない。再接続ではそのジョブで実際にデータを取得できたエンジンを優先候補として維持し、機械的な切替警告の連打を避ける。真の切替、プロセス終了コード、取得バイト数、秘匿済みstderrを追跡する。

**進捗監視と試行の記録（要求§6.5.1）：** 子プロセスごとに試行ID、入力エンジン、段階、開始・最終進捗の単調時計、取得量、終了区分を管理する。開始猶予、読取期限、無進捗期限は分離し、確定／停止中の処理を録画停滞と誤認しない。候補値としてHLSの本編待ち20秒、読取30秒、出力停滞45秒、短い取得の目安64KiBをローカルの評価に使用してよいが、採用値は入力特性・バッファ・低ビットレート・実メディア検証に基づいて確定する。取得量単独で切替や成功を決定しない。出力増加だけで本編の完全性を証明しない。開始失敗・中断・配信終了確認・明示停止・アクセス拒否を分類し、設定の再接続回数を守り、候補の切替も有限にする。プロセス終了後の最終検証とDB確定を通じて実際の媒体を保全する。

#### 3. コメント収集ジョブ

**想定状態：** `PENDING / CONNECTING / COLLECTING / RECONNECTING / COMPLETED / PARTIAL / FAILED / DISABLED`。録画とは別状態、別エラー情報を保持。

収集・保存する主な値：`platform_comment_id`（取得時のみ）、配信セッション、投稿者ID／表示名、本文、匿名フラグ・番号（確認できた場合のみ）、投稿時刻、受信時刻、保存時刻、ソース。UTF-8で日本語・絵文字・改行を保持。

- コメントIDが得られる場合は同一セッション内で重複排除。得られない場合は正当な連投を消さないよう慎重に判定する。
- 切断検知、指数バックオフ、再取得、受信・保存・失敗件数、最終受信時刻、欠損区間を記録。`PARTIAL` で終了しても失敗保存件数0のことがあるため、**終了理由・終了経路**を別に記録し、受信件数＝保存件数を「配信中の全コメント取得」と同一視しない。
- TikTokのSDKが返す受信タスクを実際の接続寿命に合わせて待ち、例外を収集ジョブへ伝える。停止時は接続を解放し、キュー内の受信済みコメントを可能な範囲で書き切る。未起動／接続失敗／収集できて0件は別状態として保存する。
- 大量コメントに対して有界キュー、短いバッチ書込、失敗時再試行を設計。処理遅延で監視ループを長く停止させない。
- ツイキャス匿名番号はAPIと補助WebSocketを一意照合できた場合だけ補完。曖昧ならNULL。

**接続世代・増分境界（要求§4.1.3、§4.2.2、§7.4）：** TikTokは接続試行に世代トークンと対象Room IDを割り当てる。保留イベントは世代単位の有界キューで扱い、一致が確認されてから保存キューへ渡す。失敗時の廃棄・超過・旧世代の到着を理由別に計数し、欠損を記録する。ツイキャスは同一Movie IDの確実な境界、取得中のページ位置、見たID、ページ進捗を区別する。公式のslice_id／offset等の実契約に合わせ、短いページ・件数差・削除・空ページ・反復を検査し、枠と時間・ページ上限内で追加取得する。永続境界は保存／重複確認済みの連続した取得範囲だけをトランザクション内で進める。失敗した未取得区間を越える場合は明示した欠損の記録と境界変更を同一コミットにし、未取得を正常にしない。IDが無いコメントは強い証拠がなければ統合せず、再送の疑いと真正な連投を分ける。

#### 4. 時刻と同期情報

| 値                                   | 意味                                     |
| ------------------------------------ | ---------------------------------------- |
| `posted_at`                          | プラットフォームの投稿時刻（取れた場合） |
| `received_at`                        | アプリでの受信時刻                       |
| `stream_offset_ms`                   | 配信開始からの経過時間（計算できる場合） |
| `recording_offset_ms`                | 録画開始からの経過時間（計算できる場合） |
| `first_media_at` / `media_start_pts` | 取得できる場合の最初のメディア時刻／PTS  |
| `sync_offset_ms` / `sync_method`     | 動画時間への補正値と算出根拠             |

内部絶対時刻はUTC。画面表示は設定されたIANAタイムゾーン（初期Asia/Tokyo）。SRTの相対タイムコードはタイムゾーン変換しない。**録画プロセスの起動時刻と動画の0秒は必ずしも一致しない**ため、実測・近似・手動補正・同期不能を区別する。記録できない精度を推測しない。確定前FLVや一時ファイルの長さが最終MP4と異なる場合、**実際に保持したファイルのメディア長**をDBと字幕切り詰めの根拠にする。

#### 5. 録画セグメント単位のSRT

- コメントの**正本はSQLite**。コメント字幕はSRTとして動画と同じフォルダに保存する。JSONL/CSV/TXTのエクスポートは設けない。
- 確定済み動画セグメントの**対象時間帯に字幕対象コメントがある場合**、同じ保存ディレクトリに要求§10.3のコメントSRTを1個自動生成する。再接続で複数セグメントなら個別生成。対象コメントが0件なら**空SRTを生成せず「字幕対象なし（スキップ）」**を記録する。コメント収集未実施・失敗による0件とは区別する。
- 録画ジョブの完了・手動停止・PARTIAL・CANCELLED時も、有効な動画セグメントに対して生成を試みる。
- 各動画のメディア開始を0秒として相対時刻を作る。`HH:MM:SS,mmm`、開始＜終了、連番1始まり、同時刻の安定順序、設定した表示秒数、末尾の打切りを守る。
- 投稿時刻がない場合の受信時刻使用、実測と近似の区別、時刻補正、生成日時、失敗理由、遅延未保存の可能性を記録。
- 動画がない／時刻が算出不能ならSRTを偽造せず、未生成・保留・失敗として表示する。**生成失敗で動画やSQLiteコメントを削除しない**。
- SRTの手動再生成は明示的操作とし、対象のセグメントと実際の結果を記録する。録画終了後もコメント収集が継続中／キュー未排出なら生成・0件スキップは**暫定**とする。対象セグメントのコメント／時刻補正に関する入力版数と生成に用いた版数を追跡し、後着・修正で不一致なら再同期待ちへ遷移。収集終了と書込完了後、版数を再確認して自動再同期／スキップ確定し、途中で版数が変われば再試行する。失敗時は最後の正常SRT、動画・コメント原本を保全し再同期失敗を表示。収集 `PARTIAL`／欠損ありなら全件性は保証しない。
- **競合対策：** セグメントごとに再生成を直列化し、開始時の `srt_input_version` で同一Windowsボリュームの一時SRTを作る。公開直前に版数・ファイル対応・コメント期限削除状態と進行中の削除操作を再照合し、不一致なら公開せず再試行待ちにする。対象セグメントが所有する既存SRTのみ安全に置換できる。公開後〜DB確定の間に版数が変われば現行版と偽らず「再同期待ち」として記録、再起動照合する。失敗時は最後の正常SRTを残す。0件への正常な更新では旧SRTを確実に除去してからスキップを確定する。期限削除済みコメントからSRTを再生成しない。

#### 6. 障害・回復の観点

録画・コメント・SRTのどこで問題が起きたかを独立に表示する。ファイル完成直前の強制終了、録画エンジン異常、API枠不足、コメント保存失敗、同期不能の各ケースは統合／障害テストを設定する。欠損区間を正常な成果物として表現しない。

#### 7. セッション全体の取得結果と検証境界

- 最新の録画ジョブが `COMPLETED` でも、**同一セッションの全ジョブ**に過去の失敗、`invalid`／`pending` の保持セグメント、`collection_gaps` があれば別途表示する。ジョブ終了状態からセッション全体の無欠損収録を導かない。
- セッション要約は、有効な保持ファイルの有無、未確定／無効ファイル、取得不能区間、独立したコメント収集状態を別の観点として集計する。詳細な「一部取得」等の確定ロジックは状態遷移表とともに機能Specで定義し、**必要な情報が欠ければ品質は未判定**にする。
- 録画成功の確認段階を、(1)実ファイル存在、(2)映像／音声ストリーム・コーデック・長さの整合、(3)実再生、(4)配信本編の冒頭／末尾・途中の連続性と分ける。ffprobeで形式が読めることだけで(3)(4)を証明しない。
- ツイキャスでは本編の収録を録画成功の条件とする。[HLSの境界判定](#ツイキャスのhls)で除外できなかった先頭候補は、既知素材の映像・音声のパケット列を照合する。同じ素材の繰り返し回数が異なる場合も、1周期単位で両ストリームの全内容が一致すればプリロールと判定する。
- 確認済みプリロールは採番と正式保存の前に除外し、確定したセグメント履歴やSRTに含めない。負のケースとして、同程度の長さの本編、通常の不連続、既知素材と本編が混在する録画の保全を確認する。本編の取得不能や中断は、プリロールを除外した経緯と区別して記録する。

### 04. データベース・ファイル保存設計

> 出典：[`docs/requirements.md`](requirements.md) §9–10、§15。**設計ドラフト。以下はDDL確定版ではない。**

#### 1. 永続化アーキテクチャ

SQLite 3（SQLAlchemy 2.x / Alembic）を**WSL2内のLinux ext4**に置き、WAL、`busy_timeout`、`foreign_keys=ON`、短いトランザクション、適切なインデックスを用いる。通常運用の動画・対応字幕はWindowsローカルドライブに保存。隔離は要求§15.2.1の専用Linux領域を使用する。NAS／DrvFS上にSQLiteの正本を置かない。

| 情報      | 既定の保存先例                                      | 主な用途                                   |
| --------- | --------------------------------------------------- | ------------------------------------------ |
| SQLite    | `~/.local/share/live-rec-lab/data`                  | 設定・配信者・セッション・ジョブ・コメント |
| ログ      | `~/.local/state/live-rec-lab/logs`                  | 障害調査と詳細ログ                         |
| 起動状態  | `~/.local/state/live-rec-lab/runtime/endpoint.json` | 実効固定ポート、PID、インスタンス識別      |
| 動画・SRT | `/mnt/d/LiveRecorder/`                              | Windows側の既定録画保管（設定変更可）      |

別worktreeからの同時試験実行時は**DB、ログ、endpoint.jsonに加え、録画動画・全対応字幕保存ルートもインスタンス別に分離**（隔離のLinux領域は要求§15.2.1）して競合・上書きを避ける。DB永続設定を反映した実効録画ルートと、運用者が確認した他の録画アプリケーション／別インスタンスの実効ルートをWindows実体パスで照合し、同一・親子衝突なら録画開始を拒否する。自動取得できない既存アプリの設定は導入時に手動照合する。SQLiteバックアップにはOnline Backup API等を使い、稼働中DBファイルだけを直接コピーしない。

#### 2. 初期エンティティ候補

| テーブル                          | 責務                                                                                                   | 主要関連                                              |
| --------------------------------- | ------------------------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| `streamers`                       | サービス別監視アカウント、3種ON/OFF、最終監視                                                          | `(platform, platform_user_id)`一意                    |
| `stream_sessions`                 | 1回の配信、配信ID、期間、状態、`next_segment_number`、期限削除状態、固定命名日時・タイトル、衝突識別子 | `streamer_id`                                         |
| `recording_jobs`                  | 録画実行単位、エンジン、停止指示、再試行                                                               | `stream_session_id`                                   |
| `speech_jobs` / `speech_attempts` | 音声キュー・試行・正常結果版                                                                           | DM-15（セグメントFK）                                 |
| `ass_artifacts` / 字幕公開台帳    | ASS入力版・生成結果・公開復旧                                                                          | DM-16〜17                                             |
| `recording_segments`              | 分割動画、物理ファイル、検証状態、同期、SRT                                                            | `recording_job_id`＋`stream_session_id`（親整合検証） |
| `comment_collection_jobs`         | コメント収集実行、再接続、件数、失敗                                                                   | `stream_session_id`                                   |
| `comments`                        | 元コメント、識別・時刻・ソース                                                                         | `stream_session_id`                                   |
| `collection_gaps`                 | 録画／コメント取得の欠損期間と理由                                                                     | `stream_session_id`                                   |
| `system_settings`                 | 監視頻度などの通常設定                                                                                 | `key`                                                 |
| `runtime_instances`               | 起動インスタンスの生存管理（必要なら）                                                                 | `instance_id`                                         |
| `system_events`                   | 検索可能な警告・障害イベントの要約                                                                     | 関連セッション等                                      |
| `deletion_operations`             | 削除開始前の台帳、対象・方式・進捗・失敗・再開                                                         | 削除後にも残る対象ID                                  |

重要制約：`streamers`のサービス＋正規化IDは一意。`archived_at` 設定済み配信者の同じキーによる再登録は既存行を復活させ、過去履歴を保持し、3種のON/OFFは新しい登録操作の明示値で再設定する。配信ID取得時は同一配信の二重セッション生成を防止。コメントID取得時は同セッション内 `(stream_session_id, platform_comment_id)` を一意化する。セッション削除時のDB関連レコードとWindowsファイルの扱いは詳細設計で確定する。秘匿情報を `system_settings` に通常文字列として不用意に入れない。

**録画セグメントの整合性**：`recording_segments` に `stream_session_id` を保持し、`recording_jobs` の `(id, stream_session_id)` への複合外部キー（親側複合一意キー付き）で親ジョブとセッションの一致を保証する。**`UNIQUE(stream_session_id, segment_number)`** により全録画ジョブ横断の保持番号を一意にする（未採番行はNULL）。未確定レコードは仮ID／一時パスで追跡し、空試行は番号を消費しない。実データの保持判断後、同一SQLite書込トランザクションで `stream_sessions.next_segment_number` を排他的に進めて番号・正式パスを予約する。予約後のクラッシュ・削除・確定失敗でも番号を再利用しない。renameは同一Windowsボリューム内で上書き禁止とし、再起動時は予約・一時ファイル・正式ファイル・DBを照合する。保持した `invalid`／`pending` や過去の番号を隠さず、詳細DDLと復旧状態はfeatureの `data-model.md`／migrationに落とす。

**実体との照合**：DBの `valid`、`duration_ms`、ファイル名は単独では実ファイルの存在・再生可能性を保証しない。動画の最終実体の映像・音声ストリーム、コーデック、長さ、サイズ、検証日時と状態、古いエラー／欠損の記録を対応づける。既存DB上の誤判定を無検証で一括書換えしない。

#### 3. 動画とSRTのフォルダ設計

```text
D:\LiveRecorder\
└── streamer01\
    └── 2026-11-23\
        ├── tiktok_streamer01_20261123_210000_夜の雑談_s001.mp4
        ├── tiktok_streamer01_20261123_210000_夜の雑談_s001_comment.srt
        ├── tiktok_streamer01_20261123_210000_夜の雑談_s001_speech.srt
        ├── tiktok_streamer01_20261123_210000_夜の雑談_s001.ass
        ├── tiktok_streamer01_20261123_210000_夜の雑談_s002.mp4
        ├── tiktok_streamer01_20261123_210000_夜の雑談_s002_comment.srt
        ├── tiktok_streamer01_20261123_210000_夜の雑談_s002_speech.srt
        ├── tiktok_streamer01_20261123_210000_夜の雑談_s002.ass
        ├── twitcasting_streamer01_20261123_220000_作業配信_s001.mp4
        ├── twitcasting_streamer01_20261123_220000_作業配信_s001_comment.srt
        ├── twitcasting_streamer01_20261123_220000_作業配信_s001_speech.srt
        └── twitcasting_streamer01_20261123_220000_作業配信_s001.ass
```

- 第一階層は原則**正規化済み配信者IDのみ**（異なるサービスでも正規化済みIDが同じなら同一フォルダ）。Windows上で危険／表現不能な文字列や異なるID同士の安全名衝突があるときのみ、元の正規化済みID由来の短い識別子を付加する。**ID→実ディレクトリ名の対応はプラットフォーム非依存の永続マッピングで固定**し、後から衝突が見つかっても既存ディレクトリは改名しない。管理外ディレクトリを自動的に所有物と見なさない。**DB上でアカウントは統合しない**。
- 第二階層は配信開始日の `YYYY-MM-DD`（設定タイムゾーン）。配信開始時刻が無いときは検知日。同一配信のセグメントは同じ日付フォルダに置き、日をまたいでも分けない。第三階層に動画と対応するコメントSRT・音声SRT・統合ASSを保存。
- ファイル名はR §10.3に従い `{platform}_{streamer_id}_{YYYYMMDD_HHMMSS}_{title}_sNNN` の順序とする。最初の正式名予約時に `filename_datetime` と `filename_title` を採番・パス予約と同じトランザクションで固定し、後着情報・タイトル変更・設定変更・再起動後も再計算しない。同一配信では日時・タイトルを共有し、**全ジョブを通じた保持セグメントの番号**だけを進める。空の破棄試行を `sNNN` の採番対象としない。各ファイルの録画開始時刻はファイル名に使わない。別サービス・別配信の同名上書きを防ぐ。最初の正式名の衝突時のみセッションID由来の安全な短い識別子を番号の直前へ追加し、同一セッション内で永続固定する（追加衝突なら延長して初回予約、途中衝突は保留・照合）。排他的なファイル確定とWindows大文字小文字の同名判定を使う。表示名の変更でディレクトリを改名しない。すでに保存したファイルは改名しない。
- Windows禁止文字／予約名／大文字小文字・Unicode正規化／末尾空白・ピリオド／パストラバーサルと管理外ディレクトリへの衝突を `storage.py` で集中処理する。正規化済みIDの一意なマッピングとWindowsディレクトリ名の一意制約・既存ファイルとの照合は `storage.py` と 本書§16 に具体化する。ファイル名の `filename_disambiguator`（セッション単位）とは別の機構とする。各コンポーネントで独自にパスを結合しない。`storage.py` の共通パス解決で確定動画stemから要求§10.3の3種の字幕パスを導出し、R §10.3のタイトル安全化（NFC・禁止文字置換・空欄はuntitled・長さ制限）、接尾辞・拡張子を含む長さ、Windows同名性、全成果物の初回予約と管理パスを照合する。生成・再生成・照合・削除・復旧で同じ解決規則を使い、管理外の同名物は保護する。

#### 4. セグメントの安全な確定処理

```text
recording_segmentsに未確定レコードを先行作成（空試行の採番は保留）
    ↓
Windows録画先と同一ボリューム上の一意な一時ファイルに記録
    ↓
有意な保持対象と空の破棄試行を分け、後者は採番しない
    ↓
必要ならリマックスし、保持する最終形式をffprobe等で検証
    ↓
映像・音声・コーデック・長さ・サイズ・実在を確認
    ↓
同一セッションの保持番号と衝突時の固定識別子をDBで予約し、上書き禁止で同一ボリューム内の正式名へ確定
    ↓
SQLite上で最終ファイル・検証結果・実メディア長を確定
    ↓
対象コメントがあれば対応SRT生成、0件ならスキップ（収集未確定なら暫定）；入力版数と生成時版数を照合して再同期
  → セッション収束後に音声キュー投入、コメント／音声版更新に伴うASS生成（§14）
```

ファイルシステムのrenameとSQLiteコミットは完全には原子的にならない。強制終了後は一時ファイル・正式ファイル・DB状態を**双方向で照合**し、異常 `pending`、手動停止でない取消で残った一時ファイル、DBが `valid` でも0バイト／不在の正式ファイルを検出する。未登録ファイルを即削除しない。WSL2 ext4からNTFSへのクロスデバイスrenameに依存しない。 排他的確定の手段は要求§10.3に従って選定し、隔離Linuxで先行、実使用Windowsマウントの専用試験フォルダで非上書き・同名性・競合を実証してから通常媒体処理を有効化する。未実証時は保留する。**未確定レコードを先に作ること**と**空試行に番号を消費させないこと**の両立は、仮ID／仮パス等を使う詳細設計で実現する。

#### 5. 容量・整合性・削除

- 起動時／監視時に録画先のマウント状態、書き込み権限、空き容量を確認。利用できない場合に勝手にWSL2内へ保存しない。
- 保存容量上限、最低空き容量、録画件数・配信者別容量を管理。**自動削除は初期OFF**。有効化時は確定済みの古い動画＋全対応字幕の「ファイルのみ削除」に限定し、DBのコメント・履歴・欠損は保持。容量不足時は未確定データを消さず新規録画を抑止して警告。
- コメント保存期限は初期`0`（無期限）、正整数なら収束済み・収集／キュー書込完了・復旧照合済みセッションを`ended_at`起算で判定する。期限削除は動画を残し、削除操作開始後のコメント書込・SRT生成を排他停止してから、派生コメントSRT・統合ASSの削除を照合してSQLiteコメントを削除、`comments_purged_at`・`purged_comment_count`と`deletion_operations`の内部モードに記録する。途中失敗は冪等に再開し、未収集／0件と区別して表示する。期限削除済みコメントSRT・ASSは再生成不可、音声SRTは対象外。**手動SQLiteバックアップは削除対象外**であり、利用者が旧バックアップを安全に破棄・更新する。初期版に自動復元は設けず、手動復元後は保持期限・派生コメントSRT・ASSの再照合なしに再開しない。
- 手動削除は「ファイルのみ」または「ファイル＋履歴」の2種。履歴のみ削除は提供しない。ファイルのみは意図的削除の時刻とメタデータを維持（`validation_status` と別に記録）。両方の場合は動画／全対応字幕削除完了後にセッション配下のジョブ・コメント・欠損を参照整合性を保って削除する。`deletion_operations` に削除前から対象・方式・進捗を記録し、履歴削除後も操作台帳を保持する。配信者削除は履歴ありなら `archived_at` による論理削除とする。
- 進行中・未確定・照合中のセッションは削除不可。削除処理の未完了・一部失敗を永続化し、再起動時に再照合。許可済みルート外／シンボリックリンクをたどらず、一部成功を完了として扱わない。
- DB登録済みファイルの不在、0バイト、最終ファイルのサイズ・長さ・メディア検証の不一致、無効／未確定、一時ファイル、孤立ファイルを判別する。異常の回復・再検証は変更履歴付きで行い、過去の取得欠損を後続ジョブの成功によって削除しない。
- WSL2 `ext4.vhdx` のホスト側容量増加にも注意する。安全条件が明確でない自動修復・削除を行わない。

**競合とストレージ走査：** 同一セッション／セグメント／管理パスに共通の操作ガードを設ける。SQLiteの短い書込で対象操作を予約し、時間のかかるI/O中はDBトランザクションを保持しない。確定・SRT生成・削除は同一対象の予約を再検査し、順序を統一して循環待ちを防ぐ。台帳に各対象ファイルの完了を残し、再起動時は現在のルートと世代を再確認する。照合では確定済みと書込中を区別し、走査時点・検査不能・変更中を表示する。分類件数は表示するサンプルやページの件数と独立集計し、読取不能を正常に数えない。

#### 6. 詳細設計で確定する事項

型・NOT NULL・補助インデックス・ON DELETE方針の詳細、保存期間ポリシー、削除処理の再開状態、トランザクション境界、ファイル復旧状態の詳細は 本書§16 と `alembic/versions/` に具体化する。本書の列挙を完成したDDLとみなさない。サービス非依存の配信者IDからディレクトリ名への永続対応、セッション単位連番、空試行の回収、全ジョブと欠損の集計、SRTの版数照合、期限削除と削除台帳の意味は、そのデータモデルと現行コードを参照する。

### 05. Web/API・UI/UX基本設計

> 出典：[`docs/requirements.md`](requirements.md) §2–3、§10.1.1.2、§11、[`docs/ui-spec.md`](ui-spec.md) 全体。**UI詳細の正本は `docs/ui-spec.md`。**

#### 1. Web構成

初期のWebフロントエンドは**FastAPI + Jinja2 + HTMX + 軽量JavaScript**。React/Next.js/Vueの独立SPAやNodeビルドを必須にしない。`src/live_rec_lab/web/` 以下にテンプレート・CSS・JSを集約する。

- `app.py`：FastAPI構築・lifespan・ルータ登録。
- `web/pages.py`：ページHTMLを返す。部分更新は `web/templates/partials/` を返す。
- `web/api.py`：`/api/v1` のREST境界。契約は 本書§17〜18に定める。
- `services/`：UI、CLI、RESTの共通の業務操作。UI側がDBを直接変更しない。
- HTMXの更新は入力中のフォーム、フォーカス、スクロール、検索条件を不必要に破壊しない。

#### 2. 情報アーキテクチャ

主要ナビは**運用・データ5項目と管理3項目の計8項目**（配置はUI正本§3.4に従う）。セッション詳細、ログ詳細、追加・編集、設定内カテゴリは下位ビューとする。

| グループ     | URL           | ナビ表示名     | 主目的／重要な下位ビュー                                                             |
| ------------ | ------------- | -------------- | ------------------------------------------------------------------------------------ |
| 運用・データ | `/`           | ダッシュボード | 現在の配信・録画・コメント収集、直近記録・異常への導線                               |
| 運用・データ | `/streamers`  | 配信者一覧     | サービス別配信者、独立した監視・録画・コメント設定、追加・編集                       |
| 運用・データ | `/recordings` | 録画履歴       | 配信セッション単位の検索・詳細、セグメントとSRTの対応                                |
| 運用・データ | `/comments`   | コメント       | 配信別閲覧／横断検索、時刻昇降順、投稿者・日付などの絞り込み、関連セッションへの導線 |
| 運用・データ | `/speech`     | 音声字幕       | 文字起こし待ち行列・再生成・発話本文検索、ASS状態                                    |
| 管理         | `/system`     | システム状態   | プロセス・保存先・依存ツールの健康状態、障害の影響と診断要約                         |
| 管理         | `/logs`       | ログ           | 発生時刻の降順で検索・絞り込み、詳細の閲覧・コピー                                   |
| 管理         | `/settings`   | 設定           | 監視、録画・保存、コメント・SRT、音声字幕、表示、詳細設定                            |

ダッシュボード、配信者一覧、コメントから関連する録画履歴へ移動できる。録画履歴にはコメントのみ保存されたセッションも含め、録画の有無とコメント収集状態を独立して示す。録画セグメントのないセッションではコメント閲覧へ案内する。**一覧1行は1配信セッション**とし、セッションの録画結果は最新ジョブ単独ではなく全ジョブ・保持セグメント・欠損から要約する。

システム状態の警告から、確認済みの時刻・重要度・出力元・関連対象を引き継いでログを開く。ログから配信者一覧や録画履歴へ進むリンクは、実際に記録された識別子に基づく。画面を往復するときは検索条件とページ位置を可能な範囲で復元する。

#### 3. 情報の粒度と状態表示

- **配信者**（監視の設定単位）、**配信セッション**（1回のLIVE）、**録画ジョブ**、**録画セグメント**、**コメント収集ジョブ**、**コメント**を視覚的に区別。**「最新ジョブ完了」「有効ファイルあり」「セッション内に欠損あり」**は併存し得る別の事実として表す。
- 配信中＝緑、録画中＝赤、コメント収集中＝青。色だけで伝えず、状態文言・ピクトグラム等を併用。録画連動ロゴ／ファビコンと抑制可能な動きも現行UI仕様に含む。
- `UNKNOWN` ≠ `OFFLINE`、取得不能 ≠ `0件`、設定ON ≠ 録画中、処理受付 ≠ 完了、SRT未生成 ≠ 生成失敗、**字幕対象なし（スキップ）** ≠ コメント未収集、コメント確定待ちのSRT ≠ 確定済みSRT、最新状態 ≠ 古いキャッシュ。監視不明の表示では最終正常確認時刻と現在の確認時刻を混同しない。
- 最終取得日時と表示更新の鮮度を共通位置に出す。ログの発生時刻は別に表示する。再読込はブラウザ標準機能を使い、外部サービスへの追加監視を発生させない。部分更新では入力・選択・フォーカス・ログ詳細の閲覧を維持する。更新間隔は要求仕様§11.1の永続設定を参照し、HTMXまたはブラウザ側タイマーで必要な領域だけ更新する。保存成功後にタイマーを再設定し、旧タイマーを解除する。他の開いている画面も次回の設定取得時に反映する。処理中の更新要求を重ねず、非表示・操作中は延期し、再開時に連続した追い付き要求を送らない。読み取りは内部DB・状態・ログに限定する。
- テーブル中心の一覧と、詳細への段階的展開を優先。巨大なカード群や説明文の追加だけで情報設計の不足を補わない。
- 数値単位、タイムゾーン、欠損表示、未保存変更、確定していない操作結果を一貫して扱う。
- 録画履歴詳細では、保存されたセグメント番号を再採番せず、所属ジョブ、`valid`／`invalid`／`pending`、実ファイルの有無・サイズ・長さ、SRT生成／スキップ、関連欠損とログに段階的にアクセスできるようにする。
- `last_error` を現在の障害として断定せず、発生時刻・対象ジョブ・その後の回復を取得できる範囲で見せる。警告数の減少を復旧と表示しない。情報不足時は「未判定」「要確認」とする。

#### 4. UIデザインの決定と実装

`prompts/redesign-ui.md` を初期UIと単独リデザインに使用する。初期実装・単独リデザインともに実質的に異なる3案を提示し、利用者が採用案を明示選択する。選択前は採用案に依存する製品UI実装を保留し、独立作業は継続する。実配信や字幕の実証とUI実装を分離し、隔離fixtureで検証する。採用案と利用者の選択記録を保存する。

固定するのは**単一の明るいライト系テーマ**、フォントの「Windows標準（初期）」と「Noto Sans JP」の2択、配信中＝緑・録画中＝赤・コメント収集中＝青という意味と非色情報の併用です。**具体的なHEX値・シェル幅・余白・カード構成は固定しません。** ダークテーマ、テーマ切替、OSの配色への追従、別フォントの追加は行いません。これらを変更するときは `docs/requirements.md` と `docs/ui-spec.md` の事前改訂が必要です。Windows PCブラウザの1440px／1280px／1024px、拡大表示、キーボード操作、WCAG 2.2 AA目標を評価します。

完成後にUIだけを再デザインするときは、**録画・DB・API・セキュリティ等の動作を維持**し、表示系のコード・テスト・デザイン記録に作業範囲を限定します。

#### 5. 安全な操作とAPI境界

- トグル・保存・録画停止/再開・SRT再生成・削除は、処理中／受付済／成功／失敗／結果不明を区別し、二重操作を防ぐ。監視OFFで両ジョブ、録画OFFで録画のみ、コメントOFF（全体・配信者）でコメントのみを安全に停止する影響を操作前に表示。ON復帰と手動停止指示の優先を混同しない。
- 論理削除済みの同一サービス・正規化IDの再登録は新規追加ではなく再有効化と表示し、既存履歴を維持する。監視・録画・コメントON/OFFは再登録フォームの明示値で設定する。コメント期限削除済みセッションは0件／未収集と区別し、コメントSRT・ASS再生成を表示しない。音声SRT再生成は維持する。コメント期限設定の説明では、稼働DB／管理SRTからの削除と、古い手動SQLiteバックアップに残り得るデータを区別し、バックアップの手動破棄・更新を案内する。
- 破壊的操作は対象・影響範囲・復元可否を確認し、既定はキャンセル側に置く。初期削除は「ファイルのみ（動画＋全対応字幕、履歴保持）」と「ファイル＋履歴」に限定し、履歴のみ／進行中の削除は提供しない。未完了・一部失敗を明示する。
- ログインがなくても状態変更系リクエストにはCSRF・Origin/Host等の対策を適用し、GETで変更を実行しない。限定例外は要求§13.7に従う。
- 動画のブラウザ再生・ライブプレビュー・コメント同期再生・HLS配信は初期のUIに入れない。動画とSRTはWindows側の保存先で利用する。Web UIにダウンロード操作を設けず、APIでもファイル本体を返さない。
- `/logs` は読み取り専用のHTML画面とする。既存に読取実装がなくても、構造化ログ・ローテーション済みログ・利用可能なイベント要約の読取・サーバー側検索・ページング・詳細表示は初期実装必須とする。ログ専用の公開APIは必須としない。記録済みの本文・トレースは**古いログ／例外文も含めサーバー応答前に再マスキング**し、一覧・詳細・コピーのすべてで秘匿処理後の非秘匿本文を省略せず表示する。空、検索結果0件、取得不能、古い情報、取得可能期間の制約を区別する。システム状態のメディア診断では、実際のプロセスから解決した `ffmpeg`／`ffprobe` の実体・対応・検証時刻を表示可能な範囲で示し、試作環境の固定パスやバージョンを正常条件にしない。
- RESTと画面のHTTP契約は 本書§17〜18に定める。契約に無いファイル取得をUIから追加しない。

**定期更新の実装契約：** 各部分更新領域に要求世代、現在の検索／ページ／対象キー、処理中フラグを持たせる。送信直前・swap直前に利用者の操作状態、世代とキーを照合し、古い応答や操作を壊すswapを破棄する。設定保存・検索変更・画面遷移時に旧世代を無効化する。停止中のタイマーを積み上げず、操作終了または可視化後に現条件を1回取得し、設定間隔で再開する。成功した対象領域だけ取得時点を更新し、失敗時の最終成功値を保つ。DOM ID等でフォーカス・展開状態・スクロールを復元できる場合は復元し、対象が消えた場合は安全な位置へ戻す。Webからの照会はDB／保存済み状態の読取に限定する。

#### 6. UIの横断的な受け入れ観点

- 最新の録画ジョブだけ `COMPLETED`、過去ジョブに失敗／`invalid`／`pending`／欠損がある実例で、録画履歴・ダッシュボードが全区間成功と誤認させない。
- セグメントに対応コメントが0件の場合の「字幕対象なし」、収集未実施、収集失敗、SRT生成失敗を区別する。空のSRTファイルを生成済みとして演出しない。
- DBに `valid` とあるがファイル不在／0バイト／最終メディア長不一致のとき、成功と誤表示せず診断へ進める。
- 古いエラーと現在の回復、`UNKNOWN` と明示的 `OFFLINE`、プロセスの古い診断結果と最新結果を区別する。確定した8画面の範囲で解決する。

### 06. 運用・セキュリティ・品質設計

> 出典：[`docs/requirements.md`](requirements.md) §11.6–11.8、§13–17、§20、§23、[`docs/ui-spec.md`](ui-spec.md) §11–16。**設計ドラフト**。

#### 1. 運用と起動

- WSL2 Ubuntu 24.04でPython 3.12 + uvにより直接実行する。Dockerは初期の必須条件としない。
- CLI候補は `start`、`status`、`url`、`backup`、停止時の `config get web_port` と `config set web_port PORT`。起動後の実効固定ポートを標準出力と原子的な `endpoint.json` に記録する。
- Windowsサインイン後のWSL2/アプリ起動を運用対象に含む。Windowsロック中・画面OFFでは継続できるか実測。スリープ／休止／シャットダウン中は録画できない。
- 起動前に録画の実効保存ルート（DB永続設定を含む）を他のアプリケーション／別インスタンスの確認済み保存先と照合し、Windows上の同一・親子衝突なら録画開始を拒否する。自動で既存設定を取得できなければ導入時の手動確認対象とする。
- 停止復帰時は、DBのジョブ状態、実在する録画子プロセス、Windows上の一時ファイル／正式ファイルを照合する。過去の異常 `CANCELLED` や `pending`、有意な `.part` ファイル、DB上の `valid` でも実ファイルが不在・空のケースを検査し、無検証で消去／成功変更しない。
- 修正・設定変更・録画ツール更新後の実機試験は、**修正を読み込んだ新しい稼働インスタンス**の起動時刻・必要な実行ファイルを照合してから行う。シェルのPATH変更だけで `systemd`／常駐プロセスの起動環境に反映されたとは扱わない。

#### 2. 認証なしローカルアプリの防御境界

- デフォルトバインドは**`127.0.0.1`のみ**。LAN・インターネットへ認証なしで公開しない。
- 管理UI/APIは保存・削除を含む強い操作が可能。CSRF防止、Origin/Host検証、CORS最小化、GETによる状態変更禁止が必要。
- URL/パス正規化、SSRF、パストラバーサル、コマンドインジェクション、XSS、SQLインジェクションを防御。管理画面やAPIでは録画動画・字幕のファイル本体を提供しない。
- TwitCasting Client Secret、Euler Streamキー等はGitや診断ログに入れない。`.env.example`はダミー値のみ。Linux通常ユーザー権限で動作。
- コメントには視聴者ID・本文が含まれる。個人用ローカル保管と削除・保存期間を運用設計に含める。手動SQLiteバックアップは期限削除に連動しないため、利用者が旧スナップショットを破棄・更新する手順と、手動復元後の保持期限再照合を必須とする（初期版の復元CLIは提供しない）。

#### 3. ログ・可観測性

構造化ログ（例：JSON Lines）に、UTC時刻、level、component、event_code、exception/traceback、correlation ID、セッション/ジョブ/配信者ID、プラットフォーム、外部HTTP status、レート情報、WebSocket切断理由、録画子プロセスexit code、再試行、欠損区間、容量異常を記録。APIキーや署名付きストリームURLはマスクする。

- HTTPステータスを本文のパースに先行させ、非JSON・空本文の403／429／5xxも記録する。認証拒否・レート制限・タイムアウト等を `OFFLINE` と誤記録しない。
- 録画エンジンの実切替・終了コード・取得バイト数・秘匿済みstderr、使用する `ffmpeg`／`ffprobe` の実体とバージョン・コーデック対応、最終ファイル検証結果を追跡する。
- 同じ監視警告の連打は抑止しても、継続回数・最終発生・回復・影響区間を失わない。`last_error` は発生時刻と対象ジョブを持ち、後続ジョブの回復を打ち消さない。

`system_events` は診断・検索向けの要約を保持する。`/logs` の初期実装では既存コードにリーダーがなくても、現行・ローテーション済み構造化ログを制限付きで読んで時刻・レベル・出力元・キーワード絞り込み／ページング／詳細表示できるバックエンドを用意する。システム状態は健康状態と影響を要約し、`/logs` は現行・ローテーション済みの構造化ログと利用できるイベント要約を読み取り専用で参照する。対応関係は記録済みのIDで追跡する。動画、コメント、SRTの成否は個別にログ・画面表示する。

- ログ読取は許可したアプリケーションのログディレクトリと対象ファイルに限定する。リクエストからファイルパスを受け取らず、行・ファイルの読み取りと応答件数を制限する。
- ローテーション中の消失、破損行、巨大な行、アクセス拒否を扱い、管理画面や監視・録画の処理を長時間止めない。本文やトレースの秘密情報は記録時と閲覧時に保護する。保存時に混入した秘密情報も**サーバー応答前に再マスキング**し、検索結果・詳細・コピーすべてに同じ安全な本文を用いる。

#### 4. 復旧と障害分離

想定障害：WSL2停止、Python強制終了、SQLiteロック／書込失敗、外部ネット切断、API枠不足・認証失敗、子プロセス異常、コメントWS切断、容量不足、Windows録画先の書込拒否、未確定ファイル。

- 監視／録画／コメント収集の故障ドメインを分離。
- 二重録画・二重セッション・コメント重複保存を復旧時にも防ぐ。
- 復旧できなかった時間は `collection_gaps` に記録し、欠損を推測で補わない。同じ配信で後続の録画ジョブが成功しても、過去ジョブの失敗・保持した無効ファイル・欠損はセッション集計から落とさない。
- 自動削除は既定OFF。有効化しても確定済み動画＋全対応字幕のファイルのみを対象にし、コメント・履歴・欠損を残す。進行中・未確定を削除しない。手動削除の一部失敗・強制終了を未完了として復旧し、破損ファイルの無検証上書き、別ドライブへの黙った代替保存をしない。

**再起動の処理順序（要求§14.2.1）：** 排他を確立→保存ルート／未完了台帳・ファイルの再照合→未確定媒体の検証・確定→コメントSRT・音声SRT・ASSの公開台帳とコメント保存境界の照合→対象配信の状態確認→再開可否を決定する。対象単位の復旧保留で無関係なジョブをブロックしない。配信終了と運用停止を区別し、確定終了したセッションには再接続を生成しない。手動停止フラグ・設定を再評価する。再開時は既存のセッションと番号予約を用いる。欠損に原因、対象ジョブまたは接続世代、復旧操作識別子、開始・終了の根拠（実測／近似／不明）を持たせる。未確定・不明の時刻をNULLで表し、gap_statusで開区間と閉区間を区別する。確認済みのメディア終端／コメント境界から欠損を開く。再試行時は同じ中断の識別子を再利用し、確実な復帰または終了確認により閉じる。復旧手続の成功と無欠損取得を別に集計する。

#### 5. テスト構成

| 層   | 主な検証                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| ---- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 単体 | コメント期限設定・期限判定と論理削除済み配信者の再登録、配信者フォルダの永続マッピング／Windows安全名衝突、古い例外ログの再マスキング、セッション固定のファイル名衝突識別子、SRT版数の公開前後競合と0件への更新、ID正規化、入力検証、設定レート、UNKNOWN／ENDINGの遷移、HTTPエラー／空本文、受信タスク寿命、空試行の連番非消費、セッション内採番、最終メディアの映像コーデック検証、SRTスキップ・収集終了後の後着コメント再同期、ログの絞り込み・秘匿・相関情報                                                 |
| 統合 | コメント期限削除によるコメントSRT・ASS／DB一致、旧手動バックアップの残存注意表示、既存アプリの実効ルート比較、配信者復帰後の履歴保持、既存読取器のない状態からのログ検索・詳細閲覧、配信検知→録画とコメントの独立開始、SQLite、動画確定、SRT、REST/UI、バックアップ、ローテーションをまたぐログの読取と画面間導線。複数ジョブを持つ1セッションの最新成功＋過去失敗／無効／欠損集計、確定後の実ファイル／DB長の照合                                                                                              |
| 障害 | コメント期限削除途中の停止／再開、旧バックアップからの手動復元時の期限再照合、別IDのフォルダ衝突と再起動保持、Windows同名衝突・予約後競合、SRT公開直前／直後の版数更新、LIVE中の403／429／タイムアウト→UNKNOWN継続、ENDING猶予と終了未確認の収束、遅延コメント／キュードレイン／SRT版数・暫定・再同期失敗、複合外部キーと採番予約後クラッシュ、設定OFFの停止、削除途中失敗・復旧、子プロセス強制停止、無効FLV／0バイトMP4、取消後の未確定ファイル、DB書込、NTFS保存先、二重実行、ログの消失・破損・アクセス拒否 |
| 実機 | TikTok/ツイキャスAPI・権限、コメントの継続受信・部分終了理由、ツイキャスHLSの短い先頭／不連続／本編冒頭・末尾、最終動画の実再生と録画区間、使用中プロセスとバイナリの確認、Windows↔WSL2 localhost、ロック・画面OFF・スリープ                                                                                                                                                                                                                                                                                    |
| UX   | 主要8ナビと下位ビュー、ログ詳細の閲覧・コピー、条件復元、状態鮮度、セッションと最新ジョブの区別、SRTスキップ／未収集、過去エラー・実ファイル不一致の可視化、キーボード、1024／1280／1440px                                                                                                                                                                                                                                                                                                                      |

モックイベント、ローカルHLS、テストSQLiteでオフラインテストを成立させる。`docs/requirements.md` §17の固定 `TC` と§23の `AC` をテストコード・実行結果へ対応付け、未対応を記録する。主要8画面と下位ビューは実ブラウザで操作確認し、ブラウザ環境がない場合は「未検証」と明記する。ただしモック成功を実サービス接続の動作確認と混同しない。プリロール境界の有効ケースだけでなく**本編冒頭を誤って破棄する否定ケース**も試す。ffprobe成功、動画の実再生、配信全区間の収録、コメント全件性、SRT時刻精度は別々の証拠で確認する。テスト実行時点が異なる合格件数を合算して単一の最終実行結果としない。

**並列試験の隔離設計（要求§17）**：tmp_path、worker別DB・設定・保存先・runtime・ポート・ブラウザコンテキストを利用する。外部APIは模擬、通常の秘密は非継承。実資源は明示許可まで既定拒否する。

分離不能資源のみ `serial` とし、並列終了後に直列実行する。負荷に応じworker数を調整し、失敗を直列成功で隠さない。実接続／実機は独立した明示許可を要する。

最小隔離を先に整え、各機能のfixture・否定試験を段階的に追加する。専用ハーネス全体の完成を実装開始条件としない。テストの子プロセス回収と実資源の非干渉をTC-111で確認する。

#### 6. 受入・報告基準

各機能の結果を**実機検証済み／ローカル検証済み／未検証／実装不可**に分ける。監視、録画、コメント収集、動画確定、SRT自動生成・再生成、コメント検索、Windows側保存、録画動画・字幕のHTTP取得経路の不在、再起動復旧、主要8画面、システム状態とログの導線・検索・詳細閲覧を追跡する。

実機確認の報告は、**実行インスタンス／起動時刻、配信ID、ジョブID、セグメント、DB状態、最終ファイル、SRT、関連ログ**を同一事例として突き合わせ、確認できなかった点を明示する。「実配信1件で改善した」は一般的な全配信パターンでの解決や過去のDB不整合の修復を意味しない。

## 12. 設計判断・未決定事項・リスク台帳

### 12.1 機能実装・採用時の検証事項

実装前は安全境界と有望候補の確認に限定し、実機・実配信は機能実装後の採用検証でよい。未確認機能のみ利用を保留する。

| ID      | 不確実性                                                          | 検証方法と成功基準                                                                                     |
| ------- | ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| RISK-01 | TikTokLive/Euler Streamの実際の無料枠、署名・接続・再試行時の消費 | API操作ごとのHTTP応答、実際の消費、429、接続数、利用条件を記録                                         |
| RISK-02 | TikTokのライブ検出・録画ストリームの利用可能性                    | 実配信で検知・録画・再接続・ffprobeを検証（権限範囲内）                                                |
| RISK-03 | ツイキャスBasic認証、コメント増分・レート枠の共有単位             | 実APIレスポンスとレート制限ヘッダーから挙動を記録                                                      |
| RISK-04 | ツイキャス匿名番号の視聴用WebSocket取得・API照合                  | 実配信で同一Movie／コメントID対応、匿名番号の正確さを検証。曖昧ならNULL                                |
| RISK-05 | SRTの正確な0秒同期と未確定/分割録画の復旧                         | PTS・first media時刻・補正値を実測し、同期法ごとの誤差を報告                                           |
| RISK-06 | WSL2 NAT/mirroredのWindows localhost到達性                        | 実使用構成で固定ポート・設定変更後の到達性・競合拒否・プロセス生存確認を実機試験。未使用構成は追加検証 |
| RISK-07 | 同一NTFSボリューム内rename、強制停止直前/直後のDB乖離             | 障害注入と再起動照合をテストし、上書き・消失がないことを確認                                           |

### 12.2 機能Specで具体化すべき事項（横断方針は確定・詳細値は未確定）

- **確定方針：** 明示的終了根拠→`ENDING`→猶予満了時再確認。`UNKNOWN`／`ERROR` だけで確認済み終了にしない。孤立セッションは期限付き再照合の後、未確認の収束を区別し、同一配信IDを再利用する。**詳細待ち：** 配信ID不在時の識別、再開判定の状態遷移表。猶予15秒・孤立再確認120秒は要求§11.8.2に従う。
- **確定方針：** 監視OFFは当該配信者の稼働中の録画・コメントも停止、録画OFF／コメントOFFは対象ジョブだけ停止。全体コメント設定と配信者設定はAND。復帰時は同一セッション内で重複起動せず、録画手動停止指示を優先する。**詳細待ち：** 各ジョブの完全遷移表、再試行・内部取消と手動指示のイベント区分。停止時間予算は要求§14.4。
- **確定方針：** 同じ正規化済み配信者IDはサービスをまたいで同じフォルダとし、異なるIDがWindows安全名へ変換された結果衝突したときのみ、元ID由来の安定した識別子で分離し、その対応をサービス非依存に永続固定する。既存フォルダは改名せず、管理外フォルダを黙って取り込まない。**詳細待ち：** ディレクトリ名と対応表のDDL／一意制約。
- **確定方針：** `recording_segments.stream_session_id` と親ジョブへの複合外部キー、`UNIQUE(stream_session_id, segment_number)`、セッション単位 `next_segment_number` を採用。保持判定後にトランザクションで予約し、空試行は採番せず、予約番号は再利用しない。正式名が他セッション／管理外ファイルと衝突する場合は最初の予約時にセッションID由来の安全な識別子を固定し、排他的に確定する。**詳細待ち：** 型・インデックス・ファイル操作との復旧状態・移行手順。
- **確定方針：** 手動バックアップは期限削除の対象外。自動復元CLIは設けず、停止中の手動復元は要求§15.2.1に従う。
- **確定方針：** 削除は「ファイルのみ」または「ファイル＋履歴」、履歴のみは不提供。自動削除は初期OFFで確定済みファイルのみ。稼働中は削除不可、`deletion_operations` に処理を先行記録して部分失敗は再起動照合、履歴あり配信者の削除は `archived_at` による論理削除、同一キーの再登録は既存行の再有効化（履歴維持、3設定は明示値で再指定）。コメント期限は初期`0`（無期限）、有効時は収束済みセッションに対して派生SRTを先に削除しSQLiteコメントを消去する別の内部処理（台帳・再開・削除済み状態あり）。**詳細待ち：** 参照整合性DDL、削除再試行のトランザクション境界、期限削除の状態値とテスト。
- `/api/v1`の個々のパス・メソッド・入出力・例外・冪等性。ログ一覧／詳細のサーバー側取得処理は初期必須（ログ専用公開APIや新基盤は不要）。
- **確定方針：** SRTはセグメントごとの字幕入力版数と生成版数を照合、コメント未確定なら暫定、後着・補正変更なら再同期、収集終了・キュー書込完了後に確定。公開前・公開後の入力版数再照合と同一セグメント所有の旧SRT置換、0件更新時の旧SRT除去、期限削除済みの生成禁止を守る。**詳細待ち：** 動画0秒補正の失敗条件、再試行API・状態値。表示秒数は要求§11.8.1の既定4秒。
- Windowsサインイン後の起動方法、プロセス管理（終了時の予算は要求§14.4）。修正後インスタンスの特定、`ffmpeg`／`ffprobe` の実行環境、終了時キュー書込・有意な一時データ回収。

### 12.3 追加検証課題

下表は初期実装で検証すべき技術リスクと受け入れ条件である。

| ID      | 設計上の論点                                    | 検証条件・留保                                                                                                                                             |
| ------- | ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| RISK-08 | `UNKNOWN`／HTTP拒否で配信中のジョブを誤終了する | 403／429／タイムアウト／非JSON／不明statusを再現し、録画・コメントを維持。明示終了の検知と孤立セッションの収束も別途確認。複数の配信条件で検証する         |
| RISK-09 | TikTok受信タスクの寿命とコメント損失            | SDK戻り値・遅延受信・例外伝播・停止時キュードレインを確認。少数の受信／保存件数一致は配信の全コメント取得の証拠ではない                                    |
| RISK-10 | FFmpegと最終ファイルの検証不足                  | 実使用バイナリと映像コーデック対応、不明FLV・0バイトMP4・リマックス失敗、確定後durationとDBの一致を検証。特定環境のバージョン／固定パスを要求にしない      |
| RISK-11 | 再接続・取消・セグメント連番の整合              | 空試行の番号非消費、ジョブ横断番号、`ENDING` 猶予、子プロセス停止・異常 `pending` 回収を検証。移行・復元対象DBの不整合を自動的に修正済みとみなさない       |
| RISK-12 | ツイキャスHLSでプリロールのみ記録               | 不連続タグの省略、初期化情報の切り替え、既知素材の繰り返しを検証する。正式保存・採番前の除外と本編冒頭の保全を確認する。固定秒数による除外を行わない       |
| RISK-13 | 最新ジョブ完了による過去の欠損隠蔽              | 全録画ジョブ、保持セグメント、`collection_gaps`、コメントジョブをセッション単位で照合し、失敗を隠さず一覧・詳細に出す                                      |
| RISK-14 | 0cue SRTと収集未実施の誤同一視                  | 対象区間コメント0件は「スキップ」、収集不能・収集失敗・生成失敗は別状態。後着コメントによる暫定SRTの再同期を含め、最終ファイル長とタイムコードの整合を検証 |
| RISK-15 | 古い稼働プロセス・ログで修正を誤判定            | 修正反映後のインスタンス・実使用バイナリ・時刻を照合し、古い `last_error` と継続エラー・回復を別々に判定                                                   |

**実装への引き渡し**：`RISK-01`〜`RISK-15` は `tasks.md` の対応テストへ紐付け、実施日時・試験方法・結果・未検証の理由を残す。

**優先する横断受け入れシナリオ**：同一配信で `FAILED`／`invalid`／欠損が生じた後、次の録画ジョブだけ `COMPLETED` となるケースを必須とする。最新ジョブの状態、動画・コメント・SRT・欠損の実体、画面に表示するセッション取得結果を一致させ、全区間完了を偽装しない。

## 13. 実装時の技術上の注意事項と検証条件

以下は初期実装の設計・検証条件である。具体的なバイナリ版・絶対パス・待機時間・判定閾値は環境に応じて調査し、固定要件と混同しない。

### 13.1 配信監視・コメント受信

- HTTPステータスを本文のJSON解析より先に判定する。空本文・HTML応答、403、429、5xx、接続失敗、タイムアウト、不正JSONを区別し、`connect_error`、`timeout`、`invalid_json` 等の診断コード、対象、最終成功時刻、直近失敗時刻、回復を記録する。サービス／配信者単位でバックオフし、継続障害の持続時間・件数・直近発生と回復後の再発を追跡する。
- `LIVE` とRoom IDを確認して開始する。`last_seen_live_at` はLIVE確認時のみ更新する。監視不能が継続し録画子プロセスが終了した孤立セッションは期限付き再照合と診断で収束させ、無条件の終了確定も無期限のLIVE放置も避ける。
- 同一録画ジョブ内の再試行では実際にデータを書けたエンジンを優先候補にし、見かけ上の切替を記録しない。真の切替、新しい失敗、回復後の再発を記録する。短命終了時には秘匿済みstderr、終了コード、書込バイト数を照合する。
- TikTokLiveの採用版で `start()` が受信タスクを返す場合は、開始コルーチンの復帰と受信終了を区別し、受信タスクの終了を待つ。終了時はキューをドレインして永続化し、受信例外は上位に伝播する。キャンセル時はSDKタスク・WebSocket・HTTPクライアントを回収する。保存成功・失敗・切断理由・時間帯・最終受信時刻を別に記録する。受信件数と保存件数の一致は配信全体の完全取得を証明しない。

### 13.2 メディアツールと録画確定

- ffmpegとffprobeは実行可能な同一運用版の組を解決し、コーデック対応を診断する。非対話シェルに対話シェルのPATH変更が反映されると仮定しない。設定可能な候補と実行環境のPATHから解決し、使用する実体を記録する。WSLのPOSIXパスをWindows版実行ファイルに無変換で渡さない。
- 存在・非0バイト・音声・durationだけで `valid` としない。映像ストリームの `codec_name` と正式出力自体のprobeを確認し、未知映像、リマックス失敗、0バイトを成功扱いしない。有意な一時ファイルは回復可能な形で保全する。HEVCの `hvc1` は実際にHEVCと判定された場合だけ適用する。正式出力のサイズ・長さ・コンテナ・コーデックを再計測してDBと照合し、SRTは確定媒体の時間長を使用する。
- `ENDING` と終了確定、手動停止、内部取消、子プロセス終了を区別する。停止・取消でも子プロセスを回収し、有意な一時データを可能な限り確定する。異常 `CANCELLED`／`pending` と孤立ファイルを再起動時に照合する。空試行は保持セグメントの番号を消費しないが、取得欠損と再試行の事実は残す。番号の予約・所有・非上書きは§9の制約に従う。

### 13.3 ツイキャスのHLS・プリロール・再接続

- 公式APIの公開配信用 `hls_url` を優先する。URIは独立引数とし、必要な入力オプション（`-icy 0`、適切なUser-Agent／Referer）は採用版の契約と利用条件を確認して入力前に指定する。署名や認証情報は秘匿し、アクセス制限を回避しない。ローカルのループバックプレイリストを使う場合は外部接続先・転送先を制限する。
- 初期の有限プレイリストに終了タグがあっても、本編の根拠がなければ設定された待機範囲で更新を待つ。判別不能なら取得可能な映像を保全する。ファイル名、長さ、最初の不連続タグだけでプリロールと断定しない。明示キュー、メディアURI、初期化情報、不連続を組み合わせ、識別不能な本編候補を削除しない。
- メディアプレイリストの404では公式HLS起点から再解決する。バリアント変更・シーケンス巻き戻りは入力を分離し、公開済みのメディアを読み終えられるようにする。`#EXT-X-MAP` のURIパスまたはバイト範囲の変更、更新時の先頭初期化情報変更では、境界までを現在のFFmpeg入力とし、後続を同一ジョブ内の次の入力に引き継ぐ。メディアシーケンス、不連続タグ、暗黙バイト範囲の開始位置を維持する。通常の初期化切替にネットワーク障害用の再接続回数・待機を消費しない。
- 本編と断定できない終了タグは公式監視と同一Movie IDの再確認で判断する。FFmpegの正常終了・最終出力検証・HLS資源取得の成功が揃うまで取得済みと断定しない。異常終了またはメディア404時は、同じ有限プレイリストも再接続上限内で再試行可能とする。再試行時は署名クエリの変化で同一候補を二重保存しない一方、異なる短い本編候補を除外しない。公式終了確定・手動停止・再試行上限を区別し、未取得区間を欠損として記録する。
- 内容照合によるプリロール除外は正式保存前の小さな先頭候補に限定する。コンテナ固有情報を除く既知の映像・音声パケット列の1周期を比較し、両方が同じ回数の周期で構成されることを確認できる場合に限り採番前に除外する。未知素材・本編混在候補は他の明示根拠がない限り保全する。長さや固定秒数のみの除外は禁止する。
- 診断にはHLS取得段階・HTTP応答件数、初期化ファイル／メディアの別、メディアシーケンス、不連続・初期化タグの数、セグメント数、終了タグ、FFmpeg終了区分・終了コード・書込量・秘匿済みstderrを含める。URL・署名・認証情報を診断に含めない。HTTP 200でも入力解析が失敗する可能性を考慮する。
- 実FFmpegと一時SQLiteによる有限・更新型HLSの初期化切替、暗黙バイト範囲、404後再解決、同一有限候補の再試行、終了タグと公式監視の組合せ、通常の本編冒頭・短い本編の保全、繰り返しプリロールの除外、転送先制限を試験する。実配信で先頭・本編・末尾を照合し、短いファイルだけの取得を完全収録としない。

### 13.4 横断検証と状態表現

- 全ジョブ・全セグメント・欠損・コメント収集からセッション取得結果を構成する。最新ジョブの完了とセッションの欠損ありは両立する。`last_error` の発生時刻・対象ジョブ・回復状況を区別し、記録された識別子・時刻に基づいてログへの導線を提供する。実ファイル不在・0バイト・一時ファイル・DBとdurationの不一致を可視化し、無断で修正・改名しない。
- SRTは対象コメントなし、収集未実施・失敗、未生成、生成失敗、同期不能、暫定・確定を区別し、0cueの空ファイルを成功扱いしない。
- 実装・修正の反映を起動時刻、使用バイナリ、インスタンスID、コード版で確認する。試験結果は異なる時点の件数を合算せず、実行ごとに証跡を記録する。実配信試験は許諾・保存先分離後に実施し、同一セッションIDで監視・録画・コメント・DB・最終媒体・SRT・ログを照合する。全編デコード、音声、先頭・末尾、配信実時間に対する収録範囲、コメント受信継続、SRT同期を別々に検証する。
- 優先する否定ケース：LIVE中の403／429／タイムアウト／不明status、エンジン切替・途中書込・再試行・手動／強制停止、未知FLV・HEVC／H.264・0バイトMP4、複数ジョブと空試行、SDK遅延・例外・取消、プリロールと本編混在、0cue／未収集／時刻不明、保存済みのinvalid／pending・飛び番号、最新ジョブ完了と他ジョブ失敗の同居。関連するTCとRISKの証跡を記録する。

## 14. 音声文字起こし・統合ASSの初期実装設計

### 14.1 処理境界と依存関係

要求§7.9–7.10、§10.3–10.5、§11.9、§13.6を正本とする。`postprocess/jobs.py` が永続キューと回収を管理し、`postprocess/transcription.py` がFFmpeg音声抽出・faster-whisper推論・cue整形、`postprocess/ass.py` がASSレンダリングを担当する。アプリ親プロセスでのみSQLiteを更新する。推論子には管理済み入力、一意な試行ID、設定スナップショット、一時出力先のみを渡す。子からの結果は構造化メタデータとして検証し、信頼できないパス・範囲外cueを拒否する。コマンドは引数配列で実行し、シェル展開や任意パス指定を禁止する。

`pyproject.toml` の `speech` extraにfaster-whisperを指定し、uv.lockでCTranslate2等の解決結果を固定する。コアの初期導入は `uv sync --locked`、音声実装段階の追加導入は `uv sync --locked --extra speech`、実行は `uv run --locked --extra speech live-rec-lab start`。コアのみの起動を許容するが、音声機能の依存欠落はblockedとして表示し、初期版の受け入れを免除しない。WSL2のCUDAドライバ・ライブラリ・GPU認識・モデル取得は採用版で実証する。モデル取得とキャッシュは要求§7.9・§11.8.3・§17に従い、ライブラリの既定キャッシュへのフォールバックを防ぐよう明示パス・子環境・ローカル読込条件を指定する。隔離取得は別途承認した準備工程のみ、ローカル試験は自動取得なし。依存パッケージ導入はコアのPhase 1と音声のPhase 4に分離する。抽出一時音声は制限付き作業領域、公開一時字幕は最終動画と同一Windowsボリューム。終了時に所有物だけ清掃する。モデルは同一推論子で再利用可能とし、モデル変更・デバイス変更時に解放／再読込する。親は子の生存と進捗を監視し、停止期限や資源異常時に回収する。

音声extraの未導入でもコアが起動するよう遅延import／子プロセス内importを採用する。依存衝突は採用版を理由付きで調整し、ロックを再現可能にする。uvの全extra解決自体がコアを妨げる場合は一時的にspeech定義を分離し、音声段階で正式定義とロックを復元する。未導入を成功にしない。

### 14.2 ジョブと公開プロトコル

1. セッション収束・媒体確定後、適格セグメントを列挙し、重複なしで音声キューへ投入。終了未確認から同一配信が復帰した場合は未開始分を保留し、実行中の対象は安全に中断・再投入する。公開前に収束状態を再確認する。
2. 自動／手動起動条件とリソースを検査。DBで1件をclaimし、試行IDと入力媒体世代を固定。推論と音声抽出中はDB書込ロックを保持しない。録画・コメントの負荷を優先し、起動を保留できる。
3. タイムラインを維持して抽出・認識し、基本VAD・cue境界検証を行う。高度な品質判定・語時刻補正の最適化は追加検証とする。音声SRTを一意な一時ファイルに生成する。
4. セグメント操作ガードで媒体世代、試行ID、公開版、削除予約を再照合。初回非上書き／所有済み版のみ置換し、公開操作台帳でファイルとDBを照合してコミットする。無発話では管理済み旧版の除去を確認する。失敗時は旧正常版を保持する。
5. 音声結果版とコメントSRTの入力・生成版、媒体世代、表示設定をキーにASSをキュー投入する。ASSの描画は確定媒体の表示寸法と回転を使用し、Speech下中央・Comment上方レーンの分離、ASSの時刻精度・安全なエスケープを検査。片側・暫定・欠損理由を保持し、入力の変更時は再投入する。
6. 停止時は新規claim停止、子プロセスを期限内で回収。起動時に所有物・媒体世代・台帳とDBを照合し、失われたrunning試行を中断として記録し適格なら再投入する。再起動・同一入力の繰り返しで重複生成しない。

### 14.3 永続化と保全

本書§16のDM-15〜17に従う。音声ジョブの試行履歴、最後の正常版、最新試行を分離し、ASS入力タプルと公開版・暫定／部分状態を記録する。発話検索は管理済み音声SRTの確認済み結果を安全にパースして行う。キャッシュは結果版ごとに無効化し、全文・ファイル本体をログに載せない。ファイル削除は全対応字幕と一時成果物を対象に、推論停止または終了確認後の共通操作ガードを使用。コメント期限削除ではコメントSRT・ASS公開を先に禁止し、全ASSの削除確認後にDBコメントを消す。音声SRTは保持し、期限削除済み対象のASS再生成は拒否する。

### 14.4 検証・リスク

| ID      | 技術リスク                                              | 検証条件                                                                                                |
| ------- | ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| RISK-16 | WSL2 CUDA／CTranslate2の互換性、メモリ枯渇・録画負荷    | 採用版と実使用GPUまたは明示CPUで推論、同時録画、子異常、モデル未取得を測定。コア継続とblocked表示を確認 |
| RISK-17 | 音声タイムラインと幻覚フィルタ、ASS描画・版数・期限削除 | 既知音声と時刻・縦横回転・外部プレイヤー、反復発話、版競合、削除／再起動の障害注入を確認                |

TC-081〜088を単体・統合・障害・実機に分け、GPU試験の模擬成功を実機成功とみなさない。音声・ASSの生成失敗を録画取得欠損と混同せず、独立した実行証跡を保持する。

## 15. 設定・操作・診断の横断設計

### 15.1 設定の型と版

要求§11.8.1〜3を唯一の値定義とし、`config.py` にキー・型・既定値・検証・反映方針を持つ共通レジストリを実装する。API/UIの設定メタデータと初期値はこれから生成し、ハードコードした別の既定値表を作らない。内部ポリシーと環境値は通常設定の編集対象から分離。非秘密設定の版を単調増加させ、比較・検証・書込をSQLite短期取引で実行。フィールドの反映は保存後のコミット済みスナップショットで行い、各機能の実効値・反映失敗・待機を追跡する。ポート・ルート・監視枠・字幕版更新の境界は要求どおり。設定変更時に実行中の処理を無断再起動しない。設定の期限・削除範囲を拡大する操作は確認を要する。

### 15.2 配信者の表示順

`streamers.sort_order`、一覧の順序版 `streamer_order_revision` と `streamer_order_mode` を持つ。順序版は一覧構成変更と順位変更に限って増加し、状態ポーリングと独立させる。DBでは有効行の順位を一意にする部分一意索引等を採用し、全順位再割当は衝突しない一時順位を用いた単一取引。アーカイブ行はNULL順位（履歴保持）。再有効化・追加は現在の最大順位の次へ、整数上限が近ければ同じ取引で詰め直す。順序比較キーは要求§11.3、検索用の正規化比較値を持ち、日本語にも対応する。監視スケジューラは表示順位を読まない。並べ替えのUIは全対象・版をスナップショットにし、ドラッグ／キーボードで同じ操作を発行、競合時は最新順に復帰する。

#### 15.2.1 配信者入力アダプター

要求§5.1.1の検証候補を事前調査・採用実証で確認し、採用した形式のみ共通normalize_identityに登録する。Web・API・CLIが同じ検証器を利用する。URL解析→許可スキーム／ホスト／パス検証→ID検証→正規プロフィールURL再構成を行う。アカウント同一性キー、検索キー、Windows保存キーは別々に生成し、DBの一意制約はアカウント同一性のみ。既存行のplatform／ID書換えを更新サービスで拒否し、同一キーの復帰は順位版と3設定を含む原子的再有効化とする。

### 15.3 コメントの閲覧・集計・字幕表記

`/comments` のmode=session/all、session*id、order、検索条件、pageを正規化し、モードごとの下位ビューに分ける。セッション索引は動画の有無ではなく取得ジョブ／コメント／期限削除の状態を起点とする。本文の時間キーは COALESCE(posted_at, received_at) とIDの複合順。同じキーを日付フィルターとページングに使い、取得時刻の由来を返す。進行中の追加で件数が変わる場合もページの整合と表示の鮮度を保つ。SQLのLIKEはパラメータ化し、文字列の `%`／`*` をリテラルとして扱う検索入力ではエスケープする。全件ブラウザ転送は禁止。

要求§7.5.1のコメントカウンターは処理結果の確定単位で同一DB取引に反映し、バッチまたはイベント処理識別子の一意性で再適用を防ぐ。pendingの更新と確定時の減算を排他的に行う。DB停止中の未永続観測を正常な確定統計としない。ジョブ間の集計と現在残存件数を分離する。匿名番号の照合失敗は受信本文の破棄とは別の診断。

コメントSRTとASSは共通の投稿者表示関数を用いる。匿名判定・番号の信頼性は実取得の属性に限定し、表示文字列だけを正規化する。要求§7.7の例・時刻・表示秒数と§7.10のエスケープを共有し、原文は不変。字幕設定変更時の版更新は既存のDM-07/16/17に従う。

### 15.4 外部連携とセキュリティ

要求§4.1.2、§4.2.4〜5、§6.2、§13.2.1、§13.3.1に従う。監視アダプターは署名不要経路の利用可否と実消費を採用版で比較し、署名を要する経路はコメント／録画の枠と分離して記録。状態観測には制限確認の根拠と時刻を含める。確認済み制限は録画・コメント双方の開始／再接続を拒否し、稼働中なら各ジョブへ停止を通知する。状態不明から制限種別を創作しない。候補を順に検証するがアクセス拒否はフォールバック条件としない。品質対応表は採用エンジンごとの実証結果から構築し、試行に要求品質／選択品質／理由を記録する。外部署名通信は許可先・必要フィールドの境界をSDK版ごとに検証し、秘密を送信／記録しない。

HTTPヘッダーは例外応答も通る共通ミドルウェアで設定。CSPは要求§13.2.1、テンプレート自動エスケープ、HTMX eval無効、テキストDOM更新、同一オリジン資産を実装し、CSP違反とブラウザ操作を回帰試験する。Host／Origin／CSRFを変更操作に独立適用。インラインの動的styleが必要ならCSSクラス等に置換し、CSPを一律緩和しない。

#### 15.4.1 媒体URL検証・実接続

要求§4.1.4.1に従い、SDKの実応答からRoom・媒体候補・品質・解決時点を一時的に受け取る。検証済み候補オブジェクトだけを録画起動サービスに渡す。許可ホストとDNSの全解決先を検証し、実際の接続先を検証済みアドレスに拘束できる転送方式を採用版で実証する（TLSのホスト検証を維持）。各リダイレクト、プレイリスト内URIも再検証。直接起動ツールが自動追従する参照先を安全に制御できないときは、検証付き転送経路を評価し、それでも保証できなければ当該方式を不採用とする。FFmpegの不要なプロトコルを許可しない。秘密付きURLをプロセス引数・stderr・診断へ露出しない安全な受渡し方式を検証し、秘密をログ／永続化へ流さない。既存の制限停止・試行予算と整合し、同一Roomのみ再解決する。

#### 15.4.2 外部経路の採用記録

要求§4.1.2.1とTC-097／106に従い主経路を選択・実装・実証する。research.mdには採用版、利用条件、Room/Movie、消費、品質、結果、未確認と理由を簡潔に記載。主経路が不適合なら許可された代替を評価する。実証は該当機能の利用条件であり、独立作業を妨げない。

### 15.5 診断・停止・集計

要求§14.3〜5に従い、管理ループと子プロセスのハートビート／実進捗・期待次回を別管理する。`runtime_instances` と機能別スナップショット（instance_id、component、scope、状態、確認・成功・次回・理由）を照合し、旧インスタンスの値を現在の正常値にしない。生存応答とreadinessを分け、`/api/v1/health` の503契約を適用。外部・容量・版の診断はバックグラウンドで更新、GETはキャッシュ読取。エンジン診断は `recorder_engine_stats` をサービス＋エンジンで一意化し、試行IDで確定失敗／成功の二重計数を防ぐ。利用可否と本編成功を区別する。

停止の全体期限と各並行処理の予算は要求§14.4の単調時計に従う。停止を開始した親は受付を止め、管理対象のプロセスグループ・受信タスク・推論子だけに通知し、期限に従い回収と永続化を優先する。期限切れは台帳・未回収理由を記録し、再起動照合へ。タイムアウトを積み上げて30秒を超える設計にしない。

ログは単一書込サービスで要求§14.3の容量・世代を管理する。読取時はファイル世代／位置を照合し、回転競合は再読取または範囲不足を返す。イベント抑制では `system_events` の同一障害episode、最初／最後の発生、回復時刻、発生数を保持。期間ごとの集計で抑制された発生の全時刻を得られない場合は事象数（初発時刻）と正確に取得できた発生数のみを提示し、不明な期間別発生数を推測しない。ログとの二重集計を禁止する。

ダッシュボードの容量は実体照合済み管理ファイルを一意に集計し、サービス＋配信者行・論理削除済み・保存ルート別に帰属する。共有フォルダ走査で他配信者の容量を重複計上しない。総計・上位・その他・未確認と取得時点を返す。警告期間・環境要約は要求§11.2.1に従い、別の時系列DBや常駐サービスを導入しない。

#### 15.5.1 診断スケジュール

要求§14.5.1を正本とし、管理タスクに診断キューを設ける。対象キー単位で重複投入をまとめ、状態遷移で更新、定期処理で期限の来た対象だけを更新する。空き容量の60秒保守はstat相当の限定処理、容量集計は確定・削除の差分と照合結果から再計算する。プローブは別タスク／管理子で有限実行し、DBは短いスナップショット取引のみ。優先処理が滞る場合は診断を延期し、次回予定と理由を保存。古いインスタンスのスナップショットは現在の正常を保証しない。

### 15.6 非同期ストレージ照合

要求§10.5.1に従い、`storage_scan_runs` に実行ID・instance_id・対象ルート／管理集合スナップショット・状態・時刻・処理済み件数・集計・範囲不足・有限の結果明細を保持する。queued/runningを全体で一意にclaimし、同時要求には同じIDを返す。取消はフラグ、バッチ間で確認し、cancelledと部分結果を確定。走査中の登録・ファイル世代変更・マウント断は未確認に分類する。短いDB読取と有界I/Oの反復、録画優先のyield、有限期限（実証で決定）を適用し、完全走査できない場合はpartial。外部リンク／再解析ポイントで管理ルート外を走査しない。終了時に総数と明細上限を別記し、最後のcompletedとlatest runを分ける。再起動時は未完了をinterruptedに更新し、破壊操作を行わない。GETは台帳参照のみ。

### 15.7 試行予約と予算の復旧

要求§6.5.2、DM-21に従う。`job_retry_budgets` はセッション・ジョブ種別・明示再開世代で一意、現行世代を種別ごとに一つに制約する。各ジョブは対応する予算FKを持つ。初回予約と追加試行予算をトランザクションでclaimする。`job_attempts` は予算FK・ジョブ種別・実ジョブFK・試行番号・一意ID・状態・理由・開始／終了を持ち、種別に対応するFKが一つだけ有効でセッション／種別を照合する。試行番号は予算世代内一意、初回は番号0、追加は1から上限まで。消費と予約を同じDB書込で確定し、外部処理後は同じIDで結果を確定する。クラッシュ後は未確定予約を不明として収束させ、既存子／接続の回収後、必要なら別の追加試行を消費する。明示停止・制限・予算枯渇の抑止は同一セッション内の自動ジョブ再作成でも迂回不可。次回時刻、時計不整合、明示再開による新世代を検証する。

### 15.8 HTTP共通処理と非同期操作

同期操作は設定版／順序版・DB一意制約・短い取引で競合を防ぐ。非同期は既存ジョブID・状態と削除／字幕台帳で重複防止し、受付と結果を区別する。汎用受付テーブルは設けない。HTTP入力・エラー・ページングは§17。

### 15.9 UIインタラクション・検証基盤の設計境界

恒久的な表示・操作契約はUI正本§5.4.1〜2、§5.6、§6.2.1、§6.3.1、§6.4.1、§6.5.1、§6.7、§8.1.2、§13.7、§14.4〜5を正とする。以下は実装責務の分離であり、DBスキーマ・API・設定レジストリ・業務状態・主要画面を追加／変更しない。

- Web表示用の共通変換器に状態種別と既存状態・鮮度・時刻根拠を渡し、ラベル・意味色・記号・動き・表示時刻を一元化する。業務サービスや集計結果の更新を行わない。既存の管理ID、成果物版、ログ世代／安定位置と部品種別をテンプレートの安定識別に使用する。表示名や行番号は同一性の根拠にしない。
- ブラウザ側は操作中の領域を置換しない方式を優先し、詳細な復元機構は必要な場合に追加する。画面・対象・条件・要求世代、既存の設定／順序版、操作状態を管理し、送信時と反映直前に競合を検査する。適用許可された更新だけで対象別の閲覧基準・展開・フォーカスを保全する。明示操作と定期読取は別経路として扱い、URL・履歴と結果を同期し、定期更新では履歴を増やさない。日時の表示整形とAPIの日付境界／DBソートは分離する。
- 操作中／結果未確認はUIの状態であり、サーバーの終端状態と混同しない。§17.2・§20の現在値・業務ジョブID・設定版／順序版で照合する。応答欠落時は最後の確定値と未確認表示を保ち、危険な自動再実行や未確認の確定を禁止する。照合に必要な操作情報は当該操作の範囲で扱い、秘密・未保存入力をURLやログへ出さない。並べ替えは既存の全順序／版付き原子保存、フォームは設定レジストリによる正規化・検証・原子保存に従う。表示上の操作保護はサーバー側の不変条件の代替ではない。
- 開発専用fixtureとブラウザ検証は製品から隔離する。採用UIを共通テンプレートで実装し、実配信・字幕未実証でも隔離データで検証する。利用者選択が明示要求された場合だけ当該デザインの適用を待つ。
- Playwright・axe-core等は開発検証の候補。代表ケースの実ブラウザ操作を優先し、自動アクセシビリティ検査・追加幅・画像証跡・独立レビューは必要に応じ拡充する。製品実行時依存や通常起動の開発ルートは増やさない。

### 15.10 2モード・共通診断・隔離検証

要求§13.7、§14.6、§15.2.1を正とする。共通CLI／config／app factory／opsを利用し、モードは設定解決前に確定する。通常は実運用、開発者は隔離専用とし、実運用を直接開く開発者起動は設けない。

- 専用Linux基底・状態・録画領域・ポート、DEV\_環境キーと `.env.dev` を分離する。通常の設定・秘密を継承しない。停止中の隔離設定も共通検証器を通す。通常の媒体だけの異常は当該操作を保留し、管理領域・隔離境界・ポート異常は起動拒否。
- `~/.local/state/live-rec-lab-shared/` に非秘密の保護パス設定、短い設定変更ロック、外部通信ロックと小規模な外部予算状態を置く。要求§15.2.1に従い現行・変更先・必要な旧パスを照合する。独立境界台帳・専用復旧機構は不要。ファイル操作直前に所有・世代・実体を再確認する。
- 手動DB復元は停止中の排他、退避、整合性検査、DB外の復元未完了マーカー、媒体・期限・予算等の照合を使う。マーカー中の業務起動を拒否する。自動復元は設けない。 復元は短い停止中保守手順と既存業務処理を優先し、複雑な競合はマーカーを残し手動確認または安全な差戻しとする。起動時復旧も確実な対象のみ処理し、孤立物・不明世代は保全・保留する。
- 外部通信は通常・許可済み隔離・実接続試験を通じ1実行に限定し、共有レートリミッターとDB外の永続予算を使用する。 通常運用の登録・有効化に基づく通信と開発の範囲限定明示許可は要求§15.2.1に従い区別する。許可済み範囲内の通常再試行に個別承認は不要。通常DB復元で予算を戻さない。未確認の消費は安全側に保留する。
- OpenAPI／Swagger UI／ReDocは隔離開発者のみ。通常のCSRF・Origin／Host・CSPを維持し、非ブラウザ操作は既存保護付きAPIを優先し、必要時のみ要求§13.7の限定例外を利用する。専用障害注入APIや専用デバッグUIは必須でない。curl／Python手順を実測してマニュアルへ記載する。
- diagnoseは既存ログ・ヘルス・SQLite読取専用接続・有界な実行時情報を共用する。DB作成・移行・設定変更をしない。JSON契約は要求§14.6、秘密や本文は出力しない。通常DBに依存する障害は一貫したバックアップを隔離に取り込み、実運用パス・秘密・自動処理を切り離す。
- fixture・障害再現は隔離専用。各機能の実装に応じて検証を追加する。実配信をUI実装の前提とせず、採用案に依存する製品UI実装は利用者の3案からの明示選択後に行う。通常運用の回帰を別途行う。

## 16. 永続データモデル制約（DM）

SQLite WAL、短い同期トランザクション、busy_timeout、全接続でforeign_keys=ON。時刻はUTC、表示・ファイル名のみIANAタイムゾーンで変換する。

| 制約                         | 所有テーブル・規則                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| DM-01 配信者                 | `streamers` の UNIQUE(platform, platform_user_id)。履歴があれば archived_at で除外。再登録はアーカイブ解除と指定された3つの真偽値を同一書込で確定。過去セッションへのFKを維持。                                                                                                                                                                                                                                                                                                                                                                             |
| DM-02 配信セッション         | `stream_sessions` の UNIQUE(streamer_id, platform_stream_id)。IDなしの観測では既存セッションの確認に留め、新規作成を保留。状態、最終LIVE、終了根拠、platform_ended_atとended_atを別々に保存。未確認収束で推測時刻を入れない。                                                                                                                                                                                                                                                                                                                               |
| DM-03 採番とFK               | `recording_jobs` の UNIQUE(id, stream_session_id)。`recording_segments` の FOREIGN KEY(recording_job_id, stream_session_id) REFERENCES recording_jobs(id, stream_session_id)、UNIQUE(stream_session_id, segment_number)。NULLは未採番を表す。SQLite書込トランザクションで next_segment_number と正式名を予約し、確定失敗・クラッシュ・削除で巻き戻さない。空試行は予約せず、欠損・イベントを記録する。                                                                                                                                                      |
| DM-04 Windows配置            | `streamer_folders` は normalized_id の主キーと Windows同名性キーの一意制約。正規化・大文字小文字・終端ドット／空白・予約名を処理し、異ID衝突は元ID由来ハッシュを延長して解決。既存マッピング・フォルダを再利用し、管理外のフォルダは拒否。現在のディスク上も照合する。                                                                                                                                                                                                                                                                                      |
| DM-05 保存段階               | 未採番行→同一ボリューム一時録画→保持判定→予約済み→排他的公開→確定。初回予約でセッションの `filename_datetime`（配信開始または検知のIANA変換済み文字列）、`filename_title`（安全化済みタイトル）、フォルダを固定し、各採番で正式パスを予約する。動画と全字幕のパスは要求§10.3の命名規則で一元解決し、接尾辞込みの長さ・Windows同名性・管理パスを照合する。最初の予約で同名ならID由来 filename_disambiguator を固定し、以後は同じ値を使用。後から同名が生じた場合は保留・復旧し、既存物に触れない。再起動は予約・一時・正式パスとファイルの同一性を照合する。 |
| DM-06 コメント               | `comments` の UNIQUE(stream_session_id, platform_comment_id)。NULLのIDは強い同一性の証拠がない限り統合しない。posted_at・received_at・保存時刻とオフセットを分ける。録画開始より前／不明の録画オフセットはNULL。本文・匿名番号は実取得データ。時刻・セッション・投稿者に検索用インデックス。                                                                                                                                                                                                                                                                |
| DM-07 SRT                    | セグメントごとに input_version、generated_from_version、provisional、status、srt_path、補正値・同期法。該当入力の変更と版数増分を同じSQLite書込に含める。一意一時ファイルと公開前チェック、管理下の旧コメントSRTのみ置換。srt_pathは要求§10.3のコメントSRTを指す。公開後に版数照合、スキップは旧SRTの安全な除去後。再起動時は版数／公開状態を比較する。                                                                                                                                                                                                     |
| DM-08 削除台帳               | `deletion_operations` は削除後も残る対象ID・mode・status・対象パスと所有物判定情報・時刻・エラー。台帳を先にコミットし、対象の所有・状態を逐次照合。ファイルのみは files_deleted_at を確定、ファイル＋履歴は配下のジョブ・コメント・欠損を参照整合性を保って除去する。                                                                                                                                                                                                                                                                                      |
| DM-09 コメント期限           | UTC ended_at＋正整数日数（初期0）。収束、ジョブと書込の完了、復旧中でないことが条件。台帳登録以降は書込・SRT生成を排他停止、管理コメントSRT・統合ASS確認後にコメントを物理削除し comments_purged_at と purged_comment_count を確定。動画、履歴、ジョブ、欠損を維持。部分失敗は台帳で復旧し、期限削除済みコメントからのコメントSRT・ASS再生成・再取得は拒否、音声SRTを保持。                                                                                                                                                                                 |
| DM-10 実行・イベント         | `system_settings` は非秘密設定（固定Webポート、自動更新間隔、録画ルート、speech_subtitles_enabled／speech_model／speech_language／speech_deviceを含む。既定値・検証・反映契約は原本に従う）、`runtime_instances` は実行識別。イベント要約に実際の相関IDを残し、削除済み履歴からは参照だけを切る。WAL DBはOnline Backup APIで退避。手動復元後に期限SRTの整合を再評価する。                                                                                                                                                                                   |
| DM-11 試行と収録範囲         | 録画セグメントに実エンジン、試行イベントの関連を持つ。started_at／finished_atは壁時計範囲、duration_msは確定メディア長。試行の進捗・終了理由はイベントに関連付け、空試行の採番禁止を維持。                                                                                                                                                                                                                                                                                                                                                                  |
| DM-12 コメント接続・カーソル | 接続世代・対象配信ID・確認結果をジョブと関連付ける。接続世代・確認済み配信ID・verified_boundary・last_durable_atと取得失敗／欠損の境界をDBで追跡し、コメント保存／重複確認と境界更新を同じトランザクションで行う。旧接続世代を別セッションに保存しない。                                                                                                                                                                                                                                                                                                    |
| DM-13 ファイル世代・操作排他 | DM-05、DM-07〜09、DM-15〜17共通。削除台帳の対象ごとに管理ID、ルート実体、相対パス、検証済み世代識別情報と状態を保持する。再試行直前に照合し、別世代・未接続・識別不能を保留。同一対象の確定・生成・削除を予約で直列化する。                                                                                                                                                                                                                                                                                                                                 |
| DM-14 欠損と復旧             | collection_gapsの開始／終了を実測・近似・不明と区別し、不明時刻はNULL、gap_status（open／closed）で終端不明と復旧待ちを区別する。対象セッション・ジョブ種別・中断識別子（recovery_key）で復旧処理の重複を防止。最後の確実な取得から次の確実な取得／終了確認までを記録し、推測の終端を作らない。                                                                                                                                                                                                                                                             |
| DM-15 音声ジョブと試行       | `speech_jobs` の `segment_id` UNIQUE・FK、`speech_attempts` の job FK・試行番号 UNIQUE(job_id, attempt_no)、claim識別子、起動種別、設定スナップショット、媒体世代、状態・理由・開始／終了・duration_ms。最新試行と最後の正常結果を分離し、同時runningは1件（DB制約＋親管理）。成功版は結果版・要求§10.3の音声SRT相対パス・cue数・世代・無発話状態を保持。削除済みセグメントへの投入禁止。                                                                                                                                                                   |
| DM-16 ASS成果物              | `ass_artifacts` の segment_id UNIQUE・FK、状態、要求§10.3のASS path、結果版、入力タプル（媒体世代、音声結果版、コメント入力／生成版、表示設定版）、暫定・片側・欠損理由、cue数・省略数・公開日時・実ファイル世代。入力変更で再同期を予約。コメント期限削除中／済みの公開・再投入を拒否。                                                                                                                                                                                                                                                                    |
| DM-17 字幕公開・復旧         | 音声SRT／ASSの共通公開台帳（segment_id、kind、operation_id、input_revision、temp／final管理パス、世代、状態、エラー）。予約・公開直前／直後・DB確定を追跡し、DM-13の排他と所有照合を適用。初回非上書き・再生成時の旧正常版保全、0cue時の安全除去、停止／再起動の冪等回収。ファイルのみ削除は全字幕成果物をdeletedにし、履歴削除はFK配下を除去する。コメント期限削除はコメントSRTとASSのみ先行削除。                                                                                                                                                         |

Alembicは初期スキーマを作成し、将来のスキーマ変更は独立した移行として管理する。既存番号・ファイル名は取得済み記録。旧命名で予約済みの既存セッションは既存パス／stemを基に命名を継続し、旧名を解析して不確実なタイトルを捏造したり改名したりしない。新規セッションにR §10.3の5要素形式を適用する。重複や照合不能な旧データを発見した場合は対象を保護して移行を保留する。

### 16.1 設定・表示順・診断の追加制約

| ID    | データ・制約                                                                                                                                                                                                                                                                                                                                            |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| DM-18 | `streamers.sort_order` は有効行で一意の正整数、アーカイブ行はNULL。`streamer_order_revision` は一覧構成／順位変更の比較更新用。設定版・`streamer_order_mode` と区別し、全順位変更を単一取引で保存。表示順は監視と独立。                                                                                                                                 |
| DM-19 | `comment_collection_jobs` の排他的カウンター・最終受信／確定・計数完全性を要求§7.5.1どおり管理する。非負整数、確定スナップショットの等式、処理識別子で冪等更新。エラー事象数・匿名番号欠損・残存コメント数は別。実効時刻＋IDの安定検索索引。                                                                                                            |
| DM-20 | `recorder_engine_stats` は(service, engine)一意。版、解決元、利用可否、確認／成功／失敗時刻、失敗理由、累積／連続失敗、再試行数を保持。試行IDで重複計数防止。機能別健康状態はinstance_id＋component＋scopeで識別し、鮮度・期待次回・理由を返す。`system_events` は障害episodeと初発／最終／回復、取得できた件数を保持し、期間集計はログと重複させない。 |

DM-10の設定値は要求§11.8.1〜3のキー・型・既定・単位・範囲と設定版に従い、起動時の欠落キー補完・保存の原子性を保証する。秘密情報は通常設定に保存しない。

### 16.2 外部境界・診断・操作の制約

| ID    | データ・制約                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ----- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| DM-21 | `job_retry_budgets` にセッションFK・job_kind・generationの一意キー、現行世代の一意制約、retry_limit（0〜30の開始時値）、retry_used（0〜retry_limit）、initial_attempt_reserved、last_attempt_id、retry_suppression_reason、next_retry_at（UTC）を保持。録画・コメントジョブはretry_budget_idで参照し、設定ON/OFF等による新ジョブも同一世代を共有する。既存retry_count/reconnect_countはジョブ単位の追加試行統計で、予算判定はretry_usedのみ。`job_attempts` は一意ID、予算FK、種別に応じた排他的ジョブFK、UNIQUE(retry_budget_id, attempt_no)、状態・根拠・時刻。セッション・種別の一致を制約する。初回0、追加1〜上限。予約と消費を原子的に確定し、未確定予約は返却しない。ジョブを自動再作成してもセッションの抑止を回避しない。 |
| DM-22 | `storage_scan_runs` は実行ID主キー、instance_id、対象範囲・スナップショット、状態、取消要求、開始／終了、確認数・分類別集計・有限明細・未確認範囲・期限理由を保持。queued/runningの同時1件制約。再起動時interruptedへ収束し、最後のcompletedと最新結果を区別。読取専用。DM-20の診断スナップショットは確認時点・期待次回・延期理由を保持する。                                                                                                                                                                                                                                                                                                                                                                                     |
| DM-23 | 同期操作は一意制約・設定版／順序版を使う。非同期は既存業務ジョブ・削除／字幕台帳で重複防止し、同一対象の競合を拒否する。汎用operation_receiptsテーブルは不要。                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |

DM-01のplatformと正規IDは登録後不変とし、検索用の正規化・DM-04のWindows安全名と分ける。採用した入力プロファイルは要求§5.1.1に従う。

外部予算はアプリDB外に保持する。共有単位・制限窓・消費済み／不明・429待機を小規模なSQLiteまたは原子的状態ファイルで管理し、通信前に安全側に計上する。復元・再起動・切替で予算を戻さない（要求§15.2.1）。

## 17. HTTP・CLI契約

ループバックの同一オリジンのみを基本とする。変更操作はCSRFとOrigin／Host検証を行い、GETは参照のみ。隔離データの非ブラウザ専用経路の限定例外は要求§13.7と本書§15.10に限る。レスポンスはJSONまたは日本語HTML。認証情報・生ログ・動画や字幕ファイル本体は返さない。

| 操作                                                                          | 入力・主要出力                                                                                                                                                                                                                                                                                                                                                                                                                               |
| ----------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| GET /api/v1/health、/api/v1/system                                            | 要求§14.5の生存・ready・録画三値、機能別状態・確認／成功／期待次回・鮮度、エンジン診断・環境・警告・保存先。必須基盤不良／停止中はhealth 503、任意機能劣化のみは200。                                                                                                                                                                                                                                                                        |
| GET /api/v1/dashboard                                                         | 内部保存済みKPI、容量（配信者別・総計・未確認）、警告期間 `period=24h/7d/30d`（既定24h）、環境要約、時刻・範囲・診断リンク。                                                                                                                                                                                                                                                                                                                 |
| GET／POST /api/v1/streamers、PATCH／DELETE /api/v1/streamers/{id}             | ID・サービス・表示名・監視／録画／コメント。POSTは3設定を明示。再有効化は同じIDと履歴を返す。DELETEは確認必須。                                                                                                                                                                                                                                                                                                                              |
| GET /api/v1/streamers/order                                                   | mode、全有効IDの確定順、order_revision。GET一覧はq、platform、status、modeで絞り込み可能。                                                                                                                                                                                                                                                                                                                                                   |
| PUT /api/v1/streamers/order                                                   | `ordered_ids`（全有効IDの順序）、`order_revision`。不正集合422、競合409、保存後の順と新版。モード変更は設定版付き設定更新。                                                                                                                                                                                                                                                                                                                  |
| GET /api/v1/sessions、/api/v1/sessions/{id}                                   | サービス・配信者・日時・状態による検索、セッション単位の集計とセグメント・欠損                                                                                                                                                                                                                                                                                                                                                               |
| POST /api/v1/sessions/{id}/stop、/resume                                      | stopは従来の録画手動停止。resumeは既定scope=recording、停止／上限到達したコメント収集の明示再開時のみscope=commentsも受理。明示確認・有効設定・同一LIVE・制限解除等を検証し、受付とジョブ確定を分ける。新予算は要求§6.5.2に従う。                                                                                                                                                                                                            |
| DELETE /api/v1/sessions/{id}                                                  | 明示確認と mode=files または mode=files_and_history。結果に操作ID・状態・理由を含める。復旧中・危険な操作および終了未確認のファイル＋履歴削除は409。                                                                                                                                                                                                                                                                                         |
| GET /api/v1/comments                                                          | `mode=session/all`、`session_id`、`order=asc/desc`、本文／投稿者／配信者／サービス／日付・ページング。sessionでID未選択なら配信一覧、選択済みまたはallなら本文。実効時刻とIDの安定順、期限削除・状態・取得時点を別表示。                                                                                                                                                                                                                     |
| POST /api/v1/segments/{id}/srt                                                | 対象・補正値。生成／暫定／対象なし／再同期待ち／失敗を返す。処理不可・期限削除・競合は対象理由を返す。                                                                                                                                                                                                                                                                                                                                       |
| GET /api/v1/speech                                                            | 配信者・サービス・セッション・状態・ページ。キュー集計、媒体・音声最新試行／正常版・ASS状態、操作可否。                                                                                                                                                                                                                                                                                                                                      |
| GET /api/v1/speech/cues                                                       | session_id、segment_id、q、order、page。確認済み音声SRTの構造化cue（時刻・本文）、結果版・取得状態。ファイル本体を返さない。                                                                                                                                                                                                                                                                                                                 |
| POST /api/v1/segments/{id}/speech                                             | 個別再生成。操作ID、受付／拒否理由・現行試行状態を返す。重複／削除／未確定は409。                                                                                                                                                                                                                                                                                                                                                            |
| POST /api/v1/speech/retry-failed                                              | 失敗対象のみ一括再試行。対象範囲・確認・受付件数／拒否件数と理由を返す。                                                                                                                                                                                                                                                                                                                                                                     |
| POST /api/v1/segments/{id}/ass                                                | 個別ASS再生成。期限削除・競合・不適格は409。受付と結果を分ける。                                                                                                                                                                                                                                                                                                                                                                             |
| GET /speech/cues                                                              | 発話本文の日本語下位ビュー。                                                                                                                                                                                                                                                                                                                                                                                                                 |
| GET／PUT /api/v1/settings                                                     | 公開可能な非秘密設定と保存版。値と版を検証して反映する。固定Webポート、録画ルート、自動更新間隔、音声自動生成 `speech_subtitles_enabled` (bool)、`speech_model`、`speech_language`、`speech_device` を含む。§7.9の許可値と版を検証する。全通常設定・型・範囲・既定値・反映契約・編集可否のメタデータは要求§11.8.1〜3に従い、ポートの現在値と次回起動値を区別する。動作中のポート変更は次回起動から、自動更新間隔は保存後に再起動なしで反映。 |
| GET /、/streamers、/recordings、/comments、/speech、/system、/logs、/settings | 指定順の日本語主要画面。配信セッション詳細でSRT等を扱う。                                                                                                                                                                                                                                                                                                                                                                                    |
| GET /logs（クエリと同画面の詳細）                                             | 時間・重要度・出力元・本文・実在相関IDの絞り込み。範囲・件数・順序・ページ・取得状態と秘匿済み詳細。ログの任意パス入力は受け付けない。                                                                                                                                                                                                                                                                                                       |

入力不正は共通詳細契約の400／422、オリジン等の拒否は403、対象不存在は404、現在の状態に対して操作不可は409、読み取りや保存失敗は説明と実際の状態を返す。UIでは安全に再試行できる範囲を示す。

CLIは `live-rec-lab start`、`status`、`url`、`stop`、`backup DEST`、`config get web_port`、`config set web_port PORT`、`diagnose --json`。`--dev` は隔離対象を選択する。固定ポートの競合は拒否し自動代替しない。停止中の設定変更は対象を確認する。バックアップの保存場所とコメント残存を警告する。DB本体のHTTP取得は設けない。

### 17.1 読取と操作状態の契約

- 部分更新は現在の検索条件・ページ・対象キーに対応する保存済みデータと取得時点、取得不能／古い状態を返す。クライアント側で要求世代と対象キーを照合し、古い応答を反映しない。設定保存は現行の版検証を維持する。定期GETは外部監視・状態変更を行わない。
- セッション詳細では実際のエンジン、壁時計範囲、確定媒体長、欠損境界の根拠、削除操作IDと保留／部分失敗を必要に応じ返す。システム状態の照合結果は正常総数・確認総数・異常分類・未確認と表示上限を区別する。既存URLと削除2方式を変更しない。

- HTTP防御は要求§13.2.1の共通ヘッダー・CSP・自動エスケープを成功／失敗応答とも適用。GETは副作用なし。設定保存は設定版を比較し、未知・秘密キーを拒否し全値を原子的に検証・保存。削除範囲拡大は確認を必須とする。並べ替えの専用版は設定版・監視状態と独立し、保存済みの順を返す。セッション詳細には§7.5.1の収集統計・制限停止理由・要求／実品質・エンジン試行診断を含める。

### 17.2 共通詳細契約（全管理API）

要求§13.2.2を具体化する。既存のループバック・Origin/Host/CSRF・設定版／順序版を保持する。要求§13.7の隔離データの非ブラウザ専用経路のみ限定例外を認める。

| 項目   | 契約                                                                                                                                                                                                                                                                                                                                                                                      |
| ------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 入力   | JSONはUTF-8、Content-Typeを検証。変更本文は最大1 MiB（ストリーム読取でも超過を拒否）、クエリ全体8 KiB、検索文字列最大256 Unicodeコードポイント、IDは正のSQLite 64bit整数、未知キー／不正型・非有限値・重複JSONキーを拒否。フォームにも同等制限を適用。配信者文字列と表示名は要求§5.1.1。設定値は§11.8の専用定義を優先し、順序一覧が本文上限に収まらない場合は明示拒否して部分保存しない。 |
| ページ | 一覧の既定page=1、page_size=50、1〜200。正整数、超過・負値・offsetの安全範囲外は422。総数と明細は同一読取スナップショット、total/page/page_size/has_next/as_ofを返す。範囲外ページは空結果（0件とは別）、UIは現在の絞り込みを維持し有効ページへ戻れる。                                                                                                                                   |
| 日付   | 表示タイムゾーンのYYYY-MM-DDを厳密解釈。開始日0時以上、終了日の翌日0時未満をUTCへ変換する。DSTで境界が曖昧／存在しない場合は明示的な境界規則（最初の有効時刻）を使用し、日付自体が存在しない場合は422。start>end、不正日付は422。                                                                                                                                                         |
| ソート | コメント・発話は既定のasc/descと時刻＋ID、履歴はセッション時刻降順＋ID降順、ログは記録時刻降順＋安定位置、音声一覧はジョブ作成時刻降順＋ID降順。配信者は既定／カスタム順契約。各画面の既存固定順・検索条件は変更しない。未対応キー・orderを拒否しSQL断片を受け取らない。                                                                                                                  |
| エラー | JSON `{ "error": { "code": "VALIDATION_ERROR", "message": "日本語説明", "fields": { "field": "理由" }, "correlation_id": "実在ID" } }`。fieldsは必要時、秘密は除去。400構文不正、403防御拒否、404不存在、409状態／版競合、413本文超過、415形式不正、422検証不正、503必須基盤・一時利用不能。成功は200／201、非同期受付202、処理結果は別途取得。HTMLも同じ分類を表示。                     |
| 冪等性 | 同期更新は一意制約・設定版／順序版により競合を検出。非同期は対象と業務ジョブの状態・削除台帳で重複実行を防ぐ。結果不明時は現在値と業務状態を再取得し、危険な再実行をしない。汎用Idempotency-Keyの必須化・詳細失効管理は不要。                                                                                                                                                             |

非同期操作はqueued／running／completed／partial／failed／cancelled／interrupted等の業務状態を使用する。受付は業務ジョブ／操作ID・状態・確認先を返し、既存業務APIから結果を確認する。同期操作は200／201で確定結果を返す。

| 操作                                  | 契約                                                                                       |
| ------------------------------------- | ------------------------------------------------------------------------------------------ |
| POST /api/v1/system/scans             | 202、単一照合受付、実行中なら既存scan_idと状態。対象は管理ルート集合（任意パス指定不可）。 |
| GET /api/v1/system/scans/latest       | 最新実行および最後のcompleted（未実行ならnull）、取得時点と結果要約。                      |
| GET /api/v1/system/scans/{id}         | 実行状態・処理済み数・対象数（不明ならnull）・範囲・集計と有限の分類別明細、結果取得時点。 |
| POST /api/v1/system/scans/{id}/cancel | 202、取消要求を記録。終了済みは現在状態を返し再実行しない。                                |

登録POSTは `platform`、`account`（IDまたは許可URL）、任意`display_name`、明示3真偽値。PATCHは表示名・3設定のみ（platform／account／正規IDは422）。重複有効行409、論理削除行の再有効化は同じDB IDを返す。採用実証で確定した検証プロファイルを使用する。通常起動の `/docs`、`/redoc`、`/openapi.json`、`/docs/oauth2-redirect` は404。

## 18. 業務操作・状態遷移契約

| 入力・条件                      | 結果                                                                                                                        | 副作用                                                                                                                                                 |
| ------------------------------- | --------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| LIVE（同じ／新しいID）          | LIVE。同じIDの既存セッションを優先。新しいIDは別セッション。                                                                | 確認済みLIVEで有効な独立ジョブをそれぞれ最大1件起動。期限削除済みコメントの再収集は禁止。                                                              |
| 明示OFFLINE／明示終了           | LIVE → ENDING                                                                                                               | 要求§11.8.2の終了猶予。監視で再確認する。録画は継続。                                                                                                  |
| ENDING中にLIVE                  | ENDING → LIVE                                                                                                               | 同一セッションを継続。                                                                                                                                 |
| 猶予後の明示終了再確認          | ENDING → ENDED                                                                                                              | 終了根拠と時刻を保存してジョブを安全停止。                                                                                                             |
| UNKNOWN／ERROR                  | 監視結果のみ更新。即座にENDEDへ遷移しない。                                                                                 | 両ジョブが停止し正常確認不能なら要求§11.8.2の孤立再確認後、終了未確認と理由を保存。後の同一IDを再利用。内部ポリシー値を試験で注入可能にする。          |
| 監視OFF                         | DISABLED。稼働セッションは安全停止後に終了未確認。                                                                          | 双方のジョブと新規監視を停止。OFFLINEは記録しない。                                                                                                    |
| 録画OFF                         | 対象の録画ジョブだけ停止。                                                                                                  | 他方は継続。有効ファイルは確定。                                                                                                                       |
| コメントOFF（全体または配信者） | 対象の収集ジョブだけ停止。                                                                                                  | キューを排出し、全体OFF時も個別値を保持。                                                                                                              |
| ON復帰                          | 監視ON、確認済みLIVE、対象機能ONで起動可能。                                                                                | 要求§6.5.2〜3の全抑止と残予算を満たすときのみ一意に新ジョブを起動。同一配信の手動録画停止が優先。                                                      |
| 手動停止／再開                  | 当該配信の録画自動起動を抑止／抑止を解除。                                                                                  | 手動録画再開は有効設定かつ確認済みLIVEの同一セッションで新規セグメント。コメントは停止／上限到達時の明示再開のみ同じ確認条件で新予算を取得（§6.5.2）。 |
| 録画子終了                      | PENDING → STARTING → RECORDING → FINALIZING → COMPLETED／PARTIAL／FAILED／CANCELLED。再試行時はRECONNECTINGからSTARTINGへ。 | 内部取消・手動停止・設定停止・配信終了を区別。子を回収し、一時ファイルを検査。                                                                         |
| コメント接続                    | PENDING → CONNECTING → COLLECTING。切断時はRECONNECTINGを経て再接続、終了時はCOMPLETED／PARTIAL／FAILED／DISABLED。         | 理由、受信・保存・失敗・欠損を記録。受信終了とDB書込完了を区別。                                                                                       |

共通実装（SHOULD）：要求§10.4.1に従い、セグメント単位のガードと操作種別付きSQLite台帳を録画確定・字幕公開・削除で共有してよい。セッション全体の操作は全対象を保護する。台帳分割・専用復旧器は必須ではない。起動時は未完了台帳・未確定媒体から限定照合し、不明な対象は安全な保留・診断へ移す。自動修復を強制しない。所有／世代／外部置換への安全性を証明できない操作は無効とし、期限削除・旧正常版・非上書き・版照合は維持する。

### 18.1 削除・SRT・保存の契約

- 字幕の生成・再生成・削除・照合・復旧は、要求§10.3の共通パス解決とDB上の管理パス・ファイル世代照合に従う。字幕種別を混同せず、管理外の同名ファイルを操作しない。
- 削除要求は確定済みセッション、指定モードと明示確認を必須（終了未確認のファイル＋履歴は409）とし、進行中・未確定・復旧中なら競合を返す。操作が開始したら同じ操作を参照し、途中失敗の再試行は台帳と照合して処理する。DB更新前に対象ファイルの削除を確認する。
- コメント期限削除は独立ジョブで収束後だけ。排他フラグ／台帳を先に記録し、収集・コメントSRT／ASS再生成を停止して管理コメントSRT・ASSを先に除去（音声SRTは保持）する。DB側で確定するまで未完了とする。詳細は DM-08・DM-09（本書§16）。
- SRT生成の入力は有効な動画、確定後の長さ、対象コメントと版数、同期根拠・補正。要求§11.8.1の表示秒数、同時刻はDB ID順、負・末尾外・欠損区間は除外。投稿時刻が無い場合は受信時刻と根拠を記録する。公開前後の競合では未同期状態を返し、収集終了・キュー排出後に再試行する。既存字幕の所有確認に失敗した場合は元ファイルを保護する。
- 実効録画ルートはDB設定、環境値、要求仕様§10.2の既定値の順。設定変更後は新規セッションに適用し既存保存先を移動しない。比較対象は運用者が確認した実経路と実行中の別インスタンス。未確認・同一／親子・利用不可・容量不足時は録画起動を抑止し、現在の診断を返す。比較済みルートの変更時は再照合する。
- API時間計測は期限またはリトライ時刻まで非同期待機。ツイキャスAPIはコメント増分、録画復旧、定期監視、手動更新の優先度順に共有枠を調整する。認証・利用制限の回避は対象外。

### 18.2 障害・競合時の追加契約

- 録画試行：接続待ち／読取停止／出力停滞／初期化切替／停止要求を区別する。許可された抽出失敗だけ切替候補とし、有効取得後は同じエンジンを優先する。終了コード0だけで配信終了としない。子回収後に確定・欠損記録へ進む（DM-11、TC-071〜072）。
- コメント：接続世代と配信ID確認後のみ保存。ツイキャスのページ取得は確実な保存境界と未取得境界を別に管理し、繰り返しや枠不足は有限終了・欠損へ（DM-12、TC-073〜074）。
- 削除：対象ごとの実効ルート・世代・管理IDを予約／実行／再試行直前に確認。別世代・未接続・識別不能は保留または409。再試行は元台帳を使用し新しい対象を削除しない。照合は読取専用、表示上限と総数を分離（DM-13、TC-075〜076）。
- 復旧：確認済み終了済みを再接続しない。終了未確認は既存の有限収束条件を適用し、同一LIVE確認・手動停止・設定・容量を評価する。中断理由・境界根拠・一意な中断識別を用いて欠損を開閉し、台帳とSRTを冪等復旧（DM-14、TC-079〜080）。

### 18.3 音声・ASS業務契約

- 収束済み・確定済み有効セグメントのみ自動投入。音声なしskipped、無発話completed、GPU／依存欠落blocked。設定OFFは自動開始を抑止するが手動再生成は適格条件下で許可。
- pending → running → completed／failed／skipped。blockedは条件回復後pending。再生成は新試行、旧正常結果を保持。中断したrunningは台帳照合後に安全に回収し、適格ならpendingへ戻す。削除済みはdeleted。
- 音声・コメントの成功結果更新に伴うASS再同期は入力版タプルで冪等化。期限削除中／済みはASSの公開・再投入拒否。全対応字幕の削除、操作予約と媒体世代・版の照合、旧正常版保全はDM-13・DM-15〜17に従う。

### 18.4 設定・監視・表示・停止の詳細契約

- 設定は要求§11.8.1〜3の唯一の定義に従う。再接続予算は初回を除く全追加試行（録画候補切替を含む）で消費し、同じ原因の無限再起動を拒否する。終了猶予15秒・孤立再確認120秒は内部ポリシーでありUI設定ではない。
- 明示的な制限判明は録画・コメント両方の新規取得を禁止し、稼働中の双方を安全停止。制限解除を確認するまで再接続不可。通信不明は制限・OFFLINEと混同しない。署名不要監視の実証、公式HLS→公開tc-hls→Streamlink HLSの検証順、要求／実品質は要求§4、§6.2を参照する。
- 配信者の表示順は順序版と有効ID集合を検証して原子的に保存し、監視順は変更しない。コメントの配信別／横断は同じ時刻キーとページング契約を使用。受信分類は要求§7.5.1、字幕の投稿者表記と4秒既定は§7.7・§11.8.1を参照する。匿名属性は推定しない。
- 機能別健康状態・エンジン診断・容量／障害期間の集計は要求§11.2.1、§14.5に従う。停止は§14.4の共通期限を使用、子を並行回収して未完了台帳を保全する。ログ回転は§14.3。

### 18.5 外部境界・照合・復旧の業務契約

- 配信者入力・同一性は要求§5.1.1とDM-01・DM-04に従う。登録後のplatform／正規ID書換え禁止、再有効化・検索キー・保存名の分離。
- TikTokLive媒体候補は要求§4.1.4.1、設計§15.4.1のRoom一致・実接続・全参照先検証を満たしたものだけ。署名不要監視と候補採用は要求§4.1.2.1・設計§15.4.2で未検証／採用／保留を管理し、署名必須候補へ自動移行しない。
- 録画・コメントの初回予約・追加試行消費はDM-21と要求§6.5.2に従う。クラッシュ後に不明予約を返却せず、同一配信の上限／抑止／バックオフを復元する。新予算は確認付き明示再開・新しい確認済み配信のみ。
- 照合はDM-22の単一非同期実行、読取専用、有限・取消可能。最新実行と完了結果を保持し、再起動でinterrupted、記録済み部分結果を保全。診断の更新契機は要求§14.5.1、HTTP定期GETは保存済み結果のみ。
- 操作の重複防止は本書§17とDM-23に従い、既存業務ジョブ・台帳・設定版を使う。

## 19. 外部連携・実環境の実現性調査と採用条件

初期実装の技術調査計画。記載する契約・候補を採用版で確認し、実測結果を記録する。実機・ローカル試験は未実施。

| 対象                 | 判断と理由                                                                                                                                                         | 代替・証跡・未検証                                                                                                                                                                                                                                                                                                                     |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| TikTok監視           | 条件付きで実装可能。SDKの公開配信確認に加え、原本指定のroom応答からHTTPステータス先行で状態とIDを取得する。2＋IDはLIVE、4は終了候補、1／3／不正値はUNKNOWN。       | Streamlinkにもroom APIを読む実装がある。SDKの真偽値だけではroom状態とエラーの区別に不足する。DNS・実配信接続を検証する。[Streamlinkの実装](https://github.com/streamlink/streamlink/blob/master/src/streamlink/plugins/tiktok.py)                                                                                                      |
| TikTokコメント       | 条件付きで実装可能。採用版のTikTokLiveを確認し、公開配信のCommentEventとSDKが返す受信タスクを扱う。Euler Streamの無料署名を使う。                                  | 署名サービスはHTTP 429・残枠を公開する。確認、初回署名／接続、再接続、長時間接続、同時接続それぞれの消費・外部送信・限度を実配信で要測定。固定の無料枠値に依存しない。[SDKドキュメント](https://ttl.docs.isaackogan.com/)、[Euler Streamのレート制限](https://www.eulerstream.com/docs/api/rate-limits)                                |
| TikTok動画           | Streamlinkを初期優先、yt-dlpを抽出障害時の候補とする。採用版は依存定義と実測で確定する。どちらも公開LIVEの実装を持つ。                                             | SDK連携の動画取得は正式API・実配信検証が揃う場合に評価。アクセス制限を理由とする切替を抑止。動画の通し再生と区間網羅は未検証。[Streamlinkの実装](https://github.com/streamlink/streamlink/blob/master/src/streamlink/plugins/tiktok.py)、[yt-dlpの実装](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/tiktok.py)       |
| ツイキャス公式API    | 条件付きで実装可能。Basic認証、current_liveのmovieオブジェクト、公式hls_url、commentsのslice_idとlimit 50を使う。Basic認証枠と個々の操作の残枠を保守的に扱う。     | 公式APIには配信の保護フラグがある。実資格情報でのBasic認証、ページング、共有枠単位、本編の先頭・末尾は未検証。[公式APIリファレンス](https://apiv2-doc.twitcasting.tv/)                                                                                                                                                                 |
| ツイキャス匿名番号   | 未検証。番号が得られる視聴用WebSocketと照合契約が確認できるまで番号をNULLとする。                                                                                  | 公式Realtime /livesは新着ライブ用。アクセス権、同一MovieとコメントID、一意照合、切断を実機で確認してから採用する。[公式APIリファレンス](https://apiv2-doc.twitcasting.tv/)                                                                                                                                                             |
| 実行環境             | Ubuntu 24.04、WSL2カーネル、Python 3.12.x、uvをコマンドで確認する。.python-version、pyproject.toml、uv.lock、開発憲法の整合と、FFmpeg／ffprobeの実行版を確認する。 | 採用するビルドのHEVC等の可否は実ファイルで診断。Windowsドライブのマウント・書込権限を確認する。Windowsドライブのファイル確定とWindowsブラウザの実使用ネットワークモードでの接続は実機検証が必要。依存を隔離環境で解決し、ロックする。                                                                                                  |
| 利用条件とライセンス | 取得はサービスの利用条件と必要な権限が整った配信に限定する。通常の公式API／公開取得経路のみを候補とする。                                                          | 公開表示と抽出の許可は別に確認する。実配信テストは適用条件・利用者権限・分離済み保存先の確認後に実施。各配布物のライセンスは選定したバージョンの配布メタデータおよびライセンスを参照する。[TikTokの公式規約](https://www.tiktok.com/legal/page/row/terms-of-service/ja)、[ツイキャス利用規約](https://twitcasting.tv/indexlicense.php) |

### 19.1 実証計画

- SDKの戻り値、受信タスクの寿命、既知Room ID、状態値の判定、遅延受信、終了時キュードレイン、例外伝播を採用版で確認する。
- ツイキャスの50件飽和時の再照合、残枠制御、プリロール除外と通常の本編冒頭の保全を検証する。
- ローカルHLS、模擬イベント、実FFmpeg、一時SQLiteを用い録画・コメント・SRTまで照合する。
- 実資格情報、許可済み配信、Euler操作別枠、匿名番号の一意照合、Windowsドライブの確定、実使用ネットワークモード（他方は追加検証）、サインイン起動、ロック・画面OFF・再起動を個別に検証する。

- 録画の進捗監視期限・試行切替判定の候補は、正常な低ビットレート、バッファ、HLSプリロール・初期化切替、実際の媒体停止で評価する。候補値は固定要件ではない。
- TikTok接続先IDを採用SDKで確認し、保留・再接続・旧世代イベントの実挙動を検証する。ツイキャスのページ境界・カーソルの意味と欠損判断は公式仕様および許可済み実配信で確認する。
- Windows保存先の実体／ファイル世代識別、外部置換との競合防止、マウント断の判定を実機で検証する。安全性を確認できない削除を保留する。
- 自動更新は遅延・逆順・操作中のswap・非表示復帰を実ブラウザで検証する。反復再起動で欠損境界と削除／字幕の台帳を照合する。

- 音声認識はfaster-whisperの採用版・Python 3.12・CTranslate2・WSL2 CUDA・モデルの互換を確認し、実使用デバイスを優先し、未使用デバイスは追加検証とする。既知音声・先頭無音・縦横回転ASS（長尺・同時録画の詳細負荷、品質フィルタの偽陽性、語時刻最適化は追加検証）と外部プレイヤーでの表示、再生成と期限削除・停止の競合を検証する。RISK-16〜17、TC-081〜088。実測結果は未実施。

### 19.2 監視・品質・診断の実証計画

- 署名不要監視経路とSDK候補を採用版・権限・room判定・署名消費・レート枠で比較する。明示制限は両ジョブを止め、外部通信先と必要項目を秘密非露出で検証する。
- ツイキャスは要求§4.2.4の順序で公開／許諾済み配信の本編開始・継続・再接続・確定を確認する。品質の実ラベル・実際の解像度／ビットレート・下位選択理由を記録し、アクセス拒否で候補を切り替えない。内部タイムアウトは実測で確定する。
- 健康状態は無配信、長い監視巡回、枠待ち、GPU保留、DB不可、子停止、ログ回転、容量不明を模擬して検証する。Windows側容量／保存実体、停止時間予算、CSP実ブラウザ検証の証跡を分離して記録する。

現時点の実証結果は未検証。TC-091〜092、097〜100の実施条件と結果を記録する。

### 19.3 外部契約と採用条件の調査（未実施）

| 候補           | 検証内容                                                                                                                                | 判定   |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| 配信者入力     | 要求§5.1.1の文字数・文字種・URL・大小文字同一性、公式情報・許可済み実アカウント、`c:`等の形式。採用プロファイルを確定する。             | 未検証 |
| TikTok監視     | 署名不要候補と代替の操作別実消費、状態・Room、外部条件、負荷・公平性。全候補署名必須時は無料枠評価と利用者承認。                        | 未検証 |
| TikTokLive録画 | SDK実応答のRoom・HTTPS媒体候補・品質・URL寿命、全接続先の検証可能性・秘匿受渡し・再解決・拒否時停止。安全性を保証できない方式は不採用。 | 未検証 |
| 録画方式・品質 | TikTok優先候補、ツイキャス公式HLS→公開tc-hls→Streamlink HLS。利用条件、要求／実品質、本編・再接続・確定、プリロール、署名枠。           | 未検証 |
| 照合・診断     | 管理集合／NTFS世代、有限走査期限・バッチ、並行録画・コメント・推論時の負荷、プローブ期限、取消・再起動。                                | 未検証 |

採用記録には依存版、確認日時、利用条件、操作・対象範囲、実測結果、未確認部分、判定理由を簡潔に記す。実配信・NTFS実証とローカル障害試験を分離し、未実証は機能別の利用判定に反映する。UI実装は停止しない。

### 19.4 事前調査と採用実証の分離

事前調査は公式契約・利用条件・候補・実証計画を整理し、未確認を未検証として残せば完了できる。実装前に採用必須の安全境界は確認するが、後続のアダプターやCLIの完成を事前調査完了条件にしない。後続の候補別実装・許可済み実配信試験で実証し、採用／不採用／未検証を理由とともに記録する。採用不可の任意候補の成功を求めず、採用経路の必須実証を省略しない。HTTPルートの404等はHTTPアプリを作成してから検証する。 第一候補で各機能の正常経路を先行し、代替経路は確定機能として必要な範囲を後段で拡充する。匿名番号は確実な経路がなければNULL。進展が止まった調査は理由・未確認・再評価条件を記録して保留し、独立作業へ移る。採用条件未達の接続は有効化しない。

## 20. 再試行・削除・操作受付・署名枠の境界設計

- DM-21の予算世代と原因別抑止状態は要求§6.5.2〜3を正とする。抑止理由は複数保持できる構造とし、設定状態・手動停止・枯渇・制限・利用枠・容量・復旧保留を別に評価する。`retry_suppression_reason` は現在の代表理由、全理由は別フィールドまたは関連行で保持。設定版は新予算作成時のみ取得する。明示再開・世代切替・最初の予約を一意に関連づける。
- DM-02/08/21: 終了未確認のファイル＋履歴は予約前に409、状態を操作ガード下で再確認。同一配信再検知時に予算・停止・採番・期限削除状態を残す。ファイルのみ削除は既存の安全条件に従う。
- DM-23: 同期更新と既存業務ジョブによる重複防止を用いる。結果不明時は再取得・照合し、新たな危険操作を自動実行しない。手動復元は要求§15.2.1に従う。
- 外部署名枠は実際の共有単位・同時接続制限・制限窓で管理する。共通レートリミッターと永続予算に通信前に安全側に計上し、不明消費は確証なく戻さない。
- 外部予算は§15.10に従いインスタンス切替・復元をまたいで保持する。DM-21のジョブ試行予算とは別に適用する。

## 21. 固定要件・設計・試験の最小対応基準

以下は機能別の簡易追跡表の入力。検証優先度は要求§17を正とし、A/B/Cに応じた代表試験・部分実施・複数IDの統合検証を許容する。工程名・試験ファイルは候補であり実装済みを意味しない。R=要求、D=設計、U=UI、C=AGENTS.md。固定IDの対応を保ち、詳細は正本を参照する。

Aの複合TCは基本条件を先行し、詳細条件はB/Cとして記録可。B/Cでも要求§17の利用開始前の代表安全条件は必須。全IDを同時に合格させるゲートではない。L2/L3の判断は要求§17.5に従い実装・検証・機能別利用可否を区別する。

### 21.1 受け入れ条件

| 原本節                                                                                                                                     | AC     | 関連TC                                         | FR                                             | 工程責務                                                                                                                                                            | 証跡・検証先                                                                                                                     |
| ------------------------------------------------------------------------------------------------------------------------------------------ | ------ | ---------------------------------------------- | ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| R §4.1、R §5.2                                                                                                                             | AC-001 | TC-011、TC-018、TC-023、TC-045                 | FR-002                                         | 監視・ジョブ制御、TikTok連携、実TikTok CLI実証                                                                                                                      | tests/test_monitoring.py                                                                                                         |
| R §4.2、R §5.2                                                                                                                             | AC-002 | TC-011、TC-023、TC-033、TC-045                 | FR-002                                         | 監視・ジョブ制御、ツイキャス連携                                                                                                                                    | tests/test_twitcasting.py                                                                                                        |
| R §5.1、D §9.1                                                                                                                             | AC-003 | TC-001、TC-002、TC-037                         | FR-001                                         | 配信者・保持・削除基盤、本番UI実装                                                                                                                                  | tests/test_web_ui.py                                                                                                             |
| R §4、R §5.3                                                                                                                               | AC-004 | TC-004、TC-018、TC-023、TC-025、TC-065         | FR-003                                         | 監視・ジョブ制御、TikTok連携、ツイキャス連携                                                                                                                        | tests/test_monitoring.py、tests/test_e2e.py                                                                                      |
| R §5.1、R §6.1                                                                                                                             | AC-005 | TC-027、TC-034、TC-037                         | FR-004                                         | 監視・ジョブ制御、本番UI実装                                                                                                                                        | tests/test_api.py、tests/test_e2e.py                                                                                             |
| R §4.1.4、R §6                                                                                                                             | AC-006 | TC-023、TC-029、TC-043、TC-044                 | FR-005                                         | TikTok連携、録画・確定・復旧、実TikTok CLI実証                                                                                                                      | tests/test_e2e.py、tests/test_recording.py                                                                                       |
| R §4.2.3、R §6                                                                                                                             | AC-007 | TC-020、TC-029、TC-067                         | FR-005                                         | 録画・確定・復旧、ツイキャス連携                                                                                                                                    | tests/test_recording.py、tests/test_twitcasting.py                                                                               |
| R §6.1、R §16.2                                                                                                                            | AC-008 | TC-023、TC-044、TC-052                         | FR-005                                         | 監視・ジョブ制御、録画・確定・復旧、実TikTok CLI実証                                                                                                                | tests/test_e2e.py、tests/test_web_settings.py                                                                                    |
| R §6.5、R §8.3                                                                                                                             | AC-009 | TC-043、TC-044、TC-045、TC-063                 | FR-005                                         | 監視・ジョブ制御、録画・確定・復旧                                                                                                                                  | tests/test_recording.py                                                                                                          |
| D §9.4、R §10.2–10.3、R §14.2                                                                                                              | AC-010 | TC-008、TC-022、TC-029、TC-053、TC-054、TC-059 | FR-008                                         | 設定・保存基盤、録画・確定・復旧                                                                                                                                    | tests/test_storage.py、tests/test_recording.py、tests/test_recovery.py                                                           |
| R §11.4                                                                                                                                    | AC-011 | TC-034、TC-039、TC-041                         | FR-013                                         | 履歴・検索・照合サービス、本番UI実装                                                                                                                                | tests/test_web_ui.py                                                                                                             |
| R §6.4、R §8.3、R §11.4                                                                                                                    | AC-012 | TC-041、TC-064                                 | FR-013                                         | 録画・確定・復旧、履歴・検索・照合サービス                                                                                                                          | tests/test_web_ui.py                                                                                                             |
| R §2.3、R §6.3、R §10.5                                                                                                                    | AC-013 | TC-021、TC-064                                 | FR-005                                         | 録画・確定・復旧                                                                                                                                                    | tests/test_recording.py、tests/test_recovery.py                                                                                  |
| R §4.1.3、R §7                                                                                                                             | AC-014 | TC-019、TC-024、TC-045                         | FR-006                                         | TikTok連携、コメント保存、実TikTok CLI実証                                                                                                                          | tests/test_comments.py、tests/test_e2e.py                                                                                        |
| R §4.2.2、R §7                                                                                                                             | AC-015 | TC-024、TC-033、TC-045                         | FR-006                                         | コメント保存、ツイキャス連携                                                                                                                                        | tests/test_twitcasting.py                                                                                                        |
| R §4、R §7.2                                                                                                                               | AC-016 | TC-006、TC-028、TC-033                         | FR-006                                         | TikTok連携、コメント保存、ツイキャス連携                                                                                                                            | tests/test_comments.py、tests/test_twitcasting.py                                                                                |
| R §7.2                                                                                                                                     | AC-017 | TC-006、TC-028                                 | FR-006                                         | コメント保存                                                                                                                                                        | tests/test_comments.py、tests/test_e2e.py                                                                                        |
| R §7.3、R §8.2                                                                                                                             | AC-018 | TC-009、TC-010、TC-028                         | FR-006                                         | コメント保存、コメントSRT                                                                                                                                           | tests/test_comments.py、tests/test_srt.py                                                                                        |
| R §7.2、R §8.1、D §9.6                                                                                                                     | AC-019 | TC-007、TC-028                                 | FR-006                                         | DB基盤、コメント保存                                                                                                                                                | tests/test_db.py、tests/test_e2e.py                                                                                              |
| R §7.6、R §11.5                                                                                                                            | AC-020 | TC-034、TC-037、TC-039                         | FR-013                                         | 履歴・検索・照合サービス、本番UI実装                                                                                                                                | tests/test_web_ui.py                                                                                                             |
| R §7.7–7.8、R §10.3                                                                                                                        | AC-021 | TC-010、TC-016、TC-032、TC-042、TC-056、TC-060 | FR-007                                         | 設定・保存基盤、コメントSRT、実TikTok CLI実証                                                                                                                       | tests/test_storage.py、tests/test_e2e.py、tests/test_srt.py                                                                      |
| R §11.1、U §3–4                                                                                                                            | AC-022 | TC-034                                         | FR-011                                         | 本番UI実装、実ブラウザ試験                                                                                                                                          | tests/test_web_ui.py／実行証跡                                                                                                   |
| R §2.2、R §13.1                                                                                                                            | AC-023 | TC-034、TC-051、TC-052                         | FR-015                                         | HTTP API、CLI・WSL2運用                                                                                                                                             | tests/test_api.py、tests/test_operations.py                                                                                      |
| R §4.1.1、R §5.1、R §11.3                                                                                                                  | AC-024 | TC-001、TC-002、TC-037                         | FR-001                                         | 配信者・保持・削除基盤、本番UI実装                                                                                                                                  | tests/test_web_ui.py／実行証跡                                                                                                   |
| R §6.4、R §11.2–11.4                                                                                                                       | AC-025 | TC-034、TC-041                                 | FR-011                                         | 履歴・検索・照合サービス、本番UI実装                                                                                                                                | tests/test_web_ui.py、tests/test_e2e.py                                                                                          |
| R §7.5、R §11.2–11.5                                                                                                                       | AC-026 | TC-027、TC-034、TC-039                         | FR-011                                         | コメント保存、本番UI実装                                                                                                                                            | tests/test_comments.py、tests/test_e2e.py                                                                                        |
| R §11.4                                                                                                                                    | AC-027 | TC-034、TC-039                                 | FR-013                                         | 履歴・検索・照合サービス、本番UI実装                                                                                                                                | tests/test_web_ui.py                                                                                                             |
| R §11.4、R §12、R §13.5                                                                                                                    | AC-028 | TC-031                                         | FR-015                                         | HTTP API                                                                                                                                                            | tests/test_api.py                                                                                                                |
| R §7.6、R §11.5                                                                                                                            | AC-029 | TC-034、TC-037、TC-039                         | FR-013                                         | 履歴・検索・照合サービス、本番UI実装                                                                                                                                | tests/test_web_ui.py                                                                                                             |
| R §7.4–7.5、R §11.5                                                                                                                        | AC-030 | TC-019、TC-024、TC-026、TC-042、TC-045         | FR-006                                         | TikTok連携、コメント保存、本番UI実装                                                                                                                                | tests/test_comments.py、tests/test_web_ui.py                                                                                     |
| R §2.2、R §5.2、R §10.2、R §11.1、R §11.8                                                                                                  | AC-031 | TC-011、TC-023、TC-030、TC-040、TC-070         | FR-009、FR-011、FR-015                         | 設定・保存基盤、本番UI実装、HTTP API、CLI・WSL2運用                                                                                                                 | tests/test_web_settings.py                                                                                                       |
| R §8、R §11.4                                                                                                                              | AC-032 | TC-039、TC-041                                 | FR-013                                         | 履歴・検索・照合サービス、本番UI実装                                                                                                                                | tests/test_web_ui.py                                                                                                             |
| R §11.6–11.7、R §14.3                                                                                                                      | AC-033 | TC-034、TC-035、TC-036                         | FR-014                                         | ログ・診断基盤、本番UI実装                                                                                                                                          | tests/test_web_ui.py、tests/test_logs.py                                                                                         |
| R §6.4、R §7.8、R §10.5、R §11.4                                                                                                           | AC-034 | TC-041、TC-042、TC-064                         | FR-013                                         | 録画・確定・復旧、コメントSRT、履歴・検索・照合サービス                                                                                                             | tests/test_web_ui.py、tests/test_recording.py、tests/test_srt.py                                                                 |
| R §11.7、R §14.3                                                                                                                           | AC-035 | TC-012、TC-013、TC-035、TC-036、TC-061         | FR-014                                         | ログ・診断基盤、本番UI実装                                                                                                                                          | tests/test_logs.py、tests/test_web_ui.py                                                                                         |
| R §11.1、R §11.7、U §5                                                                                                                     | AC-036 | TC-036、TC-040、TC-061                         | FR-011                                         | ログ・診断基盤、本番UI実装、実ブラウザ試験                                                                                                                          | tests/test_logs.py、tests/test_web_ui.py                                                                                         |
| R §11.1、U §2、U §5–6、U §14–16                                                                                                            | AC-037 | TC-040                                         | FR-012                                         | 本番UI実装、実ブラウザ試験                                                                                                                                          | tests/test_web_ui.py、tests/test_web_settings.py                                                                                 |
| R §2.1–2.3、R §15.2                                                                                                                        | AC-038 | TC-051                                         | FR-016                                         | 環境構築、CLI・WSL2運用、導入・実機運用検証                                                                                                                         | tests/test_operations.py                                                                                                         |
| R §2.2、D §9.9、R §11.8、R §15.2                                                                                                           | AC-039 | TC-051、TC-052、TC-070                         | FR-015                                         | 本番UI実装、HTTP API、CLI・WSL2運用                                                                                                                                 | tests/test_operations.py、tests/test_web_settings.py／実行証跡                                                                   |
| R §2.1、R §14.2、R §15.2                                                                                                                   | AC-040 | TC-051                                         | FR-016                                         | CLI・WSL2運用、導入・実機運用検証                                                                                                                                   | tests/test_operations.py                                                                                                         |
| D §9、R §10、R §15.2                                                                                                                       | AC-041 | TC-028、TC-029、TC-046                         | FR-008                                         | DB基盤、録画・確定・復旧、CLI・WSL2運用                                                                                                                             | tests/test_db.py、tests/test_e2e.py、tests/test_recovery.py                                                                      |
| R §14.2、R §15.2                                                                                                                           | AC-042 | TC-050、TC-051、TC-053、TC-059                 | FR-016                                         | 録画・確定・復旧、CLI・WSL2運用、導入・実機運用検証                                                                                                                 | tests/test_recovery.py、tests/test_operations.py／実行証跡                                                                       |
| R §14.1、R §14.3                                                                                                                           | AC-043 | TC-043、TC-044、TC-045、TC-048、TC-061、TC-062 | FR-014                                         | ログ・診断基盤、監視・ジョブ制御、録画・確定・復旧                                                                                                                  | tests/test_logs.py、tests/test_recording.py、tests/test_monitoring.py                                                            |
| R §10.4、R §14.1                                                                                                                           | AC-044 | TC-048、TC-049                                 | FR-009                                         | 設定・保存基盤、録画・確定・復旧                                                                                                                                    | tests/test_storage.py、tests/test_web_settings.py                                                                                |
| R §4、R §7.1、R §14.1                                                                                                                      | AC-045 | TC-019、TC-027、TC-033、TC-045                 | FR-018                                         | TikTok連携、実TikTok CLI実証、ツイキャス連携、導入・実機運用検証                                                                                                    | tests/test_e2e.py、tests/test_twitcasting.py、tests/test_comments.py                                                             |
| R §12、R §13.5                                                                                                                             | AC-046 | TC-031、TC-034                                 | FR-015                                         | 本番UI実装、HTTP API                                                                                                                                                | tests/test_api.py、tests/test_web_ui.py                                                                                          |
| R §12                                                                                                                                      | AC-047 | TC-031、TC-034                                 | FR-015                                         | 本番UI実装、HTTP API                                                                                                                                                | tests/test_api.py、tests/test_web_ui.py                                                                                          |
| R §8.2、R §12                                                                                                                              | AC-048 | TC-031、TC-034                                 | FR-015                                         | 本番UI実装、HTTP API                                                                                                                                                | tests/test_api.py、tests/test_web_ui.py                                                                                          |
| R §2.2、R §12、R §13.1                                                                                                                     | AC-049 | TC-031、TC-052                                 | FR-015                                         | HTTP API、CLI・WSL2運用                                                                                                                                             | tests/test_api.py、tests/test_operations.py                                                                                      |
| R §10.1–10.2                                                                                                                               | AC-050 | TC-030、TC-049                                 | FR-009                                         | 設定・保存基盤、CLI・WSL2運用                                                                                                                                       | tests/test_storage.py、tests/test_web_settings.py                                                                                |
| R §10.2                                                                                                                                    | AC-051 | TC-017、TC-057、TC-058                         | FR-008                                         | DB基盤、設定・保存基盤                                                                                                                                              | tests/test_storage.py                                                                                                            |
| R §11.7、R §13.3、R §14.3                                                                                                                  | AC-052 | TC-013、TC-014、TC-036、TC-061、TC-062         | FR-014                                         | ログ・診断基盤、HTTP API                                                                                                                                            | tests/test_logs.py、tests/test_web_ui.py                                                                                         |
| R §13.6、R §15.2、R §15.2.1                                                                                                                | AC-053 | TC-038、TC-046、TC-047、TC-055                 | FR-010、FR-016、FR-038                         | 配信者・保持・削除基盤、CLI・WSL2運用、導入・実機運用検証                                                                                                           | tests/test_retention.py、tests/test_operations.py                                                                                |
| R §17、R §23                                                                                                                               | AC-054 | TC-068                                         | FR-017                                         | 統合時簡易照合                                                                                                                                                      | 簡易追跡・Feature照合                                                                                                            |
| R §19、R §22、C（開発憲法）                                                                                                                | AC-055 | TC-069                                         | FR-017                                         | 仕様・追跡設計、統合時簡易照合                                                                                                                                      | 簡易追跡・Feature照合                                                                                                            |
| R §4.2.3–4.2.4、R §6.5.1、R §6.3–6.6                                                                                                       | AC-056 | TC-071、TC-072                                 | FR-005、FR-008                                 | 録画・確定・復旧、ツイキャス連携、実TikTok CLI実証                                                                                                                  | tests/test_recording.py、tests/test_twitcasting.py、tests/test_recovery.py                                                       |
| R §4.1.3、R §7.4、R §4.2.2                                                                                                                 | AC-057 | TC-073、TC-074                                 | FR-006                                         | TikTok連携、コメント保存、ツイキャス連携                                                                                                                            | tests/test_comments.py、tests/test_e2e.py、tests/test_twitcasting.py                                                             |
| R §10.4.1、R §13.6、R §10.5、R §11.6                                                                                                       | AC-058 | TC-075、TC-076                                 | FR-010、FR-009、FR-013                         | 配信者・保持・削除基盤、SRT競合試験、履歴・検索・照合サービス、システム診断UI                                                                                       | tests/test_retention.py、tests/test_storage.py、tests/test_srt.py、tests/test_web_ui.py                                          |
| R §11.1–11.1.1、R §11.1.1、R §11.8                                                                                                         | AC-059 | TC-077、TC-078                                 | FR-011、FR-012、FR-015                         | 本番UI実装、実ブラウザ試験、HTTP API                                                                                                                                | tests/test_web_ui.py、tests/test_web_settings.py、tests/test_api.py                                                              |
| R §14.2.1、R §6.1、R §8.3、R §14.4                                                                                                         | AC-060 | TC-079、TC-080                                 | FR-003、FR-004、FR-016、FR-005、FR-006、FR-010 | 監視・ジョブ制御、録画・確定・復旧、コメント保存、CLI・WSL2運用、コメントSRT、配信者・保持・削除基盤                                                                | tests/test_recovery.py、tests/test_operations.py、tests/test_retention.py、tests/test_srt.py                                     |
| R §7.9、R §14、R §16                                                                                                                       | AC-061 | TC-081、TC-083                                 | FR-019                                         | 音声キュー・推論子、字幕ローカル実証                                                                                                                                | tests/test_speech.py                                                                                                             |
| R §7.9、R §10.3                                                                                                                            | AC-062 | TC-082、TC-083、TC-085                         | FR-019                                         | 音声認識・SRT、字幕ローカル実証                                                                                                                                     | tests/test_speech.py                                                                                                             |
| R §7.10、R §10.3                                                                                                                           | AC-063 | TC-084、TC-085                                 | FR-020                                         | ASS生成、字幕ローカル実証                                                                                                                                           | tests/test_ass.py                                                                                                                |
| R §10.4、R §13.6                                                                                                                           | AC-064 | TC-085、TC-086                                 | FR-020                                         | 字幕ローカル実証                                                                                                                                                    | tests/test_retention.py                                                                                                          |
| R §11.9、R §12                                                                                                                             | AC-065 | TC-087                                         | FR-021                                         | 本番UI実装、HTTP API、実ブラウザ試験                                                                                                                                | tests/test_web_ui.py                                                                                                             |
| R §7.9、R §16、R §17                                                                                                                       | AC-066 | TC-088                                         | FR-019、FR-020                                 | 実GPU・CPU検証                                                                                                                                                      | tests/test_speech.py                                                                                                             |
| R §11.8.1–11.8.3、R §2.2、R §10.2                                                                                                          | AC-067 | TC-089、TC-090                                 | FR-022                                         | DB基盤、設定・保存基盤、本番UI実装、HTTP API、CLI・WSL2運用                                                                                                         | tests/test_web_settings.py、tests/test_api.py、tests/test_operations.py                                                          |
| R §4.2.5、R §13.2.1、R §13.3.1                                                                                                             | AC-068 | TC-091、TC-092                                 | FR-023                                         | 監視・ジョブ制御、TikTok連携、録画・確定・復旧、コメント保存、ツイキャス連携、HTTP API、実ブラウザ試験                                                              | tests/test_api.py、tests/test_monitoring.py、tests/test_e2e.py                                                                   |
| R §5.1、R §11.3                                                                                                                            | AC-069 | TC-093                                         | FR-024                                         | DB基盤、配信者・保持・削除基盤、本番UI実装、HTTP API、実ブラウザ試験                                                                                                | tests/test_db.py、tests/test_api.py、tests/test_web_ui.py                                                                        |
| R §7.6、R §11.5                                                                                                                            | AC-070 | TC-094                                         | FR-025                                         | 履歴・検索・照合サービス、本番UI実装、HTTP API、実ブラウザ試験                                                                                                      | tests/test_api.py、tests/test_web_ui.py                                                                                          |
| R §7.5.1、R §7.7–7.10                                                                                                                      | AC-071 | TC-095、TC-096                                 | FR-026                                         | DB基盤、コメント保存、コメントSRT、履歴・検索・照合サービス、ASS生成                                                                                                | tests/test_comments.py、tests/test_srt.py、tests/test_ass.py                                                                     |
| R §4.1.2、R §4.2.4、R §5.2、R §6.2、R §6.5                                                                                                 | AC-072 | TC-097                                         | FR-027                                         | 事前調査、監視・ジョブ制御、TikTok連携、録画・確定・復旧、実TikTok CLI実証、ツイキャス連携、外部・実機証跡                                                          | tests/test_monitoring.py、tests/test_recording.py、tests/test_twitcasting.py／実配信証跡                                         |
| R §14.3–14.5、R §11.6–11.7                                                                                                                 | AC-073 | TC-098、TC-099                                 | FR-028                                         | DB基盤、ログ・診断基盤、録画・確定・復旧、CLI・WSL2運用、システム診断UI、音声キュー・推論子、実GPU・CPU検証                                                         | tests/test_operations.py、tests/test_logs.py、tests/test_recording.py、tests/test_speech.py                                      |
| R §11.2.1、R §10.4                                                                                                                         | AC-074 | TC-100                                         | FR-029                                         | 履歴・検索・照合サービス、本番UI実装、実ブラウザ試験、システム診断UI                                                                                                | tests/test_web_ui.py、tests/test_api.py、tests/test_operations.py                                                                |
| R §4.1.4.1; D §15.4.1                                                                                                                      | AC-075 | TC-101                                         | FR-030                                         | 事前調査、TikTok連携、録画・確定・復旧、実TikTok CLI実証                                                                                                            | tests/test_recording.py、tests/test_monitoring.py／実配信                                                                        |
| R §5.1.1; D §15.2.1; U §8.2                                                                                                                | AC-076 | TC-102                                         | FR-031                                         | 事前調査、DB基盤、設定・保存基盤、配信者・保持・削除基盤、本番UI実装、HTTP API、実ブラウザ試験                                                                      | tests/test_api.py、tests/test_storage.py、tests/test_web_ui.py                                                                   |
| R §10.5.1, §11.6.1; D §15.6; U §11.4                                                                                                       | AC-077 | TC-103                                         | FR-032                                         | DB基盤、履歴・検索・照合サービス、HTTP API、実ブラウザ試験、システム診断UI                                                                                          | tests/test_storage.py、tests/test_api.py、tests/test_web_ui.py                                                                   |
| R §6.5.2, §14.2.1; D §9.3, §9.5, §15.7                                                                                                     | AC-078 | TC-104                                         | FR-033                                         | DB基盤、監視・ジョブ制御、録画・確定・復旧、コメント保存、CLI・WSL2運用                                                                                             | tests/test_db.py、tests/test_recording.py、tests/test_comments.py、tests/test_recovery.py                                        |
| R §14.5.1; D §15.5.1; U §11.3                                                                                                              | AC-079 | TC-105                                         | FR-034                                         | ログ・診断基盤、監視・ジョブ制御、録画・確定・復旧、コメント保存、CLI・WSL2運用、システム診断UI、音声キュー・推論子                                                 | tests/test_operations.py、tests/test_monitoring.py、tests/test_web_ui.py                                                         |
| R §4.1.2.1, §4.1.4, §4.2.3–4.2.4, §6.2; D §15.4.2                                                                                          | AC-080 | TC-106                                         | FR-035                                         | 事前調査、TikTok連携、録画・確定・復旧、実TikTok CLI実証、ツイキャス連携、外部・実機証跡                                                                            | research.md／実配信、tests/test_monitoring.py、tests/test_recording.py                                                           |
| R §13.2.2; D §15.8; U §6.2                                                                                                                 | AC-081 | TC-107、TC-108                                 | FR-036                                         | 環境構築、DB基盤、履歴・検索・照合サービス、HTTP API、実ブラウザ試験、CLI・WSL2運用                                                                                 | tests/test_api.py、tests/test_web_ui.py、tests/test_operations.py                                                                |
| R §17.5, §19, §23; D §12; U §15–16                                                                                                         | AC-082 | TC-109                                         | FR-037                                         | 独立実装・機能別検証・UI3案提示と利用者選択の記録（開発チェック）                                                                                                   | 簡易レビュー                                                                                                                     |
| R §7.9, §10.1〜2, §11.8.1〜3, §13.7, §14.6, §15.2.1, §17; D §14.1, §15.10, §16.2, §17, §20; U §3.5, §14.4〜14.5, §16.9; AGENTS.md 証跡境界 | AC-083 | TC-110、TC-111                                 | FR-038                                         | 最小隔離・段階的テスト基盤、開発者モード・共通診断基盤、設定・保存基盤、ログ・診断基盤、HTTP API、CLI・WSL2運用、開発専用UI検証基盤、実ブラウザ試験、統合時簡易照合 | tests/test_operations.py、tests/test_api.py、tests/test_web_ui.py、tests/conftest.py／隔離シナリオ・隔離・既定拒否の代表試験結果 |

### 21.2 固定テストの逆引き

| §17 ID | 優先度 | 原本の検証対象                                                                                                                                                                                                                                                                                       | 関連AC                                                                                                 | FR                             | 工程責務                                                                                                                      | 証跡・検証先                                                                                                                        |
| ------ | ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| TC-001 | A      | 配信者登録                                                                                                                                                                                                                                                                                           | AC-003、AC-024                                                                                         | FR-001                         | 配信者・保持・削除基盤                                                                                                        | tests/test_web_ui.py、tests/test_monitoring.py                                                                                      |
| TC-002 | B      | ID正規化                                                                                                                                                                                                                                                                                             | AC-003、AC-024                                                                                         | FR-001                         | 配信者・保持・削除基盤                                                                                                        | tests/test_monitoring.py、tests/test_storage.py                                                                                     |
| TC-003 | A      | URL検証                                                                                                                                                                                                                                                                                              | 横断条件（原本の固定テスト）                                                                           | FR-001                         | 配信者・保持・削除基盤                                                                                                        | tests/test_monitoring.py、tests/test_twitcasting.py                                                                                 |
| TC-004 | A      | 配信状態判定                                                                                                                                                                                                                                                                                         | AC-004                                                                                                 | FR-004                         | 監視・ジョブ制御                                                                                                              | tests/test_monitoring.py                                                                                                            |
| TC-005 | A      | 録画状態遷移（手動停止・同一配信の自動再開抑止を含む）                                                                                                                                                                                                                                               | 横断条件（原本の固定テスト）                                                                           | FR-004                         | 監視・ジョブ制御                                                                                                              | tests/test_monitoring.py、tests/test_e2e.py                                                                                         |
| TC-006 | A      | コメントデータ変換                                                                                                                                                                                                                                                                                   | AC-016、AC-017                                                                                         | FR-006                         | コメント保存                                                                                                                  | tests/test_comments.py、tests/test_twitcasting.py                                                                                   |
| TC-007 | A      | 重複排除                                                                                                                                                                                                                                                                                             | AC-019                                                                                                 | FR-006                         | コメント保存                                                                                                                  | tests/test_comments.py                                                                                                              |
| TC-008 | A      | R §10.3の5要素順のファイル名生成（IANAタイムゾーン、開始日時欠落時の検知日時、タイトルの日本語・禁止文字・空欄・長さ制限、同一配信内のタイトル／日時固定と再起動・設定変更後の継続採番、共通配信者／日付フォルダ、別サービス間の上書き防止、全字幕のstem一致）                                       | AC-010                                                                                                 | FR-008                         | 設定・保存基盤                                                                                                                | tests/test_storage.py、tests/test_recording.py                                                                                      |
| TC-009 | B      | 時刻計算                                                                                                                                                                                                                                                                                             | AC-018                                                                                                 | FR-006                         | コメント保存                                                                                                                  | tests/test_comments.py、tests/test_srt.py                                                                                           |
| TC-010 | A      | 録画セグメント単位のコメントSRT生成・§10.3の命名と時刻補正                                                                                                                                                                                                                                           | AC-018、AC-021                                                                                         | FR-007                         | コメントSRT                                                                                                                   | tests/test_srt.py                                                                                                                   |
| TC-011 | B      | 設定値検証（サービス別監視頻度の範囲・小数値・変更時の即時反映）                                                                                                                                                                                                                                     | AC-001、AC-002、AC-031                                                                                 | FR-004                         | 監視・ジョブ制御                                                                                                              | tests/test_web_settings.py、tests/test_monitoring.py                                                                                |
| TC-012 | B      | ログの時刻範囲・重要度・出力元・キーワード絞り込み、降順整列・ページング、ログの長文／複数行の安全な表示                                                                                                                                                                                             | AC-035                                                                                                 | FR-014                         | ログ・診断基盤                                                                                                                | tests/test_logs.py                                                                                                                  |
| TC-013 | A      | ログの秘密情報マスキング、許可外パスの読取防止、存在しない相関識別子での誤リンク防止                                                                                                                                                                                                                 | AC-035、AC-052                                                                                         | FR-014                         | ログ・診断基盤                                                                                                                | tests/test_logs.py、tests/test_web_ui.py                                                                                            |
| TC-014 | B      | 保存時に漏れた例外文・古いログ・スタックトレースの秘密情報を検索結果／詳細／コピー前に再マスキングし、非秘匿本文を保持すること                                                                                                                                                                       | AC-052                                                                                                 | FR-014                         | ログ・診断基盤                                                                                                                | tests/test_logs.py                                                                                                                  |
| TC-015 | B      | コメント保存期限の`0`／正整数の検証、収束日時基準の対象判定、論理削除済み配信者の再登録と既存一意キーの再利用                                                                                                                                                                                        | 横断条件（原本の固定テスト）                                                                           | FR-010                         | 配信者・保持・削除基盤                                                                                                        | tests/test_operations.py、tests/test_retention.py、tests/test_web_ui.py                                                             |
| TC-016 | A      | 動画・全字幕の正式名衝突（接尾辞込みの長さ・Windows同名性を含む）時のセッション識別子固定・衝突追加時の拒否、SRT入力版数変更に伴う公開前・公開後の再照合                                                                                                                                             | AC-021                                                                                                 | FR-007                         | 設定・保存基盤、コメントSRT                                                                                                   | tests/test_storage.py、tests/test_recording.py、tests/test_srt.py                                                                   |
| TC-017 | B      | 異なる正規化済みIDのWindows安全名が一致する場合の安定したフォルダ識別子・永続マッピング、同じ正規化済みIDのサービス横断でのフォルダ共有、管理外フォルダの誤取込防止                                                                                                                                  | AC-051                                                                                                 | FR-008                         | 設定・保存基盤                                                                                                                | tests/test_storage.py                                                                                                               |
| TC-018 | A      | TikTokのHTTP応答（403/429/5xx・空本文・非JSON）と通信障害をOFFLINEへ誤変換しないこと、未知状態で稼働中セッションを終了させないこと                                                                                                                                                                   | AC-001、AC-004                                                                                         | FR-004                         | 監視・ジョブ制御                                                                                                              | tests/test_monitoring.py                                                                                                            |
| TC-019 | B      | TikTokLiveの受信タスク寿命・遅延コメント・停止時キュードレイン・例外伝播・子タスク解放                                                                                                                                                                                                               | AC-014、AC-030、AC-045                                                                                 | FR-006                         | コメント保存                                                                                                                  | tests/test_comments.py、tests/test_monitoring.py                                                                                    |
| TC-020 | B      | 不連続タグの有無、初期化情報の切り替わり、既知のプリロール素材の繰り返しを使った識別、確認済みプリロールの保存・採番・履歴からの除外と、**本編冒頭を誤って破棄しない**否定ケース                                                                                                                     | AC-007                                                                                                 | FR-005、FR-018                 | 録画・確定・復旧、ツイキャス連携                                                                                              | tests/test_recording.py、tests/test_twitcasting.py                                                                                  |
| TC-021 | A      | FLV映像コーデック不明・0バイトMP4の拒否、ffmpeg/ffprobe実体の選択、確定後メディア長の再検証                                                                                                                                                                                                          | AC-013                                                                                                 | FR-005                         | 録画・確定・復旧                                                                                                              | tests/test_recording.py                                                                                                             |
| TC-022 | A      | 空試行の連番非消費、複数ジョブでの `UNIQUE(stream_session_id, segment_number)`・複合外部キー・同時採番予約、予約後クラッシュ時の番号非再利用と欠損保存                                                                                                                                               | AC-010                                                                                                 | FR-005                         | 録画・確定・復旧                                                                                                              | tests/test_db.py、tests/test_recording.py、tests/test_recovery.py                                                                   |
| TC-023 | A      | 配信開始から録画開始まで（サービス別監視頻度の変更・再起動なしの反映・TikTok最大10回／分／ツイキャス初期値10回／分と最大60回／分の独立監視枠を検証）                                                                                                                                                 | AC-001、AC-002、AC-004、AC-006、AC-008、AC-031                                                         | FR-004                         | 監視・ジョブ制御                                                                                                              | tests/test_e2e.py、tests/test_monitoring.py                                                                                         |
| TC-024 | A      | 配信開始からコメント収集開始まで                                                                                                                                                                                                                                                                     | AC-014、AC-015、AC-030                                                                                 | FR-006                         | コメント保存                                                                                                                  | tests/test_e2e.py                                                                                                                   |
| TC-025 | A      | 配信終了時の録画停止                                                                                                                                                                                                                                                                                 | AC-004                                                                                                 | FR-004                         | 監視・ジョブ制御                                                                                                              | tests/test_e2e.py                                                                                                                   |
| TC-026 | A      | 配信終了時のコメント収集停止                                                                                                                                                                                                                                                                         | AC-030                                                                                                 | FR-004                         | 監視・ジョブ制御                                                                                                              | tests/test_e2e.py                                                                                                                   |
| TC-027 | A      | 録画とコメント収集の独立動作（監視OFF・録画OFF・配信者別コメントOFF・全体コメントOFF／復帰・手動停止優先の組合せを含む）                                                                                                                                                                             | AC-005、AC-026、AC-045                                                                                 | FR-004                         | 監視・ジョブ制御                                                                                                              | tests/test_e2e.py、tests/test_monitoring.py                                                                                         |
| TC-028 | A      | SQLiteへの保存                                                                                                                                                                                                                                                                                       | AC-016、AC-017、AC-018、AC-019、AC-041                                                                 | FR-006                         | コメント保存                                                                                                                  | tests/test_db.py、tests/test_comments.py                                                                                            |
| TC-029 | A      | Windows側保存先へのファイル確定（同一ボリューム内の一時ファイルから）・ディレクトリ作成                                                                                                                                                                                                              | AC-006、AC-007、AC-010、AC-041                                                                         | FR-008                         | 設定・保存基盤                                                                                                                | tests/test_recording.py、tests/test_storage.py                                                                                      |
| TC-030 | A      | 録画ルート既定 `/mnt/d/LiveRecorder`、設定画面からの変更・永続化、DB＞環境変数＞既定値の優先順と既存ファイルの非移動を確認し、DBの保存済み設定で環境変数が上書きされるケースを含め、他のアプリケーション・別インスタンスの実効ルートとの同一／親子衝突を起動前に検出し、録画を起動せず警告すること   | AC-031、AC-050                                                                                         | FR-008、FR-009                 | 設定・保存基盤、本番UI実装、HTTP API                                                                                          | tests/test_storage.py、tests/test_web_settings.py                                                                                   |
| TC-031 | A      | 録画動画・字幕の取得リンクが画面に無く、確定済みファイルが旧取得URLへの直接リクエストでも返らないこと                                                                                                                                                                                                | AC-028、AC-046、AC-047、AC-048、AC-049                                                                 | FR-011                         | 本番UI実装                                                                                                                    | tests/test_api.py                                                                                                                   |
| TC-032 | A      | 録画完了時のセグメント別SRT自動生成（PARTIAL／CANCELLED含む）・手動再生成とSQLiteの元コメント保持                                                                                                                                                                                                    | AC-021                                                                                                 | FR-007                         | コメントSRT                                                                                                                   | tests/test_e2e.py、tests/test_api.py、tests/test_srt.py                                                                             |
| TC-033 | B      | ツイキャスBasic認証、コメント取得優先のAPI残枠制御、匿名番号WebSocketとAPIコメントの一意照合・曖昧照合時のNULL保持・重複排除                                                                                                                                                                         | AC-002、AC-015、AC-016、AC-045                                                                         | FR-018                         | ツイキャス連携                                                                                                                | tests/test_twitcasting.py                                                                                                           |
| TC-034 | A      | **主要8画面**（`/`・`/streamers`・`/recordings`・`/comments`・`/speech`・`/system`・`/logs`・`/settings`）のURL・サイドバー表示名・表示順と、主要な一覧／下位詳細の遷移                                                                                                                              | AC-005、AC-011、AC-020、AC-022、AC-023、AC-025、AC-026、AC-027、AC-029、AC-033、AC-046、AC-047、AC-048 | FR-011                         | 本番UI実装                                                                                                                    | tests/test_web_ui.py                                                                                                                |
| TC-035 | C      | システム状態の警告からログへの条件引継ぎ、ログから録画履歴／配信者一覧への遷移、検索条件とページ位置の維持（相関情報が存在する場合）                                                                                                                                                                 | AC-033、AC-035                                                                                         | FR-014                         | ログ・診断基盤                                                                                                                | tests/test_web_ui.py                                                                                                                |
| TC-036 | B      | 構造化ログの読取・検索・詳細展開・コピー表示と、ログが0件／取得不能／古い情報の状態、ローテーションをまたぐ読取（既存読取器がない環境からもバックエンド実装）                                                                                                                                        | AC-033、AC-035、AC-036、AC-052                                                                         | FR-014                         | ログ・診断基盤                                                                                                                | tests/test_logs.py、tests/test_web_ui.py                                                                                            |
| TC-037 | B      | 論理削除→同一サービス・ID再登録→旧履歴維持・重複行なし・明示した3設定のみ復帰、コメント期限削除後のコメント画面／履歴／SRT状態表示                                                                                                                                                                   | AC-003、AC-005、AC-020、AC-024、AC-029                                                                 | FR-001                         | 配信者・保持・削除基盤                                                                                                        | tests/test_web_ui.py、tests/test_retention.py                                                                                       |
| TC-038 | B      | コメント期限削除後も旧SQLiteバックアップには本文が残り得ることをCLI／UIで説明し、通常DBとSRTの削除結果をバックアップ削除成功と誤表示しないこと                                                                                                                                                       | AC-053                                                                                                 | FR-010                         | 配信者・保持・削除基盤                                                                                                        | tests/test_operations.py、tests/test_retention.py                                                                                   |
| TC-039 | B      | コメントだけ取得されたセッションが録画履歴から探せてコメントへ遷移でき、録画セグメントなしのSRT操作が表示されないこと                                                                                                                                                                                | AC-011、AC-020、AC-026、AC-027、AC-029、AC-032                                                         | FR-011                         | 本番UI実装                                                                                                                    | tests/test_web_ui.py                                                                                                                |
| TC-040 | B      | 設定のフォント2種類・ライト系単一テーマ、画面共通の最終取得日時、デフォルト5秒の自動部分更新、設定変更の永続化と再起動なしの反映、不正値拒否、更新の重複抑止と外部監視リクエスト非発生、入力・選択・ログ閲覧を妨げないこと                                                                           | AC-031、AC-036、AC-037                                                                                 | FR-011                         | 本番UI実装、HTTP API、実ブラウザ試験                                                                                          | tests/test_web_settings.py、tests/test_web_ui.py                                                                                    |
| TC-041 | B      | 最新録画ジョブは `COMPLETED` だが過去の `FAILED`・無効／未確定セグメント・欠損があるセッションで、完了の誤表示と欠損の欠落が発生しないこと                                                                                                                                                           | AC-011、AC-012、AC-025、AC-032、AC-034                                                                 | FR-011                         | 本番UI実装                                                                                                                    | tests/test_web_ui.py                                                                                                                |
| TC-042 | B      | SRT対象コメントが0件の場合の暫定スキップ→後着コメント→入力版数更新→再生成／確定、収集未実施・失敗との差異、生成失敗と復旧・ファイル実在確認                                                                                                                                                          | AC-021、AC-030、AC-034                                                                                 | FR-007                         | コメントSRT                                                                                                                   | tests/test_srt.py                                                                                                                   |
| TC-043 | B      | ネットワーク切断                                                                                                                                                                                                                                                                                     | AC-006、AC-009、AC-043                                                                                 | FR-005                         | 録画・確定・復旧                                                                                                              | tests/test_monitoring.py、tests/test_recording.py                                                                                   |
| TC-044 | B      | 録画プロセス異常終了                                                                                                                                                                                                                                                                                 | AC-006、AC-008、AC-009、AC-043                                                                         | FR-005                         | 録画・確定・復旧                                                                                                              | tests/test_recording.py                                                                                                             |
| TC-045 | B      | コメント接続切断・Euler Stream無料枠超過とツイキャスAPI残枠不足                                                                                                                                                                                                                                      | AC-001、AC-002、AC-009、AC-014、AC-015、AC-030、AC-043、AC-045                                         | FR-006                         | コメント保存                                                                                                                  | tests/test_comments.py、tests/test_twitcasting.py                                                                                   |
| TC-046 | B      | SQLiteロック競合・書き込み失敗・安全なバックアップ                                                                                                                                                                                                                                                   | AC-041、AC-053                                                                                         | FR-016                         | CLI・WSL2運用                                                                                                                 | tests/test_operations.py、tests/test_db.py                                                                                          |
| TC-047 | B      | 停止中の手動復元、現行DB退避・整合性検査・復元未完了マーカーによる起動拒否、媒体・コメント期限・停止指示・試行／外部予算の再照合、中断時の差戻しを検証する。古いDBだけで削除・再送・予算初期化しない。                                                                                               | AC-053                                                                                                 | FR-010、FR-016、FR-038         | 配信者・保持・削除基盤、CLI・WSL2運用、復元試験                                                                               | tests/test_retention.py、tests/test_operations.py                                                                                   |
| TC-048 | A      | Windows側保存先の容量不足・ドライブ未接続・NTFSアクセス拒否                                                                                                                                                                                                                                          | AC-043、AC-044                                                                                         | FR-008                         | 設定・保存基盤                                                                                                                | tests/test_storage.py、tests/test_web_settings.py                                                                                   |
| TC-049 | B      | 実効パスの同一・親子関係、大小文字差、Windows実体パス差異、保存済みDB設定と環境変数の優先順位による録画先の誤共有を防止すること                                                                                                                                                                      | AC-044、AC-050                                                                                         | FR-008                         | 設定・保存基盤                                                                                                                | tests/test_storage.py                                                                                                               |
| TC-050 | B      | Worker再起動                                                                                                                                                                                                                                                                                         | AC-042                                                                                                 | FR-004                         | 監視・ジョブ制御                                                                                                              | tests/test_recovery.py、tests/test_operations.py                                                                                    |
| TC-051 | B      | Windows再起動／WSL2停止相当のプロセス再起動                                                                                                                                                                                                                                                          | AC-023、AC-038、AC-039、AC-040、AC-042                                                                 | FR-016                         | CLI・WSL2運用                                                                                                                 | tests/test_operations.py                                                                                                            |
| TC-052 | A      | 二重録画防止                                                                                                                                                                                                                                                                                         | AC-008、AC-023、AC-039、AC-049                                                                         | FR-004                         | 監視・ジョブ制御                                                                                                              | tests/test_recording.py、tests/test_operations.py                                                                                   |
| TC-053 | B      | 未確定ファイル回収                                                                                                                                                                                                                                                                                   | AC-010、AC-042                                                                                         | FR-005                         | 録画・確定・復旧                                                                                                              | tests/test_recovery.py                                                                                                              |
| TC-054 | A      | Windows側保存先で同名上書きが起きないこと。別セッション・管理外ファイルとの同名衝突、Windows大文字小文字の衝突、予約とrenameの間の競合を注入する                                                                                                                                                     | AC-010                                                                                                 | FR-008                         | 設定・保存基盤                                                                                                                | tests/test_recording.py、tests/test_storage.py                                                                                      |
| TC-055 | A      | コメント期限削除のSRT先行削除→DB削除と、両段階の強制終了・権限エラー後の再起動復旧。期限削除済みコメントやSRTが再生成されないこと                                                                                                                                                                    | AC-053                                                                                                 | FR-010                         | 配信者・保持・削除基盤                                                                                                        | tests/test_retention.py                                                                                                             |
| TC-056 | B      | SRT再生成での版数競合（公開直前／ファイル公開後〜DB確定前）、旧正常版保全、0件更新時の旧SRT除去失敗、再起動時の版数照合                                                                                                                                                                              | AC-021                                                                                                 | FR-007                         | コメントSRT                                                                                                                   | tests/test_srt.py                                                                                                                   |
| TC-057 | B      | 同じ配信者IDを複数サービスで監視したとき、フォルダのみ共通になりDB上のセッションは混ざらないこと                                                                                                                                                                                                     | AC-051                                                                                                 | FR-008                         | 設定・保存基盤                                                                                                                | tests/test_storage.py                                                                                                               |
| TC-058 | B      | 異なるIDのフォルダ安全名衝突（Windows予約名、記号置換、大小文字・Unicode・末尾ドット／空白）と追加登録・再起動後の既存フォルダ誤共有／改名防止                                                                                                                                                       | AC-051                                                                                                 | FR-008                         | 設定・保存基盤                                                                                                                | tests/test_storage.py                                                                                                               |
| TC-059 | B      | アプリ強制終了後のSQLite整合性・録画セグメントの回収と状態再照合（正式ファイルへのリネーム直前／直後にDB更新が未完了のケースを含む）                                                                                                                                                                 | AC-010、AC-042                                                                                         | FR-005                         | 録画・確定・復旧                                                                                                              | tests/test_recovery.py                                                                                                              |
| TC-060 | A      | SRTの同期不能・近似同期・遅延コメント・自動生成失敗時に、動画とコメント正本が失われないこと                                                                                                                                                                                                          | AC-021                                                                                                 | FR-007                         | コメントSRT                                                                                                                   | tests/test_srt.py、tests/test_retention.py                                                                                          |
| TC-061 | B      | ログ読取中のローテーション・ファイル不在・壊れたログ行・巨大なログ行・アクセス拒否でも、管理画面や録画ジョブ全体が停止しないこと                                                                                                                                                                     | AC-035、AC-036、AC-043、AC-052                                                                         | FR-014                         | ログ・診断基盤                                                                                                                | tests/test_logs.py                                                                                                                  |
| TC-062 | A      | ログ出力・ログ画面からAPIキー等が漏えいしないこと                                                                                                                                                                                                                                                    | AC-043、AC-052                                                                                         | FR-014                         | ログ・診断基盤                                                                                                                | tests/test_logs.py                                                                                                                  |
| TC-063 | B      | `ENDING` の猶予中に録画を誤停止しないこと、タイムアウト取消・子プロセス回収後の一時ファイル確定、異常 `CANCELLED` / `pending` の復旧                                                                                                                                                                 | AC-009                                                                                                 | FR-004                         | 監視・ジョブ制御                                                                                                              | tests/test_recording.py、tests/test_recovery.py                                                                                     |
| TC-064 | B      | コーデック不明な有意の一時ファイルを検証前に成功扱いせず保全し、0バイトMP4・DB上の `valid`・実ファイル不在の矛盾を検出すること                                                                                                                                                                       | AC-012、AC-013、AC-034                                                                                 | FR-005                         | 録画・確定・復旧                                                                                                              | tests/test_recording.py、tests/test_recovery.py                                                                                     |
| TC-065 | B      | 録画監視の不明状態が続く間の回復・明示OFFLINE→ENDING→再確認・終了未確認での有限収束と、同じ配信での重複ジョブ生成防止                                                                                                                                                                                | AC-004                                                                                                 | FR-004                         | 監視・ジョブ制御                                                                                                              | tests/test_monitoring.py                                                                                                            |
| TC-066 | B      | 削除処理中の強制終了・一部ファイル削除・自動削除・履歴付き配信者削除を模擬し、孤立ファイル・誤成功・コメント／欠損の意図しない消失がないこと 終了未確認ではファイル＋履歴を拒否し、同一ID再検知で予算・停止・採番・削除状態を保全する。                                                              | 横断条件（原本の固定テスト）                                                                           | FR-010                         | 配信者・保持・削除基盤                                                                                                        | tests/test_retention.py、tests/test_web_ui.py                                                                                       |
| TC-067 | B      | ツイキャスの短いプリロールのみで終わる場合の録画結果と、実配信でのプリロール除外、本編の先頭・末尾の収録確認                                                                                                                                                                                         | AC-007                                                                                                 | FR-005、FR-018                 | 録画・確定・復旧、ツイキャス連携                                                                                              | tests/test_recording.py、tests/test_twitcasting.py                                                                                  |
| TC-068 | B      | （開発チェック）固定AC／TCの欠落・重複・誤参照を簡易追跡表またはスクリプトで確認する。専用pytestは不要。                                                                                                                                                                                             | AC-054                                                                                                 | FR-017                         | 統合時簡易照合                                                                                                                | 簡易追跡・Feature照合                                                                                                               |
| TC-069 | B      | （開発チェック）Spec Kitの対象Featureを照合し、既存 `initial-live-recorder` の誤上書き・重複生成を防ぐ。簡易確認でよい。                                                                                                                                                                             | AC-055                                                                                                 | FR-017                         | 統合時簡易照合                                                                                                                | 簡易追跡・Feature照合                                                                                                               |
| TC-070 | B      | 固定Webポートの既定8082、設定画面での変更・永続化、現在値と次回起動値の区別、次回起動への反映、範囲外・0・非整数の拒否、使用中ポートでの起動失敗と自動代替なし、停止時CLIでの設定復旧、endpointの生存確認を検証する。                                                                                | AC-031、AC-039                                                                                         | FR-015                         | 本番UI実装、HTTP API、CLI・WSL2運用                                                                                           | tests/test_operations.py、tests/test_web_settings.py、tests/test_api.py                                                             |
| TC-071 | B      | 録画の入力停止・出力無進捗・正常な低ビットレートとバッファ・本編初期化切替を模擬し、停滞だけを回収・再接続する。公開HLSの転送先拒否、本編待ち期限、プリロールだけの試行を検証する。                                                                                                                  | AC-056                                                                                                 | FR-005                         | 録画・確定・復旧、ツイキャス連携                                                                                              | tests/test_recording.py、tests/test_twitcasting.py                                                                                  |
| TC-072 | B      | 初期取得失敗・有効メディア取得後の切断・アクセス拒否・明示停止・終了コード0で配信継続・試行上限を注入。適切な切替／同一エンジン再試行／停止、確定媒体・採番・試行別エンジンと壁時計／再生時間の記録を確認する。                                                                                      | AC-056                                                                                                 | FR-005、FR-008                 | 録画・確定・復旧、実TikTok CLI実証                                                                                            | tests/test_recording.py、tests/test_recovery.py                                                                                     |
| TC-073 | B      | TikTok接続のRoom ID一致／不一致／未確認、再接続中の旧世代イベント、保留キュー超過と確認失敗を注入し、別セッションへの誤保存なし、欠損と録画独立を確認する。                                                                                                                                          | AC-057                                                                                                 | FR-006                         | TikTok連携、コメント保存                                                                                                      | tests/test_comments.py、tests/test_e2e.py                                                                                           |
| TC-074 | B      | ツイキャスの初回0件後の大量投稿、50件飽和、短いページ、疎なID、削除による件数減少、ページ重複／逆転、途中失敗、枠不足、DBコミット失敗を注入し、保存境界・重複排除・正当な連投・欠損・有限終了を確認する。                                                                                            | AC-057                                                                                                 | FR-006                         | コメント保存、ツイキャス連携                                                                                                  | tests/test_twitcasting.py、tests/test_comments.py                                                                                   |
| TC-075 | A      | 削除失敗と再起動、同名別世代への置換、照合後の競合、未マウントと本当の不在、Windows実体識別、録画確定／SRT公開／期限削除との競合を注入。新しいファイルを削除せず、台帳が保留・部分失敗を保持することを確認する。                                                                                     | AC-058                                                                                                 | FR-010                         | 配信者・保持・削除基盤、SRT競合試験                                                                                           | tests/test_retention.py、tests/test_storage.py、tests/test_srt.py                                                                   |
| TC-076 | C      | 登録済み欠落・サイズ不一致・未登録・一時・検証不良・削除済み・走査中の変化／読取不能を注入。表示上限を超えても確認総数・正常総数が正しく、無断修復・削除・取込みがないことを確認する。                                                                                                               | AC-058                                                                                                 | FR-009、FR-013                 | 履歴・検索・照合サービス、システム診断UI                                                                                      | tests/test_storage.py、tests/test_web_ui.py                                                                                         |
| TC-077 | C      | 実ブラウザで定期更新応答の遅延／逆順、送信後の入力・選択・ダイアログ・ログ閲覧、検索条件変更、保存直後を再現し、状態・フォーカス・表示時点が巻き戻らないことを確認する。                                                                                                                             | AC-059                                                                                                 | FR-011、FR-012                 | 本番UI実装、実ブラウザ試験                                                                                                    | tests/test_web_ui.py                                                                                                                |
| TC-078 | C      | 非表示タブからの復帰、更新失敗と再取得、設定間隔変更、未保存設定・入力エラーの保持、重複送信抑止、録画マークの不明状態、外部サービスへの追加監視なしを実ブラウザとAPI計測で確認する。                                                                                                                | AC-059                                                                                                 | FR-011、FR-015                 | 本番UI実装、HTTP API、実ブラウザ試験                                                                                          | tests/test_web_settings.py、tests/test_web_ui.py、tests/test_api.py                                                                 |
| TC-079 | B      | 再起動時に同一LIVE・別ID・確認済み終了・UNKNOWN・終了未確認・手動停止・個別OFFを組み合わせ、既存データ保全・二重起動なし・正しい再開／保留／収束を確認する。                                                                                                                                         | AC-060                                                                                                 | FR-003、FR-004、FR-016         | 監視・ジョブ制御、録画・確定・復旧、コメント保存、CLI・WSL2運用                                                               | tests/test_recovery.py、tests/test_operations.py                                                                                    |
| TC-080 | B      | 停止・強制終了・反復起動で、最後の確実な取得境界と不明時刻を区別した欠損、開区間の閉鎖、重複欠損防止、未確定媒体・削除台帳・SRT版数の冪等復旧、無関係な対象の継続を確認する。                                                                                                                        | AC-060                                                                                                 | FR-005、FR-006、FR-010、FR-016 | 録画・確定・復旧、コメント保存、コメントSRT、配信者・保持・削除基盤、CLI・WSL2運用                                            | tests/test_recovery.py、tests/test_retention.py、tests/test_srt.py、tests/test_operations.py                                        |
| TC-081 | A      | 確定済み／進行中／削除済み／音声なし／無発話／部分録画の投入判定、重複防止、ON/OFF・設定スナップショット、1件推論、GPU不可時の録画継続と明示CPU選択。                                                                                                                                                | AC-061                                                                                                 | FR-019                         | 音声キュー・推論子                                                                                                            | tests/test_speech.py                                                                                                                |
| TC-082 | A      | 既知音声・先頭無音・遅延で0秒同期、cue境界・UTF-8・発話なしを検証。VAD、品質フィルタ、語時刻補正、短い相づちの詳細品質評価は改善検証とする。                                                                                                                                                         | AC-062                                                                                                 | FR-019                         | 音声認識・SRT                                                                                                                 | tests/test_speech.py                                                                                                                |
| TC-083 | B      | 音声生成成功／失敗／再生成・旧正常版保持・無発話置換、推論子プロセス異常・GPU OOM・強制終了、再起動回収と二重投入防止。                                                                                                                                                                              | AC-061、AC-062                                                                                         | FR-019                         | 音声キュー・推論子、音声認識・SRT、字幕ローカル実証                                                                           | tests/test_speech.py                                                                                                                |
| TC-084 | A      | ASSのSpeech／Comment、基本レーン、表示寸法、同期・終端・エスケープ、片側／0cue／後着と入力版競合を検証。高密度・細かな表示最適化は改善検証とする。                                                                                                                                                   | AC-063                                                                                                 | FR-020                         | ASS生成                                                                                                                       | tests/test_ass.py                                                                                                                   |
| TC-085 | B      | §10.3に従う3種の字幕名・同一stem（衝突識別子を含む）の照合、音声SRT・ASSの非上書き初回公開／原子的再生成、世代照合、削除予約・再生成競合、途中停止・同名別世代・未マウント・公開後DB確定前の復旧。                                                                                                   | AC-062、AC-063、AC-064                                                                                 | FR-019、FR-020                 | 音声認識・SRT、ASS生成、字幕ローカル実証                                                                                      | tests/test_speech.py、tests/test_ass.py                                                                                             |
| TC-086 | A      | コメント期限削除時にコメントSRT・統合ASSを先に削除、音声SRT・動画を保持、ASS再生成拒否・一時成果物／手動復元からのコメント復活防止。ファイルのみ／ファイル＋履歴は全対応字幕を対象。                                                                                                                 | AC-064                                                                                                 | FR-020                         | 字幕ローカル実証                                                                                                              | tests/test_retention.py、tests/test_recovery.py                                                                                     |
| TC-087 | B      | 音声字幕画面・本文検索・絞り込み・ページング・昇降順・個別再生成・失敗一括再試行・ASS再生成、旧結果と最新試行、設定・診断、遅延応答とファイル取得禁止。                                                                                                                                              | AC-065                                                                                                 | FR-021                         | 本番UI実装、HTTP API、実ブラウザ試験                                                                                          | tests/test_web_ui.py、tests/test_api.py                                                                                             |
| TC-088 | B      | 利用可能なGPUまたは明示CPUで代表音声を推論し、録画／コメント／Webの独立継続、モデル欠落、外部プレイヤー同期を検証する。未使用デバイス・長尺／複数録画の詳細負荷は追加検証とし、未確認を明示する。                                                                                                    | AC-066                                                                                                 | FR-019、FR-020                 | 実GPU・CPU検証                                                                                                                | tests/test_speech.py、tests/test_ass.py                                                                                             |
| TC-089 | B      | 全設定の欠落キー初期化・保存値優先、型／境界／非有限値／未知キー／秘密キー、設定版競合・原子的保存と既定値のUI/API/DB一致を検証する。                                                                                                                                                                | AC-067                                                                                                 | FR-022                         | DB基盤、設定・保存基盤、本番UI実装、HTTP API、CLI・WSL2運用                                                                   | tests/test_web_settings.py、tests/test_api.py、tests/test_operations.py                                                             |
| TC-090 | B      | 設定の反映時期、ポート・ルート・環境優先順位、監視枠をリセットしない頻度変更、録画上限・容量・期限・SRT再同期・ジョブ設定スナップショット・不正起動値を検証する。                                                                                                                                    | AC-067                                                                                                 | FR-022                         | DB基盤、設定・保存基盤、本番UI実装、HTTP API、CLI・WSL2運用                                                                   | tests/test_web_settings.py、tests/test_api.py、tests/test_operations.py                                                             |
| TC-091 | A      | 全HTTP応答のセキュリティヘッダー、Host/Origin/CSRF、外部入力XSS、秘密応答の非キャッシュ、CSP下のHTMX・フォント・操作を実ブラウザで検証する。                                                                                                                                                         | AC-068                                                                                                 | FR-023                         | 監視・ジョブ制御、TikTok連携、録画・確定・復旧、コメント保存、ツイキャス連携、HTTP API、実ブラウザ試験                        | tests/test_api.py、tests/test_monitoring.py、tests/test_e2e.py                                                                      |
| TC-092 | A      | 採用SDKの署名通信先／送信項目を秘密非露出のテストで確認し、制限配信の両ジョブ開始拒否・稼働中停止・再接続禁止、通信不明との区別・取得済み保全を検証する。                                                                                                                                            | AC-068                                                                                                 | FR-023                         | 監視・ジョブ制御、TikTok連携、録画・確定・復旧、コメント保存、ツイキャス連携、HTTP API、実ブラウザ試験                        | tests/test_api.py、tests/test_monitoring.py、tests/test_e2e.py                                                                      |
| TC-093 | B      | 名前・IDの部分一致・安定既定順・カスタム順の永続化、追加／再有効化／削除、ドラッグとキーボード・取消、絞り込み制限・版競合・失敗復元、監視順不変を検証する。                                                                                                                                         | AC-069                                                                                                 | FR-024                         | DB基盤、配信者・保持・削除基盤、本番UI実装、HTTP API、実ブラウザ試験                                                          | tests/test_db.py、tests/test_api.py、tests/test_web_ui.py                                                                           |
| TC-094 | B      | 配信別一覧・コメントのみ・収集失敗／期限削除・横断検索、実効時刻の昇降順・同時刻ID順・日付範囲・ページング・URLと戻る操作・遅延応答を検証する。                                                                                                                                                      | AC-070                                                                                                 | FR-025                         | 履歴・検索・照合サービス、本番UI実装、HTTP API、実ブラウザ試験                                                                | tests/test_api.py、tests/test_web_ui.py                                                                                             |
| TC-095 | C      | コメント延べ受信・新規保存・重複・理由別破棄・最終保存失敗・pendingの排他集計、通信エラー別集計、再試行／DB失敗／再起動・期限削除後の残存数と計数不完全性を検証する。                                                                                                                                | AC-071                                                                                                 | FR-026                         | DB基盤、コメント保存、コメントSRT、履歴・検索・照合サービス、ASS生成                                                          | tests/test_comments.py、tests/test_srt.py、tests/test_ass.py                                                                        |
| TC-096 | B      | SRT／ASSの表示名＋ID・片方のみ・匿名番号取得／未取得・不明・本文なし、改行／制御文字／絵文字・同時刻・重なり・既定4秒・末尾打切り、原文保全と再同期を検証する。                                                                                                                                      | AC-071                                                                                                 | FR-026                         | DB基盤、コメント保存、コメントSRT、履歴・検索・照合サービス、ASS生成                                                          | tests/test_comments.py、tests/test_srt.py、tests/test_ass.py                                                                        |
| TC-097 | A      | 採用する監視・HLS経路の署名消費、独立枠、品質・本編・再接続・確定、明示拒否時の切替禁止を許可済み実配信で確認。未採用候補の全実証は不要。                                                                                                                                                            | AC-072                                                                                                 | FR-027                         | 事前調査、監視・ジョブ制御、TikTok連携、録画・確定・復旧、実TikTok CLI実証、ツイキャス連携、外部・実機証跡                    | tests/test_monitoring.py、tests/test_recording.py、tests/test_twitcasting.py／実配信証跡                                            |
| TC-098 | B      | 機能別ハートビート・期待次回・idle/disabled/blocked/unknown/degraded、DB失敗の503・字幕障害の独立、子の進捗と長尺処理、エンジン成功／失敗・重複防止を検証する。                                                                                                                                      | AC-073                                                                                                 | FR-028                         | DB基盤、ログ・診断基盤、録画・確定・復旧、CLI・WSL2運用、システム診断UI、音声キュー・推論子、実GPU・CPU検証                   | tests/test_operations.py、tests/test_logs.py、tests/test_recording.py、tests/test_speech.py                                         |
| TC-099 | B      | 並行停止の30秒全体予算・録画20秒・コメント5秒・25秒までの子回収・台帳保全／期限超過、10MiB×現行含む5ログの回転・読取競合・再マスキング・再起動を検証する。                                                                                                                                           | AC-073                                                                                                 | FR-028                         | DB基盤、ログ・診断基盤、録画・確定・復旧、CLI・WSL2運用、システム診断UI、音声キュー・推論子、実GPU・CPU検証                   | tests/test_operations.py、tests/test_logs.py、tests/test_recording.py、tests/test_speech.py                                         |
| TC-100 | C      | 管理済み動画／全字幕の容量帰属・共有フォルダ二重計上防止・論理削除・不在／未確認、24時間／7日／30日の障害集計と範囲不足・環境要約・リンク、5秒更新の内部読取のみを検証する。                                                                                                                         | AC-074                                                                                                 | FR-029                         | 履歴・検索・照合サービス、本番UI実装、実ブラウザ試験、システム診断UI                                                          | tests/test_web_ui.py、tests/test_api.py、tests/test_operations.py                                                                   |
| TC-101 | B      | Room不一致／不明、URL不存在、HTTPS／許可先、DNS再解決・リダイレクト・HLS参照、期限切れ・制限・秘匿を障害注入し、採用経路を許可済み実配信で検証する。                                                                                                                                                 | AC-075                                                                                                 | FR-030                         | 事前調査、TikTok連携、録画・確定・復旧、実TikTok CLI実証                                                                      | tests/test_recording.py、tests/test_monitoring.py／実配信                                                                           |
| TC-102 | B      | 採用ID形式・URL許可リストの境界、制御文字・符号化・不正ホスト・表示名長・空名、検索／保存用変換の分離、同一キー競合・再有効化・ID変更拒否を検証する。                                                                                                                                                | AC-076                                                                                                 | FR-031                         | 事前調査、DB基盤、設定・保存基盤、配信者・保持・削除基盤、本番UI実装、HTTP API、実ブラウザ試験                                | tests/test_api.py、tests/test_storage.py、tests/test_web_ui.py                                                                      |
| TC-103 | B      | 照合受付・重複開始・取消・部分結果・再起動中断・世代変化・マウント断・表示上限と総数、Web GET副作用なし、録画・コメント並行負荷を検証する。                                                                                                                                                          | AC-077                                                                                                 | FR-032                         | DB基盤、履歴・検索・照合サービス、HTTP API、システム診断UI、実ブラウザ試験                                                    | tests/test_storage.py、tests/test_api.py、tests/test_web_ui.py                                                                      |
| TC-104 | B      | 試行予約前後・起動前後のクラッシュ、二重予約、上限0／到達、設定変更・監視再有効化・再起動、バックオフ、明示再開と別配信ID、録画とコメント独立予算を検証する。 設定OFF/ON・手動停止・枯渇・制限解除・容量回復の複合抑止、明示再開再送と設定版変更、終了未確認の履歴削除拒否・同一ID再検知を検証する。 | AC-078                                                                                                 | FR-033                         | DB基盤、監視・ジョブ制御、録画・確定・復旧、コメント保存、CLI・WSL2運用                                                       | tests/test_db.py、tests/test_recording.py、tests/test_comments.py、tests/test_recovery.py                                           |
| TC-105 | C      | 診断周期・状態イベント・起動／設定変更／障害時更新、取得不能・延期・時刻鮮度、重複抑止、有限期限、並行録画・コメント・推論時の負荷を検証する。                                                                                                                                                       | AC-079                                                                                                 | FR-034                         | ログ・診断基盤、監視・ジョブ制御、録画・確定・復旧、コメント保存、CLI・WSL2運用、システム診断UI、音声キュー・推論子           | tests/test_operations.py、tests/test_monitoring.py、tests/test_web_ui.py                                                            |
| TC-106 | A      | 採用経路の操作別消費・利用条件・品質・再接続、無料枠不足・制限時の保留を確認する。共通レートリミッター、再起動・消費不明・429・同時接続制限を検証する。                                                                                                                                              | AC-080                                                                                                 | FR-035                         | 事前調査、TikTok連携、録画・確定・復旧、実TikTok CLI実証、ツイキャス連携、外部・実機証跡                                      | research.md／実配信、tests/test_monitoring.py、tests/test_recording.py                                                              |
| TC-107 | B      | 型・本文／検索／ページ上限・日付境界・安定順・不正ソート、共通エラー、同期操作の版競合、非同期ジョブの重複防止・結果確認・再起動、GET無副作用を検証する。結果不明時の危険な再実行を防ぐ。                                                                                                            | AC-081                                                                                                 | FR-036                         | DB基盤、履歴・検索・照合サービス、HTTP API、実ブラウザ試験                                                                    | tests/test_api.py、tests/test_web_ui.py                                                                                             |
| TC-108 | B      | 通常起動のSwagger／ReDoc／OpenAPI／OAuth補助ルートが404となり、管理API・セキュリティヘッダー・隔離スキーマ試験が成立することを検証する。                                                                                                                                                             | AC-081                                                                                                 | FR-036                         | 環境構築、HTTP API、CLI・WSL2運用                                                                                             | tests/test_api.py、tests/test_operations.py                                                                                         |
| TC-109 | B      | （開発チェック）実配信・字幕が未実証でも隔離fixtureでUIを独立実装・検証できること、UIの3案提示・利用者の明示選択・選択前の製品UI適用保留と未検証状態が正確であること、CLIから登録・起動・停止・保存確認が可能であることを確認する。簡易追跡表と依存関係のレビューでよく、専用pytestは不要。          | AC-082                                                                                                 | FR-037                         | 仕様・追跡設計、実TikTok CLI実証、本番UI実装、統合時簡易照合、収束検証、UI現状調査、UI3案提示・利用者選択の記録、独立字幕検証 | 簡易追跡・依存関係レビュー                                                                                                          |
| TC-110 | A      | 通常／開発者の起動選択、設定・資格情報・保存先・ポートの分離、保護パス／リンク／親子衝突と排他、通常ルート非公開、隔離設定・バックアップ・子起動時の逸脱拒否、スナップショット取込時の副作用防止を検証する。外部通信排他・永続予算・復元マーカーも確認する。                                         | AC-083                                                                                                 | FR-038                         | 開発者モード・共通診断基盤、設定・保存基盤、HTTP API、CLI・WSL2運用                                                           | tests/test_operations.py、tests/test_api.py、tests/test_retention.py                                                                |
| TC-111 | B      | 両モードの読取専用診断・秘匿・取得不能／対象不一致、隔離fixture、通常セキュリティ回帰、worker別一時領域、実接続／実機の既定拒否、モデル自動取得禁止、所有子だけの終了処理を代表例で検証する。機能実装とともに追加し、全入口の否定試験を先行ゲートにしない。                                          | AC-083                                                                                                 | FR-038                         | 最小隔離・段階的テスト基盤、開発者モード・共通診断基盤、ログ・診断基盤、開発専用UI検証基盤、実ブラウザ試験、統合時簡易照合    | tests/test_operations.py、tests/test_web_ui.py、tests/conftest.py／隔離シナリオ・モデル境界・証跡隔離・隔離・既定拒否の代表試験結果 |

### 21.3 原本の章と実装への導線

| 正本文書・節                                                    | 対応                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| R §1                                                            | FR-011、FR-015、FR-018／仕様・追跡設計、本番UI実装、HTTP API                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| R §2                                                            | FR-022、FR-009、FR-015〜016／環境構築、設定・保存基盤、ログ・診断基盤、CLI・WSL2運用、導入・実機運用検証                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| R §3 → D §3                                                     | FR-003〜006、FR-011／監視・ジョブ制御、TikTok連携、録画・確定・復旧、コメント保存、本番UI実装、HTTP API                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| R §4                                                            | FR-023、FR-027、FR-001〜006、FR-018／事前調査、監視・ジョブ制御、TikTok連携、録画・確定・復旧、コメント保存、コメントSRT、実TikTok CLI実証、ツイキャス連携；FR-030、FR-035／事前調査、TikTok連携、録画・確定・復旧、実TikTok CLI実証、ツイキャス連携、外部・実機証跡                                                                                                                                                                                                                                                                                                           |
| R §5                                                            | FR-024、FR-027、FR-001〜004／監視・ジョブ制御、配信者・保持・削除基盤、本番UI実装；FR-031／事前調査、DB基盤、設定・保存基盤、配信者・保持・削除基盤、本番UI実装、HTTP API、実ブラウザ試験                                                                                                                                                                                                                                                                                                                                                                                      |
| R §6                                                            | FR-027、FR-028、FR-004〜005、FR-008／監視・ジョブ制御、録画・確定・復旧、実TikTok CLI実証；FR-033／DB基盤、監視・ジョブ制御、録画・確定・復旧、コメント保存、CLI・WSL2運用                                                                                                                                                                                                                                                                                                                                                                                                     |
| R §7                                                            | FR-025、FR-026、FR-006〜007、FR-013、FR-019〜020／TikTok連携、コメント保存、コメントSRT、実TikTok CLI実証、ツイキャス連携、履歴・検索・照合サービス、音声キュー・推論子、音声認識・SRT、ASS生成、字幕ローカル実証、実GPU・CPU検証                                                                                                                                                                                                                                                                                                                                              |
| R §8                                                            | FR-005〜008、FR-013／DB基盤、録画・確定・復旧、コメント保存、コメントSRT、履歴・検索・照合サービス                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| R §9 → D §9                                                     | FR-022、FR-024、FR-026、FR-028、FR-001、FR-003、FR-006〜010、FR-014〜016、FR-019〜020／DB基盤、設定・保存基盤、ログ・診断基盤、監視・ジョブ制御、録画・確定・復旧、コメント保存、コメントSRT、配信者・保持・削除基盤、CLI・WSL2運用、音声キュー・推論子、音声認識・SRT、ASS生成、字幕ローカル実証；FR-033、FR-032、FR-036／DB基盤、監視・ジョブ制御、録画・確定・復旧、コメント保存、履歴・検索・照合サービス、HTTP API、CLI・WSL2運用、システム診断UI                                                                                                                         |
| R §10                                                           | FR-022、FR-029、FR-008〜010、FR-013／設定・保存基盤、録画・確定・復旧、配信者・保持・削除基盤、履歴・検索・照合サービス；FR-032／DB基盤、履歴・検索・照合サービス、HTTP API、システム診断UI                                                                                                                                                                                                                                                                                                                                                                                    |
| R §11                                                           | FR-022、FR-024〜026、FR-028〜029、FR-001、FR-007、FR-010〜014、FR-021／ログ・診断基盤、コメントSRT、配信者・保持・削除基盤、履歴・検索・照合サービス、本番UI実装、HTTP API、実ブラウザ試験；FR-031、FR-032、FR-033、FR-034、FR-036／本番UI実装、HTTP API、実ブラウザ試験、システム診断UI                                                                                                                                                                                                                                                                                       |
| R §12                                                           | FR-015／本番UI実装、HTTP API                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| R §13                                                           | FR-023、FR-009〜010、FR-014〜016、FR-018／設定・保存基盤、ログ・診断基盤、配信者・保持・削除基盤、HTTP API、CLI・WSL2運用、導入・実機運用検証；FR-030、FR-036／TikTok連携、録画・確定・復旧、実TikTok CLI実証、HTTP API                                                                                                                                                                                                                                                                                                                                                        |
| R §14                                                           | FR-028〜029、FR-003〜010、FR-014、FR-016／ログ・診断基盤、監視・ジョブ制御、TikTok連携、録画・確定・復旧、コメント保存、コメントSRT、配信者・保持・削除基盤、CLI・WSL2運用；FR-033、FR-034／DB基盤、ログ・診断基盤、監視・ジョブ制御、録画・確定・復旧、コメント保存、CLI・WSL2運用、システム診断UI、音声キュー・推論子                                                                                                                                                                                                                                                        |
| R §15                                                           | FR-009、FR-015〜016／環境構築、CLI・WSL2運用、導入・実機運用検証                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| R §16                                                           | FR-027〜028、FR-002、FR-004〜006、FR-016、FR-019〜020／DB基盤、監視・ジョブ制御、録画・確定・復旧、コメント保存、CLI・WSL2運用、実GPU・CPU検証                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| R §17                                                           | FR-017／統合時簡易照合、固定TC全行                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| R §18                                                           | FR-017〜018／初期の拡張対象外を検査、本番UI実装、HTTP API、収束検証                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| R §19                                                           | FR-017、FR-037／最小共通基盤、録画・コメント、UI/API、音声・ASSを技術依存に応じて実装。実配信・実機検証は独立した確認。UI採用案の明示選択は採用案依存タスクだけの前提。                                                                                                                                                                                                                                                                                                                                                                                                        |
| R §20                                                           | FR-017／環境構築、統合時簡易照合、収束検証                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| R §21                                                           | FR-017／DB基盤、設定・保存基盤、ログ・診断基盤、監視・ジョブ制御、TikTok連携、録画・確定・復旧、コメント保存、コメントSRT、実TikTok CLI実証、ツイキャス連携、配信者・保持・削除基盤、履歴・検索・照合サービス、本番UI実装、HTTP API、実ブラウザ試験、CLI・WSL2運用、導入・実機運用検証、統合時簡易照合、収束検証、SRT競合試験、システム診断UI、復元試験、外部・実機証跡、UI現状調査、UI参考調査、UI3案比較（必須）、UI採用案選択、UI採用版確定、UI採用版照合、UI独立レビュー（推奨）、音声キュー・推論子、音声認識・SRT、ASS生成、字幕ローカル実証、実GPU・CPU検証および配布物 |
| R §22                                                           | FR-017〜018／実TikTok CLI実証、実ブラウザ試験、CLI・WSL2運用、導入・実機運用検証、統合時簡易照合、収束検証                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| R §23                                                           | FR-017／固定AC全行、統合時簡易照合、収束検証                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| R §24                                                           | FR-017／導入・実機運用検証、統合時簡易照合、収束検証と成果物の実行確認                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| R §25                                                           | 全FR・US・SC／仕様・追跡設計、統合時簡易照合、収束検証                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| D §16〜20                                                       | 全DM・HTTP・業務・採用・境界契約／各担当工程、統合時簡易照合                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| D §21                                                           | 全AC・TC・UI・RISKの統合時簡易照合、重要課題の収束検証                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| R §10.2, §11.8.1〜3, §13.7, §14.6, §15.2.1, §17 → D §15.10, §17 | FR-038／開発者モード・共通診断基盤、設定・保存基盤、ログ・診断基盤、HTTP API、CLI・WSL2運用、開発専用UI検証基盤、実ブラウザ試験、統合時簡易照合                                                                                                                                                                                                                                                                                                                                                                                                                                |

### 21.4 UI受け入れ項目

UI正本§16を起点に機能単位で簡易追跡する。以下の詳細な検証文はUI正本§2.4・§16の等価実装と段階的検証方針で読み替える。関連FR・TCは機能・安全性の対応を示すものであり、UI固有の視覚階層・操作効率・ストレス検証・インタラクションと品質検査の証跡を代替しない。§16.7の表示補足も対応する§16.6と§16.8の検証に含める。

| 原本           | 位置と検証                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| U §16.1 項目1  | 共通ナビゲーションは**8つの主要ナビ（運用・データ5／管理3）**であり、セッション詳細・編集・ログ詳細等は下位ビューとして整理されている。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                |
| U §16.1 項目2  | 主要ナビの表示名と順序は「運用・データ：ダッシュボード／配信者一覧／録画履歴／コメント／音声字幕」「管理：システム状態／ログ／設定」。要求仕様§11.1の主要URL（`/logs` を含む）を維持している。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                         |
| U §16.1 項目3  | ダッシュボードの稼働中配信・最近の録画・警告から、関連する記録や診断へ直接遷移できる。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                                                                 |
| U §16.1 項目4  | 録画履歴は配信セッションを基本単位とし、録画の有無とコメント収集の有無を混同せず表示できる。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                                                           |
| U §16.1 項目5  | 録画のないコメント取得セッションを見失わず、コメント閲覧へ進める。動画なしで架空のSRT操作は出さない。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                                                  |
| U §16.1 項目6  | 一覧 → 詳細 → 戻るで、検索条件・絞り込み・ページ位置が維持される。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                                                                                     |
| U §16.1 項目7  | ダッシュボードは状況把握、システム状態は健康状態・診断要約、ログは時系列の詳細調査という役割が明確で、同じ情報の無意味な重複がない。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                   |
| U §16.1 項目8  | システム状態の警告から関連ログへ、ログから関連する録画履歴・配信者一覧へ、利用可能な識別情報の範囲で遷移できる。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                                       |
| U §16.2 項目1  | 配信／録画／コメント収集／コメントSRT／音声SRT／ASS／画面更新／操作結果を独立した状態として表示できる。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                                                |
| U §16.2 項目2  | **未取得と0件、監視停止と接続失敗、受付と完了、未生成と生成失敗、現在値と古い値**を明確に区別する。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                                                    |
| U §16.2 項目3  | 上部に情報の表示時点を共通配置し、アプリ独自の画面再読込ボタンは設けない。前回値の表示や更新失敗を誤認させず、ページ再読み込みにはブラウザ標準機能を利用する。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                         |
| U §16.2 項目4  | 定期更新が既定間隔・設定変更・再起動なしの反映に従い、入力・編集中に作業を妨げず、スクロール・フォーカス・検索条件を不用意に失わせない。遅延／逆順応答、送信後の操作開始、非表示復帰、設定保存／入力エラーで操作状態と取得時点を保全する（TC-077、TC-078）。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                           |
| U §16.2 項目5  | 二重送信を防止し、結果未確認や競合の際に成功と表示しない。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                                                                                             |
| U §16.2 項目6  | 危険操作の対象と影響範囲、未保存変更、操作後の結果を分かりやすく示す。監視／録画／コメントの設定OFFと復帰の影響、および削除2方式・一部失敗を正しく表示する。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                           |
| U §16.2 項目7  | サービス別監視頻度の入力範囲、共通監視枠、実効値との差、エラー表示が分かりやすい。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                                                                     |
| U §16.2 項目8  | 録画セグメントごとの動画・SRT対応、状態、欠損区間、再生成可否を識別できる。録画動画・字幕のダウンロード操作を表示しない。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                              |
| U §16.2 項目9  | 同じ配信の複数録画ジョブのうち最新ジョブが「完了」でも、前ジョブの `FAILED`、`invalid` / `pending` セグメントや既存の欠損区間が隠れず、セッション全体を完全成功と誤表示しない。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                        |
| U §16.2 項目10 | 過去の飛び番号は機械的に振り直さず、保存された番号・ファイル名・所属ジョブの整合性を崩さない。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                                                         |
| U §16.2 項目11 | SRTのスキップ／生成済み／生成失敗／収集未実施を区別し、0cueの空SRTを成功済みとして誤表示しない。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                                                       |
| U §16.2 項目12 | 最終メディア検証とDBの不一致、古いエラー、監視のUNKNOWNと明示的終了を識別できる情報・診断導線を備える。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                                                |
| U §16.2 項目13 | コメントを配信横断で検索でき、関連セッションへ移動できる。コメント出力メニューを表示しない。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                                                           |
| U §16.2 項目14 | ログは時系列・レベル・出力元・本文を取得可能な範囲で閲覧・絞り込みでき、長文詳細・コピー・空／取得失敗／古い情報の状態を適切に扱う。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                   |
| U §16.2 項目15 | ログ画面に未提供の収集・ダウンロード・削除・全文検索等の機能を捏造しておらず、ログレベルの編集は設定へ集約されている。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                                 |
| U §16.3 項目1  | ゼロベースの参考調査と採用理由を `<UI>/design.md` に記録する。初期は3〜5件、本格再設計は5〜10件を目安とし、取得不能は記録する。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                        |
| U §16.3 項目2  | 初期実装・単独再設計ともに、情報構造・密度・シェル・視覚表現が実質的に異なる3案のモックアップを同一画面・同一データで提示する。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                        |
| U §16.3 項目3  | 利用者が3案から明示的に選択した採用案と選択記録を `<UI>/design.md` に保存し、8画面へ一貫適用する。選択前は採用案に依存する製品UI実装を保留し、独立作業は継続する。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                     |
| U §16.3 項目4  | Inter、紫グラデーション、光るボタン、セリフ見出しの一語イタリック、4枚並列統計カードの定型を使わず、冗長なAI風装飾を全画面で監査している。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                             |
| U §16.3 項目5  | 実ブラウザで自己レビューし改善する。独立レビュー・比較画像・上位改善案の記録は単独再設計で推奨、実施不能は未実施とする。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                               |
| U §16.3 項目6  | UI単独リデザインでは、録画・コメント・DB・API・安全性・CLI・サービス等の**非UIの振る舞いを変更せず**、関連するフロントエンド回帰試験を実施している。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                   |
| U §16.3 項目7  | ライト系テーマを**1種類のみ**使用し、テーマ切り替えやOSテーマ連動は実装していない。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                                                                    |
| U §16.3 項目8  | 設定のフォントプルダウンは**Windows標準／Noto Sans JP**の2択。初期値、`palt`、選択保持、プレビュー、全画面反映を検証している。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                         |
| U §16.3 項目9  | 情報の階層、選定したデザイントークン、テーブルの数値・単位・行高・状態表現が統一され、選定根拠が説明できる。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                                           |
| U §16.3 項目10 | 主要な画面の目的、情報の関係、状態、操作の優先順位が、長い説明文に頼らず**配置・グルーピング・視覚階層**から把握できる。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                               |
| U §16.3 項目11 | 追加した補助説明・サブタイトル・バッジ・カード・アイコン・グラフには、重複しない明確な役割がある。汎用SaaS風の定型表現やカードの入れ子で情報密度を損ねていない。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                       |
| U §16.3 項目12 | 必須ラベル・数値単位・状態・安全上の説明を残したうえで、冗長なUI文言を削り、自然で簡潔な日本語にしている。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                                             |
| U §16.3 項目13 | 配信中＝緑、録画中＝赤、コメント収集中＝青。録画中とエラーを形状・文字で区別し、録画連動ロゴ・ファビコンと動きの抑制を確認している。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                   |
| U §16.3 項目14 | 1440px／1024px、両フォント、キーボード操作、フォーカス、ラベル、コントラストを確認する。1280px・拡大表示の追加検証は推奨。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                             |
| U §16.3 項目15 | 主要8画面と重要な下位ビューを実ブラウザで確認し、補助説明文を隠すテスト、視覚階層のテスト、代表タスクの操作検証、共通部品の回帰確認を実施している（未実施のものは未検証として記録）。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                  |
| U §16.3 項目16 | ログイン画面、動画再生、モバイル専用UI、未提供操作を追加していない。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                                                                                   |
| U §16.3 項目17 | 実装できていないUIや未検証の動作を、成功・完成済みとして見せていない。 （FR-011〜014／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                                                                                                 |
| U §16.4 項目1  | 音声字幕画面で待機／処理中／保留／失敗／無発話／対象外／削除済みと旧正常版を区別し、検索・再生成・失敗一括再試行を確認する。 （FR-021／本番UI実装、HTTP API、実ブラウザ試験、TC-087）                                                                                                                                                                                                                                                                                                        |
| U §16.4 項目2  | 録画詳細と音声字幕画面でコメントSRT・音声SRT・ASSの対応・暫定／片側／期限削除・生成可否を区別し、発話本文を検索できる。 （FR-021／本番UI実装、HTTP API、実ブラウザ試験、TC-087）                                                                                                                                                                                                                                                                                                             |
| U §16.4 項目3  | 設定・GPU診断・キューの状態と反映時期が正しく、字幕処理や更新失敗で操作を破壊せず、ファイル本体を配信しない。 （FR-021／本番UI実装、HTTP API、実ブラウザ試験、TC-087）                                                                                                                                                                                                                                                                                                                       |
| U §16.5 項目1  | 全設定の既定値・範囲・単位・反映時期、保存失敗・競合時の入力保持、容量上限・再接続・取得間隔を確認する（TC-089〜090）。 （FR-022〜029／本番UI実装、HTTP API、実ブラウザ試験、システム診断UI）                                                                                                                                                                                                                                                                                                |
| U §16.5 項目2  | 名前・ID検索、デフォルト／カスタム順、ドラッグとキーボード、絞り込み中の制限、版競合と再読込後の保持を確認する（TC-093）。 （FR-022〜029／本番UI実装、HTTP API、実ブラウザ試験、システム診断UI）                                                                                                                                                                                                                                                                                             |
| U §16.5 項目3  | コメント配信別／横断、録画なし・期限削除済み、昇降順、時刻根拠とページ・検索条件の復元を確認する（TC-094）。 （FR-022〜029／本番UI実装、HTTP API、実ブラウザ試験、システム診断UI）                                                                                                                                                                                                                                                                                                           |
| U §16.5 項目4  | 収集統計の区別、匿名表示・本文正規化と診断、取得不能を0としないことを確認する（TC-095〜096）。 （FR-022〜029／本番UI実装、HTTP API、実ブラウザ試験、システム診断UI）                                                                                                                                                                                                                                                                                                                         |
| U §16.5 項目5  | HTTPセキュリティヘッダー・エスケープと実ブラウザ機能の互換性を確認する（TC-091）。 （FR-022〜029／本番UI実装、HTTP API、実ブラウザ試験、システム診断UI）                                                                                                                                                                                                                                                                                                                                     |
| U §16.5 項目6  | 機能別健康状態・エンジン診断・停止中・古い情報、ログの取得可能範囲と診断への導線を確認する（TC-098〜099）。 （FR-022〜029／本番UI実装、HTTP API、実ブラウザ試験、システム診断UI）                                                                                                                                                                                                                                                                                                            |
| U §16.5 項目7  | 配信者別容量・警告期間集計・環境要約の正確性、未確認の区別と既存8画面の優先情報・5秒更新を確認する（TC-100）。 （FR-022〜029／本番UI実装、HTTP API、実ブラウザ試験、システム診断UI）                                                                                                                                                                                                                                                                                                         |
| U §16.6 項目1  | 登録時の入力例・制限・表示名既定・編集時の不変ID・フィールド別エラー・再有効化と入力保持を確認する（TC-102）。 （FR-031、TC-102／配信者・保持・削除基盤、本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                                                              |
| U §16.6 項目2  | 手動照合の受付・重複防止・取消・途中結果・前回結果・未確認・鮮度と診断延期を確認する（TC-103、105）。 （FR-032、FR-034、TC-103、105／本番UI実装、HTTP API、実ブラウザ試験、システム診断UI）                                                                                                                                                                                                                                                                                                  |
| U §16.6 項目3  | 共通ページング・日付境界・エラー、同期版競合、非同期受付／完了、重複防止と結果不明時の安全な照合、予算・抑止・終了未確認の履歴削除拒否を確認する（TC-104、107）。 （FR-033、FR-036、TC-104、107／本番UI実装、HTTP API、実ブラウザ試験）                                                                                                                                                                                                                                                      |
| U §16.8 項目1  | 主情報／補足情報、独立した比較軸、数値・日時・単位の整列、長い識別子の全文確認を主要一覧で確認し、列の圧縮による情報欠落や過剰な装飾がない（§2.8、6.6）。 （FR-011〜014、TC-077〜078／本番UI実装・実ブラウザ試験・デザイン検証）                                                                                                                                                                                                                                                             |
| U §16.8 項目2  | 8画面と下位ビューの現在地・親への導線、共通表示時点と対象別鮮度、主要操作の配置、拡大時の遮蔽防止を確認する（§3.5）。 （FR-011〜014、TC-077〜078／本番UI実装・実ブラウザ試験・デザイン検証）                                                                                                                                                                                                                                                                                                 |
| U §16.8 項目3  | 一覧の状態記号・ラベル・色、録画連動ロゴ・ファビコン、動き抑制、状態不明時の経過表示、壁時計とメディア長の区別を確認する（§5.5）。 （FR-011、TC-077〜078／本番UI実装・実ブラウザ試験・デザイン検証）                                                                                                                                                                                                                                                                                         |
| U §16.8 項目4  | 健康状態・優先KPI・補足KPIの階層、稼働中セッション3領域、管理容量とボリューム容量の分母・鮮度、障害集計と診断導線を確認する（§7.4）。 （FR-011、FR-029、TC-100／本番UI実装・実ブラウザ試験・デザイン検証）                                                                                                                                                                                                                                                                                   |
| U §16.8 項目5  | 配信者の複合セル、実状態と共通アクセントの設定トグル、行アクションの安全な分離、絞り込み・ドラッグ／キーボード操作と確定順の維持を確認する（§8.1.1）。 （FR-004、FR-011、FR-024、FR-031、TC-027、TC-093、TC-102／本番UI実装・実ブラウザ試験・デザイン検証）                                                                                                                                                                                                                                  |
| U §16.8 項目6  | セッション一覧の比較軸・件数リンク、配信／録画／コメントの概要比較、セグメントに従属する3字幕・版・欠損・安全な削除2方式の表示を確認する（§9.1.1、9.2.1、9.3.1）。 （FR-010、FR-011、FR-013、FR-021、FR-026、TC-075、TC-086〜087、TC-094〜095、TC-104／本番UI実装・実ブラウザ試験・デザイン検証）                                                                                                                                                                                            |
| U §16.8 項目7  | 本文中心の領域・投稿者／時刻根拠・配信文脈、配信別／横断検索・条件・昇降順・ページ復元、音声キューと発話本文への文脈リンク、長文・HTML風文字列の安全な表示を確認する（§10.2.1、10A.1）。 （FR-021、FR-023、FR-025、FR-026、TC-087、TC-091、TC-094〜096／本番UI実装・実ブラウザ試験・デザイン検証）                                                                                                                                                                                           |
| U §16.8 項目8  | エンジンの可否／実績、照合の分類別総数と表示件数・前回結果、診断鮮度、ログ重要度・要約／全文・秘匿・関連導線を確認する（§11.5、12.5）。 （FR-014、FR-028、FR-032、FR-034、TC-076、TC-098〜099、TC-103、TC-105／本番UI実装・実ブラウザ試験・デザイン検証）                                                                                                                                                                                                                                    |
| U §16.8 項目9  | 6カテゴリ内の入力行・範囲・単位・反映条件、保存領域の未保存／失敗／競合／適用待ち、非表示カテゴリのエラーへの移動と入力保持を確認する（§13.6）。 （FR-022、TC-089〜090／本番UI実装・実ブラウザ試験・デザイン検証）                                                                                                                                                                                                                                                                           |
| U §16.8 項目10 | 採用済みトークンと共通部品の対応、状態・サービス・操作アイコンの意味統一、両フォントの数値整列、フィルター・ページング・空状態の共通化を全画面で監査する（§6.6）。 （FR-011〜014、FR-036、TC-107／本番UI実装・実ブラウザ試験・デザイン検証）                                                                                                                                                                                                                                                 |
| U §16.8 項目11 | §14.3の初期代表ケースを実ブラウザで確認し、幅・フォント・結果・未検証を記録する。追加ケースの画像は必要に応じ保存する。 （FR-011〜014、FR-017、FR-021、FR-037、TC-109／本番UI実装・実ブラウザ試験・デザイン検証）                                                                                                                                                                                                                                                                            |
| U §16.9 項目1  | 配信者・セッション・コメント・発話・ログ等の安定IDと部品種別でフォーカス・展開・スクロール基準を復元し、行追加・同名・対象消失で誤対象への移動がない（§5.4.1）。 （FR-011、TC-077〜078／本番UI実装・実ブラウザ試験、UI固有ケースの証跡）                                                                                                                                                                                                                                                     |
| U §16.9 項目2  | 明示検索・ページ送り・保存・入力エラー・ダイアログ閉鎖と定期更新でフォーカス／閲覧位置が契約どおり異なり、遅延・逆順・利用者の移動後に巻き戻らない（§5.4.2）。 （FR-011、TC-077〜078／本番UI実装・実ブラウザ試験、UI固有ケースの証跡）                                                                                                                                                                                                                                                       |
| U §16.9 項目3  | 全順序編集のドラッグ挿入位置・端スクロール・Escape／ポインター中断・キーボード・クリック代替・動き抑制・確定保存を確認し、競合・結果未確認・絞り込み中の誤保存がない（§8.1.2）。 （FR-024、FR-036、TC-093、TC-107／本番UI実装・実ブラウザ試験、UI固有ケースの証跡）                                                                                                                                                                                                                          |
| U §16.9 項目4  | 確定値・送信中・受付／実行中・明示拒否／競合・部分失敗・結果未確認を区別し、応答消失後に現在値／業務ジョブ状態を照合し、危険な再実行をしない（§6.2.1）。 （FR-011、FR-036、TC-078、TC-107／本番UI実装・実ブラウザ試験、UI固有ケースの証跡）                                                                                                                                                                                                                                                  |
| U §16.9 項目5  | 重要度別通知、手動閉鎖、ポインター／フォーカス中の消去停止、同一通知の抑制、周期失敗のインライン表示、読み上げとフォーカス保全を確認する（§6.3.1）。 （FR-011、TC-077〜078／本番UI実装・実ブラウザ試験、UI固有ケースの証跡）                                                                                                                                                                                                                                                                 |
| U §16.9 項目6  | メニュー・タブ・ダイアログの意味に合うキーボード操作とフォーカス復帰、非表示領域への誤移動防止、削除／保持・確認チェックの初期化・安全な取消を確認する（§6.4.1）。 （FR-011、FR-036、TC-075、TC-107／本番UI実装・実ブラウザ試験、UI固有ケースの証跡）                                                                                                                                                                                                                                        |
| U §16.9 項目7  | 適用済み条件・モード・順序・ページのURL再現、変更時の先頭ページ、範囲外ページ、戻る／進む・閲覧基準復元、自動更新の履歴非追加、秘密／未保存値の非混入を確認する（§6.7）。 （FR-011、FR-025、FR-036、TC-077〜078、TC-094、TC-107／本番UI実装・実ブラウザ試験、UI固有ケースの証跡）                                                                                                                                                                                                            |
| U §16.9 項目8  | 型付き比較で元値への復帰、複数カテゴリの変更／入力エラー、成功時のみ基準更新、競合・結果未確認・離脱取消時の入力保持と保存／適用待ちの区別を確認する（§13.7）。 （FR-022、FR-031、FR-036、TC-078、TC-089〜090、TC-102、TC-107／本番UI実装・実ブラウザ試験、UI固有ケースの証跡）                                                                                                                                                                                                              |
| U §16.9 項目9  | 年・日付境界、表示タイムゾーン／DST、24時間超、元の精度・同一秒の安定順・未知時刻、壁時計経過と媒体長を共通整形で検証する（§6.5.1）。 （FR-011、FR-025、FR-036、TC-094、TC-107／本番UI実装・実ブラウザ試験、UI固有ケースの証跡）                                                                                                                                                                                                                                                             |
| U §16.9 項目10 | 全既知状態・初回未確認・未知コード・古い状態の共通変換と一覧／詳細／概要／ロゴの意味整合を検証し、表示変換が業務状態を変更しない（§5.6）。 （FR-011、TC-077〜078／本番UI実装・実ブラウザ試験、UI固有ケースの証跡）                                                                                                                                                                                                                                                                           |
| U §16.9 項目11 | 隔離fixtureで代表シナリオを確認し、通常起動に開発ルート・模擬機能がなく、実運用資源へ接続しない。実証結果とは区別する（§14.4）。 （FR-011、FR-017、FR-023、FR-036、FR-037、FR-038、TC-091、TC-108〜111／開発専用UI検証基盤・実ブラウザ試験、UI固有ケースの証跡）                                                                                                                                                                                                                             |
| U §16.9 項目12 | 代表ブラウザ操作・DOMはみ出し／遮蔽・JS／CSP・基本アクセシビリティを確認し、採用版と結果・未検証を記録する。詳細自動検査・撮影・独立レビューは推奨。証跡の秘匿・Git除外は維持する（§14.5、AGENTS.md、TC-111）。 （FR-011〜014、FR-037、FR-038、TC-077〜078、TC-091、TC-109、TC-111／開発専用UI検証基盤・実ブラウザ試験、UI固有ケースの証跡）                                                                                                                                                 |
| U §16.9 項目13 | 開発者モードの実効モードとデータ環境が全画面で文字により識別でき、隔離データの明示、通常運用での非表示、8画面／単一ライト／フォント2択の維持、Webからのモード切替不可を確認する。開発者モードのプレビュー・模擬操作は隔離のみで、通常運用相当のCSPとブラウザ操作を別途回帰する（§3.5、§14.4、要求§13.7、TC-110〜111）。 隔離保存先の境界外指定拒否・入力保持と外部接続の既定停止表示も確認する。 （FR-038、TC-110〜111／本番UI実装・開発専用UI検証基盤・実ブラウザ試験、UI固有ケースの証跡） |

### 21.5 原本テストの追加候補

次の識別子は追加検証条件（Featureの生成時に引き継ぐ）。既存TCで独立して証明されない部分を検証し、必要時に§17末尾へ追加を提案する。

- FT-001（AC-008）同時に2配信を録画し上限を超えると待機する。録画・確定・復旧、実TikTok CLI実証。
- FT-002（AC-023、039、049）ログインなしのHTTP応答、別Origin拒否、固定ポート・競合拒否（TC-070）、二重起動、無効PIDを別々に確認する。HTTP API、CLI・WSL2運用。
- FT-003（AC-040）画面ロック／画面OFFをWindows実機でそれぞれ再現。スリープ／休止による中断後に復旧を確認する。導入・実機運用検証。
- FT-004（AC-046〜048）8画面およびAPIルート一覧で再生・プレビュー・同期再生が無いことを検査する。本番UI実装、HTTP API。

### 21.6 リスク対応

| RISK    | 工程責務                                                                                                               |
| ------- | ---------------------------------------------------------------------------------------------------------------------- |
| RISK-01 | 事前調査、TikTok連携、実TikTok CLI実証、外部・実機証跡（TC-106）                                                       |
| RISK-02 | 事前調査、TikTok連携、録画・確定・復旧、実TikTok CLI実証（TC-101、106）                                                |
| RISK-03 | ツイキャス連携、コメント保存                                                                                           |
| RISK-04 | ツイキャス連携                                                                                                         |
| RISK-05 | 録画・確定・復旧、コメントSRT、導入・実機運用検証                                                                      |
| RISK-06 | CLI・WSL2運用、導入・実機運用検証                                                                                      |
| RISK-07 | 設定・保存基盤、録画・確定・復旧、導入・実機運用検証、配信者・保持・削除基盤、SRT競合試験、システム診断UI              |
| RISK-08 | 監視・ジョブ制御、TikTok連携                                                                                           |
| RISK-09 | TikTok連携、コメント保存、実TikTok CLI実証、ツイキャス連携                                                             |
| RISK-10 | 環境構築、録画・確定・復旧、実TikTok CLI実証、ツイキャス連携                                                           |
| RISK-11 | DB基盤、監視・ジョブ制御、録画・確定・復旧、コメント保存、コメントSRT、配信者・保持・削除基盤、CLI・WSL2運用（TC-104） |
| RISK-12 | ツイキャス連携、導入・実機運用検証                                                                                     |
| RISK-13 | 履歴・検索・照合サービス、本番UI実装、実ブラウザ試験                                                                   |
| RISK-14 | コメントSRT、実TikTok CLI実証、履歴・検索・照合サービス                                                                |
| RISK-15 | ログ・診断基盤、実ブラウザ試験、CLI・WSL2運用、導入・実機運用検証、本番UI実装、HTTP API                                |
| RISK-16 | 音声キュー・推論子、音声認識・SRT、実GPU・CPU検証                                                                      |
| RISK-17 | 音声認識・SRT、ASS生成、字幕ローカル実証、実GPU・CPU検証                                                               |

### 21.7 追加データ制約・開発チェック

| 制約／チェック       | 関連原本・検証                        | 工程責務                                                                   |
| -------------------- | ------------------------------------- | -------------------------------------------------------------------------- |
| DM-21                | R §6.5.2、D §15.7、AC-078、TC-104     | DB基盤、監視・ジョブ制御、録画・確定・復旧、コメント保存、CLI・WSL2運用    |
| DM-22                | R §10.5.1、D §15.6、AC-077、TC-103    | DB基盤、履歴・検索・照合サービス、HTTP API、システム診断UI、実ブラウザ試験 |
| DM-23                | R §13.2.2、D §15.8、AC-081、TC-107    | DB基盤、HTTP API、実ブラウザ試験                                           |
| 独立実装・機能別検証 | R §19、AC-082、TC-109（開発チェック） | UI・録画・字幕の独立タスク、3案提示・利用者選択・未検証記録                |

初期状態では実証未実施。外部候補は採用証跡が得られるまで未検証。
