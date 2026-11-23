# AGENTS.md

## コミットメッセージ

コミットは利用者が求めたときだけ行う。対象ファイルだけを `git add -- <paths>` でステージし、次の形式で書く（この書式の正本は本節）。

- **件名**は `type(scope): summary` の形式で、全体を72文字以内にする。`type` は変更の種類（例は `feat`、`fix`、`docs`、`test`）、`scope` は主な対象（例は `core`、`web`、`contracts`、`development`）を表す。`summary` は `add`、`fix`、`clarify` など英語の動詞の原形で始め、変更内容を具体的に書く。
- **本文**は件名から空行を1行空けて日本語で書く。変更の目的と要点、実際に実行した検証と結果、重要な未検証事項とその理由を記す。該当する機能ディレクトリ（例 `specs/002-core-web-slice`）があれば記載する。

```text
docs(development): clarify commit message rules

開発ツール間で件名と本文の書き方を揃えるため、AGENTS.md に形式と例を追記した。
検証として git diff --check とリポジトリの静的検証を実行し、どちらも通過した。
```
