# offer-hunter

Let **Claude Code** run your job search: every couple of hours (or when you click Refresh) it pulls new posts from job APIs (company Greenhouse / Lever / Ashby boards, JSearch = Google for Jobs, Adzuna), with LinkedIn browsing as a fallback, keeps the ones that match your rules and lists them on a local **job board** page where you pick which ones to apply to, applies to your picks on the company's own site with your answers and resume, and logs every job (with its link) for you to review.

- `JobSearch\` - Claude's working folder (`CLAUDE.md` = instructions for Claude, `applog.py` = application log + answer bank helper, `jobfeed.py` = job API fetcher, `jobboard.py` + `jobboard.html` = job board page)
- `examples\` - templates for your private files
- `private\` - **your** data (created by you, never committed)
- `claude-browser-bridge\` - MCP server + Chrome extension that lets Claude use your own Chrome
- `launcher\` - source of `StartJobSearch.exe` (one double-click to start everything)

## Prerequisites

Windows, Google Chrome, [Node.js](https://nodejs.org) 20+, Python 3, and [Claude Code](https://claude.com/claude-code) (`claude`) signed in to your Claude account.

## First-time setup

**Easiest:** clone the repo, open a terminal in it, run `claude` and say **"set me up"**. Claude follows [SETUP.md](SETUP.md): it asks for your resume, builds your private files from it, asks only what the resume can't answer, and sets up the browser bridge and launcher with you.

Manual setup instead: `<repo>` = the folder you cloned into, e.g. `C:\Users\<username>\offer-hunter`.

1. **Clone**
   ```powershell
   git clone https://github.com/<github-username>/offer-hunter.git
   cd offer-hunter
   ```
2. **Create your private files** from the templates:
   ```powershell
   mkdir private
   copy examples\.env.example                    private\.env
   copy examples\job-search-config.example.json  private\job-search-config.json
   copy examples\resume.example.json             private\resume.json
   ```
   Then fill them in:
   - `private\.env` - your contact info (`APPLICANT_*`), companies to skip (`SKIP_COMPANIES`) and the `ANSWER_*` answer bank for application forms. Claude adds new answers here as you give them.
   - `private\job-search-config.json` - roles, seniority, locations (LinkedIn geoIds), skip rules, loop interval, and `jobFeed`: location patterns, company board slugs and search queries for the job APIs.
   - Optional job API keys in `private\.env` (free tiers): `JSEARCH_API_KEY` (JSearch on rapidapi.com) and `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` (developer.adzuna.com). Without them only the company boards are searched. Check with `python jobfeed.py --plan` in `JobSearch\`.
   - `private\resume.json` - your resume as structured data; put the PDF next to it and set its path in `autoApplyLoop` step 4 of the config.
   - `private\applications.json` is created automatically on first use.
3. **Set up the browser bridge** - follow [claude-browser-bridge/START_HERE.md](claude-browser-bridge/START_HERE.md) (build, load the Chrome extension, paste the token, `claude mcp add ...`).
4. **Build the launcher**: run `launcher\build.cmd` (uses the C# compiler that ships with Windows). Check it with `StartJobSearch.exe --dry-run`.

## Daily use

Double-click `StartJobSearch.exe`. It validates your config, opens Chrome, opens the job board page (`http://127.0.0.1:8766`) and starts Claude Code with "continue my job search". Run it once: if a session is already open, it only opens the job board (only one Claude session can use Chrome). While the session is open, the job board fetches new jobs every `jobBoard.fetchEveryHours` hours (default 2) and Claude applies to the jobs you put in the apply queue - there are no other timed rounds. Claude asks you only for things it can't do itself (passwords, CAPTCHAs, verification codes, questions not in your answer bank).

**Job board page:** every new matching post shows up there with its title, company, location, posted time, source, description and links, plus two boxes to decide at a glance: **Salary** (from the job API, or the pay range found in the description) and **Experience** (required years and level from the description, green / amber / red against `experienceYears.max` / `stretchAt` in your config). Click **Add to apply queue** on the jobs you want (reorder the queue on the right) or **Skip** the ones you don't - skipped jobs never come back. Claude starts on queued jobs within seconds of your click (its queue watcher, `python jobboard.py watch`, also re-checks every `jobBoard.queueCheckSeconds` seconds, default 30) and applies to each queued job in your order, in a new Chrome tab per job, filling in the form like a person would. Sign-in pages: Claude enters your email and Chrome's password manager (or you) fills the password - passwords are never stored or typed by Claude. A job that needs you (CAPTCHA, verification code, unknown question, a site that isn't allowed) moves to **Waiting on you** with its tab left open and a **Continue application** link, and Claude goes on with the next job. Each job's status (applying, submitted, waiting on you) updates on the page. **Refresh jobs** runs the job feed immediately. To let Claude also apply to clear matches you haven't picked, set `jobBoard.requireSelection` to `false`.

Without the launcher: `cd <repo>\JobSearch`, run `python jobboard.py` (opens the page) and `claude`, and say `continue my job search`.

Useful commands (in `JobSearch\`): `python applog.py open` (jobs waiting on you), `python applog.py list --status Submitted`, `python applog.py list --no-link`, `python jobfeed.py` (new matching posts right now), `python jobfeed.py --plan` (API keys + request budget used), `python jobboard.py` (job board page), `python jobboard.py queue` (your apply queue).

## Privacy

Everything personal lives in `private\`, which is gitignored. Keep names, personal paths and real values out of every other file.
