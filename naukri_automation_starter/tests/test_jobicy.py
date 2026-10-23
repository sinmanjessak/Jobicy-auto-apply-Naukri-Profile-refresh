import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from naukri_automation.application_tracker import update_job_status
from naukri_automation.db import connect
from naukri_automation.jobicy import JobicyFetchError, fetch_jobicy_vacancies, parse_jobicy_payload
from naukri_automation.jobicy_scheduler import (
    SCHEDULE_HOURS, TASK_NAME, disable_windows_schedule, enable_windows_schedule, scheduler_trigger,
)
from naukri_automation.vacancy_intake import ranked_unreviewed_vacancies
from naukri_automation.vacancy_intake import ranked_unreviewed_vacancies


def setup_paths(tmp_path):
    resume = tmp_path / "resume.txt"
    resume.write_text("Python backend engineer with 5 years of experience. SQL, AWS.", encoding="utf-8")
    return tmp_path / "test.sqlite3", resume


def sample_job(**changes):
    job = {
        "id": 44001,
        "url": "https://jobicy.com/jobs/backend-python-example",
        "jobTitle": "Backend Developer",
        "companyName": "Example Remote Co",
        "jobGeo": "India",
        "jobLevel": "Senior",
        "jobExcerpt": "Python backend role.",
        "jobDescription": "<h2>Requirements</h2><p>Must have Python and SQL. Build APIs.</p><script>ignore me</script>",
        "pubDate": "2026-10-16T09:00:00Z",
        "salaryMin": 80000,
        "salaryMax": 100000,
        "salaryCurrency": "USD",
        "salaryPeriod": "yearly",
        "jobType": ["full-time"],
        "jobIndustry": ["Engineering"],
    }
    job.update(changes)
    return job


def payload(*jobs):
    return {"success": True, "jobs": list(jobs), "nextCursor": None, "hasMore": False}


def test_parser_maps_source_fields_and_does_not_infer_experience_or_missing_values():
    records, skipped = parse_jobicy_payload(payload(sample_job(), {"jobTitle": "Missing", "companyName": "Acme"}))
    assert skipped == [{"item": 2, "reason": "missing required source field(s): description"}]
    job = records[0]
    assert job["title"] == "Backend Developer"
    assert job["company"] == "Example Remote Co"
    assert job["location"] == "India"
    assert job["url"] == "https://jobicy.com/jobs/backend-python-example"
    assert "Must have Python and SQL" in job["description"]
    assert "ignore me" not in job["description"]
    assert job["experience_requirement"] == ""
    assert job["required_skills"] == ["Python", "SQL"]
    assert job["salary"] == "80000–100000 USD yearly"
    assert job["source_listing_id"] == "44001"
    assert job["source_metadata"]["published_at"] == "2026-10-16T09:00:00Z"
    assert job["source_metadata"]["job_level"] == "Senior"


@pytest.mark.parametrize("value", [None, [], {"jobs": "not a list"}, {"success": False, "error": "private detail"}])
def test_parser_rejects_malformed_or_unsuccessful_responses(value):
    with pytest.raises(JobicyFetchError):
        parse_jobicy_payload(value)


def test_parser_rejects_noncanonical_job_links_and_keeps_missing_salary_blank():
    records, skipped = parse_jobicy_payload(payload(sample_job(url="https://other.example/job", salaryMin=None,
                                                               salaryMax=None, salaryCurrency=None)))
    assert records == []
    assert skipped[0]["reason"] == "canonical Jobicy HTTPS URL missing or invalid"
    valid, _ = parse_jobicy_payload(payload(sample_job(salaryMin=None, salaryMax=None, salaryCurrency=None)))
    assert valid[0]["salary"] == ""


def test_fetch_uses_role_tags_bounded_count_and_existing_matching_intake(tmp_path):
    db, resume = setup_paths(tmp_path)
    calls = []

    def fake_get(params, timeout):
        calls.append((params, timeout))
        return payload(sample_job())

    result = fetch_jobicy_vacancies(database_path=db, resume_path=resume,
                                    target_roles=("Python Developer", "Backend Developer"),
                                    per_role=2, timeout_seconds=3, total_timeout_seconds=6,
                                    get_json=fake_get, recorded_at="2026-10-17T09:00:00+05:30")
    assert calls == [({"count": 2, "tag": "Python Developer"}, 3),
                     ({"count": 2, "tag": "Backend Developer"}, 3)]
    assert (result["processed"], result["inserted"], result["updated"]) == (2, 1, 1)
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        job = conn.execute("SELECT * FROM jobs").fetchone()
        tracker = conn.execute("SELECT status FROM application_tracker WHERE job_id=?", (job["id"],)).fetchone()
        history = conn.execute("SELECT COUNT(*) FROM application_status_history WHERE job_id=?", (job["id"],)).fetchone()[0]
        evaluations = conn.execute("SELECT COUNT(*) FROM job_match_evaluations WHERE job_id=?", (job["id"],)).fetchone()[0]
        rows = conn.execute("SELECT * FROM vacancy_intake_records WHERE job_id=?", (job["id"],)).fetchall()
    assert tracker["status"] == "To Review"
    assert history == 1 and evaluations == 2
    assert all(row["intake_source"] == "jobicy" for row in rows)
    assert all(row["source_listing_id"] == "44001" for row in rows)
    assert json.loads(rows[0]["source_metadata_json"])["job_level"] == "Senior"


def test_jobicy_vacancy_flows_into_existing_ranked_queue_with_provenance(tmp_path):
    db, resume = setup_paths(tmp_path)
    result = fetch_jobicy_vacancies(database_path=db, resume_path=resume, target_roles=("Backend Developer",),
                                    get_json=lambda p, t: payload(sample_job()),
                                    recorded_at="2026-10-17T09:00:00+05:30")
    queue = ranked_unreviewed_vacancies(database_path=db)
    assert len(queue) == 1 and queue[0]["id"] == result["results"][0]["job_id"]
    assert queue[0]["source"] == "jobicy"
    assert queue[0]["source_listing_id"] == "44001"
    assert queue[0]["source_metadata"]["published_at"] == "2026-10-16T09:00:00Z"
    assert "source listing is still open" in " ".join(queue[0]["information_to_verify"]).casefold()


def test_fetch_rejects_out_of_bounds_count_without_network_call(tmp_path):
    db, resume = setup_paths(tmp_path)
    calls = []
    with pytest.raises(ValueError, match="bounded ranges"):
        fetch_jobicy_vacancies(database_path=db, resume_path=resume, target_roles=("Python Developer",),
                               per_role=21, get_json=lambda *args: calls.append(args))
    assert calls == []


def test_jobicy_vacancy_flows_into_existing_ranked_queue_with_provenance(tmp_path):
    db, resume = setup_paths(tmp_path)
    result = fetch_jobicy_vacancies(database_path=db, resume_path=resume, target_roles=("Backend Developer",),
                                    get_json=lambda p, t: payload(sample_job()),
                                    recorded_at="2026-10-17T09:00:00+05:30")
    queue = ranked_unreviewed_vacancies(database_path=db)
    assert len(queue) == 1 and queue[0]["id"] == result["results"][0]["job_id"]
    assert queue[0]["source"] == "jobicy"
    assert queue[0]["source_listing_id"] == "44001"
    assert queue[0]["source_metadata"]["published_at"] == "2026-10-16T09:00:00Z"
    assert "source listing is still open" in " ".join(queue[0]["information_to_verify"]).casefold()


def test_fetch_rejects_out_of_bounds_count_without_network_call(tmp_path):
    db, resume = setup_paths(tmp_path)
    calls = []
    with pytest.raises(ValueError, match="bounded ranges"):
        fetch_jobicy_vacancies(database_path=db, resume_path=resume, target_roles=("Python Developer",),
                               per_role=21, get_json=lambda *args: calls.append(args))
    assert calls == []


def test_duplicate_retrieval_preserves_existing_tracker_status_history_and_notes(tmp_path):
    db, resume = setup_paths(tmp_path)
    fake = lambda params, timeout: payload(sample_job())
    first = fetch_jobicy_vacancies(database_path=db, resume_path=resume, target_roles=("Backend Developer",),
                                   get_json=fake, recorded_at="2026-10-17T09:00:00+05:30")
    job_id = first["results"][0]["job_id"]
    update_job_status(job_id, "Interview", note="user note remains", database_path=db,
                      changed_at="2026-10-17T10:00:00+05:30")
    with connect(db) as conn:
        conn.execute("UPDATE runs SET started_at='2020-01-01 00:00:00' WHERE task='jobicy_discovery'")
    second = fetch_jobicy_vacancies(database_path=db, resume_path=resume, target_roles=("Backend Developer",),
                                    get_json=fake, recorded_at="2026-10-17T21:00:00+05:30")
    assert second["inserted"] == 0 and second["updated"] == 1
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM application_tracker WHERE job_id=?", (job_id,)).fetchone()[0] == "Interview"
        assert conn.execute("SELECT COUNT(*) FROM application_status_history WHERE job_id=?", (job_id,)).fetchone()[0] == 2
        assert conn.execute("SELECT note FROM application_job_notes WHERE job_id=?", (job_id,)).fetchone()[0] == "user note remains"
        assert conn.execute("SELECT COUNT(*) FROM job_match_evaluations WHERE job_id=?", (job_id,)).fetchone()[0] == 2


def test_jobicy_identifier_deduplicates_when_canonical_url_changes(tmp_path):
    db, resume = setup_paths(tmp_path)
    original = sample_job()
    fetch_jobicy_vacancies(database_path=db, resume_path=resume, target_roles=("Backend Developer",),
                           get_json=lambda p, t: payload(original))
    with connect(db) as conn:
        conn.execute("UPDATE runs SET started_at='2020-01-01 00:00:00' WHERE task='jobicy_discovery'")
    revised = sample_job(url="https://jobicy.com/jobs/backend-python-revised")
    result = fetch_jobicy_vacancies(database_path=db, resume_path=resume, target_roles=("Backend Developer",),
                                    get_json=lambda p, t: payload(revised))
    assert result["inserted"] == 0 and result["updated"] == 1
    with connect(db) as conn:
        rows = conn.execute("SELECT id,url FROM jobs").fetchall()
    assert len(rows) == 1
    assert rows[0]["url"] == revised["url"]


def test_timeout_and_failed_response_do_not_write_jobs(tmp_path):
    db, resume = setup_paths(tmp_path)

    def timeout(params, seconds):
        raise JobicyFetchError("Jobicy request timed out")

    with pytest.raises(JobicyFetchError, match="timed out"):
        fetch_jobicy_vacancies(database_path=db, resume_path=resume, target_roles=("Python Developer",),
                               get_json=timeout)
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        run = conn.execute("SELECT status,details FROM runs WHERE task='jobicy_discovery'").fetchone()
    assert run["status"] == "failed" and run["details"] == "JobicyFetchError"


def test_one_hour_cooldown_prevents_repeated_requests(tmp_path):
    db, resume = setup_paths(tmp_path)
    calls = []
    fetch_jobicy_vacancies(database_path=db, resume_path=resume, target_roles=("Python Developer",),
                           get_json=lambda p, t: (calls.append(1) or payload(sample_job())))
    with pytest.raises(JobicyFetchError, match="one run per hour"):
        fetch_jobicy_vacancies(database_path=db, resume_path=resume, target_roles=("Python Developer",),
                               get_json=lambda p, t: calls.append(2))
    assert calls == [1]


def test_scheduler_has_two_india_timezone_triggers():
    trigger = scheduler_trigger("Asia/Kolkata")
    assert SCHEDULE_HOURS == (9, 21)
    assert str(trigger.timezone) == "Asia/Kolkata"
    assert trigger.fields[5].name == "hour"
    assert str(trigger.fields[5]) == "9,21"


def test_total_timeout_prevents_intake_when_transport_exceeds_budget(tmp_path):
    db, resume = setup_paths(tmp_path)

    def slow_response(params, timeout):
        time.sleep(1.05)
        return payload(sample_job())

    with pytest.raises(JobicyFetchError, match="total timeout"):
        fetch_jobicy_vacancies(database_path=db, resume_path=resume, target_roles=("Python Developer",),
                               per_role=1, timeout_seconds=1, total_timeout_seconds=1,
                               get_json=slow_response)
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_windows_schedule_install_and_disable_are_mocked_and_bounded(tmp_path):
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = enable_windows_schedule(project_root=tmp_path, python_executable=tmp_path / ".venv" / "Scripts" / "python.exe",
                                     runner=runner, windows=True)
    disable_windows_schedule(runner=runner, windows=True)
    assert result["task_name"] == TASK_NAME
    assert result["hours"] == (9, 21)
    launcher = Path(result["launcher"])
    assert launcher.exists()
    assert "naukri_automation.jobicy_scheduler" in launcher.read_text(encoding="utf-8")
    assert calls[0][0][:6] == ["schtasks.exe", "/Create", "/TN", TASK_NAME, "/SC", "ONLOGON"]
    assert calls[1][0] == ["schtasks.exe", "/Run", "/TN", TASK_NAME]
    assert calls[2][0] == ["schtasks.exe", "/End", "/TN", TASK_NAME]
    assert calls[3][0] == ["schtasks.exe", "/Delete", "/TN", TASK_NAME, "/F"]
    assert all(call[1]["timeout"] == 30 for call in calls)


def test_legacy_intake_migration_preserves_existing_rows(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    with connect(db) as conn:
        conn.execute("INSERT INTO jobs(title,company) VALUES('Old role','Old co')")
        job_id = conn.execute("SELECT id FROM jobs").fetchone()[0]
        conn.execute("DROP TABLE vacancy_intake_records")
        conn.execute("""CREATE TABLE vacancy_intake_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,job_id INTEGER NOT NULL,
            intake_source TEXT NOT NULL CHECK(intake_source IN ('csv','manual')),
            recorded_at TEXT NOT NULL,required_skills_json TEXT NOT NULL DEFAULT '[]',
            missing_information_json TEXT NOT NULL DEFAULT '[]',FOREIGN KEY(job_id) REFERENCES jobs(id))""")
        conn.execute("INSERT INTO vacancy_intake_records(job_id,intake_source,recorded_at) VALUES(?,?,?)",
                     (job_id, "csv", "2026-01-01T00:00:00+00:00"))
    with connect(db) as conn:
        row = conn.execute("SELECT * FROM vacancy_intake_records").fetchone()
        conn.execute("INSERT INTO vacancy_intake_records(job_id,intake_source,recorded_at,source_listing_id) VALUES(?,?,?,?)",
                     (job_id, "jobicy", "2026-10-17T09:00:00+05:30", "44001"))
    assert row["intake_source"] == "csv" and row["recorded_at"] == "2026-01-01T00:00:00+00:00"
