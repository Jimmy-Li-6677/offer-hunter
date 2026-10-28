"""Jobs from the simplify.jobs website search (its Typesense search API) for the job board.

The job board's "Search Simplify website" button opens a dialog: the X-TYPESENSE-API-KEY header the simplify.jobs
page sends (Chrome DevTools > Network > Fetch/XHR > multi_search > Request Headers - the value, the header line or the
whole copied header block), saved in ..\\private\\.env as SIMPLIFY_TYPESENSE_KEY (blank reuses the saved one), and
search options: posted within, locations, experience level, work mode, minimum salary, role searches, search filters
on/off. The board's own server then runs this: all the role searches at once, filtered on the search server (US, the
chosen levels, software job categories, posted within the window), merged into one newest-first list, batchSize (250)
different jobs per click - each click takes the next older 250 (the cursor is kept in the job board file) until the
window has no older jobs, then the next click starts again from the newest. Every fetched job that matches the options, passes the search filters
(jobfilters.py) and is not on the board yet is added - no cap.

The API is undocumented: it can change at any time. Field names (query_by, filter_by) and the queries live in
browserSearch.simplifySite in the config, so they can be fixed without code changes; `--raw` prints one result
document to see the real field names.

Usage (from the JobSearch folder):
  python simplifysite.py            -> fetch the next batch and add the candidates (like the button; saved key, default options)
  python simplifysite.py --dry-run  -> only print what the first batch would add (the cursor is not used or moved)
  python simplifysite.py --raw      -> print the first result document as returned by the API (to see its fields)
"""
import argparse
import concurrent.futures
import datetime as dt
import html
import json
import re
import urllib.error
import urllib.request

import applog  # same folder: log + .env helpers
import jobfilters  # same folder: the search filters
import jobinfo  # same folder: salary / work mode from the job text
import simplifyjobs  # same folder: same_job (company + title match)

KEY_NAME = "SIMPLIFY_TYPESENSE_KEY"
CURSOR = "simplifySiteCursor"  # job board file key: where the next click's batch starts
HOSTS = ["js-ha.simplify.jobs", "js1.simplify.jobs", "js2.simplify.jobs", "js3.simplify.jobs"]
DEFAULTS = {
    "collection": "jobs",
    "queries": ["software engineer", "software developer", "full stack engineer", "backend engineer",
                "frontend engineer", "data engineer", "QA engineer", "devops engineer", "machine learning engineer"],
    "queryBy": "title",
    "filterBy": "",
    "perPage": 250,
    "batchSize": 250,
    "sortBy": "start_date:desc",
    "postedField": "start_date",
    "levelField": "experience_level",
    "countryField": "physical_countries",
    # Simplify's job categories ("functions" on each result); only jobs tagged with one of these are kept
    "functionsField": "functions",
    "functions": ["Software Engineering", "Backend Engineering", "Full-Stack Engineering", "Frontend Engineering",
                  "Mobile Engineering", "Systems Software Engineering", "AI Application Engineering",
                  "AI & Machine Learning", "Machine Learning Engineering", "Computer Vision", "Data Engineering",
                  "QA & Testing", "Manual & Functional QA", "Test Automation Engineering", "DevOps & Infrastructure",
                  "DevOps Engineering", "Platform Engineering", "Site Reliability Engineering", "Cloud Engineering"],
}
UTC = dt.timezone.utc
# simplify.jobs experience_level values, in order; the dialog's default = the entry-level ones
LEVELS = ["Entry Level/New Grad", "Junior", "Mid Level", "Senior", "Expert or higher"]
DEFAULT_LEVELS = ["Entry Level/New Grad", "Junior"]
MODES = {"remote": "Remote", "hybrid": "Hybrid", "in person": "On-site", "onsite": "On-site", "on-site": "On-site"}


class KeyRejected(Exception):
    """The search server refused the key (401 / 403): a fresh one has to be copied from the simplify.jobs page."""


# ---------- the key in private/.env ----------
def saved_key():
    return applog.read_env(applog.ENV).get(KEY_NAME, ("", ""))[0] if applog.ENV.exists() else ""


def key_from(text):
    """The key from what the user pasted: the bare value, or a copied header line / header block
    ("x-typesense-api-key: abc123" among other request headers)."""
    text = (text or "").strip()
    m = re.search(r"x-typesense-api-key\s*:\s*[\"']?([A-Za-z0-9+/=_\-]+)", text, re.I)
    if m:
        return m.group(1)
    if re.search(r"[\r\n]|:\s", text):
        raise ValueError("no X-TYPESENSE-API-KEY line in what you pasted - copy it from Request Headers of the "
                         "multi_search request")
    return text


def save_key(key):
    """Store the key as SIMPLIFY_TYPESENSE_KEY in private/.env (and the empty key in examples/.env.example)."""
    key = key_from(key)
    # plain keys are letters / digits; scoped keys (what simplify.jobs uses) are long base64 ending in "="
    if not re.fullmatch(r"[A-Za-z0-9+/=_\-]{8,4096}", key):
        raise ValueError("that does not look like a Typesense key (letters, digits, + / = only - no spaces or quotes)")
    for path, value in ((applog.ENV, key), (applog.ENV_EXAMPLE, "")):
        if not path.exists():
            continue
        raw = path.read_bytes().decode("utf-8")
        eol = "\r\n" if "\r\n" in raw else "\n"
        lines = raw.splitlines()
        for i, line in enumerate(lines):
            if line.startswith(f"{KEY_NAME}="):
                if path == applog.ENV_EXAMPLE:
                    break
                lines[i] = f"{KEY_NAME}={value}"
                break
        else:
            lines += ["", "# simplify.jobs website search (job board button): X-TYPESENSE-API-KEY header from the page",
                      f"{KEY_NAME}={value}"]
        path.write_bytes((eol.join(lines) + eol).encode("utf-8"))


# ---------- the API ----------
def settings(cfg):
    return {**DEFAULTS, **cfg.get("browserSearch", {}).get("simplifySite", {})}


def post(key, searches):
    """One multi_search request; returns its results (one per search)."""
    body = json.dumps({"searches": searches}).encode("utf-8")
    last = None
    for host in HOSTS:
        req = urllib.request.Request(f"https://{host}/multi_search?use_cache=true&cache_ttl=60", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-TYPESENSE-API-KEY": key,
                                              "User-Agent": "offer-hunter-jobboard"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8")).get("results", [])
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise KeyRejected("Simplify rejected the key - copy a fresh X-TYPESENSE-API-KEY from the simplify.jobs page")
            raise RuntimeError(f"Simplify search HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
        except urllib.error.URLError as e:  # try the next host
            last = e
    raise RuntimeError(f"Simplify search unreachable: {last}")


def server_filter(s, o, hours=None):
    """filter_by for the search server, so the result pages hold only the jobs the options want: US jobs, the chosen
    experience levels, software job categories (functions), posted (start_date) within the chosen hours. Location
    areas / pay / work mode are checked here afterwards (see wanted)."""
    parts = [s["filterBy"]] if s.get("filterBy") else []
    if o is not None:
        parts.append(f"{s['countryField']}:=`United States`")
        if o["levels"]:
            parts.append(f"{s['levelField']}:=[" + ",".join(f"`{lv}`" for lv in o["levels"]) + "]")
        if s.get("functions"):
            parts.append(f"{s['functionsField']}:=[" + ",".join(f"`{f}`" for f in s["functions"]) + "]")
        since = int(dt.datetime.now(UTC).timestamp() - (hours or o["hours"]) * 3600)
        parts.append(f"{s['postedField']}:>={since}")
    return " && ".join(parts)


def signature(s, o):
    """What a batch series depends on: a click with other role searches / levels / window starts a new series."""
    return json.dumps([s["queries"], o["levels"] if o else None, o["hours"] if o else None, s.get("functions")])


def search(key, s, o=None, cursor=None):
    """The next batch: up to batchSize DIFFERENT jobs from all the role searches together, newest first, each posted
    no later than the oldest job of the last batch, filtered on the server (server_filter: US, levels, software
    categories, posted within the window). Returns (result documents, new cursor). The cursor ({sig, before: the
    oldest start_date fetched so far, edge: ids fetched at exactly that time, batch, newest, oldest, beyond, done})
    moves back in time with every click. When the posted-within window has no older jobs, the next clicks go on past
    it (beyond = True: older posts, up to maxAgeDays (30) old), so no click fetches the same jobs again; after that
    the series is done and the next click starts again from the newest. Other options (sig) start a new series."""
    sig = signature(s, o)
    fresh = not cursor or cursor.get("sig") != sig or cursor.get("done") or "before" not in cursor
    c = {"sig": sig, "before": None, "edge": [], "batch": 0} if fresh else dict(cursor)
    size = int(s.get("batchSize", s["perPage"]))
    base = {"collection": s["collection"], "query_by": s["queryBy"], "per_page": min(int(s["perPage"]), 250), "page": 1}
    max_hours = float(s.get("maxAgeDays", 30)) * 24
    beyond = bool(c.get("beyond")) and o is not None and o["hours"] < max_hours
    filt = [f for f in (server_filter(s, o, max_hours if beyond else None),) if f]
    if c["before"] is not None:
        filt.append(f"{s['postedField']}:<={int(c['before'])}")
    if filt:
        base["filter_by"] = " && ".join(filt)
    if s.get("sortBy"):
        base["sort_by"] = s["sortBy"]
    results = post(key, [{**base, "q": q} for q in s["queries"]])
    pool, more = {}, False
    for q, res in zip(s["queries"], results):
        if res.get("error"):
            raise RuntimeError(f"Simplify search error ({q}): {res['error']} (fix browserSearch.simplifySite in the "
                               "config; `python simplifysite.py --raw` shows the real field names)")
        more = more or res.get("found", 0) > base["per_page"]
        for h in res.get("hits", []):
            d = h.get("document", {})
            if d.get("id") and d["id"] not in c["edge"]:
                pool[d["id"]] = d
    when = lambda d: d.get(s["postedField"]) or 0
    docs = sorted(pool.values(), key=when, reverse=True)[:size]
    c["done"] = not docs or (not more and len(pool) <= size)
    c["beyond"] = c["past"] = beyond  # past = this batch is from before the window
    if c["done"] and not beyond and o is not None and o["hours"] < max_hours:
        c["done"], c["beyond"] = False, True  # window used up: the next click goes on with older posts
    if docs:
        oldest = when(docs[-1])
        c["edge"] = (c["edge"] if oldest == c["before"] else []) + [d["id"] for d in docs if when(d) == oldest]
        c["before"], c["batch"] = oldest, c["batch"] + 1
        c["newest"], c["oldest"] = when(docs[0]), oldest
    return docs, c


def progress(c, s, fetched):
    """Text for the search note: what this batch fetched and where the next click goes on."""
    day = lambda t: dt.datetime.fromtimestamp(t, UTC).strftime("%m/%d %H:%M") if t else "?"
    msg = f"batch {c.get('batch', 0)}: fetched {fetched} different jobs"
    if fetched:
        msg += f" posted {day(c.get('oldest'))} - {day(c.get('newest'))} UTC"
    if c.get("past"):
        msg += " (older than the posted-within window)"
    if c["done"]:
        return msg + " - no older jobs left, the next click starts again from the newest"
    if c.get("beyond") and not c.get("past"):
        return msg + " - posted-within window used up, the next click goes on with older posts"
    return msg + " - the next click fetches the next older jobs"


# ---------- documents -> job board jobs (field names guessed with fallbacks) ----------
def first(d, *names):
    for n in names:
        v = d.get(n)
        if v not in (None, "", [], {}):
            return v
    return None


def text(v):
    if isinstance(v, dict):
        return first(v, "name", "value", "label", "title") or ""
    if isinstance(v, list):
        return "; ".join(t for t in (text(x) for x in v) if t)
    return "" if v is None else str(v)


def when(v):
    """Posted time from an epoch (seconds or ms) or an ISO string."""
    if isinstance(v, (int, float)) and v > 0:
        return dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v, UTC).isoformat(timespec="seconds")
    if isinstance(v, str) and v:
        try:
            return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(UTC).isoformat(timespec="seconds")
        except ValueError:
            return None
    return None


def salary(d):
    lo, hi = first(d, "min_salary", "salary_min", "minSalary"), first(d, "max_salary", "salary_max", "maxSalary")
    if lo or hi:
        f = lambda x: f"${x:,.0f}" if isinstance(x, (int, float)) else str(x)
        return " - ".join(f(x) for x in (lo, hi) if x)
    return text(first(d, "salary", "salary_range", "compensation"))


def to_job(d):
    jid = str(first(d, "id", "job_id", "uuid") or "")
    location = text(first(d, "locations", "location", "location_names", "city"))
    remote = bool(re.search(r"\bremote\b", location, re.I)) or first(d, "remote", "is_remote") is True
    raw_level = first(d, "experience_level", "experience_levels", "seniority", "level")
    levels = [text(x) for x in (raw_level if isinstance(raw_level, list) else [raw_level] if raw_level else [])]
    level = "; ".join(levels)
    mode = MODES.get(text(first(d, "travel_requirements", "work_mode", "location_type")).strip().lower(), "")
    years = first(d, "min_years_experience", "years_of_experience", "min_experience")
    facts = [x for x in (level and f"level {level}", years is not None and f"{years}+ yrs") if x]
    desc = text(first(d, "description", "summary", "requirements"))
    return {
        "source": "simplifysite", "id": jid, "company": text(first(d, "company_name", "company", "organization")),
        "title": text(first(d, "title", "job_title", "name")), "location": location,
        "workMode": mode or ("Remote" if remote and ";" not in location else ""),
        "remote": mode == "Remote" or (not mode and remote and ";" not in location), "levels": levels,
        "functions": [text(x) for x in d.get("functions") or []],
        "salary": salary(d),
        "posted": when(first(d, "posted_date", "date_posted", "start_date", "created_date", "posted_at", "created_at",
                             "updated_date")),  # start_date = posted; updated_date = last refreshed by Simplify
        "url": f"https://simplify.jobs/p/{jid}" if jid else "",
        "applyUrl": text(first(d, "url", "apply_url", "application_url", "job_url")),
        "description": "simplify.jobs website search" + (": " + "; ".join(facts) if facts else "") + ". "
                       + (desc[:600] if desc else "Requirements (years, clearance, pay) not verified - check the posting."),
    }


def options(raw, cfg):
    """The search options from the button's dialog, checked and filled with the config defaults:
    hours (posted within), limit (max jobs to add, 0 = no cap), areas (names from `locations`, [] = all), workModes
    ([] = any; a job that doesn't state one counts as hybrid / on-site), minPay (yearly $, 0 = any), payListed (only jobs with pay),
    includeNoPay (with minPay: keep jobs without pay), searchFilters (false = only drop old / already logged jobs),
    queries (role searches, [] = browserSearch.simplifySite.queries), levels (simplify.jobs experience levels,
    [] = any)."""
    raw, bs = raw or {}, cfg.get("browserSearch", {})
    num = lambda k, d, lo, hi: min(max(float(raw.get(k) or d), lo), hi)
    names = {loc["name"] for loc in cfg.get("locations", [])}
    words = lambda k: [str(x).strip() for x in raw.get(k) or [] if str(x).strip()]
    return {
        "hours": num("hours", bs.get("simplifySite", {}).get("postedWithinHours", bs.get("postedWithinHours", 24)),
                     1, 24 * 30),
        "limit": int(num("limit", 0, 0, 10000)),
        "areas": [a for a in words("areas") if a in names],
        "workModes": [m for m in words("workModes") if m in ("Remote", "Hybrid", "On-site")],
        "minPay": num("minPay", 0, 0, 1000000),
        "payListed": bool(raw.get("payListed")),
        "includeNoPay": raw.get("includeNoPay", True) is not False,
        "searchFilters": raw.get("searchFilters", True) is not False,
        "queries": words("queries")[:20],
        "levels": [lv for lv in words("levels") if lv in LEVELS] if "levels" in raw else list(DEFAULT_LEVELS),
    }


def summary(o):
    """Short text of the chosen options for the search note."""
    parts = [f"last {o['hours']:g}h", ", ".join(o["areas"]) or "all locations"]
    if o["levels"] and len(o["levels"]) < len(LEVELS):
        parts.append("/".join(o["levels"]))
    if o["workModes"]:
        parts.append("/".join(o["workModes"]))
    if o["minPay"]:
        parts.append(f"${o['minPay'] / 1000:g}K+" + ("" if o["includeNoPay"] else " (pay listed)"))
    elif o["payListed"]:
        parts.append("pay listed")
    if not o["searchFilters"]:
        parts.append("search filters off")
    return ", ".join(parts)


def wanted(j, o, ctx):
    """The dialog's location / work mode / salary / experience level choices, and a software job category."""
    if o.get("functions") and j.get("functions") and not set(j["functions"]) & set(o["functions"]):
        return False
    if o["levels"] and j.get("levels") and not set(j["levels"]) & set(o["levels"]):
        return False
    if o["areas"] and not set(jobfilters.areas(j, ctx)) & set(o["areas"]):
        return False
    mode = j.get("workMode") or jobinfo.work_mode(j)
    if o["workModes"]:
        # not stated: a city without "remote" is hybrid or on-site, whichever is picked
        modes = {mode} if mode else {"Hybrid", "On-site"}
        if not modes & set(o["workModes"]):
            return False
    pay = jobinfo.salary_annual(j.get("salary") or "")
    if pay is None:
        return not o["payListed"] and (not o["minPay"] or o["includeNoPay"])
    return pay[1] >= o["minPay"]


NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def details(job_id):
    """The posting text from the job's simplify.jobs page (the search API has no description): requirements first
    (years / clearance are read from them), then preferred qualifications and the description. '' if unavailable."""
    req = urllib.request.Request(f"https://simplify.jobs/p/{job_id}", headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            m = NEXT_DATA.search(r.read().decode("utf-8", "replace"))
        post = json.loads(m.group(1))["props"]["pageProps"]["jobPosting"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return ""
    plain = lambda v: re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(v or "")))).strip()
    parts = [("Requirements", "; ".join(post.get("requirements") or [])),
             ("Preferred", "; ".join(post.get("desirable") or [])), ("Description", plain(post.get("description"))[:2500])]
    return " ".join(f"{k}: {v}" for k, v in parts if v)


def add_details(jobs, workers=8):
    """Put each job's posting text (details) into its description, several pages at a time."""
    with concurrent.futures.ThreadPoolExecutor(workers) as ex:
        for j, text in zip(jobs, ex.map(lambda j: details(j["id"]), jobs)):
            if text:
                j["description"] = j["description"].split(". Requirements (years", 1)[0] + ". " + text


def candidates(docs, board_jobs, cfg, opts=None, total=None):
    """Jobs to add: posted in the last opts.hours hours (default browserSearch.postedWithinHours), pass the search
    filters and the dialog's choices, not on the board yet, newest first, at most opts.limit (0 = all)."""
    o = {"functions": settings(cfg)["functions"], **(opts or options({}, cfg))}
    hours, limit = o["hours"], o["limit"]
    # jobs from past the window (a series going on with older posts) are not dropped as old
    oldest = min((d.get(settings(cfg)["postedField"]) or 0 for d in docs), default=0)
    if oldest:
        hours = max(hours, (dt.datetime.now(UTC).timestamp() - oldest) / 3600 + 1)
    seen = {simplifyjobs.same_job(j) for j in board_jobs.values()}
    pool = []
    for j in map(to_job, docs):
        k = simplifyjobs.same_job(j)
        if j["title"] and j["company"] and f"simplifysite:{j['id']}" not in board_jobs and k not in seen:
            seen.add(k)
            pool.append(j)
    pool.sort(key=lambda j: j["posted"] or "", reverse=True)
    ctx = jobfilters.context(cfg=cfg, hours=hours)
    hard = {"old", "logged"}  # dropped even with the search filters off
    ok = lambda j: not [f for f in jobfilters.check(j, ctx)[0] if o["searchFilters"] or f in hard]
    found = [j for j in pool if wanted(j, o, ctx) and ok(j)]
    add_details(found)  # the posting text: years required / clearance can now be checked
    found = [j for j in found if ok(j)][:limit or None]
    for j in found:
        age = (dt.datetime.now(UTC) - dt.datetime.fromisoformat(j["posted"])).total_seconds() / 3600 if j["posted"] else None
        tag = f"last {o['hours']:g}h" if age is None or age <= o["hours"] else f"posted {age / 24:.1f} days ago"
        j["description"] = f"[{tag}] " + j["description"]
    return found, len(docs) if total is None else total, hours


def run(board_jobs, cfg=None, key=None, opts=None, cursor=None):
    """One click: the next batch (search) turned into candidates. Returns (jobs to add, note text, new cursor)."""
    cfg = cfg if cfg is not None else json.loads(jobfilters.CONFIG.read_text(encoding="utf-8"))
    key = key or saved_key()
    if not key:
        raise KeyRejected("no Simplify key saved - click the button and paste the X-TYPESENSE-API-KEY header")
    o = options(opts, cfg)
    s = settings(cfg)
    if o["queries"]:
        s["queries"] = o["queries"]
    docs, c = search(key, s, o, cursor)
    return candidates(docs, board_jobs, cfg, o)[0], progress(c, s, len(docs)), c


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--raw", action="store_true", help="print the first result document and stop")
    a = ap.parse_args()
    cfg = json.loads(jobfilters.CONFIG.read_text(encoding="utf-8"))
    if a.raw:
        docs = search(saved_key(), settings(cfg))[0]
        print(f"{len(docs)} documents; first one:")
        print(json.dumps(docs[0] if docs else {}, indent=2)[:4000])
        return
    import jobboard  # same folder
    with jobboard.LOCK:
        board = jobboard.load_board()
    jobs, note, c = run(board["jobs"], cfg, cursor=None if a.dry_run else board.get(CURSOR))
    for j in jobs:
        print(f'{j["posted"]}  {j["company"]} | {j["title"]} | {j["location"]}\n    {j["applyUrl"] or j["url"]}')
    if a.dry_run:
        print(f"{len(jobs)} candidates - {note} - dry run, nothing added")
        return
    with jobboard.LOCK:
        board = jobboard.load_board()
        n = jobboard.merge(board, jobs)
        board[CURSOR] = c
        jobboard.save_board(board)
    print(f"{n} new jobs added to the board - {note}")


if __name__ == "__main__":
    main()
