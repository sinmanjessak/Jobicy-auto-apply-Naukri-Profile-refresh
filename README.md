# Naukri Automation & Jobicy Job Assistant

A local Node.js automation project for maintaining a Naukri profile,
organizing job leads, and supporting a resume-based job application
workflow. The project is designed to run on a Windows PC and can be
scheduled with Windows Task Scheduler.

> **Important:** Capabilities depend on the scripts and configuration
> present in your local checkout. The commands below reflect the known
> Naukri scripts. Treat Jobicy job discovery/auto-apply as enabled only
> if the corresponding integration exists in your version. Review every
> application before submission and comply with each job site's terms.

## Contents

-   [Overview](#overview)
-   [Features](#features)
-   [Requirements](#requirements)
-   [Installation](#installation)
-   [Configuration](#configuration)
-   [Usage](#usage)
-   [Jobicy integration](#jobicy-integration)
-   [Scheduling on Windows](#scheduling-on-windows)
-   [Security and privacy](#security-and-privacy)
-   [Troubleshooting](#troubleshooting)
-   [Project status and limitations](#project-status-and-limitations)

## Overview

The project aims to reduce repetitive job-search administration by
keeping job-search materials organized and helping identify relevant
vacancies.

### Naukri workflow

The known scripts include:

-   `naukri-profile-refresh.js` --- runs the Naukri profile-refresh
    workflow.
-   `auto-apply-runner.js` --- starts the application workflow for a
    selected platform; the known invocation is
    `node auto-apply-runner.js naukri`.

Website behavior, login state, and available controls can change. Always
check the logs and verify the result on the website rather than assuming
a run succeeded.

### Jobicy workflow

Jobicy can be used as a separate source of job listings if the Jobicy
adapter/feed integration is included in your checkout. Job discovery and
job application are different steps: a listing being found or ranked
does **not** mean an application was submitted. Confirm which steps your
current implementation supports before enabling unattended operation.

## Features

Depending on the scripts and configuration in your checkout, the project
may support:

-   Running a Naukri profile-refresh workflow.
-   Running a platform-specific application workflow.
-   Ranking or reviewing job leads against your resume when listing data
    is available.
-   Maintaining local job statuses and notes.
-   Producing local reports to help review profile health and
    application readiness.
-   Scheduling supported scripts with Windows Task Scheduler.

Do not assume a feature is active merely because it is described here;
check the source code, configuration, and logs in your local checkout.

## Requirements

-   Windows 10/11 (commands below use PowerShell).
-   Node.js and npm.
-   Git.
-   A GitHub account if you want to back up/version-control the project.
-   Valid website access and authentication for any platform workflow
    you choose to run.

Check your installations:

``` powershell
node --version
npm --version
git --version
```

Install Node.js from the official site: https://nodejs.org/

## Installation

### 1. Clone the repository

Replace the URL with your own repository URL:

``` powershell
git clone https://github.com/YOUR_USERNAME/Jobicy-auto-apply-Naukri-Profile-refresh.git
cd Jobicy-auto-apply-Naukri-Profile-refresh
```

If you already have the project locally, open PowerShell in that project
directory instead.

### 2. Install dependencies

If the project contains a `package.json`, run:

``` powershell
npm install
```

If there is no `package.json`, inspect the project README and scripts
before installing dependencies; do not run this command from an
unrelated folder.

### 3. Configure local settings

Inspect the project for its expected configuration files and environment
variables. If it provides an `.env.example`, copy it to `.env` and fill
in only the settings the code actually expects:

``` powershell
Copy-Item .env.example .env
notepad .env
```

Do not invent environment variable names; check the source code or
example configuration. Never commit `.env`, passwords, API tokens,
cookies, or browser session data.

### 4. Check the available scripts

Review `package.json` and the project files to confirm the current
script names:

``` powershell
Get-ChildItem
Get-Content package.json
```

If `package.json` is not present, skip the second command.

## Configuration

Before running an automation:

1.  Confirm the target platform and account are correct.
2.  Confirm the resume and profile information are current.
3.  Review the job criteria and application settings supported by your
    version.
4.  Keep credentials and session data local.
5.  Start with a manual run and inspect its output before scheduling it.

Do not bypass CAPTCHAs, access controls, or platform security
protections. Stop if the website presents a verification or unexpected
error page.

## Usage

Run these commands from the project root.

### Naukri profile refresh

``` powershell
node naukri-profile-refresh.js
```

This starts the profile-refresh script. Check the output and confirm on
Naukri that the intended update actually occurred.

### Naukri application workflow

``` powershell
node auto-apply-runner.js naukri
```

This starts the Naukri workflow implemented by the runner. Its exact
behavior depends on the current code and configuration. Review job
details and application status; do not treat a successful process exit
as proof that an employer received an application.

### Job shortlist and status tracking

If your checkout includes a local shortlist CSV or status tracker,
follow its schema and review the job descriptions, source links, and
status values before acting. Keep personal information out of public
repositories and generated public reports.

## Jobicy integration

Jobicy support depends on the Jobicy adapter and configuration included
in your current code.

A typical workflow, **if implemented**, is:

1.  Retrieve available listings from the configured Jobicy source.
2.  Filter or rank listings using your chosen job criteria and resume.
3.  Review job title, employer, location, eligibility, and application
    URL.
4.  Open the employer's application page and submit only after checking
    the details.
5.  Record the actual outcome in your tracker.

**Do not assume Jobicy itself has submitted an application.** Job boards
may provide listings that link to an employer's own application system.
The application step may need manual completion unless your code
explicitly supports that site's workflow.

To verify what your checkout supports, search the code for `Jobicy`,
API/feed URLs, listing parsers, and the relevant runner commands.
Document the real command and required configuration here after
confirming them. Avoid adding API endpoints or environment variables
that the code does not use.

## Scheduling on Windows

You can use Windows Task Scheduler to run a script on a schedule.

1.  Press `Win + R`, type `taskschd.msc`, and press Enter.

2.  Choose **Create Basic Task**.

3.  Give it a descriptive name, such as `Naukri Profile Refresh`.

4.  Choose a daily trigger and set the desired time.

5.  Choose **Start a program**.

6.  Set **Program/script** to the full path to `node.exe` (find it with
    `where.exe node`).

7.  Set **Add arguments** to the script path, for example:

    ``` text
    "C:\path\to\project\naukri-profile-refresh.js"
    ```

8.  Set **Start in** to the project directory.

9.  Save, then use **Run** to test the task and inspect its logs.

Create separate tasks for different workflows rather than scheduling
multiple scripts together before testing. Your PC must be powered on and
available for a scheduled task to run. Site logins can expire, and
websites may change their UI.

### Optional Git backup task

If you use a local Git repository, commit and push meaningful project
changes. A scheduled script should not create empty or artificial
commits just to change the contribution graph. Configure `.gitignore`
before the first commit and confirm that credentials, browser profiles,
generated reports, and personal data are excluded.

## Security and privacy

-   Keep `.env`, access tokens, passwords, cookies, browser profiles,
    and authentication state out of Git.
-   Use `.gitignore` for local configuration, dependencies, logs,
    browser data, and sensitive reports.
-   Review `git status` before committing.
-   If a secret was committed, removing the file in a later commit does
    not erase it from Git history. Revoke or rotate the secret and
    follow GitHub's guidance for sensitive data.
-   Avoid committing resumes, contact details, job application records,
    or employer-confidential information to a public repository.
-   Store only the data you need and review it before sharing logs or
    screenshots.
-   Respect the terms, rate limits, and access controls of Naukri,
    Jobicy, and employer application sites. Do not run unattended
    submissions unless you have verified that the workflow is permitted
    and safe.

## Troubleshooting

### `git push` is rejected

The remote branch may contain commits missing locally. Check:

``` powershell
git status
git remote -v
git branch --show-current
```

Resolve any unfinished merge or rebase before pulling or pushing. Avoid
`git push --force` unless you fully understand the impact and have
intentionally chosen to replace remote history.

### A script cannot find a file

Run it from the project root and verify the filename:

``` powershell
Get-Location
Get-ChildItem
```

### Website login or automation fails

-   Sign in manually and check whether the session expired.
-   Check the script output and local logs.
-   Look for site maintenance, CAPTCHA, verification, or changed page
    controls.
-   Stop rather than attempting to bypass security checks.
-   Re-test manually after updating the code.

### A job appears in the tracker but was not applied to

A listing being fetched, ranked, or marked shortlisted is not the same
as an application being submitted. Verify the application status on the
employer or job platform.

## Project status and limitations

This repository is intended as a local job-search productivity tool.
Before describing it as fully automated, verify each capability against
the current code:

-   [ ] Naukri profile-refresh script runs and its result is verified.
-   [ ] Naukri application workflow runs and reports outcomes
    accurately.
-   [ ] Jobicy listing retrieval is implemented and tested.
-   [ ] Resume-based matching/ranking is implemented and tested.
-   [ ] Application submission is supported for the relevant destination
    and permitted by its rules.
-   [ ] Logs avoid exposing secrets and personal data.
-   [ ] Windows scheduled tasks have been tested.

## Contributing

For personal changes, create a branch, make a meaningful change, test
it, and commit it with a descriptive message. Do not include
credentials, private session data, or confidential job-search records in
pull requests.

## Disclaimer

This project is provided for personal productivity and educational use.
It is not affiliated with or endorsed by Naukri or Jobicy. Job
availability, site behavior, and platform policies can change. Users are
responsible for checking listings, eligibility, application accuracy,
and compliance with applicable terms.
