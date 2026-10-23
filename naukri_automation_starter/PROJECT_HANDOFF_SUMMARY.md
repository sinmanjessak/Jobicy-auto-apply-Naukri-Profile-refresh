# Naukri Local-First Project — Handoff Summary

Audit snapshot: 2026-10-17 (Asia/Calcutta)  
Project root: `C:\Users\tiwar\Downloads\naukri_automation_starter\naukri_automation_starter`

## Current verification

- The local CLI starts and lists the implemented commands, including `fetch-jobicy`, `application-queue`, the tracker, preparation, review, and profile-health commands.
- `application-queue` ran successfully against the local database. The displayed unreviewed entries came from `csv` and `manual` intake, including sample/example URLs. This run does not prove Jobicy discovery.
- Jobicy diagnostics are inconsistent: `logs/jobicy_discovery.jsonl` contains a `request_complete` event with one listing and an `intake_complete` event, but the main SQLite database (`data/naukri_automation.sqlite3`) contains no `jobicy` rows in `vacancy_intake_records`. Its latest persisted `jobicy_discovery` run is `failed` (`2026-10-16 21:39:29` UTC / `2026-10-17 03:09:29` India time). The diagnostic log has no run identifier to tie those events to that persisted run. Therefore, a real listing reaching the ranked queue is **not verified**.
- No new API request was made during this verification. The CLI previously reported its one-run-per-hour cooldown; do not alter/delete run history to bypass that limit.
- The first local full-suite invocation failed during pytest temp-directory setup because the system temp location could not create a directory. Rerunning with a workspace temp path succeeded: **158 passed in 7.11s** using `--basetemp .pytest-handoff`.
- No website was opened and no profile, application status, or tracker record was changed during this verification.

## Implemented feature map

The application is a Python CLI organized under `naukri_automation/`, with local SQLite persistence and tests under `tests/`.

| Capability | Main implementation | CLI / evidence boundary |
|---|---|---|
| Configuration and local resume/matching | `config.py`, `resume.py`, `matching.py`, `job_matching.py` | Local deterministic/heuristic processing; does not prove real-world match quality. |
| Profile inspection and health | `browser.py`, `inspection.py`, `profile_health.py`, `profile_freshness.py`, `tasks.py` | Browser/profile workflows have safety checks; historic human review is not current independent verification. |
| Resume validation and upload diagnostics | `resume_verification.py`, `upload.py`, `diagnostics.py` | Upload CLI is dry-run by default and live upload is disabled; previous selection diagnostics were not proof of a saved profile resume. |
| Vacancy intake and ranking | `vacancy_intake.py`, `job_matching.py` | `import-vacancies`, `add-vacancy`, `application-queue`; supplied data only. Scores are heuristics. |
| Jobicy discovery and schedule | `jobicy.py`, `jobicy_scheduler.py`, `db.py`, `config.py` | `fetch-jobicy`, `enable-jobicy-schedule`, `disable-jobicy-schedule`; live retrieval currently unverified as described above. |
| Tracker and review | `application_tracker.py`, `shortlist_review.py`, `application_review_pack.py` | Manual statuses and review decisions; never submits applications. |
| Preparation and quality checks | `application_preparation.py`, `application_quality.py` | Local drafts/reports, based only on available evidence; human editing and factual review remain required. |
| Browser/task scaffolding | `browser.py`, `tasks.py`, `scheduler.py` | Naukri login/profile actions remain manual; do not treat page load as profile update. |

The repo also contains generated local data and reports, `.browser-profile/`, `logs/`, and `data/`. Preserve the browser profile, SQLite history, resume, and existing tracker records. Do not copy secrets from `.env` into another platform. Keep credentials, cookies, tokens, and resume contents out of logs.

## Existing CLI entry point

Run commands from the project root in PowerShell using the project virtual environment:

```powershell
.\.venv\Scripts\python.exe -m naukri_automation.cli --help
.\.venv\Scripts\python.exe -m naukri_automation.cli application-queue
.\.venv\Scripts\python.exe -m naukri_automation.cli fetch-jobicy
```

The documented Jobicy source is its public API (`https://jobicy.com/api/v2/remote-jobs`), as referenced by the project README. The implementation is intentionally bounded and rate-limited (one discovery run per hour), with request/total timeouts, response-size limits, canonical Jobicy URL validation, provenance, and existing intake/matching integration. It does not scrape Naukri. Tests mock API and Task Scheduler behavior; mock tests are not evidence of production API access.

Scheduling commands (not verified as enabled on this machine):

```powershell
.\.venv\Scripts\python.exe -m naukri_automation.cli enable-jobicy-schedule
.\.venv\Scripts\python.exe -m naukri_automation.cli disable-jobicy-schedule
```

The README describes a Windows Task Scheduler on-logon launcher and a background 09:00/21:00 Asia/Kolkata schedule. Confirm the task state locally before relying on it. A sleeping/off computer cannot fetch during downtime.

## Recommended next step for a new coding platform

First reconcile the Jobicy run evidence before adding features:

1. Check the current `DATABASE_PATH` from configuration without printing `.env` secrets.
2. Correlate one real `fetch-jobicy` invocation across a unique run ID in structured logs, the `runs` row, intake provenance, and queue output. Add a run ID if needed.
3. Ensure tests write diagnostics to temporary paths and do not contaminate project `logs/`.
4. After the cooldown, make one bounded manual fetch if desired; then confirm the same source listing ID and canonical URL in SQLite and the queue. Do not repeatedly retry connection failures or bypass the one-hour limit.
5. Only then decide whether production discovery is working. Preserve all statuses and history, especially the existing Applied record.

Do not build email access, automatic application submission, profile edits/uploads, CAPTCHA bypasses, authenticated-site scraping, or cloud deployment as part of this reconciliation.
