"""Append privacy-safe JSONL diagnostic events for browser workflows."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

_SAFE_FIELDS = {
    "error_type", "timeout_ms", "input_count", "update_control_count", "dialog_count",
    "profile_path_match", "login_form_count", "accessible_label_match", "file_type_match",
    "accepts_pdf", "control_enabled", "selected_name_match", "selected_size_match",
    "confirmation_signal", "failure_signal", "metadata_signal", "dry_run", "live",
    "page_load_state", "preflight_valid", "file_size_bytes", "http_status",
    "matching_input_count", "total_input_count", "expected_file_size_bytes",
    "selected_file_count", "selected_file_size_bytes",
    "selection_method",
    "elapsed_ms", "headless", "profile_present", "existing_page_count",
    "profile_mode", "page_selection", "ready_state", "startup_stage",
    "blocked_request_count",
    "input_replaced", "original_input_connected", "current_file_input_count",
    "request_method", "resource_type", "url_origin", "response_status",
    "message_kind", "error_name", "selection_error_event_count",
    "upload_request_observed", "upload_response_accepted", "upload_response_status",
    "site_success_notification", "filename_visible_after_reload", "new_profile_metadata",
    "saved_profile_verified",
    "immediate_selected_file_count", "immediate_selected_file_size_bytes",
    "immediate_selected_name_match", "immediate_selected_size_match",
    "local_file_valid", "site_resume_observed", "filename_matches",
    "content_hash_matches", "content_verified", "verification_record_id",
    "site_check_performed",
    "resume_site_observed", "section_count", "action_count",
}


def _log_path() -> Path:
    return Path("logs") / "resume_upload.jsonl"


def log_event(stage: str, status: str, **details) -> None:
    """Write timestamps and allowlisted scalar diagnostics; omit page/file contents."""
    event = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "stage": stage,
        "status": status,
    }
    for key, value in details.items():
        if key in _SAFE_FIELDS and isinstance(value, (bool, int, float, type(None))):
            event[key] = value
        elif key == "error_type" and isinstance(value, str) and value.replace("_", "").isalnum():
            event[key] = value[:64]
        elif key == "selection_method" and value == "playwright_set_input_files":
            event[key] = value
        elif key == "request_method" and value in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}:
            event[key] = value
        elif key == "resource_type" and value in {"document", "stylesheet", "image", "media", "font", "script", "texttrack", "xhr", "fetch", "eventsource", "websocket", "manifest", "other"}:
            event[key] = value
        elif key == "url_origin" and isinstance(value, str) and len(value) <= 200 and "?" not in value and "#" not in value:
            event[key] = value
        elif key == "message_kind" and value in {"console_error", "page_error", "request_failed", "response"}:
            event[key] = value
        elif key == "error_name" and isinstance(value, str) and value.replace("_", "").isalnum():
            event[key] = value[:64]
        elif key == "profile_mode" and value == "persistent_user_data_directory":
            event[key] = value
        elif key == "page_selection" and value in {
            "reused_existing_site_tab", "reused_blank_tab", "created_new_tab",
        }:
            event[key] = value
        elif key == "ready_state" and value in {"loading", "interactive", "complete", "unknown"}:
            event[key] = value
        elif key == "startup_stage" and value in {
            "playwright_initialization", "chromium_launch", "persistent_context_creation",
            "page_creation", "navigation", "page_readiness",
        }:
            event[key] = value
    target = _log_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
