"""
Conflict-free timetable generation for colleges of any size.

The generator runs in two phases.

Phase 1 -- CP-SAT decides WHEN each class meets.
    Decision variable x[a, t] in {0,1}: assignment ``a`` runs in period ``t``.
    Hard constraints: teacher concurrency, division concurrency, teacher
    unavailability, per-assignment weekly-hour cap, per-teacher weekly cap, and
    an aggregate room-pool capacity per (period, room type, strength tier).
    Objective: maximise the total number of scheduled hours.

Phase 2 -- bipartite matching decides WHERE each class meets.
    Within a single period, every chosen assignment is matched to a distinct
    compatible room using augmenting-path bipartite matching.

Why the split matters
---------------------
Modelling the room as a decision variable too (x[a, r, t]) is the textbook
formulation but it is intractable at college scale: a 20-division college with
180 classes produced 253,800 Boolean variables and CP-SAT could not find *any*
feasible schedule within the time limit, whereas the two-phase model needs only
8,460 variables and proves optimality in seconds. Room choice is a pure
assignment problem once periods are fixed, so it belongs in a matching step
rather than inside the SAT model.

Room selection can still fail for one specific period, because strength tiers
share the same pool of rooms, so the aggregate capacity bound is necessary but
not always sufficient. When that happens the offending periods are banned and
phase 1 is re-solved, so the stored result is always conflict-free and any
shortfall is reported honestly as a SchedulingIssue.
"""

from collections import defaultdict
import logging
import time

import psutil
from django.conf import settings
from django.db import transaction
from ortools.sat.python import cp_model

from .models import (
    Semester,
    Assignment,
    Room,
    TimeSlot,
    TeacherUnavailability,
    TimetableEntry,
    SchedulingIssue,
    SolverRun,
)

logger = logging.getLogger(__name__)

DEFAULT_TIME_LIMIT_SECONDS = 60.0
DEFAULT_WORKERS = 4
MAX_ROOM_MATCH_ROUNDS = 4


class TimetableSolverError(Exception):
    """Raised when timetable scheduling fails or required data is missing."""
    pass


def _time_limit():
    return float(getattr(settings, "TIMETABLE_SOLVER_TIME_LIMIT", DEFAULT_TIME_LIMIT_SECONDS))


def _worker_count():
    return int(getattr(settings, "TIMETABLE_SOLVER_WORKERS", DEFAULT_WORKERS))


def _compatible_rooms(assignment, rooms):
    """Rooms that satisfy both the room-type rule and the capacity rule."""
    return [
        room
        for room in rooms
        if room.is_lab == assignment.subject.is_lab
        and room.capacity >= assignment.division.strength
    ]


def _match_rooms_in_period(assignment_ids, assignments_by_id, rooms):
    """
    Assign each assignment in one period a distinct compatible room.

    Returns ``(matched, unmatched_ids)`` where ``matched`` is a list of
    ``(assignment_id, room_id)`` pairs. Augmenting paths give a maximum
    bipartite matching, which is instant at single-period sizes.
    """
    adjacency = {
        a_id: [room.id for room in _compatible_rooms(assignments_by_id[a_id], rooms)]
        for a_id in assignment_ids
    }

    room_to_assignment = {}

    def try_assign(assignment_id, visited_rooms):
        for room_id in adjacency.get(assignment_id, ()):
            if room_id in visited_rooms:
                continue
            visited_rooms.add(room_id)
            holder = room_to_assignment.get(room_id)
            if holder is None or try_assign(holder, visited_rooms):
                room_to_assignment[room_id] = assignment_id
                return True
        return False

    for assignment_id in assignment_ids:
        try_assign(assignment_id, set())

    # room_to_assignment maps room_id -> assignment_id
    pairs = list(room_to_assignment.items())
    placed_assignment_ids = {assignment_id for _room_id, assignment_id in pairs}
    unmatched = [a_id for a_id in assignment_ids if a_id not in placed_assignment_ids]
    return [(assignment_id, room_id) for room_id, assignment_id in pairs], unmatched


def _room_pool_sizes(assignments, rooms):
    """Number of usable rooms per (subject is_lab, division strength) tier."""
    sizes = {}
    for a in assignments:
        key = (a.subject.is_lab, a.division.strength)
        if key not in sizes:
            sizes[key] = len(_compatible_rooms(a, rooms))
    return sizes


def _build_and_solve(assignments, time_slots, teacher_unavail, time_limit, banned_pairs, pool_sizes):
    """
    Phase 1. Returns ``(status_name, chosen_pairs, stats)`` where chosen_pairs is
    a list of ``(assignment_id, time_slot_id)`` with a value of 1.
    """
    model = cp_model.CpModel()

    var_index = {}
    teacher_slot = defaultdict(list)
    division_slot = defaultdict(list)
    assignment_vars = defaultdict(list)
    pool_vars = defaultdict(list)

    for a in assignments:
        banned_slots = teacher_unavail[a.teacher_id]
        for slot in time_slots:
            if slot.id in banned_slots:
                continue  # Constraint: teacher unavailability is inviolable
            var = model.NewBoolVar(f"x_{a.id}_{slot.id}")
            var_index[(a.id, slot.id)] = var
            assignment_vars[a.id].append(var)
            teacher_slot[(a.teacher_id, slot.id)].append(var)
            division_slot[(a.division_id, slot.id)].append(var)
            pool_vars[(slot.id, a.subject.is_lab, a.division.strength)].append(var)

    # Retry support: forbid periods that previously could not be given a room.
    for assignment_id, slot_id in banned_pairs:
        var = var_index.get((assignment_id, slot_id))
        if var is not None:
            model.Add(var == 0)

    # A teacher cannot be in two places in the same period.
    for var_list in teacher_slot.values():
        model.Add(sum(var_list) <= 1)

    # A division cannot attend two subjects in the same period.
    for var_list in division_slot.values():
        model.Add(sum(var_list) <= 1)

    # Never place more classes in a period than there are usable rooms.
    for (slot_id, is_lab, strength), var_list in pool_vars.items():
        pool = pool_sizes.get((is_lab, strength), 0)
        if pool <= 0:
            continue
        model.Add(sum(var_list) <= pool)

    # Never schedule more hours than the curriculum requests.
    for a in assignments:
        if assignment_vars.get(a.id):
            model.Add(sum(assignment_vars[a.id]) <= a.weekly_hours())

    # Never exceed a teacher's contractual weekly maximum.
    teacher_of = {a.teacher_id: a.teacher for a in assignments}
    teacher_all = defaultdict(list)
    for (teacher_id, _slot_id), var_list in teacher_slot.items():
        teacher_all[teacher_id].extend(var_list)
    for teacher_id, var_list in teacher_all.items():
        teacher = teacher_of.get(teacher_id)
        if teacher is not None and teacher.max_hours_per_week:
            model.Add(sum(var_list) <= teacher.max_hours_per_week)

    if assignment_vars:
        model.Maximize(sum(var for var_list in assignment_vars.values() for var in var_list))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_workers = _worker_count()

    variable_count = len(model.Proto().variables)
    constraint_count = len(model.Proto().constraints)

    started = time.perf_counter()
    status = solver.Solve(model)
    elapsed = time.perf_counter() - started

    status_name = solver.StatusName(status)
    chosen = []
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        chosen = [
            (assignment_id, slot_id)
            for (assignment_id, slot_id), var in var_index.items()
            if solver.Value(var) == 1
        ]

    return status_name, chosen, {
        "variable_count": variable_count,
        "constraint_count": constraint_count,
        "wall_time_seconds": elapsed,
    }


def generate_timetable(semester_id: int) -> dict:
    """
    Generate a conflict-free timetable for the given semester.

    Returns a dict with ``success``, ``total_hours_scheduled``,
    ``total_hours_requested``, ``issues_count`` and a human readable ``message``.
    """
    try:
        semester = Semester.objects.get(pk=semester_id)
    except Semester.DoesNotExist:
        raise TimetableSolverError(f"Semester with id {semester_id} does not exist.")

    assignments = list(
        Assignment.objects.filter(semester=semester)
        .select_related("teacher", "subject", "division")
        .order_by("pk")
    )

    if not assignments:
        return {
            "success": True,
            "total_hours_scheduled": 0,
            "total_hours_requested": 0,
            "issues_count": 0,
            "message": "No assignments found for this semester.",
        }

    time_slots = list(TimeSlot.objects.all().order_by("day", "period_number"))
    if not time_slots:
        raise TimetableSolverError("No time slots available in database. Generate time slots first.")

    rooms = list(Room.objects.all())
    lab_rooms = [r for r in rooms if r.is_lab]
    regular_rooms = [r for r in rooms if not r.is_lab]

    # Pre-checks: fail loudly when a required room type does not exist at all.
    for a in assignments:
        if a.subject.is_lab and not lab_rooms:
            raise TimetableSolverError(
                f"No lab room exists for '{a.subject.name}' -- add at least one Room with is_lab=True."
            )
        if not a.subject.is_lab and not regular_rooms:
            raise TimetableSolverError(
                f"No regular room exists for '{a.subject.name}' -- add at least one Room with is_lab=False."
            )

    teacher_unavail = defaultdict(set)
    for teacher_id, slot_id in TeacherUnavailability.objects.all().values_list(
        "teacher_id", "time_slot_id"
    ):
        teacher_unavail[teacher_id].add(slot_id)

    assignment_by_id = {a.id: a for a in assignments}
    slot_by_id = {s.id: s for s in time_slots}
    room_by_id = {r.id: r for r in rooms}
    total_requested_hours = sum(a.weekly_hours() for a in assignments)
    pool_sizes = _room_pool_sizes(assignments, rooms)

    process = psutil.Process()
    cpu_percent_before = psutil.cpu_percent(interval=None)
    ram_used_before_mb = process.memory_info().rss / (1024 * 1024)

    time_limit = _time_limit()
    banned_pairs = set()
    scheduled = []
    total_solve_seconds = 0.0
    stats = {"variable_count": 0, "constraint_count": 0}
    status_name = "UNKNOWN"

    for round_index in range(MAX_ROOM_MATCH_ROUNDS):
        status_name, chosen, stats = _build_and_solve(
            assignments, time_slots, teacher_unavail, time_limit, banned_pairs, pool_sizes
        )
        total_solve_seconds += stats["wall_time_seconds"]

        if status_name not in ("OPTIMAL", "FEASIBLE"):
            break

        chosen_by_slot = defaultdict(list)
        for assignment_id, slot_id in chosen:
            chosen_by_slot[slot_id].append(assignment_id)

        scheduled = []
        unmatched = []
        for slot_id, assignment_ids in chosen_by_slot.items():
            matched, missed = _match_rooms_in_period(assignment_ids, assignment_by_id, rooms)
            scheduled.extend((a_id, slot_id, room_id) for a_id, room_id in matched)
            unmatched.extend((a_id, slot_id) for a_id in missed)

        if not unmatched:
            break

        if round_index < MAX_ROOM_MATCH_ROUNDS - 1:
            banned_pairs.update(unmatched)
            logger.info(
                "Room matching round %d: %d class(es) had no room; retrying with %d banned periods",
                round_index + 1, len(unmatched), len(banned_pairs),
            )

    ram_used_after_mb = process.memory_info().rss / (1024 * 1024)
    cpu_percent_after = psutil.cpu_percent(interval=None)

    def _record_run():
        SolverRun.objects.create(
            semester=semester,
            cpu_percent_before=round(cpu_percent_before, 2),
            cpu_percent_after=round(cpu_percent_after, 2),
            ram_used_before_mb=round(ram_used_before_mb, 2),
            ram_used_after_mb=round(ram_used_after_mb, 2),
            wall_time_seconds=round(total_solve_seconds, 4),
            variable_count=stats.get("variable_count", 0),
            constraint_count=stats.get("constraint_count", 0),
            solver_status=status_name,
        )

    if status_name not in ("OPTIMAL", "FEASIBLE"):
        _record_run()
        raise TimetableSolverError(
            f"Solver could not find a feasible schedule within {time_limit:.0f}s. "
            "Please check if room constraints or working hours are severely under-provisioned."
        )

    assignment_scheduled_count = defaultdict(int)
    for assignment_id, _slot_id, _room_id in scheduled:
        assignment_scheduled_count[assignment_id] += 1

    total_scheduled_hours = len(scheduled)

    # ------------------------------------------------------------------
    # Honest diagnostics for anything that could not be scheduled
    # ------------------------------------------------------------------
    div_requested_totals = defaultdict(int)
    teacher_requested_totals = defaultdict(int)
    for a in assignments:
        div_requested_totals[a.division_id] += a.weekly_hours()
        teacher_requested_totals[a.teacher_id] += a.weekly_hours()

    total_slots_count = len(time_slots)
    scheduling_issues = []

    for a in assignments:
        requested = a.weekly_hours()
        scheduled_hours = assignment_scheduled_count[a.id]
        if scheduled_hours >= requested:
            continue

        reasons = []
        suggestions = []
        teacher = a.teacher
        division = a.division
        unavail_count = len(teacher_unavail[teacher.id])
        matching_rooms = _compatible_rooms(a, rooms)

        if not matching_rooms:
            reasons.append(
                f"No room with sufficient capacity is available for {division.name}; "
                f"the division requires at least {division.strength} seats."
            )
            suggestions.append(
                f"Add or configure a {'lab' if a.subject.is_lab else 'regular'} room "
                f"with capacity of at least {division.strength} seats."
            )
        elif teacher_requested_totals[teacher.id] > teacher.max_hours_per_week:
            reasons.append(
                f"{teacher.name}'s total assigned workload "
                f"({teacher_requested_totals[teacher.id]}h/wk) exceeds the maximum allowed "
                f"limit of {teacher.max_hours_per_week}h/wk."
            )
            suggestions.append(
                f"Increase {teacher.name}'s max hours per week or reassign "
                f"{division.name} to another faculty member."
            )
        elif unavail_count > 0:
            reasons.append(
                f"{teacher.name} has {unavail_count} unavailable slot(s) limiting "
                "conflict-free placement."
            )
            suggestions.append(
                f"Consider relaxing unavailability restrictions for {teacher.name} to "
                "unlock available periods."
            )

        if div_requested_totals[division.id] > total_slots_count:
            reasons.append(
                f"{division.name} has {div_requested_totals[division.id]} total requested "
                f"hours, exceeding the total {total_slots_count} available time slots in the week."
            )
            suggestions.append(
                f"Reduce curriculum hours for {division.name} or configure additional "
                "daily periods/working days."
            )

        if not reasons:
            reasons.append(
                f"{teacher.name}'s other assigned divisions occupy all available "
                "conflict-free slots this week."
            )
            suggestions.append(
                f"Consider adding more time slots or reducing weekly hours for "
                f"{a.subject.name} in {division.name} by "
                f"{requested - scheduled_hours} hour(s)/week."
            )

        scheduling_issues.append(
            SchedulingIssue(
                semester=semester,
                assignment=a,
                hours_requested=requested,
                hours_scheduled=scheduled_hours,
                reason=" ".join(reasons),
                suggestion=" ".join(suggestions),
            )
        )

    entries = [
        TimetableEntry(
            semester=semester,
            assignment=assignment_by_id[assignment_id],
            room=room_by_id[room_id],
            time_slot=slot_by_id[slot_id],
        )
        for assignment_id, slot_id, room_id in scheduled
    ]

    with transaction.atomic():
        TimetableEntry.objects.filter(semester=semester).delete()
        SchedulingIssue.objects.filter(semester=semester).delete()
        TimetableEntry.objects.bulk_create(entries)
        SchedulingIssue.objects.bulk_create(scheduling_issues)

    _record_run()

    return {
        "success": True,
        "total_hours_scheduled": total_scheduled_hours,
        "total_hours_requested": total_requested_hours,
        "issues_count": len(scheduling_issues),
        "message": (
            f"Successfully generated timetable with {total_scheduled_hours}/{total_requested_hours} "
            f"hours scheduled ({len(scheduling_issues)} issues reported)."
        ),
    }