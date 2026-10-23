from dataclasses import replace

from typer.testing import CliRunner

import naukri_automation.db as db_module
import naukri_automation.vacancy_intake as intake_module
import naukri_automation.application_preparation as preparation_module
import naukri_automation.application_review_pack as review_pack_module
from naukri_automation.cli import app
from naukri_automation.config import settings as base_settings


def test_import_and_ranked_queue_cli_commands_work_locally(tmp_path, monkeypatch):
    db = tmp_path / "cli.sqlite3"
    resume = tmp_path / "resume.txt"
    resume.write_text("Python backend developer; 5 years of experience.", encoding="utf-8")
    local_settings = replace(base_settings, database_path=db, resume_path=resume)
    monkeypatch.setattr(db_module, "settings", local_settings)
    monkeypatch.setattr(intake_module, "settings", local_settings)

    csv_path = tmp_path / "vacancies.csv"
    csv_path.write_text(
        "title,company,description,url,required_skills\n"
        '"Backend Developer","Example Co","Build Python APIs.","https://example.test/1","Python;AWS"\n',
        encoding="utf-8",
    )
    runner = CliRunner()
    imported = runner.invoke(app, ["import-vacancies", str(csv_path)])
    assert imported.exit_code == 0, imported.output
    assert "1 new" in imported.output
    queued = runner.invoke(app, ["application-queue"])
    assert queued.exit_code == 0, queued.output
    assert "Example Co" in queued.output
    assert "Backend Developer" in queued.output
    assert "Python" in queued.output
    assert "AWS" in queued.output
    assert "prepare-application" in queued.output
    assert "application-review-pack" in queued.output


def test_manual_add_vacancy_cli_accepts_pasted_description(tmp_path, monkeypatch):
    db = tmp_path / "manual-cli.sqlite3"
    resume = tmp_path / "resume.txt"
    resume.write_text("Python engineer", encoding="utf-8")
    local_settings = replace(base_settings, database_path=db, resume_path=resume)
    monkeypatch.setattr(db_module, "settings", local_settings)
    monkeypatch.setattr(intake_module, "settings", local_settings)
    result = CliRunner().invoke(app, [
        "add-vacancy", "--title", "Python Developer", "--company", "Example",
        "--description", "Must have Python.", "--required-skills", "Python",
    ])
    assert result.exit_code == 0, result.output
    assert "Job #1" in result.output


def test_prepare_queue_job_runs_existing_draft_and_pack_workflows_without_status_change(tmp_path, monkeypatch):
    db = tmp_path / "queue-prepare.sqlite3"
    resume = tmp_path / "resume.txt"
    resume.write_text("Python backend developer with 5 years experience.", encoding="utf-8")
    local_settings = replace(base_settings, database_path=db, resume_path=resume)
    for module in (db_module, intake_module, preparation_module, review_pack_module):
        monkeypatch.setattr(module, "settings", local_settings)
    monkeypatch.setattr(preparation_module, "extract_resume_text", lambda _path: "Python backend developer with 5 years experience.")
    monkeypatch.setattr(review_pack_module, "extract_resume_text", lambda _path: "Python backend developer with 5 years experience.")

    created = intake_module.add_vacancy(
        title="Backend Developer", company="Example Co", description="Build Python APIs.",
        url="https://example.test/backend", database_path=db, resume_path=resume,
    )
    result = CliRunner().invoke(app, [
        "prepare-queue-job", str(created["job_id"]), "--cover-letter",
        "--preparation-output-dir", str(tmp_path / "drafts"),
        "--review-pack-output-dir", str(tmp_path / "packs"),
    ])
    assert result.exit_code == 0, result.output
    assert "Preparation report:" in result.output
    assert "Cover-letter draft:" in result.output
    assert "Review pack:" in result.output
    assert "Tracker status remains To Review" in result.output
    with db_module.connect(db) as conn:
        assert conn.execute("SELECT status FROM application_tracker WHERE job_id=?", (created["job_id"],)).fetchone()[0] == "To Review"
        assert conn.execute("SELECT COUNT(*) FROM application_material_drafts WHERE job_id=?", (created["job_id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM application_review_packs WHERE job_id=?", (created["job_id"],)).fetchone()[0] == 1


def test_prepare_queue_job_rejects_id_missing_from_queue_without_generating_records(tmp_path, monkeypatch):
    db = tmp_path / "queue-reject.sqlite3"
    resume = tmp_path / "resume.txt"
    resume.write_text("Python engineer", encoding="utf-8")
    local_settings = replace(base_settings, database_path=db, resume_path=resume)
    monkeypatch.setattr(db_module, "settings", local_settings)
    monkeypatch.setattr(intake_module, "settings", local_settings)
    result = CliRunner().invoke(app, ["prepare-queue-job", "987"])
    assert result.exit_code == 1
    assert "not currently in the unreviewed vacancy queue" in result.output
    with db_module.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM application_material_drafts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM application_review_packs").fetchone()[0] == 0
