# Bug Fix: ツイキャスで途中終了したHLSの再試行

- **Slug**: twitcasting-preroll-only
- **Fixed**: 2026-11-24
- **Assessment**: ./assessment.md
- **Status**: partial

## Summary

FFmpegが途中終了して一部の動画が保存された場合でも、同じ終了タグ付きHLSプレイリストを直ちに再試行できるようにした。FFmpegの標準エラーとHLS資源の取得結果を関連付け、今回の実配信で観測された中断を次回の録画で切り分けられるようにした。

## Changes

| File | Change | Notes |
| --- | --- | --- |
| `src/live_rec_lab/recording/manager.py` | modified | 正常終了し、出力検証とHLS資源取得に問題がなかった場合に取得済みとして扱う。失敗した同じ有限プレイリストは初回用の待機を省き、既存の再接続上限内で再試行する。異常終了時の診断に秘匿処理済みの標準エラー、終了コード、書き込みバイト数を含める。 |
| `src/live_rec_lab/recording/hls_playlist.py` | modified | 初期化ファイルとメディアの取得結果をHTTPステータスなどの件数で記録する。診断には公開したプレイリストのメディアシーケンス、不連続と初期化タグの数も含める。取得結果やプレイリストの要約にURLや署名は含めない。 |
| `tests/test_recording.py` | modified | 異常終了とメディア404からの再試行、診断の秘匿処理と初期化・メディアの区別を検証する。 |

## Tests Added or Updated

- `test_twitcasting_unconfirmed_eof_preserves_video_and_recovers[interrupted_same_finite]` — FFmpegの終了コード183と保存済みの先頭映像を模擬する。URL署名が変わっても同じプレイリストを再試行し、映像を確定する。標準エラーのURLは伏せられる。
- `test_twitcasting_unconfirmed_eof_preserves_video_and_recovers[missing_media_same_finite]` — FFmpegの終了コードが0でもメディア取得が404なら再試行する。
- `test_mirror_diagnostics_distinguish_init_and_media_responses` — 初期化ファイルの200とメディアの404を区別し、署名が診断に含まれないことを確認する。

## Local Verification

- 修正前に同じ有限プレイリストの再試行が阻まれることと、初期化・メディアのHTTP結果が診断に残らないことをテストで確認した。
- `PATH="$PWD/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider --tb=short` — 213件通過、依存ライブラリの非推奨警告3件。
- `.venv/bin/ruff check .` と `git diff --check` — 通過。

## Deviations from Assessment

再発記録ではFFmpegの終了コード183がHLS起点の404に先行した。模擬入力で再現できた再試行の阻害を修正した。2026-11-24 11:52 JSTの実配信でFFmpegが最初に中断した直接原因は、当時の標準エラーと個々の取得結果がないため未特定。TC-067の実配信による先頭・本編・末尾の確認は未実施。前回の経緯は `fix.md` と `recurrence.md` に記録されている。

## Follow-ups

- `$speckit-bug-test slug=twitcasting-preroll-only` で検証結果を記録する。実配信で同じ症状が出た場合は、対応する録画試行のFFmpeg標準エラーと初期化・メディアの取得結果を照合する。
