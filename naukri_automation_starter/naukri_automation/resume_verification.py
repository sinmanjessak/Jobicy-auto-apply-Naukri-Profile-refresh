"""Local PDF checks and evidence classification for read-only resume verification."""
from __future__ import annotations

import hashlib
from pathlib import Path, PureWindowsPath

from .upload import UploadWorkflowError, validate_pdf

CANONICAL_RESUME_PATH = Path("data") / "resume.pdf"
LOCAL_VALID = "Local file valid"
SITE_OBSERVED = "Site resume observed"
FILENAME_MATCHES = "Filename matches"
CONTENT_VERIFIED = "Content verified"
UNVERIFIED = "Unverified"


def sha256_file(path: Path) -> str:
    """Hash a file incrementally without returning or logging its contents."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_local_pdf(path: Path = CANONICAL_RESUME_PATH) -> dict:
    """Validate a PDF and return safe metadata; failed details are stage codes only."""
    path = Path(path)
    if not path.is_file():
        return {
            "local_status": UNVERIFIED, "local_filename": path.name,
            "local_size_bytes": 0, "local_sha256": None, "local_error_code": "missing_file",
        }
    try:
        metadata = validate_pdf(path)
        return {
            "local_status": LOCAL_VALID,
            "local_filename": Path(metadata["path"]).name,
            "local_size_bytes": int(metadata["size"]),
            "local_sha256": sha256_file(Path(metadata["path"])),
            "local_error_code": None,
        }
    except UploadWorkflowError:
        return {
            "local_status": UNVERIFIED, "local_filename": Path(path).name,
            "local_size_bytes": 0, "local_sha256": None, "local_error_code": "invalid_pdf",
        }
    except OSError:
        return {
            "local_status": UNVERIFIED, "local_filename": Path(path).name,
            "local_size_bytes": 0, "local_sha256": None, "local_error_code": "unreadable_file",
        }


def filename_matches(expected_filename: str, displayed_filename: str) -> bool:
    """Compare basenames case-insensitively across Windows and POSIX separators."""
    expected = PureWindowsPath(str(expected_filename).strip().replace("/", "\\")).name
    displayed = PureWindowsPath(str(displayed_filename).strip().replace("/", "\\")).name
    return bool(expected and displayed and expected.casefold() == displayed.casefold())


def classify_resume_verification(
    local: dict,
    *,
    site_check_performed: bool = False,
    site_resume_observed: bool = False,
    displayed_filename: str = "",
    displayed_upload_date: str = "",
    downloaded_copy: Path | None = None,
) -> dict:
    """Classify only explicit user observations and byte-hash evidence."""
    valid_local = local.get("local_status") == LOCAL_VALID and bool(local.get("local_sha256"))
    site_observed = bool(site_resume_observed)
    site_checked = bool(site_check_performed or site_observed)
    raw_name = _clean_user_metadata(displayed_filename, 255) if site_observed else ""
    clean_name = PureWindowsPath(raw_name.replace("/", "\\")).name if raw_name else ""
    clean_date = _clean_user_metadata(displayed_upload_date, 64) if site_observed else ""
    matches = bool(valid_local and site_observed and clean_name and filename_matches(local["local_filename"], clean_name))
    download_meta = None
    content_verified = False
    if downloaded_copy is not None and valid_local and site_observed:
        download_meta = inspect_local_pdf(Path(downloaded_copy))
        content_verified = (
            download_meta["local_status"] == LOCAL_VALID
            and download_meta["local_sha256"] == local["local_sha256"]
        )
    return {
        **local,
        "site_resume_observed": site_observed,
        "site_check_performed": site_checked,
        "site_status": SITE_OBSERVED if site_observed else UNVERIFIED,
        "displayed_filename": clean_name or None,
        "displayed_upload_date": clean_date or None,
        "filename_matches": matches,
        "filename_status": FILENAME_MATCHES if matches else UNVERIFIED,
        "content_verified": content_verified,
        "content_status": CONTENT_VERIFIED if content_verified else UNVERIFIED,
        "downloaded_copy_valid": bool(download_meta and download_meta["local_status"] == LOCAL_VALID),
        "downloaded_size_bytes": download_meta["local_size_bytes"] if download_meta else None,
        "downloaded_sha256": download_meta["local_sha256"] if download_meta else None,
        "evidence_source": "user_confirmed",
        "independent_verification": False,
    }


def _clean_user_metadata(value: str, limit: int) -> str:
    """Normalize bounded user-entered filename/date metadata; reject control chars."""
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return ""
    cleaned = " ".join(value.split())
    return cleaned if len(cleaned) <= limit else ""
