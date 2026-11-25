# 隔離環境の起動・確認・停止

隔離環境は、通常運用と分けた設定・DB・録画先で LiveRecorder を試すための開発者モード。以下のコマンドは WSL のリポジトリ直下で実行する。起動後に別のシェルから操作する場合も、同じ WSL ユーザー・リポジトリ・`DEV_` 設定を使う。

## 1. 準備

```bash
uv sync --locked
if [ ! -e .env.dev ]; then
  cp -n .env.dev.example .env.dev
fi
chmod 600 .env.dev
```

既定の保存先を使う場合は、`.env.dev` のパス設定はコメントのままでよい。

| 用途 | WSL 側の既定値 |
| --- | --- |
| DB・モデル | `~/.local/share/live-rec-lab-dev/data/`（モデルはその中の `models/`） |
| 状態・ログ | `~/.local/state/live-rec-lab-dev/` |
| 録画・字幕 | `~/.local/share/live-rec-lab-dev/recordings/` |
| Web | `http://127.0.0.1:8083` |

保存先や資格情報の設定は、プロセス環境の `DEV_` 付き変数、`.env.dev`、隔離の既定値の順で解決する。通常運用の `.env` や資格情報は隔離の起動に引き継がれない。

## 2. 起動

```bash
uv run --locked live-rec-lab start --dev
```

隔離起動では、配信・署名への外部通信は既定で無効。起動時に表示された URL をブラウザで開く。

音声字幕も試す場合は、[音声字幕モデルの導入](README.md#音声字幕モデルの導入) に従って隔離用モデルを用意し、次のコマンドで起動する。

```bash
uv sync --locked --extra speech
uv run --locked --extra speech live-rec-lab start --dev
```

## 3. 状態・URL・診断を確認

起動コマンドはサーバーを前面で実行する。起動後は別の WSL シェルで次を実行する。

```bash
uv run --locked live-rec-lab status --dev
uv run --locked live-rec-lab url --dev
uv run --locked live-rec-lab diagnose --dev --json
```

- `status` は稼働状態・PID・インスタンス ID を表示する。
- `url` は実際に待ち受けている URL を表示する。
- `diagnose` は動作中・停止中ともに使える読取専用診断。JSON の `mode` が `developer`、`data_environment` が `isolated` であることと、`components.storage.root` が目的の録画先であることを確認する。

## 4. 停止

```bash
uv run --locked live-rec-lab stop --dev
uv run --locked live-rec-lab status --dev
```

`status` の `停止中 (mode=developer)` で停止を確認できる。停止中の `status` の終了コードは `1`。以降の設定・診断・バックアップにも `--dev` を付けて隔離側を指定する。

## ポートを変更する

いまの値を `config get` で確認する。ポートが競合する場合は、隔離環境を停止してから未使用のポートを設定する。例では 8084 を使う。

```bash
uv run --locked live-rec-lab config get web_port --dev
uv run --locked live-rec-lab config set web_port 8084 --dev
```

変更は次回の隔離起動から反映される。`url --dev` で待ち受け先を確認できる。

## 許可済みの実配信に接続する

実接続の許可範囲と隔離専用の資格情報を確認し、隔離インスタンスを停止してから次で起動する。

```bash
uv run --locked live-rec-lab start --dev --allow-live
```

`--allow-live` はその起動にだけ有効。音声字幕も使う場合は `uv run` 側に `--extra speech` を付ける。

バックアップ・復元や隔離用の配信者登録は [コマンドリファレンス](commands.md) を参照。
