import json
import re
import pytest

from naukri_automation.application_preparation import prepare_application_materials
from naukri_automation.application_quality import check_application_quality
from naukri_automation.db import connect


def tracked_job(db, description="Must have Python and Kubernetes. Build backend APIs."):
    with connect(db) as conn:
        cursor = conn.execute("INSERT INTO jobs(title,company,url,description) VALUES(?,?,?,?)",
                              ("Backend Developer", "Example Co", "https://example.test/job", description))
        job_id = cursor.lastrowid
        conn.execute("INSERT INTO application_tracker(job_id,status,status_changed_at,created_at,updated_at) VALUES(?,?,?,?,?)",
                     (job_id, "Shortlisted", "2026-10-16T00:00:00+05:30", "2026-10-16T00:00:00+05:30", "2026-10-16T00:00:00+05:30"))
        conn.execute("""INSERT INTO job_match_evaluations(job_id,evaluated_at,score,breakdown_json,reasons_json,
            missing_requirements_json,uncertainties_json,mismatches_json,job_snapshot_json)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (job_id, "2026-10-16T00:00:00+05:30", 50, "{}", json.dumps(["Python overlaps"]),
             json.dumps(["Kubernetes"]), "[]", "[]", json.dumps({"title": "Backend Developer", "company": "Example Co",
             "url": "https://example.test/job", "description": description})))
    return job_id


def make_draft(db, tmp_path, *, questions=None):
    job_id = tracked_job(db)
    qfile = None
    if questions is not None:
        qfile = tmp_path / "questions.txt"
        qfile.write_text("\n".join(questions), encoding="utf-8")
    result = prepare_application_materials(job_id, database_path=db, output_dir=tmp_path / "drafts",
                                           include_cover_letter=True, questions_path=qfile,
                                           resume_text="Employment: Acme Systems 2020-2022\nPython developer with Python API experience.")
    return job_id, result


def test_unsupported_claim_is_uncertain_not_proof_of_missing_qualification(tmp_path):
    db = tmp_path / "quality.sqlite3"
    job_id, draft = make_draft(db, tmp_path)
    cover = draft["cover_letter_path"]
    cover.write_text(cover.read_text(encoding="utf-8") + "\nI have Kubernetes experience.\n", encoding="utf-8")
    result = check_application_quality(job_id, database_path=db, output_dir=tmp_path / "reports",
                                       resume_text="Employment: Acme Systems 2020-2022\nPython developer with Python API experience.")
    finding = next(item for item in result["findings"] if item["kind"] == "resume support")
    assert finding["classification"] == "Uncertain — user review required"
    assert "Kubernetes" in finding["finding"]
    assert "does not prove" in finding["finding"]


@pytest.mark.parametrize("resume_line,draft_line", [
    ("Employment: Acme Systems 2020-2022", "Employment: Acme Systems 2021-2024"),
    ("Education: Example University 2018-2022", "Education: Example University 2019-2023"),
])
def test_contradictory_employment_or_education_dates_are_confirmed(tmp_path, resume_line, draft_line):
    db = tmp_path / "dates.sqlite3"
    job_id, draft = make_draft(db, tmp_path)
    cover = draft["cover_letter_path"]
    cover.write_text(cover.read_text(encoding="utf-8") + f"\n{draft_line}\n", encoding="utf-8")
    result = check_application_quality(job_id, database_path=db, output_dir=tmp_path / "reports",
                                       resume_text=f"{resume_line}\nPython developer.")
    finding = next(item for item in result["findings"] if item["kind"] == "date inconsistency")
    assert finding["classification"] == "Confirmed issue"
    assert finding["section"] == "Cover letter"


def test_missing_saved_draft_file_is_reported_as_confirmed_issue(tmp_path):
    db = tmp_path / "missing-file.sqlite3"
    job_id, draft = make_draft(db, tmp_path)
    draft["cover_letter_path"].unlink()
    result = check_application_quality(job_id, database_path=db, output_dir=tmp_path / "reports",
                                       resume_text="Employment: Acme Systems 2020-2022\nPython developer.")
    assert any(item["kind"] == "draft file unavailable" and item["section"] == "Cover letter"
               and item["classification"] == "Confirmed issue" for item in result["findings"])


def test_missing_question_answer_is_a_confirmed_issue_and_unaddressed_requirement_uncertain(tmp_path):
    db = tmp_path / "missing.sqlite3"
    job_id, draft = make_draft(db, tmp_path, questions=["Describe your Python skills."])
    answers = draft["question_answers_path"]
    text = answers.read_text(encoding="utf-8")
    text = re.sub(r"(?m)^Draft answer:.*$", "Draft answer:", text)
    answers.write_text(text, encoding="utf-8")
    result = check_application_quality(job_id, database_path=db, output_dir=tmp_path / "reports",
                                       resume_text="Employment: Acme Systems 2020-2022\nPython developer.")
    assert any(item["kind"] == "missing answer" and item["classification"] == "Confirmed issue" for item in result["findings"])
    assert any(item["kind"] == "job requirement not addressed" and item["requirement"] == "Kubernetes"
               and item["classification"] == "Uncertain — user review required" for item in result["findings"])


def test_quality_report_is_saved_with_job_and_draft_paths_without_status_change(tmp_path):
    db = tmp_path / "persist.sqlite3"
    job_id, draft = make_draft(db, tmp_path)
    result = check_application_quality(job_id, database_path=db, output_dir=tmp_path / "reports",
                                       resume_text="Employment: Acme Systems 2020-2022\nPython developer.")
    assert result["report_path"].is_file()
    saved_report = result["report_path"].read_text(encoding="utf-8")
    assert f"Job ID: {job_id}" in saved_report
    assert str(draft["improvement_report_path"]) in saved_report
    with connect(db) as conn:
        row = conn.execute("SELECT * FROM application_draft_quality_reports WHERE id=?", (result["report_id"],)).fetchone()
        status = conn.execute("SELECT status FROM application_tracker WHERE job_id=?", (job_id,)).fetchone()[0]
    assert row["job_id"] == job_id
    assert row["draft_id"] == draft["draft_id"]
    assert row["report_path"] == str(result["report_path"])
    assert json.loads(row["findings_json"]) == result["findings"]
    assert status == "Shortlisted"
