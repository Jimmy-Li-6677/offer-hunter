"""Job feed - pull fresh job posts from job APIs instead of browsing LinkedIn search pages.

Sources (configured in ../private/job-search-config.json -> "jobFeed"):
  ats      Greenhouse / Lever / Ashby public job-board APIs for a list of companies (no key needed).
           Exact first-published timestamps and direct company apply links.
  jsearch  JSearch (Google for Jobs: LinkedIn, Indeed, Glassdoor, ZipRecruiter, company sites).
           Key: JSEARCH_API_KEY in ../private/.env (RapidAPI or OpenWeb Ninja key, see jobFeed.jsearch.endpoint).
  adzuna   Adzuna job search API. Keys: ADZUNA_APP_ID + ADZUNA_APP_KEY in ../private/.env.
Keyed sources are skipped when their key is missing; paid-quota sources keep a monthly request budget
(usage counted in ../private/jobfeed-state.json).

Usage (from the JobSearch folder):
  python jobfeed.py                  -> fetch all enabled sources, print new matching jobs (newest first),
                                        save them to ../private/jobfeed-latest.json
  python jobfeed.py --hours 24       -> override jobFeed.postedWithinHours for this run
  python jobfeed.py --source ats     -> only one source (ats / jsearch / adzuna); repeatable
  python jobfeed.py --rejected       -> also list jobs that were filtered out, with the reason
  python jobfeed.py --plan           -> show which sources / requests would run and the API budget, without fetching
  python jobfeed.py --check-boards   -> verify every company slug in jobFeed.ats still returns jobs

"New" = passes the title / location / freshness / skip-company filters and is not already in
../private/applications.json (same company+role or same job id, as in `applog.py check`).
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import applog  # same folder: log + .env helpers
import jobboard  # same folder: the job board page's data (user's selected / skipped jobs)

PRIVATE = applog.PRIVATE
CONFIG = PRIVATE / "job-search-config.json"
STATE = PRIVATE / "jobfeed-state.json"
LATEST = PRIVATE / "jobfeed-latest.json"
UTC = dt.timezone.utc
UA = {"User-Agent": "offer-hunter-jobfeed/1.0"}


# ---------- helpers ----------
def http_json(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def parse_time(v):
    """ISO string / epoch ms -> aware UTC datetime (None if unknown)."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v, UTC)
    try:
        t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    except ValueError:
        return None


def job(source, company, title, location, url, posted=None, apply_url=None, remote=False, job_id=None,
        salary="", publisher="", direct=True, country="", description="", board=""):
    return {"source": source, "id": str(job_id or ""), "company": (company or "").strip(), "title": (title or "").strip(),
            "location": (location or "").strip(), "remote": bool(remote), "posted": posted, "url": url or apply_url or "",
            "applyUrl": apply_url or url or "", "salary": salary, "publisher": publisher, "direct": direct,
            "country": (country or "").strip(), "description": re.sub(r"\s+", " ", description or "").strip(), "board": board}


def text_of(markup):
    """HTML (possibly entity-escaped, as Greenhouse sends it) -> plain text on one line."""
    s = html.unescape(html.unescape(markup or ""))
    s = re.sub(r"<(br|/p|/li|/h\d|/div)[^>]*>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def real_items(items):
    """Drop template placeholders like "<ashby-org-slug>" so an unedited example config costs no requests."""
    return [i for i in items if i and "<" not in i]


def load_state():
    month = dt.date.today().strftime("%Y-%m")
    st = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    if st.get("month") != month:
        st = {"month": month, "requests": {}}
    return st


def save_state(st):
    STATE.write_text(json.dumps(st, indent=2) + "\n", encoding="utf-8")


class Budget:
    """Per-round and per-month request limits for quota-limited APIs."""

    def __init__(self, state, name, cfg):
        self.state, self.name = state, name
        self.round_left = int(cfg.get("maxRequestsPerRound", 3))
        self.monthly = int(cfg.get("monthlyRequestBudget", 0)) or None
        self.min_gap = float(cfg.get("minHoursBetweenRuns", 0))

    def stop_reason(self):
        if self.monthly and self.used >= self.monthly:
            return f"monthly request budget used up ({self.used}/{self.monthly}) - no more requests until next month"
        return "per-round request limit reached - the remaining searches run in the next rounds"

    def too_soon(self):
        last = parse_time(self.state.get("lastRun", {}).get(self.name))
        return bool(last and self.min_gap and dt.datetime.now(UTC) - last < dt.timedelta(hours=self.min_gap))

    def mark_run(self):
        self.state.setdefault("lastRun", {})[self.name] = dt.datetime.now(UTC).isoformat()

    def rotate(self, items):
        """Items starting at the saved cursor, so successive rounds walk through all searches."""
        cur = self.state.setdefault("cursor", {}).get(self.name, 0) % max(len(items), 1)
        return items[cur:] + items[:cur]

    def advance(self, n, total):
        self.state.setdefault("cursor", {})[self.name] = (self.state["cursor"].get(self.name, 0) + n) % max(total, 1)

    @property
    def used(self):
        return self.state["requests"].get(self.name, 0)

    def take(self):
        if self.round_left <= 0 or (self.monthly and self.used >= self.monthly):
            return False
        self.round_left -= 1
        self.state["requests"][self.name] = self.used + 1
        return True


# ---------- sources ----------
def fetch_greenhouse(slug):
    d = http_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    out = []
    for j in d.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        out.append(job("greenhouse", j.get("company_name") or slug, j.get("title"), loc, j.get("absolute_url"),
                       parse_time(j.get("first_published")), remote="remote" in loc.lower(), job_id=j.get("id"),
                       board=slug))  # description: fetched later for kept jobs only (add_descriptions)
    return out


def fetch_lever(slug):
    d = http_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in d:
        cat = j.get("categories") or {}
        loc = " / ".join(cat.get("allLocations") or [cat.get("location") or ""])
        out.append(job("lever", slug, j.get("text"), loc, j.get("hostedUrl"), parse_time(j.get("createdAt")),
                       apply_url=j.get("applyUrl"), remote=(j.get("workplaceType") == "remote" or "remote" in loc.lower()),
                       job_id=j.get("id"), country=j.get("country") or "",
                       description=j.get("descriptionPlain") or text_of(j.get("description"))))
    return out


def fetch_ashby(slug):
    d = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    out = []
    for j in d.get("jobs", []):
        if j.get("isListed") is False:
            continue
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        loc = " / ".join(x for x in locs if x)
        comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
        out.append(job("ashby", slug, j.get("title"), loc, j.get("jobUrl"), parse_time(j.get("publishedAt")),
                       apply_url=j.get("applyUrl"),
                       remote=bool(j.get("isRemote")) or j.get("workplaceType") == "Remote" or "remote" in loc.lower(),
                       job_id=j.get("id"), salary=comp, country=addr.get("addressCountry", ""),
                       description=j.get("descriptionPlain") or text_of(j.get("descriptionHtml"))))
    return out


ATS_FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


def add_descriptions(jobs, log):
    """Greenhouse's board list has no descriptions; fetch them for the (few) kept jobs only."""
    todo = [j for j in jobs if not j["description"] and j["source"] == "greenhouse" and j["board"] and j["id"]]

    def one(j):
        d = http_json(f"https://boards-api.greenhouse.io/v1/boards/{j['board']}/jobs/{j['id']}")
        j["description"] = text_of(d.get("content"))

    with cf.ThreadPoolExecutor(8) as ex:
        for f in [ex.submit(one, j) for j in todo]:
            try:
                f.result()
            except Exception as e:  # a missing description must not stop the round
                log(f"  ! description not loaded: {e}")


def source_ats(cfg, budget_state, log):
    boards = [(kind, slug) for kind in ATS_FETCHERS for slug in real_items(cfg.get(kind, []))]
    jobs, errors = [], []
    with cf.ThreadPoolExecutor(12) as ex:
        futs = {ex.submit(ATS_FETCHERS[k], s): (k, s) for k, s in boards}
        for f in cf.as_completed(futs):
            try:
                jobs += f.result()
            except Exception as e:  # one dead board must not stop the round
                errors.append(f"{futs[f][0]}/{futs[f][1]}: {e}")
    log(f"ats: {len(boards)} company boards, {len(jobs)} postings" + (f", {len(errors)} errors" if errors else ""))
    for e in errors:
        log("  ! " + e)
    return jobs


def source_jsearch(cfg, state, log, env, hours):
    key = env.get("JSEARCH_API_KEY")
    if not key:
        log("jsearch: skipped (no JSEARCH_API_KEY in private/.env)")
        return []
    budget = Budget(state, "jsearch", cfg)
    if budget.too_soon():
        log(f"jsearch: skipped (last run < {budget.min_gap:g}h ago, minHoursBetweenRuns)")
        return []
    budget.mark_run()
    if cfg.get("endpoint", "rapidapi") == "openwebninja":
        base, headers = "https://api.openwebninja.com/jsearch/search", {"x-api-key": key}
    else:
        base, headers = "https://jsearch.p.rapidapi.com/search", {"X-RapidAPI-Key": key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
    date_posted = "today" if hours <= 24 else "3days" if hours <= 72 else "week"
    jobs, queries, done = [], real_items(cfg.get("queries", [])), 0
    for q in budget.rotate(queries):
        if not budget.take():
            log(f"jsearch: {budget.stop_reason()}")
            break
        params = {"query": q, "page": 1, "num_pages": cfg.get("numPages", 1), "country": cfg.get("country", "us"),
                  "date_posted": date_posted}
        if cfg.get("jobRequirements"):
            params["job_requirements"] = cfg["jobRequirements"]
        try:
            d = http_json(base + "?" + urllib.parse.urlencode(params), headers)
        except urllib.error.HTTPError as e:
            log(f"jsearch: '{q}' -> HTTP {e.code} {e.reason}")
            if e.code in (401, 403, 429):
                break
            continue
        rows = d.get("data") or []
        for j in rows:
            # prefer a direct company apply option over job boards (LinkedIn Easy Apply etc.)
            opts = j.get("apply_options") or []
            direct = [o for o in opts if o.get("is_direct")]
            apply = (direct[0]["apply_link"] if direct else j.get("job_apply_link"))
            loc = ", ".join(x for x in (j.get("job_city"), j.get("job_state"), j.get("job_country")) if x)
            sal = ""
            if j.get("job_min_salary"):
                sal = f'{j.get("job_min_salary")}-{j.get("job_max_salary")} {j.get("job_salary_period") or ""}'.strip()
            jobs.append(job("jsearch", j.get("employer_name"), j.get("job_title"), loc or ("Remote" if j.get("job_is_remote") else ""),
                            j.get("job_apply_link"), parse_time(j.get("job_posted_at_datetime_utc")), apply_url=apply,
                            remote=j.get("job_is_remote"), job_id=j.get("job_id"), salary=sal,
                            publisher=j.get("job_publisher") or "", direct=bool(direct) or bool(j.get("job_apply_is_direct")),
                            country=j.get("job_country") or "", description=j.get("job_description") or ""))
        log(f"jsearch: '{q}' -> {len(rows)} jobs")
        done += 1
    budget.advance(done, len(queries))
    log(f"jsearch: {budget.used} requests used this month" + (f" of {budget.monthly}" if budget.monthly else ""))
    return jobs


def source_adzuna(cfg, state, log, env, hours):
    app_id, app_key = env.get("ADZUNA_APP_ID"), env.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        log("adzuna: skipped (no ADZUNA_APP_ID / ADZUNA_APP_KEY in private/.env)")
        return []
    budget = Budget(state, "adzuna", cfg)
    if budget.too_soon():
        log(f"adzuna: skipped (last run < {budget.min_gap:g}h ago, minHoursBetweenRuns)")
        return []
    budget.mark_run()
    jobs, done = [], 0
    searches = [(q, w) for q in real_items(cfg.get("queries", [])) for w in (real_items(cfg.get("where") or []) or [""])]
    for what, where in budget.rotate(searches):
        if not budget.take():
            log(f"adzuna: {budget.stop_reason()}")
            break
        params = {"app_id": app_id, "app_key": app_key, "what": what, "results_per_page": cfg.get("resultsPerPage", 50),
                  "max_days_old": max(1, -(-hours // 24)), "sort_by": "date", "content-type": "application/json"}
        if where:
            params["where"] = where
        try:
            d = http_json(f"https://api.adzuna.com/v1/api/jobs/{cfg.get('country', 'us')}/search/1?" + urllib.parse.urlencode(params))
        except urllib.error.HTTPError as e:
            log(f"adzuna: '{what}' {where} -> HTTP {e.code} {e.reason}")
            if e.code in (401, 403, 429):
                break
            continue
        rows = d.get("results") or []
        for j in rows:
            sal = f'{int(j["salary_min"])}-{int(j.get("salary_max") or j["salary_min"])}' if j.get("salary_min") else ""
            loc = (j.get("location") or {}).get("display_name", "")
            jobs.append(job("adzuna", (j.get("company") or {}).get("display_name"), html.unescape(j.get("title", "")), loc,
                            j.get("redirect_url"), parse_time(j.get("created")), remote="remote" in (j.get("title", "") + loc).lower(),
                            job_id=j.get("id"), salary=sal, publisher="adzuna", direct=False, description=text_of(j.get("description")),
                            country=cfg.get("country", "us")))
        log(f"adzuna: '{what}' {where or '(anywhere)'} -> {len(rows)} jobs")
        done += 1
    budget.advance(done, len(searches))
    log(f"adzuna: {budget.used} requests used this month" + (f" of {budget.monthly}" if budget.monthly else ""))
    return jobs


# ---------- filtering ----------
def compile_any(patterns):
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.I) if patterns else None


def filter_jobs(jobs, fc, hours, skip_companies, logged, now):
    title_in, title_out = compile_any(fc.get("titleInclude", [])), compile_any(fc.get("titleExclude", []))
    loc_in = compile_any(fc.get("locationInclude", []))
    non_us = compile_any(fc.get("remoteExcludeLocations", []))
    us = {c.lower() for c in fc.get("remoteCountries", ["", "US", "USA", "United States"])}
    vague = compile_any(fc.get("locationVague", []))
    skip = [applog.norm(c) for c in skip_companies if c.strip()]
    cutoff = now - dt.timedelta(hours=hours)

    def check(j):
        """Reason the job is filtered out, or None to keep it. May flag j["checkLocation"]."""
        if j["posted"] is None:
            if not fc.get("keepUndated", False):
                return "no posted date"
        elif j["posted"] < cutoff:
            return "older than window"
        if title_in and not title_in.search(j["title"]):
            return "title not a target role"
        if title_out and title_out.search(j["title"]):
            return "title excluded (seniority/type)"
        if j["country"].lower() not in us:  # e.g. "CA-Ontario-Toronto" would otherwise match the California pattern
            return "outside the US"
        remote_us = j["remote"] and not (non_us and non_us.search(j["location"]))
        # multi-location posts ("Toronto, CA / San Mateo, CA"): one target-area location without a foreign name is enough
        parts = re.split(r"\s*[;/|]\s*|\s+or\s+", j["location"])
        in_area = any(loc_in and loc_in.search(p) and not (non_us and non_us.search(p)) for p in parts)
        if not remote_us and not in_area:
            if not (vague and vague.fullmatch(j["location"].strip())):
                return "location not in target areas"
            j["checkLocation"] = True  # board gives no city ("Hybrid", "In-Office"): keep, verify on the posting
        company = applog.norm(j["company"])
        if company and any(s in company or company in s for s in skip):
            return "skip company"
        if any(applog.matches(e, j["company"], j["title"], j["id"] or None) for e in logged):
            return "already in applications.json"
        return None

    keep, rejected, seen = [], [], set()
    for j in jobs:
        key = (applog.norm(j["company"]), applog.norm(j["title"]))
        if key in seen:
            continue  # same job from two sources: keep the first (ats first = direct company link)
        seen.add(key)
        reason = check(j)
        if reason:
            rejected.append({**j, "reason": reason})
        else:
            keep.append(j)
    keep.sort(key=lambda j: j["posted"] or dt.datetime.min.replace(tzinfo=UTC), reverse=True)
    return keep, rejected


def age(t, now):
    if not t:
        return "?"
    m = int((now - t).total_seconds() // 60)
    return f"{m}m" if m < 60 else f"{m // 60}h{m % 60:02d}" if m < 48 * 60 else f"{m // 1440}d"


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float)
    ap.add_argument("--source", action="append", choices=["ats", "jsearch", "adzuna"])
    ap.add_argument("--rejected", action="store_true")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--check-boards", action="store_true")
    a = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252; titles contain dashes, emoji, ...
        stream.reconfigure(encoding="utf-8", errors="replace")

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    fc = cfg.get("jobFeed")
    if not fc:
        sys.exit("No \"jobFeed\" section in private/job-search-config.json (template: examples/job-search-config.example.json).")
    env = {k: v for k, (v, _q) in applog.read_env(applog.ENV).items()} if applog.ENV.exists() else {}
    hours = a.hours or fc.get("postedWithinHours") or cfg.get("postedWithinHours", 24)
    sources = a.source or [s for s in ("ats", "jsearch", "adzuna") if (fc.get(s) or {}).get("enabled", False)]
    state = load_state()
    log = lambda m: print(m, file=sys.stderr)

    if a.check_boards:
        ats = fc.get("ats", {})
        for kind, fetch in ATS_FETCHERS.items():
            for slug in real_items(ats.get(kind, [])):
                try:
                    n = len(fetch(slug))
                    print(f"{'OK ' if n else 'EMPTY'} {kind}/{slug}: {n} postings")
                except Exception as e:
                    print(f"BAD {kind}/{slug}: {e}")
        return

    if a.plan:
        print(f"window: last {hours}h | sources: {', '.join(sources) or 'none enabled'}")
        ats = fc.get("ats", {})
        print("ats boards:", ", ".join(f"{k}={len(ats.get(k, []))}" for k in ATS_FETCHERS))
        for name, keys in (("jsearch", ["JSEARCH_API_KEY"]), ("adzuna", ["ADZUNA_APP_ID", "ADZUNA_APP_KEY"])):
            c = fc.get(name, {})
            has = all(env.get(k) for k in keys)
            n = len(c.get("queries", [])) * (len(c.get("where") or [""]) if name == "adzuna" else 1)
            print(f"{name}: {'enabled' if c.get('enabled') else 'disabled'}, key {'set' if has else 'MISSING'}, "
                  f"{n} searches/round (max {c.get('maxRequestsPerRound', 3)}), "
                  f"{state['requests'].get(name, 0)}/{c.get('monthlyRequestBudget', 'unlimited')} requests used in {state['month']}")
        return

    jobs = []
    if "ats" in sources:
        jobs += source_ats(fc.get("ats", {}), state, log)
    if "jsearch" in sources:
        jobs += source_jsearch(fc.get("jsearch", {}), state, log, env, hours)
    if "adzuna" in sources:
        jobs += source_adzuna(fc.get("adzuna", {}), state, log, env, hours)
    save_state(state)

    now = dt.datetime.now(UTC)
    skip = env.get("SKIP_COMPANIES", "").split(",")
    keep, rejected = filter_jobs(jobs, fc, hours, skip, applog.load()["jobs"], now)
    board = jobboard.load_board()
    skipped = {k for k, j in board["jobs"].items() if j.get("choice") == "skipped"}
    rejected += [{**j, "reason": "skipped by the user on the job board"} for j in keep if jobboard.job_key(j) in skipped]
    keep = [j for j in keep if jobboard.job_key(j) not in skipped]

    if a.rejected:
        counts = {}
        for r in rejected:
            counts[r["reason"]] = counts.get(r["reason"], 0) + 1
        print("Filtered out: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda x: -x[1])))
        for r in rejected:
            if r["reason"] not in ("older than window", "title not a target role"):
                print(f'  - [{r["reason"]}] {r["company"]} | {r["title"]} | {r["location"]} | {r["url"]}')
    add_descriptions(keep, log)
    if not keep:
        print(f"No new matching jobs posted in the last {hours:g}h ({len(jobs)} fetched). "
              "Run with --rejected to see why jobs were filtered out, or --hours 48 for a wider window.")
    else:
        print(f"{len(keep)} new matching jobs posted in the last {hours:g}h (of {len(jobs)} fetched):")
    for i, j in enumerate(keep, 1):
        flags = " ".join(x for x in ("REMOTE" if j["remote"] else "", "CHECK-LOCATION" if j.get("checkLocation") else "",
                                     "" if j["direct"] else f"via {j['publisher'] or j['source']}") if x)
        print(f'{i:>3}. [{age(j["posted"], now):>5}] {j["company"]} | {j["title"]} | {j["location"]}'
              f'{" | " + j["salary"] if j["salary"] else ""}{" | " + flags if flags else ""}\n'
              f'      {j["applyUrl"]}')
        if j["description"]:
            d = j["description"]
            print("      " + (d[:220] + "..." if len(d) > 220 else d))
    for j in keep:  # full text for Claude to read; capped so the file stays small
        j["description"] = j["description"][:6000]
        j["posted"] = j["posted"].isoformat() if j["posted"] else None
    LATEST.write_text(json.dumps({"fetchedAt": now.isoformat(), "hours": hours, "jobs": keep},
                                 indent=2, default=str, ensure_ascii=False) + "\n", encoding="utf-8")
    with jobboard.LOCK:  # every new job also goes on the job board page for the user to pick from
        board = jobboard.load_board()
        added = jobboard.sync_latest(board)
        jobboard.prune(board, applog.load()["jobs"])
        jobboard.save_board(board)
    if keep:
        print(f"Job board: {added} added, {len(keep) - added} already listed - the user picks what to apply to "
              f"(python jobboard.py queue).")


if __name__ == "__main__":
    main()
