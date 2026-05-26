# YAML Configuration Documentation

このドキュメントは `.maigent/config.yaml` / `.maigent/config.yaml.sample` の設定項目を説明します。

設定ファイルは `config.toml` でも書けますが、このドキュメントではYAML形式を前提にします。

## 読み込み順

設定は以下の順で読み込まれます。後から読まれたものが前の値を上書きします。

1. `~/.maigent/`
2. アプリ直下の `.maigent/`
3. プロジェクト内の `.maigent/`

読み込み処理は `agent/config.py` の `load_runtime_config()` です。

注意:
- YAMLローダーは簡易実装です
- 基本的なネスト、真偽値、整数、リストに対応します
- 複雑なYAML機能が必要なら `config.toml` を推奨します
- 同じキーを複数回書くと後勝ちになり、前の定義が消える場合があります

## 全体例

```yaml
providers:
  openai:
    enabled: true
    model: gpt-5
    api_key: sk-...
    base_url: https://api.openai.com/v1
    api_mode: auto

llm:
  max_retries: 1

final_evaluation:
  enabled: true
  max_retries: 3
  llm_max_retries: 1
  reasoning_effort: none
  max_output_tokens: 160

tools:
  rag:
    enabled: true
  web_search:
    enabled: false
  sandbox:
    enabled: true
    image: maigent-sandbox:py311
    timeout_seconds: 300
    install_libraries_on_run: false
    allowed_libraries:
      - pandas
      - numpy

tool_selector:
  enabled: true
  reasoning_effort: none
  max_output_tokens: 1024
  max_retries: 1

dynamic_replanner:
  enabled: true
  reasoning_effort: true
  max_output_tokens: 1024
  llm_max_retries: 1

dynamic_finalizer:
  enabled: true
  reasoning_effort: none
  max_output_tokens: 160
  llm_max_retries: 1

sandbox_code_generation:
  llm_max_retries: 1
```

## providers

LLMプロバイダ設定です。

対応プロバイダ:
- `openai`
- `ollama`
- `lmstudio`
- `openrouter`
- `azure`
- `bedrock`

複数が `enabled: true` の場合は以下の順で最初に有効なものが使われます。

```text
openai -> ollama -> lmstudio -> openrouter -> azure -> bedrock
```

### providers.openai

```yaml
providers:
  openai:
    enabled: true
    model: gpt-5
    api_key: sk-...
    base_url: https://api.openai.com/v1
    api_mode: auto
```

項目:
- `enabled`: このプロバイダを使うか
- `model`: 使用モデル
- `api_key`: APIキー。環境変数 `OPENAI_API_KEY` でも可
- `base_url`: OpenAI互換APIのURL。環境変数 `OPENAI_BASE_URL` でも可
- `api_mode`: `auto`、`responses`、`chat`

`api_mode`:
- `auto`: Responses APIを試し、失敗時にChat Completionsへフォールバック
- `responses`: Responses APIを使う
- `chat`: Chat Completions APIを使う

### providers.ollama

```yaml
providers:
  ollama:
    enabled: false
    model: llama3.1
    base_url: http://localhost:11434/v1
    api_mode: chat
```

OllamaのOpenAI互換エンドポイントを使います。APIキーが空でも内部的にダミー値を使います。

### providers.lmstudio

```yaml
providers:
  lmstudio:
    enabled: false
    model: local-model
    base_url: http://localhost:1234/v1
    api_mode: chat
```

LM StudioのOpenAI互換エンドポイントを使います。

### providers.openrouter

```yaml
providers:
  openrouter:
    enabled: false
    model: openai/gpt-4o-mini
    api_key: sk-or-...
    base_url: https://openrouter.ai/api/v1
    api_mode: chat
    http_referer: http://localhost:8000
    x_title: Maigent Agent Web
```

追加ヘッダ:
- `http_referer`: `HTTP-Referer`
- `x_title`: `X-Title`

### providers.azure

```yaml
providers:
  azure:
    enabled: false
    model: azure-deployment-name
    api_key: azure-key
    azure_endpoint: https://your-resource.openai.azure.com
    api_version: 2024-02-15-preview
    api_mode: chat
```

Azure OpenAIを使います。

必須:
- `model`: Azureのdeployment名
- `api_key`
- `azure_endpoint`

### providers.bedrock

```yaml
providers:
  bedrock:
    enabled: false
    model: anthropic.claude-3-5-sonnet-20240620-v1:0
    region: ap-northeast-1
    profile: default
```

AWS Bedrockを使います。

認証:
- `profile` を使う
- または `aws_access_key_id`、`aws_secret_access_key`、`aws_session_token` を設定する
- 環境変数も利用可能

## トップレベル model/api_key/base_url

`providers` を使わない場合、トップレベルの以下をOpenAI互換APIとして扱います。

```yaml
model: gpt-5
api_key: sk-...
base_url: https://api.openai.com/v1
api_mode: auto
```

`providers` がある場合は、基本的に有効なproviderの設定が優先されます。

## llm

内部LLM補助呼び出しの共通設定です。

```yaml
llm:
  max_retries: 1
```

### llm.max_retries

`None`、空文字、例外が返った場合に、同じLLM補助呼び出しを何回リトライするかです。

例:
- `0`: リトライなし。初回のみ
- `1`: 初回 + 1回リトライ
- `2`: 初回 + 2回リトライ

上限は実装上5です。

対象:
- `tool_selector`
- `dynamic_replanner`
- `dynamic_finalizer`
- `final_evaluation`
- `rag_decision`
- `rag_candidate_judge`
- `sandbox_code_generation`

個別セクションの `llm_max_retries` がある場合は、そちらが優先されます。

## final_evaluation

回答をユーザーへ返す前の最終評価設定です。

```yaml
final_evaluation:
  enabled: true
  max_retries: 3
  llm_max_retries: 1
  reasoning_effort: none
  max_output_tokens: 160
```

項目:
- `enabled`: 最終評価を行うか
- `max_retries`: 評価NG時に別プランで再実行する回数
- `llm_max_retries`: 評価LLMが空応答/None/例外を返したときの短いリトライ回数
- `reasoning_effort`: Responses APIに渡すreasoning effort
- `max_output_tokens`: 評価LLMの最大出力トークン数

注意:
- `max_retries` は0から3に丸められます
- UIから保存した有効/無効と `max_retries` はDBの `AppSetting` が優先されます
- `llm_max_retries` は評価NG時の再プラン回数ではありません

## tools

実行ツールの有効/無効と詳細設定です。

### tools.rag

```yaml
tools:
  rag:
    enabled: true
```

許可済みローカルファイル検索を有効化します。

`enabled: true` の場合でも、読み取り許可パスがなければ検索対象はありません。

### tools.web_search

```yaml
tools:
  web_search:
    enabled: false
```

外部Web検索用の設定です。現状の実装では未実装通知を返します。

### tools.sandbox

```yaml
tools:
  sandbox:
    enabled: true
    image: maigent-sandbox:py311
    timeout_seconds: 300
    install_libraries_on_run: false
    allowed_libraries:
      - pandas
      - numpy
```

項目:
- `enabled`: sandbox実行を有効化するか
- `image`: Dockerイメージ名
- `timeout_seconds`: 実行タイムアウト秒。1から600に丸められます
- `install_libraries_on_run`: 実行ごとに `allowed_libraries` をpip installするか
- `allowed_libraries`: 実行時インストールを許可するライブラリ

推奨:
- 通常は `install_libraries_on_run: false`
- 必要ライブラリはDockerイメージに事前インストールする

sandboxの安全性:
- 入力ファイルは直接マウントしません
- 通常はDockerネットワークを無効化します
- ファイル保存は `maigent_artifacts` JSONをstdoutへ出し、ホスト側brokerが権限検証して行います

## tool_selector

LLMに初期ツール列を選ばせる設定です。

```yaml
tool_selector:
  enabled: true
  reasoning_effort: none
  max_output_tokens: 1024
  max_retries: 1
  llm_max_retries: 1
```

項目:
- `enabled`: LLMツール選択を有効化するか
- `reasoning_effort`: reasoning effort
- `max_output_tokens`: 最大出力トークン数
- `max_retries`: 空応答/不正JSON/無効JSON時に選択処理全体を再試行する回数
- `llm_max_retries`: 1回のLLM呼び出しが空応答/None/例外だった場合の短い再試行回数

注意:
- `max_retries` はtool selector専用の既存設定です
- `llm_max_retries` と併用すると試行回数が増えるため、低遅延にしたい場合は片方を小さくしてください

## dynamic_replanner

1タスク実行後の動的リプラン設定です。

```yaml
dynamic_replanner:
  enabled: true
  reasoning_effort: true
  max_output_tokens: 1024
  llm_max_retries: 1
```

項目:
- `enabled`: 動的リプランを有効化するか
- `reasoning_effort`: reasoning effort。`true` は `medium`、`false` は `none` として扱います
- `max_output_tokens`: 最大出力トークン数
- `llm_max_retries`: 空応答/None/例外時の再試行回数

返せるアクション:
- `keep`
- `replace`
- `finish`

## dynamic_finalizer

最終成果物の扱いをLLMで判断する設定です。

```yaml
dynamic_finalizer:
  enabled: true
  reasoning_effort: none
  max_output_tokens: 160
  llm_max_retries: 1
```

項目:
- `enabled`: 最終ルーティングを有効化するか
- `reasoning_effort`: reasoning effort
- `max_output_tokens`: 最大出力トークン数
- `llm_max_retries`: 空応答/None/例外時の再試行回数

返せるアクション:
- `save`: 保存相当で終了
- `discard`: 一時検証として終了
- `add_tasks`: 追加タスクを実行

## sandbox_code_generation

sandboxで使うPythonコードをLLM生成する場合の設定です。

```yaml
sandbox_code_generation:
  llm_max_retries: 1
```

項目:
- `llm_max_retries`: コード生成LLMが空応答/None/例外を返した場合の再試行回数

## rag_decision

必要な場合だけ追加できます。

```yaml
rag_decision:
  llm_max_retries: 1
```

ローカルファイル検索が必要かをLLMで判定する補助呼び出しのリトライ回数です。

## rag_candidate_judge

必要な場合だけ追加できます。

```yaml
rag_candidate_judge:
  llm_max_retries: 1
```

BM25で弱い候補をLLMで関連判定する補助呼び出しのリトライ回数です。

## reasoning_effort

設定可能な値:

- `none`
- `minimal`
- `low`
- `medium`
- `high`
- `true`
- `false`

`true` は `medium` として扱います。`false` は `none` として扱います。

注意:
- Chat Completions APIでは `reasoning_effort` は送信されません
- Responses APIでは `reasoning: {"effort": ...}` として送信されます
- プロバイダや互換APIによって対応状況が異なります

## max_output_tokens

内部判断用LLM呼び出しの最大出力トークン数です。

用途:
- ツール選択や評価などの短いJSON出力を低コスト・低遅延にする
- 長すぎる説明を抑制する

注意:
- 小さすぎるとJSONが途中で切れてパースできない場合があります
- `dynamic_replanner` はタスク列を返すため、`final_evaluation` より大きめが安全です

## 設定変更時の確認コマンド

現在読み込まれている設定を確認する例:

```bash
uv run python manage.py shell -c "from agent.config import load_runtime_config; c=load_runtime_config(''); print(c.active_provider, c.model); print(c.tool_enabled('sandbox')); print(c.control_config('dynamic_finalizer'))"
```

テスト:

```bash
uv run python manage.py test agent
```

