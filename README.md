# LiveRecorder

LiveRecorder は、Windows 11 上の WSL2 で動かす個人用のライブ配信記録アプリです。TikTok LIVE とツイキャスを監視し、配信が始まったら動画を自動で録画し、視聴者コメントを時刻と投稿者情報つきで残します。操作は Windows の PC ブラウザから行います。

録画ファイルとSRTは Windows 側の指定ドライブに保存します。エクスプローラーから保存先を開き、外部の動画プレイヤーで利用します。**Web管理画面・APIから動画やSRTのダウンロードは提供しません。** コメントは画面で検索でき、確定した録画セグメントごとにコメント収集の確定状態を踏まえて SRT を生成・再同期します。

## 起動して画面を開く

Windows 11＋WSL2 Ubuntu 24.04で、Python 3.12、uv、FFmpegを用意し、リポジトリのルートで起動します。

```bash
uv sync --locked
uv run --locked live-rec-lab start
```

出力されたループバックURLをブラウザで開きます。URLは `uv run --locked live-rec-lab url`、プロセスの状態は `status` でも確認できます。同じ環境で `uv run --locked live-rec-lab stop` を実行すると安全停止します。

録画を有効にする前に、DBに保存済みの録画先と既存アプリ／試作の実効保存先を照合してください。[初回導入とサインイン時の起動](docs/INSTALL_WSL2.md)・[設定](docs/CONFIGURATION.md)

実配信とWindowsホスト固有の項目は [検証範囲](docs/LIMITATIONS.md) を参照してください。

## 配信者を登録する

サイドバーの **配信者一覧**（`/streamers`）で配信者を追加する。

- TikTok はユーザー ID、`@username`、プロフィール URL のいずれでも登録できる。
- ツイキャスはユーザー ID で登録する。API は Client ID と Client Secret による HTTP Basic 認証を使い、利用者ごとのログインは不要。
- 同じアカウントは重複して登録できない。
- 配信者ごとに、監視、録画、コメント収集を別々に有効にできる。

対象は、公開配信か、録画してよい権限がある配信に限る。

## 配信が始まったあと

監視はサービスごとの回数で行う。初期値はどちらも 10 回／分。上限は TikTok が 10 回／分、ツイキャスが 60 回／分。回数は配信者ごとではなく、そのサービス全体で共有する。設定画面で変更でき、次回の巡回から反映される。ツイキャスでは、公式 API の枠をコメント収集に優先して使う。

配信開始を検知すると、録画とコメント収集をそれぞれ起動する。一方が失敗しても、もう一方は止めない。

- 配信中、録画中、コメント収集中は別の状態として表示する。取得できない状態を「オフライン」や「0 件」にはしない。
- 録画は Windows 側の保存先へ直接書く。試作専用の例は `D:\live-rec-lab\recordings`（WSL2 からは `/mnt/d/live-rec-lab/recordings`）。ドライブは設定で変えられる。
- フォルダは配信者 ID、その下は日付。ファイル名で TikTok とツイキャスを区別する。
- コメントの正本は SQLite に残る。録画セグメントが確定すると、同じフォルダに同名の SRT を 1 つ作る。元のコメントはSQLiteで検索できます。

## 記録を探す

サイドバーは**「運用・データ」4画面＋「管理」3画面、計7画面**です。詳細・編集は各画面の下位ビューで行います。

| グループ | 表示名 | URL | 主な操作 |
| --- | --- | --- | --- |
| 運用・データ | ダッシュボード | `/` | 配信・録画・コメント収集の状況、直近の記録と障害 |
| 運用・データ | 配信者一覧 | `/streamers` | 配信者の追加・編集・削除、監視・録画・コメント収集のON/OFF |
| 運用・データ | 録画履歴 | `/recordings` | 配信セッションの検索、動画・SRT・欠損状態の確認、必要な削除操作 |
| 運用・データ | コメント | `/comments` | 本文・投稿者・配信者・日付による検索・閲覧 |
| 管理 | システム状態 | `/system` | プロセス、保存先、依存ツール、警告・障害の要約 |
| 管理 | ログ | `/logs` | 構造化ログ・イベントの検索、絞り込み、詳細閲覧（読取専用） |
| 管理 | 設定 | `/settings` | 監視頻度、保存先、同時録画数、タイムゾーン、フォントなど |

**録画履歴は配信セッションを単位**とします。動画がなくてもコメントが残っていれば履歴に表示し、コメントへ移動できます。セグメント詳細からSRTの状態確認・再生成を扱います。一覧へ戻るときは検索条件を維持します。

動画・SRTはWindows側の保存先から利用します。Web管理画面には動画のダウンロード、ブラウザ再生、ライブプレビュー、再生同期コメント表示は設けません。

## 設定で変えること

**設定**（`/settings`）で、次を変更する。

- サービスごとの監視頻度
- 同時に録画する上限、録画品質、保存先、空き容量の下限、保存期限
- タイムゾーン（初期値は `Asia/Tokyo`）
- 画面のフォント（Windows 標準、または Noto Sans JP）
- Web のポート（自動割り当て、または固定）

データベース、ログ、起動 URL は WSL2 の Linux 側に置く。録画動画と SRT だけを Windows のドライブに置く。リポジトリ名は `live-rec-lab`、アプリケーション名は LiveRecorder である。実行時ディレクトリはリポジトリ名に合わせる。Windows の録画フォルダ例は、このアプリケーションの動画と SRT の保存先である。

| 置き場所                               | 内容                         |
| -------------------------------------- | ---------------------------- |
| `~/.local/share/live-rec-lab/data`     | SQLite                       |
| `~/.local/state/live-rec-lab/runtime`  | 起動 URL とプロセスの状態    |
| `~/.local/state/live-rec-lab/logs`     | ログ                         |
| `/mnt/d/live-rec-lab/recordings`（試作専用例） | 動画セグメントと対応する SRT |

SQLiteの手動バックアップは `uv run --locked live-rec-lab backup DEST` を使います。旧バックアップには期限削除前のコメントが残る場合があります。

## 関連文書

- 機能と制約の正本は [requirements.md](requirements.md)
- 画面の構成と文言の正本は [docs/UI_UX.md](docs/UI_UX.md)
- 実装を進めるための指示は [docs/prompts/master.md](docs/prompts/master.md)
- 設計の横断整理は [docs/design/README.md](docs/design/README.md)
- 実装・運用の補助資料は [構成](docs/ARCHITECTURE.md)・[設定](docs/CONFIGURATION.md)・[WSL2導入](docs/INSTALL_WSL2.md)・[障害対応](docs/TROUBLESHOOTING.md)・[制限と未検証](docs/LIMITATIONS.md)。
- 受け入れ条件の状況は [要件追跡表](specs/001-initial-live-recorder/traceability.md)
- `.env.example` は認証情報の**名前だけ**を示すテンプレートです。値を記入する `.env` はGit管理外です。
