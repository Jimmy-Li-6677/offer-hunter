"""Job board - a local web page where the user asks Claude for job candidates and approves the ones to apply to.

The page has a "Search Google Jobs" and a "Search LinkedIn" button: a click saves a search request in
../private/job-board.json and the queue watcher (python jobboard.py watch, run by Claude's session as a Monitor) prints
it; Claude then searches in a new Chrome tab, picks about 10 candidates (autoApplyLoop step 2g) and adds them with
`python jobboard.py add <file.json> --search <id>`. The user approves jobs by putting them in the apply queue (in
order) or skips them. Claude applies only to the queued jobs (autoApplyLoop steps 2s / 2q), in order, and logs each one
in applications.json - the page shows that log status.

Usage (from the JobSearch folder):
  python jobboard.py                  -> start the page at http://127.0.0.1:<jobBoard.port> (default 8766) and open it
  python jobboard.py serve [--no-browser]
  python jobboard.py queue            -> selected jobs still to apply to, in the user's order (exit 1 if none)
  python jobboard.py queue --quiet    -> one line for the queue watcher: "<n> jobs in the apply queue" (exit 0) / "apply queue empty" (exit 1)
  python jobboard.py applying <key>   -> mark a queued job as being applied to now (shown on the page)
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

import answerbank  # same folder: the .env answer bank as a question list ("Update my answers")
import applog  # same folder: log helpers
import jobfilters  # same folder: the search filters as named checks (checkboxes on the page)
import jobinfo  # same folder: salary / experience from the job text

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
    """Add / refresh fetched jobs; the user's choice (selected / skipped / order) is kept."""
    added = 0
    for j in jobs:
        k = job_key(j)
        old = board["jobs"].get(k)
        if old is None:
            added += 1
            board["jobs"][k] = {**j, "key": k, "firstSeen": now_iso(), "choice": "new", "order": None}
        else:
            keep = {f: old.get(f) for f in ("key", "firstSeen", "choice", "order", "choiceAt", "applyingAt")}
            board["jobs"][k] = {**old, **j, **keep}
    return added


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
    return {"salary": salary, "experience": exp, "workMode": j.get("workMode") or jobinfo.work_mode(j)}


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
    out = []
    for j in board["jobs"].values():
        st, e = status(j, log_jobs)
        facts = card_facts(j)
        flags = [f for f in jobfilters.check({**j, **facts}, ctx)[0] if f not in jobfilters.STATUS_FILTERS] if ctx else []
        pay = jobinfo.salary_annual(facts["salary"])
        out.append({**j, **facts, "status": st, "logNotes": (e or {}).get("notes", ""),
                    "logUrl": (e or {}).get("url", ""), "logDate": (e or {}).get("date", ""),
                    "flags": flags, "areas": jobfilters.areas(j, ctx) if ctx else [],
                    "payMin": pay[0] if pay else None, "payMax": pay[1] if pay else None})
    cfg = config()
    meta = {"searchRequests": board.get("searches", [])[-5:],
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


def reorder(board, keys):
    for i, k in enumerate(keys, 1):
        if k in board["jobs"]:
            board["jobs"][k]["order"] = i


# ---------- search requests (the page's search buttons; Claude does the search in Chrome) ----------
SEARCH_SOURCES = {"google": "Google Jobs", "linkedin": "LinkedIn"}
OPEN_SEARCH = ("requested", "running")


def request_search(board, source):
    """Add a search request, unless one for the same source is still open."""
    reqs = board.setdefault("searches", [])
    if any(r["source"] == source and r["status"] in OPEN_SEARCH for r in reqs):
        return None
    r = {"id": f"s{len(reqs) + 1}", "source": source, "status": "requested", "requestedAt": now_iso(), "note": ""}
    reqs.append(r)
    del reqs[:-50]  # keep the last 50
    return r


def set_search(board, sid, status, note=None, found=None):
    r = next((r for r in board.get("searches", []) if r["id"] == sid), None)
    if r is None:
        sys.exit(f"no search request {sid}")
    r["status"] = status
    r[f"{status}At"] = now_iso()
    if note is not None:
        r["note"] = note
    if found is not None:
        r["found"] = found
    return r


# ---------- "Mark as submitted" on jobs waiting on the user ----------
WAITING = {"Needs User", "Needs approval", "Pending", "Candidate"}


APPROVED = "Approved"
WAITING_ACTIONS = {  # action -> (new applications.json status, note, statuses it applies to)
    "submitted": ("Submitted", "Marked submitted by the user on the job board", WAITING | {APPROVED}),
    "skipped": ("Skipped", "Skipped by the user on the job board", WAITING | {APPROVED}),
    "approve": (APPROVED, "Approved by the user on the job board - apply anyway", WAITING - {"Needs User"} | {APPROVED}),
}


def waiting_action(board, key, action):
    """A job waiting on the user: mark it submitted (the user finished it), skip it, or approve it (back in the apply
    queue, applied to even though it failed a rule). Returns an error text, or None."""
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
    if action == "approve" and e["status"] == APPROVED and j.get("choice") == "selected":
        return None  # already approved and in the apply queue
    today = dt.date.today().isoformat()
    e.update({"status": new, "date": today, "url": e.get("url") or j.get("applyUrl") or j.get("url") or "",
              "notes": f'{e.get("notes", "")} | {text} {today} (was {e["status"]}).'.strip(" |")})
    if j.get("id"):
        e["ids"] = sorted(set(e.get("ids", [])) | {j["id"]})
    applog.save(data)
    if action == "approve":  # to the end of the queue, as a fresh pick
        j.pop("applyingAt", None)
        j["choice"] = "new"
    set_choice(board, [key], "selected" if action == "approve" else "skipped" if action == "skipped" else j.get("choice", "new"))
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
        if self.path == "/api/jobs":
            with LOCK:
                board = load_board()
            return self.send(200, view(board))
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
            return self.send(200, view(board))
        if self.path == "/api/answers":
            n = answerbank.save(body.get("answers") or {})
            return self.send(200, {"saved": n, "questions": answerbank.questions()})
        if self.path == "/api/search":
            if body.get("source") not in SEARCH_SOURCES:
                return self.send(400, {"error": "source must be " + " or ".join(SEARCH_SOURCES)})
            with LOCK:
                board = load_board()
                request_search(board, body["source"])
                save_board(board)
            return self.send(200, view(board))
        self.send(404, {"error": "not found"})


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
        """(queued job keys, open search requests) or None if the board can't be read right now."""
        try:
            m = current()
            with LOCK:
                board = m.load_board()
            return ([j["key"] for j in m.queue(board)],
                    [r for r in board.get("searches", []) + board.get("profileRequests", []) if r["status"] == "requested"])
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

    first = keys() or ([], [])
    seen, seen_req = set(first[0]), {r["id"] for r in first[1]}
    if seen:
        print(f"{len(seen)} job{'' if len(seen) == 1 else 's'} in the apply queue: {' '.join(sorted(seen))}", flush=True)
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
        cur, reqs = got
        new = [k for k in cur if k not in seen]
        if new:
            print(f"new in apply queue ({len(cur)} queued): {' '.join(new)}", flush=True)
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
            log_jobs = applog.load()["jobs"]
            for i, j in enumerate(q, 1):
                e = log_entry(j, log_jobs)
                print(f'{i:>3}. {j["company"]} | {j["title"]} | {j.get("location", "")}'
                      f'{" | APPLYING" if j.get("applyingAt") else ""}\n'
                      f'      key: {j["key"]}  id: {j.get("id") or "-"}\n      {(e or {}).get("url") or j.get("applyUrl") or j.get("url")}')
                if e:  # status Approved
                    print(f'      APPROVED BY THE USER - apply even though it failed a rule earlier: {e.get("notes", "")}')
        elif a.cmd == "applying":
            j = board["jobs"].get(a.key)
            if not j:
                sys.exit(f"No job with key {a.key} on the board.")
            j["applyingAt"] = now_iso()
            save_board(board)
            print(f'applying: {j["company"]} | {j["title"]}')


if __name__ == "__main__":
    main()
