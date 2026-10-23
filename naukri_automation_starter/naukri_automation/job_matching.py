"""CSV/manual job ingestion, local matching, history, and shortlist export."""
from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import settings
from .db import (
    latest_job_match_results, list_matchable_jobs, save_job_match_evaluation,
    upsert_job_listing,
)
from .matching import evaluate_job_match
from .resume import extract_resume_text

FIELD_ALIASES = {
    "title": ("title", "job title", "role"),
    "company": ("company", "employer"),
    "location": ("location", "job location"),
    "description": ("description", "job description", "job_description"),
    "url": ("url", "job url", "job_url", "source url"),
    "experience_requirement": ("experience", "experience requirement", "experience_required", "years of experience"),
    "salary": ("salary", "compensation", "salary range"),
    "required_skills": ("required skills", "required_skills", "skills", "must have skills"),
}
_TRACKING_KEYS = {"ref", "source", "trk", "trackingid", "tracking_id"}


def normalize_job_url(url: str | None) -> str | None:
    value = (url or "").strip()
    if not value:
        return None
    parts = urlsplit(value)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Job URL must be an http(s) URL without embedded credentials")
    host = parts.hostname.casefold()
    port = parts.port
    netloc = host if port is None or (parts.scheme.casefold(), port) in {("http", 80), ("https", 443)} else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    query = sorted((key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
                   if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_KEYS)
    return urlunsplit((parts.scheme.casefold(), netloc, path, urlencode(query), ""))


def fallback_job_key(job: dict) -> str:
    """Stable fallback: normalized title/company/location and description digest."""
    normalize = lambda value: " ".join(str(value or "").casefold().split())
    payload = {
        "title": normalize(job.get("title")), "company": normalize(job.get("company")),
        "location": normalize(job.get("location")),
        "description_sha256": hashlib.sha256(normalize(job.get("description")).encode("utf-8")).hexdigest(),
    }
    return "fallback:" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _canonical_headers(headers: list[str]) -> dict[str, str]:
    mapping = {}
    normalized_headers = {" ".join((header or "").strip().casefold().replace("_", " ").split()): header for header in headers}
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            key = " ".join(alias.casefold().replace("_", " ").split())
            if key in normalized_headers:
                mapping[field] = normalized_headers[key]
                break
    if "title" not in mapping:
        raise ValueError("CSV must include a title column; missing titles are not invented")
    return mapping


def parse_job_csv(path: Path) -> tuple[list[dict], list[dict]]:
    """Parse supplied CSV; blank optional fields stay blank and are never inferred."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Job CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("CSV needs a header row")
        mapping = _canonical_headers(reader.fieldnames)
        records, skipped = [], []
        for row_number, row in enumerate(reader, start=2):
            record = {field: (row.get(header) or "").strip() for field, header in mapping.items()}
            if not record.get("title"):
                skipped.append({"row": row_number, "reason": "missing title"})
                continue
            record.setdefault("company", "")
            record.setdefault("location", "")
            record.setdefault("description", "")
            record.setdefault("url", "")
            record.setdefault("experience_requirement", "")
            record.setdefault("salary", "")
            record.setdefault("required_skills", "")
            try:
                record["normalized_url"] = normalize_job_url(record["url"])
            except ValueError:
                skipped.append({"row": row_number, "reason": "invalid job URL"})
                continue
            record["fallback_key"] = None if record["normalized_url"] else fallback_job_key(record)
            records.append(record)
    return records, skipped


def import_job_csv(path: Path, database_path: Path | None = None) -> dict:
    records, skipped = parse_job_csv(path)
    inserted = updated = 0
    for record in records:
        _job_id, was_inserted = upsert_job_listing(
            record, normalized_url=record["normalized_url"], fallback_key=record["fallback_key"],
            database_path=database_path,
        )
        inserted += int(was_inserted)
        updated += int(not was_inserted)
    return {"inserted": inserted, "updated": updated, "skipped": skipped, "processed": len(records)}


def _evaluate_and_save(job: dict, resume_text: str, database_path: Path | None = None) -> dict:
    job = dict(job)
    result = evaluate_job_match(resume_text, job)
    snapshot = {key: job.get(key) for key in (
        "title", "company", "location", "url", "description", "experience_requirement", "salary",
    )}
    if job.get("required_skills"):
        snapshot["required_skills"] = list(job["required_skills"])
    evaluation_id = save_job_match_evaluation(int(job["id"]), result, snapshot, database_path)
    return {**result, "evaluation_id": evaluation_id, "job": snapshot}


def evaluate_saved_jobs(resume_path: Path | None = None, limit: int = 1000,
                        database_path: Path | None = None) -> list[dict]:
    resume_text = extract_resume_text(resume_path or settings.resume_path)
    results = [_evaluate_and_save(job, resume_text, database_path) for job in list_matchable_jobs(database_path)]
    return sorted(results, key=lambda result: (-result["score"], result["job"]["title"].casefold()))[:limit]


def evaluate_manual_description(description_path: Path, *, title: str = "", company: str = "",
                                location: str = "", url: str = "", experience_requirement: str = "",
                                salary: str = "", resume_path: Path | None = None,
                                database_path: Path | None = None) -> dict:
    description = Path(description_path).read_text(encoding="utf-8")
    job = {
        "title": title.strip(), "company": company.strip(), "location": location.strip(),
        "description": description, "url": url.strip(),
        "experience_requirement": experience_requirement.strip(), "salary": salary.strip(),
    }
    normalized = normalize_job_url(job["url"])
    job_id, _inserted = upsert_job_listing(
        job, normalized_url=normalized,
        fallback_key=None if normalized else fallback_job_key(job),
        database_path=database_path,
    )
    stored = next(row for row in list_matchable_jobs(database_path) if int(row["id"]) == job_id)
    resume_text = extract_resume_text(resume_path or settings.resume_path)
    return _evaluate_and_save(stored, resume_text, database_path)


def load_ranked_shortlist(limit: int = 100, database_path: Path | None = None) -> list[dict]:
    rows = latest_job_match_results(limit, database_path)
    results = []
    for row in rows:
        snapshot = json.loads(row["job_snapshot_json"])
        results.append({
            **snapshot, "score": row["score"], "evaluated_at": row["evaluated_at"],
            "reasons": json.loads(row["reasons_json"]),
            "missing_requirements": json.loads(row["missing_requirements_json"]),
            "uncertainties": json.loads(row["uncertainties_json"]),
            "mismatches": json.loads(row["mismatches_json"]),
            "breakdown": json.loads(row["breakdown_json"]),
        })
    return results


def export_shortlist(path: Path, limit: int = 100, database_path: Path | None = None) -> int:
    """Export latest saved evaluations; empty source fields remain empty."""
    results = load_ranked_shortlist(limit, database_path)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = ("score", "title", "company", "location", "url", "experience_requirement", "salary",
               "reasons", "missing_requirements", "uncertainties", "mismatches", "evaluated_at")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for result in results:
            writer.writerow({
                **{field: ("" if result.get(field) is None else result.get(field))
                   for field in columns if field not in {"reasons", "missing_requirements", "uncertainties", "mismatches"}},
                "reasons": " | ".join(result["reasons"]),
                "missing_requirements": " | ".join(result["missing_requirements"]),
                "uncertainties": " | ".join(result["uncertainties"]),
                "mismatches": " | ".join(result["mismatches"]),
            })
    return len(results)
