"""Tailored resume per job: the master LaTeX resume + the keywords the user marked as "I have this" on a job card.

On the job board the user clicks a missing keyword on a card -> it moves to "You have" for that job only (saved as the
job's `addedSkills` in ../private/job-board.json). When the job is in the apply queue the board server builds a copy of
the master resume (resumeTailor.masterTex in the config, in ../private/) with those keywords added to the matching
Technical Skills line, compiles it with pdfLaTeX (MiKTeX / TeX Live, the same engine Overleaf uses) into
../private/<resumeTailor.outDir>/<job>/<resumeTailor.pdfName>, and the queue run uploads that PDF for the job
(`python jobboard.py queue` prints the resume to use). Jobs without added keywords use the master resume PDF.
The master .tex is never changed.

Usage (from the JobSearch folder):
  python tailor.py <job key>       -> build that job's resume now (prints the PDF path)
  python tailor.py --check         -> show the LaTeX compiler found and the master files
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


def build(job, cfg=None):
    """Build the job's tailored resume. Returns {'status': 'ready' | 'master' | 'failed', 'pdf', 'added', 'note'}."""
    st = settings(cfg)
    keywords = list(job.get("addedSkills") or [])
    if not keywords or not st["enabled"]:
        return {"status": "master", "pdf": str(st["masterPdf"]), "added": [], "note": "no added keywords - master resume"}
    if not st["tex"] or not st["tex"].exists():
        return {"status": "failed", "pdf": str(st["masterPdf"]), "added": keywords,
                "note": "master .tex not found (resumeTailor.masterTex in the config)"}
    exe = compiler()
    if not exe:
        return {"status": "failed", "pdf": str(st["masterPdf"]), "added": keywords,
                "note": "no LaTeX compiler (install MiKTeX: winget install MiKTeX.MiKTeX)"}
    try:
        text, placed = tailor_tex(st["tex"].read_bytes().decode("utf-8"), keywords)  # keep the master's line endings
    except ValueError as e:
        return {"status": "failed", "pdf": str(st["masterPdf"]), "added": keywords, "note": str(e)}
    tag = hashlib.sha1(str(job.get("key", "")).encode()).hexdigest()[:8]  # unique per job
    out = st["outDir"] / f'{safe_name(job.get("company", "") + "-" + job.get("title", ""))}-{tag}'
    out.mkdir(parents=True, exist_ok=True)
    stem = Path(st["pdfName"]).stem
    (out / f"{stem}.tex").write_bytes(text.encode("utf-8"))
    cmd = [exe, "-interaction=nonstopmode", "-halt-on-error", f"-jobname={stem}", f"{stem}.tex"]
    if "miktex" in exe.lower():
        cmd.insert(1, "--enable-installer")  # MiKTeX: install missing packages without asking
    try:
        r = subprocess.run(cmd, cwd=out, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return {"status": "failed", "pdf": str(st["masterPdf"]), "added": keywords, "note": "pdflatex timed out"}
    pdf = out / f"{stem}.pdf"
    if r.returncode != 0 or not pdf.exists():
        err = next((l for l in r.stdout.splitlines() if l.startswith("!")), f"pdflatex exit {r.returncode}")
        return {"status": "failed", "pdf": str(st["masterPdf"]), "added": keywords, "note": f"LaTeX error: {err}"}
    for ext in (".aux", ".log", ".out"):
        (out / f"{stem}{ext}").unlink(missing_ok=True)
    pages = pdf_pages(pdf)
    where = ", ".join(f"{k} -> {v}" for k, v in placed.items()) or "all already on the resume"
    note = f"added {where}"
    if pages > st["maxPages"]:
        note += f" | WARNING: {pages} pages (more than {st['maxPages']})"
    return {"status": "ready", "pdf": str(pdf), "added": keywords, "placed": placed, "pages": pages, "note": note}


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
              f"output: {st['outDir']}")
        return 0
    import jobboard  # same folder (lazy: jobboard imports this module)
    return jobboard.build_resume(argv[0], wait=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
