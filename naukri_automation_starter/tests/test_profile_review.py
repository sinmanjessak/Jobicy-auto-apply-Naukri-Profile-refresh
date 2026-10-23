import json

import pytest

from naukri_automation.db import latest_profile_section_reviews, list_profile_health_reports, save_profile_review_report
from naukri_automation.profile_health import (
    PROFILE_REVIEW_STATES, PROFILE_SECTIONS, build_profile_health_report,
    make_section_review_records,
)
from naukri_automation import tasks


def review_inputs(status="Checked", note="Visible section reviewed by user."):
    statuses = {section: status for section in PROFILE_SECTIONS}
    notes = {section: note for section in PROFILE_SECTIONS}
    return statuses, notes


def test_section_review_statuses_are_validated_and_timestamped():
    statuses, notes = review_inputs("Needs review", "I could not confirm section content.")
    records = make_section_review_records(statuses, notes, reviewed_at="2026-10-16T09:00:00+05:30")

    assert tuple(record["section_name"] for record in records) == PROFILE_SECTIONS
    assert {record["status"] for record in records} == {"Needs review"}
    assert {record["reviewed_at"] for record in records} == {"2026-10-16T09:00:00+05:30"}
    assert set(PROFILE_REVIEW_STATES) == {"Not checked", "Checked", "Missing", "Needs review", "Unverified"}


@pytest.mark.parametrize("status,note", [
    ("Update verified", "wrong status"),
    ("Checked", ""),
    ("Missing", "x" * 241),
])
def test_invalid_user_review_is_rejected(status, note):
    statuses, notes = review_inputs()
    statuses[PROFILE_SECTIONS[0]] = status
    notes[PROFILE_SECTIONS[0]] = note
    with pytest.raises(ValueError):
        make_section_review_records(statuses, notes)


def test_report_uses_user_status_and_marks_it_not_independently_verified():
    statuses, notes = review_inputs("Checked", "Section was visible on screen.")
    records = make_section_review_records(statuses, notes, "2026-10-16T09:00:00+05:30")
    mapping = {record["section_name"]: record for record in records}
    report = build_profile_health_report(
        {"controls": {"buttons": [], "links": [], "inputs": []}},
        section_reviews=mapping,
    )

    assert all(item["status"] == "Checked" for item in report["sections"])
    assert all(item["evidence_source"] == "user_confirmed" for item in report["sections"])
    assert all(item["independent_verification"] is False for item in report["sections"])
    assert report["sections"][0]["evidence_note"] == "Section was visible on screen."
    assert report["profile_update"]["status"] == "Unverified"


def test_profile_review_and_report_are_persisted_in_sqlite(tmp_path):
    statuses, notes = review_inputs("Missing", "User did not see this section in the profile view.")
    records = make_section_review_records(statuses, notes, "2026-10-16T09:00:00+05:30")
    report = {
        "checked_at": "2026-10-16T09:00:00+05:30",
        "overall_status": "Needs review",
        "sections": [{"name": row["section_name"], "status": row["status"], "evidence_note": row["evidence_note"]} for row in records],
    }
    db_path = tmp_path / "health.sqlite3"

    report_id = save_profile_review_report(report, records, database_path=db_path)
    latest = latest_profile_section_reviews(database_path=db_path)
    rows = list_profile_health_reports(database_path=db_path)

    assert len(rows) == 1 and rows[0]["id"] == report_id
    assert len(latest) == len(PROFILE_SECTIONS)
    assert latest["Resume"]["status"] == "Missing"
    assert latest["Resume"]["independent_verification"] is False
    assert json.loads(rows[0]["report_json"])["sections"][0]["evidence_note"] == notes["Resume"]


class FakeReadOnlyPage:
    url = "https://www.naukri.com/mnjuser/profile"

    def __init__(self):
        self.goto_calls = []

    def goto(self, *args, **kwargs):
        self.goto_calls.append((args, kwargs))

    def __getattr__(self, name):
        if name in {"locator", "get_by_role", "evaluate", "click", "fill", "set_input_files"}:
            raise AssertionError(f"unsafe or page-reading API accessed: {name}")
        raise AttributeError(name)


def test_review_workflow_is_read_only_and_does_not_log_evidence(monkeypatch):
    page = FakeReadOnlyPage()

    class FakeContext:
        closed = False
        def close(self):
            self.closed = True

    class FakePlaywright:
        stopped = False
        def stop(self):
            self.stopped = True

    context, playwright = FakeContext(), FakePlaywright()
    monkeypatch.setattr(tasks, "_inspected_profile_url", lambda: "https://www.naukri.com/mnjuser/profile")
    monkeypatch.setattr(tasks, "open_naukri", lambda headless=False: (playwright, context, page))
    monkeypatch.setattr(tasks, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks, "record_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks, "generate_profile_health_report", lambda **kwargs: ({
        "checked_at": "now", "timezone": "Asia/Kolkata", "overall_status": "Needs review",
        "sections": [], "resume": {"status": "Checked", "profile_section_status": "Needs review", "verification_status": "Unverified"},
        "notice_period_comparison": {"preference": "Immediate joiner", "status": "Unverified", "note": "No field evidence."},
        "preferences": {"salary": "Flexible for the right role", "target_roles": []},
        "profile_update": {"status": "Unverified"},
    }, 99))
    monkeypatch.setattr(tasks, "render_profile_health_summary", lambda report: "summary")
    user_note = "Visible section; private value is intentionally not entered."
    answers = iter([""] + [answer for _ in PROFILE_SECTIONS for answer in ("2", user_note)])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    result = tasks.review_profile_read_only()

    assert result["overall_status"] == "Needs review"
    assert page.goto_calls == []
    assert context.closed is True and playwright.stopped is True


def test_review_does_not_save_if_profile_route_is_not_confirmed(monkeypatch):
    page = FakeReadOnlyPage()
    page.url = "https://www.naukri.com/login"
    monkeypatch.setattr(tasks, "_inspected_profile_url", lambda: "https://www.naukri.com/mnjuser/profile")
    monkeypatch.setattr(tasks, "open_naukri", lambda headless=False: (None, None, page))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    monkeypatch.setattr(tasks, "generate_profile_health_report", lambda **kwargs: pytest.fail("report must not be saved"))
    monkeypatch.setattr(tasks, "log_event", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="Profile route was not confirmed"):
        tasks.review_profile_read_only()


def test_navigation_error_text_is_not_exposed(monkeypatch, capsys):
    class NavigationFailurePage(FakeReadOnlyPage):
        url = "about:blank"
        def goto(self, *args, **kwargs):
            raise RuntimeError("https://www.naukri.com/?session=private-secret")

    page = NavigationFailurePage()
    monkeypatch.setattr(tasks, "_inspected_profile_url", lambda: "https://www.naukri.com/mnjuser/profile")
    monkeypatch.setattr(tasks, "open_naukri", lambda headless=False: (None, None, page))
    monkeypatch.setattr(tasks, "log_event", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError) as error:
        tasks.review_profile_read_only()
    output = capsys.readouterr().out
    assert "private-secret" not in str(error.value)
    assert "private-secret" not in output
