import json
from pathlib import Path

import pytest
from pypdf import PdfWriter

from naukri_automation.config import DEFAULT_TARGET_ROLES, Settings
from naukri_automation.db import list_profile_health_reports
from naukri_automation.profile_health import (
    PROFILE_SECTIONS,
    REPORT_STATES,
    PROFILE_REVIEW_STATES,
    build_profile_health_report,
    generate_profile_health_report,
    render_profile_health_summary,
)


def sample_inventory():
    return {
        "controls": {
            "buttons": [
                {"label": "Edit resume headline"}, {"label": "Edit key skills"},
                {"label": "Edit employment: Private Employer"}, {"label": "Edit education: M.Tech"},
                {"label": "Edit IT skill: Python"}, {"label": "Edit project: Private Project"},
                {"label": "Edit profile summary"}, {"label": "Accomplishments"},
                {"label": "Edit career profile"}, {"label": "Edit personal details"},
            ],
            "links": [],
            "inputs": [{"label": "Upload resume"}],
        }
    }


def write_pdf(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def test_configuration_defaults_validate_user_preferences():
    config = Settings()
    assert config.notice_period_preference == "Immediate joiner"
    assert config.salary_preference == "Flexible for the right role"
    assert config.target_roles == DEFAULT_TARGET_ROLES
    assert config.timezone == "Asia/Kolkata"


@pytest.mark.parametrize("kwargs", [
    {"notice_period_preference": "Available next month"},
    {"salary_preference": "₹25 LPA"},
    {"target_roles": ()},
    {"target_roles": ("SDE", "sde")},
    {"timezone": "Not/A_Timezone"},
])
def test_invalid_preferences_are_rejected(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)


def test_missing_canonical_resume_is_reported(tmp_path):
    report = build_profile_health_report(sample_inventory(), resume_path=tmp_path / "missing.pdf")
    assert report["resume"]["status"] == "Missing"
    assert report["resume"]["verification_status"] == "Unverified"
    assert report["overall_status"] == "Missing"


def test_incomplete_profile_marks_observed_sections_for_review_and_absent_as_not_checked(tmp_path):
    inventory = {"controls": {"buttons": [{"label": "Resume headline"}], "links": [], "inputs": []}}
    resume = write_pdf(tmp_path / "resume.pdf")
    report = build_profile_health_report(inventory, resume_path=resume)

    by_name = {section["name"]: section["status"] for section in report["sections"]}
    assert len(by_name) == len(PROFILE_SECTIONS)
    assert by_name["Resume headline"] == "Needs review"
    assert by_name["Employment"] == "Not checked"
    assert report["resume"]["status"] == "Checked"
    assert report["resume"]["profile_section_status"] == "Not checked"
    assert report["overall_status"] == "Needs review"


def test_absent_inspection_label_is_not_inferred_as_missing(tmp_path):
    inventory = {"controls": {"buttons": [], "links": [], "inputs": []}}
    report = build_profile_health_report(inventory, resume_path=write_pdf(tmp_path / "resume.pdf"))
    assert all(section["status"] == "Not checked" for section in report["sections"])


def test_conflicting_reliable_notice_period_is_flagged_for_manual_review(tmp_path):
    inventory = sample_inventory()
    inventory["observed_profile_data"] = {
        "notice_period": {
            "label": "Notice period", "value": "30 days", "visible": True, "unique": True,
        }
    }
    report = build_profile_health_report(inventory, resume_path=write_pdf(tmp_path / "resume.pdf"))

    comparison = report["notice_period_comparison"]
    assert comparison["status"] == "Needs review"
    assert comparison["conflict"] is True
    assert comparison["observed"] == "30 days"


def test_unverified_profile_and_notice_data_are_not_inferred(tmp_path):
    report = build_profile_health_report(None, resume_path=write_pdf(tmp_path / "resume.pdf"))

    assert report["inspection_status"] == "Unverified"
    assert all(section["status"] == "Unverified" for section in report["sections"])
    assert report["notice_period_comparison"]["status"] == "Unverified"
    assert report["resume"]["verification_status"] == "Unverified"
    assert report["profile_update"] == {
        "status": "Unverified", "performed": False, "verified": False,
        "note": "This report is read-only; no profile update was attempted.",
    }
    assert REPORT_STATES.issuperset({section["status"] for section in report["sections"]})
    assert "Flexible for the right role" in render_profile_health_summary(report)


def test_profile_health_report_history_is_persisted_with_timestamp(tmp_path):
    db_path = tmp_path / "reports.sqlite3"
    inventory_path = tmp_path / "inspection.json"
    inventory_path.write_text(json.dumps(sample_inventory()), encoding="utf-8")
    resume = write_pdf(tmp_path / "resume.pdf")

    report, report_id = generate_profile_health_report(
        inspection_path=inventory_path, resume_path=resume, database_path=db_path,
    )
    rows = list_profile_health_reports(database_path=db_path)

    assert report_id == report["history_id"] == rows[0]["id"]
    assert rows[0]["checked_at"] == report["checked_at"]
    persisted = json.loads(rows[0]["report_json"])
    assert persisted["preferences"]["salary"] == "Flexible for the right role"
    assert "Private Employer" not in rows[0]["report_json"]
    assert "Private Project" not in rows[0]["report_json"]


def test_report_uses_current_inspection_labels_without_claiming_live_verification(tmp_path):
    inspection = Path("data/site_inspection/latest.json")
    if not inspection.is_file():
        pytest.skip("saved inspection is not available")
    inventory = json.loads(inspection.read_text(encoding="utf-8"))
    report = build_profile_health_report(inventory, resume_path=write_pdf(tmp_path / "resume.pdf"))

    assert len(report["sections"]) == 11
    assert report["resume"]["verification_status"] == "Unverified"
    assert report["profile_update"]["performed"] is False

