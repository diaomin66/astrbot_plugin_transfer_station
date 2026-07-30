import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { JSDOM } from "jsdom";

const root = new URL("../../", import.meta.url);

const settle = async (window, timeoutMs = 3000) => {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    if (
      window.__testBridgeState.pending === 0 &&
      !window.document.querySelector("#refreshButton").disabled
    ) {
      return;
    }
  }
  throw new Error("Timed out waiting for gift Page requests to settle");
};

async function loadPage(relativeHtml, relativeScript, bridge) {
  const html = await readFile(new URL(relativeHtml, root), "utf8");
  const script = await readFile(new URL(relativeScript, root), "utf8");
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

test(
  "gift code row stays aligned and two-click delete calls bridge endpoint",
  { timeout: 10000 },
  async (t) => {
  let items = [
    {
      id: "7",
      code: "CODE-007",
      created_at: "2026-07-30T00:00:00+00:00",
    },
  ];
  const posts = [];
  const bridge = {
    ready: async () => ({ isDark: false }),
    onContext: () => {},
    apiGet: async (endpoint) => {
      if (endpoint === "summary") {
        return {
          available_codes: items.length,
          claimed_users: 0,
          pending_newcomers: 0,
          known_users: 0,
          today_newcomers: 0,
          gift_manual_reviews: 0,
        };
      }
      if (endpoint === "codes") {
        return { items, total: items.length, has_more: false };
      }
      if (endpoint === "claims" || endpoint === "gift-reviews") {
        return { items: [], total: 0, has_more: false };
      }
      throw new Error(`unexpected GET ${endpoint}`);
    },
    apiPost: async (endpoint, body) => {
      posts.push({ endpoint, body });
      if (endpoint === "codes/delete") {
        items = [];
        return { deleted_id: body.id };
      }
      throw new Error(`unexpected POST ${endpoint}`);
    },
  };
  const dom = await loadPage(
    "pages/gift_codes/index.html",
    "pages/gift_codes/app.js",
    bridge,
  );
  t.after(() => dom.window.close());
  const row = dom.window.document.querySelector("#codeRows tr");
  assert.ok(row);
  assert.equal(row.querySelectorAll("td").length, 4);
  assert.equal(row.children[0].textContent, "7");
  assert.match(row.children[1].textContent, /^C.*07$/);

  const deleteButton = [...row.querySelectorAll("button")].find(
    (button) => button.textContent === "删除",
  );
  deleteButton.click();
  deleteButton.click();
  await settle(dom.window);
  assert.deepEqual(JSON.parse(JSON.stringify(posts.at(-1))), {
    endpoint: "codes/delete",
    body: { id: "7" },
  });
  assert.equal(dom.window.document.querySelectorAll("#codeRows tr").length, 0);
  },
);
