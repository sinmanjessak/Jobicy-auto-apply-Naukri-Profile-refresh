import json
from pathlib import Path

import pytest
from pypdf import PdfWriter

from naukri_automation import upload


def make_pdf():
    path = Path(".test-upload-workflow.pdf")
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def expected_structure(label="Upload resume"):
    return {
        "path": "/mnjuser/profile",
        "passwordFieldCount": 0,
        "visibleDialogCount": 0,
        "allFileInputCount": 1, "hiddenFileInputCount": 0,
        "allFileInputs": [{
            "domIndex": 0, "tag": "input", "type": "file", "name": "resume", "id": "resumeInput",
            "accessibleName": label, "accept": ".pdf", "required": False,
            "multiple": False, "disabled": False, "visible": True,
            "formMethod": "post", "formId": "resumeForm",
        }],
        "fileInputs": [{
            "domIndex": 0,
            "tag": "input", "type": "file", "name": "resume", "id": "resumeInput",
            "accessibleName": label, "accept": ".pdf", "required": False,
            "multiple": False, "disabled": False, "visible": True,
            "formMethod": "post", "formId": "resumeForm",
        }],
        "updateControls": [{
            "tag": "input", "type": "button", "role": "button", "name": "Update resume",
            "disabled": False, "sameFormAsFileInput": True, "visible": True,
        }],
    }


class FakeLocator:
    def __init__(self, page, kind, index=0):
        self.page = page
        self.kind = kind
        self.index = index

    def count(self):
        return 1

    def set_input_files(self, path, timeout):
        if self.page.selection_error:
            raise self.page.selection_error
        local = Path(path)
        self.page.selected = self.page.selected_override or {"count": 1, "file": {"name": local.name, "size": local.stat().st_size}}

    def evaluate(self, script):
        if "getComputedStyle" in script:
            return self.page.bound_input
        if "selection_input" in script and "window[key] = el" in script:
            self.page.marked_input = True
            return {"connected": True, "domIndex": self.index}
        if "originalStillInDocument" in script:
            selected = dict(self.page.selected)
            selected.setdefault("originalConnected", True)
            selected.setdefault("originalStillInDocument", True)
            selected.setdefault("currentInputCount", self.page.structure.get("allFileInputCount", 1))
            return selected
        return self.page.selected

    def click(self, timeout):
        self.page.click_count += 1
        if self.page.click_error:
            raise self.page.click_error


class FakeFileInputs:
    def __init__(self, page):
        self.page = page

    def count(self):
        return self.page.structure.get("allFileInputCount", 1)

    def nth(self, index):
        self.page.bound_index = index
        return FakeLocator(self.page, "file", index)


class FakeRequest:
    def __init__(self, method="GET", resource_type="document", navigation=False):
        self.method = method
        self.resource_type = resource_type
        self._navigation = navigation

    def is_navigation_request(self):
        return self._navigation


class FakeRoute:
    def __init__(self, request):
        self.request = request
        self.action = None

    def abort(self, error_code=None):
        self.action = "abort"

    def continue_(self):
        self.action = "continue"


class FakePage:
    def __init__(self, states=None, structure=None, click_error=None, selection_error=None, wait_error=None,
                 selected_override=None, bound_input_override=None, settled_selection_override=None):
        self.url = "https://www.naukri.com/mnjuser/profile"
        self.states = list(states or [{}, {}, {}])
        self.structure = structure or expected_structure()
        self.click_error = click_error
        self.selection_error = selection_error
        self.wait_error = wait_error
        self.selected = {}
        self.selected_override = selected_override
        self.settled_selection_override = settled_selection_override
        self.bound_input = bound_input_override or {
            "domIndex": 0, "tag": "input", "type": "file", "name": "resume", "id": "resumeInput",
            "accessibleName": "Upload resume", "visible": True, "disabled": False,
        }
        self.bound_index = None
        self.route_handler = None
        self.route_pattern = None
        self.submit_guard_installed = False
        self.click_count = 0
        self.selection_count = 0
        self.handlers = {}
        self.marked_input = False

    def on(self, event_name, callback):
        self.handlers.setdefault(event_name, []).append(callback)

    def remove_listener(self, event_name, callback):
        self.handlers[event_name].remove(callback)

    def wait_for_timeout(self, timeout):
        assert 0 < timeout <= 500
        if self.settled_selection_override is not None:
            self.selected = self.settled_selection_override

    def evaluate(self, script, *args):
        if "fileInputs" in script and "document.querySelectorAll('input[type=\"file\"]')" in script:
            return self.structure
        if "document.addEventListener('submit'" in script:
            self.submit_guard_installed = True
            return True
        self.selection_count += 1
        if self.states:
            return self.states.pop(0)
        return {}

    def get_by_label(self, label, exact):
        assert label == "Upload resume" and exact is True
        return FakeLocator(self, "file")

    def locator(self, selector):
        assert selector == 'input[type="file"]'
        return FakeFileInputs(self)

    def route(self, pattern, handler):
        self.route_pattern = pattern
        self.route_handler = handler

    def get_by_role(self, role, name, exact):
        assert role == "button" and name == "Update resume" and exact is True
        return FakeLocator(self, "update")

    def wait_for_function(self, script, arg, timeout):
        if self.wait_error:
            raise self.wait_error

    def reload(self, wait_until, timeout):
        if getattr(self, "reload_error", None):
            raise self.reload_error


@pytest.fixture
def pdf():
    path = make_pdf()
    yield path
    path.unlink(missing_ok=True)


def quiet_events(monkeypatch):
    monkeypatch.setattr(upload, "log_event", lambda *args, **kwargs: None)


def test_dry_run_inspects_actual_semantics_without_attaching_or_clicking(pdf, monkeypatch):
    quiet_events(monkeypatch)
    page = FakePage()

    result = upload.upload_resume_on_page(page, resume_path=pdf)

    assert result["status"] == "ready"
    assert result["mode"] == "dry_run"
    assert result["upload_performed"] is False
    assert result["controls"]["fileInputs"][0]["accessibleName"] == "Upload resume"
    assert page.selected == {}
    assert page.click_count == 0


def test_missing_file_is_rejected_before_page_inspection(monkeypatch):
    quiet_events(monkeypatch)
    page = FakePage()

    with pytest.raises(upload.UploadWorkflowError, match="missing"):
        upload.upload_resume_on_page(page, resume_path=Path(".definitely-missing-resume.pdf"))

    assert page.selection_count == 0
    assert page.click_count == 0


def test_unexpected_controls_block_without_attaching_file(pdf, monkeypatch):
    quiet_events(monkeypatch)
    page = FakePage(structure=expected_structure(label="Choose a file"))

    with pytest.raises(upload.UploadWorkflowError, match="differ"):
        upload.upload_resume_on_page(page, resume_path=pdf)

    assert page.selected == {}
    assert page.click_count == 0


def test_wrong_dom_input_binding_blocks_before_file_selection(pdf, monkeypatch):
    quiet_events(monkeypatch)
    wrong = {"domIndex": 0, "tag": "input", "type": "file", "name": "other", "id": "otherInput",
             "accessibleName": "Upload resume", "visible": True, "disabled": False}
    page = FakePage(bound_input_override=wrong)

    with pytest.raises(upload.UploadWorkflowError, match="changed after inspection"):
        upload.upload_resume_on_page(page, file_selection_test=True, resume_path=pdf)

    assert page.selected == {}
    assert page.click_count == 0


@pytest.mark.parametrize("selected_override", [
    {"count": 1, "file": {"name": "different.pdf", "size": 123}},
    {"count": 1, "file": {"name": "test-upload-workflow.pdf", "size": 123}},
])
def test_filename_or_size_mismatch_stops_before_update(pdf, monkeypatch, selected_override):
    quiet_events(monkeypatch)
    page = FakePage(selected_override=selected_override)

    with pytest.raises(upload.UploadWorkflowError, match="metadata does not match"):
        upload.upload_resume_on_page(page, file_selection_test=True, resume_path=pdf)

    assert page.click_count == 0


def test_playwright_file_selection_is_verified_in_exact_input_before_update(pdf, monkeypatch):
    quiet_events(monkeypatch)
    page = FakePage(states=[{}, {}, {"success": True}])

    result = upload.upload_resume_on_page(page, file_selection_test=True, resume_path=pdf)

    assert page.bound_index == 0
    assert page.selected["count"] == 1
    assert page.selected["file"]["name"] == pdf.name
    assert page.selected["file"]["size"] == pdf.stat().st_size
    assert result["status"] == "selected"
    assert result["upload_performed"] is False
    assert page.click_count == 0


def test_hidden_file_input_is_not_selected_when_visible_match_is_second(pdf, monkeypatch):
    quiet_events(monkeypatch)
    structure = expected_structure()
    hidden = dict(structure["allFileInputs"][0], domIndex=0, id="hiddenInput", visible=False,
                  accessibleName="")
    visible = dict(structure["allFileInputs"][0], domIndex=1)
    structure["allFileInputs"] = [hidden, visible]
    structure["fileInputs"] = [visible]
    structure["allFileInputCount"] = 2
    structure["hiddenFileInputCount"] = 1
    bound = {"domIndex": 1, "tag": "input", "type": "file", "name": "resume", "id": "resumeInput",
             "accessibleName": "Upload resume", "visible": True, "disabled": False}
    page = FakePage(states=[{}, {}, {"success": True}], structure=structure, bound_input_override=bound)

    result = upload.upload_resume_on_page(page, file_selection_test=True, resume_path=pdf)

    assert page.bound_index == 1
    assert result["status"] == "selected"
    assert page.click_count == 0


def test_file_selection_test_verifies_file_without_update_or_confirmation(pdf, monkeypatch):
    quiet_events(monkeypatch)
    page = FakePage(states=[])

    result = upload.upload_resume_on_page(page, file_selection_test=True, resume_path=pdf)

    assert result["mode"] == "file_selection_test"
    assert result["status"] == "selected"
    assert result["file_selection_verified"] is True
    assert result["upload_performed"] is False
    assert result["selected_file_count"] == 1
    assert result["selected_file_size_bytes"] == pdf.stat().st_size
    assert result["selected_name_match"] is True
    assert result["selected_size_match"] is True
    assert result["immediate_selected_file_count"] == 1
    assert result["immediate_selected_name_match"] is True
    assert page.route_pattern is None
    assert page.submit_guard_installed is False
    assert page.marked_input is True
    assert page.click_count == 0
    assert page.handlers == {"pageerror": [], "console": [], "requestfailed": [], "response": []}


def test_replaced_file_input_is_detected_and_fails_closed(pdf, monkeypatch):
    quiet_events(monkeypatch)
    selected = {"count": 1, "file": {"name": pdf.name, "size": pdf.stat().st_size},
                "originalConnected": False, "originalStillInDocument": False, "currentInputCount": 2}
    page = FakePage(selected_override=selected)

    with pytest.raises(upload.UploadWorkflowError, match="replaced or detached"):
        upload.upload_resume_on_page(page, file_selection_test=True, resume_path=pdf)

    assert page.click_count == 0
    assert page.route_pattern is None


def test_zero_files_selected_is_not_reported_as_success(pdf, monkeypatch):
    quiet_events(monkeypatch)
    page = FakePage(selected_override={"count": 0, "file": None})
    with pytest.raises(upload.UploadWorkflowError, match="no selected file"):
        upload.upload_resume_on_page(page, file_selection_test=True, resume_path=pdf)
    assert page.click_count == 0


def test_selection_cleared_by_page_handler_is_distinguished(pdf, monkeypatch):
    quiet_events(monkeypatch)
    immediate = {"count": 1, "file": {"name": pdf.name, "size": pdf.stat().st_size}}
    page = FakePage(selected_override=immediate, settled_selection_override={"count": 0, "file": None})
    with pytest.raises(upload.UploadWorkflowError, match="selected immediately but the page cleared"):
        upload.upload_resume_on_page(page, file_selection_test=True, resume_path=pdf)
    assert page.click_count == 0


def test_file_selection_timeout_is_reported_without_update(pdf, monkeypatch):
    quiet_events(monkeypatch)
    page = FakePage(selection_error=TimeoutError())
    with pytest.raises(upload.UploadWorkflowError, match="Could not attach"):
        upload.upload_resume_on_page(page, file_selection_test=True, resume_path=pdf)
    assert page.click_count == 0
    assert page.route_pattern is None
    assert all(not listeners for listeners in page.handlers.values())


def test_diagnostic_observer_failure_stops_before_file_selection(pdf, monkeypatch):
    quiet_events(monkeypatch)
    page = FakePage()

    def fail_observer(event_name, callback):
        if event_name == "console":
            raise RuntimeError("private details omitted")
        page.handlers.setdefault(event_name, []).append(callback)

    page.on = fail_observer
    with pytest.raises(upload.UploadWorkflowError, match="safe page diagnostics"):
        upload.upload_resume_on_page(page, file_selection_test=True, resume_path=pdf)
    assert page.selected == {}
    assert page.click_count == 0


def test_page_event_observers_log_safe_failure_and_response_metadata(monkeypatch):
    events = []
    monkeypatch.setattr(upload, "log_event", lambda stage, status, **fields: events.append((stage, status, fields)))
    page = FakePage()
    counts, cleanup = upload._observe_selection_events(page)

    class Request:
        method = "GET"
        resource_type = "xhr"
        url = "https://example.test/path?token=secret#fragment"

    class Response:
        request = Request()
        url = request.url
        status = 503

    class ConsoleMessage:
        type = "error"
        text = "https://example.test/path?token=secret"

    page.handlers["console"][0](ConsoleMessage())
    page.handlers["requestfailed"][0](Request())
    page.handlers["response"][0](Response())
    page.handlers["pageerror"][0](RuntimeError("sensitive text"))
    cleanup()

    serialized = json.dumps(events)
    assert "secret" not in serialized
    assert "sensitive text" not in serialized
    assert "https://example.test" in serialized
    assert counts[0] == 3
    assert {event[0] for event in events} >= {
        "selection_console_error", "selection_request", "selection_response", "selection_page_error"
    }


def test_unexpected_dialog_blocks_without_attaching_file(pdf, monkeypatch):
    quiet_events(monkeypatch)
    structure = expected_structure()
    structure["visibleDialogCount"] = 1
    page = FakePage(structure=structure)

    with pytest.raises(upload.UploadWorkflowError, match="dialog"):
        upload.upload_resume_on_page(page, resume_path=pdf)

    assert page.selected == {}
    assert page.click_count == 0


def test_unreadable_pdf_is_rejected_before_inspecting_page(monkeypatch):
    quiet_events(monkeypatch)
    path = Path(".test-unreadable-upload.pdf")
    path.write_text("not a PDF", encoding="utf-8")
    page = FakePage()
    try:
        with pytest.raises(upload.UploadWorkflowError, match="readable"):
            upload.upload_resume_on_page(page, resume_path=path)
        assert page.selection_count == 0
        assert page.click_count == 0
    finally:
        path.unlink(missing_ok=True)


def test_live_upload_is_disabled_before_page_interaction(pdf, monkeypatch):
    quiet_events(monkeypatch)
    page = FakePage()
    with pytest.raises(upload.UploadWorkflowError, match="Live resume upload is disabled"):
        upload.upload_resume_on_page(page, live=True, resume_path=pdf)
    assert page.selection_count == 0
    assert page.selected == {}
    assert page.click_count == 0


@pytest.mark.parametrize("before,after,request_seen,response_status,expected", [
    ({"filename_visible": False}, {"filename_visible": True, "reload_completed": True, "siteResumeSuccess": False}, True, 201, "verified"),
    ({"filename_visible": False}, {"filename_visible": False, "reload_completed": True, "siteResumeSuccess": True}, True, 200, "unverified"),
    ({"filename_visible": False}, {"filename_visible": False, "reload_completed": True, "siteResumeSuccess": False}, True, 201, "unverified"),
    ({"filename_visible": False}, {"filename_visible": False, "reload_completed": True, "failure": True}, True, 500, "failed"),
    ({"filename_visible": True}, {"filename_visible": True, "reload_completed": True}, True, 200, "unverified"),
])
def test_post_upload_evidence_is_classified_conservatively(
    before, after, request_seen, response_status, expected,
):
    result = upload._classify_post_upload_verification(
        before, after, upload_request_observed=request_seen,
        upload_response_status=response_status,
    )
    assert result["status"] == expected
    if expected == "verified":
        assert result["saved_profile_verified"] is True
    if after.get("siteResumeSuccess"):
        assert result["site_success_notification"] is True
        assert result["saved_profile_verified"] is False


def test_post_upload_verification_reloads_and_requires_new_filename(pdf, monkeypatch):
    quiet_events(monkeypatch)
    page = FakePage()
    observed = {"filename_visible": True, "siteResumeSuccess": True, "failure": False}
    monkeypatch.setattr(upload, "_post_upload_state", lambda _page, _filename: observed)

    result = upload.verify_post_upload_state(
        page, pdf.name, {"filename_visible": False},
        upload_request_observed=True, upload_response_status=201,
    )

    assert result["status"] == "verified"
    assert result["upload_request_observed"] is True
    assert result["upload_response_accepted"] is True
    assert result["site_success_notification"] is True
    assert result["saved_profile_verified"] is True


def test_post_upload_reload_timeout_is_unverified(pdf, monkeypatch):
    quiet_events(monkeypatch)
    page = FakePage()
    page.reload_error = TimeoutError()
    result = upload.verify_post_upload_state(
        page, pdf.name, {"filename_visible": False},
        upload_request_observed=True, upload_response_status=200,
    )
    assert result["status"] == "unverified"
    assert result["saved_profile_verified"] is False


def test_live_disabled_does_not_launch_or_touch_browser(pdf, monkeypatch):
    from naukri_automation import tasks
    quiet_events(monkeypatch)
    monkeypatch.setattr(tasks, "validate_pdf", lambda path: {"path": pdf.resolve(), "size": pdf.stat().st_size})
    monkeypatch.setattr(tasks, "open_naukri", lambda **kwargs: pytest.fail("browser must not launch"))
    result = tasks.run_resume_upload(live=True)
    assert result["status"] == "disabled"
    assert result["upload_performed"] is False


def test_diagnostic_log_has_timestamp_and_omits_unapproved_content(monkeypatch):
    from naukri_automation.diagnostics import log_event
    from naukri_automation import diagnostics
    path = Path(".test-resume-upload-diagnostics.jsonl")
    monkeypatch.setattr(diagnostics, "_log_path", lambda: path)
    log_event(
        "test_stage", "observed", error_type="TimeoutError", resume_contents="private resume text",
        selection_method="playwright_set_input_files", matching_input_count=1,
        total_input_count=2, expected_file_size_bytes=177628,
        selected_file_count=1, selected_file_size_bytes=177628,
    )
    try:
        event = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
        assert event["stage"] == "test_stage"
        assert event["status"] == "observed"
        assert "timestamp" in event
        assert "resume_contents" not in event
        assert "private resume text" not in json.dumps(event)
        assert event["selection_method"] == "playwright_set_input_files"
        assert event["matching_input_count"] == 1
        assert event["total_input_count"] == 2
        assert event["expected_file_size_bytes"] == event["selected_file_size_bytes"]
    finally:
        path.unlink(missing_ok=True)
