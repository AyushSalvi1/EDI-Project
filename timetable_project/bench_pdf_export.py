"""Generate the four PDF reports against a 30-division dataset and validate them."""
import os
import sys
import time
import django

sys.path.insert(0, r"C:\EDI Project\TimeTableGenerator\timetable_project")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "timetable_project.settings")
django.setup()

import pdfplumber

from scheduler.models import Assignment, Room, Semester, SolverRun, Teacher, TimetableEntry, YearDivision
from scheduler.importer import import_college_data_from_dict
from scheduler.solver import generate_timetable
from scheduler.capacity import analyse_capacity
from scheduler.pdf_export import (
    build_division_timetable_pdf,
    build_full_institution_pdf,
    build_issues_pdf,
    build_teacher_load_pdf,
)
from bench_scale import build_payload

Assignment.objects.all().delete()
TimetableEntry.objects.all().delete()
SolverRun.objects.all().delete()
Semester.objects.all().delete()
YearDivision.objects.all().delete()
Room.objects.all().delete()
Teacher.objects.all().delete()

result = import_college_data_from_dict(build_payload(30))
sem = result["semester"]
print(f"imported: {YearDivision.objects.count()} divisions, "
      f"{Assignment.objects.count()} assignments, {Room.objects.count()} rooms")

plan = analyse_capacity(sem)
print(f"capacity verdict: {plan['verdict']} | divisions={plan['division_count']} "
      f"| demand={plan['demand']} supply={plan['supply']} "
      f"| utilisation={plan['utilisation_percent']}% "
      f"| blocking={len(plan['blocking_findings'])}")
for f in plan["blocking_findings"]:
    print("   BLOCKING:", f["message"])

t0 = time.perf_counter()
res = generate_timetable(sem.id)
print(f"solve: {time.perf_counter()-t0:.2f}s -> {res['message']}")

out_dir = r"C:\Users\lenovo\AppData\Local\Temp\kilo\pdfout"
os.makedirs(out_dir, exist_ok=True)

jobs = [
    ("division.pdf", lambda: build_division_timetable_pdf(sem, YearDivision.objects.first())),
    ("full_institution.pdf", lambda: build_full_institution_pdf(sem)),
    ("faculty_load.pdf", lambda: build_teacher_load_pdf(sem)),
    ("issues.pdf", lambda: build_issues_pdf(sem)),
]

for name, builder in jobs:
    t0 = time.perf_counter()
    buf = builder()
    data = buf.getvalue()
    path = os.path.join(out_dir, name)
    with open(path, "wb") as fh:
        fh.write(data)
    elapsed = time.perf_counter() - t0
    with pdfplumber.open(path) as pdf:
        pages = len(pdf.pages)
        text = pdf.pages[min(1, pages - 1)].extract_text() or ""
    print(f"{name:<24} {len(data)/1024:8.1f} KB  {pages:>3} pages  {elapsed:5.2f}s")
    print(f"    page2 text sample: {' '.join(text.split())[:110]}")