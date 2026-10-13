# Claude Browser Bridge

Your own, self-hosted way to let **Claude Code** control **your Chrome**: open tabs, read pages, click, type, pick dropdown options, tick checkboxes, upload files and take screenshots. No third-party browser product involved.

```
Claude Code ──MCP (stdio)──► server/ (Node, on your PC)
                               │ ws://127.0.0.1:8765 + secret token
                               ▼
                            extension/ (Chrome, Manifest V3)
                               │ chrome.debugger (DevTools Protocol)
                               ▼
                            your real tabs (your sign-ins)
```

## Setup (one time)

`<repo>` = the folder you cloned this project into, e.g. `C:\Users\<username>\offer-hunter`. Step-by-step version: [START_HERE.md](START_HERE.md).

1. **Build the server**
   ```powershell
   cd <repo>\claude-browser-bridge\server
   npm install
   npm run build
   npm run token      # prints the port + secret token
   ```
2. **Load the extension**: Chrome → `chrome://extensions` → turn on *Developer mode* → **Load unpacked** → choose the `extension` folder.
3. Click the extension icon → paste the **token** → **Save & connect**.
4. **Register with Claude Code** (once per PC):
   ```powershell
   claude mcp add --scope user browser-bridge -- node <repo>\claude-browser-bridge\server\dist\index.js
   ```
5. Start `claude`. The badge turns **ON** (green) once Claude Code has started the server and the extension has connected.

## Tools exposed to Claude

| Tool | What it does |
|---|---|
| `list_tabs`, `open_tab`, `close_tab`, `navigate` | Tab management (navigate also supports `back` / `forward`) |
| `read_page` | Lists interactive elements with refs (`e12`), role, label, value, required/offscreen flags |
| `get_text` | Visible page text |
| `click` | Real mouse click on a ref or x,y (works with React/Workday) |
| `type` | Real keyboard text input (optional clear) |
| `press_key` | Enter, Tab, Escape, arrows, … |
| `select_option` | Native `<select>` **and** custom comboboxes / react-select |
| `set_checked` | Checkbox / radio (clicks the label for custom-styled inputs) |
| `upload_file` | Attaches local files to `<input type=file>` **without** a file picker |
| `wait_for_password` | Sign-in hand-off: waits until **you** fill the password field(s) (Chrome password manager or typing), then lets Claude continue. Never reads the password, only "filled" / "matches" |
| `scroll`, `screenshot`, `wait` | Utilities |

## Safety

- Server listens on **127.0.0.1 only** and requires the secret token (`BRIDGE_TOKEN` in `<repo>/private/.env`, created on first start; real environment variables override it).
- **Allowed sites list** in the extension options; anything else is refused unless you tick "Allow all sites".
- Chrome shows a *"started debugging this browser"* bar and the badge shows ▶ while a command runs.
- **Stop** button in the extension popup disconnects immediately.
- Never use it to type passwords: sign in yourself.

## Troubleshooting

- Badge **!** → no token set. Badge empty → server not running (start `claude`) or port 8765 in use.
- "Element eN not found" → page changed; call `read_page` again.
- Only one Claude Code session can own port 8765 at a time.
