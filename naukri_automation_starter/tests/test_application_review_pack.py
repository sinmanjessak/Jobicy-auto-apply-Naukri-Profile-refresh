import json

import pytest

from naukri_automation.application_preparation import prepare_application_materials
from naukri_automation.application_quality import check_application_quality
from naukri_automation.application_review_pack import (
    create_application_review_pack, record_application_review_decision,
)
from naukri_automation.db import connect


def create_job(db, *, description="Must have Python and Kubernetes. 3+ years experience.",
               title="Backend Developer", url="https://example.test/job/1"):
    with connect(db) as conn:
        cur = conn.execute("INSERT INTO jobs(title,company,location,url,description,experience_requirement,salary) VALUES(?,?,?,?,?,?,?)",
                           (title, "Example Co", "Pune", url, description, "3+ years", ""))
        job_id = cur.lastrowid
        conn.execute("INSERT INTO application_tracker(job_id,status,status_changed_at,created_at,updated_at) VALUES(?,?,?,?,?)",
                     (job_id, "Shortlisted", "2026-10-16T00:00:00+05:30", "2026-10-16T00:00:00+05:30", "2026-10-16T00:00:00+05:30"))
        cur = conn.execute("""INSERT INTO job_match_evaluations(job_id,evaluated_at,score,breakdown_json,reasons_json,
            missing_requirements_json,uncertainties_json,mismatches_json,job_snapshot_json)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (job_id, "2026-10-16T00:00:00+05:30", 61, json.dumps({"source": "Imported shortlist; no original breakdown."}),
             json.dumps(["Title aligns using configured target roles", "Python overlap"]), json.dumps(["Kubernetes"]),
             json.dumps(["Experience uncertain"]), "[]", json.dumps({"title": title, "company": "Example Co", "location": "Pune",
             "url": url, "description": description, "experience_requirement": "3+ years", "salary": ""})))
        evaluation_id = cur.lastrowid
    return job_id, evaluation_id


def test_pack_reports_missing_evidence_and_unverified_fields_without_inventing_values(tmp_path):
    db = tmp_path / "missing.sqlite3"
    job_id, _ = create_job(db, description="", url="")
    pack = create_application_review_pack(job_id, database_path=db, resume_path=tmp_path / "missing.pdf",
                                          output_dir=tmp_path / "packs")
    report = pack["report_path"].read_text(encoding="utf-8")
    assert "No stored job description" in report
    assert "Canonical resume could not be read locally; resume evidence is Unverified." in report
    assert "Source URL (not opened): `not supplied`" in report
    assert "Salary information: Not supplied; no amount inferred" in report
    assert "No user-confirmed review record" in report


def test_pack_consolidates_saved_job_match_drafts_quality_and_user_review(tmp_path):
    db = tmp_path / "complete.sqlite3"
    job_id, evaluation_id = create_job(db)
    resume = tmp_path / "resume.txt"
    resume.write_text("Python developer and Backend Engineer.", encoding="utf-8")
    draft = prepare_application_materials(job_id, database_path=db, resume_path=resume,
                                         output_dir=tmp_path / "drafts", include_cover_letter=True)
    quality = check_application_quality(job_id, database_path=db, resume_path=resume,
                                        output_dir=tmp_path / "quality")
    with connect(db) as conn:
        conn.execute("INSERT INTO application_reviews(job_id,evaluation_id,reviewed_at,note) VALUES(?,?,?,?)",
                     (job_id, evaluation_id, "2026-10-16T01:00:00+05:30", "Description reviewed by user"))
    pack = create_application_review_pack(job_id, database_path=db, resume_path=resume, output_dir=tmp_path / "packs")
    report = pack["report_path"].read_text(encoding="utf-8")
    assert "Stored score: 61/100" in report
    assert "heuristic, not a hiring probability" in report
    assert "Kubernetes" in report
    assert "not located in extracted resume text (uncertain; absence from text is not proof" in report
    assert "Python developer and Backend Engineer" in report
    assert "user-confirmed job-description review is recorded" in report
    assert str(draft["improvement_report_path"]) in report
    assert str(draft["cover_letter_path"]) in report
    assert str(quality["report_path"]) in report
    assert "Tracker status (stored; unchanged): Shortlisted" in report
    with connect(db) as conn:
        saved = conn.execute("SELECT * FROM application_review_packs WHERE id=?", (pack["pack_id"],)).fetchone()
        status = conn.execute("SELECT status FROM application_tracker WHERE job_id=?", (job_id,)).fetchone()[0]
    assert saved["job_id"] == job_id
    assert saved["source_draft_id"] == draft["draft_id"]
    assert saved["quality_report_id"] == quality["report_id"]
    assert status == "Shortlisted"


def test_unverified_salary_and_quality_state_are_labelled(tmp_path):
    db = tmp_path / "unverified.sqlite3"
    job_id, _ = create_job(db)
    pack = create_application_review_pack(job_id, database_path=db, resume_text="Python developer.",
                                          output_dir=tmp_path / "packs")
    report = pack["report_path"].read_text(encoding="utf-8")
    assert "Salary information: Not supplied" in report
    assert "No application-preparation draft record was found" in report
    assert "Latest quality check: Unverified" in report


def test_explicit_review_decisions_are_append_only_and_never_change_tracker_status(tmp_path):
    db = tmp_path / "decision.sqlite3"
    job_id, _ = create_job(db)
    first = create_application_review_pack(job_id, database_path=db, resume_text="Python developer.", output_dir=tmp_path / "packs")
    decision = record_application_review_decision(job_id, "Needs changes", note="Review missing requirements.",
                                                  database_path=db, recorded_at="2026-10-16T02:00:00+05:30")
    second = create_application_review_pack(job_id, database_path=db, resume_text="Python developer.", output_dir=tmp_path / "packs")
    text = second["report_path"].read_text(encoding="utf-8")
    assert "Needs changes" in text
    assert f"review pack #{first['pack_id']}" in text
    with connect(db) as conn:
        status = conn.execute("SELECT status FROM application_tracker WHERE job_id=?", (job_id,)).fetchone()[0]
        decision_count = conn.execute("SELECT COUNT(*) FROM application_review_decisions WHERE job_id=?", (job_id,)).fetchone()[0]
    assert decision["status_unchanged"] == status == "Shortlisted"
    assert decision_count == 1
    with pytest.raises(ValueError, match="Decision must be one of"):
        record_application_review_decision(job_id, "Ready to Apply", database_path=db)


def test_cannot_record_decision_before_generating_pack(tmp_path):
    db = tmp_path / "no-pack.sqlite3"
    job_id, _ = create_job(db)
    with pytest.raises(KeyError, match="generate one first"):
        record_application_review_decision(job_id, "Reviewed", database_path=db)
