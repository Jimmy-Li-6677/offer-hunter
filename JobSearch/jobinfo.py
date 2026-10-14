"""Salary range and experience level from a job post's text, for the job board cards.

Best effort (regex over the description): used by jobfeed.py on the full description of new jobs, and by
jobboard.py for jobs already on the board that were saved without these fields.
"""
import re

_NUM = r"\$\s?\d{1,3}(?:,\d{3})*(?:\.\d+)?\s?[kK]?"
_SALARY = re.compile(rf"({_NUM})\s*(?:-|–|—|to|and)\s*({_NUM})(?:\s*(?:USD|usd))?")
_HOURLY = re.compile(r"^\s*(?:USD\s*)?(?:/|per|an|a)\s*(?:hr|hour)|^\s*(?:USD\s*)?hourly", re.I)

_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_N = r"(\d{1,2}|" + "|".join(_WORDS) + r")"
# "3+ years", "2-4 years", "at least 2 years", "minimum of five (5) years"; "experience" must be close by
_YEARS = re.compile(rf"\b{_N}(?:\s*\(\d{{1,2}}\))?\s*(\+|plus)?\s*(?:(?:-|–|—|to)\s*{_N}\s*\+?\s*)?(?:years?|yrs?)\b", re.I)
_PREFERRED = re.compile(r"\b(preferred|nice[- ]to[- ]have|bonus points|pluses|we prefer|strong candidates may also|"
                        r"desired qualifications|it'?s a plus|added plus)\b", re.I)
_CONTEXT = re.compile(r"experience|industry|professional|hands[- ]on|building|developing|working|software|engineering|"
                      r"programming|coding|production", re.I)
_LEVELS = [  # (pattern, label) - first match wins; title is checked before the description
    (r"\bnew[- ]?grad(uate)?s?\b|\brecent (college )?grad", "New Grad"),
    (r"\bearly[- ]career\b|\buniversity grad", "Early Career"),
    (r"\bentry[- ]level\b", "Entry level"),
    (r"\bjunior\b|\bjr\.?\b", "Junior"),
    (r"\bassociate\b", "Associate"),
    (r"\b(principal|staff|distinguished)\b", "Staff+"),
    (r"\b(senior|sr\.?)\b", "Senior"),
    (r"\blead\b", "Lead"),
    (r"\b(engineer|developer|SWE|SDE)\s+(I|1)\b", "Level I"),
    (r"\b(engineer|developer|SWE|SDE)\s+(II|2)\b", "Level II"),
    (r"\b(engineer|developer|SWE|SDE)\s+(III|3|IV|4)\b", "Level III+"),
]


def _money(s):
    v = float(re.sub(r"[^\d.]", "", s))
    return v * 1000 if s.strip().lower().endswith("k") else v


def salary_from(text):
    """'$168,680 – $212,490' / '$28 – $39 /hr' from the first pay range in the text, else ''."""
    for m in _SALARY.finditer(text or ""):
        lo, hi = _money(m.group(1)), _money(m.group(2))
        if not 0 < lo <= hi:
            continue
        hourly = bool(_HOURLY.match(text[m.end():m.end() + 12])) or hi < 500
        if not hourly and lo < 20000:  # e.g. "$5-10 million" funding, not pay
            continue
        if hourly:
            return f"${lo:,.2f}".rstrip("0").rstrip(".") + f" – ${hi:,.2f}".rstrip("0").rstrip(".") + " /hr"
        return f"${lo:,.0f} – ${hi:,.0f}"
    return ""


def _num(s):
    return int(s) if s.isdigit() else _WORDS[s.lower()]


def experience_from(text, title=""):
    """{'years': minimum years required (the most demanding requirement) or None, 'text': '3+ yrs' / '0-2 yrs' / '',
    'level': 'New Grad' / 'Senior' / ... or ''}."""
    text = text or ""
    cut = _PREFERRED.search(text)
    required = text[:cut.start()] if cut and cut.start() > 200 else text  # skip "preferred / nice to have" sections
    best = None
    for m in _YEARS.finditer(required):
        near = required[max(0, m.start() - 80):m.end() + 80].lower()
        if not _CONTEXT.search(near):  # "years of experience / building / in software ...", not "10 years of history"
            continue
        lo, plus, hi = _num(m.group(1)), m.group(2), m.group(3)
        if lo > 15 or (hi and _num(hi) > 20):
            continue
        if best is None or lo > best[0]:
            best = (lo, f"{lo}-{_num(hi)} yrs" if hi else f"{lo}+ yrs" if plus or lo else "0 yrs")
    level = next((label for pat, label in _LEVELS if re.search(pat, title or "", re.I)), "")
    if not level:  # description: only the entry-level labels ("lead projects", "senior engineers" are not the job's level)
        level = next((label for pat, label in _LEVELS[:4] if re.search(pat, text, re.I)), "")
    return {"years": best[0] if best else None, "text": best[1] if best else "", "level": level}
