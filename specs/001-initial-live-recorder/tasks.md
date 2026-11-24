# Tasks — 初期 LiveRecorder

## Phase 1 — セットアップと早期UI比較

- [x] T001 Python pinと依存版を確認しロック・同期する `pyproject.toml` `uv.lock`
- [x] T002 [P] 架空fixtureで初期UI比較3案を作る（FR-006） `specs/001-initial-live-recorder/ui/comparison.html` `specs/001-initial-live-recorder/ui/design.md`
- [x] T003 利用者の明示選択を記録する（FR-006） `specs/001-initial-live-recorder/ui/design.md`

## Phase 2 — 隔離と共通基盤

- [x] T004 専用一時領域の初期化、所有・パス検証、排他、構造化ログを実装・検証する（FR-007、FR-008、`data-model.md` の `SystemEvent.correlation_id`） `src/live_rec_lab/isolation.py` `tests/test_isolation.py`
- [x] T005 隔離CLI、SQLiteとfixture経路、通常／隔離の境界・固定ポートを実装する（FR-007、FR-008、FR-010、`data-model.md` の `RuntimeConfig.web_port`）。通常境界と起動はT015で確認する `src/live_rec_lab/cli.py` `src/live_rec_lab/runtime.py` `src/live_rec_lab/db.py` `tests/test_fixture.py` `tests/test_runtime.py`

## Phase 3 — US1 配信収集（P1）

- [x] T006 [US1] ローカルHLS／FFmpegから検証済み媒体を排他確定し固定命名する（FR-003、`data-model.md` の `RecordingSegment.number` と `RecordingSegment.media_generation`） `src/live_rec_lab/media.py` `tests/test_media.py`
- [x] T007 [US1] 模擬イベントの検知・登録、セッション、コメント保存と独立したジョブ状態を実装する（FR-001、FR-002、FR-004、`data-model.md` の `Streamer.identity`、`StreamSession.source_identity`、`Comment.recording_offset_ms`） `src/live_rec_lab/db.py` `src/live_rec_lab/collection.py` `tests/test_fixture.py`
- [x] T008 [US1] コメントSRTと、片方の異常・不明・無コメント・再実行のTikTok隔離通し試験を追加する（FR-002、FR-004、`data-model.md` の `SubtitlePublication.input_generation`） `src/live_rec_lab/subtitles.py` `tests/test_fixture.py`
- [x] T009 [P] [US1] 外部共通排他・永続予算・許可・待機を実装・検証する（FR-010、`data-model.md` の `RequestBudget.window`） `src/live_rec_lab/budget.py` `tests/test_budget.py`
- [ ] T010 [US1] TikTok検知・コメントと録画経路、ツイキャス公式API・HLSとコメント経路を結び、模擬入出力で両サービスを検証する（FR-001〜004、FR-010） `src/live_rec_lab/providers/` `tests/test_providers.py`

独立確認は架空の両サービス配信で録画・コメント・SRTと片方失敗時の結果を照合する。

## Phase 4 — US2 履歴・字幕・Web（P2）

- [x] T011 [P] [US2] 音声ジョブ・発話検索・音声SRT・統合ASSを実装・隔離検証する（FR-005、`data-model.md` の `SpeechCue.offset_range` と `SubtitlePublication.input_generation`） `src/live_rec_lab/speech.py` `src/live_rec_lab/subtitles.py` `tests/test_speech.py`
- [ ] T012 [P] [US2] 採用案非依存の検索・詳細・設定・保守のデータ契約、HTTP防御とモード制御を実装する（FR-004〜008） `src/live_rec_lab/web/app.py` `tests/test_web.py`
- [x] T013 [US2] 明示選択後に共通シェルと主要8画面、詳細、操作・状態表現を実装する（FR-006） `src/live_rec_lab/web/templates/` `src/live_rec_lab/web/static/`
- [x] T014 [US2] 1440／1024と両フォント、代表動線、空・異常・更新競合を隔離ブラウザで確認する（FR-006、FR-007） `tests/test_web.py` `specs/001-initial-live-recorder/ui/design.md`

独立確認はfixtureから8画面、録画・コメント・発話検索、字幕の生成結果に到達する。

## Phase 5 — US3 運用とデータ保全（P3）

- [ ] T015 [P] [US3] 通常／隔離の起動・所有子停止・読取診断、固定ポート、Windows起動、容量・ログ・設定を証明する（FR-007、FR-008） `src/live_rec_lab/runtime.py` `src/live_rec_lab/cli.py` `manual/README.md` `tests/test_runtime.py`
- [ ] T016 [US3] 概要・詳細に影響する公開／削除台帳、期限・容量削除、整合バックアップとマーカー付き復元を実装し、再試行と置換を検証する（FR-003、FR-009、`data-model.md` の `DeletionOperation.generation` と `SubtitlePublication.input_generation`） `src/live_rec_lab/maintenance.py` `tests/test_maintenance.py`
- [ ] T017 [US3] 許可が揃った機能について通常Windows媒体・実配信・実機を検証し、利用可否を記録する（FR-001、FR-003、FR-005、FR-010） `specs/001-initial-live-recorder/traceability.md`

独立確認は他インスタンスや旧版に対する無変更、停止・復帰と手動復元時の安全性を照合する。

## Phase 6 — 統合

- [x] T018 全AC／TCを機能単位で照合し、機能別の実装・検証・利用可否と残課題を記録する `specs/001-initial-live-recorder/traceability.md` `README.md` `manual/README.md`

## Dependencies & Execution Order

T002は計画直後に独立実施。T004 → T005の最小隔離 → T006／T007 → T008を優先。T009は同じ実通信経路を変更しない範囲で独立。T010はT008・T009後。T011とT012の主要部分は隔離fixtureを使って外部実証を待たずに進められる。T013だけT003の明示選択とT012を要し、T014はT013後。T015・T016は開発fixtureを使って進めるが通常利用には各安全条件とT017の該当実機結果が必要。T018は一通りの実装と代表試験後。

## Implementation Strategy

最小TikTok縦断を先にテストし、ツイキャス・音声・共通管理へ拡張する。各実装の入口で代表テストを先に追加し、失敗理由を確認した後に実装と回帰を行う。固定IDは追跡表で軽く管理する。

## 再開点

- L1到達。両サービスの模擬経路、3種の字幕、主要8画面、運用CLIと隔離保守を実装・検証した。実配信録画・通常Windows媒体・未実装MUSTが残るためL2は部分到達。
- T005とT015では通常／開発者の設定・管理領域分離、8082／8083、所有確認付き起動・停止、読取診断を一時データで確認。T015の残件はWindowsログオンタスク登録・再起動・ロック中の実測と、詳細診断。
- T010は両サービスで模擬検知からローカルHLS録画、SQLiteコメント、コメントSRTまで接続した。実配信録画入力・署名、参照先検証、プリロール、欠損の実運用は残件。
- T011は独立した音声・ASSジョブ、世代確認と再生成、発話検索を隔離検証。T017でモデルの取得とCPU英語／CUDA英語・日本語の既知音声認識を確認。外部プレイヤー同期と長時間負荷は未検証。
- T012は運用設定、媒体削除、ストレージ照合のWeb操作を追加。録画品質・エンジン選択、停止指示、コメントSRTの設定変更と再同期、配信者カスタム順等が残件。
- T016は隔離でのファイルのみ／ファイル＋履歴削除、自動容量・期限処理、世代付き台帳と中断復旧、DB手動復元の準備・照合・差戻しを確認。Windows専用試験で録画確定の非上書きを確認した。削除退避方式がWindowsで成立しなかったため通常媒体を保留。通常DBの媒体・外部予算横断での復元実測も残件。
- T017の実配信は対象指定済み。専用設定、外部枠の共同利用確認、終了条件の確定と採用経路の安全条件を揃えてから行う。
- T018では正本の全83 AC・111 TCを機能群へ割り当て、実装・確認結果・利用保留を[追跡表](traceability.md)に記録した。

## 検証記録

- 2026-11-24、Spec Kitの前提チェックと読取Analyzeで10機能要件・11制約フィールドを18タスクの実装先に照合。仕様品質チェック7/7。
- Ruff lint／formatと、外部接続を使用しない並列pytest 47件に合格。serialは選択対象0件。live／hardwareは未実施。
- インストール済みCLIで両サービスのHLS、3種字幕、診断、SQLite Online Backup、使用中ポートの拒否、自分の隔離サーバーのHTTP応答を確認した。コアCLIは音声依存なしのPython 3.12でも実行した。
- 比較HTMLはChromium 1024px／1440pxで切替・架空セッション詳細を確認。採用UIと両フォントは未検証。

- 採用Aの回帰は並列pytest 50件、Ruff lint／format、JavaScript構文、wheelのテンプレートと静的資産を確認。Chromiumで1024／1440px・両フォント設定、配信者の登録／編集／削除、Escのフォーカス復帰、履歴の検索条件、コメント／発話検索、更新延期、空・長文・複数ページ・警告、CSPと同一オリジンを確認。

- 残作業を再開後、外部接続を使わない並列pytest 74件とRuff／JavaScript構文チェックが通過。Chromiumで音声設定、予約、隔離CLIワーカーによるモデル不足時の保留と既存字幕の維持を確認。実配信、署名通信、モデル取得、実音声認識、Windows媒体への書込みと実復元は未実施。
- 固定ポートと停止中の隔離DB復元を追加後、並列pytest 85件が通過。serial対象なし。Chromiumの1024px・1440pxで次回ポートの保存、稼働ポートの維持、再起動要否を確認。異なるDB・WAL付きDBの再開拒否と退避DBへの差戻しを一時領域で確認した。
- 中断からの再開後、最新の並列pytest 135件が通過。serial対象なし。Chromiumの1024px・1440pxで固定ポートと運用設定の保存、隔離のストレージ照合・削除を確認。通常ルートの公開範囲と開発者Swagger UI／ReDocのローカル資産を確認した。
- `large-v3` を固定リビジョンから取得し、CPU・CUDAの推論、合成日本語の音声SRTと統合ASS、FFplayの無画面再生を確認。Windowsの録画確定試験と利用保留は[追跡表](traceability.md)に記録した。
