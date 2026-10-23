"""Guarded, explicit resume-upload workflow based on live page semantics."""
from __future__ import annotations

import os
from pathlib import Path
from pathlib import PureWindowsPath
from urllib.parse import urlsplit

from pypdf import PdfReader

from .config import settings
from .diagnostics import log_event

RESUME_PATH = Path("data") / "resume.pdf"
PROFILE_PATH = "/mnjuser/profile"
UPLOAD_LABEL = "Upload resume"
UPDATE_NAME = "Update resume"
LIVE_UPLOAD_ENABLED = False


class UploadWorkflowError(RuntimeError):
    """Safe workflow error whose message never includes page or PDF content."""

    def __init__(self, stage: str, status: str, message: str):
        self.stage = stage
        self.status = status
        super().__init__(message)


def validate_pdf(path: Path = RESUME_PATH) -> dict:
    """Verify a readable, nonempty PDF without logging or returning its contents."""
    log_event("pdf_preflight", "started")
    if not path.is_file():
        log_event("pdf_preflight", "missing", preflight_valid=False)
        raise UploadWorkflowError("pdf_preflight", "blocked", "Required file data/resume.pdf is missing.")
    if path.suffix.lower() != ".pdf":
        log_event("pdf_preflight", "invalid_extension", preflight_valid=False)
        raise UploadWorkflowError("pdf_preflight", "blocked", "The resume must be a PDF file.")
    try:
        file_size = path.stat().st_size
        if file_size <= 0:
            raise ValueError("empty")
        with path.open("rb") as handle:
            reader = PdfReader(handle, strict=True)
            if reader.is_encrypted:
                raise ValueError("encrypted")
            page_count = len(reader.pages)
            if page_count < 1:
                raise ValueError("empty_pdf")
    except Exception as exc:
        log_event("pdf_preflight", "unreadable", preflight_valid=False, error_type=type(exc).__name__)
        raise UploadWorkflowError("pdf_preflight", "blocked", "data/resume.pdf is not a readable, unencrypted PDF.") from None
    log_event("pdf_preflight", "valid", preflight_valid=True, file_size_bytes=file_size)
    absolute_path = path.resolve(strict=True)
    return {"path": absolute_path, "name": absolute_path.name, "size": file_size}


_INSPECT_UPLOAD_DOM = r"""() => {
  const visible = el => {
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
  };
  const clean = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  const accessibleName = el => {
    const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
      .map(id => document.getElementById(id)?.innerText || '').join(' ');
    return clean(el.getAttribute('aria-label') || labelled ||
      Array.from(el.labels || []).map(label => label.innerText).join(' ') ||
      el.getAttribute('title') || (el.tagName === 'INPUT' ? el.getAttribute('value') : el.innerText));
  };
  const allFiles = Array.from(document.querySelectorAll('input[type="file"]'));
  const files = allFiles.filter(visible);
  const updates = Array.from(document.querySelectorAll('button,input[type="button"],input[type="submit"],[role="button"]'))
    .filter(visible).map(el => ({
      tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(),
      role: el.getAttribute('role') || '', name: accessibleName(el), disabled: !!el.disabled,
      sameFormAsFileInput: files.length === 1 && !!files[0].form && el.form === files[0].form,
      visible: true
    }));
  const describeFileInput = (el, domIndex) => ({
    domIndex,
    tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(),
    name: el.getAttribute('name') || '', id: el.id || '',
    accessibleName: accessibleName(el), ariaLabel: el.getAttribute('aria-label') || '',
    ariaLabelledby: el.getAttribute('aria-labelledby') || '', accept: el.getAttribute('accept') || '',
    required: !!el.required, multiple: !!el.multiple, disabled: !!el.disabled,
    visible: visible(el), formMethod: el.form?.method || '',
    formId: el.form?.id || ''
  });
  const allFileInputs = allFiles.map(describeFileInput);
  const fileInputs = allFileInputs.filter(el => el.visible);
  return {
    path: location.pathname,
    passwordFieldCount: document.querySelectorAll('input[type="password"]').length,
    visibleDialogCount: Array.from(document.querySelectorAll('[role="dialog"],[aria-modal="true"]')).filter(visible).length,
    fileInputs, allFileInputs, allFileInputCount: allFiles.length, hiddenFileInputCount: allFiles.length - files.length,
    updateControls: updates.filter(el => el.name.trim().toLowerCase() === 'update resume')
  };
}"""


def inspect_upload_controls(page) -> dict:
    """Read the current DOM's upload control attributes without changing the page."""
    return page.evaluate(_INSPECT_UPLOAD_DOM)


def _inspect_result(page, file_info: dict) -> tuple[dict, object, object]:
    structure = inspect_upload_controls(page)
    url = urlsplit(page.url)
    expected_host = urlsplit(settings.base_url).hostname
    file_controls = structure.get("fileInputs", [])
    updates = structure.get("updateControls", [])
    login_count = structure.get("passwordFieldCount", 0)
    dialogs = structure.get("visibleDialogCount", 0)
    profile_match = url.hostname == expected_host and url.path.rstrip("/") == PROFILE_PATH
    input_match = (
        len(file_controls) == 1
        and file_controls[0].get("tag") == "input"
        and file_controls[0].get("type") == "file"
        and file_controls[0].get("accessibleName", "").casefold() == UPLOAD_LABEL.casefold()
        and not file_controls[0].get("disabled")
        and file_controls[0].get("visible")
    )
    update_match = len(updates) == 1 and not updates[0].get("disabled")
    accepts_pdf = True
    if input_match and file_controls[0].get("accept"):
        accepts_pdf = ".pdf" in file_controls[0]["accept"].casefold() or "application/pdf" in file_controls[0]["accept"].casefold()
    safe_diag = {
        "profile_path_match": profile_match,
        "login_form_count": login_count,
        "dialog_count": dialogs,
        "input_count": len(file_controls),
        "update_control_count": len(updates),
        "accessible_label_match": input_match,
        "file_type_match": input_match,
        "accepts_pdf": accepts_pdf,
        "control_enabled": update_match,
    }
    log_event("page_structure", "inspected", **safe_diag)

    if login_count:
        raise UploadWorkflowError("page_structure", "blocked", "A login form is present. Sign in manually in the visible browser, then retry.")
    if not profile_match:
        raise UploadWorkflowError("page_structure", "blocked", "The visible browser is not on the Naukri profile page. Navigate there manually and retry.")
    if dialogs:
        raise UploadWorkflowError("page_structure", "blocked", "An unexpected dialog is open. Close or resolve it manually, then retry.")
    if not input_match or not update_match or not accepts_pdf:
        raise UploadWorkflowError("page_structure", "blocked", "The live upload controls differ from the inspected structure; no file was attached.")

    # Bind the exact DOM-discovered file input, not merely any element returned
    # by label resolution. A hidden or custom input must not be selected by guess.
    matching_inputs = [
        item for item in structure.get("allFileInputs", file_controls)
        if item.get("tag") == "input" and item.get("type") == "file"
        and item.get("visible") and not item.get("disabled")
        and item.get("accessibleName", "").casefold() == UPLOAD_LABEL.casefold()
    ]
    all_file_locator = page.locator('input[type="file"]')
    total_input_count = structure.get("allFileInputCount", len(structure.get("allFileInputs", file_controls)))
    if all_file_locator.count() != total_input_count or len(matching_inputs) != 1:
        log_event("file_input_binding", "failed", matching_input_count=len(matching_inputs), total_input_count=total_input_count)
        raise UploadWorkflowError("file_input_binding", "blocked", "The inspected upload input is not uniquely identifiable; no file was attached.")
    file_locator = all_file_locator.nth(matching_inputs[0]["domIndex"])
    bound_input = file_locator.evaluate(r"""el => {
      const style = getComputedStyle(el), rect = el.getBoundingClientRect();
      const visible = style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
      const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
        .map(id => document.getElementById(id)?.innerText || '').join(' ');
      const accessibleName = (el.getAttribute('aria-label') || labelled ||
        Array.from(el.labels || []).map(label => label.innerText).join(' ') ||
        el.getAttribute('title') || el.getAttribute('value') || '').replace(/\s+/g, ' ').trim();
      return {domIndex: Array.from(document.querySelectorAll('input[type="file"]')).indexOf(el),
        tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(),
        name: el.getAttribute('name') || '', id: el.id || '', accessibleName, visible, disabled: !!el.disabled};
    }""")
    expected_binding = {key: matching_inputs[0].get(key) for key in bound_input}
    if bound_input != expected_binding:
        log_event("file_input_binding", "failed", matching_input_count=len(matching_inputs), total_input_count=total_input_count)
        raise UploadWorkflowError("file_input_binding", "blocked", "The live file input changed after inspection; no file was attached.")
    update_locator = page.get_by_role("button", name=UPDATE_NAME, exact=True)
    log_event("file_input_binding", "passed", matching_input_count=len(matching_inputs), total_input_count=total_input_count)
    structure["matchingUploadInputCount"] = len(matching_inputs)
    if update_locator.count() != 1:
        raise UploadWorkflowError("accessible_controls", "blocked", "The expected accessible upload controls are not unique; no file was attached.")
    return structure, file_locator, update_locator


def _post_upload_state(page, filename: str) -> dict:
    """Read safe signals from the rendered page; message text never leaves JS."""
    return page.evaluate(r"""filename => {
      const visible = el => {
        const s = getComputedStyle(el), r = el.getBoundingClientRect();
        return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
      };
      const nodes = Array.from(document.querySelectorAll('[role="alert"],[role="status"],[aria-live]')).filter(visible);
      const messages = nodes.map(el => (el.innerText || '').replace(/\s+/g, ' ').trim()).join(' ');
      // A specific resume-upload acknowledgment is only server-acceptance evidence,
      // never proof that profile metadata persisted.
      const siteResumeSuccess = /resume\s+uploaded\s+successfully/i.test(messages);
      const failure = /(upload|resume|file).{0,30}(failed|failure|error|unable)|failed.{0,30}(upload|resume|file)/i.test(messages);
      const hasResumeRegion = el => {
        for (let node = el.parentElement, depth = 0; node && depth < 8; node = node.parentElement, depth++) {
          if (!node.matches('section, article, [role="region"]')) continue;
          const ids = (node.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
          const labelled = ids.map(id => document.getElementById(id)?.innerText || '').join(' ');
          const heading = node.querySelector('h1,h2,h3,h4,h5,h6');
          const accessibleName = node.getAttribute('aria-label') || labelled || heading?.innerText || '';
          if (/\bresume\b/i.test(accessibleName)) return true;
        }
        return false;
      };
      const filenameVisible = Array.from(document.querySelectorAll('body *')).some(el => {
        if (el.children.length && el.children.length > 0) return false;
        return visible(el) && (el.innerText || '').trim() === filename && hasResumeRegion(el);
      });
      return {siteResumeSuccess, failure, filenameVisible};
    }""", filename)


def _classify_post_upload_verification(
    before: dict,
    after_reload: dict,
    *,
    upload_request_observed: bool,
    upload_response_status: int | None,
) -> dict:
    """Separate local selection, transport, site acknowledgment, and persistence.

    Only a newly visible expected filename after a read-only reload verifies saved
    profile state. A toast or 2xx response is recorded as intermediate evidence.
    """
    transport_accepted = (
        upload_request_observed and isinstance(upload_response_status, int)
        and 200 <= upload_response_status < 300
    )
    metadata_added = bool(after_reload.get("filename_visible")) and not bool(before.get("filename_visible"))
    persisted = bool(after_reload.get("reload_completed")) and metadata_added
    failure_signal = bool(after_reload.get("failure"))
    return {
        "status": "verified" if persisted else "failed" if failure_signal else "unverified",
        "upload_request_observed": bool(upload_request_observed),
        "upload_response_accepted": transport_accepted,
        "site_success_notification": bool(after_reload.get("siteResumeSuccess")),
        "filename_visible_after_reload": bool(after_reload.get("filename_visible")),
        "new_profile_metadata": metadata_added,
        "saved_profile_verified": persisted,
        "failure_signal": failure_signal,
    }


def verify_post_upload_state(
    page,
    filename: str,
    before: dict,
    *,
    upload_request_observed: bool = False,
    upload_response_status: int | None = None,
) -> dict:
    """Read-only post-upload verification; reloads once and requires new metadata."""
    log_event("post_upload_verification", "started", timeout_ms=settings.browser_navigation_timeout_ms)
    try:
        immediate_state = _post_upload_state(page, filename)
    except Exception as exc:
        log_event("server_acceptance", "unverified", error_type=type(exc).__name__)
        immediate_state = {"siteResumeSuccess": False, "failure": False}
    try:
        page.reload(wait_until="domcontentloaded", timeout=settings.browser_navigation_timeout_ms)
        after = _post_upload_state(page, filename)
        after["reload_completed"] = True
        # A site toast may disappear during reload; preserve its boolean only.
        after["siteResumeSuccess"] = bool(
            immediate_state.get("siteResumeSuccess") or after.get("siteResumeSuccess")
        )
        after["failure"] = bool(immediate_state.get("failure") or after.get("failure"))
    except Exception as exc:
        log_event("post_upload_verification", "unverified", error_type=type(exc).__name__)
        after = {"reload_completed": False, "filename_visible": False,
                 "siteResumeSuccess": False, "failure": False}
    result = _classify_post_upload_verification(
        before, after, upload_request_observed=upload_request_observed,
        upload_response_status=upload_response_status,
    )
    log_event("upload_request", "observed" if upload_request_observed else "not_observed",
              upload_request_observed=upload_request_observed)
    response_status = (
        "accepted" if result["upload_response_accepted"]
        else "rejected" if isinstance(upload_response_status, int) and upload_response_status >= 400
        else "unverified"
    )
    log_event("upload_response", response_status,
              upload_response_status=upload_response_status,
              upload_response_accepted=result["upload_response_accepted"])
    log_event("server_acceptance", "site_acknowledged" if result["site_success_notification"] else "unverified",
              site_success_notification=result["site_success_notification"])
    log_event("saved_profile_verification", "verified" if result["saved_profile_verified"] else result["status"],
              filename_visible_after_reload=result["filename_visible_after_reload"],
              new_profile_metadata=result["new_profile_metadata"],
              saved_profile_verified=result["saved_profile_verified"])
    log_event(
        "post_upload_verification", result["status"],
        upload_request_observed=result["upload_request_observed"],
        upload_response_accepted=result["upload_response_accepted"],
        site_success_notification=result["site_success_notification"],
        filename_visible_after_reload=result["filename_visible_after_reload"],
        new_profile_metadata=result["new_profile_metadata"],
        saved_profile_verified=result["saved_profile_verified"],
        failure_signal=result["failure_signal"],
    )
    return result


_MARK_INPUT_JS = r"""el => {
  const key = Symbol.for('naukri_automation.selection_input');
  window[key] = el;
  return {connected: el.isConnected, domIndex: Array.from(document.querySelectorAll('input[type="file"]')).indexOf(el)};
}"""

_CHECK_MARKED_INPUT_JS = r"""el => {
  const key = Symbol.for('naukri_automation.selection_input');
  const original = window[key];
  const current = Array.from(document.querySelectorAll('input[type="file"]'));
  const files = original ? Array.from(original.files || []) : [];
  return {
    originalConnected: !!original && original.isConnected,
    originalStillInDocument: !!original && current.includes(original),
    currentInputCount: current.length,
    count: files.length,
    file: files[0] ? {name: files[0].name, size: files[0].size} : null
  };
}"""


def _safe_origin(url: str) -> str:
    """Keep only URL origin; never retain paths, query strings, or fragments."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port is not None else ""
        return f"{parts.scheme}://{host}{port}" if host else "unknown"
    except (TypeError, ValueError):
        return "unknown"


def _observe_selection_events(page) -> tuple[list[int], object]:
    """Observe page/network errors without changing or blocking page requests."""
    counts = [0]
    handlers = []

    def attach(event_name, callback):
        page.on(event_name, callback)
        handlers.append((event_name, callback))

    def page_error(error):
        counts[0] += 1
        log_event("selection_page_error", "observed", message_kind="page_error",
                  error_name=type(error).__name__)

    def console_message(message):
        if message.type == "error":
            counts[0] += 1
            log_event("selection_console_error", "observed", message_kind="console_error")

    def request_failed(request):
        counts[0] += 1
        log_event("selection_request", "failed", message_kind="request_failed",
                  request_method=request.method.upper(), resource_type=request.resource_type,
                  url_origin=_safe_origin(request.url))

    def response_received(response):
        request = response.request
        log_event("selection_response", "observed", message_kind="response",
                  request_method=request.method.upper(), resource_type=request.resource_type,
                  url_origin=_safe_origin(response.url), response_status=response.status)

    try:
        attach("pageerror", page_error)
        attach("console", console_message)
        attach("requestfailed", request_failed)
        attach("response", response_received)
    except Exception as exc:
        for event_name, callback in handlers:
            try:
                page.remove_listener(event_name, callback)
            except Exception:
                pass
        log_event("selection_event_observers", "failed", error_type=type(exc).__name__)
        raise UploadWorkflowError("selection_event_observers", "failed",
                                  "Could not attach safe page diagnostics; no file was selected.") from None

    def remove_observers():
        for event_name, callback in handlers:
            try:
                page.remove_listener(event_name, callback)
            except Exception:
                pass

    log_event("selection_event_observers", "installed")
    return counts, remove_observers


def upload_resume_on_page(
    page,
    *,
    live: bool = False,
    file_selection_test: bool = False,
    resume_path: Path = RESUME_PATH,
) -> dict:
    """Inspect by default; upload submission is disabled pending live verification."""
    if live and file_selection_test:
        raise ValueError("live upload and file-selection test modes are mutually exclusive")
    if live and not LIVE_UPLOAD_ENABLED:
        log_event("live_upload", "disabled")
        raise UploadWorkflowError(
            "live_upload", "disabled",
            "Live resume upload is disabled until post-upload verification is validated against the website.",
        )
    file_info = validate_pdf(resume_path)
    dry_run = not live and not file_selection_test
    log_event("page_structure", "started", dry_run=dry_run, live=live)
    try:
        structure, file_locator, update_locator = _inspect_result(page, file_info)
    except UploadWorkflowError as exc:
        log_event(exc.stage, exc.status)
        raise
    except Exception as exc:
        status = "timed_out" if type(exc).__name__.casefold() == "timeouterror" else "failed"
        log_event("page_structure", status, error_type=type(exc).__name__)
        raise UploadWorkflowError("page_structure", status, "Could not inspect the upload controls safely; no file was attached.") from None
    result = {
        "mode": "file_selection_test" if file_selection_test else "dry_run",
        "status": "ready" if dry_run else "unverified",
        "upload_performed": False,
        "confirmation": "not_applicable" if file_selection_test else "not_checked",
        "controls": structure,
    }
    if dry_run:
        log_event("dry_run", "ready", dry_run=True)
        return result

    selection_event_count = None
    remove_observers = None
    if file_selection_test:
        selection_event_count, remove_observers = _observe_selection_events(page)
        log_event(
            "file_selection_test", "started",
            matching_input_count=structure.get("matchingUploadInputCount", 1),
            total_input_count=structure.get("allFileInputCount", 0),
            expected_file_size_bytes=file_info["size"],
        )
    log_event(
        "file_selection", "started", live=live,
        selection_method="playwright_set_input_files",
    )
    try:
        if file_selection_test:
            file_locator.evaluate(_MARK_INPUT_JS)
            log_event("selected_input_identity", "recorded")
        file_locator.set_input_files(str(file_info["path"]), timeout=settings.browser_navigation_timeout_ms)
        if file_selection_test:
            immediate_selected = file_locator.evaluate(_CHECK_MARKED_INPUT_JS)
            immediate_file = immediate_selected.get("file") if isinstance(immediate_selected, dict) else None
            immediate_count = immediate_selected.get("count", 0) if isinstance(immediate_selected, dict) else 0
            immediate_size = immediate_file.get("size") if isinstance(immediate_file, dict) else None
            immediate_name = immediate_file.get("name", "") if isinstance(immediate_file, dict) else ""
            immediate_name_match = (
                os.path.normcase(PureWindowsPath(immediate_name).name)
                == os.path.normcase(Path(file_info["path"]).resolve().name)
            )
            immediate_size_match = isinstance(immediate_size, int) and immediate_size == file_info["size"]
            log_event(
                "selected_file_immediate_check", "passed" if immediate_count == 1 and immediate_name_match and immediate_size_match else "failed",
                selected_file_count=immediate_count,
                selected_file_size_bytes=immediate_size if isinstance(immediate_size, int) else 0,
                selected_name_match=immediate_name_match,
                selected_size_match=immediate_size_match,
            )
            # Give the page's own change handlers a short bounded turn to settle;
            # this is not a submission and no Update control is invoked here.
            try:
                page.wait_for_timeout(350)
            except Exception as exc:
                log_event("selection_settle", "failed", error_type=type(exc).__name__, timeout_ms=350)
            selected = file_locator.evaluate(_CHECK_MARKED_INPUT_JS)
        else:
            selected = file_locator.evaluate("el => { const files = Array.from(el.files || []); return {count: files.length, file: files[0] ? {name: files[0].name, size: files[0].size} : null}; }")
    except Exception as exc:
        if remove_observers:
            remove_observers()
        log_event("file_selection", "failed", error_type=type(exc).__name__)
        raise UploadWorkflowError("file_selection", "failed", "Could not attach the selected local PDF. No update button was clicked.") from None
    finally:
        if remove_observers:
            remove_observers()

    selected_file = selected.get("file") if isinstance(selected, dict) else None
    expected_file = Path(file_info["path"]).resolve()
    selected_name = selected_file.get("name", "") if isinstance(selected_file, dict) else ""
    selected_basename = PureWindowsPath(selected_name).name
    name_match = os.path.normcase(selected_basename) == os.path.normcase(expected_file.name)
    selected_size = selected_file.get("size") if isinstance(selected_file, dict) else None
    size_match = isinstance(selected_size, int) and selected_size == file_info["size"]
    selected_count = selected.get("count", 0) if isinstance(selected, dict) else 0
    original_connected = bool(selected.get("originalConnected", True)) if isinstance(selected, dict) else False
    original_still_present = bool(selected.get("originalStillInDocument", True)) if isinstance(selected, dict) else False
    input_replaced = not original_connected or not original_still_present
    current_input_count = selected.get("currentInputCount", structure.get("allFileInputCount", 0)) if isinstance(selected, dict) else 0
    selection_ok = name_match and size_match and selected_count == 1 and not input_replaced
    log_event("selected_input_after_change", "replaced" if input_replaced else "present",
              input_replaced=input_replaced, original_input_connected=original_connected,
              current_file_input_count=current_input_count)
    log_event(
        "selected_file_check", "passed" if selection_ok else "failed",
        matching_input_count=structure.get("matchingUploadInputCount", 1),
        total_input_count=structure.get("allFileInputCount", len(structure.get("allFileInputs", []))),
        expected_file_size_bytes=file_info["size"], selected_file_count=selected_count,
        selected_file_size_bytes=selected_size if isinstance(selected_size, int) else 0,
        selected_name_match=name_match, selected_size_match=size_match,
        input_replaced=input_replaced, original_input_connected=original_connected,
        current_file_input_count=current_input_count,
    )
    result.update({
        "selected_file_count": selected_count,
        "selected_file_size_bytes": selected_size if isinstance(selected_size, int) else 0,
        "selected_name_match": name_match,
        "selected_size_match": size_match,
        "input_replaced": input_replaced,
        "original_input_connected": original_connected,
        "current_file_input_count": current_input_count,
    })
    if file_selection_test:
        result.update({
            "immediate_selected_file_count": immediate_count,
            "immediate_selected_file_size_bytes": immediate_size if isinstance(immediate_size, int) else 0,
            "immediate_selected_name_match": immediate_name_match,
            "immediate_selected_size_match": immediate_size_match,
        })
    if not selection_ok:
        if input_replaced:
            message = "The upload input was replaced or detached during file selection; selection is unverified. Update was not clicked."
        elif selected_count == 0:
            if file_selection_test and immediate_count == 1 and immediate_name_match and immediate_size_match:
                message = "The file was selected immediately but the page cleared it during its change handling; selection is unverified. Update was not clicked."
            else:
                message = "The inspected input had no selected file immediately after Playwright selection; selection is unverified. Update was not clicked."
        else:
            message = "The selected file metadata does not match data/resume.pdf; selection is unverified. Update was not clicked."
        raise UploadWorkflowError("selected_file_check", "failed", message)
    if file_selection_test:
        result["status"] = "selected"
        result["file_selection_verified"] = True
        result["selection_error_event_count"] = selection_event_count[0]
        log_event("file_selection_test", "selected", selection_error_event_count=selection_event_count[0])
        return result

    raise AssertionError("Unexpected workflow branch; live submission is disabled.")
