# Bug Fix: ツイキャスHLSの初期化情報切替で録画が中断する

- **Slug**: twitcasting-preroll-only
- **Fixed**: 2026-11-24
- **Assessment**: ./assessment.md
- **Status**: partial

## Summary

HLSの初期化情報が変わったとき、従来のFFmpegには境界までのプレイリストを渡す。取得済みの後続プレイリストは新しいFFmpeg入力として同じ録画ジョブ内で録画し、切替前後を別々のセグメントとして確定する。

## Changes

| File | Change | Notes |
| --- | --- | --- |
| `src/live_rec_lab/recording/hls_playlist.py` | modified | `#EXT-X-MAP` のURIパスまたはバイト範囲が変わる場合に前後を分ける。プレイリスト更新で先頭の初期化情報が変わった場合も切り替える。後続のメディアシーケンス、既存の不連続タグ、先頭の暗黙バイト範囲を引き継ぐ。 |
| `src/live_rec_lab/recording/manager.py` | modified | 初期化切替で検証済みの後続プレイリストを次の録画試行へ引き継ぐ。この切替にはネットワーク障害用の再接続回数や待機を適用しない。 |
| `tests/test_recording.py` | modified | 異なる初期化情報を持つ二つの合成fMP4を、実FFmpegと一時SQLiteを使う録画経路で検証する。 |

## Tests Added or Updated

- `test_twitcasting_keeps_both_sides_of_an_initialization_change` — 起動時の有限プレイリストと更新中のライブプレイリストで、それぞれ切替前後の二つの映像を同じジョブで確定する。再接続カウントは0。
- `test_mirror_preserves_unclassified_prefix_and_queues_following_media` — 識別情報がない先頭も保存対象に含め、後続のシーケンスと不連続タグを維持する。
- `test_split_preserves_implicit_media_range_offset_after_initialization_change` — 切替直後の暗黙バイト開始位置を引き継ぐ。

## Local Verification

- 修正前の合成HLSでは、初期化情報が混在した入力から有効なファイルが1件しか残らなかった。別のローカル対照実験でも、初期化情報を切り替えた後のフレームで FFmpeg の `missing picture in access unit` を再現した。
- `PATH="$PWD/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider --tb=short` — 216件通過、依存ライブラリの非推奨警告3件。
- `.venv/bin/ruff check .` と `git diff --check` — 通過。

## Deviations from Assessment

2026-11-24 12:13 JST開始の録画ジョブ3では、初期化ファイル9リクエストとメディア11リクエストがHTTP 200だった。FFmpegは `Invalid NAL unit size` を繰り返して183で終了し、DBには2.104秒の有効な出力1件が残った。前回の再接続対策を読み込んだプロセスでの再発だった。

今回の対応は、HTTP取得後の入力境界を扱う。実配信で初期化URIが切り替わった時系列は履歴に残っていないため、実ジョブの直接原因と本編の収録は未確認。前回の `fix.md`、`recurrence.md`、`fix-followup.md` は履歴として保持する。

## Follow-ups

- `$speckit-bug-test slug=twitcasting-preroll-only` で、最新の録画プロセスによる許可済み実配信の先頭・本編・末尾を確認し、TC-067の結果を記録する。
