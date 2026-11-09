"""Job board - a local web page where the user asks Claude for job candidates and approves the ones to apply to.

The page has a "Search Google Jobs" and a "Search LinkedIn" button: a click saves a search request in
../private/job-board.json and the queue watcher (python jobboard.py watch, run by Claude's session as a Monitor) prints
it; Claude then searches in a new Chrome tab, picks about 10 candidates (autoApplyLoop step 2g) and adds them with
`python jobboard.py add <file.json> --search <id>`. The user approves jobs by putting them in the apply queue (in
order) or skips them. Claude applies only to the queued jobs (autoApplyLoop steps 2s / 2q), in order, and logs each one
in applications.json - the page shows that log status.
The "Search SimplifyJobs" button is handled by this server itself (simplifyjobs.py, the SimplifyJobs new-grad GitHub
list) - no Claude search; its request goes straight to "running", so the watcher does not announce it.

Usage (from the JobSearch folder):
  python jobboard.py                  -> start the page at http://127.0.0.1:<jobBoard.port> (default 8766) and open it
  python jobboard.py serve [--no-browser]
  python jobboard.py queue            -> selected jobs still to apply to, in the user's order (exit 1 if none)
  python jobboard.py queue --quiet    -> one line for the queue watcher: "<n> jobs in the apply queue" (exit 0) / "apply queue empty" (exit 1)
  python jobboard.py applying <key>   -> mark a queued job as being applied to now (shown on the page)
  python tailor.py <key>              -> (re)build a queued job's tailored resume now (normally done when it is queued)
  python jobboard.py watch            -> watcher for a Claude Monitor: prints one line each time jobs are added to the
                                         apply queue or a search button is clicked (within ~2 seconds; full check every
                                         jobBoard.queueCheckSeconds, default 30). Runs until stopped.
  python jobboard.py searches [--all] -> search requests not finished yet (exit 1 if none); --all = also earlier ones
  python jobboard.py search <id> running|done|failed [--note TEXT]
                                      -> report a search request's progress (shown on the page)
  python jobboard.py add <file.json> [--search <id>]
                                      -> add the candidates Claude found to the board: a JSON list of {source, id, company,
                                         title, location, workMode, url, applyUrl, posted, salary, description, ...};
                                         --search marks that search request done with the number found
  python jobboard.py sync             -> add the jobs from ../private/jobfeed-latest.json (manual jobfeed.py run) to the board
  python jobboard.py profile running|done|failed [--note TEXT]
                                      -> report the "Update my answers" resume request (autoApplyLoop step 2u) on the page

Job status on the page: new, selected, applying (from this file) or the applications.json status once logged
(Submitted, Needs User, Needs approval, Skipped, ...); skipped = the user skipped it on the page.
"""
import argparse
import base64
import datetime as dt
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import activity  # same folder: activity log of the scheduled search (what ran, what it found, what it cost)
import answerbank  # same folder: the .env answer bank as a question list ("Update my answers")
import applog  # same folder: log helpers
import jobfilters  # same folder: the search filters as named checks (checkboxes on the page)
import jobinfo  # same folder: salary / experience from the job text
import matchscore  # same folder: resume match score per job (rules, no AI)
import tailor  # same folder: tailored resume per queued job (master .tex + added keywords)

HERE = Path(__file__).resolve().parent
PRIVATE = applog.PRIVATE
BOARD = PRIVATE / "job-board.json"
LATEST = PRIVATE / "jobfeed-latest.json"
CONFIG = PRIVATE / "job-search-config.json"
PAGE = HERE / "jobboard.html"
LOCK = threading.Lock()          # board file read-modify-write


# ---------- board file ----------
def job_key(j):
    """Stable id for a job across runs: source + job id, else company + title."""
    if j.get("id"):
        return f'{j.get("source", "")}:{j["id"]}'
    return f'{applog.norm(j.get("company"))}|{applog.norm(j.get("title"))}'


def config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8")).get("jobBoard", {})
    except (OSError, ValueError):
        return {}


def load_board():
    if BOARD.exists():
        return json.loads(BOARD.read_text(encoding="utf-8"))
    return {"jobs": {}}


def save_board(board):
    PRIVATE.mkdir(exist_ok=True)
    tmp = BOARD.with_suffix(".tmp")
    tmp.write_text(json.dumps(board, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(BOARD)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def merge(board, jobs):
    """Add / refresh fetched jobs; the user's choice (selected / skipped / order) is kept. Removed jobs stay removed."""
    added, removed = 0, set(board.get("removed", []))
    for j in jobs:
        k = job_key(j)
        if k in removed:
            continue
        old = board["jobs"].get(k)
        if old is None:
            added += 1
            board["jobs"][k] = {**j, "key": k, "firstSeen": now_iso(), "choice": "new", "order": None}
        else:
            keep = {f: old.get(f) for f in ("key", "firstSeen", "choice", "order", "choiceAt", "applyingAt")}
            board["jobs"][k] = {**old, **j, **keep}
    return added


REMOVED_KEEP = 5000  # removed job keys remembered so searches don't add them again


def remove_jobs(board, keys):
    """The To review tab's Remove all button: delete jobs still to review (choice new) from the board and remember
    their keys so later searches skip them. Queued, skipped and logged jobs are never removed. Returns the count."""
    log_jobs = applog.load()["jobs"]
    gone = [k for k in keys if (board["jobs"].get(k) or {}).get("choice") == "new"
            and not log_entry(board["jobs"][k], log_jobs)]
    for k in gone:
        del board["jobs"][k]
    board["removed"] = (board.get("removed", []) + gone)[-REMOVED_KEEP:]
    return len(gone)


def known_jobs(board):
    """Board jobs plus removed keys - what searches treat as already seen."""
    return {**dict.fromkeys(board.get("removed", []), {}), **board["jobs"]}


def prune(board, log_jobs):
    """Drop unselected jobs older than jobBoard.keepDays (default 14) that the user never acted on."""
    days = config().get("keepDays", 14)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    for k, j in list(board["jobs"].items()):
        seen = dt.datetime.fromisoformat(j.get("firstSeen", now_iso()))
        if j.get("choice") == "new" and seen < cutoff and not log_entry(j, log_jobs):
            del board["jobs"][k]


def sync_latest(board):
    if not LATEST.exists():
        return 0
    return merge(board, json.loads(LATEST.read_text(encoding="utf-8")).get("jobs", []))


def log_entry(j, log_jobs):
    """The applications.json entry for this job, if Claude has logged it."""
    return next((e for e in reversed(log_jobs) if applog.matches(e, j.get("company", ""), j.get("title"), j.get("id") or None)), None)


def status(j, log_jobs):
    e = log_entry(j, log_jobs)
    if e and e["status"] == APPROVED and j.get("choice") == "selected":
        return ("applying" if j.get("applyingAt") else "selected"), e
    if e:
        return e["status"], e
    if j.get("choice") == "skipped":
        return "skipped", None
    if j.get("choice") == "selected":
        return ("applying" if j.get("applyingAt") else "selected"), None
    return "new", None


def card_facts(j):
    """Salary + experience boxes on the job card; jobs saved before jobfeed.py stored them get them from the description."""
    desc = j.get("description") or ""
    salary = jobinfo.salary_from(j.get("salary") or "") or j.get("salary") or jobinfo.salary_from(desc)
    exp = j.get("experience") or jobinfo.experience_from(desc, j.get("title", ""))
    if j.get("levels"):  # simplify.jobs tags (Junior / Mid Level): shown on the card only, not used by the filters
        exp = {**exp, "tag": " / ".join(j["levels"])}
    return {"salary": salary, "experience": exp, "workMode": j.get("workMode") or jobinfo.work_mode(j),
            "clearance": jobinfo.clearance_from(f'{j.get("title", "")}. {desc}')}


def experience_limits():
    """experienceYears from the search requirements: within max = fit, up to stretchAt = stretch, above = too senior."""
    try:
        e = json.loads(CONFIG.read_text(encoding="utf-8")).get("experienceYears", {})
    except (OSError, ValueError):
        e = {}
    return {"max": e.get("max", 2), "stretchAt": e.get("stretchAt", 3)}


def view(board):
    """Jobs for the page: board entries + their current status from applications.json."""
    log_jobs = applog.load()["jobs"]
    try:
        ctx = jobfilters.context(log_jobs=log_jobs)
    except (OSError, ValueError):
        ctx = None
    prof = matchscore.profile()  # resume keyword match score: rules only, no AI
    out = []
    for j in board["jobs"].values():
        st, e = status(j, log_jobs)
        facts = card_facts(j)
        flags = [f for f in jobfilters.check({**j, **facts}, ctx)[0] if f not in jobfilters.STATUS_FILTERS] if ctx else []
        pay = jobinfo.salary_annual(facts["salary"])
        out.append({**j, **facts, "status": st, "logNotes": (e or {}).get("notes", ""),
                    "logUrl": (e or {}).get("url", ""), "logDate": (e or {}).get("date", ""),
                    "flags": flags, "areas": jobfilters.areas(j, ctx) if ctx else [], "match": matchscore.score(j, prof),
                    "payMin": pay[0] if pay else None, "payMax": pay[1] if pay else None})
    cfg = config()
    import simplifysite  # same folder
    try:
        full = json.loads(CONFIG.read_text(encoding="utf-8"))
        so = simplifysite.options({}, full)
        simplify = {"keySaved": bool(simplifysite.saved_key()), "hours": so["hours"], "limit": so["limit"],
                    "queries": simplifysite.settings(full)["queries"], "levels": simplifysite.LEVELS,
                    "defaultLevels": simplifysite.DEFAULT_LEVELS}
    except (OSError, ValueError):
        simplify = {"keySaved": False, "hours": 24, "limit": 10, "queries": [], "levels": [], "defaultLevels": []}
    meta = {"searchRequests": board.get("searches", [])[-5:], "simplifySite": simplify, "schedule": board.get("schedule"), "models": get_models(board), "modelChoices": MODEL_CHOICES, "modelTasks": MODEL_TASKS,
            "profileRequest": (board.get("profileRequests") or [None])[-1]}
    filters = [{"id": f, "label": label, "rule": rule} for f, label, rule in jobfilters.FILTERS
               if f not in jobfilters.STATUS_FILTERS]
    areas = [name for name, _rx in ctx["areas"]] if ctx else []
    return {"jobs": out, **meta, "filters": filters, "areas": areas, "requireSelection": cfg.get("requireSelection", True),
            "queueCheckSeconds": cfg.get("queueCheckSeconds", 30), "experienceYears": experience_limits()}


def queue(board):
    """Selected jobs not logged yet (or logged as Approved: the user said apply anyway), in the user's order."""
    log_jobs = applog.load()["jobs"]
    sel = [j for j in board["jobs"].values() if j.get("choice") == "selected"
           and (lambda e: e is None or e["status"] == APPROVED)(log_entry(j, log_jobs))]
    return sorted(sel, key=lambda j: (j.get("order") is None, j.get("order") or 0, j.get("choiceAt") or ""))


def set_choice(board, keys, choice):
    order = max([j.get("order") or 0 for j in board["jobs"].values()] + [0])
    for k in keys:
        j = board["jobs"].get(k)
        if not j:
            continue
        if choice == "selected" and j.get("choice") != "selected":
            order += 1
            j["order"] = order
        if choice != "selected":
            j["order"] = None
            j.pop("applyingAt", None)
        j["choice"], j["choiceAt"] = choice, now_iso()


# ---------- keywords marked "I have this" on a card -> tailored resume for that job (tailor.py) ----------
def set_skill(board, key, name, add):
    """Add / remove a keyword in the job's addedSkills. Returns an error text, or None."""
    j = board["jobs"].get(key)
    if j is None:
        return "job not on the board"
    name = (name or "").strip()
    if not name or len(name) > 80:
        return "keyword missing"
    skills = [s for s in j.get("addedSkills") or [] if s != name]
    if add:
        skills.append(name)
    j["addedSkills"] = skills
    j.pop("resume", None)  # out of date; rebuilt right away when the job is in the apply queue
    return None


def build_resume(key, wait=False):
    """Build the job's tailored resume (tailor.build) in a background thread and save the result as the job's
    `resume` on the board. Jobs without added keywords get the master resume right away. Returns 0 / 1 (CLI)."""
    with LOCK:
        board = load_board()
        j = board["jobs"].get(key)
        if j is None:
            print(f"{key}: not on the board")
            return 1
        skills = list(j.get("addedSkills") or [])
        if not skills:
            j["resume"] = tailor.build(j)
            save_board(board)
            if wait:
                print(f'{key}: {j["resume"]["note"]}: {j["resume"]["pdf"]}')
            return 0
        j["resume"] = {"status": "building", "added": skills, "note": "building the tailored resume", "at": now_iso()}
        save_board(board)
        job = dict(j)

    def work():
        try:
            res = tailor.build(job)
        except Exception as e:  # never kill the server thread
            res = {"status": "failed", "pdf": str(tailor.settings()["masterPdf"]), "added": skills, "note": f"error: {e}"}
        res["at"] = now_iso()
        with LOCK:
            b = load_board()
            jj = b["jobs"].get(key)
            if jj is not None and list(jj.get("addedSkills") or []) == skills:  # else a newer build is on its way
                jj["resume"] = res
                save_board(b)
        if wait:
            print(f'{key}: {res["status"]} - {res["note"]}\n  {res["pdf"]}')
        return res

    if wait:
        return 0 if work()["status"] == "ready" else 1
    threading.Thread(target=work, daemon=True).start()
    return 0


def start_builds(board, keys):
    """After a save: (re)build the resume of each listed job that is in the apply queue."""
    for k in keys:
        j = board["jobs"].get(k)
        if j and j.get("choice") == "selected":
            build_resume(k)


def reorder(board, keys):
    for i, k in enumerate(keys, 1):
        if k in board["jobs"]:
            board["jobs"][k]["order"] = i


# ---------- search requests (the page's search buttons; Claude does the search in Chrome) ----------
SEARCH_SOURCES = {"google": "Google Jobs", "linkedin": "LinkedIn", "simplify": "SimplifyJobs", "simplifysite": "Simplify website",
                  "company": "company career sites"}
SELF_SEARCH = {"simplify", "simplifysite", "company"}  # searched by this server itself (simplifyjobs.py / simplifysite.py), not by Claude in Chrome
OPEN_SEARCH = ("requested", "running")


def request_search(board, source):
    """Add a search request, unless one for the same source is still open."""
    reqs = board.setdefault("searches", [])
    stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
    for r in reqs:  # a server-side search cut off by a server restart never finishes
        if r["source"] in SELF_SEARCH and r["status"] == "running" and dt.datetime.fromisoformat(r["requestedAt"]) < stale:
            r["status"], r["note"] = "failed", "interrupted (job board restarted)"
    if any(r["source"] == source and r["status"] in OPEN_SEARCH for r in reqs):
        return None
    status = "running" if source in SELF_SEARCH else "requested"  # the watcher announces only "requested" ones
    # next number after the highest id: len(reqs) + 1 repeated the same id once the list was capped at 50
    n = max([int(r["id"][1:]) for r in reqs if r["id"][1:].isdigit()] + [0]) + 1
    r = {"id": f"s{n}", "source": source, "status": status, "requestedAt": now_iso(), "note": ""}
    reqs.append(r)
    del reqs[:-50]  # keep the last 50
    return r


def run_simplify(sid):
    """Background thread for the "Search SimplifyJobs" button: fetch the SimplifyJobs new-grad list and add candidates."""
    import simplifyjobs  # same folder
    try:
        with LOCK:
            board_jobs = known_jobs(load_board())
        jobs, step, hours = simplifyjobs.run(board_jobs)
        with LOCK:
            board = load_board()
            n = merge(board, jobs)
            set_search(board, sid, "done", note=f"SimplifyJobs new-grad list, last {hours:g}h (widen step {step})", found=n)
            save_board(board)
    except Exception as e:  # network / format errors: show them on the page
        with LOCK:
            board = load_board()
            set_search(board, sid, "failed", note=f"{type(e).__name__}: {e}")
            save_board(board)


def run_company(sid):
    """Background thread for the "Search company sites" button: ask every company in companyJobs.companies (Fortune 500 /
    large employers) for new jobs through their careers sites' public job APIs (companyjobs.py) and add candidates."""
    import companyjobs  # same folder
    try:
        with LOCK:
            board_jobs = known_jobs(load_board())
        jobs, note = companyjobs.run(board_jobs)
        with LOCK:
            board = load_board()
            n = merge(board, jobs)
            set_search(board, sid, "done", note=note, found=n)
            save_board(board)
    except Exception as e:  # network / config errors: show them on the page
        with LOCK:
            board = load_board()
            set_search(board, sid, "failed", note=f"{type(e).__name__}: {e}")
            save_board(board)


def run_simplify_site(sid, opts):
    """Background thread for the "Search Simplify website" button: fetch the next batch from the simplify.jobs search
    API with the dialog's options (continuing where the last click stopped) and add every candidate."""
    import simplifysite  # same folder
    try:
        with LOCK:
            board = load_board()
        jobs, note, cursor = simplifysite.run(known_jobs(board), opts=opts, cursor=board.get(simplifysite.CURSOR))
        with LOCK:
            board = load_board()
            n = merge(board, jobs)
            board[simplifysite.CURSOR] = cursor
            set_search(board, sid, "done", note=f"simplify.jobs: {note} ({simplifysite.summary(opts)})", found=n)
            save_board(board)
    except Exception as e:  # key rejected / network / format errors: show them on the page
        with LOCK:
            board = load_board()
            set_search(board, sid, "failed", note=f"{type(e).__name__}: {e}")
            save_board(board)


def set_search(board, sid, status, note=None, found=None):
    # newest first: boards saved before ids were unique can hold the same id more than once
    r = next((r for r in reversed(board.get("searches", [])) if r["id"] == sid), None)
    if r is None:
        sys.exit(f"no search request {sid}")
    r["status"] = status
    r[f"{status}At"] = now_iso()
    if note is not None:
        r["note"] = note
    if found is not None:
        r["found"] = found
    return r


# ---------- AI model per task (the page's AI model button) ----------
# Which Claude model does the work of a queue run. "session" = whatever model this Claude session runs on; any other
# value = Claude hands that job's work to a subagent running that model (Agent tool `model`), see autoApplyLoop step 2q.
MODEL_CHOICES = {"session": "Same as this session", "haiku": "Haiku (cheapest)", "sonnet": "Sonnet (balanced)",
                 "opus": "Opus (strongest, costliest)", "fable": "Fable"}
MODEL_TASKS = {"resume": "Reading the job post and revising the resume",
               "fill": "Filling the application forms"}
MODEL_DEFAULTS = {"resume": "session", "fill": "session"}


def get_models(board):
    m = board.get("models") or {}
    return {t: (m.get(t) if m.get(t) in MODEL_CHOICES else MODEL_DEFAULTS[t]) for t in MODEL_TASKS}


def set_models(board, raw):
    """Save the chosen models. Returns an error text, or None."""
    new = get_models(board)
    for t, v in (raw or {}).items():
        if t not in MODEL_TASKS or v not in MODEL_CHOICES:
            return f"unknown model choice {t}={v}"
        new[t] = v
    board["models"] = new
    return None


# ---------- compact brief for one job: a fresh subagent can apply from this alone (keeps the main session small) ----------
BRIEF_SECRET = {"APPLICANT_PASSWORD", "APPLICANT_PASSWORD_ALT"}  # typed only with the browser bridge's type_secret, never printed


def apply_brief(board, key):
    """Self-contained text for ONE queued job: link, resume, mode, rules, applicant data + answer bank, logging commands.
    Never contains the account password or tokens. Returns (text, error)."""
    import re
    j = board["jobs"].get(key)
    if j is None:
        return None, "job not on the board"
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    ap = cfg.get("apply") or {}
    auto, test = ap.get("autoSubmit", False), ap.get("testMode", False)
    env = applog.read_env(applog.ENV)
    plain = {k: v for k, (v, _q) in env.items() if k.startswith(("APPLICANT_", "ANSWER_")) and k not in BRIEF_SECRET}
    sub = lambda s: re.sub(r"\$\{([A-Z0-9_]+)\}", lambda m: plain.get(m.group(1), m.group(0)), s)
    r = j.get("resume") or tailor.build({**j, "addedSkills": []})
    link = j.get("applyUrl") or j.get("url")
    status = "Test" if test else "Submitted"
    lg = (f'python applog.py add --company "{j.get("company")}" --role "{j.get("title")}" --location "{j.get("location", "")}" '
          f'--status <STATUS> --id {j.get("id") or j["key"]} --url "{link}" --notes "<one line>"')
    out = [f'APPLY BRIEF - {j.get("company")} | {j.get("title")} | {j.get("location", "")}',
           f'key: {key}   id: {j.get("id") or "-"}   work dir: the JobSearch folder (cd there for python commands)',
           f'apply link: {link}',
           f'resume to upload: {r["pdf"]}' + (f'  (tailored: added {", ".join(j["addedSkills"])})' if j.get("addedSkills") else ""),
           f'MODE: apply.autoSubmit={auto} apply.testMode={test}. ' +
           (f'Click the final Submit, then log status {status}.' if auto else
            'Fill everything but do NOT click the final Submit; leave the tab open and log status "Needs User" with note "READY TO SUBMIT ...".') +
           (' (Test = sent, waiting for the user to move it to Submitted on the board.)' if test and auto else ""),
           "", "STEPS"]
    steps = []
    if tailor.wants_revision(j):
        steps.append(f'RESUME FIRST (REQUIRED - do not skip, and upload the rebuilt PDF, not the master): python tailor.py brief {key} (posting + exact resume bullets + rules), then write an edits JSON file in your '
                     f'scratchpad and run python tailor.py revise {key} <file> (it rejects invented numbers / tools; fix and rerun; '
                     f'it rebuilds the PDF above). Reword only what your real experience shows; skills-order only is fine.')
    steps += [
        "Check the posting against the rules below BEFORE filling anything; if it breaks one, do not apply: log \"Needs approval\" with the reason and reply.",
        "Open the apply link in a NEW tab with the browser-bridge tools (open_tab). 'Site not allowed: <domain>' -> log Needs User 'site not allowed: <domain>' and reply.",
        "Fill every field from APPLICANT / ANSWERS below (answer bank first). NEVER GUESS: if a question is not covered by ANSWERS - especially anything about the "
        "applicant's personal, family or spouse situation, military / veteran status of relatives, criminal / legal / health / financial history, or references - do "
        "NOT pick an answer: leave the tab open, log Needs User with the exact question, and reply (the user answers once and it is saved to the bank). "
        "Upload the resume with upload_file (a 'Choose File' button works too). "
        "Do dropdowns ONE AT A TIME (parallel ones clash); for Workday-style multi-level pickers click through the menu.",
        "Sign-in / create account pages: email = APPLICANT_EMAIL; type the password ONLY with type_secret(name=APPLICANT_PASSWORD) into password fields "
        "(never read, print or log it). If an account already exists use sign in, else Create Account. If APPLICANT_PASSWORD is rejected at sign in, or the site says it cannot set that password, retry ONCE with type_secret(name=APPLICANT_PASSWORD_ALT) (same rules: never print it) before giving up or resetting; if the account is created with it, mention \"used ALT password\" in the log note. Email code / verification link: do NOT use "
        "mailcheck.py (not set up). Open https://outlook.live.com/mail/0/ in a SECOND new tab (already signed in), wait ~20 s after the site says it "
        "sent the mail, check Inbox then Junk, open ONLY the newest mail from that company / site, copy just the code or the verify link from it "
        "(never paste the mail text in your reply), close the Outlook tab, go back to the application tab and type the code / open the link. "
        "If the mail is not there after 2 tries (a minute apart), or Outlook asks to sign in -> log Needs User.",
        "Never touch CAPTCHA frames. Blocked (CAPTCHA, code you cannot get, a question the bank does not cover, site not allowed): leave the tab OPEN, "
        "log Needs User with the reason, reply.",
        f'Finish per MODE above. Log with: {lg}  (use the status named in MODE / the reason above; always include --url).',
        "TOKEN HYGIENE: use read_page with a query / visibleOnly; avoid get_text of whole pages and screenshots unless stuck; do not paste page text in your reply.",
        'REPLY with ONE line only: "<status> | <company> | <title> | <short reason> | tab open/closed". Close the tab of a finished job.']
    out += [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    out += ["", "RULES (skip -> Needs approval): active security clearance required ('able to obtain' / Public Trust is fine); CBP work; years required > "
            f"{experience_limits().get('max')}; degree beyond a Bachelor's with no Bachelor's alternative; staffing agency; skip companies "
            f"({sub('${SKIP_COMPANIES}')}); 2027-grad-only programs; Amazon SDE roles. Level is judged from the TITLE only (junior to mid is fine; senior / staff / "
            "principal / lead / manager in the title is not).", "", "APPLICANT"]
    out += [f"{k}={sub(v)}" for k, v in plain.items() if k.startswith("APPLICANT_")]
    out += ["", "ANSWERS (question -> answer)"]
    for k, (v, q) in env.items():
        if k.startswith("ANSWER_") and k not in BRIEF_SECRET:
            out.append(f"- {q or k}: {sub(v)}")
    return "\n".join(out), None


# ---------- AI analysis between the score cut and the apply queue (autoApplyLoop step 2t) ----------
def pending_analysis(board):
    """Jobs the schedule found that score high enough and wait for Claude to read the posting, best match first."""
    jobs = [j for j in board["jobs"].values() if (j.get("analysis") or {}).get("status") == "pending" and j.get("choice") == "new"]
    return sorted(jobs, key=lambda j: -((j.get("analysis") or {}).get("score") or 0))


def analysis_result(board, key, verdict, note):
    """Claude's verdict on a posting: pass -> the job goes to the apply queue; fail -> it is logged Needs approval with the
    reason (the user can still Approve & apply on the board). Returns an error text, or None."""
    j = board["jobs"].get(key)
    if j is None:
        return "job not on the board"
    if verdict not in ("pass", "fail"):
        return "verdict must be pass or fail"
    j["analysis"] = {**(j.get("analysis") or {}), "status": verdict, "note": (note or "")[:600], "doneAt": now_iso()}
    if verdict == "pass":
        set_choice(board, [key], "selected")
        return None
    data = applog.load()
    if log_entry(j, data["jobs"]) is None:
        data["jobs"].append({"date": dt.date.today().isoformat(), "company": j.get("company", ""), "role": j.get("title", ""),
                             "location": j.get("location", ""), "status": "Needs approval",
                             "url": j.get("applyUrl") or j.get("url") or "", "ids": [j["id"]] if j.get("id") else [],
                             "notes": f"AI analysis (scheduled search) - not queued: {note}"})
        applog.save(data)
    return None


# ---------- scheduled search + auto-queue (the page's Schedule button) ----------
SCHEDULE_SOURCES = ("company", "simplify", "simplifysite")  # the searches the board server runs itself, no Claude needed
SCHEDULE_DEFAULTS = {"hours": 8, "intervalMinutes": 60, "minScore": 75, "autoQueue": True, "maxQueuePerRun": 10,
                     "maxAgeHours": 168, "analyze": False}  # a queued job may be posted up to this long ago (the Simplify website search reaches back a week)


def schedule_settings(raw):
    """Checked schedule choices from the dialog (jobBoard.schedule in the config can change the defaults / caps)."""
    cfg = {**SCHEDULE_DEFAULTS, **(config().get("schedule") or {})}
    raw = raw or {}
    num = lambda k, lo, hi: min(max(float(raw.get(k) or cfg[k]), lo), hi)
    sources = [x for x in raw.get("sources") or [] if x in SCHEDULE_SOURCES]
    if not sources:
        raise ValueError("pick at least one search")
    return {"sources": sources, "hours": num("hours", 0.5, 24 * 14), "intervalMinutes": num("intervalMinutes", 10, 24 * 60),
            "minScore": num("minScore", 1, 100), "autoQueue": raw.get("autoQueue", True) is not False,
            "maxAgeHours": num("maxAgeHours", 1, 24 * 30), "analyze": raw.get("analyze", cfg["analyze"]) is not False,
            "maxQueuePerRun": int(cfg["maxQueuePerRun"])}


def start_schedule(board, raw):
    st = schedule_settings(raw)
    now = dt.datetime.now(dt.timezone.utc)
    prev = board.get("schedule")
    if prev and prev.get("active"):  # restarting: close the old one in the log
        activity.log({"type": "schedule_end", "schedule": prev["startedAt"], "reason": "restarted"})
    board["schedule"] = {**st, "active": True, "startedAt": now.isoformat(timespec="seconds"),
                         "endsAt": (now + dt.timedelta(hours=st["hours"])).isoformat(timespec="seconds"),
                         "nextRunAt": now.isoformat(timespec="seconds"), "runs": 0, "queued": 0, "note": "starting"}
    activity.log({"type": "schedule_start", "ts": board["schedule"]["startedAt"], **{k: st[k] for k in (
        "sources", "hours", "intervalMinutes", "minScore", "maxAgeHours", "analyze", "autoQueue")}})
    return board["schedule"]


def stop_schedule(board):
    sch = board.get("schedule")
    if sch and sch.get("active"):
        sch["active"], sch["note"], sch["stoppedAt"] = False, "stopped by the user", now_iso()
        activity.log({"type": "schedule_end", "schedule": sch["startedAt"], "reason": "stopped by the user"})


def auto_queue_jobs(board, since, min_score, limit, max_age_hours=168, analyze=False):
    """Put the jobs on the board (not only the ones this schedule found) in the apply queue when they are still to review, fail
    no search filter (the board's "old" filter is replaced by the schedule's own max_age_hours) and score at least
    min_score with similar skills counted as matches (matchscore simScore).
    Returns [(job key, company, title, score)], best match first."""
    picked = []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=max_age_hours)
    for j in view(board)["jobs"]:
        if j.get("status") != "new" or j.get("choice") != "new" or j.get("analysis"):
            continue  # already queued / skipped / applied / waiting on the user / already sent to analysis
        if [f for f in j.get("flags") or [] if f != "old"]:
            continue
        try:  # posted date, else the day the board first saw it
            when = dt.datetime.fromisoformat(j.get("posted") or j.get("firstSeen") or "")
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
            if when < cutoff:
                continue  # posted before the schedule's age limit
        except ValueError:
            pass
        sim = (j.get("match") or {}).get("simScore")
        if sim is not None and sim >= min_score:
            picked.append((sim, j))
    picked.sort(key=lambda t: -t[0])
    picked = picked[:limit] if limit else picked
    if analyze:  # Claude reads each posting first (autoApplyLoop step 2t); passing jobs are queued by `jobboard.py analyzed`
        for sim, j in picked:
            board["jobs"][j["key"]]["analysis"] = {"status": "pending", "score": sim, "at": now_iso()}
    else:
        set_choice(board, [j["key"] for _s, j in picked], "selected")
    for sim, j in picked:
        board["jobs"][j["key"]]["autoQueued"] = {"score": sim, "at": now_iso()}
    return [(j["key"], j.get("company"), j.get("title"), sim) for sim, j in picked]


def run_search_now(source):
    """One search of the schedule, run in this thread. Returns a short result text."""
    with LOCK:
        board = load_board()
        opts = None
        if source == "simplifysite":
            import simplifysite  # same folder
            if not simplifysite.saved_key():
                txt = "Simplify website: no key saved (open Search Simplify website once and paste it)"
                return txt, {"status": "skipped", "note": txt}
            opts = simplifysite.options({}, json.loads(CONFIG.read_text(encoding="utf-8")))
        r = request_search(board, source)
        if r is None:
            txt = f"{SEARCH_SOURCES[source]}: a search is already running - skipped"
            return txt, {"status": "skipped", "note": txt}
        save_board(board)
    if source == "simplifysite":
        run_simplify_site(r["id"], opts)
    else:
        {"company": run_company, "simplify": run_simplify}[source](r["id"])
    with LOCK:
        done = next((x for x in load_board().get("searches", []) if x["id"] == r["id"]), r)
    return (f'{SEARCH_SOURCES[source]}: {done["status"]}, {done.get("found", 0)} new' +
            (f' ({done["note"]})' if done["status"] == "failed" else "")), done


def timed_search(source, started, rnd):
    """run_search_now + an activity log line (button, how long, status, how many new jobs met the filters)."""
    import time
    t0 = time.monotonic()
    text, done = run_search_now(source)
    done = done or {}
    activity.log({"type": "search", "schedule": started, "round": rnd, "button": activity.BUTTONS.get(source, source),
                  "seconds": round(time.monotonic() - t0), "status": done.get("status", "skipped"),
                  "found": done.get("found", 0), "note": done.get("note") or text})
    return text


def schedule_cycle():
    """If a schedule is active and due: run its searches, then queue the good matches. Returns True when it ran."""
    with LOCK:
        board = load_board()
        sch = board.get("schedule")
        if not sch or not sch.get("active"):
            return False
        now = dt.datetime.now(dt.timezone.utc)
        if now < dt.datetime.fromisoformat(sch["nextRunAt"]):
            return False
        if now >= dt.datetime.fromisoformat(sch["endsAt"]):
            sch["active"], sch["note"] = False, f'finished after {sch["runs"]} runs, {sch["queued"]} jobs queued'
            save_board(board)
            activity.log({"type": "schedule_end", "schedule": sch["startedAt"], "reason": sch["note"]})
            return False
        sch["running"], sch["note"] = True, "searching…"
        save_board(board)
        started, sources, rnd = sch["startedAt"], list(sch["sources"]), sch.get("runs", 0) + 1
    import time
    t_round = time.monotonic()
    results = []
    for src in sources:
        try:
            results.append(timed_search(src, started, rnd))
        except Exception as e:  # one failing search never stops the schedule
            results.append(f"{SEARCH_SOURCES[src]}: {type(e).__name__}: {e}")
    queued = []
    with LOCK:
        board = load_board()
        sch = board.get("schedule") or {}
        sch["running"] = False
        if not sch.get("active"):  # stopped meanwhile
            save_board(board)
            return True
        if sch.get("autoQueue"):
            try:
                queued = auto_queue_jobs(board, started, sch["minScore"], sch.get("maxQueuePerRun", 10), sch.get("maxAgeHours", 168),
                                         sch.get("analyze", False))
            except Exception as e:
                results.append(f"auto-queue failed: {type(e).__name__}: {e}")
        sch["runs"] += 1
        sch["toAnalyze" if sch.get("analyze") else "queued"] = sch.get("toAnalyze" if sch.get("analyze") else "queued", 0) + len(queued)
        sch["lastRunAt"] = now_iso()
        nxt = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=sch["intervalMinutes"])
        sch["nextRunAt"] = nxt.isoformat(timespec="seconds")
        if nxt >= dt.datetime.fromisoformat(sch["endsAt"]):
            sch["active"] = False
            sch["note"] = f'finished after {sch["runs"]} runs, {sch["queued"]} jobs queued'
        else:
            sch["note"] = "; ".join(results)
        sch["lastQueued"] = [f"{c} - {t} ({sc}%)" for _k, c, t, sc in queued]
        sch["lastWasAnalysis"] = bool(sch.get("analyze"))
        save_board(board)
        activity.log({"type": "round", "schedule": started, "round": rnd, "seconds": round(time.monotonic() - t_round),
                      "mode": "analysis" if sch.get("analyze") else "queue", "jobs": sch["lastQueued"]})
        if not sch.get("active"):
            activity.log({"type": "schedule_end", "schedule": started, "reason": sch["note"]})
    if queued and not sch.get("analyze"):  # tailored-resume builds take the board lock themselves
        start_builds(load_board(), [k for k, *_ in queued])
    print(f'schedule run: {"; ".join(results)}; queued {len(queued)}')
    return True


def scheduler_loop():
    import time
    while True:
        try:
            schedule_cycle()
        except Exception as e:  # never stop the board over the scheduler
            print(f"scheduler: {type(e).__name__}: {e}")
        time.sleep(20)


# ---------- "Mark as submitted" on jobs waiting on the user ----------
WAITING = {"Needs User", "Needs approval", "Pending", "Candidate"}


APPROVED = "Approved"
CONTINUE_NOTE = "Continue application clicked by the user on the job board - finish it where it stopped"
WAITING_ACTIONS = {  # action -> (new applications.json status, note, statuses it applies to)
    "submitted": ("Submitted", "Marked submitted by the user on the job board", WAITING | {APPROVED, "Test"}),
    "skipped": ("Skipped", "Skipped by the user on the job board", WAITING | {APPROVED}),
    "approve": (APPROVED, "Approved by the user on the job board - apply anyway", WAITING - {"Needs User"} | {APPROVED}),
    "continue": (APPROVED, CONTINUE_NOTE, {"Needs User", APPROVED}),
}
REQUEUE = ("approve", "continue")  # these put the job back in the apply queue, at the end


def waiting_action(board, key, action):
    """A job waiting on the user: mark it submitted (the user finished it), skip it, approve it (back in the apply
    queue, applied to even though it failed a rule) or continue it (a Needs User job the user reopened: back in the
    apply queue so Claude finishes it). Returns an error text, or None."""
    if action not in WAITING_ACTIONS:
        return "unknown action"
    new, text, allowed = WAITING_ACTIONS[action]
    j = board["jobs"].get(key)
    if j is None:
        return "job not on the board"
    data = applog.load()
    e = log_entry(j, data["jobs"])
    if e is None or e.get("status") not in allowed:
        return f"this action is only for jobs that are {' / '.join(sorted(allowed))}"
    if action in REQUEUE and e["status"] == APPROVED and j.get("choice") == "selected":
        return None  # already back in the apply queue
    today = dt.date.today().isoformat()
    e.update({"status": new, "date": today, "url": e.get("url") or j.get("applyUrl") or j.get("url") or "",
              "notes": f'{e.get("notes", "")} | {text} {today} (was {e["status"]}).'.strip(" |")})
    if j.get("id"):
        e["ids"] = sorted(set(e.get("ids", [])) | {j["id"]})
    applog.save(data)
    if action in REQUEUE:  # to the end of the queue, as a fresh pick
        j.pop("applyingAt", None)
        j["choice"] = "new"
    set_choice(board, [key], "selected" if action in REQUEUE else "skipped" if action == "skipped" else j.get("choice", "new"))
    save_board(board)
    return None


# ---------- "Update my answers": resume upload -> Claude reads it and fills the answer bank (autoApplyLoop 2u) ----------
UPLOADS = PRIVATE / "uploads"
RESUME_TYPES = {".pdf", ".docx", ".doc", ".txt"}
MAX_RESUME = 5 * 1024 * 1024


def save_resume(name, data_b64):
    """Store an uploaded resume in ../private/uploads/ and return its path (ValueError if not acceptable)."""
    ext = Path(name or "").suffix.lower()
    if ext not in RESUME_TYPES:
        raise ValueError("upload a PDF, DOCX, DOC or TXT resume")
    data = base64.b64decode(data_b64 or "", validate=True)
    if not data or len(data) > MAX_RESUME:
        raise ValueError("the resume must be between 1 byte and 5 MB")
    UPLOADS.mkdir(parents=True, exist_ok=True)
    stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in Path(name).stem)[:60] or "resume"
    path = UPLOADS / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{stem}{ext}"
    path.write_bytes(data)
    return path


def request_profile(board, path):
    reqs = board.setdefault("profileRequests", [])
    r = {"id": f"p{len(reqs) + 1}", "status": "requested", "file": str(path), "requestedAt": now_iso(), "note": ""}
    reqs.append(r)
    del reqs[:-20]
    return r


def set_profile(board, status, note=None):
    reqs = board.get("profileRequests", [])
    if not reqs:
        sys.exit("no profile request")
    r = reqs[-1]
    r["status"] = status
    r[f"{status}At"] = now_iso()
    if note is not None:
        r["note"] = note
    return r


def board_version():
    """Changes whenever what /api/jobs shows can change: the board, applications.json, the config, .env (Simplify key), resume.json (match scores)
    and the date. The page sends it back so an unchanged board is not rebuilt and resent on every poll."""
    stamps = [dt.date.today().isoformat()]
    for f in (BOARD, applog.LOG, CONFIG, applog.ENV, matchscore.RESUME):
        try:
            stamps.append(str(f.stat().st_mtime_ns))
        except OSError:
            stamps.append("0")
    return "-".join(stamps)


def local_request(handler):
    """Requests must come from this page's own host (blocks DNS-rebinding reads of the answer bank)."""
    return handler.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")


# ---------- web server ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "jobboard"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def same_origin(self):
        """Only this page may change data: blocks other websites from posting to the local server."""
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        return host.split(":")[0] in ("127.0.0.1", "localhost") and (origin is None or origin.split("//")[-1] == host)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self.send(200, PAGE.read_bytes(), "text/html")
        path, _, query = self.path.partition("?")
        if path == "/api/jobs":
            version = board_version()  # before reading: a change during the read shows up on the next poll
            if parse_qs(query).get("v", [None])[0] == version:
                return self.send(200, {"unchanged": True, "version": version})
            with LOCK:
                board = load_board()
            return self.send(200, {**view(board), "version": version})
        if path == "/api/activitylog":
            if not local_request(self):
                return self.send(403, {"error": "forbidden"})
            return self.send(200, {"text": activity.render(), "file": str(activity.MD)})
        if self.path == "/api/answers":
            if not local_request(self):
                return self.send(403, {"error": "forbidden"})
            return self.send(200, {"questions": answerbank.questions()})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.same_origin() or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send(403, {"error": "forbidden"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self.send(400, {"error": "invalid JSON"})
        if self.path == "/api/choice":
            if body.get("choice") not in ("selected", "skipped", "new"):
                return self.send(400, {"error": "choice must be selected, skipped or new"})
            with LOCK:
                board = load_board()
                set_choice(board, body.get("keys", []), body["choice"])
                save_board(board)
            if body["choice"] == "selected":
                start_builds(board, body.get("keys", []))
            with LOCK:
                board = load_board()
            return self.send(200, view(board))
        if self.path == "/api/skill":
            with LOCK:
                board = load_board()
                err = set_skill(board, body.get("key", ""), body.get("name"), bool(body.get("add")))
                if not err:
                    save_board(board)
            if err:
                return self.send(400, {"error": err})
            start_builds(board, [body.get("key", "")])
            with LOCK:
                board = load_board()
            return self.send(200, view(board))
        if self.path == "/api/remove":
            with LOCK:
                board = load_board()
                remove_jobs(board, body.get("keys", []))
                save_board(board)
            return self.send(200, view(board))
        if self.path == "/api/order":
            with LOCK:
                board = load_board()
                reorder(board, body.get("keys", []))
                save_board(board)
            return self.send(200, view(board))
        if self.path == "/api/resume":
            try:
                path = save_resume(body.get("name"), body.get("data"))
            except ValueError as e:
                return self.send(400, {"error": str(e)})
            with LOCK:
                board = load_board()
                request_profile(board, path)
                save_board(board)
            return self.send(200, view(board))
        if self.path == "/api/waiting":
            with LOCK:
                board = load_board()
                err = waiting_action(board, body.get("key", ""), body.get("action", ""))
            if err:
                return self.send(400, {"error": err})
            start_builds(board, [body.get("key", "")])
            with LOCK:
                board = load_board()
            return self.send(200, view(board))
        if self.path == "/api/answers":
            n = answerbank.save(body.get("answers") or {})
            return self.send(200, {"saved": n, "questions": answerbank.questions()})
        if self.path == "/api/models":
            with LOCK:
                board = load_board()
                err = set_models(board, {k: body.get(k) for k in MODEL_TASKS if body.get(k) is not None})
                if err:
                    return self.send(400, {"error": err})
                save_board(board)
            return self.send(200, view(board))
        if self.path == "/api/schedule":
            with LOCK:
                board = load_board()
                if body.get("action") == "stop":
                    stop_schedule(board)
                else:
                    try:
                        start_schedule(board, body)
                    except (ValueError, TypeError) as e:
                        return self.send(400, {"error": str(e)})
                save_board(board)
            return self.send(200, view(board))
        if self.path == "/api/search":
            if body.get("source") not in SEARCH_SOURCES:
                return self.send(400, {"error": "source must be " + " or ".join(SEARCH_SOURCES)})
            opts = None
            if body["source"] == "simplifysite":
                import simplifysite  # same folder
                try:
                    if (body.get("key") or "").strip():
                        simplifysite.save_key(body["key"])
                    elif not simplifysite.saved_key():
                        raise ValueError("paste the X-TYPESENSE-API-KEY header from the simplify.jobs page")
                    opts = simplifysite.options(body.get("options"), json.loads(CONFIG.read_text(encoding="utf-8")))
                except (ValueError, TypeError) as e:
                    return self.send(400, {"error": str(e)})
            with LOCK:
                board = load_board()
                r = request_search(board, body["source"])
                if r and opts:
                    r["note"] = simplifysite.summary(opts)
                save_board(board)
            if r and r["source"] in SELF_SEARCH:
                args = (r["id"], opts) if r["source"] == "simplifysite" else (r["id"],)
                target = {"simplifysite": run_simplify_site, "company": run_company}.get(r["source"], run_simplify)
                threading.Thread(target=target, args=args, daemon=True).start()
            return self.send(200, view(board))
        self.send(404, {"error": "not found"})


def ensure_skillbank(max_age_days=30):
    """Build the keyword skill bank (skillbank.py) when it is missing or older than a month - in the background, so
    the page opens right away (scores use the hand-written keywords until it is ready)."""
    import time
    import skillbank  # same folder
    try:
        fresh = skillbank.RAW.exists() and time.time() - skillbank.RAW.stat().st_mtime < max_age_days * 86400
        if skillbank.CACHE.exists() and fresh:
            return
        board = load_board()
        texts = [f'{j.get("title", "")}. {j.get("description", "")}' for j in board["jobs"].values()]
        learned = [x for j in board["jobs"].values() for x in j.get("simplifySkills") or []]
        skillbank.build(texts, learned, fresh=not fresh)
    except Exception as e:  # never stop the board over the bank
        print(f"skill bank not built: {e}")


def serve(open_browser=True):
    port = int(config().get("port", 8766))
    url = f"http://127.0.0.1:{port}/"
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        print(f"Port {port} is in use - the job board is probably already running: {url}")
        if open_browser:
            webbrowser.open(url)
        return
    print(f"Job board: {url}  (Ctrl+C to stop)")
    try:  # a schedule that was already running when the activity log was introduced still gets its header (earlier rounds are not logged)
        sch = load_board().get("schedule")
        if sch and sch.get("active") and not any(e["type"] == "schedule_start" and e["ts"] == sch["startedAt"] for e in activity.events()):
            activity.log({"type": "schedule_start", "ts": sch["startedAt"], **{k: sch.get(k) for k in (
                "sources", "hours", "intervalMinutes", "minScore", "maxAgeHours", "analyze", "autoQueue")}})
    except Exception as e:
        print(f"activity log backfill: {e}")
    threading.Thread(target=ensure_skillbank, daemon=True).start()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def watch():
    """Print a line whenever jobs enter the apply queue or a search button is clicked: on every board save (a click on
    the page) and every queueCheckSeconds."""
    import importlib
    import time
    every = float(config().get("queueCheckSeconds", 30))
    # the watcher runs for hours: use the current jobboard.py / applog.py code (reloaded when the files change), so
    # changes to what counts as "queued" apply without restarting it
    code = {"mod": None, "stamp": None}

    def current():
        stamp = tuple(f.stat().st_mtime for f in (Path(__file__), HERE / "applog.py"))
        if stamp != code["stamp"]:
            import jobboard as fresh
            importlib.reload(applog)
            code["mod"], code["stamp"] = importlib.reload(fresh), stamp
        return code["mod"]

    def keys():
        """(queued (job key, time it was queued), open search requests, keys waiting for analysis) or None if the board can't be read right now.
        The time makes a job that left the queue and came back (Continue / Approve & apply) count as new, even when
        the watcher never saw the queue without it."""
        try:
            m = current()
            with LOCK:
                board = m.load_board()
            return ([(j["key"], j.get("choiceAt")) for j in m.queue(board)],
                    [r for r in board.get("searches", []) + board.get("profileRequests", []) if r["status"] == "requested"],
                    [j["key"] for j in m.pending_analysis(board)])
        except (OSError, ValueError):
            return None

    def mtime():
        try:
            return BOARD.stat().st_mtime
        except OSError:
            return None

    def announce(reqs):
        for r in reqs:
            if "file" in r:
                print(f"profile request {r['id']}: new resume {r['file']} - read it and fill the answer bank "
                      f"(autoApplyLoop step 2u)", flush=True)
            else:
                print(f"search request {r['id']}: {r['source']} - search {SEARCH_SOURCES[r['source']]} in Chrome "
                      f"(autoApplyLoop step 2g)", flush=True)

    def announce_analysis(pending, seen_an):
        new = [k for k in pending if k not in seen_an]
        if new:
            print(f"analyze request: {len(pending)} job{'' if len(pending) == 1 else 's'} waiting for AI analysis - run "
                  f"`python jobboard.py analyze` and judge each posting (autoApplyLoop step 2t): {' '.join(new)}", flush=True)

    first = keys() or ([], [], [])
    seen, seen_req, seen_an = set(first[0]), {r["id"] for r in first[1]}, set(first[2])
    announce_analysis(first[2], set())
    if seen:
        print(f"{len(seen)} job{'' if len(seen) == 1 else 's'} in the apply queue: {' '.join(sorted(k for k, _ in seen))}",
              flush=True)
    announce(first[1])
    last_m, last_check = mtime(), time.monotonic()
    while True:
        time.sleep(2)
        m = mtime()
        if m == last_m and time.monotonic() - last_check < every:
            continue
        last_m, last_check = m, time.monotonic()
        got = keys()
        if got is None:
            continue
        cur, reqs, pend = got
        announce_analysis(pend, seen_an)
        seen_an |= set(pend)
        new = [k for k in cur if k not in seen]
        if new:
            print(f"new in apply queue ({len(cur)} queued): {' '.join(k for k, _ in new)}", flush=True)
        announce([r for r in reqs if r["id"] not in seen_req])
        seen, seen_req = set(cur), seen_req | {r["id"] for r in reqs}


def main():
    for stream in (sys.stdout, sys.stderr):
        if stream:  # None under pythonw.exe (the launcher starts the server without a console)
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve")
    s.add_argument("--no-browser", action="store_true")
    qp = sub.add_parser("queue")
    qp.add_argument("--quiet", action="store_true")
    ap_ = sub.add_parser("applying")
    ap_.add_argument("key")
    sub.add_parser("sync")
    addp = sub.add_parser("add")
    addp.add_argument("file")
    addp.add_argument("--search", help="search request id this run answers (marked done)")
    srp = sub.add_parser("searches")
    srp.add_argument("--all", action="store_true", help="also finished requests, with their notes")
    pp = sub.add_parser("profile")
    pp.add_argument("status", choices=["running", "done", "failed"])
    pp.add_argument("--note")
    sp = sub.add_parser("search")
    sp.add_argument("id")
    sp.add_argument("status", choices=["running", "done", "failed"])
    sp.add_argument("--note")
    sub.add_parser("watch")
    sub.add_parser("analyze")
    lgp = sub.add_parser("log")
    lgp.add_argument("--all", action="store_true", help="every scheduled run, not only the latest")
    abp = sub.add_parser("applybrief")
    abp.add_argument("key")
    anp = sub.add_parser("analyzed")
    anp.add_argument("key")
    anp.add_argument("verdict", choices=["pass", "fail"])
    anp.add_argument("--note", default="")
    a = ap.parse_args()

    if a.cmd == "watch":
        try:
            return watch()
        except KeyboardInterrupt:
            return

    if a.cmd in (None, "serve"):
        return serve(open_browser=not getattr(a, "no_browser", False))
    with LOCK:
        board = load_board()
        if a.cmd == "sync":
            n = sync_latest(board)
            save_board(board)
            print(f"{n} new jobs added to the board ({len(board['jobs'])} total)")
        elif a.cmd == "add":
            jobs = json.loads(Path(a.file).read_text(encoding="utf-8"))
            jobs = jobs.get("jobs", []) if isinstance(jobs, dict) else jobs
            for j in jobs:
                j.setdefault("source", "linkedin")
                j.setdefault("applyUrl", j.get("url"))
            n = merge(board, jobs)
            prune(board, applog.load()["jobs"])
            if a.search:
                set_search(board, a.search, "done", found=n)
            save_board(board)
            print(f"{n} new jobs added to the board ({len(board['jobs'])} total)")
        elif a.cmd == "searches":
            open_reqs = [r for r in board.get("searches", []) if r["status"] in OPEN_SEARCH]
            for r in (board.get("searches", []) if a.all else open_reqs):
                print(f"{r['id']}  {r['source']:<8} {r['status']:<9} requested {r['requestedAt']}"
                      f"{'  found ' + str(r['found']) if 'found' in r else ''}{'  - ' + r['note'] if r.get('note') else ''}")
            if not open_reqs:
                print("no open search requests")
                sys.exit(1)
        elif a.cmd == "profile":
            r = set_profile(board, a.status, a.note)
            save_board(board)
            print(f"{r['id']} profile: {r['status']}")
        elif a.cmd == "search":
            r = set_search(board, a.id, a.status, a.note)
            save_board(board)
            print(f"{r['id']} {r['source']}: {r['status']}")
        elif a.cmd == "queue":
            q = queue(board)
            if a.quiet:  # queue watcher: one line, exit code says whether there is work
                print(f"{len(q)} job{'' if len(q) == 1 else 's'} in the apply queue" if q else "apply queue empty")
                sys.exit(0 if q else 1)
            if not q:
                waiting = sum(1 for j in board["jobs"].values() if j.get("choice") == "new")
                print(f"No selected jobs waiting. {waiting} new jobs are on the board for the user to review.")
                sys.exit(1)
            print(f"{len(q)} selected jobs to apply to, in the user's order:")
            mdl = get_models(board)
            print("AI MODEL (the user's choice on the board): " + "; ".join(f"{MODEL_TASKS[t].lower()} = {mdl[t]}" for t in MODEL_TASKS) +
                  " - 'session' = do it yourself; otherwise do that part in an Agent subagent with model=<that value> "
                  "(skip the subagent when it is the model you already run on) - autoApplyLoop step 2q")
            print("CONTEXT: run EACH job in a fresh Agent subagent (prompt: cd to the JobSearch folder, run `python jobboard.py applybrief <key>`, "
                  "follow it exactly, reply with its ONE-line result) so this session stays small; one subagent at a time (one browser).")
            log_jobs = applog.load()["jobs"]
            for i, j in enumerate(q, 1):
                e = log_entry(j, log_jobs)
                print(f'{i:>3}. {j["company"]} | {j["title"]} | {j.get("location", "")}'
                      f'{" | APPLYING" if j.get("applyingAt") else ""}\n'
                      f'      key: {j["key"]}  id: {j.get("id") or "-"}\n      {(e or {}).get("url") or j.get("applyUrl") or j.get("url")}')
                r = j.get("resume") or tailor.build({**j, "addedSkills": []})
                if j.get("addedSkills") and r.get("status") != "ready":
                    print(f'      RESUME NOT READY ({r.get("status")}: {r.get("note")}) - run: python tailor.py {j["key"]}')
                else:
                    extra = (f' (tailored: + {", ".join(j["addedSkills"])} - also list these in skills fields)'
                             if j.get("addedSkills") else "")
                    print(f'      resume: {r["pdf"]}{extra}')
                    if r.get("needsPlacement"):
                        print(f'      PLACE FIRST (step 4b): {", ".join(r["needsPlacement"])} - reword one real bullet '
                              f'(python tailor.py place "<kw>" "<entry>" "<original>" "<new>") or Skills only '
                              f'(python tailor.py place "<kw>" skills); that rebuilds this resume')
                if tailor.wants_revision(j):
                    print(f'      REVISE RESUME FIRST (step 4b): python tailor.py brief {j["key"]} -> read the posting, write the '
                          f'edits file, python tailor.py revise {j["key"]} <edits.json> (rebuilds the resume above)')
                elif j.get("revision"):
                    print(f'      resume revised for this posting: {j["revision"].get("summary") or "yes"}')
                if e and e.get("notes", "").rfind(CONTINUE_NOTE) > e.get("notes", "").rfind("Approved by the user"):
                    print(f'      CONTINUE - the user clicked Continue application: finish this Needs User job where it stopped '
                          f'(reuse its open tab if it is still there; the user may have done the blocked step): {e.get("notes", "")}')
                elif e:  # status Approved
                    print(f'      APPROVED BY THE USER - apply even though it failed a rule earlier: {e.get("notes", "")}')
        elif a.cmd == "log":
            print(activity.render(show_all=a.all))
        elif a.cmd == "applybrief":
            text, err = apply_brief(board, a.key)
            if err:
                sys.exit(err)
            print(text)
        elif a.cmd == "analyze":
            pend = pending_analysis(board)
            if not pend:
                print("No jobs waiting for analysis.")
                sys.exit(1)
            mdl = get_models(board)
            full = json.loads(CONFIG.read_text(encoding="utf-8"))
            print(f"AI MODEL (the user's choice for reading the post): {mdl['resume']} - 'session' = do it yourself, otherwise an Agent subagent "
                  f"with model=<that value> (skip when it is the model you run on)")
            print(f"{len(pend)} job(s) scored high enough (similar skills counted) and wait for your analysis. For each: read the posting "
                  f"(open its link for the full text), check it against the rules (years <= {experience_limits().get('max')}, no ACTIVE clearance "
                  f"(\"able to obtain\" / Public Trust is OK), no CBP, no staffing agency, pay vs salaryExpectation, location / work mode, "
                  f"skip companies, 2027-grad-only, level judged from the TITLE only: junior to mid is fine, reject only senior / staff / principal / lead / manager in the title) and run: python jobboard.py analyzed <key> pass|fail --note \"<reason>\"\n"
                  f"  pass -> the job goes to the apply queue (a queue run applies); fail -> logged Needs approval with your note.")
            for i, j in enumerate(pend, 1):
                facts = card_facts(j)
                print(f'\n{i:>3}. {j["company"]} | {j["title"]} | {j.get("location", "")} | match {(j.get("analysis") or {}).get("score")}%'
                      f'\n      key: {j["key"]}\n      {j.get("applyUrl") or j.get("url")}'
                      f'\n      experience: {facts["experience"].get("text") or "-"} | salary: {facts["salary"] or "-"} | '
                      f'work mode: {facts["workMode"] or "-"} | clearance: {(facts["clearance"] or {}).get("quote") or "none mentioned"}'
                      f'\n      {(j.get("description") or "")[:4000]}')
        elif a.cmd == "analyzed":
            err = analysis_result(board, a.key, a.verdict, a.note)
            if err:
                sys.exit(err)
            save_board(board)
            if a.verdict == "pass":
                start_builds(board, [a.key])
            print(f"{a.key}: analysis {a.verdict}" + (" - in the apply queue" if a.verdict == "pass" else " - logged Needs approval"))
        elif a.cmd == "applying":
            j = board["jobs"].get(a.key)
            if not j:
                sys.exit(f"No job with key {a.key} on the board.")
            j["applyingAt"] = now_iso()
            save_board(board)
            print(f'applying: {j["company"]} | {j["title"]}')


if __name__ == "__main__":
    main()
