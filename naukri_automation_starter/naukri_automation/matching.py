"""Transparent, deterministic resume-to-job matching (no AI/API required)."""
import re
from datetime import datetime

from .config import settings

STOPWORDS = {
    "and", "the", "for", "with", "from", "your", "you", "our", "are", "will",
    "have", "has", "this", "that", "to", "of", "in", "on", "a", "an", "or",
    "as", "by", "at", "is", "be", "we", "it", "experience", "years", "work",
    "role", "job", "team", "ability", "strong", "good", "including", "using",
}


def tokens(text: str) -> set[str]:
    """Extract useful technical and role terms, preserving terms such as C++ and C#."""
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9+#.]{1,}", (text or "").lower())
    return {word.strip(".#") for word in words if word not in STOPWORDS and len(word.strip(".#")) > 1}


def score_job(resume_text: str, job_description: str, title: str = "") -> tuple[int, str]:
    """Return a transparent 0–100 lexical overlap score and its explanation.

    This is a baseline, not a probability of getting hired. It deliberately avoids
    external services and can be replaced by semantic/LLM matching later.
    """
    resume_terms = tokens(resume_text)
    description_terms = tokens(f"{title} {job_description}")
    if not resume_terms or not description_terms:
        return 0, "Insufficient resume or job-description text"

    shared = sorted(resume_terms & description_terms)
    # Score against a capped subset of job terms so long descriptions don't dominate.
    denominator = max(1, min(len(description_terms), 40))
    score = min(100, round(100 * len(shared) / denominator))
    reason = "Shared terms: " + (", ".join(shared[:15]) if shared else "none")
    return score, reason


# Conservative lexical catalog: a skill is recorded only when an alias occurs
# explicitly in the supplied resume/job text. This is not an exhaustive ontology.
SKILL_ALIASES = {
    "Python": ("python",), "Java": ("java",), "C#": ("c#", "c sharp"),
    ".NET": (".net", "dotnet"), "ASP.NET": ("asp.net", "asp net"),
    "C++": ("c++",), "JavaScript": ("javascript", "js"), "TypeScript": ("typescript",),
    "SQL": ("sql",), "PostgreSQL": ("postgresql", "postgres"), "MySQL": ("mysql",),
    "MongoDB": ("mongodb",), "Redis": ("redis",), "React": ("react", "react.js"),
    "Node.js": ("node.js", "nodejs"), "FastAPI": ("fastapi",), "Flask": ("flask",),
    "Django": ("django",), "REST API": ("rest api", "restful api"), "GraphQL": ("graphql",),
    "AWS": ("aws", "amazon web services"), "Azure": ("azure",), "GCP": ("gcp", "google cloud platform"),
    "Docker": ("docker",), "Kubernetes": ("kubernetes", "k8s"), "Git": ("git",),
    "Machine Learning": ("machine learning",), "Deep Learning": ("deep learning",),
    "TensorFlow": ("tensorflow",), "PyTorch": ("pytorch",), "NLP": ("nlp", "natural language processing"),
    "Data Analysis": ("data analysis", "data analytics"), "Pandas": ("pandas",),
    "NumPy": ("numpy",), "Tableau": ("tableau",), "Power BI": ("power bi",),
    "Apache Spark": ("apache spark", "spark"), "Hadoop": ("hadoop",),
    "HTML": ("html",), "CSS": ("css",), "Linux": ("linux",),
}
_REQUIREMENT_CUES = re.compile(
    r"\b(required|required skill|must have|must be|need to|proficien(?:t|cy)|minimum|mandatory|requirement)\b",
    re.IGNORECASE,
)


def extract_skills(text: str) -> set[str]:
    normalized = (text or "").casefold()
    found = set()
    for canonical, aliases in SKILL_ALIASES.items():
        if any(re.search(r"(?<![\w])" + re.escape(alias.casefold()) + r"(?![\w])", normalized) for alias in aliases):
            found.add(canonical)
    return found


def extract_experience_years(text: str) -> float | None:
    """Return only an explicitly stated numeric experience duration."""
    patterns = (
        r"\b(\d+(?:\.\d+)?)(?:\s*[-–]\s*\d+(?:\.\d+)?)?\s*\+?\s*(?:years?|yrs?)\b",
        r"\b(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:professional\s+)?(?:work\s+)?experience\b",
        r"\bexperience\s*(?:of|:)?\s*(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\b",
        r"\b(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\s+in\s+(?:software|backend|data|development|engineering)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text or "", re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def _required_skills(description: str, skills: set[str]) -> set[str]:
    required = set()
    text = description or ""
    for sentence in re.split(r"[\n.;]+", text):
        if not _REQUIREMENT_CUES.search(sentence):
            continue
        required.update(extract_skills(sentence) & skills)
    return required


def extract_required_skills(description: str) -> set[str]:
    """Return catalogued skills appearing in sentences with explicit requirement cues."""
    mentioned = extract_skills(description)
    return _required_skills(description, mentioned)


def _role_match(title: str, target_roles: tuple[str, ...]) -> tuple[float, str | None]:
    title_tokens = set(re.findall(r"[a-z0-9]+", (title or "").casefold()))
    if not title_tokens:
        return 50.0, None
    for role in target_roles:
        role_tokens = set(re.findall(r"[a-z0-9]+", role.casefold()))
        if role_tokens and role_tokens.issubset(title_tokens):
            return 100.0, role
    return 0.0, None


def evaluate_job_match(resume_text: str, job: dict, *, target_roles=None,
                       weights: tuple[int, int, int] | None = None,
                       mismatch_penalty: int | None = None,
                       evaluated_at: str | None = None) -> dict:
    """Evaluate a supplied listing with explicit components and uncertainty."""
    roles = tuple(target_roles or settings.target_roles)
    role_weight, skill_weight, experience_weight = weights or (
        settings.match_role_weight, settings.match_skill_weight, settings.match_experience_weight,
    )
    penalty = settings.match_mismatch_penalty if mismatch_penalty is None else mismatch_penalty
    if min(role_weight, skill_weight, experience_weight, penalty) < 0 or role_weight + skill_weight + experience_weight != 100 or penalty > 100:
        raise ValueError("Scoring weights must be non-negative, total 100, and penalty at most 100")

    description = str(job.get("description") or "")
    title = str(job.get("title") or "")
    role_value, matched_role = _role_match(title, roles)
    resume_skills = extract_skills(resume_text)
    job_skills = extract_skills(description)
    supplied_skills = job.get("required_skills") or []
    if isinstance(supplied_skills, str):
        supplied_skills = [supplied_skills]
    supplied_skills = extract_skills(" ".join(str(item) for item in supplied_skills))
    job_skills |= supplied_skills
    required_skills = _required_skills(description, job_skills) | supplied_skills
    scored_skills = required_skills or job_skills
    if scored_skills:
        overlap = scored_skills & resume_skills
        skill_value = 100 * len(overlap) / len(scored_skills)
    else:
        overlap, skill_value = set(), 50.0

    resume_years = extract_experience_years(resume_text)
    requirement_text = str(job.get("experience_requirement") or "")
    job_years = extract_experience_years(requirement_text) if requirement_text else None
    if job_years is None:
        job_years = extract_experience_years(description)
    mismatches: list[str] = []
    uncertainties: list[str] = []
    if resume_years is not None and job_years is not None:
        experience_value = 100.0 if resume_years >= job_years else 0.0
        if resume_years < job_years:
            mismatches.append(
                f"Explicit experience gap: resume text states {resume_years:g} years; listing requires at least {job_years:g}."
            )
    else:
        experience_value = 50.0
        uncertainties.append("Experience compatibility is uncertain because a numeric duration is not explicit on both sides.")
    if not title:
        uncertainties.append("No title was supplied; target-role alignment is uncertain.")
    if not job_skills:
        uncertainties.append("No catalogued skills were recognized in the listing text.")
    if not required_skills and job_skills:
        uncertainties.append("No catalogued skills were explicitly marked as required; all recognized listing skills are used as a proxy.")

    breakdown = {
        "target_role": round(role_value), "required_skill_overlap": round(skill_value),
        "experience_compatibility": round(experience_value),
        "weights": {"target_role": role_weight, "required_skill_overlap": skill_weight,
                    "experience_compatibility": experience_weight},
        "mismatch_penalty": penalty if mismatches else 0,
    }
    raw = (role_value * role_weight + skill_value * skill_weight + experience_value * experience_weight) / 100
    score = max(0, min(100, round(raw) - (penalty if mismatches else 0)))
    missing = sorted(required_skills - resume_skills)
    reasons = []
    if matched_role:
        reasons.append(f"Title aligns with configured target role: {matched_role}.")
    elif title:
        reasons.append("Title did not match a configured target role using exact token alignment.")
    if overlap:
        reasons.append("Resume text explicitly contains overlapping skills: " + ", ".join(sorted(overlap)) + ".")
    elif scored_skills:
        reasons.append("No overlap was found between recognized listing skills and resume text.")
    else:
        reasons.append("Skill comparison used a neutral score because the listing contains no recognized skills.")
    if resume_years is not None and job_years is not None and not mismatches:
        reasons.append(f"Explicit experience duration meets the stated minimum ({resume_years:g} vs {job_years:g} years).")
    for skill in missing:
        uncertainties.append(f"Required skill not found in resume text: {skill} (absence from text does not prove the candidate lacks it).")
    if mismatches:
        reasons.extend(mismatches)
    return {
        "evaluated_at": evaluated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "score": score, "breakdown": breakdown, "reasons": reasons,
        "matched_skills": sorted(overlap), "missing_requirements": missing,
        "uncertainties": uncertainties, "mismatches": mismatches,
        "limitations": "Transparent lexical heuristic; not a hiring probability or validated measure of real-world job fit.",
    }
