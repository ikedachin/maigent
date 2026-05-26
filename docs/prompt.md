# Prompt Documentation

このドキュメントは `prompt/` 配下の各プロンプトが、エージェント実行のどの段階で何のために使われるかを説明します。

プロンプトは `agent/prompt_loader.py` の `load_prompt()` から読み込まれ、`{{name}}` 形式の変数を置換して使われます。LLMに渡す本文は主に `agent/views.py` と `agent/openai_client.py` から組み立てられます。

## 全体方針

プロンプトは大きく2種類あります。

- `*_instructions.txt`: LLMの役割、出力形式、禁止事項を指定するシステム寄りの指示
- `*_prompt.txt`: 実行時のコンテキストを埋め込むユーザー寄りの入力テンプレート

JSONを期待するプロンプトでは、実装側が `_extract_json_object()` でJSON部分を抜き出して解釈します。空応答、`None`、例外が返った場合は `_complete_response_with_retries()` により設定回数だけ再試行されます。

## base_instructions.txt

通常回答生成に使う基本指示です。

目的:
- このアプリ内のローカルエージェントとして振る舞わせる
- 実際には実行していないシェルコマンドを「実行した」と言わせない
- 実行が必要な場合は承認リクエストを提案させる

主な利用箇所:
- `agent/views.py` の通常回答生成

## initial_clarifier_instructions.txt / initial_clarifier_prompt.txt

初期プラン作成前に、依頼内容だけでは安全に実行計画を決められないかを判定するプロンプトです。

期待出力:

```json
{"needs_clarification": true, "reason": "short reason", "questions": ["question 1", "question 2"]}
```

質問が必要な場合、通常のassistantメッセージとして理由と最大3問の質問を返し、ツール実行や最終評価には進みません。空応答、不正JSON、空の質問配列の場合は既存のプラン作成へフォールバックします。

関連設定:
- `initial_clarifier.enabled`
- `initial_clarifier.max_output_tokens`
- `initial_clarifier.reasoning_effort`
- `initial_clarifier.llm_max_retries`

## tool_selection_instructions.txt / tool_selection_prompt.txt

初期プラン作成前に、LLMへ「どのツールをどの順番で使うか」を選ばせるためのプロンプトです。

入力される情報:
- ユーザーリクエスト
- 現在利用可能なツール一覧

期待出力:

```json
{
  "steps": [
    {"tool": "rag", "purpose": "Find local context."},
    {"tool": "sandbox", "purpose": "Compute exact result."}
  ],
  "rag_query": "optional short query",
  "reason": "short reason"
}
```

利用可能なツール:
- `final`: LLMだけで回答する
- `rag`: 許可済みローカルファイルを検索する
- `sandbox`: Docker内でPythonを実行する
- `web_search`: 外部情報収集用。現状は未実装通知を返す

関連設定:
- `tool_selector.enabled`
- `tool_selector.max_output_tokens`
- `tool_selector.reasoning_effort`
- `tool_selector.max_retries`
- `tool_selector.llm_max_retries`

## rag_decision_instructions.txt / rag_decision_prompt.txt

ルールベースの初期プランで直接回答になった場合に、念のためローカルファイル検索が必要かをLLMに判定させるプロンプトです。

期待出力はJSONではなく、ラベル付きテキストです。

```text
RAG_REQUIRED
QUERY: concise search words
REASON: short reason
```

または:

```text
NO_RAG
REASON: short reason
```

使う場面:
- 固有名詞、私有プロジェクト、ローカル文書、架空設定など、一般知識だけでは答えにくい質問

## rag_candidate_judge_instructions.txt / rag_candidate_judge_prompt.txt

BM25検索で十分なスコアが出なかった場合に、候補ファイルのスニペットをLLMに見せて「回答に役立つファイルか」を判定します。

期待出力:

```json
{"relevant_indexes": [1, 2], "reason": "short reason"}
```

JSONでない場合は以下も許容します。

```text
RELEVANT_INDEXES: 1, 2
REASON: short reason
```

目的:
- キーワード一致だけでは拾いにくい関連ファイルを救う
- 汎用語だけが一致した無関係ファイルを除外する

## sandbox_code_generation_instructions.txt

sandboxで実行するPythonコードをLLMに生成させるための指示です。

使う場面:
- sandboxタスクが選ばれたが、入力から単純な式や既知のCSV処理コードを自動生成できない場合
- `requires_llm_sandbox_program()` が真になるようなグループ集計など

重要な制約:
- Pythonコードのみを返す
- Markdownフェンスや説明文を返さない
- ローカルファイルを直接読まない
- ネットワークを使わない
- ファイルを書き込まない

ファイル保存が必要な場合:

```json
{"maigent_artifacts":[{"path":"/absolute/requested/path.ext","content":"file contents","append":false}]}
```

このJSONをstdoutに出すと、Django側のbrokerが書き込み権限を検証してホスト側で保存します。

関連設定:
- `sandbox_code_generation.llm_max_retries`
- `tools.sandbox.*`

## evaluation_criteria.txt

初期プラン作成時に `evaluation_criteria` へ入れる評価基準文のテンプレートです。

コード側は依頼内容から `base` / `rag` / `sandbox` / `summary` / `list` / `rag_selected` のどのセクションを使うかだけを判断し、実際の文面はこのファイルから読み込みます。

例:

```text
[base]
- The answer directly addresses the user's request.

[sandbox]
- If exact computation or code execution is requested, the answer includes the computed result and does not rely on unsupported mental arithmetic.
```

## dynamic_replanner_instructions.txt / dynamic_replanner_prompt.txt

1タスク実行後に、残りのプランキューをどうするか判断するプロンプトです。

入力される情報:
- 最終ゴール
- 評価基準
- 過去のプラン履歴
- タスク実行履歴
- 現在の残りキュー

期待出力:

```json
{
  "action": "keep",
  "reason": "current queue remains valid"
}
```

または:

```json
{
  "action": "replace",
  "reason": "Need debugging first.",
  "tasks": [{"tool": "sandbox", "purpose": "Run a narrower check."}]
}
```

または:

```json
{
  "action": "finish",
  "reason": "Goal is satisfied.",
  "final_message": "optional message"
}
```

アクション:
- `keep`: 現在のキューを維持
- `replace`: 残りキューを置換
- `finish`: 実行ループを終了

関連設定:
- `dynamic_replanner.enabled`
- `dynamic_replanner.max_output_tokens`
- `dynamic_replanner.reasoning_effort`
- `dynamic_replanner.llm_max_retries`

## dynamic_finalizer_instructions.txt / dynamic_finalizer_prompt.txt

全タスク終了後に、成果物や最終メッセージをどう扱うか判断するプロンプトです。

入力される情報:
- 最終ゴール
- タスク実行履歴
- 現在の成果物または最終メッセージ

期待出力:

```json
{"action": "save", "reason": "Result should be preserved."}
```

```json
{"action": "discard", "reason": "Temporary verification only."}
```

```json
{
  "action": "add_tasks",
  "reason": "Need one more validation.",
  "tasks": [{"tool": "sandbox", "purpose": "Validate the artifact."}]
}
```

アクション:
- `save`: 成果物を保存相当として扱って終了
- `discard`: 一時検証として終了
- `add_tasks`: 追加タスクをキューへ入れて再実行

関連設定:
- `dynamic_finalizer.enabled`
- `dynamic_finalizer.max_output_tokens`
- `dynamic_finalizer.reasoning_effort`
- `dynamic_finalizer.llm_max_retries`

## final_evaluation_instructions.txt / final_evaluation_prompt.txt

回答をユーザーへ返す前に、事前に決めたゴールと評価基準を満たしているか判定するプロンプトです。

入力される情報:
- ユーザー質問
- プラン作成時のゴール
- プラン作成時の評価基準
- 候補回答

期待出力:

```json
{"adequate": true, "reason": "answers the question"}
```

または:

```json
{"adequate": false, "reason": "too vague"}
```

JSONでない場合は `ADEQUATE` / `INADEQUATE` と `REASON` ラベルも許容します。

注意:
- `final_evaluation.max_retries` は「評価NG時に別プランでやり直す回数」
- `final_evaluation.llm_max_retries` は「評価LLMが空応答/None/例外を返したときの短い再試行回数」

## retry_feedback_prefix.txt

最終評価で失敗した理由を、次の回答生成プロンプトの先頭に付けるためのテンプレートです。

目的:
- 前回の失敗理由を次の回答生成に反映する
- 同じ失敗を繰り返さないようにする

使う場面:
- `final_evaluation.enabled` が有効
- 候補回答が不十分と判定された
- 再プラン/再回答を行う

## プロンプトを追加・変更するときの注意

1. 新しいプロンプトファイルを `prompt/` に追加する
2. `load_prompt("file_name.txt", key=value)` で読み込む
3. 期待出力形式を明確にする
4. JSONを期待する場合は失敗時のフォールバックを実装する
5. 空応答や例外に備えて `_complete_response_with_retries()` を使う
6. テストで正常系、空応答、パース失敗を確認する
