"""Windows-friendly background schedule for bounded Jobicy discovery."""
from __future__ import annotations

import ctypes
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import settings
from .jobicy import fetch_jobicy_vacancies

TASK_NAME = "NaukriJobicyDiscoveryScheduler"
SCHEDULE_HOURS = (9, 21)
SCHEDULE_MINUTE = 0
_MUTEX_NAME = "Local\\NaukriJobicyDiscoveryScheduler"


def scheduler_trigger(timezone: str | None = None) -> CronTrigger:
    """Build the twice-daily India-time trigger (the timezone comes from config)."""
    return CronTrigger(hour=",".join(map(str, SCHEDULE_HOURS)), minute=SCHEDULE_MINUTE,
                       timezone=ZoneInfo(timezone or settings.timezone))


def run_scheduled_discovery() -> None:
    """Fetch and rank only; errors are summarized without listing contents."""
    try:
        result = fetch_jobicy_vacancies()
        _log_event("scheduled_fetch_complete", status="success", inserted=result["inserted"],
                   updated=result["updated"], processed=result["processed"])
    except Exception as exc:
        _log_event("scheduled_fetch_failed", status="failed", error_type=type(exc).__name__)


def _log_event(stage: str, **safe_fields) -> None:
    root = Path(__file__).resolve().parent.parent
    path = root / "logs" / "jobicy_scheduler.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {"timestamp": datetime.now(ZoneInfo(settings.timezone)).isoformat(timespec="seconds"),
             "stage": stage, **safe_fields}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def _windows_launcher(project_root: Path, python_executable: Path) -> Path:
    root = Path(project_root).resolve()
    executable = Path(python_executable).resolve()
    pythonw = executable.with_name("pythonw.exe")
    if not pythonw.is_file():
        pythonw = executable
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    launcher = data_dir / "jobicy_scheduler.cmd"
    content = (
        "@echo off\r\n"
        f'cd /d "{root}"\r\n'
        f'"{pythonw}" -m naukri_automation.jobicy_scheduler\r\n'
    )
    if launcher.exists():
        existing = launcher.read_text(encoding="utf-8", errors="replace")
        if "naukri_automation.jobicy_scheduler" not in existing:
            raise RuntimeError("Refusing to overwrite an unrelated file at the scheduler launcher path")
    launcher.write_text(content, encoding="utf-8", newline="")
    return launcher


def enable_windows_schedule(*, project_root: Path | None = None, python_executable: Path | None = None,
                            runner=subprocess.run, windows: bool | None = None) -> dict:
    """Install an on-logon task that starts a timezone-aware background daemon."""
    is_windows = os.name == "nt" if windows is None else windows
    if not is_windows:
        raise RuntimeError("Windows Task Scheduler is available only on Windows")
    root = Path(project_root or Path(__file__).resolve().parent.parent)
    executable = Path(python_executable or sys.executable)
    launcher = _windows_launcher(root, executable)
    try:
        completed = runner(["schtasks.exe", "/Create", "/TN", TASK_NAME, "/SC", "ONLOGON",
                            "/TR", f'"{launcher}"', "/RL", "LIMITED", "/F"],
                           check=False, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Could not invoke Windows Task Scheduler") from None
    if completed.returncode != 0:
        raise RuntimeError("Windows Task Scheduler did not accept the discovery task")
    try:
        started = runner(["schtasks.exe", "/Run", "/TN", TASK_NAME],
                         check=False, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Task was installed, but its background scheduler could not be started") from None
    if started.returncode != 0:
        raise RuntimeError("Task was installed, but its background scheduler could not be started")
    return {"task_name": TASK_NAME, "launcher": launcher, "hours": SCHEDULE_HOURS,
            "timezone": settings.timezone, "started": True}


def disable_windows_schedule(*, runner=subprocess.run, windows: bool | None = None) -> None:
    """Stop and remove only this project's named task; leave the launcher file intact."""
    is_windows = os.name == "nt" if windows is None else windows
    if not is_windows:
        raise RuntimeError("Windows Task Scheduler is available only on Windows")
    try:
        # /End may return nonzero when the task is installed but not currently running.
        runner(["schtasks.exe", "/End", "/TN", TASK_NAME],
               check=False, capture_output=True, text=True, timeout=30)
        completed = runner(["schtasks.exe", "/Delete", "/TN", TASK_NAME, "/F"],
                           check=False, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Could not invoke Windows Task Scheduler") from None
    if completed.returncode != 0:
        raise RuntimeError("Windows Task Scheduler could not remove the discovery task")


def _acquire_windows_mutex():
    if os.name != "nt":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if not handle:
        raise OSError("Could not acquire the Jobicy scheduler process lock")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return None
    return (kernel32, handle)


def main() -> None:
    mutex = _acquire_windows_mutex()
    if mutex is None:
        _log_event("scheduler_already_running", status="skipped")
        return
    scheduler = BlockingScheduler(timezone=ZoneInfo(settings.timezone))
    scheduler.add_job(run_scheduled_discovery, scheduler_trigger(), id="jobicy-fetch-rank-twice-daily",
                      replace_existing=True, coalesce=True, max_instances=1, misfire_grace_time=3600)
    _log_event("scheduler_started", status="running", hours=SCHEDULE_HOURS, timezone=settings.timezone)
    try:
        scheduler.start()
    finally:
        if isinstance(mutex, tuple):
            mutex[0].CloseHandle(mutex[1])


if __name__ == "__main__":
    main()
