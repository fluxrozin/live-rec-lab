# データモデルと制約

SQLite WAL、短い同期トランザクション、busy_timeout、全接続でforeign_keys=ON。時刻はUTC、表示・ファイル名のみIANAタイムゾーンで変換する。

| 制約 | 所有テーブル・規則 |
| --- | --- |
| DM-01 配信者 | `streamers` の UNIQUE(platform, platform_user_id)。履歴があれば archived_at で除外。再登録はアーカイブ解除と指定された3つの真偽値を同一書込で確定。過去セッションへのFKを維持。 |
| DM-02 配信セッション | `stream_sessions` の UNIQUE(streamer_id, platform_stream_id)。IDなしの観測では既存セッションの確認に留め、新規作成を保留。状態、最終LIVE、終了根拠、platform_ended_atとended_atを別々に保存。未確認収束で推測時刻を入れない。 |
| DM-03 採番とFK | `recording_jobs` の UNIQUE(id, stream_session_id)。`recording_segments` の FOREIGN KEY(recording_job_id, stream_session_id) REFERENCES recording_jobs(id, stream_session_id)、UNIQUE(stream_session_id, segment_number)。NULLは未採番を表す。SQLite書込トランザクションで next_segment_number と正式名を予約し、確定失敗・クラッシュ・削除で巻き戻さない。空試行は予約せず、欠損・イベントを記録する。 |
| DM-04 Windows配置 | `streamer_folders` は normalized_id の主キーと Windows同名性キーの一意制約。正規化・大文字小文字・終端ドット／空白・予約名を処理し、異ID衝突は元ID由来ハッシュを延長して解決。既存マッピング・フォルダを再利用し、管理外のフォルダは拒否。現在のディスク上も照合する。 |
| DM-05 保存段階 | 未採番行→同一ボリューム一時録画→保持判定→予約済み→排他的公開→確定。予約はセッション日時（配信開始または検知）、フォルダ、正式パスを固定する。最初の予約で同名ならID由来 filename_disambiguator を固定し、以後は同じ値を使用。後から同名が生じた場合は保留・復旧し、既存物に触れない。再起動は予約・一時・正式パスとファイルの同一性を照合する。 |
| DM-06 コメント | `comments` の UNIQUE(stream_session_id, platform_comment_id)。NULLのIDは強い同一性の証拠がない限り統合しない。posted_at・received_at・保存時刻とオフセットを分ける。録画開始より前／不明の録画オフセットはNULL。本文・匿名番号は実取得データ。時刻・セッション・投稿者に検索用インデックス。 |
| DM-07 SRT | セグメントごとに input_version、generated_from_version、provisional、status、srt_path、補正値・同期法。該当入力の変更と版数増分を同じSQLite書込に含める。一意一時ファイルと公開前チェック、管理下の旧SRTのみ置換。公開後に版数照合、スキップは旧SRTの安全な除去後。再起動時は版数／公開状態を比較する。 |
| DM-08 削除台帳 | `deletion_operations` は削除後も残る対象ID・mode・status・対象パスと所有物判定情報・時刻・エラー。台帳を先にコミットし、対象の所有・状態を逐次照合。ファイルのみは files_deleted_at を確定、ファイル＋履歴は配下のジョブ・コメント・欠損を参照整合性を保って除去する。 |
| DM-09 コメント期限 | UTC ended_at＋正整数日数（初期0）。収束、ジョブと書込の完了、復旧中でないことが条件。台帳登録以降は書込・SRT生成を排他停止、管理SRT確認後にコメントを物理削除し comments_purged_at と purged_comment_count を確定。動画、履歴、ジョブ、欠損を維持。部分失敗は台帳で復旧し、期限削除済みからの再生成・再取得は拒否。 |
| DM-10 実行・イベント | `system_settings` は非秘密設定、`runtime_instances` は実行識別。イベント要約に実際の相関IDを残し、削除済み履歴からは参照だけを切る。WAL DBはOnline Backup APIで退避。手動復元後に期限SRTの整合を再評価する。 |

Alembicは初期スキーマを作成し、再利用する試作スキーマへの追加は変更履歴を分離する。既存番号・ファイル名は取得済み記録。重複や照合不能な旧データを発見した場合は対象を保護して移行を保留する。
