# LiveRecorder コマンドリファレンス

Windows の WSL 上で、リポジトリ直下から実行する。導入・画面操作・復元の手順は [運用マニュアル](README.md) を参照。

構文中の `[ ]` は省略可能、`< >` は置き換える引数を表す。製品 CLI は `uv run --locked live-rec-lab <コマンド>` で実行する。音声字幕を使う起動では `uv run --locked --extra speech live-rec-lab start` を使う。

全ての製品サブコマンドで、`--dev` を省略すると通常環境、指定すると隔離環境を対象にする。隔離インスタンスを確認・停止・復元する場合は、各コマンドにも `--dev` を指定する。

## 起動・確認・停止・再起動

| 構文（`live-rec-lab` に続く部分） | 用途・結果 |
| --- | --- |
| `start [--dev] [--allow-live]` | 指定モードで起動する。既定 URL は通常が `http://127.0.0.1:8082`、隔離が `http://127.0.0.1:8083`。 |
| `status [--dev]` | 稼働中は URL・PID・インスタンス ID・起動時刻を表示して終了コード `0`。停止中は `1`。 |
| `url [--dev]` | 稼働中の URL だけを標準出力に表示する。稼働していなければ終了コード `1`。 |
| `stop [--dev]` | SIGTERM を送り、停止を最大 30 秒待つ。停止済みまたは停止確認は `0`。確認できなければ `1`。 |
| `restart [--dev] [--allow-live]` | 正常停止を確認して、同じモードで前面起動する。停止済みなら起動する。停止を確認できなければ `1` で終了する。 |
| `diagnose [--dev] [--json]` | 読取専用診断。`--json` で機械可読形式。終了コードは `0`＝正常、`1`＝一部確認不可、`2`＝診断不可。 |
| `--help` / `<コマンド> --help` | コマンド一覧または各コマンドのヘルプを表示する。 |

`--allow-live` は隔離起動で実接続の許可を受けた場合に指定する。一回の起動にだけ適用され、再起動でも必要な場合は明示する。

```bash
uv run --locked --extra speech live-rec-lab start
uv run --locked live-rec-lab status
uv run --locked live-rec-lab url
uv run --locked live-rec-lab diagnose --json
uv run --locked live-rec-lab stop
```

起動中の操作は別の WSL シェルから実行する。`--dev` 付きの一連の実行例は [隔離環境の起動・確認・停止](isolated.md) を参照。

## 設定

| 構文 | 用途・結果 |
| --- | --- |
| `config get web_port [--dev]` | 保存済みのポート、未設定ならモードの既定値を表示する。 |
| `config set web_port <ポート> [--dev]` | 停止中に 1024〜65535 のポートを保存する。次回起動から反映する。 |

```bash
uv run --locked live-rec-lab config get web_port
uv run --locked live-rec-lab config set web_port 8090
```

CLI で扱う設定キーは `web_port`。その他の運用設定は Web の「設定」で操作する。

## バックアップ・復元

| 構文 | 用途・結果 |
| --- | --- |
| `backup <保存先> [--dev]` | SQLite DB のバックアップを作成する。稼働中も実行可能で、既存ファイルへの上書きは拒否する。 |
| `restore prepare <候補DB> [--dev]` | 停止中に候補を検査し、現 DB を退避して復元用のマーカーを作る。 |
| `restore finish [--dev]` | 手動で DB を交換した後、整合性を検査して復元を確定する。 |
| `restore abort [--dev]` | 復元を中止し、退避した DB に差し戻す。 |

バックアップにはコメント本文などの個人情報が含まれる。復元前に [運用マニュアルの手動復元手順](README.md#5-容量削除照合バックアップ復元) を確認し、同じモードで各ステップを実行する。

```bash
uv run --locked live-rec-lab backup "$HOME/live-rec-lab-backup.sqlite3"
uv run --locked live-rec-lab restore prepare "$HOME/live-rec-lab-backup.sqlite3"
# prepare の出力と運用マニュアルに従って DB を手動交換する。
uv run --locked live-rec-lab restore finish
```

## 隔離環境の開発用コマンド

| コマンド | 用途 |
| --- | --- |
| `uv run --locked python -m live_rec_lab.devtools register <プラットフォーム> <アカウント>` | 停止中の隔離 DB に配信者を登録する。プラットフォームは `tiktok` または `twitcasting`。任意の `--name <表示名>` と、`--no-monitor`・`--no-record`・`--no-comments` を指定できる。省略時は監視・録画・コメントを有効にする。 |
| `uv run --locked --extra speech python -m live_rec_lab.devtools fetch-model <モデル>` | 利用者が承認した準備工程で、`large-v3` または `large-v3-turbo` を隔離のモデル領域へ取得する。任意の `DEV_HF_TOKEN` があれば認証し、未設定なら未認証で取得する。 |

通常環境のモデル取得方法と配置先は [音声字幕モデルの導入](README.md#音声字幕モデルの導入) を参照。
