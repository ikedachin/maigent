# AGENTS.md のサンプル/テンプレート

これは `AGENTS.md` の書き方を示すサンプルです。ファイル名が `AGENTS.sample.md` になっているため、
`.maigent/config.yaml.sample` と同じ考え方で、実際には読み込まれません。

## なぜ読み込まれないのか

Maigentが読み込むのは、各 `.maigent/` レイヤー（`~/.maigent/`、アプリ直下、プロジェクト内）に置かれた
`AGENTS.md` という**完全一致のファイル名**だけです（`load_agents_md()` 参照）。
このファイルは `AGENTS.sample.md` という別名なので、探索対象に含まれません。

実際に有効化したい場合は、このファイルの中身を参考にしながら、同じ `.maigent/` フォルダ内に
`AGENTS.md` という名前で新規作成してください（このファイルをコピーしてリネームしても構いません）。

## 書き方

`AGENTS.md` はプレーンテキスト（Markdown可）で、system instructions の `base_instructions.txt` の直後、
スレッド要約メモリより前にそのまま差し込まれます。JSON構造やfrontmatterは不要です。有効/無効の設定項目もなく、
ファイルの有無だけで反映されます。

以下は記述例です。

```markdown
このプロジェクトでは常に丁寧語で回答してください。
金額は必ず税込表記にしてください。
コードの提案には必ず簡単な説明を添えてください。
```

## レイヤーについて

`~/.maigent/AGENTS.md`（ユーザー共通）、アプリ直下の `.maigent/AGENTS.md`（このリポジトリ全体）、
プロジェクト内の `.maigent/AGENTS.md`（プロジェクト固有）の3層があり、存在するものをすべて連結して使います。
`.maigent/config.yaml` のような「後の層が上書き」ではなく、内容を積み重ねる方式です。
