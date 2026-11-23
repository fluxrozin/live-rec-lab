# 07. 資料間の不整合・要決定事項

> 対象：GitHub `main` / `d3f2b3a83c0895ecfbe136d8d997dfc611d575ff`（2026-11-23確認）。この文書は**検証・解決の作業台帳**。未確定事項を勝手に確定させない。

## A. 確認されたリポジトリ内の不一致

2026-11-23 に解消した。抽出基準コミットの時点では未修正だった。

| ID | 不一致（修正前） | 対応 | 状態 |
| --- | --- | --- | --- |
| GAP-01 | `requirements.md`はPython **3.12.x**を明示。一方 `.python-version` は `3.14`、`pyproject.toml`は `requires-python = ">=3.14"` | `.python-version` を `3.12`、`requires-python` を `>=3.12,<3.13` にした。`uv lock` は `requires-python = "==3.12.*"`、`uv sync --locked` と `uv run` は CPython 3.12.13 で通過した | 解消 |
| GAP-02 | `requirements.md` §10.1.1.1 と実リポジトリは `src/live_rec_lab/`。ただし§10.1.1.2, §10.1.1.7等に `src/live_recorder/web/` が残る | §10.1.1.2 と §10.1.1.7 を `src/live_rec_lab/web/` に統一した | 解消 |
| GAP-03 | `requirements.md` のDB・ログ・runtimeディレクトリは通常 `live-rec-lab`。一方 `README.md` に `live-recorder` が残る | 実行時ディレクトリはリポジトリ名 `live-rec-lab` に合わせ、`README.md` もこれに統一した。アプリケーション名は LiveRecorder。Windows の録画フォルダ例 `D:\LiveRecorder\recordings` はその動画と SRT の保存先であり、実行時ディレクトリとは別パス。`requirements.md` §10.1、`README.md`、開発憲法に記載した | 解消 |
| GAP-04 | `.specify/memory/constitution.md` はSpec Kitのテンプレートで、開発原則が未記入 | 要件と `AGENTS.md` に沿う開発憲法 v1.0.0 を制定した | 解消 |
| GAP-05 | `docs/UI_UX.md` は**ライト系1テーマのみ**。後日2テーマを採用する場合は原本の変更が必要 | ライト系1テーマを採用する。2テーマ化は `requirements.md` と `docs/UI_UX.md` を先に改訂するまで行わない。原本は変更していない | 解消 |

## B. 機能実装前に要検証

| ID | 不確実性 | 検証方法と成功基準 |
| --- | --- | --- |
| RISK-01 | TikTokLive/Euler Streamの実際の無料枠、署名・接続・再試行時の消費 | API操作ごとのHTTP応答、実際の消費、429、接続数、利用条件を記録 |
| RISK-02 | TikTokのライブ検出・録画ストリームの利用可能性 | 実配信で検知・録画・再接続・ffprobeを検証（権限範囲内） |
| RISK-03 | ツイキャスBasic認証、コメント増分・レート枠の共有単位 | 実APIレスポンスとレート制限ヘッダーから挙動を記録 |
| RISK-04 | ツイキャス匿名番号の視聴用WebSocket取得・API照合 | 実配信で同一Movie／コメントID対応、匿名番号の正確さを検証。曖昧ならNULL |
| RISK-05 | SRTの正確な0秒同期と未確定/分割録画の復旧 | PTS・first media時刻・補正値を実測し、同期法ごとの誤差を報告 |
| RISK-06 | WSL2 NAT/mirroredのWindows localhost到達性 | 両構成で動的ポート・プロセス生存確認を実機試験 |
| RISK-07 | 同一NTFSボリューム内rename、強制停止直前/直後のDB乖離 | 障害注入と再起動照合をテストし、上書き・消失がないことを確認 |

## C. 機能Specで具体化すべき未決定事項

- セッションIDが取得不能のときの識別・終了猶予と再開規則。
- 録画／コメントの**完全な状態遷移表**、タイムアウト、手動停止・再開条件。
- SQLiteの完全なDDL、インデックス、削除時の参照整合性、保存期間。
- `/api/v1`の個々のパス、メソッド、入出力、例外、idempotency。
- SRTのコメント表示秒数既定値、映像0秒と絶対時刻が一致しないケースの補正・失敗条件。
- Windowsサインイン後の起動方法、プロセス管理と終了時タイムアウト。

## D. 次の段階と扱い

1. **済（2026-11-23）** 既存原本の不一致（A）を解消し、Python 3.12、パッケージ名 `live_rec_lab`、実行時ディレクトリ `live-rec-lab`、ライト系1テーマを一致させた。リポジトリ名は `live-rec-lab`、アプリケーション名は LiveRecorder。
2. **済（2026-11-23）** `.specify/memory/constitution.md` を v1.0.0 の開発原則にした。
3. 外部依存のPOCを必要最小限実施し、実機結果を `docs/research/` または該当featureの `research.md` に保存。
4. 小さなvertical sliceごとに `specs/NNN-feature/` を作り、`spec.md`→`plan.md`→`tasks.md`→実装・テストを進める。
5. 機能・設計変更が生じたら原本と本設計、機能Spec、受入テストを整合させる。

**注意**：B と C は未検証・未決定のままである。A の解消は抽出基準コミットより後の作業ツリーで行った。
