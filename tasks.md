# tasks.md（作業記録）

最終更新: 2026-11-25 / ブランチ `trial/full-sdd-05` / 実装担当: Claude Code

## 試験許可（利用者指示 2026-11-25）
- 実配信: TikTok `yanaks64`、ツイキャス `maoki_it`。資格情報は既存アプリ（~/projects/live-recorder）と非共有、署名キーは無料枠。**期間・終了条件は利用者が「テスト準備完了」時に通知**（通知まで実接続しない）。
- Windows: 専用検証フォルダ `D:\LiveRecorder`（`/mnt/d/LiveRecorder`）、全操作許可。許可は `~/.local/state/live-rec-lab-shared/windows-test-permit.json`（隔離のみ有効）。
- GPU/モデル: RTX 5070 Ti、faster-whisper モデル取得を許可。
- 保護パス: `~/projects/live-recorder/data`（`protected-paths.json`）。既存アプリは Docker で :8080 稼働中（触らない）。

## 機能表
| 機能 | 実装状態 | 動作確認 | 利用可否・残件／次の作業 |
|---|---|---|---|
| 最小起動（start/status/url/stop/config/backup/diagnose） | 実装済み | ローカル: 起動・二重起動拒否・停止・diagnose JSON・Host/Origin拒否・/docs 404。mirroredのTIME_WAITで再バインドが最大約2分遅れるため、listener不在のEADDRINUSEは同ポートで待機（実測40秒） | 利用可（隔離）。通常モードは未起動 |
| 隔離境界・保護パス・Windows許可 | 実装済み | ローカル: 許可外の録画ルート拒否 | 保護パス=~/projects/live-recorder/data |
| 外部通信ゲート・共通ロック・永続予算 | 実装済み | ローカル: 既定拒否。TikTokLive/Streamlink/ffmpeg内部通信はゲートを通れない（要求§19の残余境界）。起動前にゲートで宛先を検査し、httpxはフックで強制 | 実接続時に実測で確認 |
| DB（Alembic初期revision、WAL） | 実装済み | ローカル: 新規作成・未知DB拒否 | 試行予算テーブル含む |
| ffmpeg/ffprobe解決 | 実装済み | 非対話PATHは /usr/bin の 6.1.1。`DEV_LIVE_REC_LAB_FFMPEG_BIN` 指定で 9.1 | READMEに設定手順を記載予定 |
| UI 5案比較 | 実装済み（プレビュー） | 実ブラウザ(Edge 1440)でA〜Eの主要見本を表示確認 | **A案採用済み（修正指示2点、ui/design.md）**。Phase 5 でトークン→シェル→共通部品→代表3画面 |
| 媒体確定・命名（storage） | 実装済み | **実機(D:\LiveRecorder, 9p drvfs)**: renameat2(NOREPLACE)=EINVALで不可、os.linkは非上書き（大小文字違いもEEXIST）。pytest --run-hardware 2件合格（非上書き・外部置換拒否・縦疎通） | 実機確認済み（Windows媒体の確定）。容量・マウント断は未確認 |
| 配信者登録（入力検証・重複・再有効化・フォルダ名） | 実装済み | ローカル: 入力プロファイル（IDは小文字へ正規化） | ID大小文字同一性は実アカウントで確認予定 |
| 録画（子プロセス・進捗監視・有限再試行・確定・SRT） | 実装済み | ローカル（実ffmpeg・疑似配信）: 検知→録画/コメント独立→停止→mp4確定→収束→コメントSRT | 実配信は許可済み・開始通知待ち |
| コメント収集（キュー・バッチ・重複排除・再接続） | 実装済み | ローカル: ID重複排除（8受信→6保存） | |
| TikTok アダプター | 実装済み | **実配信**: 監視・Streamlink録画・TikTokLive接続（Room ID一致）を確認（上記テスト記録） | 利用可（隔離・`--allow-live`）。yt-dlp経路は未実証 |
| ツイキャス アダプター | 実装済み | **実配信**: current_live・comments(実コメント保存)・Streamlink録画を確認 | 利用可（隔離）。公式HLS直取得はffmpegのTLS対応が必要（自動切替）。プリロール除外・匿名番号は未実装 |
| Web 8画面・API（A案、HTMX、設定・配信者・録画・コメント・ログ・システム） | 実装済み | ローカル+実ブラウザ(Edge): 画面描画、設定の未保存検出・検証・原子性・離脱確認、HTTP防御(Host/Origin/CSRF)、配信者API。pytest test_web | 実表示で共通側を調整済み。残: 配信者追加/削除ダイアログ・並べ替えの実ブラウザ操作、削除・手動収束API(Phase 7) |
| 音声SRT・統合ASS | 実装済み | **実GPU(RTX 5070 Ti, CUDA float16, large-v3)**: Windows SAPI合成の日本語音声を文字起こし→音声SRT・ASS生成（pytest --run-hardware）。モデル取得済み(Systran/faster-whisper-large-v3 rev edaa852e, 2.9GB, 2026-11-25、隔離models/)。単体16.5秒(ロード含む) | ASSの外部プレイヤー表示は未確認（Windows側プレイヤー）。実配信音声は未確認。speech extraにnvidia-cublas/cudnn(cu12)が必要（実測）。PyAV 19非互換のためWAVをnumpyで渡す |
| アーカイブ削除・手動収束・容量/期限・照合 | 実装済み | ローカル(pytest test_archives 4件): 世代照合削除、外部置換/未接続は保留しDB保持、削除済みの再検知防止、稼働中/未確認/自動削除の拒否。実ブラウザで配信者追加・不正入力表示・並べ替え・削除確認 | 利用可（隔離）。実Windows媒体での削除は未実施（Windowsマウントの世代識別=dev/ino/sizeは確定時に実測済み） |
| バックアップ・停止中の手動復元 | 実装済み | ローカル(pytest test_restore): 候補検査・退避・マーカー・手動交換・削除済み照合 | 実運用DBでの復元は未実施 |
| Windowsサインイン後の自動起動 | スクリプト実装済み（scripts/windows、UTF-8 BOM必須・引数は明示引用） | **実機**: Windows PowerShell→wsl.exe の非対話起動（隔離で実施）→WindowsからのHTTP(localhost, mirrored)到達→停止を確認。非対話PATHでは ffmpeg 6.1.1 が選ばれる（実測）。タスクスケジューラ登録・実ログオンは未実施 | 登録は利用者の手動操作（docs/manual.md §2）。登録・再ログオン確認までは自動起動を利用不可として扱う |
| README・運用マニュアル | 実装済み | 実コード・実測と照合して記述 | 実配信後に未検証欄を更新 |

## 技術判断・外部保留メモ
- Euler Stream 無料枠: 公式rate-limitsページに数値の記載なし（確認 2026-11-25）。`/accounts/me/rate_limits` を実測予定。それまで接続は保守的な計上（10分4回）。
- ffmpeg: 非対話PATHは6.1.1、対話は9.1（~/.bashrc）。`LIVE_REC_LAB_FFMPEG_BIN` で固定する。9.1 でfilter `adelay` 付きの音声入力が停止する事象あり（製品経路はstream copyのため影響なし）。
- ツイキャス公式API: current_live/comments は60回/60秒（公式 apiv2-doc.twitcasting.tv、確認 2026-11-25）。

## 実配信テスト記録（隔離・`--allow-live`、利用者許可: TikTok `yanaks64`・ツイキャス `maoki_it`、期間 2026-11-25 14:30 JST まで、終了条件=推奨）
終了条件（推奨として採用）: 期限(14:30)到達、または公式枠(429/残枠)・アクセス制限・想定外の外部エラーが継続した時点で停止。録画は取得済みを確定して終了。
- **TikTok監視**: 公開room APIで検知。`liveRoom.roomId` がRoom IDとして使え、TikTokLive接続時のRoom IDと一致を確認。`status=1` が一時的に返りUNKNOWN扱い（終了・配信中にしない）を確認。
- **TikTok録画**: Streamlink が実配信を取得（HEVC 1080x1920 + AAC, FLV）。ライブFLVは読取時に長さが取れないため、映像コーデック識別後にstream copyでMP4化し確定後に検証する方式へ修正（実測で必要と判明）。確定MP4（数十〜百数十MB）を `D:\LiveRecorder\live-test` に非上書きで確定。
- **TikTokコメント**: TikTokLive + Euler署名で接続成功。受信件数は配信の状況による（接続・Room ID一致までを確認）。Euler無料キー `/accounts/me/rate_limits`: 分60・時500・日1500、接続1回で残枠が1減ることを実測。
- **ツイキャス**: current_live 検知OK（`movie.id`, `hls_url`）、commentsは slice_id=0 が400になる不具合を修正（0件の間はoffset取得）。実コメントを保存できた。公式HLSの直接取得は、利用中のffmpeg 9.1 がTLS非対応のため不可（`ffmpeg -protocols` にhttpsなし）→ HTTPS対応を検出して自動でStreamlinkへ切替。Streamlinkで有効なMP4を確定。
- **結果（14:12〜14:24 JST、再起動を含む）**: 有効セグメント9本（ツイキャス5本 h264 計約10分、TikTok 4本 HEVC 1080x1920 計約10分、約2.9GB）を `D:\LiveRecorder\live-test` に確定。FLV修正前の1本（27MB）は無効として一時ファイルを保全。コメントは両サービスとも低調（ツイキャスの匿名コメントを実保存し、該当セグメントにコメントSRT生成、他は「対象なし」）。収録音声は両配信とも無音(-91dB)のため音声SRT・ASSは「対象なし(発話なし)」で正しく終端（実音声の文字起こしは合成音声のGPU試験で確認済み）。
- 運用上の発見: 再起動・予期せぬエラーのたびに追加試行予算を消費する（設計どおり）。開発中の不具合で消費した隔離DBの予算は、テスト用DB上でのみ手動リセットした。

## 初期残件（2026-11-25 時点）
実行可能な残件: 0 件。下記は外部要因による保留。
| 保留 | 試した経路・根拠 | 代替 | 再開条件 |
|---|---|---|---|
| ツイキャスのプリロール除外・匿名コメント番号・公開tc-hls | 配信側の状況に依存し、今回の配信では確認できていない。匿名番号は取得できなければNULL | 公式HLS→Streamlinkの2段（tc-hlsは未実装） | プリロールや匿名コメントがある配信での確認 |
| yt-dlp録画（TikTokの代替エンジン） | 実配信(`u971368`/`mihiro519`、room API=LIVE)で試行: yt-dlp 2026.11.23 は curl-cffi 導入後も「not currently live」を返す（抽出器が署名なしの webcast/room/info を使い同じ「Request params error」になるため）。**現状のTikTokでは利用不可と実測** | Streamlink（実録画済み） | yt-dlpの抽出器がTikTokの現行仕様へ追随した版で再試験 |
| TikTokLive+FFmpeg録画（3番目のエンジン） | 主経路（Streamlink）が成立したため未採用 | Streamlink→yt-dlp | 主経路が不成立の場合に評価 |
| 実配信音声のASS外部プレイヤー表示 | libass描画までは確認 | - | Windows側プレイヤーで目視 |
| Windows自動起動の登録・WSL再起動後の復帰 | 非対話起動とlocalhost到達は実機確認済み。タスク登録は利用者操作 | - | 利用者が登録して実ログオンで確認 |

## 実配信テストの追加知見（2026-11-25 14:30以降の追試）
- （訂正 2026-11-25 17時台）下の「年齢制限」判定は**誤検知**だった。SDKの `fetch_room_info` は応答の `data` に `prompts` があるか項目が1つ以下だと `AgeRestrictedError` を投げるが、実応答は `{"message":…,"prompts":"Request params error"}`（署名なしの要求不備）で年齢制限の根拠ではない。判定を削除し、`u971368`・`mihiro519`（room API=LIVE）でStreamlink録画が成立（HEVC MP4確定）。確実な根拠があるアクセス制限（ツイキャスの合言葉等）の停止機構は残す。
- （旧記述・誤検知）**TikTok（yanaks64）はSDKが年齢制限を返す**（`AgeRestrictedError: Age restricted stream`）。匿名接続ではコメントが届かず（接続後30秒でRoomMessageのみ）、room APIの視聴者数は1。要求§4.2.5に従い、制限は回避せず（ログイン/sessionidは使わない）、判明時に録画・コメントの双方を停止し `end_reason=restricted` で収束、同じ配信は再開しない（実配信で動作確認、回帰テスト追加）。Streamlinkは制限下でも録画できたが、仕様上は停止が正。
- **ツイキャスの匿名コメント番号**: 公式API v2は共有アカウント `c:tw3`／名前「匿名コメント」しか返さず、番号（#881 等）は含まない。番号は視聴用WebSocket（非公式）経由でのみ取得できる可能性があるが、利用許可の範囲外と判断され未実施。番号はNULLのまま「(匿名)」と表示（要求§4.2.2）。
- **ASS**: アプリ停止時に収束した場合、後処理（SRT・音声・ASS）まで進めずASSが生成されない不具合を修正。起動時に収束済みセッションの未処理分を再処理する。

## 追記（2026-11-25 15時台）
- **プリロール除外（実配信で確認）**: 公式HLS先頭の黒地「…」4秒（60フレーム周期×2、無音）を、フレーム内容の周期照合＋無音で判定し、プリロールのみの取得は正式保存・採番前に除外、プリロール+本編は先頭を切り落として保存（判別不能は保全）。実配信で除外イベントを確認。
- **匿名コメント番号（実配信で確認）**: 視聴用WebSocket（非公式 `eventpubsuburl.php`、利用者が許可）のイベント `author.name="匿名コメント#579"` と、公式APIと共通のコメントIDで照合。実配信で番号つき保存を確認。取得できなければNULL。SRT/ASSの表記は利用者指定で「(匿名コメント#579)」（要求§7.7を更新）。
- **公開tc-hls**: 直接URL（metastream.m3u8）は404で独自実装はしない。Streamlinkの twitcasting プラグイン（8.6.3）は `streamserver.php` の `tc-hls` ストリーム（公開の視聴用HLS）を使うため、Streamlink経路の実録画（実配信で有効なMP4を確定済み）が公開tc-hls→Streamlink HLSの段に相当する。公式HLS→Streamlinkの2段で運用。
- **ASS**: MPC-HCで表示確認済み（UTF-8 BOM付き）。

- **公開tc-hls（実配信で通し確認）**: `streamserver.php` の `tc-hls.streams` からURLを取得。隔離専用の環境変数 `DEV_LIVE_REC_LAB_TWITCASTING_FORCE_ENGINE=tc-hls` で経路を固定し、製品の録画フローで約151秒のH.264 MP4（2.9MB）を確定（engine=tc-hls, valid）。通常運用の順序は 公式HLS→tc-hls→Streamlink のまま。
- **yt-dlpのTikTok利用**: 利用者提案により既定の `tiktok_engine_order` から除外（設定値としては選択可）。

## TikTokコメント（実配信で確認、2026-11-25 17:3x）
- 原因と修正: room API の `streamId` は**ストリームID**で、WebSocketの接続先Room ID（例 7712000000000000001）とは別。従来はstreamIdをRoom IDとしてSDKへ渡していたため接続できてもチャットが届かず（RoomMessage 1件のみ）、先の「Room ID一致」は自分が渡した値との比較にすぎなかった。参照実装(fluxrozin/live-recorder 07c6404)と同じく Room ID を渡さず `start(fetch_room_info=True)` で接続し、`room_info.stream_id` と監視の `streamId` で照合する方式に修正。
- 実配信（`yanaks64`）: 10件受信・5件保存（重複排除後）。再接続は指数バックオフ（連続リトライで空応答になる実測）。
- 年齢制限の判定: 旧「年齢制限」断定は誤検知（上記）。正規の接続手順で room info が取得できない（`AgeRestrictedError`）場合のみアクセス制限として録画・コメントを停止する（参照実装と同じ扱い）。
- Euler無料キーでTikTokチャットは取得できる（実測）。

- **明示再開API**（`POST /api/v1/sessions/{id}/resume`、scope=recording/comments）を追加（要求§6.5.2〜3、設計§17）。手動停止・枯渇後に新しい予算世代で再開。画面はダッシュボードのアクティブセッションにボタン。pytestで確認。

## 再開点
Phase 1〜8 の実装を完了（UI=A案）。次: 利用者から実配信の期間・終了条件の通知を受けたら、`uv run live-rec-lab start --dev --allow-live` で TikTok `yanaks64` / ツイキャス `maoki_it` を実測し、tasks.md の保留行と docs/manual.md §7 を更新する。
