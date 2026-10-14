#!/usr/bin/env node
// Claude Browser Bridge - MCP server.
// Claude Code talks to this process over stdio (MCP). This process talks to the
// Chrome extension over a localhost-only WebSocket protected by a secret token.
// IMPORTANT: stdout is reserved for the MCP protocol - log only to stderr.

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { WebSocketServer, WebSocket } from "ws";
import { z } from "zod";
import { randomBytes } from "node:crypto";
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// ---------- config (token + port) ----------
// BRIDGE_TOKEN / BRIDGE_PORT come from <workspace>/private/.env, where <workspace> is the folder that
// contains this repo (server/dist/index.js -> ../../..). Real environment variables win.
// A token from the old config.json (<workspace>/.claude-browser-bridge or ~/.claude-browser-bridge)
// is moved into .env once; with neither, a new token is generated into .env.
const REPO_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const ENV_FILE = join(REPO_DIR, "..", "private", ".env");
const LEGACY_CONFIG_FILES = [
  join(REPO_DIR, "..", ".claude-browser-bridge", "config.json"),
  join(homedir(), ".claude-browser-bridge", "config.json"),
];

interface Config {
  token: string;
  port: number;
}

// Fills existing empty "KEY=" lines (e.g. from a copied .env.example) and appends the keys that are missing.
function appendEnv(vars: Record<string, string | number>) {
  let text = existsSync(ENV_FILE) ? readFileSync(ENV_FILE, "utf8") : "";
  const missing: string[] = [];
  for (const [k, v] of Object.entries(vars)) {
    const line = new RegExp(`^${k}=.*$`, "m");
    if (line.test(text)) text = text.replace(line, `${k}=${v}`);
    else missing.push(`${k}=${v}`);
  }
  if (missing.length)
    text += (text && !text.endsWith("\n") ? "\n" : "") +
      "\n# Claude Browser Bridge (paste BRIDGE_TOKEN into the Chrome extension)\n" + missing.join("\n") + "\n";
  mkdirSync(dirname(ENV_FILE), { recursive: true });
  writeFileSync(ENV_FILE, text);
}

function loadConfig(): Config {
  if (existsSync(ENV_FILE)) process.loadEnvFile(ENV_FILE);
  if (!process.env.BRIDGE_TOKEN) {
    const legacy = LEGACY_CONFIG_FILES.find((f) => existsSync(f));
    const config: Config = legacy
      ? (JSON.parse(readFileSync(legacy, "utf8")) as Config)
      : { token: randomBytes(24).toString("hex"), port: 8765 };
    appendEnv({ BRIDGE_TOKEN: config.token, BRIDGE_PORT: config.port });
    console.error(`[bridge] saved BRIDGE_TOKEN to ${ENV_FILE}` + (legacy ? ` (moved from ${legacy}; that file can be deleted)` : ""));
    return config;
  }
  return { token: process.env.BRIDGE_TOKEN, port: Number(process.env.BRIDGE_PORT) || 8765 };
}

const config = loadConfig();

if (process.argv.includes("--print-token")) {
  console.log(`Port:  ${config.port}`);
  console.log(`Token: ${config.token}`);
  console.log(`(from BRIDGE_TOKEN in ${ENV_FILE})`);
  process.exit(0);
}

const log = (...args: unknown[]) => console.error("[bridge]", ...args);

// ---------- call log: one line per browser action ----------
// <repo>/logs/bridge-calls-YYYY-MM-DD.log  (server/dist/index.js -> ../../logs)
const LOG_DIR = join(REPO_DIR, "logs");
if (!existsSync(LOG_DIR)) mkdirSync(LOG_DIR, { recursive: true });
const tabUrls = new Map<number, string>(); // last known URL per tab, filled by tab-returning commands

function summarizeArgs(args: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(args)) {
    if (k === "tabId") continue;
    let s = typeof v === "string" ? v : JSON.stringify(v);
    if (k === "text" && typeof v === "string" && v.length > 60) s = v.slice(0, 60) + "…";
    parts.push(`${k}=${JSON.stringify(s).slice(0, 200)}`);
  }
  return parts.join(" ");
}

function writeCallLog(tool: string, args: Record<string, unknown>, ok: boolean, ms: number, detail: string) {
  const now = new Date();
  const stamp = now.toLocaleString("sv-SE").replace(" ", "T"); // local time, sortable
  const tabId = typeof args.tabId === "number" ? args.tabId : undefined;
  const url = (args.url as string | undefined) ?? (tabId !== undefined ? tabUrls.get(tabId) : undefined) ?? "";
  let host = "";
  try {
    host = url && !["back", "forward"].includes(url) ? new URL(url).hostname : "";
  } catch {}
  const line = [
    stamp,
    tool.padEnd(13),
    ok ? "OK " : "ERR",
    `${ms}ms`.padStart(7),
    `tab=${tabId ?? "-"}`,
    `site=${host || "-"}`,
    summarizeArgs(args),
    `-> ${detail.replace(/\s+/g, " ").slice(0, 200)}`
  ].join(" | ");
  try {
    appendFileSync(join(LOG_DIR, `bridge-calls-${now.toLocaleDateString("sv-SE")}.log`), line + "\n");
  } catch (e) {
    log("could not write call log:", (e as Error).message);
  }
}

function rememberTabs(result: any) {
  const items = Array.isArray(result) ? result : [result];
  for (const t of items) {
    if (t && typeof t.tabId === "number" && typeof t.url === "string") tabUrls.set(t.tabId, t.url);
  }
}

// ---------- WebSocket link to the extension ----------
let extensionSocket: WebSocket | null = null;
let nextId = 1;
const pending = new Map<number, { resolve: (v: any) => void; reject: (e: Error) => void; timer: NodeJS.Timeout }>();

const wss = new WebSocketServer({ host: "127.0.0.1", port: config.port });
let portTaken = false; // another Claude session's bridge owns the port: the extension talks to that one, not to us

wss.on("error", (err: NodeJS.ErrnoException) => {
  if (err.code === "EADDRINUSE") {
    portTaken = true;
    log(`Port ${config.port} is already in use (another bridge server running?). Browser tools will fail.`);
  } else {
    log("WebSocket server error:", err.message);
  }
});

wss.on("connection", (socket, req) => {
  const url = new URL(req.url ?? "/", "http://127.0.0.1");
  if (url.searchParams.get("token") !== config.token) {
    log("Rejected connection with bad token");
    socket.close(4001, "bad token");
    return;
  }
  if (extensionSocket && extensionSocket.readyState === WebSocket.OPEN) {
    extensionSocket.close(4002, "replaced by new connection");
  }
  extensionSocket = socket;
  log("Extension connected");

  socket.on("message", (data) => {
    let msg: any;
    try {
      msg = JSON.parse(data.toString());
    } catch {
      return;
    }
    if (msg.type === "ping") {
      socket.send(JSON.stringify({ type: "pong" }));
      return;
    }
    const entry = pending.get(msg.id);
    if (!entry) return;
    pending.delete(msg.id);
    clearTimeout(entry.timer);
    if (msg.ok) entry.resolve(msg.result);
    else entry.reject(new Error(msg.error ?? "Unknown extension error"));
  });

  socket.on("close", () => {
    if (extensionSocket === socket) extensionSocket = null;
    log("Extension disconnected");
  });
});

function sendCommand(cmd: string, args: Record<string, unknown> = {}, timeoutMs = 30000): Promise<any> {
  return new Promise((resolve, reject) => {
    if (portTaken) {
      reject(
        new Error(
          `Another Claude Code session already runs the browser bridge (port ${config.port}), so Chrome is connected to ` +
            "that session, not this one. Only one job-search session can use Chrome at a time: close the other Claude " +
            "window (or keep working there), then restart this session."
        )
      );
      return;
    }
    if (!extensionSocket || extensionSocket.readyState !== WebSocket.OPEN) {
      reject(
        new Error(
          "Chrome extension is not connected. Open Chrome, make sure the Claude Browser Bridge extension is enabled, " +
            "and that its options page has the correct token (run `npm run token` in the server folder)."
        )
      );
      return;
    }
    const id = nextId++;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`Command "${cmd}" timed out after ${timeoutMs} ms`));
    }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    extensionSocket.send(JSON.stringify({ id, cmd, args }));
  });
}

// ---------- MCP tools ----------
const server = new McpServer({ name: "claude-browser-bridge", version: "0.1.0" });

type ToolResult = { content: Array<{ type: "text"; text: string } | { type: "image"; data: string; mimeType: string }>; isError?: boolean };

const text = (value: unknown): ToolResult => ({
  content: [{ type: "text", text: typeof value === "string" ? value : JSON.stringify(value, null, 2) }],
});

// Errors that will not go away by retrying.
const NON_RETRYABLE = [/Site not allowed/i, /Another Claude Code session/i, /Unknown command/i, /Invalid URL/i, /Provide ref or x,y/i, /No option matching/i, /No tab with id/i];
const MAX_ATTEMPTS = 3;

async function run(cmd: string, args: Record<string, unknown>, timeoutMs?: number): Promise<ToolResult> {
  let lastError = "";
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    const started = Date.now();
    try {
      const result = await sendCommand(cmd, args, timeoutMs);
      rememberTabs(result);
      const detail =
        cmd === "read_page" || cmd === "get_text"
          ? `${String(result).length} chars`
          : typeof result === "string"
            ? result
            : JSON.stringify(result);
      writeCallLog(cmd, args, true, Date.now() - started, (attempt > 1 ? `[attempt ${attempt}] ` : "") + detail);
      return text(attempt > 1 ? `${typeof result === "string" ? result : JSON.stringify(result, null, 2)}\n(succeeded on attempt ${attempt})` : result);
    } catch (err) {
      lastError = (err as Error).message;
      const retryable = !NON_RETRYABLE.some((re) => re.test(lastError));
      writeCallLog(cmd, args, false, Date.now() - started, `[attempt ${attempt}/${MAX_ATTEMPTS}] ${lastError}`);
      if (!retryable || attempt === MAX_ATTEMPTS) break;
      await new Promise((r) => setTimeout(r, 1000 * attempt)); // 1s, 2s backoff
    }
  }
  return { content: [{ type: "text", text: `Error after retries: ${lastError}` }], isError: true };
}

const tabId = z.number().int().describe("Chrome tab id (from list_tabs or open_tab)");
const ref = z.string().describe('Element ref from read_page, e.g. "e12"');

server.tool("list_tabs", "List open Chrome tabs (id, title, url, active).", {}, async () => run("list_tabs", {}));

server.tool(
  "open_tab",
  "Open a new Chrome tab at a URL and return its tab id.",
  { url: z.string().describe("URL to open") },
  async ({ url }) => run("open_tab", { url }, 45000)
);

server.tool("close_tab", "Close a Chrome tab.", { tabId }, async (a) => run("close_tab", a));

server.tool(
  "navigate",
  "Navigate a tab to a URL and wait for it to load. Use url 'back' or 'forward' for history.",
  { tabId, url: z.string() },
  async (a) => run("navigate", a, 45000)
);

server.tool(
  "read_page",
  "List the interactive elements on the page (links, buttons, inputs, selects, checkboxes, comboboxes) with a ref id, " +
    "role, label, current value and whether it is visible. Use the refs with click/type/select_option/set_checked/upload_file.",
  {
    tabId,
    visibleOnly: z.boolean().optional().describe("Only elements currently in the viewport (default false)"),
    query: z.string().optional().describe("Optional case-insensitive text filter on label/name"),
  },
  async (a) => run("read_page", a)
);

server.tool(
  "get_text",
  "Return the visible text of the page (truncated to maxChars, default 20000).",
  { tabId, maxChars: z.number().int().optional() },
  async (a) => run("get_text", a)
);

server.tool(
  "click",
  "Click an element by ref (real mouse event, works with React apps), or click at viewport coordinates x,y.",
  { tabId, ref: ref.optional(), x: z.number().optional(), y: z.number().optional() },
  async (a) => run("click", a)
);

server.tool(
  "type",
  "Focus an element by ref and type text into it (real keyboard input). Set clear=true to replace existing text.",
  { tabId, ref, text: z.string(), clear: z.boolean().optional() },
  async (a) => run("type", a)
);

server.tool(
  "press_key",
  "Press a key in the focused element, e.g. Enter, Tab, Escape, ArrowDown, Backspace.",
  { tabId, key: z.string() },
  async (a) => run("press_key", a)
);

server.tool(
  "select_option",
  "Choose an option. Works on native <select> (by option text or value) and on custom comboboxes/react-select " +
    "(clicks it, types the text, then picks the best matching option).",
  { tabId, ref, option: z.string().describe("Option text (or value) to choose") },
  async (a) => run("select_option", a)
);

server.tool(
  "set_checked",
  "Check or uncheck a checkbox / radio button by ref.",
  { tabId, ref, checked: z.boolean() },
  async (a) => run("set_checked", a)
);

server.tool(
  "upload_file",
  "Attach local file(s) to an <input type=file> by ref, without opening the file picker.",
  { tabId, ref, paths: z.array(z.string()).describe("Absolute local file paths") },
  async (a) => run("upload_file", a)
);

server.tool(
  "scroll",
  "Scroll an element into view (ref) or scroll the page up/down.",
  { tabId, ref: ref.optional(), direction: z.enum(["up", "down"]).optional(), amount: z.number().optional() },
  async (a) => run("scroll", a)
);

server.tool(
  "screenshot",
  "Take a screenshot of the visible part of a tab.",
  { tabId },
  async (a) => {
    const started = Date.now();
    try {
      const result = await sendCommand("screenshot", a);
      writeCallLog("screenshot", a, true, Date.now() - started, "image");
      return { content: [{ type: "image", data: result.data, mimeType: "image/jpeg" }] };
    } catch (err) {
      writeCallLog("screenshot", a, false, Date.now() - started, (err as Error).message);
      return { content: [{ type: "text", text: `Error: ${(err as Error).message}` }], isError: true };
    }
  }
);

server.tool(
  "wait",
  "Wait a number of milliseconds (max 10000).",
  { ms: z.number().int().min(0).max(10000) },
  async ({ ms }) => {
    await new Promise((r) => setTimeout(r, ms));
    writeCallLog("wait", { ms }, true, ms, `waited ${ms} ms`);
    return text(`waited ${ms} ms`);
  }
);

// Hand-off for sign-in / account pages: Claude never types passwords. It fills the rest, calls this,
// the user fills the password (Chrome password manager or typing), and Claude continues afterwards.
server.tool(
  "wait_for_password",
  "Wait until the USER has filled every visible password field in the tab (via Chrome's password manager or typing), " +
    "then return so the job can continue (e.g. click Sign In / Create Account). Password values are never read out: " +
    "only whether the fields are filled and whether password + confirm match. Also returns early if the password page " +
    "goes away (user submitted it). Tell the user you are waiting before calling this.",
  {
    tabId,
    timeoutSec: z.number().int().min(10).max(900).optional().describe("Give up after this many seconds (default 300)"),
  },
  async ({ tabId, timeoutSec = 300 }) => {
    const started = Date.now();
    const deadline = started + timeoutSec * 1000;
    let lastFingerprint = "";
    let sawFields = false;
    let status: any = null;
    let errors = 0;
    const finish = (ok: boolean, msg: string): ToolResult => {
      writeCallLog("wait_for_password", { tabId, timeoutSec }, ok, Date.now() - started, msg);
      return ok ? text(msg) : { content: [{ type: "text", text: msg }], isError: true };
    };
    while (Date.now() < deadline) {
      try {
        status = await sendCommand("password_status", { tabId });
        errors = 0;
      } catch (err) {
        const message = (err as Error).message;
        if (NON_RETRYABLE.some((re) => re.test(message))) return finish(false, `Error: ${message}`);
        // Page navigating (e.g. after the user pressed Sign In) - keep polling.
        if (++errors >= 10) return finish(false, `Error: ${message}`);
        await new Promise((r) => setTimeout(r, 1500));
        continue;
      }
      if (status.count > 0) sawFields = true;
      if (sawFields && status.count === 0) {
        return finish(true, `Password page is gone (user submitted it?). Now at ${status.url} - read the page and continue.`);
      }
      const complete = status.count > 0 && status.filled === status.count && status.match;
      // Require two identical polls in a row so we don't continue while the user is still typing.
      if (complete && status.fingerprint === lastFingerprint) {
        return finish(
          true,
          `Password filled by the user in ${status.count} field(s)` +
            (status.count > 1 ? " and they match" : "") +
            ". Continue the job (e.g. click Sign In / Create Account)."
        );
      }
      lastFingerprint = complete ? status.fingerprint : "";
      await new Promise((r) => setTimeout(r, 1500));
    }
    const state = status
      ? `${status.filled}/${status.count} password field(s) filled${status.match ? "" : ", fields do NOT match"}`
      : "no status";
    return finish(false, `Timed out after ${timeoutSec}s waiting for the user to fill the password (${state}).`);
  }
);

// ---------- start ----------
const transport = new StdioServerTransport();
await server.connect(transport);
log(`MCP server ready. Waiting for the Chrome extension on ws://127.0.0.1:${config.port}`);
