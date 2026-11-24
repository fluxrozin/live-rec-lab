# LiveRecorder

LiveRecorder は、Windows 11 上の WSL2 で動かす個人用のライブ配信記録アプリです。TikTok LIVE とツイキャスを監視し、配信が始まったら動画を自動で録画し、視聴者コメントを時刻と投稿者情報つきで残します。操作は Windows の PC ブラウザから行います。

通常運用の録画ファイルと全対応字幕は Windows 側の指定ドライブに保存します（隔離開発者モードは専用Linux領域）。エクスプローラーから保存先を開き、外部の動画プレイヤーで利用します。**Web管理画面・APIから動画や字幕のダウンロードは提供しません。** コメントは画面で検索でき、確定した録画セグメントごとにコメント収集の確定状態を踏まえて SRT を生成・再同期します。

## 現在の開発状態

両サービスの模擬配信をローカルHLSで録画し、SQLiteへのコメント保存と3種の字幕生成を確認しました。採用Aの主要8画面から字幕の予約、ストレージ照合、隔離媒体の削除を操作できます。通常・開発者モードの起動と停止は一時データで検証済みです。`large-v3` のCPUとCUDA推論も合成音声で確認しました。実配信の録画・署名通信と通常Windows媒体の削除は利用保留です。

WSL2のリポジトリルートで、Python 3.12、uv、システムのFFmpeg／ffprobeを用意します。次のコマンドは専用の一時領域に架空データを保存します。

```bash
uv sync --locked
fixture_root="$(mktemp -d)/live-rec-lab-fixture"
uv run --locked live-rec-lab fixture init "$fixture_root"
uv run --locked live-rec-lab fixture run "$fixture_root"
uv run --locked live-rec-lab fixture run "$fixture_root" --service twitcasting
uv run --locked live-rec-lab fixture postprocess "$fixture_root"
uv run --locked live-rec-lab fixture serve "$fixture_root"
```

起動後にコンソールに表示されたURL（既定は [127.0.0.1:8083](http://127.0.0.1:8083/)）を開きます。隔離インスタンスは別シェルから `fixture status`、`fixture url`、`fixture stop` で確認・停止できます。ポートの変更、診断、バックアップ、隔離DBの手動復元手順は [Quickstart](specs/001-initial-live-recorder/quickstart.md) を参照してください。画面構成は [採用Aの設計記録](specs/001-initial-live-recorder/ui/design.md) にあります。

## 開発の再開

[初期Featureのタスクと再開点](specs/001-initial-live-recorder/tasks.md) と [開始プロンプト](prompts/start.md) を参照してください。Spec Kit 1.4.4 のCodex統合を導入済みです。開発原則の正本は [AGENTS.md](AGENTS.md)、製品・UI・技術の正本は [要求](docs/requirements.md)、[UI仕様](docs/ui-spec.md)、[技術設計](docs/system-design.md) です。

## 起動して画面を開く

Windows 11＋WSL2 Ubuntu 24.04で、Python 3.12、uv、FFmpegを用意し、リポジトリのルートで起動します。

```bash
uv sync --locked --extra speech
uv run --locked --extra speech live-rec-lab start
```

CUDAを使う場合は `speech` の代わりに `speech-cuda` を指定します。外部監視や新規ジョブを停止して起動を確認する場合は `LIVE_REC_LAB_DISABLE_SCHEDULER=1` を付けます。

コンソールに出る `http://127.0.0.1:<port>` をブラウザで開きます。同じ環境で次を使います。

- `uv run --locked --extra speech live-rec-lab url` は、生存を確認できたURLを表示します。
- `uv run --locked --extra speech live-rec-lab status` は、プロセスの状態を表示します。
- `uv run --locked --extra speech live-rec-lab stop` は、安全停止を要求します。
- `uv run --locked --extra speech live-rec-lab backup DEST` は、SQLiteのオンラインバックアップを作ります。

URLは `~/.local/state/live-rec-lab/runtime/endpoint.json` にも残ります。終了したプロセスのURLは案内しません。ログインはなく、待ち受けは `127.0.0.1` だけです。既定URLは `http://127.0.0.1:8082` です。固定ポートは設定画面から変更でき、再起動後に反映します。競合時は起動エラーとなり、自動で別ポートに切り替えません。

録画を有効にする前に、DBへ保存済みの録画先と、他の録画アプリケーションや別インスタンスの実効保存先を照合してください。[初回導入](manual/README.md#install-wsl2)・[設定](manual/README.md#configuration)

画面ロック中とディスプレイOFF中の継続、スリープと休止、Windowsへサインインしたあとの自動起動は想定動作です。実機で確認できた範囲は [制限と検証範囲](manual/README.md#limitations) を参照してください。

## 開発者モード

通常と別のDB・状態ディレクトリ・固定ポートで開発者モードを起動します。

```bash
uv run --locked --extra speech live-rec-lab start --dev
# 別シェルで隔離インスタンスの診断・停止
uv run --locked --extra speech live-rec-lab diagnose --dev --json
uv run --locked --extra speech live-rec-lab stop --dev
# 通常運用インスタンスの読み取り専用診断
uv run --locked --extra speech live-rec-lab diagnose --json
```

開発者モードは隔離データ・専用設定・固定ポートを使用し、外部通信は既定停止です。通常起動には開発用ルートがありません。実運用の調査は読み取り専用診断とSQLiteバックアップの隔離コピーを使用し、実運用DBを直接開発起動しません。保護パス・排他・worktree分離・手動復元は [統合マニュアル](manual/README.md#developer-mode) を参照してください。

## 開発時の自動テスト

```bash
uv run --locked --extra speech pytest -n auto -m "not serial and not live and not hardware"
uv run --locked --extra speech pytest -n 0 -m "serial and not live and not hardware"
```

独立テストは並列化し、分離不能な資源を占有するテストのみ `serial` として別に実行します。負荷に応じて `-n N` で調整でき、`pytest` 単独はローカル試験の直列フォールバックです。`live`／`hardware` は明示許可と実行フラグが必要で、標準実行から除外します。最小の隔離を先に確保し、テスト基盤は機能に合わせて拡張します。詳細は要求§17と統合マニュアルを参照してください。

## 配信者を登録する

サイドバーの **配信者一覧**（`/streamers`）で配信者を追加する。

- TikTok はユーザー ID、`@username`、プロフィール URL（`/live` 形式を含む）を登録対象とする。実際に受理する形式は要求§5.1.1の実証済みプロファイルに従う。
- ツイキャスはユーザー ID またはプロフィール URL で登録する。API は Client ID と Client Secret による HTTP Basic 認証を使い、利用者ごとのログインは不要。
- 同じアカウントは重複して登録できない。登録後のサービス・正規IDは不変で、別アカウントは新規登録する。
- 配信者ごとに、監視、録画、コメント収集を別々に有効にできる。

対象は、公開配信か、録画してよい権限がある配信に限る。

## 配信が始まったあと

監視はサービスごとの回数で行う。初期値はどちらも 10 回／分。上限は TikTok が 10 回／分、ツイキャスが 60 回／分。回数は配信者ごとではなく、そのサービス全体で共有する。設定画面で変更でき、次回の巡回から反映される。ツイキャスでは、公式 API の枠をコメント収集に優先して使う。

配信開始を検知すると、録画とコメント収集をそれぞれ起動する。一方が失敗しても、もう一方は止めない。

- 配信中、録画中、コメント収集中は別の状態として表示する。取得できない状態を「オフライン」や「0 件」にはしない。
- 録画は Windows 側の保存先へ直接書く。既定は `D:\LiveRecorder`（WSL2 からは `/mnt/d/LiveRecorder`）。保存先は設定画面で変更できる。
- ツイキャスでプリロールと確認できた区間は、録画ファイルと履歴のセグメントから除外する。セグメント番号は保存対象の録画に付ける。[制限と検証範囲](manual/README.md#limitations)
- フォルダは配信者 ID、その下は日付。録画ファイル名は `tiktok_streamer01_20261123_210000_夜の雑談_s001.mp4` のように、プラットフォーム・配信者ID・配信開始日時（設定IANAタイムゾーン）・配信タイトル・セグメント番号の順とする。[命名規則](docs/requirements.md#103-ファイル名)
- コメントの正本は SQLite に残り、画面で検索できます。録画セグメントが確定すると、同じフォルダに動画stemに `_comment.srt` を付けたコメントSRTを、対象コメントがある場合に1つ作ります。コメントSRTに加え、音声SRTと統合ASSを生成します。

## 記録を探す

サイドバーは**「運用・データ」5画面＋「管理」3画面、計8画面**です。詳細・編集は各画面の下位ビューで行います。

| グループ     | 表示名         | URL           | 主な操作                                                           |
| ------------ | -------------- | ------------- | ------------------------------------------------------------------ |
| 運用・データ | ダッシュボード | `/`           | 配信・録画・コメント収集の状況、直近の記録と障害                   |
| 運用・データ | 配信者一覧     | `/streamers`  | 配信者の追加・編集・削除、名前／ID検索、並べ替え、独立した3設定    |
| 運用・データ | 録画履歴       | `/recordings` | 配信セッションの検索、動画・各字幕・欠損状態の確認、必要な削除操作 |
| 運用・データ | コメント       | `/comments`   | 配信別閲覧・横断検索・時刻昇降順                                   |
| 運用・データ | 音声字幕       | `/speech`     | 文字起こし状態・発話本文・再生成・ASS状態                          |
| 管理         | システム状態   | `/system`     | プロセス、保存先、依存ツール、警告・障害の要約                     |
| 管理         | ログ           | `/logs`       | 構造化ログ・イベントの検索、絞り込み、詳細閲覧（読取専用）         |
| 管理         | 設定           | `/settings`   | 監視頻度、保存先、同時録画数、タイムゾーン、フォントなど           |

**録画履歴は配信セッションを単位**とします。動画がなくてもコメントが残っていれば履歴に表示し、コメントへ移動できます。セグメント詳細からSRTの状態確認・再生成を扱います。一覧へ戻るときは検索条件を維持します。

動画・SRTはWindows側の保存先から利用します。Web管理画面には動画のダウンロード、ブラウザ再生、ライブプレビュー、再生同期コメント表示は設けません。

## 設定で変えること

**設定**（`/settings`）で、次を変更する。

- サービスごとの監視頻度
- 同時に録画する上限、録画品質、保存先、空き容量の下限、保存期限
- タイムゾーン（初期値は `Asia/Tokyo`）
- 画面のフォント（Windows 標準、または Noto Sans JP）
- 固定Webポート（通常既定8082、隔離既定8083、再起動後に反映）
- Web画面の自動部分更新間隔（既定5秒、再起動不要）

データベース、ログ、起動 URL は WSL2 の Linux 側に置く。通常運用の録画動画と対応字幕だけを Windows のドライブに置く。隔離時は専用Linux領域とする。リポジトリ名は `live-rec-lab`、アプリケーション名は LiveRecorder である。実行時ディレクトリはリポジトリ名に合わせる。Windows の既定録画フォルダは、このアプリケーションの動画と SRT の保存先である。

| 置き場所                                       | 内容                                              |
| ---------------------------------------------- | ------------------------------------------------- |
| `~/.local/share/live-rec-lab/data/app.sqlite3` | SQLite                                            |
| `~/.local/state/live-rec-lab/runtime`          | 起動URLとプロセスの状態                           |
| `~/.local/state/live-rec-lab/logs`             | ログ                                              |
| `/mnt/d/LiveRecorder`（既定・変更可）          | 動画セグメントと対応するコメントSRT・音声SRT・ASS |

SQLiteの手動バックアップは `uv run --locked --extra speech live-rec-lab backup DEST` を使います。動いているデータベースファイルを、そのままコピーしません。旧バックアップには期限削除前のコメントが残る場合があります。

## 関連文書・開発入口

- [製品要求仕様](docs/requirements.md)・[統合システム設計](docs/system-design.md)・[UI/UX仕様](docs/ui-spec.md)
- [開発プロンプト利用ガイド](prompts/README.md)：Spec Kitの導入・利用・再開・バグ修正・機能追加。
- [一気通貫開発開始プロンプト](prompts/start.md)：チャットへ渡す短い開始・再開指示。
- [全体実装プロンプト](prompts/implement.md)：初期実装・再開・収束の詳細指示。
- [UI設計・リデザインプロンプト](prompts/redesign-ui.md)：初期UI・単独リデザインとも3案の比較提示と利用者の明示選択が必須。選択待ちは採用案依存の製品UI実装に限定し、独立作業は継続する。
- [統合マニュアル](manual/README.md)：導入・設定・構成・障害対応・制限。現行コードと照合して更新する。
- 要件追跡表はSDD開始後に対象Featureの `traceability.md` として生成する。正本の対応基準は [統合設計 §21](docs/system-design.md)。
- 実装時に生成する `.env.example` は認証情報の**名前だけ**を示すテンプレートです。値を記入する `.env` はGit管理外です。

## 音声字幕と統合ASS

確定動画の音声認識は初期ON、モデルlarge-v3、言語ja、CUDAを既定とします。GPUが利用不能ならシステム状態と音声字幕で保留理由を確認し、必要なら設定からCPUを明示選択します。初回モデル取得には許可済み配布元への通信とLinux側の空き容量が必要です。隔離では明示承認した準備工程で専用キャッシュへ取得し、ローカル自動試験では自動取得しません。通常キャッシュを流用せず、未取得は保留／未検証とします（要求§7.9・§17）。CUDA・モデル・依存の互換性は導入環境で確認し、推論処理の負荷が大きければ自動生成をOFFにできます。録画・コメントは継続します。

「音声字幕」でキュー、失敗、旧正常版、生成時間、発話本文、個別／失敗一括再試行を確認します。ASSはコメント・音声更新時に再同期し、個別再生成できます。動画と同じフォルダの `<動画stem>_comment.srt`（コメント）、`<動画stem>_speech.srt`（音声）、`<動画stem>.ass`（統合）を外部プレイヤーで使います。ASSは単一トラックです。自動認識は誤認識を含み得ます。コメント期限削除ではコメントSRTとASSを削除し、音声SRTは保持します。全ファイル削除は動画と全対応字幕を対象とします。Webから動画・字幕ファイル本体を取得する機能はありません。

設定の正本は [要求仕様 §11.8.1〜3](docs/requirements.md)。配信者一覧の検索・カスタム並べ替え、コメントの配信別閲覧／横断検索、機能別健康状態、配信者別容量と障害期間集計を初期実装に含む。

システム状態では非同期の手動ストレージ照合を実行・取消できる。読み取り専用で、Webの定期更新は保存された診断結果の読取のみ。録画・コメントの再試行予算は再起動後も維持する。通常起動ではSwagger・ReDoc・OpenAPIを公開しない。
