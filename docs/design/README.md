# LiveRecorder — 設計文書索引

> **位置付け：既存の確定資料から抽出した設計ドラフト（実装済みを意味しない）**
>
> 抽出基準：`fluxrozin/live-rec-lab` / `main` / `d3f2b3a83c0895ecfbe136d8d997dfc611d575ff`（2026-11-23確認）

## 文書の役割と優先順位

1. [`../../requirements.md`](../../requirements.md) — 機能・非機能要件、制約、受入条件の**正本**。
2. [`../UI_UX.md`](../UI_UX.md) — UI/UXの詳細方針の**正本**。矛盾時は `requirements.md` を優先。
3. 本ディレクトリ — 上記から抽出した**実装前の横断設計**。新しい要件を勝手に確定させない。
4. `../../specs/NNN-feature/` — GitHub Spec Kitで機能単位に作る `spec.md`、`plan.md`、`tasks.md` と必要な `research.md`、`data-model.md`、`contracts/` 等。
5. `../../.specify/` — Spec Kit本体、テンプレート、Constitution。アプリ設計文書の保管場所にしない。

## 文書一覧

| 文書 | 設計する範囲 | 主な出典 |
| --- | --- | --- |
| [01-system-architecture.md](01-system-architecture.md) | 全体構成、プロセス、モジュール責務、依存関係 | 要件 §2–3, §10.1.1 |
| [02-platforms-and-monitoring.md](02-platforms-and-monitoring.md) | 配信サービス接続、ID正規化、レート制御、状態検知 | 要件 §4–5 |
| [03-recording-and-comments.md](03-recording-and-comments.md) | 録画・コメントの独立ジョブ、時刻、SRT、欠損 | 要件 §6–8 |
| [04-data-and-storage.md](04-data-and-storage.md) | SQLiteエンティティ、ファイル配置、確定と復旧 | 要件 §9–10 |
| [05-web-and-ux.md](05-web-and-ux.md) | Web/API境界、画面遷移、状態表現、UI原則 | 要件 §11、UI/UX全体 |
| [06-operations-and-quality.md](06-operations-and-quality.md) | 運用、セキュリティ、ログ、テスト、受入条件 | 要件 §13–17, §23 |
| [07-decisions-and-gaps.md](07-decisions-and-gaps.md) | 既存資料間の不整合、要実証事項、未決定事項 | 上記全体・リポジトリ現状 |

## ステータスの読み方

- **確定要件の抽出**：出典に明記された必須事項。設計文書の言い換えで仕様を変えない。
- **設計方針**：要件が指示する責務や処理の整理。詳細なAPIシグネチャやDDLなどは実装フェーズで検証する。
- **要検証／要決定**：外部APIや実機の挙動、未定の振る舞い。未実装・未確認を「対応済み」と表現しない。

## 変更・利用ルール

- 機能要件を変更する場合、先に `requirements.md` を更新し、影響する設計・Spec Kit featureへ反映する。UI固有変更は `docs/UI_UX.md` も更新する。
- 本書の各設計は、Source of Truthの**再掲ではなく、各コンポーネントを実装できる粒度に整理した参照用文書**である。重複記述が矛盾した場合は原本を確認する。
- 細かなRESTエンドポイント、SQLカラム型・制約、状態遷移表の完全版は、実装対象が確定したとき `specs/NNN-feature/` に作る。横断設計との整合を保つ。
- `src/` 内に実装のない空モジュールや、将来機能用のダミーコードを生成しない。
- 初期対象は **Windows 11 + WSL2 Ubuntu 24.04 / Python 3.12 / uv / TikTok LIVE + ツイキャス**。本リポジトリの現在の `README.md` と要件の前提に合わせたもので、別構成のWindowsネイティブ案を無断で混入しない。
