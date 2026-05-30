# Functions Documentation

このドキュメントは、Maigent Agent Webが持つ主要機能と、新しい機能を追加するときの実装手順を説明します。

## アーキテクチャ概要

主要モジュール:

- `agent/views.py`: Webリクエスト、SSEストリーミング、エージェント実行ループ、動的リプラン
- `agent/tooling.py`: ルールベースのプラン作成、ツール選択、sandbox用Python生成ロジック
- `agent/openai_client.py`: OpenAI互換API、Azure、BedrockへのLLM呼び出し
- `agent/config.py`: `config.yaml` / `config.toml` の読み込みと設定解決
- `agent/models.py`: Django DBモデル
- `agent/slash_commands.py`: `/read`、`/write` などのスラッシュコマンド
- `agent/file_broker.py`: sandboxやコマンドからの安全なホストファイル書き込み
- `prompt/`: LLMに渡すプロンプトテンプレート

## エージェント実行フロー

1. ユーザーがメッセージを送信する
2. `load_runtime_config()` で設定を読み込む
3. 完了済み会話履歴を時系列でまとめ、最新ユーザーメッセージを明示したLLM入力を作る
4. `initial_clarifier` が有効なら会話履歴込みの入力で不足情報の有無を判定させ、必要なら質問を返して停止する
5. `tool_selector` が有効なら最新ユーザーメッセージを基準に初期ツール列を選ばせる
6. 失敗した場合は `build_agent_plan()` でルールベースプランを作る
7. `AgentState.plan_queue` にタスクを入れる
8. 先頭タスクを1つ実行する
9. タスク結果を `AgentTaskRecord` に保存する
10. `dynamic_replanner` が有効なら残りキューを維持/置換/終了する
11. キューが空になったら `dynamic_finalizer` が成果物の扱いを判断する
12. 必要なら追加タスクを実行する
13. 回答候補を生成する
14. `final_evaluation` が有効なら会話履歴込みで回答を評価し、不十分なら再プランする
15. `Message` と `AgentRun` に結果を保存する

## 状態管理

### AgentState

`agent/views.py` の `AgentState` は1回のエージェント実行状態を保持します。

主なフィールド:

- `goal`: 最終ゴール
- `evaluation_criteria`: 最終評価基準
- `input_text`: 現在のLLM入力。初期値はスレッド内の会話履歴と最新ユーザーメッセージ
- `plan_queue`: これから実行するタスクキュー
- `plan_history`: 初期プランやリプランの履歴
- `task_history`: 実行済みタスクの結果履歴
- `final_message`: タスクが直接生成した最終メッセージ
- `stopped`: 実行ループ停止フラグ
- `run`: DB上の `AgentRun`

### DB永続化

`agent/models.py` の以下のモデルが実行履歴を保存します。

- `AgentRun`: エージェント実行全体
- `AgentTaskRecord`: 各タスクの実行結果
- `Message`: ユーザー/アシスタントメッセージ
- `ProjectAccessPath`: 読み書き許可パス
- `FeatureFlag`: 機能フラグ
- `AppSetting`: UIから保存する設定

## ツール

### final

LLMだけで回答します。明示的な外部ツール処理は行いません。

使われる場面:
- 雑談
- 一般知識
- ファイルや計算が不要な質問

### rag

許可済みローカルファイルを検索し、関連コンテキストを回答入力に追加します。

主な処理:
- `ProjectAccessPath` で許可されたパスだけを読む
- テキストファイルやディレクトリ一覧を候補化する
- BM25風スコアでランキングする
- スコアが弱い場合は `rag_candidate_judge` でLLM判定する

関連関数:
- `_build_rag_input()`
- `_collect_allowed_path_context()`
- `_collect_relevant_allowed_files()`
- `_judge_rag_candidate_paths_with_llm()`

### sandbox

Dockerコンテナ内でPythonコードを実行します。

主な処理:
- 入力からPythonコード、式、CSV集計コードを抽出/生成
- 一時ディレクトリに `script.py` を作成
- Dockerで `python /work/script.py` を実行
- 標準出力を結果として返す

関連関数:
- `build_sandbox_program()`
- `run_sandbox()`
- `_generate_sandbox_code_with_retries()`
- `generate_sandbox_code()`

安全設計:
- sandboxは許可フォルダを直接マウントしない
- 通常はネットワークを無効化する
- ファイル保存はstdoutの typed `maigent_sandbox_result` JSONをホスト側brokerが検証して行う
- RAGで選ばれたCSV/TSV/JSON/TXT/Markdownは、許可パス検証後に `SandboxDataset` としてホスト側で読み、`load_dataset("rag_1")` などの固定APIをsandboxコードへ注入する
- LLM生成コードがCSV/TSV行をPython文字列へ再転記した場合は、ポリシー違反として再生成する

### web_search

現在は計画に選択されても未実装通知を返します。

今後追加する場合は、新しい外部検索処理を `_execute_agent_task()` の `web_search` 分岐に実装します。

## 動的リプラン

### dynamic_replanner

1タスク実行後に残りキューを更新します。

返せるアクション:
- `keep`: 現在のキューを維持
- `replace`: キューを置換
- `finish`: 実行を終了

関連関数:
- `_replan_after_step()`
- `_replan_after_step_with_llm()`

### dynamic_finalizer

全タスク終了後に成果物の扱いを判断します。

返せるアクション:
- `save`: 終了
- `discard`: 終了
- `add_tasks`: 追加検証や追加処理を実行

関連関数:
- `_route_final_output()`

## 最終評価

`final_evaluation` が有効な場合、回答を返す前にLLMで十分性を評価します。

評価に失敗した場合:
- `final_evaluation.max_retries` の範囲で別プランを試す
- 失敗理由を `retry_feedback_prefix.txt` で次の回答生成に渡す
- 全て失敗した場合は回答末尾に警告を付ける

関連関数:
- `_generate_with_final_evaluation()`
- `_evaluate_final_answer()`
- `_avoid_failed_plan()`

## LLM補助呼び出しのリトライ

内部判断用LLM呼び出しは `_complete_response_with_retries()` を使います。

リトライ対象:
- `None`
- 空文字
- 例外

設定:
- 共通: `llm.max_retries`
- 個別: `<section>.llm_max_retries`

対象例:
- `final_evaluation`
- `tool_selector`
- `dynamic_replanner`
- `dynamic_finalizer`
- `rag_decision`
- `rag_candidate_judge`
- `sandbox_code_generation`

## スラッシュコマンド

`agent/slash_commands.py` が処理します。

主なコマンド:
- `/status`: 現在のプロジェクト、モデル、設定ソース、機能フラグを表示
- `/model`: 現在のモデルを表示
- `/read <path>`: 許可済みファイルを読む
- `/ls <path>`: 許可済みフォルダを一覧表示
- `/file [path]`: 許可済みファイル/フォルダを表示、または指定パスを読む
- `/write <path> -- <content>`: 許可済みパスへ書き込み
- `/append <path> -- <content>`: 許可済みパスへ追記
- `/features list|enable|disable`: 機能フラグ操作
- `/compact`: スレッド要約を更新
- `/resume`: 再開可能なスレッドを表示
- `/fork`: スレッドを複製
- `/memories`: メモリ有効/無効を切り替え

## ファイルアクセスと保存

読み取りは `ProjectAccessPath` に登録されたパスに制限されます。書き込みはプロジェクトの書き出し先フォルダ配下に制限されます。

書き込みの追加条件:
- `file_write` feature flagが有効
- 書き込み先がプロジェクトの書き出し先フォルダ配下
- 親フォルダが存在する
- 書き込み内容が `MAX_BROKER_WRITE_CHARS` 以下
- 画像成果物は `content_base64` で保存し、PNG/JPEG/WebP/GIF のみチャット内表示リンクを生成する

関連関数:
- `is_path_allowed()`
- `write_allowed_text_file()`
- `write_allowed_binary_file()`
- `serve_artifact_image()`
- `_persist_sandbox_artifacts()`

## 新しいツールを追加する方法

例として `database` ツールを追加する場合の手順です。

1. `agent/tooling.py` の `AgentPlanStep.tool` に使う名前を決める
2. `agent/views.py` の `_available_tool_specs()` にツール説明を追加する
3. `agent/views.py` の `_parse_plan_tasks()` の許可ツール集合に名前を追加する
4. `agent/views.py` の `_execute_agent_task()` に `if step.tool == "database":` 分岐を追加する
5. 必要なら `config.yaml` の `tools.database.enabled` を読む設定を追加する
6. `prompt/tool_selection_instructions.txt` の説明が不足する場合は更新する
7. テストを追加する

最低限のテスト:
- 設定が無効なら選択されない
- `tool_selector` が返したJSONから計画に入る
- `_execute_agent_task()` が期待結果を返す
- 失敗時に `dynamic_replanner` が扱える

## 新しいLLM補助判断を追加する方法

1. `prompt/<name>_instructions.txt` を追加する
2. `prompt/<name>_prompt.txt` を追加する
3. `agent/views.py` で `load_prompt()` して入力を組み立てる
4. `_complete_response_with_retries(config, ..., config_name="<name>")` を使う
5. `config.yaml` に必要なら `<name>.llm_max_retries`、`max_output_tokens`、`reasoning_effort` を追加する
6. JSONパース失敗時のフォールバックを実装する
7. 空応答、例外、不正JSONのテストを追加する

## 新しいDB永続化項目を追加する方法

1. `agent/models.py` にフィールドまたはモデルを追加する
2. `uv run python manage.py makemigrations agent` でmigrationを作る
3. `uv run python manage.py migrate` で適用する
4. 書き込み箇所を `agent/views.py` などに追加する
5. テストで保存内容を確認する

## 新しい設定を追加する方法

1. `agent/config.py` にプロパティまたは `control_config()` の読み取りを追加する
2. `.maigent/config.yaml.sample` に例を追加する
3. `docs/yaml.md` を更新する
4. 設定読み込みテストを追加する
