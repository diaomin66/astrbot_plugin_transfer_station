import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { JSDOM } from "jsdom";

const root = new URL("../../", import.meta.url);

const waitFor = async (window, predicate, label, timeoutMs = 3000) => {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    if (predicate()) {
      return;
    }
  }
  throw new Error(`Timed out waiting for ${label}`);
};

const settle = async (window) => {
  await waitFor(
    window,
    () =>
      window.__testBridgeState.pending === 0 &&
      !window.document.querySelector("#refreshAllButton").disabled,
    "campaign Page requests to settle",
  );
};

async function loadCampaignPage(bridge) {
  const html = await readFile(
    new URL("pages/campaigns/index.html", root),
    "utf8",
  );
  const script = await readFile(
    new URL("pages/campaigns/app.js", root),
    "utf8",
  );
  const dom = new JSDOM(html, {
    url: "https://astrbot.test/plugin-page",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  dom.window.HTMLElement.prototype.scrollIntoView = () => {};
  const bridgeState = { pending: 0 };
  const trackedBridge = { ...bridge };
  for (const method of ["ready", "apiGet", "apiPost"]) {
    const original = bridge[method].bind(bridge);
    trackedBridge[method] = async (...args) => {
      bridgeState.pending += 1;
      try {
        return await original(...args);
      } finally {
        bridgeState.pending -= 1;
      }
    };
  }
  dom.window.__testBridgeState = bridgeState;
  dom.window.AstrBotPluginPage = trackedBridge;
  dom.window.eval(script);
  await settle(dom.window);
  return dom;
}

function summary() {
  return {
    lottery: {
      activity_count: 0,
      active_count: 0,
      participant_count: 0,
      winner_count: 0,
      paid_winner_count: 0,
      manual_review_count: 0,
    },
    compensation: {
      activity_count: 0,
      active_count: 0,
      record_count: 0,
      paid_count: 0,
      manual_review_count: 0,
      used_raw_quota: "0",
    },
  };
}

function settings(revision = "revision-one") {
  return {
    revision,
    newapi_base_url: "https://newapi.example.com",
    newapi_user_id: "7",
    newapi_timeout_seconds: 10,
    newapi_verify_ssl: true,
    newapi_allow_insecure_http: false,
    newapi_username: "root",
    newapi_access_token_configured: true,
    newapi_password_configured: false,
    lottery_enabled: true,
    lottery_enabled_group_ids: [],
    compensation_enabled: true,
    compensation_enabled_group_ids: [],
  };
}

function emptyList() {
  return { items: [], total: 0, page: 1, page_size: 100, has_more: false };
}

function baseBridge(overrides = {}) {
  return {
    ready: async () => ({ isDark: false }),
    onContext: () => {},
    apiGet: async (endpoint, params = {}) => {
      if (endpoint === "campaigns/summary") {
        return summary();
      }
      if (endpoint === "campaigns/settings") {
        return settings();
      }
      if (
        endpoint === "campaigns/lotteries" ||
        endpoint === "campaigns/compensations"
      ) {
        return emptyList();
      }
      throw new Error(`unexpected GET ${endpoint} ${JSON.stringify(params)}`);
    },
    apiPost: async (endpoint) => {
      throw new Error(`unexpected POST ${endpoint}`);
    },
    ...overrides,
  };
}

test(
  "settings draft survives refresh and stale revision is submitted unchanged",
  { timeout: 10000 },
  async (t) => {
    let remoteSettings = settings("revision-one");
    const posts = [];
    const bridge = baseBridge({
      apiGet: async (endpoint) => {
        if (endpoint === "campaigns/settings") {
          return remoteSettings;
        }
        if (endpoint === "campaigns/summary") {
          return summary();
        }
        if (
          endpoint === "campaigns/lotteries" ||
          endpoint === "campaigns/compensations"
        ) {
          return emptyList();
        }
        throw new Error(`unexpected GET ${endpoint}`);
      },
      apiPost: async (endpoint, body) => {
        posts.push({ endpoint, body });
        if (endpoint === "campaigns/settings/save") {
          throw new Error("设置已被其他页面更新");
        }
        throw new Error(`unexpected POST ${endpoint}`);
      },
    });
    const dom = await loadCampaignPage(bridge);
    t.after(() => dom.window.close());

    const input = dom.window.document.querySelector("#settingsBaseUrl");
    input.value = "https://local-draft.example.com";
    input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    remoteSettings = settings("revision-two");
    dom.window.document.querySelector("#refreshAllButton").click();
    await settle(dom.window);

    assert.equal(input.value, "https://local-draft.example.com");
    assert.equal(
      dom.window.__campaignPageTest.getSettingsBaseRevision(),
      "revision-one",
    );
    assert.equal(dom.window.__campaignPageTest.isSettingsDirty(), true);
    assert.match(
      dom.window.document.querySelector("#settingsConflictState").textContent,
      /远端设置已变化/,
    );

    dom.window.document
      .querySelector("#settingsForm")
      .dispatchEvent(
        new dom.window.Event("submit", { bubbles: true, cancelable: true }),
      );
    await settle(dom.window);
    assert.equal(posts.at(-1).endpoint, "campaigns/settings/save");
    assert.equal(posts.at(-1).body.revision, "revision-one");

    dom.window.document.querySelector("#settingsReloadButton").click();
    await settle(dom.window);
    assert.equal(input.value, remoteSettings.newapi_base_url);
    assert.equal(
      dom.window.__campaignPageTest.getSettingsBaseRevision(),
      "revision-two",
    );
    assert.equal(dom.window.__campaignPageTest.isSettingsDirty(), false);
  },
);

test(
  "stale settings GET cannot overwrite a successful save",
  { timeout: 10000 },
  async (t) => {
    const initial = settings("revision-one");
    const saved = {
      ...settings("revision-saved"),
      newapi_base_url: "https://saved.example.com",
    };
    let settingsCalls = 0;
    let resolveStale;
    const staleResponse = new Promise((resolve) => {
      resolveStale = resolve;
    });
    const bridge = baseBridge({
      apiGet: async (endpoint) => {
        if (endpoint === "campaigns/settings") {
          settingsCalls += 1;
          return settingsCalls === 1 ? initial : staleResponse;
        }
        if (endpoint === "campaigns/summary") {
          return summary();
        }
        if (
          endpoint === "campaigns/lotteries" ||
          endpoint === "campaigns/compensations"
        ) {
          return emptyList();
        }
        throw new Error(`unexpected GET ${endpoint}`);
      },
      apiPost: async (endpoint) => {
        if (endpoint === "campaigns/settings/save") {
          return { message: "已保存", settings: saved, warning: "" };
        }
        throw new Error(`unexpected POST ${endpoint}`);
      },
    });
    const dom = await loadCampaignPage(bridge);
    t.after(() => dom.window.close());

    dom.window.document.querySelector("#refreshAllButton").click();
    await waitFor(dom.window, () => settingsCalls === 2, "stale settings request");
    const input = dom.window.document.querySelector("#settingsBaseUrl");
    input.value = saved.newapi_base_url;
    input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    dom.window.document
      .querySelector("#settingsForm")
      .dispatchEvent(
        new dom.window.Event("submit", { bubbles: true, cancelable: true }),
      );
    await waitFor(
      dom.window,
      () =>
        dom.window.__campaignPageTest.getSettingsBaseRevision() === saved.revision,
      "saved settings revision",
    );
    assert.equal(input.value, saved.newapi_base_url);
    assert.equal(
      dom.window.__campaignPageTest.getSettingsBaseRevision(),
      saved.revision,
    );

    resolveStale(initial);
    await settle(dom.window);
    assert.equal(input.value, saved.newapi_base_url);
    assert.equal(
      dom.window.__campaignPageTest.getSettingsBaseRevision(),
      saved.revision,
    );
  },
);

test(
  "settings save disables every form control and submits an immutable snapshot",
  { timeout: 10000 },
  async (t) => {
    let resolveSave;
    let submitted;
    const pendingSave = new Promise((resolve) => {
      resolveSave = resolve;
    });
    const saved = {
      ...settings("revision-saved"),
      newapi_base_url: "https://saved.example.com",
    };
    let remoteSettings = settings();
    const bridge = baseBridge({
      apiGet: async (endpoint) => {
        if (endpoint === "campaigns/settings") {
          return remoteSettings;
        }
        if (endpoint === "campaigns/summary") {
          return summary();
        }
        if (
          endpoint === "campaigns/lotteries" ||
          endpoint === "campaigns/compensations"
        ) {
          return emptyList();
        }
        throw new Error(`unexpected GET ${endpoint}`);
      },
      apiPost: async (endpoint, body) => {
        if (endpoint === "campaigns/settings/save") {
          submitted = JSON.parse(JSON.stringify(body));
          return pendingSave;
        }
        throw new Error(`unexpected POST ${endpoint}`);
      },
    });
    const dom = await loadCampaignPage(bridge);
    t.after(() => dom.window.close());
    const form = dom.window.document.querySelector("#settingsForm");
    const input = dom.window.document.querySelector("#settingsBaseUrl");
    input.value = saved.newapi_base_url;
    input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));

    form.dispatchEvent(
      new dom.window.Event("submit", { bubbles: true, cancelable: true }),
    );
    await waitFor(dom.window, () => submitted, "settings save request");
    assert.equal([...form.elements].every((control) => control.disabled), true);

    input.value = "https://late-edit.example.com";
    input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    assert.equal(
      submitted.settings.newapi_base_url,
      "https://saved.example.com",
    );
    assert.equal(submitted.settings.newapi_user_id, "7");

    remoteSettings = saved;
    resolveSave({ message: "已保存", settings: saved, warning: "" });
    await settle(dom.window);
    assert.equal([...form.elements].some((control) => control.disabled), false);
    assert.equal(input.value, saved.newapi_base_url);
  },
);

test(
  "dirty lottery draft blocks destructive actions and custom reason modal submits revision",
  { timeout: 10000 },
  async (t) => {
    const activity = {
      id: "9007199254740993555",
      revision: "7",
      group_id: "100",
      title: "草稿活动",
      description: "原始说明",
      keyword: "参与抽奖",
      status: "draft",
      start_at: "2026-07-30T00:00:00+00:00",
      draw_at: "2026-07-30T02:00:00+00:00",
      claim_duration_seconds: 86400,
      participant_count: 0,
      winner_count: 0,
      paid_winner_count: 0,
      manual_review_count: 0,
    };
    const detail = {
      activity,
      prizes: [],
      participant_count: 0,
      eligible_count: 0,
      winners: [],
      winner_total: 0,
      winner_page: 1,
      winner_page_size: 20,
      winner_has_more: false,
    };
    const posts = [];
    const bridge = baseBridge({
      apiGet: async (endpoint, params = {}) => {
        if (endpoint === "campaigns/summary") {
          return summary();
        }
        if (endpoint === "campaigns/settings") {
          return settings();
        }
        if (endpoint === "campaigns/lotteries") {
          return params.scope === "active"
            ? { items: [activity], total: 1, has_more: false }
            : emptyList();
        }
        if (endpoint === "campaigns/compensations") {
          return emptyList();
        }
        if (endpoint === "campaigns/lotteries/detail") {
          return detail;
        }
        throw new Error(`unexpected GET ${endpoint}`);
      },
      apiPost: async (endpoint, body) => {
        posts.push({ endpoint, body });
        if (endpoint === "campaigns/lotteries/update") {
          throw new Error("标题格式错误");
        }
        if (endpoint === "campaigns/lotteries/cancel") {
          return { message: "已取消" };
        }
        throw new Error(`unexpected POST ${endpoint}`);
      },
    });
    const dom = await loadCampaignPage(bridge);
    t.after(() => dom.window.close());
    dom.window.document
      .querySelector("#lotteryLiveList .activity-card button")
      .click();
    await settle(dom.window);

    const draftForm = dom.window.document.querySelector(
      "#lotteryDetailPanel .detail-columns .subpanel form",
    );
    const title = draftForm.querySelector("input");
    title.value = "本地未保存标题";
    title.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    const publish = [...dom.window.document.querySelectorAll(
      "#lotteryDetailPanel .detail-header__actions button",
    )].find((button) => button.textContent === "发布活动");
    publish.click();
    publish.click();
    await settle(dom.window);
    assert.equal(
      posts.some((item) => item.endpoint === "campaigns/lotteries/publish"),
      false,
    );
    assert.equal(title.value, "本地未保存标题");

    draftForm.dispatchEvent(
      new dom.window.Event("submit", { bubbles: true, cancelable: true }),
    );
    await settle(dom.window);
    assert.equal(title.value, "本地未保存标题");
    assert.equal(posts.at(-1).endpoint, "campaigns/lotteries/update");

    draftForm
      .querySelector("button[type='button']")
      .dispatchEvent(new dom.window.Event("click", { bubbles: true }));
    await settle(dom.window);
    const prizeName = dom.window.document.querySelector(
      "#lotteryDetailPanel .prize-list + form input",
    );
    prizeName.value = "一等奖";
    prizeName.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    dom.window.document.querySelector("#refreshAllButton").click();
    await settle(dom.window);
    assert.equal(prizeName.value, "一等奖");

    dom.window.document
      .querySelector(
        "#lotteryDetailPanel .detail-columns .subpanel form button[type='button']",
      )
      .click();
    await settle(dom.window);
    const cancel = [...dom.window.document.querySelectorAll(
      "#lotteryDetailPanel .detail-header__actions button",
    )].find((button) => button.textContent === "取消活动");
    cancel.click();
    cancel.click();
    await settle(dom.window);
    const modal = dom.window.document.querySelector("#reasonModal");
    assert.equal(modal.hidden, false);
    dom.window.document.querySelector("#reasonModalInput").value = "运营取消";
    dom.window.document
      .querySelector("#reasonModalForm")
      .dispatchEvent(
        new dom.window.Event("submit", { bubbles: true, cancelable: true }),
      );
    await settle(dom.window);
    assert.deepEqual(JSON.parse(JSON.stringify(posts.at(-1))), {
      endpoint: "campaigns/lotteries/cancel",
      body: {
        activity_id: activity.id,
        revision: activity.revision,
        reason: "运营取消",
      },
    });
  },
);

test(
  "lottery draft and prize forms disable every control during their requests",
  { timeout: 10000 },
  async (t) => {
    const activity = {
      id: "81",
      revision: "1",
      group_id: "100",
      title: "并发编辑测试",
      description: "",
      keyword: "参与抽奖",
      status: "draft",
      start_at: "2026-07-30T00:00:00+00:00",
      draw_at: "2026-07-30T02:00:00+00:00",
      claim_duration_seconds: 86400,
      participant_count: 0,
      winner_count: 0,
      paid_winner_count: 0,
      manual_review_count: 0,
    };
    const detail = {
      activity,
      prizes: [],
      participant_count: 0,
      eligible_count: 0,
      winners: [],
      winner_total: 0,
      winner_page: 1,
      winner_page_size: 20,
      winner_has_more: false,
    };
    let resolveUpdate;
    let resolvePrize;
    let updateRequested = false;
    let prizeRequested = false;
    const bridge = baseBridge({
      apiGet: async (endpoint, params = {}) => {
        if (endpoint === "campaigns/summary") {
          return summary();
        }
        if (endpoint === "campaigns/settings") {
          return settings();
        }
        if (endpoint === "campaigns/lotteries") {
          return params.scope === "active"
            ? { items: [activity], total: 1, has_more: false }
            : emptyList();
        }
        if (endpoint === "campaigns/compensations") {
          return emptyList();
        }
        if (endpoint === "campaigns/lotteries/detail") {
          return detail;
        }
        throw new Error(`unexpected GET ${endpoint}`);
      },
      apiPost: async (endpoint) => {
        if (endpoint === "campaigns/lotteries/update") {
          updateRequested = true;
          return new Promise((resolve) => {
            resolveUpdate = resolve;
          });
        }
        if (endpoint === "campaigns/lotteries/prizes/add") {
          prizeRequested = true;
          return new Promise((resolve) => {
            resolvePrize = resolve;
          });
        }
        throw new Error(`unexpected POST ${endpoint}`);
      },
    });
    const dom = await loadCampaignPage(bridge);
    t.after(() => dom.window.close());
    dom.window.document
      .querySelector("#lotteryLiveList .activity-card button")
      .click();
    await settle(dom.window);

    const draftForm = dom.window.document.querySelector(
      "#lotteryDetailPanel .detail-columns .subpanel form",
    );
    draftForm.dispatchEvent(
      new dom.window.Event("submit", { bubbles: true, cancelable: true }),
    );
    await waitFor(dom.window, () => updateRequested, "lottery draft update");
    assert.equal(
      [...draftForm.elements].every((control) => control.disabled),
      true,
    );
    resolveUpdate({ message: "已更新" });
    await settle(dom.window);

    const prizeForm = dom.window.document.querySelector(
      "#lotteryDetailPanel .prize-list + form",
    );
    const prizeName = prizeForm.querySelector("input");
    prizeName.value = "一等奖";
    prizeName.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    prizeForm.dispatchEvent(
      new dom.window.Event("submit", { bubbles: true, cancelable: true }),
    );
    await waitFor(dom.window, () => prizeRequested, "lottery prize add");
    assert.equal(
      [...prizeForm.elements].every((control) => control.disabled),
      true,
    );
    resolvePrize({ message: "已添加" });
    await settle(dom.window);
  },
);

test(
  "history pagination failure restores the previous page",
  { timeout: 10000 },
  async (t) => {
    const historyItem = {
      id: "77",
      revision: "1",
      group_id: "100",
      title: "历史抽奖",
      status: "completed",
      draw_at: "2026-07-30T02:00:00+00:00",
      drawn_at: "2026-07-30T02:00:00+00:00",
    };
    const bridge = baseBridge({
      apiGet: async (endpoint, params = {}) => {
        if (endpoint === "campaigns/summary") {
          return summary();
        }
        if (endpoint === "campaigns/settings") {
          return settings();
        }
        if (endpoint === "campaigns/lotteries") {
          if (params.scope === "active") {
            return emptyList();
          }
          if (Number(params.page) === 2) {
            throw new Error("历史分页暂时不可用");
          }
          return {
            items: [historyItem],
            total: 13,
            page: 1,
            page_size: 12,
            has_more: true,
          };
        }
        if (endpoint === "campaigns/compensations") {
          return emptyList();
        }
        throw new Error(`unexpected GET ${endpoint}`);
      },
    });
    const dom = await loadCampaignPage(bridge);
    t.after(() => dom.window.close());

    dom.window.document.querySelector("#lotteryHistoryNext").click();
    await settle(dom.window);

    assert.equal(dom.window.__campaignPageTest.getLotteryHistoryPage(), 1);
    assert.match(
      dom.window.document.querySelector("#lotteryHistoryPage").textContent,
      /第 1 页/,
    );
    assert.match(
      dom.window.document.querySelector("#lotteryHistoryList").textContent,
      /历史抽奖/,
    );
  },
);

test(
  "stale history failure cannot roll back a newer filter request",
  { timeout: 10000 },
  async (t) => {
    let rejectPageThree;
    let pageThreeRequested = false;
    const pageThree = new Promise((_resolve, reject) => {
      rejectPageThree = reject;
    });
    const historyItem = (page, title) => ({
      id: String(100 + page),
      revision: "1",
      group_id: "100",
      title,
      status: "completed",
      draw_at: "2026-07-30T02:00:00+00:00",
      drawn_at: "2026-07-30T02:00:00+00:00",
    });
    const bridge = baseBridge({
      apiGet: async (endpoint, params = {}) => {
        if (endpoint === "campaigns/summary") {
          return summary();
        }
        if (endpoint === "campaigns/settings") {
          return settings();
        }
        if (endpoint === "campaigns/lotteries") {
          if (params.scope === "active") {
            return emptyList();
          }
          if (Number(params.page) === 3) {
            pageThreeRequested = true;
            return pageThree;
          }
          const page = Number(params.page || 1);
          return {
            items: [
              historyItem(
                page,
                params.group_id ? "筛选后的第一页" : `历史第 ${page} 页`,
              ),
            ],
            total: 36,
            page,
            page_size: 12,
            has_more: page < 3,
          };
        }
        if (endpoint === "campaigns/compensations") {
          return emptyList();
        }
        throw new Error(`unexpected GET ${endpoint}`);
      },
    });
    const dom = await loadCampaignPage(bridge);
    t.after(() => dom.window.close());

    dom.window.document.querySelector("#lotteryHistoryNext").click();
    await settle(dom.window);
    assert.equal(dom.window.__campaignPageTest.getLotteryHistoryPage(), 2);

    dom.window.document.querySelector("#lotteryHistoryNext").click();
    await waitFor(dom.window, () => pageThreeRequested, "third history page");
    dom.window.document.querySelector("#lotteryHistoryGroup").value = "100";
    dom.window.document
      .querySelector("#lotteryHistoryFilter")
      .dispatchEvent(
        new dom.window.Event("submit", { bubbles: true, cancelable: true }),
      );
    await waitFor(
      dom.window,
      () =>
        dom.window.__campaignPageTest.getLotteryHistoryPage() === 1 &&
        /筛选后的第一页/.test(
          dom.window.document.querySelector("#lotteryHistoryList").textContent,
        ),
      "newer filtered history",
    );

    rejectPageThree(new Error("stale page failed"));
    await settle(dom.window);
    assert.equal(dom.window.__campaignPageTest.getLotteryHistoryPage(), 1);
    assert.match(
      dom.window.document.querySelector("#lotteryHistoryList").textContent,
      /筛选后的第一页/,
    );
  },
);

test(
  "all current activities render, large IDs remain strings, and stale detail is ignored",
  { timeout: 10000 },
  async (t) => {
    const firstId = "9007199254740993123";
    const secondId = "9007199254740993987";
    const activities = Array.from({ length: 60 }, (_, index) => ({
      id:
        index === 0
          ? firstId
          : index === 1
            ? secondId
            : `90071992547410${String(index).padStart(3, "0")}`,
      revision: "1",
      group_id: "100",
      title: `活动 ${index + 1}`,
      status: "open",
      start_at: "2026-07-30T00:00:00+00:00",
      draw_at: "2026-07-30T02:00:00+00:00",
      participant_count: index,
      winner_count: 0,
      paid_winner_count: 0,
      manual_review_count: 0,
    }));
    let resolveFirstDetail;
    let firstDetailRequested = false;
    const firstDetail = new Promise((resolve) => {
      resolveFirstDetail = resolve;
    });
    const detailFor = (id, title) => ({
      activity: {
        ...activities.find((item) => item.id === id),
        title,
        description: "",
        keyword: "参与抽奖",
        claim_duration_seconds: 86400,
      },
      prizes: [],
      participant_count: 0,
      eligible_count: 0,
      winners: [],
      winner_total: 0,
      winner_page: 1,
      winner_page_size: 20,
      winner_has_more: false,
    });
    const bridge = baseBridge({
      apiGet: async (endpoint, params = {}) => {
        if (endpoint === "campaigns/summary") {
          return summary();
        }
        if (endpoint === "campaigns/settings") {
          return settings();
        }
        if (endpoint === "campaigns/lotteries") {
          return params.scope === "active"
            ? {
                items: activities,
                total: activities.length,
                has_more: false,
              }
            : emptyList();
        }
        if (endpoint === "campaigns/compensations") {
          return emptyList();
        }
        if (endpoint === "campaigns/lotteries/detail") {
          if (params.activity_id === firstId) {
            firstDetailRequested = true;
            return firstDetail;
          }
          if (params.activity_id === secondId) {
            return detailFor(secondId, "第二个活动");
          }
        }
        throw new Error(`unexpected GET ${endpoint}`);
      },
    });
    const dom = await loadCampaignPage(bridge);
    t.after(() => dom.window.close());

    const cards = [
      ...dom.window.document.querySelectorAll("#lotteryLiveList .activity-card"),
    ];
    assert.equal(cards.length, 60);
    assert.equal(cards[0].dataset.activityId, firstId);
    assert.equal(cards[1].dataset.activityId, secondId);

    cards[0].querySelector("button").click();
    await waitFor(
      dom.window,
      () => firstDetailRequested,
      "first lottery detail request",
    );
    cards[1].querySelector("button").click();
    await waitFor(
      dom.window,
      () =>
        dom.window.document.querySelector("#lotteryDetailPanel").dataset
          .activityId === secondId,
      "second lottery detail",
    );
    assert.equal(
      dom.window.document.querySelector("#lotteryDetailPanel").dataset.activityId,
      secondId,
    );
    assert.equal(
      dom.window.__campaignPageTest.getSelectedLotteryId(),
      secondId,
    );

    resolveFirstDetail(detailFor(firstId, "过期响应"));
    await settle(dom.window);
    assert.equal(
      dom.window.document.querySelector("#lotteryDetailPanel").dataset.activityId,
      secondId,
    );
    assert.match(
      dom.window.document.querySelector("#lotteryDetailPanel h3").textContent,
      /第二个活动/,
    );
  },
);

test(
  "an in-flight lottery action cannot reopen a previously selected activity",
  { timeout: 10000 },
  async (t) => {
    const firstId = "91";
    const secondId = "92";
    const activities = [firstId, secondId].map((id) => ({
      id,
      revision: "1",
      group_id: "100",
      title: `活动 ${id}`,
      description: "",
      keyword: "参与抽奖",
      status: "draft",
      start_at: "2026-07-30T00:00:00+00:00",
      draw_at: "2026-07-30T02:00:00+00:00",
      claim_duration_seconds: 86400,
      participant_count: 0,
      winner_count: 0,
      paid_winner_count: 0,
      manual_review_count: 0,
    }));
    const detailFor = (activity) => ({
      activity,
      prizes: [],
      participant_count: 0,
      eligible_count: 0,
      winners: [],
      winner_total: 0,
      winner_page: 1,
      winner_page_size: 20,
      winner_has_more: false,
    });
    let resolveUpdate;
    let updateRequested = false;
    const bridge = baseBridge({
      apiGet: async (endpoint, params = {}) => {
        if (endpoint === "campaigns/summary") {
          return summary();
        }
        if (endpoint === "campaigns/settings") {
          return settings();
        }
        if (endpoint === "campaigns/lotteries") {
          return params.scope === "active"
            ? { items: activities, total: 2, has_more: false }
            : emptyList();
        }
        if (endpoint === "campaigns/compensations") {
          return emptyList();
        }
        if (endpoint === "campaigns/lotteries/detail") {
          return detailFor(
            activities.find((activity) => activity.id === params.activity_id),
          );
        }
        throw new Error(`unexpected GET ${endpoint}`);
      },
      apiPost: async (endpoint) => {
        if (endpoint === "campaigns/lotteries/update") {
          updateRequested = true;
          return new Promise((resolve) => {
            resolveUpdate = resolve;
          });
        }
        throw new Error(`unexpected POST ${endpoint}`);
      },
    });
    const dom = await loadCampaignPage(bridge);
    t.after(() => dom.window.close());
    const cards = [
      ...dom.window.document.querySelectorAll("#lotteryLiveList .activity-card"),
    ];
    cards[0].querySelector("button").click();
    await settle(dom.window);
    const firstForm = dom.window.document.querySelector(
      "#lotteryDetailPanel .detail-columns .subpanel form",
    );
    firstForm.dispatchEvent(
      new dom.window.Event("submit", { bubbles: true, cancelable: true }),
    );
    await waitFor(dom.window, () => updateRequested, "first activity update");

    cards[1].querySelector("button").click();
    await waitFor(
      dom.window,
      () =>
        dom.window.__campaignPageTest.getSelectedLotteryId() === secondId &&
        dom.window.document.querySelector("#lotteryDetailPanel").dataset
          .activityId === secondId,
      "second activity selection",
    );
    resolveUpdate({ message: "已更新" });
    await settle(dom.window);

    assert.equal(
      dom.window.__campaignPageTest.getSelectedLotteryId(),
      secondId,
    );
    assert.equal(
      dom.window.document.querySelector("#lotteryDetailPanel").dataset
        .activityId,
      secondId,
    );
  },
);

test(
  "manual review uses the compensation activity ID and serial without numeric coercion",
  { timeout: 10000 },
  async (t) => {
    const activityId = "9007199254740993777";
    const posts = [];
    const activity = {
      id: activityId,
      group_id: "100",
      title: "服务补偿",
      status: "open",
      per_display_amount: "5",
      per_raw_quota: "2500000",
      total_display_amount: "100",
      total_raw_quota: "50000000",
      used_raw_quota: "2500000",
      display_type: "USD",
      start_at: "2026-07-30T00:00:00+00:00",
      end_at: "2026-07-30T02:00:00+00:00",
      claim_count: 1,
      paid_count: 0,
      manual_review_count: 1,
    };
    const bridge = baseBridge({
      apiGet: async (endpoint, params = {}) => {
        if (endpoint === "campaigns/summary") {
          return summary();
        }
        if (endpoint === "campaigns/settings") {
          return settings();
        }
        if (endpoint === "campaigns/lotteries") {
          return emptyList();
        }
        if (endpoint === "campaigns/compensations") {
          return params.scope === "active"
            ? { items: [activity], total: 1, has_more: false }
            : emptyList();
        }
        if (endpoint === "campaigns/compensations/detail") {
          assert.equal(params.activity_id, activityId);
          return {
            activity,
            records: [
              {
                id: "9223372036854775000",
                serial: "C202607300001",
                qq_id: "200",
                api_user_id: "9007199254740993999",
                api_username: "alice",
                raw_quota: "2500000",
                display_amount: "5",
                status: "manual_review",
                updated_at: "2026-07-30T00:10:00+00:00",
              },
            ],
            record_total: 1,
            record_page: 1,
            record_page_size: 20,
            record_has_more: false,
          };
        }
        throw new Error(`unexpected GET ${endpoint}`);
      },
      apiPost: async (endpoint, body) => {
        posts.push({ endpoint, body });
        if (endpoint === "campaigns/compensations/review") {
          return { message: "核查完成" };
        }
        throw new Error(`unexpected POST ${endpoint}`);
      },
    });
    const dom = await loadCampaignPage(bridge);
    t.after(() => dom.window.close());
    let scrollCount = 0;
    dom.window.HTMLElement.prototype.scrollIntoView = () => {
      scrollCount += 1;
    };

    dom.window.document
      .querySelector("#compensationLiveList .activity-card button")
      .click();
    await settle(dom.window);
    const reviewButton = [
      ...dom.window.document.querySelectorAll(
        "#compensationDetailPanel .table-actions button",
      ),
    ].find((button) => button.textContent === "确认到账");
    assert.ok(reviewButton);
    reviewButton.click();
    dom.window.document.querySelector("#refreshAllButton").click();
    await settle(dom.window);
    assert.equal(reviewButton.isConnected, true);
    assert.equal(reviewButton.classList.contains("is-confirming"), true);
    assert.equal(scrollCount, 1);
    reviewButton.click();
    await settle(dom.window);
    assert.deepEqual(JSON.parse(JSON.stringify(posts.at(-1))), {
      endpoint: "campaigns/compensations/review",
      body: {
        activity_id: activityId,
        serial: "C202607300001",
        success: true,
      },
    });
  },
);
