# Bug Verification: ツイキャスでプリロールしか保存されない

- **Slug**: twitcasting-preroll-only
- **Tested**: 2026-11-24
- **Assessment**: ./assessment.md
- **Fix**: ./fix.md
- **Latest fix**: ./fix-initialization.md（中間修正は ./fix-followup.md）
- **Result**: verified

## Summary

最新修正を反映した稼働環境のツイキャス実配信で、6.037秒のプリロールと、79.872秒の本編が別ファイルとして同じ録画ジョブに保存された。両ファイルの映像と音声をFFmpegで最後までデコードでき、録画ジョブは `COMPLETED`、録画欠損の記録は0件だった。

## Checks Performed

| Check | Command / Action | Result | Notes |
| --- | --- | --- | --- |
| Reproduction (post-fix) | 最新変更後に起動したインスタンスのSQLiteを読み取り専用で照合。ジョブ4の保存済みMP4を `ffprobe`、SHA-256、全編デコードで検証 | pass | 12:23:03 JSTにインスタンスが起動し、12:23:53に録画開始。12:25:44に `COMPLETED`、終了理由は `platform_end`。1本目は修正前に残ったプリロールとSHA-256が一致。2本目は別内容で、映像は1,198フレーム、音声は79.872秒。 |
| 新規の実配信接続 | 保存済みの修正後の実録画を使用 | skipped | 追加のツイキャス接続や録画の開始・停止操作は行わなかった。 |
| New / updated tests | `PATH="$PWD/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider tests/test_recording.py tests/test_twitcasting.py -k 'mirror or initialization or unconfirmed_eof or preroll or redirect' --tb=short` | pass | 終了済みHLSとライブ更新中のHLSの初期化切替、404からの復旧、プリロールと通常冒頭の保全、URL転送制限を検証。終了コード0、23件通過。 |
| Regression suite | `PATH="$PWD/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider --tb=short` | pass | 終了コード0、216件通過。依存ライブラリから非推奨警告3件。 |
| Lint / diff | `.venv/bin/ruff check .`、`.venv/bin/ruff check --no-cache src/live_rec_lab/recording/hls_playlist.py src/live_rec_lab/recording/manager.py tests/test_recording.py`、`git diff --check` | pass | すべて終了コード0。 |
| キャッシュなしの全体Lint | `.venv/bin/ruff check --no-cache .` | fail（既存） | 終了コード1。今回未変更の `alembic/env.py:3` に `I001` が1件。GitのHEADから読み出した同ファイルにも同じ指摘を再現した。 |

全編デコードには FFmpeg 6.1.1 を使用した。各MP4を `ffmpeg -nostdin -v error -xerror -i <MP4> -map 0:v:0 -map 0:a:0 -c:v rawvideo -c:a pcm_s16le -f nut pipe:1` で処理し、出力は破棄した。両ファイルとも終了コード0、標準エラー出力なし。別途 `-f null` で出力した確認では、終了コード0だったが出力muxerのDTS単調性診断が出た。

## Output Excerpts

```text
job 4  COMPLETED  platform_end  recording_gaps=0
segment 1  6.037375s   H.264 1280x720 + AAC  matches_previous_preroll=True
segment 2  79.872500s  H.264 704x396  + AAC  matches_previous_preroll=False
23 passed, 48 deselected
216 passed, 3 warnings
```

## Residual Risks

- 修正後の実配信は1件。公式APIによる配信終了の正確な時刻がDBに残っていないため、配信元との先頭・末尾のフレーム単位での比較はできていない。
- キャッシュなしの全体Lintに、既存のAlembicファイルのインポート順の指摘が残る。

## Recommendation

今回の再現条件では、プリロールだけで録画が終わる不具合を検証済みとする。配信パターン全般での冒頭・末尾の欠落有無は、複数の公開配信を使う別の検証事項として扱う。
