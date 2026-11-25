# LiveRecorder 運用マニュアル

コマンドの構文・オプション・使用例は [コマンドリファレンス](commands.md) を参照する。
開発者モードで試す場合は [隔離環境の起動・確認・停止](isolated.md) を参照する。

この手順は実装・実測に基づく。確認済みと未確認の範囲は末尾「検証範囲」と [tasks.md](../../tasks.md) を参照する。

## 1. 導入

前提: Windows 11 + WSL2（Ubuntu 24.04）、Windows側の保存ドライブ（既定 `D:\LiveRecorder` = WSL上 `/mnt/d/LiveRecorder`）。

1. uv を導入（Python 3.12 は `.python-version` により uv が用意する。システムの `python3` が3.14でも影響しない）。
2. `uv sync --locked`。音声字幕も使う場合は下記の手順で `speech` extra を導入する。
3. ffmpeg / ffprobe を用意し、**同じディレクトリの組**を `LIVE_REC_LAB_FFMPEG_BIN` で固定する。WSLの対話シェル（`~/.bashrc`）と非対話シェルでPATHが異なり、非対話では古い `/usr/bin/ffmpeg`（6.1.1）が選ばれる例を実測した。システム状態画面・`diagnose --json` で使用中の版を確認できる。
4. `.env.example` を `.env`（通常）、`.env.dev.example` を `.env.dev`（隔離）にコピーし `chmod 600`。秘密値（`TWITCASTING_CLIENT_ID/SECRET`、`SIGN_API_KEY`、任意の `HF_TOKEN`）はこれらに置く。Git・DB・ログ・Web応答には出ない。
### 音声字幕モデルの導入

音声字幕は通常運用でも隔離環境でも利用できる。依存は `uv sync --locked --extra speech` で導入する。`speech` extra には faster-whisper と CUDA ランタイム（`nvidia-cublas-cu12` / `nvidia-cudnn-cu12`）が含まれる。モデルは別途用意し、アプリは起動・診断・字幕生成時に自動取得しない。

| モード | WSL 側の既定のモデル配置先 | データディレクトリの変更 |
| --- | --- | --- |
| 通常 | `~/.local/share/live-rec-lab/data/models/` | `LIVE_REC_LAB_DATA_DIR`（起動環境または `.env`）配下の `models/` |
| 隔離（`--dev`） | `~/.local/share/live-rec-lab-dev/data/models/` | `DEV_LIVE_REC_LAB_DATA_DIR`（起動環境または `.env.dev`）配下の `models/` |

**通常運用**では、アプリを起動する WSL ユーザーと同じ設定で、リポジトリ直下から次を実行する。例は既定の `large-v3`（Hugging Face の `Systran/faster-whisper-large-v3`、約 2.9 GB）。配布元の利用が承認済みで、WSL 側に空き容量があることを確認してから取得する。任意の `HF_TOKEN` を `.env` またはプロセス環境に置くと取得要求が認証され、未認証の警告は出ない。未設定でも公開モデルは取得できる。隔離の取得は `DEV_HF_TOKEN` だけを使い、通常の `HF_TOKEN` は読まない。字幕生成はオフラインで、トークンを送らない。

```bash
uv sync --locked --extra speech
uv run --locked --extra speech python - <<'PY'
import os

from live_rec_lab.config import apply_hf_token, resolve_environment

env = resolve_environment(dev=False)
models = env.model_dir
os.environ["HF_HOME"] = str(models / ".hf-home")
os.environ["HF_ENDPOINT"] = "https://huggingface.co"
apply_hf_token(env.secrets.get("HF_TOKEN"))

from faster_whisper.utils import download_model

print(download_model("large-v3", cache_dir=str(models)))
PY
```

承認済みの取得済みモデルを配置する場合は、`models--Systran--faster-whisper-large-v3/` ディレクトリを、`refs/`・`snapshots/`・`blobs/` と内部リンクを保ったまま通常の `models/` 直下にコピーする。推論はこの Hugging Face キャッシュ構造からローカルで読み込む。

通常起動は `uv run --locked --extra speech live-rec-lab start`。Windows 用の自動起動スクリプトも同じコマンドを使う。設定画面の「音声字幕」でモデル・言語・デバイスを選べる。既定は `large-v3`・日本語（`ja`）・CUDA。`large-v3-turbo` を選ぶ場合は、そのモデルを用意する。

**隔離環境**では、利用者が承認した準備工程で `uv run --locked --extra speech python -m live_rec_lab.devtools fetch-model large-v3` を実行する。このコマンドの取得先は隔離の `models/`。`.env.dev` またはプロセス環境の `DEV_HF_TOKEN` があれば認証して取得し、なければ未認証で取得する。起動は `uv run --locked --extra speech live-rec-lab start --dev`。

## 2. 起動と停止

| 操作       | 通常運用                                            | 隔離（開発者）                                               |
| ---------- | --------------------------------------------------- | ------------------------------------------------------------ |
| 起動       | `live-rec-lab start`                                | `live-rec-lab start --dev [--allow-live]`                    |
| ポート     | 8082（変更可、次回起動から）                        | 8083                                                         |
| データ     | `~/.local/share/live-rec-lab/data`                  | `~/.local/share/live-rec-lab-dev/data`                       |
| 状態・ログ | `~/.local/state/live-rec-lab`                       | `~/.local/state/live-rec-lab-dev`                            |
| 録画先     | `/mnt/d/LiveRecorder`（DB保存値 > 環境変数 > 既定） | 専用Linux領域、または許可済みWindows専用試験フォルダ         |
| 外部通信   | 有効化した登録対象のみ                              | 既定で拒否。`--allow-live`（永続化しない）と共通ロックが必要 |

- 音声字幕を使う場合の起動は [音声字幕モデルの導入](#音声字幕モデルの導入) の `--extra speech` 付きコマンドを使う。
- 停止: `live-rec-lab stop [--dev]`（SIGTERM。録画の子プロセスを回収し、取得済みを確定し、コメントを排出する。確認済みの終了とは扱わず、セッションは「終了未確認」で収束する）。
- 再起動は `live-rec-lab restart [--dev]`。停止を確認した後、同じモードを前面で起動する。
- ポート: 設定ポートに直接バインドし、自動変更しない。WSL2の mirrored モードでは前回接続のTIME_WAITで再バインドが40秒以上遅れる実測があるため、別プロセスがlistenしていない場合に限り同じポートで最大150秒待機する。別プロセスが使用中なら即座に拒否する。
- 停止中のポート確認・変更: `live-rec-lab config get web_port` / `config set web_port 8090`。
- 二重起動は排他ロックで拒否される。`start` はポート確保・DB移行後に待受を始め、`runtime/endpoint.json` にURL・PID・起動時刻・インスタンスIDを書く。`status` / `url` は生存確認済みの情報だけを返す。

### Windows サインイン後の自動起動（利用者の手動登録）

`scripts/windows/start-live-rec-lab.ps1` が `wsl.exe` で `scripts/start-live-rec-lab.sh`（`uv run --locked --extra speech live-rec-lab start`）を非対話・非表示で起動する。タスクスケジューラへの登録は利用者が行う（本アプリは自動登録しない）。

```powershell
schtasks /Create /TN "LiveRecorder" /SC ONLOGON /RL LIMITED /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File <リポジトリのWindowsパス>\scripts\windows\start-live-rec-lab.ps1"
```

このスクリプトによる Windows→WSL の非対話起動と、Windows からの localhost 接続は実機で確認済み（登録したタスクの実ログオン動作は未確認）。スクリプトは UTF-8 BOM 付きで保存すること（Windows PowerShell 5.1 の文字化け回避）。非対話起動では `LIVE_REC_LAB_FFMPEG_BIN` 未設定だと古い ffmpeg が選ばれる。

Windows のスリープ・休止では録画が中断する。画面ロック・ディスプレイOFFでは継続する。

## 3. 画面の使い方

- ダッシュボード: 稼働中セッション、KPI、保存先容量、警告・エラー。左上のマークは待機中=青、録画中=赤（パルス）。自動更新は設定の間隔（既定5秒）。操作中は更新しない。稼働中のセッションでは「録画を停止」ができる。停止後も同じ配信が続いているときは「録画を再開」「コメント収集を再開」で、新しい試行予算を付けて明示再開する。
- 配信者: 追加（サービス・ID/プロフィールURL・表示名・監視/録画/コメント。既定は3つともON）、スイッチで即時反映、名前・IDとサービスでの絞り込み、表示名の編集、並べ替え（上下）、削除（確認つき、履歴は残る）。稼働中の配信者は削除できない。
- 録画履歴／詳細: セグメントごとの状態・長さ・容量、コメントSRTの再生成、欠損・ジョブ・イベント。終了未確認のアーカイブは「手動収束」を確定してから削除できる。削除はアーカイブ全体のみ（確認つき）。
- コメント: 横断検索／配信別、本文・投稿者・サービス・日付、昇降順、ページング。
- 音声字幕: 状態、個別再生成、失敗分の再試行、発話本文の閲覧。
- 設定: 6カテゴリ（監視、録画・保存、コメント・SRT、音声字幕、表示、詳細設定）。全設定を一括保存（版照合・全値検証・原子的）。未保存の変更は離脱時に確認する。フォントはWindows標準／Noto Sans JP。
- システム状態: 実行環境、ffmpeg、保存先、資格情報の有無、ストレージ照合（読取専用）。
- ログ: 構造化ログ（10 MiB × 5世代）を重要度・出力元・本文で絞り込む。

## 4. 録画・コメント・字幕

- 検知→セッション→録画ジョブとコメントジョブを独立に起動。どちらかが失敗してももう一方は継続する。
- 録画は同一ボリュームの一時ファイルへ書き、ffprobeで映像コーデック・長さを検証し、MP4へstream copy（不可なら元形式が再生可能な場合のみ代替）して、**非上書き**で正式名へ確定する。Windowsマウント（9p）では `renameat2(NOREPLACE)` が使えないため、`link` + `unlink` で確定する（大小文字違いの同名も拒否される、実測）。
- 保存先: `<録画ルート>/<配信者ID>/<YYYY-MM-DD>/<platform>_<ID>_<YYYYMMDD_HHMMSS>_<タイトル>_sNNN.mp4`、字幕は同stemの `_comment.srt` / `_speech.srt` / `.ass`。同名衝突時だけセッション由来の識別子を付ける。
- コメントSRT: 収束後、検証済みセグメントごとに動画0秒基準で自動生成。録画範囲外のコメントは押し込まない。0件なら「対象なし」（空ファイルは作らない）。同期は録画ファイルへの初回データ到着時刻（実測）を基準にし、近似であることをDBに記録する。匿名コメントの字幕表記は、番号を照合できたとき ` (匿名コメント#番号)`、番号がないとき ` (匿名コメント)`。コメント画面では「匿名」と表示する。
- 音声SRT・統合ASS: 収束後、同時1件の子プロセスで faster-whisper（`speech_device` = `cuda` / `cpu`。暗黙のCPU切替なし）。GPU・モデル・依存が不足なら「実行不可（blocked）」を理由つきで表示し、コメントのみの部分ASSを生成する。録画・コメントは止めない。
- ツイキャスの公式HLSでは、内容照合でプリロール（先頭の黒地約4秒）と判定できた部分だけを本編の前から外す。プリロールだけの取得は正式な録画にしない。判別できない場合は残す。
- 再接続: 追加試行は `reconnect_max_attempts` を予算としてDBに永続化し、再起動・再検知で復活しない。手動停止した配信は自動再開しない。同じ配信が続いているときの明示再開は、ダッシュボードの再開ボタンだけが新しい試行予算を作る。
- TikTok の既定の録画エンジンは Streamlink のみ。yt-dlp は現行の TikTok では取得できないため既定順に入っておらず、設定画面から切り替える項目はない。

## 5. 容量・削除・照合・バックアップ・復元

- 容量: 新規録画の前に最低空き容量（`min_free_gib`）と上限（`max_recording_gib`）を判定。判定不能は不可として扱い、録画を保留する（待機後に再判定）。
- 自動削除は既定OFF。ON時のみ、保存期限（`archive_retention_days`）または容量条件で、**終了確認済み**の最古のアーカイブから削除する（終了未確認・稼働中は自動削除しない）。
- 削除: 最小記録（`deleted-archives.jsonl`、復元対象外）→ 管理ファイルを記録した世代と照合して削除 → 全ファイル確認後にDB一括削除。世代不一致・未接続ドライブ・非通常ファイルは「保留」とし、DBを残す。削除済み配信は再検知しても再取得しない。
- 照合（システム状態）: 登録ファイルの実在・サイズ、未登録メディア、一時ファイルを読取専用で一覧する。修復・取込・削除はしない。
- バックアップ: `live-rec-lab backup DEST`（SQLite Online Backup、上書きしない）。**コメント本文など個人情報を含む**。保存場所と共有範囲に注意し、不要な古いコピーは削除する。
- 停止中の手動復元（自動復元はない）:
  1. `live-rec-lab stop`、`live-rec-lab restore prepare <候補.sqlite3>`（排他取得・現DBの退避・候補の整合性とスキーマ検査・マーカー作成）。
  2. DBファイルを手動で交換（`-wal` / `-shm` は削除）。
  3. `live-rec-lab restore finish`（稼働中の残骸を終了未確認で収束し、削除済み記録と照合して復活を防ぐ。照合できなければマーカーを維持）。やり直す場合は `restore abort`（退避DBへ差し戻す）。
  - マーカーがある間は通常起動を拒否する。外部予算と削除済み記録はDBの外にあり、復元では戻らない。

## 6. 診断と障害対応

- `live-rec-lab diagnose --json [--dev]`: 読取専用（DB作成・移行・外部通信なし）。終了コード 0=ok / 1=partial / 2=unavailable。
- 起動を拒否された場合: メッセージに理由（保護パスとの衝突、隔離境界、ポート使用中、排他、復元マーカー）が出る。自動で別ポート・別領域へ切り替えない。
- 録画が始まらない: ダッシュボード/配信者の状態（外部通信は無効 / 状態不明）、システム状態の保存先、`/logs` を確認。隔離環境で実配信に接続するには `--allow-live`。
- 「状態不明（http_429 等）」: 終了とは扱わない。待機後に再確認する。403・429・タイムアウトだけではアクセス制限と扱わない。
- 合言葉や、確実な根拠のあるアクセス制限が確認された配信は、録画とコメントの両方を止める。取得済みのファイルとコメントは残る。その配信は再開しない。
- 音声字幕の `model_missing` は、選択中のモデルが起動モードの `models/` にあるか確認する。`speech_extra_missing` は `--extra speech` 付きの導入・起動手順を確認する。準備後は「音声字幕」から再試行できる。
- コメント接続が見送られる: 署名キー未設定、または署名枠の保守的な計上が上限（30回/時間。無料キーの実測は分60・時500・日1500、接続1回で1消費）。
- ツイキャスの録画順は、ffmpeg が HTTPS（TLS）対応なら公式HLS → 公開 tc-hls → Streamlink。非対応（`ffmpeg -protocols` に https が無い）のときは公式HLSと公開 tc-hls を使わず、Streamlink だけを使う。

## 7. 検証範囲

実機確認済み: Windowsマウント（`D:\LiveRecorder`）での非上書き確定・外部置換の保護・縦疎通、隔離環境の RTX 5070 Ti（CUDA, float16, large-v3）での日本語音声の文字起こし→音声SRT/ASS、Edge での画面表示と設定操作、libass でのASS描画。通常領域でのモデル取得・推論は未確認。
ローカルのみ（疑似配信 + 実ffmpeg + 一時SQLite）: 検知→録画/コメント→確定→収束→コメントSRT、停止時の回収、再起動時回収、アーカイブ削除の安全性、手動復元。
実配信で確認済み（隔離・2026-11-25、対象 `yanaks64` / `u971368` / `mihiro519` / `maoki_it`）: TikTok のコメント（Euler 無料キー、`yanaks64` で受信・保存。room API の `streamId` はストリームIDで、接続先Room IDはSDKが解決する）、 TikTok の room API 監視・Room ID・Streamlink での実録画・TikTokLive 接続（Room ID 一致）・署名枠の実測、ツイキャスの current_live・comments（実コメント保存）・Streamlink での実録画。
ツイキャスのプリロール除外（公式HLS先頭の黒地4秒を内容照合で除外）、匿名コメント番号（視聴用WebSocket、コメントID照合。字幕は番号があるとき ` (匿名コメント#番号)`、ないとき ` (匿名コメント)`）、ASSの表示（MPC-HC）も確認済み。
ツイキャスの公開 tc-hls は、製品の録画順（公式HLSの次）で実配信を録画し確定まで確認済み。確認時は隔離専用の `DEV_LIVE_REC_LAB_TWITCASTING_FORCE_ENGINE=tc-hls` で経路を固定した。
**TikTok の yt-dlp 録画は現状利用不可（実測）**: 配信中の `u971368`・`mihiro519` で、yt-dlp 2026.11.23 は「not currently live」を返した（抽出器が署名なしの room/info を使うため）。既定の `tiktok_engine_order` は Streamlink のみ。設定画面にエンジン順の項目はない。
**未検証**: Windowsサインイン後の自動起動（タスク登録・実ログオン）、WSL再起動後の復帰。これらは実証までは利用不可として扱う。
