from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from .config import settings
from .tasks import inspect_profile
from .db import record_run
from rich.console import Console

console = Console()

def scheduled_check():
    """Scheduled check scaffold. It does not change the profile until configured."""
    console.print("09:00 job triggered. Profile-update workflow is not configured yet; no changes made.")
    record_run("scheduled_profile_check", "needs_configuration", "No profile update performed")


def main():
    scheduler = BlockingScheduler(timezone=settings.timezone)
    scheduler.add_job(scheduled_check, CronTrigger(hour=settings.refresh_hour, minute=settings.refresh_minute, timezone=settings.timezone), id="profile-refresh-check", replace_existing=True)
    console.print(f"Scheduler running; daily trigger at {settings.refresh_hour:02d}:{settings.refresh_minute:02d} ({settings.timezone}). Keep this process running.")
    scheduler.start()

if __name__ == "__main__":
    main()
