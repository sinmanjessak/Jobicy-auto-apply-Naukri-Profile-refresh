"""Local, read-only freshness summaries from saved review evidence."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .config import settings
from .db import (
    latest_profile_section_reviews, latest_resume_verification,
    save_profile_freshness_report,
)
from .diagnostics import log_event
from .profile_health import PROFILE_SECTIONS

FRESHNESS_CATEGORIES = ("Current", "Review due", "Stale", "Never reviewed", "Unverified")
_REVIEW_STATUSES = {"Checked", "Missing", "Needs review"}


def _timestamp(value: str | None, tz: ZoneInfo) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed.astimezone(tz)
    except (ValueError, OverflowError):
        return None


def _section_freshness(review: dict | None, now: datetime, current_days: int, due_days: int) -> str:
    if not review:
        return "Never reviewed"
    if review.get("status") not in _REVIEW_STATUSES or review.get("status") in {"Not checked", "Unverified"}:
        return "Unverified"
    evidence_source = review.get("evidence_source")
    independent = bool(review.get("independent_verification"))
    if not (independent or evidence_source == "user_confirmed"):
        return "Unverified"
    reviewed = _timestamp(review.get("reviewed_at"), now.tzinfo)
    if reviewed is None or reviewed > now:
        return "Unverified"
    age_days = (now - reviewed).total_seconds() / 86400
    if age_days <= current_days:
        return "Current"
    if age_days <= due_days:
        return "Review due"
    return "Stale"


def _resume_states(record: dict | None) -> dict:
    unverified = {
        "local_file": "Unverified", "site_resume": "Unverified",
        "filename_match": "Unverified", "content_comparison": "Unverified",
        "checked_at": None, "evidence_type": "none",
    }
    if not record or not record.get("site_check_performed"):
        return unverified
    observed = bool(record.get("site_resume_observed"))
    filename_matches = observed and bool(record.get("filename_matches"))
    # A label or stored status alone cannot establish byte equality. Require the
    # valid manually downloaded copy and exact hashes from the recorded check.
    content_matches = bool(
        observed and record.get("downloaded_copy_valid")
        and record.get("local_sha256") and record.get("downloaded_sha256")
        and record.get("local_sha256") == record.get("downloaded_sha256")
    )
    return {
        "local_file": "Local file valid" if record.get("local_status") == "Local file valid" else "Unverified",
        "site_resume": "Site resume observed" if observed else "Unverified",
        "filename_match": "Filename matches" if filename_matches else "Unverified",
        "content_comparison": "Content verified" if content_matches else "Unverified",
        "checked_at": record.get("checked_at"),
        "evidence_type": "user-confirmed" if not record.get("independent_verification") else "independently observed",
    }


def build_profile_freshness_report(
    reviews: dict[str, dict], resume_verification: dict | None, *,
    generated_at: str | None = None, current_days: int | None = None,
    due_days: int | None = None, timezone_name: str | None = None,
) -> dict:
    tz_name = timezone_name or settings.timezone
    tz = ZoneInfo(tz_name)
    current = settings.profile_review_current_days if current_days is None else current_days
    due = settings.profile_review_due_days if due_days is None else due_days
    if current < 1 or due <= current:
        raise ValueError("Freshness thresholds must satisfy 1 <= current days < review due days")
    now = _timestamp(generated_at, tz) if generated_at else datetime.now(tz)
    if now is None:
        raise ValueError("generated_at must be a valid timestamp")
    sections = []
    for name in PROFILE_SECTIONS:
        review = reviews.get(name)
        category = _section_freshness(review, now, current, due)
        status = review.get("status") if review else "Not checked"
        source = review.get("evidence_source") if review else None
        independent = bool(review.get("independent_verification")) if review else False
        evidence_type = (
            "independently observed" if independent else
            "user-confirmed" if source == "user_confirmed" else
            "unknown" if review else "none"
        )
        sections.append({
            "name": name, "last_reviewed_at": review.get("reviewed_at") if review else None,
            "evidence_type": evidence_type, "status": status,
            "freshness": category,
            "independent_verification": independent,
        })

    priority = {"Missing": 0, "Needs review": 1, "Stale": 2, "Review due": 3,
                "Unverified": 4, "Not checked": 4, "Never reviewed": 4, "Current": 5}
    actions = []
    for item in sections:
        status, category = item["status"], item["freshness"]
        if status == "Missing":
            reason = "Previously recorded as missing; visually confirm whether this remains true."
        elif status == "Needs review":
            reason = "A prior review flagged this section for manual review."
        elif category == "Stale":
            reason = "The last recorded review is older than the configured review-due window."
        elif category == "Review due":
            reason = "The last recorded review is outside the current window."
        elif category in {"Never reviewed", "Unverified"}:
            reason = "No reliable review evidence establishes the current state."
        else:
            continue
        actions.append({"section": item["name"], "priority": priority.get(status, priority.get(category, 9)),
                        "reason": reason})
    actions.sort(key=lambda item: (item["priority"], PROFILE_SECTIONS.index(item["section"])))
    preferences = {
        "notice_period": settings.notice_period_preference,
        "salary": settings.salary_preference,
        "target_roles": list(settings.target_roles),
    }
    return {
        "generated_at": now.isoformat(timespec="seconds"), "timezone": tz_name,
        "thresholds": {
            "current_days": current, "review_due_days": due,
            "label": "Project heuristics only; not official Naukri ranking rules. Review age does not establish accuracy or improve ranking.",
        },
        "sections": sections, "resume_verification": _resume_states(resume_verification),
        "preferences": preferences, "action_list": actions,
        "read_only": True,
        "evidence_note": "Historical reviews describe what was observed then; they do not independently establish current profile accuracy.",
    }


def generate_profile_freshness_report(*, database_path=None, generated_at: str | None = None) -> tuple[dict, int]:
    """Read saved SQLite evidence and append a report snapshot; never opens a browser."""
    try:
        reviews = latest_profile_section_reviews(database_path)
        resume = latest_resume_verification(database_path, require_site_check=True)
        report = build_profile_freshness_report(reviews, resume, generated_at=generated_at)
        report_id = save_profile_freshness_report(report, database_path)
    except Exception as exc:
        log_event("profile_freshness_report", "failed", error_type=type(exc).__name__)
        raise RuntimeError("Could not generate the local profile-freshness report.") from None
    report["history_id"] = report_id
    log_event("profile_freshness_report", "saved", section_count=len(report["sections"]),
              action_count=len(report["action_list"]), resume_site_observed=report["resume_verification"]["site_resume"] == "Site resume observed")
    return report, report_id


def render_profile_freshness_summary(report: dict) -> str:
    counts = {state: sum(row["freshness"] == state for row in report["sections"])
              for state in FRESHNESS_CATEGORIES}
    lines = [
        f"Profile freshness: report #{report.get('history_id', 'not saved')} — {report['generated_at']} ({report['timezone']})",
        "Sections: " + ", ".join(f"{state}: {counts[state]}" for state in FRESHNESS_CATEGORIES),
        "Section details:",
        *[
            f"- {row['name']}: last review {row['last_reviewed_at'] or 'never'}; "
            f"evidence {row['evidence_type']}; status {row['status']}; freshness {row['freshness']}."
            for row in report["sections"]
        ],
        "Resume: " + "; ".join(f"{key.replace('_', ' ')}: {value}" for key, value in report["resume_verification"].items() if key not in {"checked_at", "evidence_type"}),
        f"Resume evidence: {report['resume_verification']['evidence_type']}; checked at {report['resume_verification']['checked_at'] or 'unknown'}.",
        "Preferences: Immediate joiner; Flexible for the right role; target roles are configured for future filtering.",
        "Review age is a project heuristic, not an official ranking rule or evidence of current accuracy.",
    ]
    if report["action_list"]:
        lines.append("Attention first:")
        lines.extend(f"- {item['section']}: {item['reason']}" for item in report["action_list"][:5])
    else:
        lines.append("Attention first: no recorded missing or overdue sections; this does not prove current accuracy.")
    return "\n".join(lines)
