const DEFAULT_SITES = [
  "linkedin.com", "greenhouse.io", "ashbyhq.com", "lever.co", "myworkdayjobs.com",
  "icims.com", "amazon.jobs", "careers.microsoft.com", "google.com", "spacex.com", "example.com"
];

const $ = (id) => document.getElementById(id);

async function load() {
  const cfg = await chrome.storage.local.get(["token", "port", "allowedSites", "allowAllSites", "enabled", "status"]);
  $("token").value = cfg.token ?? "";
  $("port").value = cfg.port ?? 8765;
  $("sites").value = (cfg.allowedSites ?? DEFAULT_SITES).join("\n");
  $("allowAll").checked = !!cfg.allowAllSites;
  showStatus(cfg.enabled === false ? "stopped" : cfg.status);
}

function showStatus(s) {
  const text = {
    connected: "🟢 Connected to Claude Code",
    busy: "🟡 Working…",
    off: "⚪ Not connected (is Claude Code running with the bridge server?)",
    error: "🔴 No token set",
    stopped: "⛔ Stopped"
  }[s] ?? "⚪ Not connected";
  $("status").textContent = "Status: " + text;
}

$("save").onclick = async () => {
  const sites = $("sites").value.split(/\s+/).map((s) => s.trim().toLowerCase()).filter(Boolean);
  await chrome.storage.local.set({
    token: $("token").value.trim(),
    port: Number($("port").value) || 8765,
    allowedSites: sites,
    allowAllSites: $("allowAll").checked,
    enabled: true
  });
  chrome.runtime.sendMessage({ type: "reconnect" });
  setTimeout(load, 1500);
};

$("start").onclick = async () => {
  await chrome.storage.local.set({ enabled: true });
  setTimeout(load, 1500);
};

$("stop").onclick = async () => {
  await chrome.storage.local.set({ enabled: false });
  showStatus("stopped");
};

chrome.storage.onChanged.addListener((changes) => {
  if (changes.status) showStatus(changes.status.newValue);
});

load();
