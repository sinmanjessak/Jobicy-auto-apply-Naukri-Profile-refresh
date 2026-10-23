"""Visible, manually authenticated browser startup with privacy-safe diagnostics."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from .config import settings
from .diagnostics import log_event

logger = logging.getLogger("naukri_automation.browser")


class BrowserStartupError(RuntimeError):
    """Safe-to-display browser startup error without raw Playwright exception text."""

    def __init__(self, stage: str, message: str):
        self.stage = stage
        super().__init__(f"Browser startup failed during {stage}: {message}")


def _origin(url: str) -> str:
    """Return scheme/host/port only, so URL paths and credentials are never logged."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port is not None else ""
        return f"{parts.scheme}://{host}{port}" if host else "configured site"
    except (TypeError, ValueError):
        return "configured site"


def _diagnostic(stage: str, status: str, **fields) -> None:
    """Emit timestamped, allowlisted JSON to both the console and project log."""
    event = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "component": "browser",
        "stage": stage,
        "status": status,
        **fields,
    }
    logger.info(json.dumps(event, sort_keys=True))
    log_event(f"browser_{stage}", status, **fields)


def _close_quietly(context, pw) -> None:
    if context is not None:
        try:
            context.close()
        except Exception:
            pass
    if pw is not None:
        try:
            pw.stop()
        except Exception:
            pass


def open_naukri(headless: bool = False):
    """Launch the project's persistent profile and open/reuse a Naukri tab.

    This launches a new Playwright persistent context backed by the same user-data
    directory on each invocation. It does not attach to an already-running browser
    process. Login, OTP, CAPTCHA, and other access controls remain manual.
    """
    configured_level = getattr(logging, str(settings.log_level).upper(), logging.INFO)
    logging.basicConfig(
        level=min(configured_level, logging.INFO),
        format="%(message)s",
    )
    host = _origin(settings.base_url)
    pw = context = None
    stage = "playwright_initialization"
    stage_started = time.monotonic()
    profile_exists = settings.browser_profile_path.is_dir()
    _diagnostic("launch", "started", headless=bool(headless), profile_present=profile_exists,
                profile_mode="persistent_user_data_directory", timeout_ms=settings.browser_launch_timeout_ms)
    _diagnostic(stage, "started", timeout_ms=settings.browser_launch_timeout_ms)
    try:
        pw = sync_playwright().start()
        _diagnostic(stage, "succeeded", elapsed_ms=round((time.monotonic() - stage_started) * 1000))
    except Exception as exc:
        elapsed = round((time.monotonic() - stage_started) * 1000)
        _diagnostic(stage, "failed", elapsed_ms=elapsed, error_type=type(exc).__name__, timeout_ms=settings.browser_launch_timeout_ms)
        _diagnostic("launch", "failed", elapsed_ms=elapsed, startup_stage=stage, error_type=type(exc).__name__)
        raise BrowserStartupError(stage, "Playwright could not initialize. Check the installed Playwright runtime and retry.") from None

    # Playwright exposes Chromium startup and persistent-context creation as one
    # launch_persistent_context operation. Record both sub-stages; they complete
    # together when that documented operation returns.
    stage_started = time.monotonic()
    for launch_stage in ("chromium_launch", "persistent_context_creation"):
        _diagnostic(launch_stage, "started", timeout_ms=settings.browser_launch_timeout_ms,
                    profile_mode="persistent_user_data_directory")
    try:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(settings.browser_profile_path),
            headless=headless,
            viewport={"width": 1440, "height": 900},
            args=["--start-maximized"],
            timeout=settings.browser_launch_timeout_ms,
        )
        pages = context.pages
        elapsed = round((time.monotonic() - stage_started) * 1000)
        for launch_stage in ("chromium_launch", "persistent_context_creation"):
            _diagnostic(launch_stage, "succeeded", elapsed_ms=elapsed)
        _diagnostic("launch", "succeeded", elapsed_ms=elapsed, existing_page_count=len(pages),
                    profile_mode="persistent_user_data_directory")
    except Exception as exc:
        _close_quietly(context, pw)
        elapsed = round((time.monotonic() - stage_started) * 1000)
        for launch_stage in ("chromium_launch", "persistent_context_creation"):
            _diagnostic(launch_stage, "failed", elapsed_ms=elapsed, error_type=type(exc).__name__,
                        timeout_ms=settings.browser_launch_timeout_ms)
        _diagnostic("launch", "failed", elapsed_ms=elapsed, startup_stage="persistent_context_creation",
                    error_type=type(exc).__name__)
        raise BrowserStartupError(
            "persistent_context_creation",
            "Chromium could not start its persistent context before the launch timeout. The profile was not changed; check whether another browser process is using it.",
        ) from None

    stage = "page_creation"
    stage_started = time.monotonic()
    _diagnostic(stage, "started", timeout_ms=settings.browser_launch_timeout_ms)
    try:
        # These defaults bound locator and navigation operations on this context.
        context.set_default_timeout(settings.browser_navigation_timeout_ms)
        context.set_default_navigation_timeout(settings.browser_navigation_timeout_ms)
        site_page = next((page for page in pages if _origin(page.url) == host), None)
        blank_page = next((page for page in pages if page.url in ("", "about:blank")), None)
        if site_page is not None:
            page = site_page
            page_selection = "reused_existing_site_tab"
            _diagnostic(stage, "reused", page_selection=page_selection,
                        existing_page_count=len(pages), elapsed_ms=round((time.monotonic() - stage_started) * 1000))
        elif blank_page is not None:
            page = blank_page
            page_selection = "reused_blank_tab"
            _diagnostic(stage, "reused", page_selection=page_selection,
                        existing_page_count=len(pages), elapsed_ms=round((time.monotonic() - stage_started) * 1000))
        else:
            page = context.new_page()
            page_selection = "created_new_tab"
            _diagnostic(stage, "succeeded", page_selection=page_selection,
                        elapsed_ms=round((time.monotonic() - stage_started) * 1000))

        if page_selection == "reused_existing_site_tab":
            _diagnostic("navigation", "skipped", page_selection=page_selection)
        else:
            stage = "navigation"
            stage_started = time.monotonic()
            _diagnostic(stage, "started", timeout_ms=settings.browser_navigation_timeout_ms,
                        page_selection=page_selection)
            response = page.goto(settings.base_url, wait_until="domcontentloaded",
                                 timeout=settings.browser_navigation_timeout_ms)
            status_code = response.status if response is not None else None
            elapsed = round((time.monotonic() - stage_started) * 1000)
            if status_code is not None and status_code >= 400:
                _diagnostic(stage, "failed", http_status=status_code, elapsed_ms=elapsed)
                raise BrowserStartupError(stage, f"the configured site returned HTTP {status_code}.")
            _diagnostic(stage, "succeeded", http_status=status_code, elapsed_ms=elapsed)

        stage = "page_readiness"
        stage_started = time.monotonic()
        _diagnostic(stage, "started", timeout_ms=settings.browser_page_load_timeout_ms)
        try:
            page.wait_for_load_state("load", timeout=settings.browser_page_load_timeout_ms)
            _diagnostic(stage, "succeeded", elapsed_ms=round((time.monotonic() - stage_started) * 1000),
                        ready_state="complete")
        except Exception as exc:
            try:
                ready_state = page.evaluate("() => document.readyState")
            except Exception:
                ready_state = "unknown"
            elapsed = round((time.monotonic() - stage_started) * 1000)
            if ready_state not in ("interactive", "complete"):
                _diagnostic(stage, "failed", elapsed_ms=elapsed, error_type=type(exc).__name__,
                            ready_state=ready_state, timeout_ms=settings.browser_page_load_timeout_ms)
                raise BrowserStartupError(stage, "the page did not become interactive before its readiness timeout.") from None
            _diagnostic(stage, "timed_out_but_interactive", elapsed_ms=elapsed,
                        error_type=type(exc).__name__, ready_state=ready_state,
                        timeout_ms=settings.browser_page_load_timeout_ms)
        return pw, context, page
    except BrowserStartupError as exc:
        _close_quietly(context, pw)
        raise
    except Exception as exc:
        failed_stage = stage
        _close_quietly(context, pw)
        _diagnostic(failed_stage, "failed", error_type=type(exc).__name__)
        raise BrowserStartupError(
            failed_stage,
            "the browser startup stage failed. Check the timestamped diagnostic log; the profile was not deleted or reset.",
        ) from None
