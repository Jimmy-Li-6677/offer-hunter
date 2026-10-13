# Starting Claude Browser Bridge

Uses your **Claude subscription through Claude Code** — no Claude API key, no extra cost.

`<repo>` below is the folder you cloned this project into, e.g. `C:\Users\<username>\offer-hunter`.
Prerequisites: [Node.js](https://nodejs.org) 20+, Google Chrome, Claude Code (`claude`).

## One-time setup (~5 min)

1. **Build the server and get your token** — in PowerShell (or in the Claude prompt prefixed with `!`):
   ```powershell
   cd <repo>\claude-browser-bridge\server
   npm install
   npm run build
   npm run token
   ```
   Copy the `Token:` value. It is saved as `BRIDGE_TOKEN` in `<repo>\private\.env` (created if missing).

2. **Load the extension in Chrome**
   - Go to `chrome://extensions`
   - Turn on **Developer mode** (top right)
   - Click **Load unpacked** → choose `<repo>\claude-browser-bridge\extension`
   - Click the puzzle-piece icon → **pin** "Claude Browser Bridge"

3. **Connect the extension** — click its icon → paste the token → **Save & connect**.

4. **Register with Claude Code** (once per PC):
   ```powershell
   claude mcp add --scope user browser-bridge -- node <repo>\claude-browser-bridge\server\dist\index.js
   ```

## Every time

1. **Open Chrome** (the extension starts by itself).
2. **Start Claude Code:**
   ```powershell
   cd <repo>\JobSearch
   claude
   ```
   Claude Code starts the bridge server automatically.
3. **Check the extension badge:**
   | Badge | Meaning |
   |---|---|
   | 🟢 ON | Connected, ready |
   | ▶ | Claude is controlling a tab right now |
   | (blank) | Not connected — make sure `claude` is running; wait up to 30 s |
   | ! | No token saved — redo setup step 3 |
4. **Tell Claude what to do**, e.g. `continue my job search` or `use browser-bridge to fill this application: <url>`.
5. **Stop anytime:** extension icon → **Stop**, or exit Claude Code (`/exit` or Ctrl+C).

## After changing the code

```powershell
cd <repo>\claude-browser-bridge\server
npm run build
```
Then restart `claude`. For extension changes: `chrome://extensions` → click the **reload ↻** icon on Claude Browser Bridge.

## Troubleshooting

- **Badge stays blank:** only one Claude Code session can own port 8765 — close other `claude` windows.
- **"Site not allowed"** — add the site in the extension popup's *Allowed sites* list.
- **"Element eN not found"** — the page changed; ask Claude to read the page again.
- **Check registration:** `claude mcp list` should show `browser-bridge … ✔ Connected`.
