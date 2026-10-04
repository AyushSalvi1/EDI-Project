"""End-to-end test of the PDF import pipeline against the messy sample PDF."""
import os
import sys
import django

sys.path.insert(0, r"C:\EDI Project\TimeTableGenerator\timetable_project")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "timetable_project.settings")
django.setup()

import subprocess

from scheduler.models import (Assignment, Room, Semester, SolverRun, Subject, Teacher,
                               TeacherUnavailability, TimetableEntry, YearDivision)
from scheduler.pdf_import import build_payload, extract_document_title, extract_tables
from scheduler.importer import import_college_data_from_dict
from scheduler.solver import generate_timetable

pdf_path = r"C:\Users\lenovo\AppData\Local\Temp\kilo\pdfout\messy_college.pdf"
subprocess.run([sys.executable, "make_sample_pdf.py", pdf_path], check=True,
               stdout=subprocess.DEVNULL)

print("=" * 78)
print("STEP 1: extract tables")
print("=" * 78)
tables = extract_tables(pdf_path)
for t in tables:
    print(f"  table#{t['index']} page {t['page']}  role={t['role']:<12} "
          f"rows={t['row_count']:<3} confidence={t['mapping_confidence']}%")
    print(f"      headers: {t['headers']}")
    print(f"      mapping : {t['mapping']}")
    if t["unmapped"]:
        print(f"      unmapped: {t['unmapped']}")

print()
print("=" * 78)
print("STEP 2: build payload (no manual overrides)")
print("=" * 78)
payload = build_payload(tables, overrides={}, document_title=extract_document_title(pdf_path))
warnings = payload.pop("_warnings", [])
col = payload["college"]
print(f"  college    : {col['name']}")
print(f"  semester   : {col['semester_name']}")
print(f"  dates      : {col['start_date']} -> {col['end_date']}")
print(f"  working day: {col['working_days']}")
print(f"  timings    : {col['daily_start_time']}-{col['daily_end_time']} "
      f"({col['period_duration_minutes']}min) lunch {col['lunch_break']['start_time']}-"
      f"{col['lunch_break']['end_time']}")
print(f"  years      : {[(y['year'], len(y['divisions'])) for y in payload['years']]}")
print(f"  subjects   : {len(payload['subjects'])} "
      f"({sum(1 for s in payload['subjects'] if s['is_lab'])} lab)")
print(f"  rooms      : {payload['rooms']}")
print(f"  teachers   : {len(payload['teachers'])}")
for t in payload["teachers"]:
    print(f"      {t['name']:<26} max={t['max_hours_per_week']}h  "
          f"allocs={len(t['allocations'])}  unavail={t['unavailable_slots']}")
print("  warnings:")
for w in warnings:
    print(f"      - {w}")

print()
print("=" * 78)
print("STEP 3: import into database and solve")
print("=" * 78)
Assignment.objects.all().delete()
TimetableEntry.objects.all().delete()
SolverRun.objects.all().delete()
Semester.objects.all().delete()
YearDivision.objects.all().delete()
Room.objects.all().delete()
Subject.objects.all().delete()
Teacher.objects.all().delete()
TeacherUnavailability.objects.all().delete()

result = import_college_data_from_dict(payload)
sem = result["semester"]
print(f"  imported: {result['divisions_count']} divisions, {result['subjects_count']} subjects, "
      f"{result['rooms_count']} rooms, {result['teachers_count']} teachers, "
      f"{result['assignments_count']} assignments, {result['slots_count']} slots")

print("  divisions created:")
for d in YearDivision.objects.all().order_by("year", "division_number"):
    print(f"      Y{d.year}-D{d.division_number} ({d.name}) strength={d.strength}")

print("  subjects created:")
for s in Subject.objects.all().order_by("code"):
    print(f"      {s.code:<28} {s.name:<30} lab={s.is_lab}")

print("  teacher unavailability:")
for u in TeacherUnavailability.objects.select_related("teacher", "time_slot"):
    print(f"      {u.teacher.name} -> {u.time_slot}")

solve = generate_timetable(sem.id)
print(f"  solve: {solve['message']}")

print()
print("  VERIFY no conflicts in stored timetable:")
entries = list(TimetableEntry.objects.filter(semester=sem).select_related(
    "assignment__teacher", "assignment__division", "assignment__subject", "room", "time_slot"))
print(f"      entries={len(entries)}")
dup_teacher = [(e.assignment.teacher_id, e.time_slot_id) for e in entries]
dup_div = [(e.assignment.division_id, e.time_slot_id) for e in entries]
dup_room = [(e.room_id, e.time_slot_id) for e in entries]
print(f"      teacher double-bookings : {len(dup_teacher) - len(set(dup_teacher))}")
print(f"      division double-bookings: {len(dup_div) - len(set(dup_div))}")
print(f"      room double-bookings    : {len(dup_room) - len(set(dup_room))}")
unavail = set(TeacherUnavailability.objects.values_list("teacher_id", "time_slot_id"))
violations = [(e.assignment.teacher.name, e.time_slot.slot_code) for e in entries
              if (e.assignment.teacher_id, e.time_slot_id) in unavail]
print(f"      unavailability violations: {len(violations)}")
bad_room = [e for e in entries if e.assignment.subject.is_lab != e.room.is_lab]
print(f"      lab/theory room mismatches: {len(bad_room)}")
small = [e for e in entries if e.room.capacity < e.assignment.division.strength]
print(f"      undersized rooms          : {len(small)}")