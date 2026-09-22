from collections import defaultdict
import logging
import time

import psutil
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


class TimetableSolverError(Exception):
    """Custom exception raised when timetable scheduling fails or data is missing."""
    pass


def generate_timetable(semester_id: int) -> dict:
    """
    Generates a conflict-free timetable for the given semester using Google OR-Tools CP-SAT.
    
    Hard constraints:
      1. Teacher cannot be in two places at the same time slot.
      2. Division cannot have two subjects in the same time slot.
      3. Room cannot hold two divisions in the same time slot.
      4. Lab subjects only in lab rooms; theory subjects only in non-lab rooms.
      5. Teacher is never scheduled during their TeacherUnavailability time slots.
      6. Scheduling only happens within existing TimeSlots.
      7. For each assignment, scheduled_hours <= weekly_hours().
      
    Soft objective:
      Maximize total scheduled hours across all assignments.
      
    After solving:
      - Clears previous TimetableEntry and SchedulingIssue rows for this semester.
      - Saves new TimetableEntry rows.
      - Creates SchedulingIssue rows for any assignment where scheduled_hours < weekly_hours().
    """
    try:
        semester = Semester.objects.get(pk=semester_id)
    except Semester.DoesNotExist:
        raise TimetableSolverError(f"Semester with id {semester_id} does not exist.")

    assignments = list(
        Assignment.objects.filter(semester=semester)
        .select_related("teacher", "subject", "division")
    )

    if not assignments:
        return {
            "success": True,
            "total_hours_scheduled": 0,
            "total_hours_requested": 0,
            "issues_count": 0,
            "message": "No assignments found for this semester."
        }

    rooms = list(Room.objects.all())
    time_slots = list(TimeSlot.objects.all().order_by("day", "period_number"))

    # Pre-checks for missing resources
    if not time_slots:
        raise TimetableSolverError("No time slots available in database. Generate time slots first.")

    regular_rooms = [r for r in rooms if not r.is_lab]
    lab_rooms = [r for r in rooms if r.is_lab]

    for a in assignments:
        if a.subject.is_lab and not lab_rooms:
            raise TimetableSolverError(
                f"No lab room exists for '{a.subject.name}' -- add at least one Room with is_lab=True."
            )
        if not a.subject.is_lab and not regular_rooms:
            raise TimetableSolverError(
                f"No regular room exists for '{a.subject.name}' -- add at least one Room with is_lab=False."
            )

    # Teacher unavailabilities map: teacher_id -> set of time_slot_ids
    unavailability_records = TeacherUnavailability.objects.all().values_list("teacher_id", "time_slot_id")
    teacher_unavail = defaultdict(set)
    for t_id, slot_id in unavailability_records:
        teacher_unavail[t_id].add(slot_id)

    # -------------------------------------------------------------------------
    # CP-SAT Model Formulation
    # -------------------------------------------------------------------------
    model = cp_model.CpModel()

    # Decision variables: x[a_id, r_id, slot_id] in {0, 1}
    # Only create variable if:
    # 1. room type matches subject type (lab vs theory)
    # 2. teacher is available in that slot
    x = {}

    # Index structures for fast constraint posting
    teacher_slot_vars = defaultdict(list)    # (teacher_id, slot_id) -> list of vars
    division_slot_vars = defaultdict(list)   # (division_id, slot_id) -> list of vars
    room_slot_vars = defaultdict(list)       # (room_id, slot_id) -> list of vars
    assignment_vars = defaultdict(list)      # a_id -> list of vars
    teacher_all_vars = defaultdict(list)     # teacher_id -> list of vars

    for a in assignments:
        matching_rooms = [
            room
            for room in (lab_rooms if a.subject.is_lab else regular_rooms)
            if room.capacity >= a.division.strength
        ]
        t_id = a.teacher.id
        div_id = a.division.id

        for slot in time_slots:
            slot_id = slot.id
            if slot_id in teacher_unavail[t_id]:
                continue  # Constraint 5: Teacher unavailable

            for r in matching_rooms:
                r_id = r.id
                var_name = f"x_{a.id}_{r_id}_{slot_id}"
                var = model.NewBoolVar(var_name)

                x[a.id, r_id, slot_id] = var
                assignment_vars[a.id].append(var)
                teacher_slot_vars[t_id, slot_id].append(var)
                division_slot_vars[div_id, slot_id].append(var)
                room_slot_vars[r_id, slot_id].append(var)
                teacher_all_vars[t_id].append(var)

    # Constraint 1: Teacher cannot be in two places at the same time slot
    for (t_id, slot_id), var_list in teacher_slot_vars.items():
        model.Add(sum(var_list) <= 1)

    # Constraint 2: Division cannot have two subjects at the same time slot
    for (div_id, slot_id), var_list in division_slot_vars.items():
        model.Add(sum(var_list) <= 1)

    # Constraint 3: Room cannot hold two divisions at the same time slot
    for (r_id, slot_id), var_list in room_slot_vars.items():
        model.Add(sum(var_list) <= 1)

    # Constraint 7: Scheduled hours <= weekly_hours() for each assignment
    total_requested_hours = 0
    for a in assignments:
        req_hours = a.weekly_hours()
        total_requested_hours += req_hours
        if a.id in assignment_vars:
            model.Add(sum(assignment_vars[a.id]) <= req_hours)

    # Additional constraint: Teacher max hours per week
    for t_id, var_list in teacher_all_vars.items():
        teacher_obj = next((a.teacher for a in assignments if a.teacher.id == t_id), None)
        if teacher_obj and teacher_obj.max_hours_per_week:
            model.Add(sum(var_list) <= teacher_obj.max_hours_per_week)

    # Soft Objective: Maximize total scheduled hours across all assignments
    all_vars = list(x.values())
    if all_vars:
        model.Maximize(sum(all_vars))

    # Solve
    solver = cp_model.CpSolver()
    # Parameters for solver responsiveness
    solver.parameters.max_time_in_seconds = 30.0
    solver.parameters.num_workers = 4

    process = psutil.Process()
    cpu_percent_before = psutil.cpu_percent(interval=None)
    ram_used_before_mb = process.memory_info().rss / (1024 * 1024)
    variable_count = len(model.Proto().variables)
    constraint_count = len(model.Proto().constraints)
    solve_started = time.perf_counter()
    status = solver.Solve(model)
    wall_time_seconds = time.perf_counter() - solve_started
    cpu_percent_after = psutil.cpu_percent(interval=None)
    ram_used_after_mb = process.memory_info().rss / (1024 * 1024)
    solver_status = solver.StatusName(status)

    resource_metrics = {
        "cpu_percent_before": round(cpu_percent_before, 2),
        "cpu_percent_after": round(cpu_percent_after, 2),
        "ram_used_before_mb": round(ram_used_before_mb, 2),
        "ram_used_after_mb": round(ram_used_after_mb, 2),
        "wall_time_seconds": round(wall_time_seconds, 4),
        "variable_count": variable_count,
        "constraint_count": constraint_count,
        "solver_status": solver_status,
    }
    logger.info("Solver resource metrics: %s", resource_metrics)
    SolverRun.objects.create(semester=semester, **resource_metrics)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise TimetableSolverError(
            "Solver could not find a feasible schedule. "
            "Please check if room constraints or working hours are severely under-provisioned."
        )

    # -------------------------------------------------------------------------
    # Persist Results
    # -------------------------------------------------------------------------
    scheduled_entries = []
    assignment_scheduled_count = defaultdict(int)

    # Pre-fetch lookup dictionaries
    room_dict = {r.id: r for r in rooms}
    slot_dict = {s.id: s for s in time_slots}
    assignment_dict = {a.id: a for a in assignments}

    for (a_id, r_id, slot_id), var in x.items():
        if solver.Value(var) == 1:
            scheduled_entries.append(
                TimetableEntry(
                    semester=semester,
                    assignment=assignment_dict[a_id],
                    room=room_dict[r_id],
                    time_slot=slot_dict[slot_id],
                )
            )
            assignment_scheduled_count[a_id] += 1

    total_scheduled_hours = len(scheduled_entries)
    scheduling_issues = []

    # Calculate division-level and teacher-level totals for intelligent diagnostics
    div_requested_totals = defaultdict(int)
    teacher_requested_totals = defaultdict(int)
    for a in assignments:
        div_requested_totals[a.division.id] += a.weekly_hours()
        teacher_requested_totals[a.teacher.id] += a.weekly_hours()

    total_slots_count = len(time_slots)

    for a in assignments:
        requested = a.weekly_hours()
        scheduled = assignment_scheduled_count[a.id]

        if scheduled < requested:
            # Construct meaningful, specific reason and actionable suggestion
            reasons = []
            suggestions = []

            t = a.teacher
            div = a.division
            unavail_count = len(teacher_unavail[t.id])

            matching_rooms = [
                room
                for room in (lab_rooms if a.subject.is_lab else regular_rooms)
                if room.capacity >= div.strength
            ]

            if not matching_rooms:
                reasons.append(
                    f"No room with sufficient capacity is available for {div.name}; "
                    f"the division requires at least {div.strength} seats."
                )
                suggestions.append(
                    f"Add or configure a {'lab' if a.subject.is_lab else 'regular'} room "
                    f"with capacity of at least {div.strength} seats."
                )
            elif teacher_requested_totals[t.id] > t.max_hours_per_week:
                reasons.append(
                    f"{t.name}'s total assigned workload ({teacher_requested_totals[t.id]}h/wk) "
                    f"exceeds maximum allowed limit of {t.max_hours_per_week}h/wk."
                )
                suggestions.append(
                    f"Increase {t.name}'s max hours per week or reassign {a.division.name} "
                    f"to another faculty member."
                )
            elif unavail_count > 0:
                reasons.append(
                    f"{t.name} has {unavail_count} unavailable slot(s) limiting conflict-free placement."
                )
                suggestions.append(
                    f"Consider relaxing unavailability restrictions for {t.name} to unlock available periods."
                )

            if div_requested_totals[div.id] > total_slots_count:
                reasons.append(
                    f"{div.name} has {div_requested_totals[div.id]} total requested hours, "
                    f"exceeding the total {total_slots_count} available time slots in the week."
                )
                suggestions.append(
                    f"Reduce curriculum hours for {div.name} or configure additional daily periods/working days."
                )

            if not reasons:
                reasons.append(
                    f"{t.name}'s other assigned divisions occupy all available conflict-free slots this week."
                )
                suggestions.append(
                    f"Consider adding more time slots or reducing weekly hours for {a.subject.name} "
                    f"in {div.name} by {requested - scheduled} hour(s)/week."
                )

            scheduling_issues.append(
                SchedulingIssue(
                    semester=semester,
                    assignment=a,
                    hours_requested=requested,
                    hours_scheduled=scheduled,
                    reason=" ".join(reasons),
                    suggestion=" ".join(suggestions),
                )
            )

    with transaction.atomic():
        # Clear existing entries and issues for this semester
        TimetableEntry.objects.filter(semester=semester).delete()
        SchedulingIssue.objects.filter(semester=semester).delete()

        # Bulk create new entries and issues
        TimetableEntry.objects.bulk_create(scheduled_entries)
        SchedulingIssue.objects.bulk_create(scheduling_issues)

    return {
        "success": True,
        "total_hours_scheduled": total_scheduled_hours,
        "total_hours_requested": total_requested_hours,
        "issues_count": len(scheduling_issues),
        "message": (
            f"Successfully generated timetable with {total_scheduled_hours}/{total_requested_hours} "
            f"hours scheduled ({len(scheduling_issues)} issues reported)."
        )
    }
