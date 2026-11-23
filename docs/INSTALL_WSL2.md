# Windows 11＋WSL2への導入

## 初回の起動

1. WindowsのPowerShellで `wsl -l -v` を実行し、UbuntuがWSLバージョン2で起動することを確認します。Ubuntu 24.04 LTSを基準とします。
2. リポジトリをWSLのホーム配下に置き、作業ディレクトリをリポジトリのルートへ移します。一般ユーザーでPython 3.12、uv、FFmpegを準備します。実際の実行環境で `ffmpeg -version` と `ffprobe -version` を確認します。
3. `uv sync --locked` を実行します。資格情報を使う場合は `.env.example` を `.env` にコピーし、権限を `chmod 600 .env` に設定してから値を記入します。
4. 録画する場合は専用のWindowsドライブを選びます。他のアプリや試作の**現在の実効録画ルート**をそれぞれの設定から確認し、`LIVE_REC_LAB_EXISTING_RECORDING_ROOTS` に設定します。DBやログのディレクトリも別々にします。変数や設定の詳細は [設定](CONFIGURATION.md) を参照してください。
5. WSL内で起動します。起動時にSQLiteのマイグレーションと未完了処理の照合を行います。

```bash
uv run --locked live-rec-lab start
```

6. コンソールに表示される `http://127.0.0.1:<port>` をWindowsのブラウザで開きます。同じ環境と作業ディレクトリで `uv run --locked live-rec-lab status`、`uv run --locked live-rec-lab url` を使えます。
7. 「設定」で実効保存先と状態を確認し、「配信者一覧」で利用条件と権限を確認した配信者を登録します。「ダッシュボード」で録画とコメント収集を個別に確認します。「録画履歴」のセッション詳細から保存・欠損・字幕・コメントへ移動できます。
8. 停止は `uv run --locked live-rec-lab stop` または起動シェルで Ctrl+C。停止処理が終わるまで待ちます。強制停止の次回起動では未確定ファイルと台帳を照合します。

Windowsから接続できない場合は [障害対応](TROUBLESHOOTING.md) を参照してください。画面ロックとディスプレイOFFは処理継続を想定し、スリープと休止は中断を伴います。実機での検証範囲は [制限](LIMITATIONS.md) に記載しています。

## バックアップと手動復元

```bash
uv run --locked live-rec-lab backup "$HOME/live-rec-lab-backup-$(date +%F).sqlite3"
```

CLIはSQLite Online Backupを使い、一貫したスナップショットを新規ファイルに保存します。既存の宛先を指定した場合は別名を選んでください。旧スナップショットには保存期限前のコメント本文や投稿者情報が含まれます。保管場所を記録し、不要になったバックアップを安全に破棄するか期限処理後の新しいバックアップに差し替えてください。

手動復元時はアプリを停止し、現在のDBを先にバックアップします。現在適用しているコメント保存期限と録画ルートも控えます。対象DBと対応ファイルを確認し、停止中にスナップショットをLinux側の `app.sqlite3` へ復元します。古いDBに属するWAL・SHMを混在させないでください。

復元DBにはバックアップ当時の設定が入っています。最初は監視を停止した状態で起動し、「設定」で現在のコメント保存期限と録画ルートを再設定します。

```bash
LIVE_REC_LAB_DISABLE_SCHEDULER=1 uv run --locked live-rec-lab start
```

設定を保存後、停止して同じ方法で再起動すると、監視を止めたまま起動時の期限照合が走ります。「システム状態」で削除・復元の照合状態を見て、対象セッションのコメント／管理SRTが保存期限どおりになったことを確認してください。照合が完了したら停止し、環境変数を付けずに通常起動します。

## Windowsサインイン後の起動

PowerShellでリポジトリの `scripts/start_windows.ps1` を起動し、最初にプレビューを確認します。WSLディストリビューション名、WSL内のリポジトリパス、uvへのパスは実環境に合わせます。

```powershell
.\scripts\start_windows.ps1 -Distro Ubuntu-24.04 -Repo '~/projects/live-rec-lab' -Uv '~/.local/bin/uv' -InstanceName personal
```

同じ引数に `-Register` を付けるとサインイン時の起動タスク `LiveRecorder-Lab-personal` を登録します。同名のタスクがある場合は停止します。WSLのユーザーsystemdで管理する場合は `scripts/live-rec-lab.service` が設定例です。Windowsサインイン時にWSLを起動する構成と合わせて使用してください。

更新時は手動バックアップと安全停止の後にソースを更新し、リポジトリで `uv sync --locked` を実行します。その後に `start` を実行し、移行結果と使用中のURLを確認してください。
