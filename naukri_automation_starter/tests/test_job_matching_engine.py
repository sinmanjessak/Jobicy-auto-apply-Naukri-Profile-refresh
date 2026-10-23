import csv
import sqlite3

import pytest

from naukri_automation.db import connect, list_matchable_jobs
from naukri_automation.job_matching import (
    evaluate_manual_description, evaluate_saved_jobs, export_shortlist,
    fallback_job_key, import_job_csv, normalize_job_url, parse_job_csv,
)
from naukri_automation.matching import evaluate_job_match, extract_experience_years, extract_skills


def write_csv(path, headers, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_csv_parsing_supports_aliases_optional_fields_and_skips_missing_title(tmp_path):
    source = write_csv(tmp_path / "jobs.csv", ["Job Title", "Company", "Job Location", "Job Description", "Job URL", "Experience Required", "Salary"], [
        {"Job Title": "Python Developer", "Company": "Acme", "Job Location": "Pune",
         "Job Description": "Must have Python and SQL", "Job URL": "", "Experience Required": "3+ years", "Salary": ""},
        {"Job Title": "", "Job Description": "Description without a title"},
    ])
    records, skipped = parse_job_csv(source)
    assert len(records) == 1
    assert records[0]["title"] == "Python Developer"
    assert records[0]["url"] == ""
    assert records[0]["experience_requirement"] == "3+ years"
    assert records[0]["salary"] == ""
    assert skipped == [{"row": 3, "reason": "missing title"}]


def test_missing_listing_fields_are_not_fabricated(tmp_path):
    source = write_csv(tmp_path / "minimal.csv", ["title"], [{"title": "Backend Developer"}])
    records, skipped = parse_job_csv(source)
    assert skipped == []
    assert records[0]["description"] == records[0]["url"] == ""
    assert records[0]["company"] == records[0]["salary"] == ""
    assert normalize_job_url(records[0]["url"]) is None


def test_url_normalization_and_duplicate_import_keep_one_listing_and_no_fake_url(tmp_path):
    db = tmp_path / "jobs.sqlite3"
    source = write_csv(tmp_path / "dupes.csv", ["title", "company", "description", "job_url"], [
        {"title": "Backend Developer", "company": "Acme", "description": "Python", "job_url": "HTTPS://Example.COM/jobs/1/?utm_source=news"},
        {"title": "Backend Developer", "company": "Acme", "description": "Python", "job_url": "https://example.com/jobs/1#details"},
    ])
    records, _ = parse_job_csv(source)
    assert records[0]["normalized_url"] == records[1]["normalized_url"] == "https://example.com/jobs/1"
    result = import_job_csv(source, database_path=db)
    stored = list_matchable_jobs(db)
    assert result["inserted"] == 1 and result["updated"] == 1
    assert len(stored) == 1
    assert stored[0]["url"] == "https://example.com/jobs/1#details"


def test_fallback_duplicate_key_normalizes_text_and_persists_url_as_null(tmp_path):
    db = tmp_path / "fallback.sqlite3"
    first = {"title": "Python Developer", "company": "Acme", "location": "Pune", "description": "Must have Python"}
    second = {"title": " python   developer ", "company": "ACME", "location": "pune", "description": "Must have   Python"}
    assert fallback_job_key(first) == fallback_job_key(second)
    source = write_csv(tmp_path / "fallback.csv", ["title", "company", "location", "description"], [first, second])
    result = import_job_csv(source, database_path=db)
    stored = list_matchable_jobs(db)
    assert result["inserted"] == 1 and result["updated"] == 1
    assert len(stored) == 1 and stored[0]["url"] is None


def test_skill_extraction_and_required_skill_overlap_are_explainable():
    assert extract_skills("Python, PostgreSQL, and React") == {"Python", "PostgreSQL", "React"}
    result = evaluate_job_match(
        "Python SQL; 5 years of experience",
        {"title": "Backend Developer", "description": "Required: Python, SQL, AWS", "experience_requirement": "3+ years"},
        target_roles=("Backend",), evaluated_at="2026-10-16T12:00:00+05:30",
    )
    assert result["score"] > 0
    assert result["matched_skills"] == ["Python", "SQL"]
    assert result["missing_requirements"] == ["AWS"]
    assert any("Backend" in reason for reason in result["reasons"])
    assert result["breakdown"]["weights"] == {
        "target_role": 25, "required_skill_overlap": 55, "experience_compatibility": 20,
    }
    assert "not a hiring probability" in result["limitations"]


def test_experience_is_uncertain_without_both_explicit_durations():
    assert extract_experience_years("Experience: 4+ years") == 4
    result = evaluate_job_match(
        "Python developer; worked at Acme",
        {"title": "Python Developer", "description": "Python required", "experience_requirement": "5+ years"},
    )
    assert result["breakdown"]["experience_compatibility"] == 50
    assert result["mismatches"] == []
    assert any("numeric duration is not explicit" in item for item in result["uncertainties"])


def test_explicit_experience_mismatch_is_separated_from_unknown_skills():
    result = evaluate_job_match(
        "Python; 2 years of experience",
        {"title": "Python Developer", "description": "Must have Python and AWS", "experience_requirement": "5+ years"},
        target_roles=("Python Developer",), mismatch_penalty=15,
    )
    assert result["mismatches"] and "2 years" in result["mismatches"][0]
    assert result["missing_requirements"] == ["AWS"]
    assert result["breakdown"]["mismatch_penalty"] == 15
    assert result["score"] < 100


def test_csv_export_uses_latest_evaluation_and_leaves_url_blank(tmp_path):
    db = tmp_path / "evaluation.sqlite3"
    resume = tmp_path / "resume.txt"
    resume.write_text("Python SQL; 4 years of experience", encoding="utf-8")
    source = write_csv(tmp_path / "job.csv", ["title", "description", "experience"], [{
        "title": "Backend Developer", "description": "Required: Python and SQL", "experience": "3+ years",
    }])
    assert import_job_csv(source, database_path=db)["inserted"] == 1
    results = evaluate_saved_jobs(resume, database_path=db)
    assert len(results) == 1
    output = tmp_path / "shortlist.csv"
    assert export_shortlist(output, database_path=db) == 1
    with output.open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["title"] == "Backend Developer"
    assert row["url"] == ""
    assert row["score"] == str(results[0]["score"])
    assert "Python" in row["reasons"]


def test_manual_description_is_evaluated_and_saved_without_url(tmp_path):
    db = tmp_path / "manual.sqlite3"
    description = tmp_path / "description.txt"
    description.write_text("Required: Python. Experience 3+ years.", encoding="utf-8")
    resume = tmp_path / "resume.txt"
    resume.write_text("Python; 5 years of experience", encoding="utf-8")
    result = evaluate_manual_description(
        description, title="Python Developer", resume_path=resume, database_path=db,
    )
    assert result["job"]["url"] is None
    with connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM job_match_evaluations").fetchone()[0]
    assert count == 1


def test_legacy_jobs_schema_migrates_without_losing_rows(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,source_id TEXT UNIQUE,
        title TEXT NOT NULL,company TEXT DEFAULT '',location TEXT DEFAULT '',url TEXT NOT NULL UNIQUE,
        description TEXT DEFAULT '',match_score INTEGER,match_reason TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'discovered',discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("INSERT INTO jobs(title,url,description) VALUES('Old listing','https://example.com/old','Text')")
    conn.commit()
    conn.close()
    with connect(db) as migrated:
        row = migrated.execute("SELECT * FROM jobs WHERE id=1").fetchone()
        columns = {column["name"]: column for column in migrated.execute("PRAGMA table_info(jobs)")}
    assert row["title"] == "Old listing" and row["url"] == "https://example.com/old"
    assert columns["url"]["notnull"] == 0
    assert "experience_requirement" in columns
