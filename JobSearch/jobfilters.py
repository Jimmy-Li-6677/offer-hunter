"""The search filters from job-search-config.json (jobFeed regexes, experienceYears, skip.rules) as named checks.

Used by jobfeed.py (each job's first failing filter = the reason on its card / --rejected) and by jobboard.py (every
failing filter of every job on the board, so the page can hide jobs by filter with its checkboxes).
"""
import datetime as dt
import json
import re

import applog  # same folder: log + .env helpers
import jobinfo  # same folder: experience from the job text

CONFIG = applog.PRIVATE / "job-search-config.json"
UTC = dt.timezone.utc

# (id, checkbox label, config rule it comes from) - in the order jobfeed.py reports the first failing one
FILTERS = [
    ("undated", "No posted date", "jobFeed.keepUndated"),
    ("old", "Older than the posted-within window", "jobFeed.postedWithinHours"),
    ("title", "Title not a target role", "jobFeed.titleInclude"),
    ("titleExcluded", "Senior / intern / non-software title", "jobFeed.titleExclude"),
    ("nonUs", "Outside the US", "jobFeed.remoteExcludeLocations"),
    ("location", "Location not in your areas", "locations / jobFeed.locationInclude"),
    ("internship", "Internship", "jobFeed (schedule)"),
    ("experience", "Requires too many years", "experienceYears.max / skip: 3+ years"),
    ("level", "Senior / staff / lead level", "jobFeed.experienceFilter"),
    ("lowPay", "Pay below your salary expectation", "salaryExpectation"),
    ("clearance", "Active clearance required", "skip: active security clearance"),
    ("cbp", "CBP (Customs and Border Protection) work", "skip: CBP work"),
    ("grad2027", "2027-grad-only program", "skip: 2027-grad-only programs"),
    ("amazonSde", "Amazon SDE role (cooldown)", "skip: Amazon SDE roles"),
    ("staffing", "Staffing agency", "skip: staffing-agency reposts / jobFeed.staffingAgencies"),
    ("easyApply", "LinkedIn Easy Apply only (no company posting found)", "skip: LinkedIn Easy Apply jobs without a company posting (autoApplyLoop 2c)"),
    ("skipCompany", "Company on your skip list", "skip.companies (SKIP_COMPANIES)"),
    ("dailyCap", "Company daily cap reached", "skip: more than 2 applications to a company a day"),
    ("logged", "Already in applications.json", "skip: already in applications.json"),
    ("skipped", "Skipped by you on the job board", "job board"),
]
LABELS = {f: label for f, label, _rule in FILTERS}
STATUS_FILTERS = {"logged", "skipped"}  # the job board shows these as tabs, not as checkboxes

CBP = re.compile(r"\b(customs and border protection|CBP)\b", re.I)
GRAD_2027 = re.compile(r"\bclass of 2027\b|\b2027 grad|\bgraduat\w*[^.]{0,60}\b2027\b", re.I)
GRAD_2025 = re.compile(r"\b2025\b")
AMAZON_SDE = re.compile(r"\b(software development engineer|SDE)\b", re.I)
CLEARANCE = re.compile(r"\b(active|current)\s+(TS|top secret|secret|TS/SCI)\b|\bTS/SCI\b|\bpolygraph\b", re.I)


def compile_any(patterns):
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.I) if patterns else None


def context(cfg=None, env=None, log_jobs=None, skipped_keys=(), hours=None, max_years="config", now=None):
    """Everything the checks need, read once per run / page load."""
    cfg = cfg if cfg is not None else json.loads(CONFIG.read_text(encoding="utf-8"))
    if env is None:
        env = {k: v for k, (v, _q) in applog.read_env(applog.ENV).items()} if applog.ENV.exists() else {}
    fc = cfg.get("jobFeed", {})
    if max_years == "config":
        max_years = cfg.get("experienceYears", {}).get("max", 2) if fc.get("experienceFilter", True) else None
    log_jobs = applog.load()["jobs"] if log_jobs is None else log_jobs
    today = dt.date.today().isoformat()
    # areas for the job board's location dropdown: each non-remote entry of `locations` goes with the
    # jobFeed.locationInclude regex at the same position; remote entries match remote US jobs
    inc, areas = list(fc.get("locationInclude", [])), []
    for loc in cfg.get("locations", []):
        if loc.get("workModes") == ["remote"]:
            areas.append((loc["name"], "remote"))
        elif inc:
            areas.append((loc["name"], re.compile(inc.pop(0), re.I)))
    return {
        "areas": areas,
        "fc": fc, "now": now or dt.datetime.now(UTC),
        "payExpect": cfg.get("salaryExpectation") or {},
        "hours": hours or fc.get("postedWithinHours") or cfg.get("postedWithinHours", 24), "maxYears": max_years,
        "titleIn": compile_any(fc.get("titleInclude", [])), "titleOut": compile_any(fc.get("titleExclude", [])),
        "locIn": compile_any(fc.get("locationInclude", [])), "nonUs": compile_any(fc.get("remoteExcludeLocations", [])),
        "vague": compile_any(fc.get("locationVague", [])),
        "skip": [applog.norm(c) for c in env.get("SKIP_COMPANIES", "").split(",") if c.strip()],
        "staffing": [applog.norm(c) for c in fc.get("staffingAgencies", []) if c.strip()],
        "log": log_jobs, "skippedKeys": set(skipped_keys),
        "sentToday": [e for e in log_jobs if e.get("status") == "Submitted" and e.get("date") == today],
    }


def _posted(j):
    p = j.get("posted")
    if isinstance(p, str):
        try:
            return dt.datetime.fromisoformat(p)
        except ValueError:
            return None
    return p


def _named(company, names):
    return any(re.search(rf"\b{re.escape(n)}\b", company) for n in names)


def check(j, ctx, key=None):
    """(ids of every filter the job fails, in FILTERS order; extras). extras: checkLocation = no city given but the
    location is vague ("Hybrid", "United States") - kept, verify on the posting; experience = from the description."""
    fc, out = ctx["fc"], []
    title, location, desc = j.get("title") or "", (j.get("location") or "").strip(), j.get("description") or ""
    posted = _posted(j)
    if posted is None:
        if not fc.get("keepUndated", False):
            out.append("undated")
    elif posted < ctx["now"] - dt.timedelta(hours=ctx["hours"]):
        out.append("old")
    if ctx["titleIn"] and not ctx["titleIn"].search(title):
        out.append("title")
    if ctx["titleOut"] and ctx["titleOut"].search(title):
        out.append("titleExcluded")
    if ctx["nonUs"] and ctx["nonUs"].search(location):
        out.append("nonUs")
    check_location = False
    remote = j.get("remote") or bool(re.search(r"\b(anywhere|remote)\b", location, re.I))
    parts = re.split(r"\s*[;/|]\s*|\s+or\s+", location)
    if not remote and not any(ctx["locIn"] and ctx["locIn"].search(p) for p in parts):
        if ctx["vague"] and ctx["vague"].fullmatch(location):
            check_location = True
        else:
            out.append("location")
    if re.search(r"intern", j.get("schedule") or "", re.I):
        out.append("internship")
    exp = j.get("experience") or jobinfo.experience_from(desc, title)
    if ctx["maxYears"] is not None and exp.get("years") is not None and exp["years"] > ctx["maxYears"]:
        out.append("experience")
    if re.search(r"\b(senior|staff|principal|lead)\b", exp.get("level") or "", re.I):
        out.append("level")
    pay, expect = jobinfo.salary_annual(j.get("salary") or ""), expected_pay(j, ctx)
    if pay and expect and pay[1] < expect:
        out.append("lowPay")
    if fc.get("skipClearance", True) and CLEARANCE.search(desc):
        out.append("clearance")
    if CBP.search(desc) or CBP.search(title):
        out.append("cbp")
    if GRAD_2027.search(desc) and not GRAD_2025.search(desc):
        out.append("grad2027")
    company = applog.norm(j.get("company"))
    if "amazon" in company.split() and AMAZON_SDE.search(title):
        out.append("amazonSde")
    if company and _named(company, ctx["staffing"]):
        out.append("staffing")
    if j.get("easyApply"):
        out.append("easyApply")
    if company and _named(company, ctx["skip"]):
        out.append("skipCompany")
    if company and sum(applog.matches(e, j.get("company", "")) for e in ctx["sentToday"]) >= applog.DAILY_COMPANY_CAP:
        out.append("dailyCap")
    if any(applog.matches(e, j.get("company", ""), title, j.get("id") or None) for e in ctx["log"]):
        out.append("logged")
    if key and key in ctx["skippedKeys"]:
        out.append("skipped")
    return out, {"checkLocation": check_location, "experience": exp, "pay": pay, "expect": expect}


def expected_pay(j, ctx):
    """salaryExpectation for the job's area (the lowest one for multi-city posts), else its default; None = no
    expectation set. Jobs without a listed salary never fail it."""
    e = ctx.get("payExpect") or {}
    by = e.get("byLocation") or {}
    vals = [by[a] for a in areas(j, ctx) if by.get(a)]
    return min(vals) if vals else e.get("default") or None


def areas(j, ctx):
    """Names of the `locations` the job is in (several for multi-city posts); [] = none of them."""
    location = j.get("location") or ""
    remote = j.get("remote") or bool(re.search(r"\b(anywhere|remote)\b", location, re.I))
    us = not (ctx["nonUs"] and ctx["nonUs"].search(location)) or re.search(r"\b(US|USA|United States)\b", location)
    return [name for name, rx in ctx["areas"] if (remote and us if rx == "remote" else rx.search(location))]


def reason(flag, j, ctx, extras):
    """Text for --rejected / the card, e.g. 'requires 3+ yrs', 'older than 24h'."""
    if flag == "experience":
        e = extras["experience"]
        return f"requires {e.get('text') or str(e.get('years')) + '+ yrs'}"
    if flag == "level":
        return f"level {extras['experience'].get('level')}"
    if flag == "lowPay":
        return f"pay up to ${extras['pay'][1] / 1000:,.0f}K, below your ${extras['expect'] / 1000:,.0f}K expectation"
    if flag == "old":
        return f"older than {ctx['hours']:g}h"
    return LABELS[flag].lower() if flag not in ("cbp", "easyApply") else LABELS[flag]
