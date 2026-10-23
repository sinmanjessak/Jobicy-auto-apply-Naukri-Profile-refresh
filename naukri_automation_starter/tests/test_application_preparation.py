import json

import pytest

from naukri_automation.application_preparation import prepare_application_materials
from naukri_automation.db import connect


def add_tracked_job(db, *, description="Must have Python and Kubernetes. Build internal APIs.", status="Shortlisted"):
    with connect(db) as conn:
        cursor = conn.execute(
            "INSERT INTO jobs(title,company,location,url,description,experience_requirement,match_score) VALUES(?,?,?,?,?,?,?)",
            ("Backend Developer", "Example Co", "Pune", "https://example.test/job", description, "", 62),
        )
        job_id = cursor.lastrowid
        conn.execute("INSERT INTO application_tracker(job_id,status,status_changed_at,created_at,updated_at) VALUES(?,?,?,?,?)",
                     (job_id, status, "2026-10-16T00:00:00+05:30", "2026-10-16T00:00:00+05:30", "2026-10-16T00:00:00+05:30"))
        conn.execute(
            """INSERT INTO job_match_evaluations(job_id,evaluated_at,score,breakdown_json,reasons_json,
               missing_requirements_json,uncertainties_json,mismatches_json,job_snapshot_json)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (job_id, "2026-10-16T00:00:00+05:30", 62,
             json.dumps({"source": "import"}), json.dumps(["Title aligns with configured target role: Backend."]),
             json.dumps(["Kubernetes"]), json.dumps(["Kubernetes not found in resume text"]), json.dumps([]),
             json.dumps({"title": "Backend Developer", "company": "Example Co", "location": "Pune",
                         "url": "https://example.test/job", "description": description,
                         "experience_requirement": "", "salary": ""})),
        )
    return job_id


def test_missing_description_stops_without_creating_files_or_history(tmp_path):
    db = tmp_path / "tracker.sqlite3"
    job_id = add_tracked_job(db, description="")
    with pytest.raises(ValueError, match="No stored job description"):
        prepare_application_materials(job_id, database_path=db, resume_text="Python engineer", output_dir=tmp_path / "drafts")
    assert not (tmp_path / "drafts").exists()
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM application_material_drafts").fetchone()[0] == 0


def test_report_uses_supported_resume_evidence_and_marks_missing_claims_for_confirmation(tmp_path):
    db = tmp_path / "tracker.sqlite3"
    job_id = add_tracked_job(db)
    result = prepare_application_materials(
        job_id, database_path=db, output_dir=tmp_path / "drafts",
        resume_text="Python developer. Built REST API services for an internal tool.",
    )
    report = result["improvement_report_path"].read_text(encoding="utf-8")
    assert "Resume evidence (verbatim): “Python developer." in report
    assert "Suggested wording (evidence only, verbatim)" in report
    assert "Kubernetes: explicitly cued" in report
    assert "Confirm whether you have this qualification" in report
    assert "I have Kubernetes" not in report
    assert "inferred relevance" in report


def test_cover_letter_questions_and_paths_are_persisted_without_status_change(tmp_path):
    db = tmp_path / "tracker.sqlite3"
    job_id = add_tracked_job(db)
    canonical = tmp_path / "resume.txt"
    canonical.write_text("Python engineer. Built REST API services for a reporting project.", encoding="utf-8")
    questions = tmp_path / "questions.txt"
    questions.write_text("What is your notice period?\nWhat salary do you expect?\nAre you authorized to work here?", encoding="utf-8")
    result = prepare_application_materials(
        job_id, database_path=db, resume_path=canonical, questions_path=questions,
        output_dir=tmp_path / "materials", include_cover_letter=True,
    )
    assert result["improvement_report_path"].is_file()
    assert result["cover_letter_path"].is_file()
    assert result["question_answers_path"].is_file()
    answers = result["question_answers_path"].read_text(encoding="utf-8")
    assert "Immediate joiner" in answers
    assert "Flexible for the right role" in answers
    assert "Your input needed" in answers
    cover = result["cover_letter_path"].read_text(encoding="utf-8")
    assert "[Your name]" in cover
    with connect(db) as conn:
        row = conn.execute("SELECT * FROM application_material_drafts WHERE id=?", (result["draft_id"],)).fetchone()
        status = conn.execute("SELECT status FROM application_tracker WHERE job_id=?", (job_id,)).fetchone()[0]
    assert row["job_id"] == job_id
    assert row["improvement_report_path"] == str(result["improvement_report_path"])
    assert row["cover_letter_path"] == str(result["cover_letter_path"])
    assert row["question_answers_path"] == str(result["question_answers_path"])
    assert status == "Shortlisted"
    assert canonical.read_text(encoding="utf-8").startswith("Python engineer")


def test_absent_resume_facts_are_not_guessed_in_question_answers(tmp_path):
    db = tmp_path / "tracker.sqlite3"
    job_id = add_tracked_job(db)
    questions = tmp_path / "questions.txt"
    questions.write_text("Describe your Kubernetes experience.", encoding="utf-8")
    result = prepare_application_materials(
        job_id, database_path=db, questions_path=questions, output_dir=tmp_path / "drafts",
        resume_text="Python developer.",
    )
    answer_file = result["question_answers_path"].read_text(encoding="utf-8")
    assert "Your input needed" in answer_file
    assert "do not claim it without confirmation" in answer_file
    assert "I have Kubernetes" not in answer_file


def test_drafts_are_append_only_across_runs(tmp_path):
    db = tmp_path / "tracker.sqlite3"
    job_id = add_tracked_job(db)
    first = prepare_application_materials(job_id, database_path=db, output_dir=tmp_path / "drafts", resume_text="Python developer.")
    second = prepare_application_materials(job_id, database_path=db, output_dir=tmp_path / "drafts", resume_text="Python developer.")
    assert first["improvement_report_path"] != second["improvement_report_path"]
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM application_material_drafts WHERE job_id=?", (job_id,)).fetchone()[0] == 2
