"""Job feed - pull fresh job posts from Google Jobs through SerpApi (https://serpapi.com/google-jobs-api).

Google Jobs aggregates LinkedIn, Indeed, Glassdoor, ZipRecruiter and company career sites. Each search is one
(query, location) pair from ../private/job-search-config.json -> "jobFeed.serpapi" and costs one SerpApi request
per results page (10 jobs). Key: SERPAPI_API_KEY in ../private/.env (serpapi.com -> Dashboard -> API key).
Searches rotate across runs within maxRequestsPerRound; usage is counted against monthlyRequestBudget in
../private/jobfeed-state.json and paced so each day gets an even share of what is left of the month. LinkedIn search by Claude stays an optional extra source (jobboard.py add).

Usage (from the JobSearch folder):
  python jobfeed.py                  -> run the next searches, save every job Google returned to
                                        ../private/jobfeed-raw.json, print the new matching jobs (newest first),
                                        save them to ../private/jobfeed-latest.json and add them to the job board
  python jobfeed.py --hours 24       -> override jobFeed.postedWithinHours for this run
  python jobfeed.py --rejected       -> also list jobs that were filtered out, with the reason
  python jobfeed.py --plan           -> searches, key, budget used and SerpApi searches left, without searching
  python jobfeed.py --plan --json    -> the numbered searches, the next one and today's budget as JSON
  python jobfeed.py --search N       -> run only search N (the job board asks the user which search before each fetch)

"New" = passes the title / location / freshness / experience / skip-company filters and is not already in
../private/applications.json (same company+role or same job id, as in `applog.py check`) or skipped on the board.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import applog  # same folder: log + .env helpers
import jobboard  # same folder: the job board page's data (user's selected / skipped jobs)
import jobfilters  # same folder: the search filters as named checks
import jobinfo  # same folder: salary / experience from the job text

PRIVATE = applog.PRIVATE
CONFIG = PRIVATE / "job-search-config.json"
STATE = PRIVATE / "jobfeed-state.json"
LATEST = PRIVATE / "jobfeed-latest.json"
RAW = PRIVATE / "jobfeed-raw.json"  # every job Google returned in the last run, before any filter
UTC = dt.timezone.utc
SEARCH_URL = "https://serpapi.com/search.json"
ACCOUNT_URL = "https://serpapi.com/account.json"  # free: does not count as a search
# Apply links on these sites are aggregator copies; a link on any other site is taken as the company's own page.
AGGREGATORS = re.compile(r"linkedin\.|indeed\.|glassdoor\.|ziprecruiter\.|monster\.|simplyhired\.|careerbuilder\.|"
                         r"dice\.|talent\.com|jooble\.|adzuna\.|lensa\.|jobright\.|builtin\.|wellfound\.|snagajob\.|"
                         r"salary\.com|bebee\.|jobleads\.|teal\.|getwork\.|whatjobs\.|learn4good\.|tealhq\.", re.I)


# ---------- helpers ----------
def http_json(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": "offer-hunter-jobfeed/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:  # SerpApi explains errors in a JSON body ({"error": "..."})
        try:
            msg = json.load(e).get("error")
        except ValueError:
            msg = None
        raise RuntimeError(f"HTTP {e.code}: {msg or e.reason}") from None


def posted_time(text, now):
    """Google Jobs' relative 'posted_at' ('3 hours ago', '1 day ago', 'Just posted') -> aware UTC datetime or None."""
    t = (text or "").strip().lower()
    if not t:
        return None
    if t in ("just posted", "today", "just now"):
        return now
    m = re.match(r"(\d+)\+?\s*(minute|min|hour|hr|day|week|month)s?\s+ago", t)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    hours = {"minute": n / 60, "min": n / 60, "hour": n, "hr": n, "day": 24 * n, "week": 168 * n, "month": 720 * n}[unit]
    return now - dt.timedelta(hours=hours)


def pick_apply(options):
    """(link, direct?, site) - the company's own apply page when Google lists one, else the first aggregator link."""
    options = [o for o in options or [] if o.get("link")]
    for o in options:
        if not AGGREGATORS.search(o["link"]):
            return o["link"], True, o.get("title", "")
    if options:
        return options[0]["link"], False, options[0].get("title", "")
    return "", False, ""


def to_job(r, now):
    ext = dict(r.get("detected_extensions") or {})
    # Google Jobs often leaves detected_extensions empty and only lists the chips ('23 hours ago', '90K–112K a year',
    # 'Full-time', 'Work from home') in 'extensions'
    for e in r.get("extensions") or []:
        if not ext.get("posted_at") and (re.search(r"\bago$", e, re.I) or e.lower() in ("just posted", "today")):
            ext["posted_at"] = e
        elif not ext.get("salary") and re.search(r"\d.*\b(a|an|per)\s+(year|hour|month|week)\b", e, re.I):
            ext["salary"] = e
        elif not ext.get("schedule_type") and re.fullmatch(r"(full|part)[- ]time|contractor|internship|temp\w*", e, re.I):
            ext["schedule_type"] = e
        elif e.lower() == "work from home":
            ext["work_from_home"] = True
    link, direct, site = pick_apply(r.get("apply_options"))
    highlights = " ".join(f'{h.get("title", "")}: {" ".join(h.get("items", []))}' for h in r.get("job_highlights") or [])
    location = (r.get("location") or "").strip()
    remote = bool(ext.get("work_from_home")) or bool(re.search(r"\b(anywhere|remote)\b", location, re.I))
    raw_id = r.get("job_id") or f'{r.get("company_name")}|{r.get("title")}'
    return {"source": "serpapi", "id": hashlib.sha1(raw_id.encode("utf-8")).hexdigest()[:16],
            "company": (r.get("company_name") or "").strip(), "title": (r.get("title") or "").strip(),
            "location": location, "remote": remote, "posted": posted_time(ext.get("posted_at"), now),
            "postedText": ext.get("posted_at", ""), "url": r.get("share_link") or link, "applyUrl": link or r.get("share_link", ""),
            "salary": ext.get("salary", ""), "publisher": (r.get("via") or "").removeprefix("via ").strip() if not direct else site,
            "direct": direct, "description": re.sub(r"\s+", " ", f'{r.get("description", "")} {highlights}').strip(),
            "schedule": ext.get("schedule_type", "")}


def load_state():
    month = dt.date.today().strftime("%Y-%m")
    st = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    if st.get("month") != month:
        st = {"month": month, "requests": {}, "cursor": st.get("cursor", {})}
    return st


def save_state(st):
    STATE.write_text(json.dumps(st, indent=2) + "\n", encoding="utf-8")


def searches(sc):
    """Every (query, location) pair, locations interleaved so each run covers several areas; 'remoteQueries' run
    with location 'United States'."""
    locs = sc.get("locations", []) + ["United States"] * bool(sc.get("remoteQueries"))
    out = []
    for i, q in enumerate(sc.get("queries", [])):
        rq = sc.get("remoteQueries", [])
        out += [(rq[i % len(rq)] if loc == "United States" else q, loc) for loc in locs]
    return list(dict.fromkeys(out))  # each remote query once, not once per query


def budget(sc, state):
    """(requests allowed in this run, reason when 0) - maxRequestsPerRound, capped by maxRequestsPerDay and by an even
    daily share of what is left of monthlyRequestBudget. Starts a new day in `state`."""
    monthly = int(sc.get("monthlyRequestBudget", 0)) or None
    cap = int(sc.get("maxRequestsPerDay", 0)) or None
    used = state["requests"].get("serpapi", 0)
    today = dt.date.today().isoformat()
    if state.get("day", {}).get("date") != today:
        state["day"] = {"date": today, "requests": 0, "startUsed": used}
    left = int(sc.get("maxRequestsPerRound", 4))
    daily = cap
    if monthly:
        left = min(left, monthly - used)
        if left <= 0:
            return 0, f"monthly request budget used up ({used}/{monthly}) - no more searches until next month"
        # spread what is left of the month evenly: today's share = remaining at the start of today / days left
        nxt = (dt.date.today().replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        share = max(1, -(-(monthly - state["day"]["startUsed"]) // (nxt - dt.date.today()).days))
        daily = min(share, cap) if cap else share
    if daily:
        left = min(left, daily - state["day"]["requests"])
        if left <= 0:
            return 0, f"today's searches are used ({daily} requests/day) - more searches tomorrow"
    return left, ""


def plan_info(sc, state):
    """The searches (numbered from 1, as --search takes them), the next one in the rotation and today's budget - for the
    job board's 'which search?' prompt."""
    s = searches(sc)
    left, why = budget(sc, state)
    cur = state.get("cursor", {}).get("serpapi", 0) % max(len(s), 1)
    return {"searches": [{"n": i, "query": q, "location": loc} for i, (q, loc) in enumerate(s, 1)],
            "next": cur + 1 if s else None, "left": left, "blocked": why,
            "today": state["day"]["requests"], "maxPerDay": int(sc.get("maxRequestsPerDay", 0)) or None,
            "monthUsed": state["requests"].get("serpapi", 0), "monthly": int(sc.get("monthlyRequestBudget", 0)) or None}


# ---------- SerpApi ----------
def fetch(sc, state, key, log, raw=None, only=None):
    """Run the next searches within the round / day / month budget (only = run just that search, 0-based). Returns the
    jobs (dicts from to_job); the untouched Google results of each search are appended to `raw`."""
    now = dt.datetime.now(UTC)
    all_searches = searches(sc)
    if not all_searches:
        log("serpapi: no searches configured (jobFeed.serpapi.queries / locations)")
        return []
    last = state.get("lastRun", {}).get("serpapi")
    gap = float(sc.get("minHoursBetweenRuns", 0))
    if only is None and last and gap and now - dt.datetime.fromisoformat(last) < dt.timedelta(hours=gap):
        log(f"serpapi: skipped - last run less than {gap:g}h ago (jobFeed.serpapi.minHoursBetweenRuns)")
        return []
    left, why = budget(sc, state)
    if left <= 0:
        log(f"serpapi: {why}")
        return []
    cur = state.setdefault("cursor", {}).get("serpapi", 0) % len(all_searches)
    if only is not None:  # the search the user picked on the job board; the rotation continues after it
        cur = only % len(all_searches)
        order = [all_searches[cur]]
    else:
        order = all_searches[cur:] + all_searches[:cur]
    pages = max(1, int(sc.get("pagesPerSearch", 1)))
    jobs, done = [], 0
    for q, loc in order:
        if left <= 0:
            break
        params = {"engine": "google_jobs", "q": q, "location": loc, "hl": sc.get("hl", "en"), "gl": sc.get("gl", "us"),
                  "api_key": key}
        if sc.get("radiusKm"):
            params["lrad"] = sc["radiusKm"]
        for _ in range(pages):
            if left <= 0:
                break
            try:
                data = http_json(SEARCH_URL + "?" + urllib.parse.urlencode(params))
            except Exception as e:
                log(f"serpapi: '{q}' in {loc}: {e}")
                break
            if data.get("error"):  # e.g. "Google hasn't returned any results for this query."
                if "hasn't returned any results" not in data["error"]:
                    log(f"serpapi: '{q}' in {loc}: {data['error']}")
                break
            # only successful searches count toward the daily / monthly budget
            left -= 1
            state["requests"]["serpapi"] = state["requests"].get("serpapi", 0) + 1
            state["day"]["requests"] += 1
            res = data.get("jobs_results", [])
            if raw is not None:
                raw.append({"query": q, "location": loc, "jobs_results": res})
            jobs += [to_job(r, now) for r in res]
            log(f"serpapi: '{q}' in {loc}: {len(res)} jobs")
            token = (data.get("serpapi_pagination") or {}).get("next_page_token")
            if not token:
                break
            params["next_page_token"] = token
        done += 1
    state["cursor"]["serpapi"] = (cur + done) % len(all_searches)
    state.setdefault("lastRun", {})["serpapi"] = now.isoformat()
    return jobs


# ---------- filters ----------
def filter_jobs(jobs, ctx):
    """(kept jobs, rejected jobs with "reason" = the first filter they fail) - the filters are in jobfilters.py."""
    keep, rejected, seen = [], [], set()
    for j in jobs:
        key = (applog.norm(j["company"]), applog.norm(j["title"]))
        if key in seen:
            continue  # same job from two searches
        seen.add(key)
        flags, extras = jobfilters.check(j, ctx, jobboard.job_key(j))
        j["experience"] = extras["experience"]
        if extras["checkLocation"]:
            j["checkLocation"] = True  # no city given ("Hybrid", "United States"): keep, verify on the posting
        if flags:
            rejected.append({**j, "reason": jobfilters.reason(flags[0], j, ctx, extras)})
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
    ap.add_argument("--rejected", action="store_true")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--json", action="store_true", help="with --plan: the plan as JSON (used by the job board)")
    ap.add_argument("--search", type=int, help="run only this search (its number in --plan --json / the job board prompt)")
    a = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252; titles contain dashes, emoji, ...
        stream.reconfigure(encoding="utf-8", errors="replace")

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    fc = cfg.get("jobFeed")
    if not fc:
        sys.exit("No \"jobFeed\" section in private/job-search-config.json (template: examples/job-search-config.example.json).")
    sc = fc.get("serpapi", {})
    env = {k: v for k, (v, _q) in applog.read_env(applog.ENV).items()} if applog.ENV.exists() else {}
    key = env.get("SERPAPI_API_KEY") or os.environ.get("SERPAPI_API_KEY", "")
    hours = a.hours or fc.get("postedWithinHours") or cfg.get("postedWithinHours", 24)
    exp_cfg = cfg.get("experienceYears", {})
    max_years = exp_cfg.get("max", 2) if fc.get("experienceFilter", True) else None
    state = load_state()
    log = lambda m: print(m, file=sys.stderr)

    if a.plan and a.json:
        print(json.dumps(plan_info(sc, state)))
        return
    if a.plan:
        s = searches(sc)
        print(f"window: last {hours:g}h | experience filter: {'max ' + str(max_years) + ' yrs' if max_years is not None else 'off'}")
        print(f"serpapi: {'enabled' if sc.get('enabled', True) else 'disabled'}, key {'set' if key else 'MISSING (SERPAPI_API_KEY)'}, "
              f"{len(s)} searches, up to {sc.get('maxRequestsPerRound', 4)} requests per run, "
              f"{state.get('day', {}).get('requests', 0) if state.get('day', {}).get('date') == dt.date.today().isoformat() else 0}"
              f"/{sc.get('maxRequestsPerDay') or 'no cap'} today, "
              f"{state['requests'].get('serpapi', 0)}/{sc.get('monthlyRequestBudget', 'unlimited')} used in {state['month']}")
        cur = state.get("cursor", {}).get("serpapi", 0) % max(len(s), 1)
        for i, (q, loc) in enumerate(s[cur:] + s[:cur], 1):
            print(f"  {i:>2}. {q} | {loc}")
        if key:
            try:
                acc = http_json(ACCOUNT_URL + "?" + urllib.parse.urlencode({"api_key": key}))
                print(f"SerpApi account: {acc.get('plan_name', '?')}, {acc.get('total_searches_left', '?')} searches left "
                      f"this month ({acc.get('this_month_usage', '?')} used)")
            except Exception as e:
                print(f"SerpApi account check failed: {e}")
        return

    if not sc.get("enabled", True):
        sys.exit("jobFeed.serpapi.enabled is false - no job API to fetch from (LinkedIn search: autoApplyLoop step 2b).")
    if not key:
        sys.exit("SERPAPI_API_KEY is missing in private/.env - get a key at serpapi.com (Dashboard -> API key).")
    raw = []
    if a.search is not None and not 1 <= a.search <= len(searches(sc)):
        sys.exit(f"--search must be 1..{len(searches(sc))} (python jobfeed.py --plan --json lists them)")
    jobs = fetch(sc, state, key, log, raw, None if a.search is None else a.search - 1)
    save_state(state)
    if not raw:  # no search ran (budget used, error): keep the last results instead of overwriting them with nothing
        sys.exit("No search ran - see the message above. The last results were kept.")
    RAW.write_text(json.dumps({"fetchedAt": dt.datetime.now(UTC).isoformat(), "searches": raw}, indent=2, ensure_ascii=False)
                   + "\n", encoding="utf-8")

    now = dt.datetime.now(UTC)
    board = jobboard.load_board()
    skipped = {k for k, j in board["jobs"].items() if j.get("choice") == "skipped"}
    ctx = jobfilters.context(cfg, env, skipped_keys=skipped, hours=hours, max_years=max_years, now=now)
    keep, rejected = filter_jobs(jobs, ctx)

    if a.rejected:
        counts = {}
        for r in rejected:
            counts[r["reason"]] = counts.get(r["reason"], 0) + 1
        print("Filtered out: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda x: -x[1])))
        for r in rejected:
            if not r["reason"].startswith(("older than", "title not a target role")):
                print(f'  - [{r["reason"]}] {r["company"]} | {r["title"]} | {r["location"]} | {r["applyUrl"]}')
    if not keep:
        print(f"No new matching jobs posted in the last {hours:g}h ({len(jobs)} fetched). "
              "Run with --rejected to see why jobs were filtered out, or --hours 48 for a wider window.")
    else:
        print(f"{len(keep)} new matching jobs posted in the last {hours:g}h (of {len(jobs)} fetched):")
    for i, j in enumerate(keep, 1):
        flags = " ".join(x for x in ("REMOTE" if j["remote"] else "", "CHECK-LOCATION" if j.get("checkLocation") else "",
                                     "" if j["direct"] else f"via {j['publisher'] or 'aggregator'}") if x)
        print(f'{i:>3}. [{age(j["posted"], now):>5}] {j["company"]} | {j["title"]} | {j["location"]}'
              f'{" | " + j["salary"] if j["salary"] else ""}{" | " + flags if flags else ""}\n'
              f'      {j["applyUrl"]}')
        if j["description"]:
            d = j["description"]
            print("      " + (d[:220] + "..." if len(d) > 220 else d))
    # jobFeed.keepAll: every job Google returned goes on the board; the ones a filter would drop carry the reason
    board_jobs = keep + ([{**{k: v for k, v in r.items() if k != "reason"}, "feedNote": r["reason"]} for r in rejected]
                         if fc.get("keepAll", False) else [])
    for j in board_jobs:  # full text for Claude to read; capped so the file stays small
        j["salary"] = jobinfo.salary_from(j["salary"]) or j["salary"] or jobinfo.salary_from(j["description"])
        j["description"] = j["description"][:6000]
        j["posted"] = j["posted"].isoformat() if j["posted"] else None
    LATEST.write_text(json.dumps({"fetchedAt": now.isoformat(), "hours": hours, "jobs": board_jobs},
                                 indent=2, default=str, ensure_ascii=False) + "\n", encoding="utf-8")
    with jobboard.LOCK:  # every new job also goes on the job board page for the user to pick from
        board = jobboard.load_board()
        added = jobboard.sync_latest(board)
        jobboard.prune(board, applog.load()["jobs"])
        jobboard.save_board(board)
    if board_jobs:
        print(f"Job board: {added} added, {len(board_jobs) - added} already listed"
              f"{f' ({len(board_jobs) - len(keep)} marked with the filter that would drop them)' if len(board_jobs) > len(keep) else ''}"
              f" - the user picks what to apply to (python jobboard.py queue).")


if __name__ == "__main__":
    main()
