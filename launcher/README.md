# StartJobSearch.exe

Build it once with `<repo>\launcher\build.cmd` (`<repo>` = the folder you cloned this project into, e.g. `C:\Users\<username>\offer-hunter`), then double-click `<repo>\StartJobSearch.exe` to:

1. Load and validate `private\job-search-config.json` and `private\.env` (an error box explains any problem).
   The `.env` must contain at least one `ANSWER_*` entry, the application answer bank, and a value for every
   `${NAME}` placeholder used in the config. All `.env` values are loaded into the Claude Code session's environment.
2. Start Chrome if it isn't running (`launcher.startChrome`, `launcher.chromePath`).
3. Start the job board page (`JobSearch\jobboard.py`, with `pythonw.exe` so no console window opens) unless it is already
   running, and open `http://127.0.0.1:<jobBoard.port>` in Chrome (`jobBoard.openOnStart`; needs Python on PATH).
   Its Search Google Jobs / Search LinkedIn buttons ask the Claude session to search in Chrome and add candidates.
4. If a job-search session is already running (its browser bridge owns `BRIDGE_PORT`, default 8765), stop here and say so -
   a second session could not use Chrome (you can still choose to start one).
5. Open Claude Code in `JobSearch\` with a prompt that starts the session: the queue watcher applies to the jobs in the
   apply queue, using the browser tool in `launcher.browserTool`, and `--permission-mode` from
   `launcher.claudePermissionMode` (set it to `""` to be asked for every permission).

Edit the search requirements in `private\job-search-config.json` (templates: `examples\`). No rebuild is needed; Claude re-reads it for every queue run.

Rebuild after changing `StartJobSearch.cs`: run `launcher\build.cmd`. It uses the C# compiler that ships with Windows, so nothing needs installing.
Check what would launch, without launching it: `StartJobSearch.exe --dry-run > out.txt`.
