import json
from datetime import datetime, timedelta

import pytest

from naukri_automation.config import DEFAULT_TARGET_ROLES, Settings
from naukri_automation.db import (
    connect, latest_profile_section_reviews, list_profile_freshness_reports,
    save_profile_review_report, save_resume_verification,
)
from naukri_automation.profile_freshness import (
    build_profile_freshness_report, generate_profile_freshness_report,
    render_profile_freshness_summary,
)
from naukri_automation.profile_health import PROFILE_SECTIONS

NOW = "2026-10-16T12:00:00+05:30"


def review(at, status="Checked", **more):
    return {"status": status, "reviewed_at": at, "evidence_source": "user_confirmed",
            "independent_verification": False, **more}


@pytest.mark.parametrize(("days", "expected"), [
    (30, "Current"), (31, "Review due"), (90, "Review due"), (91, "Stale"),
])
def test_freshness_threshold_boundaries(days, expected):
    when = (datetime.fromisoformat(NOW) - timedelta(days=days)).isoformat()
    report = build_profile_freshness_report(
        {"Resume": review(when)}, None, generated_at=NOW,
        current_days=30, due_days=90,
    )
    assert report["sections"][0]["freshness"] == expected


def test_missing_reviews_are_never_reviewed_and_user_observations_are_not_independent():
    report = build_profile_freshness_report({}, None, generated_at=NOW)
    assert len(report["sections"]) == 11
    assert all(row["freshness"] == "Never reviewed" for row in report["sections"])
    assert all(row["status"] == "Not checked" and row["evidence_type"] == "none" for row in report["sections"])
    assert report["action_list"][0]["section"] == "Resume"
    assert all(not row["independent_verification"] for row in report["sections"])


def test_action_list_prioritizes_missing_and_review_flags_over_age_activity():
    observations = {name: review(NOW) for name in PROFILE_SECTIONS}
    observations.update({
        "Personal details": review("2020-01-01T10:00:00+05:30"),
        "Resume": review(NOW, "Missing"),
        "Education": review(NOW, "Needs review"),
    })
    report = build_profile_freshness_report(observations, None, generated_at=NOW)
    assert [item["section"] for item in report["action_list"][:3]] == [
        "Resume", "Education", "Personal details",
    ]


def test_historical_latest_review_is_selected_and_old_user_review_is_stale(tmp_path):
    db = tmp_path / "history.sqlite3"
    report = {"checked_at": "2026-10-16T10:00:00+05:30", "overall_status": "Checked"}
    save_profile_review_report(report, [{
        "section_name": "Resume headline", "status": "Checked",
        "reviewed_at": "2020-01-01T10:00:00+05:30", "evidence_note": "Visible.",
    }], database_path=db)
    save_profile_review_report(report, [{
        "section_name": "Resume headline", "status": "Needs review",
        "reviewed_at": "2026-06-01T10:00:00+05:30", "evidence_note": "Review flagged.",
    }], database_path=db)
    reviews = latest_profile_section_reviews(db)
    result = build_profile_freshness_report(reviews, None, generated_at=NOW)
    item = next(row for row in result["sections"] if row["name"] == "Resume headline")
    assert item["status"] == "Needs review"
    assert item["last_reviewed_at"] == "2026-06-01T10:00:00+05:30"
    assert item["freshness"] == "Stale"
    assert item["evidence_type"] == "user-confirmed"


def test_resume_filename_match_does_not_verify_content():
    record = {
        "site_check_performed": True, "site_resume_observed": True,
        "local_status": "Local file valid", "filename_matches": True,
        "local_sha256": "abc", "downloaded_copy_valid": False,
        "downloaded_sha256": None, "content_verified": False,
        "checked_at": NOW, "independent_verification": False,
    }
    result = build_profile_freshness_report({}, record, generated_at=NOW)["resume_verification"]
    assert result["local_file"] == "Local file valid"
    assert result["site_resume"] == "Site resume observed"
    assert result["filename_match"] == "Filename matches"
    assert result["content_comparison"] == "Unverified"
    record.update(downloaded_copy_valid=True, downloaded_sha256="different", content_verified=True)
    result = build_profile_freshness_report({}, record, generated_at=NOW)["resume_verification"]
    assert result["content_comparison"] == "Unverified"


def test_content_requires_valid_download_and_matching_hashes():
    record = {
        "site_check_performed": True, "site_resume_observed": True,
        "local_status": "Local file valid", "filename_matches": True,
        "local_sha256": "abc", "downloaded_copy_valid": True,
        "downloaded_sha256": "abc", "checked_at": NOW,
    }
    result = build_profile_freshness_report({}, record, generated_at=NOW)["resume_verification"]
    assert result["content_comparison"] == "Content verified"


def test_unverified_bad_or_future_timestamps_do_not_get_freshness_claims():
    report = build_profile_freshness_report({
        "Resume": review("not-a-date"), "Education": review("2027-01-01T10:00:00+05:30"),
        "Employment": review(NOW, "Unverified"),
    }, None, generated_at=NOW)
    by_name = {row["name"]: row["freshness"] for row in report["sections"]}
    assert by_name["Resume"] == "Unverified"
    assert by_name["Education"] == "Unverified"
    assert by_name["Employment"] == "Unverified"


def test_configuration_threshold_validation():
    assert Settings().profile_review_current_days == 30
    assert Settings().profile_review_due_days == 90
    with pytest.raises(ValueError):
        Settings(profile_review_current_days=90, profile_review_due_days=90)


def test_report_generation_is_append_only_and_does_not_create_review_records(tmp_path):
    db = tmp_path / "freshness.sqlite3"
    report_data = {"checked_at": NOW, "overall_status": "Checked"}
    records = [{"section_name": name, "status": "Checked", "reviewed_at": NOW,
                "evidence_note": "Seen by user."} for name in PROFILE_SECTIONS]
    save_profile_review_report(report_data, records, database_path=db)
    save_resume_verification({
        "checked_at": NOW, "local_status": "Local file valid", "site_status": "Site resume observed",
        "local_filename": "resume.pdf", "local_size_bytes": 1,
        "site_resume_observed": True, "filename_status": "Filename matches", "filename_matches": True,
        "content_status": "Unverified", "content_verified": False, "site_check_performed": True,
        "evidence_source": "user_confirmed", "independent_verification": False,
    }, database_path=db)

    first, _ = generate_profile_freshness_report(database_path=db, generated_at=NOW)
    second, _ = generate_profile_freshness_report(database_path=db, generated_at=NOW)
    rows = list_profile_freshness_reports(database_path=db)
    with connect(db) as conn:
        review_count = conn.execute("SELECT COUNT(*) FROM profile_section_reviews").fetchone()[0]
    persisted = json.loads(rows[0]["report_json"])

    assert first["preferences"]["notice_period"] == "Immediate joiner"
    assert first["preferences"]["salary"] == "Flexible for the right role"
    assert tuple(first["preferences"]["target_roles"]) == DEFAULT_TARGET_ROLES
    assert first["resume_verification"]["content_comparison"] == "Unverified"
    assert len(rows) == 2 and persisted["read_only"] is True
    assert review_count == 11
    assert first["history_id"] != second["history_id"]
    assert "Seen by user." not in rows[0]["report_json"]
    summary = render_profile_freshness_summary(first)
    assert "Resume: last review" in summary
    assert "evidence user-confirmed; status Checked; freshness Current" in summary
