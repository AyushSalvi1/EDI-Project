"""Benchmark the CP-SAT solver at realistic multi-division college scales."""
import os
import sys
import time
import django

sys.path.insert(0, r"C:\EDI Project\TimeTableGenerator\timetable_project")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "timetable_project.settings")
django.setup()

from scheduler.models import Assignment, Room, Semester, SolverRun, Teacher, TimetableEntry, YearDivision
from scheduler.importer import import_college_data_from_dict
from scheduler.solver import generate_timetable

THEORY = [
    {"id": "S1", "name": "Applied Mathematics", "is_lab": False},
    {"id": "S2", "name": "Engineering Physics", "is_lab": False},
    {"id": "S3", "name": "Data Structures", "is_lab": False},
    {"id": "S4", "name": "Digital Logic", "is_lab": False},
]
LABS = [
    {"id": "S5", "name": "Physics Laboratory", "is_lab": True},
    {"id": "S6", "name": "Programming Laboratory", "is_lab": True},
]


def build_payload(num_divisions):
    """Each division takes 4 theory + 2 lab subjects; each teacher covers ~3 classes."""
    allocations = []
    for d in range(1, num_divisions + 1):
        for s in THEORY + LABS:
            allocations.append((d, s))

    teachers = []
    teachers_per_division = 2
    idx = 0
    for d in range(1, num_divisions + 1):
        for t in range(teachers_per_division):
            idx += 1
            mine = [
                {"year": 1, "division": div, "subject_id": subj["id"],
                 "total_hours_for_semester": 30}
                for (div, subj) in allocations
                if div == d and subj["id"] in {"S1", "S2", "S5"} or
                (div == d and subj["id"] in {"S3", "S4", "S6"} and t == 1)
            ]
            if mine:
                teachers.append({
                    "id": f"T{idx}", "name": f"Prof. Faculty {idx}",
                    "max_hours_per_week": 16,
                    "unavailable_slots": ["MON-P1"],
                    "allocations": mine,
                })

    return {
        "college": {
            "name": f"Benchmark College ({num_divisions} divisions)",
            "semester_name": f"Bench {num_divisions}",
            "start_date": "2026-01-05", "end_date": "2026-04-20",
            "working_days": ["MON", "TUE", "WED", "THU", "FRI", "SAT"],
            "daily_start_time": "09:00", "daily_end_time": "17:00",
            "period_duration_minutes": 60,
            "lunch_break": {"start_time": "13:00", "end_time": "14:00"},
        },
        "years": [{"year": 1, "number_of_divisions": num_divisions, "strength_per_division": 60}],
        "subjects": THEORY + LABS,
        "rooms": {"number_of_regular_rooms": max(10, num_divisions),
                  "regular_room_capacity": 70,
                  "number_of_lab_rooms": max(3, num_divisions // 4),
                  "lab_room_capacity": 60},
        "teachers": teachers,
    }


def run(num_divisions):
    Assignment.objects.all().delete()
    TimetableEntry.objects.all().delete()
    SchedulerRun = SolverRun
    SchedulerRun.objects.all().delete()
    Semester.objects.all().delete()
    YearDivision.objects.all().delete()
    Room.objects.all().delete()
    Teacher.objects.all().delete()

    t0 = time.perf_counter()
    result = import_college_data_from_dict(build_payload(num_divisions))
    import_s = time.perf_counter() - t0
    sem = result["semester"]

    print(f"  import: {import_s:6.2f}s | assignments={Assignment.objects.count():4d} "
          f"rooms={Room.objects.count():3d} teachers={Teacher.objects.count():3d}", flush=True)

    t0 = time.perf_counter()
    try:
        res = generate_timetable(sem.id)
        wall = time.perf_counter() - t0
    except Exception as exc:
        print(f"  SOLVE FAILED after {time.perf_counter()-t0:.2f}s: {type(exc).__name__}: {str(exc)[:160]}",
              flush=True)
        return
    run_row = SolverRun.objects.filter(semester=sem).latest("created_at")
    print(f"  SOLVE: wall={wall:6.2f}s solver_reported={run_row.wall_time_seconds:6.2f}s "
          f"vars={run_row.variable_count:7d} constraints={run_row.constraint_count:7d} "
          f"status={run_row.solver_status}", flush=True)
    print(f"  RESULT: {res['total_hours_scheduled']}/{res['total_hours_requested']} hours, "
          f"{res['issues_count']} issues, entries={TimetableEntry.objects.filter(semester=sem).count()}",
          flush=True)


if __name__ == "__main__":
    sizes = [int(a) for a in sys.argv[1:]] or [20]
    for n in sizes:
        print(f"\n=== {n} divisions ===", flush=True)
        run(n)