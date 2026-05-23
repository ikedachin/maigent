# Maigent Agent Web

ローカルで動くDjango製のエージェントチャットWebアプリです。

## Setup

```bash
uv sync
uv run python manage.py migrate
docker build -t maigent-sandbox:py311 docker/sandbox
uv run python manage.py runserver
```

設定は `~/.maigent/config.toml` と、プロジェクト内の `.maigent/config.toml` から読みます。プロジェクト設定がユーザー設定を上書きします。

```toml
model = "gpt-5"
api_key = "sk-..."
base_url = "https://api.openai.com/v1"
api_mode = "auto"
```

`model` または `default_model` は必須です。APIキーはDBへ保存しません。

## Tool configuration

`.maigent/config.yaml` でツールを管理できます。

```yaml
tools:
  rag:
    enabled: true
  web_search:
    enabled: false
  sandbox:
    enabled: false
    image: maigent-sandbox:py311
    timeout_seconds: 20
    install_libraries_on_run: false
    allowed_libraries:
      - beautifulsoup4
      - charset-normalizer
      - lxml
      - matplotlib
      - numpy
      - openpyxl
      - pandas
      - pdfplumber
      - pillow
      - pypdf
      - python-docx
      - python-pptx
      - reportlab
      - requests
      - scipy
      - seaborn
      - tabulate
      - xlsxwriter
      - xlrd
```

`sandbox.enabled` を `true` にすると、計算やPython実行が必要そうなメッセージでDocker sandboxを使います。通常は `install_libraries_on_run: false` のままにして、Dockerイメージ側へ必要なライブラリを事前インストールしてください。`true` にするとsandbox実行ごとに `allowed_libraries` を `pip install` するため、起動が遅くなります。

## Sandbox image

Docker sandboxを使う前に、ローカル用イメージを一度ビルドしてください。

```bash
docker build -t maigent-sandbox:py311 docker/sandbox
```

ビルド後、次のコマンドで確認できます。

```bash
docker run --rm maigent-sandbox:py311 python --version
```

`.maigent/config.yaml` の `tools.sandbox.image` が `maigent-sandbox:py311` を指していれば、このアプリはそのイメージを使います。標準の `docker/sandbox/Dockerfile` には、CSV/Excel処理、PDF/Word/PowerPoint処理、グラフ作成、HTML解析、HTTP取得で使う一般的な事務作業向けライブラリを事前インストールしています。ライブラリを追加する場合はDockerfileにも追記し、もう一度 `docker build -t maigent-sandbox:py311 docker/sandbox` を実行してください。実行時インストールが必要な場合だけ `.maigent/config.yaml` で `install_libraries_on_run: true` にします。
