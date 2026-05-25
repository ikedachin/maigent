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
    cmdWrite: "読み書き許可済みファイルへ書き込む",
    cmdAppend: "読み書き許可済みファイルへ追記する",
    cmdExperimental: "試験的機能の状態を表示",
    cmdAgent: "エージェント設定の状態を表示",
    cmdTheme: "テーマ設定の状態を表示",
    cmdApps: "アプリ連携の状態を表示",
    selectRepository: "リポジトリを選択",
    selectRepositoryHelp: "フォルダをクリックして移動し、現在のフォルダをプロジェクトパスに設定できます。",
    currentFolder: "現在のフォルダ",
    parentFolder: "上へ",
    useThisFolder: "このフォルダを使う",
    noFolders: "表示できるフォルダがありません。",
    repo: "Git",
    directoryLoadError: "フォルダを読み込めませんでした。",
    complete: "完了",
    streaming: "生成中",
    running: "実行中",
    preparing: "準備中",
    partialProgress: "進行ログ（一部・最新3行）",
    error: "エラー",
    pending: "待機中",
    elapsedTime: "実行時間",
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
    cmdWrite: "Write an allowed read/write file",
    cmdAppend: "Append to an allowed read/write file",
    cmdExperimental: "Show experimental feature status",
    cmdAgent: "Show agent settings status",
    cmdTheme: "Show theme settings status",
    cmdApps: "Show app integration status",
    selectRepository: "Select repository",
    selectRepositoryHelp: "Click folders to navigate, then set the current folder as the project path.",
    currentFolder: "Current folder",
    parentFolder: "Up",
    useThisFolder: "Use this folder",
    noFolders: "No folders available.",
    repo: "Git",
    directoryLoadError: "Could not load folders.",
    complete: "complete",
    streaming: "streaming",
    running: "running",
    preparing: "preparing",
    partialProgress: "Progress log (partial, latest 3 lines)",
    error: "error",
    pending: "pending",
    elapsedTime: "Elapsed time",
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
  const pre = document.createElement("pre");
  pre.textContent = content || "";
  if (role === "assistant" && state === "streaming" && !content) {
    pre.className = "message-loading";
    pre.innerHTML = `<span>${t("running")}</span><span class="typing-dots" aria-hidden="true"><i></i><i></i><i></i></span>`;
  }
  meta.append(roleNode, actions);
  article.append(meta, pre);
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
    progress.append(label, list);
    article.append(progress);
  }
  container.append(article);
  scrollMessagesToBottom();
  return { article, pre, meta, progress };
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
    if (payload.delta) {
      if (node.pre.classList.contains("message-loading")) {
        node.pre.className = "";
        node.pre.textContent = "";
      }
      node.pre.textContent += payload.delta;
    }
    if (payload.error) {
      node.article.classList.add("error");
      node.pre.className = "";
      node.pre.textContent = payload.error;
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
    node.pre.className = "";
    const status = node.meta.querySelector("[data-status]");
    if (status) {
      status.dataset.status = "error";
      status.textContent = t("error");
    }
    setElapsedTime(node, performance.now() - startedAt);
    if (!node.pre.textContent.trim()) {
      node.pre.textContent = t("error");
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
    const content = message?.querySelector("pre")?.textContent || "";
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
  if (!modal || !openButton || !closeButton || !chooseButton || !parentButton || !pathInput) return;

  document.querySelectorAll("[data-open-directory-picker]").forEach((button) => {
    button.addEventListener("click", async () => {
      directoryPicker.target = button.dataset.pickerTarget || "project";
      const input = directoryPicker.target === "access" ? accessPathInput : pathInput;
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
    const input = directoryPicker.target === "access" ? accessPathInput : pathInput;
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
