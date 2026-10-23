import json
from pathlib import Path
from urllib.parse import urlsplit
from datetime import datetime
from zoneinfo import ZoneInfo

from rich.console import Console
from .browser import BrowserStartupError, open_naukri
from .db import record_run
from .inspection import sanitize_url, save_inventory, verify_profile_refresh_inventory
from .upload import UploadWorkflowError, upload_resume_on_page, validate_pdf
from .diagnostics import log_event
from .config import settings
from .profile_health import PROFILE_SECTIONS, PROFILE_REVIEW_STATES, generate_profile_health_report, make_section_review_records, render_profile_health_summary
from .db import save_resume_verification
from .resume_verification import CANONICAL_RESUME_PATH, LOCAL_VALID, classify_resume_verification, inspect_local_pdf

console = Console()


def inspect_profile(headless: bool = False):
    """Open a visible browser for manual navigation, then save a read-only UI inventory."""
    pw = context = None
    try:
        pw, context, page = open_naukri(headless=headless)
        console.print("[bold]Naukri opened in a visible browser.[/bold]")
        console.print("1. Sign in manually if needed.")
        console.print("2. Navigate manually to your profile/resume page.")
        console.print("3. Return here and press Enter to capture visible UI control labels.")
        console.print("No profile fields are changed, no buttons are clicked, and no applications are submitted.")
        if not headless:
            input("Press Enter after you are on the profile/resume page...")
        output = save_inventory(page)
        console.print(f"[green]Saved read-only UI inventory:[/green] {output}")
        console.print(f"Page title: {page.title()}")
        console.print(f"Page URL (credentials/query/fragment removed): {sanitize_url(page.url)}")
        console.print("Review the local JSON before sharing it; page labels and link paths may still reveal information.")
    except BrowserStartupError as exc:
        record_run("profile_inspection", "error", exc.stage)
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None
    except Exception as exc:
        record_run("profile_inspection", "error", type(exc).__name__)
        raise
    finally:
        if context:
            context.close()
        if pw:
            pw.stop()


def _inspected_profile_url() -> str:
    """Build a trusted profile URL from the saved, previously observed route."""
    inventory_path = Path("data") / "site_inspection" / "latest.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    parts = urlsplit(inventory.get("page_url", ""))
    host = (parts.hostname or "").casefold()
    if (parts.scheme != "https" or not (host == "naukri.com" or host.endswith(".naukri.com"))
            or parts.path != "/mnjuser/profile" or parts.username or parts.password):
        raise RuntimeError("The saved inspection does not contain the expected Naukri profile route. Capture a fresh inspection first.")
    return f"https://{parts.netloc}/mnjuser/profile"


def review_profile_read_only() -> dict:
    """Open the visible profile and collect only human-confirmed section reviews."""
    pw = context = None
    try:
        profile_url = _inspected_profile_url()
        log_event("profile_review", "started")
        pw, context, page = open_naukri(headless=False)
        current = urlsplit(page.url)
        if current.path != "/mnjuser/profile":
            try:
                page.goto(profile_url, wait_until="domcontentloaded", timeout=settings.browser_navigation_timeout_ms)
            except Exception as exc:
                log_event("profile_review_navigation", "failed", error_type=type(exc).__name__)
                raise RuntimeError("Navigation to the inspected profile route failed; no review report was saved.") from None

        console.print("Read-only profile review. The automation will not inspect profile values or click page controls.")
        console.print("Browser instructions: if asked, sign in manually and complete any OTP/CAPTCHA yourself; open the Profile page (/mnjuser/profile); visually review each section; do not click Edit, Update, Save, Upload, Delete, Apply, or submission controls.")
        input("When the visible browser shows your profile page, return here and press Enter...")
        current = urlsplit(page.url)
        host = (current.hostname or "").casefold()
        if not (host == "naukri.com" or host.endswith(".naukri.com")) or current.path != "/mnjuser/profile":
            log_event("profile_review_page_check", "failed")
            raise RuntimeError("Profile route was not confirmed. No review report was saved; navigate manually to /mnjuser/profile and retry.")

        statuses: dict[str, str] = {}
        notes: dict[str, str] = {}
        choice_map = {str(i + 1): state for i, state in enumerate(PROFILE_REVIEW_STATES)}
        for section in PROFILE_SECTIONS:
            console.print(f"\n[bold]{section}[/bold]")
            console.print("1 Not checked | 2 Checked | 3 Missing | 4 Needs review | 5 Unverified")
            while True:
                choice = input("Status number: ").strip()
                if choice in choice_map:
                    statuses[section] = choice_map[choice]
                    break
                console.print("Enter a number from 1 to 5.")
            note = input("Concise evidence note (do not enter personal values; max 240 characters): ").strip()
            if statuses[section] == "Not checked" and not note:
                note = "Not reviewed during this session."
            notes[section] = note

        records = make_section_review_records(statuses, notes)
        section_reviews = {
            row["section_name"]: {
                "status": row["status"], "reviewed_at": row["reviewed_at"],
                "evidence_note": row["evidence_note"],
            }
            for row in records
        }
        report, report_id = generate_profile_health_report(
            section_reviews=section_reviews, review_records=records,
        )
        console.print("\n" + render_profile_health_summary(report))
        console.print(f"Saved user-confirmed read-only review as report #{report_id}.")
        console.print("Section observations are user-confirmed; automation did not independently verify section content. No profile changes were made.")
        log_event("profile_review", "saved", section_count=len(records))
        record_run("profile_review", "saved", f"report_id={report_id}")
        return report
    except (BrowserStartupError, OSError, json.JSONDecodeError, RuntimeError, ValueError) as exc:
        log_event("profile_review", "failed", error_type=type(exc).__name__)
        console.print(f"[red]{exc}[/red]")
        raise
    except Exception as exc:
        # Playwright exception text may include a full URL or page data; never display it.
        log_event("profile_review", "failed", error_type=type(exc).__name__)
        console.print("[red]Read-only profile review failed during browser navigation or local persistence. No profile changes were requested.[/red]")
        raise RuntimeError("Read-only profile review failed; see the safe stage diagnostics and retry.") from None
    finally:
        if context:
            context.close()
        if pw:
            pw.stop()


def verify_resume_read_only(*, local_only: bool = False) -> dict:
    """Verify local PDF metadata and collect optional user-confirmed page evidence."""
    pw = context = None
    try:
        log_event("resume_verification", "started", dry_run=True)
        local = inspect_local_pdf(CANONICAL_RESUME_PATH)
        if local["local_status"] == LOCAL_VALID:
            console.print(f"Local file valid: {local['local_filename']} ({local['local_size_bytes']} bytes)")
            console.print(f"Local SHA-256: {local['local_sha256']}")
        else:
            console.print(f"Local file validation Unverified ({local['local_error_code']}); site verification will not be attempted.")

        site_observed = False
        site_check_performed = False
        displayed_filename = ""
        displayed_upload_date = ""
        downloaded_copy = None
        if local["local_status"] == LOCAL_VALID and not local_only:
            try:
                profile_url = _inspected_profile_url()
            except (OSError, json.JSONDecodeError, RuntimeError):
                profile_url = None
            if profile_url:
                pw, context, page = open_naukri(headless=False)
                current = urlsplit(page.url)
                if current.path != "/mnjuser/profile":
                    try:
                        page.goto(profile_url, wait_until="domcontentloaded", timeout=settings.browser_navigation_timeout_ms)
                    except Exception as exc:
                        log_event("resume_verification_navigation", "failed", error_type=type(exc).__name__)
                        page = None
                if page is not None:
                    console.print("Visible browser opened with the existing persistent profile. Sign in manually if asked, complete any OTP/CAPTCHA yourself, and inspect the Resume section. Do not edit, update, save, upload, delete, apply, or submit anything.")
                    console.print("Optional content check is manual only: the automation will not click Download. If you later choose it, you will be explicitly asked first.")
                    input("When the profile page is visible, return to PowerShell and press Enter...")
                    current = urlsplit(page.url)
                    host = (current.hostname or "").casefold()
                    if (host == "naukri.com" or host.endswith(".naukri.com")) and current.path == "/mnjuser/profile":
                        site_check_performed = True
                        site_observed = input("Do you see a resume displayed in the Resume section? [y/N]: ").strip().casefold() in {"y", "yes"}
                        if site_observed:
                            displayed_filename = input("Displayed resume filename (exactly as shown; blank if unavailable): ").strip()
                            displayed_upload_date = input("Displayed upload date (if shown; otherwise blank): ").strip()
                            optional_download = input("Optional byte-for-byte comparison? Type y only if you choose to manually download the displayed resume: ").strip().casefold() in {"y", "yes"}
                            if optional_download:
                                console.print("In the visible browser, manually use the site's displayed resume download, save the file, then return here and enter its local path. No download control will be clicked by automation.")
                                user_path = input("Path to the manually downloaded PDF (blank to skip): ").strip().strip('"')
                                downloaded_copy = Path(user_path).expanduser() if user_path else None
                    else:
                        console.print("The profile route could not be confirmed. Site evidence remains Unverified; no page contents were read.")
            else:
                console.print("No trusted profile route is available from the saved inspection. Run inspect-profile first; site evidence remains Unverified.")

        verification = classify_resume_verification(
            local,
            site_check_performed=site_check_performed,
            site_resume_observed=site_observed,
            displayed_filename=displayed_filename,
            displayed_upload_date=displayed_upload_date,
            downloaded_copy=downloaded_copy,
        )
        verification["checked_at"] = datetime.now(ZoneInfo(settings.timezone)).isoformat(timespec="seconds")
        log_event(
            "resume_site_evidence", "captured" if verification["site_resume_observed"] else "unverified",
            site_check_performed=verification["site_check_performed"],
            site_resume_observed=verification["site_resume_observed"],
            filename_matches=verification["filename_matches"],
            content_hash_matches=verification["content_verified"],
        )
        verification_id = save_resume_verification(verification)
        verification["id"] = verification_id
        report, report_id = generate_profile_health_report()
        log_event(
            "resume_verification", "saved", local_file_valid=verification["local_status"] == LOCAL_VALID,
            site_resume_observed=verification["site_resume_observed"], filename_matches=verification["filename_matches"],
            content_hash_matches=verification["content_verified"], content_verified=verification["content_verified"],
            verification_record_id=verification_id,
        )
        record_run("resume_verification", "saved", f"verification_id={verification_id};report_id={report_id}")
        console.print(f"\nResume verification #{verification_id}; profile-health report #{report_id}")
        console.print(f"Local file: {verification['local_status']}")
        console.print(f"Site evidence: {verification['site_status']} (user-confirmed; not independently verified)")
        if verification["displayed_filename"]:
            console.print(f"Displayed filename: {verification['displayed_filename']}")
        if verification["displayed_upload_date"]:
            console.print(f"Displayed upload date: {verification['displayed_upload_date']}")
        console.print(f"Filename comparison: {verification['filename_status']}")
        console.print(f"Content comparison: {verification['content_status']}")
        console.print(f"Profile-health resume verification: {report['resume']['verification_status']}")
        return verification
    except Exception as exc:
        log_event("resume_verification", "failed", error_type=type(exc).__name__)
        if isinstance(exc, (RuntimeError, ValueError)):
            console.print(f"[red]{exc}[/red]")
            raise
        console.print("[red]Read-only resume verification failed. No profile changes were requested; see safe stage diagnostics.[/red]")
        raise RuntimeError("Read-only resume verification failed.") from None
    finally:
        if context:
            context.close()
        if pw:
            pw.stop()


def dry_run_apply():
    console.print("Dry-run only: application submission is disabled.")
    console.print("No forms were filled and no applications were submitted.")
    record_run("apply", "dry_run", "No forms submitted")


def verify_profile_refresh(path: Path | None = None) -> dict:
    """Read a saved UI inventory and report candidate labels without browser actions."""
    source = path or (Path("data") / "site_inspection" / "latest.json")
    inventory = json.loads(source.read_text(encoding="utf-8"))
    return verify_profile_refresh_inventory(inventory)


def run_resume_upload(*, live: bool = False, file_selection_test: bool = False) -> dict:
    """Open a visible persistent context for dry-run or selection diagnostics only."""
    if live and file_selection_test:
        raise ValueError("live upload and file-selection test modes are mutually exclusive")
    mode = "live" if live else "file_selection_test" if file_selection_test else "dry_run"
    if live:
        log_event("live_upload", "disabled")
        console.print("Live upload is disabled until post-upload verification is validated against the website. No browser was opened and no file was attached.")
        return {"mode": mode, "status": "disabled", "upload_performed": False}
    # Validate the fixed project resume before launching/interacting with a page.
    pw = context = None

    def pause_for_review() -> None:
        try:
            input("Inspect the visible page; press Enter to close this browser session...")
        except EOFError:
            console.print("No console input is available; closing the browser context.")

    try:
        initial_pdf = validate_pdf(Path("data") / "resume.pdf")
        log_event("browser_session", "started", live=live, dry_run=not live)
        pw, context, page = open_naukri(headless=False)
        console.print("Visible Naukri browser opened with the existing persistent profile.")
        console.print("Use the already signed-in session. If needed, navigate manually to /mnjuser/profile.")
        console.print("No automated sign-in, OTP, CAPTCHA, or security-check handling is performed.")
        if live:
            console.print("LIVE MODE: if all page and file checks pass, this will attach data/resume.pdf and click Update resume once.")
        elif file_selection_test:
            console.print("FILE-SELECTION DIAGNOSTIC: select data/resume.pdf in the inspected input and observe the page's file-change behavior. No Update click or explicit form submission is performed; normal page requests are not intercepted.")
        else:
            console.print("DRY RUN: the file will not be attached and no profile control will be clicked.")
        input("Press Enter after the visible browser is on your Naukri profile page...")
        current_pdf = validate_pdf(Path("data") / "resume.pdf")
        if current_pdf["size"] != initial_pdf["size"] or current_pdf["path"].stat().st_mtime_ns != initial_pdf["path"].stat().st_mtime_ns:
            raise UploadWorkflowError("pdf_preflight", "blocked", "data/resume.pdf changed during browser setup; no file was attached.")
        result = upload_resume_on_page(
            page, live=live, file_selection_test=file_selection_test,
            resume_path=Path("data") / "resume.pdf",
        )
        controls = result.pop("controls", {})
        file_controls = controls.get("fileInputs", [])
        update_controls = controls.get("updateControls", [])
        console.print(f"Visible file inputs: {len(file_controls)}; total file inputs in the page DOM: {controls.get('allFileInputCount', len(file_controls))}.")
        if file_controls:
            control = file_controls[0]
            console.print(f"File input: type={control.get('type')!r}, accessible name={control.get('accessibleName')!r}, accept={control.get('accept')!r}, visible={control.get('visible')}")
            console.print(f"File input form: method={control.get('formMethod')!r}, form id={control.get('formId')!r}")
        if update_controls:
            control = update_controls[0]
            console.print(f"Update control: tag={control.get('tag')!r}, type={control.get('type')!r}, role={control.get('role')!r}, accessible name={control.get('name')!r}, disabled={control.get('disabled')}, same form as file input={control.get('sameFormAsFileInput')}")
        console.print(f"Mode: {result['mode']}; status: {result['status']}; confirmation: {result['confirmation']}.")
        if file_selection_test:
            console.print(
                f"FileList check: count={result.get('selected_file_count', 0)}; "
                f"name_match={result.get('selected_name_match', False)}; "
                f"size_match={result.get('selected_size_match', False)}; "
                f"bytes={result.get('selected_file_size_bytes', 0)}."
            )
            if "immediate_selected_file_count" in result:
                console.print(
                    f"Immediately after set_input_files: count={result['immediate_selected_file_count']}; "
                    f"name_match={result['immediate_selected_name_match']}; "
                    f"size_match={result['immediate_selected_size_match']}; "
                    f"bytes={result['immediate_selected_file_size_bytes']}."
                )
            console.print("Selection diagnostic finished: no Update click or explicit form submission was performed. Page-request interception was not installed; inspect the visible result before closing.")
            pause_for_review()
        if result["status"] == "verified":
            console.print("The page showed a new success confirmation or the expected resume filename after the action.")
        elif result["status"] == "failed":
            console.print("The page showed an upload failure message.")
        elif live:
            console.print("Upload result unverified; no success is claimed. Check the visible page manually.")
        record_run("resume_upload", result["status"], json.dumps({k: v for k, v in result.items() if k != "controls"}, sort_keys=True))
        return result
    except UploadWorkflowError as exc:
        log_event(exc.stage, exc.status)
        record_run("resume_upload", exc.status, json.dumps({"stage": exc.stage}, sort_keys=True))
        console.print(f"[red]{exc}[/red]")
        if file_selection_test and context:
            console.print("No Update click or form submission was performed.")
            pause_for_review()
        return {"mode": mode, "status": exc.status, "stage": exc.stage, "upload_performed": False}
    except BrowserStartupError as exc:
        log_event("browser_session", "failed", error_type=type(exc).__name__, startup_stage=exc.stage)
        record_run("resume_upload", "failed", json.dumps({"stage": exc.stage}, sort_keys=True))
        console.print(f"[red]{exc}[/red]")
        return {"mode": mode, "status": "failed", "stage": exc.stage, "upload_performed": False}
    finally:
        if context:
            context.close()
        if pw:
            pw.stop()
