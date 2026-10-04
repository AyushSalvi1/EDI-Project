"""
Capacity planning analysis.

The usual reason a large college cannot be scheduled is not the solver -- it is
physical infrastructure. Thirty divisions cannot all meet simultaneously in a
college with twenty classrooms. This module answers, before the solver is ever
run: "is this college physically schedulable, and if not, what is missing?"
"""

from collections import defaultdict

from .models import Assignment, Room, TimeSlot, YearDivision


def analyse_capacity(semester):
    """
    Analyse whether the semester is physically schedulable.

    Returns a dict describing peak concurrency demand versus supply, with a
    list of human-readable findings and an overall verdict.
    """
    assignments = list(
        Assignment.objects.filter(semester=semester)
        .select_related("teacher", "subject", "division")
        .order_by("pk")
    )
    time_slots = list(TimeSlot.objects.all().order_by("day", "period_number"))
    rooms = list(Room.objects.all())

    divisions = YearDivision.objects.filter(assignments__semester=semester).distinct()
    division_count = divisions.count()

    regular_rooms = [r for r in rooms if not r.is_lab]
    lab_rooms = [r for r in rooms if r.is_lab]

    # --- Weekly room demand versus weekly room supply ----------------------
    # Classes do not all have to meet at once, so the binding test is the
    # aggregate one: the total weekly hours of a subject type must fit in the
    # number of usable rooms of that type multiplied by the number of periods.
    theory_hours = sum(a.weekly_hours() for a in assignments if not a.subject.is_lab)
    lab_hours = sum(a.weekly_hours() for a in assignments if a.subject.is_lab)

    slot_count = len(time_slots)
    supply = {
        "theory": len(regular_rooms),
        "lab": len(lab_rooms),
    }
    demand = {
        "theory": theory_hours,
        "lab": lab_hours,
    }
    weekly_supply = {
        kind: supply[kind] * slot_count for kind in ("theory", "lab")
    }

    findings = []
    blocking = False

    for kind in ("theory", "lab"):
        if demand[kind] == 0:
            continue
        label = "laboratory" if kind == "lab" else "classroom"
        shortfall = demand[kind] - weekly_supply[kind]
        rooms_needed = -(-demand[kind] // slot_count) if slot_count else 0
        if shortfall > 0:
            blocking = True
            extra = rooms_needed - supply[kind]
            findings.append(
                {
                    "severity": "blocking",
                    "message": (
                        f"{demand[kind]} {label} periods are needed each week but the college "
                        f"only has capacity for {weekly_supply[kind]} "
                        f"({supply[kind]} {label}{'s' if supply[kind] != 1 else ''} x "
                        f"{slot_count} periods). Add at least {extra} more {label}"
                        f"{'s' if extra != 1 else ''}, reduce curriculum hours, or add periods."
                    ),
                }
            )
        elif rooms_needed == supply[kind]:
            findings.append(
                {
                    "severity": "warning",
                    "message": (
                        f"{label.title()} capacity is exactly sufficient "
                        f"({supply[kind]} needed, {supply[kind]} available). Every usable "
                        "room-period will be consumed, so any closure makes this "
                        "semester unschedulable."
                    ),
                }
            )

    # Divisions exceeding the room count must be staggered across periods.
    if division_count > supply["theory"] and supply["theory"] > 0:
        findings.append(
            {
                "severity": "info",
                "message": (
                    f"{division_count} divisions share {supply['theory']} classrooms, so "
                    "classes are staggered across the week. This is valid but leaves no "
                    "spare classroom for an ad-hoc class or a make-up session."
                ),
            }
        )

    # --- Capacity / strength coverage ------------------------------------
    strengths = sorted({a.division.strength for a in assignments})
    strength_rows = []
    for strength in strengths:
        theory_ok = [r for r in regular_rooms if r.capacity >= strength]
        lab_ok = [r for r in lab_rooms if r.capacity >= strength]
        strength_rows.append(
            {
                "strength": strength,
                "theory_rooms": len(theory_ok),
                "lab_rooms": len(lab_ok),
            }
        )
        if not theory_ok:
            findings.append(
                {
                    "severity": "blocking",
                    "message": (
                        f"No classroom seats at least {strength} students, but some "
                        f"divisions have that strength. Add a larger classroom."
                    ),
                }
            )
            blocking = True
        if any(a.subject.is_lab for a in assignments if a.division.strength == strength) and not lab_ok:
            findings.append(
                {
                    "severity": "blocking",
                    "message": (
                        f"No laboratory seats at least {strength} students, but lab "
                        f"classes are required for divisions of that strength."
                    ),
                }
            )
            blocking = True

    # --- Room utilisation -------------------------------------------------
    weekly_room_slots = slot_count * supply["theory"] + slot_count * supply["lab"]

    total_requested = sum(a.weekly_hours() for a in assignments)
    utilisation = (total_requested / weekly_room_slots * 100) if weekly_room_slots else 0.0

    # --- Teacher capacity -------------------------------------------------
    teacher_load = defaultdict(int)
    for a in assignments:
        teacher_load[a.teacher_id] += a.weekly_hours()
    overcommitted = [
        (a_id, hours) for a_id, hours in teacher_load.items()
        if hours > (a.teacher.max_hours_per_week or 0)
    ]
    for assignment_id, hours in overcommitted:
        teacher = next(a.teacher for a in assignments if a.teacher_id == assignment_id)
        findings.append(
            {
                "severity": "warning",
                "message": (
                    f"{teacher.name} is allocated {hours}h/week but their contract "
                    f"limit is {teacher.max_hours_per_week}h/week."
                ),
            }
        )

    # --- Division weekly load --------------------------------------------
    division_load = defaultdict(int)
    for a in assignments:
        division_load[a.division_id] += a.weekly_hours()
    for division_id, hours in division_load.items():
        if hours > slot_count:
            division = next(a.division for a in assignments if a.division_id == division_id)
            findings.append(
                {
                    "severity": "blocking",
                    "message": (
                        f"{division.name} is allocated {hours}h/week but the week only "
                        f"has {slot_count} periods. Reduce hours or add periods."
                    ),
                }
            )
            blocking = True

    return {
        "semester": semester,
        "division_count": division_count,
        "assignment_count": len(assignments),
        "time_slot_count": slot_count,
        "demand": demand,
        "supply": supply,
        "weekly_supply": weekly_supply,
        "strength_rows": strength_rows,
        "total_requested_hours": total_requested,
        "weekly_room_slots": weekly_room_slots,
        "utilisation_percent": round(utilisation, 1),
        "findings": findings,
        "blocking_findings": [f for f in findings if f["severity"] == "blocking"],
        "warning_findings": [f for f in findings if f["severity"] == "warning"],
        "info_findings": [f for f in findings if f["severity"] == "info"],
        "verdict": "unschedulable" if blocking else "schedulable",
    }