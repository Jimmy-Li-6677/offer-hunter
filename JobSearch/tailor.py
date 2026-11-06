"""Tailored resume per job: the master LaTeX resume + the keywords the user marked as "I have this" on a job card.

On the job board the user clicks a missing keyword on a card -> it moves to "You have" for that job only (saved as the
job's `addedSkills` in ../private/job-board.json). When the job is in the apply queue the board server builds a copy of
the master resume (resumeTailor.masterTex in the config, in ../private/) with those keywords added to the matching
Technical Skills line, compiles it with pdfLaTeX (MiKTeX / TeX Live, the same engine Overleaf uses) into
../private/<resumeTailor.outDir>/<job>/<resumeTailor.pdfName>, and the queue run uploads that PDF for the job
(`python jobboard.py queue` prints the resume to use). Jobs without added keywords use the master resume PDF.
The master .tex is never changed.

Supporting the keyword in the experience (user 2026-11-06): Claude decides, from the master resume and resume.json,
whether the user's real experience already covers each added keyword. If one existing bullet truthfully does, it is
reworded to name the keyword (`python tailor.py place "<kw>" "<entry>" "<original>" "<new>"`, LaTeX copied exactly
from the master .tex); if not, the keyword stays on the Technical Skills line only (`python tailor.py place "<kw>"
skills`). Saved in ../private/keyword-placements.json and reused for every later job with that keyword. Rewording
keeps the resume length; a rewrite that would push it past resumeTailor.maxPages is left out.

Usage (from the JobSearch folder):
  python tailor.py <job key>                                -> build that job's resume now (prints the PDF path)
  python tailor.py --check                                  -> compiler, master files, resume entries
  python tailor.py placements                               -> saved placement for each keyword
  python tailor.py place "<kw>" skills                      -> Technical Skills only (experience doesn't show it)
  python tailor.py place "<kw>" "<entry>" "<original>" "<new>" -> reword that bullet; rebuilds queued jobs using it
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import applog  # same folder: log helpers

PRIVATE = applog.PRIVATE
CONFIG = PRIVATE / "job-search-config.json"

# which skills line a keyword goes on: (kind, keyword regex); first match wins, else "tools"
KINDS = [
    ("lang", r"^(Go|Golang|Rust|Kotlin|Swift|Ruby|PHP|Scala|Perl|R|MATLAB|Bash|Shell( scripting)?|HTML5?|CSS3?|"
             r"Objective-C|Dart|Elixir|Haskell|Lua|Julia|Groovy|F#|VB\.NET|Visual Basic|Solidity|Assembly|Fortran|"
             r"COBOL|TypeScript|JavaScript|Python|Java|C\+\+|C#|C)$"),
    ("ai", r"\b(AI|ML|LLMs?|GenAI|Generative|Machine learning|Deep learning|NLP|Computer vision|PyTorch|TensorFlow|"
           r"Keras|scikit|Hugging ?Face|LangChain|RAG|Agents?|MCP|OpenAI|Copilot|Cursor|Prompt|Embeddings?|Vector)\b"),
    ("data", r"\b(SQL|NoSQL|Postgre|MySQL|Oracle|Mongo|Redis|Cassandra|DynamoDB|Elasticsearch|Snowflake|BigQuery|"
             r"Redshift|Databricks|Spark|Hadoop|Kafka|RabbitMQ|Airflow|ETL|Data|Databases?|GraphQL|gRPC|REST|APIs?|"
             r"Queues?|Streaming|Pandas|NumPy|Tableau|Power BI|Excel|Microservices|Event)\b"),
    ("devops", r"\b(AWS|Azure|GCP|Google Cloud|Cloud|Docker|Kubernetes|K8s|Terraform|Ansible|Helm|CI/CD|Jenkins|"
               r"GitHub|GitLab|Git|DevOps|Linux|Unix|Test(s|ing)?|QA|Selenium|Cypress|Playwright|Jest|JUnit|pytest|"
               r"Monitoring|Observability|Prometheus|Grafana|Datadog|Splunk|Security|Lambda|S3|EC2|Serverless|Nginx|"
               r"IIS|Agile|Scrum|Jira)\b"),
    ("web", r"\b(React|Angular|Vue|Svelte|Next|Node|Express|Spring|Django|Flask|FastAPI|Rails|Laravel|\.NET|ASP|"
            r"Blazor|MAUI|Xamarin|Flutter|Android|iOS|Tailwind|Bootstrap|jQuery|Redux|Frontend|Front-end|Backend|"
            r"Back-end|Full[- ]?stack|Web|Mobile|UI|UX|Frameworks?)\b"),
]
# which line label (the \textbf{...} text in the Technical Skills section) takes each kind
LINE_FOR = {"lang": r"language", "web": r"framework|web|front|mobile", "data": r"data|api|database|backend",
            "devops": r"devops|test|cloud|infra|tool", "ai": r"\bai\b|machine|ml|tool", "tools": r"tool|other|ai"}
SKILL_LINE = re.compile(r"^(\s*\\textbf\{(?P<label>[^}]*)\}\{:\s*)(?P<items>.*?)(?P<end>\}\s*(?:\\\\)?\s*)$")


def settings(cfg=None):
    cfg = cfg if cfg is not None else json.loads(CONFIG.read_text(encoding="utf-8"))
    rt = cfg.get("resumeTailor") or {}
    tex = PRIVATE / rt["masterTex"] if rt.get("masterTex") else next(iter(sorted(PRIVATE.glob("*.tex"))), None)
    pdf_name = rt.get("pdfName") or (tex.stem + ".pdf" if tex else "Resume.pdf")
    return {"enabled": rt.get("enabled", True), "tex": tex, "pdfName": pdf_name,
            "masterPdf": PRIVATE / (rt.get("masterPdf") or pdf_name), "outDir": PRIVATE / rt.get("outDir", "resumes"),
            "maxPages": rt.get("maxPages", 1)}


def compiler():
    """Path of pdflatex (PATH, or the usual MiKTeX / TeX Live folders), or None."""
    found = shutil.which("pdflatex")
    if found:
        return found
    roots = [os.environ.get("LOCALAPPDATA", "") + r"\Programs\MiKTeX", r"C:\Program Files\MiKTeX",
             os.environ.get("APPDATA", "") + r"\MiKTeX"]
    for root in roots:
        for p in Path(root).glob("miktex/bin/*/pdflatex.exe") if root and Path(root).exists() else []:
            return str(p)
    for p in sorted(Path("C:/texlive").glob("*/bin/*/pdflatex.exe"), reverse=True) if Path("C:/texlive").exists() else []:
        return str(p)
    return None


def latex_escape(s):
    return re.sub(r"([&%$#_{}])", r"\\\1", s).replace("~", r"\textasciitilde{}").replace("^", r"\textasciicircum{}")


def kind_of(keyword):
    for kind, rx in KINDS:
        if re.search(rx, keyword, re.I if kind != "lang" else 0):
            return kind
    return "tools"


def tailor_tex(tex, keywords):
    """The master .tex text with each keyword added to the end of the best-fitting Technical Skills line.
    Keywords already on a skills line are left out. Returns (new text, {keyword: line label})."""
    lines = tex.splitlines(keepends=True)
    skill_rows = [i for i, l in enumerate(lines) if SKILL_LINE.match(l.rstrip("\r\n"))]
    if not skill_rows:
        raise ValueError("no Technical Skills lines (\\textbf{Label}{: item, item}) found in the master resume")
    placed = {}
    for kw in keywords:
        plain = lambda i: re.sub(r"\\(.)", r"\1", SKILL_LINE.match(lines[i].rstrip("\r\n"))["items"])
        if any(re.search(rf"(?<![\w+#]){re.escape(kw)}(?![\w+#])", plain(i), re.I) for i in skill_rows):
            continue  # already listed
        want = LINE_FOR[kind_of(kw)]
        row = next((i for i in skill_rows if re.search(want, SKILL_LINE.match(lines[i].rstrip("\r\n"))["label"], re.I)),
                   skill_rows[-1])
        body, nl = lines[row].rstrip("\r\n"), lines[row][len(lines[row].rstrip("\r\n")):]
        m = SKILL_LINE.match(body)
        items = m["items"].rstrip()
        lines[row] = f'{m.group(1)}{items}{", " if items else ""}{latex_escape(kw)}{m["end"]}{nl}'
        placed[kw] = re.sub(r"\\(.)", r"\1", m["label"])
    return "".join(lines), placed


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")[:60] or "job"


def pdf_pages(path):
    return len(re.findall(rb"/Type\s*/Page(?!s)", Path(path).read_bytes()))


# ---------- placements: where each added keyword is supported by the user's real experience ----------
# ../private/keyword-placements.json, written by Claude (`python tailor.py place ...`) from the master resume and
# resume.json: either a rewrite of ONE existing bullet that truthfully covers the keyword, or "skills" (the experience
# doesn't show it -> Technical Skills line only). Reused for every later job with the same keyword.
PLACEMENTS = PRIVATE / "keyword-placements.json"
SKILLS_ONLY = "skills"


def load_placements():
    try:
        return json.loads(PLACEMENTS.read_text(encoding="utf-8")).get("keywords", {})
    except (OSError, ValueError):
        return {}


def save_placement(keyword, value):
    data = load_placements()
    data[keyword] = value
    PLACEMENTS.write_text(json.dumps({"_about": "Per keyword the user marked 'I have this' on the job board: where = "
                                      "'skills' (Technical Skills only) or the resume entry whose bullet `original` "
                                      "(LaTeX, exact text in the master .tex) is replaced by `bullet` in that job's "
                                      "tailored resume. Written by Claude from the user's real experience (tailor.py).",
                                      "keywords": data}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


_SUBHEADING = re.compile(r"\\resumeSubheading\s*\{([^}]*)\}")
_PROJECT = re.compile(r"\\resumeProjectHeading\s*\{\s*\\textbf\{([^}]*)\}")


def entries(tex):
    """[(name, first line, last line)] of each work experience (company) / project that has bullets in the .tex."""
    lines, out = tex.splitlines(), []
    for i, l in enumerate(lines):
        nxt = l + " " + (lines[i + 1] if i + 1 < len(lines) else "")
        m = (_SUBHEADING.search(nxt) if "\\resumeSubheading" in l else
             _PROJECT.search(nxt) if "\\resumeProjectHeading" in l else None)
        if m and any("\\resumeItemListStart" in x for x in lines[i + 1:i + 5]):  # entries with bullets only
            end = next((k for k in range(i, len(lines)) if "\\resumeItemListEnd" in lines[k]), len(lines) - 1)
            out.append((re.sub(r"\\(.)", r"\1", m.group(1)).strip(), i, end))
    return out


def entry_names(cfg=None):
    try:
        return [n for n, _, _ in entries(settings(cfg)["tex"].read_bytes().decode("utf-8"))]
    except (OSError, AttributeError):
        return []


def rewrite(tex, where, original, new):
    """tex with `original` replaced by `new` inside entry `where`, or None when that text is not there (any more)."""
    lines = tex.splitlines(keepends=True)
    for name, a, b in entries(tex):
        if name == where:
            part = "".join(lines[a:b + 1])
            if original in part:
                return "".join(lines[:a]) + part.replace(original, new, 1) + "".join(lines[b + 1:])
    return None


def _compile(exe, out, stem, text):
    (out / f"{stem}.tex").write_bytes(text.encode("utf-8"))
    cmd = [exe, "-interaction=nonstopmode", "-halt-on-error", f"-jobname={stem}", f"{stem}.tex"]
    if "miktex" in exe.lower():
        cmd.insert(1, "--enable-installer")  # MiKTeX: install missing packages without asking
    try:
        r = subprocess.run(cmd, cwd=out, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return "pdflatex timed out"
    if r.returncode != 0 or not (out / f"{stem}.pdf").exists():
        return "LaTeX error: " + next((l for l in r.stdout.splitlines() if l.startswith("!")), f"pdflatex exit {r.returncode}")
    for ext in (".aux", ".log", ".out"):
        (out / f"{stem}{ext}").unlink(missing_ok=True)
    return None


def build(job, cfg=None):
    """Build the job's tailored resume: added keywords on the Technical Skills lines, plus each keyword's placement
    (a reworded existing bullet, keyword-placements.json); a rewrite that no longer applies or would push the resume past
    resumeTailor.maxPages is left out (Skills only). Returns {'status': 'ready' | 'master' | 'failed', 'pdf', 'added',
    'note', 'bullets': {keyword: entry}, 'needsPlacement': [keywords Claude has not placed yet]}."""
    st = settings(cfg)
    keywords = list(job.get("addedSkills") or [])
    fail = lambda note: {"status": "failed", "pdf": str(st["masterPdf"]), "added": keywords, "note": note}
    if not keywords or not st["enabled"]:
        return {"status": "master", "pdf": str(st["masterPdf"]), "added": [], "note": "no added keywords - master resume"}
    if not st["tex"] or not st["tex"].exists():
        return fail("master .tex not found (resumeTailor.masterTex in the config)")
    exe = compiler()
    if not exe:
        return fail("no LaTeX compiler (install MiKTeX: winget install MiKTeX.MiKTeX)")
    master = st["tex"].read_bytes().decode("utf-8")  # bytes: keep the master's line endings
    try:
        base, placed = tailor_tex(master, keywords)
    except ValueError as e:
        return fail(str(e))
    pl = load_placements()
    needs = [kw for kw in keywords if kw not in pl]
    todo = [(kw, pl[kw]) for kw in keywords if kw in pl and pl[kw].get("where") != SKILLS_ONLY]
    tag = hashlib.sha1(str(job.get("key", "")).encode()).hexdigest()[:8]  # unique per job
    out = st["outDir"] / f'{safe_name(job.get("company", "") + "-" + job.get("title", ""))}-{tag}'
    out.mkdir(parents=True, exist_ok=True)
    stem = Path(st["pdfName"]).stem
    dropped, missed = [], []
    while True:
        text, used = base, []
        for kw, p in todo:
            t = rewrite(text, p.get("where"), p.get("original", ""), p.get("bullet", ""))
            if t is None:
                missed.append(kw) if kw not in missed else None
            else:
                text, used = t, used + [(kw, p["where"])]
        err = _compile(exe, out, stem, text)
        if err:
            return fail(err)
        pages = pdf_pages(out / f"{stem}.pdf")
        applied = [kw for kw, _ in used]
        if pages <= st["maxPages"] or not applied:
            break
        last = applied[-1]  # too long: leave the last rewrite out and build again
        dropped.insert(0, last)
        todo = [(kw, p) for kw, p in todo if kw != last]
    note = "added " + (", ".join(f"{k} -> {v}" for k, v in placed.items()) or "nothing new to Skills (already listed)")
    if used:
        note += " | in experience: " + ", ".join(f"{kw} -> {w}" for kw, w in used)
    if dropped:
        note += f" | Skills only to stay at {st['maxPages']} page(s): {', '.join(dropped)}"
    if missed:
        note += f" | bullet text changed, Skills only: {', '.join(missed)}"
    if needs:
        note += f" | not placed yet: {', '.join(needs)}"
    if pages > st["maxPages"]:
        note += f" | WARNING: {pages} pages (more than {st['maxPages']})"
    return {"status": "ready", "pdf": str(out / f"{stem}.pdf"), "added": keywords, "placed": placed, "pages": pages,
            "bullets": dict(used), "dropped": dropped, "needsPlacement": needs, "note": note}


def place(keyword, where, original=None, new=None):
    """Save a keyword's placement (checked against the master .tex). Returns an error text, or None."""
    if where == SKILLS_ONLY:
        save_placement(keyword, {"where": SKILLS_ONLY})
        return None
    tex = settings()["tex"].read_bytes().decode("utf-8")
    if where not in [n for n, _, _ in entries(tex)]:
        return f"no resume entry {where!r} (entries: {', '.join(entry_names())})"
    if not original or not new or rewrite(tex, where, original, new) is None:
        return "the original text must be copied exactly from that entry's bullet in the master .tex"
    save_placement(keyword, {"where": where, "original": original, "bullet": new})
    return None


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        if stream:
            stream.reconfigure(encoding="utf-8", errors="replace")
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--check":
        st = settings()
        print(f"compiler: {compiler() or 'NOT FOUND'}\nmaster tex: {st['tex']}\nmaster pdf: {st['masterPdf']}\n"
              f"output: {st['outDir']}\nresume entries: {', '.join(entry_names())}")
        return 0
    if argv[0] == "placements":
        for kw, p in sorted(load_placements().items()):
            print(f'{kw}: {p.get("where")}' + (f'\n    was: {p["original"]}\n    now: {p["bullet"]}' if p.get("bullet") else ""))
        return 0
    import jobboard  # same folder (lazy: jobboard imports this module)
    if argv[0] == "place" and len(argv) in (3, 5):
        err = place(*argv[1:])
        if err:
            print(f"{argv[1]}: {err}")
            return 1
        print(f"{argv[1]}: placed ({argv[2]})")
        for j in jobboard.queue(jobboard.load_board()):  # rebuild queued jobs that use it
            if argv[1] in (j.get("addedSkills") or []):
                jobboard.build_resume(j["key"], wait=True)
        return 0
    return jobboard.build_resume(argv[0], wait=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
