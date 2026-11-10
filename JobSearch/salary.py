"""The ONE way the apply agents decide what to type into a salary / pay-expectation field (user 2026-11-10: not a random number).

The number is built from three things:
  1. LOCATION - the user's expected yearly pay for the job's area (config salaryExpectation.byLocation / default; for a post in several
     cities the area of the office chosen on the form, else the lowest) = the FLOOR: never ask for less than that.
  2. THE POSTED RANGE - the salary range of the posting (lo - hi): the target starts inside it and never goes above its top.
  3. EXPERIENCE - the user's years (salaryExpectation.userYears, default 2) against the years the posting asks for: a role that asks for no
     more than the user has -> the MIDDLE of the posted range; a role that asks for more (a stretch) -> 35% into the range.
No posted range -> the location expectation itself.   number = clamp(range point, floor, hi), rounded to the nearest $1,000.

  python salary.py <job key> [--area "DC / Northern Virginia"] [--lo 64700 --hi 127600] [--years 3]
      job key: a key from `python jobboard.py queue`; the posting's range is read from its text unless --lo / --hi are given
      (a range the agent read on the page wins). No clear range in the posting: the agent looks the role up on Glassdoor and passes
      its base-pay range as --lo / --hi --source glassdoor. Prints the number FIRST (digits only), then the reasoning on the following lines.
      For a bucket dropdown ("$75,001 - $85,000") pick the bucket that contains the number.
"""
import argparse
import json
import re
import sys

import applog
import jobfilters

RANGE = re.compile(r"\$\s*([\d,]{2,7}(?:\.\d+)?)\s*(k|K)?\s*(?:-|–|—|to)\s*\$?\s*([\d,]{2,7}(?:\.\d+)?)\s*(k|K)?")


def _num(txt, k):
    v = float(txt.replace(",", ""))
    return v * 1000 if (k or v < 1000) else v


def posted_range(text):
    """(lo, hi) of the first plausible yearly salary range in a text ($85,000 - $120,000 / $85K-$120K), else None."""
    for m in RANGE.finditer(text or ""):
        lo, hi = _num(m.group(1), m.group(2)), _num(m.group(3), m.group(4) or m.group(2))
        if 20000 <= lo <= hi <= 1000000:
            return lo, hi
    return None


def recommend(floor, lo=None, hi=None, years_req=None, user_years=2):
    """-> (number, reason)."""
    if not (lo and hi and hi >= lo):
        return round(floor / 1000) * 1000, f"no posted range: your location expectation {floor:,.0f}"
    stretch = years_req is not None and years_req > user_years
    pos = 0.35 if stretch else 0.5
    raw = lo + pos * (hi - lo)
    n = min(max(raw, floor), hi)
    why = (f"posted {lo:,.0f}-{hi:,.0f}; {'stretch role (asks %s yrs, you have %s)' % (years_req, user_years) if stretch else 'level fits your experience'} "
           f"-> {int(pos * 100)}% into the range = {raw:,.0f}; location floor {floor:,.0f}"
           + (f" raised it to {floor:,.0f}" if raw < floor <= hi else "")
           + (f"; capped at the top of the range {hi:,.0f}" if floor > hi else ""))
    return round(n / 1000) * 1000, why


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("key", nargs="?")
    ap.add_argument("--area")
    ap.add_argument("--lo", type=float)
    ap.add_argument("--hi", type=float)
    ap.add_argument("--years", type=float)
    ap.add_argument("--source", default="posting", help="where --lo/--hi came from (posting / glassdoor), shown in the reasoning")
    a = ap.parse_args()
    cfg = json.loads(jobfilters.CONFIG.read_text(encoding="utf-8"))
    e = cfg.get("salaryExpectation") or {}
    by, default = e.get("byLocation") or {}, e.get("default") or 100000
    user_years = float(e.get("userYears", 2))
    job = {}
    if a.key:
        import jobboard
        board = jobboard.load_board()
        job = board["jobs"].get(a.key) or {}
        if not job:
            print("unknown job key", file=sys.stderr)
            sys.exit(2)
    ctx = jobfilters.context(cfg=cfg)
    if a.area:
        floor, where = by.get(a.area, default), a.area
    else:
        floor = jobfilters.expected_pay(job, ctx) if job else default
        floor, where = floor or default, ", ".join(jobfilters.areas(job, ctx)) or "default"
    lo, hi = a.lo, a.hi
    if not (lo and hi) and job:
        r = posted_range(f'{job.get("salary") or ""} {job.get("description") or ""}')
        if r:
            lo, hi = r
    years = a.years
    if years is None and job:
        import jobinfo
        years = (job.get("experience") or jobinfo.experience_from(job.get("description") or "", job.get("title") or "")).get("years")
    number, why = recommend(float(floor), lo, hi, years, user_years)
    print(int(number))
    if lo and hi:
        why = why.replace("posted ", "range from " + (a.source if (a.lo and a.hi) else "posting") + " ", 1)
    print(f"area: {where} (expectation {floor:,.0f}) | {why}")
    if lo and hi and hi < floor:
        print(f"NOTE: the posted range tops out below your expectation for this area ({hi:,.0f} < {floor:,.0f})")


if __name__ == "__main__":
    main()
