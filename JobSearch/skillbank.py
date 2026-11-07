"""Skill bank: thousands of tech skills with their other names, for the keyword match score - plain rules, no AI.

matchscore.VOCAB (about 200 hand-written keywords with careful patterns) stays the first layer. This adds:
  - O*NET Technology Skills (onetcenter.org, CC BY 4.0): the tools / languages / platforms listed for computer,
    engineering and science occupations, e.g. "Apache Kafka", "Atlassian JIRA", "Amazon Redshift".
  - Stack Overflow's most used tags with their tag synonyms (api.stackexchange.com, CC BY-SA): e.g. "postgres" ->
    postgresql, "k8s" -> kubernetes, with each tag's one-line description.
  - Learned: every skill name Simplify tags on a job (simplifySkills), saved as they come in.
Left out: terms the VOCAB already covers (VOCAB wins), STOP words (degrees, soft skills, generic nouns), names and
other names under 3 letters, and names made only of ordinary English words - a word counts as ordinary when the
board's postings write it in plain lower case (tech names are written "Kafka", "NumPy", ordinary words "design").

Matching: a text is cut into words; every run of 1-4 words is looked up word by word (lower case, without dots /
hyphens: "Node.js" = "NodeJS" = "Node JS", "Spring-Boot" = "Spring Boot"). An abbreviation ("AWS", "SAN") only counts
in capitals; a one-word plain-letter name only counts when the text capitalises it ("Kafka", not "kafka").

  python skillbank.py build [--download]   build cache/skillbank.json (downloads the lists the first time, about 80
                                           requests; --download fetches them again - monthly is plenty)
  python skillbank.py find "<text>"        the bank terms found in a text
  python skillbank.py stats                how many terms, by source
"""
import collections
import functools
import gzip
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache" / "skillbank.json"       # the bank (public data, gitignored)
RAW = HERE / "cache" / "skillbank-raw.json"     # the downloaded lists, so rebuilding doesn't download again

ONET_URL = "https://www.onetcenter.org/dl_files/database/db_{}_text/Technology%20Skills.txt"
ONET_VERSIONS = ["31_0", "30_2", "30_1", "30_0", "29_3"]
# O*NET occupation groups whose tools count: computer + math (15), engineering (17), life / physical science (19)
ONET_GROUPS = ("15-", "17-", "19-")
SE_API = "https://api.stackexchange.com/2.3"
SO_PAGES = 25  # 100 a page, most used first; 25 pages is the most Stack Exchange allows without an API key

# never keywords, whatever a list says (degrees, soft skills, generic nouns, company names, too-broad topics)
STOP = {
    "computer science", "software engineering", "engineering", "mathematics", "math", "statistics", "physics",
    "communication", "english", "leadership", "teamwork", "problem solving", "management", "business", "research",
    "education", "design", "testing", "development", "programming", "software", "hardware", "data", "code", "web",
    "internet", "email", "microsoft", "google", "amazon", "apple", "oracle", "ibm", "adobe", "windows", "linkedin",
    "nvidia", "meta", "facebook", "twitter", "github copilot", "office", "excel", "word", "outlook", "teams",
    "artificial intelligence", "ai", "api", "apis", "rest", "restful", "oop", "open source", "real time", "realtime",
    "programming languages", "operating systems", "operating system", "web applications", "web application",
    "web services", "web development", "user interface", "command line", "automated tests", "software testing",
    "conditional statements", "coding style", "if statement", "for loop", "new operator", "foreach", "while loop",
    "e commerce", "ecommerce", "3d", "2d", "io", "ip", "csv", "pdf", "max", "min", "view", "element", "factory",
    "resources", "warnings", "timezone", "download", "center", "ads", "bigdata", "big data", "multilingual",
    "transparency", "basic", "project management", "data analysis", "data science", "user experience",
    "director", "metro", "fetch", "advice", "traits", "interrupt", "std", "flash", "resource center", "apache",
    "intel", "paypal", "instagram", "workday", "salesforce", "fortinet", "cisco", "vmware", "slack",
    "go", "r", "c", "d", "swift", "rust", "spring", "express", "flask", "ruby", "perl", "dart", "julia",
}


def norm(s):
    """One word's key: lower case without dots, hyphens, underscores or slashes ('Node.js' -> 'nodejs')."""
    return re.sub(r"[\s._/\-]+", "", str(s).lower())


_WORD = re.compile(r"\.?[A-Za-z0-9][A-Za-z0-9+#]*(?:\.[A-Za-z0-9+#]+)*")


def _words(text, starts=False):
    """[(surface, key)] for each word of a text (C++, C#, .NET, Node.js stay one word); with starts=True also
    whether the word opens a sentence / list item: [(surface, key, opens)]."""
    text = text or ""
    if not starts:
        return [(m.group(0), norm(m.group(0))) for m in _WORD.finditer(text)]
    out = []
    for m in _WORD.finditer(text):
        before = text[max(0, m.start() - 8):m.start()].rstrip()
        opens = m.start() < 8 and not before or bool(before) and before[-1] in ".;:!?•·-*(\n"
        out.append((m.group(0), norm(m.group(0)), opens))
    return out


def key_of(name):
    """A name's key: its word keys joined by spaces ('Spring-Boot' -> 'spring boot')."""
    return " ".join(k for _, k in _words(name) if k)


def _is_acronym(name):
    return bool(re.fullmatch(r"[A-Z][A-Z0-9&+]{1,6}", name.strip()))


# ---------- building the bank ----------

def _get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "offer-hunter skill bank", "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=60) as r:
                b = r.read()
            return gzip.decompress(b) if b[:2] == b"\x1f\x8b" else b
        except urllib.error.HTTPError:
            raise
        except OSError:
            if i == tries - 1:
                raise
            time.sleep(3)


def _se(path, **params):
    q = "&".join(f"{k}={v}" for k, v in {"site": "stackoverflow", **params}.items())
    d = json.loads(_get(f"{SE_API}/{path}?{q}"))
    if d.get("backoff"):
        time.sleep(int(d["backoff"]) + 1)
    return d


def download():
    """The raw lists: {'onet': [[example, commodity title], ...], 'so': {tag: {'what', 'aka', 'count'}}}."""
    onet = []
    for v in ONET_VERSIONS:
        try:
            raw = _get(ONET_URL.format(v)).decode("utf-8", "replace")
        except OSError:
            continue
        seen = set()
        for line in raw.splitlines()[1:]:
            cols = line.split("\t")
            if len(cols) >= 4 and cols[0].startswith(ONET_GROUPS) and cols[1] not in seen:
                seen.add(cols[1])
                onet.append([cols[1].strip(), cols[3].strip()])
        break
    so = {}
    for page in range(1, SO_PAGES + 1):
        try:
            d = _se("tags", order="desc", sort="popular", pagesize=100, page=page)
        except OSError:  # quota / page limit: keep what came in
            break
        for t in d.get("items", []):
            so[t["name"]] = {"what": "", "aka": [], "count": t.get("count", 0)}
        if not d.get("has_more"):
            break
    for page in range(1, SO_PAGES + 1):
        try:
            d = _se("tags/synonyms", order="desc", sort="applied", pagesize=100, page=page)
        except OSError:
            break
        for s in d.get("items", []):
            if s["to_tag"] in so:
                so[s["to_tag"]]["aka"].append(s["from_tag"])
        if not d.get("has_more"):
            break
    data = {"downloaded": time.strftime("%Y-%m-%d"), "onet": onet, "so": so}
    RAW.parent.mkdir(exist_ok=True)
    RAW.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


VENDORS = ("Microsoft ", "Oracle ", "Apache ", "Amazon ", "Google ", "IBM ", "Atlassian ", "Adobe ", "SAP ",
           "Red Hat ", "The MathWorks ", "Autodesk ", "Dassault Systemes ", "Siemens ", "National Instruments ",
           "JetBrains ", "Salesforce ", "Cisco ", "VMware ", "Elastic ", "MongoDB ", "Progress ", "Bentley ")


def describe(raw, tags):
    """Fill in the one-line descriptions (tag wiki excerpts) of these Stack Overflow tags, 20 per request, and save
    the raw lists. Stops when the daily quota (300 requests without an API key) runs low."""
    todo = [t for t in tags if t in raw["so"] and not raw["so"][t]["what"]]
    for i in range(0, len(todo), 20):
        batch = ";".join(urllib.request.quote(n, safe="") for n in todo[i:i + 20])
        try:
            d = _se(f"tags/{batch}/wikis", pagesize=20)
        except (OSError, ValueError):
            break
        for w in d.get("items", []):
            if w.get("tag_name") in raw["so"]:
                raw["so"][w["tag_name"]]["what"] = re.sub(r"\s+", " ", w.get("excerpt") or "").strip()[:240]
        if d.get("quota_remaining", 99) < 20:
            break
    RAW.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")


def _onet_entry(name):
    """(display name, other names) for an O*NET example, or None for a kind of software ('web browser software')."""
    bare = re.sub(r"\s+software$", "", name).strip()
    if not bare or bare == bare.lower():
        return None
    aka = set()
    m = re.search(r"\s([A-Z][A-Z0-9]{1,6})$", bare)  # "Amazon Web Services AWS": AWS only if it is the initials
    if m:
        head = bare[:m.start()].strip()
        initials = "".join(w[0] for w in re.findall(r"[A-Za-z][\w']*", head) if w[0].isupper() or len(w) > 3).upper()
        if initials.endswith(m.group(1)) or initials == m.group(1):
            aka |= {m.group(1), head}
            bare = head
        else:
            return bare, aka
    for vendor in VENDORS:  # "Atlassian JIRA" -> JIRA, "Apache Kafka" -> Kafka
        if bare.startswith(vendor) and len(bare) - len(vendor) >= 3:
            aka.add(bare[len(vendor):])
    return bare, aka


def _pretty(tag):
    """Display name for a Stack Overflow tag: 'spring-boot' -> 'Spring Boot' (the postings' spelling wins later)."""
    return " ".join(w if re.search(r"[^a-z]", w) else w.capitalize() for w in tag.replace("-", " ").split())


def build(board_texts=(), learned=(), fresh=False):
    import matchscore  # same folder (lazy: matchscore imports this module)
    try:
        raw = download() if fresh or not RAW.exists() else json.loads(RAW.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = download()

    # how the postings write each word and each whole name: ordinary English words are written in lower case
    low, cap = collections.Counter(), collections.Counter()
    spelled = collections.defaultdict(collections.Counter)
    for text in board_texts:
        ws = _words(text)
        for surface, k in ws:
            (low if surface == surface.lower() else cap)[k] += 1
        for n in (1, 2, 3):
            for i in range(len(ws) - n + 1):
                part = ws[i:i + n]
                if any(s != s.lower() for s, _ in part):
                    spelled[" ".join(k for _, k in part)][" ".join(s for s, _ in part)] += 1

    def ordinary(word_key):
        return low[word_key] >= 2 and low[word_key] >= 0.3 * (low[word_key] + cap[word_key])

    dropped = set()

    def usable(name):
        k = key_of(name)
        if not k or len(k.replace(" ", "")) < 3 or name.lower().strip() in STOP or k in STOP or k.replace(" ", "") in STOP:
            return False
        if all(ordinary(w) for w in k.split()) and not _is_acronym(name):
            dropped.add(name)
            return False
        return True

    entries = {}  # key -> {'name', 'what', 'aka': set, 'src'}

    def add(name, what, aka, src):
        name = name.strip()
        if not usable(name) or matchscore.terms(name) or any(matchscore.terms(a) for a in aka):
            return  # VOCAB already knows it (by its name or one of its other names)
        k = key_of(name)
        e = entries.setdefault(k, {"name": name, "what": what, "aka": set(), "src": src})
        e["what"] = e["what"] or what
        e["aka"] |= {a for a in aka if key_of(a) != k and usable(a)}

    for name, what in raw["onet"]:
        got = _onet_entry(name)
        if got:
            add(got[0], what, got[1], "O*NET")
    for tag, e in sorted(raw["so"].items(), key=lambda kv: -kv[1]["count"]):
        k = key_of(_pretty(tag))
        aka = {_pretty(a) for a in e["aka"]}
        if k in entries:  # O*NET has a proper display name: keep it, add the tag's synonyms
            entries[k]["aka"] |= {a for a in aka if usable(a)}
            entries[k]["what"] = entries[k]["what"] or e["what"]
            continue
        add(_pretty(tag), e["what"], aka, "Stack Overflow")
        if k in entries and entries[k]["src"] == "Stack Overflow":
            entries[k].setdefault("tag", tag)
    for name in learned:
        add(name, "", set(), "Simplify")
    missing = [e["tag"] for e in entries.values() if e.get("tag") and not raw["so"][e["tag"]]["what"]]
    if missing:  # descriptions for the tags that made it into the bank (once; saved in the raw file)
        describe(raw, missing)
        for e in entries.values():
            if e.get("tag"):
                e["what"] = e["what"] or raw["so"][e["tag"]]["what"]
    for k, e in entries.items():  # the postings' own spelling ("NumPy", not "Numpy")
        if e["src"] == "Stack Overflow" and spelled.get(k):
            e["name"] = spelled[k].most_common(1)[0][0]
    data = {"built": time.strftime("%Y-%m-%d"), "downloaded": raw.get("downloaded", ""),
            "sources": "O*NET Technology Skills (onetcenter.org, CC BY 4.0); Stack Overflow tags (CC BY-SA)",
            "dropped": sorted(dropped),
            "terms": [{"name": e["name"], "what": e["what"], "aka": sorted(e["aka"]), "src": e["src"]}
                      for e in sorted(entries.values(), key=lambda e: e["name"].lower())]}
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    load.cache_clear()
    _find.cache_clear()
    return data


def learn(names):
    """Add skill names Simplify tagged (simplifySkills) that the bank doesn't know yet."""
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    index, _ = load()
    import matchscore  # same folder
    new = [n for n in dict.fromkeys(n.strip() for n in names if n) if key_of(n) not in index
           and len(norm(n)) >= 3 and n.lower() not in STOP and not matchscore.terms(n)]
    if new:
        data["terms"] += [{"name": n, "what": "", "aka": [], "src": "Simplify"} for n in new]
        CACHE.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        load.cache_clear()
        _find.cache_clear()


# ---------- matching ----------

@functools.lru_cache(maxsize=1)
def load():
    """(index, info): index = {key: (term name, needs capitals)}, also keyed by the joined form of multi-word names
    ('nodejs' for 'Node JS'); info = {term name: {'what', 'aka', 'src'}}. Empty when the bank isn't built."""
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    index = {}
    forms = [(t["name"], t["name"]) for t in data["terms"]] + [(a, t["name"]) for t in data["terms"] for a in t["aka"]]
    for form, name in forms:  # main names first, so a main name always wins its key
        k = key_of(form)
        if k:
            index.setdefault(k, (name, _is_acronym(form)))
            index.setdefault(k.replace(" ", ""), (name, _is_acronym(form)))
    info = {t["name"]: t for t in data["terms"]}
    return index, info


def find(text, extra=None):
    """Bank terms in a text (set of names). extra = {key: name} looked up first (e.g. the resume's own skill items)."""
    return set(_find(text or "", tuple(sorted((extra or {}).items()))))


@functools.lru_cache(maxsize=8192)
def _find(text, extra):
    index, _ = load()
    extra = dict(extra)
    if not index and not extra:
        return frozenset()
    words = _words(text, starts=True)
    found = set()
    i = 0
    while i < len(words):
        for n in (4, 3, 2, 1):
            if i + n > len(words):
                continue
            part = words[i:i + n]
            key = " ".join(w[1] for w in part)
            if key in extra:
                found.add(extra[key])
                break
            hit = index.get(key)
            if not hit:
                continue
            name, caps = hit
            surface = " ".join(w[0] for w in part)
            if caps and surface != surface.upper():
                continue  # an abbreviation only in capitals ("SAN", not "San Diego")
            if n == 1 and surface.isalpha() and (surface == surface.lower()
                                                 or (surface == surface.capitalize() and part[0][2])):
                continue  # "kafka" mid-sentence or "Advice" opening a sentence could be anything; "Kafka" is the tool
            found.add(name)
            i += n - 1
            break
        i += 1
    return frozenset(found)


def info(name):
    """{'what', 'aka', 'src'} for a bank term ({} if unknown)."""
    return load()[1].get(name, {})


def main(argv):
    if argv[:1] == ["build"]:
        import jobboard  # same folder
        board = jobboard.load_board()
        texts = [f'{j.get("title", "")}. {j.get("description", "")}' for j in board["jobs"].values()]
        learned = [s for j in board["jobs"].values() for s in j.get("simplifySkills") or []]
        data = build(texts, learned, fresh="--download" in argv)
        print(f'{len(data["terms"])} terms saved to {CACHE} ({len(data["dropped"])} ordinary-English names dropped)')
        return 0
    if argv[:1] == ["find"] and len(argv) > 1:
        print(sorted(find(" ".join(argv[1:]))))
        return 0
    if argv[:1] == ["stats"]:
        _, inf = load()
        print(len(inf), "terms:", dict(collections.Counter(t["src"] for t in inf.values())))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
