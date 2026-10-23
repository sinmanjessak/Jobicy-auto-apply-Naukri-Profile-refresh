"""Read-only profile health snapshots based on saved inspection evidence."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import settings
from .db import latest_profile_section_reviews, latest_resume_verification, save_profile_health_report, save_profile_review_report
from .diagnostics import log_event
from .upload import UploadWorkflowError, validate_pdf
from .resume_verification import CONTENT_VERIFIED, FILENAME_MATCHES, LOCAL_VALID, SITE_OBSERVED, UNVERIFIED, inspect_local_pdf

PROFILE_SECTIONS = (
    "Resume", "Resume headline", "Key skills", "Employment", "Education",
    "IT skills", "Projects", "Profile summary", "Accomplishments",
    "Career profile", "Personal details",
)
REPORT_STATES = {
    "Not checked", "Checked", "Missing", "Needs review", "Unverified",
    "Update performed", "Update verified",
}
PROFILE_REVIEW_STATES = ("Not checked", "Checked", "Missing", "Needs review", "Unverified")
DEFAULT_INSPECTION_PATH = Path("data") / "site_inspection" / "latest.json"

_ALIASES = {
    "Resume": ("resume", "upload resume", "download resume", "delete resume"),
    "Resume headline": ("resume headline", "edit resume headline"),
    "Key skills": ("key skills", "edit key skills"),
    "Employment": ("employment", "add employment"),
    "Education": ("education", "add education", "add doctorate/phd"),
    "IT skills": ("it skills", "add it skills"),
    "Projects": ("projects", "add project"),
    "Profile summary": ("profile summary", "edit profile summary"),
    "Accomplishments": (
        "accomplishments", "add online profile", "add work sample",
        "add publication", "add presentation", "add patent", "add certification",
    ),
    "Career profile": ("career profile", "edit career profile"),
    "Personal details": ("personal details", "edit personal details"),
}


def _labels(inventory: dict) -> list[str]:
    controls = inventory.get("controls", {})
    labels: list[str] = []
    for group in ("buttons", "links", "inputs"):
        items = controls.get(group, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                value = item.get("label", "")
                if isinstance(value, str):
                    labels.append(" ".join(value.casefold().split()))
    return labels


def _section_evidence_count(section: str, labels: list[str]) -> int:
    aliases = _ALIASES[section]
    count = 0
    for label in labels:
        if any(label == alias or label.startswith(alias + ":") for alias in aliases):
            count += 1
    return count


def _resume_availability(path: Path) -> dict:
    if not path.is_file():
        return {"status": "Missing", "exists": False, "readable_pdf": False, "size_bytes": 0}
    try:
        size = path.stat().st_size
    except OSError:
        return {"status": "Needs review", "exists": True, "readable_pdf": False, "size_bytes": 0}
    try:
        validated = validate_pdf(path)
        valid = validated["size"] == size
    except UploadWorkflowError:
        valid = False
    return {
        "status": "Checked" if valid else "Needs review",
        "exists": True,
        "readable_pdf": valid,
        "size_bytes": size,
    }


def _reliable_notice_observation(inventory: dict | None) -> str | None:
    """Accept only explicit, visible, unique structured field evidence.

    Current UI inventories deliberately omit field values, so the current saved
    inventory normally yields None here.
    """
    if not isinstance(inventory, dict):
        return None
    fields = inventory.get("observed_profile_data")
    observation = fields.get("notice_period") if isinstance(fields, dict) else None
    if not isinstance(observation, dict):
        return None
    value = observation.get("value")
    label = observation.get("label")
    if (
        observation.get("visible") is True
        and observation.get("unique") is True
        and isinstance(label, str)
        and "notice period" in " ".join(label.casefold().split())
        and isinstance(value, str)
        and value.strip()
    ):
        return value.strip()[:120]
    return None


def make_section_review_records(statuses: dict[str, str], notes: dict[str, str], reviewed_at: str | None = None) -> list[dict]:
    """Validate human observations without interpreting their evidence notes."""
    timestamp = reviewed_at or datetime.now(ZoneInfo(settings.timezone)).isoformat(timespec="seconds")
    if set(statuses) != set(PROFILE_SECTIONS) or set(notes) != set(PROFILE_SECTIONS):
        raise ValueError("A status and evidence note are required for each profile section")
    records = []
    for section in PROFILE_SECTIONS:
        status = statuses[section]
        if status not in PROFILE_REVIEW_STATES:
            raise ValueError(f"Invalid review status for {section}")
        note = notes[section]
        if not isinstance(note, str):
            raise ValueError(f"Evidence note for {section} must be text")
        note = " ".join(note.split())
        if not note:
            raise ValueError(f"Evidence note for {section} is required")
        if len(note) > 240:
            raise ValueError(f"Evidence note for {section} must be 240 characters or fewer")
        records.append({
            "section_name": section, "status": status, "reviewed_at": timestamp,
            "evidence_note": note,
        })
    return records


def build_profile_health_report(
    inventory: dict | None,
    *,
    resume_path: Path = Path("data") / "resume.pdf",
    checked_at: str | None = None,
    inspection_state: str | None = None,
    section_reviews: dict[str, dict] | None = None,
    resume_verification: dict | None = None,
) -> dict:
    """Build a report from local file checks and label-only inspection evidence."""
    if inventory is not None and not isinstance(inventory.get("controls"), dict):
        raise ValueError("Inspection must contain a controls object")
    inspection_status = inspection_state or ("Checked" if inventory is not None else "Unverified")
    labels = _labels(inventory) if inventory is not None else []
    sections = []
    for section in PROFILE_SECTIONS:
        evidence_count = _section_evidence_count(section, labels)
        if inventory is None:
            state = "Unverified"
            note = "No valid saved inspection is available; section presence was not checked."
        elif evidence_count:
            state = "Needs review"
            note = "A section label was observed; content completeness and accuracy were not inspected."
        else:
            state = "Not checked"
            note = "No section evidence was captured; absence cannot be inferred from this inspection."
        item = {"name": section, "status": state, "observed_label_count": evidence_count, "note": note}
        review = (section_reviews or {}).get(section)
        if isinstance(review, dict) and review.get("status") in PROFILE_REVIEW_STATES:
            item.update({
                "status": review["status"],
                "reviewed_at": review.get("reviewed_at"),
                "evidence_note": review.get("evidence_note", ""),
                "evidence_source": "user_confirmed",
                "independent_verification": False,
                "note": "User-confirmed observation; the automation did not independently inspect section values.",
            })
        sections.append(item)

    resume = _resume_availability(Path(resume_path))
    resume_section = next(item for item in sections if item["name"] == "Resume")
    if section_reviews and isinstance(section_reviews.get("Resume"), dict):
        profile_resume_state = section_reviews["Resume"].get("status", "Unverified")
    elif inventory is None:
        profile_resume_state = "Unverified"
    elif resume_section["observed_label_count"]:
        profile_resume_state = "Needs review"
    else:
        profile_resume_state = "Not checked"
    resume.update({
        "canonical_path": "data/resume.pdf",
        "profile_section_status": profile_resume_state,
    })
    verification = resume_verification or {}
    current_local = inspect_local_pdf(Path(resume_path))
    local_hash_is_current = bool(
        current_local.get("local_status") == LOCAL_VALID
        and verification.get("local_sha256")
        and verification.get("local_sha256") == current_local.get("local_sha256")
    )
    content_status = verification.get("content_status", UNVERIFIED)
    if content_status == CONTENT_VERIFIED and not local_hash_is_current:
        content_status = UNVERIFIED
    filename_status = verification.get("filename_status", UNVERIFIED)
    if verification.get("local_filename") and verification.get("local_filename") != current_local.get("local_filename"):
        filename_status = UNVERIFIED
    site_status = verification.get("site_status", UNVERIFIED)
    if site_status != SITE_OBSERVED:
        site_status = UNVERIFIED
    verification_status = (
        CONTENT_VERIFIED if content_status == CONTENT_VERIFIED else
        FILENAME_MATCHES if filename_status == FILENAME_MATCHES else
        SITE_OBSERVED if site_status == SITE_OBSERVED else UNVERIFIED
    )
    resume["verification_status"] = verification_status
    resume["verification"] = {
        "local_status": LOCAL_VALID if current_local.get("local_status") == LOCAL_VALID else UNVERIFIED,
        "local_filename": current_local.get("local_filename"),
        "local_size_bytes": current_local.get("local_size_bytes", 0),
        "local_sha256": current_local.get("local_sha256"),
        "site_status": site_status,
        "filename_status": filename_status,
        "content_status": content_status,
        "checked_at": verification.get("checked_at"),
        "displayed_filename": verification.get("displayed_filename"),
        "displayed_upload_date": verification.get("displayed_upload_date"),
        "evidence_source": verification.get("evidence_source"),
        "independent_verification": bool(verification.get("independent_verification", False)),
        "note": "Site details are user-confirmed; content is verified only by matching SHA-256 of a user-provided downloaded PDF.",
    }
    resume["verification_note"] = "Local validation or filename equality alone does not prove the site resume content; see evidence states."

    observed_notice = _reliable_notice_observation(inventory)
    if observed_notice is None:
        notice_comparison = {
            "status": "Unverified", "preference": settings.notice_period_preference,
            "observed": None, "conflict": False,
            "note": "The saved inspection does not contain a uniquely identified notice-period value.",
        }
    else:
        conflict = observed_notice.casefold() != settings.notice_period_preference.casefold()
        notice_comparison = {
            "status": "Needs review" if conflict else "Checked",
            "preference": settings.notice_period_preference,
            "observed": observed_notice,
            "conflict": conflict,
            "note": "Observed value conflicts with the configured preference; verify manually." if conflict
                    else "Observed value matches the configured preference.",
        }

    if resume["status"] == "Missing":
        overall = "Missing"
    elif any(item["status"] in {"Missing", "Needs review", "Not checked"} for item in sections) or notice_comparison["conflict"]:
        overall = "Needs review"
    elif "Unverified" in {inspection_status, resume["verification_status"], notice_comparison["status"]}:
        overall = "Unverified"
    else:
        overall = "Checked"

    return {
        "checked_at": checked_at or datetime.now(ZoneInfo(settings.timezone)).isoformat(timespec="seconds"),
        "timezone": settings.timezone,
        "overall_status": overall,
        "inspection_status": inspection_status,
        "resume": resume,
        "sections": sections,
        "preferences": {
            "notice_period": settings.notice_period_preference,
            "salary": settings.salary_preference,
            "target_roles": list(settings.target_roles),
        },
        "notice_period_comparison": notice_comparison,
        "profile_update": {
            "status": "Unverified", "performed": False, "verified": False,
            "note": "This report is read-only; no profile update was attempted.",
        },
        "state_legend": {
            "Not checked": "No review evidence was recorded for this section.",
            "Checked": "Evidence was checked; this does not imply profile content is complete.",
            "Missing": "Required local file or inspected section evidence was absent.",
            "Needs review": "Evidence exists or a conflict was found, but a person must review it.",
            "Update performed": "A profile-changing action was recorded; not used by this read-only report.",
            "Update verified": "A saved change was independently confirmed; not used by this read-only report.",
            "Unverified": "Available evidence cannot establish the state.",
            LOCAL_VALID: "The canonical local PDF passed the existing readable, unencrypted PDF preflight.",
            SITE_OBSERVED: "A user confirmed that a resume is displayed; automation did not inspect its values.",
            FILENAME_MATCHES: "The user-confirmed displayed filename matches the local basename; content may differ.",
            CONTENT_VERIFIED: "The local PDF and a user-provided downloaded PDF have identical SHA-256 hashes.",
        },
    }


def generate_profile_health_report(
    *,
    inspection_path: Path = DEFAULT_INSPECTION_PATH,
    resume_path: Path | None = None,
    database_path: Path | None = None,
    section_reviews: dict[str, dict] | None = None,
    review_records: list[dict] | None = None,
) -> tuple[dict, int]:
    """Read saved evidence, build the report, and persist a timestamped snapshot."""
    inventory = None
    inspection_state = "Missing"
    if inspection_path.is_file():
        try:
            candidate = json.loads(inspection_path.read_text(encoding="utf-8"))
            if not isinstance(candidate, dict) or not isinstance(candidate.get("controls"), dict):
                raise ValueError("invalid inspection structure")
            inventory = candidate
            inspection_state = "Checked"
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            inspection_state = "Needs review"
            log_event("profile_health_inspection", "failed", error_type=type(exc).__name__)
    if section_reviews is None:
        try:
            section_reviews = latest_profile_section_reviews(database_path)
        except Exception as exc:
            log_event("profile_health_review_load", "failed", error_type=type(exc).__name__)
            section_reviews = {}
    try:
        resume_verification = latest_resume_verification(database_path, require_site_check=True)
    except Exception as exc:
        log_event("profile_health_resume_verification_load", "failed", error_type=type(exc).__name__)
        resume_verification = None
    report = build_profile_health_report(
        inventory,
        resume_path=resume_path or settings.resume_path,
        inspection_state=inspection_state,
        section_reviews=section_reviews,
        resume_verification=resume_verification,
    )
    try:
        if review_records is not None:
            report_id = save_profile_review_report(report, review_records, database_path=database_path)
        else:
            report_id = save_profile_health_report(report, database_path=database_path)
    except Exception as exc:
        log_event("profile_health_persistence", "failed", error_type=type(exc).__name__)
        raise RuntimeError("Could not save the profile-health report to SQLite.") from None
    report["history_id"] = report_id
    log_event(
        "profile_health_report", "saved", section_count=len(report["sections"]),
        resume_available=report["resume"]["exists"], overall_needs_review=report["overall_status"] == "Needs review",
    )
    return report, report_id


def render_profile_health_summary(report: dict) -> str:
    """Create a concise human-readable summary without profile content values."""
    counts = {state: sum(section["status"] == state for section in report["sections"])
              for state in ("Not checked", "Checked", "Missing", "Needs review", "Unverified")}
    resume = report["resume"]
    notice = report["notice_period_comparison"]
    return "\n".join((
        f"Profile health: {report['overall_status']} (history #{report.get('history_id', 'not saved')})",
        f"Checked at: {report['checked_at']} ({report['timezone']})",
        f"Canonical resume: {resume['status']} locally, {resume['profile_section_status']} in profile UI; verification {resume['verification_status']}.",
        f"Sections: {counts['Checked']} checked, {counts['Needs review']} need review, {counts['Missing']} missing, {counts['Not checked']} not checked, {counts['Unverified']} unverified.",
        f"Notice period: preference {notice['preference']}; comparison {notice['status']}. {notice['note']}",
        f"Salary preference: {report['preferences']['salary']}; target roles configured: {len(report['preferences']['target_roles'])}.",
        "Profile update: Unverified; this report made no changes.",
    ))
