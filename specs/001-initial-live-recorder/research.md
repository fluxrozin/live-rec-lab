# 実現性調査

2026-11-24。資料で判明した契約、実行環境の観測、未検証を区別する。

| 対象 | 判断と理由 | 代替・証跡・未検証 |
| --- | --- | --- |
| TikTok監視 | 条件付きで実装可能。SDKの公開配信確認に加え、原本指定のroom応答からHTTPステータス先行で状態とIDを取得する。2＋IDはLIVE、4は終了候補、1／3／不正値はUNKNOWN。 | Streamlinkにもroom APIを読む実装がある。SDKの真偽値だけではroom状態とエラーの区別に不足する。現行環境のDNS制限により実配信未検証。[Streamlinkの実装](https://github.com/streamlink/streamlink/blob/master/src/streamlink/plugins/tiktok.py) |
| TikTokコメント | 条件付きで実装可能。キャッシュ済みTikTokLive 7.0.1を採用し、公開配信のCommentEventとSDKが返す受信タスクを扱う。Euler Streamの無料署名を使う。 | 署名サービスはHTTP 429・残枠を公開する。確認、初回署名／接続、再接続、長時間接続、同時接続それぞれの消費・外部送信・限度を実配信で要測定。固定の無料枠値に依存しない。[SDKドキュメント](https://ttl.docs.isaackogan.com/)、[Euler Streamのレート制限](https://www.eulerstream.com/docs/api/rate-limits) |
| TikTok動画 | Streamlink 8.6.3を初期優先、yt-dlp 2026.10.26を抽出障害時の候補とする。どちらも公開LIVEの実装を持つ。 | SDK連携の動画取得は正式API・実配信検証が揃う場合に評価。アクセス制限を理由とする切替を抑止。動画の通し再生と区間網羅は未検証。[Streamlinkの実装](https://github.com/streamlink/streamlink/blob/master/src/streamlink/plugins/tiktok.py)、[yt-dlpの実装](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/tiktok.py) |
| ツイキャス公式API | 条件付きで実装可能。Basic認証、current_liveのmovieオブジェクト、公式hls_url、commentsのslice_idとlimit 50を使う。Basic認証枠と個々の操作の残枠を保守的に扱う。 | 公式APIには配信の保護フラグがある。実資格情報でのBasic認証、ページング、共有枠単位、本編の先頭・末尾は未検証。[公式APIリファレンス](https://apiv2-doc.twitcasting.tv/) |
| ツイキャス匿名番号 | 未検証。番号が得られる視聴用WebSocketと照合契約が確認できるまで番号をNULLとする。 | 公式Realtime /livesは新着ライブ用。アクセス権、同一MovieとコメントID、一意照合、切断を実機で確認してから採用する。[公式APIリファレンス](https://apiv2-doc.twitcasting.tv/) |
| 実行環境 | Ubuntu 24.04.5、WSL2カーネル、Python 3.12.3、uv 0.13.9をコマンドで確認。.python-version、pyproject.toml、uv.lock、開発憲法が3.12系列で一致。FFmpegとffprobeはPATH上で同じ6.1.1系を確認。 | 採用するビルドのHEVC等の可否は実ファイルで診断。管理された作業環境で /mnt/d は読み取り専用。Windowsドライブのファイル確定とWindowsブラウザのNAT／mirrored接続は実機検証が必要。依存は既存のuvキャッシュを隔離コピーしてオフラインで解決する。 |
| 利用条件とライセンス | 取得はサービスの利用条件と必要な権限が整った配信に限定する。通常の公式API／公開取得経路のみを候補とする。 | 公開表示と抽出の許可は別に確認する。実配信テストは適用条件・利用者権限・分離済み保存先の確認後に実施。各配布物のライセンスは選定したバージョンの配布メタデータおよびライセンスを参照する。[TikTokの公式規約](https://www.tiktok.com/legal/page/row/terms-of-service/ja)、[ツイキャス利用規約](https://twitcasting.tv/indexlicense.php) |

## 現行実装での再確認

- TikTokLive 7.0.1のインストール済みソースで、`start()` が受信タスクを返すこと、既知Room IDの指定、既定の生存確認が状態値2の厳密な判定と一致しないことを確認した。監視はHTTP応答と状態値を検証し、受信は確認済みRoom IDへ接続する。SDKのテストは遅延イベント、終了時キュードレイン、受信例外を模擬した。
- ツイキャスは公式リファレンスのHTTP Basic、`current_live`、`hls_url`、`comments` の `slice_id` 連携を参照した。50件で飽和した場合のページ再照合と残枠制御を模擬した。初期プリロールの識別は実ファイル名だけに委ねず、通常の不連続より前の映像を保持する回帰試験を実施した。
- ローカルのFFmpegで作成したHLS素材を録画子プロセスに入力し、模擬TikTokイベントからSQLiteとSRTまで照合した。WSL2の現行バイナリはFFmpeg／ffprobe 6.1.1。これは採用可能な固定下限を意味せず、実際のコーデックをファイル単位で検証する。
- 実サービスの認証情報を使った接続、正式な利用権限のある試験配信、Euler Streamの操作別枠、ツイキャス匿名番号を照合する視聴用WebSocketは未検証。読み取り専用のWindowsドライブでは媒体を確定せず、Windows側のNAT／mirrored通信、サインイン起動、ロック時・再起動の継続試験も保留した。
