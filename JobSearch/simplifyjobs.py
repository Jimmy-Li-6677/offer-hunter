"""New-grad jobs from the SimplifyJobs GitHub list (github.com/SimplifyJobs/New-Grad-Positions) for the job board.

The job board's "Search SimplifyJobs" button runs this in the board's own server (no Chrome, no Claude search needed):
it downloads the list's listings.json, keeps active software / data / AI roles that pass the search filters
(jobfilters.py: title, location, skip rules, already logged) and are not on the board yet, newest first, widening the
posted-within window (12h -> 24h -> 3 days -> 1 week) only while fewer than browserSearch.candidatesPerSearch pass,
and adds them to the board as candidates for the user to approve.

Usage (from the JobSearch folder):
  python simplifyjobs.py            -> add candidates to the board now (same as the button)
  python simplifyjobs.py --dry-run  -> only print what would be added
"""
import argparse
import datetime as dt
import json
import re
import urllib.request

import applog  # same folder: log helpers
import jobfilters  # same folder: the search filters

LISTINGS_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"
REPO_URL = "https://github.com/SimplifyJobs/New-Grad-Positions"
CATEGORIES = {"Software", "Software Engineering", "AI/ML/Data", "Data Science, AI & Machine Learning"}
WIDEN_HOURS = [None, 24, 72, 168]  # None = postedWithinHours (top level of the config)
GRAD_2027_TITLE = re.compile(r"\b2027\b")  # "... - 2027 Start" (skip rule: 2027-grad-only programs)
UTC = dt.timezone.utc


def fetch(url=LISTINGS_URL):
    req = urllib.request.Request(url, headers={"User-Agent": "offer-hunter-jobboard"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def to_job(x):
    """A listings.json entry as a job board job."""
    locs = x.get("locations") or []
    location = "; ".join(locs)
    remote = any(re.search(r"\bremote\b", l, re.I) for l in locs)
    posted = dt.datetime.fromtimestamp(x["date_posted"], UTC).isoformat(timespec="seconds") if x.get("date_posted") else None
    facts = [f"category {x.get('category')}"]
    if x.get("degrees"):
        facts.append("degrees " + ", ".join(x["degrees"]))
    if x.get("sponsorship"):
        facts.append(f"sponsorship {x['sponsorship']}")
    return {
        "source": "simplify", "id": x["id"], "company": x.get("company_name", ""), "title": x.get("title", ""),
        "location": location, "workMode": "Remote" if remote and len(locs) == 1 else "", "remote": remote and len(locs) == 1,
        "salary": "", "posted": posted, "url": f"https://simplify.jobs/p/{x['id']}", "applyUrl": x.get("url") or "",
        "description": f"SimplifyJobs new-grad list ({REPO_URL}): " + "; ".join(facts)
                       + ". Requirements (years, clearance, pay) not verified - check the posting.",
    }


def same_job(j):
    """Company + title, with level numbers spelled one way ("Engineer 1" = "Engineer I")."""
    title = re.sub(r"\b(1|i)\b", "i", re.sub(r"\b(2|ii)\b", "ii", applog.norm(j.get("title"))))
    return applog.norm(j.get("company")), title


def candidates(listings, board_jobs, limit, cfg=None):
    """(jobs to add, widen step used, hours used). board_jobs = the board's jobs (key -> job): those are left out,
    also when they came from another source (same company + title)."""
    pool = [to_job(x) for x in listings
            if x.get("active") and x.get("is_visible", True) and x.get("category") in CATEGORIES
            and not GRAD_2027_TITLE.search(x.get("title", ""))]
    seen = {same_job(j) for j in board_jobs.values()}
    pool = [j for j in pool if f"simplify:{j['id']}" not in board_jobs and same_job(j) not in seen
            and not seen.add(same_job(j))]  # also one per company + title within the list
    pool.sort(key=lambda j: j["posted"] or "", reverse=True)
    log_jobs = applog.load()["jobs"]
    found, step, hours = [], 0, None
    for step, hours in enumerate(WIDEN_HOURS, 1):
        hours = hours or (cfg or {}).get("postedWithinHours", 12)
        ctx = jobfilters.context(cfg=cfg, log_jobs=log_jobs, hours=hours)
        found = [j for j in pool if not jobfilters.check(j, ctx)[0]]
        if len(found) >= limit:
            break
    found = found[:limit]
    for j in found:
        j["description"] = f"[widen {step}: last {hours:g}h] " + j["description"]
    return found, step, hours


def run(board_jobs, cfg=None):
    cfg = cfg if cfg is not None else json.loads(jobfilters.CONFIG.read_text(encoding="utf-8"))
    limit = cfg.get("browserSearch", {}).get("candidatesPerSearch", 10)
    return candidates(fetch(), board_jobs, limit, cfg)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    import jobboard  # same folder
    with jobboard.LOCK:
        board = jobboard.load_board()
    jobs, step, hours = run(board["jobs"])
    for j in jobs:
        print(f'{j["posted"]}  {j["company"]} | {j["title"]} | {j["location"]}\n    {j["applyUrl"]}')
    if a.dry_run:
        print(f"{len(jobs)} candidates (widen step {step}, last {hours:g}h) - dry run, nothing added")
        return
    with jobboard.LOCK:
        board = jobboard.load_board()
        n = jobboard.merge(board, jobs)
        jobboard.save_board(board)
    print(f"{n} new jobs added to the board (widen step {step}, last {hours:g}h)")


if __name__ == "__main__":
    main()
