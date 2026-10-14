"""Job board - a local web page to review the job posts from jobfeed.py and pick which ones to apply to.

While it runs, the server fetches new jobs (jobfeed.py) every jobBoard.fetchEveryHours hours and when the user clicks
Refresh jobs; every job found is added to ../private/job-board.json. The page lists them; the user selects jobs
(and orders them) or skips them. Claude applies only to the selected jobs (autoApplyLoop steps 2s / 2q), in order,
and logs each one in applications.json - the page shows that log status.

Usage (from the JobSearch folder):
  python jobboard.py                  -> start the page at http://127.0.0.1:<jobBoard.port> (default 8766) and open it
  python jobboard.py serve [--no-browser]
  python jobboard.py queue            -> selected jobs still to apply to, in the user's order (exit 1 if none)
  python jobboard.py queue --quiet    -> one line for the queue watcher: "<n> jobs in the apply queue" (exit 0) / "apply queue empty" (exit 1)
  python jobboard.py applying <key>   -> mark a queued job as being applied to now (shown on the page)
  python jobboard.py watch            -> queue watcher for a Claude Monitor: prints one line each time jobs are added to the
                                         apply queue (seconds after "Add to apply queue" is clicked; full check every
                                         jobBoard.queueCheckSeconds, default 30). Runs until stopped.
  python jobboard.py sync             -> add the jobs from ../private/jobfeed-latest.json to the board

Job status on the page: new, selected, applying (from this file) or the applications.json status once logged
(Submitted, Needs User, Needs approval, Skipped, ...); skipped = the user skipped it on the page.
"""
import argparse
import datetime as dt
import json
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import applog  # same folder: log helpers

HERE = Path(__file__).resolve().parent
PRIVATE = applog.PRIVATE
BOARD = PRIVATE / "job-board.json"
LATEST = PRIVATE / "jobfeed-latest.json"
CONFIG = PRIVATE / "job-search-config.json"
PAGE = HERE / "jobboard.html"
LOCK = threading.Lock()          # board file read-modify-write
REFRESH_LOCK = threading.Lock()  # one jobfeed.py run at a time


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
    if e:
        return e["status"], e
    if j.get("choice") == "skipped":
        return "skipped", None
    if j.get("choice") == "selected":
        return ("applying" if j.get("applyingAt") else "selected"), None
    return "new", None


def view(board):
    """Jobs for the page: board entries + their current status from applications.json."""
    log_jobs = applog.load()["jobs"]
    out = []
    for j in board["jobs"].values():
        st, e = status(j, log_jobs)
        out.append({**j, "status": st, "logNotes": (e or {}).get("notes", ""), "logUrl": (e or {}).get("url", ""),
                    "logDate": (e or {}).get("date", "")})
    cfg = config()
    fetched = last_fetch()
    every = float(cfg.get("fetchEveryHours", 2))
    meta = {"fetchedAt": fetched.isoformat() if fetched else None, "fetchEveryHours": every,
            "nextFetchAt": ((fetched or dt.datetime.now(dt.timezone.utc)) + dt.timedelta(hours=every)).isoformat()
            if every and FETCHER["running"] else None,
            "fetching": REFRESH_LOCK.locked(), "lastFetchError": FETCHER["error"]}
    return {"jobs": out, **meta, "requireSelection": cfg.get("requireSelection", True),
            "queueCheckSeconds": cfg.get("queueCheckSeconds", 30)}


def last_fetch():
    """When jobfeed.py last ran (by the page, the timer or by hand)."""
    try:
        return dt.datetime.fromisoformat(json.loads(LATEST.read_text(encoding="utf-8"))["fetchedAt"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def queue(board):
    """Selected jobs not logged yet, in the user's order."""
    log_jobs = applog.load()["jobs"]
    sel = [j for j in board["jobs"].values() if j.get("choice") == "selected" and not log_entry(j, log_jobs)]
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


def run_jobfeed():
    """Run jobfeed.py (it also adds its results to the board). Returns (ok, output)."""
    if not REFRESH_LOCK.acquire(blocking=False):
        return False, "A refresh is already running."
    try:
        r = subprocess.run([sys.executable, str(HERE / "jobfeed.py")], cwd=HERE, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=600)
        out = (r.stderr + r.stdout).strip()
        return r.returncode == 0, out[-4000:]
    except subprocess.TimeoutExpired:
        return False, "jobfeed.py did not finish within 10 minutes."
    finally:
        REFRESH_LOCK.release()


def fetch_and_sync():
    """Run the job feed and put its jobs on the board. Used by Refresh jobs and the automatic timer."""
    ok, out = run_jobfeed()
    FETCHER["error"] = None if ok else (out.splitlines() or ["job feed failed"])[-1]
    with LOCK:
        board = load_board()
        sync_latest(board)
        prune(board, applog.load()["jobs"])
        save_board(board)
    return ok, out, board


FETCHER = {"running": False, "error": None}


def auto_fetch():
    """Background timer: fetch new jobs every jobBoard.fetchEveryHours (re-read each minute; 0 = only on Refresh)."""
    FETCHER["running"] = True
    while True:
        every = float(config().get("fetchEveryHours", 2))
        last = last_fetch()
        due = every > 0 and (last is None or dt.datetime.now(dt.timezone.utc) - last >= dt.timedelta(hours=every))
        if due and not REFRESH_LOCK.locked():
            try:
                fetch_and_sync()
            except Exception as e:  # never let the timer die
                FETCHER["error"] = str(e)
        threading.Event().wait(60)


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
                if sync_latest(board):
                    save_board(board)
            return self.send(200, view(board))
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
        if self.path == "/api/refresh":
            ok, out, board = fetch_and_sync()
            return self.send(200, {"ok": ok, "output": out, **view(board)})
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
    threading.Thread(target=auto_fetch, daemon=True).start()
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def watch():
    """Print a line whenever jobs enter the apply queue: on every board save (a click on the page) and every queueCheckSeconds."""
    import time
    every = float(config().get("queueCheckSeconds", 30))

    def keys():
        try:
            with LOCK:
                return [j["key"] for j in queue(load_board())]
        except (OSError, ValueError):
            return None

    def mtime():
        try:
            return BOARD.stat().st_mtime
        except OSError:
            return None

    seen = set(keys() or [])
    if seen:
        print(f"{len(seen)} job{'' if len(seen) == 1 else 's'} in the apply queue: {' '.join(sorted(seen))}", flush=True)
    last_m, last_check = mtime(), time.monotonic()
    while True:
        time.sleep(2)
        m = mtime()
        if m == last_m and time.monotonic() - last_check < every:
            continue
        last_m, last_check = m, time.monotonic()
        cur = keys()
        if cur is None:
            continue
        new = [k for k in cur if k not in seen]
        if new:
            print(f"new in apply queue ({len(cur)} queued): {' '.join(new)}", flush=True)
        seen = set(cur)


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
            for i, j in enumerate(q, 1):
                print(f'{i:>3}. {j["company"]} | {j["title"]} | {j.get("location", "")}'
                      f'{" | APPLYING" if j.get("applyingAt") else ""}\n'
                      f'      key: {j["key"]}  id: {j.get("id") or "-"}\n      {j.get("applyUrl") or j.get("url")}')
        elif a.cmd == "applying":
            j = board["jobs"].get(a.key)
            if not j:
                sys.exit(f"No job with key {a.key} on the board.")
            j["applyingAt"] = now_iso()
            save_board(board)
            print(f'applying: {j["company"]} | {j["title"]}')


if __name__ == "__main__":
    main()
