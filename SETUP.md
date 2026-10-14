# First-time setup (guided by Claude)

**For the user:** open a terminal in the folder you cloned this repo into, run `claude`, and say **"set me up"**. Claude reads this file, asks for your resume, fills in your private files from it, and asks you only what the resume can't answer. It takes about 15 minutes.

**For Claude:** follow the steps below in order. `<repo>` = the repo folder (the one containing this file). Everything you create goes into `<repo>\private\` (gitignored) - never write personal data anywhere else, and never commit it.

Ground rules:
- Never invent facts. Anything the resume doesn't say and the user didn't answer stays empty; list it at the end.
- Ask in small batches (3-4 questions at a time, multiple choice where possible), not one long questionnaire.
- Self-identification questions (gender, race, veteran, disability, ...) are optional - always offer "Prefer not to answer" / "I don't wish to answer".
- Never ask for, type or store passwords. Never put real values into `examples\`.

## 1. Create the private folder

If `<repo>\private\` already has files, ask before overwriting anything. Otherwise:
```powershell
mkdir <repo>\private
copy <repo>\examples\.env.example                   <repo>\private\.env
copy <repo>\examples\job-search-config.example.json <repo>\private\job-search-config.json
copy <repo>\examples\resume.example.json            <repo>\private\resume.json
```
`private\applications.json` is created automatically by `applog.py` (step 7).

## 2. Get the resume

Ask the user for the full path of their resume (PDF preferred; DOCX is fine to read, but a PDF is needed for uploading - ask them to export one if they only have DOCX).
- Read it completely.
- Copy the PDF into `<repo>\private\` **keeping its file name** (employers see the file name, e.g. `FirstLastResume.pdf`).

## 3. Fill `private\resume.json` from the resume

Use the structure of `examples\resume.example.json`: `summary`, every job in `experience` (title, company, location, `from` / `to` as MM/YYYY, `current: true` + `to: null` for the current job, and **all** bullets word for word), `education` (school, location, degree, field, from/to, GPA if listed), `skills` (grouped as in the template). Set `source` to `<pdf file name> (updated <today>)`. Show the user a short summary (jobs + dates, degree) and ask them to confirm or correct.

## 4. Fill `private\.env`

Keep the file's format: one `# Q: <question>` comment line above each `KEY="value"` line; use `'...'` if a value contains double quotes. Leave `BRIDGE_TOKEN` / `BRIDGE_PORT` alone (step 8 fills them).

**From the resume (then confirm with the user):**
| Key | Value |
|---|---|
| `APPLICANT_EMAIL`, `APPLICANT_PHONE`, `APPLICANT_LINKEDIN_URL`, `APPLICANT_PORTFOLIO_URL` | contact block of the resume |
| `APPLICANT_STREET`, `APPLICANT_CITY_STATE_ZIP` | address if listed (else ask) |
| `ANSWER_CONTACT_NAME` | full legal name |
| `ANSWER_EDU_DEGREE`, `ANSWER_MISC_HIGHEST_EDUCATION`, `ANSWER_STD_HIGHEST_DEGREE` | degree, school, dates, GPA |
| `ANSWER_STD2_SCHOOL_DROPDOWN`, `ANSWER_STD2_GRADUATION_DATE_EXACT_DAY_UNKNOWN` | official school name; graduation date as MM/01/YYYY |
| `ANSWER_EXP_CURRENT`, `ANSWER_EXP_PREVIOUS`, `ANSWER_STD_CURRENT_COMPANY` | "Title, Company, City ST, Mon YYYY - Present" |
| `ANSWER_EXP_TOTAL_PROFESSIONAL_EXPERIENCE`, `ANSWER_MISC_YEARS_OF_RELEVANT_EXPERIENCE` | total years of full-time work (excluding internships unless the user says otherwise) |
| `ANSWER_STD_PROFESSIONAL_LANGUAGES` | languages / frameworks used at work |
| `ANSWER_LOC_WEBSITE_PORTFOLIO` | `${APPLICANT_PORTFOLIO_URL}` |
| `ANSWER_RESUME_CURRENT_RESUME` | full path of the PDF in `private\` |
| `SKIP_COMPANIES` | **suggest** the current and past employers on the resume (comma-separated); the user confirms or edits |

**Ask the user** (batch these):
1. Preferred first name (if different), prefix (Mr. / Ms. / Mx. / none).
2. Work authorization: legally authorized to work in the US? Need sponsorship now or later? Citizenship status / "U.S. person"? Over 18?
3. Security clearance: current clearance (if any); OK with roles that require obtaining one; any agencies / contract types to avoid.
4. Compensation: desired salary or range (and whether "midpoint of the posted range" is fine); OK with below-median pay for a good fit?
5. Work style: remote / hybrid / onsite; willing to relocate (which states); travel %; days available; notice period / earliest start date.
6. Standard yes/no questions (suggest the usual answer, let them change it): previously employed by the company, employee referral, relatives at the company, non-compete / NDA limits, government employee before, SMS consent, how did you hear about us (default "LinkedIn").
7. Voluntary self-ID (optional - offer "Prefer not to answer"): gender, Hispanic/Latino, race, veteran status, disability, transgender.
8. Open-ended template: what to say for "Why this company?" (strengths, what they want to learn, interests) - draft one from the resume and let them edit.

Fill the matching `ANSWER_*` keys. For `ANSWER_SIGNIN_*` keep the policy text ("the user enters passwords / verification codes"); never a password. Keys that are specific to someone else's past applications (e.g. a single company's essay) can stay empty. When a question has no key yet, add one with: `python applog.py answer --section <section> --question "<q>" --answer "<a>"` (run in `<repo>\JobSearch`).

## 5. Fill `private\job-search-config.json`

Start from the copied template and keep its structure, `loop`, and all `autoApplyLoop` steps.
- `roles`: from the resume's titles and skills, plus what the user wants next - confirm the list.
- `seniority` and `experienceYears` (`max` = the most years a posting may require for an automatic application, `stretchAt` = from here on skip or ask): from the user's total experience; confirm.
- `postedWithinHours` (how fresh posts must be) and `loop.intervalHours` (how often to search): suggest 24 and 2.
- `locations`: one entry per area the user wants, with `workModes`. For LinkedIn, open `https://www.linkedin.com/jobs/search/?location=<city>` in the browser and take the `geoId` from the resulting URL; if that's not possible, use `"linkedinKeyword": "in <City> <State>"` instead. Add `{"name": "Remote US", "workModes": ["remote"]}` if they want remote.
- `minSalary`: from step 4.
- `skip.rules`: keep the generic rules; add the user's deal-breakers (clearance, industries, agencies, contract-only, ...). `skip.companies` stays `["${SKIP_COMPANIES}"]`.
- `sources.careerSites`: ask which company career sites they want checked besides LinkedIn.
- `jobFeed` (the job APIs that replace LinkedIn browsing as the main source, used by `python jobfeed.py`):
  - `locationInclude`: one case-insensitive regex per area, listing the cities, state names and state codes of `locations`, e.g. `\b(Austin|TX|Texas)\b`. Remote US jobs pass automatically.
  - `ats.greenhouse` / `ats.lever` / `ats.ashby`: board slugs of companies the user wants watched. The slug is the name in the company's job-board URL: `job-boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`, `jobs.ashbyhq.com/<slug>`. Verify with `python jobfeed.py --check-boards`.
  - `jsearch.queries` / `adzuna.queries` / `adzuna.where`: search phrases built from `roles` + `seniority`, and the user's areas.
  - API keys (optional, free tiers): JSearch - sign up on rapidapi.com, subscribe to the JSearch API, put the key in `JSEARCH_API_KEY`. If the key comes from app.openwebninja.com instead, set `jsearch.endpoint` to `"openwebninja"`. Adzuna - register at developer.adzuna.com and put the app id and key in `ADZUNA_APP_ID` / `ADZUNA_APP_KEY`. Set each `monthlyRequestBudget` to the plan's monthly quota. Without keys, only the company boards (`ats`) are used.
  - Check: `python jobfeed.py --plan`, then `python jobfeed.py`.
- `launcher.chromePath`: check the file exists (`C:\Program Files\Google\Chrome\Application\chrome.exe` or `%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe`). `launcher.claudePermissionMode`: explain `"auto"` (fewer prompts) vs `""` (ask for every action) and let them choose.
- `autoApplyLoop` step 4: replace `..\private\Resume.pdf` with the real PDF path from step 2.
- `profile`: fill each note from the answers above (short sentences).
- `standingRules`: keep; add any standing rule the user states.
- Remove the top-level `_about` (it only describes the template). Validate: `python -c "import json; json.load(open(r'<repo>\private\job-search-config.json', encoding='utf-8'))"`.

## 6. Ask for everything that is still missing

The resume never covers everything. Find the gaps and fill them with the user before going on:
```powershell
cd <repo>\JobSearch
python applog.py missing
```
It lists, from `private\.env`, `private\job-search-config.json` and `private\resume.json`:
- **REQUIRED** - must be filled before the job search can start: contact info, legal name, work authorization / sponsorship, every `${...}` placeholder the config uses, real locations (no `<placeholder>`s), the resume PDF named in `autoApplyLoop` step 4, `BRIDGE_TOKEN` (comes in step 8 - skip it here).
- **Optional / unconfirmed** - empty answer-bank entries, empty `profile` notes, empty resume fields, and values marked "assumed" / "confirm" / "ask again".

Then go through the list **one small batch at a time** (up to 4 questions per round, multiple choice where it makes sense, always allowing a free-text answer):
1. All REQUIRED items first, then the optional ones in file order (`.env` → config → `resume.json`).
2. Word each question for the user (e.g. "When could you start a new job?"), not as the key name. Offer "Skip for now" on optional items and "Prefer not to answer" on self-identification items.
3. Write each answer right away to the right place: `.env` answers with `python applog.py answer --section <section> --question "<q>" --answer "<a>"` (updates the existing key), config / resume fields by editing the JSON (keep it valid).
4. Re-run `python applog.py missing` after each batch and continue until REQUIRED shows "all set" (apart from `BRIDGE_TOKEN`) and the user has answered or skipped every optional item.

Skipped optional items are fine: Claude asks again the first time an application form needs them.

## 7. Create the application log
```powershell
cd <repo>\JobSearch
python applog.py open
```
This creates `private\applications.json` (empty) from the template.

## 8. Browser bridge and launcher

Follow `claude-browser-bridge\START_HERE.md`. Claude can run the commands; the user must do the Chrome parts:
1. Claude: `cd <repo>\claude-browser-bridge\server`, `npm install`, `npm run build`, `npm run token` (writes `BRIDGE_TOKEN` into `private\.env`).
2. User: `chrome://extensions` → Developer mode → **Load unpacked** → `<repo>\claude-browser-bridge\extension`; click the extension icon → paste the token → **Save & connect**; add the job sites they use to *Allowed sites* (LinkedIn, greenhouse.io, ashbyhq.com, lever.co, myworkdayjobs.com, ...).
3. Claude: `claude mcp add --scope user browser-bridge -- node <repo>\claude-browser-bridge\server\dist\index.js` (skip if `claude mcp list` already shows `browser-bridge`).
4. Claude: run `<repo>\launcher\build.cmd`, then `<repo>\StartJobSearch.exe --dry-run > %TEMP%\dry.txt` and check it reports the config, the `.env` values and no missing `${NAME}` placeholders. Fix anything it complains about.

The user must restart Claude Code once (or double-click `StartJobSearch.exe`) so the `browser-bridge` tools load.

## 9. Finish

Run `python applog.py missing` one last time - it must print "REQUIRED: all set" (exit code 0). Then tell the user:
- what was filled from the resume and what they answered,
- which keys / fields are still empty (they can fill them later, or Claude asks the first time a form needs them),
- how to start: double-click `StartJobSearch.exe`, or `cd <repo>\JobSearch`, `claude`, "continue my job search",
- that everything personal is in `private\` and is never committed.
