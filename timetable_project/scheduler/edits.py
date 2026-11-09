"""
Manual timetable editing operations used by administrators.

Every manual change is re-validated against the same rules the CP-SAT solver
enforces (room type, room capacity, room occupancy, teacher concurrency, division
concurrency and teacher unavailability) before anything is written, so an admin
can never introduce a conflict that the solver would have prevented.
"""

from datetime import time
from django.db import transaction

from .models import (
    Room,
    TeacherUnavailability,
    TimetableChangeLog,
    TimetableEntry,
)


def _format_time(t):
    """Safely format time whether it is a datetime.time or str."""
    if hasattr(t, "strftime"):
        return t.strftime("%H:%M")
    return str(t)[:5]


def placement_conflicts(assignment, time_slot, room, exclude_entry_ids=()):
    """
    Return a list of rich, human-readable conflicts for placing ``assignment`` into
    ``(time_slot, room)``. An empty list means the placement is legal.
    Provides complete forensic details of who/what is occupying the slot.
    """
    conflicts = []
    excluded = tuple(exclude_entry_ids) or (None,)

    subject = assignment.subject
    division = assignment.division
    teacher = assignment.teacher

    # Rule 1: Room type must match subject type (Lab vs Classroom)
    if room.is_lab != subject.is_lab:
        required = "laboratory" if subject.is_lab else "theory classroom"
        actual = "Lab" if room.is_lab else "Classroom"
        conflicts.append(
            f"Lab / Classroom Mismatch: '{subject.name}' requires a {required}, but {room.name} is designated as a {actual} (wrong room type)."
        )

    # Rule 2: Room must seat the whole division
    if room.capacity < division.strength:
        conflicts.append(
            f"Capacity Deficit: {room.name} seats {room.capacity}, but class {division.name} (Year {division.year}) has {division.strength} students enrolled."
        )

    siblings = TimetableEntry.objects.filter(
        semester_id=assignment.semester_id
    ).exclude(id__in=excluded).select_related(
        "assignment__subject",
        "assignment__teacher",
        "assignment__division",
        "room",
        "time_slot",
    )

    # Rule 3: A room hosts one class per period
    room_occ = siblings.filter(time_slot=time_slot, room=room).first()
    if room_occ:
        r_subj = room_occ.assignment.subject.name
        r_teacher = room_occ.assignment.teacher.name
        r_div = room_occ.assignment.division.name
        r_year = room_occ.assignment.division.year
        conflicts.append(
            f"Classroom / Lab Conflict: {room.name} is already occupied at {time_slot.slot_code} "
            f"({_format_time(time_slot.start_time)}-{_format_time(time_slot.end_time)}) "
            f"for subject '{r_subj}' allocated to {r_teacher} for class {r_div} (Year {r_year})."
        )

    # Rule 4: A teacher teaches one class per period
    teacher_occ = siblings.filter(time_slot=time_slot, assignment__teacher_id=assignment.teacher_id).first()
    if teacher_occ:
        t_subj = teacher_occ.assignment.subject.name
        t_div = teacher_occ.assignment.division.name
        t_year = teacher_occ.assignment.division.year
        t_room = teacher_occ.room.name
        conflicts.append(
            f"Teacher Availability Conflict: Faculty {teacher.name} already teaches another class at {time_slot.slot_code} "
            f"({_format_time(time_slot.start_time)}-{_format_time(time_slot.end_time)}) — "
            f"conducting '{t_subj}' for class {t_div} (Year {t_year}) in room {t_room}."
        )

    # Rule 5: A division attends one class per period
    div_occ = siblings.filter(time_slot=time_slot, assignment__division_id=assignment.division_id).first()
    if div_occ:
        d_subj = div_occ.assignment.subject.name
        d_teacher = div_occ.assignment.teacher.name
        d_room = div_occ.room.name
        conflicts.append(
            f"Student Availability Conflict: Class {division.name} (Year {division.year}) already has a class at {time_slot.slot_code} — "
            f"attending '{d_subj}' with {d_teacher} in room {d_room}."
        )

    # Rule 6: Teacher unavailability is inviolable
    if TeacherUnavailability.objects.filter(
        teacher_id=assignment.teacher_id, time_slot=time_slot
    ).exists():
        conflicts.append(
            f"Teacher Unavailability: Faculty {teacher.name} is marked unavailable at {time_slot.slot_code} "
            f"({_format_time(time_slot.start_time)}-{_format_time(time_slot.end_time)})."
        )

    return conflicts


def find_suitable_room(assignment, time_slot, exclude_entry_ids=()):
    """
    Search for a legally available room for ``assignment`` at ``time_slot``.
    Matches room type (lab vs lecture), capacity, and checks occupancy.
    """
    subject = assignment.subject
    division = assignment.division
    excluded = tuple(exclude_entry_ids) or (None,)

    occupied_room_ids = set(
        TimetableEntry.objects.filter(
            semester_id=assignment.semester_id,
            time_slot=time_slot
        ).exclude(id__in=excluded).values_list("room_id", flat=True)
    )

    candidates = Room.objects.filter(
        is_lab=subject.is_lab,
        capacity__gte=division.strength
    ).exclude(id__in=occupied_room_ids).order_by("capacity")

    return candidates.first()


def _describe(entry):
    assignment = entry.assignment
    return (
        f"{assignment.division.name} | {assignment.subject.name} | "
        f"{assignment.teacher.name} @ {entry.time_slot.slot_code} in {entry.room.name}"
    )


@transaction.atomic
def move_entry(entry, new_slot, new_room=None, user=None):
    """
    Reschedule a single entry to ``new_slot`` / ``new_room``.
    If ``new_room`` is None, checks ``entry.room`` and auto-finds a legal alternative room if needed.
    If ``new_room`` is explicitly passed, respects the explicit choice and validates it strictly.
    Returns ``(entry, conflicts)``. On conflict nothing is written.
    """
    if new_room is not None:
        target_room = new_room
        conflicts = placement_conflicts(entry.assignment, new_slot, target_room, [entry.id])
    else:
        target_room = entry.room
        initial_conflicts = placement_conflicts(entry.assignment, new_slot, target_room, [entry.id])
        if initial_conflicts and any("already occupied" in c or "Mismatch" in c or "seats" in c for c in initial_conflicts):
            alt_room = find_suitable_room(entry.assignment, new_slot, [entry.id])
            if alt_room:
                target_room = alt_room
                conflicts = placement_conflicts(entry.assignment, new_slot, target_room, [entry.id])
            else:
                conflicts = initial_conflicts
        else:
            conflicts = initial_conflicts

    if conflicts:
        return entry, conflicts

    before = _describe(entry)
    entry.time_slot = new_slot
    entry.room = target_room
    entry.save(update_fields=["time_slot", "room"])
    after = _describe(entry)

    TimetableChangeLog.objects.create(
        semester_id=entry.semester_id,
        changed_by=user if (user and user.is_authenticated) else None,
        action="MOVE",
        detail=f"MOVED\nBefore: {before}\nAfter:  {after}",
    )
    return entry, []


@transaction.atomic
def swap_entries(entry_a, entry_b, user=None):
    """
    Exchange the positions of two entries.

    When both entries sit in the same period the rooms are exchanged; otherwise
    the time slots are exchanged and each entry keeps its room.

    Returns ``(True, [])`` on success or ``(False, conflicts)`` when the swap
    would break a rule. The two rows are removed and re-inserted with their
    original primary keys inside the transaction, so the
    ``(semester, time_slot, room)`` unique constraint is never transiently
    violated while the two rows exchange places.
    """
    if entry_a.pk == entry_b.pk:
        return False, ["Select two different timetable entries to swap."]

    if entry_a.semester_id != entry_b.semester_id:
        return False, ["Both entries must belong to the same semester."]

    same_period = entry_a.time_slot_id == entry_b.time_slot_id
    if same_period:
        target_for_a = (entry_a.time_slot, entry_b.room)
        target_for_b = (entry_b.time_slot, entry_a.room)
    else:
        target_for_a = (entry_b.time_slot, entry_a.room)
        target_for_b = (entry_a.time_slot, entry_b.room)

    excluded = [entry_a.pk, entry_b.pk]
    conflicts = placement_conflicts(
        entry_a.assignment, target_for_a[0], target_for_a[1], excluded
    )
    conflicts += placement_conflicts(
        entry_b.assignment, target_for_b[0], target_for_b[1], excluded
    )
    if conflicts:
        return False, conflicts

    before = f"A: {_describe(entry_a)}\nB: {_describe(entry_b)}"
    original_pks = (entry_a.pk, entry_b.pk)
    rebuilt = [
        TimetableEntry(
            pk=original_pks[0],
            semester_id=entry_a.semester_id,
            assignment_id=entry_a.assignment_id,
            room_id=target_for_a[1].id,
            time_slot_id=target_for_a[0].id,
        ),
        TimetableEntry(
            pk=original_pks[1],
            semester_id=entry_b.semester_id,
            assignment_id=entry_b.assignment_id,
            room_id=target_for_b[1].id,
            time_slot_id=target_for_b[0].id,
        ),
    ]

    TimetableEntry.objects.filter(pk__in=original_pks).delete()
    TimetableEntry.objects.bulk_create(rebuilt)

    after = "\n".join(["A: " + _describe(rebuilt[0]), "B: " + _describe(rebuilt[1])])
    TimetableChangeLog.objects.create(
        semester_id=entry_a.semester_id,
        changed_by=user if (user and user.is_authenticated) else None,
        action="SWAP",
        detail=f"SWAPPED\nBefore:\n{before}\nAfter:\n{after}",
    )
    return True, []


@transaction.atomic
def delete_entry(entry, user=None):
    """Remove a single entry from the timetable and record the change."""
    description = _describe(entry)
    semester_id = entry.semester_id
    TimetableEntry.objects.filter(pk=entry.pk).delete()

    TimetableChangeLog.objects.create(
        semester_id=semester_id,
        changed_by=user if (user and user.is_authenticated) else None,
        action="DELETE",
        detail=f"REMOVED\n{description}",
    )


def available_rooms_for(assignment):
    """Rooms that legally satisfy the room-type and capacity rules for an assignment."""
    return [
        room
        for room in Room.objects.all()
        if room.is_lab == assignment.subject.is_lab and room.capacity >= assignment.division.strength
    ]
