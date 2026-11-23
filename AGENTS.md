# AGENTS.md

## 作業・安全性・仕様の入口

- 確定要件は `requirements.md`、UI/UXは `docs/UI_UX.md` を正本とする。`docs/design/` は設計参考、`docs/ARCHITECTURE.md`・`CONFIGURATION.md`・`INSTALL_WSL2.md`・`TROUBLESHOOTING.md`・`LIMITATIONS.md` は実装前と関係する実装タスクで確認し、古い試作結果を現在の機能と取り違えない。詳細な実行順と優先順位は `docs/prompts/master.md` に従う。
- Git操作前にブランチと変更を確認する。`main` への変更は利用者の明示許可が必要。許可なくブランチ作成・切替、破壊的なGit操作、commit・push・mergeをしない。Spec Kitフックにも同じ制約を適用する。
- Spec KitのFeature選択はブランチ名から推測しない。初回は `.specify/feature.json` が示す `specs/001-initial-live-recorder` を用い、既存 `spec.md` があれば `speckit-specify` によるテンプレート上書きを禁止する。
- `speckit-analyze` は読取専用で、同Skillの検出後に修正するには必要な利用者承認を得る。`speckit-converge` は `tasks.md` への追記専用とし、原本起点の追跡監査を別途行う。
- `.env`・認証情報・DB・録画ファイル・実行時状態をGitへ追加しない。実装時は `.gitignore` とGit追跡状況を確認する。

## コミットメッセージ

コミットは利用者が求めたときだけ行う。対象ファイルだけを `git add -- <paths>` でステージし、次の形式で書く（この書式の正本は本節）。

- **件名**は `type(scope): summary` の形式で、全体を72文字以内にする。`type` は変更の種類（例は `feat`、`fix`、`docs`、`test`）、`scope` は主な対象（例は `core`、`web`、`contracts`、`development`）を表す。`summary` は `add`、`fix`、`clarify` など英語の動詞の原形で始め、変更内容を具体的に書く。
- **本文**は件名から空行を1行空けて日本語で書く。変更の目的と要点、実際に実行した検証と結果、重要な未検証事項とその理由を記す。該当する機能ディレクトリ（例 `specs/002-core-web-slice`）があれば記載する。

```text
docs(development): clarify commit message rules

開発ツール間で件名と本文の書き方を揃えるため、AGENTS.md に形式と例を追記した。
検証として git diff --check とリポジトリの静的検証を実行し、どちらも通過した。
```
