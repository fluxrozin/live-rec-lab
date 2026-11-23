# 04. データベース・ファイル保存設計

> 出典：[`requirements.md`](../../requirements.md) §9–10、§15。**設計ドラフト。以下はDDL確定版ではない。**

## 1. 永続化アーキテクチャ

SQLite 3（SQLAlchemy 2.x / Alembic）を**WSL2内のLinux ext4**に置き、WAL、`busy_timeout`、`foreign_keys=ON`、短いトランザクション、適切なインデックスを用いる。動画・対応SRTはWindowsローカルドライブに保存。NAS／DrvFS上にSQLiteの正本を置かない。

| 情報 | 既定の保存先例 | 主な用途 |
| --- | --- | --- |
| SQLite | `~/.local/share/live-rec-lab/data` | 設定・配信者・セッション・ジョブ・コメント |
| ログ | `~/.local/state/live-rec-lab/logs` | 障害調査と詳細ログ |
| 起動状態 | `~/.local/state/live-rec-lab/runtime/endpoint.json` | 自動ポート、PID、インスタンス識別 |
| 動画・SRT | `/mnt/d/LiveRecorder/recordings/` | Windows側の録画保管（ドライブ可変） |

別worktreeからの同時試験実行時は**DB、ログ、endpoint.jsonのディレクトリを分離**して競合を避ける。SQLiteバックアップにはOnline Backup API等を使い、稼働中DBファイルだけを直接コピーしない。

## 2. 初期エンティティ候補

| テーブル | 責務 | 主要関連 |
| --- | --- | --- |
| `streamers` | サービス別監視アカウント、3種ON/OFF、最終監視 | `(platform, platform_user_id)`一意 |
| `stream_sessions` | 1回の配信、配信ID、期間、状態 | `streamer_id` |
| `recording_jobs` | 録画実行単位、エンジン、停止指示、再試行 | `stream_session_id` |
| `recording_segments` | 分割動画、物理ファイル、検証状態、同期、SRT | `recording_job_id` |
| `comment_collection_jobs` | コメント収集実行、再接続、件数、失敗 | `stream_session_id` |
| `comments` | 元コメント、識別・時刻・ソース | `stream_session_id` |
| `collection_gaps` | 録画／コメント取得の欠損期間と理由 | `stream_session_id` |
| `system_settings` | 監視頻度などの通常設定 | `key` |
| `runtime_instances` | 起動インスタンスの生存管理（必要なら） | `instance_id` |
| `system_events` | 検索可能な警告・障害イベントの要約 | 関連セッション等 |

重要制約：`streamers`のサービス＋正規化IDは一意。配信ID取得時は同一配信の二重セッション生成を防止。コメントID取得時は同セッション内 `(stream_session_id, platform_comment_id)` を一意化する。セッション削除時のDB関連レコードとWindowsファイルの扱いは詳細設計で確定する。秘匿情報を `system_settings` に通常文字列として不用意に入れない。

## 3. 動画とSRTのフォルダ設計

```text
D:\LiveRecorder\recordings\
└── streamer01\
    └── 2026-11-23\
        ├── tiktok_streamer01_20261123_210000_s001.mp4
        ├── tiktok_streamer01_20261123_210000_s001.srt
        ├── twitcasting_streamer01_20261123_220000_s001.mp4
        └── twitcasting_streamer01_20261123_220000_s001.srt
```

- 第一階層は**正規化済み配信者IDのみ**（異なるサービスでもID文字列が同じなら同一フォルダ）。ただし**DB上でアカウントを統合しない**。
- 第二階層はセグメント開始日の `YYYY-MM-DD`（設定タイムゾーン）。第三階層に動画と対応SRTを保存。
- ファイル名にプラットフォーム・ID・開始時刻・セグメント番号等を含め、別サービス・別セッションの同名上書きを防ぐ。表示名の変更でディレクトリを改名しない。
- Windows禁止文字／予約名／大文字小文字衝突／パストラバーサルを `storage.py` で集中処理する。各コンポーネントで独自にパスを結合しない。

## 4. セグメントの安全な確定処理

```text
recording_segmentsに未確定レコード作成
    ↓
Windows録画先と同一ボリューム上の一時ファイルに記録
    ↓
ffprobe等で整合性を検証（必要なら無劣化リマックス）
    ↓
同一ボリューム内で正式ファイル名へrename
    ↓
SQLite上でファイル・検証結果を確定
    ↓
対応セグメントのコメントSRTを生成・記録
```

ファイルシステムのrenameとSQLiteコミットは完全には原子的にならない。強制終了後は一時ファイル・正式ファイル・DB状態を**双方向で照合**する。未登録ファイルを即削除しない。WSL2 ext4からNTFSへのクロスデバイスrenameに依存しない。

## 5. 容量・整合性・削除

- 起動時／監視時に録画先のマウント状態、書き込み権限、空き容量を確認。利用できない場合に勝手にWSL2内へ保存しない。
- 保存容量上限、最低空き容量、録画件数・配信者別容量を管理。**自動削除は初期OFF**。
- DB登録済みファイルの不在、サイズ不一致、未確定、一時ファイル、孤立ファイルを判別する。
- WSL2 `ext4.vhdx` のホスト側容量増加にも注意する。安全条件が明確でない自動修復・削除を行わない。

## 6. 詳細設計で確定する事項

型・NOT NULL・全ユニークキー・インデックス・ON DELETE方針、保存期間ポリシー、トランザクション境界、ファイル復旧状態の詳細は**実装するfeatureの `data-model.md` とAlembic migration**へ落とす。本書の列挙を完成したDDLとみなさない。
