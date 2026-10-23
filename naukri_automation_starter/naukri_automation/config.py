from dataclasses import dataclass
from pathlib import Path
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dotenv import load_dotenv

load_dotenv()

DEFAULT_TARGET_ROLES = (
    "SDE", "Backend", "Artificial Intelligence Engineer",
    "Fullstack Developer", "Data Analyst", "Data Scientist",
    "Python Developer", "DotNet Developer",
)

@dataclass(frozen=True)
class Settings:
    base_url: str = os.getenv("NAUKRI_BASE_URL", "https://www.naukri.com/")
    resume_path: Path = Path(os.getenv("RESUME_PATH", "./data/resume.pdf"))
    database_path: Path = Path(os.getenv("DATABASE_PATH", "./data/naukri_automation.sqlite3"))
    browser_profile_path: Path = Path(os.getenv("BROWSER_PROFILE_PATH", ".browser-profile"))
    browser_launch_timeout_ms: int = int(os.getenv("BROWSER_LAUNCH_TIMEOUT_MS", "30000"))
    browser_navigation_timeout_ms: int = int(os.getenv("BROWSER_NAVIGATION_TIMEOUT_MS", "30000"))
    browser_page_load_timeout_ms: int = int(os.getenv("BROWSER_PAGE_LOAD_TIMEOUT_MS", "10000"))
    upload_confirmation_timeout_ms: int = int(os.getenv("UPLOAD_CONFIRMATION_TIMEOUT_MS", "10000"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    live_submission: bool = os.getenv("LIVE_SUBMISSION", "false").strip().lower() == "true"
    timezone: str = os.getenv("TIMEZONE", "Asia/Kolkata")
    refresh_hour: int = int(os.getenv("PROFILE_REFRESH_HOUR", "9"))
    refresh_minute: int = int(os.getenv("PROFILE_REFRESH_MINUTE", "0"))
    target_roles: tuple[str, ...] = tuple(
        x.strip() for x in os.getenv("TARGET_ROLES", ",".join(DEFAULT_TARGET_ROLES)).split(",") if x.strip()
    )
    target_locations: tuple[str, ...] = tuple(x.strip() for x in os.getenv("TARGET_LOCATIONS", "India,Remote").split(",") if x.strip())
    min_match_score: int = int(os.getenv("MIN_MATCH_SCORE", "60"))
    notice_period_preference: str = os.getenv("NOTICE_PERIOD_PREFERENCE", "Immediate joiner").strip()
    salary_preference: str = os.getenv("SALARY_PREFERENCE", "Flexible for the right role").strip()
    # Project heuristics only; these are not Naukri ranking rules.
    profile_review_current_days: int = int(os.getenv("PROFILE_REVIEW_CURRENT_DAYS", "30"))
    profile_review_due_days: int = int(os.getenv("PROFILE_REVIEW_DUE_DAYS", "90"))
    match_role_weight: int = int(os.getenv("MATCH_ROLE_WEIGHT", "25"))
    match_skill_weight: int = int(os.getenv("MATCH_SKILL_WEIGHT", "55"))
    match_experience_weight: int = int(os.getenv("MATCH_EXPERIENCE_WEIGHT", "20"))
    match_mismatch_penalty: int = int(os.getenv("MATCH_MISMATCH_PENALTY", "20"))
    jobicy_results_per_role: int = int(os.getenv("JOBICY_RESULTS_PER_ROLE", "5"))
    jobicy_request_timeout_seconds: int = int(os.getenv("JOBICY_REQUEST_TIMEOUT_SECONDS", "15"))
    jobicy_total_timeout_seconds: int = int(os.getenv("JOBICY_TOTAL_TIMEOUT_SECONDS", "60"))

    def __post_init__(self):
        if not self.target_roles or any(not role.strip() for role in self.target_roles):
            raise ValueError("TARGET_ROLES must contain at least one non-empty role")
        normalized_roles = [role.casefold() for role in self.target_roles]
        if len(normalized_roles) != len(set(normalized_roles)):
            raise ValueError("TARGET_ROLES must not contain duplicates")
        if self.notice_period_preference.casefold() != "immediate joiner":
            raise ValueError("NOTICE_PERIOD_PREFERENCE must be 'Immediate joiner'; do not invent an availability date")
        if self.salary_preference.casefold() != "flexible for the right role":
            raise ValueError("SALARY_PREFERENCE must be 'Flexible for the right role'; numerical values are not configured")
        if self.profile_review_current_days < 1 or self.profile_review_due_days <= self.profile_review_current_days:
            raise ValueError("Profile review thresholds must satisfy 1 <= current days < review due days")
        if min(self.match_role_weight, self.match_skill_weight, self.match_experience_weight) < 0 or (
            self.match_role_weight + self.match_skill_weight + self.match_experience_weight != 100
        ):
            raise ValueError("MATCH_ROLE_WEIGHT, MATCH_SKILL_WEIGHT, and MATCH_EXPERIENCE_WEIGHT must be non-negative and total 100")
        if not 0 <= self.match_mismatch_penalty <= 100:
            raise ValueError("MATCH_MISMATCH_PENALTY must be between 0 and 100")
        if not 1 <= self.jobicy_results_per_role <= 20:
            raise ValueError("JOBICY_RESULTS_PER_ROLE must be between 1 and 20")
        if not 1 <= self.jobicy_request_timeout_seconds <= 60:
            raise ValueError("JOBICY_REQUEST_TIMEOUT_SECONDS must be between 1 and 60")
        if not self.jobicy_request_timeout_seconds <= self.jobicy_total_timeout_seconds <= 300:
            raise ValueError("JOBICY_TOTAL_TIMEOUT_SECONDS must be between the request timeout and 300")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("TIMEZONE must be a valid IANA timezone") from None

settings = Settings()
settings.database_path.parent.mkdir(parents=True, exist_ok=True)
Path("logs").mkdir(exist_ok=True)
