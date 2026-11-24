# Implementation Plan — 初期 LiveRecorder

**Branch** `trial/full-sdd-03`  
**Date** 2026-11-24  
**Spec** [spec.md](spec.md)

## Summary

Windows 11／WSL2 の単一利用者向けに2サービスの収集・3種字幕・8画面を実装する。最初に隔離されたTikTok相当の検知→録画／コメント→SQLite→コメントSRTを通す。計画直後の比較案は架空データのみとし、UI選択待ちもコアを実装する。

## Technical Context

- Python `>=3.12,<3.13`、uv、SQLite WAL。最小fixture CLIは標準ライブラリのSQLiteを使い、既存の要件に沿って共通スキーマとマイグレーションへ成長させる。
- FastAPI、Uvicorn 1 worker、Jinja2・HTML・軽量JS。通常ポート8082、隔離8083、127.0.0.1のみ。
- FFmpeg／ffprobe の実行ペア、Streamlink第一候補、yt-dlp代替。TikTokLive、Euler Stream、ツイキャス公式API。音声は後段に faster-whisper（GPU既定、明示CPU）。
- pytest／xdistとRuff。実配信と実機は許可付き独立試験。監視上限は10／60回毎分。SQLiteはLinux、通常の録画はWindows、隔離の録画は専用Linux領域。

## Constitution Check

| 原則 | 設計時に確認した境界                                                                             |
| ---- | ------------------------------------------------------------------------------------------------ |
| I    | 両サービス・3種字幕・8画面を維持。最小隔離と比較3案から開始する。                                |
| II   | 独立収集、パス実体、非上書き、世代、公開・削除台帳、整合バックアップと停止中復元をタスク化する。 |
| III  | LIVE・OFFLINE・UNKNOWN、未収集と取得済み0件を区別する。固定IDの確認は統合時にまとめる。          |
| IV   | 実モード切替前にデータ境界・HTTP防御・外部許可と予算を検証する。                                 |
| V    | JSONログ・読取専用診断・実行中プロセスの所有確認・環境差を運用タスクに含める。                   |

設計後の照合でも安全条件を各利用開始前の試験へ割り当てた。隔離fixtureの結果から通常Windows媒体の対応可否は推定しない。

## Project Structure

### Documentation

[research.md](research.md)、[data-model.md](data-model.md)、[contracts](contracts/)、[quickstart.md](quickstart.md)、[tasks.md](tasks.md)、[UI設計記録](ui/design.md)、[追跡表](traceability.md)。

### Source Code

| パス                                             | 責務                                                 |
| ------------------------------------------------ | ---------------------------------------------------- |
| `src/live_rec_lab/cli.py`                        | モードを先に選ぶCLI、隔離fixture、読取診断、制御     |
| `src/live_rec_lab/isolation.py` ・ `runtime.py`  | fixture専用領域、通常／隔離設定、パス境界、排他      |
| `src/live_rec_lab/db.py` ・ `collection.py`      | SQLite、登録・セッション・コメント、検知と独立ジョブ |
| `src/live_rec_lab/media.py` ・ `subtitles.py`    | FFmpeg／ffprobe、命名、排他的確定、字幕              |
| `src/live_rec_lab/providers/` ・ `budget.py`     | 各社イベント、通信の共通排他と永続枠                 |
| `src/live_rec_lab/speech.py` ・ `maintenance.py` | 文字起こし・ASS、保全・復元・期限処理                |
| `src/live_rec_lab/web/`                          | HTTP防御、読取・操作契約、選択案の8ビュー            |
| `tests/` ・ `manual/`                            | 代表fixture・検証、実行と保守の手順                  |

## Review Focus

- 配信確認失敗とオフラインを区別する（T008）。
- 片方の異常時に他方が保存される（T008）。
- 外部送り先・通常領域の指定をfixtureから拒否する（T004、T005）。
- 既存同名・改変世代・復元中断で取得済み成果物を保全する（T006、T016）。
- 過去の取得結果と現在の障害を画面で両方判別できる（T012、T014）。

## Complexity Tracking

単一Feature・単一管理プロセスのまま進める。UIの構造は比較対象として隔離し、選択前は製品スキン・ナビの実装を保留する。媒体書込・実接続・復元は採用環境における安全条件の成立を個別に判定する。
