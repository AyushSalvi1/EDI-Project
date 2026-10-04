"""
Manual timetable editing operations used by administrators.

Every manual change is re-validated against the same rules the CP-SAT solver
enforces (room type, room capacity, room occupancy, teacher concurrency, division
concurrency and teacher unavailability) before anything is written, so an admin
can never introduce a conflict that the solver would have prevented.
"""

from django.db import transaction

from .models import (
    Room,
    TeacherUnavailability,
    TimetableChangeLog,
    TimetableEntry,
)


def placement_conflicts(assignment, time_slot, room, exclude_entry_ids=()):
    """
    Return a list of human-readable conflicts for placing ``assignment`` into
    ``(time_slot, room)``. An empty list means the placement is legal.
    """
    conflicts = []
    excluded = tuple(exclude_entry_ids) or (None,)

    subject = assignment.subject
    division = assignment.division
    teacher = assignment.teacher

    # Rule: room type must match subject type.
    if room.is_lab != subject.is_lab:
        required = "laboratory" if subject.is_lab else "theory classroom"
        conflicts.append(
            f"'{subject.name}' needs a {required}, but {room.name} is the wrong room type."
        )

    # Rule: room must seat the whole division.
    if room.capacity < division.strength:
        conflicts.append(
            f"{room.name} seats {room.capacity}, but {division.name} has {division.strength} students."
        )

    siblings = TimetableEntry.objects.filter(semester_id=assignment.semester_id).exclude(
        id__in=excluded
    )

    # Rule: a room hosts one class per period.
    if siblings.filter(time_slot=time_slot, room=room).exists():
        conflicts.append(f"{room.name} is already occupied at {time_slot.slot_code}.")

    # Rule: a teacher teaches one class per period.
    if siblings.filter(time_slot=time_slot, assignment__teacher_id=assignment.teacher_id).exists():
        conflicts.append(
            f"{teacher.name} already teaches another class at {time_slot.slot_code}."
        )

    # Rule: a division attends one class per period.
    if siblings.filter(time_slot=time_slot, assignment__division_id=assignment.division_id).exists():
        conflicts.append(
            f"{division.name} already has a class at {time_slot.slot_code}."
        )

    # Rule: teacher unavailability is inviolable.
    if TeacherUnavailability.objects.filter(
        teacher_id=assignment.teacher_id, time_slot=time_slot
    ).exists():
        conflicts.append(
            f"{teacher.name} is marked unavailable at {time_slot.slot_code}."
        )

    return conflicts


def _describe(entry):
    assignment = entry.assignment
    return (
        f"{assignment.division.name} | {assignment.subject.name} | "
        f"{assignment.teacher.name} @ {entry.time_slot.slot_code} in {entry.room.name}"
    )


@transaction.atomic
def move_entry(entry, new_slot, new_room, user=None):
    """
    Reschedule a single entry to ``new_slot`` / ``new_room``.

    Returns ``(entry, conflicts)``. On conflict nothing is written.
    """
    conflicts = placement_conflicts(entry.assignment, new_slot, new_room, [entry.id])
    if conflicts:
        return entry, conflicts

    before = _describe(entry)
    entry.time_slot = new_slot
    entry.room = new_room
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