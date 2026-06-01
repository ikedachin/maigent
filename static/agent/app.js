function csrfToken(form) {
  const input = form.querySelector("input[name=csrfmiddlewaretoken]");
  return input ? input.value : "";
}

const translations = {
  ja: {
    brandSubtitle: "エージェントワークスペース",
    projects: "プロジェクト",
    metadataOnly: "メタデータのみ",
    projectNamePlaceholder: "プロジェクト名",
    browse: "選択",
    addProject: "プロジェクトを追加",
    threads: "スレッド",
    memoryOn: "メモリ有効",
    memoryOff: "メモリ無効",
    threadMemory: "スレッドメモリ",
    noThreadMemory: "まだ要約メモリはありません。",
    threadTitlePlaceholder: "スレッド名",
    newThread: "新規スレッド",
    deleteThread: "スレッドを削除",
    deleteThreadConfirm: "このスレッドを削除しますか？この操作は元に戻せません。",
    modelLabel: "モデル",
    messages: (count) => `${count}件のメッセージ`,
    emptyState: "メッセージまたはスラッシュコマンドで開始してください。",
    composerPlaceholder: "/status またはメッセージを入力",
    send: "送信",
    darkMode: "ダーク",
    lightMode: "ライト",
    toggleTheme: "テーマを切替",
    config: "設定",
    sources: "読み込み元",
    state: "状態",
    noConfig: "設定なし",
    ragTopK: "RAG top_k",
    finalEvaluation: "最終評価",
    finalEvaluationRetries: "最終評価リトライ",
    saveSettings: "設定を保存",
    tools: "ツール",
    accessPaths: "アクセス許可",
    accessPathPlaceholder: "/path/to/file-or-folder",
    outputFolder: "書き出し先",
    outputPathPlaceholder: "/path/to/output-folder",
    saveOutputFolder: "書き出し先を保存",
    outputFolderHelp: "ファイル書き出しはこのフォルダ配下に限定されます。",
    noOutputFolder: "書き出し先フォルダは未設定です。",
    readOnly: "読み取りのみ",
    readWrite: "読み書き",
    notePlaceholder: "メモ",
    addAccessPath: "許可パスを追加",
    deleteAccessPath: "許可パスを削除",
    deleteAccessPathConfirm: "この許可パスを削除しますか？",
    noAccessPaths: "許可パスは未設定です。",
    featureFlags: "機能フラグ",
    none: "なし",
    approvals: "承認",
    commandPlaceholder: "コマンド提案",
    rationalePlaceholder: "理由",
    requestApproval: "承認を依頼",
    approve: "承認",
    reject: "却下",
    noApprovals: "承認リクエストはありません。",
    automations: "オートメーション",
    noAutomations: "オートメーションは未設定です。",
    slashCommands: "スラッシュコマンド例",
    cmdStatus: "現在のプロジェクト、モデル、機能フラグを表示",
    cmdModel: "使用中のモデル設定を表示",
    cmdResume: "再開可能なスレッドを表示",
    cmdCompact: "このスレッドの要約メモリを更新",
    cmdFork: "現在の会話を新しいスレッドへ分岐",
    cmdFeaturesList: "機能フラグ一覧を表示",
    cmdFeaturesEnable: "機能フラグを有効化",
    cmdFeaturesDisable: "機能フラグを無効化",
    cmdMemories: "スレッドメモリのオン/オフを切替",
    cmdFile: "許可済みファイル/フォルダを表示",
    cmdFilePath: "ファイルなら読み取り、フォルダなら一覧表示",
    cmdRead: "許可済みファイルを読み取る",
    cmdLs: "許可済みフォルダの一覧を表示",
    cmdWrite: "書き出し先フォルダ配下のファイルへ書き込む",
    cmdAppend: "書き出し先フォルダ配下のファイルへ追記する",
    cmdExperimental: "試験的機能の状態を表示",
    cmdAgent: "エージェント設定の状態を表示",
    cmdTheme: "テーマ設定の状態を表示",
    cmdApps: "アプリ連携の状態を表示",
    selectRepository: "リポジトリを選択",
    selectRepositoryHelp: "フォルダをクリックして移動し、現在のフォルダを入力欄に設定できます。",
    currentFolder: "現在のフォルダ",
    parentFolder: "上へ",
    useThisFolder: "このフォルダを使う",
    noFolders: "表示できるフォルダがありません。",
    repo: "Git",
    directoryLoadError: "フォルダを読み込めませんでした。",
    complete: "完了",
    streaming: "生成中",
    running: "実行中",
    queued: "待機中",
    preparing: "準備中",
    partialProgress: "進行ログ（一部・最新3行）",
    error: "エラー",
    pending: "待機中",
    elapsedTime: "実行時間",
    agentProgress: "エージェント進捗",
    copy: "コピー",
    copied: "コピー済み",
    copyMessage: "メッセージをコピー",
  },
  en: {
    brandSubtitle: "Agent workspace",
    projects: "Projects",
    metadataOnly: "Metadata only",
    projectNamePlaceholder: "Project name",
    browse: "Browse",
    addProject: "Add project",
    threads: "Threads",
    memoryOn: "Memory on",
    memoryOff: "Memory off",
    threadMemory: "Thread memory",
    noThreadMemory: "No summary memory yet.",
    threadTitlePlaceholder: "Thread title",
    newThread: "New thread",
    deleteThread: "Delete thread",
    deleteThreadConfirm: "Delete this thread? This cannot be undone.",
    modelLabel: "Model",
    messages: (count) => `${count} message${count === 1 ? "" : "s"}`,
    emptyState: "Start with a message or slash command.",
    composerPlaceholder: "Type a message or /status",
    send: "Send",
    darkMode: "Dark",
    lightMode: "Light",
    toggleTheme: "Toggle theme",
    config: "Config",
    sources: "Sources",
    state: "State",
    noConfig: "No config loaded",
    ragTopK: "RAG top_k",
    finalEvaluation: "Final evaluation",
    finalEvaluationRetries: "Final evaluation retries",
    saveSettings: "Save settings",
    tools: "Tools",
    accessPaths: "Access paths",
    accessPathPlaceholder: "/path/to/file-or-folder",
    outputFolder: "Output folder",
    outputPathPlaceholder: "/path/to/output-folder",
    saveOutputFolder: "Save output folder",
    outputFolderHelp: "File exports are limited to this folder.",
    noOutputFolder: "No output folder configured.",
    readOnly: "Read only",
    readWrite: "Read and write",
    notePlaceholder: "Note",
    addAccessPath: "Add access path",
    deleteAccessPath: "Delete access path",
    deleteAccessPathConfirm: "Delete this access path?",
    noAccessPaths: "No access paths configured.",
    featureFlags: "Feature flags",
    none: "None",
    approvals: "Approvals",
    commandPlaceholder: "Command proposal",
    rationalePlaceholder: "Rationale",
    requestApproval: "Request approval",
    approve: "Approve",
    reject: "Reject",
    noApprovals: "No approval requests.",
    automations: "Automations",
    noAutomations: "No automations configured.",
    slashCommands: "Slash command examples",
    cmdStatus: "Show project, model, and feature flags",
    cmdModel: "Show the active model setting",
    cmdResume: "Show resumable threads",
    cmdCompact: "Update this thread's summary memory",
    cmdFork: "Fork the current conversation into a new thread",
    cmdFeaturesList: "List feature flags",
    cmdFeaturesEnable: "Enable a feature flag",
    cmdFeaturesDisable: "Disable a feature flag",
    cmdMemories: "Toggle thread memory on or off",
    cmdFile: "Show allowed files and folders",
    cmdFilePath: "Read a file or list a folder",
    cmdRead: "Read an allowed file",
    cmdLs: "List an allowed folder",
    cmdWrite: "Write a file under the output folder",
    cmdAppend: "Append to a file under the output folder",
    cmdExperimental: "Show experimental feature status",
    cmdAgent: "Show agent settings status",
    cmdTheme: "Show theme settings status",
    cmdApps: "Show app integration status",
    selectRepository: "Select repository",
    selectRepositoryHelp: "Click folders to navigate, then set the current folder in the input.",
    currentFolder: "Current folder",
    parentFolder: "Up",
    useThisFolder: "Use this folder",
    noFolders: "No folders available.",
    repo: "Git",
    directoryLoadError: "Could not load folders.",
    complete: "complete",
    streaming: "streaming",
    running: "running",
    queued: "queued",
    preparing: "preparing",
    partialProgress: "Progress log (partial, latest 3 lines)",
    error: "error",
    pending: "pending",
    elapsedTime: "Elapsed time",
    agentProgress: "Agent progress",
    copy: "Copy",
    copied: "Copied",
    copyMessage: "Copy message",
  },
};

function activeLanguage() {
  return localStorage.getItem("maigent.language") || "ja";
}

function activeTheme() {
  const saved = localStorage.getItem("maigent.theme");
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function t(key, lang = activeLanguage(), count = null) {
  const value = translations[lang]?.[key] || translations.ja[key] || key;
  return typeof value === "function" ? value(Number(count || 0)) : value;
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("maigent.theme", theme);
  const toggle = document.querySelector("[data-theme-toggle]");
  const label = document.querySelector("[data-theme-label]");
  const nextLabel = theme === "dark" ? "lightMode" : "darkMode";
  if (label) {
    label.dataset.i18n = nextLabel;
    label.textContent = t(nextLabel);
  }
  if (toggle) {
    toggle.dataset.activeTheme = theme;
    toggle.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
    toggle.title = t("toggleTheme");
    toggle.setAttribute("aria-label", t("toggleTheme"));
  }
}

function applyLanguage(lang) {
  document.documentElement.lang = lang;
  localStorage.setItem("maigent.language", lang);
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    node.textContent = t(node.dataset.i18n, lang);
  });
  document.querySelectorAll("option[data-i18n]").forEach((node) => {
    node.textContent = t(node.dataset.i18n, lang);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => {
    node.placeholder = t(node.dataset.i18nPlaceholder, lang);
  });
  document.querySelectorAll("[data-i18n-title]").forEach((node) => {
    node.title = t(node.dataset.i18nTitle, lang);
    node.setAttribute("aria-label", t(node.dataset.i18nTitle, lang));
  });
  document.querySelectorAll("[data-i18n-count]").forEach((node) => {
    node.textContent = t(node.dataset.i18nCount, lang, node.dataset.count);
  });
  document.querySelectorAll("[data-status]").forEach((node) => {
    node.textContent = t(node.dataset.status, lang);
  });
  document.querySelectorAll("[data-activity-text]").forEach((node) => {
    node.textContent = t(node.dataset.activityText || "running", lang);
  });
  document.querySelectorAll(".lang-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.lang === lang);
  });
  applyTheme(activeTheme());
}

function isSafeMarkdownImageUrl(url) {
  if (/^https?:\/\//i.test(url)) return true;
  if (/^\/(?!\/)/.test(url)) return true;
  return /^data:image\/(?:png|jpe?g|webp|gif);base64,[a-z0-9+/=\s]+$/i.test(url);
}

function artifactPayloadToImageMarkdown(rawJson) {
  let payload;
  try {
    payload = JSON.parse(rawJson);
  } catch (error) {
    return "";
  }
  const typedResult = payload && typeof payload.maigent_sandbox_result === "object" ? payload.maigent_sandbox_result : null;
  const artifacts = Array.isArray(typedResult?.artifacts)
    ? typedResult.artifacts
    : Array.isArray(payload.maigent_artifacts)
      ? payload.maigent_artifacts
      : [];
  return artifacts
    .map((artifact) => {
      const mimeType = String(artifact?.mime_type || "").toLowerCase();
      const content = String(artifact?.content_base64 || "").replace(/\s+/g, "");
      if (!/^image\/(?:png|jpe?g|webp|gif)$/.test(mimeType) || !/^[a-z0-9+/=]+$/i.test(content)) return "";
      const name = String(artifact?.path || "image").split(/[\\/]/).pop() || "image";
      return `![${name}](data:${mimeType};base64,${content})`;
    })
    .filter(Boolean)
    .join("\n");
}

function findJsonObjectEnd(text, start) {
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let index = start; index < text.length; index += 1) {
    const char = text[index];
    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (char === "\\") {
        escaped = true;
      } else if (char === "\"") {
        inString = false;
      }
      continue;
    }
    if (char === "\"") {
      inString = true;
    } else if (char === "{") {
      depth += 1;
    } else if (char === "}") {
      depth -= 1;
      if (depth === 0) return index + 1;
    }
  }
  return -1;
}

function replaceMarkedArtifactJsonWithImages(content) {
  const marker = "<MAIGENT_ARTIFACT>";
  let text = content || "";
  let searchFrom = 0;
  while (searchFrom < text.length) {
    const markerIndex = text.indexOf(marker, searchFrom);
    if (markerIndex < 0) break;
    const objectStart = text.indexOf("{", markerIndex + marker.length);
    if (objectStart < 0) break;
    const objectEnd = findJsonObjectEnd(text, objectStart);
    if (objectEnd < 0) break;
    const markdown = artifactPayloadToImageMarkdown(text.slice(objectStart, objectEnd));
    if (!markdown) {
      searchFrom = objectEnd;
      continue;
    }
    text = `${text.slice(0, markerIndex)}${markdown}${text.slice(objectEnd)}`;
    searchFrom = markerIndex + markdown.length;
  }
  return text;
}

function replaceArtifactJsonWithImages(content) {
  let text = replaceMarkedArtifactJsonWithImages(content || "");
  text = text.replace(/```json\s*([\s\S]*?)```/gi, (syntax, rawJson) => {
    const markdown = artifactPayloadToImageMarkdown(rawJson.trim());
    return markdown || syntax;
  });
  return text
    .split("\n")
    .map((line) => {
      const trimmed = line.trim();
      if (!trimmed.includes("maigent_artifacts") && !trimmed.includes("maigent_sandbox_result")) return line;
      return artifactPayloadToImageMarkdown(trimmed) || line;
    })
    .join("\n");
}

function renderMessageContent(node, content) {
  node.textContent = "";
  node.dataset.rawContent = content || "";
  const text = replaceArtifactJsonWithImages(content || "");
  const imagePattern = /!\[([^\]\n]*)\]\(([^)\s]+)\)/g;
  let cursor = 0;
  let match;
  while ((match = imagePattern.exec(text)) !== null) {
    const [syntax, alt, url] = match;
    if (!isSafeMarkdownImageUrl(url)) continue;
    if (match.index > cursor) {
      node.append(document.createTextNode(text.slice(cursor, match.index)));
    }
    const image = document.createElement("img");
    image.className = "message-image";
    image.src = url;
    image.alt = alt || "";
    image.loading = "lazy";
    image.decoding = "async";
    node.append(image);
    cursor = match.index + syntax.length;
  }
  if (cursor < text.length) {
    node.append(document.createTextNode(text.slice(cursor)));
  }
}

function renderInitialMessages() {
  document.querySelectorAll(".message").forEach((message) => {
    const body = message.querySelector("[data-message-body]");
    const contentNode = message.querySelector("[data-message-content]");
    if (!body || !contentNode) return;
    let content = "";
    try {
      content = JSON.parse(contentNode.textContent || "\"\"");
    } catch (error) {
      content = contentNode.textContent || "";
    }
    renderMessageContent(body, content);
    contentNode.remove();
  });
}

function renderLoadingMessage(node) {
  node.textContent = "";
  node.className = "message-body message-loading";
  const label = document.createElement("span");
  label.textContent = t("running");
  const dots = document.createElement("span");
  dots.className = "typing-dots";
  dots.setAttribute("aria-hidden", "true");
  dots.append(document.createElement("i"), document.createElement("i"), document.createElement("i"));
  node.append(label, dots);
}

function appendMessage(role, content, status) {
  const container = document.querySelector("#messages");
  const empty = container.querySelector(".empty-state");
  if (empty) empty.remove();

  const article = document.createElement("article");
  article.className = `message ${role} ${status || "complete"}`;
  const meta = document.createElement("div");
  meta.className = "message-meta";
  const state = status || "complete";
  const roleNode = document.createElement("span");
  roleNode.textContent = role;
  const actions = document.createElement("div");
  actions.className = "message-actions";
  const statusNode = document.createElement("small");
  statusNode.dataset.status = state;
  statusNode.textContent = t(state);
  const copyButton = document.createElement("button");
  copyButton.type = "button";
  copyButton.className = "copy-message-button";
  copyButton.dataset.copyMessage = "";
  copyButton.dataset.i18n = "copy";
  copyButton.dataset.i18nTitle = "copyMessage";
  copyButton.title = t("copyMessage");
  copyButton.setAttribute("aria-label", t("copyMessage"));
  copyButton.textContent = t("copy");
  actions.append(statusNode, copyButton);
  const body = document.createElement("div");
  body.className = "message-body";
  body.dataset.messageBody = "";
  renderMessageContent(body, content || "");
  if (role === "assistant" && state === "streaming" && !content) {
    renderLoadingMessage(body);
  }
  meta.append(roleNode, actions);
  article.append(meta, body);
  let progress = null;
  if (role === "assistant") {
    progress = document.createElement("div");
    progress.className = "message-progress";
    progress.hidden = true;
    const label = document.createElement("div");
    label.className = "message-progress-label";
    label.dataset.i18n = "partialProgress";
    label.textContent = t("partialProgress");
    const list = document.createElement("ol");
    list.className = "message-progress-lines";
    const agents = document.createElement("ul");
    agents.className = "message-agent-progress";
    agents.hidden = true;
    progress.append(label, agents, list);
    article.append(progress);
  }
  container.append(article);
  scrollMessagesToBottom();
  return { article, body, meta, progress, content: content || "" };
}

function updateThreadSummary(summary) {
  if (summary === undefined || summary === null) return;
  const node = document.querySelector("[data-thread-summary]");
  if (!node) return;
  const text = String(summary || "").trim();
  if (text) {
    node.textContent = text;
    node.className = "memory-summary";
    node.removeAttribute("data-i18n");
  } else {
    node.textContent = t("noThreadMemory");
    node.className = "muted";
    node.dataset.i18n = "noThreadMemory";
  }
}

function formatElapsed(ms) {
  const value = Number(ms);
  if (!Number.isFinite(value) || value < 0) return "";
  if (value < 1000) return `${Math.max(1, Math.round(value))}ms`;
  if (value < 10000) return `${(value / 1000).toFixed(1)}s`;
  return `${Math.round(value / 1000)}s`;
}

function setElapsedTime(node, elapsedMs) {
  const label = formatElapsed(elapsedMs);
  if (!label) return;
  const status = node.meta.querySelector("[data-status]");
  if (!status) return;
  const statusKey = status.dataset.status || "complete";
  status.textContent = `${t(statusKey)} · ${label}`;
  status.title = t("elapsedTime");
}

function updateLiveElapsedTime(node, startedAt) {
  setElapsedTime(node, performance.now() - startedAt);
}

function setActivity(active, key = "running") {
  const indicator = document.querySelector("[data-activity-indicator]");
  const text = indicator?.querySelector("[data-activity-text]");
  const form = document.querySelector("#chat-form");
  const button = form?.querySelector("button[type=submit]");
  if (indicator) {
    indicator.hidden = !active;
    indicator.classList.toggle("active", active);
  }
  if (text) {
    text.dataset.activityText = key;
    text.textContent = t(key);
  }
  if (button) {
    button.disabled = active;
    button.classList.toggle("busy", active);
    button.textContent = active ? t("running") : t("send");
  }
}

function scrollMessagesToBottom(behavior = "smooth") {
  const container = document.querySelector("#messages");
  if (!container) return;
  container.scrollTo({ top: container.scrollHeight, behavior });
}

function streamAssistant(url, node) {
  const source = new EventSource(url);
  const startedAt = performance.now();
  updateLiveElapsedTime(node, startedAt);
  const elapsedTimer = window.setInterval(() => {
    updateLiveElapsedTime(node, startedAt);
  }, 250);

  function stopElapsedTimer() {
    window.clearInterval(elapsedTimer);
  }

  source.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.progress_tail) {
      updateProgressTail(node, payload.progress_tail, payload.progress_truncated);
    }
    if (payload.agent && payload.agent_status) {
      updateAgentProgress(node, payload.agent, payload.agent_status, payload.agent_progress || "");
    }
    if (payload.delta) {
      if (node.body.classList.contains("message-loading")) {
        node.body.className = "message-body";
        node.content = "";
      }
      node.content += payload.delta;
      renderMessageContent(node.body, node.content);
    }
    if (payload.error) {
      node.article.classList.add("error");
      node.body.className = "message-body";
      node.content = payload.error;
      renderMessageContent(node.body, node.content);
      const status = node.meta.querySelector("[data-status]");
      if (status) {
        status.dataset.status = "error";
        status.textContent = t("error");
      }
      setElapsedTime(node, payload.elapsed_ms);
      stopElapsedTimer();
      setActivity(false);
      source.close();
    }
    if (payload.done) {
      const status = node.meta.querySelector("[data-status]");
      if (status) {
        status.dataset.status = "complete";
        status.textContent = t("complete");
      }
      setElapsedTime(node, payload.elapsed_ms);
      node.article.classList.remove("streaming");
      stopElapsedTimer();
      setActivity(false);
      source.close();
    }
    const container = document.querySelector("#messages");
    if (container) {
      container.scrollTo({ top: container.scrollHeight, behavior: "smooth" });
    }
  };
  source.onerror = () => {
    node.article.classList.add("error");
    node.body.className = "message-body";
    const status = node.meta.querySelector("[data-status]");
    if (status) {
      status.dataset.status = "error";
      status.textContent = t("error");
    }
    setElapsedTime(node, performance.now() - startedAt);
    if (!node.content.trim()) {
      node.content = t("error");
      renderMessageContent(node.body, node.content);
    }
    stopElapsedTimer();
    setActivity(false);
    source.close();
  };
}

function updateProgressTail(node, lines, truncated) {
  if (!node.progress) return;
  const list = node.progress.querySelector(".message-progress-lines");
  if (!list) return;
  list.innerHTML = "";
  lines.forEach((line) => {
    const item = document.createElement("li");
    item.textContent = line;
    list.append(item);
  });
  node.progress.classList.toggle("truncated", Boolean(truncated));
  node.progress.hidden = lines.length === 0;
}

function updateAgentProgress(node, agent, status, message) {
  if (!node.progress) return;
  const list = node.progress.querySelector(".message-agent-progress");
  if (!list) return;
  const key = String(agent || "").trim();
  if (!key) return;
  let item = list.querySelector(`[data-agent="${CSS.escape(key)}"]`);
  if (!item) {
    item = document.createElement("li");
    item.dataset.agent = key;
    const name = document.createElement("strong");
    name.className = "message-agent-name";
    const state = document.createElement("span");
    state.className = "message-agent-status";
    const detail = document.createElement("span");
    detail.className = "message-agent-detail";
    item.append(name, state, detail);
    list.append(item);
  }
  item.dataset.status = status;
  item.querySelector(".message-agent-name").textContent = key;
  item.querySelector(".message-agent-status").textContent = t(status) || status;
  item.querySelector(".message-agent-detail").textContent = message ? ` ${message}` : "";
  list.hidden = false;
  node.progress.hidden = false;
}

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch (error) {
      // Fall back below when the browser blocks Clipboard API permissions.
    }
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.top = "-1000px";
  document.body.append(textarea);
  textarea.select();
  document.execCommand("copy");
  textarea.remove();
}

function setupMessageCopy() {
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy-message]");
    if (!button) return;
    const message = button.closest(".message");
    const content = message?.querySelector("[data-message-body]")?.dataset.rawContent || "";
    if (!content) return;
    await copyText(content);
    button.classList.add("copied");
    button.textContent = t("copied");
    button.setAttribute("aria-label", t("copied"));
    window.setTimeout(() => {
      button.classList.remove("copied");
      button.textContent = t("copy");
      button.setAttribute("aria-label", t("copyMessage"));
    }, 1400);
  });
}

function setupSettingsToggle() {
  const button = document.querySelector("[data-settings-toggle]");
  const bodies = Array.from(document.querySelectorAll("[data-settings-panel-body]"));
  if (!button || bodies.length === 0) return;
  const storageKey = "maigent.settingsGroupCollapsed";

  const setCollapsed = (collapsed) => {
    bodies.forEach((body) => {
      body.hidden = collapsed;
      body.classList.toggle("is-collapsed", collapsed);
    });
    button.setAttribute("aria-expanded", collapsed ? "false" : "true");
    button.classList.toggle("is-collapsed", collapsed);
    button.textContent = t("config");
    localStorage.setItem(storageKey, collapsed ? "true" : "false");
  };

  const savedState = localStorage.getItem(storageKey);
  setCollapsed(savedState === null ? true : savedState === "true");
  button.addEventListener("click", () => setCollapsed(!bodies.every((body) => body.hidden)));
}

const directoryPicker = {
  current: "",
  parent: "",
  target: "project",
};

async function loadDirectory(path = "") {
  const modal = document.querySelector("[data-directory-modal]");
  const list = document.querySelector("[data-directory-list]");
  const currentNode = document.querySelector("[data-directory-current]");
  const parentButton = document.querySelector("[data-directory-parent]");
  const empty = document.querySelector("[data-directory-empty]");
  if (!modal || !list || !currentNode || !parentButton || !empty) return;

  list.innerHTML = "";
  empty.hidden = true;
  const params = path ? `?path=${encodeURIComponent(path)}` : "";
  const response = await fetch(`/browse-directories/${params}`);
  if (!response.ok) {
    empty.hidden = false;
    empty.textContent = t("directoryLoadError");
    return;
  }
  const payload = await response.json();
  directoryPicker.current = payload.current;
  directoryPicker.parent = payload.parent;
  currentNode.textContent = payload.current;
  parentButton.disabled = !payload.parent;

  payload.directories.forEach((directory) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "directory-item";
    button.dataset.path = directory.path;
    const name = document.createElement("span");
    name.className = "directory-name";
    name.textContent = directory.name;
    button.append(name);
    if (directory.is_repo) {
      const badge = document.createElement("span");
      badge.className = "repo-badge";
      badge.textContent = t("repo");
      button.append(badge);
    }
    button.addEventListener("click", () => loadDirectory(directory.path));
    list.append(button);
  });
  empty.hidden = payload.directories.length > 0;
  if (payload.directories.length === 0) {
    empty.textContent = t("noFolders");
  }
}

function setupDirectoryPicker() {
  const modal = document.querySelector("[data-directory-modal]");
  const openButton = document.querySelector("[data-open-directory-picker]");
  const closeButton = document.querySelector("[data-close-directory-picker]");
  const chooseButton = document.querySelector("[data-choose-current-directory]");
  const parentButton = document.querySelector("[data-directory-parent]");
  const pathInput = document.querySelector("[data-project-path-input]");
  const accessPathInput = document.querySelector("[data-access-path-input]");
  const outputPathInput = document.querySelector("[data-output-path-input]");
  if (!modal || !openButton || !closeButton || !chooseButton || !parentButton || !pathInput) return;

  const targetInput = () => {
    if (directoryPicker.target === "access") return accessPathInput;
    if (directoryPicker.target === "output") return outputPathInput;
    return pathInput;
  };

  document.querySelectorAll("[data-open-directory-picker]").forEach((button) => {
    button.addEventListener("click", async () => {
      directoryPicker.target = button.dataset.pickerTarget || "project";
      const input = targetInput();
      modal.hidden = false;
      await loadDirectory(input?.value.trim() || "");
    });
  });
  closeButton.addEventListener("click", () => {
    modal.hidden = true;
  });
  modal.addEventListener("click", (event) => {
    if (event.target === modal) {
      modal.hidden = true;
    }
  });
  parentButton.addEventListener("click", () => {
    if (directoryPicker.parent) {
      loadDirectory(directoryPicker.parent);
    }
  });
  chooseButton.addEventListener("click", () => {
    const input = targetInput();
    if (input) input.value = directoryPicker.current;
    modal.hidden = true;
  });
}

document.addEventListener("DOMContentLoaded", () => {
  applyTheme(activeTheme());
  applyLanguage(activeLanguage());
  requestAnimationFrame(() => scrollMessagesToBottom("auto"));
  document.querySelectorAll(".lang-button").forEach((button) => {
    button.addEventListener("click", () => applyLanguage(button.dataset.lang));
  });
  document.querySelector("[data-theme-toggle]")?.addEventListener("click", () => {
    applyTheme(activeTheme() === "dark" ? "light" : "dark");
  });
  document.querySelectorAll("[data-confirm-delete]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(t("deleteThreadConfirm"))) {
        event.preventDefault();
      }
    });
  });
  document.querySelectorAll("[data-confirm-access-delete]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(t("deleteAccessPathConfirm"))) {
        event.preventDefault();
      }
    });
  });
  renderInitialMessages();
  setupDirectoryPicker();
  setupMessageCopy();
  setupSettingsToggle();

  const form = document.querySelector("#chat-form");
  if (!form) return;
  const messageInput = form.querySelector("textarea[name=message]");

  messageInput?.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) {
      return;
    }
    event.preventDefault();
    form.requestSubmit();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const textarea = messageInput || form.querySelector("textarea[name=message]");
    const text = textarea.value.trim();
    if (!text) return;

    appendMessage("user", text, "complete");
    textarea.value = "";
    setActivity(true, "preparing");

    const body = new FormData(form);
    body.set("message", text);

    try {
      const response = await fetch(form.action, {
        method: "POST",
        headers: { "X-CSRFToken": csrfToken(form) },
        body,
      });

      const payload = await response.json();
      if (payload.content) {
        appendMessage("assistant", payload.content, payload.error ? "error" : "complete");
        updateThreadSummary(payload.thread_summary);
        setActivity(false);
        return;
      }
      if (payload.stream_url) {
        setActivity(true, "running");
        const assistant = appendMessage("assistant", "", "streaming");
        streamAssistant(payload.stream_url, assistant);
        return;
      }
      if (payload.error) {
        appendMessage("assistant", payload.error, "error");
      }
    } catch (error) {
      appendMessage("assistant", error.message || String(error), "error");
    } finally {
      if (!document.querySelector(".message.assistant.streaming")) {
        setActivity(false);
      }
    }
  });
});
