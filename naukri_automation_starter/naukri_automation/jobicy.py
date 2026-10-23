"""Bounded client for Jobicy's documented public remote-jobs API."""
from __future__ import annotations

from datetime import datetime
from html.parser import HTMLParser
import json
import math
import time
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .config import settings
from .db import connect, record_run
from .matching import extract_required_skills
from .vacancy_intake import _ingest, _load_resume

API_URL = "https://jobicy.com/api/v2/remote-jobs"
SOURCE_NAME = "jobicy"
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_MAX_DESCRIPTION_CHARS = 100_000


class JobicyFetchError(RuntimeError):
    """Safe, user-facing error that excludes response bodies and query values."""


def _log_event(stage: str, **safe_fields) -> None:
    event = {"timestamp": datetime.now(ZoneInfo(settings.timezone)).isoformat(timespec="seconds"),
             "stage": stage, **safe_fields}
    path = Path(__file__).resolve().parent.parent / "logs" / "jobicy_discovery.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag.casefold() in {"script", "style", "noscript"}:
            self._skip_depth += 1
        elif tag.casefold() in {"br", "p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag.casefold() in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag.casefold() in {"p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def _plain_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    parser = _VisibleText()
    parser.feed(value)
    parser.close()
    return " ".join("".join(parser.parts).split())


def _is_jobicy_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    from urllib.parse import urlsplit
    parsed = urlsplit(value.strip())
    return parsed.scheme == "https" and parsed.hostname in {"jobicy.com", "www.jobicy.com"} and bool(parsed.path)


def _salary_text(job: dict) -> str:
    def amount(*keys):
        for key in keys:
            value = job.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                return f"{value:g}"
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    low = amount("salaryMin", "annualSalaryMin")
    high = amount("salaryMax", "annualSalaryMax")
    currency = str(job.get("salaryCurrency") or "").strip()
    period = str(job.get("salaryPeriod") or ("yearly" if "annualSalaryMin" in job or "annualSalaryMax" in job else "")).strip()
    if not low and not high:
        return ""
    bounds = f"{low or 'not supplied'}–{high or 'not supplied'}"
    suffix = " ".join(item for item in (currency, period) if item)
    return f"{bounds} {suffix}".strip()


def parse_jobicy_payload(payload: object) -> tuple[list[dict], list[dict]]:
    """Normalize actual API fields; leave absent facts empty and reject unsafe URLs."""
    if not isinstance(payload, dict):
        raise JobicyFetchError("Jobicy response was not a JSON object")
    if payload.get("success") is False:
        raise JobicyFetchError("Jobicy reported an unsuccessful API response")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise JobicyFetchError("Jobicy response did not contain a jobs list")

    records: list[dict] = []
    skipped: list[dict] = []
    for index, item in enumerate(jobs, start=1):
        if not isinstance(item, dict):
            skipped.append({"item": index, "reason": "listing was not an object"})
            continue
        title = str(item.get("jobTitle") or "").strip()
        company = str(item.get("companyName") or "").strip()
        url = str(item.get("url") or "").strip()
        description = _plain_text(item.get("jobDescription")) or _plain_text(item.get("jobExcerpt"))
        missing = [name for name, value in (("title", title), ("company", company), ("description", description)) if not value]
        if missing:
            skipped.append({"item": index, "reason": "missing required source field(s): " + ", ".join(missing)})
            continue
        if not _is_jobicy_url(url):
            skipped.append({"item": index, "reason": "canonical Jobicy HTTPS URL missing or invalid"})
            continue
        description = description[:_MAX_DESCRIPTION_CHARS]
        source_id = item.get("id")
        if source_id is not None and (isinstance(source_id, bool) or not isinstance(source_id, (str, int))):
            source_id = None
        metadata = {
            "published_at": item.get("pubDate") if isinstance(item.get("pubDate"), str) else None,
            "job_level": item.get("jobLevel") if isinstance(item.get("jobLevel"), str) else None,
            "job_type": item.get("jobType") if isinstance(item.get("jobType"), list) else [],
            "job_industry": item.get("jobIndustry") if isinstance(item.get("jobIndustry"), list) else [],
            "job_excerpt": _plain_text(item.get("jobExcerpt"))[:1000],
            "description_truncated": len(_plain_text(item.get("jobDescription"))) > _MAX_DESCRIPTION_CHARS,
        }
        records.append({
            "title": title,
            "company": company,
            "location": str(item.get("jobGeo") or "").strip(),
            "url": url,
            # Jobicy exposes a seniority level, not a numeric years-of-experience requirement.
            "experience_requirement": "",
            "salary": _salary_text(item),
            "description": description,
            "required_skills": sorted(extract_required_skills(description)),
            "source_listing_id": str(source_id) if source_id is not None else None,
            "source_metadata": metadata,
        })
    return records, skipped


def _get_json(params: dict, timeout: float) -> dict:
    url = API_URL + "?" + urlencode(params)
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "LocalJobVisibilityManager/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if not 200 <= status < 300:
                raise JobicyFetchError(f"Jobicy returned HTTP {status}")
            body = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise JobicyFetchError(f"Jobicy returned HTTP {exc.code}") from None
    except TimeoutError:
        raise JobicyFetchError("Jobicy request timed out") from None
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise JobicyFetchError("Jobicy request timed out") from None
        raise JobicyFetchError("Jobicy connection failed") from None
    except OSError as exc:
        if isinstance(exc, TimeoutError):
            raise JobicyFetchError("Jobicy request timed out") from None
        raise JobicyFetchError("Jobicy connection failed") from None
    if len(body) > _MAX_RESPONSE_BYTES:
        raise JobicyFetchError("Jobicy response exceeded the 5 MiB safety limit")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise JobicyFetchError("Jobicy response was not valid UTF-8 JSON") from None
    if not isinstance(payload, dict):
        raise JobicyFetchError("Jobicy response was not a JSON object")
    return payload


def fetch_jobicy_vacancies(*, database_path=None, resume_path=None, recorded_at: str | None = None,
                           target_roles: tuple[str, ...] | None = None,
                           per_role: int | None = None, timeout_seconds: int | None = None,
                           total_timeout_seconds: int | None = None,
                           get_json: Callable = _get_json) -> dict:
    """Fetch a small bounded set by configured role tags and feed existing intake/matching."""
    roles = tuple(target_roles or settings.target_roles)
    count = settings.jobicy_results_per_role if per_role is None else per_role
    request_timeout = settings.jobicy_request_timeout_seconds if timeout_seconds is None else timeout_seconds
    total_timeout = settings.jobicy_total_timeout_seconds if total_timeout_seconds is None else total_timeout_seconds
    if not roles or len(roles) > 20 or any(not role.strip() or not 3 <= len(role.strip()) <= 50 for role in roles):
        raise ValueError("Jobicy target roles must contain 1–20 non-empty tags of 3–50 characters")
    if not 1 <= count <= 20 or not 1 <= request_timeout <= 60 or not request_timeout <= total_timeout <= 300:
        raise ValueError("Jobicy fetch limits are outside the supported bounded ranges")

    with connect(database_path) as conn:
        recent_attempt = conn.execute(
            "SELECT 1 FROM runs WHERE task='jobicy_discovery' AND started_at >= datetime('now','-1 hour') LIMIT 1"
        ).fetchone()
    if recent_attempt:
        raise JobicyFetchError("Jobicy discovery is limited to one run per hour; no request was made")

    resume_text = _load_resume(resume_path)
    started = time.monotonic()
    listings: list[dict] = []
    skipped: list[dict] = []
    try:
        for role_index, role in enumerate(roles, start=1):
            remaining = total_timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise JobicyFetchError("Jobicy discovery exceeded its total timeout; no vacancies were imported")
            timeout = min(request_timeout, max(0.1, remaining))
            _log_event("request_start", role_index=role_index, result_limit=count)
            payload = get_json({"count": count, "tag": role.strip()}, timeout)
            if time.monotonic() - started > total_timeout:
                raise JobicyFetchError("Jobicy discovery exceeded its total timeout; no vacancies were imported")
            page_records, page_skipped = parse_jobicy_payload(payload)
            listings.extend(page_records)
            skipped.extend({"role": role, **item} for item in page_skipped)
            _log_event("request_complete", role_index=role_index,
                       listings=len(page_records), skipped=len(page_skipped))
    except JobicyFetchError as exc:
        record_run("jobicy_discovery", "failed", type(exc).__name__, database_path)
        _log_event("request_failed", error_type=type(exc).__name__)
        raise

    stamp = recorded_at or datetime.now(ZoneInfo(settings.timezone)).isoformat(timespec="seconds")
    results = []
    for record in listings:
        source_id = record.pop("source_listing_id")
        metadata = record.pop("source_metadata")
        results.append(_ingest(record, source=SOURCE_NAME, resume_text=resume_text,
                               database_path=database_path, recorded_at=stamp,
                               source_listing_id=source_id, source_metadata=metadata))
    summary = {"inserted": sum(bool(item["inserted"]) for item in results),
               "updated": sum(not item["inserted"] for item in results),
               "processed": len(results), "skipped": skipped, "results": results,
               "collected_at": stamp, "source": SOURCE_NAME, "roles_queried": list(roles)}
    record_run("jobicy_discovery", "success", f"processed={summary['processed']}; inserted={summary['inserted']}; updated={summary['updated']}; skipped={len(skipped)}", database_path)
    _log_event("intake_complete", processed=summary["processed"], inserted=summary["inserted"],
               updated=summary["updated"], skipped=len(skipped), collected_at=stamp)
    return summary
