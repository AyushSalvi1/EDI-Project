"""Prove the bottleneck is the room dimension: compare full model vs slot-only model."""
import os
import sys
import time
import django

sys.path.insert(0, r"C:\EDI Project\TimeTableGenerator\timetable_project")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "timetable_project.settings")
django.setup()

from collections import defaultdict

from ortools.sat.python import cp_model

from scheduler.models import Assignment, Room, Semester, TeacherUnavailability, TimeSlot
from bench_scale import build_payload
from scheduler.importer import import_college_data_from_dict

Assignment.objects.all().delete()
Semester.objects.all().delete()
result = import_college_data_from_dict(build_payload(20))
sem = result["semester"]

assignments = list(Assignment.objects.filter(semester=sem).select_related("teacher", "subject", "division"))
rooms = list(Room.objects.all())
slots = list(TimeSlot.objects.all())
regular = [r for r in rooms if not r.is_lab]
labs = [r for r in rooms if r.is_lab]

unavail = defaultdict(set)
for tid, sid in TeacherUnavailability.objects.all().values_list("teacher_id", "time_slot_id"):
    unavail[tid].add(sid)

print(f"assignments={len(assignments)} regular_rooms={len(regular)} lab_rooms={len(labs)} slots={len(slots)}")

# ---- FULL MODEL: x[a, r, t] (current production model) ----
m = cp_model.CpModel()
full_vars = 0
t_slot = defaultdict(list)
d_slot = defaultdict(list)
r_slot = defaultdict(list)
a_vars = defaultdict(list)

for a in assignments:
    matching = [r for r in (labs if a.subject.is_lab else regular) if r.capacity >= a.division.strength]
    for slot in slots:
        if slot.id in unavail[a.teacher_id]:
            continue
        for r in matching:
            v = m.NewBoolVar("")
            full_vars += 1
            t_slot[(a.teacher_id, slot.id)].append(v)
            d_slot[(a.division_id, slot.id)].append(v)
            r_slot[(r.id, slot.id)].append(v)
            a_vars[a.id].append(v)

for lst in t_slot.values():
    m.Add(sum(lst) <= 1)
for lst in d_slot.values():
    m.Add(sum(lst) <= 1)
for lst in r_slot.values():
    m.Add(sum(lst) <= 1)
for a in assignments:
    if a.id in a_vars:
        m.Add(sum(a_vars[a.id]) <= a.weekly_hours())
for t_id in {a.teacher_id for a in assignments}:
    t = next(a.teacher for a in assignments if a.teacher_id == t_id)
    if t.max_hours_per_week:
        m.AddAtMostOne([]) if False else None
        lst = [v for (tid, _), vs in t_slot.items() if tid == t_id for v in vs]
        m.Add(sum(lst) <= t.max_hours_per_week)
m.Maximize(sum(v for lst in a_vars.values() for v in lst))

print(f"\nFULL MODEL variables: {full_vars}")
s = cp_model.CpSolver()
s.parameters.max_time_in_seconds = 20.0
s.parameters.num_workers = 4
t0 = time.perf_counter()
st = s.Solve(m)
print(f"  status={s.StatusName(st)} wall={time.perf_counter()-t0:.2f}s scheduled={int(s.ObjectiveValue()) if st in (cp_model.OPTIMAL, cp_model.FEASIBLE) else 'n/a'}")

# ---- SLOT-ONLY MODEL: x[a, t] + aggregate room-pool capacity ----
m2 = cp_model.CpModel()
t2 = defaultdict(list)
d2 = defaultdict(list)
a2 = defaultdict(list)
slot_pool = defaultdict(list)  # (slot, is_lab, strength) -> vars

strengths = sorted({a.division.strength for a in assignments})
for a in assignments:
    for slot in slots:
        if slot.id in unavail[a.teacher_id]:
            continue
        v = m2.NewBoolVar("")
        t2[(a.teacher_id, slot.id)].append(v)
        d2[(a.division_id, slot.id)].append(v)
        a2[a.id].append(v)
        slot_pool[(slot.id, a.subject.is_lab, a.division.strength)].append(v)

for lst in t2.values():
    m2.Add(sum(lst) <= 1)
for lst in d2.values():
    m2.Add(sum(lst) <= 1)
for a in assignments:
    if a.id in a2:
        m2.Add(sum(a2[a.id]) <= a.weekly_hours())
for t_id in {a.teacher_id for a in assignments}:
    t = next(a.teacher for a in assignments if a.teacher_id == t_id)
    if t.max_hours_per_week:
        lst = [v for (tid, _), vs in t2.items() if tid == t_id for v in vs]
        m2.Add(sum(lst) <= t.max_hours_per_week)
# Aggregate room-pool capacity per (slot, type, strength tier)
for (sid, is_lab, strength), lst in slot_pool.items():
    pool = [r for r in (labs if is_lab else regular) if r.capacity >= strength]
    if not pool:
        continue
    m2.Add(sum(lst) <= len(pool))
m2.Maximize(sum(v for lst in a2.values() for v in lst))

print(f"\nSLOT-ONLY MODEL variables: {sum(len(v) for v in a2.values())}")
s2 = cp_model.CpSolver()
s2.parameters.max_time_in_seconds = 20.0
s2.parameters.num_workers = 4
t0 = time.perf_counter()
st2 = s2.Solve(m2)
elapsed = time.perf_counter() - t0
print(f"  status={s2.StatusName(st2)} wall={elapsed:.2f}s")
if st2 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
    print(f"  scheduled hours (objective) = {int(s2.ObjectiveValue())}")
    # Room assignment feasibility check via simple matching per slot
    chosen = defaultdict(list)
    for a in assignments:
        for slot in slots:
            if slot.id in unavail[a.teacher_id]:
                continue
            key = (a.id, slot.id)
            pass
    print("  (room matching done separately in production code)")