"""Jobs straight from big companies' own career sites (Fortune 500 and other large employers) - plain code, no AI.

The job board's "Search company sites" button runs this in the board's own server (no Chrome, no Claude): for every
company in companyJobs.companies (job-search-config.json) it asks the company's careers site for jobs matching each of
companyJobs.queries, through the site's public job API:
  workday     Workday career sites (most Fortune 500 companies): <site>/wday/cxs/<tenant>/<site>/jobs
  greenhouse  boards-api.greenhouse.io        lever   api.lever.co        ashby   api.ashbyhq.com
  oracle      Oracle Recruiting Cloud (e.g. JPMorgan Chase)
  amazon      amazon.jobs search              microsoft   apply.careers.microsoft.com search
It keeps jobs posted in the last companyJobs.postedWithinHours hours whose title / location pass the search filters
(jobfilters.py) and that are not on the board yet, reads each one's posting (qualifications, responsibilities,
description) from the same site, checks the filters again (years required, clearance) and adds up to
companyJobs.maxJobs of the newest as candidates. Each job links to the company's own posting.

  python companyjobs.py --dry-run       what would be added
  python companyjobs.py                 add them to the board now (same as the button)
  python companyjobs.py --check         test every company's site (are the API settings right?)
  python companyjobs.py --discover "Company A" "Company B" [--employees 10,001+] [--write]
                                        find each company's job board (Greenhouse / Lever / Ashby / Workday)
  python companyjobs.py --discover-file ../examples/large-companies.txt --employees 10,001+ [--write]
  python companyjobs.py --add "<job link>" --name "Company" [--write]   add one from any of its job links

Any career, not only software: the role is in the config - companyJobs.queries (what each site is searched for) and
companyJobs.titleInclude (a regex every kept title must match).
"""
import argparse
import concurrent.futures
import datetime as dt
import html
import http.client
import json
import re
import urllib.parse
import urllib.request

import applog  # same folder: log helpers
import jobfilters  # same folder: the search filters

UTC = dt.timezone.utc
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
# Works for any career: the role words live in the config (companyJobs.queries = what each site is searched for,
# companyJobs.titleInclude = a regex every kept title must match, e.g. software titles only, or banking titles only).
# These are only fallbacks when the config leaves them out.
DEFAULTS = {"queries": ["software engineer"], "postedWithinHours": 72, "maxJobs": 25, "maxPerCompany": 3, "pages": 2,
            "titleInclude": "", "minEmployees": 0,
            # Workday / Oracle / Amazon / Microsoft / own career sites only offer a keyword search that returns the best
            # few dozen results, so "Backend Developer, Palantir" is not found by "software engineer": every one of them is
            # also searched with these extra title words (Greenhouse / Lever / Ashby return the whole board anyway)
            "siteQueries": ["developer", "engineer", "backend developer", "frontend developer", "full stack", "data engineer"]}


def _get(url, body=None, timeout=30):
    headers = dict(UA)
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _try(url, body=None):
    try:
        return _get(url, body)
    except (OSError, ValueError, http.client.HTTPException):
        return None


def _plain(v):
    """HTML as one line of plain text, list items / paragraphs kept apart with '; '."""
    v = re.sub(r"<\s*(?:li|br|p|div|h\d)[^>]*>", "; ", html.unescape(str(v or "")), flags=re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", v))).strip(" ;")


def _iso(d):
    return d.astimezone(UTC).isoformat(timespec="seconds") if d else None


def _days_ago(n):
    """Date-only postings ("Posted Yesterday") are stored as the END of that day (23:59 UTC; never later than now), so a job
    posted late yesterday is not missed by a round that looks at the time since the previous round."""
    return _iso(dt.datetime.now(UTC).replace(hour=23, minute=59, second=0, microsecond=0) - dt.timedelta(days=n))


def _is_older(posted, cutoff):
    """True when a posting's time is known and before the cutoff (no cutoff / unknown time: False)."""
    if not cutoff or not posted:
        return False
    try:
        return dt.datetime.fromisoformat(posted) < cutoff
    except ValueError:
        return False


def _not_future(iso):
    """Date-only postings ("Posted Today", "2026-11-06") are stored as noon UTC of that day, which is
    still in the future late in the US evening: never store a posted time later than now."""
    if not iso:
        return iso
    try:
        d = dt.datetime.fromisoformat(iso)
    except ValueError:
        return iso
    now = dt.datetime.now(UTC)
    return _iso(now) if d.tzinfo and d > now else iso


def _job(c, jid, title, location, posted, url, text="", extra=None):
    posted = _not_future(posted)
    j = {"source": "company", "id": f'{re.sub(r"[^a-z0-9]+", "", c["name"].lower())}:{jid}', "company": c["name"],
         "title": title.strip(), "location": (location or "").strip(), "posted": posted, "url": url, "applyUrl": url,
         "remote": bool(re.search(r"\bremote\b", location or "", re.I)), "workMode": "", "salary": "",
         "ats": c["ats"], "_text": text}
    if c.get("employees"):
        j["companySize"] = c["employees"]  # the card shows it; the board's Company size filter uses it
    j.update(extra or {})
    return j


# ---------- Workday ----------

def _wd_parts(c):
    m = re.match(r"https://(([^./]+)\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)", c["site"])
    if not m:
        raise ValueError(f'{c["name"]}: workday site must look like https://<tenant>.wdN.myworkdayjobs.com/<site>')
    return m.group(1), m.group(2), m.group(3)


def _wd_posted(s):
    s = (s or "").lower()
    if "today" in s:
        return _days_ago(0)
    if "yesterday" in s:
        return _days_ago(1)
    m = re.search(r"(\d+)\+?\s*days?", s)
    return _days_ago(int(m.group(1))) if m else None


def workday(c, query, pages, cutoff=None):
    """cutoff (a scheduled round: only jobs posted since the last round): the listing is NOT always newest-first (Adobe and
    Boeing rank by relevance, Capital One by date), so every page of the search is read (up to `pages`) and the job dates are
    compared afterwards; the search stops early only when the listing has proved to be date-sorted (no page so far had a job
    newer than the one before it) and a whole page is older than the cutoff."""
    host, tenant, site = _wd_parts(c)
    out, breaks, prev_rank = [], 0, None
    for page in range(pages):
        d = _try(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                 {"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": query})
        posts = (d or {}).get("jobPostings") or []
        page_jobs = []
        for p in posts:
            path = p.get("externalPath") or ""
            if not path:
                continue
            j = _job(c, path.rsplit("_", 1)[-1] if "_" in path else path, p.get("title", ""),
                     p.get("locationsText", ""), _wd_posted(p.get("postedOn")), f"https://{host}/{site}{path}",
                     extra={"_detail": f"https://{host}/wday/cxs/{tenant}/{site}{path}"})
            page_jobs.append(j)
            rank = j["posted"] or ""
            if prev_rank is not None and rank > prev_rank:
                breaks += 1  # a job newer than the one before it: not date-sorted
            prev_rank = rank
        out += page_jobs
        if len(posts) < 20:
            break
        if cutoff and page_jobs and not breaks and all(_is_older(j["posted"], cutoff) for j in page_jobs):
            break
    return out


def workday_detail(j):
    d = _try(j["_detail"]) or {}
    info = d.get("jobPostingInfo") or {}
    locs = [info.get("location")] + list(info.get("additionalLocations") or [])
    if re.fullmatch(r"\d+\s+Locations?", j["location"] or "", re.I) and any(locs):
        j["location"] = "; ".join(l for l in locs if l)
    if info.get("startDate") and not j.get("posted"):
        j["posted"] = _not_future(info["startDate"] + "T12:00:00+00:00")
    return _plain(info.get("jobDescription"))


# ---------- Greenhouse / Lever / Ashby ----------

def greenhouse(c, query, pages, cutoff=None):
    d = _try(f'https://boards-api.greenhouse.io/v1/boards/{c["board"]}/jobs?content=true') or {}
    out = []
    for p in d.get("jobs") or []:
        if not _words_match(query, p.get("title", "")):
            continue
        out.append(_job(c, p["id"], p.get("title", ""), (p.get("location") or {}).get("name", ""),
                        p.get("first_published") or p.get("updated_at"), p.get("absolute_url", ""),
                        text=_plain(html.unescape(p.get("content") or ""))))
    return out


def lever(c, query, pages, cutoff=None):
    d = _try(f'https://api.lever.co/v0/postings/{c["board"]}?mode=json') or []
    out = []
    for p in d if isinstance(d, list) else []:
        if not _words_match(query, p.get("text", "")):
            continue
        lists = " ".join(f'{x.get("text", "")}: {_plain(x.get("content"))}.' for x in p.get("lists") or [])
        posted = _iso(dt.datetime.fromtimestamp(p["createdAt"] / 1000, UTC)) if p.get("createdAt") else None
        out.append(_job(c, p["id"], p.get("text", ""), (p.get("categories") or {}).get("location", ""), posted,
                        p.get("hostedUrl", ""), text=f'{p.get("descriptionPlain", "")} {lists}'))
    return out


def ashby(c, query, pages, cutoff=None):
    d = _try(f'https://api.ashbyhq.com/posting-api/job-board/{c["board"]}') or {}
    out = []
    for p in d.get("jobs") or []:
        if not _words_match(query, p.get("title", "")):
            continue
        out.append(_job(c, p["id"], p.get("title", ""), p.get("location", ""), p.get("publishedAt"),
                        p.get("jobUrl", ""), text=_plain(p.get("descriptionHtml") or p.get("descriptionPlain"))))
    return out


def _words_match(query, title):
    """Every word of the query in the title (these APIs return the whole board, not a search)."""
    return all(re.search(rf"\b{re.escape(w)}", title, re.I) for w in query.split())


# ---------- Oracle Recruiting Cloud ----------

def oracle(c, query, pages, cutoff=None):
    base, site = c["site"].rstrip("/"), c["siteNumber"]
    out = []
    for page in range(pages if cutoff else 1):  # a scheduled round pages on (newest first) until the cutoff
        finder = (f'findReqs;siteNumber={site},keyword="{query}",sortBy=POSTING_DATES_DESC,limit=25,offset={page * 25}')
        d = _try(f"{base}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList"
                 f"&finder={urllib.parse.quote(finder, safe='=;,')}") or {}
        page_jobs = []
        for item in d.get("items") or []:
            for r in item.get("requisitionList") or []:
                posted = _not_future(f'{r["PostedDate"]}T23:59:00+00:00') if r.get("PostedDate") else None
                page_jobs.append(_job(c, r["Id"], r.get("Title", ""), r.get("PrimaryLocation", ""), posted,
                                      f'{base}/hcmUI/CandidateExperience/en/sites/{site}/job/{r["Id"]}',
                                      extra={"_detail": f'{base}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?'
                                                        f'expand=all&onlyData=true&finder=ById;Id=%22{r["Id"]}%22,siteNumber={site}'}))
        out += page_jobs
        if len(page_jobs) < 25 or (cutoff and all(_is_older(j["posted"], cutoff) for j in page_jobs)):
            break
    return out


def oracle_detail(j):
    d = _try(j["_detail"]) or {}
    it = (d.get("items") or [{}])[0]
    parts = [("Qualifications", it.get("ExternalQualificationsStr")), ("Responsibilities", it.get("ExternalResponsibilitiesStr")),
             ("Job description", it.get("ExternalDescriptionStr"))]
    return " ".join(f"{k}: {_plain(v)}." for k, v in parts if v)


# ---------- Amazon / Microsoft ----------

def amazon(c, query, pages, cutoff=None):
    out = []
    for page in range(pages):
        d = _try("https://www.amazon.jobs/en/search.json?" + urllib.parse.urlencode(
            {"base_query": query, "country": "USA", "sort": "recent", "result_limit": 50, "offset": page * 50})) or {}
        for p in d.get("jobs") or []:
            try:
                posted = _iso(dt.datetime.strptime(re.sub(r"\s+", " ", p.get("posted_date", "")), "%B %d, %Y")
                              .replace(hour=23, minute=59, tzinfo=UTC))
                posted = _not_future(posted)
            except ValueError:
                posted = None
            text = (f'Basic Qualifications: {_plain(p.get("basic_qualifications"))}. '
                    f'Preferred Qualifications: {_plain(p.get("preferred_qualifications"))}. '
                    f'Job description: {_plain(p.get("description"))}')
            out.append(_job(c, p.get("id_icims") or p.get("id"), p.get("title", ""), p.get("normalized_location", ""),
                            posted, "https://www.amazon.jobs" + p.get("job_path", ""), text=text))
        if len(d.get("jobs") or []) < 50:
            break
        if cutoff and out and all(_is_older(j["posted"], cutoff) for j in out[-50:]):
            break  # sort=recent: the rest of the search is older
    return out


def microsoft(c, query, pages, cutoff=None):
    out = []
    for page in range(pages):
        d = _try("https://apply.careers.microsoft.com/api/pcsx/search?" + urllib.parse.urlencode(
            {"domain": "microsoft.com", "query": query, "location": "United States", "start": page * 10,
             "sort_by": "timestamp"})) or {}
        posts = (d.get("data") or {}).get("positions") or []
        for p in posts:
            posted = _iso(dt.datetime.fromtimestamp(p["postedTs"], UTC)) if p.get("postedTs") else None
            locs = p.get("standardizedLocations") or p.get("locations") or []
            out.append(_job(c, p["id"], p.get("name", ""), "; ".join(locs), posted,
                            f'https://apply.careers.microsoft.com/careers/job/{p["id"]}',
                            extra={"_detail": f'https://apply.careers.microsoft.com/api/apply/v2/jobs/{p["id"]}?domain=microsoft.com'}))
        if len(posts) < 10:
            break
        if cutoff and out and all(_is_older(j["posted"], cutoff) for j in out[-10:]):
            break  # sort_by=timestamp: the rest of the search is older
    return out


def microsoft_detail(j):
    return _plain((_try(j["_detail"]) or {}).get("job_description"))


# ---------- the company's own career site (server-rendered "SearchJobs / JobDetail" pages, e.g. Deloitte) ----------

def _get_html(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def careersite(c, query, pages, cutoff=None):
    """c["site"] = the careers base, e.g. https://apply.deloitte.com/en_US/careers. Lists SearchJobs/<query> newest first
    (jobSort=datePosted), then reads each posting's JobPosting JSON-LD for the full text. The list shows no date and the
    JSON-LD datePosted is the ORIGINAL posting date (Deloitte refreshes postings: one the site lists among its newest was
    "posted" months ago), so `posted` stays empty: being among the newest results of the date-sorted search is the
    recency signal (the board skips jobs it already has)."""
    base = c["site"].rstrip("/")
    out, per = [], 10
    for page in range(pages):
        try:
            h = _get_html(f"{base}/SearchJobs/{urllib.parse.quote(query)}?listFilterMode=1&jobSort=datePosted"
                          f"&jobRecordsPerPage={per}&jobOffset={page * per}")
        except (OSError, http.client.HTTPException):
            break
        arts = re.findall(r'<article class="article--result.*?</article>', h, re.S)
        for a in arts:
            m = re.search(r'href="([^"]*/JobDetail/[^"]+/(\d+))"[^>]*>\s*(.*?)\s*</a>', a, re.S)
            if not m:
                continue
            spans = [_plain(s) for s in re.findall(r"<span>(.*?)</span>", a, re.S)]
            loc = spans[-1] if spans else ""
            out.append(_job(c, m.group(2), _plain(m.group(3)), loc, None, m.group(1), extra={"_detail": m.group(1)}))
        if len(arts) < per:
            break

    def read(j):
        try:
            ld = re.search(r'application/ld\+json[^>]*>(.*?)</script>', _get_html(j["_detail"]), re.S)
            d = json.loads(ld.group(1)) if ld else {}
        except (OSError, ValueError, http.client.HTTPException):
            return
        j["_text"] = _plain(d.get("description") or d.get("qualifications"))
    with concurrent.futures.ThreadPoolExecutor(6) as ex:
        list(ex.map(read, out))
    return out


FETCH = {"workday": workday, "greenhouse": greenhouse, "lever": lever, "ashby": ashby, "oracle": oracle,
         "amazon": amazon, "microsoft": microsoft, "careersite": careersite}
DETAIL = {"workday": workday_detail, "oracle": oracle_detail, "microsoft": microsoft_detail}


# ---------- the search ----------

def min_size(bucket):
    """Lowest employee count of a size bucket ('1,001-5,000' -> 1001, '10,001+' -> 10001); None if unknown."""
    m = re.match(r"\s*([\d,]+)", str(bucket or ""))
    return int(m.group(1).replace(",", "")) if m else None


def settings(cfg):
    s = {**DEFAULTS, **(cfg.get("companyJobs") or {})}
    least = int(s.get("minEmployees") or 0)
    # companies bigger than minEmployees: with 1000, '1,001-5,000' and up count, '501-1,000' doesn't
    s["companies"] = [c for c in s.get("companies") or [] if c.get("enabled", True) and c.get("ats") in FETCH
                      and (not least or _top(c.get("employees")) > least)]
    return s


def _top(bucket):
    """Highest employee count of a size bucket ('501-1,000' -> 1000, '10,001+' -> infinity)."""
    nums = [int(n.replace(",", "")) for n in re.findall(r"[\d,]+", str(bucket or "")) if n.strip(",")]
    if not nums:
        return float("inf")  # unknown size: keep the company
    return float("inf") if str(bucket).strip().endswith("+") else max(nums)


def _details(j):
    """Fill in the job's posting text (qualifications first) - from the list itself or the site's detail API."""
    text = j.pop("_text", "") or ""
    if not text and j.get("_detail") and j["ats"] in DETAIL:
        text = DETAIL[j["ats"]](j)
    j.pop("_detail", None)
    j.pop("ats", None)
    head = f'{j["company"]} careers site.'
    j["description"] = f"{head} {text}" if text else f"{head} Requirements (years, clearance, pay) not verified - check the posting."
    j["qualsFrom"] = "company posting" if text else ""
    return j


def search_queries(s):
    """What a keyword-search career site is searched for: the role searches plus the extra title words (siteQueries)."""
    return list(s["queries"]) + [x for x in s["siteQueries"] if x not in s["queries"]]


def candidates(board_jobs, cfg=None, log=print, since=None, seen_ids=None):
    """(jobs to add, note). board_jobs = the board's jobs + removed keys (already seen: left out).

    since (a datetime) = a scheduled round: look ONLY at jobs posted since then (the previous round, minus a small overlap),
    from every company, deeper than the button's search: all pages of each site's role search until the postings are older
    than `since`, no maxJobs / maxPerCompany limits (companyJobs.schedule: queries, maxPagesPerQuery, maxJobs). seen_ids
    (a dict, id -> time) = jobs already read in an earlier round: skipped, and every job read now is added to it."""
    cfg = cfg if cfg is not None else json.loads(jobfilters.CONFIG.read_text(encoding="utf-8"))
    s = settings(cfg)
    if not s["companies"]:
        raise ValueError("no companies in companyJobs.companies (job-search-config.json)")
    sch = s.get("schedule") or {}
    cutoff = since
    if since:
        hours = max((dt.datetime.now(UTC) - since).total_seconds() / 3600, 0.25)
        pages, max_jobs, max_per = int(sch.get("maxPagesPerQuery", 100)), int(sch.get("maxJobs", 1000)), 10 ** 6
        queries = list(sch.get("queries") or ["engineer", "developer"])
    else:
        hours = s["postedWithinHours"]
        pages, max_jobs, max_per = s["pages"], s["maxJobs"], s["maxPerCompany"]
        queries = search_queries(s)
    ctx = jobfilters.context(cfg=cfg, log_jobs=applog.load()["jobs"], hours=hours)
    # Greenhouse / Lever / Ashby return the company's whole board: read it once and keep every title the role's
    # titleInclude matches ("Backend Engineer", "Platform Engineer" ...), not only titles with a query's exact words.
    # Workday / Oracle / Amazon / Microsoft are searches: one per query.
    whole = {"greenhouse", "lever", "ashby"}
    tasks = [(c, "") for c in s["companies"] if c["ats"] in whole]
    tasks += [(c, q) for c in s["companies"] if c["ats"] not in whole for q in queries]
    pool, failed = {}, set()
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        futures = {ex.submit(FETCH[c["ats"]], c, q, pages, cutoff): c for c, q in tasks}
        for f in concurrent.futures.as_completed(futures):
            try:
                for j in f.result():
                    pool.setdefault(j["id"], j)
            except Exception:
                failed.add(futures[f]["name"])
    seen = {(applog.norm(j.get("company")), applog.norm(j.get("title"))) for j in board_jobs.values() if j}
    multi = re.compile(r"(?:\d+|multiple)\s+Locations?", re.I)
    software = re.compile(s["titleInclude"] or ".", re.I)  # the role's titles (config), or every title
    not_role = re.compile(s["titleExclude"], re.I) if s.get("titleExclude") else None  # e.g. hardware / lab titles
    first = []
    for j in pool.values():
        if f'company:{j["id"]}' in board_jobs or (applog.norm(j["company"]), applog.norm(j["title"])) in seen:
            continue
        if seen_ids is not None and j["id"] in seen_ids:
            continue  # read in an earlier round
        if not software.search(j["title"]) or (not_role and not_role.search(j["title"])):
            continue
        same = (applog.norm(j["company"]), applog.norm(j["title"]))
        if same in seen:  # the same job posted once per city (Twitch, Amazon ...): one card
            continue
        seen.add(same)
        flags = set(jobfilters.check(j, ctx)[0])
        if multi.fullmatch(j["location"]):
            flags.discard("location")  # "3 Locations": the posting lists them - checked after reading it
        if not flags:
            first.append(j)
    first.sort(key=lambda j: j.get("posted") or "", reverse=True)
    if not since:
        first = first[:max_jobs * 4]  # the button: read at most 4x the postings needed
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        read = list(ex.map(_details, first))
    if seen_ids is not None:
        stamp = dt.datetime.now(UTC).isoformat(timespec="seconds")
        for j in read:
            seen_ids[j["id"]] = stamp
    found, per = [], {}
    for j in read:  # newest first, at most maxPerCompany per company so one big employer doesn't fill the list
        if jobfilters.check(j, ctx)[0] or per.get(j["company"], 0) >= max_per:
            continue
        per[j["company"]] = per.get(j["company"], 0) + 1
        found.append(j)
        if len(found) >= max_jobs:
            break
    for j in found:
        j["description"] = (f"[posted since the last round, {hours:.1f}h] " if since else f"[last {hours:g}h] ") + j["description"]
    note = (f"{len(s['companies'])} company sites, {'since the last round' if since else 'last'} {hours:.1f}h: {len(pool)} jobs found, "
            f"{len(first)} passed the title / location filters, {len(found)} added")
    if failed:
        note += f" | no answer from: {', '.join(sorted(failed))}"
    return found, note


def run(board_jobs, cfg=None, since=None, seen_ids=None):
    return candidates(board_jobs, cfg, since=since, seen_ids=seen_ids)


def check_sites(cfg):
    for c in settings(cfg)["companies"]:
        try:
            n = len(FETCH[c["ats"]](c, "software engineer", 1))
            print(f'{"ok " if n else "?  "} {c["name"]:28} {c["ats"]:10} {n} jobs for "software engineer"')
        except Exception as e:
            print(f'ERR {c["name"]:28} {c["ats"]:10} {e}')


# ---------- discovery: find a company's job board (no tokens) ----------

WD_HOSTS = ["wd1", "wd5", "wd3", "wd12", "wd10", "wd103", "wd108", "wd501", "wd503", "wd504", "wd100", "wd101", "wd102"]
WD_SITES = ["External", "Careers", "External_Career_Site", "ExternalCareerSite", "External_Careers", "ExternalCareers",
            "careers", "external", "{t}", "{T}", "{t}_careers", "{T}_Careers", "{T}Careers", "{t}careers", "{T}_External",
            "{t}_external", "{T}External", "Search", "jobs", "Jobs", "{T}_External_Career_Site", "{T}ExternalCareerSite",
            "{T}_Career_Site", "Global", "GlobalCareers", "Corporate", "US", "USA", "Ext", "Experienced", "Professional",
            "{T}_Jobs", "{t}jobs", "job", "Job_Site", "en-US"]
SUFFIX = re.compile(r"\b(inc|incorporated|corp|corporation|company|co|group|holdings?|plc|llc|ltd|the|technologies|"
                    r"international|global|services|systems|financial|enterprises?)\b\.?", re.I)


def _status(url, body=None):
    """HTTP status of a request (0 = no answer)."""
    headers = dict(UA)
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (OSError, ValueError, http.client.HTTPException):
        return 0


def slugs_for(name):
    """Likely board / tenant names for a company: 'Lockheed Martin Corp' -> lockheedmartin, lockheed, lockheed-martin."""
    base = SUFFIX.sub(" ", name.lower().replace("&", " and "))
    words = re.findall(r"[a-z0-9]+", base)
    out = ["".join(words), "-".join(words), words[0] if words and len(words[0]) >= 4 else "",
           "".join(w[0] for w in words) if len(words) >= 3 else "",
           "".join(re.findall(r"[a-z0-9]+", name.lower()))]
    return [s for s in dict.fromkeys(out) if len(s) >= 2]


def _count(c, queries):
    """(jobs on the board for an empty search, jobs matching the role queries) - None if the site doesn't answer."""
    try:
        total = len(FETCH[c["ats"]](c, "", 1)) if c["ats"] != "workday" else None
        if c["ats"] == "workday":
            host, tenant, site = _wd_parts(c)
            d = _try(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                     {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
            if not d or "total" not in d:
                return None
            total = d["total"]
        role = sum(len(FETCH[c["ats"]](c, q, 1)) for q in queries)
        return total, role
    except Exception:
        return None


def discover(name, queries, slugs=None):
    """Find a company's public job board: Greenhouse, Lever or Ashby (by board name), else Workday (finds the tenant's
    server with ~13 requests - right server + wrong site answers 404, anything else 422 - then tries common site
    names). Returns (entry, (all jobs, jobs for the role queries)) or (None, None)."""
    given = list(slugs or [])
    guessed = slugs_for(name)
    # a guess made of only part of the name ("general" for General Dynamics, "western" for Western Digital) finds
    # other companies' boards: such guesses only count when the board itself carries the company's name
    full = {guessed[0], guessed[1]} if len(guessed) > 1 else set(guessed)
    tries = list(dict.fromkeys(given + guessed))
    for s in tries:
        trusted = s in given or s in full
        for ats, url in (("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs"),
                         ("lever", f"https://api.lever.co/v0/postings/{s}?mode=json&limit=1"),
                         ("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{s}")):
            d = _try(url)
            if not ((isinstance(d, list) and d) or (isinstance(d, dict) and d.get("jobs"))):
                continue
            if ats == "greenhouse":  # Greenhouse names the board's company: it must be this one
                if not _same_company(name, (_try(f"https://boards-api.greenhouse.io/v1/boards/{s}") or {}).get("name", "")):
                    continue
            elif not trusted:
                continue
            c = {"name": name, "ats": ats, "board": s}
            return c, _count(c, queries)
    for t in [s for s in tries if "-" not in s and (s in given or s in full)]:
        host = next((f"{t}.{n}.myworkdayjobs.com" for n in WD_HOSTS
                     if _status(f"https://{t}.{n}.myworkdayjobs.com/wday/cxs/{t}/zz-no-such-site/jobs",
                                {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}) == 404), None)
        if not host:
            continue
        empty = None  # a site that exists but lists no jobs (an old / internal one): only if nothing better turns up
        for site in WD_SITES:
            site = site.replace("{t}", t).replace("{T}", t.capitalize())
            d = _try(f"https://{host}/wday/cxs/{t}/{site}/jobs",
                     {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
            if d and "total" in d:
                c = {"name": name, "ats": "workday", "site": f"https://{host}/{site}"}
                if d["total"]:
                    return c, _count(c, queries)
                empty = empty or c
        # a site with no jobs at all is an old / internal one, not the company's careers site: not found
    return None, None


def _same_company(name, board_name):
    """True when a job board's own company name matches ('Lucid Motors' ~ 'Lucid', 'SpaceX' ~ 'SpaceX')."""
    a = set(re.findall(r"[a-z0-9]{3,}", SUFFIX.sub(" ", name.lower())))
    b = set(re.findall(r"[a-z0-9]{3,}", SUFFIX.sub(" ", (board_name or "").lower())))
    # every word of the company's name must be on the board ("charles" is not Charles Schwab), or the names are the
    # same once spaces go ("Space X" = "SpaceX")
    return bool(a and b) and (a <= b or "".join(sorted(a)) == "".join(sorted(b))
                              or re.sub(r"[^a-z0-9]", "", name.lower()) == re.sub(r"[^a-z0-9]", "", board_name.lower()))


def entry_from_url(url, name):
    """A company entry from any job / careers link of a supported site (the surest way to add a company)."""
    m = re.search(r"https://([^./]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)", url)
    if m:
        return {"name": name, "ats": "workday", "site": f"https://{m[1]}.{m[2]}.myworkdayjobs.com/{m[3]}"}
    for ats, rx in (("greenhouse", r"greenhouse\.io/(?:embed/job_app\?for=)?([^/?#&]+)"),
                    ("lever", r"jobs\.lever\.co/([^/?#]+)"), ("ashby", r"jobs\.ashbyhq\.com/([^/?#]+)")):
        m = re.search(rx, url)
        if m and m[1] not in ("v1", "embed"):
            return {"name": name, "ats": ats, "board": m[1]}
    m = re.search(r"(https://[^/]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-z-]+/sites/([^/?#]+)", url)
    if m:
        return {"name": name, "ats": "oracle", "site": m[1], "siteNumber": m[2]}
    m = re.search(r"(https://[^/]+/[^?#]*?/careers)/JobDetail/", url)  # Deloitte-style SearchJobs / JobDetail sites
    if m:
        return {"name": name, "ats": "careersite", "site": m[1]}
    if "amazon.jobs" in url:
        return {"name": name, "ats": "amazon"}
    if "careers.microsoft.com" in url:
        return {"name": name, "ats": "microsoft"}
    return None


def save_companies(found):
    """Add / update entries in companyJobs.companies of the private config (by name), keeping the list sorted."""
    cfg = json.loads(jobfilters.CONFIG.read_text(encoding="utf-8"))
    cj = cfg.setdefault("companyJobs", {})
    have = {c["name"].lower(): c for c in cj.setdefault("companies", [])}
    for c in found:
        old = have.get(c["name"].lower())
        if old:
            old.update({k: v for k, v in c.items() if v})
        else:
            cj["companies"].append(c)
    cj["companies"].sort(key=lambda c: c["name"].lower())
    jobfilters.CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_names(path):
    """Lines 'Company name | slug1,slug2 | employees' (only the name is required; # comments)."""
    out = []
    for line in open(path, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if line:
            parts = [p.strip() for p in line.split("|")] + ["", ""]
            out.append((parts[0], [s.strip() for s in parts[1].split(",") if s.strip()], parts[2]))
    return out


def run_discovery(items, queries, write, workers=12):
    """items = [(name, slugs, employees)]. Prints what was found; with write=True saves it to the config."""
    cfg = json.loads(jobfilters.CONFIG.read_text(encoding="utf-8"))
    known = {c["name"].lower() for c in (cfg.get("companyJobs") or {}).get("companies") or []}
    found, missing = [], []
    with concurrent.futures.ThreadPoolExecutor(workers) as ex:
        results = ex.map(lambda it: (it, discover(it[0], queries, it[1])), items)
        for (name, _slugs, emp), (c, counts) in results:
            if not c:
                missing.append(name)
                print(f"--  {name}: no Greenhouse / Lever / Ashby / Workday board found - add it from a job link (--add)")
                continue
            if emp:
                c["employees"] = emp
            total, role = counts or (0, 0)
            where = c.get("board") or c.get("site")
            tag = "update" if name.lower() in known else "new"
            check = (f" - Lever / Ashby don't name the company: confirm https://jobs.{'lever.co' if c['ats'] == 'lever' else 'ashbyhq.com'}/{where} is theirs"
                     if c["ats"] in ("lever", "ashby") else "")
            print(f"OK  {name}: {c['ats']} {where} - {total} jobs, {role} for your role searches ({tag}){check}")
            found.append(c)
    if write and found:
        save_companies(found)
        print(f"{len(found)} saved to companyJobs.companies; {len(missing)} not found")
    elif found:
        print(f"{len(found)} found, {len(missing)} not found - preview only, add --write to save them")
    return found, missing


def _same_site(a, b):
    """Two company entries point at the same career site / board."""
    if a["ats"] != b["ats"]:
        return False
    if a["ats"] in ("amazon", "microsoft"):
        return True
    key = lambda c: (c.get("board") or c.get("site") or "").lower().rstrip("/")
    return key(a) == key(b)


def why(url, cfg, extra_queries=()):
    """Explain why a job link was (not) found by the company search - works for every supported site: is the company
    configured, does any of its searches return the job, and which filter drops it. Nothing is added to the board."""
    s = settings(cfg)
    link = entry_from_url(url, "?")
    if not link:
        print("not a supported job link (Workday, Greenhouse, Lever, Ashby, Oracle, Amazon, Microsoft, or a company career site)")
        return
    mine = next((c for c in s["companies"] if _same_site(link, c)), None)
    if mine is None:
        print(f"This site is not in companyJobs.companies (or the company is under companyJobs.minEmployees): add it with\n"
              f'  python companyjobs.py --add "{url}" --name "<Company>" --employees "10,001+" --write')
        return
    print(f'Company: {mine["name"]} ({mine["ats"]})')
    needle = re.split(r"[?#]", url.rstrip("/"))[0].rsplit("/", 1)[-1]
    whole = mine["ats"] in ("greenhouse", "lever", "ashby")
    queries = [""] if whole else search_queries(s) + [q for q in extra_queries if q not in search_queries(s)]
    hit, pool = None, {}
    for q in queries:
        try:
            got = FETCH[mine["ats"]](mine, q, s["pages"])
        except Exception as e:
            print(f'  search "{q}": error {type(e).__name__}: {e}')
            continue
        pool.update({j["id"]: j for j in got})
        match = next((j for j in got if needle and needle in j["url"]), None)
        print(f'  search "{q or "(whole board)"}": {len(got)} jobs' + (" - FOUND" if match else ""))
        hit = hit or match
    if hit is None:
        print("The job is not returned by any search: the site's keyword search did not list it among its best results. "
              'Try other title words with --query "<words from its title>", and add what works to companyJobs.siteQueries.')
        return
    ctx = jobfilters.context(cfg=cfg, log_jobs=applog.load()["jobs"], hours=s["postedWithinHours"])
    flags = set(jobfilters.check(hit, ctx)[0])
    if re.fullmatch(r"(?:\d+|multiple)\s+Locations?", hit["location"] or "", re.I):
        flags.discard("location")
    print(f'Found: "{hit["title"]}" | {hit["location"]} | posted {hit["posted"] or "unknown"}')
    if not re.compile(s["titleInclude"] or ".", re.I).search(hit["title"]):
        flags.add("title (companyJobs.titleInclude)")
    if s.get("titleExclude") and re.search(s["titleExclude"], hit["title"], re.I):
        flags.add("title (companyJobs.titleExclude)")
    if flags:
        print("Dropped before reading the posting by: " + ", ".join(sorted(flags)))
        return
    _details(hit)
    late = jobfilters.check(hit, ctx)[0]
    print("Dropped after reading the posting by: " + ", ".join(late) if late else
          "Passes every filter - it is added unless it is already on the board / was removed, or the maxJobs / maxPerCompany limits cut it.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--discover", nargs="+", metavar="NAME", help="find these companies' job boards")
    ap.add_argument("--discover-file", metavar="FILE", help="one company a line: Name | slug1,slug2 | employees")
    ap.add_argument("--add", metavar="URL", help="add a company from one of its job links (needs --name)")
    ap.add_argument("--name", help="company name for --add")
    ap.add_argument("--employees", default="", help="size bucket to save, e.g. 10,001+ or 1,001-5,000")
    ap.add_argument("--write", action="store_true", help="save what --discover / --add found to the config")
    ap.add_argument("--why", metavar="URL", help="explain why a job link was not found by the company search")
    ap.add_argument("--query", action="append", default=[], help="extra search words to try with --why")
    a = ap.parse_args()
    cfg = json.loads(jobfilters.CONFIG.read_text(encoding="utf-8"))
    queries = settings(cfg)["queries"]
    if a.why:
        return why(a.why, cfg, a.query)
    if a.add:
        if not a.name:
            ap.error("--add needs --name")
        c = entry_from_url(a.add, a.name)
        if not c:
            ap.error("not a supported job link (Workday, Greenhouse, Lever, Ashby, Oracle, Amazon, Microsoft)")
        if a.employees:
            c["employees"] = a.employees
        counts = _count(c, queries)
        if counts is None:
            ap.error(f"{c} does not answer - check the link")
        print(f"OK  {a.name}: {c['ats']} {c.get('board') or c.get('site') or ''} - {counts[0]} jobs, {counts[1]} for your role searches")
        if a.write:
            save_companies([c])
            print("saved to companyJobs.companies")
        else:
            print("preview only - add --write to save it")
        return
    if a.discover or a.discover_file:
        items = [(n, [], a.employees) for n in a.discover or []]
        if a.discover_file:
            items += [(n, s, e or a.employees) for n, s, e in read_names(a.discover_file)]
        return run_discovery(items, queries, a.write)
    if a.check:
        return check_sites(cfg)
    import jobboard  # same folder
    with jobboard.LOCK:
        board = jobboard.load_board()
    jobs, note = candidates(jobboard.known_jobs(board), cfg)
    for j in jobs:
        print(f'{j["posted"]}  {j["company"]} | {j["title"]} | {j["location"]}\n    {j["url"]}')
    if a.dry_run:
        print(f"{note} - dry run, nothing added")
        return
    with jobboard.LOCK:
        board = jobboard.load_board()
        n = jobboard.merge(board, jobs)
        jobboard.save_board(board)
    print(f"{n} new jobs added to the board - {note}")


if __name__ == "__main__":
    main()
