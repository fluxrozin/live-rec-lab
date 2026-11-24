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

起動後にコンソールに表示されたURL（既定は [127.0.0.1:8083](http://127.0.0.1:8083/)）を開きます。別シェルで同じ隔離ルートを指定すると、稼働状態・URLを確認して停止できます。起動したシェルで Ctrl+C も使えます。

```bash
uv run --locked live-rec-lab fixture status "$fixture_root"
uv run --locked live-rec-lab fixture url "$fixture_root"
uv run --locked live-rec-lab fixture stop "$fixture_root"
```

「設定」の「詳細設定」で保存したポートは次回起動時に適用されます。使用中のポートでは起動に失敗します。停止中に下記のコマンドで現在の設定を確認し、専用の未使用固定ポートに変更できます。`serve --port` の指定はその起動に限り有効です。

```bash
uv run --locked live-rec-lab fixture config "$fixture_root" get web_port
uv run --locked live-rec-lab fixture config "$fixture_root" set web_port 18083
```

取得結果は `/api/v1/sessions`、コメントは `/api/v1/comments?mode=all` で参照できます。

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

## 隔離DBの手動復元

専用の一時領域にある架空データに対する手順です。Webサーバーと音声などの処理を終了してから、同じ隔離ルートのバックアップのファイル名を指定してください。

```bash
uv run --locked live-rec-lab fixture restore-prepare "$fixture_root" "$snapshot"
```

`prepared` が返ると現行DBの退避版が `rollback` に示され、復元未完了マーカーが作られます。以後、サーバー起動や通常処理は拒否され、`fixture diagnose` は復元中の状態を表示します。

交換を行うときは、`data/` に `fixture.sqlite3-wal`、`fixture.sqlite3-shm`、`fixture.sqlite3-journal` がないことを確認してください。候補ファイルのコピーを同じ `data/` 内の一時ファイルに作成し、コピーが完了してから `fixture.sqlite3` に名称変更して置き換えます。差し戻す場合は候補の代わりに `rollback` のファイルを同じ手順で使用します。

```bash
staged="$(mktemp "$fixture_root/data/.restore-XXXXXX")"
cp -- "$fixture_root/state/backups/$snapshot" "$staged" &&
  mv -T -- "$staged" "$fixture_root/data/fixture.sqlite3"
uv run --locked live-rec-lab fixture restore-finish "$fixture_root"
```

`restore-finish` は交換したDBを退避時点の記録・媒体世代・期限処理・処理履歴と再照合します。確認が通るとマーカーを解除します。照合できない場合はマーカーが残るので、退避版に差し戻してから再照合できます。

## 回帰

```bash
uv run --locked ruff check .
uv run --locked pytest -n 2 -m "not serial and not live and not hardware"
```

音声ライブラリも同期する場合は `uv sync --locked --extra speech` を使います。モデル取得、実配信・署名、Windows通常録画先の操作、GPU試験は、この手順に含まれません。
