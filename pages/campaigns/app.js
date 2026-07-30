const bridge = window.AstrBotPluginPage;
const HISTORY_PAGE_SIZE = 12;
const DETAIL_PAGE_SIZE = 20;
const AUTO_REFRESH_MS = 10000;
const MAX_ACTIVITY_PAGES = 1000;

const elements = {
  freshnessBadge: document.querySelector("#freshnessBadge"),
  refreshAllButton: document.querySelector("#refreshAllButton"),
  newapiStatus: document.querySelector("#newapiStatus"),
  newapiCredentialState: document.querySelector("#newapiCredentialState"),
  newapiTestButton: document.querySelector("#newapiTestButton"),
  lotteryFeatureState: document.querySelector("#lotteryFeatureState"),
  lotteryGroupState: document.querySelector("#lotteryGroupState"),
  compensationFeatureState: document.querySelector("#compensationFeatureState"),
  compensationGroupState: document.querySelector("#compensationGroupState"),
  lotteryActiveCount: document.querySelector("#lotteryActiveCount"),
  lotteryTotalCount: document.querySelector("#lotteryTotalCount"),
  lotteryParticipantCount: document.querySelector("#lotteryParticipantCount"),
  lotteryWinnerCount: document.querySelector("#lotteryWinnerCount"),
  lotteryPaidCount: document.querySelector("#lotteryPaidCount"),
  lotteryReviewCount: document.querySelector("#lotteryReviewCount"),
  compensationActiveCount: document.querySelector("#compensationActiveCount"),
  compensationTotalCount: document.querySelector("#compensationTotalCount"),
  compensationRecordCount: document.querySelector("#compensationRecordCount"),
  compensationPaidCount: document.querySelector("#compensationPaidCount"),
  compensationQuotaCount: document.querySelector("#compensationQuotaCount"),
  compensationReviewCount: document.querySelector("#compensationReviewCount"),
  lotteryCreateForm: document.querySelector("#lotteryCreateForm"),
  lotteryCreateGroup: document.querySelector("#lotteryCreateGroup"),
  lotteryCreateTitle: document.querySelector("#lotteryCreateTitle"),
  lotteryLiveCount: document.querySelector("#lotteryLiveCount"),
  lotteryLiveList: document.querySelector("#lotteryLiveList"),
  lotteryDetailPanel: document.querySelector("#lotteryDetailPanel"),
  lotteryHistoryFilter: document.querySelector("#lotteryHistoryFilter"),
  lotteryHistoryGroup: document.querySelector("#lotteryHistoryGroup"),
  lotteryHistoryList: document.querySelector("#lotteryHistoryList"),
  lotteryHistoryPrev: document.querySelector("#lotteryHistoryPrev"),
  lotteryHistoryNext: document.querySelector("#lotteryHistoryNext"),
  lotteryHistoryPage: document.querySelector("#lotteryHistoryPage"),
  compensationOpenForm: document.querySelector("#compensationOpenForm"),
  compensationGroup: document.querySelector("#compensationGroup"),
  compensationTitle: document.querySelector("#compensationTitle"),
  compensationPerAmount: document.querySelector("#compensationPerAmount"),
  compensationDuration: document.querySelector("#compensationDuration"),
  compensationTotalAmount: document.querySelector("#compensationTotalAmount"),
  compensationLiveCount: document.querySelector("#compensationLiveCount"),
  compensationLiveList: document.querySelector("#compensationLiveList"),
  compensationDetailPanel: document.querySelector("#compensationDetailPanel"),
  compensationHistoryFilter: document.querySelector("#compensationHistoryFilter"),
  compensationHistoryGroup: document.querySelector("#compensationHistoryGroup"),
  compensationHistoryList: document.querySelector("#compensationHistoryList"),
  compensationHistoryPrev: document.querySelector("#compensationHistoryPrev"),
  compensationHistoryNext: document.querySelector("#compensationHistoryNext"),
  compensationHistoryPage: document.querySelector("#compensationHistoryPage"),
  settingsForm: document.querySelector("#settingsForm"),
  settingsBaseUrl: document.querySelector("#settingsBaseUrl"),
  settingsUserId: document.querySelector("#settingsUserId"),
  settingsTimeout: document.querySelector("#settingsTimeout"),
  settingsVerifySsl: document.querySelector("#settingsVerifySsl"),
  settingsAllowHttp: document.querySelector("#settingsAllowHttp"),
  settingsLotteryEnabled: document.querySelector("#settingsLotteryEnabled"),
  settingsLotteryGroups: document.querySelector("#settingsLotteryGroups"),
  settingsCompensationEnabled: document.querySelector(
    "#settingsCompensationEnabled",
  ),
  settingsCompensationGroups: document.querySelector(
    "#settingsCompensationGroups",
  ),
  settingsRevision: document.querySelector("#settingsRevision"),
  credentialSummary: document.querySelector("#credentialSummary"),
  settingsDirtyState: document.querySelector("#settingsDirtyState"),
  settingsConflictState: document.querySelector("#settingsConflictState"),
  settingsReloadButton: document.querySelector("#settingsReloadButton"),
  settingsSaveButton: document.querySelector("#settingsSaveButton"),
  reasonModal: document.querySelector("#reasonModal"),
  reasonModalTitle: document.querySelector("#reasonModalTitle"),
  reasonModalInput: document.querySelector("#reasonModalInput"),
  reasonModalForm: document.querySelector("#reasonModalForm"),
  reasonModalCancel: document.querySelector("#reasonModalCancel"),
  toastRegion: document.querySelector("#toastRegion"),
};

const state = {
  settings: null,
  settingsDirty: false,
  formBaseRevision: "",
  settingsGeneration: 0,
  summaryGeneration: 0,
  refreshRunning: false,
  lottery: {
    liveGeneration: 0,
    historyGeneration: 0,
    detailGeneration: 0,
    live: [],
    history: [],
    historyPage: 1,
    historyTotal: 0,
    historyHasMore: false,
    historyGroup: "",
    selectedId: "",
    detail: null,
    detailPage: 1,
    detailDirty: false,
    draftConfigDirty: false,
    prizeDraftDirty: false,
  },
  compensation: {
    liveGeneration: 0,
    historyGeneration: 0,
    detailGeneration: 0,
    live: [],
    history: [],
    historyPage: 1,
    historyTotal: 0,
    historyHasMore: false,
    historyGroup: "",
    selectedId: "",
    detail: null,
    detailPage: 1,
  },
};

const confirmTimers = new WeakMap();
let reasonResolver = null;

function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) {
    element.className = className;
  }
  if (text !== "") {
    element.textContent = String(text);
  }
  return element;
}

function setText(element, value) {
  element.textContent = String(value ?? "—");
}

function disableFormControls(form) {
  const controls = [...form.elements];
  const previous = controls.map((control) => [control, control.disabled]);
  for (const control of controls) {
    control.disabled = true;
  }
  return () => {
    for (const [control, disabled] of previous) {
      control.disabled = disabled;
    }
  };
}

function formatTime(value) {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return date.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function toShanghaiInput(value) {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(date);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}T${values.hour}:${values.minute}`;
}

function durationSpec(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) {
    return "24h";
  }
  if (value % 86400 === 0) {
    return `${value / 86400}d`;
  }
  if (value % 3600 === 0) {
    return `${value / 3600}h`;
  }
  if (value % 60 === 0) {
    return `${value / 60}m`;
  }
  return `${value}s`;
}

function displayAmount(amount, displayType) {
  if (amount === null || amount === undefined || amount === "") {
    return "—";
  }
  const suffix = displayType === "TOKENS" ? " quota" : ` ${displayType || ""}`;
  return `${amount}${suffix}`.trim();
}

function statusLabel(status) {
  return (
    {
      draft: "草稿",
      scheduled: "待开始",
      open: "进行中",
      claiming: "领奖中",
      completed: "已完成",
      cancelled: "已取消",
      pending_confirmation: "待确认",
      processing: "发放中",
      paid: "已到账",
      failed: "失败",
      manual_review: "人工核查",
      expired: "已过期",
    }[status] || status || "未知"
  );
}

function statusClass(status) {
  if (["manual_review", "cancelled", "failed"].includes(status)) {
    return "is-danger";
  }
  if (["draft", "scheduled", "pending_confirmation", "processing"].includes(status)) {
    return "is-warning";
  }
  return "";
}

function statusPill(status) {
  const pill = node(
    "span",
    `status-pill ${statusClass(status)}`.trim(),
    statusLabel(status),
  );
  pill.dataset.status = String(status || "");
  return pill;
}

function metric(text) {
  return node("span", "metric-pill", text);
}

function toast(message, kind = "info") {
  const item = node("div", `toast ${kind === "error" ? "is-error" : ""}`, message);
  elements.toastRegion.append(item);
  window.setTimeout(() => item.remove(), 4800);
}

function publicError(error, fallback) {
  const message = error instanceof Error ? error.message : String(error || "");
  return message && message !== "Plugin bridge request failed." ? message : fallback;
}

function syncLotteryDetailDirty() {
  state.lottery.detailDirty =
    state.lottery.draftConfigDirty || state.lottery.prizeDraftDirty;
}

function requireCleanLotteryDraft(message = "请先保存或放弃未完成的草稿修改") {
  if (!state.lottery.detailDirty) {
    return;
  }
  throw new Error(message);
}

function closeReasonModal(value) {
  elements.reasonModal.hidden = true;
  const resolve = reasonResolver;
  reasonResolver = null;
  if (resolve) {
    resolve(value);
  }
}

function requestReason(title) {
  if (reasonResolver) {
    closeReasonModal(null);
  }
  setText(elements.reasonModalTitle, title);
  elements.reasonModalInput.value = "";
  elements.reasonModal.hidden = false;
  window.setTimeout(() => elements.reasonModalInput.focus(), 0);
  return new Promise((resolve) => {
    reasonResolver = resolve;
  });
}

function setFreshness(stale, text = "") {
  elements.freshnessBadge.classList.toggle("is-stale", stale);
  setText(
    elements.freshnessBadge,
    text || (stale ? "数据可能已过期" : `已同步 ${formatTime(new Date())}`),
  );
}

function loading(container, textValue = "正在读取数据库…") {
  container.replaceChildren(node("div", "loading-line", textValue));
}

function empty(container, textValue) {
  container.replaceChildren(node("div", "loading-line", textValue));
}

async function apiGet(endpoint, params = {}) {
  if (!bridge) {
    throw new Error("AstrBot Page Bridge 未加载");
  }
  return bridge.apiGet(endpoint, params);
}

async function apiPost(endpoint, body = {}) {
  if (!bridge) {
    throw new Error("AstrBot Page Bridge 未加载");
  }
  return bridge.apiPost(endpoint, body);
}

async function fetchAllActivities(endpoint, feature, generation) {
  const items = [];
  for (let page = 1; page <= MAX_ACTIVITY_PAGES; page += 1) {
    if (generation !== state[feature].liveGeneration) {
      return null;
    }
    const data = await apiGet(endpoint, {
      scope: "active",
      page,
      page_size: 100,
    });
    const pageItems = Array.isArray(data.items) ? data.items : [];
    items.push(...pageItems);
    if (!data.has_more) {
      return items;
    }
  }
  throw new Error("当前活动数量超过页面安全上限");
}

function renderSummary(data) {
  const lottery = data?.lottery || {};
  const compensation = data?.compensation || {};
  setText(elements.lotteryActiveCount, lottery.active_count ?? 0);
  setText(elements.lotteryTotalCount, `累计 ${lottery.activity_count ?? 0} 场`);
  setText(elements.lotteryParticipantCount, lottery.participant_count ?? 0);
  setText(elements.lotteryWinnerCount, lottery.winner_count ?? 0);
  setText(elements.lotteryPaidCount, `已发放 ${lottery.paid_winner_count ?? 0} 人`);
  setText(elements.lotteryReviewCount, lottery.manual_review_count ?? 0);
  setText(elements.compensationActiveCount, compensation.active_count ?? 0);
  setText(
    elements.compensationTotalCount,
    `累计 ${compensation.activity_count ?? 0} 场`,
  );
  setText(elements.compensationRecordCount, compensation.record_count ?? 0);
  setText(elements.compensationPaidCount, compensation.paid_count ?? 0);
  setText(
    elements.compensationQuotaCount,
    `占用原始 quota ${compensation.used_raw_quota ?? "0"}`,
  );
  setText(elements.compensationReviewCount, compensation.manual_review_count ?? 0);
}

async function refreshSummary() {
  const generation = ++state.summaryGeneration;
  const data = await apiGet("campaigns/summary");
  if (generation !== state.summaryGeneration) {
    return;
  }
  renderSummary(data);
}

function groupScopeText(groups) {
  return Array.isArray(groups) && groups.length
    ? `${groups.length} 个白名单群`
    : "全部 QQ 群";
}

function renderCredentialSummary(settings) {
  const items = [
    {
      label: settings.newapi_access_token_configured
        ? "访问令牌：已配置"
        : "访问令牌：未配置",
      ok: settings.newapi_access_token_configured,
    },
    {
      label: settings.newapi_username
        ? `兼容账号：${settings.newapi_username}`
        : "兼容账号：未配置",
      ok: Boolean(settings.newapi_username),
    },
    {
      label: settings.newapi_password_configured
        ? "兼容密码：已配置"
        : "兼容密码：未配置",
      ok: settings.newapi_password_configured,
    },
    {
      label: settings.newapi_user_id
        ? `旧版用户 ID：${settings.newapi_user_id}`
        : "旧版用户 ID：未配置",
      ok: Boolean(settings.newapi_user_id),
    },
  ];
  elements.credentialSummary.replaceChildren(
    ...items.map((item) =>
      node("span", `credential-item ${item.ok ? "is-ok" : ""}`.trim(), item.label),
    ),
  );
}

function renderFeatureStatus(settings) {
  const lotteryEnabled = Boolean(settings.lottery_enabled);
  const compensationEnabled = Boolean(settings.compensation_enabled);
  setText(elements.lotteryFeatureState, lotteryEnabled ? "已启用" : "已停用");
  setText(
    elements.lotteryGroupState,
    groupScopeText(settings.lottery_enabled_group_ids),
  );
  setText(
    elements.compensationFeatureState,
    compensationEnabled ? "已启用" : "已停用",
  );
  setText(
    elements.compensationGroupState,
    groupScopeText(settings.compensation_enabled_group_ids),
  );
  const credentialMode = settings.newapi_access_token_configured
    ? "访问令牌"
    : settings.newapi_username && settings.newapi_password_configured
      ? "用户名密码"
      : "凭据不完整";
  setText(elements.newapiCredentialState, `认证方式：${credentialMode}`);
  renderCredentialSummary(settings);
}

function applySettingsForm(settings) {
  state.settings = settings;
  state.formBaseRevision = String(settings.revision || "");
  state.settingsDirty = false;
  elements.settingsBaseUrl.value = settings.newapi_base_url || "";
  elements.settingsUserId.value = settings.newapi_user_id || "";
  elements.settingsTimeout.value = String(settings.newapi_timeout_seconds ?? 10);
  elements.settingsVerifySsl.checked = Boolean(settings.newapi_verify_ssl);
  elements.settingsAllowHttp.checked = Boolean(
    settings.newapi_allow_insecure_http,
  );
  elements.settingsLotteryEnabled.checked = Boolean(settings.lottery_enabled);
  elements.settingsLotteryGroups.value = Array.isArray(
    settings.lottery_enabled_group_ids,
  )
    ? settings.lottery_enabled_group_ids.join("\n")
    : "";
  elements.settingsCompensationEnabled.checked = Boolean(
    settings.compensation_enabled,
  );
  elements.settingsCompensationGroups.value = Array.isArray(
    settings.compensation_enabled_group_ids,
  )
    ? settings.compensation_enabled_group_ids.join("\n")
    : "";
  setText(
    elements.settingsRevision,
    `REV ${state.formBaseRevision.slice(0, 8) || "—"}`,
  );
  setText(elements.settingsDirtyState, "设置已同步");
  setText(elements.settingsConflictState, "自动刷新不会覆盖未保存内容");
  delete elements.settingsConflictState.dataset.conflict;
}

async function refreshSettings() {
  const generation = ++state.settingsGeneration;
  const settings = await apiGet("campaigns/settings");
  if (generation !== state.settingsGeneration) {
    return;
  }
  renderFeatureStatus(settings);
  if (!state.settingsDirty) {
    applySettingsForm(settings);
    return;
  }
  if (String(settings.revision || "") !== state.formBaseRevision) {
    setText(elements.settingsDirtyState, "存在未保存设置");
    setText(
      elements.settingsConflictState,
      "远端设置已变化：请刷新或保存时处理版本冲突",
    );
    elements.settingsConflictState.dataset.conflict = "true";
  }
}

function markSettingsDirty() {
  state.settingsDirty = true;
  setText(elements.settingsDirtyState, "存在未保存设置");
  setText(elements.settingsConflictState, "当前草稿不会被自动刷新覆盖");
  delete elements.settingsConflictState.dataset.conflict;
}

async function reloadSettingsFromRemote() {
  elements.settingsReloadButton.disabled = true;
  const generation = ++state.settingsGeneration;
  try {
    const settings = await apiGet("campaigns/settings");
    if (generation !== state.settingsGeneration) {
      return;
    }
    applySettingsForm(settings);
    renderFeatureStatus(settings);
    toast("已放弃本地修改并载入远端设置");
  } catch (error) {
    toast(publicError(error, "载入远端设置失败"), "error");
  } finally {
    elements.settingsReloadButton.disabled = false;
  }
}

function parseGroupLines(value) {
  const result = [];
  const seen = new Set();
  for (const raw of String(value).split(/[\s,，]+/u)) {
    const groupId = raw.trim();
    if (!groupId || seen.has(groupId)) {
      continue;
    }
    seen.add(groupId);
    result.push(groupId);
  }
  return result;
}

async function saveSettings(event) {
  event.preventDefault();
  if (!state.formBaseRevision) {
    toast("设置尚未完成加载，请稍后重试", "error");
    return;
  }
  const restoreControls = disableFormControls(elements.settingsForm);
  state.settingsGeneration += 1;
  try {
    const data = await apiPost("campaigns/settings/save", {
      revision: state.formBaseRevision,
      settings: {
        newapi_base_url: elements.settingsBaseUrl.value.trim(),
        newapi_user_id: elements.settingsUserId.value.trim(),
        newapi_timeout_seconds: elements.settingsTimeout.value,
        newapi_verify_ssl: elements.settingsVerifySsl.checked,
        newapi_allow_insecure_http: elements.settingsAllowHttp.checked,
        lottery_enabled: elements.settingsLotteryEnabled.checked,
        lottery_enabled_group_ids: parseGroupLines(
          elements.settingsLotteryGroups.value,
        ),
        compensation_enabled: elements.settingsCompensationEnabled.checked,
        compensation_enabled_group_ids: parseGroupLines(
          elements.settingsCompensationGroups.value,
        ),
      },
    });
    state.settingsGeneration += 1;
    applySettingsForm(data.settings);
    renderFeatureStatus(data.settings);
    toast(data.message || "活动系统设置已保存");
    if (data.warning) {
      toast(data.warning, "error");
    }
    await refreshAll({ quiet: true });
  } catch (error) {
    toast(publicError(error, "保存活动系统设置失败"), "error");
    setText(
      elements.settingsConflictState,
      "保存未完成；如远端已变化，可点击“载入远端设置”放弃本地草稿",
    );
  } finally {
    restoreControls();
  }
}

async function testNewApi() {
  const original = elements.newapiTestButton.textContent;
  elements.newapiTestButton.disabled = true;
  setText(elements.newapiTestButton, "验证中…");
  setText(elements.newapiStatus, "正在连接");
  try {
    const data = await apiPost("campaigns/newapi/test", {});
    setText(elements.newapiStatus, "连接正常 / 权限有效");
    setText(
      elements.newapiCredentialState,
      `${data.username} · ${data.role} · ${data.display_type} · ${data.version}`,
    );
    toast(data.message || "New API 验证成功");
  } catch (error) {
    setText(elements.newapiStatus, "验证失败");
    toast(publicError(error, "New API 连接或权限验证失败"), "error");
  } finally {
    elements.newapiTestButton.disabled = false;
    setText(elements.newapiTestButton, original);
  }
}

function activityCard(feature, item) {
  const card = node("article", "activity-card");
  card.dataset.activityId = String(item.id);
  card.dataset.status = String(item.status || "");
  const top = node("div");
  const status = statusPill(item.status);
  const title = node("h4", "", item.title || "未命名活动");
  const meta = node("div", "activity-card__meta");
  meta.append(
    node("span", "", `#${item.id} / 群 ${item.group_id}`),
    node(
      "span",
      "",
      feature === "lottery"
        ? `开奖 ${formatTime(item.draw_at)}`
        : `结束 ${formatTime(item.end_at)}`,
    ),
  );
  top.append(status, title, meta);

  const stats = node("div", "activity-card__stats");
  if (feature === "lottery") {
    stats.append(
      metric(`参与 ${item.participant_count ?? 0}`),
      metric(`中奖 ${item.winner_count ?? 0}`),
      metric(`核查 ${item.manual_review_count ?? 0}`),
    );
  } else {
    stats.append(
      metric(`记录 ${item.claim_count ?? 0}`),
      metric(`到账 ${item.paid_count ?? 0}`),
      metric(`核查 ${item.manual_review_count ?? 0}`),
    );
  }
  const openButton = node("button", "button button--compact", "打开详情");
  openButton.type = "button";
  openButton.addEventListener("click", () => {
    if (feature === "lottery") {
      loadLotteryDetail(String(item.id), 1);
    } else {
      loadCompensationDetail(String(item.id), 1);
    }
  });
  card.append(top, stats, openButton);
  return card;
}

function renderLive(feature) {
  const featureState = state[feature];
  const container =
    feature === "lottery"
      ? elements.lotteryLiveList
      : elements.compensationLiveList;
  const count =
    feature === "lottery"
      ? elements.lotteryLiveCount
      : elements.compensationLiveCount;
  setText(count, `${featureState.live.length} 场`);
  if (!featureState.live.length) {
    empty(container, "没有进行中的活动");
    return;
  }
  container.replaceChildren(
    ...featureState.live.map((item) => activityCard(feature, item)),
  );
}

async function refreshLotteryLive() {
  const generation = ++state.lottery.liveGeneration;
  const items = await fetchAllActivities(
    "campaigns/lotteries",
    "lottery",
    generation,
  );
  if (items === null || generation !== state.lottery.liveGeneration) {
    return;
  }
  state.lottery.live = items;
  renderLive("lottery");
}

async function refreshCompensationLive() {
  const generation = ++state.compensation.liveGeneration;
  const items = await fetchAllActivities(
    "campaigns/compensations",
    "compensation",
    generation,
  );
  if (items === null || generation !== state.compensation.liveGeneration) {
    return;
  }
  state.compensation.live = items;
  renderLive("compensation");
}

function historyRow(feature, item) {
  const row = node("article", "history-row");
  row.dataset.activityId = String(item.id);
  row.append(
    node("span", "history-row__id", `#${item.id}`),
    (() => {
      const title = node("div");
      title.append(
        node("strong", "", item.title || "未命名活动"),
        node("small", "", `群 ${item.group_id}`),
      );
      return title;
    })(),
    (() => {
      const status = node("div");
      status.append(statusPill(item.status));
      return status;
    })(),
    node(
      "small",
      "",
      feature === "lottery"
        ? `开奖：${formatTime(item.drawn_at || item.draw_at)}`
        : `结束：${formatTime(item.closed_at || item.end_at)}`,
    ),
    (() => {
      const button = node("button", "button button--compact", "查看");
      button.type = "button";
      button.addEventListener("click", () => {
        if (feature === "lottery") {
          loadLotteryDetail(String(item.id), 1);
        } else {
          loadCompensationDetail(String(item.id), 1);
        }
      });
      return button;
    })(),
  );
  return row;
}

function renderHistory(feature) {
  const featureState = state[feature];
  const container =
    feature === "lottery"
      ? elements.lotteryHistoryList
      : elements.compensationHistoryList;
  const pageLabel =
    feature === "lottery"
      ? elements.lotteryHistoryPage
      : elements.compensationHistoryPage;
  const prev =
    feature === "lottery"
      ? elements.lotteryHistoryPrev
      : elements.compensationHistoryPrev;
  const next =
    feature === "lottery"
      ? elements.lotteryHistoryNext
      : elements.compensationHistoryNext;
  if (!featureState.history.length) {
    empty(container, "当前筛选条件下没有历史活动");
  } else {
    container.replaceChildren(
      ...featureState.history.map((item) => historyRow(feature, item)),
    );
  }
  setText(
    pageLabel,
    `第 ${featureState.historyPage} 页 / 共 ${featureState.historyTotal} 场`,
  );
  prev.disabled = featureState.historyPage <= 1;
  next.disabled = !featureState.historyHasMore;
}

async function refreshHistory(feature) {
  const featureState = state[feature];
  const generation = ++featureState.historyGeneration;
  const endpoint =
    feature === "lottery"
      ? "campaigns/lotteries"
      : "campaigns/compensations";
  const data = await apiGet(endpoint, {
    scope: "history",
    group_id: featureState.historyGroup,
    page: featureState.historyPage,
    page_size: HISTORY_PAGE_SIZE,
  });
  if (generation !== featureState.historyGeneration) {
    return;
  }
  featureState.history = Array.isArray(data.items) ? data.items : [];
  featureState.historyTotal = Number(data.total || 0);
  featureState.historyHasMore = Boolean(data.has_more);
  renderHistory(feature);
}

async function changeHistoryPage(feature, nextPage) {
  const featureState = state[feature];
  const previousPage = featureState.historyPage;
  const requestGeneration = featureState.historyGeneration + 1;
  featureState.historyPage = nextPage;
  try {
    await refreshHistory(feature);
  } catch (error) {
    if (featureState.historyGeneration === requestGeneration) {
      featureState.historyPage = previousPage;
      renderHistory(feature);
      toast(
        publicError(
          error,
          feature === "lottery" ? "读取抽奖历史失败" : "读取补偿历史失败",
        ),
        "error",
      );
    }
  }
}

function detailHeader(feature, activity) {
  const header = node("header", "detail-header");
  const identity = node("div");
  const identityTop = node("div");
  identityTop.append(
    statusPill(activity.status),
    node("span", "metric-pill", `#${activity.id}`),
    node("span", "metric-pill", `群 ${activity.group_id}`),
  );
  const heading = node("h3", "", activity.title || "未命名活动");
  const meta = node("div", "detail-meta");
  if (feature === "lottery") {
    meta.append(
      node("span", "", `开始：${formatTime(activity.start_at)}`),
      node("span", "", `开奖：${formatTime(activity.draw_at)}`),
      node("span", "", `领奖截止：${formatTime(activity.claim_deadline_at)}`),
    );
  } else {
    meta.append(
      node("span", "", `开始：${formatTime(activity.start_at)}`),
      node("span", "", `计划结束：${formatTime(activity.end_at)}`),
      node("span", "", `实际关闭：${formatTime(activity.closed_at)}`),
    );
  }
  identity.append(identityTop, heading, meta);
  const actions = node("div", "detail-header__actions");
  header.append(identity, actions);
  return { header, actions };
}

function detailStats(items) {
  const grid = node("div", "detail-stat-grid");
  for (const item of items) {
    const card = node("div", "detail-stat");
    card.append(node("span", "", item.label), node("strong", "", item.value));
    grid.append(card);
  }
  return grid;
}

function actionButton(label, className, action, confirmLabel = "") {
  const button = node("button", `button ${className}`.trim(), label);
  button.type = "button";
  if (confirmLabel) {
    button.addEventListener("click", () =>
      confirmButton(button, confirmLabel, action),
    );
  } else {
    button.addEventListener("click", () => runButtonAction(button, action));
  }
  return button;
}

function confirmButton(button, confirmationText, action) {
  if (button.dataset.confirming === "true") {
    const timer = confirmTimers.get(button);
    if (timer) {
      window.clearTimeout(timer);
    }
    delete button.dataset.confirming;
    button.classList.remove("is-confirming");
    runButtonAction(button, action);
    return;
  }
  const original = button.textContent;
  button.dataset.confirming = "true";
  button.dataset.originalText = original;
  button.classList.add("is-confirming");
  setText(button, confirmationText);
  const timer = window.setTimeout(() => {
    delete button.dataset.confirming;
    button.classList.remove("is-confirming");
    setText(button, original);
  }, 5000);
  confirmTimers.set(button, timer);
}

async function runButtonAction(button, action) {
  const original = button.dataset.originalText || button.textContent;
  button.disabled = true;
  setText(button, "处理中…");
  try {
    const message = await action();
    if (message) {
      toast(message);
    }
  } catch (error) {
    toast(publicError(error, "操作失败"), "error");
  } finally {
    button.disabled = false;
    button.classList.remove("is-confirming");
    delete button.dataset.confirming;
    delete button.dataset.originalText;
    setText(button, original);
  }
}

function lotteryDraftEditor(detail) {
  const activity = detail.activity;
  const editor = node("section", "subpanel");
  editor.append(node("h4", "", "DRAFT CONFIG / 草稿配置"));
  const form = node("form", "form-stack");
  const title = node("input");
  title.value = activity.title || "";
  title.maxLength = 100;
  const description = node("textarea");
  description.value = activity.description || "";
  description.maxLength = 2000;
  const keyword = node("input");
  keyword.value = activity.keyword || "";
  keyword.maxLength = 50;
  const startTime = node("input");
  startTime.type = "datetime-local";
  startTime.value = toShanghaiInput(activity.start_at);
  const drawTime = node("input");
  drawTime.type = "datetime-local";
  drawTime.value = toShanghaiInput(activity.draw_at);
  const claimDuration = node("input");
  claimDuration.value = durationSpec(activity.claim_duration_seconds);

  const controls = [
    ["活动标题", title],
    ["活动描述", description],
    ["报名口令", keyword],
    ["开始时间（上海）", startTime],
    ["开奖时间（上海）", drawTime],
    ["领奖时限", claimDuration],
  ];
  for (const [labelText, control] of controls) {
    const label = node("label");
    label.append(node("span", "", labelText), control);
    form.append(label);
    control.addEventListener("input", () => {
      state.lottery.draftConfigDirty = true;
      syncLotteryDetailDirty();
    });
  }
  const save = node("button", "button button--primary", "保存草稿配置");
  save.type = "submit";
  const reload = node(
    "button",
    "button button--ghost",
    "放弃修改并重新载入",
  );
  reload.type = "button";
  reload.addEventListener("click", async () => {
    state.lottery.draftConfigDirty = false;
    state.lottery.prizeDraftDirty = false;
    syncLotteryDetailDirty();
    await loadLotteryDetail(String(activity.id), 1, { force: true });
  });
  form.append(save, reload);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.lottery.prizeDraftDirty) {
      toast("请先添加奖项或放弃未完成的奖项输入", "error");
      return;
    }
    const restoreControls = disableFormControls(form);
    try {
      const data = await apiPost("campaigns/lotteries/update", {
        activity_id: String(activity.id),
        revision: String(activity.revision),
        title: title.value,
        description: description.value,
        keyword: keyword.value,
        start_time: startTime.value,
        draw_time: drawTime.value,
        claim_duration: claimDuration.value,
      });
      state.lottery.draftConfigDirty = false;
      syncLotteryDetailDirty();
      toast(data.message || "抽奖草稿已更新");
      await refreshLotteryAfterAction(String(activity.id));
    } catch (error) {
      toast(publicError(error, "更新抽奖草稿失败"), "error");
    } finally {
      restoreControls();
    }
  });
  editor.append(form);
  return editor;
}

function lotteryPrizePanel(detail) {
  const activity = detail.activity;
  const panel = node("section", "subpanel");
  panel.append(node("h4", "", "PRIZE MATRIX / 奖项"));
  const list = node("div", "prize-list");
  const prizes = Array.isArray(detail.prizes) ? detail.prizes : [];
  if (!prizes.length) {
    list.append(node("div", "loading-line", "尚未添加奖项"));
  } else {
    for (const prize of prizes) {
      const row = node("div", "prize-row");
      row.dataset.prizeId = String(prize.id);
      row.append(
        node("span", "prize-row__position", String(prize.position)),
        (() => {
          const info = node("div");
          info.append(
            node("strong", "", prize.name),
            node(
              "small",
              "",
              `${prize.winner_count} 人 · ${displayAmount(
                prize.display_amount,
                activity.display_type,
              )}`,
            ),
          );
          return info;
        })(),
        node(
          "code",
          "",
          prize.raw_quota ? `${prize.raw_quota} quota` : "发布时换算",
        ),
      );
      if (activity.status === "draft") {
        row.append(
          actionButton(
            "删除",
            "button--compact button--danger",
            async () => {
              requireCleanLotteryDraft();
              const data = await apiPost("campaigns/lotteries/prizes/delete", {
                activity_id: String(activity.id),
                revision: String(activity.revision),
                prize_id: String(prize.id),
              });
              await refreshLotteryAfterAction(String(activity.id));
              return data.message;
            },
            "再次点击删除",
          ),
        );
      } else {
        row.append(node("span"));
      }
      list.append(row);
    }
  }
  panel.append(list);

  if (activity.status === "draft") {
    const form = node("form", "form-stack");
    const grid = node("div", "form-grid");
    const name = node("input");
    name.placeholder = "一等奖";
    const winnerCount = node("input");
    winnerCount.type = "number";
    winnerCount.min = "1";
    winnerCount.value = "1";
    const amount = node("input");
    amount.inputMode = "decimal";
    amount.placeholder = "10";
    for (const control of [name, winnerCount, amount]) {
      control.addEventListener("input", () => {
        state.lottery.prizeDraftDirty = true;
        syncLotteryDetailDirty();
      });
    }
    for (const [labelText, control] of [
      ["奖项名称", name],
      ["中奖人数", winnerCount],
    ]) {
      const label = node("label");
      label.append(node("span", "", labelText), control);
      grid.append(label);
    }
    const amountLabel = node("label");
    amountLabel.append(node("span", "", "每人额度"), amount);
    const submit = node("button", "button", "添加奖项");
    submit.type = "submit";
    form.append(grid, amountLabel, submit);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (state.lottery.draftConfigDirty) {
        toast("请先保存草稿配置或放弃未保存的配置修改", "error");
        return;
      }
      const restoreControls = disableFormControls(form);
      try {
        const data = await apiPost("campaigns/lotteries/prizes/add", {
          activity_id: String(activity.id),
          revision: String(activity.revision),
          name: name.value,
          winner_count: winnerCount.value,
          amount: amount.value,
        });
        state.lottery.prizeDraftDirty = false;
        syncLotteryDetailDirty();
        toast(data.message || "奖项已添加");
        await refreshLotteryAfterAction(String(activity.id));
      } catch (error) {
        toast(publicError(error, "添加奖项失败"), "error");
      } finally {
        restoreControls();
      }
    });
    panel.append(form);
  }
  return panel;
}

function reviewButtons(feature, activityId, serial) {
  const actions = node("div", "table-actions");
  const endpoint =
    feature === "lottery"
      ? "campaigns/lotteries/review"
      : "campaigns/compensations/review";
  const refresh =
    feature === "lottery"
      ? () => refreshLotteryAfterAction(activityId)
      : () => refreshCompensationAfterAction(activityId);
  for (const [label, success, confirmText, className] of [
    ["确认到账", true, "再次确认到账", "button--compact"],
    ["确认失败", false, "再次确认失败", "button--compact button--danger"],
  ]) {
    actions.append(
      actionButton(
        label,
        className,
        async () => {
          const data = await apiPost(endpoint, {
            activity_id: String(activityId),
            serial: String(serial),
            success,
          });
          await refresh();
          return data.message;
        },
        confirmText,
      ),
    );
  }
  return actions;
}

function lotteryWinnerPanel(detail) {
  const activity = detail.activity;
  const panel = node("section", "subpanel");
  panel.append(node("h4", "", "WINNERS / 中奖与发放"));
  const wrap = node("div", "table-wrap");
  const table = node("table");
  const head = node("thead");
  const headRow = node("tr");
  for (const title of [
    "QQ",
    "奖项",
    "额度",
    "状态",
    "New API ID",
    "用户名",
    "流水号",
    "操作",
  ]) {
    headRow.append(node("th", "", title));
  }
  head.append(headRow);
  const body = node("tbody");
  const winners = Array.isArray(detail.winners) ? detail.winners : [];
  if (!winners.length) {
    const row = node("tr");
    const cell = node("td", "", "暂无中奖记录");
    cell.colSpan = 8;
    row.append(cell);
    body.append(row);
  } else {
    for (const winner of winners) {
      const row = node("tr");
      const payoutState = winner.payout_state || winner.payout_status || "未领取";
      const values = [
        winner.user_id,
        winner.prize_name,
        displayAmount(winner.display_amount, activity.display_type),
        statusLabel(payoutState),
        winner.api_user_id || "—",
        winner.api_username || "—",
        winner.serial || "—",
      ];
      for (const value of values) {
        row.append(node("td", "", value));
      }
      const actionCell = node("td");
      if (payoutState === "manual_review" && winner.serial) {
        actionCell.append(
          reviewButtons("lottery", String(activity.id), winner.serial),
        );
      } else {
        actionCell.textContent = "—";
      }
      row.append(actionCell);
      body.append(row);
    }
  }
  table.append(head, body);
  wrap.append(table);
  panel.append(wrap);
  panel.append(
    detailPager(
      detail.winner_page,
      detail.winner_total,
      detail.winner_has_more,
      (page) => loadLotteryDetail(String(activity.id), page),
    ),
  );
  return panel;
}

function detailPager(page, total, hasMore, load) {
  const pager = node("div", "detail-pagination");
  const prev = node("button", "button button--compact button--ghost", "上一页");
  prev.type = "button";
  prev.disabled = Number(page) <= 1;
  prev.addEventListener("click", () => load(Number(page) - 1));
  const label = node(
    "span",
    "",
    `第 ${page || 1} 页 / 共 ${total || 0} 条`,
  );
  const next = node("button", "button button--compact button--ghost", "下一页");
  next.type = "button";
  next.disabled = !hasMore;
  next.addEventListener("click", () => load(Number(page) + 1));
  pager.append(prev, label, next);
  return pager;
}

function renderLotteryDetail(detail) {
  const panel = elements.lotteryDetailPanel;
  const activity = detail.activity;
  panel.classList.remove("is-empty");
  panel.dataset.activityId = String(activity.id);
  const { header, actions } = detailHeader("lottery", activity);
  if (activity.status === "draft") {
    actions.append(
      actionButton(
        "发布活动",
        "button--primary",
        async () => {
          requireCleanLotteryDraft();
          const data = await apiPost("campaigns/lotteries/publish", {
            activity_id: String(activity.id),
            revision: String(activity.revision),
          });
          state.lottery.draftConfigDirty = false;
          state.lottery.prizeDraftDirty = false;
          syncLotteryDetailDirty();
          await refreshLotteryAfterAction(String(activity.id));
          return data.message;
        },
        "再次点击发布",
      ),
    );
  }
  if (["scheduled", "open"].includes(activity.status)) {
    actions.append(
      actionButton(
        "立即开奖",
        "button--primary",
        async () => {
          const data = await apiPost("campaigns/lotteries/draw", {
            activity_id: String(activity.id),
            revision: String(activity.revision),
          });
          await refreshLotteryAfterAction(String(activity.id));
          return data.message;
        },
        "再次点击开奖",
      ),
    );
  }
  if (["draft", "scheduled", "open", "claiming"].includes(activity.status)) {
    actions.append(
      actionButton(
        "取消活动",
        "button--danger",
        async () => {
          requireCleanLotteryDraft();
          const reason = await requestReason("请输入取消原因（可留空）");
          if (reason === null) {
            return "";
          }
          const data = await apiPost("campaigns/lotteries/cancel", {
            activity_id: String(activity.id),
            revision: String(activity.revision),
            reason,
          });
          state.lottery.draftConfigDirty = false;
          state.lottery.prizeDraftDirty = false;
          syncLotteryDetailDirty();
          await refreshLotteryAfterAction(String(activity.id));
          return data.message;
        },
        "再次点击取消",
      ),
    );
  }
  const stats = detailStats([
    { label: "参与者", value: activity.participant_count ?? 0 },
    { label: "开奖有效", value: detail.eligible_count ?? 0 },
    { label: "中奖", value: activity.winner_count ?? 0 },
    { label: "已到账", value: activity.paid_winner_count ?? 0 },
    { label: "人工核查", value: activity.manual_review_count ?? 0 },
  ]);
  const columns = node("div", "detail-columns");
  columns.append(
    activity.status === "draft"
      ? lotteryDraftEditor(detail)
      : (() => {
          const description = node("section", "subpanel");
          description.append(
            node("h4", "", "ACTIVITY BRIEF / 活动说明"),
            node("p", "", activity.description || "未设置活动说明"),
            node("p", "form-note", `报名口令：${activity.keyword || "—"}`),
          );
          return description;
        })(),
    lotteryPrizePanel(detail),
  );
  panel.replaceChildren(header, stats, columns, lotteryWinnerPanel(detail));
}

async function loadLotteryDetail(
  activityId,
  page = 1,
  { force = false, showLoading = true, scroll = true } = {},
) {
  const id = String(activityId);
  state.lottery.selectedId = id;
  state.lottery.detailPage = page;
  const generation = ++state.lottery.detailGeneration;
  if (
    showLoading &&
    (!state.lottery.detailDirty || state.lottery.detail?.activity?.id !== id)
  ) {
    loading(elements.lotteryDetailPanel, `正在读取抽奖 #${id}…`);
  }
  try {
    const detail = await apiGet("campaigns/lotteries/detail", {
      activity_id: id,
      page,
      page_size: DETAIL_PAGE_SIZE,
    });
    if (
      generation !== state.lottery.detailGeneration ||
      id !== state.lottery.selectedId
    ) {
      return;
    }
    if (
      !force &&
      state.lottery.detailDirty &&
      state.lottery.detail?.activity?.id === id
    ) {
      return;
    }
    state.lottery.detail = detail;
    state.lottery.draftConfigDirty = false;
    state.lottery.prizeDraftDirty = false;
    syncLotteryDetailDirty();
    renderLotteryDetail(detail);
    if (scroll) {
      elements.lotteryDetailPanel.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    }
    return true;
  } catch (error) {
    if (generation === state.lottery.detailGeneration) {
      toast(publicError(error, "读取抽奖详情失败"), "error");
    }
    return false;
  }
}

async function refreshLotteryAfterAction(activityId) {
  const id = String(activityId);
  const results = await Promise.allSettled([
    refreshSummary(),
    refreshLotteryLive(),
    refreshHistory("lottery"),
  ]);
  const detailOk =
    state.lottery.selectedId === id
      ? await loadLotteryDetail(id, state.lottery.detailPage)
      : true;
  if (
    results.some((result) => result.status === "rejected") ||
    detailOk === false
  ) {
    setFreshness(true);
    toast("操作已完成，但部分最新数据尚未刷新", "error");
  }
}

function compensationRecordPanel(detail) {
  const activity = detail.activity;
  const panel = node("section", "subpanel");
  panel.append(node("h4", "", "PAYOUT LEDGER / 补偿发放记录"));
  const wrap = node("div", "table-wrap");
  const table = node("table");
  const head = node("thead");
  const headRow = node("tr");
  for (const title of [
    "流水号",
    "QQ",
    "New API ID",
    "用户名",
    "额度",
    "状态",
    "更新时间",
    "操作",
  ]) {
    headRow.append(node("th", "", title));
  }
  head.append(headRow);
  const body = node("tbody");
  const records = Array.isArray(detail.records) ? detail.records : [];
  if (!records.length) {
    const row = node("tr");
    const cell = node("td", "", "暂无补偿领取记录");
    cell.colSpan = 8;
    row.append(cell);
    body.append(row);
  } else {
    for (const record of records) {
      const row = node("tr");
      const values = [
        record.serial,
        record.qq_id,
        record.api_user_id,
        record.api_username,
        displayAmount(record.display_amount, activity.display_type),
        statusLabel(record.status),
        formatTime(record.updated_at),
      ];
      for (const value of values) {
        row.append(node("td", "", value || "—"));
      }
      const actionCell = node("td");
      if (record.status === "manual_review" && record.serial) {
        actionCell.append(
          reviewButtons("compensation", String(activity.id), record.serial),
        );
      } else {
        actionCell.textContent = "—";
      }
      row.append(actionCell);
      body.append(row);
    }
  }
  table.append(head, body);
  wrap.append(table);
  panel.append(wrap);
  panel.append(
    detailPager(
      detail.record_page,
      detail.record_total,
      detail.record_has_more,
      (page) => loadCompensationDetail(String(activity.id), page),
    ),
  );
  return panel;
}

function renderCompensationDetail(detail) {
  const panel = elements.compensationDetailPanel;
  const activity = detail.activity;
  panel.classList.remove("is-empty");
  panel.dataset.activityId = String(activity.id);
  const { header, actions } = detailHeader("compensation", activity);
  if (activity.status === "open") {
    actions.append(
      actionButton(
        "关闭补偿",
        "button--danger",
        async () => {
          const reason = await requestReason("请输入关闭原因（可留空）");
          if (reason === null) {
            return "";
          }
          const data = await apiPost("campaigns/compensations/close", {
            activity_id: String(activity.id),
            reason,
          });
          await refreshCompensationAfterAction(String(activity.id));
          return data.message;
        },
        "再次点击关闭",
      ),
    );
  }
  const totalBudget =
    activity.total_display_amount === null
      ? "不限"
      : displayAmount(activity.total_display_amount, activity.display_type);
  const stats = detailStats([
    {
      label: "每人额度",
      value: displayAmount(activity.per_display_amount, activity.display_type),
    },
    { label: "总预算", value: totalBudget },
    { label: "领取记录", value: activity.claim_count ?? 0 },
    { label: "已到账", value: activity.paid_count ?? 0 },
    { label: "人工核查", value: activity.manual_review_count ?? 0 },
  ]);
  const info = node("section", "subpanel");
  info.append(
    node("h4", "", "BUDGET SNAPSHOT / 预算快照"),
    node("p", "", `已占用原始 quota：${activity.used_raw_quota ?? "0"}`),
    node(
      "p",
      "form-note",
      `每份原始 quota：${activity.per_raw_quota ?? "—"}；结束原因：${
        activity.close_reason || "—"
      }`,
    ),
  );
  panel.replaceChildren(header, stats, info, compensationRecordPanel(detail));
}

async function loadCompensationDetail(
  activityId,
  page = 1,
  { showLoading = true, scroll = true } = {},
) {
  const id = String(activityId);
  state.compensation.selectedId = id;
  state.compensation.detailPage = page;
  const generation = ++state.compensation.detailGeneration;
  if (showLoading) {
    loading(elements.compensationDetailPanel, `正在读取补偿 #${id}…`);
  }
  try {
    const detail = await apiGet("campaigns/compensations/detail", {
      activity_id: id,
      page,
      page_size: DETAIL_PAGE_SIZE,
    });
    if (
      generation !== state.compensation.detailGeneration ||
      id !== state.compensation.selectedId
    ) {
      return;
    }
    state.compensation.detail = detail;
    renderCompensationDetail(detail);
    if (scroll) {
      elements.compensationDetailPanel.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    }
    return true;
  } catch (error) {
    if (generation === state.compensation.detailGeneration) {
      toast(publicError(error, "读取补偿详情失败"), "error");
    }
    return false;
  }
}

async function refreshCompensationAfterAction(activityId) {
  const id = String(activityId);
  const results = await Promise.allSettled([
    refreshSummary(),
    refreshCompensationLive(),
    refreshHistory("compensation"),
  ]);
  const detailOk =
    state.compensation.selectedId === id
      ? await loadCompensationDetail(id, state.compensation.detailPage)
      : true;
  if (
    results.some((result) => result.status === "rejected") ||
    detailOk === false
  ) {
    setFreshness(true);
    toast("操作已完成，但部分最新数据尚未刷新", "error");
  }
}

async function createLottery(event) {
  event.preventDefault();
  const submit = elements.lotteryCreateForm.querySelector("button[type='submit']");
  submit.disabled = true;
  try {
    const data = await apiPost("campaigns/lotteries/create", {
      group_id: elements.lotteryCreateGroup.value.trim(),
      title: elements.lotteryCreateTitle.value.trim(),
    });
    const activityId = String(data.placeholders?.activity_id || "");
    toast(data.message || "抽奖草稿已创建");
    elements.lotteryCreateTitle.value = "";
    state.lottery.selectedId = activityId;
    await refreshLotteryAfterAction(activityId);
  } catch (error) {
    toast(publicError(error, "创建抽奖草稿失败"), "error");
  } finally {
    submit.disabled = false;
  }
}

async function openCompensation(event) {
  event.preventDefault();
  const submit = elements.compensationOpenForm.querySelector(
    "button[type='submit']",
  );
  submit.disabled = true;
  try {
    const data = await apiPost("campaigns/compensations/open", {
      group_id: elements.compensationGroup.value.trim(),
      title: elements.compensationTitle.value.trim(),
      per_amount: elements.compensationPerAmount.value.trim(),
      duration: elements.compensationDuration.value.trim(),
      total_amount: elements.compensationTotalAmount.value.trim(),
    });
    const activityId = String(data.placeholders?.activity_id || "");
    toast(data.message || "补偿活动已开启");
    state.compensation.selectedId = activityId;
    await refreshCompensationAfterAction(activityId);
  } catch (error) {
    toast(publicError(error, "开启补偿活动失败"), "error");
  } finally {
    submit.disabled = false;
  }
}

function activateModule(name) {
  for (const tab of document.querySelectorAll(".module-tab")) {
    tab.classList.toggle("is-active", tab.dataset.module === name);
  }
  for (const panel of document.querySelectorAll(".module-panel")) {
    panel.classList.toggle("is-active", panel.id === `${name}Module`);
  }
}

async function refreshSelectedDetails() {
  if (
    !elements.reasonModal.hidden ||
    document.querySelector(".detail-panel button.is-confirming")
  ) {
    return;
  }
  const tasks = [];
  const activeElement = document.activeElement;
  if (
    state.lottery.selectedId &&
    !state.lottery.detailDirty &&
    !elements.lotteryDetailPanel.contains(activeElement)
  ) {
    tasks.push(
      loadLotteryDetail(state.lottery.selectedId, state.lottery.detailPage, {
        showLoading: false,
        scroll: false,
      }),
    );
  }
  if (
    state.compensation.selectedId &&
    !elements.compensationDetailPanel.contains(activeElement)
  ) {
    tasks.push(
      loadCompensationDetail(
        state.compensation.selectedId,
        state.compensation.detailPage,
        {
          showLoading: false,
          scroll: false,
        },
      ),
    );
  }
  const results = await Promise.all(tasks);
  if (results.some((result) => result === false)) {
    throw new Error("活动详情刷新失败");
  }
}

async function refreshAll({ quiet = false } = {}) {
  if (state.refreshRunning) {
    return;
  }
  state.refreshRunning = true;
  elements.refreshAllButton.disabled = true;
  if (!quiet) {
    setFreshness(false, "正在同步");
  }
  const results = await Promise.allSettled([
    refreshSummary(),
    refreshSettings(),
    refreshLotteryLive(),
    refreshCompensationLive(),
    refreshHistory("lottery"),
    refreshHistory("compensation"),
  ]);
  const failed = results.some((result) => result.status === "rejected");
  if (!failed) {
    const detailResults = await Promise.allSettled([refreshSelectedDetails()]);
    if (detailResults.some((result) => result.status === "rejected")) {
      setFreshness(true);
    } else {
      setFreshness(false);
    }
  } else {
    setFreshness(true);
    if (!quiet) {
      toast("部分数据刷新失败，页面可能显示旧数据", "error");
    }
  }
  elements.refreshAllButton.disabled = false;
  state.refreshRunning = false;
}

function bindEvents() {
  for (const tab of document.querySelectorAll(".module-tab")) {
    tab.addEventListener("click", () => activateModule(tab.dataset.module));
  }
  elements.refreshAllButton.addEventListener("click", () => refreshAll());
  elements.newapiTestButton.addEventListener("click", testNewApi);
  elements.lotteryCreateForm.addEventListener("submit", createLottery);
  elements.compensationOpenForm.addEventListener("submit", openCompensation);
  elements.settingsForm.addEventListener("submit", saveSettings);
  elements.settingsReloadButton.addEventListener("click", reloadSettingsFromRemote);
  elements.reasonModalForm.addEventListener("submit", (event) => {
    event.preventDefault();
    closeReasonModal(elements.reasonModalInput.value.trim());
  });
  elements.reasonModalCancel.addEventListener("click", () => closeReasonModal(null));
  elements.reasonModal.addEventListener("click", (event) => {
    if (event.target === elements.reasonModal) {
      closeReasonModal(null);
    }
  });
  for (const control of elements.settingsForm.querySelectorAll(
    "input, textarea",
  )) {
    control.addEventListener("input", markSettingsDirty);
    control.addEventListener("change", markSettingsDirty);
  }

  elements.lotteryHistoryFilter.addEventListener("submit", (event) => {
    event.preventDefault();
    state.lottery.historyGroup = elements.lotteryHistoryGroup.value.trim();
    state.lottery.historyPage = 1;
    refreshHistory("lottery").catch((error) =>
      toast(publicError(error, "读取抽奖历史失败"), "error"),
    );
  });
  elements.compensationHistoryFilter.addEventListener("submit", (event) => {
    event.preventDefault();
    state.compensation.historyGroup =
      elements.compensationHistoryGroup.value.trim();
    state.compensation.historyPage = 1;
    refreshHistory("compensation").catch((error) =>
      toast(publicError(error, "读取补偿历史失败"), "error"),
    );
  });
  elements.lotteryHistoryPrev.addEventListener("click", () => {
    if (state.lottery.historyPage > 1) {
      changeHistoryPage("lottery", state.lottery.historyPage - 1);
    }
  });
  elements.lotteryHistoryNext.addEventListener("click", () => {
    if (state.lottery.historyHasMore) {
      changeHistoryPage("lottery", state.lottery.historyPage + 1);
    }
  });
  elements.compensationHistoryPrev.addEventListener("click", () => {
    if (state.compensation.historyPage > 1) {
      changeHistoryPage(
        "compensation",
        state.compensation.historyPage - 1,
      );
    }
  });
  elements.compensationHistoryNext.addEventListener("click", () => {
    if (state.compensation.historyHasMore) {
      changeHistoryPage(
        "compensation",
        state.compensation.historyPage + 1,
      );
    }
  });
}

async function initialize() {
  bindEvents();
  if (!bridge) {
    setFreshness(true, "Page Bridge 未加载");
    toast("请从 AstrBot 插件管理页打开本页面", "error");
    return;
  }
  try {
    const context = await bridge.ready();
    document.documentElement.dataset.theme = context?.isDark ? "dark" : "light";
    bridge.onContext((nextContext) => {
      document.documentElement.dataset.theme = nextContext?.isDark
        ? "dark"
        : "light";
    });
  } catch (error) {
    setFreshness(true, "Page Bridge 初始化失败");
    toast(publicError(error, "页面桥接初始化失败"), "error");
    return;
  }
  loading(elements.lotteryLiveList);
  loading(elements.compensationLiveList);
  loading(elements.lotteryHistoryList);
  loading(elements.compensationHistoryList);
  await refreshAll();
  window.setInterval(() => refreshAll({ quiet: true }), AUTO_REFRESH_MS);
}

window.__campaignPageTest = {
  getSelectedLotteryId: () => state.lottery.selectedId,
  getSelectedCompensationId: () => state.compensation.selectedId,
  getSettingsBaseRevision: () => state.formBaseRevision,
  isSettingsDirty: () => state.settingsDirty,
  getLotteryHistoryPage: () => state.lottery.historyPage,
};

initialize();
