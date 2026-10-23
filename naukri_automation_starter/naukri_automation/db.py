import json
import sqlite3
from pathlib import Path
from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 source_id TEXT UNIQUE,
 title TEXT NOT NULL,
 company TEXT DEFAULT '',
 location TEXT DEFAULT '',
 url TEXT UNIQUE,
 description TEXT DEFAULT '',
 experience_requirement TEXT DEFAULT '',
 salary TEXT DEFAULT '',
 normalized_url TEXT,
 fallback_key TEXT,
 match_score INTEGER,
 match_reason TEXT DEFAULT '',
 status TEXT NOT NULL DEFAULT 'discovered',
 discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 task TEXT NOT NULL,
 status TEXT NOT NULL,
 details TEXT DEFAULT '',
 started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS profile_health_reports (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 checked_at TEXT NOT NULL,
 overall_status TEXT NOT NULL,
 report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profile_section_reviews (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 report_id INTEGER NOT NULL,
 section_name TEXT NOT NULL,
 status TEXT NOT NULL,
 reviewed_at TEXT NOT NULL,
 evidence_note TEXT NOT NULL DEFAULT '',
 evidence_source TEXT NOT NULL DEFAULT 'user_confirmed',
 independent_verification INTEGER NOT NULL DEFAULT 0,
 FOREIGN KEY(report_id) REFERENCES profile_health_reports(id)
);
CREATE TABLE IF NOT EXISTS resume_verifications (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 checked_at TEXT NOT NULL,
 local_status TEXT NOT NULL,
 local_filename TEXT NOT NULL DEFAULT '',
 local_size_bytes INTEGER NOT NULL DEFAULT 0,
 local_sha256 TEXT,
 local_error_code TEXT,
 site_status TEXT NOT NULL DEFAULT 'Unverified',
 site_resume_observed INTEGER NOT NULL DEFAULT 0,
 displayed_filename TEXT,
 displayed_upload_date TEXT,
 filename_status TEXT NOT NULL DEFAULT 'Unverified',
 filename_matches INTEGER NOT NULL DEFAULT 0,
 content_status TEXT NOT NULL DEFAULT 'Unverified',
 content_verified INTEGER NOT NULL DEFAULT 0,
 downloaded_copy_valid INTEGER NOT NULL DEFAULT 0,
 downloaded_size_bytes INTEGER,
 downloaded_sha256 TEXT,
 evidence_source TEXT NOT NULL DEFAULT 'user_confirmed',
 independent_verification INTEGER NOT NULL DEFAULT 0,
 site_check_performed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS profile_freshness_reports (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 generated_at TEXT NOT NULL,
 report_json TEXT NOT NULL
);
"""

_JOB_MATCH_SCHEMA = """CREATE TABLE IF NOT EXISTS job_match_evaluations (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 job_id INTEGER NOT NULL,
 evaluated_at TEXT NOT NULL,
 score INTEGER NOT NULL,
 breakdown_json TEXT NOT NULL,
 reasons_json TEXT NOT NULL,
 missing_requirements_json TEXT NOT NULL,
 uncertainties_json TEXT NOT NULL,
 mismatches_json TEXT NOT NULL,
 job_snapshot_json TEXT NOT NULL,
 FOREIGN KEY(job_id) REFERENCES jobs(id)
);"""

_APPLICATION_TRACKER_SCHEMA = """CREATE TABLE IF NOT EXISTS application_tracker (
 job_id INTEGER PRIMARY KEY,
 status TEXT NOT NULL CHECK(status IN ('Shortlisted','To Review','Ready to Apply','Applied','Assessment','Interview','Offer','Rejected','Withdrawn','Not Suitable')),
 status_changed_at TEXT NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE TABLE IF NOT EXISTS application_status_history (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 job_id INTEGER NOT NULL,
 from_status TEXT,
 to_status TEXT NOT NULL,
 changed_at TEXT NOT NULL,
 FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE TABLE IF NOT EXISTS application_job_notes (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 job_id INTEGER NOT NULL,
 created_at TEXT NOT NULL,
 note TEXT NOT NULL,
 FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE TABLE IF NOT EXISTS application_reviews (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 job_id INTEGER NOT NULL,
 evaluation_id INTEGER,
 reviewed_at TEXT NOT NULL,
 note TEXT NOT NULL DEFAULT '',
 FOREIGN KEY(job_id) REFERENCES jobs(id),
 FOREIGN KEY(evaluation_id) REFERENCES job_match_evaluations(id)
);
CREATE TABLE IF NOT EXISTS vacancy_intake_records (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 job_id INTEGER NOT NULL,
 intake_source TEXT NOT NULL CHECK(intake_source IN ('csv','manual','jobicy')),
 recorded_at TEXT NOT NULL,
 required_skills_json TEXT NOT NULL DEFAULT '[]',
 missing_information_json TEXT NOT NULL DEFAULT '[]',
 source_listing_id TEXT,
 source_metadata_json TEXT NOT NULL DEFAULT '{}',
 FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE TABLE IF NOT EXISTS application_material_drafts (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 job_id INTEGER NOT NULL,
 generated_at TEXT NOT NULL,
 improvement_report_path TEXT NOT NULL,
 cover_letter_path TEXT,
 question_answers_path TEXT,
 FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE TABLE IF NOT EXISTS application_draft_quality_reports (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 job_id INTEGER NOT NULL,
 draft_id INTEGER NOT NULL,
 generated_at TEXT NOT NULL,
 report_path TEXT NOT NULL,
 findings_json TEXT NOT NULL,
 FOREIGN KEY(job_id) REFERENCES jobs(id),
 FOREIGN KEY(draft_id) REFERENCES application_material_drafts(id)
);
CREATE TABLE IF NOT EXISTS application_review_packs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 job_id INTEGER NOT NULL,
 generated_at TEXT NOT NULL,
 report_path TEXT NOT NULL,
 source_draft_id INTEGER,
 quality_report_id INTEGER,
 FOREIGN KEY(job_id) REFERENCES jobs(id),
 FOREIGN KEY(source_draft_id) REFERENCES application_material_drafts(id),
 FOREIGN KEY(quality_report_id) REFERENCES application_draft_quality_reports(id)
);
CREATE TABLE IF NOT EXISTS application_review_decisions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 job_id INTEGER NOT NULL,
 pack_id INTEGER NOT NULL,
 decision TEXT NOT NULL CHECK(decision IN ('Reviewed','Needs changes','Do not proceed')),
 recorded_at TEXT NOT NULL,
 note TEXT NOT NULL DEFAULT '',
 FOREIGN KEY(job_id) REFERENCES jobs(id),
 FOREIGN KEY(pack_id) REFERENCES application_review_packs(id)
);"""

_JOBS_V2 = """CREATE TABLE jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 source_id TEXT UNIQUE,
 title TEXT NOT NULL,
 company TEXT DEFAULT '',
 location TEXT DEFAULT '',
 url TEXT UNIQUE,
 description TEXT DEFAULT '',
 experience_requirement TEXT DEFAULT '',
 salary TEXT DEFAULT '',
 normalized_url TEXT,
 fallback_key TEXT,
 match_score INTEGER,
 match_reason TEXT DEFAULT '',
 status TEXT NOT NULL DEFAULT 'discovered',
 discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)"""

def connect(database_path: Path | None = None):
    path = Path(database_path) if database_path is not None else settings.database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    job_columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(jobs)")}
    if job_columns.get("url") and job_columns["url"]["notnull"]:
        # Preserve legacy data and identifiers while allowing listings without a URL.
        conn.execute("ALTER TABLE jobs RENAME TO jobs_legacy_migration")
        conn.execute(_JOBS_V2)
        conn.execute("""INSERT INTO jobs(id,source_id,title,company,location,url,description,
            match_score,match_reason,status,discovered_at,updated_at)
            SELECT id,source_id,title,company,location,url,description,match_score,
            match_reason,status,discovered_at,updated_at FROM jobs_legacy_migration""")
        conn.execute("DROP TABLE jobs_legacy_migration")
        job_columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(jobs)")}
    for column, declaration in (
        ("experience_requirement", "TEXT DEFAULT ''"), ("salary", "TEXT DEFAULT ''"),
        ("normalized_url", "TEXT"), ("fallback_key", "TEXT"),
    ):
        if column not in job_columns:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {declaration}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_normalized_url ON jobs(normalized_url) WHERE normalized_url IS NOT NULL")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_fallback_key ON jobs(fallback_key) WHERE fallback_key IS NOT NULL")
    conn.executescript(_JOB_MATCH_SCHEMA)
    conn.executescript(_APPLICATION_TRACKER_SCHEMA)
    _migrate_vacancy_intake_records(conn)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(resume_verifications)")}
    if "site_check_performed" not in columns:
        conn.execute("ALTER TABLE resume_verifications ADD COLUMN site_check_performed INTEGER NOT NULL DEFAULT 0")
        # Positive legacy observations could only have come from the guided page flow.
        conn.execute("UPDATE resume_verifications SET site_check_performed=1 WHERE site_resume_observed=1")
    return conn


def _migrate_vacancy_intake_records(conn: sqlite3.Connection) -> None:
    """Extend intake provenance without discarding historical CSV/manual rows."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='vacancy_intake_records'"
    ).fetchone()
    if row is None:
        return
    columns = {item["name"] for item in conn.execute("PRAGMA table_info(vacancy_intake_records)")}
    if "source_listing_id" not in columns:
        conn.execute("ALTER TABLE vacancy_intake_records ADD COLUMN source_listing_id TEXT")
    if "source_metadata_json" not in columns:
        conn.execute("ALTER TABLE vacancy_intake_records ADD COLUMN source_metadata_json TEXT NOT NULL DEFAULT '{}'")
    schema_sql = (row["sql"] or "").replace(" ", "").casefold()
    if "'jobicy'" not in schema_sql:
        # SQLite cannot alter a CHECK constraint. Rebuild this child table while
        # preserving every row and primary key; it has no inbound references.
        conn.execute("ALTER TABLE vacancy_intake_records RENAME TO vacancy_intake_records_legacy")
        conn.execute("""CREATE TABLE vacancy_intake_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            intake_source TEXT NOT NULL CHECK(intake_source IN ('csv','manual','jobicy')),
            recorded_at TEXT NOT NULL,
            required_skills_json TEXT NOT NULL DEFAULT '[]',
            missing_information_json TEXT NOT NULL DEFAULT '[]',
            source_listing_id TEXT,
            source_metadata_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(job_id) REFERENCES jobs(id)
        )""")
        conn.execute("""INSERT INTO vacancy_intake_records
            (id,job_id,intake_source,recorded_at,required_skills_json,missing_information_json,
             source_listing_id,source_metadata_json)
            SELECT id,job_id,intake_source,recorded_at,required_skills_json,missing_information_json,
                   source_listing_id,source_metadata_json FROM vacancy_intake_records_legacy""")
        conn.execute("DROP TABLE vacancy_intake_records_legacy")

def record_run(task: str, status: str, details: str = "", database_path: Path | None = None) -> None:
    with connect(database_path) as conn:
        conn.execute("INSERT INTO runs(task,status,details) VALUES(?,?,?)", (task, status, details))


def save_profile_health_report(report: dict, database_path: Path | None = None) -> int:
    """Persist a privacy-safe profile health snapshot and return its row id."""
    with connect(database_path) as conn:
        cursor = conn.execute(
            "INSERT INTO profile_health_reports(checked_at,overall_status,report_json) VALUES(?,?,?)",
            (report["checked_at"], report["overall_status"], json.dumps(report, sort_keys=True)),
        )
        return int(cursor.lastrowid)


def list_profile_health_reports(limit: int = 20, database_path: Path | None = None):
    if limit < 1:
        raise ValueError("limit must be positive")
    with connect(database_path) as conn:
        return conn.execute(
            "SELECT id,checked_at,overall_status,report_json FROM profile_health_reports ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()


def latest_profile_section_reviews(database_path: Path | None = None) -> dict[str, dict]:
    """Return the latest user-confirmed review for each section, newest first."""
    with connect(database_path) as conn:
        rows = conn.execute(
            """SELECT section_name,status,reviewed_at,evidence_note,evidence_source,
                      independent_verification
               FROM profile_section_reviews ORDER BY id DESC"""
        ).fetchall()
    reviews = {}
    for row in rows:
        if row["section_name"] not in reviews:
            reviews[row["section_name"]] = {
                "status": row["status"], "reviewed_at": row["reviewed_at"],
                "evidence_note": row["evidence_note"], "evidence_source": row["evidence_source"],
                "independent_verification": bool(row["independent_verification"]),
            }
    return reviews


def save_profile_review_report(report: dict, reviews: list[dict], database_path: Path | None = None) -> int:
    """Persist a report and its individual user-confirmed section reviews together."""
    with connect(database_path) as conn:
        cursor = conn.execute(
            "INSERT INTO profile_health_reports(checked_at,overall_status,report_json) VALUES(?,?,?)",
            (report["checked_at"], report["overall_status"], json.dumps(report, sort_keys=True)),
        )
        report_id = int(cursor.lastrowid)
        conn.executemany(
            """INSERT INTO profile_section_reviews
               (report_id,section_name,status,reviewed_at,evidence_note,evidence_source,independent_verification)
               VALUES(?,?,?,?,?,?,0)""",
            [(report_id, r["section_name"], r["status"], r["reviewed_at"], r["evidence_note"], "user_confirmed")
             for r in reviews],
        )
        return report_id


def save_resume_verification(verification: dict, database_path: Path | None = None) -> int:
    """Append one historical read-only resume-verification result."""
    fields = (
        "checked_at", "local_status", "local_filename", "local_size_bytes", "local_sha256",
        "local_error_code", "site_status", "site_resume_observed", "displayed_filename",
        "displayed_upload_date", "filename_status", "filename_matches", "content_status",
        "content_verified", "downloaded_copy_valid", "downloaded_size_bytes", "downloaded_sha256",
        "evidence_source", "independent_verification", "site_check_performed",
    )
    boolean_fields = {"site_resume_observed", "filename_matches", "content_verified", "downloaded_copy_valid", "independent_verification", "site_check_performed"}
    values = [verification.get(field, False if field in boolean_fields else None) for field in fields]
    values = [int(bool(value)) if field in boolean_fields else value for field, value in zip(fields, values)]
    with connect(database_path) as conn:
        cursor = conn.execute(
            f"INSERT INTO resume_verifications({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
            values,
        )
        return int(cursor.lastrowid)


def latest_resume_verification(database_path: Path | None = None, *, require_site_check: bool = False) -> dict | None:
    """Return latest verification; optionally exclude local-only runs from site evidence."""
    with connect(database_path) as conn:
        query = "SELECT * FROM resume_verifications"
        if require_site_check:
            query += " WHERE site_check_performed=1"
        query += " ORDER BY id DESC LIMIT 1"
        row = conn.execute(query).fetchone()
    if row is None:
        return None
    result = dict(row)
    for field in ("site_resume_observed", "filename_matches", "content_verified", "downloaded_copy_valid", "independent_verification", "site_check_performed"):
        result[field] = bool(result[field])
    return result


def save_profile_freshness_report(report: dict, database_path: Path | None = None) -> int:
    """Append a freshness snapshot without changing review or verification evidence."""
    with connect(database_path) as conn:
        cursor = conn.execute(
            "INSERT INTO profile_freshness_reports(generated_at,report_json) VALUES(?,?)",
            (report["generated_at"], json.dumps(report, sort_keys=True)),
        )
        return int(cursor.lastrowid)


def list_profile_freshness_reports(limit: int = 20, database_path: Path | None = None):
    if limit < 1:
        raise ValueError("limit must be positive")
    with connect(database_path) as conn:
        return conn.execute(
            "SELECT id,generated_at,report_json FROM profile_freshness_reports ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

def add_job(title: str, url: str, company: str = "", location: str = "", description: str = "", source_id: str | None = None):
    with connect() as conn:
        conn.execute("""INSERT INTO jobs(source_id,title,company,location,url,description)
          VALUES(?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title,
          company=excluded.company,location=excluded.location,description=excluded.description,
          updated_at=CURRENT_TIMESTAMP""", (source_id,title,company,location,url,description))

def list_jobs(limit: int = 30):
    with connect() as conn:
        return conn.execute("SELECT * FROM jobs ORDER BY discovered_at DESC LIMIT ?", (limit,)).fetchall()


def upsert_job_listing(job: dict, *, normalized_url: str | None, fallback_key: str | None,
                       database_path: Path | None = None, preserve_blank_fields: bool = False,
                       existing_job_id: int | None = None) -> tuple[int, bool]:
    """Insert or refresh a listing by normalized URL, otherwise by fallback key."""
    with connect(database_path) as conn:
        existing = None
        if normalized_url:
            existing = conn.execute("SELECT * FROM jobs WHERE normalized_url=?", (normalized_url,)).fetchone()
            if existing is None and job.get("url"):
                existing = conn.execute("SELECT * FROM jobs WHERE url=?", (job["url"],)).fetchone()
        if existing is None and existing_job_id is not None:
            existing = conn.execute("SELECT * FROM jobs WHERE id=?", (existing_job_id,)).fetchone()
        if existing is None and fallback_key:
            existing = conn.execute("SELECT * FROM jobs WHERE fallback_key=?", (fallback_key,)).fetchone()
        if existing is None and preserve_blank_fields and not job.get("description"):
            candidates = conn.execute(
                "SELECT * FROM jobs WHERE url IS NULL AND lower(title)=lower(?) AND lower(company)=lower(?) AND lower(location)=lower(?)",
                (job.get("title") or "", job.get("company") or "", job.get("location") or ""),
            ).fetchall()
            # Only reuse a URL-less listing when the identity is unambiguous.
            if len(candidates) == 1:
                existing = candidates[0]
        fields = (job.get("title") or "", job.get("company") or "", job.get("location") or "",
                  job.get("url") or None, job.get("description") or "",
                  job.get("experience_requirement") or "", job.get("salary") or "",
                  normalized_url, fallback_key)
        if existing:
            job_id = int(existing["id"])
            if preserve_blank_fields:
                fields = tuple(value if value not in (None, "") else existing[key] for key, value in zip(
                    ("title", "company", "location", "url", "description", "experience_requirement", "salary", "normalized_url", "fallback_key"), fields
                ))
            conn.execute("""UPDATE jobs SET title=?,company=?,location=?,url=?,description=?,
                experience_requirement=?,salary=?,normalized_url=?,fallback_key=?,updated_at=CURRENT_TIMESTAMP
                WHERE id=?""", (*fields, job_id))
            return job_id, False
        cursor = conn.execute("""INSERT INTO jobs
            (title,company,location,url,description,experience_requirement,salary,normalized_url,fallback_key)
            VALUES(?,?,?,?,?,?,?,?,?)""", fields)
        return int(cursor.lastrowid), True


def list_matchable_jobs(database_path: Path | None = None):
    with connect(database_path) as conn:
        return conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()


def save_job_match_evaluation(job_id: int, evaluation: dict, job_snapshot: dict,
                              database_path: Path | None = None) -> int:
    with connect(database_path) as conn:
        cursor = conn.execute("""INSERT INTO job_match_evaluations
            (job_id,evaluated_at,score,breakdown_json,reasons_json,missing_requirements_json,
             uncertainties_json,mismatches_json,job_snapshot_json)
            VALUES(?,?,?,?,?,?,?,?,?)""", (
                job_id, evaluation["evaluated_at"], evaluation["score"],
                json.dumps(evaluation["breakdown"], sort_keys=True),
                json.dumps(evaluation["reasons"], sort_keys=True),
                json.dumps(evaluation["missing_requirements"], sort_keys=True),
                json.dumps(evaluation["uncertainties"], sort_keys=True),
                json.dumps(evaluation["mismatches"], sort_keys=True),
                json.dumps(job_snapshot, sort_keys=True),
            ))
        return int(cursor.lastrowid)


def latest_job_match_results(limit: int = 100, database_path: Path | None = None):
    if limit < 1:
        raise ValueError("limit must be positive")
    with connect(database_path) as conn:
        return conn.execute("""SELECT e.* FROM job_match_evaluations e
            JOIN (SELECT job_id,MAX(id) AS latest_id FROM job_match_evaluations GROUP BY job_id) latest
            ON latest.latest_id=e.id ORDER BY e.score DESC,e.id DESC LIMIT ?""", (limit,)).fetchall()
