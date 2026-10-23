"""Manual, local application tracking for user-supplied job shortlist data."""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import settings
from .db import connect, save_job_match_evaluation, upsert_job_listing
from .job_matching import fallback_job_key, normalize_job_url

TRACKER_STATUSES = (
    "Shortlisted", "To Review", "Ready to Apply", "Applied", "Assessment",
    "Interview", "Offer", "Rejected", "Withdrawn", "Not Suitable",
)


def _now() -> str:
    return datetime.now(ZoneInfo(settings.timezone)).isoformat(timespec="seconds")


def _column_map(headers: list[str]) -> dict[str, str]:
    normalized = {" ".join((header or "").strip().casefold().replace("_", " ").split()): header for header in headers}
    aliases = {
        "title": ("title", "job title", "role"), "company": ("company",),
        "location": ("location",), "description": ("description", "job description"),
        "url": ("url", "job url", "source url"),
        "experience_requirement": ("experience", "experience requirement"),
        "salary": ("salary", "compensation"), "score": ("score", "match score"),
        "reasons": ("reasons", "match reasons", "match reason"),
        "missing_requirements": ("missing requirements",),
        "uncertainties": ("uncertainties", "uncertain requirements"),
        "mismatches": ("mismatches", "qualification mismatches"),
        "evaluated_at": ("evaluated at", "evaluation timestamp"),
    }
    mapping = {}
    for field, names in aliases.items():
        for name in names:
            key = " ".join(name.casefold().replace("_", " ").split())
            if key in normalized:
                mapping[field] = normalized[key]
                break
    if "title" not in mapping:
        raise ValueError("Shortlist CSV must include a title column")
    if "score" not in mapping:
        raise ValueError("Shortlist CSV must include the original match score")
    return mapping


def _split_values(value: str | None) -> list[str]:
    text = (value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
            return parsed
    except json.JSONDecodeError:
        pass
    return [item.strip() for item in text.split(" | ") if item.strip()]


def import_shortlist(path: Path, *, database_path: Path | None = None, imported_at: str | None = None) -> dict:
    """Import a ranked CSV, retaining its original evaluation and existing tracker state."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Shortlist CSV not found: {path}")
    now = imported_at or _now()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("Shortlist CSV needs a header row")
        mapping = _column_map(reader.fieldnames)
        imported = updated = skipped = 0
        for row_number, raw in enumerate(reader, start=2):
            value = lambda field: (raw.get(mapping[field]) or "").strip() if field in mapping else ""
            title = value("title")
            if not title:
                skipped += 1
                continue
            try:
                score = int(value("score"))
            except ValueError:
                skipped += 1
                continue
            if not 0 <= score <= 100:
                skipped += 1
                continue
            job = {
                "title": title, "company": value("company"), "location": value("location"),
                "description": value("description"), "url": value("url"),
                "experience_requirement": value("experience_requirement"), "salary": value("salary"),
            }
            try:
                normalized_url = normalize_job_url(job["url"])
            except ValueError:
                skipped += 1
                continue
            fallback = None if normalized_url else fallback_job_key(job)
            job_id, was_inserted = upsert_job_listing(
                job, normalized_url=normalized_url, fallback_key=fallback,
                database_path=database_path, preserve_blank_fields=True,
            )
            imported += int(was_inserted)
            updated += int(not was_inserted)
            reasons = _split_values(value("reasons"))
            missing = _split_values(value("missing_requirements"))
            uncertainties = _split_values(value("uncertainties"))
            mismatches = _split_values(value("mismatches"))
            with connect(database_path) as conn:
                listing = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                conn.execute("UPDATE jobs SET match_score=?,match_reason=?,updated_at=? WHERE id=?",
                             (score, " | ".join(reasons), now, job_id))
            snapshot = {field: listing[field] for field in (
                "title", "company", "location", "url", "description", "experience_requirement", "salary",
            )}
            evaluation = {
                "evaluated_at": value("evaluated_at") or now,
                "score": score,
                "breakdown": {"source": "Imported shortlist; original component breakdown was not supplied."},
                "reasons": reasons, "missing_requirements": missing,
                "uncertainties": uncertainties, "mismatches": mismatches,
            }
            save_job_match_evaluation(job_id, evaluation, snapshot, database_path)
            with connect(database_path) as conn:
                existing = conn.execute("SELECT 1 FROM application_tracker WHERE job_id=?", (job_id,)).fetchone()
                if existing is None:
                    conn.execute("INSERT INTO application_tracker(job_id,status,status_changed_at,created_at,updated_at) VALUES(?,?,?,?,?)",
                                 (job_id, "Shortlisted", now, now, now))
                    conn.execute("INSERT INTO application_status_history(job_id,from_status,to_status,changed_at) VALUES(?,?,?,?)",
                                 (job_id, None, "Shortlisted", now))
    return {"imported": imported, "updated": updated, "skipped": skipped}


def _job_details(job_id: int, database_path: Path | None = None) -> dict:
    with connect(database_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        tracker = conn.execute("SELECT * FROM application_tracker WHERE job_id=?", (job_id,)).fetchone()
        if job is None or tracker is None:
            raise KeyError(f"Tracked job {job_id} was not found")
        evaluation = conn.execute("SELECT * FROM job_match_evaluations WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        history = conn.execute("SELECT * FROM application_status_history WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
        notes = conn.execute("SELECT * FROM application_job_notes WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
        review = conn.execute("SELECT * FROM application_reviews WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    evaluation_data = None if evaluation is None else {
        "id": evaluation["id"], "evaluated_at": evaluation["evaluated_at"],
        "score": evaluation["score"], "breakdown": json.loads(evaluation["breakdown_json"]),
        "reasons": json.loads(evaluation["reasons_json"]),
        "missing_requirements": json.loads(evaluation["missing_requirements_json"]),
        "uncertainties": json.loads(evaluation["uncertainties_json"]),
        "mismatches": json.loads(evaluation["mismatches_json"]),
    }
    return {
        "job": dict(job), "tracker": dict(tracker), "evaluation": evaluation_data,
        "status_history": [dict(row) for row in history], "notes": [dict(row) for row in notes],
        "latest_review": dict(review) if review else None,
    }


def list_tracked_jobs(status: str | None = None, *, database_path: Path | None = None) -> list[dict]:
    if status is not None and status not in TRACKER_STATUSES:
        raise ValueError(f"Invalid status; choose one of: {', '.join(TRACKER_STATUSES)}")
    with connect(database_path) as conn:
        query = "SELECT j.id,j.title,j.company,j.location,j.url,j.match_score,t.status,t.status_changed_at FROM jobs j JOIN application_tracker t ON t.job_id=j.id"
        params: tuple = ()
        if status:
            query += " WHERE t.status=?"
            params = (status,)
        query += " ORDER BY j.match_score DESC,j.title COLLATE NOCASE"
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def record_job_review(job_id: int, *, confirmed: bool, note: str = "",
                      reviewed_at: str | None = None, database_path: Path | None = None) -> int | None:
    details = _job_details(job_id, database_path)
    if not confirmed:
        return None
    if not details["job"]["description"]:
        raise ValueError("No job description is stored; cannot confirm a description review")
    if details["evaluation"] is None:
        raise ValueError("No saved match evaluation exists to review")
    clean_note = " ".join(note.split())
    if len(clean_note) > 500:
        raise ValueError("Review note must be 500 characters or fewer")
    with connect(database_path) as conn:
        cursor = conn.execute("INSERT INTO application_reviews(job_id,evaluation_id,reviewed_at,note) VALUES(?,?,?,?)",
                              (job_id, details["evaluation"]["id"], reviewed_at or _now(), clean_note))
        return int(cursor.lastrowid)


def update_job_status(job_id: int, status: str, *, note: str = "", applied_confirmed: bool = False,
                      changed_at: str | None = None, database_path: Path | None = None) -> dict:
    if status not in TRACKER_STATUSES:
        raise ValueError(f"Invalid status; choose one of: {', '.join(TRACKER_STATUSES)}")
    if status == "Applied" and not applied_confirmed:
        raise ValueError("Applied requires explicit confirmation that you personally submitted the application")
    details = _job_details(job_id, database_path)
    if status == "Ready to Apply":
        if not details["job"]["description"]:
            raise ValueError("Review the job description first; no description is stored")
        latest = details["evaluation"]
        review = details["latest_review"]
        if latest is None or review is None or review["evaluation_id"] != latest["id"]:
            raise ValueError("Run review-job after inspecting the current description and requirements before setting Ready to Apply")
    current = details["tracker"]["status"]
    timestamp = changed_at or _now()
    clean_note = " ".join(note.split())
    if len(clean_note) > 500:
        raise ValueError("Note must be 500 characters or fewer")
    with connect(database_path) as conn:
        if status != current:
            conn.execute("UPDATE application_tracker SET status=?,status_changed_at=?,updated_at=? WHERE job_id=?",
                         (status, timestamp, timestamp, job_id))
            conn.execute("INSERT INTO application_status_history(job_id,from_status,to_status,changed_at) VALUES(?,?,?,?)",
                         (job_id, current, status, timestamp))
        if clean_note:
            conn.execute("INSERT INTO application_job_notes(job_id,created_at,note) VALUES(?,?,?)",
                         (job_id, timestamp, clean_note))
    return _job_details(job_id, database_path)


def export_tracker(path: Path, *, database_path: Path | None = None) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = list_tracked_jobs(database_path=database_path)
    columns = ("job_id", "title", "company", "location", "url", "status", "status_changed_at",
               "match_score", "match_reasons", "missing_requirements", "uncertainties", "mismatches",
               "notes", "status_history")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for entry in entries:
            detail = _job_details(entry["id"], database_path)
            evaluation = detail["evaluation"] or {}
            writer.writerow({
                "job_id": entry["id"], "title": entry["title"], "company": entry["company"],
                "location": entry["location"], "url": entry["url"] or "",
                "status": entry["status"], "status_changed_at": entry["status_changed_at"],
                "match_score": evaluation.get("score", ""),
                "match_reasons": " | ".join(evaluation.get("reasons", [])),
                "missing_requirements": " | ".join(evaluation.get("missing_requirements", [])),
                "uncertainties": " | ".join(evaluation.get("uncertainties", [])),
                "mismatches": " | ".join(evaluation.get("mismatches", [])),
                "notes": " | ".join(row["note"] for row in detail["notes"]),
                "status_history": " | ".join(f"{row['changed_at']} {row['from_status'] or '—'} -> {row['to_status']}" for row in detail["status_history"]),
            })
    return len(entries)
