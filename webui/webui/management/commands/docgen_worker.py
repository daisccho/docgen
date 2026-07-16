import time

from django.core.management.base import BaseCommand

from webui.webui.services import claim_next_job, execute_job


class Command(BaseCommand):
    help = "Process queued docgen jobs. Run as a separate service."

    def add_arguments(self, parser):
        parser.add_argument("--poll-interval", type=float, default=2.0)
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("Docgen worker started"))
        while True:
            job = claim_next_job()
            if job:
                self.stdout.write(f"Running job #{job.pk}: {job}")
                execute_job(job)
                self.stdout.write(f"Job #{job.pk}: {job.status}")
            elif options["once"]:
                return
            else:
                time.sleep(options["poll_interval"])
