import os, json
from django.core.management.base import BaseCommand
from django.conf import settings
from scheduler.importer import import_from_json


class Command(BaseCommand):
    help = 'Load sample_college_data.json and auto-generate timetable'

    def add_arguments(self, parser):
        parser.add_argument('--file', default='sample_college_data.json',
                            help='Path to JSON file (relative to project root)')
        parser.add_argument('--no-solve', action='store_true',
                            help='Import data only, do not run solver')

    def handle(self, *args, **opts):
        path = os.path.join(settings.BASE_DIR, opts['file'])
        if not os.path.exists(path):
            self.stderr.write(self.style.ERROR(f"File not found: {path}"))
            return
        with open(path, encoding='utf-8') as f:
            raw = f.read()
        result = import_from_json(raw, auto_generate=not opts['no_solve'])
        self.stdout.write(self.style.SUCCESS(f"Semester: {result['semester_name']}"))
        for log in result['logs']:
            # strip emoji characters for Windows cp1252 compatibility
            safe_log = log.encode('ascii', 'replace').decode('ascii')
            self.stdout.write(f"  {safe_log}")
        sr = result.get('solver_result')
        if sr:
            if sr.get('success'):
                self.stdout.write(self.style.SUCCESS(
                    f"  OK Solver: {sr['entries_count']} slots in {sr['solve_time']:.2f}s"
                ))
            else:
                self.stdout.write(self.style.WARNING(f"  WARNING: {sr.get('error')}"))
                for d in sr.get('diagnostics', []):
                    self.stdout.write(self.style.WARNING(f"    - {d}"))

