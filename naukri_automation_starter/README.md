# Naukri Automation — local-first staged MVP

This is a **local-first starter, not yet a verified end-to-end Naukri automation**. The implemented and testable next feature is deterministic resume-to-job matching: import a CSV of job listings, score them against your PDF resume, and view a ranked list. It uses no AI API and makes no browser submissions.

The Naukri-specific profile-refresh action and application-form interactions must be inspected and tested against the current logged-in website before being implemented. This project does not bypass CAPTCHA, OTP, login protections, or other access controls.

## 1. Requirements (Windows 11)
- Python 3.11 or newer
- Google Chrome installed (Playwright Chromium is also supported)
- A PDF resume

Open **PowerShell** and run these commands from the folder where you extracted this project:

```powershell
py --version
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env
New-Item -ItemType Directory -Force data | Out-Null
```

If PowerShell blocks activation for this terminal session, run:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

Put your resume PDF at `data\resume.pdf`. Alternatively, edit `RESUME_PATH` in `.env`. Keep `.env`, your resume, `.browser-profile`, database, and logs private; they are gitignored. Do **not** put your Naukri password in `.env`.

## 2. Verify setup

```powershell
python -m naukri_automation.cli setup-check
python -m pytest -q
```

`setup-check` should report the resume text length. If it says the resume is missing, place your PDF at `data\resume.pdf` or update `RESUME_PATH` in `.env`.

## 3. Implemented feature: resume-match local job listings

The CSV needs `title` and `url`; `company`, `location`, and `description` are optional. Start with the included sample:

```powershell
python -m naukri_automation.cli import-jobs sample_jobs.csv
python -m naukri_automation.cli rank-jobs
python -m naukri_automation.cli jobs
```

To use listings you collected yourself, create a CSV such as:

```csv
title,company,location,url,description
Python Backend Engineer,Example Co,India,https://example.com/job/123,"Python SQL REST API backend"
```

Then run `python -m naukri_automation.cli import-jobs .\data\my_jobs.csv` followed by `python -m naukri_automation.cli rank-jobs`.

The legacy `rank-jobs` score is a transparent keyword-overlap indicator, **not** a probability of getting hired. This phase does not scrape Naukri listings; import only job information you are permitted to collect.

### Local job matching engine v1

Prepare a user-supplied CSV with a `title` column; `company`, `location`, `description`, `url`/`job_url`, `experience`/`experience_requirement`, and `salary` are supported and optional. Blank values remain blank. Rows without a title or with an invalid supplied URL are skipped; no URLs, salaries, skills, or qualifications are inferred. Duplicate handling uses a normalized HTTP(S) URL (tracking parameters and fragments removed) when available. Without a URL, it uses normalized title/company/location plus a SHA-256 of normalized description; generic incomplete rows can therefore collide, so inspect imported data.

```powershell
# Import only your supplied CSV; no web page is visited.
python -m naukri_automation.cli import-jobs .\data\my_jobs.csv

# Evaluate imported listings against the local canonical resume and save a ranked evaluation history.
python -m naukri_automation.cli match-jobs

# Or evaluate a manually supplied UTF-8 description file, with optional supplied metadata.
python -m naukri_automation.cli evaluate-job .\data\one_job.txt --title "Backend Developer" --company "Example" --experience "3+ years"

# Export each listing's latest evaluation; blank URLs remain blank.
python -m naukri_automation.cli export-shortlist --output .\data\shortlist.csv
```

Scoring is deterministic and explainable: target-role title alignment, overlap with recognized skills explicitly present in the resume, and numeric experience compatibility when both texts state durations. Defaults are weights 25/55/20, which must total 100; explicit numeric experience gaps receive a 20-point penalty. Configure `MATCH_ROLE_WEIGHT`, `MATCH_SKILL_WEIGHT`, `MATCH_EXPERIENCE_WEIGHT`, and `MATCH_MISMATCH_PENALTY` in `.env`. Only explicit catalogued skill mentions count. Missing skills mean “not found in resume text” and remain uncertain; they do not prove the candidate lacks them. Experience is compared only when a numeric duration is found on both sides. Target roles use exact token alignment against the configured titles. These heuristics are not validated hiring predictions or a probability of getting hired.

Listings are deduplicated in SQLite, and every evaluation is appended to `job_match_evaluations` with an input snapshot so later imports do not erase past scores. Resume text is read locally for evaluation and is not written into job or evaluation tables or diagnostic logs. The CSV and description are treated as user-provided data; the matcher never scrapes sites, opens URLs, submits applications, contacts recruiters, or changes a profile. Tests use local fixtures and do not validate real-world matching quality.

### Vacancy intake and ranked application queue

Import vacancy information you collected yourself from a CSV. `title`, `company`, and `description` columns and nonblank values are required; the URL, location, experience, salary, and required-skills values are optional and stay blank when not supplied. Invalid URLs and rows missing required values are skipped. Matching uses the existing local resume matcher. Each imported listing is tracked as `To Review` when new; re-importing a duplicate preserves its existing status, notes, and status history while appending another match evaluation and intake record.

Duplicates use the existing normalized HTTP(S) URL strategy. When no URL is supplied, an unambiguous vacancy with the same normalized company and title is reused; this fallback can conflate separate openings with identical titles at one company, so inspect the queue. If no skills column is supplied, recognized requirements are extracted only from the provided description. A supplied `required_skills` value should use semicolon-separated items; only skills in the existing matching catalog affect the score, while the original supplied list remains available in review evidence.

Example CSV (save as `data\vacancies.csv`):

```csv
title,company,location,url,experience_requirement,salary,description,required_skills
Backend Developer,Example Co,Pune,https://example.com/jobs/123,3+ years,Not supplied,"Build Python APIs and services.",Python;SQL
```

Run the import and show the best unreviewed items:

```powershell
python -m naukri_automation.cli import-vacancies .\data\vacancies.csv
python -m naukri_automation.cli application-queue
```

You can also paste a description directly in PowerShell. Keep vacancy facts exactly as supplied; omit optional flags you do not know:

```powershell
$description = @'
Paste the complete job description here.
'@
python -m naukri_automation.cli add-vacancy --title "Backend Developer" --company "Example Co" --url "https://example.com/jobs/123" --location "Pune" --experience "3+ years" --required-skills "Python;SQL" --description $description
python -m naukri_automation.cli application-queue
```

The queue includes only vacancies imported through this intake flow that lack a user review tied to their latest saved evaluation. It ranks by heuristic score and displays saved reasons, required skills, missing information/uncertainties, URL, and job ID. It does not open URLs. To select an ID directly from that queue and run the existing preparation plus review-pack workflows together:

```powershell
# Replace 1 with the job ID displayed in application-queue.
python -m naukri_automation.cli prepare-queue-job 1 --cover-letter
```

This command first confirms the ID is still in the current unreviewed queue. It then appends a preparation draft and review-pack record, without changing tracker status. Optional supplied application questions can be included with `--questions-file .\data\application_questions.txt`. To run the existing commands independently, `prepare-application <job_id>` and `application-review-pack <job_id>` remain available.

The queue does not submit applications or modify profiles. All supplied listings remain local, and scores are not hiring probabilities. Unit tests cover intake behavior but do not validate real-world matching quality or vacancy availability.

### Jobicy remote vacancy discovery and twice-daily schedule

Jobicy publishes a documented public REST API for job-discovery tools, internal applications, and prototypes. Its documentation says no API key is required, asks clients to preserve the canonical Jobicy URL and source attribution, and advises against polling more often than once per hour. The implementation uses only `GET https://jobicy.com/api/v2/remote-jobs`; it does not use Naukri, browser automation, private endpoints, email, or application actions. See [Jobicy's API documentation](https://jobicy.com/jobs-rss-feed) and [fair-use terms](https://jobicy.com/jobs-rss-feed#fair-use).

Each run makes at most one request for each configured target-role tag, defaulting to five results per role (maximum 20 per role and 20 role tags). It has a 15-second per-request and 60-second total timeout by default, a 5 MiB response limit, and a local one-hour cooldown that applies after failed as well as successful API attempts. It fetches only the current first page for each role and does not paginate. API fields that are missing stay blank; numeric experience is not inferred from Jobicy's seniority label. Description HTML is converted to text before matching. Salary is retained only when Jobicy supplies salary values. Canonical Jobicy URLs, source listing IDs, publication metadata, and collection timestamps are stored in the existing SQLite vacancy-intake history. Repeated listings use the existing normalized-URL deduplication and append fresh match evaluations/intake observations without resetting tracker status, notes, or status history.

The role tags come from `TARGET_ROLES`. Jobicy's `geo` filters require its own location taxonomy, so this version does not invent a mapping from configured Indian city preferences; returned `jobGeo` is retained and the queue asks you to verify eligibility manually. Jobicy does not provide your notice-period preference as a filter. A missing salary remains missing; `Flexible for the right role` is not converted into a numeric range. Listings are remote listings but their geographic restrictions may still exclude a location.

Run a manual discovery, then inspect the existing ranked queue:

```powershell
.\.venv\Scripts\python.exe -m naukri_automation.cli fetch-jobicy
.\.venv\Scripts\python.exe -m naukri_automation.cli application-queue
```

Enable or disable the optional Windows schedule:

```powershell
.\.venv\Scripts\python.exe -m naukri_automation.cli enable-jobicy-schedule
.\.venv\Scripts\python.exe -m naukri_automation.cli disable-jobicy-schedule
```

Enabling creates the named Windows Task Scheduler task `NaukriJobicyDiscoveryScheduler` with an on-logon trigger and starts its background APScheduler daemon immediately. The daemon uses `TIMEZONE` (default `Asia/Kolkata`) and fetches/ranks at 09:00 and 21:00; enabling does not cause an immediate fetch. A singleton lock avoids a second scheduler process. Disabling asks Task Scheduler to end the running task and then removes the task registration.

When Windows sleeps, the daemon is suspended. On wake, at most one missed run is coalesced if it is within the one-hour misfire grace period; older missed runs are skipped and the next regular 09:00/21:00 trigger runs. When the computer is off, no fetch happens; at the next sign-in the daemon starts and waits for the next scheduled time, without replaying every missed run. The scheduler requires the project environment and internet access to remain available. Task Scheduler installation/removal affects only this named task; the generated launcher remains in `data` after disable. Tests mock the API and Windows scheduler; only a successful manual `fetch-jobicy` invocation establishes live API access, not unit tests.

### Application tracker v1 (manual only)

Import the existing ranked shortlist generated by `export-shortlist`:

```powershell
python -m naukri_automation.cli tracker-import .\data\shortlist.csv
python -m naukri_automation.cli tracker-list
python -m naukri_automation.cli tracker-show 1
python -m naukri_automation.cli tracker-export --output .\data\application_tracker.csv
```

New shortlist entries start at `Shortlisted`. Re-importing the same listing (normalized URL, or the title/company/location/description fallback key) updates available listing/match data but does not reset its tracker status, status history, or notes. Match evaluations, tracker status events, user notes, and review confirmations are stored in separate append-only history tables. If an imported shortlist omits a description, an existing stored description is retained; a new record does not get a fabricated description.

Inspect the job description and current match evidence before marking a listing ready:

```powershell
python -m naukri_automation.cli review-job 1
# After the displayed description, missing/uncertain requirements, and reasons look right,
# confirm the prompt. This records a local human review only.
python -m naukri_automation.cli tracker-status 1 "Ready to Apply"
```

`Ready to Apply` is rejected unless a description and evaluation exist and `review-job` was confirmed for that exact latest evaluation. Re-importing/re-evaluating creates a new evaluation and requires another review. To record an application, first submit it yourself outside this tool, then explicitly confirm:

```powershell
python -m naukri_automation.cli tracker-status 1 Applied --confirm-self-applied --note "Submitted by me"
```

`Applied` records the time the tracker status changed, not an inferred application date. Other available statuses are `Shortlisted`, `To Review`, `Assessment`, `Interview`, `Offer`, `Rejected`, `Withdrawn`, and `Not Suitable`. Notes are optional and append-only. This is a local tracking workflow only: it never opens job URLs, logs into Naukri, clicks Apply, submits forms, contacts recruiters, or alters the profile. Keep all site/application actions disabled.

## 4. Read-only Naukri profile UI inspection (new feature)

```powershell
python -m naukri_automation.cli inspect-profile
# or:
python -m naukri_automation.cli open-profile
```

This opens a persistent visible browser profile at `.browser-profile`. Sign in manually if prompted, navigate manually to your Naukri profile/resume page, then press Enter in the terminal. The app saves a read-only inventory of visible button labels, link labels/paths, and input labels/placeholders to `data\site_inspection\latest.json`.

Browser startup writes JSON diagnostic events to the terminal for launch, navigation, and page-load stages. It records timings, statuses, and error types only; it does not log full URLs, credentials, cookies, session tokens, or resume contents. The browser uses the persistent `.browser-profile` directory, but each command starts a new Playwright browser context using that directory; it does not attach to a browser process that is already running. It reuses an already-open Naukri tab in the launched context rather than navigating that tab back to the homepage. The profile directory is never removed or reset.

Timeout defaults are 30 seconds for browser launch, 30 seconds for navigation to DOM content loaded, and 10 seconds for the full page load event. Override them with `BROWSER_LAUNCH_TIMEOUT_MS`, `BROWSER_NAVIGATION_TIMEOUT_MS`, and `BROWSER_PAGE_LOAD_TIMEOUT_MS` if your network is slower. If full page load times out after DOM content is interactive, the visible page stays available for manual use and the timeout is logged; if navigation itself times out, the command reports a safe, stage-specific error.

Privacy and safety limits:
- It does not read typed input values, cookies, local storage, or full page body text.
- It removes URL credentials, query strings, and fragments from the saved page URL and stores only link origins/paths.
- It does not click buttons, edit profile fields, submit applications, or bypass CAPTCHA/OTP.
- Review the local JSON before sharing it; labels and link paths may still contain identifying information.
- Never share `.browser-profile`.

This is the next implementation milestone: capturing the page's visible UI structure without making changes. It does **not** yet perform a profile refresh.

To check a saved inspection for visible labels that might indicate a profile refresh control, run:

```powershell
python -m naukri_automation.cli verify-profile-refresh
```

This reports matching button/link labels as unverified candidates only. It does not infer selectors, reopen the site, click controls, or refresh the profile. If `latest.json` is missing, first capture an inspection with `inspect-profile`. Live profile-refresh actions remain disabled.

### Profile health and human review

Generate a local report from the saved inspection and resume-file check:

```powershell
python -m naukri_automation.cli profile-health
```

For a human-assisted, read-only review of the existing profile sections, run:

```powershell
python -m naukri_automation.cli review-profile
```

This opens/reuses the visible persistent browser and navigates to the profile route recorded in the saved inspection. If login has expired, sign in manually and complete any OTP/CAPTCHA yourself, then leave the browser on `/mnjuser/profile`. Return to PowerShell and press Enter. The command shows each of the 11 known section names and asks you to record one of `Not checked`, `Checked`, `Missing`, `Needs review`, or `Unverified` plus a concise evidence note. Do not enter personal field values into notes. The automation does not read profile values or page text, and does not click Edit, Update, Save, Upload, Delete, Apply, or submission controls. Reviews are stored as user-confirmed observations, not independent automation verification. Opening the page never counts as a profile update.

The health report does not infer that a section is `Missing` merely because its label was absent from an inspection. Without evidence it is `Not checked`; a user can record `Missing` after visually confirming that state. Report history and section-review status/timestamp/evidence notes are stored in SQLite. Evidence notes are saved in the local report database; keep them concise and avoid sensitive personal values. Unit tests do not verify live Naukri behavior.

### Read-only resume verification

```powershell
python -m naukri_automation.cli verify-resume
# Local PDF validation/hash only; does not open a browser:
python -m naukri_automation.cli verify-resume --local-only
```

The command validates the canonical `data\resume.pdf` using the existing readable, unencrypted PDF check and stores its filename, byte size, and SHA-256. In default mode it opens the visible persistent browser, navigates to the profile route in the saved inspection, and asks you to sign in manually if needed, inspect the Resume section, and return to PowerShell. You can record the displayed filename and upload date. These are user-confirmed observations; automation does not read page text or claim independent verification.

An optional byte-for-byte check is offered only after you confirm a resume is displayed. The tool never clicks Download. If you choose the option, manually download the displayed resume in the browser and enter the downloaded PDF path in PowerShell. `Content verified` is reported only when both PDFs validate and their SHA-256 hashes match. Filename equality alone yields `Filename matches`, not `Content verified`. If you skip the manual download, content remains `Unverified`.

Each verification is appended to the SQLite `resume_verifications` history; profile-health reports include the latest evidence and timestamp. The local database stores the displayed filename/date, so keep it private. Diagnostic logs contain only stages, result booleans, error types, and safe sizes; they exclude displayed personal metadata, hashes, credentials, cookies, tokens, and resume contents. This workflow does not upload, update, save, delete, or submit anything. Mocked unit tests do not establish that Naukri works in production.

### Profile freshness monitor

```powershell
python -m naukri_automation.cli profile-freshness
```

This command reads the latest saved SQLite section reviews and latest site-check resume verification, then appends a separate freshness-report snapshot. It does not open a browser, create review records, upload/download files, or change Naukri. Each of the 11 sections shows its last review time, evidence type, recorded status, and one of `Current`, `Review due`, `Stale`, `Never reviewed`, or `Unverified`. A user-confirmed review remains historical user-confirmed evidence; it does not mean the automation independently verified the present profile.

Defaults are `PROFILE_REVIEW_CURRENT_DAYS=30` and `PROFILE_REVIEW_DUE_DAYS=90` (Current through 30 days, Review due through 90 days, then Stale). Set them in `.env`; current days must be positive and less than review-due days. These are project heuristics only, not official Naukri ranking rules. A review cadence or profile edit is not claimed to improve ranking. The action list prioritizes sections previously marked Missing or Needs review, then stale/due or unverified evidence. It does not recommend activity for its own sake.

Resume state comes from the latest saved site-check record. Filename equality never implies content equality: content remains Unverified unless a manually downloaded copy was validated and its SHA-256 matches the local PDF. The command does not download either file. Reports are append-only in SQLite and do not duplicate or alter prior section reviews. Tests use mocked/local SQLite data and do not establish live Naukri behavior.

## 5. Resume upload workflow

```powershell
# Visible, read-only inspection is the default. It validates the PDF first,
# then opens the persistent browser for manual navigation to the profile page.
python -m naukri_automation.cli upload-resume

# Live upload is currently disabled; this command exits without opening a browser.
python -m naukri_automation.cli upload-resume --live

# Isolated file-input test; never clicks Update or submits.
python -m naukri_automation.cli upload-resume --test-file-selection
```

Both modes first confirm `data\resume.pdf` is a readable, unencrypted PDF. Dry-run opens the visible browser using `.browser-profile`; sign in manually if needed and navigate manually to `/mnjuser/profile`, then press Enter. It reports live HTML/accessibility properties for the file input and associated Update control. It does not attach the file or click anything. If the page, accessible labels, accepted file type, or controls differ from the inspected structure, the workflow stops without attaching the file.

Live upload is disabled in both the CLI and workflow. `--live` returns `disabled` before opening a browser or attaching the PDF. The verification helper separates an observed upload request, its HTTP response status, a specific site success notification, and profile metadata visible after a read-only reload. A toast or 2xx response is not enough: saved-profile verification requires the expected filename to appear after reload when it was not present in the pre-action baseline. This has not been verified against the live Naukri page, so the live action must remain disabled.

`--test-file-selection` is mutually exclusive with `--live`. It samples the exact input's `FileList` immediately after Playwright's `set_input_files()` call and again after a bounded 350 ms wait for page handlers; it also verifies whether the original input remains connected. This distinguishes a selection that never appears from one later cleared by page behavior. It records safe page-error, console-error, failed-request, and response-status diagnostics around the file-change event. It does not click Update or explicitly submit a form. Because ordinary page requests are allowed, use this mode only as a diagnostic selection step and watch the visible page; it does not claim a completed upload. The browser remains open for inspection; press Enter in the console after reviewing the result. The default remains dry-run, which never attaches the file.

Timestamped stage events are appended to `logs\resume_upload.jsonl`; events include only safe statuses, counts, booleans, timing/error types, and PDF size. They exclude page text, PDF contents, credentials, cookies, and session tokens. Local unit tests use mocked pages; they do not prove current Naukri behavior.

## 6. Read-only shortlist review

```powershell
.venv\Scripts\python.exe -m naukri_automation.cli review-shortlist
```

This compares tracked `Shortlisted`, `To Review`, and `Ready to Apply` jobs using only saved SQLite listing/evaluation records and locally extracted canonical-resume text. It prints saved descriptions and source URLs (without opening them), target-role alignment, catalogued skill overlap/gaps, numeric experience comparison when stated on both sides, saved reasons/uncertainties, score comparison, and a review priority. Skill absence from extracted text is not proof of lacking a skill. A `Ready to Apply` record is flagged when there is no recorded review tied to its latest saved evaluation; a recorded confirmation is user-confirmed, not independent verification. The command uses SQLite read-only mode and changes no statuses or history.

Imported shortlist rows did not supply original score components. The report therefore shows stored score/reasons separately from a recomputation using the current configured matcher and labels its components as current rules. If they differ, the report exposes the discrepancy. Scores are heuristic indicators, not hiring probabilities. No site behavior or matching quality is established by local unit tests.

## 6. Existing profile-check scheduler scaffold (separate from Jobicy)

```powershell
python -m naukri_automation.scheduler
```

This older scaffold stays running and records a daily profile-check placeholder. It does not update your Naukri profile or submit applications. It is separate from the Jobicy schedule above; use `enable-jobicy-schedule` / `disable-jobicy-schedule` for vacancy discovery. On a laptop, the old scaffold stops when the machine is off or asleep.

## 7. Current status and next implementation steps

Implemented and locally testable:
- Environment-based configuration and private local paths
- Read-only profile-page UI inventory after manual navigation (no edits/submissions)
- Resume upload dry-run and file-selection diagnostics; live submission disabled until persistence verification is validated
- PDF resume text extraction
- Local SQLite database and run log
- CSV import for job descriptions
- Deterministic resume/job keyword scoring and ranking
- Browser opens visibly for manual login/inspection
- Dry-run placeholder; no submission code is active

Not implemented or verified yet:
- Naukri-specific profile-refresh action and verification
- Job listing collection from Naukri
- Application form field mapping and submit-time duplicate protection
- Live application submission
- Cloud deployment, secure session persistence, and alerts
- AI/semantic job matching

We should implement each site-specific action only after inspecting the real page and testing the result. Keep live submission disabled until dry-run output is reviewed. Do not automate around CAPTCHA, OTP, or other access controls.
