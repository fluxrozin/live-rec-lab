# 開発プロンプト利用ガイド

LiveRecorder の開発を開始する人向けの手順書です。製品仕様の正本は `docs/`、共通開発原則は `AGENTS.md`、詳細な実行指示は各プロンプトです。本書はそれらを変更・代替するものではありません。

## 1. どのプロンプトを使うか

| ファイル                         | 用途                                   | 使い方                                                 |
| -------------------------------- | -------------------------------------- | ------------------------------------------------------ |
| [start.md](start.md)             | 一気通貫開発の開始・中断再開           | 内容をチャットに貼る、またはファイルを参照して実行する |
| [implement.md](implement.md)     | 全体実装の詳細指示                     | 開始プロンプトが参照する。直接実行も可能               |
| [redesign-ui.md](redesign-ui.md) | UIだけの初期設計・全面再設計・局所修正 | 全体実装から呼び出すか、完成後に単独実行する           |

**通常は `start.md` だけを実行すればよい。** `implement.md` の全文をチャットに貼る必要はない。エージェントがリポジトリ内のファイルを読めることが前提となる。

## 2. 前提環境とGitの確認

Windows 11 上の WSL2 Ubuntu 24.04 で、Linux側に配置したリポジトリを開く。Cursor、Codex、Claude Code のいずれかを用いる。ツール自体の導入は各公式手順に従う。以下は WSL2 のターミナルで実行する。

```bash
cd ~/projects/live-rec-lab  # 実際の配置先に置き換える
pwd
git status --short
git branch --show-current
uv --version
```

Git未初期化なら利用者が初期化を判断する。作業ブランチは利用者が選ぶ。mainで変更する場合は明示許可が必要。既存の変更を保護し、初期化によるファイル上書きやフックによるGit操作も確認する。利用者自身が必要なバックアップ・コミット等を判断するか、エージェントに個別に許可する。スターターの `.specify/`、`specs/`、生成済みSkillsが存在しないのは正常である。

uv が未導入なら公式の [uv installation](https://docs.astral.sh/uv/getting-started/installation/) に従う。スターターにはアプリ本体や `uv.lock` は存在しない。初回依存同期と最小パッケージ作成後の通常同期は `implement.md` §3 Phase 1 に従う。コアはspeech extraなしで同期し、音声段階で追加する。互換性問題の依存調整と必要な再lockは理由付きで委任済み。

## 3. Spec Kit の導入方法

### 3.1 CLIのインストール（端末）

Spec KitのCLIは `specify` であり、プロジェクトのPython依存とは分離してuv toolで管理する。公式の導入方法は [Installation Guide](https://github.github.com/spec-kit/installation.html) を参照する。

```bash
uv tool install specify-cli
uv tool update-shell  # PATHに見つからない場合のみ実行し、シェルを開き直す
specify version
specify --help
specify init --help
```

採用版を固定する場合は `uv tool install 'specify-cli==X.Y.Z'` のように実在する確認済みバージョンを指定する。既に導入済みならバージョンを確認して再利用する。CLIの導入とプロジェクトの初期化は別操作であり、更新時も自動で再初期化しない。

### 3.2 プロジェクトの初期化（端末）

**推奨：** 初期化も含めて一気通貫でエージェントに任せる場合、以下を手動で実行せず §4 の開始プロンプトへ進む。手動で初期化したい場合のみ、リポジトリのルートで利用するエージェントに対応する1行を実行する。

| エージェント | 初期化コマンドの例                                           | Skillsの配置先（対応版の例） |
| ------------ | ------------------------------------------------------------ | ---------------------------- |
| Cursor       | `specify init --here --integration cursor-agent --script sh` | `.cursor/skills/`            |
| Codex        | `specify init --here --integration codex --script sh`        | `.agents/skills/`            |
| Claude Code  | `specify init --here --integration claude --script sh`       | `.claude/skills/`            |

上記の統合名は [公式Integrations](https://github.github.com/spec-kit/integrations/) に基づく。採用版によってオプションや統合方式が異なるため、ローカルの `specify init --help` と採用版の公式手順を優先する。非空ディレクトリのマージ確認が出た場合は、バックアップ・上書き範囲を確認し、承認した場合のみ進む。`--force` は自動的に使わない。既存のGitリポジトリを再初期化しない。無人実行で選択待ちを避ける必要があるときだけ、対応版の `--non-interactive` を検討する。

初期化後は生成ファイル、Git差分、フックとSkillsを確認する。必要ならエージェントのセッションを開き直す。既に初期化済みなら再実行せず、採用版で対応する場合は `specify integration status` で状態を確認する。別エージェントの追加が必要な場合は公式の [統合管理手順](https://github.github.com/spec-kit/integrations/cli.html) を確認し、`specify integration install <key>` と必要な有効化を行う。生成済みSkillsを無断編集しない。

## 4. 一気通貫で実装する（推奨）

CursorではWSLのプロジェクトを開いてAgentチャットを使用する。CodexやClaude CodeではWSL側の同一リポジトリを作業対象としたセッションを使用する。

チャットに [start.md](start.md) の指示本文を貼るか、以下を送る。

```text
prompts/start.md を読み、その指示に従って LiveRecorder の開発を開始してください。
```

エージェントは `implement.md` に従い、Spec Kitから実装・検証まで自律的に進める。可逆的判断・正本に沿う派生修正は委任する。UI採用案は3案から利用者が明示選択する。重大な仕様変更や危険操作、未許可の開発時実接続は確認する。通常運用の登録・有効化済み通信と開発の範囲限定許可は要求§15.2.1に従う。実配信・実機・UI選択待ちが他機能を止めないようにする。最小隔離を先行し、テスト基盤は段階的に整備する。複数worktreeの設定は [統合マニュアル](../manual/README.md#developer-mode) を参照する。

## 5. Spec Kitの工程と手動利用方法

`specify` は端末のCLI。`speckit-*` は初期化によって導入されるエージェントのSkillであり、シェルコマンドではない。以下ではSkillの論理名を記載する。Cursorでは `/speckit-specify`、Codexでは `$speckit-specify` など、採用版の統合が実際に提供する呼び出し形式を使用する。Claude Codeの形式も生成済みSkillと公式統合ガイドで確認する。直接呼び出せなければ該当 `SKILL.md` を読み、その手順を実行する。

全体実装を任せる場合は、以下の工程を利用者が毎回手動入力する必要はない。個別実行でも `implement.md` のFeature照合、安全条件、簡易追跡と技術依存に従う。

| 工程             | Skill                        | 主な役割・成果物                                                      |
| ---------------- | ---------------------------- | --------------------------------------------------------------------- |
| 開発憲法         | `speckit-constitution`       | `AGENTS.md` の原則から `.specify/memory/constitution.md` を生成・照合 |
| 仕様化           | `speckit-specify`            | 要求正本に基づく `spec.md`                                            |
| 明確化           | `speckit-clarify`            | 必要時に曖昧さ・未確定事項を整理                                      |
| 計画             | `speckit-plan`               | `plan.md`、調査・データモデル・契約・quickstart                       |
| タスク           | `speckit-tasks`              | 依存関係付き `tasks.md`                                               |
| 品質確認         | `speckit-checklist`          | 仕様品質・検査項目                                                    |
| 整合性分析       | `speckit-analyze`            | 読取専用の矛盾・漏れ・依存検査                                        |
| 実装             | `speckit-implement`          | コード・試験・機能別検証・未検証の記録                                |
| 統合時の簡易照合 | Implementの一部              | `traceability.md` と全固定IDを一度照合。A/B/C・未検証を記録           |
| 収束             | `speckit-converge`（必要時） | 重要な追加タスクを追記し、追加実装・再検証                            |

初期Featureは `specs/001-initial-live-recorder/` を明示的に使用し、既存成果物を上書き・重複生成しない。Analyzeは読取専用とし、指摘は別工程で正本へ自律整合させる（採用Skill固有の承認契約は遵守）。重大な仕様変更だけ利用者判断を求める。固定IDの簡易対応表を維持する。Clarifyは重大な判断不能事項のみ、追加成果物は採用Skillの契約内で簡潔にする。初期は垂直スライスと早期UI比較、Aの代表検証を優先。Checklistは簡易、Analyzeは原則1回、Convergeは重要な残課題がある場合のみ。Skill固有の承認は守り、重複承認は設けない。承認対象以外の作業を続け、ターンが終了する場合は再開点を保存する。正本の段階参照はimplement.md §1に従う。

## 6. 中断後に再開する

利用量制限、ツール終了、外部条件未成立などで中断しても初期化やSpecifyをやり直さない。リポジトリと対象Featureを確認し、同じチャットまたは新しいチャットから次を送る。

```text
prompts/start.md を読み、implement.md の再開判定に従って中断箇所から続行してください。
既存成果物と検証証跡を照合し、完了済みタスクを初期化しないでください。
```

チェック済みタスクと現在のコード・検証結果を確認する。未検証事項を記録し、独立作業を継続する。モデル・エージェントを変える場合も同じ作業ツリーとFeatureを使う。

UI選択待ちで中断した場合は、保存された比較案からA/B/Cを選び「案Bを採用して、prompts/start.mdに従い同じFeatureで再開してください」のように伝える。選択は例であり利用者が決める。未選択なら採用案依存の実装を行わない。質問でターンが終わる環境では作業の同時進行を前提にしない。

## 7. 初期実装後の変更

### 7.1 バグ修正

既存の確定仕様に対する不具合なら、初期Featureの全面再生成は不要。採用版が対応する場合、公式のBug拡張を初期化済みプロジェクトへ導入する（端末）。

```bash
specify extension add bug
```

エージェントのチャットで次の順に実行する。下記はSkillの論理名であり、実際の呼び出し記号は §5 に従う。

1. `speckit-bug-assess`：再現手順、期待結果、実際の結果、ログ、関連資料を渡して原因・影響・修正範囲を精査する。
2. `speckit-bug-fix`：同一bug slugを指定して修正する。通常の読取診断とバックアップの隔離コピーで再現し、`start --dev` で失敗テスト・修正後の回帰を行う。通常運用相当の関連回帰も確認する。実運用DBを直接開発起動しない（要求§13.7、§14.6、§15.2.1）。
3. 実機で再現手順を試し、成功・失敗の証跡を記録する。
4. `speckit-bug-test`：修正・回帰試験と元の報告を照合する。要求§17に従い、独立試験はpytest-xdistで並列実行し、対象となる `serial` 試験を別途直列実行する。まだ直っていなければ失敗結果を記録し、同じbugのfix工程へ戻る（原因や範囲が変わるなら再assess）。 実接続／実機は独立した `live`／`hardware` と明示許可を要し、許可なしでは未実施とする。診断の対象・稼働コード版、テスト子の回収も照合する。

Bug拡張の導入や入力形式は採用版の [Bug Fixing Quickstart](https://github.github.com/spec-kit/guides/bugfix.html) を確認する。関連資料は原則としてassess時点で渡し、fixで新たに得た資料は追記して再評価する。仕様変更が必要ならバグ修正だけを根拠に正本を変更しない。

### 7.2 機能追加・仕様変更

初期Featureの完了状態を保護し、利用者が承認した変更について新しいFeatureを選定する。対象Featureを明示してSpecify → Clarify（必要時）→ Plan → Tasks → Checklist → Analyze → Implement → Convergeを行う。正本への変更、既存機能の回帰試験、追跡表との整合を確認する。初期実装が未完了で単に既定機能が不足している場合は、新機能扱いにせず初期Featureの不足を修正する。

### 7.3 UIだけの再設計

完成済みアプリに対して次の指示を送る。

```text
prompts/redesign-ui.md を読み、REDESIGN モードでWeb管理画面だけを再設計してください。
```

初期工程はINITIAL、単独再設計はREDESIGN、同じ案の中断再開はRESUME、採用済み範囲の局所変更はREFINEとする。参考調査の件数は目安。初期・単独再設計とも異なる3案の提示と利用者の明示選択は必須。選択前は採用案依存の製品UI実装だけを保留し、独立作業は継続する。RESUMEでは記録済みの利用者選択を再利用し、REFINEでは採用済みデザイン内の局所改善に限り再選択不要。UIだけの実行で録画・コメント・DB/APIを再実装しない。

## 8. 生成物とGit管理

| 対象                              | 位置付け                                            |
| --------------------------------- | --------------------------------------------------- |
| `.specify/`・エージェント統合     | 採用版の初期化で生成する開発環境                    |
| `.specify/memory/constitution.md` | `AGENTS.md` から生成する開発憲法                    |
| `specs/`                          | Featureごとの派生仕様・計画・タスク・追跡・検証記録 |
| `src/`・`tests/`・`uv.lock`       | 実装工程で生成する製品・テスト・依存ロック          |

スターターに含めないこととGit管理から除外することは異なる。証跡境界は `AGENTS.md` に従い、`specs/` 全体を除外せずローカル出力のみ除外する。共有すべき文書と開発環境はGit管理し、秘密・録画・DB・ログは除外する。commit/pushは明示許可が必要。正本と矛盾する派生文書は自律修正する。

## 9. 公式資料

- [GitHub Spec Kit](https://github.com/github/spec-kit)
- [Installation Guide](https://github.github.com/spec-kit/installation.html)
- [SDD Quickstart](https://github.github.com/spec-kit/quickstart.html)
- [既存プロジェクトへの導入](https://github.github.com/spec-kit/how-to/existing-projects.html)
- [エージェント別統合](https://github.github.com/spec-kit/integrations/)
- [Cursor](https://github.github.com/spec-kit/integrations/cursor-agent.html) / [Codex](https://github.github.com/spec-kit/integrations/codex.html) / [Claude Code](https://github.github.com/spec-kit/integrations/claude.html)
- [Bug Fixing Quickstart](https://github.github.com/spec-kit/guides/bugfix.html)
- [Upgrade Guide](https://github.github.com/spec-kit/upgrade.html)

アプリ自体の起動・設定・検証手順は [統合マニュアル](../manual/README.md) を参照する。
