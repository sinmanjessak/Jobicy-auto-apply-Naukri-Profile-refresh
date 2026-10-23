import logging
import json
from types import SimpleNamespace

import pytest

from naukri_automation import browser


class FakePage:
    def __init__(self, url="about:blank", goto_error=None, load_error=None, ready_state="complete"):
        self.url = url
        self.goto_error = goto_error
        self.load_error = load_error
        self.ready_state = ready_state
        self.goto_calls = []

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        if self.goto_error:
            raise self.goto_error
        self.url = url
        return SimpleNamespace(status=200)

    def wait_for_load_state(self, state, timeout):
        if self.load_error:
            raise self.load_error

    def evaluate(self, script):
        return self.ready_state


class FakeContext:
    def __init__(self, pages):
        self.pages = pages
        self.closed = False

    def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page

    def set_default_timeout(self, timeout):
        self.default_timeout = timeout

    def set_default_navigation_timeout(self, timeout):
        self.default_navigation_timeout = timeout

    def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self, context, launch_error=None):
        self.launch_error = launch_error
        self.launch_kwargs = None
        def launch_persistent_context(**kwargs):
            self.launch_kwargs = kwargs
            if self.launch_error:
                raise self.launch_error
            return context
        self.chromium = SimpleNamespace(launch_persistent_context=launch_persistent_context)
        self.stopped = False

    def start(self):
        return self

    def stop(self):
        self.stopped = True


def setup_fake(monkeypatch, context, launch_error=None):
    fake_pw = FakePlaywright(context, launch_error=launch_error)
    monkeypatch.setattr(browser, "sync_playwright", lambda: SimpleNamespace(start=lambda: fake_pw))
    monkeypatch.setattr(browser, "log_event", lambda *args, **kwargs: None)
    return fake_pw


def test_open_naukri_reuses_existing_site_tab_without_overwriting_it(monkeypatch, caplog):
    page = FakePage("https://www.naukri.com/mnjuser/profile")
    context = FakeContext([page])
    setup_fake(monkeypatch, context)

    with caplog.at_level(logging.INFO):
        pw, returned_context, returned_page = browser.open_naukri(headless=False)

    assert returned_context is context
    assert returned_page is page
    assert page.goto_calls == []
    assert pw.launch_kwargs["user_data_dir"] == str(browser.settings.browser_profile_path)
    assert pw.launch_kwargs["headless"] is False
    assert pw.launch_kwargs["timeout"] == browser.settings.browser_launch_timeout_ms
    events = [record.message for record in caplog.records]
    assert any('"stage": "launch"' in event and '"status": "succeeded"' in event for event in events)
    assert any('"stage": "playwright_initialization"' in event and '"status": "succeeded"' in event for event in events)
    assert any('"stage": "chromium_launch"' in event and '"status": "succeeded"' in event for event in events)
    assert any('"stage": "persistent_context_creation"' in event and '"status": "succeeded"' in event for event in events)
    assert any('"stage": "page_creation"' in event and '"status": "reused"' in event for event in events)
    assert any('"stage": "page_readiness"' in event and '"status": "succeeded"' in event for event in events)
    assert any('"stage": "navigation"' in event and '"status": "skipped"' in event for event in events)
    assert all('"timestamp":' in event for event in events)


def test_navigation_timeout_is_clear_and_does_not_log_url_secrets(monkeypatch, caplog):
    secret_url = "https://user:pass@www.naukri.com/profile?session=private"
    page = FakePage(goto_error=TimeoutError(secret_url))
    context = FakeContext([page])
    pw = setup_fake(monkeypatch, context)

    with caplog.at_level(logging.INFO):
        with pytest.raises(browser.BrowserStartupError, match="navigation") as error:
            browser.open_naukri()

    assert "private" not in str(error.value)
    assert "user:pass" not in str(error.value)
    assert "private" not in " ".join(record.message for record in caplog.records)
    assert "user:pass" not in " ".join(record.message for record in caplog.records)
    assert context.closed is True
    assert pw.stopped is True


def test_slow_noncritical_load_event_is_logged_but_visible_page_is_returned(monkeypatch, caplog):
    page = FakePage(load_error=TimeoutError("load event timeout"), ready_state="interactive")
    context = FakeContext([page])
    setup_fake(monkeypatch, context)

    with caplog.at_level(logging.INFO):
        _, _, returned_page = browser.open_naukri()

    assert returned_page is page
    assert any('"status": "timed_out_but_interactive"' in record.message for record in caplog.records)


def test_page_load_timeout_before_interactive_fails_and_cleans_up(monkeypatch):
    page = FakePage(load_error=TimeoutError("load timeout"), ready_state="loading")
    context = FakeContext([page])
    pw = setup_fake(monkeypatch, context)

    with pytest.raises(browser.BrowserStartupError, match="page_readiness"):
        browser.open_naukri()

    assert context.closed is True
    assert pw.stopped is True


def test_persistent_launch_failure_is_stage_specific_and_sanitized(monkeypatch, caplog):
    context = FakeContext([])
    pw = setup_fake(monkeypatch, context, launch_error=RuntimeError("password cookie session=secret"))

    with caplog.at_level(logging.INFO):
        with pytest.raises(browser.BrowserStartupError, match="persistent_context_creation") as error:
            browser.open_naukri(headless=False)

    messages = " ".join(record.message for record in caplog.records)
    events = [json.loads(record.message) for record in caplog.records]
    assert "secret" not in messages
    assert "password" not in str(error.value)
    assert "session=" not in messages
    assert pw.stopped is True
    assert any(item["stage"] == "chromium_launch" and item["status"] == "failed" for item in events)
    assert any(item["stage"] == "persistent_context_creation" and item["status"] == "failed" for item in events)


def test_playwright_initialization_failure_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(browser, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        browser,
        "sync_playwright",
        lambda: SimpleNamespace(start=lambda: (_ for _ in ()).throw(RuntimeError("private token"))),
    )

    with caplog.at_level(logging.INFO):
        with pytest.raises(browser.BrowserStartupError, match="playwright_initialization"):
            browser.open_naukri()

    messages = " ".join(record.message for record in caplog.records)
    events = [json.loads(record.message) for record in caplog.records]
    assert "private" not in messages
    assert "token" not in messages
    assert any(item["stage"] == "playwright_initialization" and item["status"] == "failed" for item in events)
