# HTTP・CLI契約

ループバックの同一オリジンのみ。変更操作はCSRFとOrigin／Host検証を行い、GETは参照のみ。レスポンスはJSONまたは日本語HTML。認証情報・生ログ・動画やSRT本体は返さない。

| 操作 | 入力・主要出力 |
| --- | --- |
| GET /api/v1/health、/api/v1/system | 起動インスタンス、健康状態、監視枠、ツール、保存先と警告 |
| GET／POST /api/v1/streamers、PATCH／DELETE /api/v1/streamers/{id} | ID・サービス・表示名・監視／録画／コメント。POSTは3設定を明示。再有効化は同じIDと履歴を返す。DELETEは確認必須。 |
| GET /api/v1/sessions、/api/v1/sessions/{id} | サービス・配信者・日時・状態による検索、セッション単位の集計とセグメント・欠損 |
| POST /api/v1/sessions/{id}/stop、/resume | 手動指示の状態を返す。受付とジョブ確定は分ける。 |
| DELETE /api/v1/sessions/{id} | 明示確認と mode=files または mode=files_and_history。結果に操作ID・状態・理由を含める。復旧中・危険な操作は409。 |
| GET /api/v1/comments | 本文／投稿者／配信者／サービス／セッション／日付・ページング。期限削除状態を別表示。 |
| POST /api/v1/segments/{id}/srt | 対象・補正値。生成／暫定／対象なし／再同期待ち／失敗を返す。処理不可・期限削除・競合は対象理由を返す。 |
| GET／PUT /api/v1/settings | 公開可能な非秘密設定と保存版。値と版を検証して反映する。動作中のポート変更は次回起動から。 |
| GET /、/streamers、/recordings、/comments、/system、/logs、/settings | 指定順の日本語主要画面。配信セッション詳細でSRT等を扱う。 |
| GET /logs（クエリと同画面の詳細） | 時間・重要度・出力元・本文・実在相関IDの絞り込み。範囲・件数・順序・ページ・取得状態と秘匿済み詳細。ログの任意パス入力は受け付けない。 |

入力不正は400／422、オリジン等の拒否は403、対象不存在は404、現在の状態に対して操作不可は409、読み取りや保存失敗は説明と実際の状態を返す。UIでは安全に再試行できる範囲を示す。

CLIは `live-rec-lab start`、`status`、`url`、`stop`、`backup DEST`。起動時に割当済みソケットをUvicornへ渡し、URLの参照時に識別子と生存を検査する。バックアップの出力先とコメント残存を表示。実行中のDBそのものを利用者に取得させるWebルートは設けない。
