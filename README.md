# ApplyPilot

An AI job application agent. Every day it finds new US tech jobs across thousands of company career sites,
picks the best matches for one candidate, and, for the jobs the candidate chooses, writes a tailored one-page
resume and cover letter and fills in the application form in a visible browser. **The candidate always reviews
and clicks Submit**; ApplyPilot never submits anything.

Built in Python with the Claude API (Anthropic), Playwright, SQLite and the Gmail API.

## How it works

```
inbox → search → free checks → Haiku screen → Sonnet review → full postings → shortlists
                                                                                   ↓
                                          you choose → research + tailor → fill the form → you submit
```

1. **Search** (every day). Reads the public job feeds of company career sites: ~1,250 Greenhouse, Lever and
   Ashby boards, 1,300+ Workday sites, SmartRecruiters, Workable and Oracle Recruiting sites, and big employers'
   own sites (Amazon, Netflix). New sites are discovered from community new-grad and internship lists,
   Hacker News "Who is hiring" and job-alert emails.
2. **Pick** (the funnel). Free code checks first (age, US, seniority in the title, field), then a lenient
   first read by Claude Haiku and a careful read by Claude Sonnet, both through the half-price Message Batches
   API. Jobs are scored out of 100: skills fit 35, entry-level fit 20, posted within 24 hours 20, company
   quality and growth 15, interview odds 10. Promising jobs without a full posting are traced to the
   employer's own page (Claude web search, verified) and read in full before they can be shortlisted.
   Two shortlists: full-time, and internships & part-time.
3. **Tailor** (only for jobs the candidate picks). Company research by web search (sourced, recent facts),
   then a resume and cover letter from the candidate's master record:
   - Claude answers in a constrained, multiple-choice format: every bullet cites real lines of the master
     record and skills come from the candidate's own list, so invented skills can't be expressed at all.
   - Code then checks every number and technology against the cited lines, job titles against roles
     actually held, and the letter's facts against the research. Fixes come back as small patches.
   - One-page ATS-friendly PDFs (HTML + Jinja2, printed with Chromium).
4. **Apply.** A label-based form filler for Greenhouse, Lever and Ashby (and generic forms). A guard injected
   into the page blocks every form submission while it fills. Sites with CAPTCHAs or sign-ups open in the
   user's own Chrome with an answer sheet instead; ApplyPilot never tries to get past bot checks.
5. **Track and learn.** SQLite records with an Excel view; employer replies are read from Gmail (read-only)
   and update each application's status; `insights` shows which kinds of applications lead to interviews.

## Engineering notes

- **LLM writes, code verifies.** Structured outputs (Pydantic schemas) for every Claude call; citations,
  numbers, technologies and links are checked in code, and failures are sent back as specific feedback.
- **Cost layering.** Free checks → a small model → a large model → expensive work only for chosen jobs.
  Prompt caching, the Batch API and a per-step cost meter keep spend at roughly $1.50-2.50 a day.
- **Evaluation.** `eval-label` builds a labelled set of real jobs; `eval` measures which good jobs the funnel
  would drop and which bad ones it would keep. Used to tune the rules (good-job recall 74% → 89%).
- **Human in the loop.** The candidate chooses every application and clicks every Submit.
- 179 automated tests (pytest), including browser tests against local forms.

## Setup

```bash
uv sync
uv run playwright install chromium
cp .env.example .env                      # add your ANTHROPIC_API_KEY
mkdir -p data && cp examples/*.yaml data/  # then replace with your own details
uv run python -m jobagent check
uv run python -m jobagent gmail-login     # optional: job alerts and employer replies (Gmail, read-only)
```

Gmail access needs a Google Cloud project with the Gmail API enabled and an OAuth "Desktop app" client saved as
`data/gmail_credentials.json`.

## Daily use

```bash
uv run python -m jobagent prepare   # ~30-90 min, unattended: inbox -> search -> score -> summary
uv run python -m jobagent apply     # choose from the shortlists; chosen jobs get a resume, letter and filled form
```

While applying: `y` submitted, `s` skip, `r` later, `f` fill the next page of a multi-page form, `o` open the
job in your own Chrome.

Other commands: `schedule --at 07:30` (daily prepare via macOS launchd), `inbox`, `insights`, `eval-label`,
`eval`, `status <id> <status>`, `rerender`, `tailor`, `check`. Run any with `--help`.

## Your data stays local

`data/` (profile, master resume, answers, Gmail token, database), `output/` (resumes and letters),
`tracker.xlsx`, `logs/`, `browser_profile/` and `.env` are git-ignored and never leave your machine except
for the Claude API calls that read job postings and write documents.

## Credits

Resume font: Source Sans 3 by Adobe, SIL Open Font License 1.1 (`templates/fonts/LICENSE.md`).
Job sources: public job-board feeds and the community lists maintained by SimplifyJobs.
