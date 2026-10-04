"""
Scheduling preferences: what the administrator wants, and whether it works.

The administrator picks working days, college timings, period length and lunch.
This module answers two questions before anything is written:

* **Is this week shape internally valid?** A lunch longer than the day, or a
  college day that yields zero periods, is rejected immediately.
* **Will it actually work?** Regenerating TimeSlot rows invalidates every
  scheduled entry, so the new week is checked against real demand first. If the
  new week cannot hold the current workload, the administrator is told exactly
  how many hours would be lost rather than being allowed to find out later.
"""

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import (
    Assignment,
    DivisionPreference,
    Room,
    SchedulingPreference,
    Semester,
    TimeSlot,
    generate_time_slots,
    plan_time_slots,
)


def describe_week(preference):
    """A human readable summary of the period grid a preference would produce."""
    days = preference.day_list()
    day_labels = dict(TimeSlot._meta.get_field("day").choices)
    # plan_time_slots is pure: describing a week must never write a TimeSlot row.
    slots = plan_time_slots(
        days, preference.day_start_time, preference.day_end_time,
        preference.period_duration_minutes,
        (preference.lunch_start_time, preference.lunch_end_time),
    )
    per_day = len(slots) // max(1, len(days)) if days else 0
    return {
        "days": days,
        "day_labels": [day_labels.get(d, d) for d in days],
        "periods_per_day": per_day,
        "slots_per_week": len(slots),
        "first_slot": slots[0] if slots else None,
        "last_slot": slots[-1] if slots else None,
    }


def analyse_preference(semester, preference):
    """
    Check a proposed week against the college's real demand.

    Returns a dict of findings. ``feasible`` is True when the week has at least
    as much teaching capacity as the curriculum currently demands.
    """
    summary = describe_week(preference)
    slots_per_week = summary["slots_per_week"]

    assignments = list(
        Assignment.objects.filter(semester=semester).select_related("division", "subject")
    )
    findings = []

    if not summary["days"] or not slots_per_week:
        return {
            "feasible": False,
            "summary": summary,
            "findings": [{
                "severity": "blocking",
                "message": "These settings produce no teaching periods at all.",
            }],
        }

    # --- Can every division's own load fit in the week? --------------------
    load_by_division = {}
    for assignment in assignments:
        load_by_division[assignment.division_id] = (
            load_by_division.get(assignment.division_id, 0) + assignment.weekly_hours()
        )

    blocked = []
    for division_id, load in load_by_division.items():
        if load > slots_per_week:
            from .models import YearDivision
            division = YearDivision.objects.filter(pk=division_id).first()
            blocked.append({
                "division": division.name if division else str(division_id),
                "requested": load,
                "available": slots_per_week,
                "short": load - slots_per_week,
            })
    for row in sorted(blocked, key=lambda r: -r["short"]):
        findings.append({
            "severity": "blocking",
            "message": (
                f"{row['division']} is allocated {row['requested']} hours a week "
                f"but this week offers only {row['available']} periods, so "
                f"{row['short']} hour(s) could not be scheduled."
            ),
        })

    # --- Can every division's own hours fit on its restricted days? ---------
    for pref in DivisionPreference.objects.filter(
        division_id__in=load_by_division
    ).select_related("division"):
        allowed = len(pref.allowed_slots())
        load = load_by_division.get(pref.division_id, 0)
        if load > allowed:
            findings.append({
                "severity": "warning",
                "message": (
                    f"{pref.division.name}'s own preference allows {allowed} "
                    f"period(s) but it needs {load} hours a week."
                ),
            })

    # --- Do the rooms still fit? -------------------------------------------
    rooms = list(Room.objects.all())
    if rooms:
        strengths = {a.division.strength for a in assignments}
        for strength in sorted(strengths):
            usable = [r for r in rooms if r.capacity >= strength]
            if not usable:
                findings.append({
                    "severity": "warning",
                    "message": (
                        f"No room seats the {strength}-student divisions. This is "
                        f"unrelated to the week shape, but it will keep lab hours "
                        f"unscheduled."
                    ),
                })
                break

    # --- Aggregate room capacity against peak concurrency ------------------
    total_weekly_hours = sum(a.weekly_hours() for a in assignments)
    room_capacity_per_week = slots_per_week * len(rooms)
    if room_capacity_per_week and total_weekly_hours > room_capacity_per_week:
        findings.append({
            "severity": "blocking",
            "message": (
                f"The curriculum needs {total_weekly_hours} room-periods a week "
                f"but the college only has {room_capacity_per_week} "
                f"({len(rooms)} rooms x {slots_per_week} periods)."
            ),
        })

    feasible = not any(f["severity"] == "blocking" for f in findings)
    if not findings:
        findings.append({
            "severity": "info",
            "message": (
                f"Every division fits, and {room_capacity_per_week} room-periods a "
                f"week comfortably covers the {total_weekly_hours} hours requested."
            ),
        })

    return {
        "feasible": feasible,
        "summary": summary,
        "findings": findings,
        "total_weekly_hours": total_weekly_hours,
    }


def get_or_create_preference(semester):
    """Return this semester's preference, seeded from the existing time slots."""
    preference = SchedulingPreference.objects.filter(semester=semester).first()
    if preference is not None:
        return preference

    slots = list(TimeSlot.objects.order_by("day", "period_number"))
    days = []
    for slot in slots:
        if slot.day not in days:
            days.append(slot.day)
    days = [d for d in days if d] or ["MON", "TUE", "WED", "THU", "FRI"]

    first = slots[0] if slots else None
    last = slots[-1] if slots else None

    return SchedulingPreference(
        semester=semester,
        working_days=",".join(days),
        day_start_time=first.start_time if first else None,
        day_end_time=last.end_time if last else None,
    )


def apply_preference(preference, user):
    """
    Rebuild the TimeSlot grid from a preference.

    This is destructive. TimeSlot rows are shared by every semester, so changing
    them can invalidate another semester's published timetable. The function
    therefore reports which other semesters were affected rather than quietly
    destroying them.
    """
    from .models import TimetableEntry, TimetableChangeLog
    from .notifications import notify_timetable_changed, snapshot_timetable
    from .solver import generate_timetable

    preference.validate()

    semester = preference.semester
    before_snapshot = snapshot_timetable(semester)

    new_days = preference.day_list()
    # Exactly the slots the new preference defines.
    wanted = {
        (slot.day, slot.period_number): slot
        for slot in generate_time_slots(
            new_days,
            preference.day_start_time,
            preference.day_end_time,
            preference.period_duration_minutes,
            (preference.lunch_start_time, preference.lunch_end_time),
        )
    }

    existing = {(slot.day, slot.period_number): slot for slot in TimeSlot.objects.all()}
    obsolete = [slot for key, slot in existing.items() if key not in wanted]
    obsolete_ids = [slot.pk for slot in obsolete]

    # Which other semesters have entries in slots that are about to disappear?
    collateral = set()
    if obsolete_ids:
        collateral = set(
            TimetableEntry.objects.filter(time_slot_id__in=obsolete_ids)
            .exclude(semester=semester)
            .values_list("semester_id", flat=True)
        )
        other_semesters = list(Semester.objects.filter(id__in=collateral))

    with transaction.atomic():
        TimetableEntry.objects.filter(semester=semester).delete()
        # Removing a slot cascades to its entries, so warn via the change log.
        TimeSlot.objects.filter(pk__in=obsolete_ids).delete()
        # Every slot in `wanted` came back from generate_time_slots already
        # persisted, so there is nothing left to insert here.

        preference.updated_by = user
        # The caller passes a *candidate* built from the submitted form, so it
        # carries no primary key even though this semester already has a stored
        # preference. Point it at the stored row so this updates that row
        # instead of inserting a second one and breaking the unique constraint.
        stored = SchedulingPreference.objects.filter(semester=semester).first()
        if stored is None:
            preference.save()
        else:
            preference.pk = stored.pk
            preference.save()

        result = generate_timetable(semester.id)

        TimetableChangeLog.objects.create(
            semester=semester,
            changed_by=user,
            action="REGEN",
            detail=(
                f"Applied timetable preferences: days {', '.join(new_days)}, "
                f"{preference.day_start_time:%H:%M}-{preference.day_end_time:%H:%M}, "
                f"{preference.period_duration_minutes} minute periods. "
                f"{result['total_hours_scheduled']}/"
                f"{result['total_hours_requested']} hours scheduled."
            ),
        )

    notify_timetable_changed(
        semester, before_snapshot,
        approved_title=f"timetable preference ({', '.join(new_days)})",
    )

    return {
        "result": result,
        "removed_slots": len(obsolete_ids),
        "collateral_semesters": other_semesters if obsolete_ids else [],
    }


def save_division_preference(division, data):
    """Persist a per-division override after validating it against real slots."""
    working_days = ",".join(data.get("working_days") or [])
    first_period = int(data.get("first_period") or 1)
    last_period = int(data.get("last_period") or 0)

    if first_period < 1:
        raise ValidationError("The first period must be 1 or greater.")
    if last_period and last_period < first_period:
        raise ValidationError(
            "The last period cannot be before the first period."
        )

    available = set(
        TimeSlot.objects.values_list("period_number", flat=True).distinct()
    )
    if first_period > max(available or {1}):
        raise ValidationError(
            f"Period {first_period} does not exist in the current week."
        )
    if last_period and last_period > max(available):
        raise ValidationError(
            f"Period {last_period} does not exist in the current week."
        )

    preference, _created = DivisionPreference.objects.update_or_create(
        division=division,
        defaults={
            "working_days": working_days,
            "first_period": first_period,
            "last_period": last_period,
            "note": (data.get("note") or "")[:200],
        },
    )
    return preference