<!--
Sync Impact Report
- Version change: unversioned template → 1.0.0 → 1.0.1
- 1.0.1: 技術制約の名称を明確化（リポジトリ名 live-rec-lab、アプリケーション名 LiveRecorder）
- Modified principles:
  - [PRINCIPLE_1_NAME] → I. 動く最小経路を先にする
  - [PRINCIPLE_2_NAME] → II. 取得済みデータを失わない
  - [PRINCIPLE_3_NAME] → III. 未確認を成功にしない
  - [PRINCIPLE_4_NAME] → IV. ローカル境界を超えない
  - [PRINCIPLE_5_NAME] → V. 障害を追える記録を残す
- Added sections: 技術制約, 開発の流れ（プレースホルダを置換）
- Removed sections: なし
- Follow-up TODOs: なし
-->

# LiveRecorder 開発憲法

## Core Principles

### I. 動く最小経路を先にする

初期版は、TikTok LIVE とツイキャスの検知、録画、コメント保存、SQLite、
セグメント単位の SRT までを、WSL2 上の単一 Python プロセスで通さなければならない。

- 使わない抽象、空モジュール、将来機能のダミー実装を先に置いてはならない。
- React、Next.js、Vue、Redis、Celery、Kubernetes、および初期必須の Docker を、
  要件にない理由で追加してはならない。
- Web 画面は `src/live_rec_lab/web/` に集め、別フロントエンドへ複製してはならない。
- ブラウザ内の動画再生、ライブプレビュー、コメントの同期表示を実装してはならない。

根拠は `requirements.md` §1.2、§2.3、§10.1.1、§12。

### II. 取得済みデータを失わない

録画とコメント収集は独立させ、一方の失敗で他方を止めてはならない。

- 取得できなかった区間を、取得できたものとして扱ってはならない。
- Windows 側の録画先が使えないとき、黙って WSL2 内へ保存先を切り替えてはならない。
- 稼働中の SQLite ファイルを、そのままコピーしてバックアップとしてはならない。
  `backup` は一貫したバックアップ手段を使わなければならない。
- 未確定の録画ファイルを、検証前に完成扱いにしてはならない。
- 秘密情報、署名付き URL、認証トークンを Git、ログ、画面へ残してはならない。

根拠は `requirements.md` §1.2、§10.2、§13.3、§14、§15.2。

### III. 未確認を成功にしない

外部 API と実機の挙動は、実測するまで未検証としなければならない。

- 動作を確認していない機能を完成済みと書いてはならない。
- `UNKNOWN`、取得失敗、未収集を、オフラインや 0 件に丸めてはならない。
- DRM、CAPTCHA、認証、アクセス制御、地域制限の回避を実装してはならない。
  対象は公開配信、または録画してよい権限がある配信に限る。
- 新しい振る舞いは `requirements.md` に書いてから実装する。
  UI 固有の変更は `docs/UI_UX.md` も先に改める。
  `docs/design/` は原本の抽出であり、原本にない要件を確定してはならない。

根拠は `requirements.md` §1.2 と `docs/design/README.md`。

### IV. ローカル境界を超えない

初期の Web はログインなしとし、`127.0.0.1` 以外へバインドしてはならない。
LAN やインターネットへ認証なしで公開してはならない。

- 状態を変える操作は POST 等で行い、CSRF と Origin/Host を検証しなければならない。
  GET で削除や設定変更をしてはならない。
- 通常の Linux ユーザー権限で動かし、root を常用してはならない。
- 利用者入力の URL を、検証なしで FFmpeg、Streamlink、yt-dlp へ渡してはならない。
- ダウンロードは録画保存ルートの外のファイルを返してはならない。

根拠は `requirements.md` §2.2、§13。

### V. 障害を追える記録を残す

失敗は握りつぶして正常終了にしてはならない。構造化ログに、UTC 時刻、
component、event、correlation_id、例外を残し、秘密情報は除かなければならない。

- 録画失敗、コメント収集失敗、外部 API 失敗は別々に記録しなければならない。
- 画面ロック中とディスプレイ OFF 中は処理を続ける前提とする。
  スリープと休止を、それと同じ継続成功の条件にしてはならない。
- 停止中に終わった配信を、録画できたものとして扱ってはならない。

根拠は `requirements.md` §14。

## 技術制約

- 実行基盤は Windows 11 + WSL2 Ubuntu 24.04、CPython 3.12.x、uv とする。
  3.13 と 3.14 を使ってはならない。
  `requires-python` は `>=3.12,<3.13`、`.python-version` は 3.12 系とする。
- パッケージ名は `live_rec_lab`、インポート経路は `src/live_rec_lab/` とする。
  Git リポジトリ名は `live-rec-lab`、アプリケーション名は LiveRecorder とする。
- 実行時データの既定は `~/.local/share/live-rec-lab/`（SQLite）と
  `~/.local/state/live-rec-lab/`（runtime と logs）とする。
  ディレクトリ名はリポジトリ名に合わせる。
- Windows 側の録画ルート例 `D:\LiveRecorder\recordings` は、アプリケーション名を使った
  動画と SRT の保存先であり、上記の実行時ディレクトリとは別パスである。
  SQLite を `/mnt/c`、NAS、SMB、同期ストレージへ置いてはならない。
- UI はライト系テーマを 1 種類だけとする。
  ダークテーマ、テーマ切替、OS の配色への追従を実装してはならない。
  フォントは Windows 標準と Noto Sans JP の 2 種類とする。
- 初期プロセスは Uvicorn 1 worker とする。
  `--reload` の常用と、監視ループの多重起動をしてはならない。

## 開発の流れ

- 正本の優先順位は `requirements.md`、次いで UI は `docs/UI_UX.md`、
  設計の横断整理は `docs/design/`、機能単位の仕様は `specs/NNN-feature/` とする。
- 機能は `spec.md`、`plan.md`、`tasks.md`、実装とテストの順で進める。
  Spec Kit の手順は `.cursor/skills/`、`.agents/skills/`、`.claude/skills/` の
  `SKILL.md` に従う。
- コミットは利用者が指示したときだけ行う。件名と本文は `AGENTS.md` の形式に従う。
- 検証していない実配信・実機の結果を、検証済みと報告してはならない。

## Governance

この憲法は、実装とレビューで守る開発原則である。
`requirements.md` と矛盾したときは要件を正とし、憲法を改訂してから実装を合わせる。

- 改訂は本書を直接更新し、版を semantic versioning で上げる。
  原則の削除や意味の変更は MAJOR、原則や節の追加は MINOR、
  文言の明確化は PATCH とする。
- 制定日と最終改訂日を本書の版表示に残す。
- レビューでは、未確認の成功表示、秘密情報の混入、
  パッケージ名と実行時ディレクトリの逸脱、Python 3.12 以外の採用を確認する。
- 日常の作業入口は `AGENTS.md` とする。
  本書は開発原則の正本であり、アプリの要件本文を重複して定義しない。

**Version**: 1.0.1 | **Ratified**: 2026-11-23 | **Last Amended**: 2026-11-23
