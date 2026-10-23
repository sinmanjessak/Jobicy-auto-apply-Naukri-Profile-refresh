import csv
import json

import pytest

from naukri_automation.application_tracker import record_job_review, update_job_status
from naukri_automation.application_review_pack import create_application_review_pack
from naukri_automation.db import connect
from naukri_automation.vacancy_intake import (
    add_vacancy, import_vacancies_csv, parse_vacancy_csv, ranked_unreviewed_vacancies,
)


def write_vacancies(path, headers, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
    return path


def setup_paths(tmp_path):
    resume = tmp_path / "resume.txt"
    resume.write_text("Backend developer with Python; 5 years of experience.", encoding="utf-8")
    return tmp_path / "vacancies.sqlite3", resume


def test_csv_import_validates_fields_scores_and_persists_supplied_skills(tmp_path):
    db, resume = setup_paths(tmp_path)
    source = write_vacancies(tmp_path / "vacancies.csv",
        ["job title", "company", "location", "source url", "experience", "salary", "description", "required skills"], [
            {"job title": "Backend Developer", "company": "Acme", "location": "Pune",
             "source url": "https://example.test/job/1?utm_source=list", "experience": "3+ years",
             "salary": "Not supplied", "description": "Build backend services.", "required skills": "Python; AWS"},
            {"job title": "", "company": "Missing Role", "description": "No title supplied"},
            {"job title": "Other Developer", "company": "", "description": "Company missing"},
        ])
    result = import_vacancies_csv(source, database_path=db, resume_path=resume,
                                  recorded_at="2026-10-17T10:00:00+05:30")
    assert (result["inserted"], result["updated"], len(result["skipped"])) == (1, 0, 2)
    assert result["results"][0]["score"] > 0
    with connect(db) as conn:
        job = conn.execute("SELECT * FROM jobs").fetchone()
        tracker = conn.execute("SELECT status FROM application_tracker WHERE job_id=?", (job["id"],)).fetchone()
        evaluation = conn.execute("SELECT * FROM job_match_evaluations").fetchone()
        intake = conn.execute("SELECT * FROM vacancy_intake_records").fetchone()
    assert job["url"] == "https://example.test/job/1?utm_source=list"
    assert tracker["status"] == "To Review"
    assert json.loads(evaluation["missing_requirements_json"]) == ["AWS"]
    assert json.loads(evaluation["job_snapshot_json"])["required_skills"] == ["Python", "AWS"]
    assert json.loads(intake["missing_information_json"]) == []


def test_csv_missing_required_columns_and_bad_url_are_rejected_safely(tmp_path):
    db, resume = setup_paths(tmp_path)
    missing_header = write_vacancies(tmp_path / "bad-header.csv", ["title", "company"],
                                     [{"title": "Developer", "company": "Acme"}])
    with pytest.raises(ValueError, match="missing required column"):
        parse_vacancy_csv(missing_header)
    bad_url = write_vacancies(tmp_path / "bad-url.csv", ["title", "company", "description", "url"],
                               [{"title": "Developer", "company": "Acme", "description": "Python", "url": "javascript:alert(1)"}])
    result = import_vacancies_csv(bad_url, database_path=db, resume_path=resume)
    assert result["inserted"] == 0
    assert result["skipped"] == [{"row": 2, "reason": "invalid job URL"}]
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_normalized_url_and_company_title_fallback_deduplicate_without_losing_status_history(tmp_path):
    db, resume = setup_paths(tmp_path)
    headers = ["title", "company", "description", "url", "required skills"]
    first = write_vacancies(tmp_path / "first.csv", headers, [{"title": "Python Developer", "company": "Acme",
        "description": "Build APIs with Python.", "url": "https://example.test/roles/1?source=mail", "required skills": "Python"}])
    second = write_vacancies(tmp_path / "second.csv", headers, [{"title": "python developer", "company": "ACME",
        "description": "Build APIs with Python and AWS.", "url": "https://example.test/roles/1#apply", "required skills": "Python;AWS"}])
    import_vacancies_csv(first, database_path=db, resume_path=resume, recorded_at="2026-10-17T10:00:00+05:30")
    job_id = import_vacancies_csv(second, database_path=db, resume_path=resume,
                                  recorded_at="2026-10-17T11:00:00+05:30")["results"][0]["job_id"]
    update_job_status(job_id, "Interview", note="Existing status/history survives intake.", database_path=db,
                      changed_at="2026-10-17T11:30:00+05:30")
    third = write_vacancies(tmp_path / "third.csv", headers, [{"title": "Python Developer", "company": "Acme",
        "description": "Updated supplied description with Python.", "url": "https://example.test/roles/1?utm_campaign=again", "required skills": "Python"}])
    result = import_vacancies_csv(third, database_path=db, resume_path=resume,
                                  recorded_at="2026-10-17T12:00:00+05:30")
    assert result["inserted"] == 0 and result["updated"] == 1
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM application_tracker WHERE job_id=?", (job_id,)).fetchone()[0] == "Interview"
        assert conn.execute("SELECT COUNT(*) FROM application_status_history WHERE job_id=?", (job_id,)).fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM vacancy_intake_records WHERE job_id=?", (job_id,)).fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM job_match_evaluations WHERE job_id=?", (job_id,)).fetchone()[0] == 3


def test_no_url_duplicate_uses_company_and_title_key_and_manual_add_requires_fields(tmp_path):
    db, resume = setup_paths(tmp_path)
    first = add_vacancy(title="Data Analyst", company="Example", description="Use SQL and Python.",
                        required_skills="SQL;Python", database_path=db, resume_path=resume)
    second = add_vacancy(title=" Data   Analyst ", company="EXAMPLE", description="Analyze data using SQL.",
                         database_path=db, resume_path=resume)
    assert first["job_id"] == second["job_id"]
    assert second["inserted"] is False
    with pytest.raises(ValueError, match="Missing required vacancy value"):
        add_vacancy(title="Developer", company="", description="Python", database_path=db, resume_path=resume)
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_ranked_queue_shows_missing_info_and_removes_latest_reviewed_job(tmp_path):
    db, resume = setup_paths(tmp_path)
    lower = add_vacancy(title="Data Analyst", company="Beta", description="Required: SQL.",
                        url="https://example.test/beta", required_skills="SQL;Terraform", database_path=db, resume_path=resume)
    higher = add_vacancy(title="Backend Developer", company="Alpha", description="Required: Python.",
                         location="Pune", url="https://example.test/alpha", required_skills="Python",
                         database_path=db, resume_path=resume)
    queue = ranked_unreviewed_vacancies(database_path=db)
    assert {item["id"] for item in queue} == {lower["job_id"], higher["job_id"]}
    assert queue[0]["id"] == higher["job_id"]
    assert queue[0]["source_url"] == "https://example.test/alpha"
    assert queue[0]["required_skills"] == ["Python"]
    assert "salary information" in queue[0]["information_to_verify"]
    assert "SQL" in queue[1]["missing_requirements"]
    assert any("not scored by the local catalog" in value and "Terraform" in value
               for value in queue[1]["information_to_verify"])
    record_job_review(higher["job_id"], confirmed=True, database_path=db)
    assert [item["id"] for item in ranked_unreviewed_vacancies(database_path=db)] == [lower["job_id"]]


def test_resume_read_error_prevents_any_vacancy_write(tmp_path):
    db = tmp_path / "no-resume.sqlite3"
    with pytest.raises(ValueError, match="Canonical resume could not be read"):
        add_vacancy(title="Developer", company="Acme", description="Python", database_path=db,
                    resume_path=tmp_path / "missing.pdf")
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_intake_job_uses_existing_review_pack_and_keeps_status(tmp_path):
    db, resume = setup_paths(tmp_path)
    result = add_vacancy(title="Backend Developer", company="Acme", description="Build services.",
                         required_skills="Python;AWS", database_path=db, resume_path=resume)
    pack = create_application_review_pack(result["job_id"], database_path=db, resume_path=resume,
                                          output_dir=tmp_path / "packs", resume_text="Python backend developer")
    text = pack["report_path"].read_text(encoding="utf-8")
    assert "Required skills supplied separately with the vacancy: Python, AWS" in text
    assert pack["status_unchanged"] == "To Review"
    with connect(db) as conn:
        status = conn.execute("SELECT status FROM application_tracker WHERE job_id=?", (result["job_id"],)).fetchone()[0]
    assert status == "To Review"
