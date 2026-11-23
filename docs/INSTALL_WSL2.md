# 導入（Windows 11 + WSL2）

この手順は、リポジトリを WSL2 の Linux 側に置いて使う場合のものです。実配信への接続成否は環境依存であり、ここでは起動までを説明します。

1. Windows 11 で WSL2 の Ubuntu 24.04 を使える状態にします。
2. リポジトリを Linux のホーム配下に置きます。例: `~/projects/live-rec-lab`。
3. Python 3.12 と [uv](https://docs.astral.sh/uv/) を入れます。要件はPython 3.12です。実行前に現行リポジトリの `.python-version`、`pyproject.toml`、`uv.lock` が整合することを確認してください。
4. FFmpeg（`ffmpeg` と `ffprobe`）を Ubuntu に入れます。
5. リポジトリで `uv sync --locked` を実行します。
6. `.env.example` を `.env` にコピーし、使うときだけツイキャスの Client ID / Secret と Euler Stream のキーを入れます。値はダミーのままでは外部サービスに接続できません。`.env` は Git に入れません。**uvはこのファイルを暗黙に読み込まないため、以降のCLIでは`--env-file .env`を明示**します。
7. 録画先の Windows ドライブを決めます。**試作専用の例**: `/mnt/d/live-rec-lab/recordings`。既存の別アプリケーションLiveRecorderや別worktree／試作インスタンスのDB・状態／ログ・動画／SRT保存先とは分離してください。**既存アプリの実効録画ルートをそのアプリの設定で確認**し、本試作のDBに保存済みの録画先がある場合は環境変数よりも**実際に適用される設定**を調べ、Windows上で同一・親子関係がないことを起動前に確認します。自動確認できない場合は手動で確かめ、未確認のまま安全な分離を前提に運用しないでください。ドライブが未マウントのときはLinux側へ自動で切り替えません。
8. 初回起動:

```bash
export LIVE_REC_LAB_RECORDING_ROOT=/mnt/d/live-rec-lab/recordings
uv run --locked --env-file .env live-rec-lab start
```

9. 表示された `http://127.0.0.1:<port>` を Windows のブラウザで開きます。ログインはありません。
10. 同じ URL は `uv run --locked --env-file .env live-rec-lab url` でも確認できます。プロセスがいないときは古い URL を出しません。
11. 配信者を登録します。確定UI仕様の画面名は「配信者一覧」です（2026-11-23の試行記録では「監視対象」と表示）。保存先は設定画面でも変更できます。
12. 停止は `uv run --locked --env-file .env live-rec-lab stop` です。
13. データベースの複製は `uv run --locked --env-file .env live-rec-lab backup <出力ファイル>` です。稼働中のDBファイルをそのままコピーしません。**バックアップには期限削除前のコメントが残る可能性**があります。バックアップはアプリの自動削除対象外なので、保存先を記録し、期限切れコメントを含む古いバックアップは利用者が安全に破棄または新しいものへ差し替えます。初期版の自動復元CLIはなく、手動復元時はコメント期限と派生SRTの整合を再確認するまで運用を再開しません。
14. アップデートは `git pull` のあと `uv sync --locked` です。スキーマは起動時にマイグレーションされます。
15. 画面ロック中とディスプレイ OFF 中は処理を続ける前提です。スリープと休止では止まります。この差の実測は未検証です。
16. Windows サインイン後の起動は `scripts/start_windows.ps1` を見ます。`-Register` を付けるとタスク スケジューラを変更します。既定では登録しません。WSL 内の再起動例は `scripts/live-rec-lab.service` です。systemd だけでは WSL 自体は常時起動しません。
17. 障害の要約は「システム状態」で確認します。確定UI仕様の「ログ」画面は時刻・重要度・出力元で絞り込む設計ですが、2026-11-23の試行記録では未実装です。ファイル確認手順は [切り分け](TROUBLESHOOTING.md) を参照してください。
18. 初期実装の受け入れではコメント保存期限によるSQLiteコメントとSRTの削除整合、論理削除済み配信者の再登録、ファイル名衝突時の非上書き、SRT版数競合の復旧も確認します。主要7画面・必要な下位ビューを実ブラウザで表示・操作し、1024／1280／1440pxを確認します。実施できなければ「未検証」と記録し、完成済みとは扱いません。
