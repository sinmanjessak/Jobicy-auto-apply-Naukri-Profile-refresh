import hashlib

from pypdf import PdfWriter

from naukri_automation.db import latest_resume_verification, save_resume_verification
from naukri_automation.profile_health import build_profile_health_report, generate_profile_health_report
from naukri_automation.resume_verification import (
    CONTENT_VERIFIED, FILENAME_MATCHES, LOCAL_VALID, SITE_OBSERVED, UNVERIFIED,
    classify_resume_verification, filename_matches, inspect_local_pdf, sha256_file,
)


def make_pdf(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def test_local_file_validation_and_streaming_sha256(tmp_path):
    pdf = make_pdf(tmp_path / "resume.pdf")
    result = inspect_local_pdf(pdf)

    assert result["local_status"] == LOCAL_VALID
    assert result["local_filename"] == "resume.pdf"
    assert result["local_size_bytes"] > 0
    assert result["local_sha256"] == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert sha256_file(pdf) == result["local_sha256"]


def test_missing_and_invalid_local_files_are_unverified(tmp_path):
    missing = inspect_local_pdf(tmp_path / "missing.pdf")
    invalid_path = tmp_path / "bad.pdf"
    invalid_path.write_bytes(b"not pdf")
    invalid = inspect_local_pdf(invalid_path)

    assert missing["local_status"] == UNVERIFIED
    assert missing["local_error_code"] == "missing_file"
    assert invalid["local_status"] == UNVERIFIED
    assert invalid["local_error_code"] == "invalid_pdf"


def test_filename_matching_handles_windows_paths_and_case():
    assert filename_matches("resume.pdf", r"C:\\Users\\User\\Downloads\\Resume.PDF")
    assert not filename_matches("resume.pdf", "updated_resume.pdf")


def test_site_and_filename_evidence_do_not_claim_content_equality(tmp_path):
    pdf = make_pdf(tmp_path / "resume.pdf")
    local = inspect_local_pdf(pdf)
    result = classify_resume_verification(
        local, site_resume_observed=True, displayed_filename="resume.pdf",
        displayed_upload_date="16 Oct 2026",
    )

    assert result["site_status"] == SITE_OBSERVED
    assert result["filename_status"] == FILENAME_MATCHES
    assert result["content_status"] == UNVERIFIED
    assert result["independent_verification"] is False


def test_unobserved_site_evidence_remains_unverified(tmp_path):
    local = inspect_local_pdf(make_pdf(tmp_path / "resume.pdf"))
    result = classify_resume_verification(local, displayed_filename="resume.pdf")

    assert result["site_status"] == UNVERIFIED
    assert result["filename_status"] == UNVERIFIED
    assert result["content_status"] == UNVERIFIED
    assert result["displayed_filename"] is None


def test_content_verification_requires_matching_hash_of_valid_download(tmp_path):
    local_pdf = make_pdf(tmp_path / "resume.pdf")
    copied_pdf = tmp_path / "downloaded.pdf"
    copied_pdf.write_bytes(local_pdf.read_bytes())
    local = inspect_local_pdf(local_pdf)

    matching = classify_resume_verification(
        local, site_resume_observed=True, displayed_filename="resume.pdf", downloaded_copy=copied_pdf,
    )
    mismatch_pdf = tmp_path / "other.pdf"
    mismatch_pdf.write_bytes(local_pdf.read_bytes() + b"\n% different byte stream\n")
    mismatch = classify_resume_verification(
        local, site_resume_observed=True, displayed_filename="resume.pdf", downloaded_copy=mismatch_pdf,
    )

    assert matching["content_status"] == CONTENT_VERIFIED
    assert matching["downloaded_sha256"] == local["local_sha256"]
    assert mismatch["content_status"] == UNVERIFIED


def test_resume_verification_history_is_append_only(tmp_path):
    pdf = make_pdf(tmp_path / "resume.pdf")
    local = inspect_local_pdf(pdf)
    db_path = tmp_path / "verification.sqlite3"
    first = classify_resume_verification(local, site_resume_observed=False)
    first["checked_at"] = "2026-10-16T10:00:00+05:30"
    second = classify_resume_verification(local, site_resume_observed=True, displayed_filename="resume.pdf")
    second["checked_at"] = "2026-10-16T11:00:00+05:30"

    first_id = save_resume_verification(first, database_path=db_path)
    second_id = save_resume_verification(second, database_path=db_path)
    latest = latest_resume_verification(database_path=db_path)
    with __import__("sqlite3").connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM resume_verifications").fetchone()[0]

    assert second_id > first_id
    assert count == 2
    assert latest["id"] == second_id
    assert latest["checked_at"] == second["checked_at"]
    assert latest["site_status"] == SITE_OBSERVED


def test_local_only_record_does_not_hide_last_site_review(tmp_path):
    pdf = make_pdf(tmp_path / "resume.pdf")
    local = inspect_local_pdf(pdf)
    db_path = tmp_path / "separate-evidence.sqlite3"
    site_review = classify_resume_verification(
        local, site_check_performed=True, site_resume_observed=True, displayed_filename="resume.pdf",
    )
    site_review["checked_at"] = "2026-10-16T10:00:00+05:30"
    local_only = classify_resume_verification(local)
    local_only["checked_at"] = "2026-10-16T11:00:00+05:30"
    save_resume_verification(site_review, database_path=db_path)
    save_resume_verification(local_only, database_path=db_path)

    latest_any = latest_resume_verification(database_path=db_path)
    latest_site = latest_resume_verification(database_path=db_path, require_site_check=True)

    assert latest_any["checked_at"] == local_only["checked_at"]
    assert latest_any["site_check_performed"] is False
    assert latest_site["checked_at"] == site_review["checked_at"]
    assert latest_site["filename_status"] == FILENAME_MATCHES


def test_existing_database_migration_preserves_positive_site_evidence(tmp_path):
    import sqlite3
    from naukri_automation.db import connect

    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE resume_verifications (id INTEGER PRIMARY KEY, site_resume_observed INTEGER NOT NULL DEFAULT 0)")
        conn.execute("INSERT INTO resume_verifications(site_resume_observed) VALUES(1)")

    with connect(db_path) as conn:
        row = conn.execute("SELECT site_resume_observed,site_check_performed FROM resume_verifications").fetchone()

    assert bool(row["site_resume_observed"]) is True
    assert bool(row["site_check_performed"]) is True


def test_profile_health_includes_verification_states_without_upgrade_from_name_only(tmp_path):
    pdf = make_pdf(tmp_path / "resume.pdf")
    local = inspect_local_pdf(pdf)
    verification = classify_resume_verification(
        local, site_resume_observed=True, displayed_filename="resume.pdf",
    )
    report = build_profile_health_report(None, resume_path=pdf, resume_verification=verification)

    assert report["resume"]["verification_status"] == FILENAME_MATCHES
    assert report["resume"]["verification"]["local_status"] == LOCAL_VALID
    assert report["resume"]["verification"]["site_status"] == SITE_OBSERVED
    assert report["resume"]["verification"]["filename_status"] == FILENAME_MATCHES
    assert report["resume"]["verification"]["content_status"] == UNVERIFIED
    assert report["resume"]["verification"]["independent_verification"] is False


def test_saved_resume_verification_flows_into_new_health_report(tmp_path):
    pdf = make_pdf(tmp_path / "resume.pdf")
    local = inspect_local_pdf(pdf)
    evidence = classify_resume_verification(local, site_resume_observed=True, displayed_filename="resume.pdf")
    evidence["checked_at"] = "2026-10-16T12:00:00+05:30"
    db_path = tmp_path / "report.sqlite3"
    save_resume_verification(evidence, database_path=db_path)

    report, _report_id = generate_profile_health_report(
        inspection_path=tmp_path / "absent-inspection.json",
        resume_path=pdf,
        database_path=db_path,
    )

    assert report["resume"]["verification_status"] == FILENAME_MATCHES
    assert report["resume"]["verification"]["checked_at"] == evidence["checked_at"]
    assert report["resume"]["verification"]["content_status"] == UNVERIFIED
