from pathlib import Path
import typer
from rich.console import Console
from rich.table import Table
from .config import settings
from .db import list_jobs, connect
from .resume import extract_resume_text
from .tasks import inspect_profile, dry_run_apply, verify_profile_refresh, run_resume_upload, review_profile_read_only, verify_resume_read_only
from .profile_health import generate_profile_health_report, render_profile_health_summary
from .profile_freshness import generate_profile_freshness_report, render_profile_freshness_summary
from .job_matching import (
    evaluate_manual_description, evaluate_saved_jobs, export_shortlist, import_job_csv,
)
from .application_tracker import (
    TRACKER_STATUSES, export_tracker, import_shortlist, list_tracked_jobs,
    record_job_review, update_job_status,
)
from .shortlist_review import build_shortlist_review
from .application_preparation import prepare_application_materials
from .application_quality import check_application_quality
from .application_review_pack import create_application_review_pack, record_application_review_decision, REVIEW_DECISIONS
from .vacancy_intake import add_vacancy, import_vacancies_csv, ranked_unreviewed_vacancies
from .jobicy import JobicyFetchError, fetch_jobicy_vacancies
from .jobicy_scheduler import enable_windows_schedule, disable_windows_schedule

app = typer.Typer(help="Local-first Naukri automation starter. Submission is disabled by default.")
console = Console()

@app.command()
def setup_check():
    """Check local configuration and resume availability."""
    console.print(f"Database: {settings.database_path}")
    console.print(f"Timezone: {settings.timezone}; scheduled refresh: {settings.refresh_hour:02d}:{settings.refresh_minute:02d}")
    console.print(f"Live submission configured: {settings.live_submission}")
    try:
        text = extract_resume_text(settings.resume_path)
        console.print(f"Resume loaded: {len(text)} characters from {settings.resume_path}")
    except Exception as exc:
        console.print(f"Resume not ready: {exc}")

@app.command("open-profile")
def open_profile():
    """Open Naukri, navigate manually to your profile, and save a read-only UI inventory."""
    inspect_profile(headless=False)


@app.command("inspect-profile")
def inspect_profile_command():
    """Alias for open-profile: capture the current profile page's visible UI structure."""
    inspect_profile(headless=False)

@app.command("verify-profile-refresh")
def verify_profile_refresh_command():
    """Read the saved UI inventory and report possible refresh labels; perform no action."""
    try:
        result = verify_profile_refresh()
    except FileNotFoundError:
        console.print("No saved inspection found. Run inspect-profile first; no browser action was performed.")
        raise typer.Exit(code=1)
    console.print(f"Inspection result: {result['status']}")
    for item in result["evidence"]:
        console.print(f"Candidate {item['kind']} label: {item['label']} (path: {item.get('path', '')})")
    console.print(result["note"])
    console.print("Live profile refresh: disabled.")

@app.command("profile-health")
def profile_health_command():
    """Generate a read-only profile-health report and save it in SQLite history."""
    try:
        report, _report_id = generate_profile_health_report()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(render_profile_health_summary(report))
    console.print("No profile fields were read from the live page or modified.")


@app.command("profile-freshness")
def profile_freshness_command():
    """Generate a local-only freshness report from existing SQLite evidence."""
    try:
        report, _report_id = generate_profile_freshness_report()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(render_profile_freshness_summary(report))
    console.print("Read-only local report; no browser was opened and no profile or review records were changed.")


@app.command("review-profile")
def review_profile_command():
    """Open a visible profile page for a human-led, read-only section review."""
    try:
        review_profile_read_only()
    except (RuntimeError, ValueError, OSError):
        raise typer.Exit(code=1) from None


@app.command("verify-resume")
def verify_resume_command(
    local_only: bool = typer.Option(False, "--local-only", help="Validate/hash the canonical PDF without opening a browser."),
):
    """Read-only local PDF and human-assisted profile resume verification."""
    try:
        result = verify_resume_read_only(local_only=local_only)
    except (RuntimeError, ValueError, OSError):
        raise typer.Exit(code=1) from None
    if result.get("local_status") != "Local file valid":
        raise typer.Exit(code=1)

@app.command("upload-resume")
def upload_resume_command(
    live: bool = typer.Option(False, "--live", help="Disabled pending verified post-upload persistence checks; no action will be taken."),
    test_file_selection: bool = typer.Option(False, "--test-file-selection", help="Select and diagnose the PDF input only; never click Update or explicitly submit a form."),
):
    """Dry-run by default; file selection is explicit and live upload is disabled."""
    if live and test_file_selection:
        raise typer.BadParameter("--live and --test-file-selection are separate modes; choose one.")
    result = run_resume_upload(live=live, file_selection_test=test_file_selection)
    if result["status"] in {"blocked", "failed", "disabled"} or (live and result["status"] != "verified"):
        raise typer.Exit(code=1)
    if test_file_selection and result["status"] != "selected":
        raise typer.Exit(code=1)

@app.command()
def apply_dry_run():
    """Run the safe placeholder for the application workflow; no submissions occur."""
    dry_run_apply()

@app.command()
def jobs(limit: int = 30):
    """List jobs saved in the local database."""
    table = Table("Title", "Company", "Location", "Score", "Status")
    for job in list_jobs(limit):
        table.add_row(job["title"], job["company"], job["location"], str(job["match_score"] or "—"), job["status"])
    console.print(table)

@app.command("import-jobs")
def import_jobs(csv_file: str):
    """Import user-provided job CSV rows; URLs and other optional fields may be blank."""
    try:
        result = import_job_csv(Path(csv_file))
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from None
    console.print(
        f"Job listings: {result['inserted']} inserted, {result['updated']} updated, "
        f"{len(result['skipped'])} skipped; database: {settings.database_path}"
    )
    for item in result["skipped"]:
        console.print(f"Skipped CSV row {item['row']}: {item['reason']}")


@app.command("import-vacancies")
def import_vacancies_command(csv_file: str = typer.Argument(..., help="User-supplied vacancy CSV.")):
    """Validate, deduplicate, and locally score supplied vacancies; no site is contacted."""
    try:
        result = import_vacancies_csv(Path(csv_file))
    except (FileNotFoundError, OSError, ValueError) as exc:
        console.print(f"[red]Could not import vacancies: {exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"Vacancies: {result['inserted']} new, {result['updated']} existing refreshed, {len(result['skipped'])} invalid row(s) skipped.")
    for item in result["results"]:
        console.print(f"Job #{item['job_id']}: heuristic score {item['score']}/100; evaluation #{item['evaluation_id']}; tracker status is preserved.", markup=False)
    for item in result["skipped"]:
        console.print(f"Skipped CSV row {item['row']}: {item['reason']}", markup=False)


@app.command("add-vacancy")
def add_vacancy_command(
    title: str = typer.Option(..., "--title", help="Supplied job title."),
    company: str = typer.Option(..., "--company", help="Supplied company name."),
    description: str = typer.Option(..., "--description", help="Pasted full description; PowerShell here-strings support multiline text."),
    url: str = typer.Option("", "--url", help="Supplied application/source URL; never opened."),
    location: str = typer.Option("", "--location", help="Supplied location, if known."),
    experience: str = typer.Option("", "--experience", help="Supplied experience requirement, if known."),
    salary: str = typer.Option("", "--salary", help="Supplied salary text, if present."),
    required_skills: str = typer.Option("", "--required-skills", help="Skills as supplied, separated with semicolons."),
):
    """Add a pasted vacancy and score it locally; does not open its URL."""
    try:
        result = add_vacancy(title=title, company=company, description=description, url=url,
                             location=location, experience_requirement=experience, salary=salary,
                             required_skills=required_skills)
    except (OSError, ValueError) as exc:
        console.print(f"[red]Could not add vacancy: {exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"Job #{result['job_id']}: heuristic score {result['score']}/100; evaluation #{result['evaluation_id']}; tracker status is preserved or initialized to To Review.")


@app.command("application-queue")
def application_queue_command(limit: int = typer.Option(100, min=1, max=5000, help="Maximum ranked unreviewed vacancies.")):
    """Show supplied, unreviewed vacancies ranked by the saved heuristic score."""
    try:
        queue = ranked_unreviewed_vacancies(limit=limit)
    except (OSError, ValueError) as exc:
        console.print(f"[red]Could not load application queue: {exc}[/red]")
        raise typer.Exit(code=1) from None
    if not queue:
        console.print("No unreviewed vacancies from the intake workflow are in the queue.")
        return
    console.print("Unreviewed vacancy queue — match scores are heuristics, not hiring probabilities.")
    for rank, item in enumerate(queue, start=1):
        console.print(f"\n{rank}. Job #{item['id']} — {item['company']} / {item['title']} — {item['match_score']}/100 — {item['status']}", markup=False)
        console.print(f"   Application URL: {item['source_url'] or 'not supplied'}", markup=False)
        console.print(f"   Source: {item['source']} (collected {item['collected_at']})", markup=False)
        if item["source"] == "jobicy":
            metadata = item["source_metadata"]
            console.print(f"   Jobicy ID: {item['source_listing_id'] or 'not supplied'}; published: {metadata.get('published_at') or 'not supplied'}; level: {metadata.get('job_level') or 'not supplied'}", markup=False)
        console.print(f"   Reasons to review: {'; '.join(item['reasons_to_apply']) or 'no saved reasons'}", markup=False)
        console.print(f"   Required skills: {', '.join(item['required_skills']) or 'not identified from supplied data'}", markup=False)
        console.print(f"   Information to verify: {'; '.join(item['information_to_verify']) or 'none flagged by local checks; manually verify listing details'}", markup=False)
        console.print(f"   For job #{item['id']}: prepare-application {item['id']} ; application-review-pack {item['id']}", markup=False)


@app.command("fetch-jobicy")
def fetch_jobicy_command():
    """Fetch a bounded set of public Jobicy listings and run existing local matching."""
    try:
        result = fetch_jobicy_vacancies()
    except (JobicyFetchError, FileNotFoundError, OSError, ValueError) as exc:
        console.print(f"[red]Jobicy discovery did not complete: {exc}[/red]", markup=False)
        raise typer.Exit(code=1) from None
    console.print(f"Jobicy discovery at {result['collected_at']}: {result['inserted']} new, "
                  f"{result['updated']} existing refreshed, {len(result['skipped'])} invalid listing(s) skipped.")
    console.print(f"Queried target-role tags: {', '.join(result['roles_queried'])}")
    console.print("Listings were saved through the existing intake and matching pipeline; scores are heuristics.")
    console.print("Run `python -m naukri_automation.cli application-queue` to inspect ranked unreviewed jobs.")


@app.command("enable-jobicy-schedule")
def enable_jobicy_schedule_command():
    """Install a Windows on-logon task that runs Jobicy fetch/rank at 09:00 and 21:00 Asia/Kolkata."""
    try:
        result = enable_windows_schedule()
    except (OSError, RuntimeError) as exc:
        console.print(f"[red]Could not enable Jobicy schedule: {exc}[/red]", markup=False)
        raise typer.Exit(code=1) from None
    console.print(f"Enabled Windows task {result['task_name']}; discovery runs at 09:00 and 21:00 ({result['timezone']}).")
    console.print("The background scheduler was started; the first fetch occurs at the next scheduled time.")


@app.command("disable-jobicy-schedule")
def disable_jobicy_schedule_command():
    """Remove this project's Windows Task Scheduler entry; no data is deleted."""
    try:
        disable_windows_schedule()
    except (OSError, RuntimeError) as exc:
        console.print(f"[red]Could not disable Jobicy schedule: {exc}[/red]", markup=False)
        raise typer.Exit(code=1) from None
    console.print("Disabled the Jobicy discovery scheduled task. Existing vacancy and application history remains unchanged.")


@app.command("prepare-queue-job")
def prepare_queue_job_command(
    job_id: int = typer.Argument(..., min=1, help="Job ID shown in application-queue."),
    with_cover_letter: bool = typer.Option(False, "--cover-letter", help="Also create an editable cover-letter draft."),
    questions_file: str = typer.Option("", "--questions-file", help="Optional UTF-8 file with one supplied application question per line."),
    preparation_output_dir: str = typer.Option("data/application_materials", "--preparation-output-dir", help="Local draft output root."),
    review_pack_output_dir: str = typer.Option("data/application_review_packs", "--review-pack-output-dir", help="Local review-pack output root."),
):
    """Prepare drafts and a review pack for a job that is currently in the unreviewed queue."""
    try:
        queue = ranked_unreviewed_vacancies()
    except (OSError, ValueError) as exc:
        console.print(f"[red]Could not load application queue: {exc}[/red]")
        raise typer.Exit(code=1) from None
    selected = next((item for item in queue if int(item["id"]) == job_id), None)
    if selected is None:
        console.print(f"Job #{job_id} is not currently in the unreviewed vacancy queue. No drafts were generated.")
        raise typer.Exit(code=1)

    try:
        prepared = prepare_application_materials(
            job_id,
            output_dir=Path(preparation_output_dir),
            include_cover_letter=with_cover_letter,
            questions_path=Path(questions_file) if questions_file else None,
        )
        pack = create_application_review_pack(job_id, output_dir=Path(review_pack_output_dir))
    except (KeyError, OSError, ValueError) as exc:
        console.print(f"[red]Could not prepare queue vacancy: {exc}[/red]")
        raise typer.Exit(code=1) from None

    console.print(f"Selected queue item: Job #{job_id} — {selected['company']} / {selected['title']}", markup=False)
    console.print(f"Preparation report: {prepared['improvement_report_path']}")
    if prepared["cover_letter_path"]:
        console.print(f"Cover-letter draft: {prepared['cover_letter_path']}")
    if prepared["question_answers_path"]:
        console.print(f"Question-answer draft: {prepared['question_answers_path']}")
    console.print(f"Review pack: {pack['report_path']}")
    console.print(f"Tracker status remains {pack['status_unchanged']}; no URL was opened and no application was submitted.")


@app.command("evaluate-job")
def evaluate_job_command(
    description_file: str = typer.Argument(..., help="Local UTF-8 text file containing the supplied job description."),
    title: str = typer.Option("", "--title", help="Title as supplied; leave blank if unknown."),
    company: str = typer.Option("", "--company", help="Company as supplied; leave blank if unknown."),
    location: str = typer.Option("", "--location", help="Location as supplied; leave blank if unknown."),
    url: str = typer.Option("", "--url", help="Source URL as supplied; never opened."),
    experience: str = typer.Option("", "--experience", help="Explicit experience requirement if provided."),
    salary: str = typer.Option("", "--salary", help="Salary text if provided; no estimate is inferred."),
):
    """Evaluate a manually supplied job description locally and save the result."""
    try:
        result = evaluate_manual_description(
            Path(description_file), title=title, company=company, location=location,
            url=url, experience_requirement=experience, salary=salary,
        )
    except (FileNotFoundError, ValueError, OSError) as exc:
        console.print(f"[red]Could not evaluate supplied description: {exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"Match score: {result['score']}/100 (heuristic, not hiring probability)")
    for reason in result["reasons"]:
        console.print(f"Reason: {reason}")
    for item in result["missing_requirements"]:
        console.print(f"Not found in resume text (uncertain): {item}")
    for item in result["uncertainties"]:
        console.print(f"Uncertain: {item}")
    for item in result["mismatches"]:
        console.print(f"Explicit mismatch: {item}")
    console.print(f"Evaluation history id: {result['evaluation_id']}; listing title: {result['job']['title'] or 'not supplied'}")


@app.command("match-jobs")
def match_jobs_command(limit: int = typer.Option(100, min=1, max=5000)):
    """Rank locally stored user-supplied listings against the canonical resume."""
    try:
        results = evaluate_saved_jobs(limit=limit)
    except (FileNotFoundError, ValueError, OSError) as exc:
        console.print(f"[red]Could not evaluate jobs: {exc}[/red]")
        raise typer.Exit(code=1) from None
    table = Table("Score", "Title", "Company", "Location", "URL", "Reasons / gaps")
    for result in results:
        details = " | ".join(result["reasons"] + result["uncertainties"] + result["mismatches"])
        table.add_row(str(result["score"]), result["job"]["title"], result["job"]["company"],
                      result["job"]["location"], result["job"]["url"] or "—", details)
    console.print(table)
    console.print("Scores are explainable local heuristics, not probabilities of getting hired.")


@app.command("export-shortlist")
def export_shortlist_command(
    output: str = typer.Option("data/shortlist.csv", "--output", "-o"),
    limit: int = typer.Option(100, min=1, max=5000),
):
    """Export the latest saved match per listing to a local CSV."""
    try:
        count = export_shortlist(Path(output), limit=limit)
    except (OSError, ValueError) as exc:
        console.print(f"[red]Could not export shortlist: {exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"Exported {count} evaluated listing(s) to {output}")


@app.command("tracker-import")
def tracker_import_command(path: str = typer.Argument("data/shortlist.csv")):
    """Import the local ranked shortlist; existing status history and notes are retained."""
    try:
        result = import_shortlist(Path(path))
    except (OSError, ValueError) as exc:
        console.print(f"[red]Could not import shortlist: {exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"Tracker import: {result['imported']} new, {result['updated']} existing updated, {result['skipped']} invalid rows skipped.")


@app.command("tracker-list")
def tracker_list_command(status: str = typer.Option("", help="Filter by an exact tracker status.")):
    """List locally tracked jobs and their current manual status."""
    try:
        rows = list_tracked_jobs(status or None)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    table = Table("ID", "Score", "Title", "Company", "Status", "Status changed", "URL")
    for row in rows:
        table.add_row(str(row["id"]), str(row["match_score"] if row["match_score"] is not None else "—"),
                      row["title"], row["company"], row["status"], row["status_changed_at"], row["url"] or "—")
    console.print(table)
    console.print("Match scores are heuristics, not hiring probabilities.")


@app.command("review-shortlist")
def review_shortlist_command():
    """Compare active shortlist records with local resume evidence; read-only."""
    try:
        report = build_shortlist_review()
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Could not review shortlist: {exc}[/red]")
        raise typer.Exit(code=1) from None
    if not report["resume_evidence_available"]:
        console.print("Canonical resume evidence unavailable; skill and experience comparisons may be incomplete.")
    table = Table("Company", "Job", "Status", "Stored score", "Target role", "Matched skills", "Not found / uncertain", "Experience", "Priority")
    for job in report["jobs"]:
        experience = job["experience"]["status"]
        table.add_row(
            job["company"] or "not supplied", job["title"] or "not supplied", job["status"],
            str(job["stored_score"] if job["stored_score"] is not None else "unavailable"),
            str(job["target_role_alignment"]), ", ".join(job["matched_skills"]) or "none / unavailable",
            ", ".join(job["skills_mentioned_not_found_in_resume_text"]) or "none / unavailable",
            experience, f"{job['review_priority']}: {job['review_priority_reason']}",
        )
    console.print(table)
    for job in report["jobs"]:
        console.print(f"\n[bold]{job['company'] or 'Company not supplied'} — {job['title'] or 'Title not supplied'} (#{job['id']})[/bold]")
        console.print(f"Status: {job['status']}; source URL (not opened): {job['url'] or 'not supplied'}")
        console.print(f"Location: {job['location'] or 'not supplied'}; experience requirement: {job['experience_requirement'] or 'not supplied'}; salary: {job['salary'] or 'not supplied'}")
        console.print(f"Description: {job['description'] or 'not stored'}")
        console.print(f"Catalogued listing skills not found in resume text (not proof they are absent): {', '.join(job['skills_mentioned_not_found_in_resume_text']) or 'none detected / unavailable'}")
        console.print(f"Explicitly required skills detected: {', '.join(job['explicitly_required_skills']) or 'none detected / unavailable'}")
        console.print(f"Required skills not found in resume text: {', '.join(job['required_skills_not_found_in_resume_text']) or 'none detected / unavailable'}")
        console.print(f"Experience: {job['experience']['summary']}")
        console.print(f"Qualification mismatches: {'; '.join(job['qualification_mismatches']) or 'none recorded by available evidence'}")
        console.print(f"Saved reasons: {'; '.join(job['saved_match_reasons']) or 'unavailable'}")
        console.print(f"Saved uncertainties: {'; '.join(job['saved_uncertainties']) or 'none recorded'}")
        console.print(f"Unavailable data: {', '.join(job['unavailable_data']) or 'none identified'}")
        console.print(f"Score: {job['stored_score'] if job['stored_score'] is not None else 'unavailable'} stored; "
                      f"{job['current_recomputed_score'] if job['current_recomputed_score'] is not None else 'unavailable'} current recomputation; "
                      f"original component breakdown {'available' if job['stored_component_breakdown_available'] else 'unavailable'}.")
        if job["current_component_breakdown"]:
            console.print(f"Current matcher components: {job['current_component_breakdown']}; {job['score_limitations']}")
        if job["ready_to_apply_review_flag"]:
            console.print(f"[yellow]Ready-to-apply review: {job['ready_to_apply_review_flag']}[/yellow]")
    console.print("Scores are heuristics, not hiring probabilities. Priority reflects role/evidence review needs, not score alone.")
    console.print("Read-only local review: no URLs opened, records changed, or applications submitted.")


@app.command("prepare-application")
def prepare_application_command(
    job_id: int = typer.Argument(..., min=1, help="Existing local application-tracker job ID."),
    output_dir: str = typer.Option("data/application_materials", "--output-dir", help="Local directory for editable Markdown drafts."),
    with_cover_letter: bool = typer.Option(False, "--cover-letter", help="Also generate an editable cover-letter draft."),
    questions_file: str = typer.Option("", "--questions-file", help="Optional UTF-8 text file with one application question per line."),
):
    """Prepare evidence-grounded local drafts; never opens URLs or submits applications."""
    try:
        result = prepare_application_materials(
            job_id, output_dir=Path(output_dir), include_cover_letter=with_cover_letter,
            questions_path=Path(questions_file) if questions_file else None,
        )
    except (KeyError, OSError, ValueError) as exc:
        console.print(f"[red]Could not prepare materials: {exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"Preparation report: {result['improvement_report_path']}")
    if result["cover_letter_path"]:
        console.print(f"Cover-letter draft: {result['cover_letter_path']}")
    if result["question_answers_path"]:
        console.print(f"Question-answer draft: {result['question_answers_path']}")
    console.print(f"Draft record #{result['draft_id']} saved for job #{job_id}; tracker status remains {result['status_unchanged']}.")
    console.print("Review and edit all files before any use. No URL was opened and no application was submitted.")


@app.command("check-application-quality")
def check_application_quality_command(
    job_id: int = typer.Argument(..., min=1, help="Tracker job whose saved application drafts should be checked."),
    draft_id: int = typer.Option(0, "--draft-id", min=0, help="Optional saved draft record ID; defaults to the latest for this job."),
    output_dir: str = typer.Option("data/application_quality", "--output-dir", help="Local directory for the quality report."),
):
    """Check saved drafts against local resume and job evidence; no site actions occur."""
    try:
        result = check_application_quality(job_id, draft_id=draft_id or None, output_dir=Path(output_dir))
    except (KeyError, OSError, ValueError) as exc:
        console.print(f"[red]Could not check application draft quality: {exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"Quality report: {result['report_path']}")
    console.print(f"Job #{job_id}; preparation record #{result['draft_id']}; confirmed issues: {result['confirmed_issue_count']}; items needing your review: {result['uncertain_count']}.")
    for item in result["findings"]:
        console.print(f"{item['classification']} — {item['section']}: {item['finding']}")
    console.print("Local check only; drafts, resume, tracker status, and Naukri were not changed.")


@app.command("application-review-pack")
def application_review_pack_command(
    job_id: int = typer.Argument(..., min=1, help="Existing tracker job ID."),
    output_dir: str = typer.Option("data/application_review_packs", "--output-dir", help="Local directory for the Markdown review pack."),
):
    """Create a consolidated local review pack; tracker status remains unchanged."""
    try:
        result = create_application_review_pack(job_id, output_dir=Path(output_dir))
    except (KeyError, OSError, ValueError) as exc:
        console.print(f"[red]Could not create review pack: {exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"Review pack #{result['pack_id']}: {result['report_path']}")
    console.print(f"Job #{job_id}; tracker status remains {result['status_unchanged']}; preparation draft #{result['draft_id'] or 'not available'}; quality report #{result['quality_report_id'] or 'not available'}.")
    console.print("No URLs were opened; no application or profile action was performed.")


@app.command("application-review-decision")
def application_review_decision_command(
    job_id: int = typer.Argument(..., min=1, help="Job ID with a previously generated review pack."),
    decision: str = typer.Argument(..., help="Explicit decision: " + ", ".join(REVIEW_DECISIONS)),
    note: str = typer.Option("", "--note", help="Optional short local note; do not include sensitive information."),
):
    """Append your review decision without changing application-tracker status."""
    try:
        result = record_application_review_decision(job_id, decision, note=note)
    except (KeyError, ValueError) as exc:
        console.print(f"[red]Could not record review decision: {exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"Decision {result['decision']!r} recorded as #{result['review_id']} for review pack #{result['pack_id']}.")
    console.print(f"Tracker status remains {result['status_unchanged']}; no application was submitted.")


@app.command("tracker-show")
def tracker_show_command(job_id: int = typer.Argument(..., min=1)):
    """Show listing details, match evidence, status history, and notes."""
    from .application_tracker import _job_details
    try:
        detail = _job_details(job_id)
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    job, tracker, evaluation = detail["job"], detail["tracker"], detail["evaluation"]
    console.print(f"Job #{job_id}: {job['title']}")
    console.print(f"Company: {job['company'] or 'not supplied'}; location: {job['location'] or 'not supplied'}")
    console.print(f"Source URL: {job['url'] or 'not supplied'}")
    console.print(f"Status: {tracker['status']} (status changed at {tracker['status_changed_at']})")
    if evaluation:
        console.print(f"Match score: {evaluation['score']}/100 — heuristic, not a hiring probability.")
        for reason in evaluation["reasons"]:
            console.print(f"Match reason: {reason}")
        for item in evaluation["missing_requirements"]:
            console.print(f"Not found in resume text (uncertain): {item}")
        for item in evaluation["uncertainties"]:
            console.print(f"Uncertain: {item}")
        for item in evaluation["mismatches"]:
            console.print(f"Explicit mismatch: {item}")
    else:
        console.print("Match score: not available")
    console.print("Experience requirement: " + (job["experience_requirement"] or "not supplied"))
    console.print("Salary: " + (job["salary"] or "not supplied"))
    console.print("Job description:\n" + (job["description"] or "not supplied"))
    for item in detail["status_history"]:
        console.print(f"Status history: {item['changed_at']} {item['from_status'] or '—'} -> {item['to_status']}")
    for item in detail["notes"]:
        console.print(f"Note ({item['created_at']}): {item['note']}")


@app.command("review-job")
def review_job_command(
    job_id: int = typer.Argument(..., min=1),
    note: str = typer.Option("", help="Optional concise note; avoid sensitive personal details."),
):
    """Display the description and gaps, then record a human review confirmation."""
    from .application_tracker import _job_details
    try:
        detail = _job_details(job_id)
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    job, evaluation = detail["job"], detail["evaluation"]
    console.print(f"Review job #{job_id}: {job['title']}")
    console.print(f"Source URL (not opened): {job['url'] or 'not supplied'}")
    console.print("Job description:\n" + (job["description"] or "not supplied"))
    if evaluation:
        console.print(f"Match score: {evaluation['score']}/100 — heuristic, not a hiring probability.")
        console.print("Missing requirements: " + ("; ".join(evaluation["missing_requirements"]) or "none recorded"))
        console.print("Uncertainties: " + ("; ".join(evaluation["uncertainties"]) or "none recorded"))
        console.print("Reasons: " + ("; ".join(evaluation["reasons"]) or "none recorded"))
    else:
        console.print("No saved match evaluation is available.")
    if not job["description"] or not evaluation:
        console.print("Review cannot be recorded: description and saved evaluation are both required.")
        raise typer.Exit(code=1)
    if not typer.confirm("After reading the description and requirements above, confirm that you reviewed them?", default=False):
        console.print("Review not recorded. Status unchanged.")
        return
    try:
        review_id = record_job_review(job_id, confirmed=True, note=note)
    except (KeyError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"Review confirmation recorded as #{review_id}. No application was submitted.")


@app.command("tracker-status")
def tracker_status_command(
    job_id: int = typer.Argument(..., min=1),
    status: str = typer.Argument(..., help="Exact status: " + ", ".join(TRACKER_STATUSES)),
    note: str = typer.Option("", help="Optional note to append to local history."),
    confirm_self_applied: bool = typer.Option(False, "--confirm-self-applied", help="Confirm that you personally submitted this application."),
):
    """Update a local tracker status. This command never submits an application."""
    try:
        result = update_job_status(job_id, status, note=note, applied_confirmed=confirm_self_applied)
    except (KeyError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"Job #{job_id} status: {result['tracker']['status']} (recorded {result['tracker']['status_changed_at']}).")
    if status == "Applied":
        console.print("Applied is recorded from your explicit confirmation; this command did not apply.")


@app.command("tracker-export")
def tracker_export_command(
    output: str = typer.Option("data/application_tracker.csv", "--output", "-o"),
):
    """Export local tracker state, status history, match reasons, and notes to CSV."""
    try:
        count = export_tracker(Path(output))
    except OSError as exc:
        console.print(f"[red]Could not export tracker: {exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"Exported {count} tracked job(s) to {output}")

@app.command("rank-jobs")
def rank_jobs():
    """Score saved jobs against the configured PDF resume and show the ranking."""
    resume = extract_resume_text(settings.resume_path)
    from .matching import score_job
    with connect() as conn:
        rows = conn.execute("SELECT id,title,description FROM jobs").fetchall()
        for row in rows:
            score, reason = score_job(resume, row["description"], row["title"])
            conn.execute(
                "UPDATE jobs SET match_score=?, match_reason=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (score, reason, row["id"]),
            )
    ranked = list_jobs(1000)
    table = Table("Score", "Title", "Company", "Location", "URL")
    for job in sorted(ranked, key=lambda item: item["match_score"] or 0, reverse=True):
        table.add_row(str(job["match_score"] or 0), job["title"], job["company"], job["location"], job["url"])
    console.print(table)
    console.print("Scores are keyword-overlap indicators, not hiring probabilities.")

@app.command()
def score(description_file: str, title: str = ""):
    """Score a local job-description text file against the configured resume."""
    resume = extract_resume_text(settings.resume_path)
    from pathlib import Path
    description = Path(description_file).read_text(encoding="utf-8")
    value, reason = score_job(resume, description, title)
    console.print(f"Baseline match score: {value}/100")
    console.print(reason)

if __name__ == "__main__":
    app()
