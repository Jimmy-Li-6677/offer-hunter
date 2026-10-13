# Job search workspace

`<repo>` = the folder this project is cloned into (the parent of this `JobSearch\` folder). Paths below are relative to this folder.

Layout:
- `JobSearch\` - this folder (Claude's working directory): `CLAUDE.md`, `applog.py`.
- `..\private\` - **all of the user's real data, gitignored**: `.env`, `job-search-config.json`, `resume.json`, the resume PDF, `applications.json`, `applications-archive.md`. A fresh clone has no `private\` yet: copy the files from `..\examples\` (drop `.example` from the names) and fill them in - see `..\README.md`.
- `..\examples\` - public templates of every file in `private\` (`.env.example`, `job-search-config.example.json`, `resume.example.json`, `Resume.example.pdf`, `applications.example.json`, `applications-archive.example.md`). Never put real data, names or personal details here. `job-search-config.example.json` is a generic template, not a copy: sample / `<placeholder>` values for roles, seniority, locations, skip rules, sources; blank `profile`; generic standingRules; the same autoApplyLoop step numbers with the workflow logic only (no names, dates, cities, companies or personal rules). When a workflow step or config field is added or changed in the real config, add the generic version there; when a new `.env` key is added, add it with an empty value to `examples\.env.example`.
- `..\launcher\` (StartJobSearch.exe source), `..\claude-browser-bridge\` (browser MCP server + Chrome extension).
- The repo is public: no names, personal paths (`C:\Users\<username>\...`) or private values in any committed file. Before any git commit / push, show the user the exact file list and wait for their OK.

Files:
- `..\private\job-search-config.json` - loaded by `StartJobSearch.exe`; search requirements and workflow (keep it valid JSON when editing):
  - search requirements: `roles`, `seniority`, `experienceYears`, `postedWithinHours`, `locations`, `minSalary`, `skip`, `sources`, `loop` (interval/cap/pacing), `launcher` settings. Source of truth for WHAT to search; they win over anything conflicting in `profile` / `standingRules`.
  - `profile` - the user's own preference notes; `standingRules` - standing search rules.
  - `autoApplyLoop` - the step-by-step "program" for each round. New workflow rules from the user go into its `steps`.
- `..\private\applications.json` - log of every submitted / skipped / needs-approval / needs-user / pending / candidate job (older markdown log, if any: `..\private\applications-archive.md`). Use `applog.py`, don't hand-edit:
  - `python applog.py check "<company>" "<role>" [--id <job id>]` before analyzing any job - exit 0 = already logged, skip it.
  - `python applog.py add --company .. --role .. --location .. --status .. --url <job link> [--notes ..] [--id ..]` after each job (updates the existing entry for the same company+role / id). Always record the job link (autoApplyLoop step 1b): the company posting / application page for Submitted (required - applog refuses without it), else the LinkedIn post. `python applog.py list --no-link` shows entries missing a link.
  - `python applog.py open` lists jobs still waiting on the user / approval; `python applog.py note "<text>"` adds a round note.
  - `python applog.py answer --section <section> --question "<q>" --answer "<a>"` saves a question the user just answered into the ANSWER_* bank in `..\private\.env` (+ empty key in `..\examples\.env.example`); run it right after every new answer (autoApplyLoop step 6d).
- Resume: the PDF in `..\private\` (exact path in autoApplyLoop step 4) is the file to upload; `..\private\resume.json` is the same resume as structured data (jobs, bullets, dates, education, skills) used to fill experience / Role Description fields (autoApplyLoop step 4a). Update both together.
- `..\private\.env` - private values, loaded by `StartJobSearch.exe` into the session environment:
  - `APPLICANT_*` (email, phone, address, LinkedIn/portfolio URLs), `SKIP_COMPANIES`, site usernames, bridge token.
  - `ANSWER_*` - the **answer bank** for application forms. Each entry has a `# Q: <question>` comment line above it. Read the whole file before every application; only ask the user about questions not covered, then save the new answer with `python applog.py answer ...`.
  Files refer to .env values as `${NAME}`: read .env and substitute the real value when filling a form. Never copy .env values into other files or into applications.json; add new private values to `..\private\.env` (and the empty key to `..\examples\.env.example`) and reference them as `${NAME}`. No passwords are stored anywhere.

## Starting the loop
When the user says "continue my job search" (or similar) at the start of a session:
1. Read `..\private\job-search-config.json` (all sections), `..\private\.env` (answer bank) and `..\private\applications.json`. Briefly list what `python applog.py open` shows (Needs User / Needs approval / Pending / Candidate).
2. Start the loop for this session by invoking the `loop` skill with (`<N>h` = `loop.intervalHours` from the config; use the absolute paths of `<repo>\private\...` on this PC):
   `<N>h Run ONE round of the job-search auto-apply workflow exactly as written in the autoApplyLoop section of <repo>\private\job-search-config.json, using its search requirements and the ANSWER_* answer bank in <repo>\private\.env (re-read both files each round). Skip a firing if a round is still running or finished <30 min ago.`
   Choose "This session only" if asked (cloud agents cannot use Chrome / LinkedIn sign-in).
3. Run the first round immediately. The loop lasts until the session ends.

Browser: Google Chrome (the user is signed in to LinkedIn), via the tool named in `launcher.browserTool` in the config (`browser-bridge` or `claude-in-chrome`). Pace LinkedIn per `loop.linkedinSearchDelayMinutes`; stop LinkedIn for the round on HTTP 429.

## Browser bridge
The self-built replacement for Claude in Chrome lives in `..\claude-browser-bridge` (MCP server `browser-bridge` + Chrome extension; token = `BRIDGE_TOKEN` in `..\private\.env`). Setup and startup steps: `..\claude-browser-bridge\START_HERE.md`. If its tools are unavailable, tell the user (check the extension badge) instead of silently switching browsers.

## Launcher
`<repo>\StartJobSearch.exe` (source + build script: `..\launcher\`) validates `private\job-search-config.json` and `private\.env`, starts Chrome, and opens Claude Code in this folder with a "continue my job search" prompt.
