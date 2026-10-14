"""The application answer bank in ../private/.env as a question list - for the job board's "Update my answers" form.

Lists every APPLICANT_* contact value and ANSWER_* answer (with its '# Q:' question and '# --- section ---'), plus the
common application questions below that the bank does not cover yet, and saves what the user fills in. Secrets
(passwords, tokens, API keys) are never listed or written here. Questions left blank are asked during an application
and saved then (autoApplyLoop step 6d).

Usage (from the JobSearch folder):
  python answerbank.py missing           -> questions without an answer
  python answerbank.py set KEY "value"   -> set one APPLICANT_* / ANSWER_* value (a new catalog key is added with its question)
"""
import argparse
import re
import sys

import applog  # same folder: .env helpers

ENV, ENV_EXAMPLE = applog.ENV, applog.ENV_EXAMPLE
SECRET = re.compile(r"PASSWORD|TOKEN|API_KEY|SECRET|^BRIDGE_PORT$")


def is_secret(key):
    """Passwords, tokens and keys outside the ANSWER_* bank (ANSWER_ keys are form answers, e.g. about clearances)."""
    return not key.startswith("ANSWER_") and bool(SECRET.search(key))

CONTACT = [  # APPLICANT_* keys shown in the form (label, key)
    ("Email", "APPLICANT_EMAIL"),
    ("Phone", "APPLICANT_PHONE"),
    ("Street address", "APPLICANT_STREET"),
    ("City, State ZIP", "APPLICANT_CITY_STATE_ZIP"),
    ("LinkedIn URL", "APPLICANT_LINKEDIN_URL"),
    ("Portfolio / personal website URL", "APPLICANT_PORTFOLIO_URL"),
]

# Common application questions: (section, question, key). Shown when the bank has no entry with that key or question.
CATALOG = [
    ("contact", "Legal first name", "ANSWER_CONTACT_LEGAL_FIRST_NAME"),
    ("contact", "Middle name", "ANSWER_CONTACT_MIDDLE_NAME"),
    ("contact", "Legal last name", "ANSWER_CONTACT_LEGAL_LAST_NAME"),
    ("contact", "Preferred first name", "ANSWER_CONTACT_PREFERRED_FIRST_NAME"),
    ("contact", "Pronouns", "ANSWER_CONTACT_PRONOUNS"),
    ("contact", "GitHub URL", "ANSWER_CONTACT_GITHUB_URL"),
    ("contact", "County of residence", "ANSWER_CONTACT_COUNTY"),
    ("eligibility", "Legally authorized to work in US", "ANSWER_ELIG_LEGALLY_AUTHORIZED_TO_WORK_IN_US"),
    ("eligibility", "Require sponsorship now or in future", "ANSWER_ELIG_REQUIRE_SPONSORSHIP_NOW_OR_IN_FUTURE"),
    ("eligibility", "Security clearance", "ANSWER_ELIG_SECURITY_CLEARANCE"),
    ("eligibility", "Willing to obtain a security clearance", "ANSWER_ELIG_WILLING_TO_OBTAIN_CLEARANCE"),
    ("eligibility", "Willing to relocate", "ANSWER_ELIG_WILLING_TO_RELOCATE"),
    ("eligibility", "Hybrid/onsite", "ANSWER_ELIG_HYBRID_ONSITE"),
    ("eligibility", "Valid driver's license", "ANSWER_ELIG_DRIVERS_LICENSE"),
    ("eligibility", "Willing to undergo a background check", "ANSWER_ELIG_BACKGROUND_CHECK"),
    ("eligibility", "Willing to undergo a drug test", "ANSWER_ELIG_DRUG_TEST"),
    ("eligibility", "Previously applied to this company", "ANSWER_ELIG_PREVIOUSLY_APPLIED"),
    ("compensation", "Desired salary", "ANSWER_COMP_DESIRED_SALARY"),
    ("compensation", "Desired hourly rate (contract / hourly roles)", "ANSWER_COMP_DESIRED_HOURLY_RATE"),
    ("compensation", "Current or most recent base salary (leave blank to decline)", "ANSWER_COMP_CURRENT_SALARY"),
    ("compensation", "Open to contract roles", "ANSWER_COMP_OPEN_TO_CONTRACT"),
    ("compensation", "Open to part-time roles", "ANSWER_COMP_OPEN_TO_PART_TIME"),
    ("availability", "Start date / earliest start", "ANSWER_AVAIL_START_DATE"),
    ("availability", "Notice period at current job", "ANSWER_AVAIL_NOTICE_PERIOD"),
    ("availability", "Willing to work evenings / weekends / on-call", "ANSWER_AVAIL_EVENINGS_WEEKENDS_ONCALL"),
    ("availability", "Preferred time zone(s) for remote work", "ANSWER_AVAIL_TIME_ZONE"),
    ("education", "Degree", "ANSWER_EDU_DEGREE"),
    ("education", "Graduation month/year", "ANSWER_EDU_GRADUATION_DATE"),
    ("education", "GPA", "ANSWER_EDU_GPA"),
    ("education", "Honors / awards / scholarships", "ANSWER_EDU_HONORS"),
    ("education", "Relevant coursework", "ANSWER_EDU_COURSEWORK"),
    ("education", "Certifications (name, issuer, year)", "ANSWER_EDU_CERTIFICATIONS"),
    ("experience", "Total professional experience", "ANSWER_EXP_TOTAL_PROFESSIONAL_EXPERIENCE"),
    ("experience", "Years of experience with C# / .NET", "ANSWER_EXP_YEARS_CSHARP"),
    ("experience", "Years of experience with JavaScript / TypeScript", "ANSWER_EXP_YEARS_JS_TS"),
    ("experience", "Years of experience with React", "ANSWER_EXP_YEARS_REACT"),
    ("experience", "Years of experience with Python", "ANSWER_EXP_YEARS_PYTHON"),
    ("experience", "Years of experience with Java", "ANSWER_EXP_YEARS_JAVA"),
    ("experience", "Years of experience with SQL / databases", "ANSWER_EXP_YEARS_SQL"),
    ("experience", "Years of experience with AWS / Azure / GCP", "ANSWER_EXP_YEARS_CLOUD"),
    ("experience", "People-management experience", "ANSWER_EXP_MANAGEMENT"),
    ("experience", "Internships (company, role, dates)", "ANSWER_EXP_INTERNSHIPS"),
    ("skills", "Programming languages (strongest first)", "ANSWER_SKILLS_PROGRAMMING_LANGUAGES"),
    ("skills", "Frameworks and tools", "ANSWER_SKILLS_FRAMEWORKS_TOOLS"),
    ("skills", "Spoken languages and proficiency", "ANSWER_SKILLS_SPOKEN_LANGUAGES"),
    ("voluntarySelfId", "Gender", "ANSWER_SELFID_GENDER"),
    ("voluntarySelfId", "Hispanic/Latino", "ANSWER_SELFID_HISPANIC_LATINO"),
    ("voluntarySelfId", "Race", "ANSWER_SELFID_RACE"),
    ("voluntarySelfId", "Veteran", "ANSWER_SELFID_VETERAN"),
    ("voluntarySelfId", "Disability", "ANSWER_SELFID_DISABILITY"),
    ("voluntarySelfId", "Sexual orientation (or 'prefer not to say')", "ANSWER_SELFID_SEXUAL_ORIENTATION"),
    ("openEndedTemplate", "Tell us about yourself (2-3 sentences)", "ANSWER_OPEN_ABOUT_ME"),
    ("openEndedTemplate", "Greatest strength", "ANSWER_OPEN_STRENGTH"),
    ("openEndedTemplate", "An area you are working to improve", "ANSWER_OPEN_IMPROVEMENT_AREA"),
    ("openEndedTemplate", "A challenging project you are proud of", "ANSWER_OPEN_PROUD_PROJECT"),
    ("openEndedTemplate", "Why are you looking for a new role?", "ANSWER_OPEN_WHY_LEAVING"),
    ("openEndedTemplate", "Career goals (next 3-5 years)", "ANSWER_OPEN_CAREER_GOALS"),
    ("miscFormFields", "How did you hear about us", "ANSWER_MISC_HOW_DID_YOU_HEAR_ABOUT_US"),
    ("miscFormFields", "Referral name (if someone referred you)", "ANSWER_MISC_REFERRAL_NAME"),
    ("miscFormFields", "References (name, relationship, contact) or 'available on request'", "ANSWER_MISC_REFERENCES"),
]


def parse():
    """(applicant: KEY -> value, answers: [{key, section, question, value}] in file order)."""
    applicant, answers, section, question = {}, [], "", ""
    for line in ENV.read_text(encoding="utf-8").splitlines():
        m = re.match(r"# --- (.+?) ---$", line)
        if m:
            section = m.group(1)
            continue
        if line.startswith("# Q:"):
            question = line[4:].strip()
            continue
        m = re.match(r"([A-Z0-9_]+)=(.*)$", line)
        if not m:
            continue
        key, v = m.group(1), m.group(2).strip()
        if len(v) >= 2 and v[0] in "\"'`" and v[-1] == v[0]:
            v = v[1:-1]
        if key.startswith("ANSWER_"):
            answers.append({"key": key, "section": section, "question": question or key, "value": v})
        elif not is_secret(key):
            applicant[key] = v
        question = ""
    return applicant, answers


def questions():
    """Every question for the form: contact values, the bank's answers, then catalog questions not in the bank."""
    applicant, answers = parse()
    out = [{"key": k, "section": "contact details", "question": label, "value": applicant.get(k, ""), "kind": "contact"}
           for label, k in CONTACT]
    out += [{**a, "kind": "answer"} for a in answers]
    have_keys = {a["key"] for a in answers}
    have_q = {a["question"].lower() for a in answers}
    out += [{"key": k, "section": s, "question": q, "value": "", "kind": "new"}
            for s, q, k in CATALOG if k not in have_keys and q.lower() not in have_q]
    for q in out:
        q["filled"] = bool(q["value"].strip())
    return out


def clean(value):
    return re.sub(r"\s*[\r\n]+\s*", " / ", str(value)).strip()


def set_applicant(key, value):
    raw = ENV.read_bytes().decode("utf-8")
    eol = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        j = next((i + 1 for i, l in enumerate(lines) if l.startswith("APPLICANT_")), len(lines))
        while j < len(lines) and lines[j].startswith("APPLICANT_"):
            j += 1
        lines.insert(j, f"{key}={value}")
    ENV.write_bytes((eol.join(lines) + eol).encode("utf-8"))


def save(values):
    """Save {KEY: value} from the form. Returns the number of values changed. Unknown or secret keys are ignored;
    blank new questions are not added (they stay on the list)."""
    known = {q["key"]: q for q in questions()}
    changed = 0
    for key, value in values.items():
        q = known.get(key)
        if q is None or is_secret(key):
            continue
        value = clean(value)
        if value == q["value"]:
            continue
        if q["kind"] == "contact":
            set_applicant(key, value)
        elif q["kind"] == "new" and not value:
            continue
        else:
            applog.upsert_answer(ENV, q["section"], q["question"], key, applog.env_quote(value))
            if q["kind"] == "new" and ENV_EXAMPLE.exists():
                applog.upsert_answer(ENV_EXAMPLE, q["section"], q["question"], key, '""')
        changed += 1
    return changed


def main():
    for stream in (sys.stdout, sys.stderr):
        if stream:
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("missing")
    sp = sub.add_parser("set")
    sp.add_argument("key")
    sp.add_argument("value")
    a = ap.parse_args()
    if a.cmd == "missing":
        miss = [q for q in questions() if not q["filled"]]
        for q in miss:
            print(f"[{q['section']}] {q['question']}  ({q['key']})")
        print(f"{len(miss)} questions without an answer")
    elif a.cmd == "set":
        if is_secret(a.key):
            sys.exit("secrets are not set through answerbank.py")
        n = save({a.key: a.value})
        print(f"{a.key}: {'saved' if n else 'unchanged or unknown key'}")


if __name__ == "__main__":
    main()
