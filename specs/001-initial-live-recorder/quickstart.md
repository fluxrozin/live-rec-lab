# Quickstart — 初期 LiveRecorder

## 録画・字幕を隔離環境で試す

WSL2にPython 3.12、uv、`/usr/bin/ffmpeg` と `/usr/bin/ffprobe` を用意し、リポジトリルートで実行します。空の専用ディレクトリを新規作成します。

```bash
uv sync --locked
fixture_root="$(mktemp -d)/live-rec-lab-fixture"
uv run --locked live-rec-lab fixture init "$fixture_root"
uv run --locked live-rec-lab fixture run "$fixture_root"
uv run --locked live-rec-lab fixture run "$fixture_root" --service twitcasting
uv run --locked live-rec-lab fixture postprocess "$fixture_root"
uv run --locked live-rec-lab fixture diagnose "$fixture_root" --json
uv run --locked live-rec-lab fixture backup "$fixture_root"
```

動画と字幕は `$fixture_root/recordings/`、データベースは `$fixture_root/data/`、ログとバックアップは `$fixture_root/state/` にあります。JSONで各処理の結果とファイルを確認できます。音声SRTとASSの発話は架空のcueです。診断はデータを書き換えません。旧バックアップには、それを取得した時点のコメントが含まれます。

## 採用Aの管理画面と隔離API

```bash
uv run --locked live-rec-lab fixture serve "$fixture_root"
```

起動後に [127.0.0.1:8083](http://127.0.0.1:8083/) を開きます。ポートが使用中の場合は、専用の未使用固定ポートを `--port` で指定します。取得結果は `/api/v1/sessions`、コメントは `/api/v1/comments?mode=all` で参照できます。終了は起動したシェルで Ctrl+C を使用します。

配信者一覧で対象の追加・編集・論理削除、録画詳細でセグメントと字幕状態、コメントと発話本文で検索を試せます。設定の「表示」でフォント・タイムゾーン・ダッシュボードの更新間隔を保存できます。[採用Aの設計記録](ui/design.md)。

## 隔離の音声ジョブ

「音声字幕」で確定セグメントの生成・再生成を予約し、別のシェルから同じ隔離ルートの待機ジョブを1件処理します。CLIで予約する場合は、詳細画面またはセッションAPIで対象のセグメントIDを確認します。

```bash
uv run --locked live-rec-lab fixture speech-queue "$fixture_root" 1
uv run --locked live-rec-lab fixture speech-work "$fixture_root"
uv run --locked live-rec-lab fixture speech-status "$fixture_root"
```

自動投入が有効な場合は、`speech-work --automatic` で未処理の確定録画を待機列に加えて1件処理できます。認識に必要なローカルモデルが未準備の場合は `blocked / model_unavailable`、録画に音声ストリームがない場合は `not_applicable / no_audio` を記録します。モデルと音声extraが用意できた場合、推論子は `$fixture_root/data/models/<設定したモデル名>/` を専用Linux領域として参照します。モデルの取得は別の許可付き準備工程です。

## 隔離バックアップの事前照合

`fixture backup` が返したバックアップのファイル名を指定します。

```bash
uv run --locked live-rec-lab fixture restore-check "$fixture_root" <バックアップのファイル名>
```

候補を現行DB・媒体世代・コメント期限処理の記録と照合し、問題があれば `blocked` と理由別件数を返します。`reviewable` は照合項目が通過した状態です。このコマンドによるDB差し替えや媒体の変更はありません。

## 回帰

```bash
uv run --locked ruff check .
uv run --locked pytest -n 2 -m "not serial and not live and not hardware"
```

音声ライブラリも同期する場合は `uv sync --locked --extra speech` を使います。モデル取得、実配信・署名、Windows通常録画先の操作、GPU試験は、この手順に含まれません。
