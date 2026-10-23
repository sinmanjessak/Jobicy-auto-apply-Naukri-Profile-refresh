"""Consolidated local application review packs and explicit review decisions."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import settings
from .db import connect
from .matching import extract_skills
from .resume import extract_resume_text

REVIEW_DECISIONS = ("Reviewed", "Needs changes", "Do not proceed")


def _now() -> str:
    return datetime.now(ZoneInfo(settings.timezone)).isoformat(timespec="seconds")


def _json_list(value: str | None) -> list:
    try:
        result = json.loads(value or "[]")
        return result if isinstance(result, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _excerpt_skills(resume_text: str, relevant: set[str]) -> list[dict]:
    excerpts = []
    seen = set()
    for line in resume_text.splitlines():
        text = " ".join(line.split())
        if not text or text in seen:
            continue
        found = sorted(extract_skills(text) & relevant)
        if found:
            excerpts.append({"skills": found, "text": text[:650]})
            seen.add(text)
    return excerpts


def _load_sources(job_id: int, database_path: Path | None) -> dict:
    with connect(database_path) as conn:
        job_row = conn.execute(
            "SELECT j.*,t.status AS tracker_status,t.status_changed_at FROM jobs j JOIN application_tracker t ON t.job_id=j.id WHERE j.id=?",
            (job_id,),
        ).fetchone()
        if job_row is None:
            raise KeyError(f"Tracked job {job_id} was not found")
        job = dict(job_row)
        evaluation_row = conn.execute(
            "SELECT * FROM job_match_evaluations WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,),
        ).fetchone()
        evaluation = dict(evaluation_row) if evaluation_row else None
        if evaluation:
            try:
                snapshot = json.loads(evaluation.get("job_snapshot_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                snapshot = {}
            for key in ("title", "company", "location", "url", "description", "experience_requirement", "salary"):
                if snapshot.get(key) not in (None, ""):
                    job[key] = snapshot[key]
            if isinstance(snapshot.get("required_skills"), list):
                job["intake_required_skills"] = snapshot["required_skills"]
        draft_row = conn.execute("SELECT * FROM application_material_drafts WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        draft = dict(draft_row) if draft_row else None
        quality_row = None
        if draft:
            quality_row = conn.execute(
                "SELECT * FROM application_draft_quality_reports WHERE job_id=? AND draft_id=? ORDER BY id DESC LIMIT 1",
                (job_id, draft["id"]),
            ).fetchone()
        quality = dict(quality_row) if quality_row else None
        review = None
        if evaluation:
            review_row = conn.execute(
                "SELECT id,evaluation_id,reviewed_at FROM application_reviews WHERE job_id=? AND evaluation_id=? ORDER BY id DESC LIMIT 1",
                (job_id, evaluation["id"]),
            ).fetchone()
            review = dict(review_row) if review_row else None
        resume_verification = conn.execute(
            "SELECT checked_at,site_status,site_resume_observed,filename_status,content_status,evidence_source,independent_verification FROM resume_verifications WHERE site_check_performed=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        decisions = conn.execute(
            "SELECT id,pack_id,decision,recorded_at,note FROM application_review_decisions WHERE job_id=? ORDER BY id DESC LIMIT 5",
            (job_id,),
        ).fetchall()
    return {"job": job, "evaluation": evaluation, "draft": draft, "quality": quality,
            "application_review": review, "resume_verification": dict(resume_verification) if resume_verification else None,
            "decisions": [dict(item) for item in decisions]}


def _render_pack(data: dict, resume_text: str | None, generated_at: str) -> str:
    job, evaluation, draft, quality = (data[k] for k in ("job", "evaluation", "draft", "quality"))
    description = str(job.get("description") or "")
    listing_skills = extract_skills(description)
    resume_skills = extract_skills(resume_text or "") if resume_text is not None else set()
    relevant = listing_skills & resume_skills
    explicit_missing = _json_list(evaluation.get("missing_requirements_json")) if evaluation else []
    uncertainties = _json_list(evaluation.get("uncertainties_json")) if evaluation else []
    mismatches = _json_list(evaluation.get("mismatches_json")) if evaluation else []
    reasons = _json_list(evaluation.get("reasons_json")) if evaluation else []
    try:
        breakdown = json.loads(evaluation.get("breakdown_json") or "{}") if evaluation else {}
    except (TypeError, json.JSONDecodeError):
        breakdown = {}

    lines = [f"# Application Review Pack — {job.get('company') or 'Company not supplied'} / {job.get('title') or 'Title not supplied'}", "",
             f"Generated: {generated_at}", f"Job ID: {job['id']}",
             f"Tracker status (stored; unchanged): {job['tracker_status']}", "",
             "## Stored listing facts", "",
             f"- Title: {job.get('title') or 'Unverified / not stored'}",
             f"- Company: {job.get('company') or 'Unverified / not stored'}",
             f"- Source URL (not opened): `{job.get('url') or 'not supplied'}`",
             f"- Location: {job.get('location') or 'Not supplied'}",
             f"- Experience requirement: {job.get('experience_requirement') or 'Not supplied'}",
             f"- Salary information: {job.get('salary') or 'Not supplied; no amount inferred'}", "",
             "## Job description and known requirements", ""]
    lines.append(description.strip() if description.strip() else "No stored job description; requirements are Unverified.")
    supplied_skills = job.get("intake_required_skills") or []
    lines += ["", "- Catalogued skills mentioned in stored description: " + (", ".join(sorted(listing_skills)) or "none detected by the local catalog"),
              "- Required skills supplied separately with the vacancy: " + (", ".join(map(str, supplied_skills)) or "not supplied"),
              "- Catalogued listing skills not located in extracted resume text (uncertain; absence from text is not proof of lacking a skill): " + (", ".join(sorted(listing_skills - resume_skills)) if resume_text is not None else "Unverified because resume text could not be read"),
              "- Requirements explicitly marked missing in saved match evaluation: " + ("; ".join(map(str, explicit_missing)) or "none recorded"),
              "- Saved match uncertainties: " + ("; ".join(map(str, uncertainties)) or "none recorded"),
              "- Saved qualification mismatches: " + ("; ".join(map(str, mismatches)) or "none recorded"), "",
              "## Match assessment", ""]
    if evaluation:
        lines.append(f"- Stored score: {evaluation['score']}/100 — heuristic, not a hiring probability.")
        lines.append(f"- Saved component breakdown: {json.dumps(breakdown, ensure_ascii=False, sort_keys=True)}")
        lines.extend(f"- Stored explanation: {reason}" for reason in reasons)
        if breakdown.get("source"):
            lines.append(f"- Component limitation: {breakdown['source']}")
    else:
        lines.append("No stored evaluation; score and explanation are Unverified.")
    lines += ["", "## Local resume evidence", ""]
    if resume_text is None:
        lines.append("Canonical resume could not be read locally; resume evidence is Unverified.")
    elif relevant:
        lines.append("The excerpts below are extracted from the local resume; only high-confidence word-boundary whitespace may be normalized, with all non-whitespace characters preserved. Their relevance is a heuristic based on skill overlap, not an independently verified qualification.")
        for excerpt in _excerpt_skills(resume_text, relevant):
            lines.append(f"- Matched catalogued skill(s): {', '.join(excerpt['skills'])}; resume excerpt: “{excerpt['text']}”")
    else:
        lines.append("No catalogued skill overlap was extracted. This does not prove the candidate lacks relevant experience.")
    lines.append("- Resume text is local document evidence; the checker does not independently verify the claims in it.")
    verification = data["resume_verification"]
    if verification:
        lines.append(f"- Stored site-resume observation: {verification['site_status']}; observed={bool(verification['site_resume_observed'])}; evidence source={verification['evidence_source']}; independent verification={bool(verification['independent_verification'])}; checked={verification['checked_at']}.")
        lines.append(f"- Stored resume filename state: {verification['filename_status']}; content comparison: {verification['content_status']}.")
    else:
        lines.append("- Site resume observation: Unverified; no saved site-check record was found.")
    review = data["application_review"]
    lines += ["", "## User-confirmed review evidence", ""]
    if review:
        lines.append(f"A user-confirmed job-description review is recorded for evaluation #{review['evaluation_id']} at {review['reviewed_at']}. This records a user confirmation, not independent verification.")
    else:
        lines.append("No user-confirmed review record tied to the latest evaluation was found.")

    lines += ["", "## Preparation drafts and latest quality check", ""]
    if draft:
        for label, key in (("Preparation report", "improvement_report_path"), ("Cover-letter draft", "cover_letter_path"), ("Question-answer draft", "question_answers_path")):
            path_value = draft.get(key)
            exists = Path(path_value).is_file() if path_value else False
            lines.append(f"- {label}: {path_value or 'Not generated'} ({'file exists' if exists else 'unavailable'})")
    else:
        lines.append("No application-preparation draft record was found.")
    if quality:
        findings = _json_list(quality.get("findings_json"))
        lines.append(f"- Latest quality report #{quality['id']} for draft #{quality['draft_id']}: {quality['report_path']} (generated {quality['generated_at']}).")
        if findings:
            for item in findings:
                lines.append(f"  - {item.get('classification', 'Unverified')} / {item.get('section', 'section')}: {item.get('finding', 'finding text unavailable')}")
        else:
            lines.append("  - It recorded zero findings under deterministic checks; this does not prove every claim is accurate.")
    else:
        lines.append("- Latest quality check: Unverified / no report linked to the latest preparation draft.")

    lines += ["", "## Human review checklist", "",
              "- [ ] Confirm the listing is still available by checking the source yourself; this pack did not open it.",
              "- [ ] Confirm eligibility, work authorization, and any employer-specific requirements.",
              "- [ ] Confirm experience requirements and that the resume accurately supports every claim.",
              "- [ ] Confirm location, work arrangement, and willingness/ability to meet it.",
              "- [ ] Review salary information if the listing provides it; no missing salary amount was inferred.",
              "- [ ] Check factual accuracy and wording in every generated draft; resolve all uncertain findings.",
              "- [ ] Review final resume and application documents before any use.",
              "- [ ] Record your decision explicitly with `application-review-decision`; this does not change tracker status.", "",
              "## Prior review decisions", ""]
    if data["decisions"]:
        for decision in reversed(data["decisions"]):
            lines.append(f"- {decision['recorded_at']}: **{decision['decision']}** (review pack #{decision['pack_id']})" + (f" — {decision['note']}" if decision["note"] else ""))
    else:
        lines.append("No review decision recorded.")
    lines += ["", "This local review pack does not open the source URL, access Naukri, submit an application, or update tracker status.", ""]
    return "\n".join(lines)


def create_application_review_pack(job_id: int, *, database_path: Path | None = None,
                                   resume_path: Path | None = None,
                                   output_dir: Path | None = None,
                                   resume_text: str | None = None) -> dict:
    data = _load_sources(job_id, database_path)
    if resume_text is None:
        path = Path(resume_path) if resume_path is not None else settings.resume_path
        try:
            resume_text = extract_resume_text(path)
        except Exception:
            resume_text = None
    generated_at = _now()
    content = _render_pack(data, resume_text, generated_at)
    root = Path(output_dir) if output_dir is not None else Path("data/application_review_packs")
    root = root / f"job_{job_id}"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y%m%d_%H%M%S_%f")
    report_path = root / f"review_pack_{stamp}.md"
    report_path.write_text(content, encoding="utf-8")
    with connect(database_path) as conn:
        cursor = conn.execute(
            "INSERT INTO application_review_packs(job_id,generated_at,report_path,source_draft_id,quality_report_id) VALUES(?,?,?,?,?)",
            (job_id, generated_at, str(report_path.resolve()), data["draft"]["id"] if data["draft"] else None,
             data["quality"]["id"] if data["quality"] else None),
        )
        pack_id = int(cursor.lastrowid)
    return {"pack_id": pack_id, "job_id": job_id, "report_path": report_path.resolve(),
            "status_unchanged": data["job"]["tracker_status"], "draft_id": data["draft"]["id"] if data["draft"] else None,
            "quality_report_id": data["quality"]["id"] if data["quality"] else None}


def record_application_review_decision(job_id: int, decision: str, *, note: str = "",
                                        database_path: Path | None = None,
                                        recorded_at: str | None = None) -> dict:
    if decision not in REVIEW_DECISIONS:
        raise ValueError(f"Decision must be one of: {', '.join(REVIEW_DECISIONS)}")
    clean_note = " ".join(note.split())
    if len(clean_note) > 1000:
        raise ValueError("Review decision note must be 1000 characters or fewer")
    with connect(database_path) as conn:
        pack = conn.execute("SELECT id FROM application_review_packs WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        if pack is None:
            raise KeyError(f"No review pack exists for job {job_id}; generate one first")
        cursor = conn.execute(
            "INSERT INTO application_review_decisions(job_id,pack_id,decision,recorded_at,note) VALUES(?,?,?,?,?)",
            (job_id, pack["id"], decision, recorded_at or _now(), clean_note),
        )
        review_id = int(cursor.lastrowid)
        status = conn.execute("SELECT status FROM application_tracker WHERE job_id=?", (job_id,)).fetchone()["status"]
    return {"review_id": review_id, "pack_id": pack["id"], "job_id": job_id,
            "decision": decision, "status_unchanged": status}
