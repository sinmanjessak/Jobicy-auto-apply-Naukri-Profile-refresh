import json

from naukri_automation.db import connect
from naukri_automation.shortlist_review import build_shortlist_review


def add_job(db, *, title="Python Developer", description="Must have Python and AWS. 3+ years experience.",
            status="Shortlisted", breakdown=None, score=33, url="https://example.test/job/1"):
    with connect(db) as conn:
        cur = conn.execute(
            "INSERT INTO jobs(title,company,location,url,description,experience_requirement,match_score) VALUES(?,?,?,?,?,?,?)",
            (title, "Example Co", "Pune", url, description, "3+ years", score),
        )
        job_id = cur.lastrowid
        conn.execute("INSERT INTO application_tracker(job_id,status,status_changed_at,created_at,updated_at) VALUES(?,?,?,?,?)",
                     (job_id, status, "2026-10-16T00:00:00+05:30", "2026-10-16T00:00:00+05:30", "2026-10-16T00:00:00+05:30"))
        cur = conn.execute(
            """INSERT INTO job_match_evaluations(job_id,evaluated_at,score,breakdown_json,reasons_json,
               missing_requirements_json,uncertainties_json,mismatches_json,job_snapshot_json)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (job_id, "2026-10-16T00:00:00+05:30", score,
             json.dumps(breakdown or {"source": "Imported shortlist; original component breakdown was not supplied."}),
             json.dumps(["Saved reason from import"]), json.dumps(["AWS"]), json.dumps(["Imported uncertainty"]),
             json.dumps(["Explicit experience gap"]), json.dumps({"title": title, "company": "Example Co",
             "location": "Pune", "url": url, "description": description, "experience_requirement": "3+ years", "salary": ""})),
        )
        evaluation_id = cur.lastrowid
    return job_id, evaluation_id


def test_missing_description_and_resume_are_reported_as_unavailable(tmp_path):
    db = tmp_path / "review.sqlite3"
    add_job(db, description="")
    report = build_shortlist_review(database_path=db, resume_path=tmp_path / "missing.pdf")
    job = report["jobs"][0]
    assert report["resume_evidence_available"] is False
    assert job["description_available"] is False
    assert job["skill_evidence_status"] == "Unavailable: no stored job description"
    assert job["current_recomputed_score"] is None
    assert job["experience"]["status"] == "Uncertain"


def test_ready_to_apply_without_review_for_latest_evaluation_is_flagged_without_status_change(tmp_path):
    db = tmp_path / "ready.sqlite3"
    job_id, _ = add_job(db, status="Ready to Apply")
    report = build_shortlist_review(database_path=db, resume_text="Python developer with 2.2 years experience")
    result = next(job for job in report["jobs"] if job["id"] == job_id)
    assert "no recorded review confirmation" in result["ready_to_apply_review_flag"]
    assert result["review_priority"] == "First"
    with connect(db) as conn:
        assert conn.execute("SELECT status FROM application_tracker WHERE job_id=?", (job_id,)).fetchone()[0] == "Ready to Apply"


def test_saved_explanations_and_current_score_math_are_reported_separately(tmp_path):
    db = tmp_path / "score.sqlite3"
    job_id, _ = add_job(db, score=32)
    report = build_shortlist_review(database_path=db, resume_text="Python developer with 2.2 years experience")
    job = next(job for job in report["jobs"] if job["id"] == job_id)
    # Role 25 + half skill overlap (27.5) + experience 0. Python round(52.5) is 52; minus 20.
    assert job["current_recomputed_score"] == 32
    assert job["score_recalculation_matches"] is True
    assert job["stored_component_breakdown_available"] is False
    assert "Original component breakdown was not supplied" in job["score_limitations"]
    assert job["target_role_alignment"] == "Python Developer"
    assert job["matched_skills"] == ["Python"]
    assert job["skills_mentioned_not_found_in_resume_text"] == ["AWS"]
    assert job["required_skills_not_found_in_resume_text"] == ["AWS"]
    assert job["qualification_mismatches"] == [
        "Explicit experience gap: resume text states 2.2 years; listing requires at least 3."
    ]
    assert job["saved_match_reasons"] == ["Saved reason from import"]
    assert job["score_label"] == "Heuristic score; not a hiring probability"


def test_review_confirmation_only_counts_when_tied_to_latest_evaluation(tmp_path):
    db = tmp_path / "reviewed.sqlite3"
    job_id, evaluation_id = add_job(db, status="Ready to Apply")
    with connect(db) as conn:
        conn.execute("INSERT INTO application_reviews(job_id,evaluation_id,reviewed_at,note) VALUES(?,?,?,?)",
                     (job_id, evaluation_id, "2026-10-16T01:00:00+05:30", "Requirements inspected by user"))
    job = build_shortlist_review(database_path=db, resume_text="Python developer with 3 years experience")["jobs"][0]
    assert job["reviewed_for_latest_evaluation"] is True
    assert "user review is recorded" in job["ready_to_apply_review_flag"]
    assert job["review_priority"] != "First"


def test_report_does_not_change_database_records(tmp_path):
    db = tmp_path / "readonly.sqlite3"
    job_id, _ = add_job(db)
    with connect(db) as conn:
        before = (conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                  conn.execute("SELECT COUNT(*) FROM job_match_evaluations").fetchone()[0],
                  conn.execute("SELECT status FROM application_tracker WHERE job_id=?", (job_id,)).fetchone()[0])
    report = build_shortlist_review(database_path=db, resume_text="Python developer with 2.2 years experience")
    assert report["read_only"] is True
    with connect(db) as conn:
        after = (conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                 conn.execute("SELECT COUNT(*) FROM job_match_evaluations").fetchone()[0],
                 conn.execute("SELECT status FROM application_tracker WHERE job_id=?", (job_id,)).fetchone()[0])
    assert after == before
