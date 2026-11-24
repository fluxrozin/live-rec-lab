# 設定

通常の設定はWebの「設定」にあります。認証情報やインスタンスのパスには環境変数を使います。`.env` はリポジトリのルートで起動する場合に読み込まれ、プロセス環境の値が優先されます。

| 環境変数 | 内容 |
| --- | --- |
| `TWITCASTING_CLIENT_ID`、`TWITCASTING_CLIENT_SECRET` | 公式APIのHTTP Basic認証 |
| `SIGN_API_KEY` | Euler Streamの署名API用。利用可能な場合に設定 |
| `LIVE_REC_LAB_DATA_DIR` | SQLiteのディレクトリ。既定 `~/.local/share/live-rec-lab/data` |
| `LIVE_REC_LAB_STATE_DIR` | ログ・実行状態。既定 `~/.local/state/live-rec-lab` |
| `LIVE_REC_LAB_RECORDING_ROOT` | DBに保存先が未登録の場合の録画ルート。専用パスの例は `/mnt/d/live-rec-lab/recordings` |
| `LIVE_REC_LAB_EXISTING_RECORDING_ROOTS` | 他のLiveRecorderや試作の実効ルート。WSLの絶対パスをセミコロンで区切る。利用中の既存ルートがないことを確認した場合のみ `none` |
| `LIVE_REC_LAB_FFMPEG_BIN` | 任意。実行可能な `ffmpeg` と `ffprobe` が揃う絶対ディレクトリ。未指定時は実行プロセスのPATHを使用 |
| `LIVE_REC_LAB_DISABLE_SCHEDULER` | `1` のとき監視とジョブ起動を止める。手動復元後の期限照合に使い、通常起動では未設定にする |

SQLiteの実ファイルはデータディレクトリの `app.sqlite3` です。SQLiteと実行状態はWSLのLinux側に置きます。別インスタンスではDB、状態／ログ用ディレクトリ、録画ルートを分離してください。DBに保存済みの録画ルートは環境値に優先します。比較対象が未確認、同一や親子のパス、ドライブ未接続、権限不足、容量不足の場合は、システム状態やログで録画開始が保留された理由を確認できます。

| 設定領域 | 主な動作 |
| --- | --- |
| 監視 | 初期値は各10回／分。TikTokは0より大きく10以下、ツイキャスは0より大きく60以下。小数可。サービス単位で共有し、次の巡回へ反映 |
| 録画・保存 | 品質、選ぶエンジン、同時録画数、容量、保存先。動画の自動削除は初期OFF。動画期限の0日は日数による期限なし |
| コメント・SRT | 全体のコメントON/OFFは配信者ごとの値と合わせて判定。コメント保存期限の初期値0は無期限。正整数は収束時刻からの日数 |
| 表示 | IANAタイムゾーンは初期 `Asia/Tokyo`。Windows標準／Noto Sans JP |
| 詳細設定 | ログレベルとWebポート。ポート変更は再起動後。0はOSの自動割り当て |

コメント期限に達した場合は、稼働DB内のコメントと管理SRTを削除し、動画と履歴を保持します。旧手動バックアップには本文と投稿者情報が残り得ます。バックアップの廃棄・差し替えと手動復元後の手順は [WSL2導入と運用](INSTALL_WSL2.md) を参照してください。

RESTで設定変更する場合は、`GET /api/v1/settings` の `revision` とレスポンスの `X-CSRF-Token` を取得してください。`PUT /api/v1/settings` のJSONに変更項目と `revision` を含め、同名のヘッダーにトークンを指定します。保存後の版が異なる場合は再取得が必要です。
