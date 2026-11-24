# Bug Fix: ツイキャスのプリロールだけで録画が終わる

- **Slug**: twitcasting-preroll-only
- **Fixed**: 2026-11-24
- **Assessment**: ./assessment.md
- **Status**: applied

## Summary

ツイキャスの初期HLSが本編へ切り替わるまで待ち、メディアプレイリストが404になった場合は公式HLSの起点から再解決する。FFmpegの終了後も、公式監視が配信中を示して本編の終了を確認できない間は同じ録画ジョブ内で再接続する。

## Changes

| File | Change | Notes |
|------|--------|-------|
| `src/live_rec_lab/recording/hls_playlist.py` | modified | 起点URLとメディアURLを保持し、404時は起点から再解決する。別のメディアやシーケンスの巻き戻りは次のFFmpeg入力で処理する。キュー、または不連続とメディア・初期化URIの組み合わせで識別できるプリロールを除く。 |
| `src/live_rec_lab/recording/manager.py` | modified | 終了判断にHLSと公式監視の状態を使う。既存の同一Movie IDの再確認と再接続上限を適用し、既存の録画確定処理で取得済みメディアを保存する。HLS取得段階・HTTPステータス、プレイリスト進行、セグメント数、終了タグ、FFmpeg終了区分をURLを含めずに記録する。 |
| `tests/test_recording.py` | added tests | 模擬HLS、実FFmpegのローカル録画、一時SQLiteによるジョブ確定を検証する。 |

初期に終了タグ付きのプレイリストしか得られず、本編の根拠がない場合は既存の待機時間内で更新を待つ。待機後も同じ候補しかなければ、取得可能な映像を保全する。再試行では署名クエリが変わっても保存済みの同じ有限プレイリストを識別し、異なる短いプレイリストは取得候補に含める。先頭を削除する判断に秒数は使わない。

本編境界を確認できず、ライブ形式のプレイリストが終了タグを出した場合は、配信状態の確認を優先する。公式監視で終了を確認できれば通常どおり確定し、別のプレイリストから収録を再開した場合や再試行上限まで確認できなかった場合は欠損を記録する。

## Tests Added or Updated

- `tests/test_recording.py::test_initialization_change_identifies_preroll_with_unmarked_media_name` — 不連続と初期化URIの変化で、無印のプリロール用メディアを識別する。既存の本編冒頭を維持するケースも実行した。
- `tests/test_recording.py::test_mirror_waits_for_main_when_finite_variant_disappears` — 6秒の初期リストと404の後、マスターから本編へ移行する。ループバック経由で実FFmpegを起動し、`ffprobe` で本編の映像と音声を検証する。
- `tests/test_recording.py::test_mirror_drains_active_variant_when_replacement_restarts_sequence` — 録画中にバリアントが変わったとき、番号を混在させず、旧入力に公開済みのメディアを読み終えられる。
- `tests/test_recording.py::test_mirror_does_not_confirm_end_after_only_playlist_progress` — 無印の先頭が複数セグメントに増えても、本編の終了を確認済みにしない。
- `tests/test_recording.py::test_twitcasting_unconfirmed_eof_preserves_video_and_recovers` — 取得済み動画の確定と再開、異なる短い本編の保存、同じ候補の重複回避と上限時の欠損、配信終了と手動停止、FFmpeg終了後に公式監視の終了が届く場合の通常確定を検証する。

## Local Verification

- 変更前の対象テストで、6秒のプレイリストの採用、誤った `COMPLETED`、異なる短い本編の取りこぼしを確認した。
- `PATH="$PWD/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider --tb=short` — 210件通過。依存ライブラリの非推奨警告が3件。
- `.venv/bin/ruff check .` と `git diff --check` — 通過。
- TC-020は模擬入力で検証した。TC-067の公開配信を使った本編・先頭・末尾の検証は未実施。

## Deviations from Assessment

- 実配信のプレイリスト履歴とFFmpeg終了コードは未取得のため、当日の404がどの段階で発生したかは未確定。評価書で再現した経路と、レビューで再現した無印のプリロールが増えてから終了する経路を修正した。
- 更新だけでは本編への移行を確定できないため、明示的な境界の根拠に公式監視の終了確認を組み合わせた。有限候補が判別できない場合の保全と、再取得時の重複抑制を追加した。

## Follow-ups

- `$speckit-bug-test slug=twitcasting-preroll-only` で検証結果を記録する。許可された公開配信では、先頭・本編・末尾と録画ジョブの状態を照合する。
