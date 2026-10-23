import csv
import json

import pytest

from naukri_automation.application_tracker import (
    TRACKER_STATUSES, export_tracker, import_shortlist, list_tracked_jobs,
    record_job_review, update_job_status,
)
from naukri_automation.db import connect, upsert_job_listing
from naukri_automation.job_matching import normalize_job_url


def write_shortlist(path, rows):
    fields = ["score", "title", "company", "location", "url", "experience_requirement", "salary",
              "reasons", "missing_requirements", "uncertainties", "mismatches", "evaluated_at", "description"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def listing(db, *, description="Must have Python. 3+ years experience.", url="https://example.com/jobs/1"):
    job = {"title": "Backend Developer", "company": "Acme", "location": "Pune",
           "description": description, "url": url, "experience_requirement": "3+ years", "salary": ""}
    normalized = normalize_job_url(url)
    return upsert_job_listing(job, normalized_url=normalized,
                              fallback_key=None if normalized else "fallback:test", database_path=db)[0]


def row(**changes):
    base = {
        "score": "82", "title": "Backend Developer", "company": "Acme", "location": "Pune",
        "url": "https://example.com/jobs/1?utm_source=mail", "experience_requirement": "3+ years",
        "salary": "", "reasons": "Role aligned | Python skill overlap",
        "missing_requirements": "AWS", "uncertainties": "Experience not stated in resume",
        "mismatches": "", "evaluated_at": "2026-10-16T10:00:00+05:30",
        "description": "Must have Python. 3+ years experience.",
    }
    base.update(changes)
    return base


def test_shortlist_import_preserves_score_reasons_uncertainties_url_and_creates_initial_status(tmp_path):
    db = tmp_path / "tracker.sqlite3"
    source = write_shortlist(tmp_path / "shortlist.csv", [row()])
    result = import_shortlist(source, database_path=db, imported_at="2026-10-16T11:00:00+05:30")

    tracked = list_tracked_jobs(database_path=db)
    assert result == {"imported": 1, "updated": 0, "skipped": 0}
    assert tracked[0]["status"] == "Shortlisted"
    assert tracked[0]["url"] == row()["url"]
    with connect(db) as conn:
        evaluation = conn.execute("SELECT * FROM job_match_evaluations").fetchone()
    assert evaluation["score"] == 82
    assert json.loads(evaluation["reasons_json"]) == ["Role aligned", "Python skill overlap"]
    assert json.loads(evaluation["uncertainties_json"]) == ["Experience not stated in resume"]
    assert json.loads(evaluation["missing_requirements_json"]) == ["AWS"]


def test_reimport_deduplicates_and_preserves_notes_status_and_status_history(tmp_path):
    db = tmp_path / "tracker.sqlite3"
    source = write_shortlist(tmp_path / "shortlist.csv", [row()])
    import_shortlist(source, database_path=db, imported_at="2026-10-16T11:00:00+05:30")
    job_id = list_tracked_jobs(database_path=db)[0]["id"]
    update_job_status(job_id, "To Review", note="Inspect requirements.",
                      changed_at="2026-10-16T11:10:00+05:30", database_path=db)
    reimport = write_shortlist(tmp_path / "shortlist-again.csv", [row(score="85", reasons="New score reason")])
    result = import_shortlist(reimport, database_path=db, imported_at="2026-10-16T12:00:00+05:30")
    with connect(db) as conn:
        job_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        histories = conn.execute("SELECT COUNT(*) FROM application_status_history").fetchone()[0]
        notes = conn.execute("SELECT note FROM application_job_notes").fetchall()
        eval_count = conn.execute("SELECT COUNT(*) FROM job_match_evaluations").fetchone()[0]
    tracked = list_tracked_jobs(database_path=db)[0]
    assert result == {"imported": 0, "updated": 1, "skipped": 0}
    assert job_count == 1 and eval_count == 2
    assert histories == 2 and [item["note"] for item in notes] == ["Inspect requirements."]
    assert tracked["status"] == "To Review"
    assert tracked["status_changed_at"] == "2026-10-16T11:10:00+05:30"
    assert tracked["match_score"] == 85


def test_shortlist_without_description_preserves_existing_description_for_review(tmp_path):
    db = tmp_path / "preserve.sqlite3"
    job_id = listing(db, description="Must have Python and SQL.")
    source = write_shortlist(tmp_path / "shortlist.csv", [row(description="")])
    import_shortlist(source, database_path=db)
    with connect(db) as conn:
        saved = conn.execute("SELECT description FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
    assert saved == "Must have Python and SQL."


def test_status_validation_timestamp_and_notes_are_append_only(tmp_path):
    db = tmp_path / "status.sqlite3"
    source = write_shortlist(tmp_path / "shortlist.csv", [row()])
    import_shortlist(source, database_path=db, imported_at="2026-10-16T11:00:00+05:30")
    job_id = list_tracked_jobs(database_path=db)[0]["id"]
    result = update_job_status(job_id, "Interview", note="Technical round scheduled by user.",
                               changed_at="2026-10-17T09:00:00+05:30", database_path=db)
    assert result["tracker"]["status"] == "Interview"
    assert result["tracker"]["status_changed_at"] == "2026-10-17T09:00:00+05:30"
    assert result["notes"][0]["note"] == "Technical round scheduled by user."
    assert [event["to_status"] for event in result["status_history"]] == ["Shortlisted", "Interview"]
    with pytest.raises(ValueError, match="Invalid status"):
        update_job_status(job_id, "Applied-ish", database_path=db)


def test_applied_requires_explicit_self_submission_confirmation(tmp_path):
    db = tmp_path / "applied.sqlite3"
    source = write_shortlist(tmp_path / "shortlist.csv", [row()])
    import_shortlist(source, database_path=db)
    job_id = list_tracked_jobs(database_path=db)[0]["id"]
    with pytest.raises(ValueError, match="explicit confirmation"):
        update_job_status(job_id, "Applied", database_path=db)
    result = update_job_status(job_id, "Applied", applied_confirmed=True,
                               changed_at="2026-10-17T09:00:00+05:30", database_path=db)
    assert result["tracker"]["status"] == "Applied"
    assert result["tracker"]["status_changed_at"] == "2026-10-17T09:00:00+05:30"
    assert "applied_at" not in result["tracker"]


def test_ready_to_apply_requires_review_of_latest_evaluation_and_description(tmp_path):
    db = tmp_path / "ready.sqlite3"
    source = write_shortlist(tmp_path / "shortlist.csv", [row()])
    import_shortlist(source, database_path=db)
    job_id = list_tracked_jobs(database_path=db)[0]["id"]
    with pytest.raises(ValueError, match="review-job"):
        update_job_status(job_id, "Ready to Apply", database_path=db)
    review_id = record_job_review(job_id, confirmed=True, note="Reviewed supplied requirements.",
                                  reviewed_at="2026-10-16T12:00:00+05:30", database_path=db)
    assert review_id is not None
    result = update_job_status(job_id, "Ready to Apply", database_path=db)
    assert result["tracker"]["status"] == "Ready to Apply"
    # A newer imported evaluation requires a new review before a later Ready transition.
    reimport = write_shortlist(tmp_path / "shortlist-new.csv", [row(score="90")])
    import_shortlist(reimport, database_path=db)
    with pytest.raises(ValueError, match="review-job"):
        update_job_status(job_id, "To Review", database_path=db)
        update_job_status(job_id, "Ready to Apply", database_path=db)


def test_tracker_export_contains_status_match_evidence_notes_and_source_url(tmp_path):
    db = tmp_path / "export.sqlite3"
    source = write_shortlist(tmp_path / "shortlist.csv", [row()])
    import_shortlist(source, database_path=db, imported_at="2026-10-16T11:00:00+05:30")
    job_id = list_tracked_jobs(database_path=db)[0]["id"]
    update_job_status(job_id, "To Review", note="Check the role details.", database_path=db)
    output = tmp_path / "tracker.csv"
    assert export_tracker(output, database_path=db) == 1
    with output.open(encoding="utf-8-sig", newline="") as handle:
        exported = next(csv.DictReader(handle))
    assert exported["status"] == "To Review"
    assert exported["match_score"] == "82"
    assert exported["match_reasons"] == "Role aligned | Python skill overlap"
    assert exported["uncertainties"] == "Experience not stated in resume"
    assert exported["url"] == row()["url"]
    assert exported["notes"] == "Check the role details."


def test_status_catalog_contains_exact_required_values():
    assert TRACKER_STATUSES == (
        "Shortlisted", "To Review", "Ready to Apply", "Applied", "Assessment",
        "Interview", "Offer", "Rejected", "Withdrawn", "Not Suitable",
    )
