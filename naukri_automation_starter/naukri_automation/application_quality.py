"""Local checker for application-preparation drafts; no site actions are performed."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import settings
from .db import connect
from .matching import extract_required_skills, extract_skills
from .resume import extract_resume_text

_DATE_RANGE = re.compile(r"\b((?:19|20)\d{2})\s*[-–—]\s*((?:19|20)\d{2}|present|current)\b", re.I)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_QUALIFIED_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\s*(?:%|percent|years?|months?|projects?|clients?|users?|hours?)\b|[$₹]\s*\d+(?:,\d{3})*(?:\.\d+)?", re.I)
_CLAIM_CUES = re.compile(r"\b(i have|i am proficient|i worked|i built|i led|i delivered|i developed|my experience|my skills|experienced in|proficient in|implemented|reduced|increased|improved|managed)\b", re.I)
_DATE_STOPWORDS = {"employment", "experience", "education", "degree", "university", "college", "school", "from", "to", "at", "the", "and", "of", "in", "as", "role", "resume", "draft", "period", "worked", "present", "current"}


def _now() -> str:
    return datetime.now(ZoneInfo(settings.timezone)).isoformat(timespec="seconds")


def _read_draft(draft_path: str | None, *, max_bytes: int = 1_000_000) -> str | None:
    if not draft_path:
        return None
    path = Path(draft_path)
    if not path.is_file():
        return None
    if path.stat().st_size > max_bytes:
        raise ValueError("A stored draft exceeds the 1 MB quality-check limit")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _answer_blocks(text: str | None) -> list[dict]:
    if not text:
        return []
    blocks = re.split(r"(?m)^## Question[ \t]+\d+[ \t]*$", text)
    results = []
    for block in blocks[1:]:
        question = re.search(r"(?s)^\s*(.*?)\n\s*Draft answer:", block)
        answer = re.search(r"(?m)^Draft answer:[ \t]*(.*)$", block)
        results.append({"question": question.group(1).strip() if question else "Question text unavailable",
                        "answer": answer.group(1).strip() if answer else ""})
    return results


def _requirement_coverage_text(cover: str | None, answers: str | None) -> str:
    # Verbatim excerpts in a cover draft can address a requirement even though they
    # are excluded from the unsupported-claim scan as already sourced resume evidence.
    parts = [cover or ""]
    parts.extend(item["answer"] for item in _answer_blocks(answers))
    return "\n".join(parts)


def _date_records(text: str) -> list[dict]:
    records = []
    for line in text.splitlines():
        match = _DATE_RANGE.search(line)
        if not match:
            continue
        tokens = {t.casefold() for t in re.findall(r"[A-Za-z][A-Za-z0-9]+", _DATE_RANGE.sub(" ", line))
                  if t.casefold() not in _DATE_STOPWORDS}
        category = "education" if re.search(r"education|degree|university|college|school", line, re.I) else "employment"
        records.append({"line": " ".join(line.split()), "start": match.group(1), "end": match.group(2).casefold(),
                        "tokens": tokens, "category": category})
    return records


def _findings(job: dict, resume_text: str | None, report_text: str | None,
              cover_text: str | None, answers_text: str | None) -> list[dict]:
    findings = []

    def add(classification: str, kind: str, section: str, message: str, requirement: str | None = None):
        findings.append({"classification": classification, "kind": kind, "section": section,
                         "requirement": requirement, "finding": message})

    answer_blocks = _answer_blocks(answers_text)
    for index, block in enumerate(answer_blocks, start=1):
        answer = block["answer"]
        if not answer or "[your input needed" in answer.casefold():
            add("Confirmed issue", "missing answer", f"Question answers / Question {index}",
                "This supplied question has no substantive draft answer. Add your own verified response before use.", block["question"])

    resume_skills = extract_skills(resume_text or "") if resume_text is not None else set()
    for index, block in enumerate(answer_blocks, start=1):
        answer = block["answer"]
        if not answer or "[your input needed" in answer.casefold():
            continue
        for skill in sorted(extract_skills(answer) - resume_skills):
            add("Uncertain — user review required", "resume support", f"Question answers / Question {index}",
                f"The answer mentions {skill}, which was not located in extracted resume text. Confirm this claim; absence from text does not prove the qualification is absent.", block["question"])
        if resume_text is None:
            add("Uncertain — user review required", "resume evidence unavailable", f"Question answers / Question {index}",
                "Resume text could not be read, so this answer could not be compared with local evidence.", block["question"])
        else:
            for number in _QUALIFIED_NUMBER.findall(answer):
                if " ".join(number.casefold().split()) not in resume_text.casefold():
                    add("Uncertain — user review required", "unsupported quantified claim", f"Question answers / Question {index}",
                        "A quantified claim is not corroborated by extracted resume text; verify its source before use.", block["question"])

    if cover_text:
        evidence_mode = False
        for line in cover_text.splitlines():
            if line.startswith("The following potentially relevant wording"):
                evidence_mode = True
                continue
            if evidence_mode and not line.strip():
                evidence_mode = False
                continue
            if evidence_mode or line.startswith("#") or line.startswith("["):
                continue
            if line.startswith("I would like to be considered") or line.startswith("I would welcome the opportunity"):
                continue
            if not _CLAIM_CUES.search(line):
                continue
            for skill in sorted(extract_skills(line) - resume_skills):
                add("Uncertain — user review required", "resume support", "Cover letter",
                    f"The cover-letter wording mentions {skill}, which was not located in extracted resume text. Confirm it before use; absence from text does not prove the qualification is absent.")
            if resume_text is None:
                add("Uncertain — user review required", "resume evidence unavailable", "Cover letter",
                    "Resume text could not be read, so factual claims in this section could not be checked.")
            else:
                for number in _QUALIFIED_NUMBER.findall(line):
                    normalized = " ".join(number.casefold().split())
                    if normalized not in resume_text.casefold():
                        add("Uncertain — user review required", "unsupported quantified claim", "Cover letter",
                            "A quantified claim is not corroborated by the extracted resume text; verify its source before use.")

    # Compare employment/education date ranges only when the same identifiable entity
    # appears in both sources. A mismatch is actionable; unrelated or missing dates stay uncertain.
    resume_dates = _date_records(resume_text or "") if resume_text is not None else []
    draft_date_lines = []
    if cover_text:
        in_evidence = False
        for line in cover_text.splitlines():
            if line.startswith("The following potentially relevant wording"):
                in_evidence = True
                continue
            if in_evidence and not line.strip():
                in_evidence = False
                continue
            if not in_evidence:
                draft_date_lines.append(("Cover letter", line))
    for index, block in enumerate(answer_blocks, start=1):
        draft_date_lines.append((f"Question answers / Question {index}", block["answer"]))
    for section, line in draft_date_lines:
        for proposed in _date_records(line):
            if resume_text is None:
                add("Uncertain — user review required", "date evidence unavailable", section,
                    "A date range appears in the draft, but resume text could not be read to check it.")
                continue
            candidates = [record for record in resume_dates if record["category"] == proposed["category"]
                          and len(record["tokens"] & proposed["tokens"]) >= min(2, len(record["tokens"]), len(proposed["tokens"]))
                          and record["tokens"] and proposed["tokens"]]
            if candidates:
                if not any((record["start"], record["end"]) == (proposed["start"], proposed["end"]) for record in candidates):
                    add("Confirmed issue", "date inconsistency", section,
                        f"The {proposed['category']} date range differs from a resume range for the same identifiable entry. Align it with the source record before use.")
            else:
                add("Uncertain — user review required", "date not corroborated", section,
                    f"The {proposed['category']} date range is not linked to a matching dated entry in the extracted resume. Verify it before use.")

    if resume_text is not None:
        required = extract_required_skills(str(job.get("description") or ""))
        coverage_text = _requirement_coverage_text(cover_text, answers_text).casefold()
        saved_requirements = {str(item) for item in job.get("saved_missing_requirements", []) if str(item).strip()}
        for requirement in sorted(required | saved_requirements):
            if requirement.casefold() not in coverage_text:
                add("Uncertain — user review required", "job requirement not addressed", "Cover letter / question answers",
                    f"The draft materials do not address this stored/recognized requirement. Consider addressing it only if you can substantiate it; the resume omission is not proof you lack it.", requirement)
    else:
        add("Uncertain — user review required", "resume evidence unavailable", "Overall",
            "Canonical resume text could not be read; support checks are incomplete.")

    if not cover_text and not answers_text:
        add("Uncertain — user review required", "no application-facing draft", "Overall",
            "Only the preparation report exists; no cover-letter or question-answer draft was available for claim/coverage review.")
    if report_text is None:
        add("Uncertain — user review required", "preparation report unavailable", "Preparation report",
            "The saved improvement-report path is missing or unreadable.")
    return findings


def _render_quality_report(job: dict, draft: dict, findings: list[dict], checked_at: str) -> str:
    confirmed = [item for item in findings if item["classification"] == "Confirmed issue"]
    uncertain = [item for item in findings if item["classification"] != "Confirmed issue"]
    lines = [f"# Application quality report — {job['company'] or 'Company not supplied'}: {job['title'] or 'Title not supplied'}", "",
             f"Checked: {checked_at}", f"Job ID: {job['job_id']}; preparation draft record: {draft['id']}",
             f"Preparation report: {draft['improvement_report_path']}",
             f"Cover-letter draft: {draft['cover_letter_path'] or 'not generated'}",
             f"Question-answer draft: {draft['question_answers_path'] or 'not generated'}", "",
             "Confirmed issues identify missing/inconsistent draft data, not candidate qualifications. Uncertain findings require your review; absence from extracted resume text is not proof a qualification is absent.", "",
             f"## Confirmed issues ({len(confirmed)})", ""]
    if confirmed:
        for item in confirmed:
            req = f" Requirement: {item['requirement']}." if item["requirement"] else ""
            lines.append(f"- **{item['section']} — {item['kind']}:** {item['finding']}{req}")
    else:
        lines.append("None detected by these deterministic checks.")
    lines += ["", f"## Uncertain items for your review ({len(uncertain)})", ""]
    if uncertain:
        for item in uncertain:
            req = f" Requirement: {item['requirement']}." if item["requirement"] else ""
            lines.append(f"- **{item['section']} — {item['kind']}:** {item['finding']}{req}")
    else:
        lines.append("None detected by these deterministic checks.")
    lines += ["", "This local heuristic report does not edit drafts, the canonical resume, tracker status, or Naukri. Review every draft manually before use.", ""]
    return "\n".join(lines)


def check_application_quality(job_id: int, *, database_path: Path | None = None,
                              resume_path: Path | None = None, output_dir: Path | None = None,
                              draft_id: int | None = None, resume_text: str | None = None) -> dict:
    """Check the latest (or selected) saved materials and append a local quality report."""
    with connect(database_path) as conn:
        job = conn.execute(
            """SELECT j.id,j.title,j.company,j.description,
                      e.id AS evaluation_id,e.missing_requirements_json,e.uncertainties_json,e.mismatches_json
               FROM jobs j LEFT JOIN job_match_evaluations e ON e.id=(
                   SELECT MAX(e2.id) FROM job_match_evaluations e2 WHERE e2.job_id=j.id
               ) WHERE j.id=?""", (job_id,),
        ).fetchone()
        if job is None:
            raise KeyError(f"Job {job_id} was not found")
        query = "SELECT * FROM application_material_drafts WHERE job_id=?"
        params: tuple = (job_id,)
        if draft_id is not None:
            query += " AND id=?"
            params = (job_id, draft_id)
        query += " ORDER BY id DESC LIMIT 1"
        draft_row = conn.execute(query, params).fetchone()
    if draft_row is None:
        raise KeyError(f"No saved application-preparation draft exists for job {job_id}")
    draft = dict(draft_row)
    report_text = _read_draft(draft.get("improvement_report_path"))
    cover_text = _read_draft(draft.get("cover_letter_path"))
    answers_text = _read_draft(draft.get("question_answers_path"))
    findings = []
    for field, label in (("improvement_report_path", "Preparation report"),
                         ("cover_letter_path", "Cover letter"),
                         ("question_answers_path", "Question answers")):
        if draft.get(field) and _read_draft(draft.get(field)) is None:
            findings.append({"classification": "Confirmed issue", "kind": "draft file unavailable",
                              "section": label, "requirement": None,
                              "finding": "A saved draft path is missing, unreadable, or exceeds the checker size limit. Restore or regenerate it before review."})
    if resume_text is None:
        canonical = Path(resume_path) if resume_path is not None else settings.resume_path
        try:
            resume_text = extract_resume_text(canonical)
        except Exception:
            resume_text = None
    job_data = dict(job)
    job_data["job_id"] = int(job["id"])
    try:
        job_data["saved_missing_requirements"] = json.loads(job_data.get("missing_requirements_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        job_data["saved_missing_requirements"] = []
    findings.extend(_findings(job_data, resume_text, report_text, cover_text, answers_text))
    checked_at = _now()
    root = Path(output_dir) if output_dir is not None else Path("data/application_quality")
    root = root / f"job_{job_id}"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y%m%d_%H%M%S_%f")
    report_path = root / f"quality_report_{stamp}.md"
    report_path.write_text(_render_quality_report(job_data, draft, findings, checked_at), encoding="utf-8")
    with connect(database_path) as conn:
        cursor = conn.execute(
            "INSERT INTO application_draft_quality_reports(job_id,draft_id,generated_at,report_path,findings_json) VALUES(?,?,?,?,?)",
            (job_id, draft["id"], checked_at, str(report_path.resolve()), json.dumps(findings, ensure_ascii=False, sort_keys=True)),
        )
        report_id = int(cursor.lastrowid)
    return {"report_id": report_id, "job_id": job_id, "draft_id": draft["id"],
            "report_path": report_path.resolve(), "findings": findings,
            "confirmed_issue_count": sum(item["classification"] == "Confirmed issue" for item in findings),
            "uncertain_count": sum(item["classification"] != "Confirmed issue" for item in findings)}
