"""Job application log helper - ../private/applications.json is the single log of every job seen.

Usage (from the JobSearch folder):
  python applog.py check "<company>" ["<role>"] [--id <job id>]   -> prints matching logged jobs + today's count for the company
      exit 0 = already logged (skip), 2 = company already has 2 submissions today (don't apply today), 1 = OK to evaluate
  python applog.py add --company C --role R --location L --status S [--notes N] [--id ID] [--url U] [--source SRC]
      (same company + role, or same job id, updates the existing entry instead of adding a duplicate)
      --url (link of the posting / application page) is required for Submitted and expected for every status
  python applog.py list [--status S] [--no-link]                  -> table of entries (--no-link: only entries without a link)
  python applog.py open                                           -> entries still needing action (Needs User / Needs approval / Pending / Candidate)
  python applog.py note "<text>"                                  -> append a round note (e.g. "round found no new posts")
  python applog.py answer --section S --question Q --answer A [--key K]
      -> save a question the user just answered to the ANSWER_* bank in ../private/.env (+ empty key in ../examples/.env.example),
         under "# --- S ---" (created if missing); the same question / key is updated instead of duplicated.
  python applog.py missing                                        -> what is still empty / unconfirmed in ../private/
      (REQUIRED items block the job search; exit 1 while any are left)

Statuses: Submitted, Skipped, Needs User, Needs approval, Pending, Candidate.
"""
import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE = ROOT / "private"    # real data, gitignored
EXAMPLES = ROOT / "examples"  # public templates
LOG = PRIVATE / "applications.json"
ENV = PRIVATE / ".env"
ENV_EXAMPLE = EXAMPLES / ".env.example"
DAILY_COMPANY_CAP = 2  # user rule 2026-10-13: at most 2 applications to the same company per day
OPEN_STATUSES = {"needs user", "needs approval", "pending", "candidate"}


def load():
    if not LOG.exists():  # fresh clone: start an empty log from the committed example
        data = json.loads((EXAMPLES / "applications.example.json").read_text(encoding="utf-8"))
        data["jobs"], data["roundNotes"] = [], []
        PRIVATE.mkdir(exist_ok=True)
        save(data)
    return json.loads(LOG.read_text(encoding="utf-8"))


def save(data):
    LOG.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def norm(s):
    """Lower-case, drop parenthetical ids/notes and punctuation, for fuzzy matching."""
    s = re.sub(r"\([^)]*\)", " ", (s or "").lower())
    s = s.replace(",", " ").replace("inc.", " ").replace("inc", " ")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def matches(entry, company, role=None, job_id=None):
    ids = {str(i) for i in entry.get("ids", [])}
    if job_id and job_id in ids:
        return True
    if job_id and ids:
        return False  # both have job ids and they differ: a different posting, even if the title is similar
    ec, c = norm(entry["company"]), norm(company)
    if not c or not (c in ec or ec in c):
        return False
    if not role:
        return True
    er, r = norm(entry["role"]), norm(role)
    return r in er or er in r


def cmd_check(a):
    data = load()
    hits = [e for e in data["jobs"] if matches(e, a.company, a.role, a.id)]
    for e in hits:
        print(row(e))
    today = dt.date.today().isoformat()
    sent_today = [e for e in data["jobs"] if e["status"] == "Submitted" and e["date"] == today and matches(e, a.company)]
    print(f"{a.company}: {len(sent_today)} submitted today (limit {DAILY_COMPANY_CAP}/day)")
    if len(sent_today) >= DAILY_COMPANY_CAP:
        print("DAILY COMPANY CAP REACHED - do not apply today; log as Candidate with note 'company daily cap'")
    if hits:
        sys.exit(0)
    if len(sent_today) >= DAILY_COMPANY_CAP:
        sys.exit(2)
    print("not logged")
    sys.exit(1)


def row(e):
    return (f'{e["date"]} | {e["status"]:<14} | {e["company"]} | {e["role"]} | {e["location"]} | {e.get("notes", "")}'
            f' | {e.get("url") or "NO LINK"}')


def cmd_add(a):
    data = load()
    ids = [a.id] if a.id else []
    entry = next((e for e in data["jobs"] if a.id and a.id in e.get("ids", [])), None) or next(
        (e for e in data["jobs"] if norm(e["company"]) == norm(a.company) and norm(e["role"]) == norm(a.role)
         and not (a.id and e.get("ids"))), None)  # both have job ids and they differ: a different posting
    fields = {"date": a.date or dt.date.today().isoformat(), "company": a.company, "role": a.role,
              "location": a.location, "status": a.status}
    if entry:
        entry.update(fields)
        if a.notes:
            entry["notes"] = a.notes
        entry["ids"] = sorted(set(entry.get("ids", [])) | set(ids))
        action = "updated"
    else:
        entry = {**fields, "url": "", "notes": a.notes or "", "ids": ids}
        data["jobs"].append(entry)
        action = "added"
    if a.url:
        entry["url"] = a.url
    if not entry.get("url"):  # user rule 2026-10-13: every job keeps the link of the posting / application page
        if a.status.lower() == "submitted":
            sys.exit("ERROR: --url is required for Submitted jobs (link of the posting / application page). Nothing saved.")
        print("WARNING: no --url given - add the posting link (python applog.py add ... --url <link>)")
    if a.source:
        entry["source"] = a.source
    save(data)
    print(f'{action}: {entry["company"]} | {entry["role"]} | {entry["status"]} | {entry.get("url", "NO LINK")}')


def cmd_list(a, only_open=False):
    for e in load()["jobs"]:
        st = e["status"].lower()
        if (a.status and not st.startswith(a.status.lower())) or (only_open and not any(st.startswith(o) for o in OPEN_STATUSES)):
            continue
        if getattr(a, "no_link", False) and e.get("url"):
            continue
        print(row(e))


def cmd_note(a):
    data = load()
    data["roundNotes"].append(f'{dt.datetime.now():%Y-%m-%d %H:%M}: {a.text}')
    save(data)


def env_quote(value):
    return f"'{value}'" if '"' in value else f'"{value}"'


def find_answer(lines, question, key):
    """Index of the '# Q:' line of an existing entry with this key or question, else None."""
    for i, line in enumerate(lines):
        if key and line.startswith(f"{key}=") and i and lines[i - 1].startswith("# Q:"):
            return i - 1
        if line.strip().lower() == f"# q: {question}".lower() and i + 1 < len(lines) and lines[i + 1].startswith("ANSWER_"):
            return i
    return None


def upsert_answer(path, section, question, key, value):
    """Insert or update '# Q: question' + 'KEY=value' at the end of '# --- section ---' in an .env-style file.
    An existing entry (same question or key) keeps its key and position; the file's line endings are kept."""
    raw = path.read_bytes().decode("utf-8")
    eol = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.splitlines()
    i = find_answer(lines, question, key)
    if i is not None:
        key = lines[i + 1].split("=", 1)[0]
        lines[i:i + 2] = [f"# Q: {question}", f"{key}={value}"]
        action = "updated"
    else:
        new = [f"# Q: {question}", f"{key}={value}"]
        header = f"# --- {section} ---"
        if header in lines:
            j = lines.index(header) + 1
            while j < len(lines) and lines[j].strip() and not lines[j].startswith("# ---"):
                j += 1
            lines[j:j] = new
        else:
            lines += ["", header, *new]
        action = "added"
    path.write_bytes((eol.join(lines) + eol).encode("utf-8"))
    return action, key


def section_prefix(section):
    """Reuse the ANSWER_<PREFIX>_ already used in that section, else derive one from the section name."""
    lines = ENV.read_text(encoding="utf-8").splitlines()
    header = f"# --- {section} ---"
    if header in lines:
        for line in lines[lines.index(header) + 1:]:
            if line.startswith("# ---"):
                break
            m = re.match(r"ANSWER_([A-Z0-9]+)_", line)
            if m:
                return m.group(1)
    return re.sub(r"[^A-Z0-9]", "", section.upper())[:8] or "MISC"


def cmd_answer(a):
    key = a.key or "ANSWER_{}_{}".format(section_prefix(a.section),
                                         "_".join(re.sub(r"[^A-Z0-9]+", " ", a.question.upper()).split()[:8]))
    action, key = upsert_answer(ENV, a.section, a.question, key, env_quote(a.answer))
    if ENV_EXAMPLE.exists():
        upsert_answer(ENV_EXAMPLE, a.section, a.question, key, '""')
    print(f"{action}: {key} (section {a.section})")


REQUIRED_ENV = ["APPLICANT_EMAIL", "APPLICANT_PHONE", "APPLICANT_CITY_STATE_ZIP", "ANSWER_CONTACT_NAME",
                "ANSWER_ELIG_LEGALLY_AUTHORIZED_TO_WORK_IN_US", "ANSWER_ELIG_REQUIRE_SPONSORSHIP_NOW_OR_IN_FUTURE"]
UNSURE = re.compile(r"confirm with|assumed|unless confirmed|ask again|tell claude|\?\?|tbd|<[^>]+>", re.I)


def read_env(path):
    """KEY -> (value, question) for an .env-style file."""
    out, question = {}, ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# Q:"):
            question = line[4:].strip()
        m = re.match(r"([A-Z0-9_]+)=(.*)$", line)
        if m:
            v = m.group(2).strip()
            if len(v) >= 2 and v[0] in "\"'`" and v[-1] == v[0]:
                v = v[1:-1]
            out[m.group(1)] = (v, question if line.startswith("ANSWER_") else "")
            question = ""
    return out


def empty_fields(v, path=""):
    if isinstance(v, dict):
        for k, x in v.items():
            if not k.startswith("_"):
                yield from empty_fields(x, f"{path}.{k}" if path else k)
    elif isinstance(v, list):
        if not v:
            yield path
        for i, x in enumerate(v):
            yield from empty_fields(x, f"{path}[{i}]")
    elif v == "" or (isinstance(v, str) and UNSURE.search(v)):
        yield path


def cmd_missing(a):
    """What is still missing in private/ before the job search can start (setup step 6)."""
    required, optional = [], []
    env = read_env(ENV) if ENV.exists() else None
    if env is None:
        required.append("private/.env does not exist (copy examples/.env.example)")
    else:
        for k in REQUIRED_ENV:
            if not env.get(k, ("", ""))[0]:
                required.append(f".env {k}")
        if not any(k.startswith("ANSWER_") and v for k, (v, _) in env.items()):
            required.append(".env has no filled ANSWER_* entries")
        for k, (v, q) in env.items():
            if k in REQUIRED_ENV or k.startswith("BRIDGE_"):
                continue
            if not v or UNSURE.search(v):
                optional.append(f".env {k}" + (f'  (Q: {q})' if q else "") + (f'  now: "{v}"' if v else ""))
        if not env.get("BRIDGE_TOKEN", ("", ""))[0]:
            required.append(".env BRIDGE_TOKEN (run: npm run token in claude-browser-bridge/server)")
    cfg_path = PRIVATE / "job-search-config.json"
    if not cfg_path.exists():
        required.append("private/job-search-config.json does not exist (copy the example)")
    else:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        text = cfg_path.read_text(encoding="utf-8")
        for name in sorted(set(re.findall(r"\$\{([A-Z0-9_]+)\}", text))):
            if env is not None and not env.get(name, ("", ""))[0]:
                required.append(f"config uses ${{{name}}} but it is empty in .env")
        for key in ("roles", "seniority", "locations"):
            if not cfg.get(key):
                required.append(f"config {key} is empty")
        for path in empty_fields({k: cfg.get(k) for k in ("roles", "seniority", "locations", "sources", "skip")}):
            required.append(f"config {path} is empty or still a <placeholder>")
        for path in empty_fields(cfg.get("profile", {}), "profile"):
            optional.append(f"config {path}")
        step4 = next((s for s in cfg.get("autoApplyLoop", {}).get("steps", []) if s.startswith("4. ")), "")
        pdf = re.search(r"((?:[A-Za-z]:\\|\.\.\\)[^\s,;\"]+?\.pdf)", step4)
        pdf_path = None if not pdf else (Path(pdf.group(1)) if ":" in pdf.group(1) else (Path(__file__).parent / pdf.group(1)))
        if not pdf_path or not pdf_path.exists():
            required.append("resume PDF named in autoApplyLoop step 4 not found" + (f": {pdf.group(1)}" if pdf else ""))
    resume_path = PRIVATE / "resume.json"
    if not resume_path.exists():
        required.append("private/resume.json does not exist")
    else:
        resume = json.loads(resume_path.read_text(encoding="utf-8"))
        for key in ("experience", "education", "skills"):
            if not resume.get(key):
                required.append(f"resume.json {key} is empty")
        for path in empty_fields(resume, "resume"):
            optional.append(f"resume.json {path}")
    print("REQUIRED before the job search can start:" if required else "REQUIRED: all set")
    for r in required:
        print("  - " + r)
    print("Optional / unconfirmed (ask the user, or leave until a form needs it):" if optional else "Optional: nothing missing")
    for o in optional:
        print("  - " + o)
    sys.exit(1 if required else 0)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check"); c.add_argument("company"); c.add_argument("role", nargs="?"); c.add_argument("--id")
    ad = sub.add_parser("add")
    for f in ("company", "role", "location", "status"):
        ad.add_argument(f"--{f}", required=True)
    for f in ("notes", "id", "url", "source", "date"):
        ad.add_argument(f"--{f}")
    ls = sub.add_parser("list"); ls.add_argument("--status"); ls.add_argument("--no-link", action="store_true")
    sub.add_parser("open")
    n = sub.add_parser("note"); n.add_argument("text")
    an = sub.add_parser("answer")
    for f in ("section", "question", "answer"):
        an.add_argument(f"--{f}", required=True)
    an.add_argument("--key")
    sub.add_parser("missing")
    a = p.parse_args()
    if a.cmd == "check":
        cmd_check(a)
    elif a.cmd == "add":
        cmd_add(a)
    elif a.cmd == "list":
        cmd_list(a)
    elif a.cmd == "open":
        a.status = None
        cmd_list(a, only_open=True)
    elif a.cmd == "answer":
        cmd_answer(a)
    elif a.cmd == "missing":
        cmd_missing(a)
    else:
        cmd_note(a)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
