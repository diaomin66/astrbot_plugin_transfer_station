const bridge = window.AstrBotPluginPage;
const PAGE_SIZE = 20;

const state = {
  codePage: 1,
  codeHasMore: false,
  claimPage: 1,
  claimHasMore: false,
};

const elements = {
  availableCodes: document.getElementById("availableCodes"),
  claimedUsers: document.getElementById("claimedUsers"),
  pendingNewcomers: document.getElementById("pendingNewcomers"),
  eligibleMembers: document.getElementById("eligibleMembers"),
  refreshButton: document.getElementById("refreshButton"),
  importForm: document.getElementById("importForm"),
  importButton: document.getElementById("importButton"),
  codeInput: document.getElementById("codeInput"),
  codeRows: document.getElementById("codeRows"),
  codeEmpty: document.getElementById("codeEmpty"),
  codeRange: document.getElementById("codeRange"),
  codePrev: document.getElementById("codePrev"),
  codeNext: document.getElementById("codeNext"),
  codePage: document.getElementById("codePage"),
  claimRows: document.getElementById("claimRows"),
  claimEmpty: document.getElementById("claimEmpty"),
  claimRange: document.getElementById("claimRange"),
  claimPrev: document.getElementById("claimPrev"),
  claimNext: document.getElementById("claimNext"),
  claimPage: document.getElementById("claimPage"),
  toast: document.getElementById("toast"),
};

function syncTheme(context) {
  document.documentElement.dataset.theme = context?.isDark ? "dark" : "light";
}

function showToast(message, tone = "info") {
  elements.toast.textContent = message;
  elements.toast.dataset.tone = tone;
  elements.toast.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    elements.toast.hidden = true;
  }, 3500);
}

async function apiGet(endpoint, params = {}) {
  return bridge.apiGet(endpoint, params);
}

async function apiPost(endpoint, body = {}) {
  return bridge.apiPost(endpoint, body);
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN");
}

function maskCode(code) {
  const value = String(code || "");
  if (value.length <= 4) return "••••";
  if (value.length <= 8) return `${value.slice(0, 1)}••••${value.slice(-2)}`;
  return `${value.slice(0, 3)}••••••${value.slice(-3)}`;
}

function createButton(label, className, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

async function loadSummary() {
  const data = await apiGet("summary");
  elements.availableCodes.textContent = String(data.available_codes ?? 0);
  elements.claimedUsers.textContent = String(data.claimed_users ?? 0);
  elements.pendingNewcomers.textContent = String(data.pending_newcomers ?? 0);
  elements.eligibleMembers.textContent = String(data.eligible_members ?? 0);
}

function renderCodes(data) {
  elements.codeRows.replaceChildren();
  const items = Array.isArray(data.items) ? data.items : [];
  for (const item of items) {
    const row = document.createElement("tr");

    const idCell = document.createElement("td");
    idCell.textContent = String(item.id);

    const codeCell = document.createElement("td");
    codeCell.className = "code-cell";
    const codeValue = document.createElement("span");
    codeValue.className = "code-value";
    codeValue.textContent = maskCode(item.code);
    codeValue.dataset.revealed = "false";
    codeCell.append(codeValue);

    const timeCell = document.createElement("td");
    timeCell.textContent = formatTime(item.created_at);

    const actionCell = document.createElement("td");
    const actions = document.createElement("div");
    actions.className = "row-actions";
    const revealButton = createButton("显示", "button secondary compact", () => {
      const revealed = codeValue.dataset.revealed === "true";
      codeValue.textContent = revealed ? maskCode(item.code) : String(item.code);
      codeValue.dataset.revealed = String(!revealed);
      revealButton.textContent = revealed ? "显示" : "隐藏";
    });
    const copyButton = createButton("复制", "button secondary compact", async () => {
      try {
        await navigator.clipboard.writeText(String(item.code));
        showToast("兑换码已复制。", "success");
      } catch {
        showToast("浏览器拒绝复制，请先显示后手动复制。", "error");
      }
    });
    const deleteButton = createButton("删除", "button danger compact", async () => {
      if (deleteButton.dataset.confirming !== "true") {
        deleteButton.dataset.confirming = "true";
        deleteButton.textContent = "确认删除";
        deleteButton.classList.add("confirming");
        deleteButton.title = "再次点击确认删除";
        deleteButton._resetTimer = window.setTimeout(() => {
          deleteButton.dataset.confirming = "false";
          deleteButton.textContent = "删除";
          deleteButton.classList.remove("confirming");
          deleteButton.title = "";
        }, 4000);
        showToast("请再次点击“确认删除”完成操作。");
        return;
      }
      window.clearTimeout(deleteButton._resetTimer);
      deleteButton.disabled = true;
      try {
        await apiPost("codes/delete", { id: item.id });
        showToast("兑换码已删除。", "success");
        await Promise.all([loadSummary(), loadCodes()]);
      } catch (error) {
        showToast(error.message || "删除失败。", "error");
      } finally {
        deleteButton.disabled = false;
      }
    });
    actions.append(revealButton, copyButton, deleteButton);
    actionCell.append(actions);
    row.append(idCell, codeCell, timeCell, actionCell);
    elements.codeRows.append(row);
  }

  elements.codeEmpty.hidden = items.length !== 0;
  state.codeHasMore = Boolean(data.has_more);
  elements.codePrev.disabled = state.codePage <= 1;
  elements.codeNext.disabled = !state.codeHasMore;
  elements.codePage.textContent = `第 ${state.codePage} 页`;
  const start = data.total ? (state.codePage - 1) * PAGE_SIZE + 1 : 0;
  const end = Math.min(state.codePage * PAGE_SIZE, Number(data.total || 0));
  elements.codeRange.textContent = `${start}–${end} / ${data.total || 0}`;
}

async function loadCodes() {
  const data = await apiGet("codes", {
    page: state.codePage,
    page_size: PAGE_SIZE,
  });
  if (state.codePage > 1 && Number(data.total || 0) <= (state.codePage - 1) * PAGE_SIZE) {
    state.codePage -= 1;
    return loadCodes();
  }
  renderCodes(data);
}

function renderClaims(data) {
  elements.claimRows.replaceChildren();
  const items = Array.isArray(data.items) ? data.items : [];
  for (const item of items) {
    const row = document.createElement("tr");
    for (const value of [
      item.user_id,
      item.group_id,
      item.code_suffix ? `••••${item.code_suffix}` : "—",
      formatTime(item.claimed_at),
    ]) {
      const cell = document.createElement("td");
      cell.textContent = String(value ?? "—");
      row.append(cell);
    }
    elements.claimRows.append(row);
  }

  elements.claimEmpty.hidden = items.length !== 0;
  state.claimHasMore = Boolean(data.has_more);
  elements.claimPrev.disabled = state.claimPage <= 1;
  elements.claimNext.disabled = !state.claimHasMore;
  elements.claimPage.textContent = `第 ${state.claimPage} 页`;
  const start = data.total ? (state.claimPage - 1) * PAGE_SIZE + 1 : 0;
  const end = Math.min(state.claimPage * PAGE_SIZE, Number(data.total || 0));
  elements.claimRange.textContent = `${start}–${end} / ${data.total || 0}`;
}

async function loadClaims() {
  const data = await apiGet("claims", {
    page: state.claimPage,
    page_size: PAGE_SIZE,
  });
  if (state.claimPage > 1 && Number(data.total || 0) <= (state.claimPage - 1) * PAGE_SIZE) {
    state.claimPage -= 1;
    return loadClaims();
  }
  renderClaims(data);
}

async function refreshAll() {
  elements.refreshButton.disabled = true;
  try {
    await Promise.all([loadSummary(), loadCodes(), loadClaims()]);
  } catch (error) {
    showToast(error.message || "读取后台数据失败。", "error");
  } finally {
    elements.refreshButton.disabled = false;
  }
}

function bindEvents() {
  elements.refreshButton.addEventListener("click", refreshAll);
  elements.importForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const content = elements.codeInput.value;
    if (!content.trim()) {
      showToast("请先输入兑换码。", "error");
      return;
    }
    elements.importButton.disabled = true;
    try {
      const result = await apiPost("codes/import", { content });
      elements.codeInput.value = "";
      state.codePage = 1;
      showToast(
        `成功导入 ${result.inserted || 0} 个，跳过 ${result.duplicates || 0} 个重复项。`,
        "success",
      );
      await Promise.all([loadSummary(), loadCodes()]);
    } catch (error) {
      showToast(error.message || "导入失败。", "error");
    } finally {
      elements.importButton.disabled = false;
    }
  });

  elements.codePrev.addEventListener("click", async () => {
    if (state.codePage <= 1) return;
    state.codePage -= 1;
    await loadCodes();
  });
  elements.codeNext.addEventListener("click", async () => {
    if (!state.codeHasMore) return;
    state.codePage += 1;
    await loadCodes();
  });
  elements.claimPrev.addEventListener("click", async () => {
    if (state.claimPage <= 1) return;
    state.claimPage -= 1;
    await loadClaims();
  });
  elements.claimNext.addEventListener("click", async () => {
    if (!state.claimHasMore) return;
    state.claimPage += 1;
    await loadClaims();
  });
}

async function init() {
  bindEvents();
  if (!bridge) {
    showToast("未检测到 AstrBot Plugin Page Bridge，请从 AstrBot WebUI 打开。", "error");
    return;
  }
  try {
    const context = await bridge.ready();
    syncTheme(context);
    bridge.onContext(syncTheme);
    await refreshAll();
  } catch (error) {
    showToast(error.message || "页面初始化失败。", "error");
  }
}

init();
