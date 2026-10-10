"""Daily scheduled jobs (UC-14). Schedule with cron at 06:00 Cairo time:

    0 6 * * *  cd /srv/kubbar && .venv/bin/python manage.py run_daily_jobs
"""
from django.core.management.base import BaseCommand

from inventory.services import run_expiry_scan


class Command(BaseCommand):
    help = "Run the daily expiry scan, reservation expiry and licence checks."

    def handle(self, *args, **options):
        run = run_expiry_scan(triggered_by="scheduler")
        if run is None:
            self.stderr.write(self.style.ERROR("Expiry scan failed; administrator alerted."))
            return
        self.stdout.write(self.style.SUCCESS(
            f"Scan OK: {run.batches_scanned} batches, {run.new_near_expiry} new near-expiry, "
            f"{run.expired} expired, {run.listings_withdrawn} listings withdrawn, "
            f"{run.escalations} escalations, value at risk {run.value_at_risk} EGP."))
