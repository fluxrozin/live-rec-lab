<!--
Sync Impact Report
Version: unfilled scaffold → 1.0.0
Principles: five template slots filled from AGENTS.md sections I–V.
Added: technical boundaries and development workflow.
Removed: scaffold examples. Deferred placeholders: none.
-->

# LiveRecorder 開発憲法

## Core Principles

### I. 動く最小経路を先にする

TikTok LIVE・ツイキャスの検知、録画、コメント、SQLite、コメントSRT・音声SRT・統合ASS、主要8画面を初期実装に含める。単一管理Pythonプロセスと必要な管理子を使い、最小隔離からTikTokの縦断経路を優先する。初期UIと全面再設計は架空データの3案から利用者が明示選択する。選択待ちも独立作業を進める。

### II. 取得済みデータを失わない

録画とコメントは独立させる。媒体の実効ルートと保護パスを確認し、衝突・不明時は操作を保留する。通常のWindows保存先が使えない場合は代替保存へ切り替えない。同一正規化IDのサービス間フォルダを共有し、異なるIDの安全名衝突は安定識別子で分離する。非上書き・所有・世代を守り、削除と字幕公開を台帳で照合する。コメント期限削除はコメントSRTとASSを先行し、音声SRT・動画・旧手動バックアップを保持する。期限削除済みASSを復活させない。DBの一貫バックアップと停止中のマーカー付き復元は要求§15.2.1に従う。

### III. 未確認を成功にしない

未確認・取得失敗・未収集をオフライン・0件・成功に置き換えない。外部API・媒体・実機は実測と区別する。公開／許諾済み配信に限定し、アクセス制御・DRM・CAPTCHA等を回避しない。固定AC／TCは機能単位で追跡し、実装・検証・利用可否を区別する。

### IV. ローカル境界を超えない

127.0.0.1限定で通常Webログインを設けず、Host・Origin・CSRF、入力検証、XSS・SSRF対策とGET無副作用を維持する。秘密・媒体本体をHTTPで公開しない。開発者機能は隔離専用とし、実配信／署名通信は既定拒否。開発の実接続は範囲を限定した明示許可と共通排他・永続予算を要する。通常運用では有効な登録対象を設定範囲で自動処理する。

### V. 障害を追える記録を残す

UTC・component・event・correlation_id・例外を構造化し、非秘匿本文を保持する。録画・コメント・外部API障害と画面ロック／画面OFF・スリープ／休止を区別する。停止中の処理を成功と扱わない。共通診断は読取専用とし、修正時は稼働版を照合する。

## 技術とデータの境界

Windows 11／WSL2 Ubuntu 24.04／Python 3.12／uv、SQLiteはLinux側、通常媒体はWindows側、隔離媒体は専用Linux側に置く。隔離と別worktreeは管理領域・固定ポートを分離する。Uvicornは1 worker、Webは `src/live_rec_lab/web/` に集約する。操作画面はライト1種・フォント2択。ブラウザ動画再生・ライブプレビュー・同期再生、別SPA・外部キュー等を初期要件に追加しない。

## 開発と検証

Specify → Plan → Tasks → Analyze → Implement、必要時Converge。要求§1.3と§17を基準に代表正常系・否定系を検証する。実接続と実機試験は許可と明示フラグを求め、通常データや秘密を試験へ継承しない。独立テストはxdistを使い、分離不能資源はserialで実施する。未実施と安全保留を記録する。

## Governance

`AGENTS.md` が開発憲法の正本。確定原則の変更は利用者承認を要する。意味変更はMAJOR、追加はMINOR、明確化はPATCH。製品・UI・技術の各正本と要求§1.3に従い、実装計画と統合時に照合する。既存変更を保護し、Gitのブランチ変更・コミット等は利用者の指示を要する。実データ・秘密・生証跡はGit管理外とする。

**Version**: 1.0.0 | **Ratified**: 2026-11-24 | **Last Amended**: 2026-11-24
