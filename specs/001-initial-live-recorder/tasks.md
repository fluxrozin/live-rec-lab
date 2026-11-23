# Tasks: LiveRecorder 初期版

[Feature仕様](spec.md) · [計画](plan.md) · [制約](data-model.md) · [操作契約](contracts/operations.md) · [HTTP契約](contracts/http.md) · [追跡表](traceability.md)

## Phase 1 — 実現性調査と入口

- [x] T001 `specs/001-initial-live-recorder/research.md` に公式契約・作業環境・実測残件を記録（RISK-01〜04、06、10）
- [x] T002 `specs/001-initial-live-recorder/spec.md` と `contracts/` に原本の要件を整理し、FeatureとGitのゲートを確認
- [x] T003 `pyproject.toml` と `uv.lock` を使いPython 3.12で依存解決・静的検査を実行し、`tests/` で起動前回帰の結果を記録 再利用したコードは、該当する安全契約が完成するまで稼働環境に接続しない。試験は隔離環境を使用し、旧削除・録画処理を有効にしない。

## Phase 2 — 共通基盤

- [x] T004 `src/live_rec_lab/db/models.py` と `alembic/versions/` に [DM-01〜DM-10](data-model.md) のDDL、FK、索引、制約、移行を実装。 `tests/test_db.py` で一意性・複合FK・移行を検証 正本制約: [DM-01](data-model.md) Streamer.archived_at、Streamer.platform、Streamer.platform_user_id。[DM-02](data-model.md) StreamSession.platform_stream_id、StreamSession.status、StreamSession.last_seen_live_at、StreamSession.platform_ended_at、StreamSession.ended_at、StreamSession.end_reason。[DM-03](data-model.md) RecordingJob.id、RecordingJob.stream_session_id、RecordingSegment.recording_job_id、RecordingSegment.stream_session_id、RecordingSegment.segment_number、StreamSession.next_segment_number。[DM-04](data-model.md) StreamerFolder.normalized_id、StreamerFolder.windows_key。[DM-05](data-model.md) StreamSession.filename_disambiguator、RecordingSegment.file_path、RecordingSegment.validation_status。[DM-06](data-model.md) Comment.stream_session_id、Comment.platform_comment_id、Comment.posted_at、Comment.received_at、Comment.created_at、Comment.recording_offset_ms。[DM-07](data-model.md) RecordingSegment.srt_input_version、RecordingSegment.srt_generated_from_version、RecordingSegment.srt_provisional、RecordingSegment.srt_path、RecordingSegment.srt_status。[DM-08](data-model.md) DeletionOperation.target_id、DeletionOperation.mode、DeletionOperation.status、DeletionOperation.target_paths、RecordingSegment.files_deleted_at。[DM-09](data-model.md) StreamSession.comments_purged_at、StreamSession.purged_comment_count。[DM-10](data-model.md) SystemSetting.key、SystemEvent.correlation_id、RuntimeInstance.instance_id。
- [x] T005 [P] `src/live_rec_lab/config.py`、`storage.py` で [DM-04・DM-05](data-model.md) のルート照合・衝突・永続フォルダを実装し、`tests/test_storage.py` に同一／親子、Windows照合・管理外・権限と障害を設定（RISK-07） [DM-04](data-model.md) StreamerFolder.normalized_id、StreamerFolder.windows_key と [DM-05](data-model.md) StreamSession.filename_disambiguator を所有・衝突の判断に用いる。
- [x] T006 [P] `src/live_rec_lab/ops/logging_config.py`、`ops/log_reader.py` に記録・応答前秘匿、範囲・段階読取・詳細・相関を実装。 `tests/test_logs.py` に旧ログ・回転・壊れた／巨大行・権限・検索を設定（RISK-15）

## Phase 3 — US1 TikTok から独立ジョブのE2Eへ

**独立試験**: 模擬LIVEとローカルHLS、遅延コメントから動画・SQLite・SRTをCLIで照合。

- [ ] T007 [US1] `src/live_rec_lab/monitoring/`、`services/jobs.py`、`services/sessions.py` で [DM-02](data-model.md) と状態契約を実装。 `tests/test_monitoring.py` にENDING・設定ON/OFF・手動停止・再開・孤立再照合・共有枠・重複を設定（RISK-08、11）
- [x] T008 [P] [US1] `src/live_rec_lab/platforms/tiktok.py` にHTTP診断・状態判定と既知Room IDによる受信を実装。 `tests/test_monitoring.py`、`tests/test_comments.py` に拒否・429・遅延・受信タスク寿命・ドレイン・再接続を設定（RISK-01、02、08、09）
- [x] T009 [US1] `src/live_rec_lab/recording/` と `ops/recovery.py` に [DM-03・DM-05](data-model.md) の採番、非上書き、子プロセス回収、FFmpeg確定再検証を実装し、`tests/test_recording.py`、`tests/test_recovery.py` で空・異常・競合・クラッシュを試験（RISK-02、05、07、10、11） [DM-03](data-model.md) RecordingSegment.stream_session_id、RecordingSegment.segment_number、RecordingSegment.recording_job_id、StreamSession.next_segment_number と [DM-05](data-model.md) RecordingSegment.file_path、RecordingSegment.validation_status を照合。
- [x] T010 [US1] `src/live_rec_lab/comments/store.py` に [DM-06・DM-07](data-model.md) の原文・時刻・短いバッチ書込を実装し、`tests/test_comments.py` でNULL・重複・キュー・失敗を試験（RISK-09）
- [x] T011 [US1] `src/live_rec_lab/comments/srt.py` に [DM-07](data-model.md) の自動／手動生成、暫定、版数競合、公開・復旧・0件除去を実装。 `tests/test_srt.py` に前後版数競合・後着・同期不能と障害を設定（RISK-05、14） [DM-07](data-model.md) RecordingSegment.srt_input_version、RecordingSegment.srt_generated_from_version、RecordingSegment.srt_provisional、RecordingSegment.srt_path、RecordingSegment.srt_status を単体と障害試験で照合。
- [x] T012 [US1] `tests/test_e2e.py` でCLI経路のローカルHLS／模擬コメント・SQLite・SRTを検証し、可能な実配信試験と証跡を `research.md` に記録（RISK-01、02、09、10、14）

## Phase 4 — US1 ツイキャスとの統合

- [x] T013 [US1] `src/live_rec_lab/platforms/twitcasting.py` と `recording/hls_playlist.py` で公式API・Basic・残枠・ページング・HLSを実装。 `tests/test_twitcasting.py` で認証、HLS、プリロール否定ケース、匿名番号不明を検証（RISK-03、04、12）

## Phase 5 — US2 履歴と保持、US3 Web/API

**独立試験**: 取得済みセッションと旧ログを使って削除・検索・診断の操作を試す。

- [x] T014 [US2] `src/live_rec_lab/services/`、`db/queries.py` で [DM-01・DM-08・DM-09](data-model.md) の再有効化、削除2方式・自動容量削除・コメント期限削除と台帳復旧を実装。 `tests/test_retention.py` に差し戻し、SRT先行、障害を設定 [DM-01](data-model.md) Streamer.archived_at、[DM-08](data-model.md) DeletionOperation.target_id、DeletionOperation.mode、DeletionOperation.status、DeletionOperation.target_paths、RecordingSegment.files_deleted_at、[DM-09](data-model.md) StreamSession.ended_at、StreamSession.comments_purged_at、StreamSession.purged_comment_count を用いる。
- [x] T015 [US2] `src/live_rec_lab/db/queries.py` と `web/templates/recordings/` で全履歴の取得品質、実体照合、期限削除とコメント検索をまとめ、`tests/test_web_ui.py` でコメントのみ・前ジョブ失敗を試験（RISK-13、14）
- [x] T016 [US3] `src/live_rec_lab/web/pages.py` と `web/templates/`、`web/static/` で主要7画面と下位ビュー、検索、設定、ログを実装し、`tests/test_web_ui.py` に状態とナビの統合試験を設定（RISK-13〜15）
- [x] T017 [US3] `src/live_rec_lab/app.py` と `web/api.py`、`web/security.py` にHTTP契約を実装し、`tests/test_api.py` で変更の安全性・ファイル非公開・応答秘匿を検証
- [ ] T018 [US3] `specs/001-initial-live-recorder/traceability.md` に実ブラウザ幅1024／1280／1440px、重要下位ビュー、フォーム・ログ・表示・操作・拡大とアクセシビリティ検証を記録

## Phase 6 — US4 WSL2運用と復旧

**独立試験**: 一時Linuxパスで動的ポート・問い合わせ・二重起動・バックアップ・停止と再起動を検証。

- [x] T019 [US4] `src/live_rec_lab/cli.py`、`ops/backup.py`、`scripts/`、`.env.example` に生存照合、排他、停止、バックアップとWindows自動起動を実装し、`tests/test_operations.py` で再起動・旧DB再照合と権限を検証（RISK-06、15）
- [x] T020 [US4] `README.md` と `docs/ARCHITECTURE.md`、`docs/CONFIGURATION.md`、`docs/INSTALL_WSL2.md`、`docs/TROUBLESHOOTING.md`、`docs/LIMITATIONS.md` を現行動作に合わせる。Windows固有の実機検証を `traceability.md` に記録（RISK-05〜07、12、15）

## Phase 7 — 品質と収束

- [x] T021 `tests/test_traceability.py` で固定AC／TC／RISKの欠落とFeature相違時のゲートを検査し、`traceability.md` に原本の独立監査・テスト結果を記録
- [x] T022 `specs/001-initial-live-recorder/tasks.md` にConvergeの追加課題があれば追記し、最大3サイクルで再実装・再監査・全体テストを実行

### Dependencies and execution order

T001–T003 → T004。T005/T006は基盤の別ファイルで並行可能。T007/T008/T009/T010 → T011 → T012 → T013。T014/T015は基盤とジョブの契約に従う。T016/T017はこれらの業務操作を呼ぶ。T018はUIと運用起動後、T020は実際に起動・検証した結果を参照。T021/T022は全工程の証跡を照合する。

## Phase 8: Convergence

- [x] T023 `src/live_rec_lab/comments/srt.py` と `db/queries.py` で字幕スナップショットから生成までの書込ロックを短縮し、設定変更・公開前後の版数競合を `tests/test_srt.py` で確認する（FR-007、FR-016、TC-016、TC-056、partial）
- [x] T024 `src/live_rec_lab/web/pages.py` と `web/templates/system/index.html` で実行時に選択したメディアツール・バージョン、復旧保留、実ファイル検証の結果を表示する（FR-005、FR-009、FR-011、TC-021、TC-064、partial）
- [x] T025 `docs/INSTALL_WSL2.md` と `tests/test_retention.py` で旧バックアップの手動復元、監視停止状態での設定照合、期限処理後の再開を検証する（FR-010、FR-016、TC-038、TC-047、partial）
- [ ] T026 `specs/001-initial-live-recorder/research.md` と `traceability.md` に許可済み実配信、外部API枠と匿名番号、Windowsホスト・NTFS・到達性の実機証跡を記録する（FR-008、FR-016、FR-018、SC-001、SC-003、partial）

### 再開時の確認結果（2026-11-24）

- T007の状態遷移と監視枠は実装し、UNKNOWN／ENDING／孤立時の収束と手動停止のローカル回帰を確認。設定ON/OFFと障害の全組み合わせ、両サービスの連続監視頻度の統合検証が残る。
- T018ではChromiumで7画面・録画詳細の3幅、代表ダイアログ、設定・ログ・検索導線を確認。Windowsクライアントでの実フォント、全画面のアクセシビリティ監査と更新状態の組み合わせは未検証。
- T026の実配信、匿名番号・外部利用枠、WindowsのNTFS・ネットワークモード・サインイン／電源状態は試験条件を確保できていない。
- 全192件のローカルテスト、実起動CLI、Chromium、依存関係ロック、静的検査の実行結果は [要件追跡表](traceability.md#実行証跡) を参照。
