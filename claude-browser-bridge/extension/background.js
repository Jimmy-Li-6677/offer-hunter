// Claude Browser Bridge - extension service worker.
// Connects to the local MCP server (ws://127.0.0.1:<port>) and executes browser
// commands using the Chrome DevTools Protocol via chrome.debugger.

const DEFAULTS = {
  token: "",
  port: 8765,
  enabled: true,
  allowAllSites: false,
  allowedSites: [
    "linkedin.com",
    "greenhouse.io",
    "ashbyhq.com",
    "lever.co",
    "myworkdayjobs.com",
    "icims.com",
    "amazon.jobs",
    "careers.microsoft.com",
    "google.com",
    "spacex.com",
    "example.com"
  ]
};

let ws = null;
let pingTimer = null;
let reconnectTimer = null;
const attachedTabs = new Set();

async function getConfig() {
  const stored = await chrome.storage.local.get(Object.keys(DEFAULTS));
  return { ...DEFAULTS, ...stored };
}

// ---------- badge / status ----------
function setBadge(state) {
  const map = {
    connected: { text: "ON", color: "#1a7f37" },
    busy: { text: "▶", color: "#bf8700" },
    off: { text: "", color: "#888888" },
    error: { text: "!", color: "#cf222e" }
  };
  const s = map[state] ?? map.off;
  chrome.action.setBadgeText({ text: s.text });
  chrome.action.setBadgeBackgroundColor({ color: s.color });
  chrome.storage.local.set({ status: state });
}

// ---------- connection ----------
async function connect() {
  const cfg = await getConfig();
  if (!cfg.enabled || !cfg.token) {
    setBadge(cfg.token ? "off" : "error");
    return;
  }
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

  try {
    ws = new WebSocket(`ws://127.0.0.1:${cfg.port}/?token=${encodeURIComponent(cfg.token)}`);
  } catch (e) {
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    setBadge("connected");
    clearInterval(pingTimer);
    // Keep the MV3 service worker alive while connected.
    pingTimer = setInterval(() => ws?.readyState === WebSocket.OPEN && ws.send(JSON.stringify({ type: "ping" })), 20000);
  };

  ws.onmessage = async (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }
    if (msg.type === "pong" || msg.id === undefined) return;
    setBadge("busy");
    try {
      const result = await handleCommand(msg.cmd, msg.args ?? {});
      ws?.send(JSON.stringify({ id: msg.id, ok: true, result }));
    } catch (err) {
      ws?.send(JSON.stringify({ id: msg.id, ok: false, error: String(err?.message ?? err) }));
    } finally {
      if (ws?.readyState === WebSocket.OPEN) setBadge("connected");
    }
  };

  ws.onclose = () => {
    clearInterval(pingTimer);
    ws = null;
    setBadge("off");
    scheduleReconnect();
  };
  ws.onerror = () => {};
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, 3000);
}

async function disconnect() {
  clearTimeout(reconnectTimer);
  clearInterval(pingTimer);
  ws?.close();
  ws = null;
  for (const tabId of [...attachedTabs]) {
    try {
      await chrome.debugger.detach({ tabId });
    } catch {}
  }
  attachedTabs.clear();
  setBadge("off");
}

chrome.runtime.onInstalled.addListener(connect);
chrome.runtime.onStartup.addListener(connect);
chrome.alarms.create("reconnect", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((a) => a.name === "reconnect" && connect());
chrome.storage.onChanged.addListener(async (changes) => {
  if (changes.enabled || changes.token || changes.port) {
    await disconnect();
    connect();
  }
});
chrome.debugger.onDetach.addListener((source) => attachedTabs.delete(source.tabId));
chrome.tabs.onRemoved.addListener((tabId) => attachedTabs.delete(tabId));
connect();

// ---------- helpers ----------
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function assertAllowed(urlString) {
  const cfg = await getConfig();
  if (cfg.allowAllSites) return;
  let host;
  try {
    host = new URL(urlString).hostname;
  } catch {
    throw new Error(`Invalid URL: ${urlString}`);
  }
  const ok = cfg.allowedSites.some((s) => host === s || host.endsWith("." + s));
  if (!ok) {
    throw new Error(`Site not allowed: ${host}. Add it in the Claude Browser Bridge options (allowed sites) if you trust it.`);
  }
}

async function getTab(tabId) {
  const tab = await chrome.tabs.get(tabId);
  await assertAllowed(tab.url || tab.pendingUrl || "");
  return tab;
}

async function cdp(tabId, method, params = {}) {
  await getTab(tabId);
  if (!attachedTabs.has(tabId)) {
    await chrome.debugger.attach({ tabId }, "1.3");
    attachedTabs.add(tabId);
  }
  return chrome.debugger.sendCommand({ tabId }, method, params);
}


// Helpers injected before every in-page function: search same-origin iframes too,
// and convert element rects inside frames to top-level viewport coordinates.
const PAGE_HELPERS = `
function __cbbDocs() {
  const out = [document];
  const walk = (d) => {
    for (const f of d.querySelectorAll("iframe, frame")) {
      try { const cd = f.contentDocument; if (cd) { out.push(cd); walk(cd); } } catch (e) {}
    }
  };
  walk(document);
  return out;
}
function __cbbFind(ref) {
  for (const d of __cbbDocs()) {
    const el = d.querySelector('[data-cbb-ref="' + ref + '"]');
    if (el) return el;
  }
  return null;
}
function __cbbOffset(el) {
  let x = 0, y = 0;
  let w = el.ownerDocument.defaultView;
  while (w && w !== window) {
    const fe = w.frameElement;
    if (!fe) break;
    const r = fe.getBoundingClientRect();
    x += r.left + fe.clientLeft;
    y += r.top + fe.clientTop;
    w = w.parent;
  }
  return { x, y };
}
`;

// Run a function inside the page and return its (JSON) result.
async function evalInPage(tabId, fn, args = {}) {
  const expression = `(() => { ${PAGE_HELPERS}; return (${fn.toString()})(${JSON.stringify(args)}); })()`;
  const res = await cdp(tabId, "Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true, userGesture: true });
  if (res.exceptionDetails) {
    throw new Error(res.exceptionDetails.exception?.description || res.exceptionDetails.text || "Page script error");
  }
  return res.result?.value;
}

function waitForLoad(tabId, timeoutMs = 30000) {
  return new Promise((resolve) => {
    const timer = setTimeout(done, timeoutMs);
    function listener(id, info) {
      if (id === tabId && info.status === "complete") done();
    }
    function done() {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

// Scroll an element into view, let the layout settle, then return a fresh center point.
async function settledCenter(tabId, ref) {
  await evalInPage(tabId, pageCenterOf, { ref });
  await sleep(350);
  return evalInPage(tabId, pageCenterOf, { ref });
}

async function mouseClick(tabId, x, y) {
  await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
  await cdp(tabId, "Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
  await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
}

const KEYS = {
  Enter: { code: "Enter", keyCode: 13, text: "\r" },
  Tab: { code: "Tab", keyCode: 9 },
  Escape: { code: "Escape", keyCode: 27 },
  Backspace: { code: "Backspace", keyCode: 8 },
  Delete: { code: "Delete", keyCode: 46 },
  ArrowDown: { code: "ArrowDown", keyCode: 40 },
  ArrowUp: { code: "ArrowUp", keyCode: 38 },
  ArrowLeft: { code: "ArrowLeft", keyCode: 37 },
  ArrowRight: { code: "ArrowRight", keyCode: 39 },
  Home: { code: "Home", keyCode: 36 },
  End: { code: "End", keyCode: 35 },
  Space: { code: "Space", keyCode: 32, text: " ", key: " " }
};

async function pressKey(tabId, key) {
  const k = KEYS[key] ?? { code: key, keyCode: key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0, text: key.length === 1 ? key : undefined };
  const base = { key: k.key ?? key, code: k.code, windowsVirtualKeyCode: k.keyCode, nativeVirtualKeyCode: k.keyCode };
  await cdp(tabId, "Input.dispatchKeyEvent", { type: "keyDown", ...base, text: k.text });
  await cdp(tabId, "Input.dispatchKeyEvent", { type: "keyUp", ...base });
}

// ---------- in-page functions (serialized and run inside the tab) ----------

function pageReadElements({ visibleOnly, query }) {
  const SELECTOR = [
    "a[href]", "button", "input", "select", "textarea", "summary", "[contenteditable='true']",
    "[role=button]", "[role=link]", "[role=combobox]", "[role=checkbox]", "[role=radio]", "[role=option]",
    "[role=tab]", "[role=menuitem]", "[role=switch]", "[role=textbox]", "[role=listbox]", "[role=searchbox]"
  ].join(",");
  window.__cbbCounter = window.__cbbCounter || 0;

  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const textOf = (id) => clean(document.getElementById(id)?.innerText);

  function labelFor(el) {
    const aria = el.getAttribute("aria-label");
    if (aria) return clean(aria);
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      const t = by.split(/\s+/).map(textOf).filter(Boolean).join(" ");
      if (t) return t;
    }
    if (el.id) {
      const l = el.ownerDocument.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return clean(l.innerText);
    }
    const wrap = el.closest("label");
    if (wrap) return clean(wrap.innerText);
    if (el.placeholder) return clean(el.placeholder);
    if (el.title) return clean(el.title);
    const t = clean(el.innerText || el.value || "");
    if (t) return t.slice(0, 100);
    return clean(el.name || "");
  }

  function roleOf(el) {
    const r = el.getAttribute("role");
    if (r) return r;
    const tag = el.tagName.toLowerCase();
    if (tag === "a") return "link";
    if (tag === "select") return "select";
    if (tag === "textarea") return "textarea";
    if (tag === "input") return `input:${(el.type || "text").toLowerCase()}`;
    return tag;
  }

  function valueOf(el) {
    if (el.type === "password") return el.value ? "••••" : "";
    if (el.type === "checkbox" || el.type === "radio") return el.checked ? "checked" : "unchecked";
    const ariaChecked = el.getAttribute("aria-checked");
    if (ariaChecked) return ariaChecked === "true" ? "checked" : "unchecked";
    if (el.tagName === "SELECT") return clean(el.selectedOptions[0]?.text);
    if (el.type === "file") return [...(el.files || [])].map((f) => f.name).join(", ");
    if (el.getAttribute("role") === "combobox") {
      const box = el.closest("[class*=container], [class*=control], [class*=select], [data-automation-id]") || el.parentElement;
      const vals = [...(box?.querySelectorAll("[class*=single-value], [class*=multi-value__label], [class*=singleValue]") || [])]
        .map((v) => clean(v.innerText))
        .filter(Boolean);
      if (vals.length) return vals.join(", ");
    }
    if ("value" in el && typeof el.value === "string" && el.tagName !== "BUTTON") return el.value.slice(0, 100);
    return "";
  }

  const vw = window.innerWidth, vh = window.innerHeight;
  const out = [];
  const all = __cbbDocs().flatMap((d) => [...d.querySelectorAll(SELECTOR)]);
  for (const el of all) {
    if (el.type === "hidden") continue;
    const style = el.ownerDocument.defaultView.getComputedStyle(el);
    const off = __cbbOffset(el);
    const r0 = el.getBoundingClientRect();
    const rect = { left: r0.left + off.x, right: r0.right + off.x, top: r0.top + off.y, bottom: r0.bottom + off.y, width: r0.width, height: r0.height };
    const rendered = rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    if (!rendered && el.type !== "file") continue;
    const inView = rendered && rect.bottom > 0 && rect.top < vh && rect.right > 0 && rect.left < vw;
    if (visibleOnly && !inView) continue;
    const label = labelFor(el);
    if (query && !label.toLowerCase().includes(query.toLowerCase())) continue;
    if (!el.dataset.cbbRef) el.dataset.cbbRef = "e" + ++window.__cbbCounter;
    const parts = [el.dataset.cbbRef, roleOf(el), JSON.stringify(label.slice(0, 120))];
    const v = valueOf(el);
    if (v) parts.push(`value=${JSON.stringify(v)}`);
    if (el.disabled) parts.push("disabled");
    if (el.required || el.getAttribute("aria-required") === "true") parts.push("required");
    if (!inView) parts.push(rendered ? "offscreen" : "hidden");
    out.push(parts.join(" | "));
    if (out.length >= 400) {
      out.push("... (truncated at 400 elements; use query to filter)");
      break;
    }
  }
  return `URL: ${location.href}\nTitle: ${document.title}\n` + out.join("\n");
}

function pageCenterOf({ ref }) {
  const el = __cbbFind(ref);
  if (!el) throw new Error(`Element ${ref} not found (page changed?). Call read_page again.`);
  el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
  const r = el.getBoundingClientRect();
  const off = __cbbOffset(el);
  return { x: r.left + off.x + r.width / 2, y: r.top + off.y + r.height / 2, tag: el.tagName, label: (el.innerText || el.value || el.getAttribute("aria-label") || "").trim().slice(0, 80) };
}

function pageFocus({ ref, clear }) {
  const el = __cbbFind(ref);
  if (!el) throw new Error(`Element ${ref} not found (page changed?). Call read_page again.`);
  el.scrollIntoView({ block: "center", behavior: "instant" });
  el.focus();
  if (clear) {
    if (typeof el.select === "function") el.select();
    else {
      const range = document.createRange();
      range.selectNodeContents(el);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
  }
  return true;
}

function pageReadValue({ ref }) {
  const el = __cbbFind(ref);
  if (!el) return null;
  if (el.type === "checkbox" || el.type === "radio") return el.checked;
  if (el.getAttribute("aria-checked")) return el.getAttribute("aria-checked") === "true";
  return "value" in el ? el.value : el.innerText;
}

function pageSelectNative({ ref, option }) {
  const el = __cbbFind(ref);
  if (!el) throw new Error(`Element ${ref} not found`);
  if (el.tagName !== "SELECT") return { native: false };
  const want = option.toLowerCase().trim();
  const opts = [...el.options];
  const match =
    opts.find((o) => o.text.trim().toLowerCase() === want || o.value.toLowerCase() === want) ||
    opts.find((o) => o.text.trim().toLowerCase().startsWith(want)) ||
    opts.find((o) => o.text.trim().toLowerCase().includes(want));
  if (!match) throw new Error(`No option matching "${option}". Options: ${opts.map((o) => o.text.trim()).join(" / ")}`);
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value").set;
  setter.call(el, match.value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  return { native: true, selected: match.text.trim() };
}

function pageFindOption({ option }) {
  const want = option.toLowerCase().trim();
  const candidates = __cbbDocs().flatMap((d) => [...d.querySelectorAll("[role=option], [class*=option]:not([class*=options])")]).filter((o) => {
    const r = o.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  const txt = (o) => (o.innerText || "").replace(/\s+/g, " ").trim();
  const match =
    candidates.find((o) => txt(o).toLowerCase() === want) ||
    candidates.find((o) => txt(o).toLowerCase().startsWith(want)) ||
    candidates.find((o) => txt(o).toLowerCase().includes(want));
  if (!match) return null;
  match.scrollIntoView({ block: "nearest", behavior: "instant" });
  const r = match.getBoundingClientRect();
  const off = __cbbOffset(match);
  return { x: r.left + off.x + r.width / 2, y: r.top + off.y + r.height / 2, text: txt(match) };
}

// Text currently displayed by a custom combobox (react-select single/multi value, etc.).
function pageComboValue({ ref }) {
  const el = __cbbFind(ref);
  if (!el) return null;
  const box = el.closest("[class*=container], [class*=control], [class*=select], [data-automation-id]") || el.parentElement;
  const vals = [...(box?.querySelectorAll("[class*=single-value], [class*=multi-value__label], [class*=singleValue]") || [])]
    .map((v) => v.innerText.trim())
    .filter(Boolean);
  if (vals.length) return vals.join(", ");
  return el.value || null;
}

function pageCheckTarget({ ref }) {
  const el = __cbbFind(ref);
  if (!el) throw new Error(`Element ${ref} not found`);
  el.scrollIntoView({ block: "center", behavior: "instant" });
  let target = el;
  const r0 = el.getBoundingClientRect();
  // Custom-styled inputs are often invisible; click their label instead.
  if ((r0.width < 2 || r0.height < 2) && el.id) {
    target = el.ownerDocument.querySelector(`label[for="${CSS.escape(el.id)}"]`) || el.closest("label") || el;
  }
  const r = target.getBoundingClientRect();
  const off = __cbbOffset(target);
  return { x: r.left + off.x + r.width / 2, y: r.top + off.y + r.height / 2 };
}

// Password fields filled by the user (typing or Chrome autofill). Never returns the values:
// only counts, whether all fields hold the same value (password + confirm), and a length
// fingerprint the server uses to tell when the user has stopped typing.
function pagePasswordStatus() {
  const fields = __cbbDocs()
    .flatMap((d) => [...d.querySelectorAll("input[type=password]")])
    .filter((el) => {
      const r = el.getBoundingClientRect();
      const s = el.ownerDocument.defaultView.getComputedStyle(el);
      return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none" && !el.disabled;
    });
  const values = fields.map((f) => f.value);
  return {
    url: location.href,
    count: fields.length,
    filled: values.filter((v) => v.length > 0).length,
    match: values.length < 2 || values.every((v) => v === values[0]),
    fingerprint: values.map((v) => v.length).join(",")
  };
}

// ---------- command handler ----------
async function handleCommand(cmd, a) {
  switch (cmd) {
    case "list_tabs": {
      const tabs = await chrome.tabs.query({});
      return tabs.map((t) => ({ tabId: t.id, title: t.title, url: t.url, active: t.active }));
    }
    case "open_tab": {
      await assertAllowed(a.url);
      const tab = await chrome.tabs.create({ url: a.url, active: true });
      await waitForLoad(tab.id);
      const t = await chrome.tabs.get(tab.id);
      return { tabId: t.id, title: t.title, url: t.url };
    }
    case "close_tab": {
      await chrome.tabs.remove(a.tabId);
      return "closed";
    }
    case "navigate": {
      await getTab(a.tabId);
      if (a.url === "back") await chrome.tabs.goBack(a.tabId);
      else if (a.url === "forward") await chrome.tabs.goForward(a.tabId);
      else {
        await assertAllowed(a.url);
        await chrome.tabs.update(a.tabId, { url: a.url });
      }
      await waitForLoad(a.tabId);
      const t = await chrome.tabs.get(a.tabId);
      return { tabId: t.id, title: t.title, url: t.url };
    }
    case "read_page":
      return evalInPage(a.tabId, pageReadElements, { visibleOnly: !!a.visibleOnly, query: a.query || "" });
    case "get_text": {
      const max = a.maxChars ?? 20000;
      return evalInPage(a.tabId, ({ max }) => (document.body?.innerText || "").slice(0, max), { max });
    }
    case "click": {
      let x = a.x, y = a.y, label = "";
      if (a.ref) {
        const c = await settledCenter(a.tabId, a.ref);
        ({ x, y, label } = c);
      }
      if (x === undefined || y === undefined) throw new Error("Provide ref or x,y");
      await mouseClick(a.tabId, x, y);
      return `clicked ${a.ref ?? `(${x},${y})`} ${label ? JSON.stringify(label) : ""}`.trim();
    }
    case "type": {
      await evalInPage(a.tabId, pageFocus, { ref: a.ref, clear: !!a.clear });
      if (a.clear) await pressKey(a.tabId, "Backspace");
      await cdp(a.tabId, "Input.insertText", { text: a.text });
      const v = await evalInPage(a.tabId, pageReadValue, { ref: a.ref });
      return `typed into ${a.ref}; value now: ${JSON.stringify(v)}`;
    }
    case "press_key":
      await pressKey(a.tabId, a.key);
      return `pressed ${a.key}`;
    case "select_option": {
      const native = await evalInPage(a.tabId, pageSelectNative, { ref: a.ref, option: a.option });
      if (native.native) return `selected "${native.selected}"`;
      // Custom combobox (react-select, Workday, etc.)
      // 1) open the menu, 2) click a visible matching option if present,
      // 3) otherwise type to filter (searchable / async lists) and try again.
      const c = await settledCenter(a.tabId, a.ref);
      await mouseClick(a.tabId, c.x, c.y);
      let opt = null;
      for (let i = 0; i < 4 && !opt; i++) {
        await sleep(250);
        opt = await evalInPage(a.tabId, pageFindOption, { option: a.option });
      }
      if (!opt) {
        await cdp(a.tabId, "Input.insertText", { text: a.option });
        for (let i = 0; i < 12 && !opt; i++) {
          await sleep(300);
          opt = await evalInPage(a.tabId, pageFindOption, { option: a.option });
        }
      }
      if (opt) {
        await mouseClick(a.tabId, opt.x, opt.y);
        await sleep(300);
        const shown = await evalInPage(a.tabId, pageComboValue, { ref: a.ref });
        return `selected "${opt.text}"${shown ? ` (field now shows: "${shown}")` : ""}`;
      }
      await pressKey(a.tabId, "Enter");
      await sleep(300);
      const shown = await evalInPage(a.tabId, pageComboValue, { ref: a.ref });
      return `no option element matched "${a.option}"; typed it and pressed Enter. Field now shows: "${shown ?? ""}" - verify.`;
    }
    case "set_checked": {
      const current = await evalInPage(a.tabId, pageReadValue, { ref: a.ref });
      if (current === a.checked) return `${a.ref} already ${a.checked ? "checked" : "unchecked"}`;
      const p = await evalInPage(a.tabId, pageCheckTarget, { ref: a.ref });
      await sleep(150);
      await mouseClick(a.tabId, p.x, p.y);
      await sleep(200);
      const now = await evalInPage(a.tabId, pageReadValue, { ref: a.ref });
      return `${a.ref} is now ${now ? "checked" : "unchecked"}`;
    }
    case "upload_file": {
      const res = await cdp(a.tabId, "Runtime.evaluate", {
        expression: `(() => { ${PAGE_HELPERS}; return __cbbFind(${JSON.stringify(a.ref)}); })()`
      });
      const objectId = res.result?.objectId;
      if (!objectId) throw new Error(`Element ${a.ref} not found`);
      await cdp(a.tabId, "DOM.setFileInputFiles", { objectId, files: a.paths });
      const names = await evalInPage(a.tabId, pageReadValue, { ref: a.ref });
      return `attached: ${a.paths.join(", ")} ${names ? "" : "(verify on page)"}`.trim();
    }
    case "scroll": {
      if (a.ref) {
        await evalInPage(a.tabId, pageCenterOf, { ref: a.ref });
        return `scrolled ${a.ref} into view`;
      }
      const dy = (a.direction === "up" ? -1 : 1) * (a.amount ?? 600);
      await evalInPage(a.tabId, ({ dy }) => window.scrollBy(0, dy), { dy });
      return `scrolled ${dy > 0 ? "down" : "up"} ${Math.abs(dy)}px`;
    }
    case "screenshot": {
      const shot = await cdp(a.tabId, "Page.captureScreenshot", { format: "jpeg", quality: 60 });
      return { data: shot.data };
    }
    case "password_status":
      return evalInPage(a.tabId, pagePasswordStatus);
    default:
      throw new Error(`Unknown command: ${cmd}`);
  }
}

// Messages from the options page.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "reconnect") {
    disconnect().then(connect).then(() => sendResponse("ok"));
    return true;
  }
  if (msg?.type === "stop") {
    chrome.storage.local.set({ enabled: false }).then(() => sendResponse("ok"));
    return true;
  }
});
