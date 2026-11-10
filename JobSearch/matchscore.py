"""Resume keyword match score for each job on the board - plain rules, no AI / no tokens.

Score = how many of the job's qualification keywords your resume has:
    matched keywords / keywords found in the post  (shown as e.g. "78% - 7/9 keywords")
Keywords are the tech terms of the VOCAB list below (about 200, specific: Lambda / S3 / DynamoDB are separate from
AWS) found in the job title and the posting's Minimum / Required Qualifications and Preferred Qualifications sections
(user 2026-11-06: responsibilities, the company blurb and benefits do not count) - so more matches = higher score -
plus the skills on Simplify's own skill list that those qualifications name (job["simplifySkills"]). Nothing else counts: experience, level, location and pay
stay on the card for you to judge.

Beyond VOCAB, the skill bank (skillbank.py: about 4,000 tools / languages / platforms from O*NET and Stack Overflow
with their other names, plus every skill Simplify tags) is matched too, and so is every item of your resume's skills
lists - so a keyword counts however the post words it ("Postgres" / "PostgreSQL", "k8s" / "Kubernetes").

Your keywords come from ../private/resume.json (skills lists, job bullets, projects). Add a term to VOCAB to give it a
careful pattern of its own (VOCAB wins over the bank).

  python matchscore.py            -> every board job's score, best first
  python matchscore.py --profile  -> the keywords read from your resume
"""
import functools
import json
import re
import sys

import applog  # same folder: paths
import glossary  # same folder: what each keyword means (the card's Missing list)
import jobinfo  # same folder: where a post's "preferred / nice to have" part starts
import skillbank  # same folder: thousands more skills + their other names (O*NET, Stack Overflow, Simplify)

RESUME = applog.PRIVATE / "resume.json"

# canonical name -> regex (case-insensitive unless the pattern says (?-i:...); matched as a whole token, so "Go"
# does not match "Google"). Specific services / topics are separate keywords (Lambda, S3, DynamoDB ... not just
# "AWS"), so the count is as fine-grained as the posting.
VOCAB = {
    # languages
    "C#": r"c#|c-sharp|csharp", "C++": r"c\+\+|cpp", "C": r"(?-i:C)(?=\s*(?:/|,|and|or)\s*(?:c\+\+|C\+\+))|c\+\+\s*(?:/|,|and|or)\s*(?-i:C)(?![A-Za-z0-9+#])|\bc\s+programming|\bc\s+language",
    "Java": r"java(?!\s*script)", "JavaScript": r"javascript|(?-i:JS)|ecmascript|es6", "TypeScript": r"typescript|(?-i:TS)",
    "Python": r"python", "Go": r"golang|(?-i:Go)(?=\s+(?:is|and|or|programming|language|developer|services?|code)|\s*[,/)]|$)",
    "Rust": r"(?-i:Rust)", "Kotlin": r"kotlin", "Swift": r"(?-i:Swift)", "Objective-C": r"objective-?c",
    "Ruby": r"ruby", "Ruby on Rails": r"rails|ruby on rails", "PHP": r"php", "Scala": r"scala",
    "R": r"(?-i:R)(?=\s*(?:,|/|\)|and|or)\s*(?:python|sql|matlab|Python|SQL|MATLAB))", "PowerShell": r"powershell",
    "Bash/Shell": r"bash|shell scripting|shell scripts?|zsh", "MATLAB": r"matlab", "Perl": r"perl", "Lua": r"lua",
    "Dart/Flutter": r"dart|flutter", "Elixir": r"elixir", "Haskell": r"haskell", "Solidity": r"solidity",
    # .NET / backend frameworks
    ".NET": r"\.net(?:\s*core|\s*framework)?|dotnet", "ASP.NET": r"asp\.net(?:\s*core|\s*mvc)?", "LINQ": r"linq",
    ".NET MAUI": r"\.net maui|maui|xamarin", "Entity Framework": r"entity framework|ef core", "Blazor": r"blazor",
    "WPF/WinForms": r"wpf|winforms|windows forms", "Spring": r"spring(?:\s*boot)?", "Django": r"django",
    "Flask": r"flask", "FastAPI": r"fastapi", "Express": r"express(?:\.js|js)|(?-i:Express)(?=\s*(?:/|,|\)|and\b|or\b))", "NestJS": r"nest\.?js",
    "Laravel": r"laravel", "Hangfire": r"hangfire",
    # frontend / web / mobile
    "React": r"react(?:\.js|js)?(?!\s*native)", "React Native": r"react native", "Angular": r"angular(?:js)?",
    "Vue": r"vue(?:\.js)?", "Next.js": r"next\.js|nextjs", "Node.js": r"node(?:\.js|js)?", "Svelte": r"svelte",
    "Redux": r"redux", "HTML": r"html5?", "CSS": r"css3?|sass|scss|less css", "Tailwind": r"tailwind(?:\s*css)?",
    "Bootstrap": r"bootstrap", "jQuery": r"jquery", "Webpack/Vite": r"webpack|vite|babel", "Leaflet/Maps": r"leaflet|mapbox|gis",
    "Accessibility": r"(?:web |digital |ui )?accessibility(?! of)|a11y|wcag|ada complian\w*|section 508", "Responsive design": r"responsive (?:design|web|ui)",
    "iOS": r"(?-i:iOS)", "Android": r"android", "Mobile apps": r"mobile app(?:lication)?s?|mobile development",
    "Chrome extensions": r"chrome extensions?|browser extensions?",
    # APIs / architecture
    "REST APIs": r"restful|(?-i:REST)(?:ful)?(?:\s*apis?|\s*services)?|rest\s*apis?|rest\s*services|web\s*apis?", "GraphQL": r"graphql", "gRPC": r"grpc",
    "WebSockets": r"web\s*sockets?|signalr|socket\.io", "API design": r"api design|designing apis",
    "Microservices": r"micro-?services?", "Event-driven": r"event[- ]driven|event bus|message bus|pub/?sub",
    "Message queues": r"message queues?|queues?|queueing|sqs|sns|rabbitmq|service bus",
    "Asynchronous processing": r"async(?:hronous)?(?:\s*processing|\s*programming|\s*jobs)?|background (?:jobs|processing|workers)",
    "Distributed systems": r"distributed systems?", "System design": r"system design|software architecture|scalab(?:le|ility)",
    "Caching": r"caching|cache", "Swagger/OpenAPI": r"swagger|openapi", "Postman": r"postman", "JSON": r"json", "XML": r"xml",
    # data
    "SQL": r"sql|t-sql|tsql|pl/sql", "Stored procedures": r"stored procedures?", "MySQL": r"mysql",
    "PostgreSQL": r"postgres(?:ql)?", "SQL Server": r"sql server|mssql", "Oracle DB": r"oracle(?:\s*db|\s*database)?",
    "MongoDB": r"mongo(?:db)?", "Redis": r"redis", "DynamoDB": r"dynamo\s*db", "Cassandra": r"cassandra",
    "Cosmos DB": r"cosmos\s*db", "NoSQL": r"nosql", "Database design": r"database design|data model(?:ing|ling)?|schema design",
    "Elasticsearch": r"elastic(?:search)?|opensearch", "Kafka": r"kafka", "Kinesis": r"kinesis", "Spark": r"spark|pyspark",
    "Databricks": r"databricks", "Airflow": r"airflow", "Snowflake": r"snowflake", "BigQuery": r"bigquery", "dbt": r"(?-i:dbt)",
    "ETL/data pipelines": r"etl|elt|data pipelines?",
    "Streaming/batch pipelines": r"stream(?:ing)? (?:pipelines?|processing|data)|batch(?:/streaming)? (?:pipelines?|processing|jobs)|real[- ]time (?:data|pipelines?)",
    "Data lakes/warehouses": r"data lakes?|data warehouses?|lakehouse", "Data engineering": r"data engineering",
    "Pandas/NumPy": r"pandas|numpy", "Excel": r"excel", "Tableau/Power BI": r"tableau|power ?bi|looker", "Analytics": r"analytics",
    # cloud
    "AWS": r"aws|amazon web services", "AWS Lambda": r"lambda", "AWS S3": r"s3", "AWS EC2": r"ec2", "AWS ECS/Fargate": r"ecs|fargate",
    "AWS Step Functions": r"step functions", "AWS CloudFormation/CDK": r"cloudformation|aws cdk|cdk", "AWS Amplify": r"amplify",
    "Azure": r"azure(?!\s*(?:devops|pipelines?))|microsoft azure", "Azure Functions": r"azure functions",
    "GCP": r"gcp|google cloud", "Serverless": r"serverless", "Cloud": r"cloud(?:-native| computing| services| platforms?| infrastructure)",
    # devops / infra
    "Docker": r"docker|containers?|containeri[sz]ation", "Kubernetes": r"kubernetes|k8s|eks|aks|gke", "Helm": r"helm",
    "CI/CD": r"ci/cd|ci\s*/\s*cd|continuous (?:integration|delivery|deployment)|(?:build|deployment|release|ci|cd) pipelines?",
    "Azure DevOps": r"azure devops|ado pipelines?|azure pipelines?", "GitHub Actions": r"github actions", "Jenkins": r"jenkins",
    "GitLab CI": r"gitlab ci", "Terraform": r"terraform", "Infrastructure as code": r"infrastructure[- ]as[- ]code|(?-i:IaC)",
    "Ansible": r"ansible", "Git": r"git|version control", "GitHub": r"github", "Linux": r"linux|unix", "IIS": r"iis", "Nginx": r"nginx",
    "DevOps": r"devops", "SRE": r"site reliability|(?-i:SRE)",
    "Observability/monitoring": r"observability|(?:application|infrastructure|performance|production|service|system health) monitoring|monitoring and alerting|alerting|datadog|grafana|prometheus|splunk|new relic|opentelemetry|structured logging",
    "Networking": r"networking|tcp/ip|tcp|http/2|dns|load balanc(?:er|ing)",
    "Performance tuning": r"performance (?:tuning|optimi[sz]ation)|profiling|latency",
    # security / compliance
    "Security": r"application security|appsec|security (?:engineering|systems|tools|tooling|platforms?|software|and privacy)|secure coding|threat model(?:ing|ling)?",
    "Authentication/authorization": r"authentication|(?<!work )(?<!employment )authorization(?! to work)(?! & sponsorship)|oauth\s*2?|openid|(?-i:SSO)|single sign[- ]on|jwt|(?-i:IAM)|identity and access",
    "Vulnerability management": r"vulnerabilit(?:y|ies)(?:\s*scanning|\s*management|\s*remediation)?|penetration testing|owasp",
    "Privacy/compliance": r"(?:data |user |consumer )?privacy(?! (?:statement|notice|policy|act|rights))|gdpr|ccpa|hipaa|soc\s*2|fedramp|pci(?:[- ]dss)?|compliance",
    "Cryptography": r"cryptograph(?:y|ic)|encryption",
    # testing / quality
    "Unit testing": r"unit tests?|unit testing|unit\s*&\s*integration|(?-i:TDD)|test[- ]driven",
    "Integration testing": r"integration tests?|integration testing", "End-to-end testing": r"end[- ]to[- ]end tests?|e2e tests?",
    "xUnit/NUnit": r"xunit|nunit|mstest", "Jest": r"jest", "Selenium": r"selenium", "Cypress": r"cypress",
    "Playwright": r"playwright", "JUnit": r"junit", "Pytest": r"pytest",
    "Test automation": r"test automation|automated testing|automation testing", "QA": r"(?-i:QA)|quality assurance",
    "Regression testing": r"regression testing", "Debugging": r"debugg?(?:ing|er)|troubleshoot(?:ing)?", "Code review": r"code reviews?",
    # AI / ML
    "Machine learning": r"machine learning|(?-i:ML)(?!\s*ops)", "Deep learning": r"deep learning|neural networks?",
    "LLMs": r"llms?|large language models?|(?-i:GPT)|openai|anthropic|claude(?!\s*code)", "Generative AI": r"generative ai|gen ?ai",
    "Prompt engineering": r"prompt engineering|prompting|prompts?", "RAG": r"(?-i:RAG)|retrieval[- ]augmented",
    "AI agents": r"ai agents?|agentic|agent workflows?|tool calling|function calling", "MCP": r"(?-i:MCP)|model context protocol",
    "AI coding tools": r"ai[- ]assisted (?:development|coding|analysis)|copilot|cursor|claude code|ai coding tools?|ai-powered tooling|llm-powered tooling",
    "Vector databases": r"vector (?:databases?|search|stores?)|embeddings|pinecone|pgvector", "PyTorch": r"pytorch",
    "TensorFlow": r"tensorflow|keras", "scikit-learn": r"scikit-?learn|sklearn", "NLP": r"(?-i:NLP)|natural language processing",
    "Computer vision": r"computer vision|opencv", "MLOps": r"mlops", "Hugging Face": r"hugging\s*face|transformers library",
    # practices / other
    "Agile/Scrum": r"agile|scrum|kanban|sprints?", "Object-oriented": r"object[- ]oriented|(?-i:OOP)", "SOLID": r"solid principles",
    "Design patterns": r"design patterns", "Data structures": r"data structures?", "Algorithms": r"algorithms?",
    "Concurrency": r"concurren(?:cy|t)|multi-?thread(?:ing|ed)|parallel(?:ism| processing)",
    "Internal/developer tooling": r"internal tool(?:ing|s)|developer tool(?:ing|s)|developer productivity|developer experience|dev tools",
    "Automation/scripting": r"automation|scripting|scripts", "Documentation": r"technical documentation|documentation",
    "Jira/Confluence": r"jira|confluence", "Full stack": r"full[- ]?stack", "Backend": r"back[- ]?end", "Frontend": r"front[- ]?end",
    # areas you don't have yet (recognised so they show as missing)
    "Embedded": r"embedded|firmware|bare[- ]metal|rtos|microcontrollers?", "Hardware interfaces": r"(?-i:SPI|I2C|UART|JTAG)|can\s*bus",
    "ROS": r"(?-i:ROS)\s*2?|robot operating system", "FPGA/Verilog": r"fpga|verilog|vhdl", "Unity/Unreal": r"unity3?d?|unreal",
    "Salesforce": r"salesforce|apex", "SAP": r"(?-i:SAP)", "Mainframe/COBOL": r"cobol|mainframe",
}
# areas you don't have: only counted from resume.json skills lists, never from job bullets (e.g. "firmware rule
# handling" in an identity-verification SDK job is not embedded development)
SKILLS_LIST_ONLY = {"Embedded", "Hardware interfaces", "ROS", "FPGA/Verilog", "Unity/Unreal", "Salesforce", "SAP",
                    "Mainframe/COBOL"}

# having the key implies the values (GitHub means you use Git; S3 / Amplify are AWS)
IMPLIES = {"GitHub": {"Git"}, "Azure DevOps": {"CI/CD", "Git", "DevOps"}, "AWS S3": {"AWS"}, "AWS Amplify": {"AWS"},
           "AWS Lambda": {"AWS", "Serverless"}, "ASP.NET": {".NET"}, ".NET MAUI": {".NET"}, "Next.js": {"React"},
           "React Native": {"Mobile apps"}, "xUnit/NUnit": {"Unit testing"}, "Stored procedures": {"SQL"},
           "MySQL": {"SQL"}, "PostgreSQL": {"SQL"}, "SQL Server": {"SQL"}, "TypeScript": {"JavaScript"},
           # a cloud provider is cloud experience; SOLID / C# / Java are object-oriented; Hangfire jobs run in the background
           "AWS": {"Cloud"}, "Azure": {"Cloud"}, "GCP": {"Cloud"}, "SOLID": {"Object-oriented"},
           "C#": {"Object-oriented"}, "Java": {"Object-oriented"}, "Hangfire": {"Asynchronous processing"}}

_B = r"(?<![A-Za-z0-9+#./-])(?:{})(?![A-Za-z0-9+#])"
_RX = {name: re.compile(_B.format(rx), re.I) for name, rx in VOCAB.items()}


def terms(text):
    """Vocabulary keywords found in a text (a copy of the cached result)."""
    return set(_terms(text or ""))


@functools.lru_cache(maxsize=8192)
def _terms(text):
    # cached per text: a job's title / description don't change between page refreshes, so only new jobs are scanned
    return frozenset(name for name, rx in _RX.items() if rx.search(text))


def _own_items(listed):
    """{key: name} for resume skills items that neither VOCAB nor the bank knows (e.g. 'Hangfire'), so a post that
    names one counts it as a keyword you have."""
    index, _ = skillbank.load()
    out = {}
    for x in listed:
        name = str(x).strip()
        k = skillbank.key_of(name)
        if k and len(k) >= 2 and not terms(name) and k not in index and k.replace(" ", "") not in index:
            out[k] = name
    return out


def profile(resume=None):
    """Your keywords: vocabulary + skill bank terms in resume.json (skills lists, job + project bullets, summary)."""
    if resume is None:
        try:
            resume = json.loads(RESUME.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            resume = {}
    listed = []
    for v in (resume.get("skills") or {}).values():
        listed.extend(v if isinstance(v, list) else [str(v)])
    parts = []
    for e in resume.get("experience") or []:
        parts += [e.get("title", "")] + list(e.get("bullets") or [])
    for p in resume.get("projects") or []:
        parts += [p.get("name", ""), p.get("tech", "")] + list(p.get("bullets") or [])
    parts.append(resume.get("summary") or "")
    skills = terms("\n".join(map(str, listed))) | (terms("\n".join(map(str, parts))) - SKILLS_LIST_ONLY)
    skills |= {str(x).strip() for x in listed if str(x).strip() in VOCAB}  # an item that IS a keyword name ("C", "Go")
    while True:  # implied skills can imply more (AWS S3 -> AWS -> Cloud)
        more = set().union(*(IMPLIES.get(t, set()) for t in skills)) - skills
        if not more:
            break
        skills |= more
    text = "\n".join(map(str, listed + parts))  # for keywords outside VOCAB (e.g. from Simplify's skill list)
    own = _own_items(listed)
    skills |= skillbank.find(text, own) | set(own.values())
    return {"skills": skills, "text": text, "own": own}


def keywords(text, prof=None, company=""):
    """Every keyword in a text: VOCAB terms + skill bank terms + your own resume skill items, without the hiring
    company's own name (a Fortinet post naming "Fortinet" is not a skill)."""
    found = terms(text) | skillbank.find(text, (prof or {}).get("own"))
    ck = skillbank.norm(company)
    if ck:
        found = {t for t in found if t in VOCAB or skillbank.norm(t) not in ck}
    return found


_SIMPLIFY_PARTS = re.compile(r"\bRequirements: (?P<req>.*?)(?: Preferred: (?P<pref>.*?))?(?: Description: (?P<desc>.*))?$", re.S)


# the closing boilerplate of a posting (EEO, benefits, pay, privacy notices) names no skills of the job
_BOILERPLATE = re.compile(r"equal (?:employment )?opportunity|\bEEO\b|privacy (?:statement|notice|policy)|#LI-|"
                          r"pay transparency|(?:base )?(?:pay|salary|compensation) range|benefits (?:include|package)|"
                          r"what we offer|our benefits|reasonable accommodation|e-verify", re.I)


def _cut(text):
    """(required part, preferred part) of a posting's text: split at the first 'preferred / desired / nice to have';
    the closing boilerplate (first EEO / benefits / pay / privacy marker after the first 300 characters) is left out."""
    b = _BOILERPLATE.search(text, 300)
    text = text[:b.start()] if b else text
    m = jobinfo._PREFERRED.search(text)
    return (text[:m.start()], text[m.start():]) if m else (text, "")


# ---------- which part of a posting counts: its Minimum / Required and Preferred Qualifications sections ----------
_Q = r"(?:qualifications?|requirements?|skills|experience)"
# headings, in order of precedence; case-insensitive phrases, plus a few bare words that must be capitalised (a heading,
# not a word in a sentence)
_HEADINGS = [
    ("pref", re.compile(r"\b(?:preferred|desired|desirable|additional|bonus|nice[- ]to[- ]have|plus)\s+" + _Q + r"\b|"
                        r"\bnice[- ]to[- ]haves?\b|\bbonus points\b|\bit'?s a plus\b|\b(?:experience|skills) that would be helpful\b|"
                        r"\bwhat will help you (?:thrive|stand out)\b|\bways to stand out\b|\bstand out from the crowd\b|\bpluses\b|\bwe(?:'d| would) love\b", re.I)),
    ("req", re.compile(r"\b(?:minimum|basic|required|must[- ]have|key|core|job|position)\s+" + _Q + r"\b|"
                       r"\bwhat you(?:'|’)?ll (?:bring|need)\b|\bwhat you(?:'|’)?ve done\b|\bwhat we(?:'|’)?re looking for\b|"
                       r"\bwho you are\b|\bwh(?:o|at) we are looking for\b|\bwhat you need to be successful\b|\bminimum education and experience\b|\bskills required\b|\byou will be a great fit if\b|\bwho we(?:'|’)?re looking for\b|\bwhat you bring\b|\byou might thrive in this role if\b|\bwhat we need to see\b|\bwhat you need to (?:have|succeed)\b|\byou (?:have|bring|will have|should have)\s*:|\brequired education\b|"
                       r"\babout you\b|\byour (?:background|experience|skills)\s*:|"
                       r"\b(?:skills|experience|qualifications|requirements) you(?:'|’)?ll (?:need|bring|have)\b|\byou(?:'|’)?ll need to (?:bring|have)\b|"
                       r"\bwhat you(?:'|’)?ll need to (?:bring|have)\b", re.I)),
    ("pref", re.compile(r"(?-i:\b(?:Preferred|PREFERRED)(?=\s*[:;]))")),
    ("req", re.compile(r"(?-i:(?<!Travel )(?<!Physical )(?<!Authorization )(?<!Program )(?<!Work )"
                       r"\b(?:Qualifications|Requirements|QUALIFICATIONS|REQUIREMENTS)\b)")),
    # section ends: only at the start of a line / list item (after ; . : ! ? or the text start, at most one word such as
    # "Key" before), never mid-sentence ("work with the team" must not end a section)
    ("other", re.compile(r"(?:^|[;.:!?]\s*)(?:[A-Za-z]+ )?(?:responsibilities|what you(?:'|’)?ll do|what you will do|what you(?:'|’)?ll get to do|what you get to do|what you(?:'|’)?ll be doing|how you(?:'|’)?ll make an impact|how you will make an impact|key duties|essential (?:job )?duties|"
                         r"day[- ]to[- ]day|about (?:the|us|our)\b|the (?:role|team|opportunity)\s*[:;]|benefits|perks|"
                         r"what we offer|compensation|pay (?:range|transparency)|salary range|base pay|equal (?:employment )?opportunity|"
                         r"why (?:join|you'll love)|life at|our (?:team|mission|culture)|location\s*:|"
                         r"what you(?:'|’)?ll (?:learn|gain|get)|what we provide|why work|how to apply|apply for this job|"
                         r"additional information|job description|overview)\b", re.I)),
]


def qual_sections(text):
    """(required qualifications text, preferred qualifications text) found under the posting's own headings, or
    ('', '') when it has none. A section runs from its heading to the next heading of any kind."""
    marks = []
    for kind, rx in _HEADINGS:
        for m in rx.finditer(text or ""):
            if not any(m.start() < e and m.end() > b for b, e, _ in marks):  # overlapping headings: first pattern wins
                marks.append((m.start(), m.end(), kind))
    marks.sort()
    req, pref = [], []
    for i, (start, end, kind) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        part = text[end:stop]
        if kind == "req":
            req.append(part)
        elif kind == "pref":
            pref.append(part)
    return " ".join(req), " ".join(pref)


# the labels simplifysite writes ('... Requirements: ... . Preferred: ...'), only after a sentence break, so a posting's
# own "Program Requirements:" heading is not taken for one
_LABELS = re.compile(r"(?:^|(?<=[.;\]]) )(Requirements|Preferred|Responsibilities|Skills|Description): ")


def _move_plus(req, des):
    """Sentences / bullets inside the required text that call themselves optional ('... is a plus', 'nice to have',
    'bonus', 'a 'Why you might stand out' list) belong to the desired part."""
    keep, moved, stand_out = [], [], False
    for piece in re.split(r"(?<=[.;])\s+|\s*;\s*|\n+", req or ""):
        if re.search(r"why you might stand out|what (?:will )?(?:set|sets) you apart|\bto stand out\b", piece, re.I):
            stand_out = True
        if stand_out or re.search(r"\ba plus\b|\b(?:is|are|would be|will be)\s+an?\s+(?:big\s+|huge\s+)?(?:bonus|advantage)\b|\bnice[- ]to[- ]have\b|\bbonus points?\b", piece, re.I):
            moved.append(piece)
        else:
            keep.append(piece)
    return " ".join(keep), f"{des or ''} {' '.join(moved)}"


def split_text(desc):
    req, des = _split_text(desc)
    return _move_plus(req, des)


def _split_text(desc):
    """(required text, desired text) the match score reads: only the job's qualifications.
    - Simplify / company-page jobs are saved as 'Requirements: ... Preferred: ... [Responsibilities: ...] [Skills: ...]
      Description: <whole posting>': Simplify's two lists (pulled from the posting's Minimum / Preferred Qualifications)
      plus the qualification sections found under the posting's own headings. Responsibilities, the company blurb,
      benefits and the rest of the description do not count.
    - Other jobs (Google / LinkedIn: 'Required: ... Preferred: ...'): their qualification sections; when the text has
      no such headings at all, the whole text cut at the first 'preferred / desired' (the older short summaries)."""
    desc = desc or ""
    parts = {}
    labels = list(_LABELS.finditer(desc))
    for i, m in enumerate(labels):
        stop = labels[i + 1].start() if i + 1 < len(labels) else len(desc)
        parts.setdefault(m.group(1), desc[m.end():stop])
    if "Requirements" in parts or "Preferred" in parts:
        post_req, post_pref = qual_sections(parts.get("Description", ""))
        req_part, pref_part = parts.get("Requirements", ""), parts.get("Preferred", "")
        cut = _HEADINGS[0][1].search(req_part)  # a company's qualifications field often holds both lists
        if cut:
            req_part, pref_part = req_part[:cut.start()], f"{req_part[cut.end():]} {pref_part}"
        return f"{req_part} {post_req}", f"{pref_part} {post_pref}"
    post_req, post_pref = qual_sections(desc)
    if post_req.strip():  # a required-qualifications heading was found (a lone "nice to have" mid-summary is not enough)
        return post_req, post_pref
    head = re.match(r"\s*Required:\s*(.*?)(?:\s+Preferred:\s*(.*))?$", desc, re.S)
    if head:
        return head.group(1), head.group(2) or ""
    return _cut(desc)


def score(j, prof):
    """{'score': 0-100 or None (no keywords found), 'label', 'matched', 'missing', 'added' (matched only because the
    user marked them on the card), 'found', 'required': [have, of], 'desired': [have, of]} - matched / missing in the post's order (required part first)."""
    title, desc = j.get("title") or "", j.get("description") or ""
    req_text, des_text = split_text(desc)
    co = j.get("company") or ""
    required = keywords(title, prof, co) | keywords(req_text, prof, co)
    desired = keywords(des_text, prof, co) - required
    # Simplify's own skill list for the job (simplify.jobs page, when the job came from Simplify): each name is
    # mapped onto VOCAB; a name VOCAB doesn't know counts as its own keyword, matched literally in your resume text
    have = set(prof["skills"])
    qual_text = f"{req_text} {des_text}"
    for name in j.get("simplifySkills") or []:
        # Simplify tags skills from the whole posting: only those the qualifications themselves name count
        if not re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", qual_text, re.I) and not (terms(name) & terms(qual_text)):
            continue
        # a name only the 'plus / preferred' text mentions is desired, not required
        in_req = bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", req_text, re.I)) or bool(terms(name) & terms(req_text))
        canon = terms(name) or skillbank.find(name)
        if canon:
            required |= canon - desired if in_req else set()
            desired |= (canon - required) if not in_req else set()
        elif name and name not in required and name not in desired:
            (required if in_req else desired).add(name)
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", prof.get("text", ""), re.I):
                have.add(name)
    found = sorted(required) + sorted(desired)
    # keywords the user marked on this job's card as "I have this" (job-board.json addedSkills; tailor.py puts them
    # on this job's resume)
    added = [t for t in j.get("addedSkills") or [] if t in found and t not in have]
    have |= set(added)
    matched = [t for t in found if t in have]
    missing = [t for t in found if t not in have]
    if not found:
        return {"score": None, "label": "None", "matched": [], "missing": [], "added": [], "found": 0,
                "required": [0, 0], "desired": [0, 0]}
    # two separate scores (user 2026-11-09): BASIC = share of the post's required / minimum keywords on the resume (this is
    # the 'score' the board sorts by and the Schedule's auto-queue uses); STAND-OUT = share of its preferred / "is a plus"
    # keywords on the resume (prefScore, shown apart, never lowers the basic score). A post with no required keywords falls
    # back to all of its keywords for the basic score.
    explain = {t: _explain(t, have) for t in missing}
    similar = [t for t in missing if explain[t]["similar"]]
    base = required if required else set(found)
    base_have = len(base & have)
    base_sim = len([t for t in similar if t in base])
    value = round(100 * base_have / len(base))
    sim_value = round(100 * (base_have + base_sim) / len(base))
    label = "Strong" if value >= 80 else "Good" if value >= 65 else "Fair" if value >= 50 else "Low"
    pref_have = len(desired & have)
    pref_sim = len([t for t in similar if t in desired])
    pref_value = round(100 * pref_have / len(desired)) if desired and required else None
    pref_sim_value = round(100 * (pref_have + pref_sim) / len(desired)) if desired and required else None
    return {"score": value, "simScore": sim_value, "similar": similar, "label": label, "matched": matched,
            "prefScore": pref_value, "prefSimScore": pref_sim_value,
            "missing": missing, "added": added, "found": len(found),
            "required": [len(required & have), len(required)], "desired": [len(desired & have), len(desired)],
            "explain": explain,
            "requiredMissing": [t for t in missing if t in required]}


def _explain(keyword, have):
    """glossary.explain for VOCAB keywords; the skill bank's description / other names for the rest."""
    e = glossary.explain(keyword, have)
    if not e["what"]:
        b = skillbank.info(keyword)
        if b:
            e["what"] = b.get("what") or ""
            e["aka"] = e["aka"] or ", ".join(b.get("aka", [])[:4])
    return e


def main(argv):
    prof = profile()
    if "--profile" in argv:
        print("keywords found in resume.json:", ", ".join(sorted(prof["skills"])))
        return 0
    import jobboard  # same folder (lazy: jobboard imports this module)
    view = jobboard.view(jobboard.load_board())
    rows = sorted(view["jobs"], key=lambda j: -((j.get("match") or {}).get("score") or -1))
    for j in rows:
        m = j.get("match") or {}
        s = "  -" if m.get("score") is None else f"{m['score']:>3}"
        print(f"{s}% {len(m.get('matched', []))}/{m.get('found', 0):<2} | {j.get('company', '')[:24]:24} | "
              f"{j.get('title', '')[:48]:48} | +{', '.join(m.get('matched', [])[:6])} | -{', '.join(m.get('missing', [])[:5])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
