"""Resume keyword match score for each job on the board - plain rules, no AI / no tokens.

Score = how many of the job's qualification keywords your resume has:
    matched keywords / keywords found in the post  (shown as e.g. "78% - 7/9 keywords")
Keywords are the tech terms of the VOCAB list below (about 200, specific: Lambda / S3 / DynamoDB are separate from
AWS) found in the job title + description - the required qualifications and the desired / preferred qualifications
alike, so more matches = higher score - plus Simplify's own skill list for jobs that came from Simplify
(job["simplifySkills"], read from the simplify.jobs page). Nothing else counts: experience, level, location and pay
stay on the card for you to judge.

Your keywords come from ../private/resume.json (skills lists, job bullets, projects). Add a term to VOCAB to have it
recognised in both your resume and job posts.

  python matchscore.py            -> every board job's score, best first
  python matchscore.py --profile  -> the keywords read from your resume
"""
import functools
import json
import re
import sys

import applog  # same folder: paths
import jobinfo  # same folder: where a post's "preferred / nice to have" part starts

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
    "Flask": r"flask", "FastAPI": r"fastapi", "Express": r"express(?:\.js|js)?", "NestJS": r"nest\.?js",
    "Laravel": r"laravel", "Hangfire": r"hangfire",
    # frontend / web / mobile
    "React": r"react(?:\.js|js)?(?!\s*native)", "React Native": r"react native", "Angular": r"angular(?:js)?",
    "Vue": r"vue(?:\.js)?", "Next.js": r"next\.js|nextjs", "Node.js": r"node(?:\.js|js)?", "Svelte": r"svelte",
    "Redux": r"redux", "HTML": r"html5?", "CSS": r"css3?|sass|scss|less css", "Tailwind": r"tailwind(?:\s*css)?",
    "Bootstrap": r"bootstrap", "jQuery": r"jquery", "Webpack/Vite": r"webpack|vite|babel", "Leaflet/Maps": r"leaflet|mapbox|gis",
    "Accessibility": r"accessibility|a11y|wcag", "Responsive design": r"responsive (?:design|web|ui)",
    "iOS": r"(?-i:iOS)", "Android": r"android", "Mobile apps": r"mobile app(?:lication)?s?|mobile development",
    "Chrome extensions": r"chrome extensions?|browser extensions?",
    # APIs / architecture
    "REST APIs": r"rest(?:ful)?(?:\s*apis?|\s*services)?|web\s*apis?", "GraphQL": r"graphql", "gRPC": r"grpc",
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
    "Authentication/authorization": r"authentication|authorization|oauth\s*2?|openid|(?-i:SSO)|single sign[- ]on|jwt|(?-i:IAM)|identity and access",
    "Vulnerability management": r"vulnerabilit(?:y|ies)(?:\s*scanning|\s*management|\s*remediation)?|penetration testing|owasp",
    "Privacy/compliance": r"privacy|gdpr|ccpa|hipaa|soc\s*2|fedramp|pci(?:[- ]dss)?|compliance",
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
           "MySQL": {"SQL"}, "PostgreSQL": {"SQL"}, "SQL Server": {"SQL"}, "TypeScript": {"JavaScript"}}

_B = r"(?<![A-Za-z0-9+#./-])(?:{})(?![A-Za-z0-9+#])"
_RX = {name: re.compile(_B.format(rx), re.I) for name, rx in VOCAB.items()}


def terms(text):
    """Vocabulary keywords found in a text (a copy of the cached result)."""
    return set(_terms(text or ""))


@functools.lru_cache(maxsize=8192)
def _terms(text):
    # cached per text: a job's title / description don't change between page refreshes, so only new jobs are scanned
    return frozenset(name for name, rx in _RX.items() if rx.search(text))


def profile(resume=None):
    """Your keywords: vocabulary terms in resume.json (skills lists, job + project bullets, summary)."""
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
    for t in list(skills):
        skills |= IMPLIES.get(t, set())
    text = "\n".join(map(str, listed + parts))  # for keywords outside VOCAB (e.g. from Simplify's skill list)
    return {"skills": skills, "text": text}


def score(j, prof):
    """{'score': 0-100 or None (no keywords found), 'label', 'matched', 'missing', 'added' (matched only because the
    user marked them on the card), 'found', 'required': [have, of], 'desired': [have, of]} - matched / missing in the post's order (required part first)."""
    title, desc = j.get("title") or "", j.get("description") or ""
    m = jobinfo._PREFERRED.search(desc)
    req_text, des_text = (desc[:m.start()], desc[m.start():]) if m else (desc, "")
    required = terms(title) | terms(req_text)
    desired = terms(des_text) - required
    # Simplify's own skill list for the job (simplify.jobs page, when the job came from Simplify): each name is
    # mapped onto VOCAB; a name VOCAB doesn't know counts as its own keyword, matched literally in your resume text
    have = set(prof["skills"])
    for name in j.get("simplifySkills") or []:
        canon = terms(name)
        if canon:
            required |= canon - desired
        elif name and name not in required:
            required.add(name)
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
    value = round(100 * len(matched) / len(found))
    label = "Strong" if value >= 80 else "Good" if value >= 65 else "Fair" if value >= 50 else "Low"
    return {"score": value, "label": label, "matched": matched, "missing": missing, "added": added, "found": len(found),
            "required": [len(required & have), len(required)], "desired": [len(desired & have), len(desired)]}


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
