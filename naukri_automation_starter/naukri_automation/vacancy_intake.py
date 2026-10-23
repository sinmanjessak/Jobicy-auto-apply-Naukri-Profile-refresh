"""Validated local vacancy intake and review queue built on existing SQLite/matching."""
from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import settings
from .db import connect, list_matchable_jobs, save_job_match_evaluation, upsert_job_listing
from .job_matching import FIELD_ALIASES, normalize_job_url
from .matching import evaluate_job_match, extract_required_skills, extract_skills
from .resume import extract_resume_text

_OPTIONAL_INFORMATION = (
    ("url", "application URL"), ("location", "location"),
    ("experience_requirement", "experience requirement"), ("salary", "salary information"),
)


def _now() -> str:
    return datetime.now(ZoneInfo(settings.timezone)).isoformat(timespec="seconds")


def _normalize(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _split_skills(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if isinstance(raw, (list, tuple)):
        values = [str(item).strip() for item in raw]
    else:
        text = str(raw or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            values = [str(item).strip() for item in parsed] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            values = re.split(r"[;,|]", text)
    result, seen = [], set()
    for value in values:
        key = _normalize(value)
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _header_map(headers: list[str]) -> dict[str, str]:
    normalized = {" ".join((h or "").strip().casefold().replace("_", " ").split()): h for h in headers}
    mapping = {}
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            key = " ".join(alias.casefold().replace("_", " ").split())
            if key in normalized:
                mapping[field] = normalized[key]
                break
    missing_headers = [field for field in ("title", "company", "description") if field not in mapping]
    if missing_headers:
        raise ValueError("Vacancy CSV is missing required column(s): " + ", ".join(missing_headers))
    return mapping


def parse_vacancy_csv(path: Path) -> tuple[list[dict], list[dict]]:
    """Parse supplied vacancy data; title/company/description are mandatory per row."""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"Vacancy CSV not found: {source}")
    records, skipped = [], []
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("Vacancy CSV needs a header row")
        mapping = _header_map(reader.fieldnames)
        for row_number, row in enumerate(reader, start=2):
            record = {field: (row.get(header) or "").strip() for field, header in mapping.items()}
            record.setdefault("location", "")
            record.setdefault("url", "")
            record.setdefault("experience_requirement", "")
            record.setdefault("salary", "")
            record["required_skills"] = _split_skills(record.get("required_skills"))
            missing_required = [field for field in ("title", "company", "description") if not record.get(field)]
            if missing_required:
                skipped.append({"row": row_number, "reason": "missing required value(s): " + ", ".join(missing_required)})
                continue
            try:
                record["normalized_url"] = normalize_job_url(record["url"])
            except ValueError:
                skipped.append({"row": row_number, "reason": "invalid job URL"})
                continue
            records.append(record)
    return records, skipped


def _company_title_key(record: dict) -> str:
    payload = json.dumps({"company": _normalize(record["company"]), "title": _normalize(record["title"])}, sort_keys=True)
    return "vacancy:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _find_url_less_company_title_match(record: dict, database_path: Path | None) -> dict | None:
    with connect(database_path) as conn:
        candidates = conn.execute("SELECT id,title,company,normalized_url,fallback_key FROM jobs WHERE normalized_url IS NULL").fetchall()
    matches = [dict(row) for row in candidates if _normalize(row["title"]) == _normalize(record["title"])
               and _normalize(row["company"]) == _normalize(record["company"])]
    return matches[0] if len(matches) == 1 else None


def _find_source_match(source: str, source_listing_id: str | None, database_path: Path | None) -> dict | None:
    if not source_listing_id:
        return None
    with connect(database_path) as conn:
        row = conn.execute(
            """SELECT j.id,j.fallback_key FROM jobs j JOIN vacancy_intake_records v ON v.job_id=j.id
               WHERE v.intake_source=? AND v.source_listing_id=? ORDER BY v.id DESC LIMIT 1""",
            (source, source_listing_id),
        ).fetchone()
    return dict(row) if row else None


def _missing_information(job: dict) -> list[str]:
    return [label for field, label in _OPTIONAL_INFORMATION if not str(job.get(field) or "").strip()]


def _ingest(record: dict, *, source: str, resume_text: str, database_path: Path | None,
            recorded_at: str, source_listing_id: str | None = None,
            source_metadata: dict | None = None) -> dict:
    record = dict(record)
    required_skills = _split_skills(record.get("required_skills"))
    recognized_skills = sorted(extract_skills(" ".join(required_skills)))
    normalized_url = record.get("normalized_url") or normalize_job_url(record.get("url"))
    matched_identity = _find_source_match(source, source_listing_id, database_path)
    if matched_identity is None:
        matched_identity = _find_url_less_company_title_match(record, database_path)
    if normalized_url:
        fallback_key = matched_identity.get("fallback_key") if matched_identity else None
    elif matched_identity:
        fallback_key = matched_identity.get("fallback_key") or _company_title_key(record)
    else:
        fallback_key = _company_title_key(record)
    record["required_skills"] = required_skills
    job_id, inserted = upsert_job_listing(
        record, normalized_url=normalized_url, fallback_key=fallback_key,
        database_path=database_path, preserve_blank_fields=True,
        existing_job_id=matched_identity.get("id") if source_listing_id and matched_identity else None,
    )
    with connect(database_path) as conn:
        listing_row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        listing = dict(listing_row)
        tracker = conn.execute("SELECT status FROM application_tracker WHERE job_id=?", (job_id,)).fetchone()
        if tracker is None:
            conn.execute(
                "INSERT INTO application_tracker(job_id,status,status_changed_at,created_at,updated_at) VALUES(?,?,?,?,?)",
                (job_id, "To Review", recorded_at, recorded_at, recorded_at),
            )
            conn.execute(
                "INSERT INTO application_status_history(job_id,from_status,to_status,changed_at) VALUES(?,?,?,?)",
                (job_id, None, "To Review", recorded_at),
            )
    match_input = {**listing, "required_skills": recognized_skills}
    evaluation = evaluate_job_match(resume_text, match_input, evaluated_at=recorded_at)
    snapshot = {field: listing.get(field) for field in (
        "title", "company", "location", "url", "description", "experience_requirement", "salary",
    )}
    snapshot["required_skills"] = required_skills
    evaluation_id = save_job_match_evaluation(job_id, evaluation, snapshot, database_path)
    with connect(database_path) as conn:
        conn.execute("UPDATE jobs SET match_score=?,match_reason=?,updated_at=? WHERE id=?",
                     (evaluation["score"], " | ".join(evaluation["reasons"]), recorded_at, job_id))
        conn.execute(
            """INSERT INTO vacancy_intake_records
               (job_id,intake_source,recorded_at,required_skills_json,missing_information_json,
                source_listing_id,source_metadata_json) VALUES(?,?,?,?,?,?,?)""",
            (job_id, source, recorded_at, json.dumps(required_skills, ensure_ascii=False),
             json.dumps(_missing_information(listing), ensure_ascii=False), source_listing_id,
             json.dumps(source_metadata or {}, ensure_ascii=False, sort_keys=True)),
        )
    return {"job_id": job_id, "inserted": inserted, "evaluation_id": evaluation_id,
            "score": evaluation["score"], "reasons": evaluation["reasons"],
            "missing_requirements": evaluation["missing_requirements"],
            "required_skills": required_skills, "missing_information": _missing_information(listing)}


def _load_resume(resume_path: Path | None) -> str:
    try:
        text = extract_resume_text(Path(resume_path) if resume_path is not None else settings.resume_path)
    except Exception as exc:
        raise ValueError(f"Canonical resume could not be read ({type(exc).__name__}); vacancy intake was not performed") from None
    if not text.strip():
        raise ValueError("Canonical resume has no extractable text; vacancy intake was not performed")
    return text


def import_vacancies_csv(path: Path, *, database_path: Path | None = None,
                         resume_path: Path | None = None, recorded_at: str | None = None) -> dict:
    records, skipped = parse_vacancy_csv(path)
    resume_text = _load_resume(resume_path)
    stamp = recorded_at or _now()
    results = [_ingest(item, source="csv", resume_text=resume_text, database_path=database_path, recorded_at=stamp)
               for item in records]
    return {"inserted": sum(item["inserted"] for item in results),
            "updated": sum(not item["inserted"] for item in results), "skipped": skipped,
            "processed": len(results), "results": results}


def add_vacancy(*, title: str, company: str, description: str, location: str = "", url: str = "",
                experience_requirement: str = "", salary: str = "", required_skills: str | list[str] = "",
                database_path: Path | None = None, resume_path: Path | None = None,
                recorded_at: str | None = None) -> dict:
    record = {"title": title.strip(), "company": company.strip(), "description": description.strip(),
              "location": location.strip(), "url": url.strip(),
              "experience_requirement": experience_requirement.strip(), "salary": salary.strip(),
              "required_skills": _split_skills(required_skills)}
    missing = [field for field in ("title", "company", "description") if not record[field]]
    if missing:
        raise ValueError("Missing required vacancy value(s): " + ", ".join(missing))
    record["normalized_url"] = normalize_job_url(record["url"])
    resume_text = _load_resume(resume_path)
    return _ingest(record, source="manual", resume_text=resume_text, database_path=database_path,
                   recorded_at=recorded_at or _now())


def ranked_unreviewed_vacancies(*, limit: int = 100, database_path: Path | None = None) -> list[dict]:
    if limit < 1:
        raise ValueError("limit must be positive")
    with connect(database_path) as conn:
        rows = conn.execute(
            """SELECT j.id,j.title,j.company,j.location,j.url,j.description,j.experience_requirement,j.salary,
                      t.status,e.id AS evaluation_id,e.evaluated_at,e.score,e.reasons_json,e.missing_requirements_json,
                      e.uncertainties_json,e.job_snapshot_json,v.required_skills_json,v.missing_information_json,
                      v.intake_source,v.recorded_at,v.source_listing_id,v.source_metadata_json
               FROM jobs j JOIN application_tracker t ON t.job_id=j.id
               JOIN job_match_evaluations e ON e.id=(SELECT MAX(e2.id) FROM job_match_evaluations e2 WHERE e2.job_id=j.id)
               JOIN vacancy_intake_records v ON v.id=(SELECT MAX(v2.id) FROM vacancy_intake_records v2 WHERE v2.job_id=j.id)
               WHERE t.status NOT IN ('Applied','Assessment','Interview','Offer','Rejected','Withdrawn','Not Suitable')
                 AND NOT EXISTS (SELECT 1 FROM application_reviews r WHERE r.job_id=j.id AND r.evaluation_id=e.id)
               ORDER BY e.score DESC,j.company COLLATE NOCASE,j.title COLLATE NOCASE LIMIT ?""", (limit,),
        ).fetchall()
    queue = []
    for row in rows:
        item = dict(row)
        try:
            snapshot = json.loads(item.get("job_snapshot_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            snapshot = {}
        try:
            item["reasons"] = json.loads(item.pop("reasons_json") or "[]")
            item["missing_requirements"] = json.loads(item.pop("missing_requirements_json") or "[]")
            item["uncertainties"] = json.loads(item.pop("uncertainties_json") or "[]")
            supplied = json.loads(item.pop("required_skills_json") or "[]")
            missing_info = json.loads(item.pop("missing_information_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            supplied, missing_info = [], []
        description = str(snapshot.get("description") or item.get("description") or "")
        supplied = list(map(str, supplied))
        skills = sorted(set(supplied) | extract_required_skills(description))
        unscored_skills = [skill for skill in supplied if not extract_skills(skill)]
        item["required_skills"] = skills
        item["unscored_required_skills"] = unscored_skills
        item["missing_information"] = missing_info
        item["source"] = item.pop("intake_source")
        item["collected_at"] = item.pop("recorded_at")
        item["source_listing_id"] = item.pop("source_listing_id")
        try:
            item["source_metadata"] = json.loads(item.pop("source_metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            item["source_metadata"] = {}
        item["reasons_to_apply"] = item.pop("reasons")
        item["source_url"] = item.pop("url")
        item["match_score"] = item.pop("score")
        item["information_to_verify"] = list(missing_info) + list(item["uncertainties"]) + list(item["missing_requirements"])
        item["information_to_verify"].extend(
            f"Required skill not scored by the local catalog; verify manually: {skill}"
            for skill in unscored_skills
        )
        if item["source"] == "jobicy":
            item["information_to_verify"].append(
                "Confirm the source listing is still open and that its remote geography permits your location."
            )
            if not item["source_metadata"].get("published_at"):
                item["information_to_verify"].append("Jobicy publication date was not supplied.")
        item["status"] = item["status"]
        queue.append(item)
    return queue
