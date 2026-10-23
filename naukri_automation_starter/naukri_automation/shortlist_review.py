"""Read-only, evidence-backed review of jobs already in the application tracker."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .config import settings
from .matching import evaluate_job_match, extract_experience_years, extract_required_skills, extract_skills
from .resume import extract_resume_text

REVIEW_STATUSES = ("Shortlisted", "To Review", "Ready to Apply")


def _decode_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        result = json.loads(raw)
        return [str(item) for item in result] if isinstance(result, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _read_only_connection(database_path: Path) -> sqlite3.Connection:
    path = Path(database_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Tracker database not found: {path}")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _experience_review(resume_text: str | None, job: dict) -> dict:
    requirement = str(job.get("experience_requirement") or "")
    description = str(job.get("description") or "")
    stated = requirement or description
    candidate_years = extract_experience_years(resume_text or "") if resume_text is not None else None
    minimum_years = extract_experience_years(stated) if stated else None
    if candidate_years is None or minimum_years is None:
        status = "Uncertain"
        summary = "Numeric experience duration is not explicit on both sides."
    elif candidate_years >= minimum_years:
        status = "Meets stated minimum"
        summary = f"Resume text states {candidate_years:g} years; listing minimum parsed as {minimum_years:g} years."
    else:
        status = "Potential mismatch"
        summary = f"Resume text states {candidate_years:g} years; listing minimum parsed as {minimum_years:g} years."
    return {
        "status": status, "summary": summary,
        "resume_years": candidate_years, "listing_minimum_years": minimum_years,
        "listing_text": requirement or ("Parsed from description" if description else None),
    }


def build_shortlist_review(*, database_path: Path | None = None,
                           resume_path: Path | None = None,
                           resume_text: str | None = None) -> dict:
    """Build a local report without opening a browser or changing SQLite records."""
    db_path = Path(database_path) if database_path is not None else settings.database_path
    resume_path = Path(resume_path) if resume_path is not None else settings.resume_path
    resume_error = None
    if resume_text is None:
        try:
            resume_text = extract_resume_text(resume_path)
        except (FileNotFoundError, OSError, ValueError, Exception) as exc:
            # PDF parsing errors vary by pypdf version; expose only a generic safe reason.
            resume_text = None
            resume_error = type(exc).__name__

    resume_skills = extract_skills(resume_text) if resume_text is not None else set()
    resume_years = extract_experience_years(resume_text) if resume_text is not None else None
    conn = _read_only_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT j.*, t.status AS tracker_status, t.status_changed_at,
                      e.id AS evaluation_id, e.evaluated_at, e.score AS evaluation_score,
                      e.breakdown_json, e.reasons_json, e.missing_requirements_json,
                      e.uncertainties_json, e.mismatches_json, e.job_snapshot_json,
                      r.evaluation_id AS reviewed_evaluation_id, r.reviewed_at
               FROM jobs j JOIN application_tracker t ON t.job_id=j.id
               LEFT JOIN job_match_evaluations e ON e.id=(
                   SELECT MAX(e2.id) FROM job_match_evaluations e2 WHERE e2.job_id=j.id
               )
               LEFT JOIN application_reviews r ON r.id=(
                   SELECT MAX(r2.id) FROM application_reviews r2 WHERE r2.job_id=j.id
               )
               WHERE t.status IN (?,?,?)
               ORDER BY j.id""",
            REVIEW_STATUSES,
        ).fetchall()
    finally:
        conn.close()

    report_jobs = []
    target_roles = tuple(settings.target_roles)
    for row in rows:
        stored = dict(row)
        try:
            snapshot = json.loads(stored.get("job_snapshot_json") or "{}")
        except json.JSONDecodeError:
            snapshot = {}
        # Prefer the exact listing snapshot used by the saved evaluation.
        job = {key: snapshot.get(key, stored.get(key)) for key in (
            "title", "company", "location", "url", "description", "experience_requirement", "salary",
        )}
        job = {key: value or "" for key, value in job.items()}
        description = str(job["description"])
        listing_skills = extract_skills(description) if description else set()
        required_skills = extract_required_skills(description) if description else set()
        matched_skills = sorted(listing_skills & resume_skills) if resume_text is not None else []
        not_in_resume = sorted(listing_skills - resume_skills) if resume_text is not None else []
        if not description:
            skill_data_status = "Unavailable: no stored job description"
        elif resume_text is None:
            skill_data_status = "Unavailable: canonical resume could not be read"
        else:
            skill_data_status = "Compared against locally extracted resume text"

        saved_reasons = _decode_list(stored.get("reasons_json"))
        saved_missing = _decode_list(stored.get("missing_requirements_json"))
        saved_uncertainties = _decode_list(stored.get("uncertainties_json"))
        saved_mismatches = _decode_list(stored.get("mismatches_json"))
        saved_score = stored.get("evaluation_score")
        if saved_score is None:
            saved_score = stored.get("match_score")
        has_saved_eval = stored.get("evaluation_id") is not None
        recomputed = evaluate_job_match(resume_text, job, target_roles=target_roles) if resume_text is not None and description else None
        try:
            stored_breakdown = json.loads(stored.get("breakdown_json") or "{}")
        except json.JSONDecodeError:
            stored_breakdown = {}
        original_breakdown_available = bool(stored_breakdown) and "source" not in stored_breakdown
        title_lower = str(job["title"]).casefold()
        aligned_role = next((role for role in target_roles if set(role.casefold().split()).issubset(set(title_lower.split()))), None)
        # Use the matcher's result for exact token alignment, including tokenized role labels.
        if recomputed is not None:
            aligned_role = next((reason.split(": ", 1)[1][:-1] for reason in recomputed["reasons"]
                                 if reason.startswith("Title aligns with configured target role:")), None)

        latest_reviewed = (stored.get("reviewed_evaluation_id") is not None
                           and stored.get("reviewed_evaluation_id") == stored.get("evaluation_id"))
        ready_flag = None
        tracker_status = stored["tracker_status"]
        if tracker_status == "Ready to Apply" and not latest_reviewed:
            ready_flag = "Required qualifications for the latest evaluation have no recorded review confirmation."
        elif tracker_status == "Ready to Apply":
            ready_flag = "A user review is recorded for the latest evaluation; this is not independent verification."

        if ready_flag and not latest_reviewed:
            priority, priority_reason = "First", "Ready to Apply without review confirmation tied to the latest evaluation."
        elif aligned_role and saved_mismatches:
            priority, priority_reason = "High", "Target-role aligned; inspect the explicitly recorded qualification mismatch."
        elif aligned_role:
            priority, priority_reason = "High", "Title aligns with a configured target role; review description evidence and uncertainties."
        elif matched_skills:
            priority, priority_reason = "Medium", "No exact target-role alignment, but the listing and resume share catalogued skills."
        else:
            priority, priority_reason = "Lower", "No exact target-role alignment or recognized skill overlap in available evidence."

        report_jobs.append({
            "id": stored["id"], "title": job["title"], "company": job["company"],
            "location": job["location"], "url": job["url"] or None,
            "status": tracker_status, "score": saved_score,
            "score_label": "Heuristic score; not a hiring probability",
            "stored_score": saved_score,
            "current_recomputed_score": recomputed["score"] if recomputed else None,
            "score_recalculation_matches": (recomputed["score"] == saved_score) if recomputed and saved_score is not None else None,
            "stored_component_breakdown_available": original_breakdown_available,
            "current_component_breakdown": recomputed["breakdown"] if recomputed else None,
            "score_limitations": ("Original component breakdown was not supplied at import; current breakdown is a recomputation using current configured rules."
                                  if not original_breakdown_available else "Saved component breakdown is available."),
            "target_role_alignment": aligned_role or "No exact configured target-role match",
            "matched_skills": matched_skills,
            "skills_mentioned_not_found_in_resume_text": not_in_resume,
            "explicitly_required_skills": sorted(required_skills),
            "required_skills_not_found_in_resume_text": sorted(required_skills - resume_skills) if resume_text is not None else [],
            "skill_evidence_status": skill_data_status,
            "experience": _experience_review(resume_text, job),
            "qualification_mismatches": recomputed["mismatches"] if recomputed else saved_mismatches,
            "saved_match_reasons": saved_reasons,
            "saved_missing_requirements": saved_missing,
            "saved_uncertainties": saved_uncertainties,
            "description": description or None,
            "experience_requirement": job["experience_requirement"] or None,
            "salary": job["salary"] or None,
            "location": job["location"] or None,
            "description_available": bool(description),
            "source_url_available": bool(job["url"]),
            "unavailable_data": [label for label, value in (
                ("job description", description), ("source URL", job["url"]),
                ("experience requirement", job["experience_requirement"]),
                ("salary", job["salary"]), ("saved match evaluation", stored.get("evaluation_id")),
                ("canonical resume text", resume_text),
            ) if not value],
            "evaluation_available": has_saved_eval,
            "reviewed_for_latest_evaluation": latest_reviewed,
            "reviewed_at": stored.get("reviewed_at") if latest_reviewed else None,
            "ready_to_apply_review_flag": ready_flag,
            "review_priority": priority, "review_priority_reason": priority_reason,
        })
    return {
        "read_only": True,
        "resume_evidence_available": resume_text is not None,
        "resume_evidence_error_type": resume_error,
        "resume_skill_count": len(resume_skills) if resume_text is not None else None,
        "resume_experience_years": resume_years,
        "target_roles": list(target_roles),
        "jobs": report_jobs,
        "limitations": ["Local stored data only; no job URL was opened or scraped.",
                        "Skill extraction is catalogued lexical evidence and does not prove a qualification is absent.",
                        "Match scores are heuristics, not probabilities of hiring."],
    }
