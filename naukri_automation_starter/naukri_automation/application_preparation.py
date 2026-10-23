"""Evidence-grounded, local-only application-material draft generation."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import settings
from .db import connect
from .matching import extract_experience_years, extract_required_skills, extract_skills
from .resume import extract_resume_text


def _timestamp() -> str:
    return datetime.now(ZoneInfo(settings.timezone)).isoformat(timespec="seconds")


def _file_stamp() -> str:
    return datetime.now(ZoneInfo(settings.timezone)).strftime("%Y%m%d_%H%M%S_%f")


def _text_parts(text: str) -> list[str]:
    return [" ".join(part.split()) for part in re.split(r"\n+", text or "") if part.strip()]


def _evidence_for_skills(resume_text: str, skills: set[str]) -> list[dict]:
    evidence = []
    seen = set()
    for part in _text_parts(resume_text):
        supported = sorted(extract_skills(part) & skills)
        if supported and part not in seen:
            seen.add(part)
            evidence.append({"skills": supported, "text": part[:700]})
    return evidence


def _read_job(job_id: int, database_path: Path | None) -> dict:
    with connect(database_path) as conn:
        row = conn.execute(
            """SELECT j.id,j.title,j.company,j.location,j.url,j.description,j.experience_requirement,j.salary,
                      t.status,e.id AS evaluation_id,e.score,e.reasons_json,e.missing_requirements_json,
                      e.uncertainties_json,e.mismatches_json,e.job_snapshot_json
               FROM jobs j JOIN application_tracker t ON t.job_id=j.id
               LEFT JOIN job_match_evaluations e ON e.id=(
                   SELECT MAX(e2.id) FROM job_match_evaluations e2 WHERE e2.job_id=j.id
               ) WHERE j.id=?""", (job_id,),
        ).fetchone()
    if row is None:
        raise KeyError(f"Tracked job {job_id} was not found")
    job = dict(row)
    snapshot = {}
    try:
        snapshot = json.loads(job.get("job_snapshot_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        pass
    # Match explanations and description are tied to the latest saved evaluation when possible.
    for field in ("title", "company", "location", "url", "description", "experience_requirement", "salary"):
        if snapshot.get(field) not in (None, ""):
            job[field] = snapshot[field]
    for field in ("reasons_json", "missing_requirements_json", "uncertainties_json", "mismatches_json"):
        try:
            job[field.removesuffix("_json")] = json.loads(job.get(field) or "[]")
        except (TypeError, json.JSONDecodeError):
            job[field.removesuffix("_json")] = []
    return job


def _question_answer(question: str, *, job: dict, evidence: list[dict], resume_text: str) -> tuple[str, str]:
    q = question.casefold()
    if any(term in q for term in ("notice period", "availability", "joining", "joiner")):
        return settings.notice_period_preference, "Explicitly configured preference."
    if any(term in q for term in ("salary", "compensation", "expected ctc", "pay expectation")):
        return settings.salary_preference, "Explicitly configured preference; no numeric amount is configured."
    if any(term in q for term in ("how many years", "years of experience", "experience duration")):
        years = extract_experience_years(resume_text)
        if years is not None:
            return f"My resume text states {years:g} years of experience.", "Numeric duration found in local resume text; confirm it is still accurate."
    question_skills = extract_skills(question)
    relevant_evidence = [item for item in evidence if not question_skills or question_skills.intersection(item["skills"])]
    if question_skills and not relevant_evidence:
        return "[Your input needed: the supplied resume does not document the named qualification.]", "No matching resume evidence was found; do not claim it without confirmation."
    if relevant_evidence and any(term in q for term in ("skill", "technology", "technical", "project", "experience", "background", "achievement", "strength")):
        quotes = "; ".join(f"“{item['text']}”" for item in relevant_evidence[:3])
        return f"My resume documents: {quotes}", "Draft uses only verbatim resume excerpts selected for overlap with this listing; review the context before use."
    return "[Your input needed: the supplied resume and confirmed configuration do not establish an answer.]", "Insufficient supported information; no answer inferred."


def _render_report(job: dict, evaluation: dict, resume_text: str, evidence: list[dict],
                   listing_skills: set[str], required_skills: set[str], missing_required: set[str]) -> str:
    title = job["title"] or "Title not supplied"
    company = job["company"] or "Company not supplied"
    lines = [
        f"# Application preparation report — {company}: {title}", "",
        f"Tracker job ID: {job['id']}", f"Current tracker status (unchanged): {job['status']}",
        f"Source URL (not opened): {job['url'] or 'not supplied'}", "",
        "## Evidence boundaries", "",
        "Job requirements below come from the stored job description. Resume evidence is quoted from the canonical local resume. PDF text extraction may normalize high-confidence word-boundary whitespace; no non-whitespace characters or facts are rewritten. Role relevance is an inference from lexical overlap; it is not proof of qualification.", "",
        "## Job requirements", "",
    ]
    if not job["description"]:
        lines.append("No job description is stored; preparation cannot establish role requirements.")
    else:
        lines.append(job["description"].strip())
    lines += ["", "Explicitly cued catalogued requirements: " + (", ".join(sorted(required_skills)) or "none detected; manually review the full description."),
              "Other catalogued skills mentioned in the listing: " + (", ".join(sorted(listing_skills)) or "none detected."), "",
              "## Existing resume evidence potentially relevant to this role", ""]
    if evidence:
        for item in evidence:
            lines += [f"- Skill overlap: {', '.join(item['skills'])} (inferred relevance from listing/resume text overlap).",
                      f"  Resume evidence (verbatim): “{item['text']}”",
                      f"  Suggested wording (evidence only, verbatim): “{item['text']}”",
                      "  Suggested edit: move this existing wording earlier within the relevant role/project entry and preserve its scope and facts. No new claim is proposed."]
    else:
        lines.append("No resume text was found that overlaps with the catalogued listing skills. This does not establish that the candidate lacks those skills.")
    lines += ["", "## Missing or uncertain requirements", ""]
    if missing_required:
        lines.extend(f"- {skill}: explicitly cued in the job description but not located in extracted resume text. Confirm whether you have this qualification before adding any claim." for skill in sorted(missing_required))
    else:
        lines.append("No catalogued explicitly cued skill requirement was found missing in the resume text.")
    other_not_found = listing_skills - extract_skills(resume_text)
    if other_not_found:
        lines.append("Other listing skills not located in resume text (not necessarily required): " + ", ".join(sorted(other_not_found)) + ".")
    if job["missing_requirements"]:
        lines.append("Saved matcher gaps: " + "; ".join(job["missing_requirements"]) + ".")
    if job["uncertainties"]:
        lines.append("Saved matcher uncertainties: " + "; ".join(job["uncertainties"]) + ".")
    if job["mismatches"]:
        lines.append("Saved explicit mismatches: " + "; ".join(job["mismatches"]) + ".")
    years = extract_experience_years(resume_text)
    requirement = str(job["experience_requirement"] or job["description"] or "")
    required_years = extract_experience_years(requirement)
    lines += ["", "## Experience and qualifications", ""]
    if years is not None and required_years is not None:
        lines.append(f"Resume text states {years:g} years; listing text yields a parsed minimum of {required_years:g} years. Verify the source wording and any range interpretation manually.")
    else:
        lines.append("A numeric duration is not explicit on both sides; experience compatibility is unverified.")
    lines += ["", "## Claims needing your confirmation before use", "",
              "- Confirm each quoted resume excerpt is current and accurately reflects your work.",
              "- Confirm any requirement not found in resume text before making a claim about it.",
              "- Manually review the complete job description for uncatalogued requirements.",
              "- Add or confirm your name, contact details, dates, employment context, results, and any other facts yourself; this assistant has not inferred them.",
              "", "## Stored match evidence", "",
              f"Stored heuristic score: {job['score'] if job['score'] is not None else 'unavailable'} (not a hiring probability)."]
    if job["reasons"]:
        lines.extend(f"- {reason}" for reason in job["reasons"])
    if not evaluation:
        lines.append("No saved match evaluation is available.")
    lines += ["", "This is an editable preparation aid. It has not changed the canonical resume, tracker status, or any Naukri profile/application.", ""]
    return "\n".join(lines)


def prepare_application_materials(job_id: int, *, database_path: Path | None = None,
                                 resume_path: Path | None = None,
                                 questions_path: Path | None = None,
                                 output_dir: Path | None = None,
                                 include_cover_letter: bool = False,
                                 resume_text: str | None = None) -> dict:
    """Generate editable local drafts and append paths to tracker history; no external actions."""
    job = _read_job(job_id, database_path)
    if not str(job.get("description") or "").strip():
        raise ValueError("No stored job description is available; no application materials were generated")
    if resume_text is None:
        canonical = Path(resume_path) if resume_path is not None else settings.resume_path
        try:
            resume_text = extract_resume_text(canonical)
        except Exception as exc:
            # Parser exceptions vary by PDF library version; never echo document text/errors.
            raise ValueError(f"Canonical resume could not be read ({type(exc).__name__}); no materials were generated") from None
    if not resume_text.strip():
        raise ValueError("Canonical resume contains no extractable text; no application materials were generated")
    if questions_path is not None:
        question_file = Path(questions_path)
        questions_raw = question_file.read_text(encoding="utf-8-sig")
        if len(questions_raw) > 100_000:
            raise ValueError("Questions file is too large; maximum size is 100 KB")
        questions = [line.strip() for line in questions_raw.splitlines() if line.strip()]
        if len(questions) > 50:
            raise ValueError("Questions file has more than 50 questions")
    else:
        questions = []

    evaluation = {
        "reasons": job.get("reasons", []),
        "missing_requirements": job.get("missing_requirements", []),
        "uncertainties": job.get("uncertainties", []),
        "mismatches": job.get("mismatches", []),
    } if job.get("evaluation_id") is not None else None
    resume_skills = extract_skills(resume_text)
    listing_skills = extract_skills(job["description"])
    required_skills = extract_required_skills(job["description"])
    evidence = _evidence_for_skills(resume_text, listing_skills & resume_skills)
    missing_required = required_skills - resume_skills
    report_text = _render_report(job, evaluation, resume_text, evidence, listing_skills, required_skills, missing_required)

    root = Path(output_dir) if output_dir is not None else Path("data/application_materials")
    root = root / f"job_{job_id}"
    root.mkdir(parents=True, exist_ok=True)
    stamp = _file_stamp()
    report_path = root / f"preparation_report_{stamp}.md"
    report_path.write_text(report_text, encoding="utf-8")

    cover_path = None
    if include_cover_letter:
        evidence_text = "\n".join(f"- {', '.join(item['skills'])}: “{item['text']}”" for item in evidence[:5])
        cover = [f"# Draft cover letter — {job['title'] or 'role not supplied'} at {job['company'] or 'company not supplied'}", "",
                 "[Review and edit this draft before any use. No message has been sent.]", "", "Dear Hiring Team,", "",
                 f"I would like to be considered for the {job['title'] or '[role title]'} role at {job['company'] or '[company]'}.",
                 "", "The following potentially relevant wording is taken directly from my local resume:", "",
                 evidence_text or "[No relevant resume excerpt was identified; add only claims you can substantiate.]", "",
                 "I would welcome the opportunity to discuss how this documented experience relates to the role.", "",
                 "Sincerely,", "[Your name]", "",
                 "Review every statement for accuracy and add personal details only after confirming them.", ""]
        cover_path = root / f"cover_letter_{stamp}.md"
        cover_path.write_text("\n".join(cover), encoding="utf-8")

    answers_path = None
    if questions:
        answer_lines = [f"# Draft application-question answers — {job['company'] or 'company'} / {job['title'] or 'role'}", "",
                        "[Review and edit every answer. Unsupported facts are left as placeholders.]", ""]
        for index, question in enumerate(questions, start=1):
            answer, basis = _question_answer(question, job=job, evidence=evidence, resume_text=resume_text)
            answer_lines += [f"## Question {index}", "", question, "", f"Draft answer: {answer}", f"Evidence/basis: {basis}", ""]
        answers_path = root / f"question_answers_{stamp}.md"
        answers_path.write_text("\n".join(answer_lines), encoding="utf-8")

    generated_at = _timestamp()
    with connect(database_path) as conn:
        cursor = conn.execute(
            """INSERT INTO application_material_drafts
               (job_id,generated_at,improvement_report_path,cover_letter_path,question_answers_path)
               VALUES(?,?,?,?,?)""",
            (job_id, generated_at, str(report_path.resolve()),
             str(cover_path.resolve()) if cover_path else None,
             str(answers_path.resolve()) if answers_path else None),
        )
        draft_id = int(cursor.lastrowid)
    return {"draft_id": draft_id, "job_id": job_id, "generated_at": generated_at,
            "improvement_report_path": report_path.resolve(),
            "cover_letter_path": cover_path.resolve() if cover_path else None,
            "question_answers_path": answers_path.resolve() if answers_path else None,
            "status_unchanged": job["status"]}
